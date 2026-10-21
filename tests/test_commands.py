from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

import custom_components.pixiihome_controller.command as command_module
from custom_components.pixiihome_controller.allocator import MessageIdAllocator
from custom_components.pixiihome_controller.capabilities import CapabilityRegistry
from custom_components.pixiihome_controller.command import (
    CommandErrorCode,
    CommandManager,
    CommandStatus,
    CorrelatedCommandRequest,
    WriteGate,
)
from custom_components.pixiihome_controller.control.models import PlantHealth
from custom_components.pixiihome_controller.exceptions import CommandValidationError
from custom_components.pixiihome_controller.protocol.commands import (
    AdaptivePeakShaving,
    BatteryCalibration,
    CommandMethod,
    DemandResponse,
    PixiiCommand,
    RunningServiceGet,
    TargetSoC,
)
from custom_components.pixiihome_controller.protocol.models import ParsedMqttMessage

SERIAL = "GATEWAY"

def healthy_plant() -> PlantHealth:
    return PlantHealth(
        telemetry_fresh=True,
        system_safe=True,
        battery_safe=True,
        grid_safe=True,
        system_events_clear=True,
        reasons=(),
    )


def parsed_ack(payload: dict[str, Any]) -> ParsedMqttMessage:
    return ParsedMqttMessage(
        serial=SERIAL,
        family="ack",
        payload=payload,
        topic=f"pixii/ack/{SERIAL}",
        received_at=datetime.now(UTC),
    )

async def make_manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    capability: tuple[str, str] = ("targetsoc", "set"),
    timeout: float = 0.05,
    health: PlantHealth | None = None,
) -> CommandManager:
    del timeout
    capabilities = CapabilityRegistry()
    capabilities.families.update({"core", "battery"})
    capabilities.command_methods.setdefault(capability[0], set()).add(capability[1])
    manager = CommandManager(
        cast(Any, SimpleNamespace()),
        SERIAL,
        capabilities,
        lambda: health or healthy_plant(),
    )
    monkeypatch.setattr(command_module, "async_wait_for_mqtt_client", _async_true)
    monkeypatch.setattr(
        command_module,
        "async_subscribe",
        _async_subscribe,
    )
    await manager.start()
    return manager


async def _async_true(hass: Any) -> bool:
    del hass
    return True


async def _async_subscribe(*args: Any, **kwargs: Any):
    del args, kwargs
    return lambda: None


def target_command(*, timeout: float = 0.05):
    return TargetSoC(90, 3600, timeout_seconds=timeout).build()


async def wait_for_pending(manager: CommandManager) -> int:
    for _ in range(20):
        if manager._pending:
            return next(iter(manager._pending))
        await asyncio.sleep(0)
    raise AssertionError("command did not become pending")


@pytest.mark.asyncio
async def test_disabled_write_gate_blocks_before_any_mqtt_publish(monkeypatch):
    manager = CommandManager(
        cast(Any, SimpleNamespace()),
        SERIAL,
        CapabilityRegistry(),
        healthy_plant,
    )
    publishes: list[Any] = []

    async def publish(*args: Any, **kwargs: Any) -> None:
        publishes.append((args, kwargs))

    monkeypatch.setattr(command_module, "async_publish", publish)
    result = await manager.execute(target_command())

    assert result.status is CommandStatus.REJECTED
    assert result.error is not None
    assert result.error.code is CommandErrorCode.GATE_BLOCKED
    assert "release_gate_closed" in result.error.details
    assert publishes == []
    calibration_result = await manager.execute(BatteryCalibration(90, 7200).build())
    assert calibration_result.status is CommandStatus.REJECTED
    assert calibration_result.error is not None
    assert calibration_result.error.code is CommandErrorCode.GATE_BLOCKED
    assert publishes == []
    assert manager.allocator.owned_ids == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("capability", "command"),
    [
        (("targetsoc", "set"), TargetSoC(90, 3600).build()),
        (("demandresponse", "set"), DemandResponse(3600, 1000).build()),
        (("batterycalibration", "set"), BatteryCalibration(90, 7200).build()),
        (
            ("adaptivepeakshaving", "set"),
            AdaptivePeakShaving(0, 1000, 1000, 1000, True, 100, 900).build(),
        ),
    ],
)
async def test_production_write_gate_blocks_every_state_command(
    monkeypatch: pytest.MonkeyPatch,
    capability: tuple[str, str],
    command: Any,
):
    manager = await make_manager(monkeypatch, capability=capability)
    publishes: list[Any] = []

    async def publish(*args: Any, **kwargs: Any) -> None:
        publishes.append((args, kwargs))

    monkeypatch.setattr(command_module, "async_publish", publish)
    result = await manager.execute(command)

    assert result.status is CommandStatus.REJECTED
    assert result.error is not None
    assert "release_gate_closed" in result.error.details
    assert publishes == []
    assert manager.diagnostics()["state_changing_publish_attempts"] == 0



@pytest.mark.asyncio
async def test_typed_validation_authorization_cannot_bypass_release_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await make_manager(monkeypatch, capability=("targetsoc", "set"))
    publishes: list[Any] = []

    async def publish(*args: Any, **kwargs: Any) -> None:
        publishes.append((args, kwargs))

    class Authorization:
        def __init__(self) -> None:
            self.requests: list[CorrelatedCommandRequest] = []

        def authorize(self, request: CorrelatedCommandRequest, serial: str) -> None:
            assert serial == SERIAL
            self.requests.append(request)

    authorization = Authorization()
    monkeypatch.setattr(command_module, "async_publish", publish)
    request = CorrelatedCommandRequest(
        method=CommandMethod.SET,
        message_id=240000001,
        service="targetsoc",
        fields={
            "starttime": "2030-01-02T03:00:00Z",
            "duration": 300,
            "values": {"socref": 90},
        },
        expected_services=("targetsoc",),
        timeout_seconds=1.0,
    )

    result = await manager.execute_scheduler_validation_correlated(request, authorization)

    assert result.status is CommandStatus.REJECTED
    assert result.error is not None
    assert result.error.details == ("release_gate_closed", "production_control_disabled")
    assert authorization.requests == [request]
    assert publishes == []
    assert manager.diagnostics()["state_changing_publish_attempts"] == 0

@pytest.mark.asyncio
async def test_production_read_only_get_publishes_without_write_gate(monkeypatch):
    manager = await make_manager(
        monkeypatch,
        capability=("runningservice", "get"),
    )
    published: dict[str, Any] = {}
    published_event = asyncio.Event()

    async def publish(hass: Any, topic: str, payload: str, **kwargs: Any) -> None:
        del hass, topic
        published["body"] = json.loads(payload)
        published["kwargs"] = kwargs
        published_event.set()

    monkeypatch.setattr(command_module, "async_publish", publish)
    task = asyncio.create_task(manager.execute(RunningServiceGet(timeout_seconds=1).build()))
    await published_event.wait()
    manager.handle_ack_message(
        parsed_ack(
            {
                "method": "get",
                "service": "idle",
                "m_id": 0,
                "response": 65535,
            }
        )
    )
    result = await task

    assert manager.gate.global_enabled is False
    assert result.error is not None
    assert result.error.code is CommandErrorCode.AMBIGUOUS_CORRELATION
    assert result.message_id is None
    assert result.ack is None
    assert published["kwargs"]["retain"] is False
    assert "m_id" not in published["body"]
    diagnostics = manager.diagnostics()
    assert diagnostics["read_only_publish_attempts"] == 1
    assert diagnostics["state_changing_publish_attempts"] == 0
    assert diagnostics["successful_publish_returns"] == 1
    assert diagnostics["pending_get_count"] == 0
    assert diagnostics["ambiguous_get_result_count"] == 1


@pytest.mark.asyncio
async def test_running_service_get_accepts_observed_service_as_ambiguous(
    monkeypatch,
):
    manager = await make_manager(
        monkeypatch,
        capability=("runningservice", "get"),
    )
    published_event = asyncio.Event()

    async def publish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        published_event.set()

    monkeypatch.setattr(command_module, "async_publish", publish)
    task = asyncio.create_task(manager.execute(RunningServiceGet(timeout_seconds=1).build()))
    await published_event.wait()
    manager.handle_ack_message(
        parsed_ack(
            {
                "method": "get",
                "service": "targetgsoc",
                "m_id": 1789479111712,
                "response": 65535,
            }
        )
    )

    result = await task

    assert result.error is not None
    assert result.error.code is CommandErrorCode.AMBIGUOUS_CORRELATION
    assert result.message_id is None
    assert result.ack is None
    assert manager.diagnostics()["ambiguous_get_result_count"] == 1
    assert manager.allocator.owned_ids == set()


@pytest.mark.asyncio
async def test_read_only_get_ignores_active_external_service(monkeypatch):
    manager = await make_manager(
        monkeypatch,
        capability=("runningservice", "get"),
        health=PlantHealth(),
    )
    published_event = asyncio.Event()

    async def publish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        published_event.set()

    monkeypatch.setattr(command_module, "async_publish", publish)
    task = asyncio.create_task(manager.execute(RunningServiceGet(timeout_seconds=1).build()))
    await published_event.wait()
    manager.handle_ack_message(
        parsed_ack(
            {
                "method": "get",
                "service": "targetgsoc",
                "m_id": 1789479111712,
                "response": 65535,
            }
        )
    )

    result = await task
    assert result.error is not None
    assert result.error.code is CommandErrorCode.AMBIGUOUS_CORRELATION


@pytest.mark.asyncio
async def test_undocumented_get_is_blocked_before_publish(monkeypatch):
    manager = await make_manager(
        monkeypatch,
        capability=("runningservice", "get"),
    )
    publishes: list[Any] = []

    async def publish(*args: Any, **kwargs: Any) -> None:
        publishes.append((args, kwargs))

    monkeypatch.setattr(command_module, "async_publish", publish)
    result = await manager.execute(
        PixiiCommand(method=CommandMethod.GET, service="undocumentedservice")
    )

    assert result.status is CommandStatus.REJECTED
    assert result.error is not None
    assert "read_only_command_unsupported" in result.error.details
    assert publishes == []


@pytest.mark.asyncio
async def test_only_one_ambiguous_get_can_be_pending(monkeypatch):
    manager = await make_manager(
        monkeypatch,
        capability=("runningservice", "get"),
    )
    published_event = asyncio.Event()

    async def publish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        published_event.set()

    monkeypatch.setattr(command_module, "async_publish", publish)
    first = asyncio.create_task(manager.execute(RunningServiceGet(timeout_seconds=1).build()))
    await published_event.wait()
    assert manager.diagnostics()["pending_get_state"] == "pending"

    second = await manager.execute(RunningServiceGet(timeout_seconds=1).build())
    assert second.error is not None
    assert second.error.code is CommandErrorCode.AMBIGUOUS_CORRELATION
    assert manager.diagnostics()["pending_get_count"] == 1

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


@pytest.mark.asyncio
async def test_production_gate_cannot_be_overridden(monkeypatch):
    manager = await make_manager(
        monkeypatch,
        capability=("batterycalibration", "set"),
    )
    publishes: list[Any] = []

    async def publish(*args: Any, **kwargs: Any) -> None:
        publishes.append((args, kwargs))

    monkeypatch.setattr(command_module, "async_publish", publish)
    with pytest.raises(TypeError):
        WriteGate(global_enabled=True)  # type: ignore[call-arg]

    result = await manager.execute(BatteryCalibration(90, 7200).build())

    assert result.status is CommandStatus.REJECTED
    assert result.error is not None
    assert result.error.details == ("release_gate_closed", "production_control_disabled")
    assert publishes == []


@pytest.mark.asyncio
async def test_unsupported_capability_blocks_before_publish(monkeypatch):
    manager = await make_manager(monkeypatch)
    manager.capabilities.unsupported_commands.add(("targetsoc", "set"))
    publishes: list[Any] = []

    async def publish(*args: Any, **kwargs: Any) -> None:
        publishes.append((args, kwargs))

    monkeypatch.setattr(command_module, "async_publish", publish)
    result = await manager.execute(target_command())

    assert result.status is CommandStatus.REJECTED
    assert result.error is not None
    assert "adapter_capability_unsupported" in result.error.details
    assert publishes == []



@pytest.mark.asyncio
async def test_malformed_ack_is_not_retained_as_capability_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await make_manager(monkeypatch)
    before_methods = {
        service: set(methods)
        for service, methods in manager.capabilities.command_methods.items()
    }
    before_messages = manager.capabilities.messages_seen

    manager.handle_ack_message(
        parsed_ack(
            {
                "method": "set",
                "service": "attacker-service",
            }
        )
    )

    assert manager.capabilities.command_methods == before_methods
    assert manager.capabilities.messages_seen == before_messages
    assert "attacker-service" not in manager.capabilities.command_methods


def test_documented_capability_can_bootstrap_without_matching_ack():
    capabilities = CapabilityRegistry()
    capabilities.families.update({"core", "battery"})

    assert capabilities.supports_command(
        "targetsoc",
        "set",
        required_families=("core", "battery"),
    )

    capabilities.observe(
        parsed_ack(
            {
                "method": "get",
                "service": "targetgsoc",
                "response": 65535,
            }
        )
    )
    assert capabilities.supports_command(
        "targetsoc",
        "set",
        required_families=("core", "battery"),
    )
    capabilities.observe(
        parsed_ack(
            {
                "method": "set",
                "service": "targetsoc",
                "response": 8,
            }
        )
    )
    assert not capabilities.supports_command(
        "targetsoc",
        "set",
        required_families=("core", "battery"),
    )
    assert not capabilities.supports_command(
        "unknownservice",
        "set",
        required_families=("core", "battery"),
    )
    assert capabilities.snapshot()["observed_command_methods"] == {
        "targetgsoc": ["get"],
        "targetsoc": ["set"],
    }


def test_not_present_response_is_diagnostic_only():
    capabilities = CapabilityRegistry()
    capabilities.families.update({"core", "battery"})
    capabilities.observe(
        parsed_ack(
            {
                "method": "set",
                "service": "targetsoc",
                "response": 9,
            }
        )
    )

    assert capabilities.supports_command(
        "targetsoc",
        "set",
        required_families=("core", "battery"),
    )
    assert capabilities.snapshot()["unsupported_command_methods"] == []
    assert capabilities.snapshot()["not_present_command_methods"] == [
        {"service": "targetsoc", "method": "set"}
    ]


@pytest.mark.asyncio
async def test_documented_target_soc_remains_release_closed(monkeypatch):
    capabilities = CapabilityRegistry()
    capabilities.families.update({"core", "battery"})
    manager = CommandManager(
        cast(Any, SimpleNamespace()),
        SERIAL,
        capabilities,
        healthy_plant,
    )
    monkeypatch.setattr(command_module, "async_wait_for_mqtt_client", _async_true)
    monkeypatch.setattr(command_module, "async_subscribe", _async_subscribe)
    await manager.start()
    publishes: list[Any] = []

    async def publish(*args: Any, **kwargs: Any) -> None:
        publishes.append((args, kwargs))

    monkeypatch.setattr(command_module, "async_publish", publish)
    result = await manager.execute(target_command())

    assert result.status is CommandStatus.REJECTED
    assert result.error is not None
    assert "release_gate_closed" in result.error.details
    assert publishes == []

@pytest.mark.asyncio
async def test_manager_subscribes_to_entry_ack_topic(monkeypatch):
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def subscribe(*args: Any, **kwargs: Any):
        calls.append((args, kwargs))
        return lambda: None

    monkeypatch.setattr(command_module, "async_wait_for_mqtt_client", _async_true)
    monkeypatch.setattr(command_module, "async_subscribe", subscribe)
    manager = CommandManager(
        cast(Any, SimpleNamespace()),
        SERIAL,
        CapabilityRegistry(),
        PlantHealth,
    )

    await manager.start()

    assert manager.ready is True
    assert calls[0][0][1] == f"pixii/ack/{SERIAL}"
    assert calls[0][1]["encoding"] is None


def test_command_builders_generate_valid_typed_payloads():
    assert RunningServiceGet().build().to_payload() == {
        "method": "get",
        "service": "runningservice",
    }
    assert DemandResponse(3600, 1200).build().with_message_id(7).to_payload() == {
        "duration": 3600,
        "method": "set",
        "m_id": 7,
        "service": "demandresponse",
        "starttime": "now",
        "values": {"pacref": 1200},
    }
    assert TargetSoC(90, 3600).build().with_message_id(8).to_payload()["values"] == {
        "socref": 90
    }
    assert BatteryCalibration(90, 7200).build().with_message_id(10).to_payload() == {
        "duration": 7200,
        "method": "set",
        "m_id": 10,
        "service": "batterycalibration",
        "starttime": "now",
        "values": {"endpoint_soc": 90},
    }
    assert AdaptivePeakShaving(0, 10000, 1000, 2000, True, 1000, 900).build().with_message_id(
        9
    ).to_payload() == {
        "duration": 900,
        "method": "set",
        "m_id": 9,
        "service": "adaptivepeakshaving",
        "starttime": "now",
        "values": {
            "charge_max": 1000,
            "discharge_max": 2000,
            "exportlim": 0,
            "importlim": 10000,
            "keep_active": True,
            "max_lim_change": 1000,
        },
    }
    assert AdaptivePeakShaving(0, 10000, 0, 0, True, 1000, 0).build().with_message_id(
        9
    ).to_payload()["duration"] == 0


@pytest.mark.asyncio
async def test_owned_message_id_can_be_reused_for_lease_renewal(monkeypatch) -> None:
    capabilities = CapabilityRegistry()
    capabilities.families.update({"core", "battery"})
    manager = CommandManager(
        cast(Any, SimpleNamespace()),
        SERIAL,
        capabilities,
        healthy_plant,
        production_control_enabled=lambda: True,
    )
    monkeypatch.setattr(command_module, "async_wait_for_mqtt_client", _async_true)
    monkeypatch.setattr(command_module, "async_subscribe", _async_subscribe)
    published: list[dict[str, Any]] = []

    async def publish(hass: Any, topic: str, payload: str, **kwargs: Any) -> None:
        del hass, topic, kwargs
        published.append(json.loads(payload))

    monkeypatch.setattr(command_module, "async_publish", publish)
    await manager.start()
    lease = AdaptivePeakShaving(0, 10000, 1000, 0, True, 1000, 120).build()
    first = asyncio.create_task(manager.execute(replace(lease, persist_message_id=True)))
    message_id = await wait_for_pending(manager)
    manager.handle_ack_message(
        parsed_ack(
            {
                "method": "set",
                "service": "adaptivepeakshaving",
                "m_id": message_id,
                "response": 65535,
            }
        )
    )
    assert (await first).successful
    assert message_id in manager.allocator.owned_ids

    renewal = asyncio.create_task(
        manager.execute(replace(lease, message_id=message_id))
    )
    await wait_for_pending(manager)
    manager.handle_ack_message(
        parsed_ack(
            {
                "method": "set",
                "service": "adaptivepeakshaving",
                "m_id": message_id,
                "response": 65535,
            }
        )
    )

    assert (await renewal).successful
    assert [item["m_id"] for item in published] == [message_id, message_id]


def test_invalid_command_capability_and_values_are_rejected():
    with pytest.raises(CommandValidationError):
        TargetSoC(101, 60).build()
    with pytest.raises(CommandValidationError):
        DemandResponse(0, 100).build()
    with pytest.raises(CommandValidationError):
        AdaptivePeakShaving(float("nan"), 0, 0, 0, True, 0, 60).build()
    with pytest.raises(CommandValidationError):
        BatteryCalibration(101, 7200).build()
    with pytest.raises(CommandValidationError):
        BatteryCalibration(90, 7200, method=CommandMethod.GET).build()
    with pytest.raises(CommandValidationError):
        TargetSoC(50, 60).build().with_message_id(0)








@pytest.mark.asyncio
async def test_recurring_external_get_is_not_state_command_correlation(monkeypatch):
    manager = await make_manager(
        monkeypatch,
        capability=("runningservice", "get"),
    )

    async def publish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs

    monkeypatch.setattr(command_module, "async_publish", publish)
    task = asyncio.create_task(manager.execute(RunningServiceGet(timeout_seconds=1).build()))
    for _ in range(20):
        if manager._pending_get is not None:
            break
        await asyncio.sleep(0)
    manager.handle_ack_message(
        parsed_ack(
            {
                "method": "get",
                "service": "targetgsoc",
                "m_id": 1789479111712,
                "response": 65535,
            }
        )
    )
    result = await task
    assert result.error is not None
    assert result.error.code is CommandErrorCode.AMBIGUOUS_CORRELATION
    assert manager.diagnostics()["ambiguous_ack_count"] == 1


@pytest.mark.asyncio
async def test_external_get_ack_without_pending_is_observation_only(monkeypatch):
    manager = await make_manager(
        monkeypatch,
        capability=("runningservice", "get"),
    )

    manager.handle_ack_message(
        parsed_ack(
            {
                "method": "get",
                "service": "targetgsoc",
                "m_id": 1789479111712,
                "response": 65535,
            }
        )
    )

    diagnostics = manager.diagnostics()
    assert diagnostics["external_get_ack_count"] == 1
    assert diagnostics["ambiguous_get_result_count"] == 0
    assert diagnostics["pending_get_count"] == 0


@pytest.mark.asyncio
async def test_state_changing_ack_never_resolves_pending_running_service_get(monkeypatch):
    manager = await make_manager(
        monkeypatch,
        capability=("runningservice", "get"),
    )
    published_event = asyncio.Event()

    async def publish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        published_event.set()

    monkeypatch.setattr(command_module, "async_publish", publish)
    task = asyncio.create_task(manager.execute(RunningServiceGet(timeout_seconds=1).build()))
    await published_event.wait()
    manager.handle_ack_message(
        parsed_ack(
            {
                "method": "set",
                "service": "targetsoc",
                "m_id": 123,
                "response": 65535,
            }
        )
    )
    await asyncio.sleep(0)
    assert not task.done()
    assert manager.diagnostics()["pending_get_count"] == 1

    manager.handle_ack_message(
        parsed_ack(
            {
                "method": "get",
                "service": "idle",
                "m_id": 0,
                "response": 65535,
            }
        )
    )
    result = await task
    assert result.error is not None
    assert result.error.code is CommandErrorCode.AMBIGUOUS_CORRELATION


@pytest.mark.asyncio
async def test_malformed_get_ack_does_not_resolve_pending_request(monkeypatch):
    manager = await make_manager(
        monkeypatch,
        capability=("runningservice", "get"),
    )
    published_event = asyncio.Event()

    async def publish(*args: Any, **kwargs: Any) -> None:
        del args, kwargs
        published_event.set()

    monkeypatch.setattr(command_module, "async_publish", publish)
    task = asyncio.create_task(manager.execute(RunningServiceGet(timeout_seconds=0.05).build()))
    await published_event.wait()
    manager.handle_ack_message(
        parsed_ack(
            {
                "method": "get",
                "service": "idle",
                "m_id": 0,
                "response": "65535",
            }
        )
    )
    result = await task

    assert result.status is CommandStatus.TIMED_OUT
    assert manager.diagnostics()["malformed_ack_count"] == 1










def test_terminal_ack_history_is_bounded_and_get_ambiguity_has_no_fake_id():
    manager = CommandManager(
        cast(Any, SimpleNamespace()),
        SERIAL,
        CapabilityRegistry(),
        PlantHealth,
        terminal_history_limit=2,
    )

    manager._remember_terminal(1, "completed")
    manager._remember_terminal(2, "completed")
    manager._remember_terminal(3, "completed")

    assert list(manager._terminal_ids) == [2, 3]


def test_allocator_persists_owned_ids_and_retires_one_shot_ids():
    stored: dict[str, Any] = {}
    first = MessageIdAllocator(next_value=10, owned_ids={10, 11}, persist=stored.update)
    persistent_id = first.allocate(persistent=True)
    assert persistent_id == 12
    assert first.active_ids == {12}
    first.retire(persistent_id)
    assert first.owned_ids == {10, 11, 12}
    first.observe_external(13)
    restored = MessageIdAllocator.from_state(stored)
    assert restored.allocate() == 13
    assert restored.snapshot()["owned_m_ids"] == [10, 11, 12]
    assert restored.snapshot()["next_m_id"] == 14

    ephemeral_state: dict[str, Any] = {}
    ephemeral = MessageIdAllocator(next_value=20, persist=ephemeral_state.update)
    one_shot_id = ephemeral.allocate()
    assert ephemeral.active_ids == {20}
    assert ephemeral.owned_ids == set()
    ephemeral.retire(one_shot_id)
    assert ephemeral.active_ids == set()
    assert ephemeral.owned_ids == set()
    assert ephemeral_state["owned_m_ids"] == []
    assert TargetSoC(50, 60).build().with_message_id(14).to_payload()["m_id"] == 14
    assert (
        TargetSoC(50, 60, message_id_as_string=True)
        .build()
        .with_message_id(15)
        .to_payload()["m_id"]
        == "15"
    )
