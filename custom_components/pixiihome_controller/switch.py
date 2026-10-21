"""Home Assistant switches for Pixii automatic controller authority."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .devices import (
    PixiiFeature,
    ensure_gateway_device,
    feature_device_info,
    gateway_device_info,
)
from .simulator_entry import SIMULATOR_SERIAL


@dataclass(frozen=True, slots=True)
class PixiiSwitchDescription:
    """Description for one persisted authority/controller feature."""

    key: str
    controller_key: str | None = None
    setting_key: str | None = None
    authority: bool = False
    unique_id_key: str | None = None
    feature: PixiiFeature | None = None


SWITCH_DESCRIPTIONS: tuple[PixiiSwitchDescription, ...] = (
    PixiiSwitchDescription("production_control", authority=True),
    PixiiSwitchDescription(
        "automatic_solar_charging",
        controller_key="automatic_solar_charging_enabled",
        feature=PixiiFeature.SOLAR_CHARGING,
    ),
    PixiiSwitchDescription(
        "load_balancing",
        controller_key="load_balancing_enabled",
        feature=PixiiFeature.LOAD_BALANCING,
    ),
    PixiiSwitchDescription(
        "automatic_price_optimization",
        controller_key="automatic_price_optimization_enabled",
        feature=PixiiFeature.PRICE_OPTIMIZATION,
    ),
    PixiiSwitchDescription(
        "price_export_allowed",
        setting_key="price_export_allowed",
        feature=PixiiFeature.PRICE_OPTIMIZATION,
    ),
    PixiiSwitchDescription(
        "price_schedule_writes_enabled",
        setting_key="price_schedule_writes_enabled",
        feature=PixiiFeature.PRICE_OPTIMIZATION,
    ),
    PixiiSwitchDescription(
        "adaptive_peak_shaving",
        controller_key="adaptive_peak_shaving_enabled",
        # Reuse the old entity identity so existing dashboards keep working.
        unique_id_key="aps_keep_active",
        feature=PixiiFeature.ADAPTIVE_PEAK_SHAVING,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up automatic controller switches for one entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return
    parent_device_id = ensure_gateway_device(hass, entry, _runtime_serial(runtime))
    async_add_entities(
        [
            PixiiControllerSwitch(runtime, description, parent_device_id)
            for description in SWITCH_DESCRIPTIONS
        ]
    )


class PixiiControllerSwitch(SwitchEntity):
    """Toggle controller state through RuntimeData's typed authority methods."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        runtime: Any,
        description: PixiiSwitchDescription,
        parent_device_id: str | None = None,
    ) -> None:
        """Initialize one controller switch."""
        self.runtime = runtime
        self.description = description
        serial = _runtime_serial(runtime)
        self._attr_unique_id = f"{serial}_{description.unique_id_key or description.key}"
        self._attr_translation_key = description.key
        self._attr_device_info = (
            gateway_device_info(serial)
            if description.feature is None
            else feature_device_info(serial, description.feature, parent_device_id)
        )

    @property
    def available(self) -> bool:
        """Only advertise a switch when its complete authority contract exists."""
        if self.description.authority:
            return callable(getattr(self.runtime, "async_set_production_control_enabled", None))
        if self.description.setting_key is not None:
            return _settings(self.runtime) is not None and callable(
                getattr(self.runtime, "async_set_operational_setting", None)
            )
        return _controller_value(
            self.runtime, self.description.controller_key
        ) is not None and callable(
            getattr(self.runtime, "async_set_controller_enabled", None)
        )

    @property
    def is_on(self) -> bool:
        """Return the authoritative enabled state, failing closed when unknown."""
        if self.description.authority:
            return bool(getattr(self.runtime, "production_control_enabled", lambda: False)())
        if self.description.setting_key is not None:
            settings = _settings(self.runtime)
            if settings is None:
                return False
            try:
                value = settings.get(self.description.setting_key)
            except (AttributeError, KeyError, TypeError, ValueError):
                return False
            return type(value) is bool and value
        return _controller_value(self.runtime, self.description.controller_key) is True

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable only through the typed runtime authority method."""
        del kwargs
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable only through the typed runtime authority method."""
        del kwargs
        await self._async_set_enabled(False)

    async def _async_set_enabled(self, enabled: bool) -> None:
        """Toggle through the runtime authority method."""
        if self.description.authority:
            setter = getattr(self.runtime, "async_set_production_control_enabled", None)
            if not callable(setter):
                raise HomeAssistantError("Production authority is unavailable for this runtime")
            result = setter(enabled, _user_id(self))
        elif self.description.setting_key is not None:
            setter = getattr(self.runtime, "async_set_operational_setting", None)
            if not callable(setter):
                raise HomeAssistantError("Operational settings are unavailable for this runtime")
            result = setter(self.description.setting_key, enabled, _user_id(self))
        else:
            setter = getattr(self.runtime, "async_set_controller_enabled", None)
            if not callable(setter):
                raise HomeAssistantError(
                    "Automatic controller state is unavailable for this runtime"
                )
            state_key = self.description.controller_key
            if state_key is None:
                raise HomeAssistantError(
                    "Automatic controller state is unavailable for this runtime"
                )
            result = setter(state_key, enabled, _user_id(self))
        if isawaitable(result):
            await result


def _settings(runtime: Any) -> Any | None:
    """Return a usable settings snapshot without assuming a runtime class."""
    try:
        settings = getattr(runtime, "operational_settings", None)
    except Exception:  # noqa: BLE001 - unavailable runtimes must fail closed
        return None
    return settings if callable(getattr(settings, "get", None)) else None


def _controller_value(runtime: Any, key: str | None) -> bool | None:
    """Read a controller flag from either ControllerState or a mapping."""
    if key is None:
        return None
    try:
        state = getattr(runtime, "controller_state", None)
        value = state.get(key) if isinstance(state, Mapping) else getattr(state, key, None)
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return value if type(value) is bool else None


def _user_id(entity: SwitchEntity) -> str | None:
    """Read the Home Assistant caller context attached to the entity call."""
    context = getattr(entity, "_context", None)
    return getattr(context, "user_id", None) if context is not None else None


def _runtime_serial(runtime: Any) -> str:
    """Return the configured serial, using simulator identity as a safe fallback."""
    try:
        serial = getattr(runtime, "serial", None)
    except Exception:  # noqa: BLE001 - identity must not make setup crash
        serial = None
    return serial if isinstance(serial, str) and serial else SIMULATOR_SERIAL
