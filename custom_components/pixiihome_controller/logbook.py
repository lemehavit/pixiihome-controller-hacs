"""Describe the cause events behind Pixii logbook entries.

A cause event shares its context with the logbook entry it explains, so Home
Assistant shows its text under *What happened* for that entry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import Event, HomeAssistant, callback

from .activity import EVENT_CAUSE
from .const import DOMAIN


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, Any]]], None],
) -> None:
    @callback
    def describe(event: Event) -> dict[str, Any]:
        return {"name": "Pixiihome Controller", "message": str(event.data.get("message", ""))}

    async_describe_event(DOMAIN, EVENT_CAUSE, describe)
