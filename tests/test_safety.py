from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest

from custom_components.pixiihome_controller.protocol.models import ParsedMqttMessage
from custom_components.pixiihome_controller.runtime import RuntimeData

SERIAL = "GATEWAY"


def _runtime(
    *,
    core: dict[str, Any] | None = None,
    battery: dict[str, Any] | None = None,
    received_at: datetime | None = None,
    include_sys_events: bool = True,
) -> RuntimeData:
    runtime = RuntimeData(cast(Any, SimpleNamespace()), SERIAL)
    timestamp = received_at or datetime.now(UTC)
    core_payload = (
        {"sys_status": 1, "grid_status": 1, "srv_type": 0} if core is None else dict(core)
    )
    if include_sys_events:
        core_payload.setdefault("sys_events", 0)
    battery_payload = {"batt_status": 1} if battery is None else battery
    runtime.telemetry.apply(
        ParsedMqttMessage(
            serial=SERIAL,
            family="core",
            payload=core_payload,
            topic="pixii/status/GATEWAY/core",
            received_at=timestamp,
        )
    )
    runtime.telemetry.apply(
        ParsedMqttMessage(
            serial=SERIAL,
            family="battery",
            payload=battery_payload,
            topic="pixii/status/GATEWAY/battery",
            received_at=timestamp,
        )
    )
    return runtime


def test_normal_idle_telemetry_is_explicitly_healthy():
    health = _runtime().plant_health()

    assert health.healthy
    assert health.reasons == ()



def test_zero_sys_events_is_healthy_and_diagnosed():
    health = _runtime().plant_health()

    assert health.healthy
    assert health.system_events == 0
    assert health.as_dict()["system_events"] == 0


@pytest.mark.parametrize(
    ("events", "include_sys_events", "reason"),
    [
        (None, False, "sys_events_missing"),
        (True, True, "sys_events_malformed"),
        ("0", True, "sys_events_malformed"),
        (-1, True, "sys_events_malformed"),
        (1, True, "sys_events_unsafe"),
        (2, True, "sys_events_unsafe"),
        (1 << 15, True, "sys_events_unsafe"),
    ],
)
def test_non_clear_sys_events_blocks_health(
    events: Any,
    include_sys_events: bool,
    reason: str,
):
    core: dict[str, Any] = {"sys_status": 1, "grid_status": 1, "srv_type": 0}
    if events is not None:
        core["sys_events"] = events

    health = _runtime(
        core=core,
        include_sys_events=include_sys_events,
    ).plant_health()

    assert not health.healthy
    assert health.system_events == events
    assert reason in health.reasons

@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("sys_status", 0, "sys_status_undefined"),
        ("sys_status", None, "sys_status_missing"),
        ("sys_status", "1", "sys_status_malformed"),
        ("sys_status", 4, "sys_status_unsafe"),
        ("sys_status", 3, "sys_status_unsafe"),
        ("sys_status", 6, "sys_status_unsafe"),
        ("batt_status", 0, "batt_status_undefined"),
        ("batt_status", None, "batt_status_missing"),
        ("batt_status", "1", "batt_status_malformed"),
        ("batt_status", 4, "batt_status_unsafe"),
        ("batt_status", 3, "batt_status_unsafe"),
        ("batt_status", 6, "batt_status_unsafe"),
    ],
)
def test_statuses_other_than_normal_block(
    field: str,
    value: Any,
    reason: str,
):
    core = {"sys_status": 1, "grid_status": 1, "srv_type": 0}
    battery = {"batt_status": 1}
    target = core if field.startswith("sys") else battery
    if value is None:
        target.pop(field)
    else:
        target[field] = value

    health = _runtime(core=core, battery=battery).plant_health()

    assert not health.healthy
    assert reason in health.reasons


@pytest.mark.parametrize(
    ("service", "reason", "protected"),
    [
        (None, "service_type_missing", False),
        ("0", "service_type_malformed", False),
        (999, "service_type_unknown", True),
        (116, None, True),
        (111, None, True),
        (107, None, False),
    ],
)
def test_service_state_is_operation_impact_not_plant_health(
    service: Any,
    reason: str | None,
    protected: bool,
):
    core: dict[str, Any] = {"sys_status": 1, "grid_status": 1}
    if service is not None:
        core["srv_type"] = service

    runtime = _runtime(core=core)
    health = runtime.plant_health()
    impact = runtime.operation_impact()

    assert health.healthy
    assert impact.protected_service_active is protected
    if reason is not None:
        assert reason in impact.reasons


@pytest.mark.parametrize("family", ["core", "battery"])
def test_stale_core_or_battery_blocks(family: str):
    old = datetime.now(UTC) - timedelta(seconds=181)
    runtime = _runtime(received_at=datetime.now(UTC))
    runtime.telemetry.received_at[family] = old

    health = runtime.plant_health()

    assert not health.healthy
    assert f"{family}_telemetry_stale" in health.reasons
