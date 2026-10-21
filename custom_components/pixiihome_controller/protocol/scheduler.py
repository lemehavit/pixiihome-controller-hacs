"""Read-only parser for sanitized Pixii scheduler evidence."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from math import isfinite
from typing import Any
from zoneinfo import ZoneInfo

from ..exceptions import SchedulerProtocolError
from .models import (
    SchedulerDetail,
    SchedulerDetailRequest,
    SchedulerEntry,
    SchedulerListRequest,
)

LIST_MESSAGE = (1, 0, 5, 3, 255, 2, 101, 52, 0, 33, 0, 0)
DETAIL_MESSAGE_PREFIX = (1, 0, 5, 3, 255, 2, 101, 200)
MAX_DETAIL_METADATA_DEPTH = 4
MAX_DETAIL_METADATA_NODES = 128
MAX_DETAIL_METADATA_KEYS = 32
MAX_DETAIL_METADATA_TEXT = 256
MAX_DETAIL_ASSET_ID_LENGTH = 128
MAX_DETAIL_SOURCE_ID = 2_147_483_647
MIN_PROTOCOL_INTEGER = -2_147_483_648
MAX_PROTOCOL_INTEGER = 2_147_483_647
MAX_DETAIL_METADATA_NUMBER_DIGITS = 32
MAX_DETAIL_METADATA_BYTES = 16_384
PIXII_SCHEDULER_ZONE = ZoneInfo("Europe/Stockholm")


def scheduler_epoch_to_gateway_wire(value: int) -> int:
    """Encode a real UTC epoch as the Gateway's local-wall-clock epoch.

    PowerShaper 2.3.63 represents scheduler wall time as an epoch whose UTC
    clock fields are actually Europe/Stockholm local fields.  LIST windows and
    DETAIL writes must use that representation even though public Home
    Assistant state uses real UTC epochs.
    """
    epoch = _bounded_protocol_integer(value, "scheduler epoch")
    if epoch in (0, MAX_PROTOCOL_INTEGER):
        return epoch
    local = datetime.fromtimestamp(epoch, UTC).astimezone(PIXII_SCHEDULER_ZONE)
    wire = int(local.replace(tzinfo=UTC).timestamp())
    return max(0, min(MAX_PROTOCOL_INTEGER, wire))


def scheduler_epoch_from_gateway_wire(value: int) -> int:
    """Decode the Gateway's Europe/Stockholm wall-clock epoch to real UTC."""
    epoch = _bounded_protocol_integer(value, "scheduler wire epoch")
    if epoch == 0:
        return 0
    wall = datetime.fromtimestamp(epoch, UTC).replace(tzinfo=None)
    local = wall.replace(tzinfo=PIXII_SCHEDULER_ZONE, fold=0)
    round_trip = local.astimezone(UTC).astimezone(PIXII_SCHEDULER_ZONE)
    if round_trip.replace(tzinfo=None) != wall:
        raise SchedulerProtocolError("scheduler wire time does not exist in Europe/Stockholm")
    return int(local.astimezone(UTC).timestamp())


def build_list_request(from_epoch: int, to_epoch: int) -> dict[str, object]:
    """Build the captured scheduler LIST request envelope."""
    bounded_from = _bounded_protocol_integer(from_epoch, "from")
    bounded_to = _bounded_protocol_integer(to_epoch, "to")
    return {
        "cmd": "message",
        "Omnibus": {
            "msg": list(LIST_MESSAGE),
            "data": {"from": bounded_from, "to": bounded_to},
        },
    }


def build_detail_request(sch_index: int) -> dict[str, object]:
    """Build the captured scheduler DETAIL request envelope."""
    bounded_index = _bounded_protocol_integer(sch_index, "sch_index")
    return {
        "cmd": "message",
        "Omnibus": {
            "msg": list(DETAIL_MESSAGE_PREFIX) + [bounded_index, 33, 1, 0],
            "data": bounded_index,
        }
    }


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchedulerProtocolError(f"{label} must be an object")
    return value


def _bounded_protocol_integer(value: Any, label: str) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not MIN_PROTOCOL_INTEGER <= value <= MAX_PROTOCOL_INTEGER
    ):
        raise SchedulerProtocolError(f"{label} must be a bounded integer")
    return value


def _number(value: Any, label: str) -> int:
    return _bounded_protocol_integer(value, label)



def _bounded_detail_value(value: Any, *, depth: int, budget: list[int]) -> object:
    """Retain only bounded JSON primitives from protocol DETAIL metadata."""
    budget[0] -= 1
    if budget[0] < 0 or depth > MAX_DETAIL_METADATA_DEPTH:
        raise SchedulerProtocolError("scheduler DETAIL metadata is too large")
    if isinstance(value, float) and not isfinite(value):
        raise SchedulerProtocolError("scheduler DETAIL metadata contains non-finite number")
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and len(str(abs(value))) > MAX_DETAIL_METADATA_NUMBER_DIGITS
    ):
        raise SchedulerProtocolError("scheduler DETAIL metadata number is too large")
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        if len(value) > MAX_DETAIL_METADATA_TEXT:
            raise SchedulerProtocolError("scheduler DETAIL metadata text is too large")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_DETAIL_METADATA_KEYS:
            raise SchedulerProtocolError("scheduler DETAIL metadata has too many keys")
        bounded: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > MAX_DETAIL_METADATA_TEXT:
                raise SchedulerProtocolError("scheduler DETAIL metadata key is invalid")
            bounded[key] = _bounded_detail_value(item, depth=depth + 1, budget=budget)
        return bounded
    if isinstance(value, list | tuple):
        if len(value) > MAX_DETAIL_METADATA_KEYS:
            raise SchedulerProtocolError("scheduler DETAIL metadata has too many items")
        return [
            _bounded_detail_value(item, depth=depth + 1, budget=budget) for item in value
        ]
    raise SchedulerProtocolError("scheduler DETAIL metadata contains an unsupported value")


def _safe_asset_id(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > MAX_DETAIL_ASSET_ID_LENGTH:
        return None
    blocked_markers = (
        "password",
        "passwd",
        "secret",
        "token",
        "sessid",
        "session",
        "alevel",
        "cookie",
        "credential",
        "api_key",
        "apikey",
        "authorization",
        "auth",
    )
    if any(marker in value.casefold() for marker in blocked_markers):
        return None
    if any(ord(char) < 32 or ord(char) > 126 for char in value):
        return None
    return value


def _message(payload: Mapping[str, Any]) -> tuple[int, ...]:
    omnibus = _mapping(payload.get("Omnibus"), "Omnibus")
    value = omnibus.get("msg")
    if not isinstance(value, list):
        raise SchedulerProtocolError("Omnibus.msg must be a bounded integer array")
    return tuple(
        _bounded_protocol_integer(item, "Omnibus.msg") for item in value
    )


def parse_list_request(payload: Mapping[str, Any]) -> SchedulerListRequest:
    message = _message(payload)
    if message != LIST_MESSAGE:
        raise SchedulerProtocolError("Unexpected scheduler LIST message")
    data = _mapping(_mapping(payload["Omnibus"], "Omnibus").get("data"), "Omnibus.data")
    return SchedulerListRequest(
        from_epoch=_number(data.get("from"), "from"),
        to_epoch=_number(data.get("to"), "to"),
        message=message,
    )


def parse_list_response(payload: Mapping[str, Any]) -> list[SchedulerEntry]:
    omnibus = _mapping(payload.get("Omnibus"), "Omnibus")
    message = _message(payload)
    if message != (1, 1, 7, 3, 255, 2, 101, 52, 0, 33, 0, 0):
        raise SchedulerProtocolError("Unexpected scheduler LIST response message")
    data = _mapping(omnibus.get("data"), "Omnibus.data")
    raw_entries = data.get("entries")
    if "entries" in data and raw_entries is None and payload.get("status") == "ok":
        # Gateway 2.3.63 returns null (not []) for a successful empty window.
        # Require the captured window fields so a missing/malformed response
        # cannot be mistaken for verified absence after a DELETE.
        start = _number(data.get("from"), "from")
        end = _number(data.get("to"), "to")
        if 0 <= start <= end:
            return []
        raise SchedulerProtocolError("Scheduler LIST empty response window is invalid")
    if not isinstance(raw_entries, list):
        raise SchedulerProtocolError("Scheduler LIST response has no entries array")
    entries: list[SchedulerEntry] = []
    for raw in raw_entries:
        item = _mapping(raw, "scheduler entry")
        entries.append(
            SchedulerEntry(
                entry=_number(item.get("entry"), "entry"),
                duration_seconds=_number(item.get("sch_duration"), "sch_duration"),
                enabled=bool(_number(item.get("sch_enabled"), "sch_enabled")),
                sch_index=_number(item.get("sch_index"), "sch_index"),
                recurrence=(
                    item.get("sch_recurrence", False)
                    if isinstance(item.get("sch_recurrence", False), bool)
                    else bool(
                        _number(item.get("sch_recurrence"), "sch_recurrence")
                    )
                ),
                start_epoch=_number(item.get("sch_start"), "sch_start"),
                service_no=_number(item.get("srv_no"), "srv_no"),
                service_type=_number(item.get("srv_type"), "srv_type"),
            )
        )
    return entries


def parse_detail_request(payload: Mapping[str, Any]) -> SchedulerDetailRequest:
    message = _message(payload)
    if message[:8] != DETAIL_MESSAGE_PREFIX or len(message) != 12 or message[9:] != (33, 1, 0):
        raise SchedulerProtocolError("Unexpected scheduler DETAIL message")
    return SchedulerDetailRequest(sch_index=message[8], message=message)


def parse_detail_response(payload: Mapping[str, Any]) -> SchedulerDetail:
    omnibus = _mapping(payload.get("Omnibus"), "Omnibus")
    message = _message(payload)
    if (
        message[:8] != (1, 1, 7, 3, 255, 2, 101, 200)
        or len(message) != 12
        or message[9:] != (33, 0, 0)
    ):
        raise SchedulerProtocolError("Unexpected scheduler DETAIL response message")
    data = _mapping(omnibus.get("data"), "Omnibus.data")
    recurrence_value = data.get("recurrence", {})
    # Preserve the flat rule used by the Gateway calendar as well as nested metadata.
    if isinstance(recurrence_value, dict):
        recurrence_value = {
            **recurrence_value,
            **{key: _number(data[key], key) for key in (
                "rec_type", "recurrence1", "recurrence2", "recurrence3",
                "recurrence4", "rec_end_time",
            ) if key in data},
        }
    bounded_recurrence = _bounded_detail_value(
        recurrence_value,
        depth=0,
        budget=[MAX_DETAIL_METADATA_NODES],
    )
    recurrence = (
        bounded_recurrence
        if isinstance(bounded_recurrence, dict)
        else {"raw": bounded_recurrence}
    )
    try:
        metadata_bytes = len(
            json.dumps(
                recurrence,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as err:
        raise SchedulerProtocolError("scheduler DETAIL metadata is not JSON-safe") from err
    if metadata_bytes > MAX_DETAIL_METADATA_BYTES:
        raise SchedulerProtocolError("scheduler DETAIL metadata is too large")
    source = data.get("source_id")
    source_id = _number(source, "source_id") if source is not None else None
    if source_id is not None and not 0 <= source_id <= MAX_DETAIL_SOURCE_ID:
        raise SchedulerProtocolError("source_id is outside its supported range")
    return SchedulerDetail(
        sch_index=message[8],
        asset_id=_safe_asset_id(data.get("asset_id")),
        duration_seconds=_number(data.get("duration"), "duration"),
        recurrence=recurrence,
        service_enabled=bool(_number(data.get("service_enabled"), "service_enabled")),
        service_no=_number(data.get("service_no"), "service_no"),
        service_priority=_number(data.get("service_prio"), "service_prio"),
        service_type=_number(data.get("service_type"), "service_type"),
        source_id=source_id,
        start_epoch=_number(data.get("start_time"), "start_time"),
    )
