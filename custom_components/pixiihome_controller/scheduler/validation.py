"""Narrow, one-shot scheduler validation write infrastructure."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import ceil, floor, isfinite
from time import monotonic
from typing import Any
from uuid import uuid4

from ..command import CommandManager, CommandStatus, CorrelatedCommandRequest
from ..control.models import ControlOperation
from ..control.policy import evaluate_control_operation
from ..exceptions import CommandValidationError
from ..protocol.commands import CommandMethod
from ..protocol.scheduler_commands import SchedulerCommand
from .models import NormalizedSchedule, ScheduleConsistency
from .mutations import (
    SchedulerCommandExecutor,
    SchedulerCreateRequest,
    SchedulerExecutionResult,
    SchedulerExecutionStatus,
    SchedulerMutationManager,
    SchedulerTransactionResult,
    SchedulerTransactionStatus,
)
from .ownership import (
    OwnershipLifecycle,
    SchedulerOwnershipRecord,
    scheduler_expected_state_from_record,
)

FIRST_WRITE_MIN_LEAD_SECONDS = 24 * 60 * 60
FIRST_WRITE_DURATION_SECONDS = 300
FIRST_WRITE_POST_GUARD_SECONDS = 60 * 60
VALIDATION_PERMIT_TTL_SECONDS = 300.0


class ValidationPermitStage(StrEnum):
    PREPARED = "prepared"
    CREATE_SET = "create_set"
    CREATE_GET = "create_get"
    DELETE = "delete"
    DELETE_GET = "delete_get"
    CONSUMED = "consumed"
    FAILED = "failed"


@dataclass(slots=True)
class SchedulerValidationPermit:
    """One in-memory Target SoC validation authorization."""

    config_entry_id: str
    gateway_serial: str
    candidate_starttime: str
    candidate_duration: int
    candidate_socref: int | float
    expires_at_monotonic: float
    stage: ValidationPermitStage = ValidationPermitStage.PREPARED
    bound_m_id: int | None = None
    _clock: Any = field(default=monotonic, repr=False)

    @classmethod
    def issue(
        cls,
        *,
        config_entry_id: str,
        gateway_serial: str,
        candidate_starttime: datetime,
        candidate_duration: int,
        candidate_socref: int | float,
        clock: Any = monotonic,
        ttl_seconds: float = VALIDATION_PERMIT_TTL_SECONDS,
    ) -> SchedulerValidationPermit:
        if not config_entry_id or not gateway_serial:
            raise CommandValidationError("validation permit scope is required")
        if candidate_starttime.tzinfo is None or candidate_starttime.utcoffset() is None:
            raise CommandValidationError("validation permit starttime must be timezone-aware")
        if candidate_duration <= 0:
            raise CommandValidationError("validation permit duration must be positive")
        if (
            isinstance(candidate_socref, bool)
            or not isinstance(candidate_socref, int | float)
            or not isfinite(float(candidate_socref))
            or not 0 <= candidate_socref <= 100
        ):
            raise CommandValidationError("validation permit socref is invalid")
        if ttl_seconds <= 0 or not isfinite(ttl_seconds):
            raise CommandValidationError("validation permit TTL is invalid")
        starttime = (
            candidate_starttime.astimezone(UTC)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return cls(
            config_entry_id=config_entry_id,
            gateway_serial=gateway_serial,
            candidate_starttime=starttime,
            candidate_duration=candidate_duration,
            candidate_socref=candidate_socref,
            expires_at_monotonic=clock() + ttl_seconds,
            _clock=clock,
        )

    @property
    def is_active(self) -> bool:
        return (
            self.stage
            not in {
                ValidationPermitStage.CONSUMED,
                ValidationPermitStage.FAILED,
            }
            and self._clock() <= self.expires_at_monotonic
        )

    def assert_context(self, config_entry_id: str, gateway_serial: str) -> None:
        if config_entry_id != self.config_entry_id or gateway_serial != self.gateway_serial:
            self._fail()
            raise CommandValidationError("validation permit scope does not match")

    def authorize(self, request: CorrelatedCommandRequest, serial: str) -> None:
        """Validate one exact command and advance its operation stage."""
        if serial != self.gateway_serial or not self.is_active:
            self._fail()
            raise CommandValidationError("validation permit is inactive")
        if self.stage is ValidationPermitStage.PREPARED:
            self._authorize_create_set(request)
            self.stage = ValidationPermitStage.CREATE_SET
            self.bound_m_id = request.message_id
            return
        if self.stage is ValidationPermitStage.CREATE_SET:
            self._authorize_get(request)
            self.stage = ValidationPermitStage.CREATE_GET
            return
        if self.stage is ValidationPermitStage.DELETE:
            self._authorize_delete(request)
            self.stage = ValidationPermitStage.DELETE_GET
            return
        if self.stage is ValidationPermitStage.DELETE_GET:
            self._authorize_get(request)
            return
        self._fail()
        raise CommandValidationError("validation permit operation order is invalid")

    def mark_create_reconciled(self) -> None:
        if not self.is_active or self.stage is not ValidationPermitStage.CREATE_GET:
            self._fail()
            raise CommandValidationError("validation CREATE is not reconciled")
        self.stage = ValidationPermitStage.DELETE

    def mark_delete_reconciled(self) -> None:
        if not self.is_active or self.stage is not ValidationPermitStage.DELETE_GET:
            self._fail()
            raise CommandValidationError("validation DELETE is not reconciled")
        self.stage = ValidationPermitStage.CONSUMED

    def fail(self) -> None:
        self._fail()

    def diagnostics(self) -> dict[str, object]:
        return {
            "active": self.is_active,
            "stage": self.stage.value,
            "bound": self.bound_m_id is not None,
        }

    def _authorize_create_set(self, request: CorrelatedCommandRequest) -> None:
        if request.method is not CommandMethod.SET or request.service != "targetsoc":
            self._fail()
            raise CommandValidationError("validation CREATE requires targetsoc SET")
        if set(request.fields) != {"starttime", "duration", "values"}:
            self._fail()
            raise CommandValidationError("validation CREATE fields are not exact")
        if request.fields["starttime"] != self.candidate_starttime:
            self._fail()
            raise CommandValidationError("validation CREATE starttime does not match")
        if request.fields["duration"] != self.candidate_duration:
            self._fail()
            raise CommandValidationError("validation CREATE duration does not match")
        values = request.fields["values"]
        if not isinstance(values, Mapping) or dict(values) != {"socref": self.candidate_socref}:
            self._fail()
            raise CommandValidationError("validation CREATE values are not exact")

    def _authorize_get(self, request: CorrelatedCommandRequest) -> None:
        if (
            request.method is not CommandMethod.GET
            or request.service not in {None, "targetsoc"}
            or request.fields
            or request.expected_services != ("targetsoc",)
        ):
            self._fail()
            raise CommandValidationError("validation GET shape is not exact")
        self._authorize_m_id(request)

    def _authorize_delete(self, request: CorrelatedCommandRequest) -> None:
        if (
            request.method is not CommandMethod.DELETE
            or request.service not in {None, "targetsoc"}
            or request.fields
            or request.expected_services != ("targetsoc",)
        ):
            self._fail()
            raise CommandValidationError("validation DELETE shape is not exact")
        self._authorize_m_id(request)

    def _authorize_m_id(self, request: CorrelatedCommandRequest) -> None:
        if self.bound_m_id is None or request.message_id != self.bound_m_id:
            self._fail()
            raise CommandValidationError("validation m_id does not match")

    def _fail(self) -> None:
        self.stage = ValidationPermitStage.FAILED


@dataclass(frozen=True, slots=True)
class SchedulerCandidateInterval:
    """Half-open interval used for deterministic first-write conflict checks."""

    start_epoch: int
    end_epoch: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.start_epoch, bool)
            or isinstance(self.end_epoch, bool)
            or not isinstance(self.start_epoch, int)
            or not isinstance(self.end_epoch, int)
            or self.end_epoch <= self.start_epoch
        ):
            raise CommandValidationError("candidate interval must have a positive duration")


@dataclass(frozen=True, slots=True)
class SchedulerCandidateConflictResult:
    """Sanitized candidate conflict decision."""

    blocked: bool
    reasons: tuple[str, ...] = ()
    overlap_count: int = 0


def evaluate_candidate_conflicts(
    candidate: SchedulerCandidateInterval,
    schedules: Iterable[NormalizedSchedule],
    owned_records: Iterable[SchedulerOwnershipRecord] = (),
) -> SchedulerCandidateConflictResult:
    """Reject every unsafe or overlapping schedule for the first validation."""
    reasons: list[str] = []
    overlap_count = 0
    for schedule in schedules:
        if schedule.recurrence.recurring is not False:
            reasons.append("schedule_recurrence_not_proven_absent")
            continue
        if schedule.consistency is ScheduleConsistency.MISMATCH:
            reasons.append("schedule_consistency_mismatch")
            continue
        if (
            schedule.duration_seconds <= 0
            or schedule.end_epoch != schedule.start_epoch + schedule.duration_seconds
        ):
            reasons.append("schedule_interval_invalid")
            continue
        if _overlaps(candidate, schedule.start_epoch, schedule.end_epoch):
            overlap_count += 1
            reasons.append("schedule_interval_overlap")

    for record in owned_records:
        if record.lifecycle is OwnershipLifecycle.ORPHANED:
            continue
        if record.lifecycle is not OwnershipLifecycle.OWNED:
            reasons.append(f"owned_record_{record.lifecycle.value}")
            continue
        try:
            expected = scheduler_expected_state_from_record(record)
            start_epoch = int(expected.starttime.timestamp())
            end_epoch = start_epoch + expected.duration
        except (CommandValidationError, TypeError, ValueError, OverflowError):
            reasons.append("owned_record_interval_invalid")
            continue
        if expected.duration <= 0:
            reasons.append("owned_record_interval_invalid")
        elif _overlaps(candidate, start_epoch, end_epoch):
            overlap_count += 1
            reasons.append("owned_schedule_interval_overlap")

    unique_reasons = tuple(dict.fromkeys(reasons))
    return SchedulerCandidateConflictResult(bool(unique_reasons), unique_reasons, overlap_count)


def _overlaps(candidate: SchedulerCandidateInterval, start_epoch: int, end_epoch: int) -> bool:
    return candidate.start_epoch < end_epoch and start_epoch < candidate.end_epoch


class SchedulerValidationCommandExecutor(SchedulerCommandExecutor):
    """Validation-only executor with no generic write capability."""

    def __init__(
        self,
        command_manager: CommandManager,
        permit: SchedulerValidationPermit,
        *,
        config_entry_id: str,
        gateway_serial: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        if timeout_seconds <= 0 or not isfinite(timeout_seconds):
            raise CommandValidationError("validation transport timeout must be finite and positive")
        permit.assert_context(config_entry_id, gateway_serial)
        self._command_manager = command_manager
        self._permit = permit
        self._timeout_seconds = timeout_seconds

    async def execute_mutation(self, command: SchedulerCommand) -> SchedulerExecutionResult:
        if not command.state_changing:
            return SchedulerExecutionResult(
                SchedulerExecutionStatus.EXECUTION_ERROR,
                reason="validation_mutation_requires_state_changing_command",
            )
        result = await self._command_manager.execute_scheduler_validation_correlated(
            self._request(command),
            self._permit,
        )
        if result.ack is not None:
            if result.status is CommandStatus.SUCCESS:
                return SchedulerExecutionResult(
                    SchedulerExecutionStatus.ACK_SUCCESS,
                    ack=result.ack,
                )
            return SchedulerExecutionResult(
                SchedulerExecutionStatus.ACK_REJECTED,
                ack=result.ack,
                reason="gateway_rejected",
            )
        if result.status is CommandStatus.TIMED_OUT:
            self._permit.fail()
            return SchedulerExecutionResult(
                SchedulerExecutionStatus.TIMED_OUT,
                reason="scheduler_transport_timeout",
            )
        if result.status is CommandStatus.DELIVERY_UNKNOWN:
            self._permit.fail()
            return SchedulerExecutionResult(
                SchedulerExecutionStatus.DELIVERY_UNKNOWN,
                reason="scheduler_publish_outcome_unknown",
            )
        self._permit.fail()
        return SchedulerExecutionResult(
            SchedulerExecutionStatus.EXECUTION_ERROR,
            reason=result.error.code.value if result.error else "scheduler_transport_error",
        )

    async def execute_get(self, command: SchedulerCommand):
        if command.method is not CommandMethod.GET:
            self._permit.fail()
            raise CommandValidationError("validation GET transport requires method=get")
        result = await self._command_manager.execute_scheduler_validation_correlated(
            self._request(command),
            self._permit,
        )
        if result.ack is not None:
            return result.ack
        self._permit.fail()
        if result.status is CommandStatus.TIMED_OUT:
            raise TimeoutError("scheduler validation keyed GET timed out")
        raise RuntimeError(result.error.code.value if result.error else "scheduler_transport_error")

    def _request(self, command: SchedulerCommand) -> CorrelatedCommandRequest:
        return CorrelatedCommandRequest(
            method=command.method,
            message_id=command.m_id,
            service=(
                command.service
                or (command.expected_services[0] if len(command.expected_services) == 1 else None)
            ),
            fields=command.fields,
            expected_services=command.expected_services,
            timeout_seconds=self._timeout_seconds,
        )


class SchedulerValidationRoundtrip:
    """Coordinate one explicit Target SoC CREATE/reconcile/DELETE attempt."""

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime

    async def run(self) -> dict[str, object]:
        runtime = self.runtime
        lock: asyncio.Lock = runtime.scheduler_validation_lock
        if lock.locked():
            return self._result("blocked", "preflight", "validation_already_running")
        async with lock:
            if runtime.scheduler_validation_attempt_consumed:
                return self._result("blocked", "preflight", "validation_attempt_consumed")
            runtime.scheduler_validation_running = True
            try:
                return await self._run_locked()
            finally:
                runtime.scheduler_validation_running = False

    async def _run_locked(self) -> dict[str, object]:
        runtime = self.runtime
        manager = runtime.scheduler_manager
        ownership = runtime.scheduler_ownership
        if not runtime.release_gate.live_writes_enabled:
            return self._result("blocked", "preflight", "release_gate_closed")
        if manager is None or ownership is None or runtime.scheduler_mutation_manager is None:
            return self._result("blocked", "preflight", "scheduler_runtime_unavailable")
        ownership_blockers = ownership.validation_blockers()
        if ownership_blockers:
            return self._result("blocked", "preflight", *ownership_blockers)

        candidate = self._candidate_interval()
        if isinstance(candidate, str):
            return self._result("blocked", "preflight", candidate)
        to_epoch = candidate.end_epoch + FIRST_WRITE_POST_GUARD_SECONDS
        try:
            await manager.refresh_list(0, to_epoch)
        except Exception:
            return self._result("blocked", "list", "scheduler_list_failed")
        if not manager.list_visibility_fresh() or not manager.list_visibility_covers(0, to_epoch):
            return self._result("blocked", "list", "scheduler_list_visibility_insufficient")

        conflict = evaluate_candidate_conflicts(
            candidate,
            manager.normalized_schedules,
            ownership.records.values(),
        )
        if conflict.blocked:
            return self._result(
                "blocked",
                "conflict",
                *conflict.reasons,
                overlap_count=conflict.overlap_count,
            )

        context = runtime.scheduler_control_context(
            ControlOperation.CREATE,
            validation_authorized=True,
        )
        decision = evaluate_control_operation(context)
        if not decision.permitted:
            return self._result("blocked", "preflight", *decision.blocked_reasons)

        socref = self._select_socref(runtime.battery)
        if isinstance(socref, str):
            return self._result("blocked", "preflight", socref)

        runtime.scheduler_validation_attempt_consumed = True
        permit = SchedulerValidationPermit.issue(
            config_entry_id=ownership.config_entry_id,
            gateway_serial=ownership.gateway_serial,
            candidate_starttime=candidate.start_datetime,
            candidate_duration=candidate.end_epoch - candidate.start_epoch,
            candidate_socref=socref,
        )
        executor = SchedulerValidationCommandExecutor(
            runtime.command_manager,
            permit,
            config_entry_id=ownership.config_entry_id,
            gateway_serial=ownership.gateway_serial,
        )
        validation_manager = SchedulerMutationManager(
            allocator=runtime.command_manager.allocator,
            ownership=ownership,
            policy_provider=lambda operation: runtime.scheduler_control_context(
                operation,
                validation_authorized=permit.is_active,
            ),
            executor=executor,
            persist_ownership=runtime.persist_scheduler_ownership,
            journal=runtime,
            transaction_id_factory=lambda: f"validation-{uuid4().hex}",
        )
        create_result = await validation_manager.create(
            SchedulerCreateRequest(
                starttime=candidate.start_datetime,
                duration_seconds=candidate.end_epoch - candidate.start_epoch,
                values={"socref": socref},
            )
        )
        if create_result.status is not SchedulerTransactionStatus.SUCCESS:
            permit.fail()
            return self._transaction_result("create", create_result)
        permit.mark_create_reconciled()
        if create_result.m_id is None:
            permit.fail()
            return self._result("failed", "create", "create_m_id_missing")

        delete_result = await validation_manager.delete(create_result.m_id)
        if delete_result.status is not SchedulerTransactionStatus.SUCCESS:
            permit.fail()
            return self._transaction_result("delete", delete_result)
        permit.mark_delete_reconciled()
        permit_diagnostics = permit.diagnostics()
        postcheck = "success"
        try:
            await manager.refresh_list(0, to_epoch)
        except Exception:
            postcheck = "failed"
        return self._result(
            "success",
            "complete",
            "transaction_success",
            create_status=create_result.status.value,
            create_lifecycle=create_result.lifecycle_after.value
            if create_result.lifecycle_after
            else None,
            delete_status=delete_result.status.value,
            delete_lifecycle=delete_result.lifecycle_after.value
            if delete_result.lifecycle_after
            else None,
            postcheck_status=postcheck,
            permit=permit_diagnostics,
        )

    def _candidate_interval(self, now: datetime | None = None) -> _CandidateWithTime | str:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        minimum = current + timedelta(seconds=FIRST_WRITE_MIN_LEAD_SECONDS)
        candidate_start = minimum.replace(minute=0, second=0, microsecond=0)
        if candidate_start < minimum:
            candidate_start += timedelta(hours=1)
        return _CandidateWithTime(
            start_epoch=int(candidate_start.timestamp()),
            end_epoch=int(candidate_start.timestamp()) + FIRST_WRITE_DURATION_SECONDS,
            start_datetime=candidate_start,
        )

    @staticmethod
    def _select_socref(battery: Any) -> int | float | str:
        raw_soc = battery.raw_soc
        minimum = battery.effective_min_soc
        maximum = battery.effective_max_soc
        if (
            raw_soc is None
            or minimum is None
            or maximum is None
            or not all(isfinite(float(value)) for value in (raw_soc, minimum, maximum))
            or not 0 <= minimum < maximum <= 100
        ):
            return "target_soc_telemetry_or_bounds_invalid"
        lower = ceil(minimum)
        upper = floor(maximum)
        if lower > upper:
            return "target_soc_bounds_have_no_integer_value"
        return min(max(round(raw_soc), lower), upper)

    @staticmethod
    def _transaction_result(phase: str, result: SchedulerTransactionResult) -> dict[str, object]:
        return {
            "status": result.status.value,
            "phase": phase,
            "reason": result.reason,
            "create_status": result.status.value if phase == "create" else None,
            "create_lifecycle": result.lifecycle_after.value
            if phase == "create" and result.lifecycle_after
            else None,
            "delete_status": result.status.value if phase == "delete" else None,
            "delete_lifecycle": result.lifecycle_after.value
            if phase == "delete" and result.lifecycle_after
            else None,
        }

    @staticmethod
    def _result(status: str, phase: str, *reasons: str, **extra: object) -> dict[str, object]:
        return {
            "status": status,
            "phase": phase,
            "reason": reasons[0] if reasons else "",
            "preflight_blockers": list(reasons),
            **extra,
        }


@dataclass(frozen=True, slots=True)
class _CandidateWithTime(SchedulerCandidateInterval):
    start_datetime: datetime = field(compare=False)


__all__ = [
    "FIRST_WRITE_DURATION_SECONDS",
    "FIRST_WRITE_MIN_LEAD_SECONDS",
    "FIRST_WRITE_POST_GUARD_SECONDS",
    "SchedulerCandidateConflictResult",
    "SchedulerCandidateInterval",
    "SchedulerValidationCommandExecutor",
    "SchedulerValidationPermit",
    "SchedulerValidationRoundtrip",
    "ValidationPermitStage",
    "evaluate_candidate_conflicts",
]
