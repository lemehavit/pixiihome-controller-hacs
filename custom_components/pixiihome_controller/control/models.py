"""Typed, side-effect-free inputs and outputs for control decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class RuntimeMode(StrEnum):
    PRODUCTION = "production"
    SIMULATOR = "simulator"


class OperatorMode(StrEnum):
    OBSERVATION = "observation"
    LOCAL_CONTROL = "local_control"


class ExternalControlState(StrEnum):
    NONE = "none"
    AGGREGATOR_PRESENT = "aggregator_present"
    UNKNOWN = "unknown"


class ScheduleOrigin(StrEnum):
    LOCAL_CREATED = "local_created"
    EXTERNAL_OBSERVED = "external_observed"
    UNKNOWN = "unknown"


class Initiative(StrEnum):
    MANUAL = "manual"
    AUTOMATION = "automation"


class AdapterCapability(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    EVIDENCE_REQUIRED = "evidence_required"


class ReleaseState(StrEnum):
    PREVIEW_ONLY = "preview_only"
    LIVE_RELEASE_CLOSED = "live_release_closed"
    LIVE_RELEASE_OPEN = "live_release_open"


class ControlOperation(StrEnum):
    READ_ONLY = "read_only"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    REPLACE = "replace"
    STOP = "stop"
    TARGETED_STOP = "targeted_stop"
    DIRECT_TARGET_SOC = "direct_target_soc"
    DIRECT_DEMAND_RESPONSE = "direct_demand_response"
    DIRECT_ADAPTIVE_PEAK_SHAVING = "direct_adaptive_peak_shaving"
    DIRECT_BATTERY_CALIBRATION = "direct_battery_calibration"
    DIRECT_CONTROL = "direct_control"
    PLANNER_APPLY = "planner_apply"
    RECOVERY_APPLY = "recovery_apply"


@dataclass(frozen=True, slots=True)
class PlantHealth:
    """Technical plant observations; active service is intentionally excluded."""

    telemetry_fresh: bool = False
    system_safe: bool = False
    battery_safe: bool = False
    grid_safe: bool = False
    system_events_clear: bool = False
    reasons: tuple[str, ...] = ("plant_health_unknown",)
    system_status: int | None = None
    battery_status: int | None = None
    grid_status: int | None = None
    system_events: Any = None

    @property
    def healthy(self) -> bool:
        return (
            self.telemetry_fresh
            and self.system_safe
            and self.battery_safe
            and self.grid_safe
            and self.system_events_clear
            and not self.reasons
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "telemetry_fresh": self.telemetry_fresh,
            "healthy": self.healthy,
            "reasons": list(self.reasons),
            "system_status": self.system_status,
            "battery_status": self.battery_status,
            "grid_status": self.grid_status,
            "system_events": self.system_events,
        }


@dataclass(frozen=True, slots=True)
class OperationImpact:
    """Operation-specific consequences kept separate from plant health."""

    active_service: int | None = None
    protected_service_active: bool = False
    external_conflict: bool = False
    immediate_effect_known: bool = False
    exact_target: bool = False
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "active_service": self.active_service,
            "protected_service_active": self.protected_service_active,
            "external_conflict": self.external_conflict,
            "immediate_effect_known": self.immediate_effect_known,
            "exact_target": self.exact_target,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ControlPolicyInput:
    """Complete operation context used by the single canonical policy."""

    operation: ControlOperation
    requested_service: str | None = None
    runtime_mode: RuntimeMode = RuntimeMode.PRODUCTION
    operator_mode: OperatorMode = OperatorMode.OBSERVATION
    external_control: ExternalControlState = ExternalControlState.UNKNOWN
    origin: ScheduleOrigin = ScheduleOrigin.UNKNOWN
    initiative: Initiative = Initiative.MANUAL
    adapter_capability: AdapterCapability = AdapterCapability.SUPPORTED
    release_state: ReleaseState = ReleaseState.LIVE_RELEASE_CLOSED
    plant_health: PlantHealth = field(default_factory=PlantHealth)
    operation_impact: OperationImpact = field(default_factory=OperationImpact)
    scheduler_visibility_fresh: bool = False
    ownership_store_valid: bool = False
    ownership_ambiguous: bool = True
    mqtt_connected: bool = False
    pending_operations: bool = False
    grant_authorized: bool = False
    automation_authorized: bool = False
    validation_authorized: bool = False
    owned_background_aps: bool = False
    future_schedule_only: bool = False
    owned_price_stop_verified: bool = False

    def __post_init__(self) -> None:
        for name, enum_type in (
            ("operation", ControlOperation),
            ("runtime_mode", RuntimeMode),
            ("operator_mode", OperatorMode),
            ("external_control", ExternalControlState),
            ("origin", ScheduleOrigin),
            ("initiative", Initiative),
            ("adapter_capability", AdapterCapability),
            ("release_state", ReleaseState),
        ):
            value = getattr(self, name)
            if not isinstance(value, enum_type):
                object.__setattr__(self, name, enum_type(value))


@dataclass(frozen=True, slots=True)
class ControlDecision:
    """Deterministic operation decision without side effects."""

    permitted: bool
    blocked_reasons: tuple[str, ...]

    @classmethod
    def from_reasons(cls, reasons: list[str]) -> ControlDecision:
        unique = tuple(dict.fromkeys(reasons))
        return cls(not unique, unique)
