"""Manual battery calibration: window guard and automatic-control hold."""

from __future__ import annotations

from time import time
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.pixiihome_controller.control.settings import OperationalSettings
from custom_components.pixiihome_controller.runtime import RuntimeData
from custom_components.pixiihome_controller.scheduler.models import (
    NormalizedRecurrence,
    NormalizedSchedule,
    ScheduleCompleteness,
    ScheduleConsistency,
    SchedulerOwnership,
)

AGGREGATOR = 7


def schedule(start, duration=3600, service_type=107, source_id=AGGREGATOR, recurring=False):
    start = int(start)
    return NormalizedSchedule(
        3, start, duration, start + duration, 300, service_type, True, None, None,
        source_id, NormalizedRecurrence(recurring, ()), SchedulerOwnership(),
        ScheduleCompleteness.COMPLETE, ScheduleConsistency.CONSISTENT,
    )


def runtime(schedules=(), *, service=0, allow_aggregator=True, status="current"):
    now = time()
    settings = OperationalSettings.defaults().as_dict()
    settings["values"]["price_adjust_external_target_soc"] = allow_aggregator
    return SimpleNamespace(
        operational_settings=OperationalSettings.from_mapping(settings),
        price_optimization_controller=SimpleNamespace(
            config=SimpleNamespace(managed_source_id=AGGREGATOR)
        ),
        telemetry=SimpleNamespace(value=lambda *_: service),
        solar_charging_controller=None,
        adaptive_peak_shaving_controller=None,
        scheduler_coordinator=SimpleNamespace(
            last_good_snapshot=SimpleNamespace(
                data_status=status,
                schedules=list(schedules),
                window_start_epoch=int(now) - 3600,
                window_end_epoch=int(now) + 2 * 86400,
            )
        ),
    )


def check(instance, duration=7200):
    RuntimeData._require_calibration_window(instance, duration)  # type: ignore[arg-type]


def test_idle_battery_with_a_free_window_may_calibrate() -> None:
    check(runtime([schedule(time() + 3 * 3600, service_type=102, source_id=None)]))


def test_demand_response_inside_the_window_blocks_calibration() -> None:
    with pytest.raises(HomeAssistantError, match="battery_calibration_conflicts_with_schedule"):
        check(runtime([schedule(time() + 1800, service_type=102, source_id=AGGREGATOR)]))


def test_aggregator_target_soc_is_overridden_only_when_allowed() -> None:
    target = [schedule(time() + 1800)]
    check(runtime(target, allow_aggregator=True))
    with pytest.raises(HomeAssistantError, match="battery_calibration_conflicts_with_schedule"):
        check(runtime(target, allow_aggregator=False))


def test_running_service_or_stale_list_blocks_calibration() -> None:
    with pytest.raises(HomeAssistantError, match="battery_calibration_service_running"):
        check(runtime(service=102))
    with pytest.raises(HomeAssistantError, match="battery_calibration_schedule_list_unavailable"):
        check(runtime(status="partial"))
    with pytest.raises(HomeAssistantError, match="battery_calibration_recurring_schedule"):
        check(runtime([schedule(time() + 86400, recurring=True)]))


@pytest.mark.asyncio
async def test_price_optimization_waits_while_calibration_runs() -> None:
    from custom_components.pixiihome_controller.price_controller import (
        PriceOptimizationController,
    )

    published: list[tuple[str, str | None]] = []
    controller = SimpleNamespace(
        runtime=SimpleNamespace(
            controller_state=SimpleNamespace(automatic_price_optimization_enabled=True),
            battery_calibration_active=lambda: True,
        ),
        _publish=lambda status, reason=None: published.append((status, reason)),
    )

    async def settle() -> None:
        return None

    controller._settle_unconfirmed_deletes = settle
    await PriceOptimizationController._tick_locked(controller)  # type: ignore[arg-type]
    assert published == [("waiting_for_priority", "battery_calibration_running")]
