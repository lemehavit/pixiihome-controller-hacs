"""Whole-day dynamic-programming planner and the standard plan's load cap."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from custom_components.pixiihome_controller.control.price_optimization import (
    ExternalWindow,
    PriceDirection,
    PriceDischargeMode,
    PriceInterval,
    PriceSeries,
    build_price_plan,
    merge_adjacent_actions,
)
from custom_components.pixiihome_controller.control.whole_day_planner import (
    build_whole_day_plan,
)

NOW = datetime(2026, 11, 2, 0, 0, tzinfo=UTC)


def _series(prices: list[float], minutes: int = 60) -> PriceSeries:
    intervals = tuple(
        PriceInterval(
            NOW + timedelta(minutes=minutes * (i + 1)),
            NOW + timedelta(minutes=minutes * (i + 2)),
            price,
        )
        for i, price in enumerate(prices)
    )
    return PriceSeries(intervals, "SEK/kWh", "prices", "fingerprint")


def _plan(series: PriceSeries, forecast, **overrides):
    arguments = dict(
        now=NOW,
        horizon_hours=48,
        minimum_lead_minutes=5,
        usable_soc_percent=10.0,
        usable_capacity_kwh=10.0,
        minimum_soc_percent=10.0,
        maximum_soc_percent=90.0,
        maximum_charge_kw=5.0,
        maximum_discharge_kw=5.0,
        minimum_difference_sek_per_kwh=0.5,
        export_allowed=False,
        export_minimum_difference_sek_per_kwh=1.0,
        round_trip_efficiency_percent=90.0,
        cycle_cost_sek_per_kwh=0.0,
        grid_import_cost_sek_per_kwh=0.5,
        forecast=forecast,
    )
    arguments.update(overrides)
    return build_whole_day_plan(series, **arguments)


def test_charges_cheap_and_covers_only_the_house_in_expensive_hours() -> None:
    series = _series([0.2, 0.2, 3.0, 3.0, 3.0, 3.0])
    result = _plan(series, lambda start, end: (1.5, 0.0))
    kinds = [slot.kind for slot in result.slots]
    assert kinds[:2] == ["grid_charge", "grid_charge"]
    assert kinds[2:] == ["self_consumption"] * 4
    # Without export the battery never discharges beyond the house load.
    assert all(slot.battery_kw >= -1.5 - 1e-6 for slot in result.slots)
    assert result.plan.ready
    assert result.planned_cost_sek < result.baseline_cost_sek
    discharge = [a for a in result.plan.actions if a.direction is PriceDirection.DISCHARGE]
    assert all(a.service == "peakshaving" for a in discharge)
    assert all(a.discharge_mode is PriceDischargeMode.SELF_CONSUMPTION for a in discharge)


def test_small_spreads_do_not_cycle_the_battery() -> None:
    series = _series([1.0, 1.0, 1.3, 1.3])
    result = _plan(series, lambda start, end: (1.0, 0.0))
    assert all(slot.kind == "idle" for slot in result.slots)
    assert result.plan.actions == ()


def test_export_needs_permission_and_its_own_minimum_profit() -> None:
    series = _series([0.1, 0.1, 4.0, 4.0])
    no_export = _plan(series, lambda start, end: (0.5, 0.0))
    assert "export" not in {slot.kind for slot in no_export.slots}
    export = _plan(series, lambda start, end: (0.5, 0.0), export_allowed=True)
    assert "export" in {slot.kind for slot in export.slots}
    exporting = [a for a in export.plan.actions if a.discharge_mode is PriceDischargeMode.EXPORT]
    assert exporting and all(a.service == "demandresponse" for a in exporting)
    assert all(float(a.values["pacref"]) < 0 for a in exporting)


def test_solar_surplus_fills_the_battery_in_the_cheapest_sunny_hours() -> None:
    # Six sunny hours with 4 kW surplus; the battery (8 kWh room) only needs two.
    prices = [1.5, 1.4, 0.3, 0.2, 1.3, 1.2, 2.5, 2.5]

    def forecast(start: datetime, end: datetime) -> tuple[float, float]:
        hour = int((start - NOW).total_seconds() // 3600) - 1
        return (1.0, 5.0 if hour < 6 else 0.0)

    result = _plan(_series(prices), forecast, maximum_charge_kw=4.0)
    kinds = [slot.kind for slot in result.slots]
    assert kinds[0] == "idle" and kinds[1] == "idle"  # export the expensive sun
    assert kinds[2] == "solar" and kinds[3] == "solar"
    assert kinds[6] == "self_consumption"
    # Solar-only charging needs no Gateway schedule.
    assert all(a.direction is not PriceDirection.CHARGE for a in result.plan.actions)


def test_known_target_soc_window_resets_energy_and_blocks_actions() -> None:
    series = _series([0.2, 3.0, 3.0, 3.0, 0.2, 3.0])
    window = ExternalWindow(
        series.intervals[2].start, series.intervals[3].end, 80.0
    )
    result = _plan(series, lambda start, end: (1.0, 0.0), external_windows=[window])
    assert result.slots[2].kind == "external" and result.slots[3].kind == "external"
    assert result.slots[3].usable_soc_percent == pytest.approx(80.0, abs=1)
    unknown = ExternalWindow(series.intervals[2].start, series.intervals[3].end, None)
    truncated = _plan(series, lambda start, end: (1.0, 0.0), external_windows=[unknown])
    assert len(truncated.slots) == 2


def test_fixed_target_refill_cost_uses_remaining_energy() -> None:
    series = _series([1.1, 1.1, 0.8, 0.8])
    window = ExternalWindow(series.intervals[2].start, series.intervals[2].end, 90.0)
    result = _plan(
        series, lambda start, end: (1.0, 0.0),
        usable_soc_percent=80.0, maximum_charge_kw=0.0,
        maximum_discharge_kw=0.0, grid_import_cost_sek_per_kwh=0.0,
        external_windows=(window,),
    )
    assert [slot.usable_soc_percent for slot in result.slots] == [80, 80, 90, 90]
    assert result.plan.actions == ()
    expected = 3.8 + 1.0 * 0.8 / 0.9 ** 0.5
    assert result.baseline_cost_sek == pytest.approx(expected)
    assert result.planned_cost_sek == pytest.approx(expected)
    assert result.plan.expected_margin_sek == pytest.approx(0.0)


def test_adjustable_target_soc_windows_let_a_discharge_stand() -> None:
    # An aggregator refills to 90 % every other hour. A fixed refill undoes
    # each discharge at a high price; a held (lowered) window keeps it.
    series = _series([1.0, 3.6, 3.4, 3.6, 3.4, 2.0])
    windows = [
        ExternalWindow(series.intervals[i].start, series.intervals[i].end, 90.0)
        for i in (2, 4)
    ]
    kwargs = dict(usable_soc_percent=90.0, minimum_lead_minutes=0)
    fixed = _plan(series, lambda start, end: (1.0, 0.0), external_windows=windows, **kwargs)
    assert all(slot.kind != "self_consumption" for slot in fixed.slots[1:5])
    held = [
        ExternalWindow(w.start, w.end, w.target_usable_soc_percent, adjustable=True)
        for w in windows
    ]
    result = _plan(series, lambda start, end: (1.0, 0.0), external_windows=held, **kwargs)
    kinds = [slot.kind for slot in result.slots]
    # Held windows are free time for the plan: their target is lowered, or
    # they are deleted where the plan uses them.
    assert kinds[1:5] == ["self_consumption"] * 4
    socs = dict(result.plan.window_socs)
    assert socs[held[0].start] < 90.0
    assert socs[held[1].start] < socs[held[0].start]


def test_rounding_past_the_house_load_follows_the_load_instead_of_exporting() -> None:
    # With 0.184 kWh energy steps, 0.6 kW of load lies between three and four
    # steps; the plan rounds up past it, which must not become a forced export.
    series = _series([0.5, 3.0])
    result = _plan(
        series, lambda start, end: (0.6, 0.0), usable_soc_percent=90.0,
        usable_capacity_kwh=18.4, export_allowed=True,
        export_minimum_difference_sek_per_kwh=1.5, minimum_lead_minutes=0,
    )
    assert result.slots[1].battery_kw < -0.65
    discharge = [a for a in result.plan.actions if a.direction is PriceDirection.DISCHARGE]
    assert discharge
    assert all(a.discharge_mode is PriceDischargeMode.SELF_CONSUMPTION for a in discharge)


def test_missing_forecast_blocks_the_whole_day_plan() -> None:
    result = _plan(_series([1.0, 2.0]), lambda start, end: None)
    assert result.plan.blockers == ("energy_forecast_unavailable",)


def test_standard_plan_caps_self_consumption_energy_by_forecast_load() -> None:
    series = _series([0.2, 3.0, 3.0, 3.0])
    arguments = dict(
        now=NOW,
        horizon_hours=48,
        minimum_lead_minutes=5,
        usable_soc_percent=90.0,
        usable_capacity_kwh=10.0,
        minimum_soc_percent=10.0,
        maximum_soc_percent=90.0,
        maximum_charge_kw=5.0,
        maximum_discharge_kw=5.0,
        minimum_difference_sek_per_kwh=0.5,
        export_allowed=False,
        export_minimum_difference_sek_per_kwh=1.0,
        round_trip_efficiency_percent=90.0,
        cycle_cost_sek_per_kwh=0.0,
        grid_import_cost_sek_per_kwh=0.5,
    )
    uncapped = build_price_plan(series, **arguments)
    capped = build_price_plan(series, **arguments, net_load_kw=lambda start, end: 1.0)
    first_uncapped = next(a for a in uncapped.actions if a.direction is PriceDirection.DISCHARGE)
    first_capped = next(a for a in capped.actions if a.direction is PriceDirection.DISCHARGE)
    # The schedule still allows full power; only the simulated energy is capped.
    assert first_capped.power_kw == first_uncapped.power_kw
    assert first_capped.projected_usable_soc_percent > first_uncapped.projected_usable_soc_percent
    assert capped.expected_margin_sek <= uncapped.expected_margin_sek
    none = build_price_plan(series, **arguments, net_load_kw=lambda start, end: 0.0)
    assert all(a.direction is not PriceDirection.DISCHARGE for a in none.actions)


def test_adjacent_identical_actions_merge_within_the_policy_duration() -> None:
    series = _series([0.2, 0.2, 0.2, 3.0], minutes=15)
    result = _plan(series, lambda start, end: (8.0, 0.0), usable_soc_percent=10.0)
    charges = [a for a in result.plan.actions if a.direction is PriceDirection.CHARGE]
    merged = merge_adjacent_actions(result.plan.actions)
    assert len(merged) < len(result.plan.actions)
    merged_charge = next(a for a in merged if a.direction is PriceDirection.CHARGE)
    assert merged_charge.start == charges[0].start
    limited = merge_adjacent_actions(result.plan.actions, max_duration_seconds=900)
    assert len(limited) == len(result.plan.actions)


def test_best_net_profit_explains_an_empty_plan() -> None:
    from custom_components.pixiihome_controller.price_controller import best_net_profit

    series = _series([1.0, 0.5, 1.2, 0.9])
    # Charge at 0.5, use at 1.2: (1.2 + 0.5) - (0.5 + 0.5) / 0.9 - 0.1.
    assert best_net_profit(series, NOW, 0.9, 0.5, 0.1) == pytest.approx(
        1.7 - 1.0 / 0.9 - 0.1
    )
    assert best_net_profit(_series([1.0]), NOW, 0.9, 0.5, 0.0) is None


def test_discharge_money_is_one_cycle_after_the_energy_cost() -> None:
    from custom_components.pixiihome_controller.control.whole_day_planner import _action

    peak = PriceInterval(NOW, NOW + timedelta(hours=1), 3.29)
    action = _action(
        peak, 2.0, 0.0, -2.0, 50.0,
        maximum_discharge_kw=10.0, step_kw=0.8, reference=0.9,
        round_trip=0.9, grid_import_cost=0.65, cycle_cost=0.3,
    )
    assert action is not None
    # Saves 3.29 + 0.65 per kWh; the energy cost (0.9 + 0.65) / 0.9 + 0.3.
    expected_net = 3.29 + 0.65 - ((0.9 + 0.65) / 0.9 + 0.3)
    assert action.net_profit_sek_per_kwh == pytest.approx(expected_net)
    assert action.net_profit_sek_per_kwh < peak.price_sek_per_kwh
    assert action.expected_margin_sek == pytest.approx(2.0 * expected_net)
    assert action.reference_price_sek_per_kwh == 0.9
    charge = _action(
        peak, 0.0, 0.0, 2.0, 50.0,
        maximum_discharge_kw=10.0, step_kw=0.8, reference=0.9,
        round_trip=0.9, grid_import_cost=0.65, cycle_cost=0.3,
    )
    assert charge is not None and charge.expected_margin_sek == 0.0


def test_discharge_before_a_fixed_target_is_valued_at_its_refill_price() -> None:
    from custom_components.pixiihome_controller.control.whole_day_planner import _basis

    assert _basis(2.0, 0.0, 0.0, 1.5) == 2.0
    assert _basis(None, 3.0, 2.0, 9.0) == 1.5
    assert _basis(None, 0.0, 0.0, 1.7) == 1.7


def test_discharge_before_a_planned_charge_is_valued_at_that_charge_price() -> None:
    # Full battery, expensive now, cheap later: the plan uses the battery now
    # and refills at 0.2, so the discharge is worth 3.0 - 0.2 / 0.9 per kWh.
    series = _series([3.0, 3.0, 0.2, 0.2, 3.0])
    result = _plan(
        series, lambda start, end: (1.0, 0.0),
        usable_soc_percent=80.0, grid_import_cost_sek_per_kwh=0.0,
    )
    first = next(a for a in result.plan.actions if a.direction is PriceDirection.DISCHARGE)
    assert first.start == series.intervals[0].start
    assert first.reference_price_sek_per_kwh == pytest.approx(0.2)


def test_discharges_below_the_minimum_profit_are_not_written() -> None:
    # Using the stored energy at 1.1 only pays a few öre per kWh against what
    # it is worth, far below the 0.5 minimum, so the energy is kept.
    series = _series([1.1, 1.1, 0.8, 0.8])
    result = _plan(
        series, lambda start, end: (1.0, 0.0),
        usable_soc_percent=80.0, grid_import_cost_sek_per_kwh=0.0,
    )
    discharges = [a for a in result.plan.actions if a.direction is PriceDirection.DISCHARGE]
    assert discharges == []
    assert result.plan.actions == ()
    assert [slot.battery_kw for slot in result.slots] == [0.0] * 4
    assert [slot.usable_soc_percent for slot in result.slots] == [80.0] * 4
    assert result.baseline_cost_sek == pytest.approx(3.8)
    assert result.planned_cost_sek == pytest.approx(result.baseline_cost_sek)
    assert result.plan.expected_margin_sek == pytest.approx(0.0)


def test_written_actions_and_reported_cost_share_the_same_trajectory() -> None:
    series = _series([0.2, 0.2, 3.0, 3.0, 3.0, 3.0])
    result = _plan(series, lambda start, end: (1.5, 0.0))
    assert any(action.direction is PriceDirection.CHARGE for action in result.plan.actions)
    assert any(action.direction is PriceDirection.DISCHARGE for action in result.plan.actions)
    eta = 0.9 ** 0.5
    energy = 1.0
    expected_cost = 0.0
    for slot in result.slots:
        hours = (slot.end - slot.start).total_seconds() / 3600
        grid = slot.load_kw - slot.solar_kw + slot.battery_kw
        expected_cost += grid * hours * (
            slot.price_sek_per_kwh + 0.5 if grid > 0 else slot.price_sek_per_kwh
        )
        energy += slot.battery_kw * hours * (eta if slot.battery_kw > 0 else 1 / eta)
        assert slot.usable_soc_percent == pytest.approx(energy * 10, abs=0.01)
    assert result.planned_cost_sek == pytest.approx(expected_cost)
    terminal_value = (sum([0.2, 0.2, 3.0, 3.0, 3.0, 3.0]) / 6 + 0.5) * eta
    expected_margin = result.baseline_cost_sek - expected_cost + terminal_value * (energy - 1.0)
    assert expected_margin > 0
    assert result.plan.expected_margin_sek == pytest.approx(expected_margin)
    assert result.plan.minimum_projected_soc == pytest.approx(
        min(10.0, *(slot.usable_soc_percent for slot in result.slots))
    )
    assert result.plan.maximum_projected_soc == pytest.approx(
        max(10.0, *(slot.usable_soc_percent for slot in result.slots))
    )


def test_lowered_targets_follow_only_the_discharges_that_are_written() -> None:
    # The DP would use the full battery at 1.1, but that is below the minimum
    # profit, so nothing is written. The aggregator's later Target SOC must
    # then not be lowered as if the battery had been used, or the aggregator
    # itself would empty it.
    series = _series([1.1, 1.1, 0.8, 0.8])
    window = ExternalWindow(
        series.intervals[2].start, series.intervals[2].end, 100.0, adjustable=True
    )
    result = _plan(
        series, lambda start, end: (1.0, 0.0),
        usable_soc_percent=80.0, grid_import_cost_sek_per_kwh=0.0,
        external_windows=(window,),
    )
    assert not [a for a in result.plan.actions if a.direction is PriceDirection.DISCHARGE]
    assert dict(result.plan.window_socs)[window.start] == pytest.approx(80.0, abs=1.0)
    assert all(slot.usable_soc_percent == pytest.approx(80.0) for slot in result.slots)
    assert result.planned_cost_sek == pytest.approx(result.baseline_cost_sek)
    assert result.plan.expected_margin_sek == pytest.approx(0.0)


def test_the_plan_does_not_export_what_a_fixed_target_buys_back_below_the_minimum() -> None:
    # 29 Oct: selling at about 7.1 kr when the aggregator's later Target SOC
    # buys the energy back at 4.36 nets about 1.3 kr/kWh, below the 2 kr
    # export minimum. The plan showed a 75-minute export (and its money) that
    # the minimum then kept from being written.
    series = _series([7.1, 7.1, 4.36, 3.0])
    window = ExternalWindow(series.intervals[2].start, series.intervals[2].end, 90.0)
    result = _plan(
        series, lambda start, end: (0.5, 0.0),
        usable_soc_percent=90.0, maximum_discharge_kw=10.0,
        minimum_difference_sek_per_kwh=1.0, export_allowed=True,
        export_minimum_difference_sek_per_kwh=2.0, cycle_cost_sek_per_kwh=0.3,
        grid_import_cost_sek_per_kwh=0.65, external_windows=(window,),
    )
    assert "export" not in {slot.kind for slot in result.slots}
    discharges = [a for a in result.plan.actions if a.direction is PriceDirection.DISCHARGE]
    assert discharges
    assert all(a.discharge_mode is PriceDischargeMode.SELF_CONSUMPTION for a in discharges)
    assert result.slots[2].usable_soc_percent == pytest.approx(90.0)
    refill_kwh = (90.0 - result.slots[1].usable_soc_percent) / 10
    assert refill_kwh > 0
    refill_cost = refill_kwh * (4.36 + 0.65) / 0.9 ** 0.5
    slot_cost = sum(
        (slot.load_kw - slot.solar_kw + slot.battery_kw)
        * (slot.price_sek_per_kwh + 0.65)
        + max(0.0, -slot.battery_kw) * 0.3
        for slot in result.slots
    )
    expected_planned_cost = slot_cost + refill_cost
    expected_baseline_cost = sum(0.5 * (price + 0.65) for price in [7.1, 7.1, 4.36, 3.0])
    assert result.baseline_cost_sek == pytest.approx(expected_baseline_cost)
    assert result.planned_cost_sek == pytest.approx(expected_planned_cost)
    terminal_value = max(0.0, (sum([7.1, 7.1, 4.36, 3.0]) / 4 + 0.65) * 0.9 ** 0.5 - 0.3)
    final_energy = result.slots[-1].usable_soc_percent / 10
    expected_margin = expected_baseline_cost - expected_planned_cost + terminal_value * (
        final_energy - 9.0
    )
    assert result.plan.expected_margin_sek == pytest.approx(expected_margin)


def test_the_plan_still_exports_when_the_buy_back_leaves_enough() -> None:
    series = _series([9.0, 9.0, 4.36, 3.0])
    window = ExternalWindow(series.intervals[2].start, series.intervals[2].end, 90.0)
    result = _plan(
        series, lambda start, end: (0.5, 0.0),
        usable_soc_percent=90.0, maximum_discharge_kw=10.0,
        minimum_difference_sek_per_kwh=1.0, export_allowed=True,
        export_minimum_difference_sek_per_kwh=2.0, cycle_cost_sek_per_kwh=0.3,
        grid_import_cost_sek_per_kwh=0.65, external_windows=(window,),
    )
    assert any(
        a.discharge_mode is PriceDischargeMode.EXPORT for a in result.plan.actions
    )
