"""Recover only the archived finite validation; never erase the failed stop."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.pixiihome_controller import price_stop_recovery as recovery
from custom_components.pixiihome_controller.allocator import MessageIdAllocator
from custom_components.pixiihome_controller.control.journal import (
    ControlJournal,
    JournalPhase,
    JournalTransaction,
)
from custom_components.pixiihome_controller.control.settings import (
    ControllerState,
    OperationalSettings,
)
from custom_components.pixiihome_controller.protocol.mqtt import parse_ack
from custom_components.pixiihome_controller.scheduler.ownership import (
    OwnershipLifecycle,
    SchedulerOwnershipStore,
)


def historical_case():
    folder = Path(__file__).parents[1] / "tests/fixtures/price_stop"
    raw = json.loads((folder / "recovery_journal.json").read_text())
    # The portable report normalized JSON numbers (10.0 -> 10). Its embedded
    # storage checksum cannot authenticate those reformatted bytes. Construct
    # a test projection of the captured fields, never bypass live validation:
    # production uses the runtime's already verified ControlJournal instead.
    journal = ControlJournal(
        config_entry_id=raw["config_entry_id"], gateway_serial=raw["gateway_serial"],
        revision=raw["revision"], command_state=raw["command_state"],
        ownership_state=raw["ownership_state"], controller_state=raw["controller_state"],
        planner_state=raw["planner_state"], replacement_state=raw["replacement_state"],
        audit_history=tuple(raw["audit_history"]),
        transactions=tuple(JournalTransaction.from_mapping(t) for t in raw["transactions"]),
    )
    evidence = json.loads((folder / "recovery_evidence.json").read_text())
    return journal, evidence


def fake_runtime():
    journal, evidence = historical_case()
    ownership = SchedulerOwnershipStore.from_state(journal.ownership_state,
        config_entry_id=journal.config_entry_id, gateway_serial=journal.gateway_serial)
    executor = SimpleNamespace(execute_get=AsyncMock(side_effect=TimeoutError()),
                               execute_mutation=AsyncMock())
    settings = OperationalSettings.defaults().set("price_schedule_writes_enabled", False)
    instance = SimpleNamespace(
        control_journal=journal, scheduler_ownership=ownership,
        scheduler_mutation_manager=SimpleNamespace(executor=executor),
        command_manager=SimpleNamespace(allocator=MessageIdAllocator.from_state(journal.command_state)),
        scheduler_coordinator=SimpleNamespace(async_refresh=AsyncMock(return_value=
            SimpleNamespace(data_status="current", schedules=[]))),
        production_control_enabled=lambda: False,
        controller_state=ControllerState(), operational_settings=settings,
        plant_health=lambda: SimpleNamespace(healthy=True),
        telemetry=SimpleNamespace(is_fresh=lambda *args, **kwargs: True,
                                  value=lambda family, key: 0),
        _control_journal_lock=asyncio.Lock(),
        _save_control_journal_verified=AsyncMock(),
    )
    return instance, evidence


def test_anonymized_evidence_proposes_reservation_without_rewriting_history():
    journal, evidence = historical_case()
    old_snapshot = journal.snapshot()
    plan = recovery.build_price_recovery_plan(journal, evidence, now=datetime.now(UTC))
    assert plan.reserved.lifecycle is OwnershipLifecycle.EXPIRED_RESERVED
    assert plan.original.lifecycle is OwnershipLifecycle.CONFLICTED
    assert plan.reserved.intended_state == plan.original.intended_state
    assert plan.reserved.reservation_until == "2026-10-18T21:11:54.823317+00:00"
    assert plan.summary()["absence_confirmed"] is False
    assert plan.summary()["identity_released"] is False
    assert journal.snapshot() == old_snapshot


@pytest.mark.parametrize("path,value", [
    (("config_entry_id",), "other"),
    (("gateway_serial",), "other"),
    (("m_id",), 1000000000005),
    (("create_transaction_id",), "missing"),
    (("delete_transaction_id",), "missing"),
    (("get_before", "topic"), "pixii/ack/other"),
    (("delete_ack", "topic"), "pixii/ack/other"),
    (("get_before", "payload", "m_id"), 1000000000005),
    (("get_before", "payload", "method"), "set"),
    (("get_before", "payload", "values", "duration"), 40),
    (("get_before", "payload", "values", "service_duration_left"), None),
    (("get_before", "payload", "values", "service_duration_left"), 0),
    (("get_before", "payload", "values", "service_status"), 0),
    (("delete_ack", "payload", "m_id"), 1000000000005),
    (("delete_ack", "payload", "method"), "set"),
    (("delete_ack", "payload", "response"), 11),
    (("delete_ack", "payload", "service"), "targetsoc"),
    (("get_before", "at"), "2026-10-18T19:09:53Z"),
    (("delete_ack", "at"), "2026-10-18T19:11:55Z"),
    (("delete_ack", "at"), "2026-10-18T19:10:00Z"),
    (("delete_ack", "at"), "2026-10-18T19:10:03"),
    (("early_idle_samples",), []),
    (("early_idle_samples", 0, "payload", "ac_w"), -100),
    (("early_idle_samples", 0, "payload", "grid_status"), 0),
    (("early_idle_samples", 0, "payload", "srv_type"), 102),
    (("early_idle_samples", 0, "topic"), "pixii/status/other/core"),
    (("early_idle_samples", 0, "at"), "2026-10-18T19:10:03Z"),
    (("early_idle_samples", 1, "at"), "2026-10-18T19:10:30Z"),
])
def test_incomplete_wrong_or_contradictory_historical_evidence_cannot_recover(path, value):
    journal, evidence = historical_case()
    node = evidence
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises((ValueError, KeyError, TypeError)):
        recovery.build_price_recovery_plan(journal, evidence, now=datetime.now(UTC))


@pytest.mark.parametrize("case", ["allocator", "create_failed", "delete_not_failed", "owner",
                                  "later_write", "pending", "unbounded", "not_expired"])
def test_recovery_requires_exact_terminal_journal_and_elapsed_native_time(case):
    journal, evidence = historical_case()
    now = datetime.now(UTC)
    if case == "allocator":
        journal = journal.with_states(command_state={"next_m_id": 1000000000007,
                                                     "owned_m_ids": []})
    elif case in {"create_failed", "delete_not_failed", "later_write", "pending"}:
        txs = list(journal.transactions)
        index = next(i for i, t in enumerate(txs) if t.transaction_id == evidence[
            "create_transaction_id" if case == "create_failed" else "delete_transaction_id"])
        if case == "later_write":
            txs.append(replace(txs[index], transaction_id="later", operation="update"))
        else:
            txs[index] = replace(txs[index], phase=JournalPhase.FAILED if case == "create_failed"
                                 else JournalPhase.PREPARED if case == "pending"
                                 else JournalPhase.RECONCILED)
        journal = replace(journal, transactions=tuple(txs))
    elif case in {"owner", "unbounded"}:
        state = deepcopy(dict(journal.ownership_state))
        item = next(r for r in state["records"] if r["m_id"] == evidence["m_id"])
        if case == "owner":
            item["owner"] = "external"
        else:
            item["intended_state"]["duration"] = 0
        journal = journal.with_states(ownership_state=state)
    else:
        now = datetime(2026, 10, 18, 21, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        recovery.build_price_recovery_plan(journal, evidence, now=now)


@pytest.mark.asyncio
@pytest.mark.parametrize("dry_run", [True, False])
async def test_recovery_reads_only_and_preserves_failed_transactions_and_allocator(
    monkeypatch, dry_run,
):
    instance, evidence = fake_runtime()
    monkeypatch.setattr(recovery, "verify_price_stopped", AsyncMock(return_value=True))
    original = instance.control_journal
    allocator = instance.command_manager.allocator.snapshot()
    result = await recovery.recover_expired_price_validation(instance, evidence, dry_run=dry_run)
    assert result["status"] == ("validated" if dry_run else "reserved")
    instance.scheduler_mutation_manager.executor.execute_mutation.assert_not_called()
    assert instance.command_manager.allocator.snapshot() == allocator
    assert instance.control_journal.transactions == original.transactions
    assert instance.control_journal.transaction(evidence["delete_transaction_id"]).phase \
        is JournalPhase.FAILED
    assert instance.control_journal.revision == original.revision + int(not dry_run)
    assert instance._save_control_journal_verified.await_count == int(not dry_run)
    if not dry_run:
        restored = ControlJournal.from_state(instance.control_journal.snapshot(),
            config_entry_id=original.config_entry_id, gateway_serial=original.gateway_serial)
        state = SchedulerOwnershipStore.from_state(restored.ownership_state,
            config_entry_id=original.config_entry_id, gateway_serial=original.gateway_serial)
        assert state.records[evidence["m_id"]].lifecycle is OwnershipLifecycle.EXPIRED_RESERVED
        assert restored.audit_history[-1]["identity_released"] is False
        assert restored.audit_history[-1]["historical_failed_transaction_unchanged"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["switch", "export_switch", "authority", "unhealthy", "busy",
                                  "stale", "no_idle", "get_error", "still_present",
                                  "inventory", "revision_changed", "late_switch", "save_error"])
async def test_recovery_fails_closed_without_commands_or_in_memory_transition(monkeypatch, case):
    instance, evidence = fake_runtime()
    original = instance.control_journal
    ownership = instance.scheduler_ownership.snapshot()
    if case == "switch":
        instance.controller_state = ControllerState(load_balancing_enabled=True)
    elif case == "export_switch":
        instance.operational_settings = instance.operational_settings.set(
            "price_export_allowed", True)
    elif case == "authority":
        instance.production_control_enabled = lambda: True
    elif case == "unhealthy":
        instance.plant_health = lambda: SimpleNamespace(healthy=False)
    elif case == "busy":
        instance.telemetry.value = lambda family, key: 107
    elif case == "stale":
        instance.telemetry.is_fresh = lambda *args, **kwargs: False
    elif case == "get_error":
        instance.scheduler_mutation_manager.executor.execute_get.side_effect = RuntimeError("lost")
    elif case == "still_present":
        instance.scheduler_mutation_manager.executor.execute_get = AsyncMock(
            return_value=parse_ack(evidence["get_before"]["payload"]))
    elif case == "inventory":
        instance.scheduler_coordinator.async_refresh.return_value = SimpleNamespace(
            data_status="stale", schedules=[])
    elif case == "save_error":
        instance._save_control_journal_verified.side_effect = OSError("atomic save failed")

    async def verify(*args):
        if case == "revision_changed":
            instance.control_journal = original.with_states()
        elif case == "late_switch":
            instance.controller_state = ControllerState(automatic_price_optimization_enabled=True)
        return case != "no_idle"

    monkeypatch.setattr(recovery, "verify_price_stopped", verify)
    with pytest.raises((ValueError, RuntimeError, OSError)):
        await recovery.recover_expired_price_validation(instance, evidence, dry_run=False)
    assert instance.scheduler_ownership.snapshot() == ownership
    assert instance.control_journal.transactions == original.transactions
    instance.scheduler_mutation_manager.executor.execute_mutation.assert_not_called()


@pytest.mark.asyncio
async def test_service_requires_admin_and_defaults_to_dry_run(monkeypatch):
    import custom_components.pixiihome_controller as integration

    instance, evidence = fake_runtime()
    selected = AsyncMock(return_value={"status": "validated"})
    monkeypatch.setattr(recovery, "recover_expired_price_validation", selected)
    monkeypatch.setattr(integration, "_select_runtime", lambda call: instance)
    admin = AsyncMock()
    monkeypatch.setattr(integration, "_async_require_admin", admin)
    call = SimpleNamespace(data={"evidence": evidence})
    result = await integration._async_recover_expired_price_validation(call)
    assert result == {"status": "validated"}
    admin.assert_awaited_once_with(call)
    selected.assert_awaited_once_with(instance, evidence, dry_run=True)
    admin.side_effect = PermissionError("not admin")
    with pytest.raises(PermissionError):
        await integration._async_recover_expired_price_validation(call)
    assert selected.await_count == 1
