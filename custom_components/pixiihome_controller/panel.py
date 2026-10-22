"""Authenticated production preview and isolated simulator control panel backend."""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
from dataclasses import replace
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path
from time import time
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.http.server import StaticPathConfig
from homeassistant.components.websocket_api.connection import ActiveConnection
from homeassistant.components.websocket_api.decorators import (
    async_response,
    require_admin,
    websocket_command,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .battery_activity import own_schedule_owner
from .const import DOMAIN
from .control.adapters import AdapterRequest
from .control.journal import JournalPhase, JournalTargetIdentity
from .control.models import AdapterCapability, ControlOperation, Initiative, ScheduleOrigin
from .control.policy import evaluate_control_operation
from .control.simulator import SIMULATOR_SCHEDULE_LIMIT, SimulatedSchedule
from .exceptions import CommandValidationError, SchedulerAuthenticationError, SchedulerProtocolError
from .protocol.scheduler_values import MANUAL_SCHEDULER_SERVICES, validate_scheduler_values
from .runtime import RuntimeData, _scheduler_snapshot
from .scheduler.models import (
    NormalizedSchedule,
    Ownership,
    ScheduleCompleteness,
    ScheduleConsistency,
)
from .simulator_entry import SIMULATOR_SCENARIOS, SimulatorEntryRuntime

PANEL_ELEMENT = "pixiihome-control-card"
PANEL_MODULE_URL = f"/{DOMAIN}/lovelace.js"
_PANEL_REGISTERED = f"{DOMAIN}_control_api_registered"
_LOGGER = logging.getLogger(__name__)

def _finite_number(value: float) -> float:
    if not isfinite(value):
        raise vol.Invalid("Numeric control values must be finite")
    return value


_PANEL_OPERATIONS = (
    ControlOperation.DIRECT_TARGET_SOC,
    ControlOperation.DIRECT_DEMAND_RESPONSE,
    ControlOperation.CREATE,
    ControlOperation.UPDATE,
    ControlOperation.DELETE,
    ControlOperation.REPLACE,
    ControlOperation.STOP,
    ControlOperation.PLANNER_APPLY,
    ControlOperation.RECOVERY_APPLY,
)
_OPERATION_SCHEMA = vol.In([operation.value for operation in _PANEL_OPERATIONS])
_PANEL_PAST_SECONDS = 24 * 60 * 60
_PANEL_FUTURE_SECONDS = 7 * 24 * 60 * 60
_PANEL_DETAIL_LIMIT = 100
_TARGET_SCHEMA = vol.Schema(
    {
        vol.Optional("m_id"): vol.All(int, vol.Range(min=0)),
        vol.Optional("sch_index"): vol.All(int, vol.Range(min=0)),
        vol.Optional("service_no"): vol.All(int, vol.Range(min=0)),
        vol.Optional("source_id"): vol.All(int, vol.Range(min=0)),
        vol.Optional("asset_id"): vol.All(str, vol.Length(min=1, max=128)),
    },
    extra=vol.PREVENT_EXTRA,
)
_SCHEDULE_INTENT_SCHEMA = vol.Schema(
    {
        vol.Required("m_id"): vol.All(int, vol.Range(min=0)),
        vol.Required("start_epoch"): vol.All(int, vol.Range(min=0)),
        vol.Required("duration_seconds"): vol.All(int, vol.Range(min=1)),
        vol.Required("socref"): vol.All(
            vol.Coerce(float),
            _finite_number,
            vol.Range(min=0, max=100),
        ),
        vol.Optional("source_id"): vol.All(int, vol.Range(min=0)),
    },
    extra=vol.PREVENT_EXTRA,
)
_INTENT_SCHEMA = vol.Schema(
    {
        vol.Optional("start_epoch"): vol.All(int, vol.Range(min=0)),
        vol.Optional("duration_seconds"): vol.All(int, vol.Range(min=1)),
        vol.Optional("enabled"): bool,
        vol.Optional("socref"): vol.All(
            vol.Coerce(float),
            _finite_number,
            vol.Range(min=0, max=100),
        ),
        vol.Optional("values"): dict,
        vol.Optional("power_w"): vol.All(vol.Coerce(float), _finite_number),
        vol.Optional("transaction_id"): vol.All(str, vol.Length(min=1, max=128)),
        vol.Optional("request_grant"): bool,
        vol.Optional("schedules"): vol.All(
            [_SCHEDULE_INTENT_SCHEMA],
            vol.Length(max=SIMULATOR_SCHEDULE_LIMIT),
        ),
    },
    extra=vol.PREVENT_EXTRA,
)

RuntimeInstance = RuntimeData | SimulatorEntryRuntime


def _runtime_for_entry(hass: HomeAssistant, entry_id: str) -> RuntimeInstance:
    runtime = hass.data.get(DOMAIN, {}).get(entry_id)
    if runtime is None or not isinstance(runtime, (RuntimeData, SimulatorEntryRuntime)):
        raise vol.Invalid("The requested Pixii config entry is not loaded")
    return runtime


async def async_panel_state(hass: HomeAssistant) -> dict[str, Any]:
    """Refresh bounded production scheduler evidence, then return sanitized state."""
    runtimes: dict[str, RuntimeInstance] = hass.data.get(DOMAIN, {})
    now_epoch = int(time())
    from_epoch = now_epoch - _PANEL_PAST_SECONDS
    to_epoch = now_epoch + _PANEL_FUTURE_SECONDS
    for runtime in runtimes.values():
        if isinstance(runtime, SimulatorEntryRuntime):
            continue
        manager = runtime.scheduler_manager
        if manager is None:
            continue
        try:
            entries = await manager.refresh_list(from_epoch, to_epoch)
        except Exception:  # noqa: BLE001 - diagnostics retain the sanitized read failure
            continue
        for entry in entries[:_PANEL_DETAIL_LIMIT]:
            try:
                await manager.refresh_detail(entry.sch_index)
            except Exception:  # noqa: BLE001 - incomplete targets remain protected
                continue
        controller = getattr(runtime, "gateway_scheduler", None)
        if controller is not None:
            runtime.scheduler_target_soc.clear()
            for service_no in dict.fromkeys(
                s.service_no for s in manager.normalized_schedules if s.service_type == 107
            ):
                try:
                    runtime.scheduler_target_soc[service_no] = (
                        await controller.client.read_target_soc(service_no)
                    )
                except Exception:  # noqa: BLE001 - absent values are never substituted with a default
                    continue
    return panel_state(hass)

OWNER_LABELS_SV = {
    "price optimization": "Prisoptimering (denna integration)",
    "solar charging": "Solladdning (denna integration)",
    "adaptive peak shaving": "Adaptiv effekttoppskapning (denna integration)",
}


def _schedule_snapshot(runtime: RuntimeData) -> list[dict[str, Any]]:
    manager = runtime.scheduler_manager
    if (
        manager is None
        or manager.list_visibility_diagnostics().get("invalidated") is True
    ):
        return []
    schedules = _scheduler_snapshot(manager)
    cache = getattr(runtime, "scheduler_target_soc", {})
    by_index = {item.sch_index: item for item in manager.normalized_schedules}
    for schedule in schedules:
        parameters = cache.get(schedule["service_no"])
        if schedule["service_type"] == 107 and parameters is not None:
            schedule["target_soc_percent"] = parameters["reference"]
        index = schedule["sch_index"]
        normalized = by_index.get(index) if isinstance(index, int) else None
        owner = None if normalized is None else own_schedule_owner(runtime, normalized)
        if owner is not None:
            schedule["owner_label"] = OWNER_LABELS_SV.get(owner, owner)
    return schedules


def _authoritative_target(
    runtime: RuntimeData,
    target: dict[str, Any],
) -> tuple[NormalizedSchedule, dict[str, Any]]:
    manager = runtime.scheduler_manager
    sch_index = target.get("sch_index")
    if manager is None or isinstance(sch_index, bool) or not isinstance(sch_index, int):
        raise vol.Invalid("A current scheduler target is required")
    matches = [
        schedule
        for schedule in manager.normalized_schedules
        if schedule.sch_index == sch_index
    ]
    if len(matches) != 1:
        raise vol.Invalid("The scheduler target is missing or ambiguous")
    schedule = matches[0]
    authoritative = {
        "sch_index": schedule.sch_index,
        "service_no": schedule.service_no,
        "source_id": schedule.source_id,
        "asset_id": schedule.asset_id,
    }
    if target.get("m_id") is not None:
        raise vol.Invalid("Scheduler m_id cannot be inferred from sch_index")
    for field_name, expected in authoritative.items():
        if field_name in target and target[field_name] != expected:
            raise vol.Invalid(f"The scheduler target {field_name} changed")
    if schedule.service_type != 107:
        raise vol.Invalid("Only Target SoC schedules can be previewed for mutation")
    if (
        schedule.completeness is not ScheduleCompleteness.COMPLETE
        or schedule.consistency is not ScheduleConsistency.CONSISTENT
    ):
        raise vol.Invalid("The scheduler target lacks complete consistent evidence")
    return schedule, authoritative


def _schedule_preview_state(schedule: NormalizedSchedule) -> dict[str, Any]:
    return {
        "sch_index": schedule.sch_index,
        "start_epoch": schedule.start_epoch,
        "end_epoch": schedule.end_epoch,
        "duration_seconds": schedule.duration_seconds,
        "service_no": schedule.service_no,
        "service_type": schedule.service_type,
        "enabled": schedule.enabled,
        "source_id": schedule.source_id,
        "asset_id": schedule.asset_id,
        "origin": schedule.ownership.ownership.value,
    }


def _production_after_state(
    operation: ControlOperation,
    before: dict[str, Any],
    intent: dict[str, Any],
) -> dict[str, Any] | None:
    if operation is ControlOperation.DELETE:
        if intent:
            raise vol.Invalid("Delete preview does not accept replacement values")
        return None
    allowed = (
        {"duration_seconds", "socref"}
        if operation is ControlOperation.UPDATE
        else {"start_epoch", "duration_seconds", "socref"}
    )
    if operation not in {ControlOperation.UPDATE, ControlOperation.REPLACE}:
        raise vol.Invalid("The production schedule operation is unsupported")
    if not intent or set(intent) - allowed:
        raise vol.Invalid("The production schedule preview contains unsupported values")
    after = dict(before)
    after.update(intent)
    if "start_epoch" in intent or "duration_seconds" in intent:
        after["end_epoch"] = after["start_epoch"] + after["duration_seconds"]
    return after


def _recovery_snapshot(runtime: RuntimeData) -> list[dict[str, Any]]:
    journal = runtime.control_journal
    if journal is None:
        return []
    return [
        transaction.as_dict()
        for transaction in journal.transactions
        if transaction.phase is JournalPhase.RECOVERY_REQUIRED
    ]


def _production_state(entry_id: str, runtime: RuntimeData) -> dict[str, Any]:
    health = runtime.plant_health()
    impact = runtime.operation_impact()
    return {
        "entry_id": entry_id,
        "gateway_serial": runtime.serial,
        "runtime_mode": "production",
        "control_mode": "local_control",
        "external_control": "unknown",
        "plant_health": health.as_dict(),
        "operation_impact": impact.as_dict(),
        "release_gate": {
            "live_writes_enabled": runtime.release_gate.live_writes_enabled,
            "production_control_enabled": getattr(
                runtime,
                "production_control_enabled",
                lambda: runtime.release_gate.live_writes_enabled,
            )(),
            "authority_revision": getattr(
                getattr(runtime, "production_authority", None), "revision", 0
            ),
            "simulator_apply_enabled": False,
            "infrastructure_status": runtime.release_gate.infrastructure_status,
            "blocked_reasons": sorted(runtime.release_gate.blocked_reasons),
        },
        "scheduler": runtime.scheduler_mutation_diagnostics(),
        "gateway_scheduler_available": getattr(runtime, "gateway_scheduler", None) is not None,
        "schedule_create_available": (
            getattr(runtime, "scheduler_mutation_manager", None) is not None
        ),
        "scheduler_evidence": (
            runtime.scheduler_manager.list_visibility_diagnostics()
            if runtime.scheduler_manager is not None else {}
        ),
        "schedules": _schedule_snapshot(runtime),
        "recovery": _recovery_snapshot(runtime),
        "control_state_error": runtime.control_state_error,
        "scenarios": [],
        "audit_history": [],
        "replacement_state": {},
    }


def _simulator_schedule_state(schedule: SimulatedSchedule) -> dict[str, Any]:
    state = schedule.as_dict()
    state.update(
        {
            "start_epoch": int(schedule.start.timestamp()),
            "end_epoch": int(schedule.end.timestamp()),
            "enabled": True,
            "asset_id": "battery-1",
            "service_type_label": "Target SoC",
            "editable": schedule.origin is ScheduleOrigin.LOCAL_CREATED,
            "deletable": schedule.origin is ScheduleOrigin.LOCAL_CREATED,
            "protected": schedule.origin is not ScheduleOrigin.LOCAL_CREATED,
            "completeness": "complete",
            "consistency": "consistent",
            "source_label": schedule.origin.value,
        }
    )
    return state


def _simulator_state(entry_id: str, runtime: SimulatorEntryRuntime) -> dict[str, Any]:
    simulator = runtime.simulator
    snapshot = simulator.snapshot()
    faults = set(snapshot["faults"])
    journal = simulator.journal.journal
    recovery = [
        transaction.as_dict()
        for transaction in journal.transactions
        if transaction.phase is JournalPhase.RECOVERY_REQUIRED
    ]
    health_reasons = sorted(
        fault
        for fault in faults
        if fault
        in {
            "telemetry_stale",
            "system_unsafe",
            "battery_unsafe",
            "grid_unsafe",
            "system_events",
        }
    )
    return {
        "entry_id": entry_id,
        "gateway_serial": runtime.serial,
        "runtime_mode": "simulator",
        "control_mode": "local_control",
        "external_control": "none",
        "clock": snapshot["clock"],
        "battery": snapshot["battery"],
        "faults": snapshot["faults"],
        "plant_health": {
            "healthy": not health_reasons,
            "telemetry_fresh": "telemetry_stale" not in faults,
            "system_safe": "system_unsafe" not in faults,
            "battery_safe": "battery_unsafe" not in faults,
            "grid_safe": "grid_unsafe" not in faults,
            "system_events_clear": "system_events" not in faults,
            "reasons": health_reasons,
        },
        "operation_impact": {
            "active_service": snapshot["active_service"],
            "protected_service_active": snapshot["active_service"] not in {0, 102, 107},
            "immediate_effect_known": snapshot["active_service"] in {0, 102, 107},
            "exact_target": True,
        },
        "release_gate": {
            "live_writes_enabled": False,
            "production_control_enabled": runtime.production_control_enabled(),
            "authority_revision": runtime.production_authority_revision,
            "simulator_apply_enabled": True,
            "infrastructure_status": "isolated_local_simulator",
            "blocked_reasons": []
            if runtime.production_control_enabled()
            else ["production_control_disabled"],
        },
        "scheduler": {
            "status": "simulator_local",
            "live_writes_enabled": False,
            "simulator_apply_enabled": True,
        },
        "schedules": [
            _simulator_schedule_state(simulator.adapter.schedules[m_id])
            for m_id in sorted(simulator.adapter.schedules)
        ],
        "recovery": recovery,
        "control_state_error": None,
        "scenarios": list(SIMULATOR_SCENARIOS),
        "controller_state": dict(journal.controller_state),
        "planner_state": dict(journal.planner_state),
        "replacement_state": dict(journal.replacement_state),
        "audit_history": [dict(item) for item in journal.audit_history],
        "state_fingerprint": simulator.state_fingerprint(),
    }


def panel_state(hass: HomeAssistant) -> dict[str, Any]:
    """Build a sanitized, side-effect-free snapshot for the admin panel."""
    runtimes: dict[str, RuntimeInstance] = hass.data.get(DOMAIN, {})
    entries = [
        _simulator_state(entry_id, runtime)
        if isinstance(runtime, SimulatorEntryRuntime)
        else _production_state(entry_id, runtime)
        for entry_id, runtime in sorted(runtimes.items())
    ]
    return {
        "entries": entries,
        "live_writes_enabled": False,
        "simulator_apply_enabled": any(
            isinstance(runtime, SimulatorEntryRuntime) for runtime in runtimes.values()
        ),
    }


def _production_preview(
    runtime: RuntimeData,
    *,
    operation: ControlOperation,
    service: str | None,
    target: dict[str, Any],
    intent: dict[str, Any],
) -> dict[str, Any]:
    intent.pop("request_grant", None)
    authoritative_target: dict[str, Any] = {}
    origin = ScheduleOrigin.LOCAL_CREATED
    exact_target = False
    canonical_service = service
    protected_schedule_conflict = False
    manager = runtime.scheduler_manager
    recovery_evidence_missing = False
    if operation is ControlOperation.DIRECT_TARGET_SOC:
        if service not in {None, "targetsoc"}:
            raise vol.Invalid("Direct Target SoC requires Target SoC service")
        canonical_service = "targetsoc"
    elif operation is ControlOperation.DIRECT_DEMAND_RESPONSE:
        if service not in {None, "demandresponse"}:
            raise vol.Invalid("Direct Demand Response requires demand response service")
        canonical_service = "demandresponse"
    elif operation is ControlOperation.CREATE:
        service = service or "targetsoc"
        if service not in MANUAL_SCHEDULER_SERVICES:
            raise vol.Invalid("scheduler_create_service_unsupported")
        canonical_service = service
    elif operation in {
        ControlOperation.UPDATE,
        ControlOperation.DELETE,
        ControlOperation.REPLACE,
        ControlOperation.STOP,
        ControlOperation.PLANNER_APPLY,
        ControlOperation.RECOVERY_APPLY,
    }:
        if service not in {None, "targetsoc"}:
            raise vol.Invalid("Scheduler previews require Target SoC service")
        canonical_service = "targetsoc"

    if operation in {
        ControlOperation.UPDATE,
        ControlOperation.DELETE,
        ControlOperation.REPLACE,
    }:
        schedule, authoritative_target = _authoritative_target(runtime, target)
        origin = {
            Ownership.INTEGRATION_OWNED: ScheduleOrigin.LOCAL_CREATED,
            Ownership.EXTERNAL: ScheduleOrigin.EXTERNAL_OBSERVED,
            Ownership.UNKNOWN: ScheduleOrigin.UNKNOWN,
        }[schedule.ownership.ownership]
        before: object = _schedule_preview_state(schedule)
        after: object = _production_after_state(operation, before, intent)
        exact_target = True
    elif operation is ControlOperation.STOP:
        if target or intent:
            raise vol.Invalid("Stop preview does not accept target or intent values")
        before = {"active_service": runtime.operation_impact().active_service}
        after = {"active_service": 0, "schedules_unchanged": True}
    elif operation is ControlOperation.DIRECT_TARGET_SOC:
        if target or set(intent) != {"socref"}:
            raise vol.Invalid("Direct Target SoC requires only socref")
        before = runtime.operation_impact().as_dict()
        after = {"target_soc_percent": intent["socref"]}
    elif operation is ControlOperation.DIRECT_DEMAND_RESPONSE:
        if target or set(intent) != {"power_w"}:
            raise vol.Invalid("Direct Demand Response requires only power_w")
        before = runtime.operation_impact().as_dict()
        after = {"power_w": intent["power_w"]}
    elif operation is ControlOperation.CREATE:
        assert service is not None
        required_values = {"socref"} if service == "targetsoc" else {"values"}
        if target or set(intent) != {"start_epoch", "duration_seconds"} | required_values:
            raise vol.Invalid(
                "Create preview requires start, duration, and Target SoC values"
            )
        if (
            type(intent["start_epoch"]) is not int
            or not int(time()) < intent["start_epoch"] <= 2_147_483_647
            or type(intent["duration_seconds"]) is not int
            or not 0 < intent["duration_seconds"] <= _PANEL_FUTURE_SECONDS
            or intent["start_epoch"] + intent["duration_seconds"] > 2_147_483_647
        ):
            raise vol.Invalid("scheduler_create_values_invalid")
        try:
            validate_scheduler_values(
                service,
                {"socref": intent["socref"]} if service == "targetsoc" else intent["values"],
            )
        except CommandValidationError as err:
            raise vol.Invalid("scheduler_create_values_invalid") from err
        protected_schedule_conflict = any(
            schedule.enabled
            and (
                schedule.recurrence.recurring is True
                or schedule.consistency is ScheduleConsistency.MISMATCH
                or (
                    intent["start_epoch"] < schedule.end_epoch
                    and schedule.start_epoch < intent["start_epoch"] + intent["duration_seconds"]
                )
            )
            for schedule in manager.normalized_schedules
        ) if manager is not None else False
        before = None
        after = {
            **intent,
            "end_epoch": intent["start_epoch"] + intent["duration_seconds"],
            "origin": ScheduleOrigin.LOCAL_CREATED.value,
        }
    elif operation is ControlOperation.PLANNER_APPLY:
        schedules = intent.get("schedules")
        if set(intent) != {"schedules"} or not isinstance(schedules, list):
            raise vol.Invalid("Planner apply preview requires schedules")
        before = _schedule_snapshot(runtime)
        protected_schedules = [schedule for schedule in before if schedule["protected"]]
        protected_schedule_conflict = any(
            (
                proposed["start_epoch"] < protected["end_epoch"]
                and protected["start_epoch"]
                < proposed["start_epoch"] + proposed["duration_seconds"]
            )
            or (
                proposed.get("m_id") is not None
                and proposed.get("m_id") == protected.get("m_id")
            )
            for proposed in schedules
            for protected in protected_schedules
        )
        after = [*protected_schedules, *schedules]
    elif operation is ControlOperation.RECOVERY_APPLY:
        transaction_id = intent.get("transaction_id")
        if (
            target
            or set(intent) != {"transaction_id"}
            or not isinstance(transaction_id, str)
        ):
            raise vol.Invalid("Recovery apply preview requires one transaction_id")
        journal = runtime.control_journal
        transaction = journal.transaction(transaction_id) if journal is not None else None
        if transaction is None or transaction.phase is not JournalPhase.RECOVERY_REQUIRED:
            raise vol.Invalid("The recovery transaction is not pending")
        if transaction.operation != ControlOperation.CREATE.value:
            m_id = transaction.target_identity.m_id
            ownership = getattr(runtime, "scheduler_ownership", None)
            record = (
                ownership.get(m_id)
                if ownership is not None and m_id is not None
                else None
            )
            if record is not None:
                origin = ScheduleOrigin.LOCAL_CREATED
            else:
                sch_index = transaction.target_identity.sch_index
                matches = (
                    [
                        schedule
                        for schedule in manager.normalized_schedules
                        if schedule.sch_index == sch_index
                    ]
                    if manager is not None and sch_index is not None
                    else []
                )
                origin = (
                    {
                        Ownership.INTEGRATION_OWNED: ScheduleOrigin.LOCAL_CREATED,
                        Ownership.EXTERNAL: ScheduleOrigin.EXTERNAL_OBSERVED,
                        Ownership.UNKNOWN: ScheduleOrigin.UNKNOWN,
                    }[matches[0].ownership.ownership]
                    if len(matches) == 1
                    else ScheduleOrigin.UNKNOWN
                )
        authoritative_target = transaction.target_identity.as_dict()
        before = transaction.as_dict()
        after = {
            **before,
            "recovery_evidence": {
                "status": "blocked",
                "reason": "production_recovery_intent_not_observable",
            },
        }
        recovery_evidence_missing = True
        exact_target = True
    else:
        raise vol.Invalid("The production panel operation is unsupported")

    context = replace(
        runtime.scheduler_control_context(operation),
        requested_service=canonical_service,
        origin=origin,
        initiative=Initiative.MANUAL,
        adapter_capability=(
            AdapterCapability.UNSUPPORTED
            if recovery_evidence_missing
            else runtime.scheduler_control_context(operation).adapter_capability
        ),
        operation_impact=replace(
            runtime.operation_impact(),
            exact_target=exact_target,
            external_conflict=protected_schedule_conflict,
        ),
        grant_authorized=False,
        automation_authorized=False,
    )
    decision = evaluate_control_operation(context)
    authority = getattr(runtime, "production_authority", None)
    production_control_enabled = getattr(
        runtime, "production_control_enabled", lambda: False
    )()
    scheduler_evidence = (
        manager.list_visibility_diagnostics() if manager is not None else {}
    )
    payload = {
        "entry_id": runtime.entry.entry_id if runtime.entry is not None else None,
        "gateway_serial": runtime.serial,
        "runtime_mode": "production",
        "operation": operation.value,
        "service": canonical_service,
        "target": authoritative_target,
        "intent": intent,
        "before": before,
        "after": after,
        "scheduler_snapshot": _schedule_snapshot(runtime),
        "scheduler_evidence": scheduler_evidence,
        "scheduler_evidence_binding": _scheduler_evidence_binding(scheduler_evidence),
        "authority_revision": getattr(authority, "revision", None),
        "production_control_enabled": production_control_enabled,
        "journal_revision": (
            runtime.control_journal.revision if runtime.control_journal is not None else None
        ),
        "blocked_reasons": list(decision.blocked_reasons),
    }
    fingerprint = _fingerprint(payload)
    preview_id = fingerprint[:16]
    m_id_ready = (
        operation not in {ControlOperation.UPDATE, ControlOperation.DELETE}
        or isinstance(authoritative_target.get("m_id"), int)
    )
    can_apply = (
        m_id_ready
        and production_control_enabled
        and not decision.blocked_reasons
        and operation
        in {
            ControlOperation.CREATE,
            ControlOperation.UPDATE,
            ControlOperation.DELETE,
            ControlOperation.RECOVERY_APPLY,
        }
    )
    if can_apply:
        if len(runtime._production_previews) >= 64:
            runtime._production_previews.clear()
        preview_id = secrets.token_hex(24)
        runtime._production_previews[preview_id] = dict(payload)
    return {
        **payload,
        "preview_id": preview_id,
        "fingerprint": fingerprint,
        "can_apply": can_apply,
        "assumptions": [
            "Preview derived from current sanitized runtime evidence",
            "Apply requires the persisted production authority and current journal revision",
            "The typed scheduler mutation manager performs ownership and readback checks",
        ],
    }


def _exact_simulator_schedule(
    runtime: SimulatorEntryRuntime,
    target: dict[str, Any],
) -> tuple[SimulatedSchedule, JournalTargetIdentity]:
    m_id = target.get("m_id")
    if isinstance(m_id, bool) or not isinstance(m_id, int):
        raise vol.Invalid("A current simulator schedule target is required")
    schedule = runtime.simulator.adapter.schedules.get(m_id)
    if schedule is None:
        raise vol.Invalid("The simulator schedule target is missing")
    authoritative = JournalTargetIdentity(
        m_id=schedule.m_id,
        sch_index=schedule.sch_index,
        service_no=schedule.service_no,
        source_id=schedule.source_id,
        asset_id=runtime.simulator.adapter.battery.asset_id,
    )
    for field_name, expected in authoritative.as_dict().items():
        if field_name in target and target[field_name] != expected:
            raise vol.Invalid(f"The simulator target {field_name} changed")
    return schedule, authoritative


def _schedule_payload(
    intent: dict[str, Any],
    *,
    before: SimulatedSchedule | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if before is not None:
        payload.update(before.as_dict())
    if "start_epoch" in intent:
        payload["start"] = datetime.fromtimestamp(intent["start_epoch"], UTC).isoformat()
    if "duration_seconds" in intent:
        payload["duration_seconds"] = intent["duration_seconds"]
    if "socref" in intent:
        payload["socref"] = _finite_number(float(intent["socref"]))
    return payload


def _simulator_request(
    runtime: SimulatorEntryRuntime,
    operation: ControlOperation,
    service: str | None,
    target: dict[str, Any],
    intent: dict[str, Any],
) -> tuple[AdapterRequest, Any, Any]:
    simulator = runtime.simulator
    battery_identity = JournalTargetIdentity(
        asset_id=simulator.adapter.battery.asset_id,
        service_no=110,
    )
    scheduler_operations = {
        ControlOperation.CREATE,
        ControlOperation.UPDATE,
        ControlOperation.DELETE,
        ControlOperation.REPLACE,
        ControlOperation.STOP,
        ControlOperation.PLANNER_APPLY,
        ControlOperation.RECOVERY_APPLY,
    }
    if operation in scheduler_operations and service not in {None, "targetsoc"}:
        raise vol.Invalid("Simulator scheduler operations require Target SoC service")
    if operation is ControlOperation.DIRECT_TARGET_SOC:
        if (
            set(intent) - {"socref"}
            or "socref" not in intent
            or not isfinite(float(intent["socref"]))
        ):
            raise vol.Invalid("Direct Target SoC requires one finite socref")
        socref = float(intent["socref"])
        if not (
            simulator.adapter.battery.min_soc_percent
            <= socref
            <= simulator.adapter.battery.max_soc_percent
        ):
            raise vol.Invalid("Direct Target SoC is outside simulated battery bounds")
        request = AdapterRequest(operation, "targetsoc", battery_identity, intent)
        state = simulator.snapshot()
        before = {
            "battery": dict(state["battery"]),
            "active_service": state["active_service"],
            "demand_response_power_w": state["demand_response_power_w"],
        }
        after = {
            "battery": {
                **state["battery"],
                "power_w": 0.0,
                "target_soc_percent": intent["socref"],
            },
            "active_service": 107,
            "demand_response_power_w": 0.0,
        }
        return request, before, after
    if operation is ControlOperation.DIRECT_DEMAND_RESPONSE:
        if (
            set(intent) - {"power_w"}
            or "power_w" not in intent
            or not isfinite(float(intent["power_w"]))
        ):
            raise vol.Invalid("Direct Demand Response requires one finite power_w")
        identity = JournalTargetIdentity(
            asset_id=simulator.adapter.battery.asset_id,
            service_no=102,
        )
        request = AdapterRequest(operation, "demandresponse", identity, intent)
        state = simulator.snapshot()
        before = {
            "battery": dict(state["battery"]),
            "active_service": state["active_service"],
            "demand_response_power_w": state["demand_response_power_w"],
        }
        after = {
            "battery": {
                **state["battery"],
                "power_w": intent["power_w"],
                "target_soc_percent": None,
            },
            "active_service": 102,
            "demand_response_power_w": intent["power_w"],
        }
        return request, before, after
    if operation is ControlOperation.STOP:
        if intent:
            raise vol.Invalid("Stop does not accept intent values")
        request = AdapterRequest(operation, "targetsoc", battery_identity)
        return request, {"active_service": simulator.adapter.active_service}, {
            "active_service": 0
        }
    if operation is ControlOperation.CREATE:
        if target.get("asset_id") not in {None, simulator.adapter.battery.asset_id}:
            raise vol.Invalid("Simulator schedule asset identity changed")
        if target.get("service_no") not in {None, 110}:
            raise vol.Invalid("Simulator schedule service identity changed")
        identity = JournalTargetIdentity(
            m_id=target.get("m_id"),
            sch_index=target.get("sch_index"),
            service_no=110,
            source_id=target.get("source_id"),
            asset_id=simulator.adapter.battery.asset_id,
        )
        payload = _schedule_payload(intent)
        if set(intent) - {"start_epoch", "duration_seconds", "socref"}:
            raise vol.Invalid("Create contains unsupported intent values")
        request = AdapterRequest(operation, "targetsoc", identity, payload)
        proposed = simulator.adapter._schedule_from_request(request).as_dict()
        return request, {}, proposed
    if operation in {
        ControlOperation.UPDATE,
        ControlOperation.DELETE,
        ControlOperation.REPLACE,
    }:
        schedule, identity = _exact_simulator_schedule(runtime, target)
        if operation is ControlOperation.DELETE:
            if intent:
                raise vol.Invalid("Delete does not accept intent values")
            request = AdapterRequest(operation, "targetsoc", identity)
            return request, schedule.as_dict(), None
        allowed = {"start_epoch", "duration_seconds", "socref"}
        if not intent or set(intent) - allowed:
            raise vol.Invalid("Update and replace require supported schedule values")
        payload = _schedule_payload(
            intent,
            before=schedule if operation is ControlOperation.REPLACE else None,
        )
        request = AdapterRequest(operation, "targetsoc", identity, payload)
        if operation is ControlOperation.REPLACE:
            proposed = simulator.adapter._schedule_from_request(
                request,
                current=schedule,
            ).as_dict()
        else:
            proposed = simulator.adapter.updated_schedule(request, schedule).as_dict()
        return request, schedule.as_dict(), proposed
    if operation is ControlOperation.PLANNER_APPLY:
        raw_schedules = intent.get("schedules")
        if set(intent) != {"schedules"} or not isinstance(raw_schedules, list):
            raise vol.Invalid("Planner apply requires schedules")
        schedules = []
        for item in raw_schedules:
            schedules.append(
                {
                    **item,
                    "start": datetime.fromtimestamp(item["start_epoch"], UTC).isoformat(),
                }
            )
            schedules[-1].pop("start_epoch")
        request = AdapterRequest(
            operation,
            "targetsoc",
            JournalTargetIdentity(asset_id=simulator.adapter.battery.asset_id),
            {"schedules": schedules},
        )
        planned, external, _ = simulator.adapter.planner_candidate(request)
        after_schedules = [
            schedule.as_dict()
            for schedule in sorted(
                (*external.values(), *planned.values()),
                key=lambda item: item.m_id,
            )
        ]
        return request, simulator.snapshot()["schedules"], after_schedules
    if operation is ControlOperation.RECOVERY_APPLY:
        transaction_id = intent.get("transaction_id")
        if set(intent) != {"transaction_id"} or not isinstance(transaction_id, str):
            raise vol.Invalid("Recovery apply requires one transaction_id")
        transaction = simulator.journal.journal.transaction(transaction_id)
        if transaction is None or transaction.phase is not JournalPhase.RECOVERY_REQUIRED:
            raise vol.Invalid("The recovery transaction is not pending")
        if transaction.operation not in {
            ControlOperation.CREATE.value,
            ControlOperation.UPDATE.value,
            ControlOperation.REPLACE.value,
        }:
            raise vol.Invalid(
                "The recovery transaction operation has no evidence-based recovery path"
            )
        request = AdapterRequest(
            operation,
            "targetsoc",
            transaction.target_identity,
            {"transaction_id": transaction_id},
        )
        evidence, recovery_rejection = simulator.recovery_evidence(request)
        if recovery_rejection is not None:
            return request, transaction.as_dict(), {
                **transaction.as_dict(),
                "recovery_evidence": {
                    "status": "blocked",
                    "reason": recovery_rejection,
                },
            }
        return request, transaction.as_dict(), {
            **transaction.as_dict(),
            "phase": JournalPhase.RECONCILED.value,
            "observed_state": evidence,
        }
    raise vol.Invalid("The simulator operation is unsupported")


def _simulator_preview(
    runtime: SimulatorEntryRuntime,
    *,
    operation: ControlOperation,
    service: str | None,
    target: dict[str, Any],
    intent: dict[str, Any],
) -> dict[str, Any]:
    request_grant = bool(intent.pop("request_grant", False))
    request, before, after = _simulator_request(
        runtime,
        operation,
        service,
        target,
        intent,
    )
    base_context = runtime.simulator.control_context(
        request,
        initiative=Initiative.MANUAL,
        grant_authorized=False,
        automation_authorized=False,
    )
    planner_rejection = None
    if operation is ControlOperation.PLANNER_APPLY:
        _, _, planner_rejection = runtime.simulator.adapter.planner_candidate(request)
    recovery_rejection = (
        runtime.simulator.recovery_evidence(request)[1]
        if operation is ControlOperation.RECOVERY_APPLY
        else None
    )
    context = replace(
        base_context,
        adapter_capability=(
            runtime.simulator.adapter.capability(operation, request.service)
            if planner_rejection is None and recovery_rejection is None
            else AdapterCapability.UNSUPPORTED
        ),
        operation_impact=replace(
            base_context.operation_impact,
            external_conflict=planner_rejection
            in {
                "planner_cannot_replace_external_schedule",
                "planner_external_schedule_conflict",
            },
        ),
    )
    decision = evaluate_control_operation(context)
    state_fingerprint = runtime.simulator.state_fingerprint()
    preview_revision = str(runtime.simulator.journal.journal.revision)
    payload = {
        "entry_id": runtime.entry.entry_id,
        "gateway_serial": runtime.serial,
        "runtime_mode": "simulator",
        "operation": operation.value,
        "service": request.service,
        "target": request.target_identity.as_dict() if request.target_identity else {},
        "intent": dict(request.payload),
        "before": before,
        "after": after,
        "state_fingerprint": state_fingerprint,
        "journal_revision": runtime.simulator.journal.journal.revision,
    }
    fingerprint = _fingerprint(payload)
    target_identity = json.dumps(
        payload["target"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    grant_scope = {
        "config_entry_id": runtime.entry.entry_id,
        "gateway_serial": runtime.serial,
        "operation": operation,
        "target_identity": target_identity,
        "fingerprint": fingerprint,
        "preview_revision": preview_revision,
    }
    grant_id: str | None = None
    if request_grant and "manual_grant_required" in decision.blocked_reasons:
        grant = runtime.grant_registry.issue(**grant_scope)
        grant_id = grant.grant_id
        decision = evaluate_control_operation(replace(context, grant_authorized=True))
    payload["blocked_reasons"] = list(decision.blocked_reasons)
    preview_id = secrets.token_hex(16)
    runtime.cache_preview(
        preview_id,
        {
            "fingerprint": fingerprint,
            "state_fingerprint": state_fingerprint,
            "request": request,
            "grant_id": grant_id,
            "grant_scope": grant_scope,
        },
    )
    return {
        **payload,
        "preview_id": preview_id,
        "fingerprint": fingerprint,
        "can_apply": decision.permitted,
        "assumptions": [
            "Execution is isolated to this local simulator config entry",
            "Apply consumes this preview exactly once",
            "Any simulator state change invalidates this preview",
        ],
    }


def preview_operation(
    runtime: RuntimeInstance,
    *,
    operation: ControlOperation,
    service: str | None,
    target: dict[str, Any],
    intent: dict[str, Any],
) -> dict[str, Any]:
    """Evaluate an exact-target preview without changing controller state."""
    if isinstance(runtime, SimulatorEntryRuntime):
        return _simulator_preview(
            runtime,
            operation=operation,
            service=service,
            target=target,
            intent=dict(intent),
        )
    return _production_preview(
        runtime,
        operation=operation,
        service=service,
        target=target,
        intent=intent,
    )


def _scheduler_evidence_binding(evidence: dict[str, object]) -> dict[str, object]:
    """Return stable scheduler evidence fields for preview/apply binding."""
    return {
        key: evidence.get(key)
        for key in (
            "invalidated",
            "from_epoch",
            "to_epoch",
            "last_successful_at",
        )
    }


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@websocket_command({vol.Required("type"): f"{DOMAIN}/panel/state"})
@require_admin
@async_response
async def websocket_panel_state(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Refresh evidence and return state to an administrator."""
    connection.send_result(msg["id"], await async_panel_state(hass))


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/panel/preview",
        vol.Required("entry_id"): str,
        vol.Required("operation"): _OPERATION_SCHEMA,
        vol.Optional("service"): vol.Any(None, str),
        vol.Optional("target", default=dict): _TARGET_SCHEMA,
        vol.Optional("intent", default=dict): _INTENT_SCHEMA,
    }
)
@require_admin
@async_response
async def websocket_panel_preview(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return a deterministic operation preview with no side effects."""
    runtime = _runtime_for_entry(hass, msg["entry_id"])
    if isinstance(runtime, RuntimeData) and msg["operation"] == "create":
        manager = runtime.scheduler_manager
        intent = msg["intent"]
        if manager is None or not {"start_epoch", "duration_seconds"} <= set(intent):
            connection.send_error(msg["id"], "preview_failed", "scheduler_unavailable")
            return
        start, duration = intent["start_epoch"], intent["duration_seconds"]
        if not int(time()) < start <= 2_147_483_647 or not 0 < duration <= _PANEL_FUTURE_SECONDS:
            connection.send_error(msg["id"], "preview_failed", "scheduler_create_values_invalid")
            return
        try:
            entries = await manager.refresh_list(start - 1, start + duration + 1)
            for item in entries[:_PANEL_DETAIL_LIMIT]:
                await manager.refresh_detail(item.sch_index)
        except Exception:  # noqa: BLE001 - no writes on failed visibility
            connection.send_error(msg["id"], "preview_failed", "scheduler_visibility_stale")
            return
    result = preview_operation(
        runtime,
        operation=ControlOperation(msg["operation"]),
        service=msg.get("service"),
        target=dict(msg["target"]),
        intent=dict(msg["intent"]),
    )
    if isinstance(runtime, RuntimeData) and result["can_apply"]:
        runtime._production_previews[result["preview_id"]].update(
            user_id=connection.user.id, expires_at=time() + 120,
        )
    connection.send_result(msg["id"], result)


async def apply_simulator_preview(
    runtime: SimulatorEntryRuntime,
    preview_id: str,
) -> dict[str, Any]:
    """Consume and apply one preview, rejecting stale or replayed tokens."""
    preview_status, result = await runtime.async_apply_preview(preview_id)
    if preview_status == "missing":
        return {
            "status": "blocked",
            "preview_id": preview_id,
            "can_apply": False,
            "blocked_reasons": ["preview_missing_or_consumed"],
        }
    if preview_status == "stale":
        return {
            "status": "blocked",
            "preview_id": preview_id,
            "can_apply": False,
            "blocked_reasons": ["simulator_state_changed"],
        }
    if result is None:
        raise RuntimeError("simulator preview execution returned no result")
    return {
        "status": result.status.value,
        "preview_id": preview_id,
        "can_apply": False,
        "transaction_id": result.transaction_id,
        "reason": result.reason,
        "blocked_reasons": list(result.decision.blocked_reasons),
        "adapter_status": (
            result.adapter_result.status.value
            if result.adapter_result is not None
            else None
        ),
        "state": _simulator_state(runtime.entry.entry_id, runtime),
    }


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/panel/apply",
        vol.Required("entry_id"): str,
        vol.Required("preview_id"): vol.All(str, vol.Length(min=16, max=64)),
    }
)
@require_admin
@async_response
async def websocket_panel_apply(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Apply one fresh preview through the correct runtime boundary."""
    runtime = _runtime_for_entry(hass, msg["entry_id"])
    preview_id = msg["preview_id"]
    if isinstance(runtime, RuntimeData):
        try:
            result = await runtime.async_apply_production_preview(
                preview_id,
                connection.user.id,
            )
        except Exception as err:  # noqa: BLE001 - expose sanitized service failure
            connection.send_result(
                msg["id"],
                {
                    "status": "blocked",
                    "preview_id": preview_id,
                    "can_apply": False,
                    "blocked_reasons": [str(err)],
                },
            )
            return
        connection.send_result(msg["id"], result)
        return
    connection.send_result(
        msg["id"],
        await apply_simulator_preview(runtime, preview_id),
    )


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/panel/recover",
        vol.Required("entry_id"): str,
        vol.Required("transaction_id"): vol.All(str, vol.Length(min=1, max=128)),
    }
)
@require_admin
@async_response
async def websocket_panel_recover(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any],
) -> None:
    """Read back a pending CREATE or DELETE without sending anything again."""
    runtime = _runtime_for_entry(hass, msg["entry_id"])
    if not isinstance(runtime, RuntimeData):
        connection.send_error(msg["id"], "unavailable", "scheduler_unavailable")
        return
    journal = runtime.control_journal
    transaction = journal.transaction(msg["transaction_id"]) if journal else None
    manager = runtime.scheduler_mutation_manager
    if (
        transaction is None or transaction.phase is not JournalPhase.RECOVERY_REQUIRED
        or transaction.operation
        not in {ControlOperation.CREATE.value, ControlOperation.DELETE.value}
        or transaction.target_identity.m_id is None or manager is None
    ):
        connection.send_error(msg["id"], "recovery_pending", "recovery_target_invalid")
        return
    try:
        if transaction.operation == ControlOperation.DELETE.value:
            # Same read-only post-check the price controller runs on its own.
            controller = runtime.price_optimization_controller
            if controller is None:
                raise RuntimeError("price_optimization_unavailable")
            await controller.check_unconfirmed_deletes()
            settled = journal is not None and all(
                item.transaction_id != transaction.transaction_id
                for item in (runtime.control_journal or journal).unresolved_transactions
            )
        else:
            result = await manager.recover_provisional(transaction.target_identity.m_id)
            settled = result.status.value == "success"
    except Exception:  # noqa: BLE001 - keep uncertain outcomes pending
        connection.send_error(msg["id"], "recovery_pending", "recovery_readback_unavailable")
        return
    if settled:
        try:
            await runtime.async_resume_after_recovery()
        except Exception:  # noqa: BLE001 - the switches can still be turned on by hand
            pass
    connection.send_result(msg["id"], {
        "status": "reconciled" if settled else "recovery_required",
        "transaction_id": transaction.transaction_id,
    })


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/panel/scenario",
        vol.Required("entry_id"): str,
        vol.Required("scenario"): vol.In(SIMULATOR_SCENARIOS),
    }
)
@require_admin
@async_response
async def websocket_panel_scenario(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    runtime = _runtime_for_entry(hass, msg["entry_id"])
    if not isinstance(runtime, SimulatorEntryRuntime):
        raise vol.Invalid("Scenarios are available only for simulator entries")
    await runtime.async_load_scenario(msg["scenario"])
    connection.send_result(msg["id"], _simulator_state(msg["entry_id"], runtime))


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/panel/advance",
        vol.Required("entry_id"): str,
        vol.Required("seconds"): vol.All(int, vol.Range(min=1, max=7 * 24 * 60 * 60)),
    }
)
@require_admin
@async_response
async def websocket_panel_advance(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    runtime = _runtime_for_entry(hass, msg["entry_id"])
    if not isinstance(runtime, SimulatorEntryRuntime):
        raise vol.Invalid("Clock advance is available only for simulator entries")
    await runtime.async_advance(msg["seconds"])
    connection.send_result(msg["id"], _simulator_state(msg["entry_id"], runtime))


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/panel/reset",
        vol.Required("entry_id"): str,
    }
)
@require_admin
@async_response
async def websocket_panel_reset(
    hass: HomeAssistant,
    connection: ActiveConnection,
    msg: dict[str, Any],
) -> None:
    runtime = _runtime_for_entry(hass, msg["entry_id"])
    if not isinstance(runtime, SimulatorEntryRuntime):
        raise vol.Invalid("Reset is available only for simulator entries")
    await runtime.async_reset()
    connection.send_result(msg["id"], _simulator_state(msg["entry_id"], runtime))


async def async_register_control_api(hass: HomeAssistant) -> None:
    """Register the optional Lovelace card module and authenticated API only."""
    if hass.data.get(_PANEL_REGISTERED):
        return
    module_path = Path(__file__).parent / "frontend" / "pixiihome-lovelace.js"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(PANEL_MODULE_URL, str(module_path), cache_headers=False)]
    )
    for command in (
        websocket_panel_state,
        websocket_panel_preview,
        websocket_panel_apply,
        websocket_panel_recover,
        websocket_panel_scenario,
        websocket_panel_advance,
        websocket_panel_reset,
        websocket_gateway_scheduler_preview,
        websocket_gateway_scheduler_apply,
        websocket_gateway_scheduler_recover,
    ):
        websocket_api.async_register_command(hass, command)
    hass.data[_PANEL_REGISTERED] = True


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/gateway_scheduler/preview",
        vol.Required("entry_id"): str,
        vol.Required("sch_index"): vol.All(int, vol.Range(min=0, max=2_147_483_647)),
        vol.Required("operation"): vol.In(("update", "delete")),
        vol.Optional("changes", default=dict): vol.Schema({
            vol.Optional("socref"): vol.Any(int, float),
            vol.Optional("start_time"): vol.All(int, vol.Range(min=1, max=2_147_483_647)),
            vol.Optional("duration"): vol.All(int, vol.Range(min=1, max=2_147_483_647)),
        }),
    }
)
@require_admin
@async_response
async def websocket_gateway_scheduler_preview(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any],
) -> None:
    """Prepare explicit authority over one observed Gateway schedule."""
    runtime = _runtime_for_entry(hass, msg["entry_id"])
    controller = runtime.gateway_scheduler if isinstance(runtime, RuntimeData) else None
    if controller is None:
        connection.send_error(msg["id"], "unavailable", "Gateway scheduler is unavailable")
        return
    try:
        result = await controller.preview(
            msg["sch_index"], msg["operation"], dict(msg["changes"]), connection.user.id,
        )
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "preview_failed", str(err))
        return
    except SchedulerAuthenticationError as err:
        connection.send_error(
            msg["id"], "preview_failed",
            f"Scheduler authentication: {err.reason.value} ({err.stage.value})",
        )
        return
    except SchedulerProtocolError as err:
        connection.send_error(msg["id"], "preview_failed", str(err))
        return
    except Exception as err:  # noqa: BLE001 - do not expose transport or authentication data
        _LOGGER.warning("Gateway schedule preview failed (%s)", type(err).__name__)
        connection.send_error(
            msg["id"], "preview_failed", "Refresh schedule evidence and try again",
        )
        return
    connection.send_result(msg["id"], result)


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/gateway_scheduler/recover",
        vol.Required("entry_id"): str,
        vol.Required("transaction_id"): vol.All(str, vol.Length(min=1, max=128)),
    }
)
@require_admin
@async_response
async def websocket_gateway_scheduler_recover(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any],
) -> None:
    """Recheck one pending Gateway operation without resending a write."""
    runtime = _runtime_for_entry(hass, msg["entry_id"])
    controller = runtime.gateway_scheduler if isinstance(runtime, RuntimeData) else None
    if controller is None:
        connection.send_error(msg["id"], "unavailable", "Gateway scheduler is unavailable")
        return
    try:
        result = await controller.recover(msg["transaction_id"], connection.user.id)
    except Exception:  # noqa: BLE001 - leave durable recovery pending on uncertain reads
        connection.send_error(
            msg["id"], "recovery_pending", "Readback unavailable; recovery remains pending",
        )
        return
    connection.send_result(msg["id"], result)


@websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/gateway_scheduler/apply",
        vol.Required("entry_id"): str,
        vol.Required("preview_id"): vol.All(str, vol.Length(min=48, max=48)),
        vol.Required("confirm_external_target"): vol.Equal(True),
    }
)
@require_admin
@async_response
async def websocket_gateway_scheduler_apply(
    hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any],
) -> None:
    """Apply a user-bound one-use approval, including for aggregator schedules."""
    runtime = _runtime_for_entry(hass, msg["entry_id"])
    controller = runtime.gateway_scheduler if isinstance(runtime, RuntimeData) else None
    if controller is None:
        connection.send_error(msg["id"], "unavailable", "Gateway scheduler is unavailable")
        return
    try:
        result = await controller.apply(msg["preview_id"], connection.user.id)
    except HomeAssistantError as err:
        connection.send_result(msg["id"], {
            "status": "blocked", "can_apply": False,
            "blocked_reasons": str(err).split(", "),
        })
        return
    except Exception:  # noqa: BLE001 - return a sanitized boundary failure
        connection.send_error(
            msg["id"], "apply_failed", "Approval expired or state changed; create a new preview",
        )
        return
    connection.send_result(msg["id"], result)




__all__ = [
    "apply_simulator_preview",
    "panel_state",
    "preview_operation",
    "websocket_panel_advance",
    "websocket_panel_apply",
    "websocket_panel_preview",
    "websocket_panel_reset",
    "websocket_panel_scenario",
    "websocket_panel_state",
]
