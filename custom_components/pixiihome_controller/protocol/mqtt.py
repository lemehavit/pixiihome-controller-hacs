"""Strict, side-effect-free Pixii MQTT parsing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from math import isfinite
from typing import Any

from ..exceptions import MqttParseError
from .models import MqttAck, ParsedMqttMessage

MAX_MQTT_PAYLOAD_BYTES = 65_536
MAX_MQTT_TOPIC_LENGTH = 256
MAX_MQTT_IDENTIFIER_LENGTH = 64
MAX_JSON_DEPTH = 8
MAX_JSON_CONTAINER_ITEMS = 256
MAX_JSON_TOTAL_NODES = 2_048
MAX_JSON_STRING_LENGTH = 4_096


def _validate_json_shape(value: Any) -> None:
    nodes = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_TOTAL_NODES:
            raise MqttParseError("Pixii MQTT payload contains too many values")
        if depth > MAX_JSON_DEPTH:
            raise MqttParseError("Pixii MQTT payload is nested too deeply")
        if isinstance(item, str):
            if len(item) > MAX_JSON_STRING_LENGTH:
                raise MqttParseError("Pixii MQTT payload string is too long")
            return
        if isinstance(item, float) and not isfinite(item):
            raise MqttParseError("Pixii MQTT payload number must be finite")
        if isinstance(item, dict):
            if len(item) > MAX_JSON_CONTAINER_ITEMS:
                raise MqttParseError("Pixii MQTT payload object is too large")
            for key, nested in item.items():
                if not isinstance(key, str) or len(key) > MAX_MQTT_IDENTIFIER_LENGTH:
                    raise MqttParseError("Pixii MQTT payload key is invalid")
                visit(nested, depth + 1)
            return
        if isinstance(item, list):
            if len(item) > MAX_JSON_CONTAINER_ITEMS:
                raise MqttParseError("Pixii MQTT payload list is too large")
            for nested in item:
                visit(nested, depth + 1)

    visit(value, 0)


def _bounded_payload_text(payload: bytes | bytearray | memoryview | str) -> str:
    if isinstance(payload, bytes | bytearray | memoryview):
        if len(payload) > MAX_MQTT_PAYLOAD_BYTES:
            raise MqttParseError("Pixii MQTT payload exceeds the size limit")
        try:
            return bytes(payload).decode("utf-8")
        except UnicodeDecodeError as err:
            raise MqttParseError("Pixii MQTT payload is not valid UTF-8") from err
    if not isinstance(payload, str):
        raise MqttParseError("Pixii MQTT payload must be bytes or text")
    if len(payload) > MAX_MQTT_PAYLOAD_BYTES:
        raise MqttParseError("Pixii MQTT payload exceeds the size limit")
    try:
        encoded_length = len(payload.encode("utf-8"))
    except UnicodeEncodeError as err:
        raise MqttParseError("Pixii MQTT payload is not valid UTF-8") from err
    if encoded_length > MAX_MQTT_PAYLOAD_BYTES:
        raise MqttParseError("Pixii MQTT payload exceeds the size limit")
    return payload


def _decode_payload(payload: bytes | bytearray | memoryview | str) -> Any:
    payload_text = _bounded_payload_text(payload)
    try:
        decoded = json.loads(payload_text)
    except (TypeError, ValueError, RecursionError) as err:
        raise MqttParseError("Pixii MQTT payload is not valid JSON") from err
    _validate_json_shape(decoded)
    return decoded


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MqttParseError(f"{label} must be an object")
    return value


def _optional_int(value: Any, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise MqttParseError(f"{label} must be an integer")
    return value


def _command_name(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > MAX_MQTT_IDENTIFIER_LENGTH
    ):
        raise MqttParseError(f"{label} must be a bounded non-empty string")
    return value


def _optional_number(value: Any, label: str) -> int | float | None:
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not isfinite(value)
    ):
        raise MqttParseError(f"{label} must be a finite number")
    return value


def _identifier(value: Any, label: str) -> int | str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise MqttParseError(f"{label} must be an integer or integer string")
    if isinstance(value, str) and not value.isdecimal():
        raise MqttParseError(f"{label} must be an integer or integer string")
    return value


def _top_level_or_values(
    payload: Mapping[str, Any], values: Mapping[str, Any], key: str
) -> Any:
    return payload[key] if key in payload else values.get(key)


def parse_topic(topic: str) -> tuple[str, str]:
    if not isinstance(topic, str) or len(topic) > MAX_MQTT_TOPIC_LENGTH:
        raise MqttParseError("Pixii MQTT topic is invalid")
    parts = topic.split("/")
    if len(parts) == 4 and parts[0:2] == ["pixii", "status"]:
        _, _, serial, family = parts
    elif len(parts) == 4 and parts[0] == "pixii" and parts[2] == "status":
        _, serial, _, family = parts
    elif len(parts) == 3 and parts[0:2] == ["pixii", "ack"]:
        _, _, serial = parts
        family = "ack"
    else:
        raise MqttParseError(f"Unsupported Pixii topic: {topic!r}")
    if (
        not serial
        or not family
        or len(serial) > MAX_MQTT_IDENTIFIER_LENGTH
        or len(family) > MAX_MQTT_IDENTIFIER_LENGTH
    ):
        raise MqttParseError("Pixii MQTT topic identity is invalid")
    return serial, family


def parse_message(
    topic: str,
    payload: bytes | bytearray | memoryview | str,
    *,
    received_at: datetime | None = None,
) -> ParsedMqttMessage:
    serial, family = parse_topic(topic)
    decoded = _decode_payload(payload)
    if family != "batt_extended" and not isinstance(decoded, dict):
        raise MqttParseError(f"Expected an object payload for {family}")
    return ParsedMqttMessage(
        serial=serial,
        family=family,
        payload=decoded,
        topic=topic,
        received_at=received_at or datetime.now(UTC),
    )


def parse_ack(payload: Mapping[str, Any]) -> MqttAck:
    """Parse documented and observed Gateway acknowledgement fields."""
    data = _mapping(payload, "ACK payload")
    values = _mapping(data.get("values", {}), "values")
    method = _command_name(_top_level_or_values(data, values, "method"), "method")
    response = _optional_int(
        _top_level_or_values(data, values, "response"),
        "response",
    )
    service = _command_name(_top_level_or_values(data, values, "service"), "service")
    if response is None:
        raise MqttParseError("response must be an integer")

    recurrence_value = _top_level_or_values(data, values, "recurrence")
    recurrence = (
        None
        if recurrence_value is None
        else dict(_mapping(recurrence_value, "recurrence"))
    )
    response_text = _top_level_or_values(data, values, "response_text")
    if response_text is not None and not isinstance(response_text, str):
        raise MqttParseError("response_text must be a string")

    return MqttAck(
        method=method,
        response=response,
        service=service,
        m_id=_identifier(_top_level_or_values(data, values, "m_id"), "m_id"),
        source_id=_optional_int(
            _top_level_or_values(data, values, "source_id"),
            "source_id",
        ),
        response_text=response_text,
        service_status=_optional_int(
            _top_level_or_values(data, values, "service_status"),
            "service_status",
        ),
        service_time_left=_optional_int(
            _top_level_or_values(data, values, "service_time_left"),
            "service_time_left",
        ),
        service_duration_left=_optional_int(
            _top_level_or_values(data, values, "service_duration_left"),
            "service_duration_left",
        ),
        duration=_optional_int(
            _top_level_or_values(data, values, "duration"),
            "duration",
        ),
        recurrence=recurrence,
        socref=_optional_number(
            _top_level_or_values(data, values, "socref"),
            "socref",
        ),
        values=dict(values),
    )
