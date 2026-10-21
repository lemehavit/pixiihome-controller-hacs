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
    async def async_refresh(self):
        return SimpleNamespace(data_status="current", schedules=[])


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
async def test_controller_can_enable_and_wait_for_tomorrow_prices() -> None:
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
    assert controller.reason == "future_price_horizon_incomplete"
    assert controller.price_source_format == "timestamped_day_arrays"
    assert controller.price_interval_count == 1
    assert mutations.created == []


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
    assert await controller._move_one_managed_external_charge(series, proposal.actions)
    assert reads == [300]
    assert controller.external_move_count == 0
