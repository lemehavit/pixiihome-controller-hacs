from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.pixiihome_controller.const import LIVE_WRITES_ENABLED
from custom_components.pixiihome_controller.control.models import (
    ControlOperation,
    ControlPolicyInput,
    OperationImpact,
    OperatorMode,
    PlantHealth,
    ReleaseState,
    RuntimeMode,
    ScheduleOrigin,
)
from custom_components.pixiihome_controller.control.policy import evaluate_control_operation
from custom_components.pixiihome_controller.exceptions import CommandValidationError
from custom_components.pixiihome_controller.protocol.models import MqttAck
from custom_components.pixiihome_controller.protocol.mqtt import parse_ack
from custom_components.pixiihome_controller.protocol.scheduler_commands import (
    MAX_SCHEDULER_DURATION_SECONDS,
    ScheduledCreate,
    ScheduledDelete,
    ScheduledGet,
    ScheduledUpdate,
    SchedulerAckStatus,
    evaluate_scheduler_ack,
    normalize_scheduler_starttime,
)
from custom_components.pixiihome_controller.scheduler.ownership import (
    OwnershipLifecycle,
    SchedulerOwnershipRecord,
    SchedulerOwnershipStore,
)

from .conftest import load_json


def test_scheduler_create_matches_sanitized_mqtt_fixture() -> None:
    fixture = load_json("mqtt/scheduler_create_targetsoc.json")
    command = ScheduledCreate(
        service="targetsoc",
        m_id=240000001,
        values={"socref": 90},
        starttime=datetime(2030, 3, 31, 3, 30, tzinfo=ZoneInfo("Europe/Stockholm")),
        duration_seconds=3600,
    ).build()

    assert command.to_payload() == fixture
    assert command.state_changing is True


def test_scheduler_voltagesupport_create_omits_unsupported_values() -> None:
    command = ScheduledCreate(
        service="voltagesupport",
        m_id=240000002,
        starttime=datetime(2030, 3, 31, 3, 30, tzinfo=ZoneInfo("Europe/Stockholm")),
        duration_seconds=3600,
    ).build()

    assert command.to_payload() == load_json("mqtt/scheduler_create_voltagesupport.json")


def test_scheduler_update_delete_and_get_match_fixtures() -> None:
    update = ScheduledUpdate(
        m_id=240000001,
        values={"socref": 80},
        duration_seconds=7200,
        service="targetsoc",
    ).build()
    delete = ScheduledDelete(m_id=240000001, expected_service="targetsoc").build()
    get = ScheduledGet(m_id=240000001, expected_service="targetsoc").build()

    assert update.to_payload() == load_json("mqtt/scheduler_update_targetsoc.json")
    assert delete.to_payload() == load_json("mqtt/scheduler_delete.json")
    assert get.to_payload() == load_json("mqtt/scheduler_get.json")
    assert "service" not in delete.to_payload()
    assert "service" not in get.to_payload()
    assert get.state_changing is False


def test_scheduler_starttime_requires_aware_future_and_normalizes_dst() -> None:
    now = datetime(2025, 1, 1, tzinfo=UTC)
    spring = datetime(2026, 3, 29, 3, 30, tzinfo=ZoneInfo("Europe/Stockholm"))
    autumn = datetime(2026, 10, 25, 3, 30, fold=1, tzinfo=ZoneInfo("Europe/Stockholm"))

    assert normalize_scheduler_starttime(spring, now=now) == "2026-03-29T01:30:00Z"
    assert normalize_scheduler_starttime(autumn, now=now) == "2026-10-25T02:30:00Z"
    with pytest.raises(CommandValidationError):
        normalize_scheduler_starttime(datetime(2030, 1, 1))
    with pytest.raises(CommandValidationError):
        normalize_scheduler_starttime(datetime(2020, 1, 1, tzinfo=UTC), now=now)


def test_scheduler_validation_rejects_unsafe_ids_services_durations_and_updates() -> None:
    for m_id in (None, True, 0, -1, 1.5):
        with pytest.raises(CommandValidationError):
            ScheduledDelete(m_id=m_id).build()  # type: ignore[arg-type]
    for service in ("not-a-service", "runningservice"):
        with pytest.raises(CommandValidationError):
            ScheduledCreate(
                service=service,
                m_id=1,
                values={"socref": 90},
                starttime=datetime(2030, 1, 1, tzinfo=UTC),
                duration_seconds=3600,
            ).build()
    for duration in (True, -1, MAX_SCHEDULER_DURATION_SECONDS + 1):
        with pytest.raises(CommandValidationError):
            ScheduledCreate(
                service="targetsoc",
                m_id=1,
                values={"socref": 90},
                starttime=datetime(2030, 1, 1, tzinfo=UTC),
                duration_seconds=duration,  # type: ignore[arg-type]
            ).build()
    with pytest.raises(CommandValidationError):
        ScheduledCreate(
            service="targetsoc",
            m_id=1,
            values={"socref": 90},
            starttime=datetime(2030, 1, 1, tzinfo=UTC),
            duration_seconds=0,
        ).build()
    zero_update = ScheduledUpdate(
        m_id=1,
        service="targetsoc",
        duration_seconds=0,
    ).build()
    assert zero_update.to_payload()["duration"] == 0
    with pytest.raises(CommandValidationError):
        ScheduledCreate(
            service="targetsoc",
            m_id=1,
            starttime=datetime(2030, 1, 1, tzinfo=UTC),
            duration_seconds=3600,
        ).build()
    with pytest.raises(CommandValidationError):
        ScheduledCreate(
            service="voltagesupport",
            m_id=1,
            values={"unsupported": True},
            starttime=datetime(2030, 1, 1, tzinfo=UTC),
            duration_seconds=3600,
        ).build()
    for socref in (-0.1, 100.1, True, float("inf")):
        with pytest.raises(CommandValidationError):
            ScheduledCreate(
                service="targetsoc",
                m_id=1,
                values={"socref": socref},
                starttime=datetime(2030, 1, 1, tzinfo=UTC),
                duration_seconds=3600,
            ).build()
    with pytest.raises(CommandValidationError):
        ScheduledCreate(
            service="targetsoc",
            m_id=1,
            values={"socref": 90, "unexpected": 1},
            starttime=datetime(2030, 1, 1, tzinfo=UTC),
            duration_seconds=3600,
        ).build()
    with pytest.raises(CommandValidationError):
        ScheduledUpdate(m_id=1, values={}).build()
    with pytest.raises(CommandValidationError):
        ScheduledUpdate(m_id=1, values={"socref": 90}, service="runningservice").build()


def test_scheduler_ack_requires_matching_m_id_method_and_applicable_service() -> None:
    command = ScheduledCreate(
        service="targetsoc",
        m_id=240000001,
        values={"socref": 90},
        starttime=datetime(2030, 1, 1, tzinfo=UTC),
        duration_seconds=3600,
    ).build()
    success_ack = parse_ack(load_json("mqtt/scheduler_ack_success.json"))
    rejected_ack = parse_ack(load_json("mqtt/scheduler_ack_rejected.json"))

    assert evaluate_scheduler_ack(command, success_ack).status is SchedulerAckStatus.SUCCESS
    assert evaluate_scheduler_ack(command, rejected_ack).status is SchedulerAckStatus.REJECTED
    assert evaluate_scheduler_ack(command, replace(success_ack, m_id=240000002)).reason == (
        "ack_m_id_mismatch"
    )
    assert evaluate_scheduler_ack(command, replace(success_ack, method="delete")).reason == (
        "ack_method_mismatch"
    )
    assert evaluate_scheduler_ack(command, replace(success_ack, service="powercontrol")).reason == (
        "ack_service_mismatch"
    )

    get = ScheduledGet(m_id=240000001, expected_service="targetsoc").build()
    get_ack = replace(success_ack, method="get")
    assert evaluate_scheduler_ack(get, get_ack).status is SchedulerAckStatus.SUCCESS
    assert evaluate_scheduler_ack(get, replace(get_ack, service="voltagesupport")).reason == (
        "ack_service_mismatch"
    )


def test_scheduler_read_identifiers_do_not_create_m_id_mapping() -> None:
    from custom_components.pixiihome_controller.protocol.scheduler import parse_detail_response

    detail = parse_detail_response(load_json("scheduler/detail_external_like_response.json"))

    assert not hasattr(detail, "m_id")
    assert detail.sch_index != detail.service_no
    assert detail.source_id == 7


def test_scheduler_ownership_round_trips_and_never_adopts_unknown_ids() -> None:
    intended_state = {"socref": 90, "nested": {"duration": 3600}}
    record = SchedulerOwnershipRecord(
        config_entry_id="entry-1",
        gateway_serial="GATEWAY",
        m_id=240000001,
        service="targetsoc",
        intended_state=intended_state,
        lifecycle=OwnershipLifecycle.OWNED,
    )
    intended_state["socref"] = 10
    store = SchedulerOwnershipStore(config_entry_id="entry-1", gateway_serial="GATEWAY")
    store.register(record)

    with pytest.raises(TypeError):
        record.intended_state["nested"]["duration"] = 1  # type: ignore[index]
    assert record.as_dict()["intended_state"]["socref"] == 90

    restored = SchedulerOwnershipStore.from_state(
        store.snapshot(),
        config_entry_id="entry-1",
        gateway_serial="GATEWAY",
    )
    assert restored.require_owned(240000001) == record
    with pytest.raises(CommandValidationError):
        restored.require_owned(240000002)
    with pytest.raises(CommandValidationError):
        restored.require_owned(240000001, gateway_serial="OTHER")
    with pytest.raises(CommandValidationError):
        SchedulerOwnershipStore.from_state(
            store.snapshot(),
            config_entry_id="entry-1",
            gateway_serial="OTHER",
        )
    with pytest.raises(CommandValidationError):
        store.register(
            SchedulerOwnershipRecord(
                config_entry_id="entry-2",
                gateway_serial="GATEWAY",
                m_id=240000002,
                service="targetsoc",
                intended_state={"socref": 80},
                lifecycle=OwnershipLifecycle.OWNED,
            )
        )

    restored.mark_conflicted(240000001)
    assert restored.get(240000001).lifecycle is OwnershipLifecycle.CONFLICTED  # type: ignore[union-attr]
    with pytest.raises(CommandValidationError):
        restored.require_owned(240000001)


def test_scheduler_policy_is_release_closed_and_fail_closed_by_default() -> None:
    assert LIVE_WRITES_ENABLED is False
    blocked = evaluate_control_operation(ControlPolicyInput(ControlOperation.CREATE))
    assert blocked.permitted is False
    assert "release_gate_closed" in blocked.blocked_reasons
    assert "scheduler_ownership_store_invalid" in blocked.blocked_reasons
    assert "scheduler_visibility_stale" in blocked.blocked_reasons

    allowed = evaluate_control_operation(
        ControlPolicyInput(
            operation=ControlOperation.CREATE,
            runtime_mode=RuntimeMode.SIMULATOR,
            operator_mode=OperatorMode.LOCAL_CONTROL,
            origin=ScheduleOrigin.LOCAL_CREATED,
            release_state=ReleaseState.PREVIEW_ONLY,
            plant_health=PlantHealth(
                telemetry_fresh=True,
                system_safe=True,
                battery_safe=True,
                grid_safe=True,
                system_events_clear=True,
                reasons=(),
            ),
            operation_impact=OperationImpact(
                active_service=0,
                immediate_effect_known=True,
            ),
            scheduler_visibility_fresh=True,
            ownership_store_valid=True,
            ownership_ambiguous=False,
            mqtt_connected=True,
        )
    )
    assert allowed.permitted
    assert allowed.blocked_reasons == ()


def test_ack_model_remains_typed_for_fixture_boundary() -> None:
    ack = parse_ack(load_json("mqtt/scheduler_ack_success.json"))
    assert isinstance(ack, MqttAck)
    assert ack.m_id == 240000001
