from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.pixiihome_controller.control.adapters import (
    AdapterExecutionStatus,
    AdapterRequest,
    ProductionReadOnlyAdapter,
)
from custom_components.pixiihome_controller.control.engine import (
    ControlEngine,
    EngineStatus,
    InMemoryJournalPort,
)
from custom_components.pixiihome_controller.control.journal import (
    ControlJournal,
    JournalPhase,
    JournalTargetIdentity,
)
from custom_components.pixiihome_controller.control.models import (
    ControlOperation,
    ControlPolicyInput,
    Initiative,
    ScheduleOrigin,
)
from custom_components.pixiihome_controller.control.simulator import (
    SIMULATOR_HISTORY_LIMIT,
    SIMULATOR_SCHEDULE_LIMIT,
    ReplacementSagaPhase,
    SimulatedSchedule,
    SimulatorAdapter,
    SimulatorRuntime,
)


def identity(m_id: int = 240000001) -> JournalTargetIdentity:
    return JournalTargetIdentity(
        m_id=m_id,
        sch_index=1,
        service_no=110,
        source_id=7,
        asset_id="battery-1",
    )


def schedule_payload(
    *,
    start: datetime = datetime(2030, 1, 2, tzinfo=UTC),
    socref: float = 80,
) -> dict[str, object]:
    return {
        "start": start,
        "duration_seconds": 3600,
        "socref": socref,
    }


@pytest.mark.asyncio
async def test_production_adapter_is_structurally_read_only() -> None:
    reads: list[AdapterRequest] = []

    async def read(request: AdapterRequest) -> dict[str, object]:
        reads.append(request)
        return {"observed": True}

    adapter = ProductionReadOnlyAdapter(read)
    journal = InMemoryJournalPort(ControlJournal.empty(
        config_entry_id="entry-a",
        gateway_serial="GATEWAY",
    ))
    engine = ControlEngine(adapter, journal, lambda: "tx-1")

    read_result = await engine.execute(
        AdapterRequest(ControlOperation.READ_ONLY),
        ControlPolicyInput(ControlOperation.READ_ONLY),
    )
    mutation_result = await engine.execute(
        AdapterRequest(
            ControlOperation.DIRECT_TARGET_SOC,
            service="targetsoc",
            target_identity=identity(),
            payload={"socref": 80},
        ),
        ControlPolicyInput(ControlOperation.DIRECT_TARGET_SOC),
    )

    assert read_result.status is EngineStatus.SUCCESS
    assert read_result.adapter_result is not None
    assert read_result.adapter_result.state == {"observed": True}
    assert mutation_result.status is EngineStatus.BLOCKED
    assert "evidence_required" in mutation_result.decision.blocked_reasons
    assert "release_gate_closed" in mutation_result.decision.blocked_reasons
    assert len(reads) == 1
    assert journal.journal.transactions == ()


@pytest.mark.asyncio
async def test_simulator_denies_generic_and_protected_direct_control() -> None:
    runtime = SimulatorRuntime()

    result = await runtime.execute(
        AdapterRequest(
            ControlOperation.DIRECT_CONTROL,
            service="ffr",
            target_identity=JournalTargetIdentity(service_no=1, asset_id="battery-1"),
            payload={"power_w": 1000},
        )
    )

    assert result.status is EngineStatus.BLOCKED
    assert "adapter_capability_unsupported" in result.decision.blocked_reasons
    assert "generic_direct_control_forbidden" in result.decision.blocked_reasons
    assert runtime.adapter.history == []
    assert runtime.journal.journal.transactions == ()


@pytest.mark.asyncio
async def test_simulator_supports_only_explicit_direct_services() -> None:
    runtime = SimulatorRuntime()

    target = await runtime.execute(
        AdapterRequest(
            ControlOperation.DIRECT_TARGET_SOC,
            service="targetsoc",
            target_identity=JournalTargetIdentity(asset_id="battery-1", service_no=110),
            payload={"socref": 75},
        )
    )
    stop = await runtime.execute(
        AdapterRequest(
            ControlOperation.STOP,
            target_identity=JournalTargetIdentity(asset_id="battery-1", service_no=110),
        )
    )
    demand = await runtime.execute(
        AdapterRequest(
            ControlOperation.DIRECT_DEMAND_RESPONSE,
            service="demandresponse",
            target_identity=JournalTargetIdentity(asset_id="battery-1", service_no=102),
            payload={"power_w": -1500},
        )
    )

    assert target.status is EngineStatus.SUCCESS
    assert stop.status is EngineStatus.SUCCESS
    assert demand.status is EngineStatus.SUCCESS
    assert runtime.adapter.battery.target_soc_percent is None
    assert runtime.adapter.active_service == 102
    assert runtime.adapter.battery.power_w == -1500
    assert all(
        transaction.phase is JournalPhase.RECONCILED
        for transaction in runtime.journal.journal.transactions
    )




@pytest.mark.asyncio
async def test_simulator_direct_control_expires_after_duration() -> None:
    runtime = SimulatorRuntime()
    initial_soc = runtime.adapter.battery.soc_percent
    result = await runtime.execute(
        AdapterRequest(
            ControlOperation.DIRECT_DEMAND_RESPONSE,
            service="demandresponse",
            target_identity=JournalTargetIdentity(asset_id="battery-1", service_no=102),
            payload={"power_w": 1000, "duration_seconds": 60},
        )
    )

    assert result.status is EngineStatus.SUCCESS
    runtime.adapter.advance(59)
    assert runtime.adapter.active_service == 102
    runtime.adapter.advance(1)
    assert runtime.adapter.active_service == 0
    assert runtime.adapter.battery.power_w == 0
    assert runtime.adapter.battery.soc_percent == pytest.approx(
        initial_soc
        + (1 * (60 / 3600) / runtime.adapter.battery.capacity_kwh * 100)
    )


@pytest.mark.asyncio
async def test_simulator_direct_service_switch_clears_superseded_state() -> None:
    runtime = SimulatorRuntime()
    target_request = AdapterRequest(
        ControlOperation.DIRECT_TARGET_SOC,
        service="targetsoc",
        target_identity=JournalTargetIdentity(asset_id="battery-1", service_no=110),
        payload={"socref": 75},
    )
    demand_request = AdapterRequest(
        ControlOperation.DIRECT_DEMAND_RESPONSE,
        service="demandresponse",
        target_identity=JournalTargetIdentity(asset_id="battery-1", service_no=102),
        payload={"power_w": -1500},
    )

    await runtime.execute(target_request)
    await runtime.execute(demand_request)
    assert runtime.adapter.battery.target_soc_percent is None

    await runtime.execute(target_request)
    assert runtime.adapter.active_service == 107
    assert runtime.adapter.demand_response_power_w == 0
    assert runtime.adapter.battery.power_w == 0
    assert runtime.adapter.battery.target_soc_percent == 75
@pytest.mark.asyncio
async def test_simulator_scheduler_crud_preserves_typed_identity_and_journal() -> None:
    runtime = SimulatorRuntime()
    create = AdapterRequest(
        ControlOperation.CREATE,
        service="targetsoc",
        target_identity=identity(),
        payload=schedule_payload(),
    )

    created = await runtime.execute(create)
    updated = await runtime.execute(
        AdapterRequest(
            ControlOperation.UPDATE,
            service="targetsoc",
            target_identity=identity(),
            payload={"socref": 70},
        )
    )
    replaced = await runtime.execute(
        AdapterRequest(
            ControlOperation.REPLACE,
            service="targetsoc",
            target_identity=identity(),
            payload=schedule_payload(socref=65),
        )
    )
    deleted = await runtime.execute(
        AdapterRequest(
            ControlOperation.DELETE,
            service="targetsoc",
            target_identity=identity(),
        )
    )

    assert [created.status, updated.status, replaced.status, deleted.status] == [
        EngineStatus.SUCCESS,
        EngineStatus.SUCCESS,
        EngineStatus.SUCCESS,
        EngineStatus.SUCCESS,
    ]
    assert runtime.adapter.schedules == {}
    transactions = runtime.journal.journal.transactions
    assert [item.operation for item in transactions] == [
        "create",
        "update",
        "replace",
        "delete",
    ]
    assert all(item.target_identity == identity() for item in transactions)
    assert all(item.phase is JournalPhase.RECONCILED for item in transactions)


@pytest.mark.asyncio
async def test_external_schedule_requires_scoped_manual_grant() -> None:
    runtime = SimulatorRuntime()
    runtime.adapter.seed_external_schedule(
        SimulatedSchedule(
            m_id=240000001,
            start=datetime(2030, 1, 2, tzinfo=UTC),
            duration_seconds=3600,
            socref=80,
            origin=ScheduleOrigin.EXTERNAL_OBSERVED,
            sch_index=9,
            source_id=12,
        )
    )
    exact_identity = JournalTargetIdentity(
        m_id=240000001,
        sch_index=9,
        service_no=110,
        source_id=12,
        asset_id="battery-1",
    )
    request = AdapterRequest(
        ControlOperation.UPDATE,
        service="targetsoc",
        target_identity=exact_identity,
        payload={"socref": 72},
    )

    blocked = await runtime.execute(request)
    granted = await runtime.execute(request, grant_authorized=True)

    assert blocked.status is EngineStatus.BLOCKED
    assert blocked.decision.blocked_reasons == ("manual_grant_required",)
    assert granted.status is EngineStatus.SUCCESS
    assert runtime.adapter.schedules[240000001].socref == 72
    assert runtime.adapter.schedules[240000001].origin is ScheduleOrigin.EXTERNAL_OBSERVED
    replaced = await runtime.execute(
        AdapterRequest(
            ControlOperation.REPLACE,
            service="targetsoc",
            target_identity=exact_identity,
            payload=schedule_payload(socref=68),
        ),
        grant_authorized=True,
    )
    blocked_after_replace = await runtime.execute(request)

    assert replaced.status is EngineStatus.SUCCESS
    assert runtime.adapter.schedules[240000001].origin is ScheduleOrigin.EXTERNAL_OBSERVED
    assert runtime.adapter.schedules[240000001].sch_index == 9
    assert runtime.adapter.schedules[240000001].source_id == 12
    assert blocked_after_replace.status is EngineStatus.BLOCKED
    assert blocked_after_replace.decision.blocked_reasons == ("manual_grant_required",)


@pytest.mark.asyncio
async def test_simulator_faults_fail_closed_before_adapter_execution() -> None:
    runtime = SimulatorRuntime()
    runtime.adapter.inject_fault("battery_unsafe")
    runtime.adapter.inject_fault("mqtt_disconnected")

    result = await runtime.execute(
        AdapterRequest(
            ControlOperation.CREATE,
            service="targetsoc",
            target_identity=identity(),
            payload=schedule_payload(),
        )
    )

    assert result.status is EngineStatus.BLOCKED
    assert "battery_not_safe" in result.decision.blocked_reasons
    assert "mqtt_disconnected" in result.decision.blocked_reasons
    assert runtime.adapter.schedules == {}
    assert runtime.adapter.history == []
@pytest.mark.asyncio
async def test_simulator_planner_apply_and_recovery_paths_are_executable() -> None:
    runtime = SimulatorRuntime()
    request = AdapterRequest(
        ControlOperation.PLANNER_APPLY,
        target_identity=JournalTargetIdentity(asset_id="battery-1"),
        payload={
            "schedules": [
                {
                    "m_id": 240000010,
                    "start": datetime(2030, 1, 2, tzinfo=UTC),
                    "duration_seconds": 1800,
                    "socref": 60,
                    "source_id": 10,
                },
                {
                    "m_id": 240000011,
                    "start": datetime(2030, 1, 2, 1, tzinfo=UTC),
                    "duration_seconds": 1800,
                    "socref": 75,
                    "source_id": 11,
                },
            ]
        },
    )

    applied = await runtime.execute(request)
    recovery_identity = JournalTargetIdentity(
        m_id=240000010,
        source_id=10,
        asset_id="battery-1",
    )
    await runtime.journal.begin_control_transaction(
        transaction_id="interrupted-create",
        operation="create",
        target_identity=recovery_identity,
        intent={
            "start": datetime(2030, 1, 2, tzinfo=UTC).isoformat(),
            "duration_seconds": 1800,
            "socref": 60,
        },
    )
    await runtime.journal.transition_control_transaction(
        "interrupted-create",
        JournalPhase.COMMAND_SENT,
    )
    runtime.journal.journal = runtime.journal.journal.mark_unfinished_recovery_required()
    transaction_count = len(runtime.journal.journal.transactions)
    recovered = await runtime.execute(
        AdapterRequest(
            ControlOperation.RECOVERY_APPLY,
            service="targetsoc",
            target_identity=recovery_identity,
            payload={"transaction_id": "interrupted-create"},
        )
    )

    assert applied.status is EngineStatus.SUCCESS
    assert recovered.status is EngineStatus.SUCCESS
    assert sorted(runtime.adapter.schedules) == [240000010, 240000011]
    assert len(runtime.journal.journal.transactions) == transaction_count
    recovered_transaction = runtime.journal.journal.transactions[-1]
    assert recovered_transaction.transaction_id == "interrupted-create"
    assert recovered_transaction.operation == "create"
    assert recovered_transaction.phase is JournalPhase.RECONCILED


@pytest.mark.asyncio
async def test_simulator_planner_rejects_external_interval_overlap() -> None:
    runtime = SimulatorRuntime()
    external = SimulatedSchedule(
        m_id=240000001,
        start=datetime(2030, 1, 2, tzinfo=UTC),
        duration_seconds=1800,
        socref=60,
        origin=ScheduleOrigin.EXTERNAL_OBSERVED,
    )
    runtime.adapter.seed_external_schedule(external)

    result = await runtime.execute(
        AdapterRequest(
            ControlOperation.PLANNER_APPLY,
            service="targetsoc",
            target_identity=JournalTargetIdentity(asset_id="battery-1"),
            payload={
                "schedules": [
                    {
                        "m_id": 240000002,
                        "start": datetime(2030, 1, 2, 0, 15, tzinfo=UTC),
                        "duration_seconds": 900,
                        "socref": 70,
                    }
                ]
            },
        )
    )

    assert result.status is EngineStatus.FAILED
    assert result.reason == "planner_external_schedule_conflict"
    assert runtime.adapter.schedules == {external.m_id: external}


@pytest.mark.asyncio
async def test_simulator_planner_preserves_unknown_and_refreshes_removed_active_state() -> None:
    runtime = SimulatorRuntime()
    now = runtime.adapter.clock.now
    local_active = SimulatedSchedule(
        m_id=240000001,
        start=now - timedelta(minutes=1),
        duration_seconds=600,
        socref=80,
        origin=ScheduleOrigin.LOCAL_CREATED,
    )
    unknown = SimulatedSchedule(
        m_id=240000002,
        start=now + timedelta(hours=1),
        duration_seconds=600,
        socref=65,
        origin=ScheduleOrigin.UNKNOWN,
    )
    runtime.adapter.schedules = {
        local_active.m_id: local_active,
        unknown.m_id: unknown,
    }
    runtime.adapter.advance(0)
    assert runtime.adapter.active_service == 107

    result = await runtime.execute(
        AdapterRequest(
            ControlOperation.PLANNER_APPLY,
            service="targetsoc",
            target_identity=JournalTargetIdentity(asset_id="battery-1"),
            payload={
                "schedules": [
                    {
                        "m_id": 240000003,
                        "start": now + timedelta(hours=2),
                        "duration_seconds": 600,
                        "socref": 70,
                    }
                ]
            },
        )
    )

    assert result.status is EngineStatus.SUCCESS
    assert sorted(runtime.adapter.schedules) == [240000002, 240000003]
    assert runtime.adapter.schedules[240000002].origin is ScheduleOrigin.UNKNOWN
    assert runtime.adapter.active_service == 0
    assert runtime.adapter.battery.target_soc_percent is None


@pytest.mark.asyncio
async def test_simulator_recovery_keeps_mismatched_identity_pending() -> None:
    runtime = SimulatorRuntime()
    runtime.adapter.schedules[240000001] = SimulatedSchedule(
        m_id=240000001,
        start=datetime(2030, 1, 2, tzinfo=UTC),
        duration_seconds=3600,
        socref=80,
        sch_index=9,
        source_id=7,
    )
    await runtime.journal.begin_control_transaction(
        transaction_id="identity-mismatch",
        operation="create",
        target_identity=identity(),
        intent={
            "start": datetime(2030, 1, 2, tzinfo=UTC).isoformat(),
            "duration_seconds": 3600,
            "socref": 80,
        },
    )
    await runtime.journal.transition_control_transaction(
        "identity-mismatch",
        JournalPhase.COMMAND_SENT,
    )
    runtime.journal.journal = runtime.journal.journal.mark_unfinished_recovery_required()

    result = await runtime.execute(
        AdapterRequest(
            ControlOperation.RECOVERY_APPLY,
            service="targetsoc",
            target_identity=identity(),
            payload={"transaction_id": "identity-mismatch"},
        )
    )
    assert result.status is EngineStatus.RECOVERY_REQUIRED
    assert result.reason == "recovery_identity_mismatch"
    assert runtime.journal.journal.transactions[-1].phase is JournalPhase.RECOVERY_REQUIRED


@pytest.mark.asyncio
async def test_simulator_recovery_rejects_operation_without_outcome_evidence() -> None:
    runtime = SimulatorRuntime()
    runtime.adapter.schedules[240000001] = SimulatedSchedule(
        m_id=240000001,
        start=datetime(2030, 1, 2, tzinfo=UTC),
        duration_seconds=3600,
        socref=80,
        sch_index=1,
        source_id=7,
    )
    await runtime.journal.begin_control_transaction(
        transaction_id="interrupted-delete",
        operation="delete",
        target_identity=identity(),
    )
    await runtime.journal.transition_control_transaction(
        "interrupted-delete",
        JournalPhase.COMMAND_SENT,
    )
    runtime.journal.journal = runtime.journal.journal.mark_unfinished_recovery_required()

    result = await runtime.execute(
        AdapterRequest(
            ControlOperation.RECOVERY_APPLY,
            service="targetsoc",
            target_identity=identity(),
            payload={"transaction_id": "interrupted-delete"},
        )
    )

    assert result.status is EngineStatus.RECOVERY_REQUIRED
    assert result.reason == "recovery_operation_unsupported"
    assert runtime.journal.journal.transactions[-1].phase is JournalPhase.RECOVERY_REQUIRED





@pytest.mark.asyncio
async def test_simulator_clock_activates_schedule_and_advances_battery() -> None:
    runtime = SimulatorRuntime()
    start = runtime.adapter.clock.now + timedelta(seconds=60)
    created = await runtime.execute(
        AdapterRequest(
            ControlOperation.CREATE,
            service="targetsoc",
            target_identity=identity(),
            payload=schedule_payload(start=start, socref=82),
        )
    )

    before = runtime.adapter.battery.soc_percent
    runtime.advance(60)
    assert created.status is EngineStatus.SUCCESS
    assert runtime.adapter.active_service == 107
    assert runtime.adapter.battery.target_soc_percent == 82

    runtime.adapter.battery.power_w = 1000
    runtime.advance(3600)
    assert runtime.adapter.battery.soc_percent > before


@pytest.mark.asyncio
async def test_adapter_validation_failure_is_journaled_terminally() -> None:
    runtime = SimulatorRuntime()

    result = await runtime.execute(
        AdapterRequest(
            ControlOperation.DIRECT_TARGET_SOC,
            service="targetsoc",
            target_identity=JournalTargetIdentity(asset_id="battery-1", service_no=110),
            payload={"socref": 99},
        )
    )

    assert result.status is EngineStatus.FAILED
    assert result.adapter_result is not None
    assert result.adapter_result.status is AdapterExecutionStatus.INVALID
    transaction = runtime.journal.journal.transactions[-1]
    assert transaction.phase is JournalPhase.FAILED
    assert transaction.reason == "target SoC is outside simulated battery bounds"


@pytest.mark.asyncio
async def test_adapter_exception_is_journaled_for_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = SimulatorRuntime()

    async def raise_after_possible_effect(*args: object) -> None:
        del args
        raise RuntimeError("transport outcome unknown")

    monkeypatch.setattr(type(runtime.adapter), "execute", raise_after_possible_effect)
    result = await runtime.execute(
        AdapterRequest(
            ControlOperation.CREATE,
            service="targetsoc",
            target_identity=identity(),
            payload=schedule_payload(),
        )
    )

    assert result.status is EngineStatus.RECOVERY_REQUIRED
    assert result.reason == "adapter_execution_uncertain"
    transaction = runtime.journal.journal.transactions[-1]
    assert transaction.phase is JournalPhase.RECOVERY_REQUIRED
    assert transaction.reason == "RuntimeError"
    assert transaction.intent["socref"] == 80


@pytest.mark.asyncio
async def test_simulator_history_is_bounded_to_recent_operations() -> None:
    adapter = SimulatorAdapter()
    request = AdapterRequest(ControlOperation.READ_ONLY)

    for _ in range(SIMULATOR_HISTORY_LIMIT + 3):
        await adapter.execute(request)

    assert len(adapter.history) == SIMULATOR_HISTORY_LIMIT


def test_simulator_restore_rejects_oversized_durable_collections() -> None:
    runtime = SimulatorRuntime()
    oversized_history = runtime.export_state()
    oversized_history["adapter"]["history"] = [
        {"operation": "read_only"}
        for _ in range(SIMULATOR_HISTORY_LIMIT + 1)
    ]

    with pytest.raises(ValueError, match="history limit"):
        SimulatorRuntime.from_state(
            oversized_history,
            config_entry_id="entry-a",
            gateway_serial="GATEWAY",
        )

    oversized_schedules = runtime.export_state()
    oversized_schedules["adapter"]["schedules"] = [
        {
            "m_id": 240000001 + index,
            "start": datetime(2030, 1, 1, tzinfo=UTC).isoformat(),
            "duration_seconds": 900,
            "socref": 50,
            "origin": ScheduleOrigin.LOCAL_CREATED.value,
        }
        for index in range(SIMULATOR_SCHEDULE_LIMIT + 1)
    ]
    with pytest.raises(ValueError, match="schedule limit"):
        SimulatorRuntime.from_state(
            oversized_schedules,
            config_entry_id="entry-a",
            gateway_serial="GATEWAY",
        )


@pytest.mark.asyncio
async def test_external_automation_requires_explicit_authorization() -> None:
    runtime = SimulatorRuntime()
    runtime.adapter.seed_external_schedule(
        SimulatedSchedule(
            m_id=240000001,
            start=datetime(2030, 1, 2, tzinfo=UTC),
            duration_seconds=3600,
            socref=80,
            origin=ScheduleOrigin.EXTERNAL_OBSERVED,
        )
    )
    exact_identity = JournalTargetIdentity(
        m_id=240000001,
        service_no=110,
        asset_id="battery-1",
    )
    request = AdapterRequest(
        ControlOperation.DELETE,
        service="targetsoc",
        target_identity=exact_identity,
    )

    blocked = await runtime.execute(request, initiative=Initiative.AUTOMATION)
    authorized = await runtime.execute(
        request,
        initiative=Initiative.AUTOMATION,
        automation_authorized=True,
    )

    assert blocked.status is EngineStatus.BLOCKED
    assert "automation_policy_not_authorized" in blocked.decision.blocked_reasons
    assert authorized.status is EngineStatus.SUCCESS
    assert runtime.adapter.schedules == {}


@pytest.mark.asyncio
async def test_replacement_saga_completes_delete_create_and_records_steps() -> None:
    runtime = SimulatorRuntime()
    await runtime.execute(
        AdapterRequest(
            ControlOperation.CREATE,
            service="targetsoc",
            target_identity=identity(),
            payload=schedule_payload(socref=80),
        )
    )

    result = await runtime.execute(
        AdapterRequest(
            ControlOperation.REPLACE,
            service="targetsoc",
            target_identity=identity(),
            payload=schedule_payload(socref=63),
        )
    )

    assert result.status is EngineStatus.SUCCESS
    assert runtime.adapter.schedules[240000001].socref == 63
    assert runtime.journal.journal.replacement_state["phase"] == (
        ReplacementSagaPhase.RECONCILED
    )
    events = [item["event"] for item in runtime.journal.journal.audit_history]
    assert events[-4:] == [
        "replacement_predecessor_deleted",
        "replacement_created",
        "replacement_reconciled",
        "operation_result",
    ]


@pytest.mark.asyncio
async def test_replacement_create_failure_compensates_predecessor() -> None:
    runtime = SimulatorRuntime()
    await runtime.execute(
        AdapterRequest(
            ControlOperation.CREATE,
            service="targetsoc",
            target_identity=identity(),
            payload=schedule_payload(socref=80),
        )
    )
    runtime.adapter.inject_fault("replace_create_failure")

    result = await runtime.execute(
        AdapterRequest(
            ControlOperation.REPLACE,
            service="targetsoc",
            target_identity=identity(),
            payload=schedule_payload(socref=60),
        )
    )

    assert result.status is EngineStatus.FAILED
    assert runtime.adapter.schedules[240000001].socref == 80
    assert runtime.journal.journal.transactions[-1].phase is JournalPhase.FAILED
    assert runtime.journal.journal.replacement_state["phase"] == (
        ReplacementSagaPhase.COMPENSATED
    )


@pytest.mark.asyncio
async def test_partial_replacement_survives_restart_and_recovers() -> None:
    runtime = SimulatorRuntime(
        journal=InMemoryJournalPort(
            ControlJournal.empty(
                config_entry_id="simulator-entry",
                gateway_serial="SIMULATOR",
            )
        )
    )
    await runtime.execute(
        AdapterRequest(
            ControlOperation.CREATE,
            service="targetsoc",
            target_identity=identity(),
            payload=schedule_payload(socref=80),
        )
    )
    runtime.adapter.inject_fault("replace_create_failure")
    runtime.adapter.inject_fault("replace_compensation_failure")
    partial = await runtime.execute(
        AdapterRequest(
            ControlOperation.REPLACE,
            service="targetsoc",
            target_identity=identity(),
            payload=schedule_payload(socref=61),
        )
    )
    transaction_id = partial.transaction_id
    assert transaction_id is not None

    restored = SimulatorRuntime.from_state(
        runtime.export_state(),
        config_entry_id="simulator-entry",
        gateway_serial="SIMULATOR",
    )
    restored.adapter.inject_fault("replace_create_failure", active=False)
    restored.adapter.inject_fault("replace_compensation_failure", active=False)
    recovered = await restored.execute(
        AdapterRequest(
            ControlOperation.RECOVERY_APPLY,
            service="targetsoc",
            target_identity=identity(),
            payload={"transaction_id": transaction_id},
        )
    )

    assert partial.status is EngineStatus.PARTIAL
    assert runtime.adapter.schedules == {}
    assert restored.journal.journal.transactions[-1].phase is JournalPhase.RECONCILED
    assert restored.journal.journal.replacement_state["phase"] == (
        ReplacementSagaPhase.RECONCILED
    )
    assert recovered.status is EngineStatus.SUCCESS
    assert restored.adapter.schedules[240000001].socref == 61




@pytest.mark.asyncio
async def test_replacement_pause_fault_is_one_shot_and_recoverable() -> None:
    runtime = SimulatorRuntime()
    await runtime.execute(
        AdapterRequest(
            ControlOperation.CREATE,
            service="targetsoc",
            target_identity=identity(),
            payload=schedule_payload(socref=80),
        )
    )
    runtime.adapter.inject_fault("replace_pause_after_delete")
    partial = await runtime.execute(
        AdapterRequest(
            ControlOperation.REPLACE,
            service="targetsoc",
            target_identity=identity(),
            payload=schedule_payload(socref=62),
        )
    )
    assert partial.transaction_id is not None

    recovered = await runtime.execute(
        AdapterRequest(
            ControlOperation.RECOVERY_APPLY,
            service="targetsoc",
            target_identity=identity(),
            payload={"transaction_id": partial.transaction_id},
        )
    )

    assert partial.status is EngineStatus.PARTIAL
    assert "replace_pause_after_delete" not in runtime.adapter.faults
    assert recovered.status is EngineStatus.SUCCESS
    assert runtime.adapter.schedules[240000001].socref == 62


@pytest.mark.asyncio
async def test_new_replacement_is_blocked_while_prior_saga_needs_recovery() -> None:
    runtime = SimulatorRuntime()
    for m_id in (240000001, 240000002):
        await runtime.execute(
            AdapterRequest(
                ControlOperation.CREATE,
                service="targetsoc",
                target_identity=identity(m_id),
                payload=schedule_payload(socref=80),
            )
        )
    runtime.adapter.inject_fault("replace_pause_after_delete")
    partial = await runtime.execute(
        AdapterRequest(
            ControlOperation.REPLACE,
            service="targetsoc",
            target_identity=identity(240000001),
            payload=schedule_payload(socref=62),
        )
    )
    saga_transaction_id = runtime.journal.journal.replacement_state["transaction_id"]

    blocked = await runtime.execute(
        AdapterRequest(
            ControlOperation.REPLACE,
            service="targetsoc",
            target_identity=identity(240000002),
            payload=schedule_payload(socref=64),
        )
    )

    assert partial.status is EngineStatus.PARTIAL
    assert blocked.status is EngineStatus.BLOCKED
    assert "pending_command_operations" in blocked.decision.blocked_reasons
    assert runtime.journal.journal.replacement_state["transaction_id"] == saga_transaction_id


@pytest.mark.asyncio
async def test_unresolved_replacement_transaction_blocks_new_saga_after_restore() -> None:
    journal = ControlJournal.empty(
        config_entry_id="simulator",
        gateway_serial="SIMULATOR",
    ).begin_transaction(
        transaction_id="interrupted-replace",
        operation=ControlOperation.REPLACE.value,
        target_identity=identity(240000001),
    )
    runtime = SimulatorRuntime(journal=InMemoryJournalPort(journal))
    runtime.adapter.schedules[240000002] = SimulatedSchedule(
        m_id=240000002,
        start=datetime(2030, 1, 2, tzinfo=UTC),
        duration_seconds=900,
        socref=70,
        sch_index=1,
        source_id=7,
    )

    blocked = await runtime.execute(
        AdapterRequest(
            ControlOperation.REPLACE,
            service="targetsoc",
            target_identity=identity(240000002),
            payload=schedule_payload(socref=64),
        )
    )

    assert blocked.status is EngineStatus.BLOCKED
    assert "pending_command_operations" in blocked.decision.blocked_reasons
    assert runtime.journal.journal.transaction("interrupted-replace") is not None
    assert runtime.journal.journal.replacement_state == {}


@pytest.mark.asyncio
async def test_timeout_and_delivery_unknown_remain_recoverable() -> None:
    timed_out = SimulatorRuntime()
    timed_out.adapter.inject_fault("timeout")
    timeout_result = await timed_out.execute(
        AdapterRequest(
            ControlOperation.DIRECT_TARGET_SOC,
            service="targetsoc",
            target_identity=JournalTargetIdentity(
                asset_id="battery-1",
                service_no=110,
            ),
            payload={"socref": 75},
        )
    )
    uncertain = SimulatorRuntime()
    uncertain.adapter.inject_fault("delivery_unknown")
    delivery_result = await uncertain.execute(
        AdapterRequest(
            ControlOperation.DIRECT_TARGET_SOC,
            service="targetsoc",
            target_identity=JournalTargetIdentity(
                asset_id="battery-1",
                service_no=110,
            ),
            payload={"socref": 74},
        )
    )

    assert timeout_result.status is EngineStatus.RECOVERY_REQUIRED
    assert timed_out.adapter.battery.target_soc_percent is None
    assert timed_out.journal.journal.transactions[-1].phase is (
        JournalPhase.RECOVERY_REQUIRED
    )
    assert delivery_result.status is EngineStatus.RECOVERY_REQUIRED
    assert uncertain.adapter.battery.target_soc_percent == 74
    assert uncertain.journal.journal.transactions[-1].phase is (
        JournalPhase.RECOVERY_REQUIRED
    )
