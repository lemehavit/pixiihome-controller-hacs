"""Automatic price-controller scheduling tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.pixiihome_controller.battery import BatteryReading
from custom_components.pixiihome_controller.control.models import PlantHealth
from custom_components.pixiihome_controller.control.policy import AutomationPolicy
from custom_components.pixiihome_controller.control.price_optimization import (
    CONF_PRICE_OPTIMIZATION,
    PriceOptimizationConfig,
)
from custom_components.pixiihome_controller.control.settings import (
    ControllerState,
    OperationalSettings,
)
from custom_components.pixiihome_controller.price_controller import (
    PRICE_OWNER,
    PriceOptimizationController,
)
from custom_components.pixiihome_controller.scheduler.mutations import (
    SchedulerTransactionStatus,
)
from custom_components.pixiihome_controller.scheduler.ownership import SchedulerOwnershipStore


class States:
    def __init__(self, state) -> None:
        self.state = state

    def get(self, entity_id: str):
        return self.state if entity_id == "sensor.prices" else None


class Coordinator:
    """Canonical inventory, kept in step with the fake manager's schedules."""

    def __init__(self) -> None:
        self.manager = None

    @property
    def last_good_snapshot(self):
        manager = getattr(self.manager, "scheduler_manager", None)
        schedules = tuple(manager.normalized_schedules) if manager is not None else ()
        return SimpleNamespace(data_status="current", schedules=schedules)

    async def async_refresh(self):
        return self.last_good_snapshot


class Mutations:
    def __init__(self) -> None:
        self.created = []
        self.deleted = []

    async def create(self, request):
        self.created.append(request)
        return SimpleNamespace(
            status=SchedulerTransactionStatus.SUCCESS,
            reason="",
        )

    async def delete(self, m_id):
        self.deleted.append(m_id)
        return SimpleNamespace(
            status=SchedulerTransactionStatus.SUCCESS,
            reason="",
        )


def policy() -> AutomationPolicy:
    return AutomationPolicy(
        policy_id="price-test",
        schema_version=1,
        revision=1,
        enabled=True,
        allowed_operation_classes=("automatic",),
        allowed_service_classes=("demandresponse", "peakshaving"),
        time_window={"start": "00:00", "end": "23:59", "days": list(range(7))},
        max_duration_seconds=7200,
        soc_min_percent=10,
        soc_max_percent=90,
        max_power_kw=10,
        external_control_allowed=True,
    )


def runtime(*, export_allowed: bool = False, schedule_writes: bool = True):
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    prices = [
        {
            "start": (start + timedelta(hours=index)).isoformat(),
            "end": (start + timedelta(hours=index + 1)).isoformat(),
            "price": price,
        }
        for index, price in enumerate((0.5, 1.0, 3.0))
    ]
    state = SimpleNamespace(
        state="0.5",
        attributes={"unit_of_measurement": "SEK/kWh", "prices": prices},
        last_updated=datetime.now(UTC),
    )
    settings = OperationalSettings.defaults().set("price_usable_capacity_kwh", 10.0)
    settings = settings.set("price_export_allowed", export_allowed)
    settings = settings.set("price_schedule_writes_enabled", schedule_writes)
    mutations = Mutations()
    instance = SimpleNamespace(
        entry=SimpleNamespace(
            options={
                CONF_PRICE_OPTIMIZATION: PriceOptimizationConfig(
                    price_entity_id="sensor.prices"
                ).as_dict()
            }
        ),
        hass=SimpleNamespace(states=States(state)),
        scheduler_mutation_manager=mutations,
        scheduler_coordinator=Coordinator(),
        scheduler_manager=SimpleNamespace(normalized_schedules=[]),
        scheduler_ownership=SchedulerOwnershipStore("entry", "gateway"),
        gateway_scheduler=None,
        operational_settings=settings,
        controller_state=ControllerState(automatic_price_optimization_enabled=True),
        battery=BatteryReading(
            usable_soc=50,
            charge_power_limit_w=10_000,
            discharge_power_limit_w=10_000,
        ),
        telemetry=SimpleNamespace(value=lambda family, key: 0),
        production_control_enabled=lambda: True,
        _automation_policy=policy,
        plant_health=lambda: PlantHealth(True, True, True, True, True, ()),
    )
    instance.scheduler_coordinator.manager = instance
    return instance, mutations


@pytest.mark.asyncio
async def test_controller_creates_owned_non_export_plan_when_export_is_disabled() -> None:
    instance, mutations = runtime(export_allowed=False)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    assert controller.status == "active"
    assert mutations.created
    assert all(request.owner == PRICE_OWNER for request in mutations.created)
    discharge = next(request for request in mutations.created if request.service == "peakshaving")
    assert discharge.values is not None
    assert discharge.values["exportlim"] == 0
    assert discharge.values["charge_max"] == 0


@pytest.mark.asyncio
async def test_controller_uses_negative_demand_response_only_with_export_permission() -> None:
    instance, mutations = runtime(export_allowed=True)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    export = next(
        request
        for request in mutations.created
        if request.service == "demandresponse"
        and request.values is not None
        and request.values["pacref"] < 0
    )
    assert export.owner == PRICE_OWNER


@pytest.mark.asyncio
async def test_controller_exposes_plan_without_writes_until_live_gate_is_enabled() -> None:
    instance, mutations = runtime(schedule_writes=False)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    assert controller.status == "preview"
    assert controller.plan is not None
    assert controller.plan.actions
    assert mutations.created == []


@pytest.mark.asyncio
async def test_controller_waits_when_no_future_price_remains() -> None:
    instance, mutations = runtime(schedule_writes=False)
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    instance.hass.states.state.attributes = {
        "unit_of_measurement": "SEK/kWh",
        "raw_today": [
            {
                "start": start.isoformat(),
                "end": (start + timedelta(hours=1)).isoformat(),
                "price": 0.5,
            }
        ],
        "raw_tomorrow": [],
    }
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.prepare()
    await controller.tick()

    assert controller.status == "waiting_prices"
    assert controller.reason == "future_price_horizon_empty"
    assert controller.price_source_format == "timestamped_day_arrays"
    assert controller.price_interval_count == 1
    assert controller.next_day_prices_available is False
    assert mutations.created == []


@pytest.mark.asyncio
async def test_controller_plans_remaining_today_before_tomorrow_is_published() -> None:
    instance, mutations = runtime()
    instance.hass.states.state.attributes = {
        "unit_of_measurement": "SEK/kWh",
        "raw_today": instance.hass.states.state.attributes["prices"],
        "raw_tomorrow": [],
    }
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    assert controller.price_source_format == "timestamped_day_arrays"
    assert controller.next_day_prices_available is False
    assert controller.status == "active"
    assert mutations.created
    assert controller.attributes()["next_day_prices_available"] is False


@pytest.mark.asyncio
async def test_new_day_prices_replace_obsolete_future_owned_schedule() -> None:
    from custom_components.pixiihome_controller.scheduler.ownership import (
        OwnershipLifecycle,
        SchedulerOwnershipRecord,
    )

    instance, mutations = runtime()
    stale_start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=5)
    instance.scheduler_ownership.records[1000000000099] = SchedulerOwnershipRecord(
        "entry", "gateway", 1000000000099, "peakshaving",
        {"starttime": stale_start.isoformat(), "duration": 1800,
         "values": {"exportlim": 0.0, "importlim": 0.0, "charge_max": 0.0,
                    "discharge_max": 5000.0, "keep_active": True}},
        lifecycle=OwnershipLifecycle.OWNED, owner=PRICE_OWNER,
    )
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    assert 1000000000099 in mutations.deleted


@pytest.mark.asyncio
@pytest.mark.parametrize("service", [101, 102, 107, 111, 121, 122, 123, 129])
async def test_foreign_services_block_all_price_mutations(service):
    instance, mutations = runtime()
    instance.telemetry.value = lambda family, key: service
    controller = PriceOptimizationController(instance)
    await controller.tick()
    assert controller.status == "waiting_for_priority"
    assert mutations.created == []
    assert mutations.deleted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [False, True])
async def test_only_verified_owned_background_aps_allows_future_price_schedules(verified):
    instance, mutations = runtime()
    instance.telemetry.value = lambda family, key: 105
    instance.solar_charging_controller = SimpleNamespace(
        owns_live_lease=lambda: True, _message_id=9001,
    )
    async def verify(message_id):
        assert message_id == 9001
        return {"successful": verified}
    instance.async_verify_aps_lease = verify
    controller = PriceOptimizationController(instance)
    await controller.tick()
    assert bool(mutations.created) is verified


@pytest.mark.asyncio
async def test_new_support_activation_mid_saga_stops_remaining_mutations():
    instance, mutations = runtime()
    create = mutations.create
    async def create_then_support(request):
        result = await create(request)
        instance.telemetry.value = lambda family, key: 102
        return result
    mutations.create = create_then_support
    controller = PriceOptimizationController(instance)
    await controller.tick()
    assert len(mutations.created) == 1
    assert controller.status == "waiting_for_priority"


@pytest.mark.asyncio
async def test_price_source_removed_mid_saga_blocks_remaining_mutations():
    instance, mutations = runtime()
    create = mutations.create
    state_get = instance.hass.states.get

    def get(entity_id):
        assert isinstance(entity_id, str)
        return state_get(entity_id)

    async def create_then_remove_source(request):
        result = await create(request)
        instance.entry.options[CONF_PRICE_OPTIMIZATION] = {}
        return result

    instance.hass.states.get = get
    mutations.create = create_then_remove_source
    controller = PriceOptimizationController(instance)
    await controller.tick()
    assert len(mutations.created) == 1
    assert mutations.deleted == []
    assert controller.status == "blocked"
    assert controller.reason == "price_mutation_context_changed"


@pytest.mark.asyncio
async def test_partial_inventory_never_authorizes_price_mutations():
    instance, mutations = runtime()
    async def partial():
        return SimpleNamespace(data_status="partial")
    instance.scheduler_coordinator.async_refresh = partial
    controller = PriceOptimizationController(instance)
    await controller.tick()
    assert controller.reason == "price_scheduler_inventory_incomplete"
    assert mutations.created == []


def external_schedule(start, duration=3600, recurring=False, service_no=300):
    from custom_components.pixiihome_controller.scheduler.models import (
        NormalizedRecurrence,
        NormalizedSchedule,
        ScheduleCompleteness,
        ScheduleConsistency,
        SchedulerOwnership,
    )
    return NormalizedSchedule(
        3, int(start.timestamp()), duration, int(start.timestamp()) + duration,
        service_no, 107, True, None, None, 7,
        NormalizedRecurrence(recurring, ()), SchedulerOwnership(),
        ScheduleCompleteness.COMPLETE, ScheduleConsistency.CONSISTENT,
    )


@pytest.mark.asyncio
async def test_external_action_removes_dependent_charge_and_discharge_from_real_plan():
    instance, mutations = runtime()
    items = instance.hass.states.state.attributes["prices"]
    start = datetime.fromisoformat(items[1]["start"])
    instance.scheduler_manager.normalized_schedules = [external_schedule(start)]
    controller = PriceOptimizationController(instance)
    await controller.tick()
    assert mutations.created == []
    assert controller.plan.actions == ()
    assert controller.external_conflict_count > 0


@pytest.mark.asyncio
async def test_recurring_scope_cannot_be_guessed_from_the_first_occurrence():
    instance, mutations = runtime()
    instance.scheduler_manager.normalized_schedules = [
        external_schedule(datetime.now(UTC) - timedelta(days=7), recurring=True)
    ]
    controller = PriceOptimizationController(instance)
    await controller.tick()
    assert controller.reason == "price_recurring_schedule_scope_unknown"
    assert mutations.created == []


def test_matching_native_number_and_shape_are_not_mqtt_ownership_evidence():
    from custom_components.pixiihome_controller.price_controller import _record_matches_schedule
    from custom_components.pixiihome_controller.scheduler.ownership import (
        OwnershipLifecycle,
        SchedulerOwnershipRecord,
    )
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=2)
    schedule = external_schedule(start, service_no=300)
    record = SchedulerOwnershipRecord(
        "entry", "gateway", 300, "targetsoc",
        {"starttime": start.isoformat(), "duration": 3600, "values": {"socref": 90}},
        lifecycle=OwnershipLifecycle.OWNED, owner=PRICE_OWNER,
    )
    assert not _record_matches_schedule(record, schedule)


@pytest.mark.asyncio
async def test_future_target_is_not_assumed_to_have_reached_its_soc_before_a_later_move():
    from dataclasses import replace

    from custom_components.pixiihome_controller.control.price_optimization import (
        normalize_price_series,
    )
    instance, _ = runtime()
    instance.entry.options[CONF_PRICE_OPTIMIZATION]["managed_source_id"] = 7
    instance.battery = replace(instance.battery, raw_soc=89, effective_min_soc=10,
                               effective_max_soc=90)
    items = instance.hass.states.state.attributes["prices"]
    first = external_schedule(datetime.fromisoformat(items[1]["start"]), service_no=300)
    second = replace(external_schedule(datetime.fromisoformat(items[2]["start"]),
                                       service_no=301), sch_index=4)
    instance.scheduler_manager.normalized_schedules = [first, second]
    reads = []
    async def read_target(service_no):
        reads.append(service_no)
        return {"reference": 10 if service_no == 300 else 90}
    instance.gateway_scheduler = SimpleNamespace(
        client=SimpleNamespace(read_target_soc=read_target),
    )
    controller = PriceOptimizationController(instance)
    series = normalize_price_series(instance.hass.states.state.attributes, "SEK/kWh")
    proposal = controller._build_plan(series, controller._validated_limits())
    # Without permission to change aggregator schedules nothing is even read.
    assert await controller._move_one_managed_external_charge(series, proposal.actions)
    assert reads == []
    instance.operational_settings = instance.operational_settings.set(
        "price_adjust_external_target_soc", True
    )
    assert await controller._move_one_managed_external_charge(series, proposal.actions)
    assert reads == [300]
    assert controller.external_move_count == 0


def _owned_price_record(m_id: int, start: datetime, duration: int = 1800):
    from custom_components.pixiihome_controller.scheduler.ownership import (
        OwnershipLifecycle,
        SchedulerOwnershipRecord,
    )
    return SchedulerOwnershipRecord(
        "entry", "gateway", m_id, "peakshaving",
        {"starttime": start.replace(microsecond=0).isoformat(), "duration": duration,
         "values": {"exportlim": 0.0, "importlim": 0.0, "charge_max": 0.0,
                    "discharge_max": 3000.0, "keep_active": True}},
        lifecycle=OwnershipLifecycle.OWNED, owner=PRICE_OWNER,
    )


@pytest.mark.asyncio
async def test_authority_release_deletes_future_and_stops_running_price_schedules():
    instance, mutations = runtime()
    now = datetime.now(UTC)
    records = instance.scheduler_ownership.records
    records[1000000000101] = _owned_price_record(1000000000101, now + timedelta(hours=3))
    records[1000000000102] = _owned_price_record(1000000000102, now - timedelta(minutes=5))
    records[1000000000103] = _owned_price_record(1000000000103, now - timedelta(hours=3))
    stopped = []

    async def stop_owned_price(m_id):
        stopped.append(m_id)
        return SimpleNamespace(status=SchedulerTransactionStatus.OUTPUT_STOPPED, reason="")

    mutations.stop_owned_price = stop_owned_price
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    problems = await controller.release_for_authority_off()

    assert problems == []
    assert mutations.deleted == [1000000000101]
    assert stopped == [1000000000102]
    assert controller.status == "disabled"


@pytest.mark.asyncio
async def test_authority_release_reports_failures_without_retrying():
    instance, mutations = runtime()
    records = instance.scheduler_ownership.records
    records[1000000000104] = _owned_price_record(
        1000000000104, datetime.now(UTC) + timedelta(hours=3)
    )

    async def failing_delete(m_id):
        mutations.deleted.append(m_id)
        return SimpleNamespace(status=SchedulerTransactionStatus.TIMED_OUT, reason="ack_timeout")

    mutations.delete = failing_delete
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    problems = await controller.release_for_authority_off()

    assert problems == ["price_1000000000104_ack_timeout"]
    assert mutations.deleted == [1000000000104]
    assert controller.status == "blocked"


@pytest.mark.asyncio
async def test_known_running_target_soc_allows_writes_only_after_its_end():
    from dataclasses import replace

    instance, mutations = runtime()
    now = datetime.now(UTC).replace(microsecond=0)
    running = external_schedule(now - timedelta(minutes=30), duration=3600)
    instance.scheduler_manager.normalized_schedules = [running]
    instance.telemetry.value = lambda family, key: 107
    instance.battery = replace(instance.battery, effective_min_soc=0, effective_max_soc=100)

    async def read_target(service_no):
        assert service_no == running.service_no
        return {"reference": 50}

    instance.gateway_scheduler = SimpleNamespace(
        client=SimpleNamespace(read_target_soc=read_target),
    )
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    end = datetime.fromtimestamp(running.end_epoch, UTC)
    assert controller.busy_until == end
    assert mutations.created
    assert all(request.starttime >= end for request in mutations.created)


@pytest.mark.asyncio
async def test_running_service_without_a_matching_schedule_still_blocks_writes():
    instance, mutations = runtime()
    later = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=6)
    instance.scheduler_manager.normalized_schedules = [external_schedule(later)]
    instance.telemetry.value = lambda family, key: 107
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    assert controller.status == "waiting_for_priority"
    assert mutations.created == []


def _aggregator_day(instance, *, adjust: bool):
    from dataclasses import replace

    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    prices = [(-1, 0.5), (0, 3.0), (1, 1.0), (2, 1.0), (3, 0.2), (4, 3.0)]
    instance.hass.states.state.attributes = {
        "unit_of_measurement": "SEK/kWh",
        "prices": [
            {"start": (start + timedelta(hours=h)).isoformat(),
             "end": (start + timedelta(hours=h + 1)).isoformat(), "price": p}
            for h, p in prices
        ],
    }
    window = external_schedule(start + timedelta(hours=1))
    instance.scheduler_manager.normalized_schedules = [window]
    instance.battery = replace(instance.battery, usable_soc=60,
                               effective_min_soc=0, effective_max_soc=100)
    instance.operational_settings = instance.operational_settings.set(
        "price_adjust_external_target_soc", adjust
    )
    calls = []

    async def read_target(service_no):
        return {"reference": 90}

    async def set_target(**kwargs):
        calls.append(kwargs)
        return {"status": "adjusted"}

    instance.gateway_scheduler = SimpleNamespace(
        client=SimpleNamespace(read_target_soc=read_target),
        set_external_target_soc=set_target,
    )
    return window, calls


@pytest.mark.asyncio
async def test_aggregator_target_between_discharge_and_recharge_is_lowered():
    instance, mutations = runtime()
    window, calls = _aggregator_day(instance, adjust=True)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    assert len(calls) == 1
    call = calls[0]
    assert call["sch_index"] == window.sch_index
    assert call["expected_reference"] == 90
    assert 10 <= call["new_reference"] < 90  # never below the price minimum SOC
    assert call["allow_delete"] is True
    assert controller.external_target_adjust_count == 1
    assert mutations.created


@pytest.mark.asyncio
async def test_whole_day_planner_also_handles_a_held_aggregator_target():
    instance, mutations = runtime()
    window, calls = _aggregator_day(instance, adjust=True)
    instance.energy_forecast = SimpleNamespace(
        net_load_kw=lambda start, end, high=False: 1.0,
        net_forecast=lambda start, end, high=False: (1.0, 0.0),
    )
    instance.operational_settings = instance.operational_settings.set(
        "price_whole_day_planner", True
    )
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    assert controller.planner_engine == "whole_day"
    assert len(calls) == 1
    assert calls[0]["sch_index"] == window.sch_index
    # The plan charges in the cheap hour the aggregator window covers.
    assert calls[0]["delete"] is True
    assert mutations.created


@pytest.mark.asyncio
@pytest.mark.parametrize("adjust", [True, False])
async def test_aggregator_target_in_the_best_hour_is_replaced_only_when_allowed(adjust):
    instance, mutations = runtime()
    window, calls = _aggregator_day(instance, adjust=adjust)
    start = datetime.fromtimestamp(window.start_epoch, UTC) - timedelta(hours=1)
    prices = [(-1, 0.5), (0, 1.0), (1, 4.0), (2, 1.0), (3, 0.2), (4, 1.0)]
    instance.hass.states.state.attributes = {
        "unit_of_measurement": "SEK/kWh",
        "prices": [
            {"start": (start + timedelta(hours=h)).isoformat(),
             "end": (start + timedelta(hours=h + 1)).isoformat(), "price": p}
            for h, p in prices
        ],
    }

    async def set_target(**kwargs):
        calls.append(kwargs)
        return {"status": "deleted" if kwargs["delete"] else "adjusted"}

    instance.gateway_scheduler.set_external_target_soc = set_target
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    window_start = datetime.fromtimestamp(window.start_epoch, UTC)
    if adjust:
        assert len(calls) == 1 and calls[0]["delete"] is True
        assert controller.external_target_delete_count == 1
        assert any(request.starttime == window_start for request in mutations.created)
    else:
        assert calls == []
        assert all(request.starttime != window_start for request in mutations.created)


@pytest.mark.asyncio
async def test_aggregator_targets_are_untouched_when_the_switch_is_off():
    instance, _ = runtime()
    _, calls = _aggregator_day(instance, adjust=False)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    assert calls == []


@pytest.mark.asyncio
async def test_unheld_aggregator_target_stops_the_plan_at_that_window():
    instance, mutations = runtime()
    window, calls = _aggregator_day(instance, adjust=True)

    async def refused(**kwargs):
        calls.append(kwargs)
        return {"status": "unchanged"}

    instance.gateway_scheduler.set_external_target_soc = refused
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    assert calls
    start = datetime.fromtimestamp(window.start_epoch, UTC)
    assert all(request.starttime + timedelta(seconds=request.duration_seconds) <= start
               for request in mutations.created)


@pytest.mark.asyncio
async def test_lowered_target_is_restored_when_no_longer_held():
    instance, _ = runtime()
    window, calls = _aggregator_day(instance, adjust=True)
    instance.battery = instance.battery.__class__(
        **{**{f: getattr(instance.battery, f) for f in instance.battery.__dataclass_fields__},
           "usable_soc": 10}
    )

    async def read_target(service_no):
        return {"reference": 40}

    instance.gateway_scheduler.client.read_target_soc = read_target
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    key = (window.sch_index, window.service_no, window.start_epoch, window.duration_seconds)
    controller._lowered[key] = 90.0

    await controller.tick()

    assert calls and calls[-1]["new_reference"] == 90.0
    assert calls[-1]["allow_delete"] is False


def test_owned_record_matches_its_unique_gateway_entry_only():
    from custom_components.pixiihome_controller.price_controller import _record_matches_schedule
    from custom_components.pixiihome_controller.scheduler.models import (
        Ownership,
        ScheduleCompleteness,
        ScheduleConsistency,
        SchedulerOwnership,
    )
    from custom_components.pixiihome_controller.scheduler.ownership import OwnershipLifecycle

    start = datetime(2026, 10, 21, 21, 0, tzinfo=UTC)
    record = SimpleNamespace(
        m_id=1000000000042,
        service="peakshaving",
        lifecycle=OwnershipLifecycle.OWNED,
        intended_state={"starttime": "2026-10-21T21:00:00Z", "duration": 2700},
    )

    def entry(index, ownership=Ownership.UNKNOWN):
        epoch = int(start.timestamp())
        return SimpleNamespace(
            sch_index=index, service_no=index, start_epoch=epoch, end_epoch=epoch + 2700,
            duration_seconds=2700, service_type=104, enabled=True,
            recurrence=SimpleNamespace(recurring=False),
            completeness=ScheduleCompleteness.COMPLETE,
            consistency=ScheduleConsistency.CONSISTENT,
            ownership=SchedulerOwnership(ownership),
        )

    ours = entry(1)
    assert _record_matches_schedule(record, ours, [ours])
    twin = entry(2)
    assert not _record_matches_schedule(record, ours, [ours, twin])
    foreign = entry(3, Ownership.EXTERNAL)
    assert not _record_matches_schedule(record, foreign, [foreign])
    provisional = SimpleNamespace(**{**vars(record), "lifecycle": OwnershipLifecycle.PROVISIONAL})
    assert not _record_matches_schedule(provisional, ours, [ours])
