"""Actual-SOC guard and exact-identity price stop regression tests."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.pixiihome_controller.control.models import OperationImpact
from custom_components.pixiihome_controller.price_guard import PriceSocGuard
from custom_components.pixiihome_controller.protocol.mqtt import parse_ack
from custom_components.pixiihome_controller.protocol.scheduler_readback import (
    SchedulerReconciliationStatus,
    reconcile_cleared_price_stop,
    reconcile_scheduler_get,
)
from custom_components.pixiihome_controller.scheduler.mutations import (
    SchedulerExecutionResult,
    SchedulerExecutionStatus,
    SchedulerTransactionStatus,
)
from custom_components.pixiihome_controller.scheduler.ownership import (
    OwnershipLifecycle,
    SchedulerOwnershipRecord,
    SchedulerOwnershipStore,
    scheduler_expected_state_from_record,
)

from .conftest import load_json
from .test_price_controller import runtime
from .test_scheduler_manager import M_ID, FakeExecutor, create_ack, get_ack, safe_context
from .test_scheduler_manager import manager as base_manager


def manager(*args, **kwargs):
    instance = base_manager(*args, **kwargs)
    instance.verify_price_stopped = AsyncMock(return_value=True)
    return instance


@pytest.mark.asyncio
@pytest.mark.parametrize("service", [102, 104, 105, 107, 111, 121])
async def test_live_trial_refuses_any_running_foreign_service_without_a_write(service):
    from custom_components.pixiihome_controller.price_guard_trial import run_price_guard_validation

    instance, mutations = runtime(schedule_writes=False)
    instance.price_optimization_controller = SimpleNamespace(
        soc_guard=SimpleNamespace(validation_lock=asyncio.Lock())
    )
    instance.load_balancing_observer = None
    instance.telemetry.value = lambda family, key: service
    with pytest.raises(ValueError, match="price_guard_trial_requires_idle"):
        await run_price_guard_validation(instance)
    assert mutations.created == []


def record(
    power=300,
    *,
    start=None,
    owner="automatic_price_optimization",
    lifecycle=OwnershipLifecycle.OWNED,
):
    start = start or datetime.now(UTC) - timedelta(seconds=10)
    return SchedulerOwnershipRecord(
        "entry-1",
        "GATEWAY",
        M_ID,
        "demandresponse",
        {
            "m_id": M_ID,
            "service": "demandresponse",
            "values": {"pacref": power},
            "starttime": start.isoformat(),
            "duration": 300,
        },
        lifecycle,
        owner,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "age,current,reason",
    [(20, 1, "phase_data_not_fresh"), (0, 19, "insufficient_phase_margin")],
)
async def test_live_trial_rejects_stale_or_overloaded_phases(age, current, reason):
    from custom_components.pixiihome_controller.price_guard_trial import run_price_guard_validation

    instance, mutations = runtime(schedule_writes=False)
    instance.price_optimization_controller = SimpleNamespace(
        soc_guard=SimpleNamespace(validation_lock=asyncio.Lock())
    )
    instance.telemetry.value = lambda family, key: 0
    instance.load_balancing_observer = SimpleNamespace(
        samples=(SimpleNamespace(current_a=current),) * 3,
        async_refresh=AsyncMock(),
        status="observing",
        snapshot={"observed_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat()},
        config=SimpleNamespace(fuses=(20, 20, 20)),
    )
    with pytest.raises(ValueError, match=reason):
        await run_price_guard_validation(instance)
    assert mutations.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "soc,power,stop", [(95, 300, True), (5, -300, True), (5, 300, False), (95, -300, False)]
)
async def test_guard_bounds_are_directional_and_use_live_usable_soc(soc, power, stop):
    instance, mutations = runtime()
    instance.price_optimization_controller = SimpleNamespace(request_refresh=lambda: None)
    instance.battery = replace(instance.battery, usable_soc=soc, power_w=power)
    item = record(power)
    instance.scheduler_ownership.records[M_ID] = item
    mutations.stop_owned_price = AsyncMock(
        return_value=SimpleNamespace(status=SchedulerTransactionStatus.SUCCESS, reason="success")
    )
    guard = PriceSocGuard(instance)
    await guard.tick()
    assert mutations.stop_owned_price.await_count == int(stop)
    assert guard.stops == int(stop)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        record(owner="manual"),
        record(lifecycle=OwnershipLifecycle.PROVISIONAL),
        record(start=datetime.now(UTC) + timedelta(hours=1)),
        record(start=datetime.now(UTC) - timedelta(hours=1)),
    ],
)
async def test_guard_never_stops_foreign_unverified_far_future_or_expired_intervals(item):
    instance, mutations = runtime()
    instance.battery = replace(instance.battery, usable_soc=99)
    instance.scheduler_ownership.records[M_ID] = item
    mutations.stop_owned_price = AsyncMock()
    guard = PriceSocGuard(instance)
    await guard.tick()
    mutations.stop_owned_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_planning_keeps_guard_and_uncertain_stops_are_not_retried():
    instance, mutations = runtime(schedule_writes=False)
    instance.price_optimization_controller = None
    instance.controller_state = replace(
        instance.controller_state, automatic_price_optimization_enabled=False
    )
    instance.battery = replace(instance.battery, usable_soc=99)
    instance.scheduler_ownership.records[M_ID] = record()
    mutations.stop_owned_price = AsyncMock(
        return_value=SimpleNamespace(
            status=SchedulerTransactionStatus.DELIVERY_UNKNOWN, reason="uncertain"
        )
    )
    guard = PriceSocGuard(instance)
    await guard.tick()
    await guard.tick()
    assert mutations.stop_owned_price.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("soc,age", [(None, 0), (float("nan"), 0), (50, 16)])
async def test_missing_or_stale_soc_requests_an_exact_owned_stop(soc, age):
    instance, mutations = runtime()
    instance.price_optimization_controller = None
    instance.battery = replace(instance.battery, usable_soc=soc)
    instance.telemetry.age = lambda family, now: timedelta(seconds=age)
    instance.scheduler_ownership.records[M_ID] = record()
    mutations.stop_owned_price = AsyncMock(
        return_value=SimpleNamespace(status=SchedulerTransactionStatus.SUCCESS, reason="success")
    )
    guard = PriceSocGuard(instance)
    await guard.tick()
    mutations.stop_owned_price.assert_awaited_once_with(M_ID)
    assert guard.reason == "price_soc_guard_telemetry_not_safe"


@pytest.mark.asyncio
async def test_trial_bound_can_stop_discharge_at_nearly_full_usable_soc():
    instance, mutations = runtime()
    instance.price_optimization_controller = None
    instance.battery = replace(instance.battery, usable_soc=99.975, power_w=-100)
    instance.scheduler_ownership.records[M_ID] = record(-300)
    mutations.stop_owned_price = AsyncMock(
        return_value=SimpleNamespace(status=SchedulerTransactionStatus.SUCCESS, reason="success")
    )
    guard = PriceSocGuard(instance)
    guard.validation_bounds[M_ID] = (100, 100)
    await guard.tick()
    mutations.stop_owned_price.assert_awaited_once_with(M_ID)
    assert guard.reason == "minimum_usable_soc_reached"


def readback(item, *, duration=None, status=5, left=250):
    return replace(
        get_ack(),
        m_id=item.m_id,
        service=item.service,
        socref=None,
        duration=duration if duration is not None else item.intended_state["duration"],
        service_status=status,
        service_duration_left=left,
        values={
            **item.intended_state["values"],
            "starttime": item.intended_state["starttime"],
            "duration": duration if duration is not None else item.intended_state["duration"],
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("absent", [True, False])
@pytest.mark.parametrize("service", ["demandresponse", "peakshaving"])
@pytest.mark.parametrize("left", [None, 0])
async def test_stop_requires_exact_pre_read_delete_ack_absence_and_physical_verification(
    absent, service, left
):
    item = record()
    if service == "peakshaving":
        item = replace(
            item,
            service=service,
            intended_state={
                **item.intended_state,
                "service": service,
                "values": {
                    "exportlim": 0,
                    "importlim": 0,
                    "charge_max": 0,
                    "discharge_max": 300,
                    "keep_active": True,
                },
            },
        )
    after = (
        replace(get_ack(), m_id=M_ID, response=11)
        if absent
        else readback(item, duration=0, status=12, left=left)
    )
    executor = FakeExecutor(
        [
            SchedulerExecutionResult(
                SchedulerExecutionStatus.ACK_SUCCESS,
                replace(create_ack(), method="delete", service=service),
            )
        ],
        [readback(item), after],
    )
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY", {M_ID: item}))
    instance.policy_provider = lambda operation: replace(
        safe_context(operation),
        operation_impact=OperationImpact(active_service=121, protected_service_active=True),
    )
    result = await instance.stop_owned_price(M_ID)
    assert (result.status is SchedulerTransactionStatus.SUCCESS) is absent
    assert instance.ownership.records[M_ID].lifecycle is (
        OwnershipLifecycle.ORPHANED if absent else OwnershipLifecycle.CONFLICTED
    )
    assert len(executor.mutation_commands) == 1
    assert executor.mutation_commands[0].to_payload() == {
        "method": "delete",
        "m_id": M_ID,
    }
    transaction = instance.journal.journal.transactions[-1]
    assert transaction.operation == "delete"
    assert transaction.intent == {"m_id": M_ID, "service": service, "absent": True}
    assert all(command.m_id == M_ID for command in executor.get_commands)


@pytest.mark.asyncio
@pytest.mark.parametrize("changed_ack", [
    {"method": "set"}, {"m_id": M_ID + 1}, {"service": "peakshaving"}, {"response": 0},
])
async def test_price_delete_cannot_succeed_with_old_set_ack_or_another_identity(changed_ack):
    item = record()
    ack = replace(create_ack(), method="delete", service=item.service)
    ack = replace(ack, **changed_ack)
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, ack)],
        [readback(item), replace(get_ack(), response=11)],
    )
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY", {M_ID: item}))
    result = await instance.stop_owned_price(M_ID)
    assert result.status is SchedulerTransactionStatus.RECONCILIATION_FAILED
    assert result.reason == "price_stop_ack_mismatch"
    assert instance.ownership.records[M_ID].lifecycle is OwnershipLifecycle.CONFLICTED
    await instance.stop_owned_price(M_ID)
    assert len(executor.mutation_commands) == 1


@pytest.mark.asyncio
async def test_future_own_price_interval_can_be_cancelled_but_does_not_claim_physical_early_stop():
    item = record(start=datetime.now(UTC) + timedelta(seconds=20))
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS,
                                  replace(create_ack(), method="delete", service=item.service))],
        [readback(item, status=1, left=None), replace(get_ack(), response=11)],
    )
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY", {M_ID: item}))
    result = await instance.stop_owned_price(M_ID)
    assert result.status is SchedulerTransactionStatus.SUCCESS
    instance.verify_price_stopped.assert_not_awaited()
    assert executor.mutation_commands[0].to_payload() == {"method": "delete", "m_id": M_ID}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner,mismatch", [("manual", False), ("automatic_price_optimization", True)]
)
async def test_stop_rejects_foreign_owner_and_changed_identity_without_any_write(owner, mismatch):
    item = record(owner=owner)
    before = readback(item)
    if mismatch:
        before = replace(before, values={**before.values, "pacref": 9000})
    executor = FakeExecutor([], [before])
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY", {M_ID: item}))
    result = await instance.stop_owned_price(M_ID)
    assert result.status is not SchedulerTransactionStatus.SUCCESS
    assert executor.mutation_commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", [OwnershipLifecycle.OWNED, OwnershipLifecycle.CONFLICTED])
@pytest.mark.parametrize(
    "status,left,mismatch,absent,success",
    [
        (2, None, False, False, True),
        (12, 0, False, False, True),
        (5, 0, False, False, False),
        (3, None, False, False, False),
        (2, 30, False, False, False),
        (2, 0, True, False, False),
        (None, None, False, True, True),
    ],
)
async def test_expired_price_reconciliation_requires_exact_inactive_readback_without_any_write(
    lifecycle, status, left, mismatch, absent, success
):
    item = record(start=datetime.now(UTC) - timedelta(hours=1), lifecycle=lifecycle)
    before = readback(item, status=status, left=left)
    if mismatch:
        before = replace(before, values={**before.values, "pacref": 9000})
    if absent:
        before = replace(get_ack(), m_id=M_ID, response=11)
    executor = FakeExecutor([], [before])
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY", {M_ID: item}))
    result = await instance.reconcile_expired_price(M_ID)
    assert (result.status is SchedulerTransactionStatus.SUCCESS) == success
    assert instance.ownership.records[M_ID].lifecycle is (
        OwnershipLifecycle.ORPHANED if success else lifecycle
    )
    assert executor.mutation_commands == []
    assert [command.m_id for command in executor.get_commands] == [M_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", [False, True])
async def test_price_reconciliation_refuses_live_or_foreign_records_without_io(foreign):
    item = record(owner="manual" if foreign else "automatic_price_optimization")
    executor = FakeExecutor([], [])
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY", {M_ID: item}))
    result = await instance.reconcile_expired_price(M_ID)
    assert result.status is not SchedulerTransactionStatus.SUCCESS
    assert executor.mutation_commands == executor.get_commands == []


def cleared_stop_case(*, expired=False):
    """Replay the exact keyed reply captured on 2026-10-18, firmware 2.3.63."""
    ack = parse_ack(load_json("mqtt/peakshaving_cleared_stop_2_3_63.json"))
    item = replace(
        record(), m_id=ack.m_id, service="peakshaving",
        intended_state={
            "m_id": ack.m_id, "service": "peakshaving",
            "values": {key: value for key, value in ack.values.items()
                       if key not in {"starttime", "duration", "service_status"}},
            "starttime": (datetime.now(UTC).replace(microsecond=0) - timedelta(
                seconds=3600 if expired else 10
            )).isoformat(), "duration": 40,
        },
    )
    return item, ack


@pytest.mark.asyncio
async def test_old_cleared_shape_cannot_confirm_deletion_even_if_physical_idle_is_observed():
    item, cleared = cleared_stop_case()
    stopped = replace(scheduler_expected_state_from_record(item), duration=0)
    assert (
        reconcile_scheduler_get(cleared, stopped).status is SchedulerReconciliationStatus.REJECTED
    )
    assert reconcile_cleared_price_stop(cleared, stopped) is not None
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS,
                                  replace(create_ack(), method="delete", m_id=item.m_id,
                                          service=item.service))],
        [readback(item), cleared, cleared],
    )
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY",
                                                              {item.m_id: item}))
    result = await instance.stop_owned_price(item.m_id)
    assert result.status is SchedulerTransactionStatus.RECONCILIATION_FAILED
    assert instance.ownership.records[item.m_id].lifecycle is OwnershipLifecycle.CONFLICTED
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 3
    assert executor.mutation_commands[0].to_payload() == {"method": "delete", "m_id": item.m_id}
    assert result.price_stop_observation["idle_observed_before_deadline"] is True
    assert result.price_stop_observation["keyed_state"] == "cleared_object"
    instance.verify_price_stopped.assert_awaited_once()
    assert instance.ownership.records[item.m_id].intended_state == item.intended_state


@pytest.mark.asyncio
@pytest.mark.parametrize("physical", [False, TimeoutError()])
@pytest.mark.parametrize("absent", [False, True])
async def test_absent_or_cleared_record_with_continuing_output_is_not_a_stop(physical, absent):
    # The live m_id 4 trial returned this cleared shape twice while core was
    # still service 104 at -89 .. -297 W. Metadata alone must never pass.
    item, cleared = cleared_stop_case()
    item = replace(item, intended_state={**item.intended_state,
                   "starttime": (datetime.now(UTC) - timedelta(seconds=10)).isoformat()})
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS,
                                  replace(create_ack(), method="delete", m_id=item.m_id,
                                          service=item.service))],
        [readback(item), replace(cleared, response=11)] if absent
        else [readback(item), cleared, cleared],
    )
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY",
                                                              {item.m_id: item}))
    instance.verify_price_stopped = AsyncMock(
        **({"side_effect": physical} if isinstance(physical, Exception)
           else {"return_value": physical})
    )
    result = await instance.stop_owned_price(item.m_id)
    assert result.status is SchedulerTransactionStatus.RECONCILIATION_FAILED
    assert result.reason == (
        "price_stop_physical_unconfirmed" if absent else "price_stop_unconfirmed"
    )
    failed = instance.ownership.records[item.m_id]
    assert failed.lifecycle is OwnershipLifecycle.CONFLICTED
    assert failed.intended_state == item.intended_state
    await instance.stop_owned_price(item.m_id)
    assert len(executor.mutation_commands) == 1
    assert executor.mutation_commands[0].to_payload() == {"method": "delete", "m_id": item.m_id}


@pytest.mark.asyncio
async def test_stop_requires_physical_verifier_before_any_write():
    item = record()
    executor = FakeExecutor([], [readback(item)])
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY", {M_ID: item}))
    instance.verify_price_stopped = None
    result = await instance.stop_owned_price(M_ID)
    assert result.reason == "price_stop_verifier_unavailable"
    assert executor.mutation_commands == []


@pytest.mark.asyncio
async def test_expiry_does_not_clear_conflict_while_output_is_still_running():
    item, cleared = cleared_stop_case(expired=True)
    item = replace(item, lifecycle=OwnershipLifecycle.CONFLICTED)
    executor = FakeExecutor([], [cleared, cleared])
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY",
                                                              {item.m_id: item}))
    instance.verify_price_stopped = AsyncMock(return_value=False)
    result = await instance.reconcile_expired_price(item.m_id)
    assert result.reason == "price_expiry_physical_unconfirmed"
    assert instance.ownership.records[item.m_id].lifecycle is OwnershipLifecycle.CONFLICTED
    assert executor.mutation_commands == []


@pytest.mark.parametrize(
    "changed",
    [
        {"method": "set"}, {"response": 11}, {"m_id": 123}, {"service": "demandresponse"},
        {"duration": 40}, {"service_status": 2}, {"service_status": 3},
        {"service_status": 5}, {"service_time_left": 1}, {"service_duration_left": 1},
        {"recurrence": {"rec_type": 1}},
        {"values": {"starttime": "now"}}, {"values": {"starttime": None}},
        {"values": {"duration": 1}}, {"values": {"service_status": 5}},
        {"values": {"discharge_max": 9000}}, {"values": {"keep_active": False}},
        {"values": {"service_time_left": 1}}, {"values": {"unknown": 1}},
    ],
)
def test_cleared_stop_rejects_incomplete_changed_or_live_identity(changed):
    item, ack = cleared_stop_case()
    if "values" in changed:
        changed = {"values": {**ack.values, **changed["values"]}}
    ack = replace(ack, **changed)
    assert reconcile_cleared_price_stop(
        ack, replace(scheduler_expected_state_from_record(item), duration=0)
    ) is None


@pytest.mark.parametrize("missing", ["starttime", "duration", "service_status", "discharge_max"])
def test_cleared_stop_requires_explicit_fields(missing):
    item, ack = cleared_stop_case()
    ack = replace(ack, values={key: value for key, value in ack.values.items() if key != missing})
    assert reconcile_cleared_price_stop(
        ack, replace(scheduler_expected_state_from_record(item), duration=0)
    ) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("second", ["running", "changed", "timeout"])
async def test_cleared_stop_does_not_retry_write_when_second_read_is_uncertain(second):
    item, cleared = cleared_stop_case()
    changed = (
        TimeoutError() if second == "timeout" else
        readback(item) if second == "running" else
        replace(cleared, values={**cleared.values, "discharge_max": 9000})
    )
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS,
                                  replace(create_ack(), method="delete", m_id=item.m_id,
                                          service=item.service))],
        [readback(item), cleared, changed],
    )
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY",
                                                              {item.m_id: item}))
    result = await instance.stop_owned_price(item.m_id)
    assert result.status is SchedulerTransactionStatus.RECONCILIATION_FAILED
    assert instance.ownership.records[item.m_id].lifecycle is OwnershipLifecycle.CONFLICTED
    assert len(executor.mutation_commands) == 1


@pytest.mark.asyncio
async def test_expired_cleared_identity_recovers_by_reads_only_and_keeps_original_deadline():
    item, cleared = cleared_stop_case(expired=True)
    item = replace(item, lifecycle=OwnershipLifecycle.CONFLICTED)
    executor = FakeExecutor([], [cleared, cleared])
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY",
                                                              {item.m_id: item}))
    result = await instance.reconcile_expired_price(item.m_id)
    assert result.status is SchedulerTransactionStatus.SUCCESS
    recovered = instance.ownership.records[item.m_id]
    assert recovered.lifecycle is OwnershipLifecycle.ORPHANED
    assert recovered.intended_state == item.intended_state
    assert executor.mutation_commands == []
    assert len(executor.get_commands) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_ok", [True, False])
@pytest.mark.parametrize("ac_power", [0, -100])
@pytest.mark.parametrize("duration", [40, 120])
@pytest.mark.parametrize("schedule_in_100_seconds", [False, True])
async def test_unproven_output_trial_keeps_samples_without_starting_guard_or_priority_trial(
    monkeypatch, cleanup_ok, ac_power, duration, schedule_in_100_seconds
):
    from custom_components.pixiihome_controller import price_guard_trial as trial

    monkeypatch.setattr(trial.asyncio, "sleep", AsyncMock())
    priority = AsyncMock()
    monkeypatch.setattr(trial, "_priority_trial", priority)
    instance, mutations = runtime(schedule_writes=False)
    item, _ = cleared_stop_case()
    guard = SimpleNamespace(validation_lock=asyncio.Lock(), validation_bounds={})
    instance.price_optimization_controller = SimpleNamespace(soc_guard=guard)
    service = 0
    instance.telemetry.value = lambda family, key: {
        ("core", "srv_type"): service,
        ("core", "ac_w"): ac_power,
        ("meter", "meter_w"): 450,
    }.get((family, key))
    instance.telemetry.is_fresh = lambda family, **kwargs: True
    # An apparently discharging sample from before this activation must not
    # be attributed to the new test, even when a cached service ID looks right.
    instance.telemetry.received_at = {"core": datetime.now(UTC) - timedelta(seconds=1)}
    schedules = [SimpleNamespace(
        enabled=True, recurrence=SimpleNamespace(recurring=False),
        start_epoch=int(datetime.now(UTC).timestamp()) + 100,
        end_epoch=int(datetime.now(UTC).timestamp()) + 3700,
    )] if schedule_in_100_seconds else []
    instance.scheduler_coordinator.async_refresh = AsyncMock(
        return_value=SimpleNamespace(data_status="current", schedules=schedules)
    )
    instance.load_balancing_observer = SimpleNamespace(
        samples=(SimpleNamespace(current_a=1),) * 3,
        async_refresh=AsyncMock(), status="observing",
        snapshot={"observed_at": datetime.now(UTC).isoformat()},
        config=SimpleNamespace(fuses=(20, 20, 20)),
        general_service_settings={"ref_delay": 10, "meter_other_srv": 1},
    )

    async def create(request):
        nonlocal service
        mutations.created.append(request)
        instance.scheduler_ownership.records[item.m_id] = item
        service = 104
        return SimpleNamespace(status=SchedulerTransactionStatus.SUCCESS, m_id=item.m_id)

    async def stop(m_id):
        instance.scheduler_ownership.records[m_id] = replace(
            item, lifecycle=OwnershipLifecycle.ORPHANED if cleanup_ok
            else OwnershipLifecycle.CONFLICTED,
        )
        return SimpleNamespace(
            status=SchedulerTransactionStatus.SUCCESS if cleanup_ok
            else SchedulerTransactionStatus.RECONCILIATION_FAILED,
            reason="transaction_success" if cleanup_ok else "price_stop_unconfirmed",
        )

    mutations.create = AsyncMock(side_effect=create)
    mutations.stop_owned_price = AsyncMock(side_effect=stop)
    mutations.executor = SimpleNamespace(execute_get=AsyncMock(return_value=readback(item)))
    if duration == 120 and schedule_in_100_seconds:
        with pytest.raises(ValueError, match="price_guard_trial_schedule_conflict"):
            await trial.run_price_guard_validation(instance, duration_seconds=duration)
        mutations.create.assert_not_awaited()
        mutations.stop_owned_price.assert_not_awaited()
        return
    result = await trial.run_price_guard_validation(instance, duration_seconds=duration)
    assert result["status"] == "failed"
    assert result["error"] == "price_guard_trial_no_running_readback"
    assert result["native_reference_delay_seconds"] == 10
    assert result["native_meter_id"] == 1
    assert len(result["samples"]) == (95 if duration == 120 else 15)
    assert result["required_schedule_window_seconds"] == (180 if duration == 120 else 70)
    assert mutations.created[0].duration_seconds == duration
    assert all(s["ac_power_w"] == ac_power and s["meter_power_w"] == 450
               for s in result["samples"])
    assert result["stop_confirmed"] is False
    assert result["priority_handoff"]["status"] == "not_started"
    assert result["cleanup"]["status"] == (
        "success" if cleanup_ok else "reconciliation_failed"
    )
    assert len(mutations.created) == 1
    mutations.stop_owned_price.assert_awaited_once_with(item.m_id)
    assert guard.validation_bounds == {}
    priority.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [0, 41, 121, 300, True, "120"])
async def test_trial_duration_rejected_before_runtime_access(duration):
    from custom_components.pixiihome_controller.price_guard_trial import run_price_guard_validation

    with pytest.raises(ValueError, match="price_guard_trial_duration_invalid"):
        await run_price_guard_validation(None, duration_seconds=duration)


@pytest.mark.asyncio
@pytest.mark.parametrize("reserved", [False, True])
@pytest.mark.parametrize("idle_after_failure", [False, True])
@pytest.mark.parametrize("stopped,priority,completed", [
    (False, {"status": "observed", "higher_priority_seen": True}, False),
    (True, {"status": "skipped", "reason": "battery_no_longer_idle"}, False),
    (True, {"status": "observed", "higher_priority_seen": False}, False),
    (True, {"status": "failed", "reason": "priority_cleanup_unconfirmed"}, False),
    (True, {"status": "observed", "higher_priority_seen": True}, True),
])
async def test_trial_only_completes_with_physical_soc_stop_and_verified_priority(
    monkeypatch, stopped, priority, completed, idle_after_failure, reserved
):
    from custom_components.pixiihome_controller import price_guard_trial as trial

    now = datetime.now(UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    instance, mutations = runtime(schedule_writes=False)
    instance.telemetry.received_at = {"core": now}
    instance.telemetry.value = lambda family, key: {
        ("core", "srv_type"): 104 if mutations.created else 0,
        ("core", "ac_w"): -100, ("meter", "meter_w"): 500,
    }.get((family, key))
    instance.telemetry.is_fresh = lambda *args, **kwargs: True

    async def sleep(seconds):
        nonlocal now
        now += timedelta(seconds=seconds)
        instance.telemetry.received_at["core"] = now

    monkeypatch.setattr(trial, "datetime", Clock)
    monkeypatch.setattr(trial.asyncio, "sleep", sleep)
    priority_call = AsyncMock(return_value=priority)
    monkeypatch.setattr(trial, "_priority_trial", priority_call)
    instance.scheduler_coordinator.async_refresh = AsyncMock(
        return_value=SimpleNamespace(data_status="current", schedules=[])
    )
    instance.load_balancing_observer = SimpleNamespace(
        samples=(SimpleNamespace(current_a=1),) * 3, status="observing",
        async_refresh=AsyncMock(), snapshot={"observed_at": now.isoformat()},
        config=SimpleNamespace(fuses=(20, 20, 20)), general_service_settings={},
    )
    guard = PriceSocGuard(instance)
    instance.price_optimization_controller = SimpleNamespace(
        soc_guard=guard, request_refresh=lambda: None
    )

    async def create(request):
        mutations.created.append(request)
        instance.scheduler_ownership.records[M_ID] = SchedulerOwnershipRecord(
            "entry", "gateway", M_ID, "peakshaving",
            {"m_id": M_ID, "service": "peakshaving", "starttime": request.starttime.isoformat(),
             "duration": request.duration_seconds, "values": dict(request.values)},
            OwnershipLifecycle.OWNED, "automatic_price_optimization",
        )
        return SimpleNamespace(status=SchedulerTransactionStatus.SUCCESS, m_id=M_ID)

    async def stop(m_id):
        instance.scheduler_ownership.records[m_id] = replace(
            instance.scheduler_ownership.records[m_id],
            lifecycle=(OwnershipLifecycle.STOPPED_RESERVED if reserved and stopped
                       else OwnershipLifecycle.ORPHANED if stopped
                       else OwnershipLifecycle.CONFLICTED),
            reservation_until=(now + timedelta(seconds=7311)).isoformat()
            if reserved and stopped else None,
        )
        return SimpleNamespace(
            status=SchedulerTransactionStatus.OUTPUT_STOPPED if reserved and stopped
            else SchedulerTransactionStatus.SUCCESS if stopped
            else SchedulerTransactionStatus.RECONCILIATION_FAILED,
            reason="transaction_success" if stopped else "price_stop_physical_unconfirmed",
            price_stop_observation=None if stopped else {
                "idle_observed_before_deadline": idle_after_failure,
                "keyed_state": "original_schedule_inactive",
            },
        )

    mutations.create = AsyncMock(side_effect=create)
    mutations.stop_owned_price = AsyncMock(side_effect=stop)
    mutations.executor = SimpleNamespace(execute_get=AsyncMock(
        return_value=readback(record(-300))
    ))
    result = await trial.run_price_guard_validation(instance)
    assert (result["status"] == "completed") is (completed and not reserved)
    assert result["stop_confirmed"] is stopped
    assert result["physical_stop_confirmed"] is stopped
    assert result["failed_stop_observation"] == guard.last_stop_observation
    if not stopped:
        assert result["failed_stop_observation"][
            "idle_observed_before_deadline"
        ] is idle_after_failure
        assert guard.attributes()["soc_guard_last_stop_observation"] == result[
            "failed_stop_observation"
        ]
    assert priority_call.await_count == int(stopped and not reserved)
    if reserved and stopped:
        assert result["status"] == "output_stopped_priority_deferred"
        assert result["absence_confirmed"] is False
        assert result["identity_released"] is False
        assert result["priority_handoff"]["status"] == "not_started"
    mutations.stop_owned_price.assert_awaited_once_with(M_ID)
    assert guard.validation_bounds == {}
    if not stopped:
        await guard.tick(now)
        assert guard.status == "recovery_required"
        assert guard.stops == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_ok", [True, False])
async def test_priority_trial_recovers_natural_expiry_by_reads_only_and_reports_cleanup_failure(
    monkeypatch, cleanup_ok
):
    from custom_components.pixiihome_controller import price_guard_trial as trial

    now = datetime.now(UTC)
    direct_at = None
    instance, mutations = runtime(schedule_writes=False)
    instance.load_balancing_observer = SimpleNamespace(
        async_refresh=AsyncMock(), samples=(SimpleNamespace(current_a=1),) * 3,
        config=SimpleNamespace(fuses=(20, 20, 20)), status="observing",
        snapshot={"observed_at": now.isoformat()},
    )
    instance.telemetry.is_fresh = lambda *args, **kwargs: True

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    async def sleep(seconds):
        nonlocal now
        now += timedelta(seconds=seconds)

    def service(family, key):
        if key == "meter_w":
            return 500
        if key == "srv_type":
            if direct_at is not None:
                return 102 if now < direct_at + timedelta(seconds=15) else 0
            return 104 if mutations.created else 0
        return 0

    async def create(request):
        mutations.created.append(request)
        instance.scheduler_ownership.records[M_ID] = SchedulerOwnershipRecord(
            "entry", "gateway", M_ID, "peakshaving",
            {"m_id": M_ID, "service": "peakshaving", "starttime": request.starttime.isoformat(),
             "duration": request.duration_seconds, "values": dict(request.values)},
            OwnershipLifecycle.OWNED, "automatic_price_optimization",
        )
        return SimpleNamespace(status=SchedulerTransactionStatus.SUCCESS, m_id=M_ID)

    async def direct(request):
        nonlocal direct_at
        direct_at = now
        return SimpleNamespace(status=trial.CommandStatus.SUCCESS,
                               ack=readback(record(), status=5, left=15))

    monkeypatch.setattr(trial, "datetime", Clock)
    monkeypatch.setattr(trial.asyncio, "sleep", sleep)
    instance.telemetry.value = service
    mutations.create = AsyncMock(side_effect=create)
    mutations.executor = SimpleNamespace(execute_get=AsyncMock(return_value=readback(record())))
    mutations.stop_owned_price = AsyncMock()
    mutations.reconcile_expired_price = AsyncMock(return_value=SimpleNamespace(
        status=SchedulerTransactionStatus.SUCCESS if cleanup_ok
        else SchedulerTransactionStatus.RECONCILIATION_FAILED,
        reason="transaction_success" if cleanup_ok else "price_expiry_physical_unconfirmed",
    ))
    instance.command_manager = SimpleNamespace(
        allocator=SimpleNamespace(allocate=Mock(return_value=M_ID + 1), retire=Mock()),
        execute_correlated=AsyncMock(side_effect=direct),
    )
    instance.begin_control_transaction = AsyncMock()
    instance.transition_control_transaction = AsyncMock()
    result = await trial._priority_trial(instance, settling_wait_seconds=15)
    assert result["higher_priority_seen"] is True
    assert result["status"] == ("observed" if cleanup_ok else "failed")
    mutations.stop_owned_price.assert_not_awaited()
    mutations.reconcile_expired_price.assert_awaited_once_with(M_ID)
    assert len(mutations.created) == 1
