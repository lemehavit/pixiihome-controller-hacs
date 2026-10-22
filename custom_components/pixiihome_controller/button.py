"""Home Assistant buttons for typed Pixii native operations."""

from __future__ import annotations

from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

from homeassistant.components.button import ButtonEntity
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
class PixiiButtonDescription:
    """Description for one typed native operation."""

    key: str
    operation: str
    feature: PixiiFeature | None = None


BUTTON_DESCRIPTIONS: tuple[PixiiButtonDescription, ...] = (
    PixiiButtonDescription("start_charging", "charge", PixiiFeature.MANUAL_CONTROL),
    PixiiButtonDescription("start_discharging", "discharge", PixiiFeature.MANUAL_CONTROL),
    PixiiButtonDescription("stop_manual_power", "manual_stop", PixiiFeature.MANUAL_CONTROL),
    PixiiButtonDescription("start_target_soc", "target_soc_start", PixiiFeature.MANUAL_CONTROL),
    PixiiButtonDescription("stop_target_soc", "target_soc_stop", PixiiFeature.MANUAL_CONTROL),
    PixiiButtonDescription(
        "start_adaptive_peak_shaving",
        "aps_start",
        PixiiFeature.ADAPTIVE_PEAK_SHAVING,
    ),
    PixiiButtonDescription(
        "stop_adaptive_peak_shaving",
        "aps_stop",
        PixiiFeature.ADAPTIVE_PEAK_SHAVING,
    ),
    PixiiButtonDescription("start_battery_calibration", "calibration_start"),
)

UNSUPPORTED_PRODUCTION_BUTTONS = frozenset({
    "stop_manual_power", "stop_target_soc", "stop_adaptive_peak_shaving",
    "start_battery_calibration",
})


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up typed native-operation buttons for one entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return
    parent_device_id = ensure_gateway_device(hass, entry, _runtime_serial(runtime))
    async_add_entities(
        [
            PixiiNativeButton(runtime, description, parent_device_id)
            for description in BUTTON_DESCRIPTIONS
            if not hasattr(runtime, "production_adapter")
            or description.key not in UNSUPPORTED_PRODUCTION_BUTTONS
        ]
    )


class PixiiNativeButton(ButtonEntity):
    """Invoke one RuntimeData-native operation with HA caller identity."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    # Buttons never poll by default, so availability was fixed at startup
    # (often while a restart held authority closed). Poll to follow it.
    _attr_should_poll = True

    def __init__(
        self,
        runtime: Any,
        description: PixiiButtonDescription,
        parent_device_id: str | None = None,
    ) -> None:
        """Initialize one operation button."""
        self.runtime = runtime
        self.description = description
        serial = _runtime_serial(runtime)
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_translation_key = description.key
        self._attr_device_info = (
            gateway_device_info(serial)
            if description.feature is None
            else feature_device_info(serial, description.feature, parent_device_id)
        )

    @property
    def available(self) -> bool:
        """Expose controls only when authority and semantics are available."""
        if not callable(getattr(self.runtime, "async_execute_native", None)):
            return False
        authority = getattr(self.runtime, "production_control_enabled", None)
        if callable(authority) and not authority():
            return False
        if (
            self.description.operation
            in {"manual_stop", "target_soc_stop", "aps_stop", "calibration_start"}
            and hasattr(self.runtime, "production_adapter")
        ):
            return False
        return True

    async def async_press(self) -> None:
        """Execute the typed operation; never publish or build a payload here."""
        executor = getattr(self.runtime, "async_execute_native", None)
        if not callable(executor):
            raise HomeAssistantError("Native Pixii operations are unavailable for this runtime")
        result = executor(self.description.operation, _user_id(self))
        if isawaitable(result):
            await result


def _user_id(entity: ButtonEntity) -> str | None:
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
