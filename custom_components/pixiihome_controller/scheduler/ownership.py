"""Offline Pixii MQTT scheduler ownership records."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from ..allocator import normalize_message_id
from ..exceptions import CommandValidationError
from ..protocol.scheduler_commands import (
    SETTABLE_SCHEDULER_SERVICES,
    ScheduledUpdate,
    SchedulerAckResult,
)
from ..protocol.scheduler_readback import (
    RECONCILABLE_SCHEDULER_SERVICES,
    SchedulerExpectedState,
    SchedulerReconciliationResult,
    SchedulerReconciliationStatus,
    apply_scheduler_update,
)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    if isinstance(value, frozenset):
        return [_thaw(item) for item in value]
    return deepcopy(value)


class OwnershipLifecycle(StrEnum):
    """Lifecycle states for locally persisted scheduler ownership."""

    PROVISIONAL = "provisional"
    OWNED = "owned"
    ORPHANED = "orphaned"
    CONFLICTED = "conflicted"
    STOPPED_RESERVED = "stopped_reserved"
    EXPIRED_RESERVED = "expired_reserved"


class SchedulerMutationOperation(StrEnum):
    """Offline mutation stages that may reconcile explicit ownership."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class SchedulerMutationEvidence:
    """Immutable operation-bound ACK, expected state, and keyed readback."""

    operation: SchedulerMutationOperation
    m_id: int
    service: str
    mutation_ack: SchedulerAckResult
    expected_state: SchedulerExpectedState | None
    readback: SchedulerReconciliationResult

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", SchedulerMutationOperation(self.operation))
        object.__setattr__(self, "m_id", normalize_message_id(self.m_id))
        if (
            not isinstance(self.service, str)
            or self.service.strip().lower() not in RECONCILABLE_SCHEDULER_SERVICES
        ):
            raise CommandValidationError(
                "scheduler evidence requires a reconcilable scheduled service"
            )
        object.__setattr__(self, "service", self.service.strip().lower())
        if not isinstance(self.mutation_ack, SchedulerAckResult):
            raise CommandValidationError("scheduler evidence requires a typed mutation ACK")
        if self.operation is SchedulerMutationOperation.DELETE:
            if self.expected_state is not None:
                raise CommandValidationError("DELETE evidence must not carry expected state")
        elif not isinstance(self.expected_state, SchedulerExpectedState):
            raise CommandValidationError(
                "CREATE and UPDATE evidence require complete expected state"
            )
        if not isinstance(self.readback, SchedulerReconciliationResult):
            raise CommandValidationError("scheduler evidence requires typed readback result")

    @property
    def expected_ack_method(self) -> str:
        """Return the protocol method required for this operation."""
        return "delete" if self.operation is SchedulerMutationOperation.DELETE else "set"

    def matches_record(self, record: SchedulerOwnershipRecord) -> bool:
        """Require ACK identity, method, service, and operation agreement."""
        return (
            self.m_id == record.m_id
            and self.service == record.service
            and self.mutation_ack.successful
            and self.mutation_ack.m_id == record.m_id
            and isinstance(self.mutation_ack.method, str)
            and self.mutation_ack.method.strip().lower() == self.expected_ack_method
            and isinstance(self.mutation_ack.service, str)
            and self.mutation_ack.service.strip().lower() == record.service
        )


@dataclass(frozen=True, slots=True)
class SchedulerOwnershipRecord:
    """One explicit ownership claim; no automatic adoption is supported."""

    config_entry_id: str
    gateway_serial: str
    m_id: int
    service: str
    intended_state: Mapping[str, Any]
    lifecycle: OwnershipLifecycle = OwnershipLifecycle.PROVISIONAL
    owner: str | None = None
    reservation_until: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.config_entry_id, str) or not self.config_entry_id.strip():
            raise CommandValidationError("scheduler ownership requires a config entry ID")
        if not isinstance(self.gateway_serial, str) or not self.gateway_serial.strip():
            raise CommandValidationError("scheduler ownership requires a Gateway serial")
        if (
            not isinstance(self.service, str)
            or self.service.strip().lower() not in SETTABLE_SCHEDULER_SERVICES
        ):
            raise CommandValidationError("scheduler ownership requires a SET-capable service")
        if not isinstance(self.intended_state, Mapping) or not self.intended_state:
            raise CommandValidationError("scheduler ownership requires intended state")
        object.__setattr__(self, "config_entry_id", self.config_entry_id.strip())
        object.__setattr__(self, "gateway_serial", self.gateway_serial.strip())
        object.__setattr__(self, "m_id", normalize_message_id(self.m_id))
        object.__setattr__(self, "service", self.service.strip().lower())
        object.__setattr__(self, "intended_state", _freeze(self.intended_state))
        if not isinstance(self.lifecycle, OwnershipLifecycle):
            object.__setattr__(self, "lifecycle", OwnershipLifecycle(self.lifecycle))
        if self.owner is not None:
            if not isinstance(self.owner, str) or not self.owner.strip():
                raise CommandValidationError("scheduler ownership owner is invalid")
            object.__setattr__(self, "owner", self.owner.strip().lower())
        if self.reservation_until is not None:
            try:
                reserved = datetime.fromisoformat(self.reservation_until)
                if reserved.tzinfo is None:
                    raise ValueError("reservation must be timezone-aware")
            except (TypeError, ValueError) as err:
                raise CommandValidationError("scheduler reservation deadline is invalid") from err
            object.__setattr__(self, "reservation_until", reserved.astimezone(UTC).isoformat())
        if self.lifecycle is OwnershipLifecycle.STOPPED_RESERVED and self.reservation_until is None:
            raise CommandValidationError("stopped reservation requires a deadline")

    def as_dict(self) -> dict[str, Any]:
        """Return JSON-safe persisted ownership state."""
        result = {
            "config_entry_id": self.config_entry_id,
            "gateway_serial": self.gateway_serial,
            "m_id": self.m_id,
            "service": self.service,
            "intended_state": _thaw(self.intended_state),
            "lifecycle": self.lifecycle.value,
        }
        if self.owner is not None:
            result["owner"] = self.owner
        if self.reservation_until is not None:
            result["reservation_until"] = self.reservation_until
        return result

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SchedulerOwnershipRecord:
        if not isinstance(value, Mapping):
            raise CommandValidationError("scheduler ownership record must be an object")
        try:
            lifecycle = OwnershipLifecycle(value["lifecycle"])
            return cls(
                config_entry_id=value["config_entry_id"],
                gateway_serial=value["gateway_serial"],
                m_id=value["m_id"],
                service=value["service"],
                intended_state=value["intended_state"],
                lifecycle=lifecycle,
                owner=value.get("owner"),
                reservation_until=value.get("reservation_until"),
            )
        except KeyError as err:
            raise CommandValidationError(
                f"scheduler ownership record is missing {err.args[0]}"
            ) from err
        except (TypeError, ValueError) as err:
            raise CommandValidationError("scheduler ownership lifecycle is invalid") from err


@dataclass(slots=True)
class SchedulerOwnershipStore:
    """Store records inside one explicit config-entry and Gateway scope."""

    config_entry_id: str
    gateway_serial: str
    records: dict[int, SchedulerOwnershipRecord] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.config_entry_id, str) or not self.config_entry_id.strip():
            raise CommandValidationError("scheduler ownership requires a config entry ID")
        if not isinstance(self.gateway_serial, str) or not self.gateway_serial.strip():
            raise CommandValidationError("scheduler ownership requires a Gateway serial")
        self.config_entry_id = self.config_entry_id.strip()
        self.gateway_serial = self.gateway_serial.strip()

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, Any] | None,
        *,
        config_entry_id: str,
        gateway_serial: str,
    ) -> SchedulerOwnershipStore:
        store = cls(config_entry_id=config_entry_id, gateway_serial=gateway_serial)
        if state is None:
            return store
        if not isinstance(state, Mapping):
            raise CommandValidationError("scheduler ownership state must be an object")
        if (
            state.get("config_entry_id") != store.config_entry_id
            or state.get("gateway_serial") != store.gateway_serial
        ):
            raise CommandValidationError("scheduler ownership state scope does not match")
        raw_records = state.get("records", [])
        if not isinstance(raw_records, list):
            raise CommandValidationError("scheduler ownership records must be a list")
        for raw_record in raw_records:
            store.register(SchedulerOwnershipRecord.from_mapping(raw_record))
        return store

    def snapshot(self) -> dict[str, Any]:
        """Return deterministic JSON-safe state for config-entry persistence."""
        return {
            "config_entry_id": self.config_entry_id,
            "gateway_serial": self.gateway_serial,
            "records": [self.records[m_id].as_dict() for m_id in sorted(self.records)],
        }
    def lifecycle_counts(self) -> dict[str, int]:
        """Return lifecycle counts without exposing record identities."""
        counts = {lifecycle.value: 0 for lifecycle in OwnershipLifecycle}
        for record in self.records.values():
            counts[record.lifecycle.value] += 1
        return counts

    def has_unresolved_records(self) -> bool:
        """Return whether CREATE validation must stop for unresolved records."""
        return any(
            record.lifecycle
            in {OwnershipLifecycle.PROVISIONAL, OwnershipLifecycle.CONFLICTED}
            for record in self.records.values()
        )

    def validation_blockers(self) -> tuple[str, ...]:
        """Return safe blockers for the first CREATE validation."""
        blockers: list[str] = []
        counts = self.lifecycle_counts()
        if counts[OwnershipLifecycle.PROVISIONAL.value]:
            blockers.append("ownership_provisional_records")
        if counts[OwnershipLifecycle.CONFLICTED.value]:
            blockers.append("ownership_conflicted_records")
        return tuple(blockers)


    def snapshot_with_registered(self, record: SchedulerOwnershipRecord) -> dict[str, Any]:
        """Build a registration snapshot without changing this live store."""
        self._validate_record_scope(record)
        existing = self.records.get(record.m_id)
        if existing is not None and existing != record:
            raise CommandValidationError("scheduler m_id already has a different ownership record")
        records = dict(self.records)
        records[record.m_id] = record
        return self._snapshot_for_records(records)

    def snapshot_with_replacement(self, record: SchedulerOwnershipRecord) -> dict[str, Any]:
        """Build a replacement snapshot without changing this live store."""
        self._validate_record_scope(record)
        if record.m_id not in self.records:
            raise CommandValidationError("scheduler ownership record does not exist")
        records = dict(self.records)
        records[record.m_id] = record
        return self._snapshot_for_records(records)

    def _validate_record_scope(self, record: SchedulerOwnershipRecord) -> None:
        if (
            record.config_entry_id != self.config_entry_id
            or record.gateway_serial != self.gateway_serial
        ):
            raise CommandValidationError("scheduler ownership record scope does not match")

    def _snapshot_for_records(
        self,
        records: Mapping[int, SchedulerOwnershipRecord],
    ) -> dict[str, Any]:
        return {
            "config_entry_id": self.config_entry_id,
            "gateway_serial": self.gateway_serial,
            "records": [records[m_id].as_dict() for m_id in sorted(records)],
        }

    def register(self, record: SchedulerOwnershipRecord) -> None:
        if (
            record.config_entry_id != self.config_entry_id
            or record.gateway_serial != self.gateway_serial
        ):
            raise CommandValidationError("scheduler ownership record scope does not match")
        existing = self.records.get(record.m_id)
        if existing is not None and existing != record:
            raise CommandValidationError("scheduler m_id already has a different ownership record")
        self.records[record.m_id] = record

    def replace(self, record: SchedulerOwnershipRecord) -> None:
        """Replace one existing scoped record after an explicit transition."""
        if (
            record.config_entry_id != self.config_entry_id
            or record.gateway_serial != self.gateway_serial
        ):
            raise CommandValidationError("scheduler ownership record scope does not match")
        if record.m_id not in self.records:
            raise CommandValidationError("scheduler ownership record does not exist")
        self.records[record.m_id] = record

    def get(self, m_id: int) -> SchedulerOwnershipRecord | None:
        """Return a record only when it was explicitly persisted by this store."""
        return self.records.get(normalize_message_id(m_id))

    def require_owned(
        self,
        m_id: int,
        *,
        config_entry_id: str | None = None,
        gateway_serial: str | None = None,
    ) -> SchedulerOwnershipRecord:
        """Fail closed for missing, mismatched, or non-owned records."""
        if (config_entry_id is not None and config_entry_id != self.config_entry_id) or (
            gateway_serial is not None and gateway_serial != self.gateway_serial
        ):
            raise CommandValidationError("scheduler ownership scope does not match")
        record = self.get(m_id)
        if record is None or record.lifecycle is not OwnershipLifecycle.OWNED:
            raise CommandValidationError("scheduler m_id is not an active integration-owned record")
        return record

    def mark_orphaned(self, m_id: int) -> None:
        record = self.records.get(normalize_message_id(m_id))
        if record is not None:
            self.records[record.m_id] = SchedulerOwnershipRecord(
                **{
                    **record.as_dict(),
                    "lifecycle": OwnershipLifecycle.ORPHANED,
                }
            )

    def mark_conflicted(self, m_id: int) -> None:
        record = self.records.get(normalize_message_id(m_id))
        if record is not None:
            self.records[record.m_id] = SchedulerOwnershipRecord(
                **{
                    **record.as_dict(),
                    "lifecycle": OwnershipLifecycle.CONFLICTED,
                }
            )


def scheduler_expected_state_from_record(
    record: SchedulerOwnershipRecord,
) -> SchedulerExpectedState:
    """Require an ownership record to carry complete scheduled-service expected state."""
    state = record.intended_state
    try:
        return SchedulerExpectedState(
            m_id=record.m_id,
            service=record.service,
            socref=state["socref"] if record.service == "targetsoc" else None,
            values=state.get("values"),
            starttime=state["starttime"],
            duration=state["duration"],
        )
    except KeyError as err:
        raise CommandValidationError(
            f"scheduler ownership intended state is missing {err.args[0]}"
        ) from err


def merge_owned_scheduler_update(
    record: SchedulerOwnershipRecord,
    update: ScheduledUpdate,
) -> SchedulerOwnershipRecord:
    """Apply a validated partial update while preserving full expected state."""
    expected = scheduler_expected_state_from_record(record)
    merged = apply_scheduler_update(expected, update.build())
    return replace(record, intended_state=merged.as_mapping())


def _matches_expected_state(
    record: SchedulerOwnershipRecord,
    expected: SchedulerExpectedState,
    readback: SchedulerReconciliationResult,
    *,
    require_record_state: bool,
) -> bool:
    observed = readback.readback
    return (
        expected.m_id == record.m_id
        and expected.service == record.service
        and readback.status is SchedulerReconciliationStatus.MATCH
        and readback.subject_m_id == record.m_id
        and readback.subject_service == record.service
        and observed is not None
        and observed.m_id == record.m_id
        and observed.service == record.service
        and observed.as_mapping() == expected.as_mapping()
        and (not require_record_state or dict(record.intended_state) == expected.as_mapping())
    )


def apply_scheduler_ownership_reconciliation(
    record: SchedulerOwnershipRecord,
    evidence: SchedulerMutationEvidence,
) -> SchedulerOwnershipRecord:
    """Advance ownership only after internally consistent ACK and readback."""
    if not evidence.matches_record(record):
        return replace(record, lifecycle=OwnershipLifecycle.CONFLICTED)
    expected = evidence.expected_state
    if evidence.operation is SchedulerMutationOperation.CREATE:
        if (
            expected is not None
            and record.lifecycle is OwnershipLifecycle.PROVISIONAL
            and _matches_expected_state(
                record,
                expected,
                evidence.readback,
                require_record_state=True,
            )
        ):
            return replace(
                record,
                lifecycle=OwnershipLifecycle.OWNED,
                intended_state=expected.as_mapping(),
            )
        return replace(record, lifecycle=OwnershipLifecycle.CONFLICTED)
    if evidence.operation is SchedulerMutationOperation.UPDATE:
        if (
            expected is not None
            and record.lifecycle is OwnershipLifecycle.OWNED
            and _matches_expected_state(
                record,
                expected,
                evidence.readback,
                require_record_state=False,
            )
        ):
            return replace(
                record,
                lifecycle=OwnershipLifecycle.OWNED,
                intended_state=expected.as_mapping(),
            )
        return replace(record, lifecycle=OwnershipLifecycle.CONFLICTED)
    if (
        record.lifecycle is OwnershipLifecycle.OWNED
        and evidence.readback.status is SchedulerReconciliationStatus.ABSENT
        and evidence.readback.subject_m_id == record.m_id
    ):
        return replace(record, lifecycle=OwnershipLifecycle.ORPHANED)
    return replace(record, lifecycle=OwnershipLifecycle.CONFLICTED)


def apply_scheduler_provisional_recovery(
    record: SchedulerOwnershipRecord,
    expected_state: SchedulerExpectedState,
    readback: SchedulerReconciliationResult,
) -> SchedulerOwnershipRecord:
    """Reconcile a provisional record from one authoritative keyed GET."""
    if record.lifecycle is not OwnershipLifecycle.PROVISIONAL:
        return replace(record, lifecycle=OwnershipLifecycle.CONFLICTED)
    if _matches_expected_state(
        record,
        expected_state,
        readback,
        require_record_state=True,
    ):
        return replace(
            record,
            lifecycle=OwnershipLifecycle.OWNED,
            intended_state=expected_state.as_mapping(),
        )
    if (
        readback.status is SchedulerReconciliationStatus.ABSENT
        and readback.subject_m_id == record.m_id
    ):
        return replace(record, lifecycle=OwnershipLifecycle.ORPHANED)
    return replace(record, lifecycle=OwnershipLifecycle.CONFLICTED)


__all__ = [
    "apply_scheduler_provisional_recovery",
    "OwnershipLifecycle",
    "SchedulerMutationEvidence",
    "SchedulerMutationOperation",
    "SchedulerOwnershipRecord",
    "SchedulerOwnershipStore",
    "apply_scheduler_ownership_reconciliation",
    "merge_owned_scheduler_update",
    "scheduler_expected_state_from_record",
]
