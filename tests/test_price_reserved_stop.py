"""Physical cessation and finite expiry do not imply releasable MQTT identity."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.pixiihome_controller import price_guard_trial, price_stop
from custom_components.pixiihome_controller.allocator import MessageIdAllocator
from custom_components.pixiihome_controller.control.journal import JournalPhase
from custom_components.pixiihome_controller.price_controller import PriceOptimizationController
from custom_components.pixiihome_controller.price_guard import PriceSocGuard
from custom_components.pixiihome_controller.protocol.mqtt import parse_ack
from custom_components.pixiihome_controller.scheduler import mutations as mutations_module
from custom_components.pixiihome_controller.scheduler.mutations import (
    SchedulerCreateRequest,
    SchedulerExecutionResult,
    SchedulerExecutionStatus,
    SchedulerTransactionStatus,
)
from custom_components.pixiihome_controller.scheduler.ownership import (
    OwnershipLifecycle,
    SchedulerOwnershipRecord,
    SchedulerOwnershipStore,
)

from .test_price_controller import runtime
from .test_price_guard import readback, record
from .test_scheduler_manager import M_ID, FakeExecutor, create_ack, manager


def reserved_manager(*, item=None, ack_changes=None, get_after=None, physical=True):
    item = item or record(-300)
    ack = replace(create_ack(), **{
        "method": "delete", "service": item.service, **(ack_changes or {}),
    })
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, ack)],
        [readback(item), get_after or TimeoutError("unidentified or missing keyed reply")],
    )
    allocator = MessageIdAllocator(next_value=M_ID)
    assert allocator.allocate(persistent=True) == M_ID
    instance = manager(executor,
        store=SchedulerOwnershipStore("entry-1", "GATEWAY", {M_ID: item}), allocator=allocator)
    instance.allow_reserved_price_stop = True
    instance.verify_price_stopped = AsyncMock(return_value=physical)
    return instance, executor, item


@pytest.mark.asyncio
async def test_physical_stop_preserves_identity_original_deadline_and_restart_quarantine():
    instance, executor, item = reserved_manager()
    before = datetime.now(UTC)
    result = await instance.stop_owned_price(M_ID)
    assert result.status is SchedulerTransactionStatus.OUTPUT_STOPPED
    assert result.reconciliation_status.value == "ambiguous"
    assert result.price_stop_observation["physical_stop_confirmed"] is True
    assert result.price_stop_observation["absence_confirmed"] is False
    assert result.price_stop_observation["identity_released"] is False
    stopped = instance.ownership.records[M_ID]
    assert stopped.lifecycle is OwnershipLifecycle.STOPPED_RESERVED
    assert stopped.intended_state == item.intended_state
    assert M_ID in instance.allocator.owned_ids
    assert M_ID in instance.allocator.active_ids
    since, deadline = instance.verify_price_stopped.await_args.args
    assert since >= before
    assert deadline == (
        datetime.fromisoformat(item.intended_state["starttime"]) + timedelta(seconds=300)
    )
    transaction = instance.journal.journal.transactions[-1]
    assert transaction.phase is JournalPhase.RECONCILED
    assert transaction.reason == "price_output_stopped_identity_reserved"
    assert transaction.intent["output_stopped"] is True
    assert transaction.intent["identity_release_requires_absence"] is True
    assert "absent" not in transaction.intent
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 2
    # A restart cannot convert a reservation into ownership or release an ID.
    instance.ownership = SchedulerOwnershipStore.from_state(instance.ownership.snapshot(),
        config_entry_id="entry-1", gateway_serial="GATEWAY")
    assert instance.price_stop_quarantine_active()
    await instance.stop_owned_price(M_ID)
    await instance.update(SimpleNamespace(m_id=M_ID, service=item.service))
    assert len(executor.mutation_commands) == 1
    attempt = await instance.create(SchedulerCreateRequest(
        datetime.now(UTC) + timedelta(seconds=10), 30, {"socref": 50},
    ))
    assert attempt.reason == "price_stop_quarantine_active"
    assert len(executor.mutation_commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [
    {"m_id": M_ID + 1}, {"response": 0}, {"response": 11},
    {"method": "set"}, {"service": "peakshaving"},
])
async def test_reserved_stop_never_compensates_wrong_or_rejected_delete_ack(changed):
    instance, executor, _ = reserved_manager(ack_changes=changed)
    result = await instance.stop_owned_price(M_ID)
    assert result.status is not SchedulerTransactionStatus.OUTPUT_STOPPED
    assert instance.ownership.records[M_ID].lifecycle is OwnershipLifecycle.CONFLICTED
    assert len(executor.mutation_commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [
    RuntimeError("transport failed"), "present", "changed", "absent",
])
async def test_identified_evidence_and_non_timeout_failures_keep_their_existing_meaning(fault):
    item = record(-300)
    after = (readback(item) if fault == "present" else
             replace(readback(item), values={**readback(item).values, "pacref": -9000})
             if fault == "changed" else
             replace(readback(item), response=11) if fault == "absent" else fault)
    instance, _, _ = reserved_manager(item=item, get_after=after)
    result = await instance.stop_owned_price(M_ID)
    if fault == "absent":
        assert result.status is SchedulerTransactionStatus.SUCCESS
        assert result.lifecycle_after is OwnershipLifecycle.ORPHANED
    else:
        assert result.status is not SchedulerTransactionStatus.OUTPUT_STOPPED
        assert result.lifecycle_after is OwnershipLifecycle.CONFLICTED


@pytest.mark.asyncio
@pytest.mark.parametrize("future,physical", [(True, True), (False, False)])
async def test_no_claim_for_future_cancellation_or_unconfirmed_physical_stop(future, physical):
    start = datetime.now(UTC) + timedelta(seconds=60) if future else None
    instance, _, _ = reserved_manager(item=record(-300, start=start), physical=physical)
    result = await instance.stop_owned_price(M_ID)
    assert result.status is not SchedulerTransactionStatus.OUTPUT_STOPPED
    assert result.lifecycle_after is OwnershipLifecycle.CONFLICTED


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", [
    OwnershipLifecycle.CONFLICTED, OwnershipLifecycle.STOPPED_RESERVED,
])
@pytest.mark.parametrize("expired,physical", [(False, True), (True, False), (True, True)])
async def test_expiry_can_reserve_but_never_release_an_unreadable_finite_price_identity(
    lifecycle, expired, physical,
):
    item = record(-300, start=datetime.now(UTC) - timedelta(seconds=400 if expired else 10),
                  lifecycle=OwnershipLifecycle.OWNED)
    reserved_until = (datetime.now(UTC) + timedelta(seconds=-1 if expired else 290)).isoformat()
    item = replace(item, lifecycle=lifecycle,
        reservation_until=reserved_until
        if lifecycle is OwnershipLifecycle.STOPPED_RESERVED else None)
    instance, executor, _ = reserved_manager(item=item, physical=physical)
    executor.gets = [TimeoutError()]
    result = await instance.reconcile_expired_price(M_ID)
    if expired and physical and lifecycle is OwnershipLifecycle.STOPPED_RESERVED:
        assert result.status is SchedulerTransactionStatus.SUCCESS
        assert result.lifecycle_after is OwnershipLifecycle.EXPIRED_RESERVED
        assert result.price_stop_observation["physical_stop_confirmed"] is False
        assert result.price_stop_observation["expiry_idle_confirmed"] is True
        assert result.price_stop_observation["absence_confirmed"] is False
        assert not instance.price_stop_quarantine_active()
    else:
        assert result.status is not SchedulerTransactionStatus.SUCCESS
        assert instance.ownership.records[M_ID] == item
    assert executor.mutation_commands == []
    assert M_ID in instance.allocator.owned_ids


@pytest.mark.asyncio
async def test_guard_counts_reserved_physical_stop_once_without_marking_absence():
    instance, mutations = runtime()
    instance.price_optimization_controller = SimpleNamespace(request_refresh=lambda: None)
    instance.battery = replace(instance.battery, usable_soc=5, power_w=-100)
    instance.scheduler_ownership.records[M_ID] = replace(
        record(-300), config_entry_id="entry", gateway_serial="gateway",
    )

    async def stop(mid):
        item = instance.scheduler_ownership.records[mid]
        instance.scheduler_ownership.replace(replace(item,
            lifecycle=OwnershipLifecycle.STOPPED_RESERVED,
            reservation_until=(datetime.now(UTC) + timedelta(seconds=300)).isoformat()))
        return SimpleNamespace(status=SchedulerTransactionStatus.OUTPUT_STOPPED,
                               reason="price_output_stopped_identity_reserved")

    mutations.stop_owned_price = AsyncMock(side_effect=stop)
    guard = PriceSocGuard(instance)
    await guard.tick()
    await guard.tick()
    assert guard.stops == 1
    mutations.stop_owned_price.assert_awaited_once_with(M_ID)


@pytest.mark.asyncio
async def test_large_native_countdown_cannot_be_shortened_to_original_deadline():
    item = record(-300)
    instance, executor, _ = reserved_manager(item=item)
    executor.gets[0] = readback(item, left=7311)
    before = datetime.now(UTC)
    result = await instance.stop_owned_price(M_ID)
    assert result.status is SchedulerTransactionStatus.OUTPUT_STOPPED
    until = datetime.fromisoformat(instance.ownership.records[M_ID].reservation_until)
    assert until >= before + timedelta(seconds=7311)
    # Neither the original duration nor reloading metadata can shorten this hold.
    result = await instance.reconcile_expired_price(M_ID)
    assert result.status is not SchedulerTransactionStatus.SUCCESS
    assert instance.price_stop_quarantine_active()


@pytest.mark.asyncio
async def test_passed_deadline_alone_does_not_allow_new_schedule_after_restart():
    item = replace(record(-300, start=datetime.now(UTC) - timedelta(seconds=400)),
        lifecycle=OwnershipLifecycle.STOPPED_RESERVED,
        reservation_until=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    instance, executor, _ = reserved_manager(item=item)
    instance.ownership = SchedulerOwnershipStore.from_state(instance.ownership.snapshot(),
        config_entry_id="entry-1", gateway_serial="GATEWAY")
    attempt = await instance.create(SchedulerCreateRequest(
        datetime.now(UTC) + timedelta(seconds=10), 30, {"socref": 50},
    ))
    assert attempt.reason == "price_stop_quarantine_active"
    assert executor.mutation_commands == []
    assert executor.get_commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("expired", [False, True])
@pytest.mark.parametrize("idle", [False, True])
async def test_planner_requires_read_only_reservation_expiry_before_new_writes(expired, idle):
    item = replace(record(-300, start=datetime.now(UTC) - timedelta(seconds=400)),
        lifecycle=OwnershipLifecycle.STOPPED_RESERVED,
        reservation_until=(datetime.now(UTC)
                           + timedelta(seconds=-1 if expired else 300)).isoformat())
    instance, fake_mutations = runtime()
    real_manager, executor, _ = reserved_manager(item=item, physical=idle)
    executor.gets = [TimeoutError()]
    instance.scheduler_ownership = real_manager.ownership
    instance.scheduler_mutation_manager = fake_mutations

    async def reconcile(mid):
        return await real_manager.reconcile_expired_price(mid)

    fake_mutations.reconcile_expired_price = AsyncMock(side_effect=reconcile)
    controller = PriceOptimizationController(instance)
    await controller.tick()
    fake_mutations.reconcile_expired_price.assert_awaited_once_with(M_ID)
    assert bool(fake_mutations.created) is (expired and idle)
    assert executor.mutation_commands == []
    assert M_ID in real_manager.allocator.owned_ids
    if not (expired and idle):
        assert controller.reason == "price_stop_quarantine_active"


@pytest.mark.asyncio
async def test_atomic_reservation_failure_never_reports_stop_success_or_releases_id():
    from .test_scheduler_manager import FailingRecoveryJournal

    instance, executor, item = reserved_manager()
    instance.journal = FailingRecoveryJournal()
    result = await instance.stop_owned_price(M_ID)
    assert result.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
    assert instance.ownership.records[M_ID] == item
    assert M_ID in instance.allocator.owned_ids
    assert len(executor.mutation_commands) == 1
    assert instance.journal.journal.transactions[-1].phase is JournalPhase.ACKNOWLEDGED


@pytest.mark.asyncio
@pytest.mark.parametrize("case,reason", [
    ("inventory_missing", "fresh_priority_schedule_conflict"),
    ("inventory_stale", "fresh_priority_schedule_conflict"),
    ("external_schedule", "fresh_priority_schedule_conflict"),
    ("phase_old", "priority_phase_margin_not_ready"),
    ("phase_future", "priority_phase_margin_not_ready"),
    ("phase_nan", "priority_phase_margin_not_ready"),
    ("phase_missing", "priority_phase_margin_not_ready"),
    ("phase_margin", "priority_phase_margin_not_ready"),
    ("grid_export", "priority_grid_or_battery_not_ready"),
    ("grid_stale", "priority_grid_or_battery_not_ready"),
    ("battery_nan", "priority_grid_or_battery_not_ready"),
    ("external_started", "battery_no_longer_idle"),
])
async def test_priority_rechecks_changed_context_before_any_activation(case, reason):
    instance, mutations = runtime()
    now = datetime.now(UTC)
    external_started = False
    instance.telemetry = SimpleNamespace(
        value=lambda family, key: {
            ("core", "srv_type"): 102 if external_started else 0,
            ("meter", "meter_w"): -500 if case == "grid_export" else 500,
        }.get((family, key)),
        is_fresh=lambda *args, **kwargs: case != "grid_stale",
    )
    schedule = SimpleNamespace(enabled=True, recurrence=SimpleNamespace(recurring=False),
        start_epoch=int(now.timestamp()), end_epoch=int(now.timestamp()) + 600)
    snapshot = SimpleNamespace(data_status="stale" if case == "inventory_stale" else "current",
        schedules=[schedule] if case == "external_schedule" else [])
    instance.scheduler_coordinator.async_refresh = AsyncMock(
        return_value=None if case == "inventory_missing" else snapshot,
    )
    samples = (SimpleNamespace(current_a=float("nan") if case == "phase_nan"
                               else 18 if case == "phase_margin" else 1),) * (
        2 if case == "phase_missing" else 3)

    async def refresh():
        nonlocal external_started
        external_started = case == "external_started"

    instance.load_balancing_observer = SimpleNamespace(
        async_refresh=refresh, samples=samples, status="observing",
        config=SimpleNamespace(fuses=(20, 20, 20)),
        snapshot={"observed_at": (now + timedelta(seconds=
            -30 if case == "phase_old" else 30 if case == "phase_future" else 0)).isoformat()},
    )
    if case == "battery_nan":
        instance.battery = replace(instance.battery, discharge_power_limit_w=float("nan"))
    result = await price_guard_trial._priority_trial(instance)
    assert result == {"status": "skipped", "reason": reason}
    assert mutations.created == []


@pytest.mark.asyncio
async def test_replay_anonymized_delete_and_core_samples_preserves_failed_outcome(
    monkeypatch,
):
    folder = Path(__file__).parents[1] / "tests/fixtures/price_stop"
    captured = json.loads((folder / "delete_observations.json").read_text())
    outcome = json.loads((folder / "delete_outcome.json").read_text())
    mid = outcome["test_message_id"]
    before = parse_ack([event["payload"] for event in captured["observations"]
                       if event["payload"].get("method") == "get"
                       and event["payload"].get("m_id") == mid][-1])
    ack = parse_ack(outcome["delete_ack"]["payload"])
    idle = [sample for sample in outcome["core_samples"]
            if sample["at"] > outcome["delete_ack"]["at"]][:2]
    assert [sample["payload"]["ac_w"] for sample in idle] == [0, 0]
    ack_time = datetime.fromisoformat(outcome["delete_ack"]["at"])
    now = ack_time
    sample_index = 0

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    item = SchedulerOwnershipRecord("entry-1", "GATEWAY", mid, before.service,
        {"m_id": mid, "service": before.service, "starttime": before.values["starttime"],
         "duration": before.duration,
         "values": {key: value for key, value in before.values.items()
                    if key not in {
                        "duration", "starttime", "service_duration_left", "service_status",
                    }}},
        OwnershipLifecycle.OWNED, "automatic_price_optimization")
    executor = FakeExecutor([SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, ack)],
                            [before, TimeoutError()])
    allocator = MessageIdAllocator(next_value=mid)
    allocator.allocate(persistent=True)
    instance = manager(executor,
        store=SchedulerOwnershipStore("entry-1", "GATEWAY", {mid: item}), allocator=allocator)
    instance.allow_reserved_price_stop = True
    telemetry = SimpleNamespace(
        value=lambda family, key: idle[sample_index]["payload"][key],
        received_at={"core": datetime.fromisoformat(idle[0]["at"])},
        is_fresh=lambda *args, **kwargs: True,
    )

    async def sleep(seconds):
        nonlocal sample_index, now
        sample_index += 1
        if sample_index >= len(idle):
            raise TimeoutError
        now = datetime.fromisoformat(idle[sample_index]["at"])
        telemetry.received_at["core"] = now

    async def verify(since, deadline):
        nonlocal now
        now = datetime.fromisoformat(idle[0]["at"])
        return await price_stop.verify_price_stopped(SimpleNamespace(
            telemetry=telemetry, plant_health=lambda: SimpleNamespace(healthy=True),
        ), since, deadline)

    monkeypatch.setattr(mutations_module, "datetime", Clock)
    monkeypatch.setattr(price_stop, "datetime", Clock)
    monkeypatch.setattr(price_stop.asyncio, "sleep", sleep)
    instance.verify_price_stopped = verify
    result = await instance.stop_owned_price(mid)
    assert result.status is SchedulerTransactionStatus.OUTPUT_STOPPED
    assert result.price_stop_observation["absence_confirmed"] is False
    assert datetime.fromisoformat(instance.ownership.records[mid].reservation_until) >= (
        ack_time + timedelta(seconds=7311)
    )
    assert instance.price_stop_quarantine_active()
    assert mid in instance.allocator.owned_ids
    assert len(executor.mutation_commands) == 1
    assert outcome["status"] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("absent", [True, False])
async def test_planner_settles_an_unconfirmed_own_delete_before_new_writes(absent):
    from .test_scheduler_manager import _timed_out_price_delete, get_ack

    get = (replace(get_ack(), response=11, m_id=M_ID, service="demandresponse")
           if absent else TimeoutError())
    executor, store, _, real_manager = _timed_out_price_delete([get])
    await real_manager.delete(M_ID)
    instance, fake_mutations = runtime()
    instance.scheduler_ownership = store
    instance.scheduler_mutation_manager = fake_mutations
    fake_mutations.recover_conflicted_price_delete = AsyncMock(
        side_effect=real_manager.recover_conflicted_price_delete
    )
    controller = PriceOptimizationController(instance)
    await controller.tick()
    fake_mutations.recover_conflicted_price_delete.assert_awaited_once()
    assert fake_mutations.recover_conflicted_price_delete.await_args.args == (M_ID,)
    assert len(executor.mutation_commands) == 1  # never a second DELETE
    assert store.records[M_ID].lifecycle is (
        OwnershipLifecycle.ORPHANED if absent else OwnershipLifecycle.CONFLICTED
    )
    # A removal confirmed by the post-check counts as one of ours.
    assert controller.delete_count == (1 if absent else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("listed", [False, True])
async def test_unanswered_get_is_settled_by_a_later_complete_list_without_the_schedule(listed):
    from custom_components.pixiihome_controller.control.settings import ControllerState

    from .test_price_controller import external_schedule
    from .test_scheduler_manager import _timed_out_price_delete

    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=3)
    executor, store, _, real_manager = _timed_out_price_delete(
        [TimeoutError(), TimeoutError()], start,
    )
    await real_manager.delete(M_ID)
    instance, fake_mutations = runtime()
    # Read-only settling also runs while price optimization is switched off.
    instance.controller_state = ControllerState()
    instance.scheduler_ownership = store
    instance.scheduler_mutation_manager = fake_mutations
    fake_mutations.recover_conflicted_price_delete = AsyncMock(
        side_effect=real_manager.recover_conflicted_price_delete
    )
    instance.scheduler_manager.normalized_schedules = (
        [replace(external_schedule(start), service_type=102)] if listed else []
    )
    controller = PriceOptimizationController(instance)
    await controller.tick()
    # The list must be read after the conflict was first seen.
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.CONFLICTED
    controller._conflicts_seen[M_ID] -= timedelta(minutes=5)
    await controller.tick()
    assert len(executor.mutation_commands) == 1
    assert store.records[M_ID].lifecycle is (
        OwnershipLifecycle.CONFLICTED if listed else OwnershipLifecycle.ORPHANED
    )
    assert controller.status == "disabled"
