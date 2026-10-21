"""Explicit archival recovery of a finite own validation; no battery mutations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite
from typing import TYPE_CHECKING, Any

from .allocator import MessageIdAllocator, normalize_message_id
from .control.journal import ControlJournal, JournalPhase
from .price_stop import verify_price_stopped
from .protocol.mqtt import parse_ack
from .protocol.scheduler_commands import ScheduledGet, ScheduledPriceStop, evaluate_scheduler_ack
from .protocol.scheduler_readback import (
    SchedulerReconciliationStatus,
    reconcile_scheduler_delete,
    reconcile_scheduler_get,
)
from .scheduler.ownership import (
    OwnershipLifecycle,
    SchedulerOwnershipRecord,
    SchedulerOwnershipStore,
    scheduler_expected_state_from_record,
)

if TYPE_CHECKING:
    from .runtime import RuntimeData


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("price_recovery_time_invalid")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("price_recovery_time_invalid")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class PriceRecoveryPlan:
    """A journal-bound proposal; does not by itself authorize a state change."""

    journal_checksum: str
    journal_revision: int
    original: SchedulerOwnershipRecord
    reserved: SchedulerOwnershipRecord
    evidence_sha256: str
    failed_transaction_id: str

    def summary(self) -> dict[str, Any]:
        return {
            "m_id": self.original.m_id, "journal_revision_before": self.journal_revision,
            "lifecycle_before": self.original.lifecycle.value,
            "lifecycle_after": self.reserved.lifecycle.value,
            "reservation_until": self.reserved.reservation_until,
            "evidence_sha256": self.evidence_sha256,
            "original_failed_transaction_id": self.failed_transaction_id,
            "historical_failed_transaction_unchanged": True,
            "absence_confirmed": False, "identity_released": False,
            "battery_mutations": 0,
        }


def build_price_recovery_plan(
    journal: ControlJournal, evidence: Mapping[str, Any], *, now: datetime,
) -> PriceRecoveryPlan:
    """Validate historical GET, DELETE, early idle and the conservative deadline.

    Only bounded, zero-export 300 W validation intervals qualify. An old
    conflict is never retired merely because a new GET times out or is idle.
    """
    if now.tzinfo is None or journal.unresolved_transactions:
        raise ValueError("price_recovery_journal_not_ready")
    if (evidence.get("config_entry_id") != journal.config_entry_id
            or evidence.get("gateway_serial") != journal.gateway_serial):
        raise ValueError("price_recovery_scope_mismatch")
    mid = normalize_message_id(evidence["m_id"])
    ownership = SchedulerOwnershipStore.from_state(journal.ownership_state,
        config_entry_id=journal.config_entry_id, gateway_serial=journal.gateway_serial)
    record = ownership.records.get(mid)
    if (record is None or record.lifecycle is not OwnershipLifecycle.CONFLICTED
            or record.owner != "automatic_price_optimization" or record.service != "peakshaving"
            or any(r.m_id != mid and r.lifecycle in {
                OwnershipLifecycle.OWNED, OwnershipLifecycle.PROVISIONAL,
                OwnershipLifecycle.CONFLICTED, OwnershipLifecycle.STOPPED_RESERVED,
            } for r in ownership.records.values())):
        raise ValueError("price_recovery_exact_conflicted_validation_required")
    expected = scheduler_expected_state_from_record(record)
    values = expected.parameters
    if (expected.duration not in {40, 120}
            or values.get("charge_max") != 0 or values.get("exportlim") != 0
            or type(values.get("discharge_max")) not in {int, float}
            or not 0 < values["discharge_max"] <= 300):
        raise ValueError("price_recovery_bounded_validation_required")
    allocator = MessageIdAllocator.from_state(journal.command_state)
    if mid not in allocator.owned_ids:
        raise ValueError("price_recovery_allocator_identity_missing")
    create = journal.transaction(evidence["create_transaction_id"])
    failed = journal.transaction(evidence["delete_transaction_id"])
    if (create is None or create.operation != "create"
            or create.phase is not JournalPhase.RECONCILED
            or create.target_identity.m_id != mid
            or dict(create.intent) != record.as_dict()["intended_state"]
            or failed is None or failed.operation != "delete"
            or failed.phase is not JournalPhase.FAILED or failed.target_identity.m_id != mid
            or failed.reason != "price_stop_unconfirmed"
            or dict(failed.intent) != {"m_id": mid, "service": "peakshaving", "absent": True}):
        raise ValueError("price_recovery_transaction_mismatch")
    # No later write to this identity may be hidden behind the failed DELETE.
    writes = [t for t in journal.transactions if t.target_identity.m_id == mid]
    if writes != [create, failed]:
        raise ValueError("price_recovery_later_or_ambiguous_write")
    before = evidence["get_before"]
    deleted = evidence["delete_ack"]
    topic = "pixii/ack/" + journal.gateway_serial
    if before.get("topic") != topic or deleted.get("topic") != topic:
        raise ValueError("price_recovery_frame_scope_mismatch")
    readback, ack = parse_ack(before["payload"]), parse_ack(deleted["payload"])
    if (readback.method != "get"
            or reconcile_scheduler_get(readback, expected).status
            is not SchedulerReconciliationStatus.MATCH
            or type(readback.service_status) is not int or not 5 <= readback.service_status <= 11
            or type(readback.time_left) is not int or not 0 < readback.time_left <= 4294967295
            or not evaluate_scheduler_ack(ScheduledPriceStop(mid, record.service).build(),
                                          ack).successful):
        raise ValueError("price_recovery_identified_evidence_invalid")
    observed, acknowledged = _time(before["at"]), _time(deleted["at"])
    original_end = expected.starttime + timedelta(seconds=expected.duration)
    if not expected.starttime <= observed <= acknowledged < original_end:
        raise ValueError("price_recovery_frame_order_invalid")
    if (acknowledged - observed).total_seconds() > 10:
        raise ValueError("price_recovery_get_not_fresh_at_delete")
    previous = acknowledged
    samples = evidence["early_idle_samples"]
    if not isinstance(samples, list) or len(samples) != 2:
        raise ValueError("price_recovery_two_early_samples_required")
    for sample in samples:
        at, core = _time(sample["at"]), sample["payload"]
        power = core.get("ac_w")
        if (sample.get("topic") != "pixii/status/" + journal.gateway_serial + "/core"
                or not previous < at < original_end or (at - previous).total_seconds() > 10
                or core.get("srv_type") != 0 or core.get("sys_status") != 1
                or core.get("grid_status") != 1 or type(power) not in {int, float}
                or not isfinite(power) or abs(power) > 50):
            raise ValueError("price_recovery_early_idle_invalid")
        previous = at
    until = max(original_end, acknowledged + timedelta(seconds=readback.time_left))
    if now <= until:
        raise ValueError("price_recovery_reservation_not_elapsed")
    return PriceRecoveryPlan(str(journal.snapshot()["checksum"]), journal.revision, record,
        replace(record, lifecycle=OwnershipLifecycle.EXPIRED_RESERVED,
                reservation_until=until.isoformat()), _digest(evidence), failed.transaction_id)


def _idle_disabled(runtime: RuntimeData) -> None:
    if (runtime.production_control_enabled()
            or any(runtime.controller_state.as_dict().values())
            or runtime.operational_settings.get("price_schedule_writes_enabled")
            or runtime.operational_settings.get("price_export_allowed")):
        raise ValueError("price_recovery_requires_all_switches_off")
    power = runtime.telemetry.value("core", "ac_w")
    if (not runtime.plant_health().healthy
            or not runtime.telemetry.is_fresh("core", max_age_seconds=10)
            or runtime.telemetry.value("core", "srv_type") != 0
            or type(power) not in {int, float} or not isfinite(power) or abs(power) > 50):
        raise ValueError("price_recovery_requires_fresh_healthy_idle")


async def _clear_window(runtime: RuntimeData) -> None:
    coordinator = runtime.scheduler_coordinator
    if coordinator is None:
        raise ValueError("price_recovery_inventory_unavailable")
    snapshot = await coordinator.async_refresh()
    now = datetime.now(UTC).timestamp()
    if snapshot is None or snapshot.data_status != "current" or any(
        s.enabled and (s.recurrence.recurring is not False
                       or s.start_epoch < now + 30 and s.end_epoch > now)
        for s in snapshot.schedules
    ):
        raise ValueError("price_recovery_schedule_window_blocked")
    _idle_disabled(runtime)


async def recover_expired_price_validation(
    runtime: RuntimeData, evidence: Mapping[str, Any], *, dry_run: bool = True,
) -> dict[str, Any]:
    """Use GET and telemetry only; optionally persist an audited reservation.

    No CREATE/SET/DELETE, mode restart, ID release or authority change exists
    on this path. A dry run performs the same reads but never saves state.
    """
    _idle_disabled(runtime)
    manager, journal = runtime.scheduler_mutation_manager, runtime.control_journal
    if manager is None or journal is None or runtime.scheduler_ownership is None:
        raise ValueError("price_recovery_unavailable")
    plan = build_price_recovery_plan(journal, evidence, now=datetime.now(UTC))
    await _clear_window(runtime)
    try:
        current = await manager.executor.execute_get(ScheduledGet(
            m_id=plan.original.m_id, expected_service=plan.original.service).build())
    except TimeoutError:
        # Timeout is not absence: the identity stays reserved in either outcome.
        current = None
    if current is not None and reconcile_scheduler_delete(
        current, expected_m_id=plan.original.m_id,
    ).status is not SchedulerReconciliationStatus.ABSENT:
        raise ValueError("price_recovery_current_identity_not_absent_or_unreadable")
    if not await verify_price_stopped(runtime, datetime.now(UTC), None):
        raise ValueError("price_recovery_fresh_idle_not_confirmed")
    await _clear_window(runtime)
    async with runtime._control_journal_lock:
        _idle_disabled(runtime)
        journal = runtime.control_journal
        ownership = runtime.scheduler_ownership
        if (journal is None or ownership is None
                or journal.revision != plan.journal_revision
                or journal.snapshot()["checksum"] != plan.journal_checksum
                or ownership.get(plan.original.m_id) != plan.original
                or runtime.command_manager.allocator.snapshot() != dict(journal.command_state)):
            raise ValueError("price_recovery_state_changed")
        result = {**plan.summary(), "status": "validated" if dry_run else "reserved",
                  "dry_run": dry_run, "fresh_idle_confirmed": True}
        if dry_run:
            return result
        candidate = journal.with_states(
            ownership_state=ownership.snapshot_with_replacement(plan.reserved),
            audit_event={**plan.summary(), "kind": "expired_price_validation_reserved",
                         "at": datetime.now(UTC).isoformat()},
        )
        await runtime._save_control_journal_verified(candidate)
        runtime.control_journal = candidate
        ownership.replace(plan.reserved)
        result["journal_revision_after"] = candidate.revision
        return result
