"""Native-device-page Lovelace dashboard configuration generation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_ENTITY_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Gateway status",
        (
            "switch.production_control",
            "sensor.production_control",
            "sensor.authority_revision",
            "sensor.recovery_status",
        ),
    ),
    (
        "Manual control",
        (
            "number.manual_power_kw",
            "number.manual_power_duration_seconds",
            "button.start_charging",
            "button.start_discharging",
            "number.target_soc_percent",
            "number.target_soc_duration_seconds",
            "button.start_target_soc",
            "number.calibration_endpoint_soc_percent",
            "number.calibration_duration_seconds",
            "button.start_battery_calibration",
        ),
    ),
    (
        "Automatic solar charging",
        (
            "switch.automatic_solar_charging",
            "sensor.solar_charging_status",
            "switch.solar_smart_enabled",
            "number.solar_smart_min_advantage_sek_per_kwh",
            "number.solar_smart_safety_margin_percent",
            "number.solar_export_start_threshold_kw",
            "number.solar_export_stop_threshold_kw",
            "number.solar_export_buffer_kw",
            "number.solar_max_charge_power_kw",
            "number.solar_max_soc_percent",
            "number.solar_max_limit_change_kw",
            "number.solar_start_debounce_seconds",
            "number.solar_stop_debounce_seconds",
            "number.solar_update_interval_seconds",
            "number.solar_lease_seconds",
        ),
    ),
    (
        "Adaptive peak shaving",
        (
            "switch.aps_keep_active",
            "sensor.adaptive_peak_shaving_status",
            "number.aps_import_limit_kw",
            "number.aps_export_limit_kw",
            "number.aps_max_charge_kw",
            "number.aps_max_discharge_kw",
            "number.aps_min_soc_percent",
            "number.aps_max_soc_percent",
            "number.aps_max_limit_change_kw",
            "number.aps_update_interval_seconds",
            "number.aps_lease_seconds",
            "button.start_adaptive_peak_shaving",
        ),
    ),
    (
        "Load balancing",
        (
            "switch.load_balancing",
            "sensor.load_balancing_observation",
            "sensor.phase_current_l1",
            "sensor.phase_current_l2",
            "sensor.phase_current_l3",
            "sensor.charge_headroom",
            "sensor.required_discharge",
            "sensor.load_balancing_usable_soc",
            "sensor.load_balancing_min_usable_soc",
        ),
    ),
    (
        "Price optimization",
        (
            "switch.automatic_price_optimization",
            "sensor.price_optimization_status",
            "sensor.price_next_charge",
            "sensor.price_next_discharge",
            "sensor.price_scheduled_count",
            "calendar.price_optimization_calendar",
            "switch.price_schedule_writes_enabled",
            "switch.price_adjust_external_target_soc",
            "switch.price_export_allowed",
            "switch.price_whole_day_planner",
            "switch.price_plan_high_consumption",
            "number.price_blocked_notify_hours",
            "number.price_grid_import_cost_sek_per_kwh",
            "number.price_min_difference_sek_per_kwh",
            "number.price_export_min_difference_sek_per_kwh",
            "number.price_max_charge_kw",
            "number.price_max_discharge_kw",
            "number.price_min_soc_percent",
            "number.price_max_soc_percent",
            "number.price_usable_capacity_kwh",
            "number.price_round_trip_efficiency_percent",
            "number.price_cycle_cost_sek_per_kwh",
        ),
    ),
    (
        "Energy forecast",
        (
            "sensor.household_load_power",
            "sensor.household_load_forecast",
            "sensor.solar_forecast_remaining_today",
            "sensor.battery_savings_today",
        ),
    ),
    (
        "Schedule planner",
        (
            "calendar.scheduler_calendar",
            "sensor.scheduler_inventory",
        ),
    ),
)


def resolve_dashboard_entity_ids(
    registry: Any,
    *,
    serial: str,
    platform: str,
) -> dict[str, str]:
    """Resolve only entities currently registered for one runtime device."""
    resolved: dict[str, str] = {}
    for _, keys in _ENTITY_GROUPS:
        for qualified_key in keys:
            domain, key = qualified_key.split(".", 1)
            entity_id = registry.async_get_entity_id(
                domain,
                platform,
                f"{serial}_{key}",
            )
            if entity_id is not None:
                resolved[qualified_key] = entity_id
    return resolved


def generate_dashboard_config(
    entry_id: str | None = None,
    *,
    entity_ids: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a bounded dashboard config using registered native entities."""
    del entry_id
    resolved = entity_ids or {}
    cards: list[dict[str, Any]] = []
    for title, keys in _ENTITY_GROUPS:
        entities = [resolved[key] for key in keys if isinstance(resolved.get(key), str)]
        if entities:
            cards.append({"type": "entities", "title": title, "entities": entities})
    return {
        "title": "Pixiihome Controller",
        "views": [{"title": "Energy", "path": "pixiihome-energy", "cards": cards}],
    }


__all__ = ["generate_dashboard_config", "resolve_dashboard_entity_ids"]
