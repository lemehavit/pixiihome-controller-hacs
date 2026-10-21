from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.pixiihome_controller.allocator import MessageIdAllocator
from custom_components.pixiihome_controller.exceptions import (
    CommandValidationError,
    SchedulerProtocolError,
)
from custom_components.pixiihome_controller.protocol.mqtt import parse_ack
from custom_components.pixiihome_controller.protocol.scheduler_commands import (
    ScheduledCreate,
    ScheduledDelete,
    ScheduledUpdate,
    SchedulerAckResult,
    evaluate_scheduler_ack,
)
from custom_components.pixiihome_controller.protocol.scheduler_readback import (
    SchedulerExpectedState,
    SchedulerReconciliationResult,
    SchedulerReconciliationStatus,
    apply_scheduler_update,
    parse_scheduler_get_ack,
    parse_scheduler_starttime,
    reconcile_scheduler_delete,
    reconcile_scheduler_get,
    scheduler_expected_state_from_command,
)
from custom_components.pixiihome_controller.scheduler.ownership import (
    OwnershipLifecycle,
    SchedulerMutationEvidence,
    SchedulerMutationOperation,
    SchedulerOwnershipRecord,
    SchedulerOwnershipStore,
    apply_scheduler_ownership_reconciliation,
    merge_owned_scheduler_update,
)

from .conftest import load_json

EXPECTED_CREATE = ScheduledCreate(
    service="targetsoc",
    m_id=240000001,
    values={"socref": 90},
    starttime=datetime(2030, 3, 31, 3, 30, tzinfo=ZoneInfo("Europe/Stockholm")),
    duration_seconds=3600,
).build()
EXPECTED_STATE = scheduler_expected_state_from_command(EXPECTED_CREATE)


def _record(
    lifecycle: OwnershipLifecycle = OwnershipLifecycle.PROVISIONAL,
) -> SchedulerOwnershipRecord:
    return SchedulerOwnershipRecord(
        config_entry_id="entry-1",
        gateway_serial="GATEWAY",
        m_id=240000001,
        service="targetsoc",
        intended_state=EXPECTED_STATE.as_mapping(),
        lifecycle=lifecycle,
    )


def _evidence(
    operation: SchedulerMutationOperation,
    mutation_ack: SchedulerAckResult,
    readback: SchedulerReconciliationResult,
    expected_state: SchedulerExpectedState | None = None,
) -> SchedulerMutationEvidence:
    return SchedulerMutationEvidence(
        operation=operation,
        m_id=240000001,
        service="targetsoc",
        mutation_ack=mutation_ack,
        expected_state=expected_state,
        readback=readback,
    )


def _matching_get_result():
    return reconcile_scheduler_get(
        parse_ack(load_json("mqtt/scheduler_get_ack_targetsoc_success.json")),
        EXPECTED_STATE,
    )


def _get_result_for(expected: SchedulerExpectedState) -> SchedulerReconciliationResult:
    ack = parse_ack(load_json("mqtt/scheduler_get_ack_targetsoc_success.json"))
    values = {
        **ack.values,
        "socref": expected.socref,
        "starttime": expected.starttime.isoformat().replace("+00:00", "Z"),
        "duration": expected.duration,
    }
    return reconcile_scheduler_get(
        replace(
            ack,
            m_id=expected.m_id,
            service=expected.service,
            socref=expected.socref,
            duration=expected.duration,
            values=values,
        ),
        expected,
    )


def _matching_set_ack():
    return evaluate_scheduler_ack(
        EXPECTED_CREATE,
        parse_ack(load_json("mqtt/scheduler_ack_success.json")),
    )


def _matching_delete_ack():
    delete = ScheduledDelete(m_id=240000001, expected_service="targetsoc").build()
    ack = replace(parse_ack(load_json("mqtt/scheduler_ack_success.json")), method="delete")
    return evaluate_scheduler_ack(delete, ack)


def test_targetsoc_get_readback_matches_complete_expected_state() -> None:
    ack = parse_ack(load_json("mqtt/scheduler_get_ack_targetsoc_success.json"))

    readback = parse_scheduler_get_ack(
        ack,
        expected_m_id=240000001,
        expected_service="targetsoc",
    )
    result = reconcile_scheduler_get(ack, EXPECTED_STATE)

    assert readback.m_id == 240000001
    assert readback.service == "targetsoc"
    assert readback.socref == 90
    assert readback.starttime == EXPECTED_STATE.starttime
    assert readback.duration == 3600
    assert readback.service_status == 5
    assert readback.service_duration_left == 3590
    assert readback.unknown_value_keys == ("future_unknown",)
    assert result.status is SchedulerReconciliationStatus.MATCH
    assert result.mismatch_fields == ()
    assert result.subject_m_id == 240000001
    assert result.subject_service == "targetsoc"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("m_id", 240000002),
        ("service", "voltagesupport"),
        ("socref", 91),
        ("starttime", "2030-04-01T01:30:00Z"),
        ("duration", 7200),
    ),
)
def test_targetsoc_get_readback_reports_exact_mismatch_field(
    field: str,
    value: object,
) -> None:
    ack = parse_ack(load_json("mqtt/scheduler_get_ack_targetsoc_success.json"))
    if field == "m_id" or field == "service" or field == "socref":
        ack = replace(ack, **{field: value})
    elif field == "duration":
        ack = replace(ack, duration=value, values={**ack.values, "duration": value})
    else:
        ack = replace(ack, values={**ack.values, field: value})

    result = reconcile_scheduler_get(ack, EXPECTED_STATE)

    assert result.status is SchedulerReconciliationStatus.MISMATCH
    assert result.mismatch_fields == (field,)


def test_runtime_readback_fields_do_not_invalidate_planned_match() -> None:
    ack = parse_ack(load_json("mqtt/scheduler_get_ack_targetsoc_success.json"))
    varied = replace(
        ack,
        service_status=1,
        service_duration_left=12,
    )

    assert (
        reconcile_scheduler_get(varied, EXPECTED_STATE).status
        is SchedulerReconciliationStatus.MATCH
    )


def test_delete_get_response_11_proves_only_matching_id_absence() -> None:
    absent = parse_ack(load_json("mqtt/scheduler_get_ack_absent.json"))

    absent_result = reconcile_scheduler_get(absent, EXPECTED_STATE)
    assert absent_result.status is SchedulerReconciliationStatus.ABSENT
    assert absent_result.subject_m_id == 240000001
    delete_absent = reconcile_scheduler_delete(absent, expected_m_id=240000001)
    assert delete_absent.status is SchedulerReconciliationStatus.ABSENT
    assert delete_absent.subject_m_id == 240000001

    wrong_id_result = reconcile_scheduler_delete(
        replace(absent, m_id=240000002),
        expected_m_id=240000001,
    )
    assert wrong_id_result.status is SchedulerReconciliationStatus.MISMATCH
    assert wrong_id_result.subject_m_id == 240000002
    assert (
        reconcile_scheduler_delete(
            replace(absent, response=65535, service="targetsoc"),
            expected_m_id=240000001,
        ).status
        is SchedulerReconciliationStatus.REJECTED
    )
    assert (
        reconcile_scheduler_delete(
            replace(absent, response=8),
            expected_m_id=240000001,
        ).status
        is SchedulerReconciliationStatus.REJECTED
    )


def test_invalid_readback_id_has_no_subject_identity() -> None:
    absent = parse_ack(load_json("mqtt/scheduler_get_ack_absent.json"))
    result = reconcile_scheduler_get(replace(absent, m_id="not-an-id"), EXPECTED_STATE)

    assert result.status is SchedulerReconciliationStatus.AMBIGUOUS
    assert result.subject_m_id is None


def test_scheduler_update_merge_preserves_unmodified_expected_fields() -> None:
    duration_update = ScheduledUpdate(
        m_id=240000001,
        service="targetsoc",
        duration_seconds=7200,
    ).build()
    soc_update = ScheduledUpdate(
        m_id=240000001,
        service="targetsoc",
        values={"socref": 80},
    ).build()

    duration_expected = apply_scheduler_update(EXPECTED_STATE, duration_update)
    soc_expected = apply_scheduler_update(EXPECTED_STATE, soc_update)

    assert duration_expected.duration == 7200
    assert duration_expected.socref == EXPECTED_STATE.socref
    assert duration_expected.starttime == EXPECTED_STATE.starttime
    assert soc_expected.socref == 80
    assert soc_expected.duration == EXPECTED_STATE.duration
    assert soc_expected.starttime == EXPECTED_STATE.starttime


def test_scheduler_timestamp_parser_rejects_naive_malformed_and_owned_now() -> None:
    with pytest.raises(SchedulerProtocolError):
        parse_scheduler_starttime(datetime(2030, 1, 1))
    with pytest.raises(SchedulerProtocolError):
        parse_scheduler_starttime("not-a-timestamp")
    with pytest.raises(SchedulerProtocolError):
        parse_scheduler_starttime("now")
    assert parse_scheduler_starttime("now", allow_now=True, require_real=False) is None


def test_malformed_or_now_get_starttime_is_rejected_for_owned_future_state() -> None:
    ack = parse_ack(load_json("mqtt/scheduler_get_ack_targetsoc_success.json"))
    for starttime in ("not-a-timestamp", "now"):
        result = reconcile_scheduler_get(
            replace(ack, values={**ack.values, "starttime": starttime}),
            EXPECTED_STATE,
        )
        assert result.status is SchedulerReconciliationStatus.REJECTED


def test_provisional_create_promotes_only_after_exact_evidence() -> None:
    record = _record()
    evidence = _evidence(
        SchedulerMutationOperation.CREATE,
        _matching_set_ack(),
        _matching_get_result(),
        EXPECTED_STATE,
    )

    reconciled = apply_scheduler_ownership_reconciliation(record, evidence)

    assert reconciled.lifecycle is OwnershipLifecycle.OWNED
    assert reconciled.as_dict()["intended_state"] == EXPECTED_STATE.as_mapping()


def test_create_rejects_fabricated_or_misbound_match_evidence() -> None:
    record = _record()
    set_ack = _matching_set_ack()
    matching = _matching_get_result()
    cases = (
        _evidence(
            SchedulerMutationOperation.CREATE,
            set_ack,
            SchedulerReconciliationResult(
                SchedulerReconciliationStatus.MATCH,
                "fabricated",
            ),
            EXPECTED_STATE,
        ),
        _evidence(
            SchedulerMutationOperation.CREATE,
            set_ack,
            replace(matching, subject_m_id=240000002),
            EXPECTED_STATE,
        ),
        _evidence(
            SchedulerMutationOperation.CREATE,
            set_ack,
            replace(matching, subject_service="voltagesupport"),
            EXPECTED_STATE,
        ),
    )

    assert all(
        apply_scheduler_ownership_reconciliation(record, evidence).lifecycle
        is OwnershipLifecycle.CONFLICTED
        for evidence in cases
    )

    changed_expected = replace(EXPECTED_STATE, socref=91)
    changed_record = SchedulerOwnershipRecord(
        config_entry_id=record.config_entry_id,
        gateway_serial=record.gateway_serial,
        m_id=record.m_id,
        service=record.service,
        intended_state=changed_expected.as_mapping(),
        lifecycle=OwnershipLifecycle.PROVISIONAL,
    )
    assert (
        apply_scheduler_ownership_reconciliation(
            changed_record,
            _evidence(
                SchedulerMutationOperation.CREATE,
                set_ack,
                matching,
                changed_expected,
            ),
        ).lifecycle
        is OwnershipLifecycle.CONFLICTED
    )


@pytest.mark.parametrize(
    "lifecycle",
    (
        OwnershipLifecycle.PROVISIONAL,
        OwnershipLifecycle.ORPHANED,
        OwnershipLifecycle.CONFLICTED,
    ),
)
def test_update_requires_owned_lifecycle(lifecycle: OwnershipLifecycle) -> None:
    record = _record(lifecycle)
    update = ScheduledUpdate(m_id=record.m_id, service="targetsoc", duration_seconds=7200).build()
    updated = apply_scheduler_update(EXPECTED_STATE, update)
    evidence = _evidence(
        SchedulerMutationOperation.UPDATE,
        evaluate_scheduler_ack(update, parse_ack(load_json("mqtt/scheduler_ack_success.json"))),
        _get_result_for(updated),
        updated,
    )

    assert (
        apply_scheduler_ownership_reconciliation(record, evidence).lifecycle
        is OwnershipLifecycle.CONFLICTED
    )


def test_update_requires_exact_new_expected_state() -> None:
    record = _record(OwnershipLifecycle.OWNED)
    update = ScheduledUpdate(m_id=record.m_id, service="targetsoc", duration_seconds=7200).build()
    updated = apply_scheduler_update(EXPECTED_STATE, update)
    update_ack = evaluate_scheduler_ack(
        update,
        parse_ack(load_json("mqtt/scheduler_ack_success.json")),
    )

    old_state_evidence = _evidence(
        SchedulerMutationOperation.UPDATE,
        update_ack,
        _matching_get_result(),
        updated,
    )
    assert (
        apply_scheduler_ownership_reconciliation(record, old_state_evidence).lifecycle
        is OwnershipLifecycle.CONFLICTED
    )

    exact_evidence = _evidence(
        SchedulerMutationOperation.UPDATE,
        update_ack,
        _get_result_for(updated),
        updated,
    )
    reconciled = apply_scheduler_ownership_reconciliation(record, exact_evidence)
    assert reconciled.lifecycle is OwnershipLifecycle.OWNED
    assert reconciled.as_dict()["intended_state"] == updated.as_mapping()

    wrong_operation = _evidence(
        SchedulerMutationOperation.UPDATE,
        _matching_delete_ack(),
        exact_evidence.readback,
        updated,
    )
    assert (
        apply_scheduler_ownership_reconciliation(record, wrong_operation).lifecycle
        is OwnershipLifecycle.CONFLICTED
    )


def test_owned_delete_requires_delete_ack_and_subject_id_absence() -> None:
    record = _record(OwnershipLifecycle.OWNED)
    absent = reconcile_scheduler_delete(
        parse_ack(load_json("mqtt/scheduler_get_ack_absent.json")),
        expected_m_id=record.m_id,
    )
    evidence = _evidence(
        SchedulerMutationOperation.DELETE,
        _matching_delete_ack(),
        absent,
    )
    assert (
        apply_scheduler_ownership_reconciliation(record, evidence).lifecycle
        is OwnershipLifecycle.ORPHANED
    )

    fabricated_absent = replace(absent, subject_m_id=None)
    assert (
        apply_scheduler_ownership_reconciliation(
            record,
            replace(evidence, readback=fabricated_absent),
        ).lifecycle
        is OwnershipLifecycle.CONFLICTED
    )
    other_id_absent = replace(absent, subject_m_id=240000002)
    assert (
        apply_scheduler_ownership_reconciliation(
            record,
            replace(evidence, readback=other_id_absent),
        ).lifecycle
        is OwnershipLifecycle.CONFLICTED
    )

    set_ack_evidence = replace(evidence, mutation_ack=_matching_set_ack())
    assert (
        apply_scheduler_ownership_reconciliation(record, set_ack_evidence).lifecycle
        is OwnershipLifecycle.CONFLICTED
    )

    wrong_id_evidence = replace(evidence, mutation_ack=replace(evidence.mutation_ack, m_id=99))
    assert (
        apply_scheduler_ownership_reconciliation(record, wrong_id_evidence).lifecycle
        is OwnershipLifecycle.CONFLICTED
    )
    wrong_service_evidence = replace(
        evidence,
        mutation_ack=replace(evidence.mutation_ack, service="voltagesupport"),
    )
    assert (
        apply_scheduler_ownership_reconciliation(record, wrong_service_evidence).lifecycle
        is OwnershipLifecycle.CONFLICTED
    )

    still_present = replace(evidence, readback=_matching_get_result())
    assert (
        apply_scheduler_ownership_reconciliation(record, still_present).lifecycle
        is OwnershipLifecycle.CONFLICTED
    )

    allocator = MessageIdAllocator(next_value=record.m_id)
    assert allocator.allocate(persistent=True) == record.m_id
    ambiguous = replace(
        evidence,
        readback=replace(absent, status=SchedulerReconciliationStatus.AMBIGUOUS),
    )
    assert (
        apply_scheduler_ownership_reconciliation(record, ambiguous).lifecycle
        is OwnershipLifecycle.CONFLICTED
    )
    assert record.m_id in allocator.owned_ids


def test_owned_update_merge_retains_complete_state_and_allows_zero_duration() -> None:
    record = SchedulerOwnershipRecord(
        config_entry_id="entry-1",
        gateway_serial="GATEWAY",
        m_id=240000001,
        service="targetsoc",
        intended_state=EXPECTED_STATE.as_mapping(),
        lifecycle=OwnershipLifecycle.OWNED,
    )
    merged = merge_owned_scheduler_update(
        record,
        ScheduledUpdate(m_id=240000001, service="targetsoc", duration_seconds=0),
    )

    assert merged.as_dict()["intended_state"] == {
        **EXPECTED_STATE.as_mapping(),
        "duration": 0,
    }


def test_ownership_defaults_provisional_and_persisted_lifecycle_is_explicit() -> None:
    record = SchedulerOwnershipRecord(
        config_entry_id="entry-1",
        gateway_serial="GATEWAY",
        m_id=240000001,
        service="targetsoc",
        intended_state=EXPECTED_STATE.as_mapping(),
    )
    assert record.lifecycle is OwnershipLifecycle.PROVISIONAL
    store = SchedulerOwnershipStore(config_entry_id="entry-1", gateway_serial="GATEWAY")
    store.register(record)
    with pytest.raises(CommandValidationError):
        store.require_owned(record.m_id)

    missing_lifecycle = record.as_dict()
    del missing_lifecycle["lifecycle"]
    with pytest.raises(CommandValidationError):
        SchedulerOwnershipRecord.from_mapping(missing_lifecycle)

    owned = replace(record, lifecycle=OwnershipLifecycle.OWNED)
    restored = SchedulerOwnershipRecord.from_mapping(owned.as_dict())
    assert restored == owned


def test_scheduler_m_id_ownership_is_persistent_until_reconciled_absence() -> None:
    allocator = MessageIdAllocator(next_value=240000001)
    m_id = allocator.allocate(persistent=True)

    allocator.retire(m_id)
    assert m_id in allocator.owned_ids
    assert allocator.allocate() != m_id

    allocator.retire(m_id, release_ownership=True)
    assert m_id not in allocator.owned_ids


def test_provisional_ownership_survives_restart_and_rejects_normal_execution() -> None:
    allocator = MessageIdAllocator(next_value=240000001)
    m_id = allocator.allocate(persistent=True)
    record = _record(OwnershipLifecycle.PROVISIONAL)
    store = SchedulerOwnershipStore(config_entry_id="entry-1", gateway_serial="GATEWAY")
    store.register(record)

    with pytest.raises(CommandValidationError):
        store.require_owned(m_id)

    restored = SchedulerOwnershipStore.from_state(
        store.snapshot(),
        config_entry_id="entry-1",
        gateway_serial="GATEWAY",
    )
    restored_record = restored.get(m_id)
    assert restored_record is not None
    assert restored_record.lifecycle is OwnershipLifecycle.PROVISIONAL
    with pytest.raises(CommandValidationError):
        restored.require_owned(m_id)
    assert allocator.allocate() != m_id


def test_targetsoc_only_reconciliation_rejects_voltage_support() -> None:
    voltage_ack = parse_ack(load_json("mqtt/scheduler_get_ack_targetsoc_success.json"))

    with pytest.raises(SchedulerProtocolError):
        parse_scheduler_get_ack(
            voltage_ack,
            expected_m_id=240000001,
            expected_service="voltagesupport",
        )
    with pytest.raises(SchedulerProtocolError):
        SchedulerExpectedState(
            m_id=240000001,
            service="voltagesupport",
            socref=90,
            starttime=EXPECTED_STATE.starttime,
            duration=3600,
        )
    with pytest.raises(CommandValidationError):
        SchedulerMutationEvidence(
            operation=SchedulerMutationOperation.DELETE,
            m_id=240000001,
            service="voltagesupport",
            mutation_ack=_matching_delete_ack(),
            expected_state=None,
            readback=reconcile_scheduler_delete(
                parse_ack(load_json("mqtt/scheduler_get_ack_absent.json")),
                expected_m_id=240000001,
            ),
        )


def test_unknown_m_id_never_becomes_owned_or_maps_https_identifiers() -> None:
    record = SchedulerOwnershipRecord(
        config_entry_id="entry-1",
        gateway_serial="GATEWAY",
        m_id=240000001,
        service="targetsoc",
        intended_state=EXPECTED_STATE.as_mapping(),
        lifecycle=OwnershipLifecycle.OWNED,
    )
    assert record.m_id != 7
    assert record.m_id != 42
    assert record.m_id != 99
    assert record.as_dict()["intended_state"]["m_id"] == record.m_id
    assert record.lifecycle is OwnershipLifecycle.OWNED
    store = SchedulerOwnershipStore(config_entry_id="entry-1", gateway_serial="GATEWAY")
    with pytest.raises(CommandValidationError):
        store.require_owned(7)

    with pytest.raises(CommandValidationError):
        SchedulerOwnershipRecord(
            config_entry_id="entry-1",
            gateway_serial="GATEWAY",
            m_id=240000002,
            service="targetsoc",
            intended_state={},
        )
