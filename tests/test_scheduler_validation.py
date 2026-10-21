from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

import custom_components.pixiihome_controller.command as command_module
import custom_components.pixiihome_controller.scheduler.validation as validation_module
from custom_components.pixiihome_controller.allocator import MessageIdAllocator
from custom_components.pixiihome_controller.capabilities import CapabilityRegistry
from custom_components.pixiihome_controller.command import (
    CommandManager,
    CommandStatus,
    CorrelatedCommandRequest,
    CorrelatedCommandResult,
)
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
from custom_components.pixiihome_controller.protocol.commands import CommandMethod
from custom_components.pixiihome_controller.protocol.models import ParsedMqttMessage
from custom_components.pixiihome_controller.protocol.mqtt import parse_ack
from custom_components.pixiihome_controller.protocol.scheduler_commands import (
    ScheduledCreate,
    ScheduledDelete,
    ScheduledGet,
)
from custom_components.pixiihome_controller.scheduler.models import (
    NormalizedRecurrence,
    NormalizedSchedule,
    ScheduleCompleteness,
    ScheduleConsistency,
    default_ownership,
)
from custom_components.pixiihome_controller.scheduler.mutations import (
    SchedulerTransactionResult,
    SchedulerTransactionStatus,
)
from custom_components.pixiihome_controller.scheduler.ownership import (
    OwnershipLifecycle,
    SchedulerMutationOperation,
    SchedulerOwnershipRecord,
    SchedulerOwnershipStore,
)
from custom_components.pixiihome_controller.scheduler.validation import (
    SchedulerCandidateInterval,
    SchedulerValidationCommandExecutor,
    SchedulerValidationPermit,
    SchedulerValidationRoundtrip,
    ValidationPermitStage,
    evaluate_candidate_conflicts,
)

from .conftest import load_json

STARTTIME = datetime(2030, 1, 2, 3, 0, tzinfo=UTC)


def test_validation_authorization_does_not_open_production_release() -> None:
    context = ControlPolicyInput(
        operation=ControlOperation.CREATE,
        runtime_mode=RuntimeMode.PRODUCTION,
        operator_mode=OperatorMode.LOCAL_CONTROL,
        origin=ScheduleOrigin.LOCAL_CREATED,
        release_state=ReleaseState.LIVE_RELEASE_CLOSED,
        plant_health=PlantHealth(
            telemetry_fresh=True,
            system_safe=True,
            battery_safe=True,
            grid_safe=True,
            system_events_clear=True,
            reasons=(),
        ),
        operation_impact=OperationImpact(active_service=0, immediate_effect_known=True),
        scheduler_visibility_fresh=True,
        ownership_store_valid=True,
        ownership_ambiguous=False,
        mqtt_connected=True,
        validation_authorized=True,
    )

    decision = evaluate_control_operation(context)

    assert not decision.permitted
    assert decision.blocked_reasons == ("release_gate_closed",)


def _request(
    method: CommandMethod,
    m_id: int = 240000001,
    *,
    fields: dict[str, object] | None = None,
    service: str | None = None,
    expected_services: tuple[str, ...] = (),
) -> CorrelatedCommandRequest:
    return CorrelatedCommandRequest(
        method=method,
        message_id=m_id,
        service=service,
        fields=fields or {},
        expected_services=expected_services,
    )


def _permit(clock: list[float]) -> SchedulerValidationPermit:
    return SchedulerValidationPermit.issue(
        config_entry_id="entry-1",
        gateway_serial="GATEWAY",
        candidate_starttime=STARTTIME,
        candidate_duration=300,
        candidate_socref=90,
        clock=lambda: clock[0],
        ttl_seconds=10,
    )


def _schedule(
    start: int,
    duration: int = 300,
    *,
    recurrence: bool | None = False,
    consistency: ScheduleConsistency = ScheduleConsistency.CONSISTENT,
    completeness: ScheduleCompleteness = ScheduleCompleteness.COMPLETE,
) -> NormalizedSchedule:
    return NormalizedSchedule(
        sch_index=1,
        start_epoch=start,
        duration_seconds=duration,
        end_epoch=start + duration,
        service_no=2,
        service_type=1,
        enabled=True,
        service_priority=None,
        asset_id=None,
        source_id=None,
        recurrence=NormalizedRecurrence(recurring=recurrence, recurrence_detail=None),
        ownership=default_ownership(),
        completeness=completeness,
        consistency=consistency,
    )


def test_validation_permit_binds_exact_create_reconcile_delete_sequence() -> None:
    clock = [0.0]
    permit = _permit(clock)
    create = _request(
        CommandMethod.SET,
        service="targetsoc",
        fields={
            "starttime": "2030-01-02T03:00:00Z",
            "duration": 300,
            "values": {"socref": 90},
        },
    )
    permit.authorize(create, "GATEWAY")
    assert permit.stage is ValidationPermitStage.CREATE_SET
    assert permit.bound_m_id == 240000001

    permit.authorize(
        _request(CommandMethod.GET, expected_services=("targetsoc",)),
        "GATEWAY",
    )
    assert permit.stage is ValidationPermitStage.CREATE_GET
    permit.mark_create_reconciled()
    permit.authorize(
        _request(CommandMethod.DELETE, expected_services=("targetsoc",)),
        "GATEWAY",
    )
    assert permit.stage is ValidationPermitStage.DELETE_GET
    permit.authorize(
        _request(CommandMethod.GET, expected_services=("targetsoc",)),
        "GATEWAY",
    )
    permit.mark_delete_reconciled()
    assert permit.stage is ValidationPermitStage.CONSUMED
    assert permit.diagnostics() == {"active": False, "stage": "consumed", "bound": True}


def test_validation_permit_rejects_wrong_context_shape_and_expiry() -> None:
    clock = [0.0]
    permit = _permit(clock)
    with pytest.raises(Exception):
        permit.authorize(
            _request(
                CommandMethod.SET,
                service="targetsoc",
                fields={
                    "starttime": "2030-01-02T03:00:00Z",
                    "duration": 301,
                    "values": {"socref": 90},
                },
            ),
            "GATEWAY",
        )
    assert permit.stage is ValidationPermitStage.FAILED
    expired = _permit(clock)
    clock[0] = 11.0
    with pytest.raises(Exception):
        expired.authorize(
            _request(
                CommandMethod.SET,
                service="targetsoc",
                fields={
                    "starttime": "2030-01-02T03:00:00Z",
                    "duration": 300,
                    "values": {"socref": 90},
                },
            ),
            "OTHER",
        )
    assert expired.stage is ValidationPermitStage.FAILED


class _ValidationCommandManager:
    serial = "GATEWAY"

    def __init__(self) -> None:
        self.requests: list[CorrelatedCommandRequest] = []

    async def execute_scheduler_validation_correlated(self, request, authorization):
        self.requests.append(request)
        authorization.authorize(request, self.serial)
        return CorrelatedCommandResult(
            status=CommandStatus.SUCCESS,
            message_id=request.message_id,
            ack=parse_ack(load_json("mqtt/scheduler_ack_success.json")),
        )


@pytest.mark.asyncio
async def test_validation_executor_uses_typed_path_for_exact_commands_only() -> None:
    clock = [0.0]
    permit = _permit(clock)
    command_manager = _ValidationCommandManager()
    executor = SchedulerValidationCommandExecutor(
        command_manager,  # type: ignore[arg-type]
        permit,
        config_entry_id="entry-1",
        gateway_serial="GATEWAY",
    )
    create = ScheduledCreate(
        service="targetsoc",
        m_id=240000001,
        starttime=STARTTIME,
        duration_seconds=300,
        values={"socref": 90},
    ).build()
    mutation = await executor.execute_mutation(create)
    assert mutation.status.value == "ack_success"
    assert permit.stage is ValidationPermitStage.CREATE_SET
    get = await executor.execute_get(
        ScheduledGet(m_id=240000001, expected_service="targetsoc").build()
    )
    assert get is not None
    assert permit.stage is ValidationPermitStage.CREATE_GET
    assert [request.method for request in command_manager.requests] == [
        CommandMethod.SET,
        CommandMethod.GET,
    ]

    permit.mark_create_reconciled()
    delete = ScheduledDelete(m_id=240000001, expected_service="targetsoc").build()
    delete_result = await executor.execute_mutation(delete)
    assert delete_result.status.value == "ack_success"
    assert permit.stage is ValidationPermitStage.DELETE_GET


def test_candidate_conflicts_use_intervals_and_fail_closed_evidence() -> None:
    candidate = SchedulerCandidateInterval(1000, 1300)
    assert not evaluate_candidate_conflicts(candidate, [_schedule(1300, 300)]).blocked
    assert evaluate_candidate_conflicts(candidate, [_schedule(1299, 1)]).blocked
    assert evaluate_candidate_conflicts(candidate, [_schedule(1100, 1)]).blocked
    assert evaluate_candidate_conflicts(candidate, [_schedule(2000, recurrence=True)]).blocked
    assert evaluate_candidate_conflicts(candidate, [_schedule(2000, recurrence=None)]).blocked
    assert evaluate_candidate_conflicts(
        candidate,
        [_schedule(2000, consistency=ScheduleConsistency.MISMATCH)],
    ).blocked


def test_candidate_conflicts_surface_existing_owned_intervals() -> None:
    record = SchedulerOwnershipRecord(
        config_entry_id="entry-1",
        gateway_serial="GATEWAY",
        m_id=240000001,
        service="targetsoc",
        intended_state={
            "socref": 90,
            "starttime": datetime.fromtimestamp(1100, UTC),
            "duration": 300,
        },
        lifecycle=OwnershipLifecycle.OWNED,
    )
    result = evaluate_candidate_conflicts(
        SchedulerCandidateInterval(1200, 1500),
        [],
        [record],
    )
    assert result.blocked
    assert "owned_schedule_interval_overlap" in result.reasons


class _RefreshClient:
    def __init__(self) -> None:
        self.fail_list = False

    async def list_entries(self, from_epoch: int, to_epoch: int):
        del from_epoch, to_epoch
        if self.fail_list:
            raise RuntimeError("LIST failed")
        return []

    async def get_detail(self, sch_index: int) -> Any:
        return SimpleNamespace(sch_index=sch_index)


@pytest.mark.asyncio
async def test_list_evidence_is_success_only_fresh_and_detail_does_not_refresh_it() -> None:
    from custom_components.pixiihome_controller.scheduler.manager import SchedulerRefreshManager

    client = _RefreshClient()
    manager = SchedulerRefreshManager(
        client,
        clock=iter([1.0, 2.0, 3.0]).__next__,
        monotonic_clock=lambda: 10.0,
    )
    await manager.refresh_list(0, 2000)
    await manager.refresh_detail(7)
    assert manager.list_visibility_fresh(now_monotonic=70.0)
    assert not manager.list_visibility_fresh(now_monotonic=71.0)
    assert manager.list_visibility_covers(100, 1900)
    assert not manager.list_visibility_covers(0, 2001)

    client.fail_list = True
    with pytest.raises(RuntimeError):
        await manager.refresh_list(0, 2000)
    assert not manager.list_visibility_fresh(now_monotonic=20.0)
    assert manager.list_visibility_diagnostics()["invalidated"] is True


class _RoundtripManager:
    def __init__(self) -> None:
        self.normalized_schedules = ()
        self.list_calls: list[tuple[int, int]] = []

    async def refresh_list(self, from_epoch: int, to_epoch: int):
        self.list_calls.append((from_epoch, to_epoch))
        return ()

    def list_visibility_fresh(self) -> bool:
        return True

    def list_visibility_covers(self, from_epoch: int, to_epoch: int) -> bool:
        return from_epoch == 0 and to_epoch == self.list_calls[-1][1]


@pytest.mark.asyncio
async def test_roundtrip_real_layers_route_absence_before_allocator_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = cls(2030, 1, 1, 0, 0, tzinfo=UTC)
            return fixed if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(validation_module, "datetime", FrozenDateTime)
    persistence_events: list[tuple[str, object, tuple[int, ...], tuple[int, ...]]] = []

    def persist_allocator(state: dict[str, Any]) -> None:
        persistence_events.append(
            (
                "allocator",
                tuple(state["owned_m_ids"]),
                tuple(sorted(allocator.owned_ids)),
                tuple(sorted(allocator.active_ids)),
            )
        )

    allocator = MessageIdAllocator(next_value=240000001, persist=persist_allocator)
    ownership = SchedulerOwnershipStore(config_entry_id="entry-1", gateway_serial="GATEWAY")

    def persist_ownership(state: dict[str, Any]) -> None:
        records = state["records"]
        lifecycle = records[0]["lifecycle"] if records else None
        persistence_events.append(
            (
                "ownership",
                lifecycle,
                tuple(sorted(allocator.owned_ids)),
                tuple(sorted(allocator.active_ids)),
            )
        )

    subscriptions: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    publications: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    created_payload: dict[str, Any] | None = None
    get_count = 0

    async def wait_for_mqtt(hass: Any) -> bool:
        del hass
        return True

    async def subscribe(*args: Any, **kwargs: Any) -> Any:
        subscriptions.append((args, kwargs))
        return lambda: None

    capabilities = CapabilityRegistry()
    command_manager = CommandManager(
        cast(Any, SimpleNamespace()),
        "GATEWAY",
        capabilities,
        lambda: PlantHealth(
            telemetry_fresh=True,
            system_safe=True,
            battery_safe=True,
            grid_safe=True,
            system_events_clear=True,
            reasons=(),
        ),
        allocator=allocator,
    )

    async def publish(hass: Any, topic: str, payload: str, **kwargs: Any) -> None:
        nonlocal created_payload, get_count
        del hass
        body = json.loads(payload)
        publications.append((topic, body, kwargs))
        method = body["method"]
        m_id = body["m_id"]
        if method == "set":
            created_payload = body
            ack_payload = {
                "method": "set",
                "service": "targetsoc",
                "m_id": m_id,
                "response": 65535,
            }
        elif method == "delete":
            ack_payload = {
                "method": "delete",
                "service": "targetsoc",
                "m_id": m_id,
                "response": 65535,
            }
        else:
            get_count += 1
            if get_count == 1:
                assert created_payload is not None
                ack_payload = {
                    "method": "get",
                    "service": "targetsoc",
                    "m_id": m_id,
                    "response": 65535,
                    "values": {
                        "socref": created_payload["values"]["socref"],
                        "starttime": created_payload["starttime"],
                        "duration": created_payload["duration"],
                    },
                }
            else:
                ack_payload = {
                    "method": "get",
                    "service": "unknown",
                    "m_id": m_id,
                    "response": 11,
                    "response_text": "arbitrary text",
                }
        command_manager.handle_ack_message(
            ParsedMqttMessage(
                serial="GATEWAY",
                family="ack",
                payload=ack_payload,
                topic="pixii/ack/GATEWAY",
                received_at=FrozenDateTime.now(UTC),
            )
        )

    monkeypatch.setattr(command_module, "async_wait_for_mqtt_client", wait_for_mqtt)
    monkeypatch.setattr(command_module, "async_subscribe", subscribe)
    monkeypatch.setattr(command_module, "async_publish", publish)
    await command_manager.start()

    scheduler_manager = _RoundtripManager()
    runtime = SimpleNamespace(
        scheduler_validation_lock=asyncio.Lock(),
        scheduler_validation_attempt_consumed=False,
        scheduler_validation_running=False,
        scheduler_manager=scheduler_manager,
        release_gate=SimpleNamespace(live_writes_enabled=False),
        scheduler_ownership=ownership,
        scheduler_mutation_manager=object(),
        scheduler_control_context=lambda operation, **kwargs: ControlPolicyInput(
            operation=operation,
            runtime_mode=RuntimeMode.PRODUCTION,
            operator_mode=OperatorMode.LOCAL_CONTROL,
            origin=ScheduleOrigin.LOCAL_CREATED,
            release_state=ReleaseState.LIVE_RELEASE_CLOSED,
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
            validation_authorized=kwargs["validation_authorized"],
        ),
        battery=SimpleNamespace(
            raw_soc=55.0,
            effective_min_soc=10.0,
            effective_max_soc=90.0,
        ),
        command_manager=command_manager,
        persist_scheduler_ownership=persist_ownership,
    )

    result = await SchedulerValidationRoundtrip(runtime).run()
    await command_manager.stop()

    assert result["status"] == "blocked"
    assert result["phase"] == "preflight"
    assert result["preflight_blockers"] == ["release_gate_closed"]
    assert publications == []
    assert len(subscriptions) == 1
    assert subscriptions[0][0][1] == "pixii/ack/GATEWAY"
    diagnostics = command_manager.diagnostics()
    assert diagnostics["scheduler_transport_timeouts"] == 0
    assert diagnostics["scheduler_get_matches"] == 0
    assert diagnostics["state_changing_publish_attempts"] == 0
    assert diagnostics["read_only_publish_attempts"] == 0
    assert scheduler_manager.list_calls == []
    assert ownership.records == {}
    assert allocator.owned_ids == set()
    assert allocator.active_ids == set()
    assert persistence_events == []


class _RoundtripMutationManager:
    calls: list[str] = []

    def __init__(self, *, executor, **kwargs):
        del kwargs
        self.executor = executor
        self.calls.append("constructed")

    async def create(self, request):
        permit = self.executor._permit
        permit.authorize(
            _request(
                CommandMethod.SET,
                service="targetsoc",
                fields={
                    "starttime": request.starttime.astimezone(UTC)
                    .replace(microsecond=0)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "duration": request.duration_seconds,
                    "values": dict(request.values or {}),
                },
            ),
            "GATEWAY",
        )
        permit.authorize(
            _request(CommandMethod.GET, expected_services=("targetsoc",)),
            "GATEWAY",
        )
        self.calls.append("create")
        return SchedulerTransactionResult(
            operation=SchedulerMutationOperation.CREATE,
            status=SchedulerTransactionStatus.SUCCESS,
            m_id=240000001,
            service="targetsoc",
            lifecycle_after=OwnershipLifecycle.OWNED,
        )

    async def delete(self, m_id: int):
        permit = self.executor._permit
        permit.authorize(
            _request(CommandMethod.DELETE, expected_services=("targetsoc",)),
            "GATEWAY",
        )
        permit.authorize(
            _request(CommandMethod.GET, expected_services=("targetsoc",)),
            "GATEWAY",
        )
        self.calls.append("delete")
        assert m_id == 240000001
        return SchedulerTransactionResult(
            operation=SchedulerMutationOperation.DELETE,
            status=SchedulerTransactionStatus.SUCCESS,
            m_id=m_id,
            service="targetsoc",
            lifecycle_after=OwnershipLifecycle.ORPHANED,
        )


@pytest.mark.parametrize(
    "automation_states",
    [
        pytest.param([], id="no-automation-entities"),
        pytest.param(
            [
                SimpleNamespace(
                    entity_id="automation.pixii_observer",
                    state="on",
                    attributes={"friendly_name": "Pixii Home: Arbitrary Observer"},
                )
            ],
            id="pixii-named-observer-on",
        ),
        pytest.param(
            [
                SimpleNamespace(
                    entity_id="automation.pulse_fix",
                    state="on",
                    attributes={"friendly_name": "Automation: Pulse fix"},
                ),
                SimpleNamespace(
                    entity_id="automation.pixii_mqtt_service",
                    state="on",
                    attributes={"friendly_name": "Pixii Home: MQTT Service"},
                ),
                SimpleNamespace(
                    entity_id="automation.renamed_legacy_control",
                    state="on",
                    attributes={"friendly_name": "Renamed legacy control"},
                ),
            ],
            id="legacy-presence-state-and-rename",
        ),
    ],
)
@pytest.mark.asyncio
async def test_roundtrip_ignores_ha_automation_names_and_states(
    monkeypatch: pytest.MonkeyPatch,
    automation_states: list[Any],
) -> None:

    monkeypatch.setattr(validation_module, "SchedulerMutationManager", _RoundtripMutationManager)
    _RoundtripMutationManager.calls.clear()
    manager = _RoundtripManager()
    ownership = SchedulerOwnershipStore(config_entry_id="entry-1", gateway_serial="GATEWAY")
    runtime = SimpleNamespace(
        scheduler_validation_lock=asyncio.Lock(),
        scheduler_validation_attempt_consumed=False,
        release_gate=SimpleNamespace(live_writes_enabled=False),
        scheduler_validation_running=False,
        scheduler_manager=manager,
        scheduler_ownership=ownership,
        scheduler_mutation_manager=object(),
        scheduler_control_context=lambda operation, **kwargs: ControlPolicyInput(
            operation=operation,
            runtime_mode=RuntimeMode.PRODUCTION,
            operator_mode=OperatorMode.LOCAL_CONTROL,
            origin=ScheduleOrigin.LOCAL_CREATED,
            release_state=ReleaseState.LIVE_RELEASE_CLOSED,
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
            validation_authorized=kwargs["validation_authorized"],
        ),
        battery=SimpleNamespace(
            raw_soc=55.0,
            effective_min_soc=10.0,
            effective_max_soc=90.0,
        ),
        hass=SimpleNamespace(states=SimpleNamespace(async_all=lambda: automation_states)),
        command_manager=SimpleNamespace(allocator=MessageIdAllocator(), pending_count=0),
        persist_scheduler_ownership=lambda state: None,
    )
    roundtrip = SchedulerValidationRoundtrip(runtime)

    first = await roundtrip.run()
    second = await roundtrip.run()

    assert first["status"] == "blocked"
    assert first["preflight_blockers"] == ["release_gate_closed"]
    assert runtime.scheduler_validation_attempt_consumed is False
    assert manager.list_calls == []
    assert _RoundtripMutationManager.calls == []
    assert second["preflight_blockers"] == ["release_gate_closed"]
