"""Regression coverage for the Gateway's successful empty LIST response."""

from copy import deepcopy
from typing import Any

import pytest

from custom_components.pixiihome_controller.exceptions import SchedulerProtocolError
from custom_components.pixiihome_controller.protocol.scheduler import (
    parse_detail_response,
    parse_list_response,
    scheduler_epoch_from_gateway_wire,
    scheduler_epoch_to_gateway_wire,
)

EMPTY_LIST: dict[str, Any] = {
    "status": "ok",
    "Omnibus": {
        "msg": [1, 1, 7, 3, 255, 2, 101, 52, 0, 33, 0, 0],
        "data": {"entries": None, "from": 1791655199, "to": 1791658801},
    },
}


def test_gateway_wall_clock_epoch_round_trip_matches_live_stockholm_evidence() -> None:
    # The live Gateway returned 1791889920 for a schedule that physically ran
    # at 11:12 Europe/Stockholm (09:12 UTC) on 2026-10-13.
    assert scheduler_epoch_from_gateway_wire(1_791_889_920) == 1_791_882_720
    assert scheduler_epoch_to_gateway_wire(1_791_882_720) == 1_791_889_920


def test_gateway_wall_clock_epoch_preserves_unbounded_list_sentinels() -> None:
    assert scheduler_epoch_to_gateway_wire(0) == 0
    assert scheduler_epoch_to_gateway_wire(2_147_483_647) == 2_147_483_647
    assert scheduler_epoch_from_gateway_wire(0) == 0


def test_captured_successful_null_entries_mean_empty_window() -> None:
    assert parse_list_response(deepcopy(EMPTY_LIST)) == []


def test_detail_preserves_flat_recurrence_rule_for_display() -> None:
    response = {"status": "ok", "Omnibus": {
        "msg": [1, 1, 7, 3, 255, 2, 101, 200, 43, 33, 0, 0],
        "data": {"asset_id": "test", "duration": 3600, "service_enabled": 1,
                 "service_no": 3, "service_prio": 0, "service_type": 107,
                 "start_time": 1_893_456_000, "rec_type": 2, "recurrence1": 3,
                 "recurrence2": 0, "recurrence3": 0, "rec_end_time": 1_900_000_000},
    }}
    rule = parse_detail_response(response).recurrence
    assert rule == {"rec_type": 2, "recurrence1": 3, "recurrence2": 0,
                    "recurrence3": 0, "rec_end_time": 1_900_000_000}


@pytest.mark.parametrize("fault", [
    "missing_entries", "missing_from", "missing_to", "bad_status", "missing_status",
    "reversed_window", "negative_window", "boolean_window", "wrong_message",
    "object_entries", "string_entries",
])
def test_incomplete_or_failed_response_does_not_prove_absence(fault: str) -> None:
    response = deepcopy(EMPTY_LIST)
    data = response["Omnibus"]["data"]
    if fault.startswith("missing_"):
        key = fault.removeprefix("missing_")
        (response if key == "status" else data).pop(key)
    elif fault == "bad_status":
        response["status"] = "error"
    elif fault == "reversed_window":
        data["to"] = data["from"] - 1
    elif fault == "negative_window":
        data["from"] = -1
    elif fault == "boolean_window":
        data["from"] = True
    elif fault == "wrong_message":
        response["Omnibus"]["msg"][7] = 200
    elif fault == "object_entries":
        data["entries"] = {}
    else:
        data["entries"] = "null"
    with pytest.raises(SchedulerProtocolError):
        parse_list_response(response)
