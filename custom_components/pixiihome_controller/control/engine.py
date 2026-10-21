"""Canonical policy-to-journal-to-adapter execution pipeline."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any, Protocol

from .adapters import (
    AdapterExecutionStatus,
    AdapterRequest,
    AdapterResult,
    ControlAdapter,
)
from .journal import (
    ControlJournal,
    JournalPhase,
    JournalTargetIdentity,
)
from .models import ControlDecision, ControlOperation, ControlPolicyInput
from .policy import evaluate_control_operation


class EngineStatus(StrEnum):
    SUCCESS = "success"
    BLOCKED = "blocked"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class EngineResult:
    status: EngineStatus
    decision: ControlDecision
    adapter_result: AdapterResult | None = None
    transaction_id: str | None = None
    reason: str | None = None


class TransactionJournalPort(Protocol):
    async def begin_control_transaction(
        self,
        *,
        transaction_id: str,
        operation: str,
        target_identity: JournalTargetIdentity,
        intent: Mapping[str, Any] | None = None,
        command_state: Mapping[str, Any] | None = None,
        ownership_state: Mapping[str, Any] | None = None,
    ) -> None: ...

    async def transition_control_transaction(
        self,
        transaction_id: str,
        phase: JournalPhase,
        *,
        reason: str | None = None,
    ) -> None: ...

    async def find_recovery_transaction_id(
        self,
        *,
        operation: str,
        target_identity: JournalTargetIdentity,
    ) -> str | None: ...


@dataclass(slots=True)
class InMemoryJournalPort:
    """Simulator journal port with the same transition rules as durable runtime state."""

    journal: ControlJournal

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
        self.journal = self.journal.begin_transaction(
            transaction_id=transaction_id,
            operation=operation,
            target_identity=target_identity,
            intent=intent,
            command_state=command_state,
            ownership_state=ownership_state,
        )

    async def transition_control_transaction(
        self,
        transaction_id: str,
        phase: JournalPhase,
        *,
        reason: str | None = None,
    ) -> None:
        self.journal = self.journal.transition_transaction(
            transaction_id,
            phase,
            reason=reason,
        )

    async def find_recovery_transaction_id(
        self,
        *,
        operation: str,
        target_identity: JournalTargetIdentity,
    ) -> str | None:
        matches = [
            item.transaction_id
            for item in self.journal.transactions
            if item.operation == operation
            and item.target_identity == target_identity
            and item.phase is JournalPhase.RECOVERY_REQUIRED
        ]
        return matches[0] if len(matches) == 1 else None

    async def reconcile_control_transaction(
        self,
        transaction_id: str,
        *,
        command_state: dict[str, Any],
        ownership_state: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        candidate = self.journal.reconcile_transaction(
            transaction_id,
            command_state=command_state,
            ownership_state=ownership_state,
            reason=reason,
        )
        self.journal = candidate


TransactionIdFactory = Callable[[], str]


@dataclass(slots=True)
class ControlEngine:
    """Execute only decisions permitted by the canonical policy."""

    adapter: ControlAdapter
    journal: TransactionJournalPort
    transaction_id_factory: TransactionIdFactory

    async def execute(
        self,
        request: AdapterRequest,
        context: ControlPolicyInput,
    ) -> EngineResult:
        capability = self.adapter.capability(request.operation, request.service)
        evaluated_context = replace(
            context,
            operation=request.operation,
            requested_service=request.service,
            adapter_capability=capability,
        )
        decision = evaluate_control_operation(evaluated_context)
        if not decision.permitted:
            return EngineResult(EngineStatus.BLOCKED, decision, reason="policy_blocked")

        if request.operation is ControlOperation.READ_ONLY:
            result = await self.adapter.execute(request)
            return EngineResult(
                EngineStatus.SUCCESS
                if result.status is AdapterExecutionStatus.SUCCESS
                else EngineStatus.FAILED,
                decision,
                adapter_result=result,
                reason=result.reason,
            )

        target_identity = request.target_identity
        if target_identity is None:
            return EngineResult(
                EngineStatus.BLOCKED,
                ControlDecision(False, ("target_identity_required",)),
                reason="target_identity_required",
            )

        transaction_id = self.transaction_id_factory()
        await self.journal.begin_control_transaction(
            transaction_id=transaction_id,
            operation=request.operation.value,
            target_identity=target_identity,
            intent=_journal_intent(request.payload),
        )
        await self.journal.transition_control_transaction(
            transaction_id,
            JournalPhase.COMMAND_SENT,
        )
        try:
            result = await self.adapter.execute(request)
        except Exception as err:
            await self.journal.transition_control_transaction(
                transaction_id,
                JournalPhase.RECOVERY_REQUIRED,
                reason=type(err).__name__,
            )
            return EngineResult(
                EngineStatus.RECOVERY_REQUIRED,
                decision,
                transaction_id=transaction_id,
                reason="adapter_execution_uncertain",
            )

        if result.status in {
            AdapterExecutionStatus.TIMED_OUT,
            AdapterExecutionStatus.DELIVERY_UNKNOWN,
        }:
            await self.journal.transition_control_transaction(
                transaction_id,
                JournalPhase.RECOVERY_REQUIRED,
                reason=result.reason or result.status.value,
            )
            return EngineResult(
                EngineStatus.RECOVERY_REQUIRED,
                decision,
                adapter_result=result,
                transaction_id=transaction_id,
                reason=result.reason or result.status.value,
            )
        if result.status is not AdapterExecutionStatus.SUCCESS:
            await self.journal.transition_control_transaction(
                transaction_id,
                JournalPhase.FAILED,
                reason=result.reason or result.status.value,
            )
            return EngineResult(
                EngineStatus.FAILED,
                decision,
                adapter_result=result,
                transaction_id=transaction_id,
                reason=result.reason,
            )

        await self.journal.transition_control_transaction(
            transaction_id,
            JournalPhase.ACKNOWLEDGED,
        )
        await self.journal.transition_control_transaction(
            transaction_id,
            JournalPhase.RECONCILED,
        )
        return EngineResult(
            EngineStatus.SUCCESS,
            decision,
            adapter_result=result,
            transaction_id=transaction_id,
        )


def _journal_intent(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("journal intent datetime must be timezone-aware")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _journal_intent(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_journal_intent(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("journal intent contains unsupported state")


__all__ = [
    "ControlEngine",
    "EngineResult",
    "EngineStatus",
    "InMemoryJournalPort",
    "TransactionJournalPort",
]
