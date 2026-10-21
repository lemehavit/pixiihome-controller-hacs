from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from custom_components.pixiihome_controller.control.load_balancing import (
    CONF_LOAD_BALANCING,
    ActiveModeIntent,
    LoadBalancingConfig,
    ModeBoundPreview,
    PhaseSample,
    phase_envelope,
    usable_soc_evidence,
)
from custom_components.pixiihome_controller.load_balancing_observer import LoadBalancingObserver
from custom_components.pixiihome_controller.runtime import RuntimeData
from custom_components.pixiihome_controller.sensor import PixiiLoadBalancingSensor
from custom_components.pixiihome_controller.telemetry import TelemetryStore


def config(**kwargs):
    return LoadBalancingConfig(fuse_l1=20, fuse_l2=20, fuse_l3=20, **kwargs)


def sample(power, battery=0):
    return PhaseSample(abs(power) / 230, 230, power, battery)


def test_worst_phase_individual_and_symmetric_power_are_distinct():
    result = phase_envelope(config(), (sample(5000), sample(1000), sample(500)), 60)
    assert result["required_discharge_w"] == 2580  # 3 * (5000 - 18 A * 230 V)
    assert result["individual_phase_estimates"][0]["required_discharge_w"] == 860
    assert result["individual_phase_estimates"][1]["required_discharge_w"] == 0
    assert result["overloaded_phases"] == [1]
    assert result["feasible"]
    assert not result["individual_phase_control_verified"]


def test_reduce_actual_charging_before_discharge_and_respect_export_phase():
    result = phase_envelope(
        config(), (sample(5000, 1000), sample(2000, 1000), sample(1500, 1000)), 60
    )
    assert result["required_discharge_w"] == 0
    assert result["charge_headroom_w"] == 420
    impossible = phase_envelope(config(), (sample(5000), sample(-4000), sample(0)), 60)
    assert impossible["battery_floor_w"] == -420
    assert impossible["battery_ceiling_w"] == -2580
    assert not impossible["feasible"]


def test_discharge_reserve_never_blocks_the_charging_direction():
    result = phase_envelope(config(min_soc=40), (sample(0), sample(0), sample(0)), 5)
    assert result["feasible"]
    assert result["charge_headroom_w"] > 0
    assert result["battery_floor_w"] == 0
    assert not result["discharge_available"]


@pytest.mark.parametrize("soc", [None, 0, 9, 10, float("nan"), 101])
def test_soc_does_not_authorize_discharge(soc):
    result = phase_envelope(config(), (sample(5000), sample(0), sample(0)), soc)
    assert not result["discharge_available_by_soc"]
    assert not result["feasible"]


@pytest.mark.parametrize(
    "phase,reason",
    [
        (PhaseSample(1, 230, 2000, 0), "phase_measurements_inconsistent"),
        (PhaseSample(25, 230, 0, 0), "reactive_current_exceeds_limit"),
        (PhaseSample(float("nan"), 230, 0, 0), "phase_data_invalid"),
        (PhaseSample(-1, 230, 0, 0), "phase_data_invalid"),
        (PhaseSample(1, 0, 0, 0), "phase_data_invalid"),
    ],
)
def test_reject_inconsistent_direction_scale_and_reactive_limits(phase, reason):
    with pytest.raises(ValueError, match=reason):
        phase_envelope(config(), (phase, sample(0), sample(0)), 60)


@pytest.mark.parametrize(
    "values",
    [
        {"enabled": "true"},
        {"phases": True},
        {"fuse_l1": 0},
        {"margin_a": 20},
        {"poll_seconds": 10, "stale_seconds": 15},
        {"max_charge_kw": float("inf")},
        {"current_l1": "switch.test"},
        {"source": "unknown"},
        {"min_soc": 101},
    ],
)
def test_invalid_configuration_is_rejected(values):
    with pytest.raises(ValueError):
        LoadBalancingConfig.from_mapping({**config().as_dict(), **values})


def intent(**kwargs):
    values = dict(
        identity="mode-A",
        generation="occurrence-1",
        desired_power_w=3000,
        observed_at=100,
        deadline=120,
        verified=True,
    )
    return ActiveModeIntent(**{**values, **kwargs})


def evaluate(guard, mode, **kwargs):
    return guard.evaluate(
        mode, **{**dict(now=100, current_power_w=3000, floor_w=-5000, ceiling_w=0), **kwargs}
    )


def test_recovery_uses_live_desire_and_never_extends_original_deadline():
    guard = ModeBoundPreview()
    assert evaluate(guard, intent())["target_power_w"] == 0
    recovery = evaluate(
        guard, intent(observed_at=105, desired_power_w=200, deadline=999), now=105, ceiling_w=5000
    )
    assert recovery["target_power_w"] == 200
    assert recovery["deadline"] == 120
    assert recovery["remaining_seconds"] == 15
    assert recovery["valid_until"] == 120
    expired = evaluate(guard, intent(observed_at=120, deadline=999), now=120, ceiling_w=5000)
    assert expired["status"] == "mode_expired"
    assert expired["target_power_w"] is None
    assert not evaluate(guard, intent(observed_at=121, deadline=999), now=121)["resume_allowed"]


def test_recovery_ramps_only_while_same_occurrence_is_verified_active():
    guard = ModeBoundPreview()
    evaluate(guard, intent())
    recovery = evaluate(guard, intent(observed_at=105), now=105, ceiling_w=5000)
    assert recovery["target_power_w"] == 500
    stopped = evaluate(guard, None, now=106)
    assert stopped["status"] == "no_active_mode"
    assert not evaluate(guard, intent(observed_at=107), now=107)["resume_allowed"]
    new_occurrence = evaluate(
        guard,
        intent(generation="occurrence-2", observed_at=108),
        now=108,
        current_power_w=0,
        ceiling_w=5000,
    )
    assert new_occurrence["target_power_w"] == 0  # no jump to old target


def test_replaced_mode_cannot_be_restored_and_shorter_deadline_wins():
    guard = ModeBoundPreview()
    evaluate(guard, intent())
    evaluate(guard, intent(identity="mode-B", observed_at=105, deadline=110), now=105)
    assert not evaluate(guard, intent(observed_at=106), now=106)["resume_allowed"]
    result = evaluate(guard, intent(identity="mode-B", observed_at=110, deadline=1000), now=110)
    assert not result["resume_allowed"]
    shorter = ModeBoundPreview()
    evaluate(shorter, intent())
    evaluate(shorter, intent(observed_at=105, deadline=108), now=105)
    assert (
        evaluate(shorter, intent(observed_at=108, deadline=120), now=108)["status"]
        == "mode_expired"
    )


@pytest.mark.parametrize(
    "mode,reason",
    [
        (intent(verified=False), "active_mode_unverified"),
        (intent(generation=""), "active_mode_unverified"),
        (intent(observed_at=80), "active_mode_stale"),
        (intent(observed_at=101), "active_mode_stale"),
        (intent(deadline=None), "mode_deadline_unknown"),
        (intent(active=False), "no_active_mode"),
        (intent(deadline=100.9), "mode_expired"),
    ],
)
def test_uncertain_intent_cannot_start_a_mode(mode, reason):
    result = evaluate(ModeBoundPreview(), mode)
    assert result == {"status": reason, "target_power_w": None, "resume_allowed": False}


def test_continuous_intent_requires_explicit_verification_and_timed_mode_cannot_convert():
    guard = ModeBoundPreview()
    assert evaluate(guard, intent(deadline=None, continuous=True))["resume_allowed"]
    timed = ModeBoundPreview()
    evaluate(timed, intent())
    assert (
        evaluate(timed, intent(deadline=None, continuous=True))["status"] == "mode_deadline_changed"
    )


def test_stop_and_replacement_remain_binding_after_an_uncertain_poll():
    guard = ModeBoundPreview()
    evaluate(guard, intent())
    evaluate(guard, intent(verified=False))
    evaluate(guard, None)
    assert not evaluate(guard, intent())["resume_allowed"]
    guard = ModeBoundPreview()
    evaluate(guard, intent())
    evaluate(guard, intent(verified=False))
    evaluate(guard, intent(identity="mode-B"))
    assert not evaluate(guard, intent())["resume_allowed"]


def runtime_with_meter(settings=None, states=None) -> tuple[RuntimeData, Any]:
    core = {"batt_soc": 60, "srv_type": 107}
    for i in (1, 2, 3):
        core.update({f"meter_a{i}": 5, f"meter_v{i}": 230, f"meter_w{i}": 1150, f"ac_w{i}": 0})

    async def read_status(
        *, phase_service=False, meter_limitation=False, general_service_settings=False
    ):
        if phase_service:
            return {"act_ref1": 0, "act_ref2": 0, "act_ref3": 0}
        if meter_limitation:
            return {
                "enabled": 0,
                "meter_id": 1,
                "limit_per_phase": 0,
                "exp_soft_enabled": 0,
                "exp_soft_limit": 0,
                "exp_hard_enabled": 0,
                "exp_hard_limit": 0,
                "gen_soft_enabled": 0,
                "gen_soft_limit": 0,
                "gen_hard_enabled": 0,
                "gen_hard_limit": 0,
                "exp_soft_limit__min": 0,
                "exp_soft_limit__max": 50000,
            }
        if general_service_settings:
            return {
                "ref_delay": 10,
                "ref_delay__min": 0,
                "ref_delay__max": 60,
                "meter_other_srv": 1,
                "meter_other_srv__min": 0,
                "meter_other_srv__max": 6,
            }
        return core

    client = SimpleNamespace(read_load_balancing_status=AsyncMock(side_effect=read_status))
    runtime = SimpleNamespace(
        serial="test",
        _scheduler_settings=lambda: {CONF_LOAD_BALANCING: settings or {}},
        gateway_scheduler=SimpleNamespace(client=client),
        telemetry=TelemetryStore(
            payloads={
                "battery": {
                    "batt_soc": 60,
                    "batt_cfg_soc_min": 10,
                    "batt_cfg_soc_max": 90,
                    "batt_status": 1,
                    "num_batt": 4,
                    "num_batt_ok": 4,
                    "batt_cap_w_ch": 20000,
                    "batt_cap_w_disch": 20000,
                }
            },
            received_at={"battery": datetime.now(UTC)},
        ),
        hass=SimpleNamespace(
            states=SimpleNamespace(get=lambda k: (states or {}).get(k)),
            async_create_task=asyncio.create_task,
            async_create_background_task=lambda coro, name: asyncio.create_task(coro, name=name),
        ),
    )
    return cast(RuntimeData, runtime), client


def test_ten_percent_reserve_uses_usable_window_not_raw_soc():
    payload = {
        "batt_soc": 18,
        "batt_cfg_soc_min": 10,
        "batt_cfg_soc_max": 90,
        "batt_soc_min": 17.5,
        "batt_soc_max": 18.5,
    }
    result = usable_soc_evidence(payload, fresh=True)
    assert result["usable_soc_percent"] == 10
    assert result["reserved_min_soc_percent"] == 10  # not dynamic pack extrema
    limited = phase_envelope(
        config(), (sample(5000), sample(0), sample(0)), result["usable_soc_percent"]
    )
    assert not limited["discharge_available_by_soc"]
    payload["batt_soc"] = 18.8
    assert usable_soc_evidence(payload, fresh=True)["usable_soc_percent"] == pytest.approx(11)


@pytest.mark.parametrize(
    "payload",
    [
        {"batt_soc": 50},
        {"batt_soc": 50, "batt_cfg_soc_min": -10, "batt_cfg_soc_max": 90},
        {"batt_soc": 50, "batt_cfg_soc_min": 10, "batt_cfg_soc_max": 110},
        {"batt_soc": True, "batt_cfg_soc_min": 10, "batt_cfg_soc_max": 90},
        {"batt_soc": float("nan"), "batt_soc_usable": 50},
    ],
)
def test_missing_or_invalid_reserves_never_fall_back_to_raw_soc(payload):
    assert usable_soc_evidence(payload, fresh=True)["usable_soc_percent"] is None


def test_native_usable_soc_is_not_normalized_a_second_time():
    payload = {
        "batt_soc": 18,
        "batt_soc_usable": 10,
        "batt_cfg_soc_min": 10,
        "batt_cfg_soc_max": 90,
    }
    assert usable_soc_evidence(payload, fresh=True)["usable_soc_percent"] == 10
    assert usable_soc_evidence(payload, fresh=False)["usable_soc_percent"] is None


@pytest.mark.asyncio
async def test_observer_clears_discharge_permission_when_battery_evidence_ages():
    runtime, _ = runtime_with_meter(config(enabled=True).as_dict())
    observer = LoadBalancingObserver(runtime)
    await observer.async_refresh()
    assert observer.snapshot["usable_soc_percent"] == 62.5
    assert observer.snapshot["min_raw_soc_equivalent_percent"] == 18
    assert observer.snapshot["discharge_available_by_soc"]
    runtime.telemetry.received_at["battery"] -= timedelta(seconds=30)
    await observer.async_refresh()
    assert observer.snapshot["usable_soc_percent"] is None
    assert observer.snapshot["discharge_blocked_reason"] == "battery_telemetry_stale"
    assert not observer.snapshot["discharge_available_by_soc"]
    assert observer.snapshot["battery_floor_w"] >= 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"batt_status": 2},
        {"batt_status": True},
        {"num_batt_ok": 3},
        {"batt_cap_w_disch": 0},
        {"batt_cap_w_disch": None},
    ],
)
async def test_battery_not_ready_blocks_discharge_even_with_high_usable_soc(mutation):
    runtime, _ = runtime_with_meter(config(enabled=True).as_dict())
    runtime.telemetry.payloads["battery"].update(mutation)
    observer = LoadBalancingObserver(runtime)
    await observer.async_refresh()
    assert observer.snapshot["discharge_available_by_soc"]
    assert not observer.snapshot["discharge_available"]
    assert observer.snapshot["battery_floor_w"] >= 0
    assert observer.snapshot["discharge_blocked_reason"] is not None


@pytest.mark.asyncio
async def test_load_balancing_soc_sensors_show_usable_basis_and_configured_reserve():
    runtime, _ = runtime_with_meter(config(enabled=True).as_dict())
    runtime.load_balancing_observer = observer = LoadBalancingObserver(runtime)
    current = PixiiLoadBalancingSensor(runtime, "load_balancing_usable_soc")
    reserve = PixiiLoadBalancingSensor(runtime, "load_balancing_min_usable_soc")
    assert reserve.available and reserve.native_value == 10
    assert not current.available
    await observer.async_refresh()
    assert current.available and current.native_value == 62.5
    runtime.telemetry.received_at["battery"] -= timedelta(seconds=30)
    await observer.async_refresh()
    assert not current.available and current.native_value is None
    assert reserve.native_value == 10


@pytest.mark.asyncio
async def test_observer_exposes_production_capability_without_writing_during_refresh():
    runtime, client = runtime_with_meter(config(enabled=True).as_dict())
    observer = LoadBalancingObserver(runtime)
    changes = []
    remove = observer.add_listener(lambda: changes.append(observer.status))
    await observer.async_refresh()
    assert observer.status == "observing"
    assert observer.snapshot["phase_currents_a"] == [5, 5, 5]
    assert observer.phase_service_available
    assert observer.meter_limitation_available
    assert observer.attributes()["meter_limitation_capability"]["meter_id"] == 1
    assert observer.attributes()["meter_limitation_capability"]["exp_soft_limit__max"] == 50000
    assert observer.attributes()["general_service_settings_available"] is True
    assert observer.attributes()["native_service_reference_delay_seconds"] == 10
    assert observer.attributes()["native_meter_other_services_id"] == 1
    assert observer.attributes()["meter_limitation_control_allowed"] is True
    assert observer.attributes()["meter_limitation_hard_limits_allowed"] is False
    assert observer.attributes()["resume_allowed"] is False
    assert observer.attributes()["control_writes"] == 0
    assert observer.attributes()["measurement_freshness_verified"] is False
    assert "measurement_age_seconds" not in observer.snapshot  # native sample age unverified
    remove()
    await observer.async_refresh()
    assert changes == ["observing"]
    assert client.read_load_balancing_status.call_count == 6  # general settings stay live


@pytest.mark.asyncio
async def test_observer_refreshes_general_service_settings_each_poll():
    runtime, client = runtime_with_meter(config(enabled=True).as_dict())
    observer = LoadBalancingObserver(runtime)
    await observer.async_refresh()
    assert observer.attributes()["native_service_reference_delay_seconds"] == 10

    original = client.read_load_balancing_status.side_effect

    async def changed_status(**kwargs):
        result = await original(**kwargs)
        if kwargs.get("general_service_settings"):
            return {**result, "ref_delay": 7}
        return result

    client.read_load_balancing_status.side_effect = changed_status
    await observer.async_refresh()

    assert observer.attributes()["native_service_reference_delay_seconds"] == 7


def test_verified_reference_delay_updates_observer_cache_and_notifies_listener():
    runtime, _ = runtime_with_meter(config(enabled=True).as_dict())
    observer = LoadBalancingObserver(runtime)
    observer.general_service_settings = {"ref_delay": 1, "meter_other_srv": 1}
    changes: list[int | None] = []
    observer.add_listener(
        lambda: changes.append(observer.attributes()["native_service_reference_delay_seconds"])
    )

    observer.apply_verified_reference_delay(10)

    assert observer.attributes()["native_service_reference_delay_seconds"] == 10
    assert changes == [10]


def test_verified_reference_delay_rejects_invalid_cache_values():
    runtime, _ = runtime_with_meter(config(enabled=True).as_dict())
    observer = LoadBalancingObserver(runtime)

    with pytest.raises(ValueError, match="service_reference_delay_invalid"):
        observer.apply_verified_reference_delay(True)


@pytest.mark.asyncio
@pytest.mark.parametrize("settings", [{}, {"enabled": True, "phases": 2}])
async def test_disabled_or_invalid_observer_never_reads(settings):
    runtime, client = runtime_with_meter(settings)
    observer = LoadBalancingObserver(runtime)
    observer.start()
    await observer.async_refresh()
    assert observer._task is None
    client.read_load_balancing_status.assert_not_called()


@pytest.mark.asyncio
async def test_observer_has_single_task_and_unloads():
    runtime, _ = runtime_with_meter(config(enabled=True).as_dict())
    observer = LoadBalancingObserver(runtime)
    observer.start()
    task = observer._task
    assert task is not None
    observer.start()
    assert observer._task is task
    await asyncio.sleep(0)
    await observer.stop()
    assert task.cancelled()
    assert observer._task is None


def external_meter(*, powers=True, age=0):
    settings = config(source="entities").as_dict()
    settings["enabled"] = True
    states = {}
    reported = datetime.now(UTC) - timedelta(seconds=age)
    for prefix, unit, value in [
        ("current", "A", "5"),
        ("voltage", "V", "230"),
        ("power", "kW", "1.15"),
    ]:
        for i in (1, 2, 3):
            if prefix == "power" and not powers:
                continue
            key = f"{prefix}_l{i}"
            entity_id = f"sensor.{key}"
            settings[key] = entity_id
            states[entity_id] = SimpleNamespace(
                state=value, last_reported=reported, attributes={"unit_of_measurement": unit}
            )
    return settings, states


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "powers,age,expected",
    [
        (True, 0, "observing"),
        (False, 0, "phase_direction_missing"),
        (True, 30, "meter_stale"),
    ],
)
async def test_external_meter_direction_units_and_freshness(powers, age, expected):
    settings, states = external_meter(powers=powers, age=age)
    runtime, _ = runtime_with_meter(settings, states)
    observer = LoadBalancingObserver(runtime)
    await observer.async_refresh()
    assert observer.status == expected
    if not powers:
        assert observer.snapshot["phase_currents_a"] == [5, 5, 5]
    assert ("charge_headroom_w" in observer.snapshot) == (expected == "observing")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("unit", "phase_data_invalid"),
        ("nan", "phase_data_invalid"),
        ("timestamp", "meter_timestamps_inconsistent"),
        ("unavailable", "phase_data_missing"),
    ],
)
async def test_invalid_meter_does_not_leave_a_valid_power_preview(mutation, expected):
    settings, states = external_meter()
    runtime, _ = runtime_with_meter(settings, states)
    observer = LoadBalancingObserver(runtime)
    await observer.async_refresh()
    state = states["sensor.current_l1"]
    if mutation == "unit":
        state.attributes["unit_of_measurement"] = "W"
    elif mutation == "nan":
        state.state = "nan"
    elif mutation == "timestamp":
        state.last_reported -= timedelta(seconds=8)
    else:
        state.state = "unavailable"
    await observer.async_refresh()
    assert observer.status == expected
    assert "charge_headroom_w" not in observer.snapshot


@pytest.mark.asyncio
async def test_sensor_reports_control_capability_and_detaches_on_removal():
    runtime, client = runtime_with_meter(config(enabled=True).as_dict())
    runtime.load_balancing_observer = observer = LoadBalancingObserver(runtime)
    status = PixiiLoadBalancingSensor(runtime, "load_balancing_observation")
    power = PixiiLoadBalancingSensor(runtime, "charge_headroom")
    assert status.available and not power.available
    await status.async_added_to_hass()
    assert len(observer._listeners) == 1
    await status.async_will_remove_from_hass()
    assert not observer._listeners
    await observer.async_refresh()
    assert power.available and power.native_value == 8970
    calls = client.read_load_balancing_status.call_count
    assert status.native_value == "observing"
    assert not status.extra_state_attributes["observation_only"]
    assert client.read_load_balancing_status.call_count == calls
    observer.status = "read_failed"
    assert not power.available and power.native_value is None


def test_duplicate_meter_binding_is_rejected():
    values, _ = external_meter()
    values["power_l1"] = values["current_l1"]
    with pytest.raises(ValueError, match="meter_entities_required"):
        LoadBalancingConfig.from_mapping(values)
