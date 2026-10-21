"""Battery activity: each change is logged with who caused it and why."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from custom_components.pixiihome_controller.battery_activity import BatteryActivityTracker
from custom_components.pixiihome_controller.scheduler.models import (
    Ownership,
    SchedulerOwnership,
)

NOW = datetime(2026, 10, 21, 16, 0, tzinfo=UTC)


class Telemetry:
    def __init__(self) -> None:
        self.service: Any = 0
        self.meter_w = 500.0

    def value(self, family: str, key: str) -> Any:
        if (family, key) == ("core", "srv_type"):
            return self.service
        if (family, key) == ("meter", "meter_w"):
            return self.meter_w
        return None

    def is_fresh(self, family: str, **_: Any) -> bool:
        return True


def _runtime(schedules: tuple[Any, ...] = ()) -> SimpleNamespace:
    events: list[dict[str, Any]] = []
    hass = SimpleNamespace(bus=SimpleNamespace(
        async_fire=lambda event, data, **_: events.append(data) if event == "logbook_entry" else None
    ))
    snapshot = SimpleNamespace(schedules=schedules)
    return SimpleNamespace(
        hass=hass,
        serial="234",
        events=events,
        telemetry=Telemetry(),
        battery=SimpleNamespace(power_w=0.0, usable_soc=55.0),
        scheduler_coordinator=SimpleNamespace(last_good_snapshot=snapshot),
        scheduler_ownership=None,
        solar_charging_controller=None,
        adaptive_peak_shaving_controller=None,
        meter_limit_load_balancer=None,
        price_optimization_controller=None,
    )


def _schedule(start: datetime, minutes: int, service: int, label: str | None) -> Any:
    epoch = int(start.timestamp())
    return SimpleNamespace(
        sch_index=7, start_epoch=epoch, end_epoch=epoch + minutes * 60,
        duration_seconds=minutes * 60,
        service_type=service, enabled=True,
        ownership=SchedulerOwnership(Ownership.EXTERNAL, label),
    )


def _tracker(runtime: Any, clock: list[datetime]) -> BatteryActivityTracker:
    return BatteryActivityTracker(runtime, lambda: clock[0])


def test_external_schedule_is_named_as_the_cause() -> None:
    dr = _schedule(NOW - timedelta(minutes=5), 60, 102, "aggregator")
    runtime = _runtime((dr,))
    clock = [NOW]
    tracker = _tracker(runtime, clock)
    tracker.sample()
    assert runtime.events == []  # first sample: idle, nothing to report
    runtime.telemetry.service = 102
    runtime.battery.power_w = -9000.0
    clock[0] += timedelta(seconds=15)
    tracker.sample()
    message = runtime.events[-1]["message"]
    assert "Demand Response (102)" in message
    assert "external schedule (aggregator)" in message
    assert "SOC 55 %" in message
    assert tracker.state().startswith("Discharging")


def test_direction_change_must_hold_before_it_is_logged() -> None:
    runtime = _runtime()
    clock = [NOW]
    tracker = _tracker(runtime, clock)
    tracker.sample()
    runtime.battery.power_w = 3000.0
    for _ in range(4):  # one minute only
        clock[0] += timedelta(seconds=15)
        tracker.sample()
    assert runtime.events == []
    clock[0] += timedelta(minutes=3)
    tracker.sample()
    assert "started charging" in runtime.events[-1]["message"]
    assert "no service running" in runtime.events[-1]["message"]


def test_unknown_service_says_it_was_not_started_here() -> None:
    runtime = _runtime()
    clock = [NOW]
    tracker = _tracker(runtime, clock)
    tracker.sample()
    runtime.telemetry.service = 107
    clock[0] += timedelta(seconds=15)
    tracker.sample()
    assert "not started by this integration" in runtime.events[-1]["message"]
    assert tracker.attributes()["recent"][0]["message"] == runtime.events[-1]["message"]


def test_new_and_removed_external_schedules_are_logged() -> None:
    runtime = _runtime()
    clock = [NOW]
    tracker = _tracker(runtime, clock)
    tracker.sample()
    later = _schedule(NOW + timedelta(hours=2), 60, 102, None)
    runtime.scheduler_coordinator.last_good_snapshot = SimpleNamespace(schedules=(later,))
    clock[0] += timedelta(seconds=15)
    tracker.sample()
    assert "New external schedule" in runtime.events[-1]["message"]
    runtime.scheduler_coordinator.last_good_snapshot = SimpleNamespace(schedules=())
    clock[0] += timedelta(seconds=15)
    tracker.sample()
    assert "removed" in runtime.events[-1]["message"]


def test_our_own_price_schedule_is_not_reported_as_external() -> None:
    start = NOW + timedelta(hours=1)
    ours = _schedule(start, 45, 104, None)
    record = SimpleNamespace(
        lifecycle=SimpleNamespace(value="owned"),
        owner="automatic_price_optimization",
        service="peakshaving",
        intended_state={"starttime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "duration": 2700},
    )
    runtime = _runtime()
    runtime.scheduler_ownership = SimpleNamespace(records={1: record})
    clock = [NOW]
    tracker = _tracker(runtime, clock)
    tracker.sample()
    runtime.scheduler_coordinator.last_good_snapshot = SimpleNamespace(schedules=(ours,))
    clock[0] += timedelta(seconds=15)
    tracker.sample()
    assert runtime.events == []
