"""Normalized read-only Pixii sensors."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import StateType

from .const import DOMAIN
from .control.price_optimization import PriceDirection
from .devices import PixiiFeature, ensure_gateway_device, feature_device_info
from .price_timeline import next_price_action, price_timeline
from .protocol.enums import service_type_label, status_label
from .runtime import RuntimeData
from .scheduler.coordinator import SchedulerInventoryCoordinator, inventory_attributes


@dataclass(frozen=True, slots=True)
class PixiiSensorDescription:
    key: str
    name: str
    family: str
    unit: str | None = None
    device_class: SensorDeviceClass | None = None
    state_class: SensorStateClass | None = None
    entity_category: EntityCategory | None = None


DESCRIPTIONS = (
    PixiiSensorDescription(
        "raw_soc",
        "Raw Battery SoC",
        "battery",
        PERCENTAGE,
        SensorDeviceClass.BATTERY,
        SensorStateClass.MEASUREMENT,
    ),
    PixiiSensorDescription(
        "usable_soc",
        "Usable Battery SoC",
        "battery",
        PERCENTAGE,
        SensorDeviceClass.BATTERY,
        SensorStateClass.MEASUREMENT,
    ),
    PixiiSensorDescription(
        "stored_energy",
        "Battery Stored Energy",
        "energy",
        UnitOfEnergy.KILO_WATT_HOUR,
        SensorDeviceClass.ENERGY_STORAGE,
        SensorStateClass.MEASUREMENT,
    ),
    PixiiSensorDescription(
        "battery_power",
        "Battery Power",
        "battery",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    PixiiSensorDescription(
        "battery_soh",
        "Battery State of Health",
        "battery",
        PERCENTAGE,
        SensorDeviceClass.BATTERY,
        SensorStateClass.MEASUREMENT,
    ),
    PixiiSensorDescription(
        "building_power",
        "Building Power",
        "meter",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    PixiiSensorDescription(
        "meter_power",
        "Grid/Meter Power",
        "meter",
        UnitOfPower.WATT,
        SensorDeviceClass.POWER,
        SensorStateClass.MEASUREMENT,
    ),
    PixiiSensorDescription("system_status", "System Status", "core"),
    PixiiSensorDescription("grid_status", "Grid Status", "core"),
    PixiiSensorDescription("battery_status", "Battery Status", "battery"),
    PixiiSensorDescription("current_service", "Current Service", "core"),
    PixiiSensorDescription("production_control", "Production Control Authority", "core"),
    PixiiSensorDescription("authority_revision", "Authority Revision", "core"),
    PixiiSensorDescription("controller_status", "Automatic Controller Status", "core"),
    PixiiSensorDescription("recovery_status", "Control Recovery Status", "core"),
)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    runtime: RuntimeData = hass.data[DOMAIN][entry.entry_id]
    parent_device_id = ensure_gateway_device(hass, entry, runtime.serial)
    entities: list[SensorEntity] = [
        PixiiSensor(runtime, description) for description in DESCRIPTIONS
        if description.key != "controller_status" or not hasattr(runtime, "production_adapter")
    ]
    if getattr(runtime, "scheduler_coordinator", None) is not None:
        entities.append(PixiiSchedulerInventorySensor(runtime, parent_device_id))
    if getattr(runtime, "load_balancing_observer", None) is not None:
        entities.extend(
            PixiiLoadBalancingSensor(runtime, key, parent_device_id)
            for key in (
                "load_balancing_observation",
                "charge_headroom",
                "required_discharge",
                "phase_current_l1",
                "phase_current_l2",
                "phase_current_l3",
                "load_balancing_usable_soc",
                "load_balancing_min_usable_soc",
            )
        )
    if getattr(runtime, "solar_charging_controller", None) is not None:
        entities.append(PixiiSolarChargingSensor(runtime, parent_device_id))
    if getattr(runtime, "adaptive_peak_shaving_controller", None) is not None:
        entities.append(PixiiAdaptivePeakShavingSensor(runtime, parent_device_id))
    if getattr(runtime, "price_optimization_controller", None) is not None:
        entities.append(PixiiPriceOptimizationSensor(runtime, parent_device_id))
        entities.extend(
            PixiiPriceScheduleSensor(runtime, key, parent_device_id)
            for key in ("price_next_charge", "price_next_discharge", "price_scheduled_count")
        )
    if getattr(runtime, "battery_activity", None) is not None:
        entities.append(PixiiBatteryActivitySensor(runtime))
    if getattr(runtime, "energy_forecast", None) is not None:
        entities.extend(
            PixiiEnergyForecastSensor(runtime, key, parent_device_id)
            for key in FORECAST_SENSOR_KEYS
        )
    async_add_entities(entities)


class PixiiSensor(SensorEntity):
    """A presentation-only view over normalized runtime state."""

    _attr_has_entity_name = True

    def __init__(self, runtime: RuntimeData, description: PixiiSensorDescription) -> None:
        self.runtime = runtime
        self.description = description
        self._attr_unique_id = f"{runtime.serial}_{description.key}"
        self._attr_name = description.name
        self._attr_native_unit_of_measurement = description.unit
        self._attr_device_class = description.device_class
        self._attr_state_class = description.state_class
        self._attr_entity_category = description.entity_category
        self._remove_listener = None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self.runtime.serial)},
            "name": f"Pixii Gateway {self.runtime.serial}",
            "manufacturer": "Pixii",
        }


    async def async_added_to_hass(self) -> None:
        self._remove_listener = self.runtime.telemetry.add_listener(self._state_changed)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    def _state_changed(self) -> None:
        self.schedule_update_ha_state()

    @property
    def native_value(self) -> StateType:
        battery = self.runtime.battery
        telemetry = self.runtime.telemetry
        key = self.description.key
        if key == "raw_soc":
            return battery.raw_soc
        if key == "usable_soc":
            return battery.usable_soc
        if key == "stored_energy":
            return battery.stored_energy_kwh
        if key == "battery_power":
            return battery.power_w
        if key == "battery_soh":
            return battery.soh
        if key == "building_power":
            return _number(telemetry.value("meter", "building_ac_w"))
        if key == "meter_power":
            return _number(telemetry.value("meter", "meter_w"))
        if key == "system_status":
            value = telemetry.value("core", "sys_status")
            return None if value is None else status_label(value)
        if key == "grid_status":
            value = telemetry.value("core", "grid_status")
            return None if value is None else status_label(value)
        if key == "battery_status":
            value = telemetry.value("battery", "batt_status")
            return None if value is None else status_label(value)
        if key == "current_service":
            value = telemetry.value("core", "srv_type")
            return None if value is None else service_type_label(value)
        if key == "production_control":
            return "enabled" if self.runtime.production_control_enabled() else "disabled"
        if key == "authority_revision":
            authority = getattr(self.runtime, "production_authority", None)
            return getattr(
                authority,
                "revision",
                1 if self.runtime.production_control_enabled() else 2,
            )
        if key == "controller_status":
            decision = getattr(self.runtime, "last_controller_decision", None)
            return decision.status.value if decision is not None else "idle"
        if key == "recovery_status":
            journal = getattr(self.runtime, "control_journal", None)
            if journal is None:
                return "none"
            return "required" if journal.unresolved_transactions else "none"
        return None

    @property
    def available(self) -> bool:
        if self.description.key in {"raw_soc", "usable_soc", "battery_power", "battery_soh"}:
            return self.runtime.telemetry.is_fresh("battery")
        if self.description.key == "stored_energy":
            return self.runtime.telemetry.is_fresh("energy")
        return self.runtime.telemetry.is_fresh(self.description.family)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.description.key not in {"raw_soc", "usable_soc"}:
            return {}
        reading = self.runtime.battery
        return {
            "effective_min_soc": reading.effective_min_soc,
            "effective_max_soc": reading.effective_max_soc,
            "reported_min_soc": reading.reported_min_soc,
            "reported_max_soc": reading.reported_max_soc,
            "usable_soc_source": reading.usable_soc_source,
            "raw_soc_formula": "gateway_native_or_configured_limits",
        }


class PixiiSolarChargingSensor(SensorEntity):
    """Expose the controller state and the evidence behind its decision."""

    _attr_has_entity_name = True
    _attr_translation_key = "solar_charging_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime: RuntimeData, parent_device_id: str | None = None) -> None:
        controller = runtime.solar_charging_controller
        if controller is None:
            raise RuntimeError("solar charging sensor requires a controller")
        self.runtime = runtime
        self.controller = controller
        self._attr_unique_id = f"{runtime.serial}_solar_charging_status"
        self._attr_device_info = feature_device_info(
            runtime.serial,
            PixiiFeature.SOLAR_CHARGING,
            parent_device_id,
        )
        self._remove_listener = None

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self.controller.add_listener(self._state_changed)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    def _state_changed(self) -> None:
        self.schedule_update_ha_state()

    @property
    def native_value(self) -> str:
        return self.controller.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.controller.attributes()


class PixiiAdaptivePeakShavingSensor(SensorEntity):
    """Expose APS state, limits, ownership, and priority evidence."""

    _attr_has_entity_name = True
    _attr_translation_key = "adaptive_peak_shaving_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, runtime: RuntimeData, parent_device_id: str | None = None) -> None:
        controller = runtime.adaptive_peak_shaving_controller
        if controller is None:
            raise RuntimeError("adaptive peak shaving sensor requires a controller")
        self.runtime = runtime
        self.controller = controller
        self._attr_unique_id = f"{runtime.serial}_adaptive_peak_shaving_status"
        self._attr_device_info = feature_device_info(
            runtime.serial,
            PixiiFeature.ADAPTIVE_PEAK_SHAVING,
            parent_device_id,
        )
        self._remove_listener = None

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self.controller.add_listener(self._state_changed)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    def _state_changed(self) -> None:
        self.schedule_update_ha_state()

    @property
    def native_value(self) -> str:
        return self.controller.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.controller.attributes()


class PixiiPriceOptimizationSensor(SensorEntity):
    """Expose the price plan, export decision, and scheduler reconciliation state."""

    _attr_has_entity_name = True
    _attr_translation_key = "price_optimization_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # Live on the entity (dashboards read them) but too large for the
    # recorder's 16 KB attribute limit, which then dropped all of them.
    _unrecorded_attributes = frozenset(
        {"actions", "schedule_timeline", "whole_day_plan", "external_windows"}
    )

    def __init__(self, runtime: RuntimeData, parent_device_id: str | None = None) -> None:
        controller = runtime.price_optimization_controller
        if controller is None:
            raise RuntimeError("price optimization sensor requires a controller")
        self.runtime = runtime
        self.controller = controller
        self._attr_unique_id = f"{runtime.serial}_price_optimization_status"
        self._attr_device_info = feature_device_info(
            runtime.serial,
            PixiiFeature.PRICE_OPTIMIZATION,
            parent_device_id,
        )
        self._remove_listener = None

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self.controller.add_listener(self._state_changed)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    def _state_changed(self) -> None:
        self.schedule_update_ha_state()

    @property
    def native_value(self) -> str:
        return self.controller.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.controller.attributes()


class PixiiPriceScheduleSensor(PixiiPriceOptimizationSensor):
    """Expose unexpired intervals; creation evidence is separate from proposals."""

    _attr_entity_category = None

    def __init__(self, runtime, key: str, parent_device_id: str | None = None) -> None:
        super().__init__(runtime, parent_device_id)
        self.key = key
        self._attr_unique_id = f"{runtime.serial}_{key}"
        self._attr_translation_key = key
        if key != "price_scheduled_count":
            self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:calendar-clock"

    def _next(self) -> dict[str, Any] | None:
        direction = (
            PriceDirection.CHARGE if self.key == "price_next_charge" else PriceDirection.DISCHARGE
        )
        return next_price_action(self.controller, direction)

    @property
    def native_value(self) -> datetime | int | None:
        if self.key == "price_scheduled_count":
            return sum(
                item["schedule_status"] == "scheduled"
                for item in price_timeline(self.controller, datetime.now(UTC))
            )
        item = self._next()
        return datetime.fromisoformat(item["start"]) if item is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entries = price_timeline(self.controller, datetime.now(UTC))
        item = self._next() if self.key != "price_scheduled_count" else None
        local_times = {
            f"{key}_local": datetime.fromisoformat(item[key]).astimezone(
                ZoneInfo("Europe/Stockholm")
            ).isoformat()
            for key in ("start", "end")
        } if item is not None else {}
        return {
            "timezone": "Europe/Stockholm",
            "controller_status": self.controller.status,
            "controller_reason": self.controller.reason,
            "last_planned_at": (
                self.controller.last_planned_at.isoformat()
                if self.controller.last_planned_at is not None else None
            ),
            **({"schedules": entries} if self.key == "price_scheduled_count"
               else item or {"schedule_status": "none"}),
            **local_times,
        }


class PixiiLoadBalancingSensor(SensorEntity):
    """Diagnostics only; never invoke a read or a control action from a getter."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        runtime: RuntimeData,
        key: str,
        parent_device_id: str | None = None,
    ) -> None:
        observer = runtime.load_balancing_observer
        if observer is None:
            raise RuntimeError("load balancing sensor requires an observer")
        self.observer = observer
        self.key = key
        self._remove_listener = None
        self._attr_translation_key = key
        self._attr_unique_id = f"{runtime.serial}_{key}"
        self._attr_device_info = feature_device_info(
            runtime.serial,
            PixiiFeature.LOAD_BALANCING,
            parent_device_id,
        )
        if key in {"load_balancing_usable_soc", "load_balancing_min_usable_soc"}:
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_icon = "mdi:battery-heart-variant"
            if key == "load_balancing_usable_soc":
                self._attr_device_class = SensorDeviceClass.BATTERY
                self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key.startswith("phase_current"):
            self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
            self._attr_device_class = SensorDeviceClass.CURRENT
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key != "load_balancing_observation":
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self.observer.add_listener(self.schedule_update_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    @property
    def available(self) -> bool:
        if self.key == "load_balancing_observation":
            return True
        if self.key == "load_balancing_min_usable_soc":
            return True
        if self.key == "load_balancing_usable_soc":
            return self.observer.snapshot.get("usable_soc_percent") is not None
        statuses = {"observing", "overload", "insufficient_capacity"}
        if self.key.startswith("phase_current"):
            return int(
                self.key[-1]
            ) <= self.observer.config.phases and self.observer.status in statuses | {
                "phase_direction_missing"
            }
        return self.observer.status in statuses

    @property
    def native_value(self) -> StateType:
        if self.key == "load_balancing_observation":
            return self.observer.status
        if self.key == "load_balancing_min_usable_soc":
            return self.observer.config.min_soc
        if not self.available:
            return None
        if self.key == "load_balancing_usable_soc":
            return self.observer.snapshot.get("usable_soc_percent")
        if self.key.startswith("phase_current"):
            currents = self.observer.snapshot.get("phase_currents_a", [])
            index = int(self.key[-1]) - 1
            return currents[index] if index < len(currents) else None
        return self.observer.snapshot.get(f"{self.key}_w")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.observer.attributes() if self.key == "load_balancing_observation" else {}


class PixiiSchedulerInventorySensor(SensorEntity):
    """Expose the shared scheduler inventory as one machine-readable sensor."""

    _attr_has_entity_name = True
    _attr_translation_key = "scheduler_inventory"
    _unrecorded_attributes = frozenset({"schedules"})

    def __init__(self, runtime: RuntimeData, parent_device_id: str | None = None) -> None:
        self.runtime = runtime
        coordinator = runtime.scheduler_coordinator
        if coordinator is None:
            raise RuntimeError("scheduler inventory sensor requires a coordinator")
        self.coordinator: SchedulerInventoryCoordinator = coordinator
        self._remove_coordinator_listener = None
        self._attr_unique_id = f"{runtime.serial}_scheduler_inventory"
        self._attr_device_info = feature_device_info(
            runtime.serial,
            PixiiFeature.SCHEDULE_PLANNER,
            parent_device_id,
        )
        self._attr_state_class = SensorStateClass.MEASUREMENT

    async def async_added_to_hass(self) -> None:
        self._remove_coordinator_listener = self.coordinator.add_listener(self._coordinator_changed)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_coordinator_listener is not None:
            self._remove_coordinator_listener()
            self._remove_coordinator_listener = None

    def _coordinator_changed(self) -> None:
        self.schedule_update_ha_state()

    @property
    def native_value(self) -> int | None:
        snapshot = self.coordinator.snapshot
        if snapshot is None or not snapshot.available:
            return None
        return len(snapshot.schedules)

    @property
    def available(self) -> bool:
        snapshot = self.coordinator.snapshot
        return snapshot is not None and snapshot.available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self.coordinator.snapshot
        if snapshot is None:
            return {"data_status": "stale", "authenticated": None, "schedules": []}
        return inventory_attributes(snapshot)


FORECAST_SENSOR_KEYS = (
    "household_load_power",
    "household_load_forecast",
    "solar_forecast_remaining_today",
    "battery_savings_today",
)


class PixiiBatteryActivitySensor(SensorEntity):
    """What the battery does now and why; each change is a logbook entry."""

    _attr_has_entity_name = True
    _attr_translation_key = "battery_activity"

    def __init__(self, runtime: RuntimeData) -> None:
        tracker = runtime.battery_activity
        if tracker is None:
            raise RuntimeError("battery activity sensor requires the tracker")
        self.runtime = runtime
        self.tracker = tracker
        self._attr_unique_id = f"{runtime.serial}_battery_activity"
        self._attr_device_info = {"identifiers": {(DOMAIN, runtime.serial)}}
        self._remove_listener = None

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self.tracker.add_listener(self._state_changed)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    def _state_changed(self) -> None:
        self.schedule_update_ha_state()

    @property
    def native_value(self) -> StateType:
        return self.tracker.state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self.tracker.attributes()


class PixiiEnergyForecastSensor(SensorEntity):
    """Household load and solar forecasts, and measured battery savings."""

    _attr_has_entity_name = True

    def __init__(self, runtime: RuntimeData, key: str, parent_device_id: str | None = None):
        service = runtime.energy_forecast
        if service is None:
            raise RuntimeError("energy forecast sensor requires the forecast service")
        self.runtime = runtime
        self.service = service
        self.key = key
        self._attr_translation_key = key
        self._attr_unique_id = f"{runtime.serial}_{key}"
        self._attr_device_info = feature_device_info(
            runtime.serial, PixiiFeature.ENERGY_FORECAST, parent_device_id,
        )
        if key == "household_load_power":
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key == "battery_savings_today":
            self._attr_native_unit_of_measurement = "SEK"
            self._attr_device_class = SensorDeviceClass.MONETARY
            self._attr_suggested_display_precision = 2
        else:
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_suggested_display_precision = 1
        self._remove_listener = None

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self.service.add_listener(self._state_changed)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None

    def _state_changed(self) -> None:
        self.schedule_update_ha_state()

    @property
    def native_value(self) -> StateType:
        service = self.service
        if self.key == "household_load_power":
            return None if service.load_w is None else round(service.load_w)
        if self.key == "household_load_forecast":
            now = datetime.now(UTC)
            return round(service.load_energy_kwh(now, now + timedelta(hours=24)), 2)
        if self.key == "solar_forecast_remaining_today":
            value = service.remaining_solar_today_kwh()
            return None if value is None else round(value, 2)
        if self.key == "battery_savings_today":
            return round(service.savings.today_sek, 2) if service.savings.day else None
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        service = self.service
        if self.key == "household_load_forecast":
            return service.load_forecast_attributes()
        if self.key == "solar_forecast_remaining_today":
            return service.solar_forecast_attributes()
        if self.key == "battery_savings_today":
            now = datetime.now(UTC)
            return {
                "yesterday_sek": (
                    None if service.savings.yesterday_sek is None
                    else round(service.savings.yesterday_sek, 2)
                ),
                "this_week_sek": round(service.savings.week_sek(now), 2),
                "this_month_sek": round(service.savings.month_sek(now), 2),
                "last_30_days_sek": round(
                    sum(value for _, value in service.savings.history[-30:]), 2
                ),
                "daily": [[day, round(value, 2)] for day, value in service.savings.history[-14:]],
                "method": "grid cost without battery minus measured grid cost, "
                          "at the price entity plus variable import cost",
            }
        return {"status": service.status, "reason": service.reason}
