"""Readable Home Assistant activity (logbook) entries for actions actually taken.

Entities only log state changes. These entries record what a controller did:
which schedule was created or removed, which limit changed and why.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.util import dt as dt_util

from .const import DOMAIN

EVENT_LOGBOOK_ENTRY = "logbook_entry"


def log_activity(runtime: Any, status_key: str, message: str) -> None:
    """Fire one logbook entry attached to the feature's status sensor.

    Logging must never affect control, so every failure is swallowed.
    """
    try:
        hass = getattr(runtime, "hass", None)
        fire = getattr(getattr(hass, "bus", None), "async_fire", None)
        if not callable(fire):
            return
        data: dict[str, Any] = {"name": "Pixii", "message": message, "domain": DOMAIN}
        entity_id = _status_entity_id(hass, runtime, status_key)
        if entity_id is not None:
            data["entity_id"] = entity_id
        fire(EVENT_LOGBOOK_ENTRY, data)
    except Exception:  # noqa: BLE001 - activity logging is best effort only
        return


def clock(value: datetime) -> str:
    """Local ``HH:MM`` for a UTC instant."""
    return dt_util.as_local(value).strftime("%H:%M")


def span(start: datetime, end: datetime) -> str:
    return f"{clock(start)}–{clock(end)}"


def _status_entity_id(hass: Any, runtime: Any, status_key: str) -> str | None:
    try:
        from homeassistant.helpers import entity_registry as er

        return er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{runtime.serial}_{status_key}"
        )
    except Exception:  # noqa: BLE001 - an unregistered sensor only loses the link
        return None


__all__ = ["clock", "log_activity", "span"]
