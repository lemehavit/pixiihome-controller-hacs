from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.pixiihome_controller.activity import EVENT_LOGBOOK_ENTRY, log_activity
from custom_components.pixiihome_controller.price_controller import PriceOptimizationController


class Bus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event: str, data: dict, **_: object) -> None:
        if event == "logbook_entry":
            self.events.append((event, data))


def test_activity_entry_is_a_logbook_event_and_never_raises() -> None:
    bus = Bus()
    runtime = SimpleNamespace(hass=SimpleNamespace(bus=bus), serial="234301002683")

    log_activity(runtime, "price_optimization_status", "Scheduled charge 14:00–15:00")
    log_activity(SimpleNamespace(), "price_optimization_status", "ignored without a bus")

    assert bus.events == [(
        EVENT_LOGBOOK_ENTRY,
        {"name": "Pixii", "message": "Scheduled charge 14:00–15:00",
         "domain": "pixiihome_controller"},
    )]


@pytest.mark.asyncio
async def test_price_schedule_creation_is_logged_with_time_power_and_profit() -> None:
    from tests.test_price_controller import runtime

    instance, mutations = runtime()
    bus = Bus()
    instance.hass.bus = bus
    instance.serial = "234301002683"
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    messages = [data["message"] for _, data in bus.events]
    plans = [message for message in messages if message.startswith("New plan")]
    assert len(plans) == 1 and "expected profit" in plans[0].lower()
    messages = [message for message in messages if message not in plans]
    assert len(messages) == len(mutations.created)
    assert all(message.startswith("Scheduled ") and " kW" in message and "kr/kWh" in message
               for message in messages)
    assert controller.status != "planning"


def test_cause_is_fired_first_in_the_same_context() -> None:
    from custom_components.pixiihome_controller.activity import EVENT_CAUSE

    fired: list[tuple[str, dict, object]] = []

    class ContextBus:
        def async_fire(self, event: str, data: dict, context: object = None) -> None:
            fired.append((event, data, context))

    runtime = SimpleNamespace(hass=SimpleNamespace(bus=ContextBus()), serial="1")
    log_activity(runtime, "price_optimization_status", "Scheduled charge", "Who: price")
    assert [event for event, _, _ in fired] == [EVENT_CAUSE, "logbook_entry"]
    assert fired[0][1]["message"] == "Who: price"
    assert fired[0][2] is fired[1][2] is not None
