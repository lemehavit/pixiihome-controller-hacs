from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from custom_components.pixiihome_controller.control.journal import (
    JOURNAL_AUDIT_LIMIT,
    JOURNAL_TRANSACTION_LIMIT,
    ControlJournal,
    JournalPhase,
    JournalTargetIdentity,
    JournalTransaction,
)
from custom_components.pixiihome_controller.exceptions import CommandValidationError

SCOPE = {"config_entry_id": "entry-a", "gateway_serial": "GATEWAY"}


def test_structured_target_identity_roundtrips_without_collapsing_fields() -> None:
    identity = JournalTargetIdentity(
        m_id=240000001,
        sch_index=7,
        service_no=110,
        source_id=17,
        asset_id="battery-a",
    )
    journal = ControlJournal.empty(**SCOPE).begin_transaction(
        transaction_id="tx-1",
        operation="create",
        target_identity=identity,
        intent={"socref": 80, "duration_seconds": 900},
    )

    restored = ControlJournal.from_state(journal.snapshot(), **SCOPE)

    transaction = restored.transaction("tx-1")
    assert transaction is not None
    assert transaction.target_identity == identity
    assert transaction.target_identity.m_id == 240000001
    assert transaction.target_identity.sch_index == 7
    assert transaction.target_identity.service_no == 110
    assert transaction.target_identity.source_id == 17
    assert transaction.target_identity.asset_id == "battery-a"
    assert transaction.intent == {"socref": 80, "duration_seconds": 900}


def test_target_identity_validates_each_namespace_independently() -> None:
    with pytest.raises(CommandValidationError):
        JournalTargetIdentity()
    with pytest.raises(CommandValidationError):
        JournalTargetIdentity(m_id=0)
    with pytest.raises(CommandValidationError):
        JournalTargetIdentity(sch_index=-1)
    with pytest.raises(CommandValidationError):
        JournalTargetIdentity(service_no=True)
    with pytest.raises(CommandValidationError):
        JournalTargetIdentity(source_id=-1)


def test_journal_recovers_last_checksummed_backup_after_primary_corruption() -> None:
    original = ControlJournal.migrate_legacy(
        **SCOPE,
        command_state={"next_m_id": 240000002, "owned_m_ids": [240000001]},
        ownership_state={},
    )
    advanced = original.with_states(
        command_state={"next_m_id": 240000003, "owned_m_ids": [240000001]}
    )
    corrupted = advanced.snapshot()
    corrupted["command_state"]["next_m_id"] = 99

    restored = ControlJournal.from_state(corrupted, **SCOPE)

    assert restored.recovered_from_backup
    assert restored.revision == original.revision
    assert restored.command_state == original.command_state


def test_journal_corruption_without_valid_backup_fails_closed() -> None:
    state = ControlJournal.empty(**SCOPE).snapshot()
    state["checksum"] = "invalid"

    with pytest.raises(CommandValidationError, match="checksum"):
        ControlJournal.from_state(state, **SCOPE)


def test_journal_scope_mismatch_and_future_schema_fail_closed() -> None:
    state = ControlJournal.empty(**SCOPE).snapshot()
    with pytest.raises(CommandValidationError, match="scope"):
        ControlJournal.from_state(
            state,
            config_entry_id="entry-b",
            gateway_serial="GATEWAY",
        )

    future = deepcopy(state)
    future["schema_version"] = 999
    with pytest.raises(CommandValidationError, match="unsupported"):
        ControlJournal.from_state(future, **SCOPE)


@pytest.mark.parametrize(
    "interrupted_phase",
    [
        JournalPhase.PREPARED,
        JournalPhase.COMMAND_SENT,
        JournalPhase.ACKNOWLEDGED,
    ],
)
def test_startup_marks_unfinished_transactions_recovery_required(
    interrupted_phase: JournalPhase,
) -> None:
    journal = ControlJournal.empty(**SCOPE).begin_transaction(
        transaction_id="tx-1",
        operation="update",
        target_identity=JournalTargetIdentity(m_id=240000001),
    )
    if interrupted_phase in {JournalPhase.COMMAND_SENT, JournalPhase.ACKNOWLEDGED}:
        journal = journal.transition_transaction("tx-1", JournalPhase.COMMAND_SENT)
    if interrupted_phase is JournalPhase.ACKNOWLEDGED:
        journal = journal.transition_transaction("tx-1", JournalPhase.ACKNOWLEDGED)

    restored = ControlJournal.from_state(journal.snapshot(), **SCOPE)
    recovery = restored.mark_unfinished_recovery_required()
    persisted = ControlJournal.from_state(recovery.snapshot(), **SCOPE)

    transaction = persisted.transaction("tx-1")
    assert transaction is not None
    assert transaction.phase is JournalPhase.RECOVERY_REQUIRED
    assert transaction.reason == "restart_recovery_required"
    assert [item.transaction_id for item in persisted.unresolved_transactions] == ["tx-1"]
    reconciled = persisted.transition_transaction("tx-1", JournalPhase.RECONCILED)
    assert reconciled.unresolved_transactions == ()
    with pytest.raises(CommandValidationError, match="transition"):
        reconciled.transition_transaction("tx-1", JournalPhase.COMMAND_SENT)


def test_schema_two_state_migrates_with_empty_product_fields() -> None:
    current = ControlJournal.empty(**SCOPE).with_states(
        command_state={"next_m_id": 240000001},
        ownership_state={"owned_m_ids": []},
    )
    state = current.snapshot()
    state.pop("controller_state")
    state.pop("planner_state")
    state.pop("replacement_state")
    state.pop("audit_history")
    state["schema_version"] = 2
    core = {key: value for key, value in state.items() if key not in {"checksum", "backups"}}
    payload = json.dumps(core, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    state["checksum"] = hashlib.sha256(payload.encode()).hexdigest()
    state["backups"] = []

    migrated = ControlJournal.from_state(state, **SCOPE)

    assert migrated.migrated_from_legacy
    assert migrated.controller_state == {}
    assert migrated.planner_state == {}
    assert migrated.replacement_state == {}
    assert migrated.audit_history == ()


def test_product_state_roundtrips_and_audit_is_bounded() -> None:
    journal = ControlJournal.empty(**SCOPE).with_product_state(
        controller_state={"runtime_mode": "simulator"},
        planner_state={"interval_seconds": 900},
        replacement_state={"phase": "prepared"},
    )
    for sequence in range(JOURNAL_AUDIT_LIMIT + 5):
        journal = journal.with_product_state(audit_event={"sequence": sequence})

    restored = ControlJournal.from_state(journal.snapshot(), **SCOPE)

    assert restored.controller_state == {"runtime_mode": "simulator"}
    assert restored.planner_state == {"interval_seconds": 900}
    assert restored.replacement_state == {"phase": "prepared"}
    assert len(restored.audit_history) == JOURNAL_AUDIT_LIMIT
    assert restored.audit_history[0]["sequence"] == 5
    assert restored.audit_history[-1]["sequence"] == JOURNAL_AUDIT_LIMIT + 4


def test_reconcile_commits_identity_state_and_terminal_phase_in_one_revision() -> None:
    journal = (
        ControlJournal.empty(**SCOPE)
        .begin_transaction(
            transaction_id="tx-1",
            operation="create",
            target_identity=JournalTargetIdentity(m_id=240000001),
        )
        .transition_transaction("tx-1", JournalPhase.COMMAND_SENT)
        .mark_unfinished_recovery_required()
    )
    starting_revision = journal.revision

    reconciled = journal.reconcile_transaction(
        "tx-1",
        command_state={"next_m_id": 240000002},
        ownership_state={"owned_m_ids": [240000001]},
        reason="observed",
    )

    assert reconciled.revision == starting_revision + 1
    assert reconciled.command_state == {"next_m_id": 240000002}
    assert reconciled.ownership_state == {"owned_m_ids": [240000001]}
    transaction = reconciled.transaction("tx-1")
    assert transaction is not None
    assert transaction.phase is JournalPhase.RECONCILED
    backup = reconciled.backups[-1]
    assert backup["command_state"] == {}
    assert backup["ownership_state"] == {}
    assert backup["transactions"][-1]["phase"] == JournalPhase.RECOVERY_REQUIRED


def test_transaction_retention_keeps_unresolved_and_recent_terminal_records() -> None:
    journal = ControlJournal.empty(**SCOPE).begin_transaction(
        transaction_id="unresolved",
        operation="replace",
        target_identity=JournalTargetIdentity(m_id=240000001),
    )
    for index in range(JOURNAL_TRANSACTION_LIMIT):
        transaction_id = f"terminal-{index}"
        journal = journal.begin_transaction(
            transaction_id=transaction_id,
            operation="create",
            target_identity=JournalTargetIdentity(m_id=240000002 + index),
        ).transition_transaction(transaction_id, JournalPhase.FAILED)

    assert len(journal.transactions) == JOURNAL_TRANSACTION_LIMIT
    assert journal.transaction("unresolved") is not None
    assert journal.transaction("terminal-0") is None
    assert journal.transaction(f"terminal-{JOURNAL_TRANSACTION_LIMIT - 1}") is not None


def test_oversized_restored_transaction_state_fails_closed() -> None:
    transactions = tuple(
        JournalTransaction(
            transaction_id=f"tx-{index}",
            operation="create",
            target_identity=JournalTargetIdentity(m_id=240000001 + index),
            phase=JournalPhase.FAILED,
        )
        for index in range(JOURNAL_TRANSACTION_LIMIT + 1)
    )

    with pytest.raises(CommandValidationError, match="transaction limit"):
        ControlJournal(
            config_entry_id=SCOPE["config_entry_id"],
            gateway_serial=SCOPE["gateway_serial"],
            transactions=transactions,
        )


def test_journal_rejects_nonfinite_json_numbers() -> None:
    with pytest.raises(CommandValidationError, match="non-JSON"):
        ControlJournal.empty(**SCOPE).begin_transaction(
            transaction_id="nonfinite",
            operation="direct_target_soc",
            target_identity=JournalTargetIdentity(asset_id="battery-1"),
            intent={"socref": float("nan")},
        )
