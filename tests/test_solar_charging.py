from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from custom_components.pixiihome_controller.battery import BatteryReading
from custom_components.pixiihome_controller.control.policy import AutomationPolicy
from custom_components.pixiihome_controller.control.settings import (
    ControllerState,
    OperationalSettings,
)
from custom_components.pixiihome_controller.solar_controller import (
    SolarChargingController,
    build_solar_lease_request,
)


def _policy() -> AutomationPolicy:
    return AutomationPolicy.from_mapping(
        {
            "policy_id": "solar-test",
            "schema_version": 1,
            "revision": 1,
            "enabled": True,
            "allowed_operation_classes": ["automatic"],
            "allowed_service_classes": ["adaptivepeakshaving"],
            "time_window": {"start": "00:00", "end": "23:59", "days": list(range(7))},
            "max_duration_seconds": 300,
            "soc_min_percent": 10,
            "soc_max_percent": 95,
            "max_power_kw": 8,
            "external_control_allowed": False,
        }
    )


class Telemetry:
    def __init__(self, *, meter_w: float = -2_000, service: int = 0) -> None:
        self.meter_w = meter_w
        self.service = service

    def is_fresh(self, family: str) -> bool:
        return family in {"core", "battery", "meter"}

    def value(self, family: str, key: str) -> object:
        if (family, key) == ("meter", "meter_w"):
            return self.meter_w
        if (family, key) == ("core", "srv_type"):
            return self.service
        return None


class FakeRuntime:
    def __init__(self) -> None:
        settings = OperationalSettings.defaults()
        for key, value in {
            "solar_start_debounce_seconds": 60,
            "solar_stop_debounce_seconds": 30,
            "solar_lease_seconds": 120,
        }.items():
            settings = settings.set(key, value)
        self.operational_settings = settings
        self.controller_state = ControllerState(automatic_solar_charging_enabled=True)
        self.telemetry = Telemetry()
        self.battery = BatteryReading(
            usable_soc=50,
            power_w=0,
            charge_power_limit_w=9_000,
        )
        self.writes: list[dict[str, object]] = []
        self.verifications: list[int] = []
        self.released: list[int] = []
        self.next_message_id = 9001

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

    async def async_execute_solar_lease(
        self,
        message_id: int | None = None,
        *,
        stop: bool = False,
    ) -> dict[str, object]:
        owned = self.next_message_id if message_id is None else message_id
        result: dict[str, object] = {
            "successful": True,
            "lifecycle": "reconciled",
            "duration_seconds": 0 if stop else 120,
            "message_id": owned,
        }
        self.writes.append(result)
        return result

    async def async_verify_solar_lease(self, message_id: int) -> dict[str, object]:
        self.verifications.append(message_id)
        return {"successful": True, "message_id": message_id}

    def release_solar_lease_identity(self, message_id: int) -> None:
        self.released.append(message_id)


def _controller(runtime: FakeRuntime, now: list[float]) -> SolarChargingController:
    controller = SolarChargingController(cast(Any, runtime), clock=lambda: now[0])
    controller.priority_list = (101, 105, 107)
    controller.priority_verified = True
    return controller


def test_solar_lease_is_charge_only_and_bounded_by_capacity_and_policy() -> None:
    settings = OperationalSettings.defaults().set("solar_max_charge_power_kw", 10)
    request = build_solar_lease_request(settings, _policy(), charge_capacity_w=9_000)

    assert request.payload == {
        "export_limit_w": -200,
        "import_limit_w": 10_000,
        "charge_max_w": 8_000,
        "discharge_max_w": 0,
        "keep_active": True,
        "max_limit_change_w": 1_000,
        "duration_seconds": 120,
    }


def test_scheduler_must_have_higher_priority_than_aps() -> None:
    assert SolarChargingController.validate_priority([101, 105, 107]) == (101, 105, 107)
    with pytest.raises(ValueError, match="solar_scheduler_priority_unverified"):
        SolarChargingController.validate_priority([105, 101, 107])
    with pytest.raises(ValueError, match="solar_scheduler_priority_unverified"):
        SolarChargingController.validate_priority([101, 107])


@pytest.mark.asyncio
async def test_soc_ceiling_stops_only_the_exact_owned_solar_lease():
    runtime = FakeRuntime()
    controller = _controller(runtime, [0.0])
    controller._message_id = 9001
    controller._lease_deadline = 120
    runtime.telemetry.service = 105
    runtime.battery = replace(runtime.battery, usable_soc=90)
    await controller.tick()
    assert controller.status == "soc_limit"
    assert runtime.verifications == [9001]
    assert len(runtime.writes) == 1
    assert runtime.writes[0]["duration_seconds"] == 0
    assert controller._message_id is None


@pytest.mark.asyncio
async def test_soc_ceiling_never_stops_foreign_or_scheduled_service():
    runtime = FakeRuntime()
    controller = _controller(runtime, [0.0])
    runtime.telemetry.service = 101
    runtime.battery = replace(runtime.battery, usable_soc=99)
    await controller.tick()
    assert runtime.writes == []


def test_missing_aps_is_inserted_directly_below_scheduler() -> None:
    assert SolarChargingController.priority_with_aps([102, 101, 107]) == (
        (102, 101, 105, 107),
        True,
    )
    assert SolarChargingController.priority_with_aps([102, 101, 105, 107]) == (
        (102, 101, 105, 107),
        False,
    )
    with pytest.raises(ValueError, match="solar_scheduler_priority_unverified"):
        SolarChargingController.priority_with_aps([102, 105, 101, 107])
    with pytest.raises(ValueError, match="solar_scheduler_priority_unverified"):
        SolarChargingController.priority_with_aps([102, 107])


@pytest.mark.asyncio
@pytest.mark.parametrize("soc", [0, 5])
async def test_solar_can_charge_below_a_discharge_reserve(soc) -> None:
    runtime = FakeRuntime()
    runtime.battery = replace(runtime.battery, usable_soc=soc)
    runtime.operational_settings = runtime.operational_settings.set(
        "solar_start_debounce_seconds", 0
    )
    controller = _controller(runtime, [0.0])
    await controller.tick()
    assert controller.status == "active"
    assert len(runtime.writes) == 1


@pytest.mark.asyncio
async def test_start_debounce_and_reconstructed_surplus_avoid_oscillation() -> None:
    runtime = FakeRuntime()
    now = [0.0]
    controller = _controller(runtime, now)

    await controller.tick()
    assert controller.status == "waiting_surplus"
    assert runtime.writes == []

    now[0] = 60
    await controller.tick()
    assert controller.status == "active"
    assert len(runtime.writes) == 1
    assert controller._message_id == 9001

    runtime.telemetry.service = 105
    runtime.telemetry.meter_w = 0
    runtime.battery = replace(runtime.battery, power_w=2_000)
    now[0] = 70
    await controller.tick()

    assert controller.status == "active"
    assert controller.potential_surplus_w == 2_000
    assert len(runtime.writes) == 1


@pytest.mark.asyncio
async def test_active_lease_is_not_renewed_after_surplus_disappears() -> None:
    runtime = FakeRuntime()
    now = [0.0]
    controller = _controller(runtime, now)
    controller._lease_deadline = 120
    controller._message_id = 9001
    runtime.telemetry.service = 105
    runtime.telemetry.meter_w = 400
    runtime.battery = replace(runtime.battery, power_w=0)

    await controller.tick()
    now[0] = 31
    await controller.tick()

    assert controller.status == "lease_expiring"
    assert runtime.writes == []


@pytest.mark.asyncio
async def test_schedule_preempts_without_a_write_or_timer_extension() -> None:
    runtime = FakeRuntime()
    runtime.telemetry.service = 101
    now = [100.0]
    controller = _controller(runtime, now)
    controller._lease_deadline = 150
    controller._message_id = 9001

    await controller.tick()

    assert controller.status == "waiting_for_idle"
    assert controller.reason == "scheduler_active"
    assert controller._lease_deadline == 150
    assert runtime.writes == []


@pytest.mark.asyncio
async def test_idle_telemetry_during_owned_lease_does_not_send_a_duplicate() -> None:
    runtime = FakeRuntime()
    now = [10.0]
    controller = _controller(runtime, now)
    controller._lease_deadline = 120
    controller._message_id = 9001

    await controller.tick()

    assert controller.status == "starting"
    assert runtime.writes == []


@pytest.mark.asyncio
async def test_renewal_verifies_and_reuses_owned_message_id() -> None:
    runtime = FakeRuntime()
    runtime.telemetry.service = 105
    runtime.battery = replace(runtime.battery, power_w=2_000)
    now = [70.0]
    controller = _controller(runtime, now)
    controller._lease_deadline = 120
    controller._message_id = 9001

    await controller.tick()

    assert runtime.verifications == [9001]
    assert runtime.writes[0]["message_id"] == 9001
    assert controller._lease_deadline == 190


@pytest.mark.asyncio
async def test_stop_uses_duration_zero_and_releases_identity() -> None:
    runtime = FakeRuntime()
    now = [10.0]
    controller = _controller(runtime, now)
    controller._lease_deadline = 120
    controller._message_id = 9001

    await controller.stop()

    assert runtime.writes == [
        {
            "successful": True,
            "lifecycle": "reconciled",
            "duration_seconds": 0,
            "message_id": 9001,
        }
    ]
    assert controller._message_id is None
    assert controller._lease_deadline == 0


@pytest.mark.asyncio
async def test_acknowledged_unverified_write_expires_without_retry() -> None:
    runtime = FakeRuntime()

    async def unverified(
        message_id: int | None = None,
        *,
        stop: bool = False,
    ) -> dict[str, object]:
        del message_id, stop
        return {
            "successful": False,
            "command_acknowledged": True,
            "lifecycle": "acknowledged_not_active",
            "duration_seconds": 120,
            "message_id": 9001,
        }

    runtime.async_execute_solar_lease = unverified  # type: ignore[method-assign]
    now = [60.0]
    controller = _controller(runtime, now)
    controller._surplus_since = 0

    await controller.tick()

    assert controller.status == "lease_expiring"
    assert controller.reason == "solar_lease_verification_failed"
    assert controller._message_id == 9001
    assert controller._lease_deadline == 180


@pytest.mark.asyncio
async def test_foreign_aps_and_maximum_usable_soc_fail_closed() -> None:
    runtime = FakeRuntime()
    runtime.telemetry.service = 105
    now = [10.0]
    controller = _controller(runtime, now)

    await controller.tick()
    assert controller.reason == "foreign_adaptive_peak_shaving_active"

    runtime.telemetry.service = 0
    runtime.battery = replace(runtime.battery, usable_soc=90)
    await controller.tick()
    assert controller.status == "soc_limit"
    assert runtime.writes == []
