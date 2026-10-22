"""Versioned canonical operational settings for native Home Assistant entities."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

OPERATIONAL_SETTINGS_VERSION = 1

# Retain persisted values for compatibility with old simulator snapshots only.
# Production controllers use LoadBalancingConfig and the feature-specific limits.
RETIRED_OPERATIONAL_SETTINGS = frozenset({
    "solar_min_soc_percent",
    "load_balancing_max_phase_current_a",
    "load_balancing_safety_margin_a",
    "load_balancing_max_charge_kw",
    "load_balancing_max_discharge_kw",
    "load_balancing_adjustment_step_kw",
    "load_balancing_update_interval_seconds",
    "load_balancing_debounce_seconds",
    "planner_reserve_soc_percent",
    "planner_min_soc_percent",
    "planner_max_soc_percent",
})


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    default: float | int | bool
    minimum: float | None = None
    maximum: float | None = None
    step: float | int | None = None
    unit: str | None = None


SETTING_SPECS: dict[str, SettingSpec] = {
    "manual_power_kw": SettingSpec("manual_power_kw", 1.0, 0.0, 10.0, 0.1, "kW"),
    "manual_power_duration_seconds": SettingSpec(
        "manual_power_duration_seconds", 3600, 60, 604800, 60, "s"
    ),
    "target_soc_percent": SettingSpec("target_soc_percent", 80.0, 10.0, 100.0, 1.0, "%"),
    "target_soc_duration_seconds": SettingSpec(
        "target_soc_duration_seconds", 3600, 60, 604800, 60, "s"
    ),
    "calibration_endpoint_soc_percent": SettingSpec(
        "calibration_endpoint_soc_percent", 90.0, 10.0, 100.0, 1.0, "%"
    ),
    "calibration_duration_seconds": SettingSpec(
        "calibration_duration_seconds", 7200, 1800, 86400, 60, "s"
    ),
    "aps_import_limit_kw": SettingSpec("aps_import_limit_kw", 0.0, -100.0, 100.0, 0.1, "kW"),
    "aps_export_limit_kw": SettingSpec("aps_export_limit_kw", 0.0, -100.0, 100.0, 0.1, "kW"),
    "aps_max_charge_kw": SettingSpec("aps_max_charge_kw", 10.0, 0.0, 100.0, 0.1, "kW"),
    "aps_max_discharge_kw": SettingSpec("aps_max_discharge_kw", 10.0, 0.0, 100.0, 0.1, "kW"),
    "aps_max_limit_change_kw": SettingSpec(
        "aps_max_limit_change_kw", 1.0, 0.1, 100.0, 0.1, "kW"
    ),
    "aps_keep_active": SettingSpec("aps_keep_active", False),
    "aps_min_soc_percent": SettingSpec("aps_min_soc_percent", 10.0, 0.0, 100.0, 1.0, "%"),
    "aps_max_soc_percent": SettingSpec("aps_max_soc_percent", 90.0, 0.0, 100.0, 1.0, "%"),
    "aps_lease_seconds": SettingSpec("aps_lease_seconds", 120, 30, 900, 1, "s"),
    "aps_update_interval_seconds": SettingSpec(
        "aps_update_interval_seconds", 10, 5, 60, 1, "s"
    ),
    "solar_export_start_threshold_kw": SettingSpec(
        "solar_export_start_threshold_kw", 1.0, 0.0, 50.0, 0.1, "kW"
    ),
    "solar_export_stop_threshold_kw": SettingSpec(
        "solar_export_stop_threshold_kw", 0.5, 0.0, 50.0, 0.1, "kW"
    ),
    "solar_max_charge_power_kw": SettingSpec(
        "solar_max_charge_power_kw", 10.0, 0.0, 10.0, 0.1, "kW"
    ),
    "solar_export_buffer_kw": SettingSpec(
        "solar_export_buffer_kw", 0.2, 0.0, 2.0, 0.1, "kW"
    ),
    "solar_max_limit_change_kw": SettingSpec(
        "solar_max_limit_change_kw", 1.0, 0.1, 10.0, 0.1, "kW"
    ),
    "solar_lease_seconds": SettingSpec("solar_lease_seconds", 120, 30, 900, 1, "s"),
    "solar_update_interval_seconds": SettingSpec(
        "solar_update_interval_seconds", 10, 5, 60, 1, "s"
    ),
    "solar_min_soc_percent": SettingSpec("solar_min_soc_percent", 10.0, 0.0, 100.0, 1.0, "%"),
    "solar_max_soc_percent": SettingSpec("solar_max_soc_percent", 90.0, 0.0, 100.0, 1.0, "%"),
    "solar_smart_enabled": SettingSpec("solar_smart_enabled", False),
    "solar_smart_min_advantage_sek_per_kwh": SettingSpec(
        "solar_smart_min_advantage_sek_per_kwh", 0.2, 0.0, 5.0, 0.01, "SEK/kWh"
    ),
    "solar_smart_safety_margin_percent": SettingSpec(
        "solar_smart_safety_margin_percent", 25.0, 0.0, 200.0, 1.0, "%"
    ),
    "solar_start_debounce_seconds": SettingSpec(
        "solar_start_debounce_seconds", 60, 0, 3600, 1, "s"
    ),
    "solar_stop_debounce_seconds": SettingSpec(
        "solar_stop_debounce_seconds", 60, 0, 3600, 1, "s"
    ),
    "load_balancing_max_phase_current_a": SettingSpec(
        "load_balancing_max_phase_current_a", 16.0, 1.0, 63.0, 0.5, "A"
    ),
    "load_balancing_safety_margin_a": SettingSpec(
        "load_balancing_safety_margin_a", 2.0, 0.0, 20.0, 0.5, "A"
    ),
    "load_balancing_max_charge_kw": SettingSpec(
        "load_balancing_max_charge_kw", 10.0, 0.0, 10.0, 0.1, "kW"
    ),
    "load_balancing_max_discharge_kw": SettingSpec(
        "load_balancing_max_discharge_kw", 10.0, 0.0, 10.0, 0.1, "kW"
    ),
    "load_balancing_adjustment_step_kw": SettingSpec(
        "load_balancing_adjustment_step_kw", 0.5, 0.1, 10.0, 0.1, "kW"
    ),
    "load_balancing_update_interval_seconds": SettingSpec(
        "load_balancing_update_interval_seconds", 30, 5, 900, 1, "s"
    ),
    "load_balancing_debounce_seconds": SettingSpec(
        "load_balancing_debounce_seconds", 30, 0, 900, 1, "s"
    ),
    "planner_reserve_soc_percent": SettingSpec(
        "planner_reserve_soc_percent", 20.0, 0.0, 100.0, 1.0, "%"
    ),
    "planner_min_soc_percent": SettingSpec("planner_min_soc_percent", 10.0, 0.0, 100.0, 1.0, "%"),
    "planner_max_soc_percent": SettingSpec("planner_max_soc_percent", 90.0, 0.0, 100.0, 1.0, "%"),
    "price_export_allowed": SettingSpec("price_export_allowed", False),
    "price_schedule_writes_enabled": SettingSpec("price_schedule_writes_enabled", False),
    "price_adjust_external_target_soc": SettingSpec(
        "price_adjust_external_target_soc", False
    ),
    "price_whole_day_planner": SettingSpec("price_whole_day_planner", False),
    "price_plan_high_consumption": SettingSpec("price_plan_high_consumption", False),
    "price_blocked_notify_hours": SettingSpec(
        "price_blocked_notify_hours", 3, 0, 48, 1, "h"
    ),
    "price_min_difference_sek_per_kwh": SettingSpec(
        "price_min_difference_sek_per_kwh", 0.5, 0.0, 20.0, 0.01, "SEK/kWh"
    ),
    "price_export_min_difference_sek_per_kwh": SettingSpec(
        "price_export_min_difference_sek_per_kwh", 1.0, 0.0, 20.0, 0.01, "SEK/kWh"
    ),
    "price_grid_import_cost_sek_per_kwh": SettingSpec(
        "price_grid_import_cost_sek_per_kwh", 0.0, 0.0, 20.0, 0.01, "SEK/kWh"
    ),
    "price_max_charge_kw": SettingSpec(
        "price_max_charge_kw", 10.0, 0.0, 100.0, 0.1, "kW"
    ),
    "price_max_discharge_kw": SettingSpec(
        "price_max_discharge_kw", 10.0, 0.0, 100.0, 0.1, "kW"
    ),
    "price_min_soc_percent": SettingSpec(
        "price_min_soc_percent", 10.0, 0.0, 100.0, 1.0, "%"
    ),
    "price_max_soc_percent": SettingSpec(
        "price_max_soc_percent", 90.0, 0.0, 100.0, 1.0, "%"
    ),
    "price_usable_capacity_kwh": SettingSpec(
        "price_usable_capacity_kwh", 0.0, 0.0, 1000.0, 0.1, "kWh"
    ),
    "price_round_trip_efficiency_percent": SettingSpec(
        "price_round_trip_efficiency_percent", 90.0, 50.0, 100.0, 1.0, "%"
    ),
    "price_cycle_cost_sek_per_kwh": SettingSpec(
        "price_cycle_cost_sek_per_kwh", 0.0, 0.0, 20.0, 0.01, "SEK/kWh"
    ),
}


@dataclass(frozen=True, slots=True)
class OperationalSettings:
    """Validated settings snapshot used by entities, controllers, and previews."""

    values: dict[str, float | int | bool]
    version: int = OPERATIONAL_SETTINGS_VERSION

    @classmethod
    def defaults(cls) -> OperationalSettings:
        return cls({key: spec.default for key, spec in SETTING_SPECS.items()})

    @classmethod
    def from_mapping(cls, value: object) -> OperationalSettings:
        if not isinstance(value, dict):
            return cls.defaults()
        raw = value.get("values", value)
        if not isinstance(raw, dict):
            return cls.defaults()
        result: dict[str, float | int | bool] = {}
        for key, spec in SETTING_SPECS.items():
            candidate = raw.get(key, spec.default)
            try:
                result[key] = _validated(spec, candidate)
            except ValueError:
                result[key] = spec.default
        return cls(result)

    def get(self, key: str) -> float | int | bool:
        return self.values[key]

    def set(self, key: str, value: object) -> OperationalSettings:
        spec = SETTING_SPECS[key]
        updated = dict(self.values)
        updated[key] = _validated(spec, value)
        return OperationalSettings(updated, self.version)

    def as_dict(self) -> dict[str, Any]:
        return {"version": self.version, "values": dict(self.values)}


def _validated(spec: SettingSpec, value: object) -> float | int | bool:
    if isinstance(spec.default, bool):
        if type(value) is not bool:
            raise ValueError("boolean setting is invalid")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("numeric setting is invalid")
    numeric = float(value)
    if not isfinite(numeric):
        raise ValueError("numeric setting is not finite")
    if spec.minimum is not None and numeric < spec.minimum:
        raise ValueError("numeric setting is below its safe minimum")
    if spec.maximum is not None and numeric > spec.maximum:
        raise ValueError("numeric setting is above its safe maximum")
    if isinstance(spec.default, int):
        if int(numeric) != numeric:
            raise ValueError("integer setting is not integral")
        return int(numeric)
    return numeric


@dataclass(frozen=True, slots=True)
class ControllerState:
    """Durable enabled-state for integration-owned automatic controllers."""

    automatic_solar_charging_enabled: bool = False
    adaptive_peak_shaving_enabled: bool = False
    load_balancing_enabled: bool = False
    automatic_price_optimization_enabled: bool = False

    @classmethod
    def from_mapping(cls, value: object) -> ControllerState:
        if not isinstance(value, dict):
            return cls()
        return cls(
            automatic_solar_charging_enabled=value.get("automatic_solar_charging_enabled") is True,
            adaptive_peak_shaving_enabled=value.get("adaptive_peak_shaving_enabled") is True,
            load_balancing_enabled=value.get("load_balancing_enabled") is True,
            automatic_price_optimization_enabled=value.get("automatic_price_optimization_enabled")
            is True,
        )

    def as_dict(self) -> dict[str, bool]:
        return {
            "automatic_solar_charging_enabled": self.automatic_solar_charging_enabled,
            "adaptive_peak_shaving_enabled": self.adaptive_peak_shaving_enabled,
            "load_balancing_enabled": self.load_balancing_enabled,
            "automatic_price_optimization_enabled": self.automatic_price_optimization_enabled,
        }


__all__ = ["ControllerState", "OperationalSettings", "SETTING_SPECS", "SettingSpec"]
