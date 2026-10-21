from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.pixiihome_controller.control.engine import EngineStatus
from custom_components.pixiihome_controller.control.planner import (
    BatteryPlanConstraints,
    DeterministicForecaster,
    DeterministicPlanner,
    ExternalInterval,
    ForecastPoint,
    ForecastSource,
    HistoricalEnergyPoint,
    PlanDirection,
    PlanStatus,
    aggregate_forecast,
)
from custom_components.pixiihome_controller.control.simulator import SimulatorRuntime

START = datetime(2030, 1, 2, tzinfo=UTC)


def constraints(**changes: object) -> BatteryPlanConstraints:
    values: dict[str, object] = {
        "asset_id": "battery-1",
        "capacity_kwh": 10.0,
        "initial_soc_percent": 50.0,
        "min_soc_percent": 10.0,
        "max_soc_percent": 90.0,
        "max_charge_kw": 2.0,
        "max_discharge_kw": 2.0,
        "round_trip_efficiency": 0.9,
        "minimum_confidence": 0.5,
    }
    values.update(changes)
    return BatteryPlanConstraints(**values)  # type: ignore[arg-type]


def forecast(
    prices: tuple[float, ...] = (0.1, 0.2, 0.9, 0.8),
    *,
    confidence: float = 1.0,
    load_kw: float = 2.0,
    solar_kw: float = 0.0,
) -> tuple[ForecastPoint, ...]:
    return tuple(
        ForecastPoint(
            start=START + timedelta(minutes=15 * index),
            duration_seconds=900,
            load_kw=load_kw,
            solar_kw=solar_kw,
            price_per_kwh=price,
            confidence=confidence,
        )
        for index, price in enumerate(prices)
    )


def test_explicit_forecast_precedes_learned_history() -> None:
    explicit = tuple(
        ForecastPoint(
            start=START + timedelta(minutes=15 * index),
            duration_seconds=900,
            load_kw=2,
            solar_kw=1,
            price_per_kwh=0.2 + index / 100,
            confidence=0.9,
        )
        for index in range(4)
    )

    result = DeterministicForecaster().forecast(
        [],
        start=START,
        intervals=4,
        explicit_forecast=explicit,
    )

    assert result.source is ForecastSource.EXPLICIT
    assert result.points == explicit


def test_forecaster_uses_deterministic_15_minute_weekday_ewma() -> None:
    history = [
        HistoricalEnergyPoint(
            timestamp=START - timedelta(weeks=week) + timedelta(minutes=15 * index),
            load_kw=2 + week + index / 10,
            solar_kw=index / 4,
            price_per_kwh=0.1 + week / 100,
        )
        for week in (3, 2, 1)
        for index in range(4)
    ]
    forecaster = DeterministicForecaster(confidence_samples=3)

    first = forecaster.forecast(history, start=START, intervals=4)
    second = forecaster.forecast(history, start=START, intervals=4)

    assert first == second
    assert first.source is ForecastSource.WEEKDAY_EWMA
    assert len(first.points) == 4
    assert all(point.duration_seconds == 900 for point in first.points)
    assert all(point.confidence == 1 for point in first.points)
    assert first.points[0].load_kw == pytest.approx(4.0725)


def test_forecaster_returns_unavailable_without_sufficient_weekday_history() -> None:
    one_sample = [
        HistoricalEnergyPoint(
            timestamp=START - timedelta(weeks=1),
            load_kw=2,
            solar_kw=0,
            price_per_kwh=0.2,
        )
    ]

    empty = DeterministicForecaster().forecast([], start=START, intervals=1)
    insufficient = DeterministicForecaster().forecast(
        one_sample,
        start=START,
        intervals=1,
    )

    assert empty.source is ForecastSource.UNAVAILABLE
    assert empty.reason == "weekday_history_insufficient"
    assert insufficient.source is ForecastSource.UNAVAILABLE
    assert insufficient.reason == "weekday_history_insufficient"


def test_planner_charges_low_and_discharges_high_within_constraints() -> None:
    result = DeterministicPlanner().plan(forecast(), constraints())

    assert result.status is PlanStatus.READY
    assert [action.direction for action in result.actions] == [
        PlanDirection.CHARGE,
        PlanDirection.DISCHARGE,
    ]
    assert result.actions[0].rationale == "charge_low_price"
    assert result.actions[1].rationale == "discharge_high_price"
    assert result.expected_savings > 0
    assert result.minimum_soc_percent is not None
    assert result.maximum_soc_percent is not None
    assert result.minimum_soc_percent >= 10
    assert result.maximum_soc_percent <= 90
    assert max(abs(action.power_kw) for action in result.actions) <= 2


def test_planner_rejects_non_15_minute_or_misaligned_forecasts() -> None:
    wrong_duration = tuple(
        replace(point, duration_seconds=1800)
        for point in forecast()
    )
    misaligned = tuple(
        replace(point, start=point.start + timedelta(seconds=60))
        for point in forecast()
    )

    duration_result = DeterministicPlanner().plan(wrong_duration, constraints())
    alignment_result = DeterministicPlanner().plan(misaligned, constraints())

    assert duration_result.status is PlanStatus.BLOCKED
    assert "forecast_interval_not_15_minutes" in duration_result.blockers
    assert alignment_result.status is PlanStatus.BLOCKED
    assert alignment_result.blockers == ("forecast_interval_not_utc_aligned",)


def test_planner_prioritizes_solar_surplus_without_grid_charge_cost() -> None:
    points = forecast(
        prices=(0.4, 0.4, 0.4, 0.4),
        load_kw=1.0,
        solar_kw=3.0,
    )

    result = DeterministicPlanner().plan(points, constraints())

    assert result.status is PlanStatus.READY
    assert result.actions
    assert result.actions[0].rationale == "capture_solar_surplus"
    assert result.planned_cost == result.baseline_cost == 0


def test_planner_fails_closed_for_uncertainty_gaps_and_external_conflicts() -> None:
    planner = DeterministicPlanner()
    uncertain = planner.plan(forecast(confidence=0.4), constraints())
    gapped_points = list(forecast())
    gapped_points[2] = replace(gapped_points[2], start=START + timedelta(hours=3))
    gapped = planner.plan(tuple(gapped_points), constraints())
    conflict = planner.plan(
        forecast(),
        constraints(),
        external_intervals=(
            ExternalInterval(
                start=START,
                end=START + timedelta(minutes=30),
                identity="external-1",
            ),
        ),
    )

    assert uncertain.blockers == ("forecast_confidence_insufficient",)
    assert gapped.blockers == ("forecast_not_contiguous",)
    assert conflict.blockers == ("external_schedule_conflict",)
    assert uncertain.actions == gapped.actions == conflict.actions == ()


def test_plan_fingerprint_and_output_are_stable() -> None:
    planner = DeterministicPlanner()

    first = planner.plan(forecast(), constraints(), first_m_id=240000001)
    second = planner.plan(forecast(), constraints(), first_m_id=240000001)
    changed = planner.plan(
        forecast(prices=(0.1, 0.2, 1.0, 0.8)),
        constraints(),
        first_m_id=240000001,
    )

    assert first == second
    assert first.fingerprint != changed.fingerprint
    assert [action.m_id for action in first.actions] == [240000001, 240000003]
    assert len({action.source_id for action in first.actions}) == len(first.actions)


def test_forecast_aggregation_preserves_energy_shape_and_minimum_confidence() -> None:
    points = list(forecast())
    points[1] = replace(points[1], confidence=0.6)

    aggregated = aggregate_forecast(tuple(points), bucket_seconds=1800)

    assert len(aggregated) == 2
    assert aggregated[0].load_kw == 2
    assert aggregated[0].price_per_kwh == pytest.approx(0.15)
    assert aggregated[0].confidence == 0.6
    assert aggregated[0].end == START + timedelta(minutes=30)


def test_forecast_aggregation_rejects_gap_between_buckets() -> None:
    points = list(forecast())
    points[2] = replace(points[2], start=points[2].start + timedelta(minutes=15))
    points[3] = replace(points[3], start=points[3].start + timedelta(minutes=15))

    with pytest.raises(ValueError, match="contiguous"):
        aggregate_forecast(tuple(points), bucket_seconds=1800)


@pytest.mark.asyncio
async def test_ready_plan_applies_through_canonical_simulator_pipeline() -> None:
    plan = DeterministicPlanner().plan(
        forecast(),
        constraints(),
        first_m_id=240000001,
    )
    runtime = SimulatorRuntime()

    result = await runtime.execute(plan.as_adapter_request(asset_id="battery-1"))

    assert plan.status is PlanStatus.READY
    assert result.status is EngineStatus.SUCCESS
    assert sorted(runtime.adapter.schedules) == [action.m_id for action in plan.actions]
    assert runtime.journal.journal.transactions[-1].operation == "planner_apply"


def test_blocked_plan_cannot_be_converted_to_adapter_request() -> None:
    plan = DeterministicPlanner().plan(forecast(confidence=0), constraints())

    with pytest.raises(ValueError, match="blocked"):
        plan.as_adapter_request(asset_id="battery-1")


def test_planner_blocks_instead_of_truncating_excess_actions() -> None:
    points = tuple(
        ForecastPoint(
            start=START + timedelta(minutes=15 * index),
            duration_seconds=900,
            load_kw=1.0,
            solar_kw=0.0,
            price_per_kwh=0.1 if index % 2 == 0 else 1.0,
            confidence=1.0,
        )
        for index in range(8)
    )

    result = DeterministicPlanner().plan(
        points,
        constraints(capacity_kwh=100.0, maximum_actions=3),
    )

    assert result.status is PlanStatus.BLOCKED
    assert result.actions == ()
    assert result.blockers == ("maximum_actions_exceeded",)
