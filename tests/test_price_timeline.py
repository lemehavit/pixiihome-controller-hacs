"""Price visibility must never turn an uncommitted proposal into a schedule."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.pixiihome_controller.calendar import PixiiPriceOptimizationCalendar
from custom_components.pixiihome_controller.control.price_optimization import (
    PriceDirection,
    PricePlan,
    PricePlanAction,
)
from custom_components.pixiihome_controller.control.settings import ControllerState
from custom_components.pixiihome_controller.price_timeline import price_timeline
from custom_components.pixiihome_controller.scheduler.ownership import (
    OwnershipLifecycle,
    SchedulerOwnershipRecord,
    SchedulerOwnershipStore,
)
from custom_components.pixiihome_controller.sensor import PixiiPriceScheduleSensor

NOW = datetime(2030, 10, 26, 22, tzinfo=UTC)


def action(start=NOW, power=2.0):
    return PricePlanAction(
        start=start, end=start + timedelta(hours=1), direction=PriceDirection.CHARGE,
        power_kw=power, service="demandresponse", values={"pacref": power * 1000},
        discharge_mode=None, price_sek_per_kwh=0.5, reference_price_sek_per_kwh=2,
        spread_sek_per_kwh=1.5, projected_usable_soc_percent=80, expected_margin_sek=0,
    )


def controller(actions=()):
    runtime = SimpleNamespace(
        serial="GATEWAY", scheduler_ownership=SchedulerOwnershipStore("entry", "GATEWAY"),
        controller_state=ControllerState(automatic_price_optimization_enabled=True),
    )
    result = SimpleNamespace(
        runtime=runtime, status="blocked", reason="waiting_for_priority",
        last_planned_at=NOW, plan=PricePlan(tuple(actions), (), "plan", 10, 90, 0),
    )
    runtime.price_optimization_controller = result
    return result


def record(instance, item, *, lifecycle=OwnershipLifecycle.OWNED, m_id=240000001):
    owned = SchedulerOwnershipRecord(
        "entry", "GATEWAY", m_id, item.service,
        {"starttime": item.start.isoformat(), "duration": item.duration_seconds,
         "values": dict(item.values)}, lifecycle=lifecycle,
        owner="automatic_price_optimization",
    )
    instance.runtime.scheduler_ownership.register(owned)


def test_preview_is_not_scheduled_and_reconciled_record_replaces_duplicate():
    item = action()
    instance = controller((item,))
    assert price_timeline(instance, NOW)[0]["schedule_status"] == "planned"
    record(instance, item)
    entries = price_timeline(instance, NOW)
    assert len(entries) == 1
    assert entries[0]["schedule_status"] == "scheduled"
    assert entries[0]["in_progress"] is True
    assert entries[0]["message_id"] == 240000001


@pytest.mark.parametrize(
    "lifecycle", [OwnershipLifecycle.PROVISIONAL, OwnershipLifecycle.CONFLICTED]
)
def test_partial_or_uncertain_write_is_never_counted_as_created(lifecycle):
    item = action()
    instance = controller((item,))
    record(instance, item, lifecycle=lifecycle)
    entries = price_timeline(instance, NOW)
    assert len(entries) == 1
    assert entries[0]["schedule_status"] == "unverified"
    sensor = PixiiPriceScheduleSensor(instance.runtime, "price_scheduled_count")
    assert sensor.native_value == 0


def test_pause_or_disable_keeps_real_schedule_but_hides_disabled_proposals():
    instance = controller((action(), action(NOW + timedelta(hours=2))))
    record(instance, action())
    instance.runtime.controller_state = ControllerState()
    assert len(price_timeline(instance, NOW)) == 1
    assert price_timeline(instance, NOW)[0]["schedule_status"] == "scheduled"
    assert price_timeline(instance, NOW + timedelta(hours=1)) == []


def test_timestamp_sensor_has_end_power_and_creation_status():
    instance = controller((action(),))
    sensor = PixiiPriceScheduleSensor(instance.runtime, "price_next_charge")
    assert sensor.native_value == NOW
    attrs = sensor.extra_state_attributes
    assert attrs["end"] == (NOW + timedelta(hours=1)).isoformat()
    assert attrs["power_kw"] == 2
    assert attrs["schedule_status"] == "planned"
    assert PixiiPriceScheduleSensor(instance.runtime, "price_next_discharge").native_value is None


def test_calendar_distinguishes_preview_and_created_with_aware_dates():
    first = action()
    second = action(NOW + timedelta(hours=2))
    instance = controller((first, second))
    record(instance, first)
    calendar = PixiiPriceOptimizationCalendar(instance.runtime)
    events = calendar._events(NOW)
    assert len(events) == 2
    assert "Skapat i Pixii" in events[0].summary
    assert "Planförslag" in events[1].summary
    assert events[0].start.tzinfo is not None
    assert events[0].end == NOW + timedelta(hours=1)
    assert events[0].uid != events[1].uid


def test_ended_proposals_and_orphaned_records_disappear():
    instance = controller((action(NOW - timedelta(hours=2)),))
    record(instance, action(), lifecycle=OwnershipLifecycle.ORPHANED)
    assert price_timeline(instance, NOW) == []
