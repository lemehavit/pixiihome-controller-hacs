"""Offline keyed MQTT scheduler GET readback and reconciliation models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from types import MappingProxyType
from typing import Any

from ..allocator import normalize_message_id
from ..exceptions import CommandValidationError, SchedulerProtocolError
from .commands import CommandMethod
from .models import MqttAck
from .scheduler_commands import (
    MAX_SCHEDULER_DURATION_SECONDS,
    SchedulerCommand,
)
from .scheduler_values import (
    MANUAL_SCHEDULER_SERVICES,
    SCHEDULER_VALUE_KEYS,
    validate_scheduler_values,
)

RECONCILABLE_SCHEDULER_SERVICES = MANUAL_SCHEDULER_SERVICES

_READBACK_VALUE_KEYS = frozenset(
    {
        "duration",
        "service_duration_left",
        "service_status",
        "service_time_left",
        "socref",
        "starttime",
    }
)
_MAX_UNKNOWN_VALUE_KEYS = 32


class SchedulerReconciliationStatus(StrEnum):
    """Result of comparing one keyed MQTT GET with expected state."""

    MATCH = "match"
    MISMATCH = "mismatch"
    ABSENT = "absent"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class SchedulerExpectedState:
    """Complete intended fields for one integration-owned scheduled service."""

    m_id: int
    service: str
    socref: int | float | None
    starttime: datetime
    duration: int
    values: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        try:
            message_id = normalize_message_id(self.m_id)
            service = _known_scheduler_service(self.service)
            parameters = validate_scheduler_values(
                service,
                {"socref": _socref(self.socref)} if service == "targetsoc" else self.values,
            )
            if (
                service == "targetsoc"
                and self.values is not None
                and dict(self.values) != parameters
            ):
                raise SchedulerProtocolError("targetsoc values conflict with socref")
            if service != "targetsoc" and self.socref is not None:
                raise SchedulerProtocolError("socref is only applicable to targetsoc")
        except CommandValidationError as err:
            raise SchedulerProtocolError("expected scheduler values or m_id are invalid") from err
        starttime = parse_scheduler_starttime(self.starttime)
        if starttime is None:
            raise SchedulerProtocolError("expected scheduler starttime must be a timestamp")
        object.__setattr__(self, "m_id", message_id)
        object.__setattr__(self, "service", service)
        object.__setattr__(self, "starttime", starttime)
        object.__setattr__(self, "duration", _duration(self.duration))
        object.__setattr__(
            self, "values", None if service == "targetsoc" else MappingProxyType(parameters)
        )

    @property
    def parameters(self) -> dict[str, Any]:
        """Return an independent copy of the exact planned service values."""
        return {"socref": self.socref} if self.service == "targetsoc" else dict(self.values or {})

    def as_mapping(self) -> dict[str, Any]:
        """Preserve existing Target SoC records and persist other schemas explicitly."""
        return {
            "m_id": self.m_id,
            "service": self.service,
            **(
                {"socref": self.socref}
                if self.service == "targetsoc"
                else {"values": self.parameters}
            ),
            "starttime": _format_scheduler_timestamp(self.starttime),
            "duration": self.duration,
        }


@dataclass(frozen=True, slots=True)
class SchedulerReadback:
    """Keyed GET values with exact service fields and unknown-field tracking."""

    m_id: int
    service: str
    socref: int | float | None
    starttime: datetime
    duration: int
    service_status: int | None = None
    service_duration_left: int | None = None
    unknown_value_keys: tuple[str, ...] = ()
    values: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        state = SchedulerExpectedState(
            self.m_id, self.service, self.socref, self.starttime, self.duration, self.values
        )
        for key in ("m_id", "service", "socref", "starttime", "duration", "values"):
            object.__setattr__(self, key, getattr(state, key))
        object.__setattr__(self, "unknown_value_keys", tuple(sorted(self.unknown_value_keys)))

    @property
    def parameters(self) -> dict[str, Any]:
        return {"socref": self.socref} if self.service == "targetsoc" else dict(self.values or {})

    def as_mapping(self) -> dict[str, Any]:
        return SchedulerExpectedState(
            self.m_id, self.service, self.socref, self.starttime, self.duration, self.values
        ).as_mapping()


@dataclass(frozen=True, slots=True)
class SchedulerReconciliationResult:
    """Structured reconciliation result with explicit subject identity."""

    status: SchedulerReconciliationStatus
    reason: str
    mismatch_fields: tuple[str, ...] = ()
    readback: SchedulerReadback | None = None
    subject_m_id: int | None = None
    subject_service: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", SchedulerReconciliationStatus(self.status))
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise SchedulerProtocolError("scheduler reconciliation reason must be non-empty")
        object.__setattr__(self, "mismatch_fields", tuple(self.mismatch_fields))
        if self.subject_m_id is not None:
            try:
                subject_m_id = normalize_message_id(self.subject_m_id)
            except CommandValidationError as err:
                raise SchedulerProtocolError(
                    "scheduler reconciliation subject m_id is invalid"
                ) from err
            object.__setattr__(self, "subject_m_id", subject_m_id)
        if self.subject_service is not None:
            if not isinstance(self.subject_service, str) or not self.subject_service.strip():
                raise SchedulerProtocolError(
                    "scheduler reconciliation subject service must be non-empty"
                )
            object.__setattr__(self, "subject_service", self.subject_service.strip().lower())

    @property
    def matched(self) -> bool:
        """Return whether the expected state was proven by keyed GET."""
        return self.status is SchedulerReconciliationStatus.MATCH


def _known_scheduler_service(value: str) -> str:
    if not isinstance(value, str):
        raise SchedulerProtocolError("scheduler service must be a string")
    service = value.strip().lower()
    if service not in RECONCILABLE_SCHEDULER_SERVICES:
        raise SchedulerProtocolError("scheduler service is not reconcilable")
    return service


def _socref(value: int | float | None) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
        or not 0 <= value <= 100
    ):
        raise SchedulerProtocolError("scheduler socref must be finite and from 0 through 100")
    return value


def _duration(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_SCHEDULER_DURATION_SECONDS
    ):
        raise SchedulerProtocolError("scheduler duration must be an unsigned uint32")
    return value


def _format_scheduler_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_scheduler_starttime(
    value: object,
    *,
    allow_now: bool = False,
    require_real: bool = True,
) -> datetime | None:
    """Parse one scheduler timestamp and normalize it to UTC.

    ``now`` is only accepted when explicitly enabled and is represented as ``None``;
    owned future scheduler readback always uses ``require_real=True``.
    """
    if isinstance(value, str) and value.strip().lower() == "now":
        if allow_now and not require_real:
            return None
        raise SchedulerProtocolError("scheduler owned readback requires a real starttime")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as err:
            raise SchedulerProtocolError("scheduler starttime is not valid ISO-8601") from err
    else:
        raise SchedulerProtocolError("scheduler starttime must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SchedulerProtocolError("scheduler starttime must be timezone-aware")
    return parsed.astimezone(UTC)


def parse_scheduler_get_ack(
    ack: MqttAck,
    *,
    expected_m_id: int,
    expected_service: str,
) -> SchedulerReadback:
    """Parse one successful keyed scheduler GET ACK for an owned service."""
    expected_id = _normalize_expected_id(expected_m_id)
    if ack.method.strip().lower() != CommandMethod.GET.value:
        raise SchedulerProtocolError("scheduler readback ACK method must be get")
    if _normalize_ack_id(ack) != expected_id:
        raise SchedulerProtocolError("scheduler readback ACK m_id does not match")
    if ack.response != 65535:
        raise SchedulerProtocolError("scheduler readback ACK was not successful")
    service = _known_scheduler_service(expected_service)
    if ack.service.strip().lower() != service:
        raise SchedulerProtocolError("scheduler readback ACK service does not match")
    starttime = parse_scheduler_starttime(ack.values.get("starttime"))
    if starttime is None:
        raise SchedulerProtocolError("scheduler readback starttime is missing")
    if ack.duration is None:
        raise SchedulerProtocolError("scheduler readback duration is missing")
    if service == "targetsoc" and ack.socref is None:
        raise SchedulerProtocolError("scheduler readback socref is missing")
    unknown_keys = tuple(
        sorted(
            key
            for key in ack.values
            if key not in _READBACK_VALUE_KEYS | SCHEDULER_VALUE_KEYS[service]
        )[:_MAX_UNKNOWN_VALUE_KEYS]
    )
    return SchedulerReadback(
        m_id=expected_id,
        service=service,
        socref=ack.socref,
        starttime=starttime,
        duration=ack.duration,
        service_status=ack.service_status,
        service_duration_left=ack.service_duration_left,
        unknown_value_keys=unknown_keys,
        values=(
            {key: ack.values[key] for key in SCHEDULER_VALUE_KEYS[service] if key in ack.values}
            if service != "targetsoc"
            else None
        ),
    )


def scheduler_expected_state_from_command(
    command: SchedulerCommand,
) -> SchedulerExpectedState:
    """Build complete Target SoC expected state from a validated CREATE command."""
    if (
        command.method is not CommandMethod.SET
        or command.service not in RECONCILABLE_SCHEDULER_SERVICES
    ):
        raise SchedulerProtocolError("expected state requires a supported scheduler SET command")
    values = command.fields.get("values")
    starttime = command.fields.get("starttime")
    duration = command.fields.get("duration")
    if (
        not isinstance(values, Mapping)
        or not isinstance(starttime, (datetime, str))
        or not isinstance(duration, int)
    ):
        raise SchedulerProtocolError("targetsoc expected state is incomplete")
    parsed_starttime = parse_scheduler_starttime(starttime)
    if parsed_starttime is None:
        raise SchedulerProtocolError("targetsoc expected state requires a real starttime")
    return SchedulerExpectedState(
        m_id=command.m_id,
        service=command.service,
        socref=values.get("socref"),
        values=values,
        starttime=parsed_starttime,
        duration=duration,
    )


def apply_scheduler_update(
    expected: SchedulerExpectedState,
    update: SchedulerCommand,
) -> SchedulerExpectedState:
    """Apply a validated SET update without forgetting unchanged expected fields."""
    if update.method is not CommandMethod.SET or update.service != expected.service:
        raise SchedulerProtocolError("scheduler update service does not match expected state")
    if update.m_id != expected.m_id:
        raise SchedulerProtocolError("scheduler update m_id does not match expected state")
    values = update.fields.get("values", expected.parameters)
    if not isinstance(values, Mapping):
        raise SchedulerProtocolError("scheduler update values must be an object")
    return SchedulerExpectedState(
        m_id=expected.m_id,
        service=expected.service,
        socref=values.get("socref"),
        values=values,
        starttime=expected.starttime,
        duration=update.fields.get("duration", expected.duration),
    )


def reconcile_scheduler_readback(
    expected: SchedulerExpectedState,
    observed: SchedulerReadback,
) -> SchedulerReconciliationResult:
    """Compare exact identity and Target SoC planned fields."""
    mismatches: list[str] = []
    if observed.m_id != expected.m_id:
        mismatches.append("m_id")
    if observed.service != expected.service:
        mismatches.append("service")
    for key in sorted(expected.parameters):
        if observed.parameters.get(key) != expected.parameters[key]:
            mismatches.append(key)
    if observed.starttime != expected.starttime:
        mismatches.append("starttime")
    if observed.duration != expected.duration:
        mismatches.append("duration")
    if mismatches:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.MISMATCH,
            "readback_fields_mismatch",
            tuple(mismatches),
            observed,
            observed.m_id,
            observed.service,
        )
    return SchedulerReconciliationResult(
        SchedulerReconciliationStatus.MATCH,
        "readback_matches_expected_state",
        readback=observed,
        subject_m_id=observed.m_id,
        subject_service=observed.service,
    )


def reconcile_scheduler_get(
    ack: MqttAck,
    expected: SchedulerExpectedState,
) -> SchedulerReconciliationResult:
    """Classify one keyed GET as match, absence, rejection, or ambiguity."""
    subject_m_id = _optional_ack_id(ack)
    subject_service = _normalize_subject_service(ack.service)
    if ack.method.strip().lower() != CommandMethod.GET.value:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.AMBIGUOUS,
            "readback_method_mismatch",
            ("method",),
            subject_m_id=subject_m_id,
            subject_service=subject_service,
        )
    try:
        ack_id = _normalize_ack_id(ack)
    except SchedulerProtocolError:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.AMBIGUOUS,
            "readback_m_id_invalid",
            ("m_id",),
            subject_service=subject_service,
        )
    if ack_id != expected.m_id:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.MISMATCH,
            "readback_m_id_mismatch",
            ("m_id",),
            subject_m_id=ack_id,
            subject_service=subject_service,
        )
    if ack.response == 11:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.ABSENT,
            "scheduler_m_id_not_configured",
            subject_m_id=ack_id,
            subject_service=subject_service,
        )
    if ack.response != 65535:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.REJECTED,
            "scheduler_get_rejected",
            subject_m_id=ack_id,
            subject_service=subject_service,
        )
    if ack.service.strip().lower() != expected.service:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.MISMATCH,
            "readback_service_mismatch",
            ("service",),
            subject_m_id=ack_id,
            subject_service=subject_service,
        )
    try:
        observed = parse_scheduler_get_ack(
            ack,
            expected_m_id=expected.m_id,
            expected_service=expected.service,
        )
    except SchedulerProtocolError:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.REJECTED,
            "readback_shape_invalid",
            ("readback",),
            subject_m_id=ack_id,
            subject_service=expected.service,
        )
    return reconcile_scheduler_readback(expected, observed)


def reconcile_scheduler_delete(
    ack: MqttAck, *, expected_m_id: int
) -> SchedulerReconciliationResult:
    """Treat only matching GET response 11 as proven deletion absence."""
    subject_m_id = _optional_ack_id(ack)
    subject_service = _normalize_subject_service(ack.service)
    if ack.method.strip().lower() != CommandMethod.GET.value:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.AMBIGUOUS,
            "delete_readback_method_mismatch",
            ("method",),
            subject_m_id=subject_m_id,
            subject_service=subject_service,
        )
    try:
        ack_id = _normalize_ack_id(ack)
    except SchedulerProtocolError:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.AMBIGUOUS,
            "delete_readback_m_id_invalid",
            ("m_id",),
            subject_service=subject_service,
        )
    expected_id = _normalize_expected_id(expected_m_id)
    if ack_id != expected_id:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.MISMATCH,
            "delete_readback_m_id_mismatch",
            ("m_id",),
            subject_m_id=ack_id,
            subject_service=subject_service,
        )
    if ack.response == 11:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.ABSENT,
            "scheduler_m_id_not_configured",
            subject_m_id=ack_id,
            subject_service=subject_service,
        )
    if ack.response == 65535:
        return SchedulerReconciliationResult(
            SchedulerReconciliationStatus.REJECTED,
            "scheduler_m_id_still_present",
            subject_m_id=ack_id,
            subject_service=subject_service,
        )
    return SchedulerReconciliationResult(
        SchedulerReconciliationStatus.REJECTED,
        "scheduler_delete_readback_rejected",
        subject_m_id=ack_id,
        subject_service=subject_service,
    )


def reconcile_cleared_price_stop(
    ack: MqttAck, expected: SchedulerExpectedState
) -> SchedulerReconciliationResult | None:
    """Recognize only the cleared Peak Shaving record observed on firmware 2.3.63.

    Status 0 means undefined, not idle. It is insufficient by itself. This
    cancellation-only shape requires the exact identity, unchanged complete parameters,
    explicit zero duration and an explicitly cleared start time. Callers must
    confirm it with a second keyed GET AND independently verify physical idle.
    The live 2026-10-18 trial proved this shape can coexist with active output.
    Ordinary CREATE/UPDATE reconciliation
    must continue rejecting this shape; no timestamp is invented for it.
    """
    if (
        expected.service != "peakshaving"
        or expected.duration != 0
        or ack.method != "get"
        or _optional_ack_id(ack) != expected.m_id
        or ack.service != expected.service
        or ack.response != 65535
        or type(ack.duration) is not int or ack.duration != 0
        or type(ack.service_status) is not int or ack.service_status != 0
        or ack.recurrence not in (None, {})
        or any(left not in (None, 0) for left in (
            ack.service_time_left, ack.service_duration_left,
        ))
    ):
        return None
    required = {"duration", "starttime", "service_status", *expected.parameters}
    allowed = required | {"service_time_left", "service_duration_left"}
    if not required <= ack.values.keys() or ack.values.keys() - allowed:
        return None
    if (
        ack.values["starttime"] != ""
        or type(ack.values["duration"]) is not int or ack.values["duration"] != 0
        or type(ack.values["service_status"]) is not int or ack.values["service_status"] != 0
        or any(type(ack.values[key]) is not int or ack.values[key] != 0
               for key in ("service_time_left", "service_duration_left") if key in ack.values)
    ):
        return None
    try:
        parameters = validate_scheduler_values(
            expected.service, {key: ack.values[key] for key in expected.parameters}
        )
    except CommandValidationError:
        return None
    if parameters != expected.parameters:
        return None
    return SchedulerReconciliationResult(
        SchedulerReconciliationStatus.MATCH,
        "price_stop_record_cleared",
        subject_m_id=expected.m_id,
        subject_service=expected.service,
    )


def _normalize_subject_service(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    return normalized or None


def _optional_ack_id(ack: MqttAck) -> int | None:
    try:
        return _normalize_ack_id(ack)
    except SchedulerProtocolError:
        return None


def _normalize_expected_id(value: int) -> int:
    try:
        return normalize_message_id(value)
    except CommandValidationError as err:
        raise SchedulerProtocolError("expected scheduler m_id is invalid") from err


def _normalize_ack_id(ack: MqttAck) -> int:
    if ack.m_id is None:
        raise SchedulerProtocolError("scheduler ACK m_id is missing")
    try:
        return normalize_message_id(ack.m_id)
    except CommandValidationError as err:
        raise SchedulerProtocolError("scheduler ACK m_id is invalid") from err


__all__ = [
    "RECONCILABLE_SCHEDULER_SERVICES",
    "SchedulerExpectedState",
    "SchedulerReadback",
    "SchedulerReconciliationResult",
    "SchedulerReconciliationStatus",
    "apply_scheduler_update",
    "parse_scheduler_get_ack",
    "parse_scheduler_starttime",
    "reconcile_scheduler_delete",
    "reconcile_scheduler_get",
    "reconcile_scheduler_readback",
    "scheduler_expected_state_from_command",
]
