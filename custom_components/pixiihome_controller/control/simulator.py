"""Deterministic in-memory plant and scheduler simulator."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite
from typing import Any

from .adapters import (
    AdapterExecutionStatus,
    AdapterRequest,
    AdapterResult,
)
from .engine import ControlEngine, EngineResult, EngineStatus, InMemoryJournalPort
from .journal import ControlJournal, JournalPhase, JournalTargetIdentity
from .models import (
    AdapterCapability,
    ControlDecision,
    ControlOperation,
    ControlPolicyInput,
    ExternalControlState,
    Initiative,
    OperationImpact,
    OperatorMode,
    PlantHealth,
    ReleaseState,
    RuntimeMode,
    ScheduleOrigin,
)
from .policy import evaluate_control_operation

SIMULATOR_HISTORY_LIMIT = 256
SIMULATOR_SCHEDULE_LIMIT = 96


@dataclass(slots=True)
class SimulatorClock:
    now: datetime = field(default_factory=lambda: datetime(2030, 1, 1, tzinfo=UTC))

    def __post_init__(self) -> None:
        if self.now.tzinfo is None:
            raise ValueError("simulator clock requires a timezone-aware datetime")
        self.now = self.now.astimezone(UTC)

    def advance(self, seconds: int | float) -> datetime:
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("simulator clock advance must be numeric")
        if not isfinite(float(seconds)) or seconds < 0:
            raise ValueError("simulator clock advance must be finite and non-negative")
        self.now += timedelta(seconds=float(seconds))
        return self.now


@dataclass(slots=True)
class SimulatedBattery:
    asset_id: str = "battery-1"
    capacity_kwh: float = 10.0
    soc_percent: float = 50.0
    min_soc_percent: float = 10.0
    max_soc_percent: float = 90.0
    power_w: float = 0.0
    target_soc_percent: float | None = None

    def __post_init__(self) -> None:
        if not self.asset_id.strip():
            raise ValueError("simulated battery requires an asset ID")
        if self.capacity_kwh <= 0 or not isfinite(self.capacity_kwh):
            raise ValueError("simulated battery capacity must be positive")
        if not 0 <= self.min_soc_percent < self.max_soc_percent <= 100:
            raise ValueError("simulated battery SoC bounds are invalid")
        if not self.min_soc_percent <= self.soc_percent <= self.max_soc_percent:
            raise ValueError("simulated battery SoC is outside configured bounds")

    @property
    def raw_soc(self) -> float:
        return self.soc_percent

    @property
    def usable_soc(self) -> float:
        return self.soc_percent

    @property
    def stored_energy_kwh(self) -> float:
        return self.capacity_kwh * self.soc_percent / 100

    @property
    def soh(self) -> float:
        return 100.0

    @property
    def effective_min_soc(self) -> float:
        return self.min_soc_percent

    @property
    def effective_max_soc(self) -> float:
        return self.max_soc_percent

    @property
    def reported_min_soc(self) -> float:
        return self.min_soc_percent

    @property
    def reported_max_soc(self) -> float:
        return self.max_soc_percent

    @property
    def usable_soc_source(self) -> str:
        return "simulator"


@dataclass(frozen=True, slots=True)
class SimulatedSchedule:
    m_id: int
    start: datetime
    duration_seconds: int
    socref: float
    origin: ScheduleOrigin = ScheduleOrigin.LOCAL_CREATED
    sch_index: int | None = None
    source_id: int | None = None
    service_no: int = 110
    service_type: int = 107

    def __post_init__(self) -> None:
        if self.start.tzinfo is None:
            raise ValueError("simulated schedule start must be timezone-aware")
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        if isinstance(self.duration_seconds, bool) or self.duration_seconds <= 0:
            raise ValueError("simulated schedule duration must be positive")
        if not 0 <= self.socref <= 100 or not isfinite(self.socref):
            raise ValueError("simulated schedule target SoC is invalid")
        if not isinstance(self.origin, ScheduleOrigin):
            object.__setattr__(self, "origin", ScheduleOrigin(self.origin))

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration_seconds)

    def as_dict(self) -> dict[str, Any]:
        return {
            "m_id": self.m_id,
            "sch_index": self.sch_index,
            "service_no": self.service_no,
            "source_id": self.source_id,
            "service_type": self.service_type,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_seconds": self.duration_seconds,
            "socref": self.socref,
            "origin": self.origin.value,
        }



class ReplacementSagaPhase(StrEnum):
    """Durable simulator replacement steps."""

    PREPARED = "prepared"
    PREDECESSOR_DELETED = "predecessor_deleted"
    COMPENSATED = "compensated"
    PARTIAL = "partial"
    REPLACEMENT_CREATED = "replacement_created"
    RECONCILED = "reconciled"

@dataclass(slots=True)
class SimulatorAdapter:
    """State-changing adapter available only inside the isolated simulator."""

    clock: SimulatorClock = field(default_factory=SimulatorClock)
    battery: SimulatedBattery = field(default_factory=SimulatedBattery)
    schedules: dict[int, SimulatedSchedule] = field(default_factory=dict)
    active_service: int = 0
    demand_response_power_w: float = 0.0
    active_control_deadline: datetime | None = None
    faults: set[str] = field(default_factory=set)
    history: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if len(self.history) > SIMULATOR_HISTORY_LIMIT:
            raise ValueError("simulator history limit exceeded")
        if len(self.schedules) > SIMULATOR_SCHEDULE_LIMIT:
            raise ValueError("simulator schedule limit exceeded")

    def capability(
        self,
        operation: ControlOperation,
        service: str | None,
    ) -> AdapterCapability:
        if operation is ControlOperation.DIRECT_CONTROL:
            return AdapterCapability.UNSUPPORTED
        if operation is ControlOperation.DIRECT_TARGET_SOC:
            return (
                AdapterCapability.SUPPORTED
                if service == "targetsoc"
                else AdapterCapability.UNSUPPORTED
            )
        if operation is ControlOperation.DIRECT_DEMAND_RESPONSE:
            return (
                AdapterCapability.SUPPORTED
                if service == "demandresponse"
                else AdapterCapability.UNSUPPORTED
            )
        if operation is ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING:
            return (
                AdapterCapability.SUPPORTED
                if service == "adaptivepeakshaving"
                else AdapterCapability.UNSUPPORTED
            )
        if operation is ControlOperation.DIRECT_BATTERY_CALIBRATION:
            return (
                AdapterCapability.SUPPORTED
                if service == "batterycalibration"
                else AdapterCapability.UNSUPPORTED
            )
        return AdapterCapability.SUPPORTED

    async def execute(self, request: AdapterRequest) -> AdapterResult:
        handlers = {
            ControlOperation.READ_ONLY: self._read,
            ControlOperation.DIRECT_TARGET_SOC: self._direct_target_soc,
            ControlOperation.DIRECT_DEMAND_RESPONSE: self._direct_demand_response,
            ControlOperation.CREATE: self._create,
            ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING: self._direct_adaptive_peak_shaving,
            ControlOperation.DIRECT_BATTERY_CALIBRATION: self._direct_battery_calibration,
            ControlOperation.UPDATE: self._update,
            ControlOperation.DELETE: self._delete,
            ControlOperation.REPLACE: self._replace,
            ControlOperation.STOP: self._stop,
            ControlOperation.PLANNER_APPLY: self._planner_apply,
            ControlOperation.RECOVERY_APPLY: self._recovery_apply,
        }
        handler = handlers.get(request.operation)
        if handler is None:
            result = AdapterResult(
                AdapterExecutionStatus.REJECTED,
                reason="simulator_operation_unsupported",
            )
        elif "timeout" in self.faults:
            result = AdapterResult(
                AdapterExecutionStatus.TIMED_OUT,
                reason="simulator_timeout",
            )
        else:
            try:
                result = handler(request)
            except (KeyError, TypeError, ValueError) as err:
                result = AdapterResult(
                    AdapterExecutionStatus.INVALID,
                    reason=str(err),
                )
            if (
                result.status is AdapterExecutionStatus.SUCCESS
                and "delivery_unknown" in self.faults
            ):
                result = AdapterResult(
                    AdapterExecutionStatus.DELIVERY_UNKNOWN,
                    state=result.state,
                    reason="simulator_delivery_unknown",
                )
        self.history.append(
            {
                "at": self.clock.now.isoformat(),
                "operation": request.operation.value,
                "service": request.service,
                "status": result.status.value,
                "reason": result.reason,
            }
        )
        del self.history[:-SIMULATOR_HISTORY_LIMIT]
        return result
    def _direct_adaptive_peak_shaving(self, request: AdapterRequest) -> AdapterResult:
        for key in (
            "export_limit_w",
            "import_limit_w",
            "charge_max_w",
            "discharge_max_w",
            "max_limit_change_w",
            "duration_seconds",
        ):
            _number(request.payload, key)
        if not isinstance(request.payload.get("keep_active"), bool):
            raise ValueError("simulated APS keep_active must be boolean")
        self.active_control_deadline = self.clock.now + timedelta(
            seconds=_positive_int(request.payload["duration_seconds"], "duration_seconds")
        )
        self.demand_response_power_w = 0.0
        self.battery.power_w = 0.0
        self.battery.target_soc_percent = None
        self.active_service = 105
        return AdapterResult(AdapterExecutionStatus.SUCCESS, self.snapshot())

    def _direct_battery_calibration(self, request: AdapterRequest) -> AdapterResult:
        endpoint_soc = _number(request.payload, "endpoint_soc")
        if not 0 <= endpoint_soc <= 100:
            raise ValueError("simulated calibration endpoint SoC is invalid")
        _number(request.payload, "duration_seconds")
        self.active_control_deadline = self.clock.now + timedelta(
            seconds=_positive_int(request.payload["duration_seconds"], "duration_seconds")
        )
        self.battery.target_soc_percent = endpoint_soc
        self.active_service = 116
        return AdapterResult(AdapterExecutionStatus.SUCCESS, self.snapshot())

    def advance(self, seconds: int | float) -> dict[str, Any]:
        elapsed = float(seconds)
        deadline = self.active_control_deadline
        if deadline is not None:
            remaining = (deadline - self.clock.now).total_seconds()
            self._integrate_battery(max(0.0, min(elapsed, remaining)))
        self.clock.advance(seconds)
        self._refresh_active_schedule()
        self._expire_active_control()
        if deadline is None:
            self._integrate_battery(elapsed)
        return self.snapshot()

    def _integrate_battery(self, seconds: float) -> None:
        hours = seconds / 3600
        if self.battery.capacity_kwh > 0:
            delta = (self.battery.power_w / 1000) * hours / self.battery.capacity_kwh * 100
            self.battery.soc_percent = min(
                self.battery.max_soc_percent,
                max(self.battery.min_soc_percent, self.battery.soc_percent + delta),
            )

    def seed_external_schedule(self, schedule: SimulatedSchedule) -> None:
        if (
            schedule.m_id not in self.schedules
            and len(self.schedules) >= SIMULATOR_SCHEDULE_LIMIT
        ):
            raise ValueError("simulator schedule limit exceeded")
        if schedule.origin is not ScheduleOrigin.EXTERNAL_OBSERVED:
            schedule = replace(schedule, origin=ScheduleOrigin.EXTERNAL_OBSERVED)
        self.schedules[schedule.m_id] = schedule

    def inject_fault(self, fault: str, *, active: bool = True) -> None:
        if active:
            self.faults.add(fault)
        else:
            self.faults.discard(fault)

    def snapshot(self) -> dict[str, Any]:
        return {
            "clock": self.clock.now.isoformat(),
            "battery": {
                "asset_id": self.battery.asset_id,
                "capacity_kwh": self.battery.capacity_kwh,
                "soc_percent": self.battery.soc_percent,
                "min_soc_percent": self.battery.min_soc_percent,
                "max_soc_percent": self.battery.max_soc_percent,
                "power_w": self.battery.power_w,
                "target_soc_percent": self.battery.target_soc_percent,
            },
            "active_service": self.active_service,
            "demand_response_power_w": self.demand_response_power_w,
            "active_control_deadline": (
                self.active_control_deadline.isoformat()
                if self.active_control_deadline is not None
                else None
            ),
            "schedules": [
                self.schedules[m_id].as_dict() for m_id in sorted(self.schedules)
            ],
            "faults": sorted(self.faults),
            "history_count": len(self.history),
        }

    def _read(self, request: AdapterRequest) -> AdapterResult:
        del request
        return AdapterResult(AdapterExecutionStatus.SUCCESS, self.snapshot())

    def _direct_target_soc(self, request: AdapterRequest) -> AdapterResult:
        socref = _number(request.payload, "socref")
        if not self.battery.min_soc_percent <= socref <= self.battery.max_soc_percent:
            raise ValueError("target SoC is outside simulated battery bounds")
        duration = request.payload.get("duration_seconds")
        self.active_control_deadline = (
            None
            if duration is None
            else self.clock.now
            + timedelta(seconds=_positive_int(duration, "duration_seconds"))
        )
        self.demand_response_power_w = 0.0
        self.battery.power_w = 0.0
        self.battery.target_soc_percent = socref
        self.active_service = 107
        return AdapterResult(AdapterExecutionStatus.SUCCESS, self.snapshot())

    def _direct_demand_response(self, request: AdapterRequest) -> AdapterResult:
        power_w = _number(request.payload, "power_w")
        duration = request.payload.get("duration_seconds")
        self.active_control_deadline = (
            None
            if duration is None
            else self.clock.now
            + timedelta(seconds=_positive_int(duration, "duration_seconds"))
        )
        self.demand_response_power_w = power_w
        self.battery.power_w = power_w
        self.battery.target_soc_percent = None
        self.active_service = 102
        return AdapterResult(AdapterExecutionStatus.SUCCESS, self.snapshot())

    def _create(self, request: AdapterRequest) -> AdapterResult:
        schedule = self._schedule_from_request(request)
        if schedule.m_id in self.schedules:
            return AdapterResult(
                AdapterExecutionStatus.REJECTED,
                reason="schedule_identity_exists",
            )
        if len(self.schedules) >= SIMULATOR_SCHEDULE_LIMIT:
            return AdapterResult(
                AdapterExecutionStatus.REJECTED,
                reason="simulator_schedule_limit_exceeded",
            )
        self.schedules[schedule.m_id] = schedule
        return AdapterResult(AdapterExecutionStatus.SUCCESS, schedule.as_dict())

    def _update(self, request: AdapterRequest) -> AdapterResult:
        m_id = _request_m_id(request)
        current = self._matching_schedule(request)
        if current is None:
            return AdapterResult(AdapterExecutionStatus.NOT_FOUND, reason="schedule_not_found")
        updated = self.updated_schedule(request, current)
        self.schedules[m_id] = updated
        return AdapterResult(AdapterExecutionStatus.SUCCESS, updated.as_dict())

    @staticmethod
    def updated_schedule(
        request: AdapterRequest,
        current: SimulatedSchedule,
    ) -> SimulatedSchedule:
        updates: dict[str, Any] = {}
        if "start" in request.payload:
            updates["start"] = _datetime(request.payload["start"])
        if "duration_seconds" in request.payload:
            updates["duration_seconds"] = _positive_int(
                request.payload["duration_seconds"], "duration_seconds"
            )
        if "socref" in request.payload:
            updates["socref"] = _number(request.payload, "socref")
        return replace(current, **updates)
    def _delete(self, request: AdapterRequest) -> AdapterResult:
        m_id = _request_m_id(request)
        removed = self._matching_schedule(request)
        if removed is not None:
            self.schedules.pop(m_id)
        if removed is None:
            return AdapterResult(AdapterExecutionStatus.NOT_FOUND, reason="schedule_not_found")
        self._refresh_active_schedule()
        return AdapterResult(AdapterExecutionStatus.SUCCESS, removed.as_dict())
    def _replace(self, request: AdapterRequest) -> AdapterResult:
        m_id = _request_m_id(request)
        current = self._matching_schedule(request)
        if current is None:
            return AdapterResult(AdapterExecutionStatus.NOT_FOUND, reason="schedule_not_found")
        schedule = self._schedule_from_request(request, current=current)
        self.schedules[m_id] = schedule
        return AdapterResult(AdapterExecutionStatus.SUCCESS, schedule.as_dict())

    def _stop(self, request: AdapterRequest) -> AdapterResult:
        del request
        self.active_control_deadline = None
        self.active_service = 0
        self.demand_response_power_w = 0.0
        self.battery.power_w = 0.0
        self.battery.target_soc_percent = None
        return AdapterResult(AdapterExecutionStatus.SUCCESS, self.snapshot())

    def planner_candidate(
        self,
        request: AdapterRequest,
    ) -> tuple[
        dict[int, SimulatedSchedule],
        dict[int, SimulatedSchedule],
        str | None,
    ]:
        """Return canonical planned/protected schedules and any deterministic rejection."""
        raw_schedules = request.payload.get("schedules")
        if not isinstance(raw_schedules, list):
            raise ValueError("planner apply requires a schedules list")
        if len(raw_schedules) > SIMULATOR_SCHEDULE_LIMIT:
            raise ValueError("planner schedule limit exceeded")
        planned: dict[int, SimulatedSchedule] = {}
        for item in raw_schedules:
            if not isinstance(item, Mapping):
                raise ValueError("planned schedule must be an object")
            identity = JournalTargetIdentity(
                m_id=item.get("m_id"),
                sch_index=item.get("sch_index"),
                source_id=item.get("source_id"),
                asset_id=self.battery.asset_id,
            )
            planned_request = AdapterRequest(
                ControlOperation.CREATE,
                service="targetsoc",
                target_identity=identity,
                payload=item,
            )
            schedule = self._schedule_from_request(planned_request)
            if schedule.m_id in planned:
                raise ValueError("planned schedule identities must be unique")
            planned[schedule.m_id] = schedule
        external = {
            m_id: schedule
            for m_id, schedule in self.schedules.items()
            if schedule.origin is not ScheduleOrigin.LOCAL_CREATED
        }
        if external.keys() & planned.keys():
            return planned, external, "planner_cannot_replace_external_schedule"
        if any(
            planned_schedule.start < external_schedule.end
            and external_schedule.start < planned_schedule.end
            for planned_schedule in planned.values()
            for external_schedule in external.values()
        ):
            return planned, external, "planner_external_schedule_conflict"
        if len(external) + len(planned) > SIMULATOR_SCHEDULE_LIMIT:
            return planned, external, "simulator_schedule_limit_exceeded"
        return planned, external, None

    def _planner_apply(self, request: AdapterRequest) -> AdapterResult:
        planned, external, rejection = self.planner_candidate(request)
        if rejection is not None:
            return AdapterResult(AdapterExecutionStatus.REJECTED, reason=rejection)
        self.schedules = {**external, **planned}
        self._refresh_active_schedule()
        return AdapterResult(AdapterExecutionStatus.SUCCESS, self.snapshot())

    def _recovery_apply(self, request: AdapterRequest) -> AdapterResult:
        schedule = self._matching_schedule(request)
        if schedule is None:
            return AdapterResult(AdapterExecutionStatus.NOT_FOUND, reason="schedule_not_found")
        return AdapterResult(AdapterExecutionStatus.SUCCESS, schedule.as_dict())

    def _matching_schedule(self, request: AdapterRequest) -> SimulatedSchedule | None:
        identity = request.target_identity
        m_id = _request_m_id(request)
        schedule = self.schedules.get(m_id)
        if schedule is None:
            return None
        assert identity is not None
        expected = {
            "sch_index": schedule.sch_index,
            "service_no": schedule.service_no,
            "source_id": schedule.source_id,
            "asset_id": self.battery.asset_id,
        }
        for field_name, observed in expected.items():
            supplied = getattr(identity, field_name)
            if supplied is not None and supplied != observed:
                raise ValueError(f"schedule target {field_name} does not match")
        return schedule

    def _schedule_from_request(
        self,
        request: AdapterRequest,
        *,
        current: SimulatedSchedule | None = None,
    ) -> SimulatedSchedule:
        m_id = _request_m_id(request)
        requested_origin = request.payload.get("origin")
        if (
            current is not None
            and requested_origin is not None
            and ScheduleOrigin(requested_origin) is not current.origin
        ):
            raise ValueError("schedule replacement cannot change provenance")
        identity = request.target_identity
        return SimulatedSchedule(
            m_id=m_id,
            start=_datetime(request.payload["start"]),
            duration_seconds=_positive_int(
                request.payload["duration_seconds"], "duration_seconds"
            ),
            socref=_number(request.payload, "socref"),
            origin=current.origin
            if current is not None
            else ScheduleOrigin(requested_origin or ScheduleOrigin.LOCAL_CREATED),
            sch_index=(
                current.sch_index
                if current is not None
                else identity.sch_index if identity is not None else None
            ),
            source_id=(
                current.source_id
                if current is not None
                else identity.source_id if identity is not None else None
            ),
            service_no=current.service_no if current is not None else 110,
            service_type=current.service_type if current is not None else 107,
        )

    def _refresh_active_schedule(self) -> None:
        active = [
            schedule
            for schedule in self.schedules.values()
            if schedule.start <= self.clock.now < schedule.end
        ]
        if not active:
            if self.active_service == 107 and self.active_control_deadline is None:
                self.active_service = 0
                self.battery.target_soc_percent = None
            return
        self.active_control_deadline = None
        self.demand_response_power_w = 0.0
        self.battery.power_w = 0.0
        schedule = min(active, key=lambda item: item.m_id)
        self.active_service = schedule.service_type
        self.battery.target_soc_percent = schedule.socref

    def _expire_active_control(self) -> None:
        if (
            self.active_control_deadline is None
            or self.clock.now < self.active_control_deadline
        ):
            return
        self.active_control_deadline = None
        self.active_service = 0
        self.demand_response_power_w = 0.0
        self.battery.power_w = 0.0
        self.battery.target_soc_percent = None


@dataclass(slots=True)
class SimulatorRuntime:
    """End-to-end simulator using canonical policy, journal, and adapter boundaries."""

    adapter: SimulatorAdapter = field(default_factory=SimulatorAdapter)
    journal: InMemoryJournalPort = field(
        default_factory=lambda: InMemoryJournalPort(
            ControlJournal.empty(
                config_entry_id="simulator",
                gateway_serial="SIMULATOR",
            )
        )
    )
    _transaction_counter: int = 0
    engine: ControlEngine = field(init=False)

    def __post_init__(self) -> None:
        if not self.journal.journal.controller_state or not self.journal.journal.planner_state:
            self.journal.journal = self.journal.journal.with_product_state(
                controller_state={
                    "runtime_mode": RuntimeMode.SIMULATOR.value,
                    "operator_mode": OperatorMode.LOCAL_CONTROL.value,
                    "release_state": ReleaseState.PREVIEW_ONLY.value,
                },
                planner_state={
                    "interval_seconds": 900,
                    "maximum_actions": 96,
                    "minimum_confidence": 0.5,
                },
            )
        self.engine = ControlEngine(
            self.adapter,
            self.journal,
            self._next_transaction_id,
        )

    async def execute(
        self,
        request: AdapterRequest,
        *,
        initiative: Initiative = Initiative.MANUAL,
        grant_authorized: bool = False,
        automation_authorized: bool = False,
    ) -> EngineResult:
        if request.operation is ControlOperation.RECOVERY_APPLY:
            result = await self._recover(
                request,
                initiative=initiative,
                grant_authorized=grant_authorized,
                automation_authorized=automation_authorized,
            )
        elif request.operation is ControlOperation.REPLACE:
            result = await self._replace_saga(
                request,
                initiative=initiative,
                grant_authorized=grant_authorized,
                automation_authorized=automation_authorized,
            )
        else:
            result = await self.engine.execute(
                request,
                self.control_context(
                    request,
                    initiative=initiative,
                    grant_authorized=grant_authorized,
                    automation_authorized=automation_authorized,
                ),
            )
        self._record_audit(request, result)
        return result

    async def _replace_saga(
        self,
        request: AdapterRequest,
        *,
        initiative: Initiative,
        grant_authorized: bool,
        automation_authorized: bool,
    ) -> EngineResult:
        replacement_phase = self.journal.journal.replacement_state.get("phase")
        replacement_pending = (
            replacement_phase
            in {
                ReplacementSagaPhase.PREPARED.value,
                ReplacementSagaPhase.PREDECESSOR_DELETED.value,
                ReplacementSagaPhase.PARTIAL.value,
                ReplacementSagaPhase.REPLACEMENT_CREATED.value,
            }
            or any(
                transaction.operation == ControlOperation.REPLACE.value
                for transaction in self.journal.journal.unresolved_transactions
            )
        )
        base_context = self.control_context(
            request,
            initiative=initiative,
            grant_authorized=grant_authorized,
            automation_authorized=automation_authorized,
        )
        context = replace(
            base_context,
            adapter_capability=self.adapter.capability(request.operation, request.service),
            pending_operations=base_context.pending_operations or replacement_pending,
        )
        decision = evaluate_control_operation(context)
        if not decision.permitted:
            return EngineResult(EngineStatus.BLOCKED, decision, reason="policy_blocked")
        identity = request.target_identity
        assert identity is not None
        try:
            predecessor = self.adapter._matching_schedule(request)
            if predecessor is None:
                return EngineResult(
                    EngineStatus.FAILED,
                    decision,
                    adapter_result=AdapterResult(
                        AdapterExecutionStatus.NOT_FOUND,
                        reason="schedule_not_found",
                    ),
                    reason="schedule_not_found",
                )
            replacement_schedule = self.adapter._schedule_from_request(
                request,
                current=predecessor,
            )
        except (KeyError, TypeError, ValueError) as err:
            return EngineResult(
                EngineStatus.FAILED,
                decision,
                adapter_result=AdapterResult(
                    AdapterExecutionStatus.INVALID,
                    reason=str(err),
                ),
                reason=str(err),
            )

        transaction_id = self._next_transaction_id()
        intent = {
            "predecessor": predecessor.as_dict(),
            "replacement": replacement_schedule.as_dict(),
        }
        await self.journal.begin_control_transaction(
            transaction_id=transaction_id,
            operation=ControlOperation.REPLACE.value,
            target_identity=identity,
            intent=intent,
        )
        await self.journal.transition_control_transaction(
            transaction_id,
            JournalPhase.COMMAND_SENT,
        )
        saga = {
            "transaction_id": transaction_id,
            "phase": ReplacementSagaPhase.PREPARED.value,
            **intent,
        }
        self._set_replacement_state(saga, "replacement_prepared")

        if "timeout" in self.adapter.faults:
            await self.journal.transition_control_transaction(
                transaction_id,
                JournalPhase.RECOVERY_REQUIRED,
                reason="simulator_timeout",
            )
            return EngineResult(
                EngineStatus.RECOVERY_REQUIRED,
                decision,
                adapter_result=AdapterResult(
                    AdapterExecutionStatus.TIMED_OUT,
                    reason="simulator_timeout",
                ),
                transaction_id=transaction_id,
                reason="simulator_timeout",
            )

        self.adapter.schedules.pop(predecessor.m_id)
        self.adapter._refresh_active_schedule()
        saga = {**saga, "phase": ReplacementSagaPhase.PREDECESSOR_DELETED.value}
        self._set_replacement_state(saga, "replacement_predecessor_deleted")

        if (
            "replace_pause_after_delete" in self.adapter.faults
            or "delivery_unknown" in self.adapter.faults
        ):
            self.adapter.faults.discard("replace_pause_after_delete")
            saga = {**saga, "phase": ReplacementSagaPhase.PARTIAL.value}
            self._set_replacement_state(saga, "replacement_partial")
            await self.journal.transition_control_transaction(
                transaction_id,
                JournalPhase.RECOVERY_REQUIRED,
                reason="replacement_predecessor_deleted",
            )
            return EngineResult(
                EngineStatus.PARTIAL,
                decision,
                adapter_result=AdapterResult(
                    AdapterExecutionStatus.DELIVERY_UNKNOWN,
                    state={"predecessor_deleted": True},
                    reason="replacement_predecessor_deleted",
                ),
                transaction_id=transaction_id,
                reason="replacement_predecessor_deleted",
            )

        if "replace_create_failure" in self.adapter.faults:
            if "replace_compensation_failure" in self.adapter.faults:
                saga = {**saga, "phase": ReplacementSagaPhase.PARTIAL.value}
                self._set_replacement_state(saga, "replacement_compensation_failed")
                await self.journal.transition_control_transaction(
                    transaction_id,
                    JournalPhase.RECOVERY_REQUIRED,
                    reason="replacement_compensation_failed",
                )
                return EngineResult(
                    EngineStatus.PARTIAL,
                    decision,
                    adapter_result=AdapterResult(
                        AdapterExecutionStatus.REJECTED,
                        state={"predecessor_deleted": True},
                        reason="replacement_compensation_failed",
                    ),
                    transaction_id=transaction_id,
                    reason="replacement_compensation_failed",
                )
            self.adapter.schedules[predecessor.m_id] = predecessor
            saga = {**saga, "phase": ReplacementSagaPhase.COMPENSATED.value}
            self._set_replacement_state(saga, "replacement_compensated")
            await self.journal.transition_control_transaction(
                transaction_id,
                JournalPhase.FAILED,
                reason="replacement_create_failed_compensated",
            )
            return EngineResult(
                EngineStatus.FAILED,
                decision,
                adapter_result=AdapterResult(
                    AdapterExecutionStatus.REJECTED,
                    state=predecessor.as_dict(),
                    reason="replacement_create_failed_compensated",
                ),
                transaction_id=transaction_id,
                reason="replacement_create_failed_compensated",
            )

        self.adapter.schedules[replacement_schedule.m_id] = replacement_schedule
        saga = {**saga, "phase": ReplacementSagaPhase.REPLACEMENT_CREATED.value}
        self._set_replacement_state(saga, "replacement_created")
        await self.journal.transition_control_transaction(
            transaction_id,
            JournalPhase.ACKNOWLEDGED,
        )
        await self.journal.transition_control_transaction(
            transaction_id,
            JournalPhase.RECONCILED,
        )
        saga = {**saga, "phase": ReplacementSagaPhase.RECONCILED.value}
        self._set_replacement_state(saga, "replacement_reconciled")
        return EngineResult(
            EngineStatus.SUCCESS,
            decision,
            adapter_result=AdapterResult(
                AdapterExecutionStatus.SUCCESS,
                state=replacement_schedule.as_dict(),
            ),
            transaction_id=transaction_id,
        )

    def _set_replacement_state(
        self,
        state: Mapping[str, Any],
        event: str,
    ) -> None:
        self.journal.journal = self.journal.journal.with_product_state(
            replacement_state=state,
            audit_event={
                "at": self.adapter.clock.now.isoformat(),
                "event": event,
                "transaction_id": state.get("transaction_id"),
                "phase": state.get("phase"),
            },
        )

    def _record_audit(self, request: AdapterRequest, result: EngineResult) -> None:
        self.journal.journal = self.journal.journal.with_product_state(
            audit_event={
                "at": self.adapter.clock.now.isoformat(),
                "event": "operation_result",
                "operation": request.operation.value,
                "status": result.status.value,
                "transaction_id": result.transaction_id,
                "reason": result.reason,
            }
        )

    def recovery_evidence(
        self,
        request: AdapterRequest,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Derive a recovery outcome from current state without mutation."""
        transaction_id = request.payload.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            return None, "recovery_transaction_required"
        transaction = self.journal.journal.transaction(transaction_id)
        if transaction is None or transaction.phase is not JournalPhase.RECOVERY_REQUIRED:
            return None, "recovery_transaction_not_pending"
        if request.target_identity != transaction.target_identity:
            return None, "recovery_target_mismatch"
        if transaction.operation == ControlOperation.REPLACE.value:
            saga = self.journal.journal.replacement_state
            if saga.get("transaction_id") != transaction_id:
                return None, "replacement_saga_missing"
            predecessor = saga.get("predecessor")
            replacement = saga.get("replacement")
            if not isinstance(predecessor, Mapping) or not isinstance(
                replacement,
                Mapping,
            ):
                return None, "replacement_saga_invalid"
            m_id = transaction.target_identity.m_id
            if m_id is None:
                return None, "recovery_identity_mismatch"
            current = self.adapter.schedules.get(m_id)
            if current is not None and current.as_dict() == dict(replacement):
                return current.as_dict(), None
            if current is not None and current.as_dict() != dict(predecessor):
                return None, "replacement_recovery_state_mismatch"
            if any(
                fault in self.adapter.faults
                for fault in (
                    "timeout",
                    "delivery_unknown",
                    "replace_create_failure",
                    "replace_compensation_failure",
                    "replace_pause_after_delete",
                )
            ):
                return None, "replacement_recovery_fault_active"
            if current is None and len(self.adapter.schedules) >= SIMULATOR_SCHEDULE_LIMIT:
                return None, "simulator_schedule_limit_exceeded"
            try:
                predicted = self.adapter._schedule_from_request(
                    AdapterRequest(
                        ControlOperation.CREATE,
                        service="targetsoc",
                        target_identity=transaction.target_identity,
                        payload=dict(replacement),
                    )
                )
            except (KeyError, TypeError, ValueError):
                return None, "replacement_saga_invalid"
            if predicted.as_dict() != dict(replacement):
                return None, "replacement_saga_invalid"
            return predicted.as_dict(), None
        if transaction.operation not in {
            ControlOperation.CREATE.value,
            ControlOperation.UPDATE.value,
        }:
            return None, "recovery_operation_unsupported"
        if not transaction.intent:
            return None, "recovery_intent_missing"
        if transaction.operation == ControlOperation.CREATE.value and not {
            "start",
            "duration_seconds",
            "socref",
        }.issubset(transaction.intent):
            return None, "recovery_intent_incomplete"
        m_id = transaction.target_identity.m_id
        if m_id is None:
            return None, "recovery_identity_mismatch"
        schedule = self.adapter.schedules.get(m_id)
        if schedule is None:
            return None, "schedule_not_found"
        observed_state = schedule.as_dict()
        observed_identity = {
            **observed_state,
            "asset_id": self.adapter.battery.asset_id,
        }
        for field_name in ("m_id", "sch_index", "service_no", "source_id", "asset_id"):
            expected = getattr(transaction.target_identity, field_name)
            if expected is not None and observed_identity.get(field_name) != expected:
                return None, "recovery_identity_mismatch"
        intent_fields = {"start", "duration_seconds", "socref", "origin"}
        if any(
            observed_state.get(field_name) != expected
            for field_name, expected in transaction.intent.items()
            if field_name in intent_fields
        ):
            return None, "recovery_state_mismatch"
        return observed_state, None

    async def _recover_replacement(
        self,
        transaction_id: str,
        decision: ControlDecision,
    ) -> EngineResult:
        saga = dict(self.journal.journal.replacement_state)
        if saga.get("transaction_id") != transaction_id:
            return EngineResult(
                EngineStatus.RECOVERY_REQUIRED,
                decision,
                transaction_id=transaction_id,
                reason="replacement_saga_missing",
            )
        raw_predecessor = saga.get("predecessor")
        raw_replacement = saga.get("replacement")
        if not isinstance(raw_predecessor, Mapping) or not isinstance(
            raw_replacement,
            Mapping,
        ):
            return EngineResult(
                EngineStatus.RECOVERY_REQUIRED,
                decision,
                transaction_id=transaction_id,
                reason="replacement_saga_invalid",
            )
        transaction = self.journal.journal.transaction(transaction_id)
        assert transaction is not None
        m_id = transaction.target_identity.m_id
        assert m_id is not None
        current = self.adapter.schedules.get(m_id)
        if current is not None and current.as_dict() == dict(raw_replacement):
            result = AdapterResult(
                AdapterExecutionStatus.SUCCESS,
                state=current.as_dict(),
            )
        else:
            if current is not None:
                if current.as_dict() != dict(raw_predecessor):
                    return EngineResult(
                        EngineStatus.RECOVERY_REQUIRED,
                        decision,
                        transaction_id=transaction_id,
                        reason="replacement_recovery_state_mismatch",
                    )
                self.adapter.schedules.pop(m_id)
                saga = {
                    **saga,
                    "phase": ReplacementSagaPhase.PREDECESSOR_DELETED.value,
                }
                self._set_replacement_state(
                    saga,
                    "replacement_recovery_predecessor_deleted",
                )
            if any(
                fault in self.adapter.faults
                for fault in (
                    "timeout",
                    "delivery_unknown",
                    "replace_create_failure",
                    "replace_compensation_failure",
                    "replace_pause_after_delete",
                )
            ):
                saga = {**saga, "phase": ReplacementSagaPhase.PARTIAL.value}
                self._set_replacement_state(saga, "replacement_recovery_fault_active")
                return EngineResult(
                    EngineStatus.PARTIAL,
                    decision,
                    transaction_id=transaction_id,
                    reason="replacement_recovery_fault_active",
                )
            create_request = AdapterRequest(
                ControlOperation.CREATE,
                service="targetsoc",
                target_identity=transaction.target_identity,
                payload=dict(raw_replacement),
            )
            result = await self.adapter.execute(create_request)
            if result.status is not AdapterExecutionStatus.SUCCESS:
                saga = {**saga, "phase": ReplacementSagaPhase.PARTIAL.value}
                self._set_replacement_state(saga, "replacement_recovery_create_failed")
                return EngineResult(
                    EngineStatus.PARTIAL,
                    decision,
                    adapter_result=result,
                    transaction_id=transaction_id,
                    reason=result.reason or "replacement_recovery_create_failed",
                )
        await self.journal.transition_control_transaction(
            transaction_id,
            JournalPhase.RECONCILED,
            reason="replacement_recovery_completed",
        )
        saga = {**saga, "phase": ReplacementSagaPhase.RECONCILED.value}
        self._set_replacement_state(saga, "replacement_recovery_reconciled")
        return EngineResult(
            EngineStatus.SUCCESS,
            decision,
            adapter_result=result,
            transaction_id=transaction_id,
        )

    async def _recover(
        self,
        request: AdapterRequest,
        *,
        initiative: Initiative,
        grant_authorized: bool,
        automation_authorized: bool,
    ) -> EngineResult:
        transaction_id = request.payload.get("transaction_id")
        if not isinstance(transaction_id, str) or not transaction_id.strip():
            return EngineResult(
                EngineStatus.BLOCKED,
                ControlDecision(False, ("recovery_transaction_required",)),
                reason="recovery_transaction_required",
            )
        transaction = next(
            (
                item
                for item in self.journal.journal.transactions
                if item.transaction_id == transaction_id
            ),
            None,
        )
        if transaction is None or transaction.phase is not JournalPhase.RECOVERY_REQUIRED:
            return EngineResult(
                EngineStatus.BLOCKED,
                ControlDecision(False, ("recovery_transaction_not_pending",)),
                transaction_id=transaction_id,
                reason="recovery_transaction_not_pending",
            )
        if request.target_identity != transaction.target_identity:
            return EngineResult(
                EngineStatus.BLOCKED,
                ControlDecision(False, ("recovery_target_mismatch",)),
                transaction_id=transaction_id,
                reason="recovery_target_mismatch",
            )

        context = replace(
            self.control_context(
                request,
                initiative=initiative,
                grant_authorized=grant_authorized,
                automation_authorized=automation_authorized,
            ),
            adapter_capability=self.adapter.capability(
                request.operation,
                request.service,
            ),
        )
        decision = evaluate_control_operation(context)
        if not decision.permitted:
            return EngineResult(
                EngineStatus.BLOCKED,
                decision,
                transaction_id=transaction_id,
                reason="policy_blocked",
            )
        _, recovery_reason = self.recovery_evidence(request)
        if recovery_reason is not None:
            return EngineResult(
                EngineStatus.RECOVERY_REQUIRED,
                decision,
                transaction_id=transaction_id,
                reason=recovery_reason,
            )
        if transaction.operation == ControlOperation.REPLACE.value:
            return await self._recover_replacement(transaction_id, decision)
        if transaction.operation not in {"create", "update", "replace"}:
            return EngineResult(
                EngineStatus.RECOVERY_REQUIRED,
                decision,
                transaction_id=transaction_id,
                reason="recovery_operation_unsupported",
            )
        if not transaction.intent:
            return EngineResult(
                EngineStatus.RECOVERY_REQUIRED,
                decision,
                transaction_id=transaction_id,
                reason="recovery_intent_missing",
            )
        result = await self.adapter.execute(request)
        if result.status is not AdapterExecutionStatus.SUCCESS:
            reason = (
                "recovery_identity_mismatch"
                if result.reason is not None
                and result.reason.startswith("schedule target ")
                else result.reason
            )
            return EngineResult(
                EngineStatus.RECOVERY_REQUIRED,
                decision,
                adapter_result=result,
                transaction_id=transaction_id,
                reason=reason,
            )
        observed_state = dict(result.state)
        observed_identity = {
            **observed_state,
            "asset_id": self.adapter.battery.asset_id,
        }
        for field_name in ("m_id", "sch_index", "service_no", "source_id", "asset_id"):
            expected = getattr(transaction.target_identity, field_name)
            if expected is not None and observed_identity.get(field_name) != expected:
                return EngineResult(
                    EngineStatus.RECOVERY_REQUIRED,
                    decision,
                    adapter_result=result,
                    transaction_id=transaction_id,
                    reason="recovery_identity_mismatch",
                )
        intent_fields = {"start", "duration_seconds", "socref", "origin"}
        if transaction.operation == "create" and not {
            "start",
            "duration_seconds",
            "socref",
        }.issubset(transaction.intent):
            return EngineResult(
                EngineStatus.RECOVERY_REQUIRED,
                decision,
                adapter_result=result,
                transaction_id=transaction_id,
                reason="recovery_intent_incomplete",
            )
        if any(
            observed_state.get(field_name) != expected
            for field_name, expected in transaction.intent.items()
            if field_name in intent_fields
        ):
            return EngineResult(
                EngineStatus.RECOVERY_REQUIRED,
                decision,
                adapter_result=result,
                transaction_id=transaction_id,
                reason="recovery_state_mismatch",
            )
        await self.journal.transition_control_transaction(
            transaction_id,
            JournalPhase.RECONCILED,
            reason="simulator_recovery_evidence_matched",
        )
        return EngineResult(
            EngineStatus.SUCCESS,
            decision,
            adapter_result=result,
            transaction_id=transaction_id,
        )

    def control_context(
        self,
        request: AdapterRequest,
        *,
        initiative: Initiative,
        grant_authorized: bool,
        automation_authorized: bool,
    ) -> ControlPolicyInput:
        faults = self.adapter.faults
        origin = self._target_origin(request)
        health_reasons = tuple(
            reason
            for reason, active in (
                ("simulator_telemetry_stale", "telemetry_stale" in faults),
                ("simulator_system_unsafe", "system_unsafe" in faults),
                ("simulator_battery_unsafe", "battery_unsafe" in faults),
                ("simulator_grid_unsafe", "grid_unsafe" in faults),
                ("simulator_system_events", "system_events" in faults),
            )
            if active
        )
        active_service = self.adapter.active_service
        return ControlPolicyInput(
            operation=request.operation,
            requested_service=request.service,
            runtime_mode=RuntimeMode.SIMULATOR,
            operator_mode=OperatorMode.LOCAL_CONTROL,
            external_control=ExternalControlState.NONE,
            origin=origin,
            initiative=initiative,
            release_state=ReleaseState.PREVIEW_ONLY,
            plant_health=PlantHealth(
                telemetry_fresh="telemetry_stale" not in faults,
                system_safe="system_unsafe" not in faults,
                battery_safe="battery_unsafe" not in faults,
                grid_safe="grid_unsafe" not in faults,
                system_events_clear="system_events" not in faults,
                reasons=health_reasons,
                system_status=1,
                battery_status=1,
                grid_status=1,
                system_events=0,
            ),
            operation_impact=OperationImpact(
                active_service=active_service,
                protected_service_active=active_service not in {0, 102, 107},
                immediate_effect_known=active_service in {0, 102, 107},
                exact_target=request.target_identity is not None,
            ),
            scheduler_visibility_fresh="scheduler_visibility_stale" not in faults,
            ownership_store_valid="ownership_invalid" not in faults,
            ownership_ambiguous="ownership_ambiguous" in faults,
            mqtt_connected="mqtt_disconnected" not in faults,
            grant_authorized=grant_authorized,
            automation_authorized=automation_authorized,
        )

    def advance(self, seconds: int | float) -> dict[str, Any]:
        return self.adapter.advance(seconds)

    def snapshot(self) -> dict[str, Any]:
        state = self.adapter.snapshot()
        journal = self.journal.journal
        state["journal"] = {
            "revision": journal.revision,
            "controller_state": dict(journal.controller_state),
            "planner_state": dict(journal.planner_state),
            "replacement_state": dict(journal.replacement_state),
            "audit_history": [dict(item) for item in journal.audit_history],
            "transactions": [
                transaction.as_dict() for transaction in journal.transactions
            ],
            "unresolved_count": len(journal.unresolved_transactions),
        }
        return state

    def state_fingerprint(self) -> str:
        """Return a stable hash that invalidates previews after any state change."""
        payload = json.dumps(
            self.snapshot(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def export_state(self) -> dict[str, Any]:
        """Return complete JSON-safe simulator state for config-entry persistence."""
        return {
            "schema_version": 1,
            "transaction_counter": self._transaction_counter,
            "adapter": {
                **self.adapter.snapshot(),
                "history": [dict(item) for item in self.adapter.history],
            },
            "journal": self.journal.journal.snapshot(),
        }

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, Any],
        *,
        config_entry_id: str,
        gateway_serial: str,
    ) -> SimulatorRuntime:
        """Restore a simulator entry without contacting MQTT or external services."""
        if not isinstance(state, Mapping) or state.get("schema_version") != 1:
            raise ValueError("simulator state schema is invalid")
        raw_adapter = state.get("adapter")
        raw_journal = state.get("journal")
        if not isinstance(raw_adapter, Mapping) or not isinstance(raw_journal, Mapping):
            raise ValueError("simulator state is incomplete")
        raw_battery = raw_adapter.get("battery")
        raw_schedules = raw_adapter.get("schedules", [])
        raw_history = raw_adapter.get("history", [])
        raw_faults = raw_adapter.get("faults", [])
        if (
            not isinstance(raw_battery, Mapping)
            or not isinstance(raw_schedules, list)
            or not isinstance(raw_history, list)
            or not isinstance(raw_faults, list)
        ):
            raise ValueError("simulator adapter state is invalid")
        if len(raw_schedules) > SIMULATOR_SCHEDULE_LIMIT:
            raise ValueError("simulator schedule limit exceeded")
        if len(raw_history) > SIMULATOR_HISTORY_LIMIT:
            raise ValueError("simulator history limit exceeded")
        adapter = SimulatorAdapter(
            clock=SimulatorClock(_datetime(raw_adapter["clock"])),
            battery=SimulatedBattery(
                asset_id=raw_battery["asset_id"],
                capacity_kwh=float(raw_battery["capacity_kwh"]),
                soc_percent=float(raw_battery["soc_percent"]),
                min_soc_percent=float(raw_battery["min_soc_percent"]),
                max_soc_percent=float(raw_battery["max_soc_percent"]),
                power_w=float(raw_battery["power_w"]),
                target_soc_percent=(
                    None
                    if raw_battery.get("target_soc_percent") is None
                    else float(raw_battery["target_soc_percent"])
                ),
            ),
            active_service=int(raw_adapter["active_service"]),
            demand_response_power_w=float(raw_adapter["demand_response_power_w"]),
            active_control_deadline=(
                None
                if raw_adapter.get("active_control_deadline") is None
                else _datetime(raw_adapter["active_control_deadline"])
            ),
            faults={str(item) for item in raw_faults},
            history=[dict(item) for item in raw_history if isinstance(item, Mapping)],
        )
        for raw_schedule in raw_schedules:
            if not isinstance(raw_schedule, Mapping):
                raise ValueError("simulator schedule state is invalid")
            schedule = SimulatedSchedule(
                m_id=raw_schedule["m_id"],
                start=_datetime(raw_schedule["start"]),
                duration_seconds=raw_schedule["duration_seconds"],
                socref=float(raw_schedule["socref"]),
                origin=ScheduleOrigin(raw_schedule["origin"]),
                sch_index=raw_schedule.get("sch_index"),
                source_id=raw_schedule.get("source_id"),
                service_no=raw_schedule.get("service_no", 110),
                service_type=raw_schedule.get("service_type", 107),
            )
            adapter.schedules[schedule.m_id] = schedule
        journal = ControlJournal.from_state(
            raw_journal,
            config_entry_id=config_entry_id,
            gateway_serial=gateway_serial,
        ).mark_unfinished_recovery_required()
        counter = state.get("transaction_counter", 0)
        if isinstance(counter, bool) or not isinstance(counter, int) or counter < 0:
            raise ValueError("simulator transaction counter is invalid")
        return cls(
            adapter=adapter,
            journal=InMemoryJournalPort(journal),
            _transaction_counter=counter,
        )

    def reset(self) -> None:
        """Reset one simulator entry while preserving its durable scope."""
        journal = self.journal.journal
        self.adapter = SimulatorAdapter()
        self.journal = InMemoryJournalPort(
            ControlJournal.empty(
                config_entry_id=journal.config_entry_id,
                gateway_serial=journal.gateway_serial,
            )
        )
        self._transaction_counter = 0
        self.__post_init__()

    def _target_origin(self, request: AdapterRequest) -> ScheduleOrigin:
        identity = request.target_identity
        if identity is None or identity.m_id is None:
            return ScheduleOrigin.LOCAL_CREATED
        schedule = self.adapter.schedules.get(identity.m_id)
        return schedule.origin if schedule is not None else ScheduleOrigin.LOCAL_CREATED

    def _next_transaction_id(self) -> str:
        self._transaction_counter += 1
        return f"sim-{self._transaction_counter:08d}"


def _request_m_id(request: AdapterRequest) -> int:
    identity = request.target_identity
    if identity is None or identity.m_id is None:
        raise ValueError("scheduler request requires m_id")
    return identity.m_id


def _number(payload: Mapping[str, Any], name: str) -> float:
    value = payload[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("schedule start must be a datetime")
    if parsed.tzinfo is None:
        raise ValueError("schedule start must be timezone-aware")
    return parsed.astimezone(UTC)


__all__ = [
    "ReplacementSagaPhase",
    "SIMULATOR_HISTORY_LIMIT",
    "SIMULATOR_SCHEDULE_LIMIT",
    "SimulatedBattery",
    "SimulatedSchedule",
    "SimulatorAdapter",
    "SimulatorClock",
    "SimulatorRuntime",
]
