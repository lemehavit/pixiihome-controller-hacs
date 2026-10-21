"""Single canonical fail-closed policy for manual and automatic operations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from .models import (
    AdapterCapability,
    ControlDecision,
    ControlOperation,
    ControlPolicyInput,
    Initiative,
    OperatorMode,
    ReleaseState,
    RuntimeMode,
    ScheduleOrigin,
)


@dataclass(frozen=True, slots=True)
class AutomationPolicy:
    """Versioned allowlist for integration-owned automatic controllers."""

    policy_id: str
    schema_version: int
    revision: int
    enabled: bool
    allowed_operation_classes: tuple[str, ...]
    allowed_service_classes: tuple[str, ...]
    time_window: Mapping[str, Any]
    max_duration_seconds: int
    soc_min_percent: float
    soc_max_percent: float
    max_power_kw: float
    external_control_allowed: bool

    @classmethod
    def disabled(cls) -> AutomationPolicy:
        return cls(
            policy_id="default",
            schema_version=1,
            revision=0,
            enabled=False,
            allowed_operation_classes=(),
            allowed_service_classes=(),
            time_window={"start": "00:00", "end": "23:59", "days": [0, 1, 2, 3, 4, 5, 6]},
            max_duration_seconds=0,
            soc_min_percent=0.0,
            soc_max_percent=100.0,
            max_power_kw=0.0,
            external_control_allowed=False,
        )

    @classmethod
    def from_mapping(cls, value: object) -> AutomationPolicy:
        if not isinstance(value, Mapping):
            return cls.disabled()
        policy_id = value.get("policy_id")
        schema_version = value.get("schema_version")
        revision = value.get("revision")
        enabled = value.get("enabled")
        operations = value.get("allowed_operation_classes")
        services = value.get("allowed_service_classes")
        window = value.get("time_window")
        max_duration = value.get("max_duration_seconds")
        soc_min = value.get("soc_min_percent")
        soc_max = value.get("soc_max_percent")
        max_power = value.get("max_power_kw")
        external = value.get("external_control_allowed")
        if (
            not isinstance(policy_id, str)
            or not policy_id.strip()
            or schema_version != 1
            or isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision < 0
            or type(enabled) is not bool
            or not isinstance(operations, list)
            or not isinstance(services, list)
            or not all(isinstance(item, str) and item for item in operations + services)
            or not isinstance(window, Mapping)
            or not isinstance(window.get("start"), str)
            or not isinstance(window.get("end"), str)
            or not isinstance(window.get("days"), list)
            or any(
                isinstance(day, bool)
                or not isinstance(day, int)
                or not 0 <= day <= 6
                for day in window["days"]
            )
            or isinstance(max_duration, bool)
            or not isinstance(max_duration, int)
            or max_duration < 0
            or isinstance(soc_min, bool)
            or not isinstance(soc_min, (int, float))
            or isinstance(soc_max, bool)
            or not isinstance(soc_max, (int, float))
            or not 0 <= float(soc_min) <= float(soc_max) <= 100
            or isinstance(max_power, bool)
            or not isinstance(max_power, (int, float))
            or not isfinite(max_power)
            or max_power < 0
            or type(external) is not bool
        ):
            return cls.disabled()
        try:
            _clock_minute(window["start"])
            _clock_minute(window["end"])
        except ValueError:
            return cls.disabled()
        return cls(
            policy_id=policy_id,
            schema_version=1,
            revision=revision,
            enabled=enabled,
            allowed_operation_classes=tuple(operations),
            allowed_service_classes=tuple(services),
            time_window={
                "start": window["start"],
                "end": window["end"],
                "days": list(window["days"]),
            },
            max_duration_seconds=max_duration,
            soc_min_percent=float(soc_min),
            soc_max_percent=float(soc_max),
            max_power_kw=float(max_power),
            external_control_allowed=external,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "schema_version": self.schema_version,
            "revision": self.revision,
            "enabled": self.enabled,
            "allowed_operation_classes": list(self.allowed_operation_classes),
            "allowed_service_classes": list(self.allowed_service_classes),
            "time_window": dict(self.time_window),
            "max_duration_seconds": self.max_duration_seconds,
            "soc_min_percent": self.soc_min_percent,
            "soc_max_percent": self.soc_max_percent,
            "max_power_kw": self.max_power_kw,
            "external_control_allowed": self.external_control_allowed,
        }

    def bounded_duration(self, start: datetime, duration_seconds: int) -> int:
        """Clip a lease at the first disallowed Stockholm minute or day."""
        if start.tzinfo is None or duration_seconds <= 0 or not self.enabled:
            return 0
        current = start.astimezone(UTC)
        stop = current + timedelta(seconds=min(duration_seconds, self.max_duration_seconds))
        instant = current
        while instant < stop:
            if not self._allows_instant(instant):
                return max(0, int((instant - current).total_seconds()))
            instant = min(stop, instant.replace(second=0, microsecond=0) + timedelta(minutes=1))
        return int((stop - current).total_seconds())

    def _allows_instant(self, instant: datetime) -> bool:
        local = instant.astimezone(ZoneInfo("Europe/Stockholm"))
        try:
            start = _clock_minute(self.time_window["start"])
            end = _clock_minute(self.time_window["end"])
        except (KeyError, TypeError, ValueError):
            return False
        minute = local.hour * 60 + local.minute
        day = local.weekday()
        if start <= end:
            return day in self.time_window["days"] and start <= minute <= end
        # An overnight window belongs to the day on which it starts.
        if minute >= start:
            return day in self.time_window["days"]
        return minute <= end and (day - 1) % 7 in self.time_window["days"]


def _clock_minute(value: object) -> int:
    if not isinstance(value, str):
        raise ValueError("policy_clock_invalid")
    parts = value.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not part.isdecimal() for part in parts):
        raise ValueError("policy_clock_invalid")
    hour, minute = map(int, parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("policy_clock_invalid")
    return hour * 60 + minute

_MUTATING_OPERATIONS = frozenset(ControlOperation) - {ControlOperation.READ_ONLY}
_SCHEDULER_OPERATIONS = {
    ControlOperation.CREATE,
    ControlOperation.UPDATE,
    ControlOperation.DELETE,
    ControlOperation.REPLACE,
    ControlOperation.STOP,
    ControlOperation.PLANNER_APPLY,
    ControlOperation.RECOVERY_APPLY,
}


def evaluate_control_operation(context: ControlPolicyInput) -> ControlDecision:
    """Evaluate one operation without allocating, persisting, or calling an adapter."""
    reasons: list[str] = []
    operation = context.operation

    if context.adapter_capability is AdapterCapability.UNSUPPORTED:
        reasons.append("adapter_capability_unsupported")
    elif context.adapter_capability is AdapterCapability.EVIDENCE_REQUIRED:
        reasons.append("evidence_required")

    if operation is ControlOperation.READ_ONLY:
        return ControlDecision.from_reasons(reasons)

    if operation not in _MUTATING_OPERATIONS:
        reasons.append("operation_unsupported")
    if context.operator_mode is OperatorMode.OBSERVATION:
        reasons.append("operator_mode_observation")
    if (
        context.runtime_mode is RuntimeMode.PRODUCTION
        and context.release_state is ReleaseState.LIVE_RELEASE_CLOSED
    ):
        reasons.append("release_gate_closed")

    if operation is ControlOperation.DIRECT_CONTROL:
        reasons.append("generic_direct_control_forbidden")
    elif operation is ControlOperation.DIRECT_TARGET_SOC:
        if context.requested_service != "targetsoc":
            reasons.append("requested_service_mismatch")
    elif operation is ControlOperation.DIRECT_DEMAND_RESPONSE:
        if context.requested_service != "demandresponse":
            reasons.append("requested_service_mismatch")
    elif operation is ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING:
        if context.requested_service != "adaptivepeakshaving":
            reasons.append("requested_service_mismatch")
    elif operation is ControlOperation.DIRECT_BATTERY_CALIBRATION:
        if context.requested_service != "batterycalibration":
            reasons.append("requested_service_mismatch")

    if operation in _SCHEDULER_OPERATIONS:
        if not context.scheduler_visibility_fresh:
            reasons.append("scheduler_visibility_stale")
        if not context.ownership_store_valid:
            reasons.append("scheduler_ownership_store_invalid")
        if context.ownership_ambiguous:
            reasons.append("scheduler_ownership_ambiguous")

    if not context.plant_health.telemetry_fresh:
        reasons.append("telemetry_stale")
    if not context.plant_health.system_safe:
        reasons.append("system_not_safe")
    if not context.plant_health.battery_safe:
        reasons.append("battery_not_safe")
    if not context.plant_health.grid_safe:
        reasons.append("grid_not_safe")
    if not context.plant_health.system_events_clear:
        reasons.append("system_events_not_clear")
    reasons.extend(context.plant_health.reasons)

    if not context.mqtt_connected:
        reasons.append("mqtt_disconnected")
    if context.pending_operations:
        reasons.append("pending_command_operations")

    impact = context.operation_impact
    own_stop = (
        operation is ControlOperation.STOP and context.owned_price_stop_verified
        and impact.exact_target
    )
    if impact.protected_service_active and not own_stop:
        reasons.append("protected_service_active")
    if impact.external_conflict:
        reasons.append("scheduler_external_conflict")
    if operation is ControlOperation.CREATE and impact.active_service not in {None, 0}:
        if not (
            context.future_schedule_only
            and context.owned_background_aps
            and impact.active_service == 105
        ):
            reasons.append("active_service_impact_unproven")
    elif (
        impact.active_service not in {None, 0}
        and not impact.immediate_effect_known and not own_stop
    ):
        reasons.append("operation_impact_unknown")
    reasons.extend(impact.reasons)

    if context.origin is ScheduleOrigin.UNKNOWN:
        reasons.append("target_origin_unknown")
    elif context.origin is ScheduleOrigin.EXTERNAL_OBSERVED:
        if context.initiative is Initiative.MANUAL and not context.grant_authorized:
            reasons.append("manual_grant_required")
        if context.initiative is Initiative.AUTOMATION and not context.automation_authorized:
            reasons.append("automation_policy_denies_external_target")

    if context.initiative is Initiative.AUTOMATION and not context.automation_authorized:
        reasons.append("automation_policy_not_authorized")

    return ControlDecision.from_reasons(reasons)


__all__ = ["AutomationPolicy", "evaluate_control_operation"]
