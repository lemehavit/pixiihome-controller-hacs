"""Automatic price-controller scheduling tests."""

from __future__ import annotations

from dataclasses import replace
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
from custom_components.pixiihome_controller.control.smart_solar import SmartSolarDecision
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
        now = int(datetime.now(UTC).timestamp())
        return SimpleNamespace(
            data_status="current", schedules=schedules, evaluated_at_epoch=now,
            window_start_epoch=now - 3600, window_end_epoch=now + 48 * 3600,
        )

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
@pytest.mark.parametrize(
    ("second_read", "expected_target", "expected_log"),
    [
        (20, 20.0, "changed from 90 % to 20 %"),
        (90, 90.0, None),
        (OSError("Gateway unavailable"), None, "unreadable (OSError: Gateway unavailable)"),
        (float("nan"), None, "unreadable (non-finite reference)"),
        (float("inf"), None, "unreadable (non-finite reference)"),
    ],
)
async def test_external_target_is_read_again_for_the_same_schedule(
    monkeypatch, second_read, expected_target, expected_log
):
    from dataclasses import replace

    import custom_components.pixiihome_controller.price_controller as module

    instance, _ = runtime()
    instance.battery = replace(instance.battery, effective_min_soc=0, effective_max_soc=100)
    schedule = external_schedule(datetime.now(UTC) + timedelta(hours=2))
    instance.scheduler_manager.normalized_schedules = [schedule]
    reads = []
    responses = iter((90, second_read))

    async def read_target(service_no):
        reads.append(service_no)
        value = next(responses)
        if isinstance(value, Exception):
            raise value
        return {"reference": value}

    instance.gateway_scheduler = SimpleNamespace(
        client=SimpleNamespace(read_target_soc=read_target)
    )
    logged = []
    monkeypatch.setattr(
        module, "log_activity",
        lambda _runtime, _key, message, cause=None: logged.append((message, cause)),
    )
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller._load_external_windows(())
    assert controller._external_windows[0].target_usable_soc_percent == 90.0
    assert controller._external_meta[0][1] == 90.0

    await controller._load_external_windows(())

    assert reads == [schedule.service_no, schedule.service_no]
    window = controller._external_windows[0]
    assert window.target_usable_soc_percent == expected_target
    assert window.known is (expected_target is not None)
    assert controller._external_meta[0][1] == expected_target
    if expected_log is None:
        assert logged == []
    else:
        assert len(logged) == 1
        assert expected_log in logged[0][0]
        assert logged[0][1] is not None and "Why:" in logged[0][1]


@pytest.mark.asyncio
async def test_next_planning_pass_uses_changed_target_for_same_schedule():
    instance, _ = runtime(schedule_writes=False)
    schedule, _ = _aggregator_day(instance, adjust=False)
    reads = []
    targets = iter((90, 20))

    async def read_target(service_no):
        reads.append(service_no)
        return {"reference": next(targets)}

    instance.gateway_scheduler.client.read_target_soc = read_target
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()
    assert controller.status == "preview"
    assert controller._external_windows[0].target_usable_soc_percent == 90.0
    first_fingerprint = controller.plan.fingerprint

    await controller.tick()

    assert controller.status == "preview"
    assert reads == [schedule.service_no, schedule.service_no]
    assert controller._external_windows[0].target_usable_soc_percent == 20.0
    assert controller.plan.fingerprint != first_fingerprint


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

    # Three hours ahead: the first price hour must stay beyond the minimum
    # lead time even when the test runs just before a full hour.
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=3)
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
async def test_whole_day_attributes_follow_filtered_discharge() -> None:
    instance, mutations = runtime(schedule_writes=False)
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    instance.hass.states.state.attributes = {
        "unit_of_measurement": "SEK/kWh",
        "prices": [
            {
                "start": (start + timedelta(hours=index)).isoformat(),
                "end": (start + timedelta(hours=index + 1)).isoformat(),
                "price": price,
            }
            for index, price in enumerate((1.1, 1.1, 0.8, 0.8))
        ],
    }
    instance.battery = replace(instance.battery, usable_soc=80)
    instance.energy_forecast = SimpleNamespace(
        net_load_kw=lambda start, end, high=False: 1.0,
        net_forecast=lambda start, end, high=False: (1.0, 0.0),
    )
    instance.operational_settings = instance.operational_settings.set(
        "price_whole_day_planner", True
    )
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    attributes = controller.attributes()
    whole_day = attributes["whole_day_plan"]
    assert attributes["planner_engine"] == "whole_day"
    assert whole_day["action_count"] == 0
    assert whole_day["segments"] == []
    assert attributes["minimum_projected_usable_soc"] == pytest.approx(80.0)
    assert attributes["maximum_projected_usable_soc"] == pytest.approx(80.0)
    assert whole_day["cost_without_battery_sek"] == pytest.approx(3.8)
    assert whole_day["planned_cost_sek"] == pytest.approx(3.8)
    assert whole_day["expected_margin_sek"] == pytest.approx(0.0)
    assert mutations.created == []


def _sunny_flat_day(instance, *, solar_charging: bool) -> None:
    """Flat prices, 3 kW sun and no load: only solar charging may fill the battery."""
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    instance.hass.states.state.attributes = {
        "unit_of_measurement": "SEK/kWh",
        "prices": [
            {
                "start": (start + timedelta(hours=index)).isoformat(),
                "end": (start + timedelta(hours=index + 1)).isoformat(),
                "price": 1.0,
            }
            for index in range(4)
        ],
    }
    instance.energy_forecast = SimpleNamespace(
        net_load_kw=lambda start, end, high=False: -3.0,
        net_forecast=lambda start, end, high=False: (0.0, 3.0),
    )
    instance.controller_state = ControllerState(
        automatic_price_optimization_enabled=True,
        automatic_solar_charging_enabled=solar_charging,
    )
    settings = instance.operational_settings.set("price_whole_day_planner", True)
    settings = settings.set("price_grid_import_cost_sek_per_kwh", 0.5)
    instance.operational_settings = settings.set("solar_max_soc_percent", 80.0)


@pytest.mark.asyncio
async def test_whole_day_plan_counts_no_solar_charging_when_it_is_switched_off(
    monkeypatch,
) -> None:
    instance, mutations = runtime(schedule_writes=False)
    _sunny_flat_day(instance, solar_charging=False)
    import custom_components.pixiihome_controller.price_controller as module

    logged: list[str] = []
    monkeypatch.setattr(
        module, "log_activity", lambda _runtime, _key, message, *_args: logged.append(message)
    )
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    attributes = controller.attributes()
    assert attributes["planner_engine"] == "whole_day"
    assert attributes["whole_day_plan"]["segments"] == []
    assert attributes["whole_day_plan"]["action_count"] == 0
    assert attributes["minimum_projected_usable_soc"] == pytest.approx(50.0)
    assert attributes["maximum_projected_usable_soc"] == pytest.approx(50.0)
    assert "Whole-day plan counts on solar charging not at all" in logged
    assert mutations.created == []


@pytest.mark.asyncio
async def test_whole_day_plan_counts_no_solar_charging_while_adaptive_peak_shaving_is_on(
    monkeypatch,
) -> None:
    instance, mutations = runtime(schedule_writes=False)
    _sunny_flat_day(instance, solar_charging=True)
    # The runtime lets peak shaving be switched on while solar charging is on.
    instance.controller_state = replace(
        instance.controller_state, adaptive_peak_shaving_enabled=True
    )
    import custom_components.pixiihome_controller.price_controller as module

    logged: list[str] = []
    monkeypatch.setattr(
        module, "log_activity", lambda _runtime, _key, message, *_args: logged.append(message)
    )
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    attributes = controller.attributes()
    assert attributes["planner_engine"] == "whole_day"
    assert attributes["whole_day_plan"]["segments"] == []
    assert attributes["maximum_projected_usable_soc"] == pytest.approx(50.0)
    assert "Whole-day plan counts on solar charging not at all" in logged
    assert mutations.created == []


@pytest.mark.parametrize("hold", ["calibration", "production_control"])
def test_solar_limit_is_off_while_the_solar_controller_is_held(hold: str) -> None:
    # The price controller itself stops on these holds, so no tick builds a plan;
    # the limit still mirrors the solar controller for any caller.
    instance, _mutations = runtime(schedule_writes=False)
    _sunny_flat_day(instance, solar_charging=True)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    assert controller._solar_charging(instance._automation_policy()).enabled is True
    if hold == "calibration":
        instance.battery_calibration_active = lambda: True
    else:
        instance.production_control_enabled = lambda: False

    assert controller._solar_charging(instance._automation_policy()).enabled is False


@pytest.mark.asyncio
async def test_whole_day_plan_counts_no_solar_charging_below_the_start_threshold() -> None:
    instance, mutations = runtime(schedule_writes=False)
    _sunny_flat_day(instance, solar_charging=True)
    # 0.5 kW surplus, below the default 1.0 kW start threshold.
    instance.energy_forecast = SimpleNamespace(
        net_load_kw=lambda start, end, high=False: -0.5,
        net_forecast=lambda start, end, high=False: (0.0, 0.5),
    )
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    attributes = controller.attributes()
    assert attributes["planner_engine"] == "whole_day"
    assert attributes["whole_day_plan"]["segments"] == []
    assert attributes["maximum_projected_usable_soc"] == pytest.approx(50.0)
    assert mutations.created == []


@pytest.mark.asyncio
async def test_whole_day_solar_segment_shows_automatic_solar_charging(monkeypatch) -> None:
    instance, mutations = runtime(schedule_writes=False)
    _sunny_flat_day(instance, solar_charging=True)
    import custom_components.pixiihome_controller.price_controller as module

    logged: list[str] = []
    monkeypatch.setattr(
        module, "log_activity", lambda _runtime, _key, message, *_args: logged.append(message)
    )
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()
    await controller.tick()

    attributes = controller.attributes()
    segments = attributes["whole_day_plan"]["segments"]
    assert attributes["planner_engine"] == "whole_day"
    assert attributes["maximum_projected_usable_soc"] == pytest.approx(80.0)
    assert segments and all(
        segment["kind"] == "solar"
        and segment["performed_by"] == "automatic_solar_charging"
        for segment in segments
    )
    assert segments[-1]["end_soc"] == pytest.approx(80.0)
    # Logged once, when the limit was first set, not on every pass.
    assert logged.count("Whole-day plan counts on solar charging up to its limits") == 1
    assert mutations.created == []


@pytest.mark.asyncio
async def test_whole_day_plan_follows_smart_solar_deferral() -> None:
    instance, mutations = runtime(schedule_writes=False)
    _sunny_flat_day(instance, solar_charging=True)
    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=2)
    instance.hass.states.state.attributes["prices"] = [
        {
            "start": (start + timedelta(hours=index)).isoformat(),
            "end": (start + timedelta(hours=index + 1)).isoformat(),
            "price": price,
        }
        for index, price in enumerate((1.5, 1.5, 1.1, 1.1))
    ]
    # 1 kW solar charging: the two cheap hours alone cannot reach the 80 % cap.
    # No losses, so whole energy steps match the 1 kW limit exactly.
    settings = instance.operational_settings.set("solar_max_charge_power_kw", 1.0)
    settings = settings.set("price_round_trip_efficiency_percent", 100.0)
    # A minimum profit no written charge reaches here: only the sun charges.
    instance.operational_settings = settings.set("price_min_difference_sek_per_kwh", 1.0)
    # Charging later at 1.0 plus the 0.2 minimum advantage: export at 1.2 or more.
    instance.solar_charging_controller = SimpleNamespace(
        _smart_deferring=True,
        smart_decision=SmartSolarDecision(
            True, "export_now_charge_later", charge_price=1.0,
            deadline=start + timedelta(hours=4),
        ),
    )
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    assert controller.whole_day is not None
    slots = controller.whole_day.slots
    assert [slot.performed_by for slot in slots[2:]] == ["automatic_solar_charging"] * 2
    assert all(slot.battery_kw == pytest.approx(0.0) for slot in slots[:2])
    segments = controller.attributes()["whole_day_plan"]["segments"]
    assert [segment["performed_by"] for segment in segments] == ["automatic_solar_charging"]
    assert segments[0]["start"] == slots[2].start.isoformat()
    assert mutations.created == []


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
async def test_refused_target_is_kept_and_planned_around_with_its_reason(caplog):
    from homeassistant.exceptions import HomeAssistantError

    instance, _ = runtime()
    window, calls = _aggregator_day(instance, adjust=True)
    logged: list[str] = []

    async def refused(**kwargs):
        calls.append(kwargs)
        raise HomeAssistantError("scheduler_installer_access_required")

    instance.gateway_scheduler.set_external_target_soc = refused
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    import custom_components.pixiihome_controller.price_controller as module

    original = module.log_activity
    module.log_activity = lambda _runtime, _key, message, *_args: logged.append(message)
    try:
        await controller.tick()
        attempts = len(calls)
        await controller.tick()
    finally:
        module.log_activity = original

    key = (window.sch_index, window.service_no, window.start_epoch, window.duration_seconds)
    assert controller._refused_targets[key] == "scheduler_installer_access_required"
    # Planned as fixed from then on: never tried again, logged once with why.
    assert attempts >= 1 and len(calls) == attempts
    refusals = [m for m in logged if "cannot be changed" in m]
    assert len(refusals) == 1 and "scheduler_installer_access_required" in refusals[0]
    assert "could not be changed: scheduler_installer_access_required" in caplog.text


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


def test_a_blocked_write_names_its_safety_checks_once(monkeypatch):
    import custom_components.pixiihome_controller.price_controller as module

    logged = []
    monkeypatch.setattr(module, "log_activity",
                        lambda _runtime, _key, message, *_args: logged.append(message))
    controller = SimpleNamespace(runtime=None, blocked_details=[], _reported_block=())
    blocked = SimpleNamespace(blocked_reasons=("scheduler_visibility_stale",))

    for _ in range(3):
        PriceOptimizationController._report_blocked(controller, "create 17:00", blocked)  # type: ignore[arg-type]

    assert logged == ["Could not create 17:00: blocked by scheduler_visibility_stale"]
    assert controller.blocked_details == ["scheduler_visibility_stale"]


@pytest.mark.asyncio
@pytest.mark.parametrize("remembered", [True, False])
async def test_a_target_we_lowered_is_raised_back_after_a_restart(remembered):
    from custom_components.pixiihome_controller.price_controller import _target_key

    instance, _ = runtime()
    window, calls = _aggregator_day(instance, adjust=True)

    async def read_target(service_no):
        return {"reference": 5}

    instance.gateway_scheduler.client.read_target_soc = read_target
    async def save(lowered):
        return None

    instance.async_save_lowered_targets = save
    instance.lowered_external_targets = {_target_key(window): 90.0} if remembered else {}
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    raised = [call for call in calls if call["new_reference"] > 5 and not call["delete"]]
    if remembered:
        # The plan keeps more than 5 %: raised towards, never above, 90 %.
        assert raised and all(call["new_reference"] <= 90 for call in raised)
        assert controller.external_target_adjust_count == 1
    else:
        # A target this integration never lowered is never raised.
        assert raised == []


@pytest.mark.asyncio
async def test_a_lowered_target_is_remembered_with_the_aggregator_value():
    from custom_components.pixiihome_controller.price_controller import _target_key

    instance, _ = runtime()
    window, calls = _aggregator_day(instance, adjust=True)
    saved = []

    async def save(lowered):
        saved.append(lowered)

    instance.async_save_lowered_targets = save
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    assert len(calls) == 1 and calls[0]["new_reference"] < 90
    assert saved[-1] == {_target_key(window): 90}


def test_saved_lowered_targets_skip_ended_and_malformed_rows():
    from time import time

    from custom_components.pixiihome_controller.runtime import _lowered_targets_from

    future = int(time()) + 3600
    rows = [[4596, 3, future, 3600, 90], [1, 2, 100, 60, 80], ["x", 2, future, 60, 80], 5]
    assert _lowered_targets_from(rows) == {(4596, 3, future, 3600): 90.0}
    assert _lowered_targets_from(None) == {}


@pytest.mark.asyncio
async def test_a_passing_safety_refusal_is_tried_again_next_pass():
    # 23 Oct: an unconfirmed removal briefly made the ownership store invalid;
    # that refusal was kept until the window ended and the evening peak
    # discharge was never written again.
    from homeassistant.exceptions import HomeAssistantError

    instance, _ = runtime()
    _, calls = _aggregator_day(instance, adjust=True)
    answers = ["refuse", "adjust"]

    async def setter(**kwargs):
        calls.append(kwargs)
        if answers.pop(0) == "refuse":
            raise HomeAssistantError(
                "scheduler_ownership_store_invalid, scheduler_ownership_ambiguous"
            )
        return {"status": "adjusted"}

    instance.gateway_scheduler.set_external_target_soc = setter
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()
    first = len(calls)
    await controller.tick()

    assert first == 1 and len(calls) == 2
    assert controller.external_target_adjust_count == 1


@pytest.mark.parametrize("cheap_later", [True, False])
def test_without_an_own_recharge_targets_are_held_up_to_the_cheapest_refill(cheap_later):
    # 29 Oct: after the 17-18 peak the aggregator refilled at 20-21 (4.36 kr)
    # and again at 22-23 (3.58). Only 18-19 was held, so the export was
    # valued against 4.36 and dropped. Hold up to the cheapest refill instead.
    from custom_components.pixiihome_controller.control.price_optimization import (
        ExternalWindow,
        PriceDirection,
        PriceInterval,
        PricePlan,
        PricePlanAction,
        PriceSeries,
    )

    instance, _ = runtime()
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    base = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(hours=3)
    prices = [7.1, 6.1, 5.5, 4.36, 3.9, 3.58 if cheap_later else 4.9]
    series = PriceSeries(tuple(
        PriceInterval(base + timedelta(hours=h), base + timedelta(hours=h + 1), p)
        for h, p in enumerate(prices)
    ), "SEK/kWh", "prices", "fingerprint")
    windows = tuple(
        ExternalWindow(base + timedelta(hours=h), base + timedelta(hours=h + 1), 100.0)
        for h in (1, 3, 5)
    )
    controller._external_windows = windows
    controller._external_meta = tuple(  # type: ignore[assignment]
        (external_schedule(w.start, service_no=300 + i), 90.0) for i, w in enumerate(windows)
    )
    discharge = PricePlanAction(
        base, base + timedelta(minutes=75), PriceDirection.DISCHARGE, 10.0, "demandresponse",
        {"pacref": -10000.0}, None, 7.1, 4.0, 3.1, 50.0, 10.0, 1.0,
    )
    controller._build_plan = lambda *a, **k: PricePlan(  # type: ignore[method-assign]
        (discharge,), (), "fp", 50.0, 100.0, 10.0,
    )

    held = controller._with_adjustable_targets(series, {})

    assert [w.adjustable for w in held] == ([True, True, False] if cheap_later
                                           else [True, False, False])


class _Clock:
    """Simulated clock for the price controller module."""

    def __init__(self, monkeypatch) -> None:
        from custom_components.pixiihome_controller import price_controller as module

        self.now = datetime.now(UTC)
        clock = self

        class FakeDatetime(datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return clock.now if tz is None else clock.now.astimezone(tz)

        monkeypatch.setattr(module, "datetime", FakeDatetime)

    def advance(self, minutes: int) -> None:
        self.now += timedelta(minutes=minutes)


def _blocked_notice_runtime(monkeypatch, hours: int = 1):
    from homeassistant.components import persistent_notification

    instance, mutations = runtime()
    instance.serial = "234301002683"
    instance.operational_settings = instance.operational_settings.set(
        "price_blocked_notify_hours", hours
    )
    events: list[dict] = []
    instance.hass.bus = SimpleNamespace(
        async_fire=lambda event, data, context=None: events.append(
            {**data, "_event": event, "_context": context}
        )
    )
    created: list[str] = []
    dismissed: list[str] = []
    monkeypatch.setattr(
        persistent_notification, "async_create",
        lambda hass, message, **kwargs: created.append(message),
    )
    monkeypatch.setattr(
        persistent_notification, "async_dismiss",
        lambda hass, notification_id: dismissed.append(notification_id),
    )
    normal_refresh = instance.scheduler_coordinator.async_refresh

    async def partial():
        return SimpleNamespace(data_status="partial")

    def block(on: bool) -> None:
        instance.scheduler_coordinator.async_refresh = partial if on else normal_refresh

    return instance, mutations, events, created, dismissed, block


def _messages(events: list[dict]) -> list[str]:
    return [event["message"] for event in events if "name" in event]


@pytest.mark.asyncio
async def test_late_block_over_several_passes_notifies_once_and_logs_once(
    monkeypatch, caplog,
) -> None:
    clock = _Clock(monkeypatch)
    instance, _, events, created, dismissed, block = _blocked_notice_runtime(monkeypatch)
    block(True)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()
    first = controller.problem_since
    assert first is not None
    for _ in range(3):
        clock.advance(30)
        await controller.tick()
        assert controller.status == "blocked"
        assert controller.reason == "price_scheduler_inventory_incomplete"
        assert controller.problem_since == first

    assert controller.problem_notified is True
    assert len(created) == 1
    assert caplog.text.count("Price optimization blocked") == 1
    stopped = [m for m in _messages(events) if "has been stopped for over 1 h" in m]
    assert len(stopped) == 1
    stopped_events = [
        event for event in events
        if "has been stopped for over 1 h" in event.get("message", "")
    ]
    assert len(stopped_events) == 1
    stopped_index = events.index(stopped_events[0])
    cause = events[stopped_index - 1]
    assert cause["_event"] == "pixiihome_controller_cause"
    assert "every planning pass has been blocked" in cause["message"]
    assert cause["_context"] is stopped_events[0]["_context"]

    # Recovery: a pass that ends active/ready resets the timer.
    block(False)
    clock.advance(30)
    await controller.tick()
    assert controller.status in {"active", "ready"}
    assert controller.problem_since is None
    assert controller.problem_notified is False
    assert dismissed == ["pixiihome_controller_234301002683_price_blocked"]
    assert "Price optimization is running again" in _messages(events)


@pytest.mark.asyncio
async def test_block_shorter_than_limit_never_notifies(monkeypatch) -> None:
    clock = _Clock(monkeypatch)
    instance, _, events, created, dismissed, block = _blocked_notice_runtime(monkeypatch)
    block(True)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()
    clock.advance(30)
    await controller.tick()
    block(False)
    clock.advance(30)
    await controller.tick()

    assert controller.problem_since is None
    assert created == []
    assert dismissed == []
    assert "Price optimization is running again" not in _messages(events)


@pytest.mark.asyncio
async def test_prepare_publishes_ready_only_on_the_switch_path() -> None:
    instance, _ = runtime()
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    seen: list[str] = []
    controller.add_listener(lambda: seen.append(controller.status))

    await controller.prepare(publish=False)
    assert controller.status == "disabled"
    assert seen == []

    await controller.prepare()
    assert controller.status == "ready"
    assert seen == ["ready"]

    instance.production_control_enabled = lambda: False
    with pytest.raises(ValueError, match="production_control_disabled"):
        await controller.prepare(publish=False)
    with pytest.raises(ValueError, match="production_control_disabled"):
        await controller.prepare()


@pytest.mark.asyncio
async def test_validation_error_inside_pass_is_still_published() -> None:
    instance, _ = runtime()
    instance.production_control_enabled = lambda: False
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]

    await controller.tick()

    assert controller.status == "blocked"
    assert controller.reason == "production_control_disabled"
    assert controller.problem_since is not None


@pytest.mark.asyncio
async def test_failing_pass_keeps_the_problem_timer_running(monkeypatch) -> None:
    clock = _Clock(monkeypatch)
    instance, _, _, created, _, block = _blocked_notice_runtime(monkeypatch)
    block(True)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    await controller.tick()
    first = controller.problem_since
    assert first is not None

    async def fail() -> None:
        controller._publish("ready")  # a passing status inside the failing pass
        raise RuntimeError("boom")

    monkeypatch.setattr(controller, "_tick_locked", fail)
    for _ in range(3):
        clock.advance(30)
        with pytest.raises(RuntimeError):
            await controller.tick()
        assert controller._in_tick is False
        assert not controller._lock.locked()
        assert controller.problem_since == first
        # What ``_run`` publishes after the exception.
        controller._publish("blocked", "price_controller_failed")
        assert controller.problem_since == first

    assert len(created) == 1


@pytest.mark.asyncio
async def test_planning_pass_never_sends_a_passing_ready_to_listeners(monkeypatch) -> None:
    instance, _, _, _, _, block = _blocked_notice_runtime(monkeypatch)
    block(True)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    seen: list[str] = []
    controller.add_listener(lambda: seen.append(controller.status))

    await controller.tick()
    await controller.tick()

    assert seen
    assert "ready" not in seen


# CAP-F4: an own schedule about to start is binding, not deleted.


def _binding_runtime(monkeypatch):
    """Runtime whose creates become OWNED records, with a simulated clock."""
    from custom_components.pixiihome_controller.scheduler.ownership import (
        OwnershipLifecycle,
        SchedulerOwnershipRecord,
    )

    clock = _Clock(monkeypatch)
    instance, mutations = runtime()
    events: list[dict] = []
    instance.hass.bus = SimpleNamespace(
        async_fire=lambda event, data, context=None: events.append(data)
    )
    ids = iter(range(1000000000500, 1000000000600))

    async def create(request):
        mutations.created.append(request)
        m_id = next(ids)
        instance.scheduler_ownership.records[m_id] = SchedulerOwnershipRecord(
            "entry", "gateway", m_id, request.service,
            {"starttime": request.starttime.astimezone(UTC).isoformat(),
             "duration": request.duration_seconds, "values": dict(request.values)},
            lifecycle=OwnershipLifecycle.OWNED, owner=PRICE_OWNER,
        )
        return SimpleNamespace(status=SchedulerTransactionStatus.SUCCESS, reason="")

    async def delete(m_id):
        mutations.deleted.append(m_id)
        instance.scheduler_ownership.records.pop(m_id, None)
        return SimpleNamespace(status=SchedulerTransactionStatus.SUCCESS, reason="")

    mutations.create = create
    mutations.delete = delete
    charge_start = datetime.fromisoformat(
        instance.hass.states.state.attributes["prices"][0]["start"]
    )
    return clock, instance, mutations, events, charge_start


def test_standard_plan_logs_forecast_charge_limit_without_gateway_calls(monkeypatch):
    from custom_components.pixiihome_controller import price_controller as module
    from custom_components.pixiihome_controller.control.price_optimization import (
        normalize_price_series,
    )

    _, instance, mutations, _, _ = _binding_runtime(monkeypatch)
    instance.energy_forecast = SimpleNamespace(
        net_load_kw=lambda _start, _end, high=False: 0.0,
        net_forecast=lambda _start, _end, high=False: (0.0, 0.0),
    )
    logged = []
    monkeypatch.setattr(module, "log_activity", lambda *_args: logged.append(_args[2:]))
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    series = normalize_price_series(instance.hass.states.state.attributes, "SEK/kWh")

    result = controller._build_plan(series, controller._validated_limits())
    controller._build_plan(series, controller._validated_limits())

    assert result.charge_demand_limited
    cap_entries = [
        (message, reason) for message, reason in logged
        if "forecast profitable use" in message
    ]
    assert len(cap_entries) == 1
    assert "Energy already stored" in cap_entries[0][1]
    assert mutations.created == []
    assert mutations.deleted == []


def _owned(m_id: int, service: str, start: datetime, values: dict):
    from custom_components.pixiihome_controller.scheduler.ownership import (
        OwnershipLifecycle,
        SchedulerOwnershipRecord,
    )

    return SchedulerOwnershipRecord(
        "entry", "gateway", m_id, service,
        {"starttime": start.isoformat(), "duration": 3600, "values": values},
        lifecycle=OwnershipLifecycle.OWNED, owner=PRICE_OWNER,
    )


def _charge_id(instance, start):
    from custom_components.pixiihome_controller.price_controller import _record_start

    return next(
        m_id for m_id, record in instance.scheduler_ownership.records.items()
        if record.service == "demandresponse" and _record_start(record) == start
    )


def _flatten_prices(instance) -> None:
    for item in instance.hass.states.state.attributes["prices"]:
        item["price"] = 1.0


def _kept(events: list[dict]) -> list[str]:
    return [m for m in _messages(events) if m.startswith("Kept ")]


@pytest.mark.asyncio
async def test_own_charge_just_before_start_is_kept_and_planned_around(monkeypatch):
    clock, instance, mutations, _, start = _binding_runtime(monkeypatch)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    await controller.tick()
    charge = _charge_id(instance, start)
    created = len(mutations.created)

    clock.now = start - timedelta(minutes=3)
    await controller.tick()

    assert charge not in mutations.deleted
    assert charge in instance.scheduler_ownership.records
    window_end = start + timedelta(hours=1)
    assert controller.plan is not None and controller.plan.ready
    assert all(a.end <= start or a.start >= window_end for a in controller.plan.actions)
    assert all(
        r.starttime >= window_end
        or r.starttime + timedelta(seconds=r.duration_seconds) <= start
        for r in mutations.created[created:]
    )


@pytest.mark.asyncio
async def test_unprofitable_charge_just_before_start_is_kept_and_logged_once(monkeypatch):
    clock, instance, mutations, events, start = _binding_runtime(monkeypatch)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    await controller.tick()
    charge = _charge_id(instance, start)

    clock.now = start - timedelta(minutes=3)
    _flatten_prices(instance)
    await controller.tick()
    clock.advance(1)
    await controller.tick()

    assert charge not in mutations.deleted
    assert charge in instance.scheduler_ownership.records
    assert len(_kept(events)) == 1


@pytest.mark.asyncio
async def test_unneeded_charge_long_before_start_is_still_removed(monkeypatch):
    clock, instance, mutations, events, start = _binding_runtime(monkeypatch)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    await controller.tick()
    charge = _charge_id(instance, start)

    clock.now = start - timedelta(minutes=30)
    _flatten_prices(instance)
    await controller.tick()

    assert charge in mutations.deleted
    assert _kept(events) == []


@pytest.mark.asyncio
async def test_authority_off_just_before_start_removes_the_binding_charge(monkeypatch):
    clock, instance, mutations, _, start = _binding_runtime(monkeypatch)

    async def stop_owned_price(m_id):
        return SimpleNamespace(status=SchedulerTransactionStatus.OUTPUT_STOPPED, reason="")

    mutations.stop_owned_price = stop_owned_price
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    await controller.tick()
    charge = _charge_id(instance, start)
    clock.now = start - timedelta(minutes=3)
    await controller.tick()
    assert charge not in mutations.deleted

    problems = await controller.release_for_authority_off()

    assert problems == []
    assert charge in mutations.deleted


@pytest.mark.asyncio
async def test_binding_window_carries_its_energy_into_the_plan(monkeypatch):
    from custom_components.pixiihome_controller.control.price_optimization import (
        PriceDirection,
        normalize_price_series,
    )

    clock, instance, _, _, start = _binding_runtime(monkeypatch)
    clock.now = start - timedelta(minutes=3)
    instance.scheduler_ownership.records[1000000000700] = _owned(
        1000000000700, "demandresponse", start, {"pacref": 4000.0}
    )
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    series = normalize_price_series(instance.hass.states.state.attributes, "SEK/kWh")
    limits = controller._validated_limits()

    plan = controller._build_plan(series, limits)
    windows = controller._binding_windows(
        {"now": clock.now, "minimum_lead_minutes": 5, "usable_capacity_kwh": 10.0,
         "minimum_soc_percent": 10.0, "maximum_soc_percent": 90.0,
         "usable_soc_percent": 50.0, "maximum_charge_kw": 10.0,
         "maximum_discharge_kw": 10.0},
        limits, None,
    )

    assert controller._binding_ids == {1000000000700}
    assert len(windows) == 1
    # 50 % + 4 kW * 1 h * sqrt(0.9) of 10 kWh.
    assert windows[0].target_usable_soc_percent == pytest.approx(50 + 4 * 0.9**0.5 * 10)
    assert plan.ready
    assert all(a.start >= start + timedelta(hours=1) for a in plan.actions)
    discharge = next(a for a in plan.actions if a.direction is PriceDirection.DISCHARGE)
    assert discharge.start == start + timedelta(hours=2)
    # Without the binding window the plan differs (it would charge on its own).
    controller.runtime.scheduler_ownership.records.clear()
    assert controller._build_plan(series, limits).fingerprint != plan.fingerprint


def test_binding_window_targets_are_chained_and_clamped(monkeypatch):
    clock, instance, _, _, start = _binding_runtime(monkeypatch)
    clock.now = start - timedelta(minutes=3)
    records = instance.scheduler_ownership.records
    records[1] = _owned(1, "demandresponse", start, {"pacref": 10000.0})
    records[2] = _owned(2, "peakshaving", start + timedelta(minutes=2), {"discharge_max": 9e3})
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    limits = controller._validated_limits()
    eta = 0.9**0.5
    arguments = {"now": clock.now, "minimum_lead_minutes": 5, "usable_capacity_kwh": 10.0,
                 "minimum_soc_percent": 10.0, "maximum_soc_percent": 90.0,
                 "usable_soc_percent": 50.0, "maximum_charge_kw": 10.0,
                 "maximum_discharge_kw": 10.0}

    charge, discharge = controller._binding_windows(arguments, limits, lambda s, e: 2.0)

    assert charge.target_usable_soc_percent == pytest.approx(90.0)
    # Self-consumption delivers at most the forecast 2 kW for one hour.
    assert discharge.target_usable_soc_percent == pytest.approx(90 - 2 / eta * 10)
    charge, discharge = controller._binding_windows(arguments, limits, None)
    assert discharge.target_usable_soc_percent == pytest.approx(10.0)

    # Stored power above what the battery and limits allow now is capped.
    capped = {**arguments, "maximum_charge_kw": 2.0, "maximum_discharge_kw": 3.0}
    charge, discharge = controller._binding_windows(capped, limits, None)
    assert charge.target_usable_soc_percent == pytest.approx(50 + 2 * eta * 10)
    assert discharge.target_usable_soc_percent == pytest.approx(50 + 2 * eta * 10 - 3 / eta * 10)

    # An own export discharge (negative pacref) lowers the level, capped and floored.
    records[2] = _owned(2, "demandresponse", start + timedelta(minutes=2), {"pacref": -9000.0})
    charge, export = controller._binding_windows(capped, limits, None)
    assert export.target_usable_soc_percent == pytest.approx(50 + 2 * eta * 10 - 3 / eta * 10)
    charge, export = controller._binding_windows(arguments, limits, None)
    assert export.target_usable_soc_percent == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_binding_schedule_with_unknown_power_ends_the_plan_at_its_start(monkeypatch):
    from custom_components.pixiihome_controller.control.price_optimization import (
        normalize_price_series,
    )

    clock, instance, mutations, _, start = _binding_runtime(monkeypatch)
    clock.now = start - timedelta(minutes=3)
    instance.scheduler_ownership.records[1000000000701] = _owned(
        1000000000701, "demandresponse", start, {"pacref": "x"}
    )
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    series = normalize_price_series(instance.hass.states.state.attributes, "SEK/kWh")

    plan = controller._build_plan(series, controller._validated_limits())

    assert plan.actions == ()
    assert not plan.ready
    await controller.tick()
    assert mutations.deleted == []
    assert mutations.created == []


@pytest.mark.asyncio
async def test_whole_day_planner_keeps_own_charge_just_before_start(monkeypatch):
    clock, instance, mutations, _, start = _binding_runtime(monkeypatch)
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
    charge = _charge_id(instance, start)
    created = len(mutations.created)

    clock.now = start - timedelta(minutes=3)
    await controller.tick()

    assert controller.planner_engine == "whole_day"
    assert charge not in mutations.deleted
    assert charge in instance.scheduler_ownership.records
    window_end = start + timedelta(hours=1)
    assert controller.plan is not None and controller.plan.ready
    assert all(a.end <= start or a.start >= window_end for a in controller.plan.actions)
    assert all(
        r.starttime >= window_end
        or r.starttime + timedelta(seconds=r.duration_seconds) <= start
        for r in mutations.created[created:]
    )


@pytest.mark.asyncio
async def test_switch_off_just_before_start_removes_the_binding_charge(monkeypatch):
    clock, instance, mutations, _, start = _binding_runtime(monkeypatch)
    controller = PriceOptimizationController(instance)  # type: ignore[arg-type]
    await controller.tick()
    charge = _charge_id(instance, start)
    clock.now = start - timedelta(minutes=3)
    await controller.tick()
    assert charge not in mutations.deleted

    await controller.disable()

    assert charge in mutations.deleted
