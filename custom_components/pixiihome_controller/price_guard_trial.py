"""Explicit administrator-only, bounded 300 W real-battery SOC-guard validation."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .command import CommandMethod, CommandStatus, CorrelatedCommandRequest
from .control.journal import JournalPhase, JournalTargetIdentity
from .protocol.commands import DemandResponse
from .protocol.scheduler_commands import ScheduledGet
from .scheduler.mutations import (
    SchedulerCreateRequest,
    SchedulerTransactionResult,
    SchedulerTransactionStatus,
)
from .scheduler.ownership import OwnershipLifecycle

if TYPE_CHECKING:
    from .runtime import RuntimeData


async def run_price_guard_validation(
    runtime: RuntimeData, *, duration_seconds: int = 40
) -> dict[str, Any]:
    if type(duration_seconds) is not int or duration_seconds not in {40, 120}:
        raise ValueError("price_guard_trial_duration_invalid")
    controller = runtime.price_optimization_controller
    if controller is None:
        raise ValueError("price_guard_trial_unavailable")
    async with controller.soc_guard.validation_lock:
        return await _run_validation_locked(runtime, duration_seconds=duration_seconds)


async def _run_validation_locked(runtime: RuntimeData, *, duration_seconds: int) -> dict[str, Any]:
    """Validate the real condition-to-stop path without changing saved SOC limits.

    A temporary bound applies only to the newly created test identity. No
    foreign schedule, running activation or saved setting is changed. The
    test interval has a fixed 40 or explicitly selected 120 second lifetime.
    """
    controller = runtime.price_optimization_controller
    manager = runtime.scheduler_mutation_manager
    ownership = runtime.scheduler_ownership
    observer = runtime.load_balancing_observer
    coordinator = runtime.scheduler_coordinator
    if controller is None or manager is None or ownership is None or coordinator is None:
        raise ValueError("price_guard_trial_unavailable")
    if bool(runtime.operational_settings.get("price_schedule_writes_enabled")):
        raise ValueError("price_guard_trial_requires_automatic_writes_disabled")
    if not runtime.production_control_enabled() or not runtime.plant_health().healthy:
        raise ValueError("price_guard_trial_plant_not_ready")
    if runtime.telemetry.value("core", "srv_type") != 0:
        raise ValueError("price_guard_trial_requires_idle")
    from .price_controller import _record_start

    expired_reconciliations = []
    for record in tuple(ownership.records.values()):
        start = _record_start(record)
        duration = record.intended_state.get("duration")
        if (
            record.owner == "automatic_price_optimization"
            and record.lifecycle in {OwnershipLifecycle.OWNED, OwnershipLifecycle.CONFLICTED,
                                     OwnershipLifecycle.STOPPED_RESERVED}
            and start is not None and type(duration) is int and duration > 0
            and start + timedelta(seconds=duration) <= datetime.now(UTC)
        ):
            result = await manager.reconcile_expired_price(record.m_id)
            expired_reconciliations.append({
                "m_id": record.m_id, "status": result.status.value, "reason": result.reason,
            })
            if result.status is not SchedulerTransactionStatus.SUCCESS:
                raise ValueError("price_guard_trial_expired_identity_not_reconciled")

    def conflicts(record):
        if record.lifecycle in {OwnershipLifecycle.PROVISIONAL, OwnershipLifecycle.CONFLICTED}:
            return True
        if record.lifecycle is not OwnershipLifecycle.OWNED:
            return False
        start = _record_start(record)
        duration = record.intended_state.get("duration")
        return not (
            record.owner == "automatic_price_optimization"
            and start is not None
            and type(duration) is int
            and start + timedelta(seconds=duration) <= datetime.now(UTC)
        )

    if any(conflicts(record) for record in ownership.records.values()):
        raise ValueError("price_guard_trial_existing_owned_schedule")
    if observer is None or not observer.samples:
        raise ValueError("price_guard_trial_phase_data_unavailable")
    await observer.async_refresh()
    observed = observer.snapshot.get("observed_at")
    if (
        observer.status != "observing"
        or not isinstance(observed, str)
        or (datetime.now(UTC) - datetime.fromisoformat(observed)).total_seconds() > 10
    ):
        raise ValueError("price_guard_trial_phase_data_not_fresh")
    if any(
        sample.current_a > fuse - 3
        for sample, fuse in zip(observer.samples, observer.config.fuses, strict=True)
    ):
        raise ValueError("price_guard_trial_insufficient_phase_margin")
    battery = runtime.battery
    soc = battery.usable_soc
    if (
        soc is None
        or not isfinite(soc)
        or soc < 20
        or battery.discharge_power_limit_w is None
        or battery.discharge_power_limit_w < 300
    ):
        raise ValueError("price_guard_trial_battery_not_ready")
    snapshot = await coordinator.async_refresh()
    now = datetime.now(UTC)
    window_seconds = 180 if duration_seconds == 120 else 70
    if snapshot is None or snapshot.data_status != "current" or any(
        item.enabled
        and (
            item.recurrence.recurring is not False
            or item.start_epoch < int((now + timedelta(seconds=window_seconds)).timestamp())
            and item.end_epoch > int(now.timestamp())
        )
        for item in snapshot.schedules
    ):
        raise ValueError("price_guard_trial_schedule_conflict")
    if runtime.telemetry.value("core", "srv_type") != 0 or not runtime.plant_health().healthy:
        raise ValueError("price_guard_trial_context_changed")
    meter = runtime.telemetry.value("meter", "meter_w")
    if (
        not runtime.telemetry.is_fresh("meter")
        or type(meter) not in {int, float}
        or not isfinite(meter)
        or meter < 400
    ):
        # With export prohibited, solar surplus cannot produce a discharge
        # observation. Do not start a physical trial that cannot prove its goal.
        raise ValueError("price_guard_trial_requires_grid_import")
    started = datetime.now(UTC) + timedelta(seconds=5)
    created = await manager.create(
        SchedulerCreateRequest(
            starttime=started,
            duration_seconds=duration_seconds,
            service="peakshaving",
            values={
                "exportlim": 0,
                "importlim": 0,
                "charge_max": 0,
                "discharge_max": 300,
                "keep_active": True,
            },
            owner="automatic_price_optimization",
        )
    )
    if created.status is not SchedulerTransactionStatus.SUCCESS or created.m_id is None:
        raise ValueError(f"price_guard_trial_create_{created.reason}")
    message_id = created.m_id
    guard = controller.soc_guard
    observation_seconds = 95 if duration_seconds == 120 else 15
    report: dict[str, Any] = {
        "status": "running",
        "test_message_id": message_id,
        "power_limit_w": 300,
        "duration_seconds": duration_seconds,
        "scheduled_start": started.isoformat(),
        "stop_confirmed": False,
        "physical_stop_confirmed": False,
        "absence_confirmed": False,
        "identity_released": False,
        "stop_method": "delete",
        "failed_stop_observation": None,
        "priority_handoff": {"status": "not_started"},
        "saved_settings_changed": False,
        "foreign_commands": 0,
        "actual_soc_crossing": False,
        "temporary_identity_bound": True,
        "expired_reconciliations": expired_reconciliations,
        "observation_window_seconds": observation_seconds,
        "required_schedule_window_seconds": window_seconds,
        "native_reference_delay_seconds": observer.general_service_settings.get("ref_delay"),
        "native_meter_id": observer.general_service_settings.get("meter_other_srv"),
        "samples": [],
    }
    try:
        # Prove an actual running owned service, then change only that test's
        # bound so the real guard condition and exact stop path are exercised.
        for _ in range(observation_seconds):
            await asyncio.sleep(1)
            ack = await manager.executor.execute_get(
                ScheduledGet(
                    m_id=message_id,
                    expected_service="peakshaving",
                ).build()
            )
            report["samples"].append(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "service": runtime.telemetry.value("core", "srv_type"),
                    "ac_power_w": runtime.telemetry.value("core", "ac_w"),
                    "core_received_at": (
                        runtime.telemetry.received_at["core"].isoformat()
                        if "core" in runtime.telemetry.received_at else None
                    ),
                    "meter_power_w": runtime.telemetry.value("meter", "meter_w"),
                    "discharge_power_limit_w": runtime.battery.discharge_power_limit_w,
                    "plant_healthy": runtime.plant_health().healthy,
                    "battery_power_w": runtime.battery.power_w,
                    "usable_soc": runtime.battery.usable_soc,
                    "keyed_status": ack.service_status,
                    "keyed_time_left": ack.time_left,
                }
            )
            service = runtime.telemetry.value("core", "srv_type")
            if service not in {0, 101, 104}:
                raise ValueError("price_guard_trial_foreign_service_preempted")
            if not runtime.plant_health().healthy:
                raise ValueError("price_guard_trial_plant_not_ready")
            if (
                ack.service_status in range(5, 12)
                and (ack.time_left is None or ack.time_left > 0)
                and service in {101, 104}
                and runtime.telemetry.is_fresh("core", max_age_seconds=10)
                and runtime.telemetry.received_at.get("core", datetime.min.replace(tzinfo=UTC))
                >= started
                and type(runtime.telemetry.value("core", "ac_w")) in {int, float}
                and -350 <= runtime.telemetry.value("core", "ac_w") < -50
            ):
                actual_soc = runtime.battery.usable_soc
                if actual_soc is None:
                    raise ValueError("price_guard_trial_soc_lost")
                guard.validation_bounds[message_id] = (min(100, actual_soc + 0.15), 100)
                before_stops = guard.stops
                await guard.tick()
                report.update(
                    {
                        "guard_status": guard.status,
                        "guard_reason": guard.reason,
                        "stop_confirmed": guard.stops > before_stops,
                        "physical_stop_confirmed": guard.stops > before_stops,
                        "failed_stop_observation": guard.last_stop_observation,
                    }
                )
                stopped_record = ownership.records.get(message_id)
                report["absence_confirmed"] = (
                    stopped_record is not None
                    and stopped_record.lifecycle is OwnershipLifecycle.ORPHANED
                )
                report["identity_released"] = report["absence_confirmed"]
                if not report["stop_confirmed"]:
                    raise ValueError("price_guard_trial_stop_not_confirmed")
                if (
                    stopped_record is not None
                    and stopped_record.lifecycle is OwnershipLifecycle.STOPPED_RESERVED
                ):
                    deadline = started + timedelta(seconds=duration_seconds)
                    if stopped_record.reservation_until is not None:
                        deadline = max(deadline,
                                       datetime.fromisoformat(stopped_record.reservation_until))
                    report["priority_reservation_until"] = deadline.isoformat()
                    if (deadline - datetime.now(UTC)).total_seconds() > duration_seconds:
                        report["status"] = "output_stopped_priority_deferred"
                        report["priority_handoff"] = {
                            "status": "not_started",
                            "reason": "reserved_interval_exceeds_trial_window",
                        }
                        return report
                    async with asyncio.timeout(duration_seconds + 2):
                        while datetime.now(UTC) <= deadline:
                            if (runtime.telemetry.value("core", "srv_type") != 0
                                    or not runtime.plant_health().healthy):
                                raise ValueError("price_guard_trial_quarantine_context_changed")
                            await asyncio.sleep(1)
                    expiry = await manager.reconcile_expired_price(message_id)
                    report["reserved_expiry"] = {
                        "status": expiry.status.value, "reason": expiry.reason,
                    }
                    if expiry.status is not SchedulerTransactionStatus.SUCCESS:
                        raise ValueError("price_guard_trial_reserved_expiry_unverified")
                await asyncio.sleep(3)
                report["final_service"] = runtime.telemetry.value("core", "srv_type")
                report["final_ac_power_w"] = runtime.telemetry.value("core", "ac_w")
                report["priority_handoff"] = await _priority_trial(
                    runtime, settling_wait_seconds=15 if duration_seconds == 120 else 7
                )
                priority = report["priority_handoff"]
                if priority.get("status") != "observed" or not priority.get("higher_priority_seen"):
                    raise ValueError("price_guard_trial_priority_not_verified")
                report["status"] = "completed"
                return report
        raise ValueError("price_guard_trial_no_running_readback")
    except Exception as exc:
        # Keep the measured samples even on failure; raising here previously
        # discarded the very evidence needed to diagnose a zero-power run.
        report.update(status="failed", error=str(exc))
        return report
    finally:
        guard.validation_bounds.pop(message_id, None)
        record = ownership.records.get(message_id)
        if record is not None and record.lifecycle is OwnershipLifecycle.OWNED:
            # Only an unattempted stop is allowed here. An uncertain write is
            # marked conflicted by the manager and expires without a retry.
            try:
                cleanup = await _cleanup_price_interval(runtime, message_id)
                report["cleanup"] = {"status": cleanup.status.value, "reason": cleanup.reason}
                if cleanup.status is not SchedulerTransactionStatus.SUCCESS:
                    report["status"] = "failed"
            except Exception as exc:
                report.update(status="failed", cleanup={"status": "unknown", "error": str(exc)})
        record = ownership.records.get(message_id)
        report["final_ownership_lifecycle"] = record.lifecycle.value if record else None


async def _priority_trial(
    runtime: RuntimeData, *, settling_wait_seconds: int = 7
) -> dict[str, Any]:
    """Emulate ordinary higher priority at zero W for 15 seconds, not FCR/FFR."""
    manager = runtime.scheduler_mutation_manager
    ownership = runtime.scheduler_ownership
    assert manager is not None and ownership is not None
    if runtime.telemetry.value("core", "srv_type") != 0 or not runtime.plant_health().healthy:
        return {"status": "skipped", "reason": "battery_no_longer_idle"}
    coordinator = runtime.scheduler_coordinator
    if coordinator is None:
        return {"status": "skipped", "reason": "scheduler_inventory_unavailable"}
    snapshot = await coordinator.async_refresh()
    now = datetime.now(UTC)
    if snapshot is None or snapshot.data_status != "current" or any(
        item.enabled and (
            item.recurrence.recurring is not False
            or item.start_epoch < int((now + timedelta(seconds=70)).timestamp())
            and item.end_epoch > int(now.timestamp())
        ) for item in snapshot.schedules
    ):
        return {"status": "skipped", "reason": "fresh_priority_schedule_conflict"}
    observer = runtime.load_balancing_observer
    if observer is None or not observer.samples:
        return {"status": "skipped", "reason": "priority_phase_data_unavailable"}
    await observer.async_refresh()
    observed = observer.snapshot.get("observed_at")
    try:
        phase_age = ((datetime.now(UTC) - datetime.fromisoformat(observed)).total_seconds()
                     if isinstance(observed, str) else -1)
    except (ValueError, TypeError):
        phase_age = -1
    if (observer.status != "observing" or not isinstance(observed, str)
            or not 0 <= phase_age <= 10 or len(observer.samples) != 3
            or any(type(sample.current_a) not in {int, float}
                   or not isfinite(sample.current_a) or sample.current_a > fuse - 3
                   for sample, fuse in
                   zip(observer.samples, observer.config.fuses, strict=True))):
        return {"status": "skipped", "reason": "priority_phase_margin_not_ready"}
    meter = runtime.telemetry.value("meter", "meter_w")
    battery = runtime.battery
    if (not runtime.telemetry.is_fresh("meter") or type(meter) not in {int, float}
            or not isfinite(meter) or meter < 400
            or battery.usable_soc is None or not isfinite(battery.usable_soc)
            or battery.usable_soc < 20 or battery.discharge_power_limit_w is None
            or not isfinite(battery.discharge_power_limit_w)
            or battery.discharge_power_limit_w < 300):
        return {"status": "skipped", "reason": "priority_grid_or_battery_not_ready"}
    if (runtime.telemetry.value("core", "srv_type") != 0
            or not runtime.plant_health().healthy):
        return {"status": "skipped", "reason": "battery_no_longer_idle"}
    result = await manager.create(
        SchedulerCreateRequest(
            starttime=datetime.now(UTC) + timedelta(seconds=4),
            duration_seconds=25,
            service="peakshaving",
            owner="automatic_price_optimization",
            values={
                "exportlim": 0,
                "importlim": 0,
                "charge_max": 0,
                "discharge_max": 300,
                "keep_active": True,
            },
        )
    )
    if result.status is not SchedulerTransactionStatus.SUCCESS or result.m_id is None:
        return {"status": "blocked", "reason": result.reason}
    price_id = result.m_id
    direct_id: int | None = None
    confirmed_direct = False
    samples = []
    report: dict[str, Any] = {"status": "not_started"}
    try:
        await asyncio.sleep(settling_wait_seconds)
        before = await manager.executor.execute_get(
            ScheduledGet(
                m_id=price_id,
                expected_service="peakshaving",
            ).build()
        )
        if before.service_status not in range(5, 12) or not runtime.plant_health().healthy:
            report.update(status="skipped", reason="owned_price_not_running")
            return report
        if runtime.telemetry.value("core", "srv_type") not in {101, 104}:
            report.update(status="skipped", reason="foreign_service_preempted")
            return report
        direct_id = runtime.command_manager.allocator.allocate(persistent=True)
        transaction = "price-priority-validation-" + uuid4().hex
        await runtime.begin_control_transaction(
            transaction_id=transaction,
            operation="native_price_priority_validation",
            target_identity=JournalTargetIdentity(m_id=direct_id),
            intent={"power_w": 0, "duration_seconds": 15},
        )
        await runtime.transition_control_transaction(transaction, JournalPhase.COMMAND_SENT)
        # The explicit trial has proved the lower activation by keyed GET;
        # ordinary unscoped direct-control writes remain forbidden here.
        # Use the same serialized correlated writer as owned scheduled stops.
        typed = DemandResponse(power_w=0, duration_seconds=15).build()
        direct = await runtime.command_manager.execute_correlated(
            CorrelatedCommandRequest(
                method=CommandMethod.SET,
                message_id=direct_id,
                service=typed.service,
                fields=typed.fields,
                expected_services=("demandresponse",),
            )
        )
        if direct.status is not CommandStatus.SUCCESS:
            await runtime.transition_control_transaction(
                transaction, JournalPhase.RECOVERY_REQUIRED, reason="trial_activation_unconfirmed"
            )
            report.update(status="unconfirmed", old_mode_restart_commands=0)
            return report
        await runtime.transition_control_transaction(transaction, JournalPhase.ACKNOWLEDGED)
        verified = await runtime.command_manager.execute_correlated(
            CorrelatedCommandRequest(
                method=CommandMethod.GET,
                message_id=direct_id,
                service="demandresponse",
                expected_services=("demandresponse",),
            )
        )
        if (
            verified.ack is None
            or verified.ack.response != 65535
            or verified.ack.service_status not in range(5, 12)
            or verified.ack.time_left is not None
            and verified.ack.time_left <= 0
        ):
            await runtime.transition_control_transaction(
                transaction, JournalPhase.RECOVERY_REQUIRED, reason="trial_readback_unconfirmed"
            )
            report.update(status="unconfirmed", old_mode_restart_commands=0)
            return report
        await runtime.transition_control_transaction(
            transaction, JournalPhase.RECONCILED, reason="exact_trial_activation_verified"
        )
        confirmed_direct = True
        for _ in range(21):
            samples.append(
                {
                    "at": datetime.now(UTC).isoformat(),
                    "service": runtime.telemetry.value("core", "srv_type"),
                    "ac_power_w": runtime.telemetry.value("core", "ac_w"),
                }
            )
            await asyncio.sleep(1)
        report.update(
            status="observed", samples=samples,
            higher_priority_seen=any(s["service"] == 102 for s in samples),
            old_mode_restart_commands=0, old_mode_duration_updates=0,
        )
        return report
    finally:
        record = ownership.records.get(price_id)
        if record is not None and record.lifecycle is OwnershipLifecycle.OWNED:
            cleanup = await _cleanup_price_interval(runtime, price_id)
            report["cleanup"] = {"status": cleanup.status.value, "reason": cleanup.reason}
            if cleanup.status is not SchedulerTransactionStatus.SUCCESS:
                report.update(status="failed", reason="priority_cleanup_unconfirmed")
        if direct_id is not None and confirmed_direct:
            runtime.command_manager.allocator.retire(direct_id, release_ownership=True)


async def _cleanup_price_interval(runtime: RuntimeData, m_id: int) -> SchedulerTransactionResult:
    """After natural expiry verify with reads; never send another stop SET."""
    from .price_controller import _record_start

    manager = runtime.scheduler_mutation_manager
    ownership = runtime.scheduler_ownership
    assert manager is not None and ownership is not None
    record = ownership.records[m_id]
    start = _record_start(record)
    duration = record.intended_state.get("duration")
    if (
        start is not None and type(duration) is int and duration > 0
        and start + timedelta(seconds=duration) <= datetime.now(UTC)
    ):
        return await manager.reconcile_expired_price(m_id)
    return await manager.stop_owned_price(m_id)
