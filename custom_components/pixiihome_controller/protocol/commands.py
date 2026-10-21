"""Typed, side-effect-free builders for future Pixii commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from math import isfinite
from typing import Any

from ..allocator import normalize_message_id, serialize_message_id
from ..exceptions import CommandValidationError

MAX_COMMAND_DURATION_SECONDS = 7 * 24 * 60 * 60
MAX_POWER_W = 10_000_000


class CommandMethod(StrEnum):
    """Pixii command methods."""

    GET = "get"
    SET = "set"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class PixiiCommand:
    """A validated command without any transport side effect."""

    method: CommandMethod
    service: str
    fields: Mapping[str, Any] = field(default_factory=dict)
    message_id: int | None = None
    accepted_services: tuple[str, ...] = ()
    timeout_seconds: float = 5.0
    message_id_as_string: bool = False
    persist_message_id: bool = False

    def __post_init__(self) -> None:
        service = self.service.strip().lower()
        supported_services = set(self.accepted_services or (service,))
        if not service or service not in supported_services:
            raise CommandValidationError("service must be a non-empty supported name")
        if self.timeout_seconds <= 0 or not isfinite(self.timeout_seconds):
            raise CommandValidationError("command timeout must be finite and positive")
        if self.message_id is not None:
            normalize_message_id(self.message_id)
        object.__setattr__(self, "service", service)
        object.__setattr__(
            self,
            "accepted_services",
            tuple(service_name.strip().lower() for service_name in self.accepted_services)
            or (service,),
        )

    @property
    def state_changing(self) -> bool:
        """Return whether the command mutates Gateway state."""
        return self.method is not CommandMethod.GET

    def with_message_id(self, message_id: int) -> PixiiCommand:
        """Return this command with an integration-owned identifier."""
        if not self.state_changing:
            raise CommandValidationError("read-only GET commands must not receive an m_id")
        return replace(self, message_id=normalize_message_id(message_id))

    def to_payload(self) -> dict[str, Any]:
        if self.state_changing and self.message_id is None:
            raise CommandValidationError(
                "state-changing commands require an integration-owned m_id"
            )
        payload: dict[str, Any] = {
            "method": self.method.value,
            "service": self.service,
        }
        if self.message_id is not None:
            payload["m_id"] = serialize_message_id(
                self.message_id, as_string=self.message_id_as_string
            )
        payload.update(dict(self.fields))
        return payload


def _duration(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CommandValidationError("duration must be an integer")
    if not 1 <= value <= MAX_COMMAND_DURATION_SECONDS:
        raise CommandValidationError("duration is outside the supported range")
    return value


def _power(value: int | float) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not isfinite(value):
        raise CommandValidationError("power must be finite numeric telemetry")
    if abs(value) > MAX_POWER_W:
        raise CommandValidationError("power is outside the supported range")
    return value


@dataclass(frozen=True, slots=True)
class RunningServiceGet:
    """Read the currently running service; its ACK is externally ambiguous."""

    timeout_seconds: float = 5.0

    def build(self) -> PixiiCommand:
        return PixiiCommand(
            method=CommandMethod.GET,
            service="runningservice",
            timeout_seconds=self.timeout_seconds,
        )


@dataclass(frozen=True, slots=True)
class DemandResponse:
    """Build a documented Demand Response state-changing payload."""

    duration_seconds: int
    power_w: int | float
    timeout_seconds: float = 5.0
    message_id_as_string: bool = False

    def build(self) -> PixiiCommand:
        return PixiiCommand(
            method=CommandMethod.SET,
            service="demandresponse",
            fields={
                "values": {"pacref": _power(self.power_w)},
                "starttime": "now",
                "duration": _duration(self.duration_seconds),
            },
            timeout_seconds=self.timeout_seconds,
            message_id_as_string=self.message_id_as_string,
        )


@dataclass(frozen=True, slots=True)
class TargetSoC:
    """Build a documented Target SoC state-changing payload."""

    soc_percent: int | float
    duration_seconds: int
    timeout_seconds: float = 5.0
    message_id_as_string: bool = False

    def build(self) -> PixiiCommand:
        if (
            isinstance(self.soc_percent, bool)
            or not isinstance(self.soc_percent, int | float)
            or not isfinite(self.soc_percent)
            or not 0 <= self.soc_percent <= 100
        ):
            raise CommandValidationError("target SoC must be finite and between 0 and 100")
        return PixiiCommand(
            method=CommandMethod.SET,
            service="targetsoc",
            accepted_services=("targetsoc", "targetgsoc"),
            fields={
                "values": {"socref": self.soc_percent},
                "starttime": "now",
                "duration": _duration(self.duration_seconds),
            },
            timeout_seconds=self.timeout_seconds,
            message_id_as_string=self.message_id_as_string,
        )


@dataclass(frozen=True, slots=True)
class AdaptivePeakShaving:
    """Build a documented Adaptive Peak Shaving payload."""

    export_limit_w: int | float
    import_limit_w: int | float
    charge_max_w: int | float
    discharge_max_w: int | float
    keep_active: bool
    max_limit_change_w: int | float
    duration_seconds: int | None = None
    timeout_seconds: float = 5.0
    message_id_as_string: bool = False

    def build(self) -> PixiiCommand:
        if not isinstance(self.keep_active, bool):
            raise CommandValidationError("keep_active must be boolean")
        fields: dict[str, Any] = {
            "values": {
                "exportlim": _power(self.export_limit_w),
                "importlim": _power(self.import_limit_w),
                "charge_max": _power(self.charge_max_w),
                "discharge_max": _power(self.discharge_max_w),
                "keep_active": self.keep_active,
                "max_lim_change": _power(self.max_limit_change_w),
            },
            "starttime": "now",
        }
        if self.duration_seconds is not None:
            # Pixii documents duration=0 as the explicit stop operation for a
            # running service identified by the same m_id.
            fields["duration"] = (
                0 if self.duration_seconds == 0 else _duration(self.duration_seconds)
            )
        return PixiiCommand(
            method=CommandMethod.SET,
            service="adaptivepeakshaving",
            fields=fields,
            timeout_seconds=self.timeout_seconds,
            message_id_as_string=self.message_id_as_string,
        )

@dataclass(frozen=True, slots=True)
class BatteryCalibration:
    """Build the documented Battery Calibration payload."""

    endpoint_soc: int | float
    duration_seconds: int
    timeout_seconds: float = 5.0
    message_id_as_string: bool = False
    method: CommandMethod = CommandMethod.SET

    def build(self) -> PixiiCommand:
        if self.method is not CommandMethod.SET:
            raise CommandValidationError("battery calibration only supports method=set")
        if (
            isinstance(self.endpoint_soc, bool)
            or not isinstance(self.endpoint_soc, int | float)
            or not isfinite(self.endpoint_soc)
            or not 0 <= self.endpoint_soc <= 100
        ):
            raise CommandValidationError(
                "battery calibration endpoint SoC must be finite and between 0 and 100"
            )
        return PixiiCommand(
            method=CommandMethod.SET,
            service="batterycalibration",
            fields={
                "values": {"endpoint_soc": self.endpoint_soc},
                "starttime": "now",
                "duration": _duration(self.duration_seconds),
            },
            timeout_seconds=self.timeout_seconds,
            message_id_as_string=self.message_id_as_string,
        )


__all__ = [
    "AdaptivePeakShaving",
    "BatteryCalibration",
    "CommandMethod",
    "DemandResponse",
    "PixiiCommand",
    "RunningServiceGet",
    "TargetSoC",
]
