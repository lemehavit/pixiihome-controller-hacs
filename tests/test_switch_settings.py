from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.pixiihome_controller.switch import (
    SWITCH_DESCRIPTIONS,
    PixiiControllerSwitch,
)


class _Runtime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.operational_settings = {"price_schedule_writes_enabled": False,
                                     "price_export_allowed": False}
        self.controller_state = SimpleNamespace(automatic_price_optimization_enabled=False)
        self.config = SimpleNamespace(gateway_serial="TEST_GATEWAY_001")

    async def async_set_operational_setting(self, key, value, user_id):
        self.calls.append(("setting", key))
        self.operational_settings[key] = value

    async def async_set_controller_enabled(self, key, enabled, user_id):
        self.calls.append(("controller", key))
        setattr(self.controller_state, key, enabled)


def _switch(runtime: _Runtime, key: str) -> PixiiControllerSwitch:
    description = next(d for d in SWITCH_DESCRIPTIONS if d.key == key)
    return PixiiControllerSwitch(runtime, description)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["price_schedule_writes_enabled", "price_export_allowed"])
async def test_setting_switch_uses_operational_setting_setter(key: str) -> None:
    runtime = _Runtime()
    switch = _switch(runtime, key)

    assert switch.available
    await switch.async_turn_on()

    assert runtime.calls == [("setting", key)]
    assert switch.is_on
    await switch.async_turn_off()
    assert not switch.is_on


@pytest.mark.asyncio
async def test_controller_switch_still_uses_controller_setter() -> None:
    runtime = _Runtime()
    switch = _switch(runtime, "automatic_price_optimization")

    await switch.async_turn_on()

    assert runtime.calls == [("controller", "automatic_price_optimization_enabled")]
