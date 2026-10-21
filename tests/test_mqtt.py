from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from custom_components.pixiihome_controller import mqtt as mqtt_module
from custom_components.pixiihome_controller.exceptions import MqttParseError
from custom_components.pixiihome_controller.mqtt import PixiiMqttTransport
from custom_components.pixiihome_controller.protocol.mqtt import (
    MAX_JSON_DEPTH,
    MAX_MQTT_PAYLOAD_BYTES,
    parse_ack,
    parse_message,
    parse_topic,
)

from .conftest import load_json


async def _async_true(*args: Any, **kwargs: Any) -> bool:
    return True


@pytest.mark.asyncio
async def test_telemetry_subscriptions_request_raw_payloads(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []

    async def subscribe(*args: Any, **kwargs: Any):
        calls.append(kwargs)
        return lambda: None

    monkeypatch.setattr(mqtt_module, "async_wait_for_mqtt_client", _async_true)
    monkeypatch.setattr(mqtt_module, "async_subscribe", subscribe)
    transport = PixiiMqttTransport(
        cast(Any, SimpleNamespace()),
        "GATEWAY",
        lambda message: None,
    )

    await transport.start()

    assert calls
    assert all(call["encoding"] is None for call in calls)


def test_current_and_legacy_topics_parse():
    assert parse_topic("pixii/status/GATEWAY/core") == ("GATEWAY", "core")
    assert parse_topic("pixii/GATEWAY/status/battery") == ("GATEWAY", "battery")


def test_payload_preserves_extra_fields_and_timestamp():
    received = datetime(2026, 1, 1, tzinfo=UTC)
    message = parse_message(
        "pixii/status/GATEWAY/core",
        '{"ac_w": 10, "future_field": true}',
        received_at=received,
    )
    assert message.payload["future_field"] is True
    assert message.received_at == received


def test_ack_and_malformed_payload():
    assert parse_message("pixii/ack/GATEWAY", '{"response": 65535}').family == "ack"
    with pytest.raises(MqttParseError):
        parse_message("pixii/status/GATEWAY/core", "not-json")
    with pytest.raises(MqttParseError):
        parse_message("pixii/status/GATEWAY/core", "[]")



def test_mqtt_payload_size_and_depth_are_bounded_before_retention() -> None:
    oversized = '{"value":"' + ("x" * MAX_MQTT_PAYLOAD_BYTES) + '"}'
    with pytest.raises(MqttParseError, match="size limit"):
        parse_message("pixii/status/GATEWAY/core", oversized)

    deeply_nested: object = 1
    for _ in range(MAX_JSON_DEPTH + 1):
        deeply_nested = [deeply_nested]
    with pytest.raises(MqttParseError, match="nested too deeply"):
        parse_message(
            "pixii/status/GATEWAY/batt_extended",
            json.dumps(deeply_nested),
        )

def test_observed_targetgsoc_ack_preserves_gateway_fields():
    ack = parse_ack(load_json("mqtt/ack_observed_targetgsoc.json"))

    assert ack.method == "get"
    assert ack.service == "targetgsoc"
    assert ack.m_id == 1789479111712
    assert ack.source_id == 7
    assert ack.response == 65535
    assert ack.duration == 3600
    assert ack.recurrence == {"type": "once"}
    assert ack.service_status == 5
    assert ack.service_time_left == 1759
    assert ack.service_duration_left is None
    assert ack.time_left == 1759
    assert ack.socref == 90


def test_documented_service_duration_left_remains_supported():
    ack = parse_ack(load_json("mqtt/ack_documented_duration.json"))

    assert ack.service_time_left is None
    assert ack.service_duration_left == 905
    assert ack.time_left == 905
    assert ack.values == {
        "duration": 3600,
        "service_duration_left": 905,
        "service_status": 5,
    }

def test_ack_accepts_equivalent_identity_and_status_fields_under_values():
    ack = parse_ack(
        {
            "values": {
                "method": "set",
                "response": 65535,
                "service": "targetsoc",
                "m_id": "42",
                "source_id": 7,
                "socref": 90,
                "service_status": 5,
            }
        }
    )

    assert ack.method == "set"
    assert ack.response == 65535
    assert ack.service == "targetsoc"
    assert ack.m_id == "42"
    assert ack.source_id == 7
    assert ack.socref == 90


def test_repeated_active_service_get_ack_identity_is_not_request_correlation():
    observation = load_json("mqtt/ack_observed_targetgsoc_polling.json")
    ack = parse_ack(observation["sample"])

    assert observation["approximate_interval_seconds"] == 5
    assert set(observation["stable_fields"]) == {
        "m_id",
        "service",
        "source_id",
        "socref",
        "duration",
        "recurrence",
    }
    assert observation["decreasing_fields"] == ["service_time_left"]
    assert ack.m_id == 1789479111712
    assert ack.service == "targetgsoc"
    assert ack.source_id == 7
