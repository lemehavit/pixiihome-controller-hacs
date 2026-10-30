from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

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
    saved: list[object] = []

    async def release():
        events.append(f"release_gate_open={gate.live_writes_enabled}")
        return ["price_1_ack_timeout"]

    async def admin(user_id):
        return None

    async def save(candidate):
        events.append("persist")
        saved.append(candidate)

    journal = SimpleNamespace(
        controller_state={},
        with_product_state=lambda controller_state: SimpleNamespace(
            controller_state=controller_state,
        ),
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
    assert len(saved) == 1
    assert saved[0].controller_state["production_control_authority"]["enabled"] is False
    assert notifications and "price_1_ack_timeout" in notifications[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("restore_error", [None, ValueError, OSError])
async def test_authority_off_persists_after_load_balancing_restore_failure(
    monkeypatch, restore_error: type[Exception] | None,
) -> None:
    from custom_components.pixiihome_controller import runtime as runtime_module

    events: list[str] = []
    notifications: list[str] = []
    activity: list[dict] = []
    saved: list[object] = []
    gate = SimpleNamespace(live_writes_enabled=True, blocked_reasons=set())
    lease = {"original": {}, "owned": [{}]}

    async def release():
        events.append("release")
        return []

    async def deactivate():
        events.append("restore_attempt")
        if restore_error is not None:
            raise restore_error(
                "meter_limitation_recovery_conflict"
                if restore_error is ValueError else "Gateway read failed"
            )
        fake.meter_limit_load_balancer.lease = None

    async def admin(user_id):
        return None

    async def save(candidate):
        events.append("persist")
        saved.append(candidate)

    journal = SimpleNamespace(
        controller_state={},
        with_product_state=lambda controller_state: SimpleNamespace(
            controller_state=controller_state,
        ),
    )
    fake = SimpleNamespace(
        _require_admin=admin,
        production_control_enabled=lambda: True,
        _async_release_owned_control=release,
        meter_limit_load_balancer=SimpleNamespace(lease=lease, deactivate=deactivate),
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
    monkeypatch.setattr(runtime_module, "log_activity",
                        lambda *args, **kwargs: activity.append(kwargs))

    await RuntimeData.async_set_production_control_enabled(fake, False, None)  # type: ignore[arg-type]

    assert events == ["release", "restore_attempt", "persist"]
    assert fake.production_authority.enabled is False
    assert len(saved) == 1
    assert saved[0].controller_state["production_control_authority"]["enabled"] is False
    assert gate.live_writes_enabled is False
    if restore_error is not None:
        assert fake.meter_limit_load_balancer.lease is lease
        assert len(notifications) == 1
        assert "load_balancing" in notifications[0]
        assert (
            "meter_limitation_recovery_conflict"
            if restore_error is ValueError
            else "Gateway read failed"
        ) in notifications[0]
        assert (
            "meter_limitation_recovery_conflict"
            if restore_error is ValueError else "Gateway read failed"
        ) in activity[-1]["cause"]
    else:
        assert fake.meter_limit_load_balancer.lease is None
        assert not notifications


def test_later_observer_restore_dismisses_only_resolved_cleanup(monkeypatch) -> None:
    from homeassistant.components import persistent_notification

    created: list[str] = []
    dismissed: list[str] = []
    monkeypatch.setattr(persistent_notification, "async_create",
                        lambda _hass, message, **_kwargs: created.append(message))
    monkeypatch.setattr(persistent_notification, "async_dismiss",
                        lambda _hass, notification_id: dismissed.append(notification_id))
    fake = SimpleNamespace(hass=object(), serial="GATEWAY")
    RuntimeData._update_authority_cleanup_notification(
        fake, ["load_balancing Service 120 (read failed)", "price_1_ack_timeout"]
    )
    RuntimeData._meter_limitation_restored(fake)
    assert "price_1_ack_timeout" in created[-1]
    assert "Service 120" not in created[-1]
    assert dismissed == []
    RuntimeData._update_authority_cleanup_notification(fake, [])
    assert dismissed == ["pixiihome_controller_GATEWAY_authority_cleanup"]

    RuntimeData._update_authority_cleanup_notification(
        fake, ["load_balancing Service 120 (read failed)"]
    )
    RuntimeData._meter_limitation_restored(fake)
    assert dismissed == [
        "pixiihome_controller_GATEWAY_authority_cleanup",
        "pixiihome_controller_GATEWAY_authority_cleanup",
    ]
    assert fake._authority_cleanup_problems == []

    RuntimeData._update_authority_cleanup_notification(
        fake, ["load_balancing Service 120 (read failed)", "price_1_ack_timeout"],
        persisted=False,
    )
    RuntimeData._meter_limitation_restored(fake)
    assert "price_1_ack_timeout" in created[-1]
    assert "Service 120" not in created[-1]
    assert "turned off in memory" in created[-1]
    assert "could not be saved" in created[-1]


@pytest.mark.asyncio
async def test_missing_journal_warns_about_unrestored_service_120_before_error(monkeypatch) -> None:
    from homeassistant.components import persistent_notification

    events: list[str] = []
    gate = SimpleNamespace(live_writes_enabled=True)

    async def admin(_user_id):
        return None

    async def release():
        return []

    async def deactivate():
        events.append("restore_attempt")
        raise OSError("Gateway read failed")

    def notify(_hass, message, **_kwargs):
        assert gate.live_writes_enabled is False
        assert "Service 120" in message
        assert "Gateway read failed" in message
        assert "turned off in memory" in message
        assert "could not be saved" in message
        events.append("notification")

    def fail_closed(reason):
        assert reason == "control_journal_unavailable"
        gate.live_writes_enabled = False
        events.append("fail_closed")

    monkeypatch.setattr(persistent_notification, "async_create", notify)
    fake = SimpleNamespace(
        _require_admin=admin, production_control_enabled=lambda: True,
        _async_release_owned_control=release,
        meter_limit_load_balancer=SimpleNamespace(lease={"original": {}}, deactivate=deactivate),
        _control_journal_lock=asyncio.Lock(), control_journal=None,
        _fail_closed_control_state=fail_closed,
        release_gate=gate, hass=object(), serial="GATEWAY",
    )
    with pytest.raises(HomeAssistantError, match="ControllerStore is unavailable"):
        await RuntimeData.async_set_production_control_enabled(fake, False, None)
    assert events == ["restore_attempt", "fail_closed", "notification"]
    assert gate.live_writes_enabled is False


@pytest.mark.asyncio
async def test_missing_journal_preserves_prior_reason_when_notification_fails(
    monkeypatch, caplog,
) -> None:
    from homeassistant.components import persistent_notification

    from custom_components.pixiihome_controller import runtime as runtime_module

    events = []
    gate = SimpleNamespace(live_writes_enabled=True)

    async def admin(_user_id):
        return None

    async def release():
        return []

    async def deactivate():
        raise OSError("Gateway unavailable")

    def fail_closed(reason):
        events.append(reason)
        gate.live_writes_enabled = False

    def fail_notification(*_args, **_kwargs):
        raise OSError("notification unavailable")

    def fail_activity(*_args, **_kwargs):
        raise OSError("activity unavailable")

    monkeypatch.setattr(persistent_notification, "async_create", fail_notification)
    monkeypatch.setattr(runtime_module, "log_activity", fail_activity)
    fake = SimpleNamespace(
        _require_admin=admin, production_control_enabled=lambda: True,
        _async_release_owned_control=release,
        meter_limit_load_balancer=SimpleNamespace(lease={"original": {}}, deactivate=deactivate),
        _control_journal_lock=asyncio.Lock(), control_journal=None,
        control_state_error="control_journal_invalid",
        _fail_closed_control_state=fail_closed,
        release_gate=gate, hass=object(), serial="GATEWAY",
    )
    with pytest.raises(HomeAssistantError, match="ControllerStore is unavailable"):
        await RuntimeData.async_set_production_control_enabled(fake, False, None)
    assert events == ["control_journal_invalid"]
    assert fake._authority_cleanup_persisted is False
    assert "Could not report authority cleanup problems" in caplog.text
    assert "Could not log authority cleanup problems" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("reporting_error", [None, "notification", "activity"])
async def test_journal_save_failure_warns_about_service_120_and_keeps_original_error(
    monkeypatch, reporting_error,
) -> None:
    from homeassistant.components import persistent_notification

    from custom_components.pixiihome_controller import runtime as runtime_module

    notifications: list[str] = []
    activity: list[tuple[tuple, dict]] = []
    def notify(_hass, message, **_kwargs):
        if reporting_error == "notification":
            raise OSError("notification unavailable")
        notifications.append(message)

    def record_activity(*args, **kwargs):
        if reporting_error == "activity":
            raise OSError("activity unavailable")
        activity.append((args, kwargs))

    monkeypatch.setattr(persistent_notification, "async_create", notify)
    monkeypatch.setattr(runtime_module, "log_activity", record_activity)
    gate = SimpleNamespace(live_writes_enabled=True, blocked_reasons=set())
    save_error = OSError("journal disk full")

    async def admin(_user_id):
        return None

    async def release():
        return []

    async def deactivate():
        raise OSError("Gateway read failed")

    async def save(_candidate):
        raise save_error

    def fail_closed(_reason):
        gate.live_writes_enabled = False

    fake = SimpleNamespace(
        _require_admin=admin, production_control_enabled=lambda: True,
        _async_release_owned_control=release,
        meter_limit_load_balancer=SimpleNamespace(lease={"original": {}}, deactivate=deactivate),
        _control_journal_lock=asyncio.Lock(),
        control_journal=SimpleNamespace(
            controller_state={}, with_product_state=lambda **_kwargs: object()
        ),
        production_authority=SimpleNamespace(revision=1),
        _save_control_journal_verified=save, _fail_closed_control_state=fail_closed,
        controller_state=None, release_gate=gate, hass=object(), serial="GATEWAY",
    )
    with pytest.raises(OSError) as caught:
        await RuntimeData.async_set_production_control_enabled(fake, False, None)
    assert caught.value is save_error
    assert gate.live_writes_enabled is False
    if reporting_error != "notification":
        assert "Service 120" in notifications[0]
        assert "Gateway read failed" in notifications[0]
        assert "turned off in memory" in notifications[0]
        assert "could not be saved" in notifications[0]
    if reporting_error != "activity":
        assert activity[-1][1]["cause"] == "load_balancing Service 120 (Gateway read failed)"
