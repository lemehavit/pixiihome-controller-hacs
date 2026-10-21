"""Diagnostics for the read-only Pixiihome Controller integration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, VERSION
from .control.journal import JOURNAL_SCHEMA_VERSION, JournalPhase
from .simulator_entry import SimulatorEntryRuntime

_TO_REDACT = {
    "ca_filename",
    "entry_id",
    "payload",
    "payloads",
    "path",
    "received_at",
    "serial",
    "topic",
    "topics",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return safe runtime diagnostics without raw MQTT payloads or credentials."""
    runtime = hass.data[DOMAIN][entry.entry_id]
    if isinstance(runtime, SimulatorEntryRuntime):
        snapshot = runtime.simulator.snapshot()
        journal = runtime.simulator.journal.journal
        diagnostics = {
            "integration_version": VERSION,
            "runtime_mode": "simulator",
            "config": {
                "entry_id": entry.entry_id,
                "serial": runtime.serial,
            },
            "read_only": {
                "live_writes_enabled": False,
                "infrastructure_status": "isolated_local_simulator",
                "blocked_reasons": [],
            },
            "simulator": {
                "clock": snapshot["clock"],
                "battery": snapshot["battery"],
                "active_service": snapshot["active_service"],
                "faults": snapshot["faults"],
                "schedule_count": len(snapshot["schedules"]),
                "history_count": snapshot["history_count"],
            },
            "journal": {
                "schema_version": JOURNAL_SCHEMA_VERSION,
                "revision": journal.revision,
                "transaction_count": len(journal.transactions),
                "unresolved_transaction_count": sum(
                    transaction.phase
                    not in {JournalPhase.RECONCILED, JournalPhase.FAILED}
                    for transaction in journal.transactions
                ),
                "replacement_state": dict(journal.replacement_state),
                "audit_history": [dict(item) for item in journal.audit_history],
            },
        }
        return async_redact_data(diagnostics, _TO_REDACT)
    now = datetime.now(UTC)
    families = sorted(runtime.telemetry.payloads)
    battery = runtime.battery
    manager = getattr(runtime, "command_manager", None)
    command_diagnostics = (
        manager.diagnostics()
        if manager is not None
        else {
            "ready": False,
            "write_gate_enabled": False,
            "control_infrastructure_status": "partially_live_validated",
            "read_only_command_path_status": "live_validated",
            "state_changing_command_path_status": "not_live_validated",
            "control_block_reasons": ["command_manager_unavailable"],
            "pending_command_count": 0,
            "last_sanitized_result": None,
            "last_error": None,
        }
    )
    scheduler_manager = getattr(runtime, "scheduler_manager", None)
    scheduler_tls_state = getattr(
        runtime,
        "scheduler_tls_state",
        {
            "tls_mode": "system_default",
            "custom_ca_configured": False,
            "x509_strict_enabled": True,
            "cert_required": True,
            "hostname_verification": True,
        },
    )
    scheduler_diagnostics = (
        scheduler_manager.diagnostics()
        if scheduler_manager is not None
        else {
            "status": "read_only_boundary_only",
            "client_boundary": "not_configured",
            "mutation_operations_enabled": False,
            "client": {
                "transport": "https",
                "configured": False,
                "redirects_allowed": False,
                "retry_count": 0,
                "bootstrap_id_in_memory": False,
                "sessid_in_memory": False,
                **scheduler_tls_state,
            },
            "configuration_error": getattr(runtime, "scheduler_config_error", None),
            "list_entry_count": 0,
            "detail_count": 0,
            "view_count": 0,
            "ownership": {
                "integration_owned": 0,
                "external": 0,
                "unknown_protected": 0,
            },
            "list_refresh_attempts": 0,
            "list_refresh_successes": 0,
            "detail_refresh_attempts": 0,
            "detail_refresh_successes": 0,
            "refresh_failures": 0,
            "last_error": None,
            "last_refresh_at": None,
            "list_visibility": {
                "fresh": False,
                "age_seconds": None,
                "invalidated": True,
                "last_successful_at": None,
                "from_epoch": None,
                "to_epoch": None,
            },
        }
    )

    mutation_diagnostics_provider = getattr(runtime, "scheduler_mutation_diagnostics", None)
    scheduler_mutation_diagnostics = (
        mutation_diagnostics_provider()
        if callable(mutation_diagnostics_provider)
        else {
            "status": "unavailable",
            "live_writes_enabled": False,
            "manager_constructed": False,
            "executor": None,
            "ownership_counts": {
                "provisional": 0,
                "owned": 0,
                "conflicted": 0,
                "orphaned": 0,
            },
            "transaction_manager": None,
            "validation": {
                "running": False,
                "attempt_consumed": False,
                "last_result": None,
            },
            "production_adapter_wired": False,
        }
    )

    diagnostics = {
        "integration_version": VERSION,
        "config": {
            "entry_id": entry.entry_id,
            "serial": runtime.serial,
        },
        "read_only": {
            "live_writes_enabled": runtime.release_gate.live_writes_enabled,
            "infrastructure_status": runtime.release_gate.infrastructure_status,
            "read_only_command_path_status": command_diagnostics.get(
                "read_only_command_path_status", "live_validated"
            ),
            "state_changing_command_path_status": command_diagnostics.get(
                "state_changing_command_path_status", "not_live_validated"
            ),
            "blocked_reasons": sorted(runtime.release_gate.blocked_reasons),
        },
        "commands": command_diagnostics,
        "scheduler": scheduler_diagnostics,
        "scheduler_mutation": scheduler_mutation_diagnostics,
        "mqtt_telemetry": {
            "messages_seen": runtime.capabilities.messages_seen,
            "families": families,
            "fresh": {family: runtime.telemetry.is_fresh(family, now=now) for family in families},
        },
        "capabilities": runtime.capabilities.snapshot(),
        "battery": {
            "raw_soc": battery.raw_soc,
            "usable_soc": battery.usable_soc,
            "stored_energy_kwh": battery.stored_energy_kwh,
            "power_w": battery.power_w,
            "soh": battery.soh,
            "effective_min_soc": battery.effective_min_soc,
            "effective_max_soc": battery.effective_max_soc,
            "reported_min_soc": battery.reported_min_soc,
            "reported_max_soc": battery.reported_max_soc,
            "usable_soc_source": battery.usable_soc_source,
        },
    }
    return async_redact_data(diagnostics, _TO_REDACT)
