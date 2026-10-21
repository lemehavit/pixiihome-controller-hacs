from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from custom_components.pixiihome_controller.aps_controller import (
    AdaptivePeakShavingController,
    build_aps_lease_request,
)
from custom_components.pixiihome_controller.battery import BatteryReading
from custom_components.pixiihome_controller.control.policy import AutomationPolicy
from custom_components.pixiihome_controller.control.settings import (
    ControllerState,
    OperationalSettings,
)


def _policy() -> AutomationPolicy:
    return AutomationPolicy.from_mapping(
        {
            "policy_id": "aps-test",
            "schema_version": 1,
            "revision": 1,
            "enabled": True,
            "allowed_operation_classes": ["automatic"],
            "allowed_service_classes": ["adaptivepeakshaving"],
            "time_window": {"start": "00:00", "end": "23:59", "days": list(range(7))},
            "max_duration_seconds": 300,
            "soc_min_percent": 10,
            "soc_max_percent": 90,
            "max_power_kw": 8,
            "external_control_allowed": False,
        }
    )


class Telemetry:
    def __init__(self, service: int = 0) -> None:
        self.service = service

    def is_fresh(self, family: str) -> bool:
        return family in {"core", "battery", "meter"}

    def value(self, family: str, key: str) -> object:
        if (family, key) == ("core", "srv_type"):
            return self.service
        return None


class FakeRuntime:
    def __init__(self) -> None:
        self.operational_settings = OperationalSettings.defaults()
        self.controller_state = ControllerState(adaptive_peak_shaving_enabled=True)
        self.telemetry = Telemetry()
        self.battery = BatteryReading(
            usable_soc=50,
            charge_power_limit_w=9_000,
            discharge_power_limit_w=7_000,
        )
        self.writes: list[dict[str, object]] = []
        self.verifications: list[int] = []
        self.released: list[int] = []

    def production_control_enabled(self) -> bool:
        return True

    def _automation_policy(self) -> AutomationPolicy:
        return _policy()

    def plant_health(self) -> Any:
        return SimpleNamespace(
            telemetry_fresh=True,
            system_safe=True,
            battery_safe=True,
            grid_safe=True,
            system_events_clear=True,
        )

    async def async_execute_peak_shaving_lease(
        self,
        message_id: int | None = None,
        *,
        request: object | None = None,
        stop: bool = False,
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "successful": True,
            "message_id": 8001 if message_id is None else message_id,
            "duration_seconds": 0 if stop else 120,
            "request": request,
        }
        self.writes.append(result)
        return result

    async def async_verify_aps_lease(self, message_id: int) -> dict[str, object]:
        self.verifications.append(message_id)
        return {"successful": True}

    def release_aps_lease_identity(self, message_id: int) -> None:
        self.released.append(message_id)


def _controller(runtime: FakeRuntime, now: list[float]) -> AdaptivePeakShavingController:
    controller = AdaptivePeakShavingController(cast(Any, runtime), clock=lambda: now[0])
    controller.priority_list = (102, 101, 105, 107)
    controller.priority_verified = True
    return controller


def test_request_uses_native_two_way_limits_and_yields_between_them() -> None:
    request = build_aps_lease_request(
        OperationalSettings.defaults()
        .set("aps_export_limit_kw", -1)
        .set("aps_import_limit_kw", 5),
        _policy(),
        usable_soc=50,
        charge_capacity_w=9_000,
        discharge_capacity_w=7_000,
    )

    assert request.payload == {
        "export_limit_w": -1_000,
        "import_limit_w": 5_000,
        "charge_max_w": 8_000,
        "discharge_max_w": 7_000,
        "keep_active": False,
        "max_limit_change_w": 1_000,
        "duration_seconds": 120,
    }


@pytest.mark.parametrize("low_soc,high_soc", [(0, 100), (5, 99), (10, 90)])
def test_usable_soc_limits_disable_only_the_exhausted_direction(low_soc, high_soc) -> None:
    settings = OperationalSettings.defaults()
    low = build_aps_lease_request(
        settings,
        _policy(),
        usable_soc=low_soc,
        charge_capacity_w=9_000,
        discharge_capacity_w=7_000,
    )
    high = build_aps_lease_request(
        settings,
        _policy(),
        usable_soc=high_soc,
        charge_capacity_w=9_000,
        discharge_capacity_w=7_000,
    )

    assert low.payload["charge_max_w"] == 8_000
    assert low.payload["discharge_max_w"] == 0
    assert high.payload["charge_max_w"] == 0
    assert high.payload["discharge_max_w"] == 7_000


def test_reversed_power_or_soc_limits_are_rejected() -> None:
    settings = OperationalSettings.defaults().set("aps_export_limit_kw", 6)
    with pytest.raises(ValueError, match="aps_limits_reversed"):
        build_aps_lease_request(
            settings,
            _policy(),
            usable_soc=50,
            charge_capacity_w=9_000,
            discharge_capacity_w=7_000,
        )
    settings = OperationalSettings.defaults().set("aps_min_soc_percent", 90)
    with pytest.raises(ValueError, match="aps_soc_limits_invalid"):
        build_aps_lease_request(
            settings,
            _policy(),
            usable_soc=50,
            charge_capacity_w=9_000,
            discharge_capacity_w=7_000,
        )


def test_priority_requires_scheduler_and_demand_response_above_aps() -> None:
    assert AdaptivePeakShavingController.priority_with_aps([102, 101, 107]) == (
        (102, 101, 105, 107),
        True,
    )
    assert AdaptivePeakShavingController.priority_with_aps([102, 101, 105, 107]) == (
        (102, 101, 105, 107),
        False,
    )
    with pytest.raises(ValueError, match="aps_service_priority_unverified"):
        AdaptivePeakShavingController.priority_with_aps([101, 105, 102, 107])


@pytest.mark.asyncio
async def test_higher_service_is_never_stopped_or_extended() -> None:
    runtime = FakeRuntime()
    runtime.telemetry.service = 101
    now = [50.0]
    controller = _controller(runtime, now)

    await controller.tick()

    assert controller.status == "waiting_for_priority"
    assert controller.reason == "scheduler_active"
    assert runtime.writes == []


@pytest.mark.asyncio
async def test_owned_lease_renews_exact_identity_and_applies_soc_change() -> None:
    runtime = FakeRuntime()
    runtime.telemetry.service = 105
    now = [70.0]
    controller = _controller(runtime, now)
    controller._message_id = 8001
    controller._lease_deadline = 120
    initial = build_aps_lease_request(
        runtime.operational_settings,
        _policy(),
        usable_soc=50,
        charge_capacity_w=9_000,
        discharge_capacity_w=7_000,
    )
    controller._applied_payload = tuple(sorted(initial.payload.items()))
    runtime.battery = replace(runtime.battery, usable_soc=90)

    await controller.tick()

    assert runtime.verifications == [8001]
    assert runtime.writes[0]["message_id"] == 8001
    request = runtime.writes[0]["request"]
    assert request is not None
    assert getattr(request, "payload")["charge_max_w"] == 0
    assert getattr(request, "payload")["discharge_max_w"] == 7_000


@pytest.mark.asyncio
async def test_foreign_aps_is_not_adopted() -> None:
    runtime = FakeRuntime()
    runtime.telemetry.service = 105
    controller = _controller(runtime, [10.0])

    await controller.tick()

    assert controller.reason == "foreign_adaptive_peak_shaving_active"
    assert runtime.writes == []
