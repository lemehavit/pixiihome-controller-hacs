from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from custom_components.pixiihome_controller.allocator import MessageIdAllocator
from custom_components.pixiihome_controller.control.engine import InMemoryJournalPort
from custom_components.pixiihome_controller.control.journal import ControlJournal, JournalPhase
from custom_components.pixiihome_controller.control.models import (
    ControlOperation,
    ControlPolicyInput,
    OperationImpact,
    OperatorMode,
    PlantHealth,
    ReleaseState,
    RuntimeMode,
    ScheduleOrigin,
)
from custom_components.pixiihome_controller.protocol.mqtt import parse_ack
from custom_components.pixiihome_controller.protocol.scheduler_commands import (
    ScheduledCreate,
    ScheduledUpdate,
)
from custom_components.pixiihome_controller.protocol.scheduler_readback import (
    scheduler_expected_state_from_command,
)
from custom_components.pixiihome_controller.scheduler.mutations import (
    SchedulerCreateRequest,
    SchedulerExecutionResult,
    SchedulerExecutionStatus,
    SchedulerMutationManager,
    SchedulerTransactionStatus,
)
from custom_components.pixiihome_controller.scheduler.ownership import (
    OwnershipLifecycle,
    SchedulerOwnershipRecord,
    SchedulerOwnershipStore,
    scheduler_expected_state_from_record,
)

from .conftest import load_json

M_ID = 240000001
START = datetime(2030, 3, 31, 3, 30, tzinfo=ZoneInfo("Europe/Stockholm"))


class FakeExecutor:
    def __init__(self, mutations: list[SchedulerExecutionResult], gets: list[Any]) -> None:
        self.mutations = list(mutations)
        self.gets = list(gets)
        self.mutation_commands = []
        self.get_commands = []

    async def execute_mutation(self, command):
        self.mutation_commands.append(command)
        return self.mutations.pop(0)

    async def execute_get(self, command):
        self.get_commands.append(command)
        result = self.gets.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


def safe_context(operation: ControlOperation) -> ControlPolicyInput:
    return ControlPolicyInput(
        operation=operation,
        runtime_mode=RuntimeMode.SIMULATOR,
        operator_mode=OperatorMode.LOCAL_CONTROL,
        origin=ScheduleOrigin.LOCAL_CREATED,
        release_state=ReleaseState.PREVIEW_ONLY,
        plant_health=PlantHealth(
            telemetry_fresh=True,
            system_safe=True,
            battery_safe=True,
            grid_safe=True,
            system_events_clear=True,
            reasons=(),
        ),
        operation_impact=OperationImpact(
            active_service=0,
            immediate_effect_known=True,
            exact_target=True,
        ),
        scheduler_visibility_fresh=True,
        ownership_store_valid=True,
        ownership_ambiguous=False,
        mqtt_connected=True,
    )


def ack(name: str):
    return parse_ack(load_json(f"mqtt/{name}.json"))

class FailingJournal:
    async def begin_control_transaction(self, **kwargs: Any) -> None:
        del kwargs
        raise OSError("durable store unavailable")

    async def transition_control_transaction(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise AssertionError("transition must not follow a failed prepare")

class FailingRecoveryJournal(InMemoryJournalPort):
    def __init__(self, journal: ControlJournal | None = None) -> None:
        super().__init__(journal or ControlJournal("entry-1", "GATEWAY"))

    async def reconcile_control_transaction(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        raise OSError("atomic recovery commit unavailable")


class FailOnceRecoveryJournal(InMemoryJournalPort):
    def __init__(self) -> None:
        super().__init__(ControlJournal("entry-1", "GATEWAY"))
        self._failures_remaining = 1

    async def reconcile_control_transaction(self, *args: Any, **kwargs: Any) -> None:
        if self._failures_remaining:
            self._failures_remaining -= 1
            raise OSError("atomic recovery commit unavailable")
        await super().reconcile_control_transaction(*args, **kwargs)


class FailingCommandSentJournal(InMemoryJournalPort):
    def __init__(self) -> None:
        super().__init__(ControlJournal("entry-1", "GATEWAY"))

    async def transition_control_transaction(
        self,
        transaction_id: str,
        phase: JournalPhase,
        *,
        reason: str | None = None,
    ) -> None:
        if phase is JournalPhase.COMMAND_SENT:
            raise OSError("command-sent commit unavailable")
        await super().transition_control_transaction(
            transaction_id,
            phase,
            reason=reason,
        )





def create_ack():
    return ack("scheduler_ack_success")


def get_ack():
    return ack("scheduler_get_ack_targetsoc_success")


def delete_ack():
    return replace(create_ack(), method="delete")


def expected_state(m_id: int = M_ID, *, socref: int = 90, duration: int = 3600):
    command = ScheduledCreate(
        service="targetsoc",
        m_id=m_id,
        starttime=START,
        duration_seconds=duration,
        values={"socref": socref},
    ).build()
    return scheduler_expected_state_from_command(command)


def matching_get(m_id: int = M_ID, *, socref: int = 90, duration: int = 3600):
    state = expected_state(m_id, socref=socref, duration=duration)
    source = get_ack()
    return replace(
        source,
        m_id=m_id,
        service="targetsoc",
        socref=socref,
        duration=duration,
        values={
            **source.values,
            "socref": socref,
            "duration": duration,
            "starttime": state.as_mapping()["starttime"],
        },
    )


def store_with_record(lifecycle: OwnershipLifecycle = OwnershipLifecycle.OWNED):
    state = expected_state()
    record = SchedulerOwnershipRecord(
        config_entry_id="entry-1",
        gateway_serial="GATEWAY",
        m_id=M_ID,
        service="targetsoc",
        intended_state=state.as_mapping(),
        lifecycle=lifecycle,
    )
    return SchedulerOwnershipStore(
        config_entry_id="entry-1",
        gateway_serial="GATEWAY",
        records={M_ID: record},
    )


def manager(executor: FakeExecutor, store=None, allocator=None, persist=None, journal=None):
    transaction_number = 0

    def next_transaction_id() -> str:
        nonlocal transaction_number
        transaction_number += 1
        return f"test-{transaction_number}"

    return SchedulerMutationManager(
        allocator=allocator or MessageIdAllocator(next_value=M_ID),
        ownership=store or SchedulerOwnershipStore("entry-1", "GATEWAY"),
        policy_provider=safe_context,
        executor=executor,
        persist_ownership=persist or (lambda state: None),
        journal=journal or InMemoryJournalPort(ControlJournal("entry-1", "GATEWAY")),
        transaction_id_factory=next_transaction_id,
    )


@pytest.mark.asyncio
async def test_create_blocks_before_allocation_or_executor() -> None:
    executor = FakeExecutor([], [])
    allocator = MessageIdAllocator(next_value=M_ID)
    manager_instance = SchedulerMutationManager(
        allocator=allocator,
        ownership=SchedulerOwnershipStore("entry-1", "GATEWAY"),
        policy_provider=lambda operation: ControlPolicyInput(operation),
        executor=executor,
        persist_ownership=lambda state: None,
        journal=InMemoryJournalPort(ControlJournal("entry-1", "GATEWAY")),
        transaction_id_factory=lambda: "blocked-test",
    )

    result = await manager_instance.create(
        SchedulerCreateRequest(
            START,
            3600,
            {"socref": 90},
            owner="automatic_price_optimization",
        )
    )

    assert result.status is SchedulerTransactionStatus.BLOCKED
    assert allocator.active_ids == set()
    assert manager_instance.ownership.records == {}
    assert executor.mutation_commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("owner,owned,allowed", [
    ("automatic_price_optimization", True, True),
    ("automatic_price_optimization", False, False),
    (None, True, False),
])
async def test_future_price_create_during_background_aps_uses_real_mutation_policy(
    owner, owned, allowed,
):
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, create_ack())],
        [matching_get()],
    )
    instance = manager(executor)
    instance.policy_provider = lambda operation: replace(
        safe_context(operation), owned_background_aps=owned,
        operation_impact=OperationImpact(active_service=105, immediate_effect_known=True),
    )
    result = await instance.create(SchedulerCreateRequest(START, 3600, {"socref": 90}, owner=owner))
    assert (result.status is SchedulerTransactionStatus.SUCCESS) is allowed
    assert bool(executor.mutation_commands) is allowed


@pytest.mark.asyncio
async def test_create_never_calls_executor_when_journal_prepare_fails() -> None:
    executor = FakeExecutor([], [])
    result = await manager(executor, journal=FailingJournal()).create(
        SchedulerCreateRequest(START, 3600, {"socref": 90})
    )

    assert result.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
    assert result.reason == "journal_prepare_persistence_failed"
    assert executor.mutation_commands == []



@pytest.mark.asyncio
async def test_create_prepare_atomically_includes_recovery_identity_state() -> None:
    executor = FakeExecutor([], [])
    journal = FailingCommandSentJournal()
    result = await manager(executor, journal=journal).create(
        SchedulerCreateRequest(START, 3600, {"socref": 90})
    )

    assert result.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
    assert executor.mutation_commands == []
    assert journal.journal.transactions[-1].phase is JournalPhase.PREPARED
    assert journal.journal.ownership_state["records"][0]["lifecycle"] == "provisional"
    assert M_ID in journal.journal.command_state["owned_m_ids"]

@pytest.mark.asyncio
async def test_create_success_requires_one_mutation_and_matching_get() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, create_ack())],
        [matching_get()],
    )
    allocator = MessageIdAllocator(next_value=M_ID)
    manager_instance = manager(executor, allocator=allocator)

    result = await manager_instance.create(
        SchedulerCreateRequest(
            START,
            3600,
            {"socref": 90},
            owner="automatic_price_optimization",
        )
    )

    assert result.status is SchedulerTransactionStatus.SUCCESS
    assert result.lifecycle_after is OwnershipLifecycle.OWNED
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 1
    assert executor.mutation_commands[0].m_id == M_ID
    assert executor.get_commands[0].m_id == M_ID
    assert manager_instance.ownership.records[M_ID].lifecycle is OwnershipLifecycle.OWNED
    assert (
        manager_instance.ownership.records[M_ID].owner
        == "automatic_price_optimization"
    )
    assert M_ID in allocator.owned_ids
    diagnostics = manager_instance.diagnostics_snapshot()
    assert diagnostics["last_result"] is result
    assert not hasattr(result, "ack")
    assert "mutation_executor_calls" in diagnostics
    assert isinstance(manager_instance.journal, InMemoryJournalPort)
    assert manager_instance.journal.journal.transactions[-1].intent == expected_state().as_mapping()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("execution", "status"),
    (
        (
            SchedulerExecutionResult(SchedulerExecutionStatus.ACK_REJECTED),
            SchedulerTransactionStatus.REJECTED,
        ),
        (
            SchedulerExecutionResult(SchedulerExecutionStatus.TIMED_OUT),
            SchedulerTransactionStatus.TIMED_OUT,
        ),
        (
            SchedulerExecutionResult(SchedulerExecutionStatus.DELIVERY_UNKNOWN),
            SchedulerTransactionStatus.DELIVERY_UNKNOWN,
        ),
        (
            SchedulerExecutionResult(SchedulerExecutionStatus.EXECUTION_ERROR),
            SchedulerTransactionStatus.EXECUTION_ERROR,
        ),
    ),
)
async def test_create_failure_preserves_provisional_without_retry(
    execution: SchedulerExecutionResult,
    status: SchedulerTransactionStatus,
) -> None:
    executor = FakeExecutor([execution], [])
    store = SchedulerOwnershipStore("entry-1", "GATEWAY")
    manager_instance = manager(executor, store=store)

    result = await manager_instance.create(SchedulerCreateRequest(START, 3600, {"socref": 90}))

    assert result.status is status
    assert len(executor.mutation_commands) == 1
    assert executor.get_commands == []
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.PROVISIONAL
    assert M_ID in manager_instance.allocator.owned_ids
    assert isinstance(manager_instance.journal, InMemoryJournalPort)
    assert manager_instance.journal.journal.transactions[-1].phase is JournalPhase.RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_create_prepare_failure_prevents_executor_and_keeps_reservation() -> None:
    executor = FakeExecutor([], [])
    allocator = MessageIdAllocator(next_value=M_ID)
    manager_instance = manager(
        executor,
        allocator=allocator,
        journal=FailingJournal(),
    )

    result = await manager_instance.create(
        SchedulerCreateRequest(START, 3600, {"socref": 90})
    )

    assert result.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
    assert result.m_id == M_ID
    assert executor.mutation_commands == []
    assert manager_instance.ownership.records == {}
    assert M_ID in allocator.owned_ids
    assert M_ID in allocator.active_ids


@pytest.mark.asyncio
async def test_update_publishes_full_expected_post_state_and_reconciles() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, create_ack())],
        [matching_get(socref=80, duration=7200)],
    )
    store = store_with_record()
    manager_instance = manager(executor, store=store)

    result = await manager_instance.update(
        ScheduledUpdate(M_ID, values={"socref": 80}, duration_seconds=7200, service="targetsoc")
    )

    assert result.status is SchedulerTransactionStatus.SUCCESS
    assert executor.mutation_commands[0].fields == {
        "values": {"socref": 80},
        "duration": 7200,
    }
    assert store.records[M_ID].intended_state["socref"] == 80
    assert store.records[M_ID].intended_state["duration"] == 7200
    assert isinstance(manager_instance.journal, InMemoryJournalPort)
    assert manager_instance.journal.journal.transactions[-1].intent == expected_state(
        socref=80,
        duration=7200,
    ).as_mapping()


@pytest.mark.asyncio
async def test_delete_persists_orphaned_before_releasing_allocator_ownership() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, delete_ack())],
        [replace(get_ack(), method="get", response=11, m_id=M_ID, service="targetsoc")],
    )
    store = store_with_record()
    allocator = MessageIdAllocator(next_value=M_ID + 1, owned_ids={M_ID})
    persisted = []
    manager_instance = manager(
        executor,
        store=store,
        allocator=allocator,
        persist=lambda state: persisted.append(state),
    )

    result = await manager_instance.delete(M_ID)

    assert result.status is SchedulerTransactionStatus.SUCCESS
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.ORPHANED
    assert M_ID not in allocator.owned_ids
    assert persisted == []
    assert isinstance(manager_instance.journal, InMemoryJournalPort)
    assert manager_instance.journal.journal.transactions[-1].intent == {
        "m_id": M_ID,
        "service": "targetsoc",
        "absent": True,
    }
    assert (
        manager_instance.journal.journal.ownership_state["records"][0]["lifecycle"]
        == "orphaned"
    )
    assert M_ID not in manager_instance.journal.journal.command_state["owned_m_ids"]


@pytest.mark.asyncio
async def test_recovery_uses_get_only_and_promotes_exact_match() -> None:
    executor = FakeExecutor([], [matching_get()])
    store = store_with_record(OwnershipLifecycle.PROVISIONAL)
    manager_instance = manager(executor, store=store)

    result = await manager_instance.recover_provisional(M_ID)

    assert result.status is SchedulerTransactionStatus.SUCCESS
    assert result.lifecycle_after is OwnershipLifecycle.OWNED
    assert executor.mutation_commands == []
    assert [command.m_id for command in executor.get_commands] == [M_ID]


@pytest.mark.asyncio
async def test_recovery_absence_orphans_and_releases_id_without_resend() -> None:
    executor = FakeExecutor(
        [],
        [replace(get_ack(), method="get", response=11, m_id=M_ID, service="targetsoc")],
    )
    store = store_with_record(OwnershipLifecycle.PROVISIONAL)
    allocator = MessageIdAllocator(next_value=M_ID + 1, owned_ids={M_ID})
    manager_instance = manager(executor, store=store, allocator=allocator)

    result = await manager_instance.recover_provisional(M_ID)

    assert result.status is SchedulerTransactionStatus.SUCCESS
    assert result.lifecycle_after is OwnershipLifecycle.ORPHANED
    assert M_ID not in allocator.owned_ids
    assert executor.mutation_commands == []


@pytest.mark.asyncio
async def test_concurrent_transactions_are_serialized_per_manager() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingExecutor(FakeExecutor):
        async def execute_mutation(self, command):
            self.mutation_commands.append(command)
            entered.set()
            await release.wait()
            return SchedulerExecutionResult(SchedulerExecutionStatus.ACK_REJECTED)

    executor = BlockingExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_REJECTED)],
        [],
    )
    store = SchedulerOwnershipStore("entry-1", "GATEWAY")
    manager_instance = manager(executor, store=store)
    first = asyncio.create_task(
        manager_instance.create(SchedulerCreateRequest(START, 3600, {"socref": 90}))
    )
    await entered.wait()
    second = asyncio.create_task(
        manager_instance.create(SchedulerCreateRequest(START, 3600, {"socref": 91}))
    )
    await asyncio.sleep(0)
    assert len(executor.mutation_commands) == 1
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.status is SchedulerTransactionStatus.REJECTED
    assert second_result.status is SchedulerTransactionStatus.REJECTED
    assert len(executor.mutation_commands) == 2
    assert manager_instance.diagnostics_snapshot()["create_attempts"] == 2


@pytest.mark.asyncio
async def test_create_timeout_recovers_exact_state_without_resending() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.TIMED_OUT)],
        [],
    )
    store = SchedulerOwnershipStore("entry-1", "GATEWAY")
    manager_instance = manager(executor, store=store)

    failed = await manager_instance.create(SchedulerCreateRequest(START, 3600, {"socref": 90}))
    executor.gets.append(matching_get())
    recovered = await manager_instance.recover_provisional(M_ID)

    assert failed.status is SchedulerTransactionStatus.TIMED_OUT
    assert failed.lifecycle_after is OwnershipLifecycle.PROVISIONAL
    assert recovered.status is SchedulerTransactionStatus.SUCCESS
    assert recovered.lifecycle_after is OwnershipLifecycle.OWNED
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 1
    assert executor.get_commands[0].m_id == M_ID


@pytest.mark.asyncio
async def test_create_delivery_unknown_recovers_exact_state_without_resending() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.DELIVERY_UNKNOWN)],
        [matching_get()],
    )
    store = SchedulerOwnershipStore("entry-1", "GATEWAY")
    manager_instance = manager(executor, store=store)

    failed = await manager_instance.create(SchedulerCreateRequest(START, 3600, {"socref": 90}))
    recovered = await manager_instance.recover_provisional(M_ID)

    assert failed.status is SchedulerTransactionStatus.DELIVERY_UNKNOWN
    assert failed.lifecycle_after is OwnershipLifecycle.PROVISIONAL
    assert recovered.lifecycle_after is OwnershipLifecycle.OWNED
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 1
    assert isinstance(manager_instance.journal, InMemoryJournalPort)
    assert manager_instance.journal.journal.transactions[-1].phase is JournalPhase.RECONCILED


@pytest.mark.asyncio
async def test_recovery_commit_failure_leaves_ownership_and_transaction_retryable() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.DELIVERY_UNKNOWN)],
        [matching_get()],
    )
    store = SchedulerOwnershipStore("entry-1", "GATEWAY")
    journal = FailingRecoveryJournal(ControlJournal("entry-1", "GATEWAY"))
    manager_instance = manager(executor, store=store, journal=journal)

    await manager_instance.create(SchedulerCreateRequest(START, 3600, {"socref": 90}))
    recovered = await manager_instance.recover_provisional(M_ID)

    assert recovered.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
    assert recovered.reason == "journal_recovery_commit_failed"
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.PROVISIONAL
    assert M_ID in manager_instance.allocator.owned_ids
    assert journal.journal.transactions[-1].phase is JournalPhase.RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_successful_create_commits_ownership_and_terminal_phase_atomically() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, create_ack())],
        [matching_get()],
    )
    store = SchedulerOwnershipStore("entry-1", "GATEWAY")
    journal = FailingRecoveryJournal(ControlJournal("entry-1", "GATEWAY"))
    manager_instance = manager(executor, store=store, journal=journal)

    result = await manager_instance.create(
        SchedulerCreateRequest(START, 3600, {"socref": 90})
    )

    assert result.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
    assert result.reason == "journal_reconciled_commit_failed"
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.PROVISIONAL
    assert journal.journal.transactions[-1].phase is JournalPhase.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_create_rejection_recovers_absence_before_releasing_id() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_REJECTED)],
        [replace(get_ack(), method="get", response=11, m_id=M_ID, service="targetsoc")],
    )
    store = SchedulerOwnershipStore("entry-1", "GATEWAY")
    allocator = MessageIdAllocator(next_value=M_ID, owned_ids=set())
    persisted = []
    manager_instance = manager(
        executor,
        store=store,
        allocator=allocator,
        persist=lambda state: persisted.append(state),
    )

    rejected = await manager_instance.create(SchedulerCreateRequest(START, 3600, {"socref": 90}))
    assert isinstance(manager_instance.journal, InMemoryJournalPort)
    assert manager_instance.journal.journal.transactions[-1].phase is JournalPhase.RECOVERY_REQUIRED
    recovered = await manager_instance.recover_provisional(M_ID)

    assert rejected.status is SchedulerTransactionStatus.REJECTED
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.ORPHANED
    assert recovered.lifecycle_after is OwnershipLifecycle.ORPHANED
    assert M_ID not in allocator.owned_ids
    assert manager_instance.journal.journal.transactions[-1].phase is JournalPhase.RECONCILED
    assert manager_instance.journal.journal.ownership_state["records"][0]["lifecycle"] == "orphaned"
    assert M_ID not in manager_instance.journal.journal.command_state["owned_m_ids"]
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("get_failure", "status"),
    (
        (TimeoutError(), SchedulerTransactionStatus.TIMED_OUT),
        (RuntimeError("GET failed"), SchedulerTransactionStatus.EXECUTION_ERROR),
    ),
)
async def test_create_post_ack_get_failure_remains_recoverable(
    get_failure: BaseException,
    status: SchedulerTransactionStatus,
) -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, create_ack())],
        [get_failure],
    )
    store = SchedulerOwnershipStore("entry-1", "GATEWAY")
    manager_instance = manager(executor, store=store)

    failed = await manager_instance.create(SchedulerCreateRequest(START, 3600, {"socref": 90}))
    executor.gets.append(matching_get())
    recovered = await manager_instance.recover_provisional(M_ID)

    assert failed.status is status
    assert failed.lifecycle_after is OwnershipLifecycle.PROVISIONAL
    assert recovered.lifecycle_after is OwnershipLifecycle.OWNED
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "readback",
    (
        matching_get(M_ID, socref=89),
        replace(matching_get(), m_id=M_ID + 1),
        replace(matching_get(), service="voltagesupport"),
    ),
)
async def test_create_successful_get_mismatch_conflicts(
    readback: Any,
) -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, create_ack())],
        [readback],
    )
    store = SchedulerOwnershipStore("entry-1", "GATEWAY")
    manager_instance = manager(executor, store=store)

    result = await manager_instance.create(SchedulerCreateRequest(START, 3600, {"socref": 90}))

    assert result.status is SchedulerTransactionStatus.RECONCILIATION_FAILED
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.CONFLICTED
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 1


@pytest.mark.asyncio
async def test_create_absence_readback_never_claims_ownership() -> None:
    absence = replace(
        get_ack(),
        method="get",
        response=11,
        m_id=M_ID,
        service="unknown",
        response_text="arbitrary text",
    )
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, create_ack())],
        [absence],
    )
    store = SchedulerOwnershipStore("entry-1", "GATEWAY")
    manager_instance = manager(executor, store=store)

    result = await manager_instance.create(SchedulerCreateRequest(START, 3600, {"socref": 90}))

    assert result.status is SchedulerTransactionStatus.RECONCILIATION_FAILED
    assert result.lifecycle_after is OwnershipLifecycle.CONFLICTED
    assert result.lifecycle_after is not OwnershipLifecycle.OWNED
    assert result.reconciliation_status is not None
    assert result.reconciliation_status.value == "absent"
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.CONFLICTED
    assert M_ID in manager_instance.allocator.owned_ids
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 1


@pytest.mark.asyncio
async def test_update_absence_readback_is_not_accepted() -> None:
    absence = replace(
        get_ack(),
        method="get",
        response=11,
        m_id=M_ID,
        service="unknown",
        response_text=None,
    )
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, create_ack())],
        [absence],
    )
    store = store_with_record()
    intended_before = dict(store.records[M_ID].intended_state)
    manager_instance = manager(executor, store=store)

    result = await manager_instance.update(
        ScheduledUpdate(M_ID, values={"socref": 80}, service="targetsoc")
    )

    assert result.status is SchedulerTransactionStatus.RECONCILIATION_FAILED
    assert result.lifecycle_after is OwnershipLifecycle.CONFLICTED
    assert result.reconciliation_status is not None
    assert result.reconciliation_status.value == "absent"
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.CONFLICTED
    assert store.records[M_ID].intended_state == intended_before
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 1


@pytest.mark.asyncio
async def test_update_timeout_remains_conservative_and_conflicted() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.TIMED_OUT)],
        [],
    )
    store = store_with_record()
    manager_instance = manager(executor, store=store)

    result = await manager_instance.update(
        ScheduledUpdate(M_ID, values={"socref": 80}, service="targetsoc")
    )

    assert result.status is SchedulerTransactionStatus.TIMED_OUT
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.CONFLICTED
    assert store.records[M_ID].lifecycle is not OwnershipLifecycle.PROVISIONAL


@pytest.mark.asyncio
async def test_delete_timeout_does_not_release_allocator_ownership() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.TIMED_OUT)],
        [],
    )
    store = store_with_record()
    allocator = MessageIdAllocator(next_value=M_ID + 1, owned_ids={M_ID})
    manager_instance = manager(executor, store=store, allocator=allocator)

    result = await manager_instance.delete(M_ID)

    assert result.status is SchedulerTransactionStatus.TIMED_OUT
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.CONFLICTED
    assert M_ID in allocator.owned_ids
    assert executor.get_commands == []


@pytest.mark.asyncio
async def test_recovery_calls_serialize_and_never_publish_set() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingGetExecutor(FakeExecutor):
        async def execute_get(self, command):
            self.get_commands.append(command)
            if len(self.get_commands) == 1:
                entered.set()
                await release.wait()
            return self.gets.pop(0)

    executor = BlockingGetExecutor([], [matching_get(), matching_get()])
    store = store_with_record(OwnershipLifecycle.PROVISIONAL)
    manager_instance = manager(executor, store=store)
    first = asyncio.create_task(manager_instance.recover_provisional(M_ID))
    await entered.wait()
    second = asyncio.create_task(manager_instance.recover_provisional(M_ID))
    await asyncio.sleep(0)

    assert len(executor.get_commands) == 1
    assert executor.mutation_commands == []
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result.status is SchedulerTransactionStatus.SUCCESS
    assert second_result.status is SchedulerTransactionStatus.EXECUTION_ERROR
    assert len(executor.get_commands) == 1


@pytest.mark.asyncio
async def test_create_owned_commit_failure_keeps_provisional_and_recovers_get_only() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, create_ack())],
        [matching_get()],
    )
    store = SchedulerOwnershipStore("entry-1", "GATEWAY")
    journal = FailOnceRecoveryJournal()
    manager_instance = manager(executor, store=store, journal=journal)
    result = await manager_instance.create(SchedulerCreateRequest(START, 3600, {"socref": 90}))
    provisional = store.records[M_ID]
    executor.gets.append(matching_get())
    recovered = await manager_instance.recover_provisional(M_ID)

    assert result.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
    assert result.lifecycle_after is OwnershipLifecycle.PROVISIONAL
    assert provisional.lifecycle is OwnershipLifecycle.PROVISIONAL
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.OWNED
    assert recovered.status is SchedulerTransactionStatus.SUCCESS
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 2


@pytest.mark.asyncio
async def test_create_conflict_commit_failure_keeps_provisional_without_retry() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, create_ack())],
        [matching_get(M_ID, socref=89)],
    )
    store = SchedulerOwnershipStore("entry-1", "GATEWAY")
    persisted: list[dict[str, Any]] = []

    def fail_conflict_commit(state: dict[str, Any]) -> None:
        persisted.append(state)
        raise RuntimeError("conflict commit unavailable")

    manager_instance = manager(executor, store=store, persist=fail_conflict_commit)
    result = await manager_instance.create(SchedulerCreateRequest(START, 3600, {"socref": 90}))

    assert result.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
    assert result.lifecycle_after is OwnershipLifecycle.PROVISIONAL
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.PROVISIONAL
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 1


@pytest.mark.asyncio
async def test_update_commit_failure_keeps_previous_owned_state_without_retry() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, create_ack())],
        [matching_get(socref=80, duration=7200)],
    )
    store = store_with_record()
    previous = store.records[M_ID]

    manager_instance = manager(
        executor,
        store=store,
        journal=FailingRecoveryJournal(),
    )
    result = await manager_instance.update(
        ScheduledUpdate(M_ID, values={"socref": 80}, duration_seconds=7200, service="targetsoc")
    )

    assert result.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
    assert store.records[M_ID] == previous
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.OWNED
    assert len(executor.mutation_commands) == 1
    assert len(executor.get_commands) == 1


@pytest.mark.asyncio
async def test_delete_orphan_commit_failure_keeps_owned_and_reserved() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, delete_ack())],
        [replace(get_ack(), method="get", response=11, m_id=M_ID, service="targetsoc")],
    )
    store = store_with_record()
    previous = store.records[M_ID]
    allocator = MessageIdAllocator(next_value=M_ID + 1, owned_ids={M_ID})

    manager_instance = manager(
        executor,
        store=store,
        allocator=allocator,
        journal=FailingRecoveryJournal(),
    )
    result = await manager_instance.delete(M_ID)

    assert result.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
    assert store.records[M_ID] == previous
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.OWNED
    assert M_ID in allocator.owned_ids


@pytest.mark.asyncio
async def test_delete_atomic_commit_ignores_split_persistence_callbacks() -> None:
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.ACK_SUCCESS, delete_ack())],
        [replace(get_ack(), method="get", response=11, m_id=M_ID, service="targetsoc")],
    )
    store = store_with_record()

    def fail_allocator_commit(state: dict[str, Any]) -> None:
        raise RuntimeError("split allocator persistence must not run")

    allocator = MessageIdAllocator(
        next_value=M_ID + 1,
        owned_ids={M_ID},
        persist=fail_allocator_commit,
    )
    persisted = []
    manager_instance = manager(
        executor,
        store=store,
        allocator=allocator,
        persist=lambda state: persisted.append(state),
    )

    result = await manager_instance.delete(M_ID)

    assert result.status is SchedulerTransactionStatus.SUCCESS
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.ORPHANED
    assert M_ID not in allocator.owned_ids
    assert persisted == []


@pytest.mark.asyncio
async def test_recovery_owned_commit_failure_keeps_provisional_and_id() -> None:
    executor = FakeExecutor([], [matching_get()])
    store = store_with_record(OwnershipLifecycle.PROVISIONAL)
    allocator = MessageIdAllocator(next_value=M_ID + 1, owned_ids={M_ID})

    def fail_recovery_commit(state: dict[str, Any]) -> None:
        raise RuntimeError("recovery commit unavailable")

    manager_instance = manager(
        executor,
        store=store,
        allocator=allocator,
        persist=fail_recovery_commit,
    )
    result = await manager_instance.recover_provisional(M_ID)

    assert result.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.PROVISIONAL
    assert M_ID in allocator.owned_ids
    assert executor.mutation_commands == []
    assert len(executor.get_commands) == 1


@pytest.mark.asyncio
async def test_recovery_orphan_commit_failure_keeps_provisional_and_id() -> None:
    executor = FakeExecutor(
        [],
        [replace(get_ack(), method="get", response=11, m_id=M_ID, service="targetsoc")],
    )
    store = store_with_record(OwnershipLifecycle.PROVISIONAL)
    allocator = MessageIdAllocator(next_value=M_ID + 1, owned_ids={M_ID})

    def fail_recovery_orphan_commit(state: dict[str, Any]) -> None:
        raise RuntimeError("recovery orphan commit unavailable")

    manager_instance = manager(
        executor,
        store=store,
        allocator=allocator,
        persist=fail_recovery_orphan_commit,
    )
    result = await manager_instance.recover_provisional(M_ID)

    assert result.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.PROVISIONAL
    assert M_ID in allocator.owned_ids
    assert executor.mutation_commands == []
    assert len(executor.get_commands) == 1


@pytest.mark.asyncio
async def test_delete_accepts_an_owned_price_schedule_of_another_service() -> None:
    # Price optimization owns demandresponse/peakshaving schedules; deleting
    # one that left the plan must not fail on a Target SoC service check.
    store = store_with_record()
    store.records[M_ID] = replace(store.records[M_ID], service="demandresponse")
    executor = FakeExecutor([], [])
    manager_instance = manager(executor, store=store)

    result = await manager_instance.delete(M_ID)

    assert result.reason != "service_mismatch"
    assert result.service == "demandresponse"


def _timed_out_price_delete(gets: list[Any], start: datetime = START):
    command = ScheduledCreate(
        service="demandresponse", m_id=M_ID, starttime=start,
        duration_seconds=3600, values={"pacref": -5000},
    ).build()
    store = store_with_record()
    store.records[M_ID] = replace(
        store.records[M_ID], service="demandresponse",
        intended_state=scheduler_expected_state_from_command(command).as_mapping(),
        owner="automatic_price_optimization",
    )
    executor = FakeExecutor(
        [SchedulerExecutionResult(SchedulerExecutionStatus.TIMED_OUT)], gets,
    )
    allocator = MessageIdAllocator(next_value=M_ID + 1, owned_ids={M_ID})
    return executor, store, allocator, manager(executor, store=store, allocator=allocator)


def _demandresponse_get(**changes: Any):
    source = get_ack()
    values = {"pacref": -5000, "starttime": source.values["starttime"], "duration": 3600,
              "service_status": 5, "service_duration_left": 3590, **changes}
    return replace(source, service="demandresponse", socref=None, values=values)


@pytest.mark.asyncio
async def test_unconfirmed_price_delete_is_settled_by_one_get_when_absent() -> None:
    absent = replace(get_ack(), response=11, m_id=M_ID, service="demandresponse")
    executor, store, allocator, manager_instance = _timed_out_price_delete([absent])
    failed = await manager_instance.delete(M_ID)
    assert failed.lifecycle_after is OwnershipLifecycle.CONFLICTED

    recovered = await manager_instance.recover_conflicted_price_delete(M_ID)

    assert recovered.status is SchedulerTransactionStatus.SUCCESS
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.ORPHANED
    assert M_ID not in allocator.owned_ids
    assert len(executor.mutation_commands) == 1  # the original DELETE only
    assert len(executor.get_commands) == 1
    journal = manager_instance.journal
    assert isinstance(journal, InMemoryJournalPort)
    assert journal.journal.transactions[-1].phase is JournalPhase.RECONCILED


@pytest.mark.asyncio
async def test_unconfirmed_price_delete_of_an_unchanged_schedule_is_owned_again() -> None:
    executor, store, _, manager_instance = _timed_out_price_delete([_demandresponse_get()])
    await manager_instance.delete(M_ID)

    recovered = await manager_instance.recover_conflicted_price_delete(M_ID)

    assert recovered.status is SchedulerTransactionStatus.SUCCESS
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.OWNED
    assert len(executor.mutation_commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", [TimeoutError(), "changed"])
async def test_unconfirmed_price_delete_stays_conflicted_without_proof(readback) -> None:
    get = _demandresponse_get(pacref=-3000) if readback == "changed" else readback
    executor, store, _, manager_instance = _timed_out_price_delete([get])
    await manager_instance.delete(M_ID)

    recovered = await manager_instance.recover_conflicted_price_delete(M_ID)

    assert recovered.status is not SchedulerTransactionStatus.SUCCESS
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.CONFLICTED
    assert len(executor.mutation_commands) == 1
    journal = manager_instance.journal
    assert isinstance(journal, InMemoryJournalPort)
    assert journal.journal.transactions[-1].phase is JournalPhase.RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_conflict_recovery_ignores_schedules_not_owned_by_price_optimization() -> None:
    executor, store, _, manager_instance = _timed_out_price_delete([])
    store.records[M_ID] = replace(store.records[M_ID], owner=None,
                                  lifecycle=OwnershipLifecycle.CONFLICTED)

    result = await manager_instance.recover_conflicted_price_delete(M_ID)

    assert result.reason == "price_delete_recovery_not_applicable"
    assert executor.get_commands == []


def _move_start(store: Any, start: datetime) -> datetime:
    # The clock moves on after the DELETE; shift the schedule into the past.
    record = store.records[M_ID]
    expected = replace(scheduler_expected_state_from_record(record), starttime=start)
    store.records[M_ID] = replace(record, intended_state=expected.as_mapping())
    return start.replace(microsecond=0)


@pytest.mark.asyncio
@pytest.mark.parametrize("readback", [TimeoutError(), "present"])
async def test_unconfirmed_price_delete_settles_once_its_interval_has_ended(readback) -> None:
    # 28 Oct: a 15:30 charge's DELETE was never confirmed and the keyed GET
    # kept failing, so every price write stayed blocked long after it ended.
    get = _demandresponse_get() if readback == "present" else readback
    executor, store, allocator, manager_instance = _timed_out_price_delete([get])
    await manager_instance.delete(M_ID)
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.CONFLICTED
    start = _move_start(store, datetime.now(UTC) - timedelta(hours=2))

    recovered = await manager_instance.recover_conflicted_price_delete(M_ID)

    assert recovered.status is SchedulerTransactionStatus.SUCCESS
    record = store.records[M_ID]
    assert record.lifecycle is OwnershipLifecycle.EXPIRED_RESERVED
    assert record.reservation_until == (start + timedelta(hours=1)).isoformat()
    assert M_ID in allocator.owned_ids  # never reused
    assert len(executor.mutation_commands) == 1  # never re-sent
    journal = manager_instance.journal
    assert isinstance(journal, InMemoryJournalPort)
    assert journal.journal.transactions[-1].phase is JournalPhase.RECONCILED
    assert not store.has_unresolved_records()


@pytest.mark.asyncio
async def test_unconfirmed_price_delete_waits_while_its_interval_is_running() -> None:
    executor, store, _, manager_instance = _timed_out_price_delete([TimeoutError()])
    await manager_instance.delete(M_ID)
    _move_start(store, datetime.now(UTC) - timedelta(minutes=30))

    recovered = await manager_instance.recover_conflicted_price_delete(M_ID)

    assert recovered.status is not SchedulerTransactionStatus.SUCCESS
    assert store.records[M_ID].lifecycle is OwnershipLifecycle.CONFLICTED
