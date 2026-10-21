"""Remove only obsolete entities owned by this integration and config entry."""

from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .control.settings import RETIRED_OPERATIONAL_SETTINGS


def remove_retired_entities(hass, entry, serial: str) -> None:
    from .button import UNSUPPORTED_PRODUCTION_BUTTONS

    registry = er.async_get(hass)
    retired = {
        *(('number', f'{serial}_{key}') for key in RETIRED_OPERATIONAL_SETTINGS),
        *(('button', f'{serial}_{key}') for key in UNSUPPORTED_PRODUCTION_BUTTONS),
        ('sensor', f'{serial}_controller_status'),
    }
    for entity in tuple(er.async_entries_for_config_entry(registry, entry.entry_id)):
        if entity.platform == DOMAIN and (entity.domain, entity.unique_id) in retired:
            registry.async_remove(entity.entity_id)
