"""Offline Pixii MQTT scheduler command models and ACK checks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Any

from ..allocator import normalize_message_id, serialize_message_id
from ..exceptions import CommandValidationError
from .commands import CommandMethod
from .models import MqttAck
from .scheduler_values import MANUAL_SCHEDULER_SERVICES, validate_scheduler_values

MAX_SCHEDULER_DURATION_SECONDS = 2**32 - 1
SETTABLE_SCHEDULER_SERVICES = MANUAL_SCHEDULER_SERVICES | frozenset({"voltagesupport"})
GET_ONLY_SERVICES = frozenset({"runningservice"})
SUPPORTED_SCHEDULER_SERVICES = SETTABLE_SCHEDULER_SERVICES | GET_ONLY_SERVICES
SCHEDULER_SERVICES = SETTABLE_SCHEDULER_SERVICES


class SchedulerAckStatus(StrEnum):
    """Offline scheduler ACK classification."""

    SUCCESS = "success"
    REJECTED = "rejected"
    MISMATCH = "mismatch"


def _service_name(value: str) -> str:
    if not isinstance(value, str):
        raise CommandValidationError("scheduler service must be a string")
    service = value.strip().lower()
    if service not in SETTABLE_SCHEDULER_SERVICES:
        raise CommandValidationError("scheduler service is not SET-capable")
    return service


def _expected_service_name(value: str) -> str:
    if not isinstance(value, str):
        raise CommandValidationError("expected scheduler service must be a string")
    service = value.strip().lower()
    if service not in SUPPORTED_SCHEDULER_SERVICES:
        raise CommandValidationError("unknown expected scheduler service")
    return service


def _target_soc_values(
    value: Mapping[str, Any] | None,
    *,
    required: bool,
) -> dict[str, Any] | None:
    if value is None:
        if required:
            raise CommandValidationError("targetsoc values are required")
        return None
    if not isinstance(value, Mapping):
        raise CommandValidationError("targetsoc values must be an object")
    copied = dict(value)
    if set(copied) != {"socref"}:
        raise CommandValidationError("targetsoc values must contain only socref")
    socref = copied["socref"]
    if (
        isinstance(socref, bool)
        or not isinstance(socref, (int, float))
        or not isfinite(float(socref))
        or not 0 <= socref <= 100
    ):
        raise CommandValidationError("targetsoc socref must be a finite number from 0 through 100")
    return copied


def _set_values(
    service: str,
    value: Mapping[str, Any] | None,
    *,
    required: bool,
) -> dict[str, Any] | None:
    if service == "targetsoc":
        return _target_soc_values(value, required=required)
    if service in MANUAL_SCHEDULER_SERVICES:
        if value is None and not required:
            return None
        return validate_scheduler_values(service, value)
    if service == "voltagesupport":
        if value is not None:
            raise CommandValidationError("voltagesupport scheduled SET must omit values")
        return None
    raise CommandValidationError("scheduler service schema is not implemented")


def _duration(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommandValidationError("scheduler duration must be an integer")
    if not 0 <= value <= MAX_SCHEDULER_DURATION_SECONDS:
        raise CommandValidationError("scheduler duration is outside uint32 seconds")
    return value


def _create_duration(value: int) -> int:
    duration = _duration(value)
    if duration == 0:
        raise CommandValidationError("scheduler CREATE duration must be greater than zero")
    return duration


def normalize_scheduler_starttime(
    value: datetime,
    *,
    now: datetime | None = None,
) -> str:
    """Convert one aware future datetime to canonical UTC ISO-8601 wire text."""
    if not isinstance(value, datetime):
        raise CommandValidationError("scheduler starttime must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise CommandValidationError("scheduler starttime must be timezone-aware")
    current = now or datetime.now(UTC)
    if current.tzinfo is None or current.utcoffset() is None:
        raise CommandValidationError("scheduler comparison time must be timezone-aware")
    utc_value = value.astimezone(UTC).replace(microsecond=0)
    current_utc = current.astimezone(UTC).replace(microsecond=0)
    if utc_value <= current_utc:
        raise CommandValidationError("scheduler starttime must be in the future")
    return utc_value.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class SchedulerCommand:
    """Validated scheduler command envelope with no transport side effect."""

    method: CommandMethod
    m_id: int
    service: str | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    expected_services: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.method, CommandMethod):
            raise CommandValidationError("scheduler method must be a CommandMethod")
        object.__setattr__(self, "m_id", normalize_message_id(self.m_id))
        normalized_service = None if self.service is None else _service_name(self.service)
        if self.method is CommandMethod.SET and normalized_service is None:
            raise CommandValidationError("scheduler set requires a service")
        if self.method is not CommandMethod.SET and normalized_service is not None:
            raise CommandValidationError("scheduler service is not used for this method")
        if not isinstance(self.fields, Mapping):
            raise CommandValidationError("scheduler command fields must be an object")
        expected_services = (
            tuple(_expected_service_name(service) for service in self.expected_services)
            if self.expected_services
            else ((normalized_service,) if normalized_service is not None else ())
        )
        object.__setattr__(self, "service", normalized_service)
        object.__setattr__(self, "fields", dict(self.fields))
        object.__setattr__(self, "expected_services", expected_services)

    @property
    def state_changing(self) -> bool:
        """Return whether the scheduler command changes Gateway state."""
        return self.method is not CommandMethod.GET

    def to_payload(self) -> dict[str, Any]:
        """Return the documented control envelope without publishing it."""
        payload: dict[str, Any] = {
            "method": self.method.value,
            "m_id": serialize_message_id(self.m_id),
        }
        if self.service is not None:
            payload["service"] = self.service
        payload.update(dict(self.fields))
        return payload


@dataclass(frozen=True, slots=True)
class ScheduledCreate:
    """Build a future scheduled service using method=set."""

    service: str
    m_id: int
    starttime: datetime
    duration_seconds: int
    values: Mapping[str, Any] | None = None

    def build(self, *, now: datetime | None = None) -> SchedulerCommand:
        service = _service_name(self.service)
        values = _set_values(service, self.values, required=True)
        fields: dict[str, Any] = {
            "starttime": normalize_scheduler_starttime(self.starttime, now=now),
            "duration": _create_duration(self.duration_seconds),
        }
        if values is not None:
            fields["values"] = values
        return SchedulerCommand(
            method=CommandMethod.SET,
            m_id=self.m_id,
            service=service,
            fields=fields,
        )


@dataclass(frozen=True, slots=True)
class ScheduledUpdate:
    """Build a scheduler update using the existing integration-owned m_id."""

    m_id: int
    values: Mapping[str, Any] | None = None
    duration_seconds: int | None = None
    service: str | None = None

    def build(self) -> SchedulerCommand:
        service = _service_name(self.service) if self.service is not None else None
        if service is None:
            raise CommandValidationError("scheduler update requires a SET-capable service")
        fields: dict[str, Any] = {}
        values = _set_values(service, self.values, required=False)
        if values is not None:
            fields["values"] = values
        if self.duration_seconds is not None:
            fields["duration"] = _duration(self.duration_seconds)
        if not fields:
            raise CommandValidationError("scheduler update must change values or duration")
        return SchedulerCommand(
            method=CommandMethod.SET,
            m_id=self.m_id,
            service=service,
            fields=fields,
        )


@dataclass(frozen=True, slots=True)
class ScheduledPriceStop:
    """Remove one exact price identity using documented keyed DELETE.

    No SET, zero-length replacement schedule or global service-off request.
    Physical cessation still needs independent verification by the caller.
    """

    m_id: int
    service: str

    def build(self) -> SchedulerCommand:
        if self.service not in {"peakshaving", "demandresponse"}:
            raise CommandValidationError("price stop requires a price service")
        return ScheduledDelete(self.m_id, expected_service=self.service).build()


@dataclass(frozen=True, slots=True)
class ScheduledDelete:
    """Build a scheduler delete using an existing integration-owned m_id."""

    m_id: int
    expected_service: str | None = None

    def build(self) -> SchedulerCommand:
        expected_services = (
            (_expected_service_name(self.expected_service),)
            if self.expected_service is not None
            else ()
        )
        return SchedulerCommand(
            method=CommandMethod.DELETE,
            m_id=self.m_id,
            expected_services=expected_services,
        )


@dataclass(frozen=True, slots=True)
class ScheduledGet:
    """Build a scheduler GET verification command by m_id."""

    m_id: int
    expected_service: str | None = None

    def build(self) -> SchedulerCommand:
        expected_services = (
            (_expected_service_name(self.expected_service),)
            if self.expected_service is not None
            else ()
        )
        return SchedulerCommand(
            method=CommandMethod.GET,
            m_id=self.m_id,
            expected_services=expected_services,
        )


@dataclass(frozen=True, slots=True)
class SchedulerAckResult:
    """Pure scheduler ACK result used before read-after-write reconciliation."""

    status: SchedulerAckStatus
    reason: str
    response: int
    m_id: int | None
    method: str
    service: str

    @property
    def successful(self) -> bool:
        return self.status is SchedulerAckStatus.SUCCESS


def evaluate_scheduler_ack(
    command: SchedulerCommand,
    ack: MqttAck,
) -> SchedulerAckResult:
    """Require m_id, method, and applicable service agreement before success."""
    try:
        ack_m_id = normalize_message_id(ack.m_id) if ack.m_id is not None else None
    except CommandValidationError:
        ack_m_id = None
    if ack_m_id != command.m_id:
        return SchedulerAckResult(
            SchedulerAckStatus.MISMATCH,
            "ack_m_id_mismatch",
            ack.response,
            ack_m_id,
            ack.method,
            ack.service,
        )
    if ack.method.strip().lower() != command.method.value:
        return SchedulerAckResult(
            SchedulerAckStatus.MISMATCH,
            "ack_method_mismatch",
            ack.response,
            ack_m_id,
            ack.method,
            ack.service,
        )
    if command.expected_services and ack.service.strip().lower() not in command.expected_services:
        return SchedulerAckResult(
            SchedulerAckStatus.MISMATCH,
            "ack_service_mismatch",
            ack.response,
            ack_m_id,
            ack.method,
            ack.service,
        )
    status = SchedulerAckStatus.SUCCESS if ack.response == 65535 else SchedulerAckStatus.REJECTED
    return SchedulerAckResult(
        status,
        "ack_success" if status is SchedulerAckStatus.SUCCESS else "gateway_rejected",
        ack.response,
        ack_m_id,
        ack.method,
        ack.service,
    )


__all__ = [
    "GET_ONLY_SERVICES",
    "MAX_SCHEDULER_DURATION_SECONDS",
    "SCHEDULER_SERVICES",
    "SETTABLE_SCHEDULER_SERVICES",
    "SUPPORTED_SCHEDULER_SERVICES",
    "ScheduledCreate",
    "ScheduledDelete",
    "ScheduledGet",
    "ScheduledUpdate",
    "SchedulerAckResult",
    "SchedulerAckStatus",
    "SchedulerCommand",
    "evaluate_scheduler_ack",
    "normalize_scheduler_starttime",
]
