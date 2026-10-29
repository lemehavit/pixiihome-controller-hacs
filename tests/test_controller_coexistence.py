"""Regression coverage for controller coexistence and authorization windows."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import custom_components.pixiihome_controller.idle_phase_controller as idle_phase_module
import custom_components.pixiihome_controller.meter_limit_controller as meter_limit_module
from custom_components.pixiihome_controller.control.load_balancing import (
    LoadBalancingConfig,
    PhaseSample,
)
from custom_components.pixiihome_controller.control.phase_gateway import PHASE_FIELDS
from custom_components.pixiihome_controller.control.policy import AutomationPolicy
from custom_components.pixiihome_controller.idle_phase_controller import IdlePhaseLoadBalancer
from custom_components.pixiihome_controller.load_balancing_observer import LoadBalancingObserver
from custom_components.pixiihome_controller.meter_limit_controller import MeterLimitLoadBalancer
from custom_components.pixiihome_controller.runtime import RuntimeData
from custom_components.pixiihome_controller.sensor import PixiiLoadBalancingSensor

from .test_price_controller import policy


class LeaseStore:
    data: dict[str, dict] = {}

    def __init__(self, _hass, _version, key):
        self.key = key

    async def async_load(self):
        from copy import deepcopy
        return deepcopy(self.data.get(self.key))

    async def async_save(self, value):
        from copy import deepcopy
        self.data[self.key] = deepcopy(value)


def _restarted_controller(monkeypatch, native_record):
    LeaseStore.data = {}
    monkeypatch.setattr(meter_limit_module, "Store", LeaseStore)
    monkeypatch.setattr(idle_phase_module, "Store", LeaseStore)
    reads = []
    writes = []
    activity = []

    class Client:
        async def read_load_balancing_status(self, **_kwargs):
            reads.append("native")
            return dict(native_record)

        async def write_meter_limitation_settings(self, value):
            reads.append("native_write")
            writes.append(dict(value))
            native_record.clear()
            native_record.update(value)

    runtime = SimpleNamespace(
        hass=SimpleNamespace(), serial="GATEWAY",
        gateway_scheduler=SimpleNamespace(client=Client()),
        controller_state=SimpleNamespace(load_balancing_enabled=False),
        production_control_enabled=lambda: False,
    )
    def record_activity(*args, **kwargs):
        activity.append((args, kwargs))

    monkeypatch.setattr(meter_limit_module, "log_activity", record_activity)
    monkeypatch.setattr(idle_phase_module, "log_activity", record_activity)
    return runtime, reads, writes, activity


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.asyncio
async def test_restart_disabled_native_lease_checks_identity_before_restore(monkeypatch, changed):
    original = {
        "enabled": 0, "exp_soft_enabled": 0, "exp_hard_enabled": 0,
        "exp_soft_limit": 0, "exp_hard_limit": 0, "gen_soft_enabled": 0,
        "gen_hard_enabled": 0, "gen_soft_limit": 0, "gen_hard_limit": 0,
        "limit_per_phase": 0, "meter_id": 1,
    }
    owned = {**original, "enabled": 1, "exp_soft_enabled": 1, "exp_soft_limit": 6000}
    current = {**owned, "exp_soft_limit": 5000} if changed else dict(owned)
    runtime, reads, writes, activity = _restarted_controller(monkeypatch, current)
    lease = {"original": original, "owned": [owned]}
    LeaseStore.data["pixiihome_controller.GATEWAY.meter_limit_lease"] = {"lease": lease}
    controller = MeterLimitLoadBalancer(runtime)
    runtime.meter_limit_load_balancer = controller
    runtime.load_balancing_observer = SimpleNamespace(attributes=controller.attributes)
    await controller.load_saved_lease()
    await controller.idle_support.load_saved_lease()
    assert reads == []
    assert writes == []
    await controller.tick()
    sensor = PixiiLoadBalancingSensor(runtime, "load_balancing_observation")
    if changed:
        assert writes == []
        assert controller.lease == lease
        assert sensor.extra_state_attributes["control_status"] == "recovery_required"
        assert (
            sensor.extra_state_attributes["control_blocked_reason"]
            == "meter_limitation_recovery_conflict"
        )
    else:
        assert reads.index("native") < reads.index("native_write")
        assert writes == [original]
        assert controller.lease is None
        assert LeaseStore.data["pixiihome_controller.GATEWAY.meter_limit_lease"] == {"lease": None}
        assert sensor.extra_state_attributes["control_status"] == "disabled"
    assert any(kwargs.get("cause") for _args, kwargs in activity)


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.asyncio
async def test_restart_disabled_phase_lease_restores_only_owned_settings(monkeypatch, changed):
    runtime, reads, writes, activity = _restarted_controller(monkeypatch, {})
    original = dict.fromkeys(PHASE_FIELDS, 0)
    owned = {**original, "act_enabled": 1, "duration": 15}
    priority = [102, 101]
    lease = {"original": original, "owned": [original, owned],
             "priority": priority, "owned_priority": [*priority, 129]}
    LeaseStore.data["pixiihome_controller.GATEWAY.idle_phase_lease"] = {"lease": lease}
    phase = PhaseGateway()
    phase.writes = 0
    phase.record = {**owned, "duration": 12} if changed else dict(owned)
    phase.priority_list = [*priority, 129]
    phase.service = 129
    phase_reads = []
    original_read = phase.read
    original_save = phase.save

    async def read(service):
        phase_reads.append("read")
        return await original_read(service)

    async def save(value):
        phase_reads.append("save")
        await original_save(value)

    phase.read = read
    phase.save = save
    monkeypatch.setattr(idle_phase_module, "PhaseGateway", lambda _client: phase)
    controller = MeterLimitLoadBalancer(runtime)
    runtime.meter_limit_load_balancer = controller
    runtime.load_balancing_observer = SimpleNamespace(attributes=controller.attributes)
    await controller.load_saved_lease()
    await controller.idle_support.load_saved_lease()
    assert reads == phase_reads == writes == []
    await controller.tick()
    sensor = PixiiLoadBalancingSensor(runtime, "load_balancing_observation")
    if changed:
        assert phase.switches == []
        assert "save" not in phase_reads
        assert controller.idle_support.lease == lease
        assert sensor.extra_state_attributes["control_status"] == "recovery_required"
        assert (
            sensor.extra_state_attributes["control_blocked_reason"]
            == "idle_phase_recovery_conflict"
        )
    else:
        assert phase_reads.index("read") < phase_reads.index("save")
        assert phase.record == original
        assert phase.priority_list == priority
        assert phase.switches == [False]
        assert controller.idle_support.lease is None
        assert sensor.extra_state_attributes["control_status"] == "disabled"
    assert writes == []
    assert any(kwargs.get("cause") for _args, kwargs in activity)


@pytest.mark.asyncio
async def test_restart_disabled_empty_leases_never_touch_gateway(monkeypatch):
    runtime, reads, writes, _activity = _restarted_controller(monkeypatch, {})
    controller = MeterLimitLoadBalancer(runtime)
    await controller.load_saved_lease()
    await controller.idle_support.load_saved_lease()
    await controller.tick()
    assert controller.status == "disabled"
    assert reads == writes == []


@pytest.mark.asyncio
async def test_restart_disabled_restores_native_and_phase_leases_together(monkeypatch):
    original = {
        "enabled": 0, "exp_soft_enabled": 0, "exp_hard_enabled": 0,
        "exp_soft_limit": 0, "exp_hard_limit": 0, "gen_soft_enabled": 0,
        "gen_hard_enabled": 0, "gen_soft_limit": 0, "gen_hard_limit": 0,
        "limit_per_phase": 0, "meter_id": 1,
    }
    owned = {**original, "enabled": 1, "exp_soft_enabled": 1, "exp_soft_limit": 6000}
    runtime, reads, writes, _activity = _restarted_controller(monkeypatch, dict(owned))
    native_key = "pixiihome_controller.GATEWAY.meter_limit_lease"
    phase_key = "pixiihome_controller.GATEWAY.idle_phase_lease"
    LeaseStore.data[native_key] = {"lease": {"original": original, "owned": [owned]}}
    phase_original = dict.fromkeys(PHASE_FIELDS, 0)
    phase_owned = {**phase_original, "act_enabled": 1, "duration": 15}
    priority = [102, 101]
    LeaseStore.data[phase_key] = {"lease": {
        "original": phase_original, "owned": [phase_original, phase_owned],
        "priority": priority, "owned_priority": [*priority, 129],
    }}
    phase = PhaseGateway()
    phase.record = dict(phase_owned)
    phase.priority_list = [*priority, 129]
    phase.service = 129
    monkeypatch.setattr(idle_phase_module, "PhaseGateway", lambda _client: phase)
    controller = MeterLimitLoadBalancer(runtime)
    await controller.load_saved_lease()
    await controller.idle_support.load_saved_lease()
    assert reads == writes == []

    await controller.tick()

    assert writes == [original]
    assert phase.record == phase_original
    assert phase.priority_list == priority
    assert phase.switches == [False]
    assert LeaseStore.data[native_key] == {"lease": None}
    assert LeaseStore.data[phase_key] == {"lease": None}
    assert controller.status == "disabled"


def test_observer_starts_for_saved_lease_even_when_observation_is_disabled():
    started = []

    def create_background_task(coro, name):
        started.append(name)
        coro.close()
        return object()

    observer = object.__new__(LoadBalancingObserver)
    observer._task = None
    observer.config = SimpleNamespace(enabled=False)
    observer.runtime = SimpleNamespace(
        hass=SimpleNamespace(async_create_background_task=create_background_task),
        meter_limit_load_balancer=SimpleNamespace(
            lease=None, idle_support=SimpleNamespace(lease={"original": {}})
        ),
    )
    observer.start()
    assert started == ["pixiihome_controller load balancing observer"]


@pytest.mark.parametrize("start,duration,expected", [
    ("2026-10-16T07:59:30+02:00", 120, 0),
    ("2026-10-16T08:00:00+02:00", 120, 120),
    ("2026-10-16T08:59:30+02:00", 120, 30),
    ("2026-10-17T08:00:00+02:00", 120, 0),
])
def test_authorization_window_clips_real_lease_duration(start, duration, expected):
    configured = replace(policy(), time_window={"start": "08:00", "end": "08:59", "days": [4]})
    assert configured.bounded_duration(datetime.fromisoformat(start), duration) == expected


def test_overnight_policy_uses_the_start_day_and_stockholm_dst():
    configured = replace(policy(), time_window={"start": "22:00", "end": "02:59", "days": [5]})
    # The repeated hour on Sunday belongs to Saturday's overnight authorization.
    for hour in (0, 1):
        assert configured.bounded_duration(datetime(2026, 10, 25, hour, 30, tzinfo=UTC), 120) == 120
    assert configured.bounded_duration(datetime(2026, 10, 25, 2, 30, tzinfo=UTC), 120) == 0


@pytest.mark.parametrize("field,value", [
    ("max_power_kw", float("nan")), ("max_power_kw", float("inf")),
    ("time_window", {"start": "99:00", "end": "23:59", "days": [4]}),
])
def test_invalid_automation_policy_fails_closed(field, value):
    data = policy().as_dict()
    data[field] = value
    assert not AutomationPolicy.from_mapping(data).enabled


class PhaseGateway:
    def __init__(self):
        self.priority_list = [102, 101, 105, 107]
        self.record = dict.fromkeys(PHASE_FIELDS, 0)
        self.service = 0
        self.switches = []

    async def read(self, service):
        return ({"enabled": 1, "profile": 0, "running_profile": 0,
                 "used_list": self.priority_list} if service == 100 else self.record)

    def settings(self, data):
        return dict(data)

    async def priority(self, values):
        self.priority_list = list(values)

    async def save(self, values):
        self.record = dict(values)

    async def switch(self, on):
        self.switches.append(on)
        self.service = 129 if on else 0

    async def core(self):
        return {"srv_type": self.service}


@pytest.mark.asyncio
async def test_idle_phase_support_yields_to_all_existing_services_and_does_not_restart_them():
    controller = object.__new__(IdlePhaseLoadBalancer)
    controller.gateway = PhaseGateway()
    controller.lease = None
    controller._lock = asyncio.Lock()
    controller.targets = controller.previous = controller.last_tick = None
    controller.runtime = SimpleNamespace()
    async def persist(lease):
        controller.lease = lease
    controller._persist = persist
    await controller._apply((300, 0, -300))
    assert controller.gateway.priority_list == [102, 101, 105, 107, 129]
    assert controller.lease["owned_priority"][-1] == 129
    # Hardware arbitration hands over to an arriving support activation.
    controller.gateway.service = 102
    await controller.tick()
    assert controller.lease is None
    assert controller.gateway.priority_list == [102, 101, 105, 107]
    assert controller.gateway.switches == [True]
    assert controller.gateway.service == 102


def _report_policy(**changes) -> AutomationPolicy:
    """The reported policy: 100 W, SOC 80-100 %, 1 s, no weekdays, only targetsoc."""
    reported = {
        "allowed_service_classes": ("targetsoc",),
        "time_window": {"start": "00:00", "end": "23:59", "days": []},
        "max_duration_seconds": 1,
        "soc_min_percent": 80,
        "soc_max_percent": 100,
        "max_power_kw": 0.1,
    }
    return replace(policy(), **{**reported, **changes})


def _policy_controller(monkeypatch, configured, *, soc, grid_w):
    activity = []

    def record_policy(*args, **kwargs):
        if "policy" in args[2]:
            activity.append((args, kwargs))

    monkeypatch.setattr(idle_phase_module, "log_activity", record_policy)
    sample = PhaseSample(abs(grid_w) / 230, 230, grid_w, 0)
    observer = SimpleNamespace(
        config=LoadBalancingConfig(),
        samples=(sample, PhaseSample(1, 230, 100, 0), PhaseSample(1, 230, 100, 0)),
        snapshot={
            "usable_soc_percent": soc,
            "battery_charge_capacity_dc_w": 10_000,
            "battery_discharge_capacity_dc_w": 10_000,
            "battery_healthy": True,
        },
    )
    state = {"policy": configured}
    controller = object.__new__(IdlePhaseLoadBalancer)
    controller.gateway = PhaseGateway()
    controller.lease = None
    controller._lock = asyncio.Lock()
    controller.targets = controller.previous = controller.last_tick = None
    controller.status, controller.reason = "standby", None
    runtime = SimpleNamespace(
        load_balancing_observer=observer,
        telemetry=SimpleNamespace(payload=lambda _name: {}),
        _automation_policy=lambda: state["policy"],
    )
    # The production validity rule, evaluated on the stubbed policy.
    runtime._automation_policy_valid = lambda: RuntimeData._automation_policy_valid(runtime)
    controller.runtime = runtime

    async def persist(lease):
        controller.lease = lease

    controller._persist = persist
    return controller, state, activity


@pytest.mark.asyncio
async def test_idle_phase_support_does_not_discharge_below_policy_soc(monkeypatch):
    # Reported case: 4 140 W discharge at 50 % SOC despite a 100 W/80 % policy.
    controller, _state, activity = _policy_controller(
        monkeypatch, _report_policy(), soc=50, grid_w=4000,
    )
    with pytest.raises(ValueError, match="insufficient_capacity"):
        controller._plan()
    controller.lease = {"original": dict.fromkeys(PHASE_FIELDS, 0), "owned": [],
                        "priority": [102, 101, 105, 107],
                        "owned_priority": [102, 101, 105, 107, 129]}
    controller.gateway.priority_list = controller.lease["owned_priority"]
    for _ in range(3):
        await controller.tick()
        assert controller.status == "blocked"
    assert controller.lease is None
    assert controller.gateway.switches == []
    # One log per transition, with the cause, not one per tick.
    assert len(activity) == 1
    assert "minimum SOC 80" in activity[0][1]["cause"]


@pytest.mark.asyncio
async def test_idle_phase_support_clips_power_and_run_time_to_policy(monkeypatch):
    controller, state, activity = _policy_controller(
        monkeypatch, _report_policy(), soc=90, grid_w=3240,
    )
    targets = controller._plan()
    assert targets is not None
    assert any(targets)
    assert all(abs(power) <= 33 for power in targets)
    assert controller.lease_seconds == 1
    state["policy"] = _report_policy(max_duration_seconds=5)
    controller.previous = controller.last_tick = None
    await controller.tick()
    assert controller.status == "balancing_idle_overload"
    assert all(abs(power) <= 33 for power in controller.targets)
    assert controller.gateway.record["duration"] == 5
    controller.gateway.writes = 1
    assert controller.attributes()["idle_support_lease_seconds"] == 5
    await controller.tick()
    # Both clips logged at their transitions only.
    assert len(activity) == 2
    assert "power limited to 33 W per phase" in activity[0][1]["cause"]
    assert "run time limited to 5 s" in activity[1][1]["cause"]


def test_idle_phase_support_blocks_overload_above_policy_power(monkeypatch):
    # L1 needs 80 W of discharge; the policy allows only 33 W per phase.
    controller, _state, activity = _policy_controller(
        monkeypatch, _report_policy(soc_min_percent=10, soc_max_percent=90),
        soc=50, grid_w=3300,
    )
    with pytest.raises(ValueError, match="insufficient_capacity"):
        controller._plan()
    assert len(activity) == 1
    assert "power limited to 33 W per phase" in activity[0][1]["cause"]


@pytest.mark.parametrize("soc,allowed", [(80, False), (81, True)])
def test_idle_phase_support_does_not_discharge_at_policy_soc_min(monkeypatch, soc, allowed):
    controller, _state, _activity = _policy_controller(
        monkeypatch, _report_policy(max_duration_seconds=15, max_power_kw=10),
        soc=soc, grid_w=3240,
    )
    if allowed:
        targets = controller._plan()
        assert targets is not None and targets[0] < 0
    else:
        with pytest.raises(ValueError, match="insufficient_capacity"):
            controller._plan()


@pytest.mark.asyncio
async def test_idle_phase_support_requires_a_policy_that_authorizes_automation(monkeypatch):
    controller, _state, _activity = _policy_controller(
        monkeypatch,
        _report_policy(max_duration_seconds=15, max_power_kw=10, allowed_operation_classes=()),
        soc=90, grid_w=3240,
    )
    assert controller.runtime._automation_policy().enabled
    await controller.tick()
    assert controller.status == "blocked"
    assert controller.reason == "automation_policy_not_authorized"
    assert controller.gateway.switches == []


@pytest.mark.asyncio
async def test_idle_phase_support_is_released_when_policy_becomes_invalid(monkeypatch):
    controller, state, activity = _policy_controller(
        monkeypatch, _report_policy(max_duration_seconds=15, max_power_kw=10),
        soc=90, grid_w=3240,
    )
    await controller.tick()
    assert controller.lease is not None
    assert controller.gateway.switches == [True]
    state["policy"] = replace(state["policy"], enabled=False)
    await controller.tick()
    assert controller.lease is None
    assert controller.status == "blocked"
    assert controller.reason == "automation_policy_not_authorized"
    assert controller.gateway.switches == [True, False]
    assert controller.gateway.priority_list == [102, 101, 105, 107]
    await controller.tick()
    assert controller.gateway.switches == [True, False]
    assert len(activity) == 1


def test_idle_phase_support_does_not_charge_at_policy_soc_max(monkeypatch):
    controller, _state, _activity = _policy_controller(
        monkeypatch, _report_policy(soc_min_percent=10, soc_max_percent=90),
        soc=90, grid_w=-3240,
    )
    with pytest.raises(ValueError, match="insufficient_capacity"):
        controller._plan()


def test_gateway_minimum_cannot_raise_the_site_fuse_limit():
    controller = object.__new__(MeterLimitLoadBalancer)
    controller.runtime = SimpleNamespace(load_balancing_observer=SimpleNamespace(
        config=LoadBalancingConfig(fuse_l1=20, fuse_l2=20, fuse_l3=20),
        samples=(PhaseSample(5, 230, 1150, 0),) * 3,
    ))
    with pytest.raises(ValueError, match="meter_limitation_limit_outside_gateway_range"):
        controller._soft_limit_w({"exp_soft_limit__min": 50_000})


@pytest.mark.asyncio
async def test_disabled_load_balancing_logs_only_changed_restore_failures_and_recovers(monkeypatch):
    activity = []
    restored = []
    monkeypatch.setattr(meter_limit_module, "log_activity",
                        lambda *args, **kwargs: activity.append((args, kwargs)))
    controller = object.__new__(MeterLimitLoadBalancer)
    controller.runtime = SimpleNamespace(
        controller_state=SimpleNamespace(load_balancing_enabled=False),
        production_control_enabled=lambda: False,
        _meter_limitation_restored=lambda: restored.append(True),
    )
    controller._lock = asyncio.Lock()
    controller.status, controller.reason = "active", None
    controller.applied_limit_w = 6000
    controller.writes = 0
    original = {
        "enabled": 0, "exp_soft_enabled": 0, "exp_hard_enabled": 0,
        "exp_soft_limit": 0, "exp_hard_limit": 0,
        "gen_soft_enabled": 0, "gen_hard_enabled": 0,
        "gen_soft_limit": 0, "gen_hard_limit": 0,
        "limit_per_phase": 0, "meter_id": 1,
    }
    owned = {**original, "enabled": 1, "exp_soft_enabled": 1, "exp_soft_limit": 6000}
    external = {**owned, "exp_soft_limit": 5000}
    controller.lease = {"original": original, "owned": [owned]}
    events: list[str] = []
    current = external
    read_fails = True

    async def stop_idle():
        events.append("stop_idle")

    async def read():
        nonlocal read_fails
        events.append("read")
        if read_fails:
            read_fails = False
            raise OSError("temporary gateway read failure")
        return current, {}

    async def write(record):
        nonlocal current
        events.append("write")
        current = dict(record)

    async def save_lease(lease):
        events.append("save_lease")
        controller.lease = lease

    controller.idle_support = SimpleNamespace(stop=stop_idle, attributes=lambda: {})
    controller._read = read
    controller._write = write
    controller._save_lease = save_lease

    await controller.tick()
    assert controller.status == "recovery_required"
    assert controller.reason == "temporary gateway read failure"
    assert controller.lease is not None
    assert "write" not in events

    for _ in range(2):
        await controller.tick()
        assert controller.attributes()["control_status"] == "recovery_required"
        assert (
            controller.attributes()["control_blocked_reason"]
            == "meter_limitation_recovery_conflict"
        )
        assert controller.lease is not None
        assert "write" not in events
    assert len(activity) == 2
    assert all(entry[0][2] == "Load balancing restore needs recovery" for entry in activity)
    assert activity[0][1]["cause"] == "temporary gateway read failure"
    assert "meter_limitation_recovery_conflict" in activity[1][1]["cause"]

    current = owned
    await controller.tick()

    assert current == original
    assert controller.lease is None
    assert controller.status == "disabled"
    assert controller.reason is None
    assert restored == [True]
    assert events[-4:] == ["stop_idle", "read", "write", "save_lease"]


@pytest.mark.asyncio
async def test_observer_survives_restore_failure_and_sensor_shows_recovery():
    runtime = SimpleNamespace(
        serial="234301002683",
        controller_state=SimpleNamespace(load_balancing_enabled=False),
        production_control_enabled=lambda: False,
    )
    controller = object.__new__(MeterLimitLoadBalancer)
    controller.runtime = runtime
    controller.lease = {"original": {}, "owned": [{}]}
    controller.status, controller.reason = "active", None
    controller.applied_limit_w = None
    controller.writes = 0
    controller.idle_support = SimpleNamespace(attributes=lambda: {})
    attempts = 0

    async def deactivate():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary restore read failure")
        controller.lease = None

    controller.deactivate = deactivate
    runtime.meter_limit_load_balancer = controller
    observer = object.__new__(LoadBalancingObserver)
    observer.runtime = runtime
    observer.config = SimpleNamespace(
        poll_seconds=0, source="pixii", fuses=(20, 20, 20), margin_a=2, min_soc=20,
    )
    observer.status = "observing"
    observer.snapshot = {}
    observer.phase_service_available = False
    observer.meter_limitation_available = False
    observer.meter_limitation_capability = {}
    observer.general_service_settings_available = False
    observer.general_service_settings = {}
    observer._listeners = []
    runtime.load_balancing_observer = observer
    permits: asyncio.Queue[None] = asyncio.Queue()
    updates: asyncio.Queue[None] = asyncio.Queue()

    async def refresh():
        await permits.get()

    observer.async_refresh = refresh
    observer.add_listener(lambda: updates.put_nowait(None))
    sensor = PixiiLoadBalancingSensor(runtime, "load_balancing_observation")
    task = asyncio.create_task(observer._run())
    try:
        permits.put_nowait(None)
        await asyncio.wait_for(updates.get(), 1)
        assert not task.done()
        assert sensor.extra_state_attributes["control_status"] == "recovery_required"
        assert (
            sensor.extra_state_attributes["control_blocked_reason"]
            == "temporary restore read failure"
        )
        assert controller.lease is not None

        permits.put_nowait(None)
        await asyncio.wait_for(updates.get(), 1)
        assert not task.done()
        assert attempts == 2
        assert sensor.extra_state_attributes["control_status"] == "disabled"
        assert sensor.extra_state_attributes["control_blocked_reason"] is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_later_native_read_clears_startup_error(monkeypatch):
    original = {
        "enabled": 0, "exp_soft_enabled": 0, "exp_hard_enabled": 0,
        "exp_soft_limit": 0, "exp_hard_limit": 0, "gen_soft_enabled": 0,
        "gen_hard_enabled": 0, "gen_soft_limit": 0, "gen_hard_limit": 0,
        "limit_per_phase": 0, "meter_id": 1,
    }
    runtime, _reads, _writes, _activity = _restarted_controller(monkeypatch, original)
    controller = MeterLimitLoadBalancer(runtime)
    controller.startup_read_error = "temporary gateway read failure"
    original.pop("meter_id")
    with pytest.raises(ValueError, match="meter_limitation_settings_incomplete"):
        await controller._read()
    assert controller.startup_read_error == "temporary gateway read failure"
    original["meter_id"] = 1
    await controller._read()
    assert controller.startup_read_error is None
    await controller.tick()
    assert (controller.status, controller.reason) == ("disabled", None)


@pytest.mark.asyncio
async def test_phase_lease_releases_after_gateway_appears(monkeypatch):
    runtime, _reads, _writes, _activity = _restarted_controller(monkeypatch, {})
    original = dict.fromkeys(PHASE_FIELDS, 0)
    owned = {**original, "act_enabled": 1, "duration": 15}
    priority = [102, 101]
    key = "pixiihome_controller.GATEWAY.idle_phase_lease"
    LeaseStore.data[key] = {"lease": {
        "original": original, "owned": [original, owned],
        "priority": priority, "owned_priority": [*priority, 129],
    }}
    phase = PhaseGateway()
    phase.record = dict(owned)
    phase.priority_list = [*priority, 129]
    monkeypatch.setattr(idle_phase_module, "PhaseGateway", lambda _client: phase)
    controller = IdlePhaseLoadBalancer(runtime)
    runtime.gateway_scheduler = None
    with pytest.raises(ValueError, match="native_meter_unavailable"):
        await controller.load_saved_lease()
    assert controller.lease is not None and controller.gateway is None
    runtime.gateway_scheduler = SimpleNamespace(client=object())
    await controller.release()
    assert controller.lease is None
    assert LeaseStore.data[key] == {"lease": None}
    assert phase.record == original


@pytest.mark.asyncio
async def test_prepare_load_does_not_log_control_off(monkeypatch):
    runtime, _reads, _writes, activity = _restarted_controller(monkeypatch, {})
    original = dict.fromkeys(PHASE_FIELDS, 0)
    LeaseStore.data["pixiihome_controller.GATEWAY.meter_limit_lease"] = {"lease": {
        "original": original, "owned": [original],
    }}
    controller = MeterLimitLoadBalancer(runtime)
    await controller.load_saved_lease()
    assert not any("after restart while control is off" in str(item) for item in activity)
    await controller.load_saved_lease(control_off=True)
    assert any("after restart while control is off" in str(item) for item in activity)


@pytest.mark.asyncio
async def test_phase_release_accepts_original_on_second_read(monkeypatch):
    runtime, _reads, _writes, _activity = _restarted_controller(monkeypatch, {})
    original = dict.fromkeys(PHASE_FIELDS, 0)
    owned = {**original, "act_enabled": 1, "duration": 15}
    phase = PhaseGateway()
    phase.record = dict(owned)
    phase.priority_list = [102, 101]
    reads = 0
    first_read = phase.read
    async def read(service):
        nonlocal reads
        if service == 129:
            reads += 1
            if reads == 2:
                phase.record = dict(original)
        return await first_read(service)
    phase.read = read
    controller = IdlePhaseLoadBalancer(runtime)
    controller.gateway = phase
    controller.lease = {"original": original, "owned": [original, owned],
                        "priority": [102, 101], "owned_priority": [102, 101, 129]}
    await controller.release()
    assert reads == 2
    assert controller.lease is None
    assert phase.record == original


def test_non_policy_plan_error_does_not_log_policy_block(monkeypatch):
    controller, _state, activity = _policy_controller(
        monkeypatch, _report_policy(), soc=50, grid_w=4000,
    )
    def invalid(*args, **kwargs):
        raise ValueError("phase_data_invalid")
    monkeypatch.setattr(idle_phase_module, "plan_phases", invalid)
    with pytest.raises(ValueError, match="phase_data_invalid"):
        controller._plan()
    assert activity == []
    assert controller.policy_note is None


@pytest.mark.asyncio
async def test_native_service_clears_previous_policy_note(monkeypatch):
    controller, _state, _activity = _policy_controller(
        monkeypatch, _report_policy(), soc=90, grid_w=3000,
    )
    controller.policy_note = ("clipped", "power limited")
    controller.gateway.service = 102
    await controller.tick()
    assert controller.policy_note is None
    assert controller.status == "standby_native_mode_active"


def test_native_capacity_error_is_not_labeled_as_policy_block(monkeypatch):
    controller, _state, activity = _policy_controller(
        monkeypatch, _report_policy(), soc=50, grid_w=4000,
    )
    controller.runtime.load_balancing_observer.snapshot["battery_discharge_capacity_dc_w"] = 0
    with pytest.raises(ValueError, match="insufficient_capacity"):
        controller._plan()
    assert activity == []
    assert controller.policy_note is None
