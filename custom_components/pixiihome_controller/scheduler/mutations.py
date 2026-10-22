"""Offline scheduler mutation transaction orchestration.

Ownership candidates are committed before live mutation through an injected synchronous or
awaitable persistence boundary. Recovery can additionally commit ownership, allocator identity,
and the original transaction phase atomically through the journal port.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from inspect import isawaitable
from typing import Any, Protocol

from ..allocator import MessageIdAllocator, normalize_message_id
from ..control.journal import JournalPhase, JournalTargetIdentity
from ..control.models import (
    ControlDecision,
    ControlOperation,
    ControlPolicyInput,
)
from ..control.policy import evaluate_control_operation
from ..exceptions import CommandValidationError
from ..protocol.models import MqttAck
from ..protocol.scheduler_commands import (
    ScheduledCreate,
    ScheduledDelete,
    ScheduledGet,
    ScheduledPriceStop,
    ScheduledUpdate,
    SchedulerCommand,
    evaluate_scheduler_ack,
)
from ..protocol.scheduler_readback import (
    RECONCILABLE_SCHEDULER_SERVICES,
    SchedulerExpectedState,
    SchedulerReconciliationResult,
    SchedulerReconciliationStatus,
    apply_scheduler_update,
    reconcile_cleared_price_stop,
    reconcile_scheduler_delete,
    reconcile_scheduler_get,
    scheduler_expected_state_from_command,
)
from .ownership import (
    OwnershipLifecycle,
    SchedulerMutationEvidence,
    SchedulerMutationOperation,
    SchedulerOwnershipRecord,
    SchedulerOwnershipStore,
    apply_scheduler_ownership_reconciliation,
    apply_scheduler_provisional_recovery,
    scheduler_expected_state_from_record,
)


class SchedulerExecutionStatus(StrEnum):
    """Transport-facing mutation delivery classifications."""

    ACK_SUCCESS = "ack_success"
    ACK_REJECTED = "ack_rejected"
    TIMED_OUT = "timed_out"
    DELIVERY_UNKNOWN = "delivery_unknown"
    EXECUTION_ERROR = "execution_error"


@dataclass(frozen=True, slots=True)
class SchedulerExecutionResult:
    """Sanitized result returned by an injected mutation executor."""

    status: SchedulerExecutionStatus
    ack: MqttAck | None = None
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", SchedulerExecutionStatus(self.status))
        if self.ack is not None and not isinstance(self.ack, MqttAck):
            raise CommandValidationError("scheduler execution ACK must be typed")
        if not isinstance(self.reason, str):
            raise CommandValidationError("scheduler execution reason must be a string")


class SchedulerCommandExecutor(Protocol):
    """Injected scheduler transport boundary with no Home Assistant dependency."""

    async def execute_mutation(self, command: SchedulerCommand) -> SchedulerExecutionResult:
        """Execute exactly one state-changing scheduler command."""
        ...

    async def execute_get(self, command: SchedulerCommand) -> MqttAck:
        """Execute exactly one keyed scheduler GET command."""
        ...

class SchedulerTransactionJournal(Protocol):
    """Durable write-ahead journal required before scheduler mutation I/O."""

    async def begin_control_transaction(
        self,
        *,
        transaction_id: str,
        operation: str,
        target_identity: JournalTargetIdentity,
        intent: Mapping[str, Any] | None = None,
        command_state: Mapping[str, Any] | None = None,
        ownership_state: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist a PREPARED transaction and optional identity state."""
        ...

    async def transition_control_transaction(
        self,
        transaction_id: str,
        phase: JournalPhase,
        *,
        reason: str | None = None,
    ) -> None:
        """Persist one transaction phase transition."""
        ...

    async def find_recovery_transaction_id(
        self,
        *,
        operation: str,
        target_identity: JournalTargetIdentity,
    ) -> str | None:
        """Return one exact unresolved recovery transaction, or none."""
        ...

    async def reconcile_control_transaction(
        self,
        transaction_id: str,
        *,
        command_state: dict[str, Any],
        ownership_state: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        """Persist recovered control state and transaction phase atomically."""
        ...


class SchedulerTransactionStatus(StrEnum):
    """Sanitized high-level scheduler transaction outcomes."""

    BLOCKED = "blocked"
    SUCCESS = "success"
    OUTPUT_STOPPED = "output_stopped"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    DELIVERY_UNKNOWN = "delivery_unknown"
    RECONCILIATION_FAILED = "reconciliation_failed"
    PERSISTENCE_FAILED = "persistence_failed"
    EXECUTION_ERROR = "execution_error"


@dataclass(frozen=True, slots=True)
class SchedulerCreateRequest:
    """Validated-input shape for an offline Target SoC CREATE request."""

    starttime: datetime
    duration_seconds: int
    values: Mapping[str, Any] | None = None
    service: str = "targetsoc"
    owner: str | None = None


@dataclass(frozen=True, slots=True)
class SchedulerTransactionResult:
    """Safe transaction result without raw Gateway or credential material."""

    operation: SchedulerMutationOperation
    status: SchedulerTransactionStatus
    m_id: int | None
    service: str
    lifecycle_before: OwnershipLifecycle | None = None
    lifecycle_after: OwnershipLifecycle | None = None
    execution_status: SchedulerExecutionStatus | None = None
    reconciliation_status: SchedulerReconciliationStatus | None = None
    blocked_reasons: tuple[str, ...] = ()
    reason: str = ""
    price_stop_observation: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", SchedulerMutationOperation(self.operation))
        object.__setattr__(self, "status", SchedulerTransactionStatus(self.status))
        if self.m_id is not None:
            object.__setattr__(self, "m_id", normalize_message_id(self.m_id))
        object.__setattr__(self, "blocked_reasons", tuple(self.blocked_reasons))
        if self.execution_status is not None:
            object.__setattr__(
                self,
                "execution_status",
                SchedulerExecutionStatus(self.execution_status),
            )
        if self.reconciliation_status is not None:
            object.__setattr__(
                self,
                "reconciliation_status",
                SchedulerReconciliationStatus(self.reconciliation_status),
            )
        if not isinstance(self.reason, str):
            raise CommandValidationError("scheduler transaction reason must be a string")


@dataclass(slots=True)
class SchedulerMutationDiagnostics:
    """Pure counters and the last sanitized transaction result."""

    create_attempts: int = 0
    update_attempts: int = 0
    delete_attempts: int = 0
    recovery_attempts: int = 0
    blocked_transactions: int = 0
    mutation_executor_calls: int = 0
    keyed_get_calls: int = 0
    successful_reconciliations: int = 0
    conflicted_transactions: int = 0
    persistence_failures: int = 0
    last_result: SchedulerTransactionResult | None = None

    def snapshot(self) -> dict[str, Any]:
        """Return diagnostics without triggering any I/O."""
        return {
            "create_attempts": self.create_attempts,
            "update_attempts": self.update_attempts,
            "delete_attempts": self.delete_attempts,
            "recovery_attempts": self.recovery_attempts,
            "blocked_transactions": self.blocked_transactions,
            "mutation_executor_calls": self.mutation_executor_calls,
            "keyed_get_calls": self.keyed_get_calls,
            "successful_reconciliations": self.successful_reconciliations,
            "conflicted_transactions": self.conflicted_transactions,
            "persistence_failures": self.persistence_failures,
            "last_result": self.last_result,
        }


ControlPolicyProvider = Callable[[ControlOperation], ControlPolicyInput]
SchedulerOwnershipPersistence = Callable[[dict[str, Any]], Awaitable[None] | None]
SchedulerTransactionIdFactory = Callable[[], str]
PriceStopVerifier = Callable[[datetime, datetime | None], Awaitable[bool]]


@dataclass(slots=True)
class SchedulerMutationManager:
    """Serialize offline scheduler transactions through injected boundaries."""

    allocator: MessageIdAllocator
    ownership: SchedulerOwnershipStore
    policy_provider: ControlPolicyProvider
    executor: SchedulerCommandExecutor
    persist_ownership: SchedulerOwnershipPersistence
    journal: SchedulerTransactionJournal
    transaction_id_factory: SchedulerTransactionIdFactory
    verify_price_stopped: PriceStopVerifier | None = None
    allow_reserved_price_stop: bool = False
    diagnostics: SchedulerMutationDiagnostics = field(default_factory=SchedulerMutationDiagnostics)
    _lock: asyncio.Lock = field(init=False, repr=False, default_factory=asyncio.Lock)

    async def create(
        self,
        request: SchedulerCreateRequest | ScheduledCreate,
    ) -> SchedulerTransactionResult:
        """Run one Target SoC CREATE without retrying or publishing directly."""
        async with self._lock:
            self.diagnostics.create_attempts += 1
            return await self._create_locked(request)

    async def update(self, update: ScheduledUpdate) -> SchedulerTransactionResult:
        """Run one owned Target SoC UPDATE without retrying."""
        async with self._lock:
            self.diagnostics.update_attempts += 1
            return await self._update_locked(update)

    async def delete(self, m_id: int | str) -> SchedulerTransactionResult:
        """Run one owned schedule DELETE (any reconcilable service) without retrying."""
        async with self._lock:
            self.diagnostics.delete_attempts += 1
            return await self._delete_locked(m_id)

    async def stop_owned_price(self, m_id: int) -> SchedulerTransactionResult:
        """Stop only an exact reconciled price identity; never a generic service.

        Keyed DELETE needs an exact ACK. Generic deletion still needs absence.
        The price-only reserved mode can separately confirm early output stop
        while retaining the identifier and quarantining its original interval.
        An unkeyed error or cleared metadata never proves absence.
        No retry is made after uncertain delivery or inconclusive evidence.
        """
        async with self._lock:
            operation = SchedulerMutationOperation.DELETE
            record = self._owned_record(m_id, operation, None)
            if isinstance(record, SchedulerTransactionResult):
                return record
            if record.owner != "automatic_price_optimization" or record.service not in {
                "demandresponse", "peakshaving",
            }:
                return self._local_failure(operation, record.m_id, record.service,
                                           record, "price_stop_owner_required")
            before, failure = await self._execute_get(
                ScheduledGet(m_id=record.m_id, expected_service=record.service).build()
            )
            expected = scheduler_expected_state_from_record(record)
            deadline = expected.starttime + timedelta(seconds=expected.duration)
            if deadline <= datetime.now(UTC):
                return self._local_failure(operation, record.m_id, record.service, record,
                                           "price_stop_deadline_passed")
            if failure is not None or before is None:
                return self._local_failure(operation, record.m_id, record.service, record,
                                           "price_stop_readback_unavailable")
            evidence = reconcile_scheduler_get(before, expected)
            if evidence.status is not SchedulerReconciliationStatus.MATCH:
                return self._local_failure(operation, record.m_id, record.service, record,
                                           "price_stop_identity_unverified")
            if self.verify_price_stopped is None:
                return self._local_failure(operation, record.m_id, record.service, record,
                                           "price_stop_verifier_unavailable")
            try:
                context = self.policy_provider(ControlOperation.STOP)
                context = replace(
                    context, owned_price_stop_verified=True,
                    operation_impact=replace(context.operation_impact, exact_target=True),
                )
                decision = evaluate_control_operation(context)
            except Exception:
                return self._local_failure(operation, record.m_id, record.service, record,
                                           "safety_evaluation_error")
            if not decision.permitted:
                return SchedulerTransactionResult(operation, SchedulerTransactionStatus.BLOCKED,
                    record.m_id, record.service, reason="safety_blocked",
                    blocked_reasons=decision.blocked_reasons)
            # Rev. 1.7 p25/40 documents deletion by unique m_id. The two
            # duration-zero SET variants failed live: immediate routing left
            # output running; timestamp routing returned "PS failed creating
            # schedule". Never fall back to either or to global service-off.
            command = ScheduledPriceStop(record.m_id, record.service).build()
            issued_at = datetime.now(UTC)
            execution, transaction_id = await self._execute_journaled_mutation(
                operation, record, command,
                intent={"m_id": record.m_id, "service": record.service,
                        **({"output_stopped": True, "identity_release_requires_absence": True}
                           if self.allow_reserved_price_stop else {"absent": True})},
            )
            if execution is None:
                return self._journal_persistence_failure(record, operation)
            if (
                execution.status is not SchedulerExecutionStatus.ACK_SUCCESS
                or execution.ack is None
            ):
                result = await self._handle_owned_mutation_failure(record, execution, operation)
                result = await self._finalize_journal(transaction_id, result)
                return self._finish(await self._observe_failed_price_stop(
                    result, expected, issued_at, deadline,
                ))
            if not evaluate_scheduler_ack(command, execution.ack).successful:
                result = await self._persist_conflict_result(record, operation, execution.status,
                    SchedulerTransactionStatus.RECONCILIATION_FAILED, "price_stop_ack_mismatch")
                result = await self._finalize_journal(transaction_id, result)
                return self._finish(await self._observe_failed_price_stop(
                    result, expected, issued_at, deadline,
                ))
            acknowledged_at = datetime.now(UTC)
            after, failure = await self._execute_get(
                ScheduledGet(m_id=record.m_id, expected_service=record.service).build()
            )
            if after is not None and failure is None:
                absent = reconcile_scheduler_delete(after, expected_m_id=record.m_id)
                if absent.status is SchedulerReconciliationStatus.ABSENT:
                    try:
                        was_running = before.service_status in range(5, 12) if (
                            type(before.service_status) is int
                        ) else False
                        physical = await self.verify_price_stopped(
                            acknowledged_at, deadline
                        ) if was_running or expected.starttime <= datetime.now(UTC) else True
                    except Exception:
                        physical = False
                    if not physical:
                        result = await self._persist_conflict_result(
                            record, operation, execution.status,
                            SchedulerTransactionStatus.RECONCILIATION_FAILED,
                            "price_stop_physical_unconfirmed",
                        )
                        return await self._finalize_journal(transaction_id, result)
                    # Keep the original deadline for audit and read-only recovery.
                    transitioned = replace(record, lifecycle=OwnershipLifecycle.ORPHANED)
                    return await self._persist_reconciled_result(
                        record, transitioned, operation, execution.status,
                        absent, transaction_id,
                    )
            if (
                self.allow_reserved_price_stop
                and after is None and failure is SchedulerTransactionStatus.TIMED_OUT
                and expected.starttime <= issued_at
                and type(before.service_status) is int and 5 <= before.service_status <= 11
                and (before.time_left is None
                     or type(before.time_left) is int and 0 < before.time_left <= 4294967295)
            ):
                try:
                    physical = await self.verify_price_stopped(acknowledged_at, deadline)
                except Exception:
                    physical = False
                if physical:
                    remaining = before.time_left
                    conservative_deadline = max(
                        deadline,
                        acknowledged_at + timedelta(seconds=remaining)
                        if type(remaining) is int and remaining > 0 else deadline,
                    )
                    return self._finish(await self._reserve_price_identity(
                        record, OwnershipLifecycle.STOPPED_RESERVED,
                        operation, transaction_id=transaction_id,
                        reservation_until=conservative_deadline.isoformat(),
                    ))
            result = await self._persist_conflict_result(record, operation, execution.status,
                SchedulerTransactionStatus.RECONCILIATION_FAILED, "price_stop_unconfirmed")
            result = await self._finalize_journal(transaction_id, result)
            return self._finish(await self._observe_failed_price_stop(
                result, expected, issued_at, deadline,
            ))

    async def _observe_failed_price_stop(
        self, result: SchedulerTransactionResult, expected: SchedulerExpectedState,
        issued_at: datetime, deadline: datetime,
    ) -> SchedulerTransactionResult:
        """Observe partial effects without accepting or retrying a failed stop.

        Firmware 2.3.63 returned response 0 while the plant subsequently went
        idle and the original schedule remained. Persist the failure first;
        neither idle telemetry nor inactive metadata can turn it into success.
        This bounded observation uses GET only and keeps the original deadline.
        """
        observation: dict[str, Any] = {
            "m_id": expected.m_id,
            "original_deadline": deadline.isoformat(),
            "idle_observed_before_deadline": False,
            "keyed_state": "unavailable",
            "readback_failure": None,
            "observation_error": None,
        }
        try:
            async with asyncio.timeout(20):
                assert self.verify_price_stopped is not None
                try:
                    observation["idle_observed_before_deadline"] = await self.verify_price_stopped(
                        issued_at, deadline,
                    )
                except Exception:
                    observation["observation_error"] = "physical_observation_failed"
                after, failure = await self._execute_get(
                    ScheduledGet(m_id=expected.m_id, expected_service=expected.service).build()
                )
                if failure is not None:
                    observation["readback_failure"] = failure.value
                elif after is not None:
                    original = reconcile_scheduler_get(after, expected)
                    stopped = replace(expected, duration=0)
                    cancelled = reconcile_scheduler_get(after, stopped)
                    inactive = after.service_status in {2, 12} and after.time_left in {None, 0}
                    if original.status is SchedulerReconciliationStatus.ABSENT:
                        observation["keyed_state"] = "absent"
                    elif original.status is SchedulerReconciliationStatus.MATCH:
                        observation["keyed_state"] = (
                            "original_schedule_inactive" if inactive
                            else "original_schedule_present"
                        )
                    elif cancelled.status is SchedulerReconciliationStatus.MATCH and inactive:
                        observation["keyed_state"] = "stopped_schedule_inactive"
                    elif reconcile_cleared_price_stop(after, stopped) is not None:
                        # One cleared reply is descriptive only, not cancellation proof.
                        observation["keyed_state"] = "cleared_object"
                    else:
                        observation["keyed_state"] = "unverified"
        except TimeoutError:
            observation["observation_error"] = "timed_out"
        except Exception:
            observation["observation_error"] = "execution_error"
        return replace(result, price_stop_observation=observation)

    async def recover_provisional(self, m_id: int | str) -> SchedulerTransactionResult:
        """Use one keyed GET to recover a provisional record without CREATE retry."""
        async with self._lock:
            self.diagnostics.recovery_attempts += 1
            return await self._recover_provisional_locked(m_id)

    async def recover_conflicted_price_delete(self, m_id: int) -> SchedulerTransactionResult:
        """Resolve an own price schedule whose DELETE outcome was unconfirmed.

        One keyed GET, nothing is sent. Proven absence (response 11) retires
        the identity; an exact, unchanged schedule is owned again so the plan
        can decide what to do with it. Anything else stays conflicted.
        """
        async with self._lock:
            self.diagnostics.recovery_attempts += 1
            operation = SchedulerMutationOperation.DELETE
            record = self.ownership.get(m_id)
            if (
                record is None
                or record.owner != "automatic_price_optimization"
                or record.service not in {"demandresponse", "peakshaving"}
                or record.lifecycle is not OwnershipLifecycle.CONFLICTED
            ):
                return self._local_failure(operation, m_id, "demandresponse", record,
                                           "price_delete_recovery_not_applicable")
            # A pending CREATE or UPDATE (for example a SOC-guard stop) has its
            # own recovery path; never settle it as a delete.
            for other in (SchedulerMutationOperation.CREATE, SchedulerMutationOperation.UPDATE):
                if await self.journal.find_recovery_transaction_id(
                    operation=other.value,
                    target_identity=JournalTargetIdentity(m_id=record.m_id),
                ) is not None:
                    return self._local_failure(operation, record.m_id, record.service, record,
                                               "price_delete_recovery_not_applicable")
            try:
                expected = scheduler_expected_state_from_record(record)
            except (CommandValidationError, TypeError, ValueError):
                return self._local_failure(operation, record.m_id, record.service, record,
                                           "recovery_expected_state_invalid")
            readback, failure = await self._execute_get(
                ScheduledGet(m_id=record.m_id, expected_service=record.service).build()
            )
            if failure is not None or readback is None:
                return self._local_failure(operation, record.m_id, record.service, record,
                                           "recovery_readback_execution_failed")
            absent = reconcile_scheduler_delete(readback, expected_m_id=record.m_id)
            if absent.status is SchedulerReconciliationStatus.ABSENT:
                transitioned = replace(record, lifecycle=OwnershipLifecycle.ORPHANED)
                reconciliation = absent
            else:
                present = reconcile_scheduler_get(readback, expected)
                if (
                    present.status is not SchedulerReconciliationStatus.MATCH
                    or expected.starttime <= datetime.now(UTC)
                ):
                    return self._local_failure(operation, record.m_id, record.service, record,
                                               "recovery_readback_unresolved")
                transitioned = replace(record, lifecycle=OwnershipLifecycle.OWNED)
                reconciliation = present
            return self._finish(await self._persist_recovery_result(
                record, transitioned, reconciliation, operation=operation,
            ))

    async def reconcile_expired_price(self, m_id: int) -> SchedulerTransactionResult:
        """Retire an expired own price identity using GET only, never a retry.

        A rejected stop may leave a conflict even after natural expiry. Both
        the original deadline and exact idle/absent readback must prove expiry;
        a pending, changed or kept-active service remains blocked.
        """
        async with self._lock:
            operation = SchedulerMutationOperation.UPDATE
            record = self.ownership.get(m_id)
            if record is None:
                return self._local_failure(operation, m_id, "peakshaving", None,
                                           "price_expiry_record_missing")
            if (
                record.owner != "automatic_price_optimization"
                or record.service not in {"demandresponse", "peakshaving"}
                or record.lifecycle not in {OwnershipLifecycle.OWNED, OwnershipLifecycle.CONFLICTED,
                                            OwnershipLifecycle.STOPPED_RESERVED}
            ):
                return self._local_failure(operation, m_id, record.service, record,
                                           "price_expiry_owner_required")
            expected = scheduler_expected_state_from_record(record)
            if expected.duration <= 0 or (
                expected.starttime + timedelta(seconds=expected.duration) > datetime.now(UTC)
            ) or (
                record.reservation_until is not None
                and datetime.fromisoformat(record.reservation_until) > datetime.now(UTC)
            ):
                return self._local_failure(operation, m_id, record.service, record,
                                           "price_expiry_deadline_not_passed")
            before, failure = await self._execute_get(
                ScheduledGet(m_id=m_id, expected_service=record.service).build()
            )
            if failure is not None or before is None:
                if (self.allow_reserved_price_stop
                        and record.lifecycle is OwnershipLifecycle.STOPPED_RESERVED
                        and failure is SchedulerTransactionStatus.TIMED_OUT):
                    try:
                        physical = self.verify_price_stopped is not None and (
                            await self.verify_price_stopped(datetime.now(UTC), None)
                        )
                    except Exception:
                        physical = False
                    if physical:
                        return self._finish(await self._reserve_price_identity(
                            record, OwnershipLifecycle.EXPIRED_RESERVED, operation,
                        ))
                return self._local_failure(operation, m_id, record.service, record,
                                           "price_expiry_readback_unavailable")
            evidence = reconcile_scheduler_get(before, expected)
            absent = reconcile_scheduler_delete(before, expected_m_id=m_id)
            cleared = await self._confirm_cleared_price_stop(
                before, replace(expected, duration=0)
            )
            if not (
                absent.status is SchedulerReconciliationStatus.ABSENT
                or cleared is not None
                or evidence.status is SchedulerReconciliationStatus.MATCH
                and before.service_status in {2, 12}
                and before.time_left in {None, 0}
            ):
                return self._local_failure(operation, m_id, record.service, record,
                                           "price_expiry_not_verified")
            # Cleared/absent keyed metadata can coexist with this same running
            # identity. Recovery must not silently remove that conflict.
            try:
                physical = self.verify_price_stopped is not None and (
                    await self.verify_price_stopped(datetime.now(UTC), None)
                )
            except Exception:
                physical = False
            if not physical:
                return self._local_failure(operation, m_id, record.service, record,
                                           "price_expiry_physical_unconfirmed")
            transitioned = replace(record, lifecycle=OwnershipLifecycle.ORPHANED)
            return await self._persist_reconciled_result(
                record, transitioned, operation, None,
                absent if absent.status is SchedulerReconciliationStatus.ABSENT
                else cleared if cleared is not None else evidence,
            )

    async def _reserve_price_identity(
        self, record: SchedulerOwnershipRecord, lifecycle: OwnershipLifecycle,
        operation: SchedulerMutationOperation, *, transaction_id: str | None = None,
        reservation_until: str | None = None,
    ) -> SchedulerTransactionResult:
        """Record physical cessation/finite expiry without claiming MQTT absence.

        Keep allocator ownership and the complete original interval. Only
        independently identified absence may subsequently release the identity.
        Neither this terminal reservation nor an unkeyed reply permits reuse,
        DELETE retry, update or automatic adoption of a native schedule index.
        """
        transitioned = replace(record, lifecycle=lifecycle,
                               reservation_until=reservation_until or record.reservation_until)
        reason = ("price_output_stopped_identity_reserved"
                  if lifecycle is OwnershipLifecycle.STOPPED_RESERVED
                  else "price_interval_expired_identity_reserved")
        try:
            if transaction_id is not None:
                await self.journal.reconcile_control_transaction(
                    transaction_id, command_state=self.allocator.snapshot(),
                    ownership_state=self.ownership.snapshot_with_replacement(transitioned),
                    reason=reason,
                )
                self.ownership.replace(transitioned)
            elif not await self._persist_transition(record, transitioned):
                raise OSError("reserved price identity persistence failed")
        except Exception:
            return SchedulerTransactionResult(
                operation, SchedulerTransactionStatus.PERSISTENCE_FAILED,
                record.m_id, record.service, lifecycle_before=record.lifecycle,
                lifecycle_after=record.lifecycle, reason="price_reservation_persistence_failed",
            )
        return SchedulerTransactionResult(
            operation,
            (SchedulerTransactionStatus.OUTPUT_STOPPED
             if lifecycle is OwnershipLifecycle.STOPPED_RESERVED
             else SchedulerTransactionStatus.SUCCESS),
            record.m_id, record.service,
            lifecycle_before=record.lifecycle, lifecycle_after=lifecycle,
            reconciliation_status=SchedulerReconciliationStatus.AMBIGUOUS,
            reason=reason,
            price_stop_observation={
                "m_id": record.m_id,
                "physical_stop_confirmed": lifecycle is OwnershipLifecycle.STOPPED_RESERVED,
                "expiry_idle_confirmed": lifecycle is OwnershipLifecycle.EXPIRED_RESERVED,
                "absence_confirmed": False, "identity_released": False,
                "reservation_until": transitioned.reservation_until,
                "original_deadline": (
                    scheduler_expected_state_from_record(record).starttime
                    + timedelta(seconds=scheduler_expected_state_from_record(record).duration)
                ).isoformat(),
            },
        )

    def price_stop_quarantine_active(self) -> bool:
        """Require read-only expiry verification, not merely a passed clock time."""
        return any(record.lifecycle is OwnershipLifecycle.STOPPED_RESERVED
                   for record in self.ownership.records.values())

    async def _confirm_cleared_price_stop(
        self, first: MqttAck, stopped: SchedulerExpectedState
    ) -> SchedulerReconciliationResult | None:
        """Repeat reads only; never retry a stop or accept status 0 alone."""
        if reconcile_cleared_price_stop(first, stopped) is None:
            return None
        await asyncio.sleep(1)
        second, failure = await self._execute_get(
            ScheduledGet(m_id=stopped.m_id, expected_service=stopped.service).build()
        )
        if failure is not None or second is None:
            return None
        return reconcile_cleared_price_stop(second, stopped)

    def diagnostics_snapshot(self) -> dict[str, Any]:
        """Return pure diagnostics without performing I/O."""
        return self.diagnostics.snapshot()

    async def _create_locked(
        self,
        request: SchedulerCreateRequest | ScheduledCreate,
    ) -> SchedulerTransactionResult:
        if self.price_stop_quarantine_active():
            return self._finish(SchedulerTransactionResult(
                SchedulerMutationOperation.CREATE, SchedulerTransactionStatus.BLOCKED,
                None, self._request_service(request), reason="price_stop_quarantine_active",
                blocked_reasons=("price_stop_quarantine_active",),
            ))
        try:
            service, starttime, duration_seconds, values = self._create_parts(request)
            self._validate_create(service, starttime, duration_seconds, values)
        except (CommandValidationError, TypeError, ValueError):
            return self._finish(
                SchedulerTransactionResult(
                    SchedulerMutationOperation.CREATE,
                    SchedulerTransactionStatus.EXECUTION_ERROR,
                    None,
                    self._request_service(request),
                    reason="request_invalid",
                )
            )

        future_only = (
            isinstance(request, SchedulerCreateRequest)
            and request.owner == "automatic_price_optimization"
            and starttime.astimezone(UTC) > datetime.now(UTC) + timedelta(minutes=1)
        )
        decision = self._policy_decision(ControlOperation.CREATE, future_only=future_only)
        if decision is None:
            return self._finish(
                SchedulerTransactionResult(
                    SchedulerMutationOperation.CREATE,
                    SchedulerTransactionStatus.EXECUTION_ERROR,
                    None,
                    service,
                    reason="safety_evaluation_error",
                )
            )
        if not decision.permitted:
            return self._finish(
                SchedulerTransactionResult(
                    SchedulerMutationOperation.CREATE,
                    SchedulerTransactionStatus.BLOCKED,
                    None,
                    service,
                    blocked_reasons=decision.blocked_reasons,
                    reason="safety_blocked",
                )
            )

        before_active = set(self.allocator.active_ids)
        try:
            m_id = self.allocator.allocate(persistent=True)
        except Exception:
            m_id = self._allocated_after_failure(before_active)
            return self._finish(
                SchedulerTransactionResult(
                    SchedulerMutationOperation.CREATE,
                    SchedulerTransactionStatus.PERSISTENCE_FAILED,
                    m_id,
                    service,
                    reason="allocator_persistence_failed",
                )
            )

        try:
            command = ScheduledCreate(
                service=service,
                m_id=m_id,
                starttime=starttime,
                duration_seconds=duration_seconds,
                values=values,
            ).build()
            expected = scheduler_expected_state_from_command(command)
            record = SchedulerOwnershipRecord(
                config_entry_id=self.ownership.config_entry_id,
                gateway_serial=self.ownership.gateway_serial,
                m_id=m_id,
                service=service,
                intended_state=expected.as_mapping(),
                lifecycle=OwnershipLifecycle.PROVISIONAL,
                owner=request.owner if isinstance(request, SchedulerCreateRequest) else None,
            )
            transaction_id = self.transaction_id_factory()
            try:
                await self.journal.begin_control_transaction(
                    transaction_id=transaction_id,
                    operation=SchedulerMutationOperation.CREATE.value,
                    target_identity=JournalTargetIdentity(m_id=record.m_id),
                    intent=expected.as_mapping(),
                    command_state=self.allocator.snapshot(),
                    ownership_state=self.ownership.snapshot_with_registered(record),
                )
                self.ownership.register(record)
            except Exception:
                self.diagnostics.persistence_failures += 1
                return self._finish(
                    SchedulerTransactionResult(
                        SchedulerMutationOperation.CREATE,
                        SchedulerTransactionStatus.PERSISTENCE_FAILED,
                        m_id,
                        service,
                        lifecycle_after=OwnershipLifecycle.PROVISIONAL,
                        reason="journal_prepare_persistence_failed",
                    )
                )
        except (CommandValidationError, TypeError, ValueError):
            return self._finish(
                SchedulerTransactionResult(
                    SchedulerMutationOperation.CREATE,
                    SchedulerTransactionStatus.EXECUTION_ERROR,
                    m_id,
                    service,
                    lifecycle_after=OwnershipLifecycle.PROVISIONAL,
                    reason="request_invalid_after_allocation",
                )
            )

        return self._finish(
            await self._run_create_mutation(
                record=record,
                command=command,
                expected=expected,
                prepared_transaction_id=transaction_id,
            )
        )

    async def _update_locked(self, update: ScheduledUpdate) -> SchedulerTransactionResult:
        record = self._owned_record(update.m_id, SchedulerMutationOperation.UPDATE, update.service)
        if isinstance(record, SchedulerTransactionResult):
            return self._finish(record)
        if record.service not in RECONCILABLE_SCHEDULER_SERVICES:
            return self._finish(
                self._local_failure(
                    SchedulerMutationOperation.UPDATE,
                    record.m_id,
                    record.service,
                    record,
                    "service_not_reconcilable",
                )
            )
        try:
            current = scheduler_expected_state_from_record(record)
            command = update.build()
            expected = apply_scheduler_update(current, command)
        except (CommandValidationError, TypeError, ValueError):
            return self._finish(
                self._local_failure(
                    SchedulerMutationOperation.UPDATE,
                    record.m_id,
                    record.service,
                    record,
                    "update_invalid",
                )
            )
        decision = self._policy_decision(ControlOperation.UPDATE)
        if decision is None:
            return self._finish(
                self._local_failure(
                    SchedulerMutationOperation.UPDATE,
                    record.m_id,
                    record.service,
                    record,
                    "safety_evaluation_error",
                )
            )
        if not decision.permitted:
            return self._finish(
                SchedulerTransactionResult(
                    SchedulerMutationOperation.UPDATE,
                    SchedulerTransactionStatus.BLOCKED,
                    record.m_id,
                    record.service,
                    lifecycle_before=record.lifecycle,
                    lifecycle_after=record.lifecycle,
                    blocked_reasons=decision.blocked_reasons,
                    reason="safety_blocked",
                )
            )
        return self._finish(
            await self._run_owned_mutation(
                operation=SchedulerMutationOperation.UPDATE,
                record=record,
                command=command,
                expected=expected,
            )
        )

    async def _delete_locked(self, m_id: int | str) -> SchedulerTransactionResult:
        record = self._owned_record(m_id, SchedulerMutationOperation.DELETE, None)
        if isinstance(record, SchedulerTransactionResult):
            return self._finish(record)
        if record.service not in RECONCILABLE_SCHEDULER_SERVICES:
            return self._finish(
                self._local_failure(
                    SchedulerMutationOperation.DELETE,
                    record.m_id,
                    record.service,
                    record,
                    "service_not_reconcilable",
                )
            )
        decision = self._policy_decision(ControlOperation.DELETE)
        if decision is None:
            return self._finish(
                self._local_failure(
                    SchedulerMutationOperation.DELETE,
                    record.m_id,
                    record.service,
                    record,
                    "safety_evaluation_error",
                )
            )
        if not decision.permitted:
            return self._finish(
                SchedulerTransactionResult(
                    SchedulerMutationOperation.DELETE,
                    SchedulerTransactionStatus.BLOCKED,
                    record.m_id,
                    record.service,
                    lifecycle_before=record.lifecycle,
                    lifecycle_after=record.lifecycle,
                    blocked_reasons=decision.blocked_reasons,
                    reason="safety_blocked",
                )
            )
        command = ScheduledDelete(m_id=record.m_id, expected_service=record.service).build()
        execution, transaction_id = await self._execute_journaled_mutation(
            SchedulerMutationOperation.DELETE,
            record,
            command,
            intent={"m_id": record.m_id, "service": record.service, "absent": True},
        )
        if execution is None:
            return self._finish(
                self._journal_persistence_failure(
                    record,
                    SchedulerMutationOperation.DELETE,
                )
            )
        if execution.status is not SchedulerExecutionStatus.ACK_SUCCESS or execution.ack is None:
            result = await self._handle_owned_mutation_failure(
                record,
                execution,
                SchedulerMutationOperation.DELETE,
            )
            return self._finish(await self._finalize_journal(transaction_id, result))
        mutation_ack = evaluate_scheduler_ack(command, execution.ack)
        if not mutation_ack.successful:
            result = await self._persist_conflict_result(
                record,
                SchedulerMutationOperation.DELETE,
                execution.status,
                SchedulerTransactionStatus.REJECTED
                if mutation_ack.status.value == "rejected"
                else SchedulerTransactionStatus.RECONCILIATION_FAILED,
                "mutation_ack_rejected"
                if mutation_ack.status.value == "rejected"
                else "mutation_ack_mismatch",
            )
            return self._finish(await self._finalize_journal(transaction_id, result))
        readback, get_failure = await self._execute_get(
            ScheduledGet(m_id=record.m_id, expected_service=record.service).build()
        )
        if get_failure is not None:
            result = await self._persist_conflict_result(
                record,
                SchedulerMutationOperation.DELETE,
                execution.status,
                get_failure,
                "readback_execution_failed",
            )
            return self._finish(await self._finalize_journal(transaction_id, result))
        assert readback is not None
        reconciliation = reconcile_scheduler_delete(readback, expected_m_id=record.m_id)
        evidence = SchedulerMutationEvidence(
            operation=SchedulerMutationOperation.DELETE,
            m_id=record.m_id,
            service=record.service,
            mutation_ack=mutation_ack,
            expected_state=None,
            readback=reconciliation,
        )
        transitioned = apply_scheduler_ownership_reconciliation(record, evidence)
        result = await self._persist_reconciled_result(
            record,
            transitioned,
            SchedulerMutationOperation.DELETE,
            execution.status,
            reconciliation,
            transaction_id,
        )
        return self._finish(
            await self._finalize_reconciled_journal(transaction_id, result)
        )

    async def _recover_provisional_locked(
        self,
        m_id: int | str,
    ) -> SchedulerTransactionResult:
        try:
            normalized_id = normalize_message_id(m_id)
        except CommandValidationError:
            return self._finish(
                SchedulerTransactionResult(
                    SchedulerMutationOperation.CREATE,
                    SchedulerTransactionStatus.EXECUTION_ERROR,
                    None,
                    "targetsoc",
                    reason="recovery_m_id_invalid",
                )
            )
        record = self.ownership.get(normalized_id)
        if record is None:
            return self._finish(
                SchedulerTransactionResult(
                    SchedulerMutationOperation.CREATE,
                    SchedulerTransactionStatus.EXECUTION_ERROR,
                    normalized_id,
                    "targetsoc",
                    reason="recovery_record_missing",
                )
            )
        if record.lifecycle is not OwnershipLifecycle.PROVISIONAL:
            return self._finish(
                self._local_failure(
                    SchedulerMutationOperation.CREATE,
                    record.m_id,
                    record.service,
                    record,
                    "recovery_record_not_provisional",
                )
            )
        try:
            expected = scheduler_expected_state_from_record(record)
        except (CommandValidationError, TypeError, ValueError):
            return self._finish(
                self._local_failure(
                    SchedulerMutationOperation.CREATE,
                    record.m_id,
                    record.service,
                    record,
                    "recovery_expected_state_invalid",
                )
            )
        readback, get_failure = await self._execute_get(
            ScheduledGet(m_id=record.m_id, expected_service=record.service).build()
        )
        if get_failure is not None:
            return self._finish(
                SchedulerTransactionResult(
                    SchedulerMutationOperation.CREATE,
                    get_failure,
                    record.m_id,
                    record.service,
                    lifecycle_before=record.lifecycle,
                    lifecycle_after=record.lifecycle,
                    reason="recovery_readback_execution_failed",
                )
            )
        assert readback is not None
        reconciliation = reconcile_scheduler_get(readback, expected)
        transitioned = apply_scheduler_provisional_recovery(record, expected, reconciliation)
        if reconciliation.status in (
            SchedulerReconciliationStatus.REJECTED,
            SchedulerReconciliationStatus.AMBIGUOUS,
        ):
            return self._finish(
                SchedulerTransactionResult(
                    SchedulerMutationOperation.CREATE,
                    SchedulerTransactionStatus.RECONCILIATION_FAILED,
                    record.m_id,
                    record.service,
                    lifecycle_before=record.lifecycle,
                    lifecycle_after=record.lifecycle,
                    reconciliation_status=reconciliation.status,
                    reason="recovery_readback_unresolved",
                )
            )
        return self._finish(
            await self._persist_recovery_result(
                record,
                transitioned,
                reconciliation,
            )
        )

    async def _run_create_mutation(
        self,
        *,
        record: SchedulerOwnershipRecord,
        command: SchedulerCommand,
        expected: SchedulerExpectedState,
        prepared_transaction_id: str,
    ) -> SchedulerTransactionResult:
        execution, transaction_id = await self._execute_journaled_mutation(
            SchedulerMutationOperation.CREATE,
            record,
            command,
            intent=expected.as_mapping(),
            prepared_transaction_id=prepared_transaction_id,
        )
        if execution is None:
            return self._journal_persistence_failure(record, SchedulerMutationOperation.CREATE)
        if execution.status is not SchedulerExecutionStatus.ACK_SUCCESS or execution.ack is None:
            result = self._handle_create_execution_failure(record, execution)
            return await self._finalize_journal(transaction_id, result)
        mutation_ack = evaluate_scheduler_ack(command, execution.ack)
        if not mutation_ack.successful:
            if mutation_ack.status.value == "rejected":
                result = self._create_uncertain_result(
                    record,
                    SchedulerTransactionStatus.REJECTED,
                    execution.status,
                    "mutation_ack_rejected",
                )
            else:
                result = await self._persist_conflict_result(
                    record,
                    SchedulerMutationOperation.CREATE,
                    execution.status,
                    SchedulerTransactionStatus.RECONCILIATION_FAILED,
                    "mutation_ack_mismatch",
                )
            return await self._finalize_journal(transaction_id, result)
        readback, get_failure = await self._execute_get(
            ScheduledGet(m_id=record.m_id, expected_service=record.service).build()
        )
        if get_failure is not None:
            result = self._create_uncertain_result(
                record,
                get_failure,
                execution.status,
                "readback_execution_failed",
            )
            return await self._finalize_journal(transaction_id, result)
        assert readback is not None
        reconciliation = reconcile_scheduler_get(readback, expected)
        evidence = SchedulerMutationEvidence(
            operation=SchedulerMutationOperation.CREATE,
            m_id=record.m_id,
            service=record.service,
            mutation_ack=mutation_ack,
            expected_state=expected,
            readback=reconciliation,
        )
        transitioned = apply_scheduler_ownership_reconciliation(record, evidence)
        result = await self._persist_reconciled_result(
            record,
            transitioned,
            SchedulerMutationOperation.CREATE,
            execution.status,
            reconciliation,
            transaction_id,
        )
        return await self._finalize_reconciled_journal(transaction_id, result)

    async def _run_owned_mutation(
        self,
        *,
        operation: SchedulerMutationOperation,
        record: SchedulerOwnershipRecord,
        command: SchedulerCommand,
        expected: SchedulerExpectedState,
    ) -> SchedulerTransactionResult:
        execution, transaction_id = await self._execute_journaled_mutation(
            operation,
            record,
            command,
            intent=expected.as_mapping(),
        )
        if execution is None:
            return self._journal_persistence_failure(record, operation)
        if execution.status is not SchedulerExecutionStatus.ACK_SUCCESS or execution.ack is None:
            result = await self._handle_owned_mutation_failure(record, execution, operation)
            return await self._finalize_journal(transaction_id, result)
        mutation_ack = evaluate_scheduler_ack(command, execution.ack)
        if not mutation_ack.successful:
            result = await self._persist_conflict_result(
                record,
                operation,
                execution.status,
                SchedulerTransactionStatus.REJECTED
                if mutation_ack.status.value == "rejected"
                else SchedulerTransactionStatus.RECONCILIATION_FAILED,
                "mutation_ack_rejected"
                if mutation_ack.status.value == "rejected"
                else "mutation_ack_mismatch",
            )
            return await self._finalize_journal(transaction_id, result)
        readback, get_failure = await self._execute_get(
            ScheduledGet(m_id=record.m_id, expected_service=record.service).build()
        )
        if get_failure is not None:
            result = await self._persist_conflict_result(
                record,
                operation,
                execution.status,
                get_failure,
                "readback_execution_failed",
            )
            return await self._finalize_journal(transaction_id, result)
        assert readback is not None
        reconciliation = reconcile_scheduler_get(readback, expected)
        evidence = SchedulerMutationEvidence(
            operation=operation,
            m_id=record.m_id,
            service=record.service,
            mutation_ack=mutation_ack,
            expected_state=expected,
            readback=reconciliation,
        )
        transitioned = apply_scheduler_ownership_reconciliation(record, evidence)
        result = await self._persist_reconciled_result(
            record,
            transitioned,
            operation,
            execution.status,
            reconciliation,
            transaction_id,
        )
        return await self._finalize_reconciled_journal(transaction_id, result)

    def _handle_create_execution_failure(
        self,
        record: SchedulerOwnershipRecord,
        execution: SchedulerExecutionResult,
    ) -> SchedulerTransactionResult:
        status = {
            SchedulerExecutionStatus.ACK_REJECTED: SchedulerTransactionStatus.REJECTED,
            SchedulerExecutionStatus.TIMED_OUT: SchedulerTransactionStatus.TIMED_OUT,
            SchedulerExecutionStatus.DELIVERY_UNKNOWN: SchedulerTransactionStatus.DELIVERY_UNKNOWN,
            SchedulerExecutionStatus.EXECUTION_ERROR: SchedulerTransactionStatus.EXECUTION_ERROR,
        }.get(execution.status, SchedulerTransactionStatus.EXECUTION_ERROR)
        reason = {
            SchedulerExecutionStatus.ACK_REJECTED: "mutation_ack_rejected",
            SchedulerExecutionStatus.TIMED_OUT: "mutation_timed_out",
            SchedulerExecutionStatus.DELIVERY_UNKNOWN: "mutation_delivery_unknown",
            SchedulerExecutionStatus.EXECUTION_ERROR: "mutation_execution_error",
        }.get(execution.status, "mutation_execution_error")
        return self._create_uncertain_result(record, status, execution.status, reason)

    @staticmethod
    def _create_uncertain_result(
        record: SchedulerOwnershipRecord,
        status: SchedulerTransactionStatus,
        execution_status: SchedulerExecutionStatus | None,
        reason: str,
    ) -> SchedulerTransactionResult:
        return SchedulerTransactionResult(
            SchedulerMutationOperation.CREATE,
            status,
            record.m_id,
            record.service,
            lifecycle_before=record.lifecycle,
            lifecycle_after=record.lifecycle,
            execution_status=execution_status,
            reason=reason,
        )

    async def _execute_journaled_mutation(
        self,
        operation: SchedulerMutationOperation,
        record: SchedulerOwnershipRecord,
        command: SchedulerCommand,
        *,
        intent: Mapping[str, Any],
        prepared_transaction_id: str | None = None,
    ) -> tuple[SchedulerExecutionResult | None, str | None]:
        transaction_id = prepared_transaction_id or self.transaction_id_factory()
        try:
            if prepared_transaction_id is None:
                await self.journal.begin_control_transaction(
                    transaction_id=transaction_id,
                    operation=operation.value,
                    target_identity=JournalTargetIdentity(m_id=record.m_id),
                    intent=intent,
                )
            await self.journal.transition_control_transaction(
                transaction_id,
                JournalPhase.COMMAND_SENT,
            )
        except Exception:
            self.diagnostics.persistence_failures += 1
            return None, transaction_id

        execution = await self._execute_mutation(command)
        if execution.status is SchedulerExecutionStatus.ACK_SUCCESS and execution.ack is not None:
            try:
                await self.journal.transition_control_transaction(
                    transaction_id,
                    JournalPhase.ACKNOWLEDGED,
                )
            except Exception:
                self.diagnostics.persistence_failures += 1
                return (
                    SchedulerExecutionResult(
                        SchedulerExecutionStatus.DELIVERY_UNKNOWN,
                        reason="journal_ack_persistence_failed",
                    ),
                    transaction_id,
                )
        return execution, transaction_id

    async def _finalize_reconciled_journal(
        self,
        transaction_id: str | None,
        result: SchedulerTransactionResult,
    ) -> SchedulerTransactionResult:
        if result.status is SchedulerTransactionStatus.SUCCESS or (
            result.status is SchedulerTransactionStatus.PERSISTENCE_FAILED
            and result.reason == "journal_reconciled_commit_failed"
        ):
            return result
        return await self._finalize_journal(transaction_id, result)


    async def _finalize_journal(
        self,
        transaction_id: str | None,
        result: SchedulerTransactionResult,
    ) -> SchedulerTransactionResult:
        if transaction_id is None:
            return result
        if (
            result.operation is SchedulerMutationOperation.CREATE
            and result.lifecycle_after is OwnershipLifecycle.PROVISIONAL
        ):
            phase = JournalPhase.RECOVERY_REQUIRED
        elif result.status is SchedulerTransactionStatus.SUCCESS:
            phase = JournalPhase.RECONCILED
        elif result.status in {
            SchedulerTransactionStatus.REJECTED,
            SchedulerTransactionStatus.RECONCILIATION_FAILED,
        }:
            phase = JournalPhase.FAILED
        else:
            phase = JournalPhase.RECOVERY_REQUIRED
        try:
            await self.journal.transition_control_transaction(
                transaction_id,
                phase,
                reason=result.reason or None,
            )
        except Exception:
            self.diagnostics.persistence_failures += 1
            return replace(
                result,
                status=SchedulerTransactionStatus.PERSISTENCE_FAILED,
                reason="journal_finalization_failed",
            )
        return result

    async def _persist_recovery_result(
        self,
        record: SchedulerOwnershipRecord,
        transitioned: SchedulerOwnershipRecord,
        reconciliation: SchedulerReconciliationResult,
        *,
        operation: SchedulerMutationOperation = SchedulerMutationOperation.CREATE,
    ) -> SchedulerTransactionResult:
        transaction_id = await self.journal.find_recovery_transaction_id(
            operation=operation.value,
            target_identity=JournalTargetIdentity(m_id=record.m_id),
        )
        if transaction_id is None:
            return await self._persist_reconciled_result(
                record,
                transitioned,
                operation,
                None,
                reconciliation,
            )
        ownership_state = self.ownership.snapshot_with_replacement(transitioned)
        command_state = self.allocator.snapshot()
        if transitioned.lifecycle is OwnershipLifecycle.ORPHANED:
            command_state["owned_m_ids"] = [
                value for value in command_state["owned_m_ids"] if value != record.m_id
            ]
        try:
            await self.journal.reconcile_control_transaction(
                transaction_id,
                command_state=command_state,
                ownership_state=ownership_state,
                reason="transaction_success",
            )
        except Exception:
            self.diagnostics.persistence_failures += 1
            return SchedulerTransactionResult(
                operation,
                SchedulerTransactionStatus.PERSISTENCE_FAILED,
                record.m_id,
                record.service,
                lifecycle_before=record.lifecycle,
                lifecycle_after=record.lifecycle,
                reconciliation_status=reconciliation.status,
                reason="journal_recovery_commit_failed",
            )
        self.ownership.replace(transitioned)
        if transitioned.lifecycle is OwnershipLifecycle.ORPHANED:
            self.allocator.active_ids.discard(record.m_id)
            self.allocator.owned_ids.discard(record.m_id)
        self.diagnostics.successful_reconciliations += 1
        return SchedulerTransactionResult(
            operation,
            SchedulerTransactionStatus.SUCCESS,
            record.m_id,
            record.service,
            lifecycle_before=record.lifecycle,
            lifecycle_after=transitioned.lifecycle,
            reconciliation_status=reconciliation.status,
            reason="transaction_success",
        )


    @staticmethod
    def _journal_persistence_failure(
        record: SchedulerOwnershipRecord,
        operation: SchedulerMutationOperation,
    ) -> SchedulerTransactionResult:
        return SchedulerTransactionResult(
            operation,
            SchedulerTransactionStatus.PERSISTENCE_FAILED,
            record.m_id,
            record.service,
            lifecycle_before=record.lifecycle,
            lifecycle_after=record.lifecycle,
            reason="journal_persistence_failed",
        )

    async def _execute_mutation(self, command: SchedulerCommand) -> SchedulerExecutionResult:
        self.diagnostics.mutation_executor_calls += 1
        try:
            result = await self.executor.execute_mutation(command)
        except TimeoutError:
            return SchedulerExecutionResult(SchedulerExecutionStatus.TIMED_OUT)
        except Exception:
            return SchedulerExecutionResult(SchedulerExecutionStatus.EXECUTION_ERROR)
        if not isinstance(result, SchedulerExecutionResult):
            return SchedulerExecutionResult(SchedulerExecutionStatus.EXECUTION_ERROR)
        return result

    async def _execute_get(
        self,
        command: SchedulerCommand,
    ) -> tuple[MqttAck | None, SchedulerTransactionStatus | None]:
        self.diagnostics.keyed_get_calls += 1
        try:
            result = await self.executor.execute_get(command)
        except TimeoutError:
            return None, SchedulerTransactionStatus.TIMED_OUT
        except Exception:
            return None, SchedulerTransactionStatus.EXECUTION_ERROR
        if not isinstance(result, MqttAck):
            return None, SchedulerTransactionStatus.EXECUTION_ERROR
        return result, None

    def _owned_record(
        self,
        m_id: int | str,
        operation: SchedulerMutationOperation,
        service: str | None,
    ) -> SchedulerOwnershipRecord | SchedulerTransactionResult:
        try:
            normalized = normalize_message_id(m_id)
            record = self.ownership.require_owned(normalized)
        except (CommandValidationError, TypeError, ValueError):
            normalized = self._safe_id(m_id)
            return self._local_failure(
                operation,
                normalized,
                service or "targetsoc",
                None,
                "ownership_not_owned",
            )
        if service is not None and service.strip().lower() != record.service:
            return self._local_failure(
                operation,
                record.m_id,
                record.service,
                record,
                "service_mismatch",
            )
        return record

    def _policy_decision(
        self, operation: ControlOperation, *, future_only: bool = False,
    ) -> ControlDecision | None:
        try:
            context = self.policy_provider(operation)
        except Exception:
            return None
        if not isinstance(context, ControlPolicyInput):
            return None
        return evaluate_control_operation(replace(context, future_schedule_only=future_only))


    async def _persist_transition(
        self,
        record: SchedulerOwnershipRecord,
        transitioned: SchedulerOwnershipRecord,
    ) -> bool:
        if transitioned == record:
            return True
        try:
            candidate = self.ownership.snapshot_with_replacement(transitioned)
            persistence = self.persist_ownership(candidate)
            if isawaitable(persistence):
                await persistence
            self.ownership.replace(transitioned)
        except Exception:
            self.diagnostics.persistence_failures += 1
            return False
        return True

    async def _persist_reconciled_result(
        self,
        record: SchedulerOwnershipRecord,
        transitioned: SchedulerOwnershipRecord,
        operation: SchedulerMutationOperation,
        execution_status: SchedulerExecutionStatus | None,
        reconciliation: SchedulerReconciliationResult,
        transaction_id: str | None = None,
    ) -> SchedulerTransactionResult:
        if transaction_id is not None and transitioned.lifecycle in {
            OwnershipLifecycle.OWNED,
            OwnershipLifecycle.ORPHANED,
        }:
            ownership_state = self.ownership.snapshot_with_replacement(transitioned)
            command_state = self.allocator.snapshot()
            if transitioned.lifecycle is OwnershipLifecycle.ORPHANED:
                command_state["owned_m_ids"] = [
                    value
                    for value in command_state["owned_m_ids"]
                    if value != record.m_id
                ]
            try:
                await self.journal.reconcile_control_transaction(
                    transaction_id,
                    command_state=command_state,
                    ownership_state=ownership_state,
                    reason="transaction_success",
                )
            except Exception:
                self.diagnostics.persistence_failures += 1
                return SchedulerTransactionResult(
                    operation,
                    SchedulerTransactionStatus.PERSISTENCE_FAILED,
                    record.m_id,
                    record.service,
                    lifecycle_before=record.lifecycle,
                    lifecycle_after=record.lifecycle,
                    execution_status=execution_status,
                    reconciliation_status=reconciliation.status,
                    reason="journal_reconciled_commit_failed",
                )
            self.ownership.replace(transitioned)
            if transitioned.lifecycle is OwnershipLifecycle.ORPHANED:
                self.allocator.active_ids.discard(record.m_id)
                self.allocator.owned_ids.discard(record.m_id)
            self.diagnostics.successful_reconciliations += 1
            return SchedulerTransactionResult(
                operation,
                SchedulerTransactionStatus.SUCCESS,
                record.m_id,
                record.service,
                lifecycle_before=record.lifecycle,
                lifecycle_after=transitioned.lifecycle,
                execution_status=execution_status,
                reconciliation_status=reconciliation.status,
                reason="transaction_success",
            )
        persisted = await self._persist_transition(record, transitioned)
        if not persisted:
            return SchedulerTransactionResult(
                operation,
                SchedulerTransactionStatus.PERSISTENCE_FAILED,
                record.m_id,
                record.service,
                lifecycle_before=record.lifecycle,
                lifecycle_after=record.lifecycle,
                execution_status=execution_status,
                reconciliation_status=reconciliation.status,
                reason="ownership_persistence_failed",
            )
        if transitioned.lifecycle is OwnershipLifecycle.OWNED:
            self.diagnostics.successful_reconciliations += 1
            return SchedulerTransactionResult(
                operation,
                SchedulerTransactionStatus.SUCCESS,
                record.m_id,
                record.service,
                lifecycle_before=record.lifecycle,
                lifecycle_after=transitioned.lifecycle,
                execution_status=execution_status,
                reconciliation_status=reconciliation.status,
                reason="transaction_success",
            )
        if transitioned.lifecycle is OwnershipLifecycle.ORPHANED:
            if not await self._release_allocator_ownership(record.m_id):
                return SchedulerTransactionResult(
                    operation,
                    SchedulerTransactionStatus.PERSISTENCE_FAILED,
                    record.m_id,
                    record.service,
                    lifecycle_before=record.lifecycle,
                    lifecycle_after=transitioned.lifecycle,
                    execution_status=execution_status,
                    reconciliation_status=reconciliation.status,
                    reason="allocator_release_persistence_failed",
                )
            self.diagnostics.successful_reconciliations += 1
            return SchedulerTransactionResult(
                operation,
                SchedulerTransactionStatus.SUCCESS,
                record.m_id,
                record.service,
                lifecycle_before=record.lifecycle,
                lifecycle_after=transitioned.lifecycle,
                execution_status=execution_status,
                reconciliation_status=reconciliation.status,
                reason="transaction_success",
            )
        self.diagnostics.conflicted_transactions += 1
        return SchedulerTransactionResult(
            operation,
            SchedulerTransactionStatus.RECONCILIATION_FAILED,
            record.m_id,
            record.service,
            lifecycle_before=record.lifecycle,
            lifecycle_after=transitioned.lifecycle,
            execution_status=execution_status,
            reconciliation_status=reconciliation.status,
            reason="reconciliation_failed",
        )

    async def _persist_conflict_result(
        self,
        record: SchedulerOwnershipRecord,
        operation: SchedulerMutationOperation,
        execution_status: SchedulerExecutionStatus | None,
        status: SchedulerTransactionStatus,
        reason: str,
    ) -> SchedulerTransactionResult:
        transitioned = replace(record, lifecycle=OwnershipLifecycle.CONFLICTED)
        persisted = await self._persist_transition(record, transitioned)
        if not persisted:
            status = SchedulerTransactionStatus.PERSISTENCE_FAILED
            reason = "ownership_persistence_failed"
        else:
            self.diagnostics.conflicted_transactions += 1
        return SchedulerTransactionResult(
            operation,
            status,
            record.m_id,
            record.service,
            lifecycle_before=record.lifecycle,
            lifecycle_after=transitioned.lifecycle if persisted else record.lifecycle,
            execution_status=execution_status,
            reason=reason,
        )

    async def _handle_owned_mutation_failure(
        self,
        record: SchedulerOwnershipRecord,
        execution: SchedulerExecutionResult,
        operation: SchedulerMutationOperation,
    ) -> SchedulerTransactionResult:
        status = {
            SchedulerExecutionStatus.ACK_REJECTED: SchedulerTransactionStatus.REJECTED,
            SchedulerExecutionStatus.TIMED_OUT: SchedulerTransactionStatus.TIMED_OUT,
            SchedulerExecutionStatus.DELIVERY_UNKNOWN: SchedulerTransactionStatus.DELIVERY_UNKNOWN,
            SchedulerExecutionStatus.EXECUTION_ERROR: SchedulerTransactionStatus.EXECUTION_ERROR,
        }.get(execution.status, SchedulerTransactionStatus.EXECUTION_ERROR)
        reason = {
            SchedulerExecutionStatus.ACK_REJECTED: "mutation_ack_rejected",
            SchedulerExecutionStatus.TIMED_OUT: "mutation_timed_out",
            SchedulerExecutionStatus.DELIVERY_UNKNOWN: "mutation_delivery_unknown",
            SchedulerExecutionStatus.EXECUTION_ERROR: "mutation_execution_error",
        }.get(execution.status, "mutation_execution_error")
        return await self._persist_conflict_result(
            record,
            operation,
            execution.status,
            status,
            reason,
        )

    async def _release_allocator_ownership(self, m_id: int) -> bool:
        try:
            self.allocator.retire(m_id, release_ownership=True)
            persistence = self.persist_ownership(self.ownership.snapshot())
            if isawaitable(persistence):
                await persistence
        except Exception:
            self.allocator.owned_ids.add(m_id)
            if self.allocator.persist is not None:
                try:
                    self.allocator.persist(self.allocator.snapshot())
                except Exception:
                    pass
            self.diagnostics.persistence_failures += 1
            return False
        return True

    def _finish(self, result: SchedulerTransactionResult) -> SchedulerTransactionResult:
        if result.status is SchedulerTransactionStatus.BLOCKED:
            self.diagnostics.blocked_transactions += 1
        self.diagnostics.last_result = result
        return result

    @staticmethod
    def _request_service(request: SchedulerCreateRequest | ScheduledCreate) -> str:
        return request.service if isinstance(request, SchedulerCreateRequest) else request.service

    @staticmethod
    def _create_parts(
        request: SchedulerCreateRequest | ScheduledCreate,
    ) -> tuple[str, datetime, int, Mapping[str, Any] | None]:
        if isinstance(request, SchedulerCreateRequest):
            return request.service, request.starttime, request.duration_seconds, request.values
        if isinstance(request, ScheduledCreate):
            return request.service, request.starttime, request.duration_seconds, request.values
        raise CommandValidationError("scheduler CREATE request has an unsupported type")

    @staticmethod
    def _validate_create(
        service: str,
        starttime: datetime,
        duration_seconds: int,
        values: Mapping[str, Any] | None,
    ) -> None:
        template = ScheduledCreate(
            service=service,
            m_id=1,
            starttime=starttime,
            duration_seconds=duration_seconds,
            values=values,
        ).build()
        scheduler_expected_state_from_command(template)

    def _local_failure(
        self,
        operation: SchedulerMutationOperation,
        m_id: int | None,
        service: str,
        record: SchedulerOwnershipRecord | None,
        reason: str,
    ) -> SchedulerTransactionResult:
        return SchedulerTransactionResult(
            operation,
            SchedulerTransactionStatus.EXECUTION_ERROR,
            m_id,
            service,
            lifecycle_before=record.lifecycle if record is not None else None,
            lifecycle_after=record.lifecycle if record is not None else None,
            reason=reason,
        )

    @staticmethod
    def _safe_id(value: Any) -> int | None:
        try:
            return normalize_message_id(value)
        except (CommandValidationError, TypeError, ValueError):
            return None

    def _allocated_after_failure(self, before_active: set[int]) -> int | None:
        new_ids = self.allocator.active_ids - before_active
        return next(iter(new_ids), None)


__all__ = [
    "SchedulerCommandExecutor",
    "SchedulerCreateRequest",
    "SchedulerExecutionResult",
    "SchedulerExecutionStatus",
    "SchedulerMutationDiagnostics",
    "SchedulerMutationManager",
    "SchedulerTransactionResult",
    "SchedulerTransactionStatus",
]
