from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
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
from custom_components.pixiihome_controller.protocol.commands import (
    CommandMethod,
    RunningServiceGet,
)
from custom_components.pixiihome_controller.protocol.models import MqttAck, ParsedMqttMessage
from custom_components.pixiihome_controller.protocol.scheduler_commands import (
    ScheduledGet,
    ScheduledPriceStop,
    SchedulerCommand,
)
from custom_components.pixiihome_controller.runtime import RuntimeData
from custom_components.pixiihome_controller.scheduler.mutations import SchedulerExecutionStatus
from custom_components.pixiihome_controller.scheduler.transport import (
    HomeAssistantSchedulerCommandExecutor,
)

from .conftest import load_json

SERIAL = "GATEWAY"
SCHEDULER_ID = 7001
START = datetime.now(UTC).replace(microsecond=0) + timedelta(days=1)


def parsed_ack(payload: dict[str, Any]) -> ParsedMqttMessage:
    return ParsedMqttMessage(
        serial=SERIAL,
        family="ack",
        payload=payload,
        topic=f"pixii/ack/{SERIAL}",
        received_at=START,
    )


def scheduler_set(m_id: int = SCHEDULER_ID) -> SchedulerCommand:
    return SchedulerCommand(
        method=CommandMethod.SET,
        m_id=m_id,
        service="targetsoc",
        fields={"starttime": "now", "duration": 3600, "values": {"socref": 90}},
    )


def scheduler_get(m_id: int = SCHEDULER_ID) -> SchedulerCommand:
    return ScheduledGet(m_id, expected_service="targetsoc").build()


async def make_manager(
    monkeypatch: pytest.MonkeyPatch,
    *,
    allocator: MessageIdAllocator | None = None,
    subscribe_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] | None = None,
) -> CommandManager:
    capabilities = CapabilityRegistry()
    capabilities.families.update({"core", "battery"})
    capabilities.command_methods.update(
        {
            "targetsoc": {"set"},
            "runningservice": {"get"},
        }
    )
    manager = CommandManager(
        cast(Any, SimpleNamespace()),
        SERIAL,
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

    async def wait_for_mqtt(hass: Any) -> bool:
        del hass
        return True

    async def subscribe(*args: Any, **kwargs: Any) -> Any:
        if subscribe_calls is not None:
            subscribe_calls.append((args, kwargs))
        return lambda: None

    monkeypatch.setattr(command_module, "async_wait_for_mqtt_client", wait_for_mqtt)
    monkeypatch.setattr(command_module, "async_subscribe", subscribe)
    await manager.start()
    return manager


async def wait_until(predicate: Any) -> None:
    for _ in range(40):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


@pytest.mark.asyncio
async def test_adapter_reuses_one_existing_ack_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subscriptions: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    manager = await make_manager(monkeypatch, subscribe_calls=subscriptions)
    await manager.start()
    adapter = HomeAssistantSchedulerCommandExecutor(manager, timeout_seconds=0.05)

    async def publish(hass: Any, topic: str, payload: str, **kwargs: Any) -> None:
        del hass, topic, payload, kwargs
        manager.handle_ack_message(
            parsed_ack(
                {
                    "method": "get",
                    "service": "targetsoc",
                    "m_id": SCHEDULER_ID,
                    "response": 65535,
                }
            )
        )

    monkeypatch.setattr(command_module, "async_publish", publish)
    await adapter.execute_get(scheduler_get())

    assert len(subscriptions) == 1
    assert subscriptions[0][0][1] == f"pixii/ack/{SERIAL}"


@pytest.mark.asyncio
async def test_scheduler_keyed_get_exact_match_returns_typed_ack(monkeypatch):
    manager = await make_manager(monkeypatch)
    adapter = HomeAssistantSchedulerCommandExecutor(manager, timeout_seconds=0.05)
    published: dict[str, Any] = {}

    async def publish(hass: Any, topic: str, payload: str, **kwargs: Any) -> None:
        del hass, topic
        published.update(body=json.loads(payload), kwargs=kwargs)
        manager.handle_ack_message(
            parsed_ack(
                {
                    "method": "get",
                    "service": "targetsoc",
                    "m_id": SCHEDULER_ID,
                    "response": 65535,
                    "values": {"socref": 90},
                }
            )
        )

    monkeypatch.setattr(command_module, "async_publish", publish)
    result = await adapter.execute_get(scheduler_get())

    assert result.m_id == SCHEDULER_ID
    assert result.method == "get"
    assert published["body"] == {"m_id": SCHEDULER_ID, "method": "get", "service": "targetsoc"}
    assert published["kwargs"] == {"qos": 0, "retain": False}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response_text",
    [
        pytest.param(None, id="response-text-absent"),
        pytest.param("arbitrary text that must not affect routing", id="response-text-arbitrary"),
    ],
)
async def test_scheduler_keyed_get_routes_documented_absence_as_failed_typed_ack(
    monkeypatch: pytest.MonkeyPatch,
    response_text: str | None,
) -> None:
    manager = await make_manager(monkeypatch)
    payload: dict[str, Any] = {
        "method": "get",
        "service": " UnKnOwN ",
        "m_id": SCHEDULER_ID,
        "response": 11,
    }
    if response_text is not None:
        payload["response_text"] = response_text

    async def publish(hass: Any, topic: str, wire_payload: str, **kwargs: Any) -> None:
        del hass, topic, wire_payload, kwargs
        manager.handle_ack_message(parsed_ack(payload))

    monkeypatch.setattr(command_module, "async_publish", publish)
    result = await manager.execute_correlated(
        CorrelatedCommandRequest(
            method=CommandMethod.GET,
            message_id=SCHEDULER_ID,
            expected_services=("targetsoc",),
            timeout_seconds=0.05,
        )
    )

    assert result.status is CommandStatus.FAILED
    assert result.error is not None
    assert result.error.code is CommandErrorCode.GATEWAY_RESPONSE
    assert result.ack is not None
    assert isinstance(result.ack, MqttAck)
    assert result.ack.m_id == SCHEDULER_ID
    assert result.ack.method == "get"
    assert result.ack.service == " UnKnOwN "
    assert result.ack.response == 11
    assert result.ack.response_text == response_text
    assert manager.diagnostics()["scheduler_get_matches"] == 1
    assert manager.diagnostics()["pending_scheduler_get_count"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ack",
    [
        pytest.param(
            {
                "method": "get",
                "service": "unknown",
                "m_id": SCHEDULER_ID + 1,
                "response": 11,
            },
            id="wrong-m-id",
        ),
        pytest.param(
            {"method": "get", "service": "unknown", "response": 11},
            id="missing-m-id",
        ),
        pytest.param(
            load_json("mqtt/price_delete_absence_unkeyed_2_3_63.json"),
            id="firmware-2-3-63-live-absence-without-method-service-or-id",
        ),
        pytest.param(
            {
                "method": "get",
                "service": "unknown",
                "m_id": "invalid",
                "response": 11,
            },
            id="invalid-m-id",
        ),
        pytest.param(
            {
                "method": "get",
                "service": "unknown",
                "m_id": SCHEDULER_ID,
                "response": 65535,
            },
            id="unknown-success",
        ),
        pytest.param(
            {
                "method": "get",
                "service": "unknown",
                "m_id": SCHEDULER_ID,
                "response": 12,
                "response_text": "scheduler id not configured",
            },
            id="unknown-other-error",
        ),
        pytest.param(
            {
                "method": "set",
                "service": "unknown",
                "m_id": SCHEDULER_ID,
                "response": 11,
            },
            id="set-method",
        ),
        pytest.param(
            {
                "method": "delete",
                "service": "unknown",
                "m_id": SCHEDULER_ID,
                "response": 11,
            },
            id="delete-method",
        ),
        pytest.param(
            {
                "method": "get",
                "service": "other",
                "m_id": SCHEDULER_ID,
                "response": 65535,
            },
            id="successful-get-wrong-service",
        ),
        pytest.param(
            {
                "method": "get",
                "service": "targetsoc",
                "m_id": SCHEDULER_ID,
                "response": "bad",
            },
            id="invalid-response",
        ),
    ],
)
async def test_scheduler_keyed_get_ignores_nonmatching_ack_then_matches(
    monkeypatch: pytest.MonkeyPatch,
    ack: dict[str, Any],
) -> None:
    manager = await make_manager(monkeypatch)
    adapter = HomeAssistantSchedulerCommandExecutor(manager, timeout_seconds=0.05)

    async def publish(hass: Any, topic: str, payload: str, **kwargs: Any) -> None:
        del hass, topic, payload, kwargs
        manager.handle_ack_message(parsed_ack(ack))
        diagnostics = manager.diagnostics()
        assert diagnostics["pending_scheduler_get_count"] == 1
        assert diagnostics["scheduler_get_matches"] == 0
        await asyncio.sleep(0)
        manager.handle_ack_message(
            parsed_ack(
                {
                    "method": "get",
                    "service": "targetsoc",
                    "m_id": SCHEDULER_ID,
                    "response": 65535,
                }
            )
        )

    monkeypatch.setattr(command_module, "async_publish", publish)
    result = await adapter.execute_get(scheduler_get())

    assert result.m_id == SCHEDULER_ID
    assert result.method == "get"
    assert result.service == "targetsoc"
    assert result.response == 65535
    assert manager.diagnostics()["scheduler_get_matches"] == 1
    assert manager.diagnostics()["pending_scheduler_get_count"] == 0


@pytest.mark.asyncio
async def test_scheduler_get_and_runningservice_get_remain_isolated(monkeypatch):
    manager = await make_manager(monkeypatch)
    adapter = HomeAssistantSchedulerCommandExecutor(manager, timeout_seconds=0.1)
    published: list[dict[str, Any]] = []

    async def publish(hass: Any, topic: str, payload: str, **kwargs: Any) -> None:
        del hass, topic, kwargs
        published.append(json.loads(payload))

    monkeypatch.setattr(command_module, "async_publish", publish)
    generic = asyncio.create_task(manager.execute(RunningServiceGet(timeout_seconds=0.1).build()))
    await wait_until(lambda: manager.diagnostics()["pending_get_count"] == 1)
    scheduler = asyncio.create_task(adapter.execute_get(scheduler_get()))
    await wait_until(lambda: len(published) == 2)

    manager.handle_ack_message(
        parsed_ack(
            {
                "method": "get",
                "service": "targetsoc",
                "m_id": SCHEDULER_ID,
                "response": 65535,
            }
        )
    )
    scheduler_ack = await scheduler
    assert scheduler_ack.m_id == SCHEDULER_ID
    assert not generic.done()

    manager.handle_ack_message(parsed_ack({"method": "get", "service": "idle", "response": 65535}))
    generic_result = await generic
    assert generic_result.error is not None
    assert generic_result.error.code is CommandErrorCode.AMBIGUOUS_CORRELATION
    assert published[0].get("service") == "runningservice"
    assert published[1]["service"] == "targetsoc"


@pytest.mark.asyncio
async def test_scheduler_pending_namespace_rejects_same_key_without_overwrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = await make_manager(monkeypatch)
    adapter = HomeAssistantSchedulerCommandExecutor(manager, timeout_seconds=0.1)
    published = asyncio.Event()

    async def publish(hass: Any, topic: str, payload: str, **kwargs: Any) -> None:
        del hass, topic, payload, kwargs
        published.set()

    monkeypatch.setattr(command_module, "async_publish", publish)
    first = asyncio.create_task(adapter.execute_get(scheduler_get()))
    await published.wait()
    second = asyncio.create_task(adapter.execute_get(scheduler_get()))
    with pytest.raises(RuntimeError, match="ambiguous_correlation"):
        await second
    assert manager.diagnostics()["pending_scheduler_get_count"] == 1
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert manager.diagnostics()["pending_scheduler_get_count"] == 0


@pytest.mark.asyncio
async def test_scheduler_default_write_gate_blocks_before_publish(monkeypatch):
    manager = await make_manager(monkeypatch)
    adapter = HomeAssistantSchedulerCommandExecutor(manager, timeout_seconds=0.05)
    publishes = []

    async def publish(*args: Any, **kwargs: Any) -> None:
        publishes.append((args, kwargs))

    monkeypatch.setattr(command_module, "async_publish", publish)
    result = await adapter.execute_mutation(scheduler_set())

    assert result.status is SchedulerExecutionStatus.EXECUTION_ERROR
    assert result.reason == "gate_blocked"
    assert publishes == []
    assert manager.diagnostics()["state_changing_publish_attempts"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["peakshaving", "demandresponse"])
async def test_price_delete_wire_retains_known_service_and_ignores_other_ack_identity(
    monkeypatch, service,
):
    manager = await make_manager(monkeypatch)
    manager.gate = WriteGate(lambda: True)
    adapter = HomeAssistantSchedulerCommandExecutor(manager, timeout_seconds=0.05)
    publishes = []

    async def publish(hass, topic, payload, **kwargs):
        publishes.append((topic, json.loads(payload), kwargs))
        for ack in [
            {"method": "set", "m_id": SCHEDULER_ID, "service": service},
            {"method": "delete", "m_id": SCHEDULER_ID + 1, "service": service},
            {"method": "delete", "m_id": SCHEDULER_ID, "service": "targetsoc"},
            {"method": "delete", "m_id": SCHEDULER_ID, "service": service},
        ]:
            manager.handle_ack_message(parsed_ack({**ack, "response": 65535}))

    monkeypatch.setattr(command_module, "async_publish", publish)
    result = await adapter.execute_mutation(ScheduledPriceStop(SCHEDULER_ID, service).build())
    assert result.status is SchedulerExecutionStatus.ACK_SUCCESS
    assert result.ack.method == "delete"
    assert publishes == [(f"pixii/{SERIAL}/control", {
        "method": "delete", "m_id": SCHEDULER_ID, "service": service,
    }, {"qos": 0, "retain": False})]


@pytest.mark.asyncio
async def test_unconfigured_runtime_does_not_construct_scheduler_mutation_adapter(monkeypatch):
    del monkeypatch
    runtime = RuntimeData(cast(Any, SimpleNamespace()), SERIAL)

    assert runtime.scheduler_executor is None
    assert runtime.scheduler_mutation_manager is None
    assert runtime.scheduler_ownership is None


@pytest.mark.asyncio
@pytest.mark.parametrize("request_count", [1, 2])
async def test_unidentified_response_11_is_diagnostic_only_even_with_pending_gets(
    monkeypatch, request_count,
):
    manager = await make_manager(monkeypatch)
    adapter = HomeAssistantSchedulerCommandExecutor(manager, timeout_seconds=1)
    publishes = []

    async def publish(hass, topic, payload, **kwargs):
        publishes.append(json.loads(payload))

    monkeypatch.setattr(command_module, "async_publish", publish)
    tasks = [asyncio.create_task(adapter.execute_get(scheduler_get(SCHEDULER_ID + offset)))
             for offset in range(request_count)]
    try:
        await wait_until(lambda: len(publishes) == request_count)
        incomplete = load_json("mqtt/price_delete_absence_unkeyed_2_3_63.json")
        manager.handle_ack_message(parsed_ack(incomplete))
        await asyncio.sleep(0)
        diagnostics = manager.diagnostics()
        assert diagnostics["unidentified_response_11_count"] == 1
        assert diagnostics["last_response_11_missing_fields"] == ["method", "service", "m_id"]
        assert diagnostics["pending_scheduler_get_count"] == request_count
        assert diagnostics["scheduler_get_matches"] == 0
        assert all(not task.done() for task in tasks)
        for offset in range(request_count):
            manager.handle_ack_message(parsed_ack({
                "method": "get", "service": "unknown",
                "m_id": SCHEDULER_ID + offset, "response": 11,
            }))
        results = await asyncio.gather(*tasks)
        assert [ack.m_id for ack in results] == [
            SCHEDULER_ID + offset for offset in range(request_count)
        ]
        assert manager.diagnostics()["unidentified_response_11_count"] == 1
        assert len(publishes) == request_count
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_unidentified_response_11_diagnostics_are_scoped_and_sanitized(monkeypatch):
    manager = await make_manager(monkeypatch)
    payload = {"response": 11, "response_msg": "sensitive text", "token": "secret"}
    foreign = parsed_ack(payload)
    manager.handle_ack_message(ParsedMqttMessage(
        serial="OTHER_GATEWAY", family="ack", payload=payload,
        topic="pixii/ack/OTHER_GATEWAY", received_at=START,
    ))
    manager.handle_ack_message(ParsedMqttMessage(
        serial=SERIAL, family="core", payload=payload,
        topic=f"pixii/status/{SERIAL}/core", received_at=START,
    ))
    manager.handle_ack_message(parsed_ack({"response": "11"}))
    manager.handle_ack_message(parsed_ack({"values": {
        "method": "get", "service": "unknown", "m_id": SCHEDULER_ID, "response": 11,
    }}))
    assert manager.diagnostics()["unidentified_response_11_count"] == 0
    manager.handle_ack_message(foreign)
    manager.handle_ack_message(parsed_ack({"response": 11, "method": "get"}))
    diagnostics = manager.diagnostics()
    assert diagnostics["unidentified_response_11_count"] == 2
    assert diagnostics["last_response_11_missing_fields"] == ["service", "m_id"]
    assert diagnostics["pending_scheduler_get_count"] == 0
    assert "secret" not in json.dumps(diagnostics)
    assert "sensitive text" not in json.dumps(diagnostics)
