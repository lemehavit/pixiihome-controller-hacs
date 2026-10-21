from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest

import custom_components.pixiihome_controller as integration
import custom_components.pixiihome_controller.simulator_entry as simulator_entry_module
from custom_components.pixiihome_controller.const import CONF_RUNTIME_MODE, DOMAIN
from custom_components.pixiihome_controller.control.adapters import AdapterRequest
from custom_components.pixiihome_controller.control.engine import EngineStatus
from custom_components.pixiihome_controller.control.journal import JournalTargetIdentity
from custom_components.pixiihome_controller.control.models import ControlOperation, RuntimeMode
from custom_components.pixiihome_controller.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.pixiihome_controller.simulator_entry import (
    SIMULATOR_SCENARIOS,
    SimulatorEntryRuntime,
)


class MemoryStore:
    values: dict[str, dict[str, Any]] = {}

    def __init__(self, hass: Any, version: int, key: str) -> None:
        del hass, version
        self.key = key

    async def async_load(self) -> dict[str, Any] | None:
        value = self.values.get(self.key)
        return deepcopy(value) if value is not None else None

    async def async_save(self, value: dict[str, Any]) -> None:
        self.values[self.key] = deepcopy(value)


@pytest.fixture(autouse=True)
def isolated_store(monkeypatch: pytest.MonkeyPatch) -> None:
    MemoryStore.values = {}
    monkeypatch.setattr(simulator_entry_module, "Store", MemoryStore)


@pytest.mark.asyncio
async def test_simulator_entry_persists_and_restores_without_transport() -> None:
    entry = cast(Any, SimpleNamespace(entry_id="sim-entry"))
    first = SimulatorEntryRuntime(cast(Any, SimpleNamespace()), entry)
    await first.async_initialize()
    result = await first.async_execute(
        AdapterRequest(
            ControlOperation.DIRECT_TARGET_SOC,
            service="targetsoc",
            target_identity=JournalTargetIdentity(
                asset_id="battery-1",
                service_no=110,
            ),
            payload={"socref": 76},
        )
    )

    restored = SimulatorEntryRuntime(cast(Any, SimpleNamespace()), entry)
    await restored.async_initialize()

    assert result.status is EngineStatus.SUCCESS
    assert restored.simulator.adapter.battery.target_soc_percent == 76
    assert restored.simulator.journal.journal.config_entry_id == "sim-entry"
    assert not hasattr(restored, "transport")


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SIMULATOR_SCENARIOS)
async def test_each_approved_simulator_scenario_is_loadable(scenario: str) -> None:
    entry = cast(Any, SimpleNamespace(entry_id=f"sim-{scenario}"))
    runtime = SimulatorEntryRuntime(cast(Any, SimpleNamespace()), entry)
    await runtime.async_initialize()

    state = await runtime.async_load_scenario(scenario)

    assert state["journal"]["controller_state"]["scenario"] == scenario
    assert runtime.simulator.journal.journal.audit_history[-1]["event"] == "scenario_loaded"


@pytest.mark.asyncio
async def test_simulator_entry_diagnostics_are_sanitized_and_transport_free() -> None:
    entry = cast(Any, SimpleNamespace(entry_id="sim-diagnostics"))
    runtime = SimulatorEntryRuntime(cast(Any, SimpleNamespace()), entry)
    await runtime.async_initialize()
    hass = SimpleNamespace(data={DOMAIN: {entry.entry_id: runtime}})

    diagnostics = await async_get_config_entry_diagnostics(
        cast(Any, hass),
        entry,
    )

    assert diagnostics["runtime_mode"] == "simulator"
    assert diagnostics["config"]["entry_id"] == "**REDACTED**"
    assert diagnostics["read_only"]["live_writes_enabled"] is False
    assert diagnostics["simulator"]["schedule_count"] == 0
    assert "mqtt_telemetry" not in diagnostics


@pytest.mark.asyncio
async def test_setup_entry_simulator_never_waits_for_mqtt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initialized = False

    class FakeSimulatorEntryRuntime:
        def __init__(self, *, hass: Any, entry: Any) -> None:
            self.hass = hass
            self.entry = entry

        async def async_initialize(self) -> None:
            nonlocal initialized
            initialized = True

    async def forbidden_mqtt_wait(hass: Any) -> bool:
        del hass
        raise AssertionError("simulator setup contacted MQTT")

    monkeypatch.setattr(integration, "SimulatorEntryRuntime", FakeSimulatorEntryRuntime)
    monkeypatch.setattr(integration, "async_wait_for_mqtt_client", forbidden_mqtt_wait)
    hass = cast(Any, SimpleNamespace(data={}))
    entry = cast(
        Any,
        SimpleNamespace(
            entry_id="sim-entry",
            data={CONF_RUNTIME_MODE: RuntimeMode.SIMULATOR.value},
        ),
    )

    assert await integration.async_setup_entry(hass, entry)
    assert initialized
    assert hass.data[DOMAIN]["sim-entry"].entry is entry
