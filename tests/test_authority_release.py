from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.pixiihome_controller.runtime import RuntimeData


class _Controller:
    def __init__(self, events: list[str], name: str, *, live_after_stop: bool = False) -> None:
        self.events, self.name, self.live_after_stop = events, name, live_after_stop

    async def stop(self) -> None:
        self.events.append(f"{self.name}_stop")

    def owns_live_lease(self) -> bool:
        return self.live_after_stop


def _fake(events: list[str], problems: list[str]):
    async def release():
        events.append("price_release")
        return problems

    return SimpleNamespace(
        _controller_transition_lock=asyncio.Lock(),
        adaptive_peak_shaving_controller=_Controller(events, "aps"),
        solar_charging_controller=_Controller(events, "solar", live_after_stop=True),
        price_optimization_controller=SimpleNamespace(release_for_authority_off=release),
    )


@pytest.mark.asyncio
async def test_release_stops_leases_and_price_schedules_and_reports_leftovers() -> None:
    events: list[str] = []
    fake = _fake(events, ["price_1_ack_timeout"])

    problems = await RuntimeData._async_release_owned_control(fake)  # type: ignore[arg-type]

    assert events == ["aps_stop", "solar_stop", "price_release"]
    assert problems == ["solar_charging_controller_lease_still_active", "price_1_ack_timeout"]


@pytest.mark.asyncio
async def test_authority_off_releases_owned_control_before_closing_the_gate(monkeypatch) -> None:
    events: list[str] = []
    gate = SimpleNamespace(live_writes_enabled=True, blocked_reasons=set())
    notifications: list[str] = []

    async def release():
        events.append(f"release_gate_open={gate.live_writes_enabled}")
        return ["price_1_ack_timeout"]

    async def admin(user_id):
        return None

    async def save(candidate):
        events.append("persist")

    journal = SimpleNamespace(
        controller_state={}, with_product_state=lambda controller_state: journal,
    )
    fake = SimpleNamespace(
        _require_admin=admin,
        production_control_enabled=lambda: True,
        _async_release_owned_control=release,
        meter_limit_load_balancer=None,
        _control_journal_lock=asyncio.Lock(),
        control_journal=journal,
        production_authority=SimpleNamespace(revision=1),
        _save_control_journal_verified=save,
        controller_state=None,
        release_gate=gate,
        hass=object(),
        serial="234301002683",
    )
    from homeassistant.components import persistent_notification

    monkeypatch.setattr(
        persistent_notification, "async_create",
        lambda hass, message, **kwargs: notifications.append(message),
    )

    await RuntimeData.async_set_production_control_enabled(fake, False, None)  # type: ignore[arg-type]

    assert events == ["release_gate_open=True", "persist"]
    assert gate.live_writes_enabled is False
    assert notifications and "price_1_ack_timeout" in notifications[0]
