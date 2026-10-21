"""Read-only diagnostic binary sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .runtime import RuntimeData


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([PixiiConnectivitySensor(hass.data[DOMAIN][entry.entry_id])])


class PixiiConnectivitySensor(BinarySensorEntity):
    _attr_has_entity_name = True
    _attr_name = "MQTT telemetry connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, runtime: RuntimeData) -> None:
        self.runtime = runtime
        self._attr_unique_id = f"{runtime.serial}_mqtt_connected"
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
    def is_on(self) -> bool:
        return any(
            self.runtime.telemetry.is_fresh(family)
            for family in ("core", "battery", "meter")
        )

    @property
    def available(self) -> bool:
        return self.runtime.capabilities.messages_seen > 0
