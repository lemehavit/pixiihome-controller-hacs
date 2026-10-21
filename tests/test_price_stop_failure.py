"""Rejected stop may have partial effects, never transaction success or a retry."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.pixiihome_controller import price_stop
from custom_components.pixiihome_controller.control.journal import JournalPhase
from custom_components.pixiihome_controller.protocol.mqtt import parse_ack
from custom_components.pixiihome_controller.scheduler import mutations
from custom_components.pixiihome_controller.scheduler.mutations import (
    SchedulerExecutionResult,
    SchedulerExecutionStatus,
    SchedulerTransactionStatus,
)
from custom_components.pixiihome_controller.scheduler.ownership import (
    OwnershipLifecycle,
    SchedulerOwnershipRecord,
    SchedulerOwnershipStore,
)

from .conftest import load_json
from .test_scheduler_manager import FakeExecutor, manager


def captured_case():
    data = load_json("mqtt/peakshaving_rejected_stop_2_3_63.json")
    before, ack, after = (parse_ack(data[key]) for key in ("before", "stop_ack", "after"))
    item = SchedulerOwnershipRecord(
        "entry-1", "GATEWAY", before.m_id, before.service,
        {"m_id": before.m_id, "service": before.service,
         "starttime": before.values["starttime"], "duration": before.duration,
         "values": {key: value for key, value in before.values.items()
                    if key not in {"starttime", "duration", "service_status"}}},
        OwnershipLifecycle.OWNED, "automatic_price_optimization",
    )
    return item, before, ack, after


@pytest.mark.asyncio
@pytest.mark.parametrize("effect", ["early_idle", "still_running", "stale", "foreign", "expired"])
async def test_captured_rejection_retains_failure_and_original_intent_after_read_only_observation(
    monkeypatch, effect,
):
    item, before, ack, after = captured_case()
    issued = datetime(2026, 10, 18, 17, 48, 38, 926129, tzinfo=UTC)
    if effect == "expired":
        issued = datetime(2026, 10, 18, 17, 50, 23, tzinfo=UTC)
    elapsed = 0

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return issued + timedelta(seconds=elapsed)

    def core(family, key):
        if elapsed < 5 or effect == "still_running":
            return 104 if key == "srv_type" else -80
        return (121 if effect == "foreign" else 0) if key == "srv_type" else 0

    telemetry = SimpleNamespace(
        value=core, received_at={"core": issued - timedelta(seconds=1)},
        is_fresh=lambda *args, **kwargs: effect != "stale",
    )

    async def sleep(seconds):
        nonlocal elapsed
        elapsed += seconds
        if elapsed >= 16:
            raise TimeoutError
        if elapsed % 5 == 0:
            telemetry.received_at["core"] = issued + timedelta(seconds=elapsed)

    monkeypatch.setattr(mutations, "datetime", Clock)
    monkeypatch.setattr(price_stop, "datetime", Clock)
    monkeypatch.setattr(price_stop.asyncio, "sleep", sleep)
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_REJECTED, ack)],
        [before, after],
    )
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY", {
        item.m_id: item,
    }))
    plant = SimpleNamespace(telemetry=telemetry, plant_health=lambda: SimpleNamespace(healthy=True))

    async def verify(since, deadline):
        # Conflict and failed journal must be durable BEFORE waiting for telemetry.
        assert instance.ownership.records[item.m_id].lifecycle is OwnershipLifecycle.CONFLICTED
        assert instance.journal.journal.transactions[-1].phase is JournalPhase.FAILED
        return await price_stop.verify_price_stopped(plant, since, deadline)

    instance.verify_price_stopped = verify
    result = await instance.stop_owned_price(item.m_id)
    observation = result.price_stop_observation
    assert observation is not None
    assert observation["idle_observed_before_deadline"] is (effect == "early_idle")
    assert observation["keyed_state"] == "original_schedule_inactive"
    assert result.status is SchedulerTransactionStatus.REJECTED
    assert result.reason == "mutation_ack_rejected"
    assert result.execution_status is SchedulerExecutionStatus.ACK_REJECTED
    assert instance.ownership.records[item.m_id].intended_state == item.intended_state
    assert instance.ownership.records[item.m_id].lifecycle is OwnershipLifecycle.CONFLICTED
    assert instance.journal.journal.transactions[-1].reason == "mutation_ack_rejected"
    assert instance.diagnostics_snapshot()["last_result"] == result
    await instance.stop_owned_price(item.m_id)
    assert len(executor.mutation_commands) == 1
    assert [command.m_id for command in executor.get_commands] == [item.m_id, item.m_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("readback,expected", [
    ("running", "original_schedule_present"), ("changed_values", "unverified"),
    ("wrong_id", "unverified"), ("wrong_service", "unverified"),
    ("wrong_method", "unverified"), ("absent", "absent"),
    ("cleared", "cleared_object"), ("cancelled", "stopped_schedule_inactive"),
    ("timeout", "unavailable"),
])
@pytest.mark.parametrize("execution_status", [
    SchedulerExecutionStatus.ACK_REJECTED, SchedulerExecutionStatus.TIMED_OUT,
    SchedulerExecutionStatus.DELIVERY_UNKNOWN,
])
async def test_observation_never_accepts_failed_stop_even_with_idle_or_cancelled_metadata(
    readback, expected, execution_status,
):
    item, before, ack, after = captured_case()
    # Rebase the old captured schedule into an active interval for this isolated test.
    start = (datetime.now(UTC) - timedelta(seconds=10)).replace(microsecond=0).isoformat()
    item = replace(item, intended_state={**item.intended_state, "starttime": start})
    before = replace(before, values={**before.values, "starttime": start})
    after = replace(after, values={**after.values, "starttime": start})
    if readback == "running":
        after = replace(after, service_status=5)
    elif readback == "changed_values":
        after = replace(after, values={**after.values, "discharge_max": 9000})
    elif readback == "wrong_id":
        after = replace(after, m_id=item.m_id + 1)
    elif readback == "wrong_service":
        after = replace(after, service="demandresponse")
    elif readback == "wrong_method":
        after = replace(after, method="set")
    elif readback == "absent":
        after = replace(after, response=11)
    elif readback == "cleared":
        after = replace(after, duration=0, service_status=0,
                        values={**after.values, "duration": 0, "starttime": "",
                                "service_status": 0})
    elif readback == "cancelled":
        after = replace(after, duration=0, values={**after.values, "duration": 0})
    executor = FakeExecutor(
        [SchedulerExecutionResult(execution_status, ack if execution_status
                                  is SchedulerExecutionStatus.ACK_REJECTED else None)],
        [before, TimeoutError() if readback == "timeout" else after],
    )
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY", {
        item.m_id: item,
    }))
    instance.verify_price_stopped = AsyncMock(return_value=True)
    result = await instance.stop_owned_price(item.m_id)
    assert result.status is not SchedulerTransactionStatus.SUCCESS
    assert result.price_stop_observation["idle_observed_before_deadline"] is True
    assert result.price_stop_observation["keyed_state"] == expected
    assert instance.ownership.records[item.m_id].lifecycle is OwnershipLifecycle.CONFLICTED
    assert len(executor.mutation_commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", [TimeoutError(), RuntimeError("observer unavailable")])
async def test_physical_observer_failure_still_reads_identity_and_retains_original_error(fault):
    item, before, ack, after = captured_case()
    start = (datetime.now(UTC) - timedelta(seconds=10)).replace(microsecond=0).isoformat()
    item = replace(item, intended_state={**item.intended_state, "starttime": start})
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_REJECTED, ack)],
        [replace(before, values={**before.values, "starttime": start}),
         replace(after, values={**after.values, "starttime": start})],
    )
    instance = manager(executor, store=SchedulerOwnershipStore("entry-1", "GATEWAY", {
        item.m_id: item,
    }))
    instance.verify_price_stopped = AsyncMock(side_effect=fault)
    result = await instance.stop_owned_price(item.m_id)
    assert result.status is SchedulerTransactionStatus.REJECTED
    assert result.reason == "mutation_ack_rejected"
    assert result.price_stop_observation["observation_error"] == "physical_observation_failed"
    assert result.price_stop_observation["idle_observed_before_deadline"] is False
    assert result.price_stop_observation["keyed_state"] == "original_schedule_inactive"
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 2
