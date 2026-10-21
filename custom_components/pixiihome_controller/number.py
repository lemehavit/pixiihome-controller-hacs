"""Home Assistant number entities for canonical Pixii operational settings."""

from __future__ import annotations

from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .control.settings import RETIRED_OPERATIONAL_SETTINGS, SETTING_SPECS
from .devices import ensure_gateway_device, feature_device_info, setting_feature
from .simulator_entry import SIMULATOR_SERIAL


@dataclass(frozen=True, slots=True)
class PixiiNumberDescription:
    """Description for one persisted operational setting."""

    key: str


NUMBER_DESCRIPTIONS: tuple[PixiiNumberDescription, ...] = tuple(
    PixiiNumberDescription(key)
    for key, spec in SETTING_SPECS.items()
    if not isinstance(spec.default, bool) and key not in RETIRED_OPERATIONAL_SETTINGS
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the canonical operational setting entities for one entry."""
    runtime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None:
        return
    parent_device_id = ensure_gateway_device(hass, entry, _runtime_serial(runtime))
    async_add_entities(
        [
            PixiiOperationalNumber(runtime, description, parent_device_id)
            for description in NUMBER_DESCRIPTIONS
        ]
    )


class PixiiOperationalNumber(NumberEntity):
    """Expose one validated operational setting without a transport path."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.AUTO

    def __init__(
        self,
        runtime: Any,
        description: PixiiNumberDescription,
        parent_device_id: str | None = None,
    ) -> None:
        """Initialize one setting-backed number entity."""
        self.runtime = runtime
        self.description = description
        spec = SETTING_SPECS[description.key]
        if spec.minimum is None or spec.maximum is None or spec.step is None:
            raise ValueError("number_setting_bounds_required")
        serial = _runtime_serial(runtime)
        self._attr_unique_id = f"{serial}_{description.key}"
        self._attr_translation_key = description.key
        self._attr_native_min_value = float(spec.minimum)
        self._attr_native_max_value = float(spec.maximum)
        self._attr_native_step = float(spec.step)
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_info = feature_device_info(
            serial,
            setting_feature(description.key),
            parent_device_id,
        )

    @property
    def available(self) -> bool:
        """Only advertise a setting when both read and write contracts exist."""
        return _settings(self.runtime) is not None and callable(
            getattr(self.runtime, "async_set_operational_setting", None)
        )

    @property
    def native_value(self) -> float | int | None:
        """Return the validated value, or no state when the contract is absent."""
        settings = _settings(self.runtime)
        if settings is None:
            return None
        try:
            value = settings.get(self.description.key)
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.description.key in {
            "price_min_difference_sek_per_kwh",
            "price_export_min_difference_sek_per_kwh",
        }:
            return {
                "calculation": "minimum_net_profit_per_delivered_ac_kwh",
                "deducts": ["grid_import_cost", "round_trip_losses", "battery_cycle_cost"],
                "grid_fee_avoided_for_self_consumption": True,
            }
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Persist through RuntimeData's canonical authority method only."""
        setter = getattr(self.runtime, "async_set_operational_setting", None)
        if not callable(setter):
            raise HomeAssistantError(
                f"Operational setting {self.description.key} is unavailable for this runtime"
            )
        result = setter(self.description.key, value, _user_id(self))
        if isawaitable(result):
            await result


def _settings(runtime: Any) -> Any | None:
    """Return a usable settings snapshot without assuming a runtime class."""
    try:
        settings = getattr(runtime, "operational_settings", None)
    except Exception:  # noqa: BLE001 - unavailable runtimes must fail closed
        return None
    return settings if callable(getattr(settings, "get", None)) else None


def _user_id(entity: NumberEntity) -> str | None:
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
