"""Exact priority ownership, simulated Gateway transitions and crash recovery."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from custom_components.pixiihome_controller.aps_controller import AdaptivePeakShavingController
from custom_components.pixiihome_controller.aps_priority import APS_PRIORITY, ApsPriority
from custom_components.pixiihome_controller.battery import BatteryReading
from custom_components.pixiihome_controller.control.phase_gateway import PHASE_FIELDS
from custom_components.pixiihome_controller.control.policy import AutomationPolicy
from custom_components.pixiihome_controller.control.settings import OperationalSettings
from custom_components.pixiihome_controller.idle_phase_controller import IdlePhaseLoadBalancer
from custom_components.pixiihome_controller.solar_controller import SolarChargingController


class Gateway:
    def __init__(self, used: list[int], service: int = 105) -> None:
        self.used = used
        self.service = service
        self.writes: list[list[int]] = []
        self.fail_read = False
        self.fail_write = False
        self.phase = dict.fromkeys(PHASE_FIELDS, 0)

    async def read(self, service: int) -> dict[str, Any]:
        if self.fail_read:
            raise ValueError("unreadable")
        if service == 129:
            return self.phase
        return {"enabled": 1, "used_list": list(self.used), "profile": 0, "running_profile": 0}

    async def priority(self, used: list[int]) -> None:
        self.used = list(used)
        self.writes.append(list(used))
        if 105 not in used and self.service == 105:
            self.service = 0
        if self.fail_write:
            raise ValueError("readback_uncertain")

    async def core(self) -> dict[str, int]:
        return {"srv_type": self.service}

    @staticmethod
    def settings(data: dict[str, Any]) -> dict[str, Any]:
        return data

    async def switch(self, on: bool) -> None:
        assert on is False  # Only service-129 stop, never a global idle command.
        self.service = 0

    async def save(self, settings: dict[str, Any]) -> None:
        self.phase = settings


class Runtime:
    def __init__(self, gateway: Gateway, saved: dict[str, Any] | None = None) -> None:
        self.native_priority_lock = asyncio.Lock()
        self.control_journal = SimpleNamespace(controller_state=deepcopy(saved or {}))
        self.gateway_scheduler = SimpleNamespace(client=gateway)
        self.meter_limit_load_balancer = None
        self.aps_priority = ApsPriority(cast(Any, self))
        self.operational_settings = OperationalSettings.defaults()
        self.battery = BatteryReading(usable_soc=50, charge_power_limit_w=5000,
                                      discharge_power_limit_w=5000)
        self.controller_state = SimpleNamespace(automatic_solar_charging_enabled=False,
                                               adaptive_peak_shaving_enabled=False)
        self.fail_save_at = 0
        self.saves = 0

    async def async_save_aps_state(self, key: str, value: Any) -> None:
        self.saves += 1
        if self.saves == self.fail_save_at:
            raise ValueError("storage_failed")
        self.control_journal.controller_state[key] = deepcopy(value)

    def _automation_policy(self) -> AutomationPolicy:
        return AutomationPolicy(
            policy_id="aps", schema_version=1, revision=1, enabled=True,
            allowed_operation_classes=("automatic",),
            allowed_service_classes=("adaptivepeakshaving",),
            time_window={"start": "00:00", "end": "23:59", "days": list(range(7))},
            max_duration_seconds=300, soc_min_percent=10, soc_max_percent=90,
            max_power_kw=8, external_control_allowed=False,
        )


@pytest.fixture(autouse=True)
def gateway_adapter(monkeypatch):
    for module in ("aps_priority", "idle_phase_controller"):
        monkeypatch.setattr(f"custom_components.pixiihome_controller.{module}.PhaseGateway",
                            lambda client: client)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [SolarChargingController, AdaptivePeakShavingController])
@pytest.mark.parametrize("service", [105, 101, 102])
async def test_disable_without_lease_restores_priority_and_preserves_schedules(kind, service):
    gateway = Gateway([102, 101, 107], service)
    runtime = Runtime(gateway)
    controller = kind(cast(Any, runtime))
    await controller.prepare()
    assert gateway.used == [102, 101, 105, 107]
    await controller.stop()
    await controller.stop()
    assert gateway.used == [102, 101, 107]
    assert gateway.service == (0 if service == 105 else service)
    assert gateway.writes == [[102, 101, 105, 107], [102, 101, 107]]
    assert runtime.control_journal.controller_state[APS_PRIORITY] is None


@pytest.mark.asyncio
async def test_preexisting_aps_is_not_owned_or_removed():
    gateway = Gateway([101, 105, 107])
    runtime = Runtime(gateway)
    controller = SolarChargingController(cast(Any, runtime))
    await controller.prepare()
    await controller.stop()
    assert gateway.writes == []
    assert gateway.service == 105
    assert runtime.aps_priority.status == "preexisting"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["external", "read"])
async def test_conflict_is_persistent_visible_and_never_overwritten(failure):
    gateway = Gateway([101, 107])
    runtime = Runtime(gateway)
    await runtime.aps_priority.acquire("solar", SolarChargingController.priority_with_aps)
    if failure == "external":
        gateway.used = [102, *gateway.used]
    else:
        gateway.fail_read = True
    with pytest.raises(ValueError):
        await runtime.aps_priority.release()
    assert runtime.aps_priority.status == "recovery_required"
    assert runtime.control_journal.controller_state[APS_PRIORITY]["status"] in {
        "unreadable", "aps_priority_recovery_conflict"
    }
    restarted = Runtime(gateway, runtime.control_journal.controller_state)
    with pytest.raises(ValueError):
        await restarted.aps_priority.release()
    assert len(gateway.writes) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("save_failure", [1, 2])
async def test_storage_failure_does_not_lose_original_or_write_without_journal(save_failure):
    gateway = Gateway([101, 107])
    runtime = Runtime(gateway)
    runtime.fail_save_at = save_failure
    with pytest.raises(ValueError, match="storage_failed"):
        await runtime.aps_priority.acquire("aps", AdaptivePeakShavingController.priority_with_aps)
    assert len(gateway.writes) == (0 if save_failure == 1 else 1)
    restarted = Runtime(gateway, runtime.control_journal.controller_state)
    await restarted.aps_priority.release()
    assert gateway.used == [101, 107]


@pytest.mark.asyncio
async def test_uncertain_write_is_restored_after_restart():
    gateway = Gateway([101, 107])
    runtime = Runtime(gateway)
    gateway.fail_write = True
    with pytest.raises(ValueError, match="readback_uncertain"):
        await runtime.aps_priority.acquire("aps", AdaptivePeakShavingController.priority_with_aps)
    restarted = Runtime(gateway, runtime.control_journal.controller_state)
    gateway.fail_write = False
    await restarted.aps_priority.release()
    assert gateway.used == [101, 107]
    assert gateway.service == 0


@pytest.mark.asyncio
async def test_restores_service_129_before_aps_base_priority(monkeypatch):
    class MemoryStore:
        saved: Any = None

        def __init__(self, *_args):
            pass

        async def async_load(self):
            return deepcopy(self.saved)

        async def async_save(self, value):
            self.saved = deepcopy(value)

    monkeypatch.setattr("custom_components.pixiihome_controller.idle_phase_controller.Store",
                        MemoryStore)
    gateway = Gateway([101, 107])
    runtime = Runtime(gateway)
    runtime.hass, runtime.serial = SimpleNamespace(), "test"  # type: ignore[attr-defined]
    await runtime.aps_priority.acquire("solar", SolarChargingController.priority_with_aps)
    idle = IdlePhaseLoadBalancer(cast(Any, runtime))
    runtime.meter_limit_load_balancer = SimpleNamespace(idle_support=idle)
    original = dict(gateway.phase)
    await idle._persist({"original": original, "priority": [101, 105, 107],
                         "owned_priority": [101, 105, 107, 129], "owned": [original]})
    gateway.used, gateway.service = [101, 105, 107, 129], 129
    await runtime.aps_priority.release()
    assert gateway.writes[-2:] == [[101, 105, 107], [101, 107]]
    assert idle.lease is None
    assert gateway.service == 0

@pytest.mark.asyncio
@pytest.mark.parametrize("kind,owner", [(SolarChargingController, "solar"),
                                        (AdaptivePeakShavingController, "aps")])
async def test_disabled_recovery_rechecks_priority_without_repeating_lease_stop(kind, owner):
    gateway = Gateway([101, 107])
    runtime = Runtime(gateway)
    controller = kind(cast(Any, runtime))
    await controller.prepare()
    controller._message_id, controller._lease_deadline = 8001, controller.clock() + 120
    stops = []
    async def stop_lease(*args, **kwargs):
        stops.append(1)
        return {"successful": False}
    runtime.async_execute_solar_lease = stop_lease
    runtime.async_execute_peak_shaving_lease = stop_lease
    runtime.release_solar_lease_identity = runtime.release_aps_lease_identity = lambda _: None
    gateway.fail_read = True
    with pytest.raises(ValueError):
        await controller.stop()
    assert stops == [1]
    gateway.fail_read = False
    await controller.tick()
    assert gateway.used == [101, 107]
    assert stops == [1]
    assert not runtime.aps_priority.pending(owner)


@pytest.mark.asyncio
async def test_priority_activity_records_owner_lists_outcomes_and_conflicts(monkeypatch):
    events = []
    monkeypatch.setattr("custom_components.pixiihome_controller.aps_priority.log_activity",
                        lambda runtime, key, message: events.append(message))
    gateway = Gateway([101, 107])
    runtime = Runtime(gateway)
    await runtime.aps_priority.acquire("solar", SolarChargingController.priority_with_aps)
    gateway.fail_read = True
    with pytest.raises(ValueError):
        await runtime.aps_priority.release()
    gateway.fail_read = False
    await runtime.aps_priority.release()
    assert all("owner=solar" in text and "original=[101, 107]" in text
               and "written=[101, 105, 107]" in text for text in events)
    assert any("written and verified" in text for text in events)
    assert any("recovery required" in text for text in events)
    assert any("original restored" in text for text in events)
