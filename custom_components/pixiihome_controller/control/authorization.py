"""Scoped manual grants and bounded automation policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from secrets import token_urlsafe

from .models import ControlOperation, ScheduleOrigin

MAX_MANUAL_GRANTS = 32


@dataclass(slots=True)
class ManualActionGrant:
    """One-use authorization bound to one observed target and preview revision."""

    grant_id: str
    config_entry_id: str
    gateway_serial: str
    operation: ControlOperation
    target_identity: str
    fingerprint: str
    preview_revision: str
    issued_at: datetime
    expires_at: datetime
    used: bool = False

    @classmethod
    def issue(
        cls,
        *,
        config_entry_id: str,
        gateway_serial: str,
        operation: ControlOperation,
        target_identity: str,
        fingerprint: str,
        preview_revision: str,
        now: datetime | None = None,
        ttl: timedelta = timedelta(minutes=5),
    ) -> ManualActionGrant:
        issued_at = (now or datetime.now(UTC)).astimezone(UTC)
        values = (
            config_entry_id,
            gateway_serial,
            target_identity,
            fingerprint,
            preview_revision,
        )
        if any(not isinstance(value, str) or not value.strip() for value in values):
            raise ValueError("manual grant scope must be non-empty")
        if ttl <= timedelta(0) or ttl > timedelta(minutes=15):
            raise ValueError("manual grant TTL must be positive and at most 15 minutes")
        return cls(
            grant_id=token_urlsafe(24),
            config_entry_id=config_entry_id.strip(),
            gateway_serial=gateway_serial.strip(),
            operation=ControlOperation(operation),
            target_identity=target_identity.strip(),
            fingerprint=fingerprint.strip(),
            preview_revision=preview_revision.strip(),
            issued_at=issued_at,
            expires_at=issued_at + ttl,
        )

    def authorize(
        self,
        *,
        config_entry_id: str,
        gateway_serial: str,
        operation: ControlOperation,
        target_identity: str,
        fingerprint: str,
        preview_revision: str,
        now: datetime | None = None,
    ) -> bool:
        checked_at = (now or datetime.now(UTC)).astimezone(UTC)
        matches = (
            not self.used
            and checked_at <= self.expires_at
            and config_entry_id == self.config_entry_id
            and gateway_serial == self.gateway_serial
            and ControlOperation(operation) is self.operation
            and target_identity == self.target_identity
            and fingerprint == self.fingerprint
            and preview_revision == self.preview_revision
        )
        if matches:
            self.used = True
        return matches


@dataclass(slots=True)
class GrantRegistry:
    """In-memory grants; a restart invalidates every issued grant."""

    _grants: dict[str, ManualActionGrant] = field(default_factory=dict)

    def issue(self, **scope: object) -> ManualActionGrant:
        grant = ManualActionGrant.issue(**scope)  # type: ignore[arg-type]
        self._grants[grant.grant_id] = grant
        while len(self._grants) > MAX_MANUAL_GRANTS:
            self._grants.pop(next(iter(self._grants)))
        return grant

    def authorize(self, grant_id: str, **scope: object) -> bool:
        grant = self._grants.get(grant_id)
        if grant is None:
            return False
        authorized = grant.authorize(**scope)  # type: ignore[arg-type]
        if authorized or grant.used:
            self._grants.pop(grant_id, None)
        return authorized

    def discard(self, grant_id: str | None) -> None:
        if grant_id is not None:
            self._grants.pop(grant_id, None)

    def clear(self) -> None:
        self._grants.clear()


@dataclass(frozen=True, slots=True)
class AutomationPolicy:
    """Explicit bounds for automatic control; external mutation is denied by default."""

    services: frozenset[str]
    operations: frozenset[ControlOperation]
    start_hour_utc: int = 0
    end_hour_utc: int = 24
    max_duration_seconds: int = 24 * 60 * 60
    min_soc: float = 0.0
    max_soc: float = 100.0
    max_abs_power_w: float = 0.0
    allow_external: bool = False

    def __post_init__(self) -> None:
        normalized_services = frozenset(service.strip().lower() for service in self.services)
        normalized_operations = frozenset(ControlOperation(value) for value in self.operations)
        object.__setattr__(self, "services", normalized_services)
        object.__setattr__(self, "operations", normalized_operations)
        if not normalized_services or not normalized_operations:
            raise ValueError("automation policy requires services and operations")
        if not 0 <= self.start_hour_utc <= 23 or not 1 <= self.end_hour_utc <= 24:
            raise ValueError("automation policy hours are invalid")
        if self.max_duration_seconds <= 0:
            raise ValueError("automation policy duration must be positive")
        if not 0 <= self.min_soc <= self.max_soc <= 100:
            raise ValueError("automation policy SoC bounds are invalid")
        if not isfinite(self.max_abs_power_w) or self.max_abs_power_w < 0:
            raise ValueError("automation policy power bound is invalid")

    def allows(
        self,
        *,
        operation: ControlOperation,
        service: str,
        start: datetime,
        duration_seconds: int,
        soc: float | None = None,
        power_w: float | None = None,
        origin: ScheduleOrigin = ScheduleOrigin.LOCAL_CREATED,
    ) -> bool:
        start_utc = start.astimezone(UTC)
        if service.strip().lower() not in self.services:
            return False
        if ControlOperation(operation) not in self.operations:
            return False
        if duration_seconds <= 0 or duration_seconds > self.max_duration_seconds:
            return False
        if not self.start_hour_utc <= start_utc.hour < self.end_hour_utc:
            return False
        if soc is not None and not self.min_soc <= soc <= self.max_soc:
            return False
        if power_w is not None and abs(power_w) > self.max_abs_power_w:
            return False
        return origin is not ScheduleOrigin.EXTERNAL_OBSERVED or self.allow_external
