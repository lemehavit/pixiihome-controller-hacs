"""Price-source and export-aware planning tests."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pixiihome_controller.control.price_optimization import (
    ExternalWindow,
    PriceDirection,
    PriceDischargeMode,
    PriceOptimizationConfig,
    build_price_plan,
    normalize_price_series,
)


def greenely(*prices: float):
    return normalize_price_series(
        {
            "current_day": [
                {"date": "Oct 16 2026", "time": f"{hour:02}:00", "price": price}
                for hour, price in enumerate(prices)
            ]
        },
        "SEK/kWh",
    )


def plan(
    series, *, export_allowed=False, export_delta=1.0, grid_import_cost=0.0,
    initial_soc=50, charge_kw=2, discharge_kw=2,
    minimum_profit=0.5, cycle_cost=0.0, end_before=None, windows=(),
    net_load_kw=None,
):
    return build_price_plan(
        series,
        now=datetime(2026, 10, 15, 21, tzinfo=UTC),
        horizon_hours=36,
        minimum_lead_minutes=5,
        usable_soc_percent=initial_soc,
        usable_capacity_kwh=10,
        minimum_soc_percent=10,
        maximum_soc_percent=90,
        maximum_charge_kw=charge_kw,
        maximum_discharge_kw=discharge_kw,
        minimum_difference_sek_per_kwh=minimum_profit,
        export_allowed=export_allowed,
        export_minimum_difference_sek_per_kwh=export_delta,
        round_trip_efficiency_percent=90,
        cycle_cost_sek_per_kwh=cycle_cost,
        grid_import_cost_sek_per_kwh=grid_import_cost,
        end_before=end_before,
        external_windows=windows,
        net_load_kw=net_load_kw,
    )


def _charged_kwh(result):
    return sum(
        action.power_kw * action.duration_seconds / 3600
        for action in result.actions if action.direction is PriceDirection.CHARGE
    )


def test_charge_requires_profitable_forecast_use_when_export_is_off() -> None:
    result = plan(
        greenely(.2, .2, 3), initial_soc=10, charge_kw=5, discharge_kw=5,
        grid_import_cost=.5, net_load_kw=lambda _start, _end: 0.0,
    )
    assert _charged_kwh(result) == 0
    assert result.charge_demand_limited


def test_charge_covers_only_small_profitable_net_load() -> None:
    result = plan(
        greenely(.2, .2, 3), initial_soc=10, charge_kw=5, discharge_kw=5,
        grid_import_cost=.5,
        net_load_kw=lambda start, _end: .1 if start == _hour(2) else 0.0,
    )
    assert 0 < _charged_kwh(result) <= .1 / .9 + 1e-9
    discharge = [a for a in result.actions if a.direction is PriceDirection.DISCHARGE]
    assert discharge
    delivered = sum(a.expected_margin_sek / a.net_profit_sek_per_kwh for a in discharge)
    assert delivered == pytest.approx(.1)


def test_existing_stored_energy_covers_forecast_before_new_charge() -> None:
    result = plan(
        greenely(.2, .2, 3), initial_soc=30, charge_kw=5, discharge_kw=5,
        grid_import_cost=.5, net_load_kw=lambda _start, _end: 1.0,
    )
    assert _charged_kwh(result) == 0


def test_negative_net_load_in_peak_does_not_justify_charge() -> None:
    result = plan(
        greenely(.2, .2, 3), initial_soc=10, charge_kw=5, discharge_kw=5,
        grid_import_cost=.5, net_load_kw=lambda _start, _end: -2.0,
    )
    assert _charged_kwh(result) == 0


@pytest.mark.parametrize("forecast", [None, float("nan")])
def test_unknown_forecast_keeps_headroom_charge_behavior(forecast) -> None:
    result = plan(
        greenely(.2, .2, 3), initial_soc=10, charge_kw=5, discharge_kw=5,
        grid_import_cost=.5, net_load_kw=lambda _start, _end: forecast,
    )
    assert _charged_kwh(result) > 5
    assert not result.charge_demand_limited


def test_absent_forecast_keeps_headroom_charge_behavior() -> None:
    result = plan(
        greenely(.2, .2, 3), initial_soc=10, charge_kw=5, discharge_kw=5,
        grid_import_cost=.5,
    )
    assert _charged_kwh(result) > 5
    assert not result.charge_demand_limited


def test_load_before_a_later_peak_that_planner_will_wait_for_does_not_fund_charge() -> None:
    result = plan(
        greenely(.2, 2, 3), initial_soc=10, charge_kw=5, discharge_kw=5,
        net_load_kw=lambda start, _end: 5.0 if start == _hour(1) else 0.0,
    )
    assert _charged_kwh(result) == 0


def test_export_can_justify_charge_with_zero_household_load() -> None:
    result = plan(
        greenely(.2, .2, 3), initial_soc=10, charge_kw=5, discharge_kw=5,
        grid_import_cost=.5, export_allowed=True, export_delta=.5,
        net_load_kw=lambda _start, _end: 0.0,
    )
    assert _charged_kwh(result) > 0
    assert any(a.discharge_mode is PriceDischargeMode.EXPORT for a in result.actions)


def test_charge_demand_does_not_cross_known_external_reset() -> None:
    window = ExternalWindow(_hour(2), _hour(3), 10.0)
    result = plan(
        greenely(.2, .2, 1, 3), initial_soc=10, charge_kw=5, discharge_kw=5,
        windows=(window,), net_load_kw=lambda _start, _end: 5.0,
    )
    assert not any(a.direction is PriceDirection.CHARGE and a.end <= window.start
                   for a in result.actions)


def test_pre_reset_charge_is_sized_only_for_its_own_peak() -> None:
    window = ExternalWindow(_hour(2), _hour(3), 10.0)
    result = plan(
        greenely(.2, 3, 1, 9), initial_soc=10, charge_kw=5, discharge_kw=5,
        windows=(window,),
        net_load_kw=lambda start, _end: 1.0 if start == _hour(1) else 5.0,
    )
    before = [a for a in result.actions
              if a.direction is PriceDirection.CHARGE and a.end <= window.start]
    assert before
    assert sum(a.power_kw * a.duration_seconds / 3600 for a in before) <= 1 / .9 + 1e-9


def test_forecast_cap_above_power_headroom_does_not_set_limit_flag() -> None:
    series = greenely(.2, 3)
    uncapped = plan(series, initial_soc=10, charge_kw=.1, discharge_kw=5)
    capped = plan(
        series, initial_soc=10, charge_kw=.1, discharge_kw=5,
        net_load_kw=lambda _start, _end: 5.0,
    )
    assert [a.power_kw for a in capped.actions if a.direction is PriceDirection.CHARGE] == [
        a.power_kw for a in uncapped.actions if a.direction is PriceDirection.CHARGE
    ]
    assert not capped.charge_demand_limited


def test_sub_threshold_charge_cap_does_not_report_demand_limit() -> None:
    result = plan(
        greenely(.2, 3), initial_soc=10, charge_kw=.04, discharge_kw=5,
        net_load_kw=lambda _start, _end: .01,
    )
    assert _charged_kwh(result) == 0
    assert not result.charge_demand_limited


def _prices_with_expensive_initial_basis(*prices: float, initial_basis: float = 3.0):
    return normalize_price_series(
        {"raw_today": [
            {"start": _hour(index), "end": _hour(index + 1), "value": price}
            for index, price in enumerate((initial_basis, *prices), start=-1)
        ]},
        "SEK/kWh",
    )


def test_expensive_existing_energy_does_not_crowd_out_cheap_profitable_charge() -> None:
    result = plan(
        _prices_with_expensive_initial_basis(.2, .2, 3), initial_soc=50,
        charge_kw=5, discharge_kw=5,
        net_load_kw=lambda start, _end: 2.0 if start == _hour(2) else 0.0,
    )
    assert _charged_kwh(result) == pytest.approx(2 / .9)
    assert _delivered_kwh(result) == pytest.approx(2)
    assert result.expected_margin_sek > 0


def test_expensive_energy_before_reset_does_not_crowd_out_cheap_charge() -> None:
    window = ExternalWindow(_hour(3), _hour(4), 10.0)
    result = plan(
        _prices_with_expensive_initial_basis(.2, .2, 3, 3), initial_soc=50,
        charge_kw=5, discharge_kw=5, windows=(window,),
        net_load_kw=lambda start, _end: 2.0 if start == _hour(2) else 0.0,
    )
    assert _charged_kwh(result) == pytest.approx(2 / .9)
    assert _delivered_kwh(result, before=window.start) == pytest.approx(2)
    assert result.expected_margin_sek > 0


def _delivered_kwh(result, *, before=None) -> float:
    return sum(
        action.expected_margin_sek / action.net_profit_sek_per_kwh
        for action in result.actions
        if action.direction is PriceDirection.DISCHARGE
        and (before is None or action.end <= before)
    )


@pytest.mark.parametrize("soc,peak", [(50, 2.4), (30, 2.0)])
def test_open_segment_keeps_new_cheap_energy_separate_from_expensive_initial_energy(
    soc, peak,
) -> None:
    result = plan(
        _prices_with_expensive_initial_basis(.2, .2, peak),
        initial_soc=soc, charge_kw=5, discharge_kw=5,
        net_load_kw=lambda start, _end: 2.0 if start == _hour(2) else 0.0,
    )
    assert _charged_kwh(result) == pytest.approx(2 / .9)
    assert _delivered_kwh(result) == pytest.approx(2)
    assert result.expected_margin_sek > 0


def test_closed_segment_counts_both_initial_and_new_qualifying_energy() -> None:
    window = ExternalWindow(_hour(3), _hour(4), 10.0)
    result = plan(
        _prices_with_expensive_initial_basis(.2, .2, 3, .2, initial_basis=.2),
        initial_soc=20, charge_kw=5, discharge_kw=5, windows=(window,),
        net_load_kw=lambda start, _end: 2.0 if start == _hour(2) else 0.0,
    )
    assert _charged_kwh(result) == pytest.approx((2 / (.9 ** .5) - 1) / (.9 ** .5))
    assert _delivered_kwh(result, before=window.start) == pytest.approx(2)


def test_less_flexible_initial_energy_is_used_before_cheap_energy() -> None:
    result = plan(
        _prices_with_expensive_initial_basis(.2, 3, 2.5, initial_basis=2),
        initial_soc=20, charge_kw=5, discharge_kw=5,
        net_load_kw=lambda start, _end: .9 if start in (_hour(1), _hour(2)) else 0.0,
    )
    discharges = [action for action in result.actions
                  if action.direction is PriceDirection.DISCHARGE]
    assert len(discharges) == 2
    assert [action.expected_margin_sek / action.net_profit_sek_per_kwh
            for action in discharges] == pytest.approx([.9, .9])
    assert discharges[0].reference_price_sek_per_kwh == pytest.approx(2)
    assert discharges[1].reference_price_sek_per_kwh == pytest.approx(.2)


def test_two_charge_prices_give_energy_weighted_discharge_basis_and_margin() -> None:
    result = plan(
        greenely(.2, .3, 3), initial_soc=10, charge_kw=1, discharge_kw=5,
        net_load_kw=lambda start, _end: 2.0 if start == _hour(2) else 0.0,
    )
    discharge = next(action for action in result.actions
                     if action.direction is PriceDirection.DISCHARGE)
    assert _charged_kwh(result) == pytest.approx(2)
    assert discharge.reference_price_sek_per_kwh == pytest.approx(.25)
    assert discharge.power_kw == pytest.approx(1.8)
    expected_net = 3 - .25 / .9
    assert discharge.net_profit_sek_per_kwh == pytest.approx(expected_net)
    assert discharge.expected_margin_sek == pytest.approx(1.8 * expected_net)


def test_forecast_caps_simulated_energy_but_keeps_full_schedule_power() -> None:
    result = plan(
        greenely(.2, 3), initial_soc=50, discharge_kw=5,
        net_load_kw=lambda start, _end: .1 if start == _hour(1) else 0.0,
    )
    discharge = next(action for action in result.actions
                     if action.direction is PriceDirection.DISCHARGE)
    assert discharge.power_kw == pytest.approx(4 * (.9 ** .5))
    assert discharge.values["discharge_max"] == pytest.approx(4000 * (.9 ** .5))
    assert _delivered_kwh(result) == pytest.approx(.1)
    assert discharge.projected_usable_soc_percent == pytest.approx(
        50 - .1 / (.9 ** .5) * 10
    )
    assert discharge.expected_margin_sek == pytest.approx(
        .1 * discharge.net_profit_sek_per_kwh
    )


def test_absent_forecast_preserves_blended_basis_and_action_choice() -> None:
    result = plan(
        _prices_with_expensive_initial_basis(.2, .2, 2.4),
        initial_soc=30, charge_kw=2, discharge_kw=2,
    )
    charges = [action for action in result.actions
               if action.direction is PriceDirection.CHARGE]
    discharge = next(action for action in result.actions
                     if action.direction is PriceDirection.DISCHARGE)
    assert len(charges) == 2
    assert _charged_kwh(result) == pytest.approx(4)
    stored_new = 4 * (.9 ** .5)
    blended_basis = (3 * 3 + stored_new * .2) / (3 + stored_new)
    assert discharge.reference_price_sek_per_kwh == pytest.approx(blended_basis)
    assert discharge.power_kw == pytest.approx(2)
    assert discharge.net_profit_sek_per_kwh == pytest.approx(2.4 - blended_basis / .9)


def test_unknown_future_forecast_preserves_blended_basis_and_action_choice() -> None:
    series = _prices_with_expensive_initial_basis(.2, .2, 2.4)
    without_forecast = plan(series, initial_soc=30, charge_kw=2, discharge_kw=2)
    unknown_future = plan(
        series, initial_soc=30, charge_kw=2, discharge_kw=2,
        net_load_kw=lambda start, _end: None if start == _hour(2) else 0.0,
    )
    assert [action.as_dict() for action in unknown_future.actions] == [
        action.as_dict() for action in without_forecast.actions
    ]
    discharge = next(action for action in unknown_future.actions
                     if action.direction is PriceDirection.DISCHARGE)
    stored_new = 4 * (.9 ** .5)
    assert _charged_kwh(unknown_future) == pytest.approx(4)
    assert discharge.reference_price_sek_per_kwh == pytest.approx(
        (3 * 3 + stored_new * .2) / (3 + stored_new)
    )


def test_self_consumption_energy_does_not_crowd_out_profitable_export_charge() -> None:
    result = plan(
        _prices_with_expensive_initial_basis(.2, 3, initial_basis=2.2),
        initial_soc=70, export_allowed=True, export_delta=1.0,
        charge_kw=5, discharge_kw=5,
        net_load_kw=lambda _start, _end: 0.0,
    )
    assert _charged_kwh(result) > 0
    assert any(action.discharge_mode is PriceDischargeMode.EXPORT
               for action in result.actions)
    assert result.expected_margin_sek > 0


def test_greenely_source_is_normalized_from_stockholm_to_utc() -> None:
    series = greenely(1.0, 2.0)

    assert series.unit == "SEK/kWh"
    assert series.source_format == "greenely_day_arrays"
    assert series.intervals[0].start == datetime(2026, 10, 15, 22, tzinfo=UTC)
    assert series.intervals[1].start == datetime(2026, 10, 15, 23, tzinfo=UTC)


def test_ore_unit_is_converted_without_magnitude_guessing() -> None:
    series = normalize_price_series(
        {
            "raw_today": [
                {
                    "start": "2026-10-16T00:00:00+02:00",
                    "end": "2026-10-16T01:00:00+02:00",
                    "value": 125,
                }
            ]
        },
        "öre/kWh",
    )

    assert series.intervals[0].price_sek_per_kwh == 1.25
    with pytest.raises(ValueError, match="price_unit_unsupported"):
        normalize_price_series({}, "EUR/MWh")


def test_duplicate_fallback_hour_requires_both_occurrences() -> None:
    with pytest.raises(ValueError, match="price_wall_time_ambiguous"):
        normalize_price_series(
            {"current_day": [{"date": "Oct 25 2026", "time": "02:00", "price": 1.0}]},
            "SEK/kWh",
        )

    series = normalize_price_series(
        {
            "current_day": [
                {"date": "Oct 25 2026", "time": "02:00", "price": 1.0},
                {"date": "Oct 25 2026", "time": "02:00", "price": 1.1},
            ]
        },
        "SEK/kWh",
    )
    assert series.intervals[1].start > series.intervals[0].start


def test_native_nordpool_datetime_attributes_match_serialized_prices_across_dst() -> None:
    local = ZoneInfo("Europe/Stockholm")
    starts = [datetime(2026, 10, 25, 2, tzinfo=local, fold=fold) for fold in (0, 1)]
    items = [
        {"start": start, "end": start.astimezone(UTC) + timedelta(hours=1), "value": 1 + i}
        for i, start in enumerate(starts)
    ]
    native = normalize_price_series({"raw_today": items}, "SEK/kWh")
    serialized = normalize_price_series({
        "raw_today": [{**item, "start": item["start"].isoformat(),
                       "end": item["end"].isoformat()} for item in items]
    }, "SEK/kWh")
    assert native.fingerprint == serialized.fingerprint
    assert native.intervals[1].start - native.intervals[0].start == timedelta(hours=1)
    with pytest.raises(ValueError, match="price_timestamp_timezone_required"):
        normalize_price_series({"raw_today": [{
            "start": datetime(2026, 10, 25, 2), "value": 1
        }]}, "SEK/kWh")


def test_export_disabled_uses_zero_export_peak_shaving() -> None:
    result = plan(greenely(1.0, 1.1, 3.0), export_allowed=False)

    discharge = next(
        action for action in result.actions if action.direction is PriceDirection.DISCHARGE
    )
    assert discharge.discharge_mode is PriceDischargeMode.SELF_CONSUMPTION
    assert discharge.service == "peakshaving"
    assert discharge.values["exportlim"] == 0
    assert discharge.values["charge_max"] == 0


def test_export_requires_its_own_spread_and_uses_signed_demand_response() -> None:
    below = plan(greenely(1.0, 1.1, 2.0), export_allowed=True, export_delta=1.5)
    above = plan(greenely(1.0, 1.1, 3.0), export_allowed=True, export_delta=1.5)

    below_discharge = next(
        action for action in below.actions if action.direction is PriceDirection.DISCHARGE
    )
    above_discharge = next(
        action for action in above.actions if action.direction is PriceDirection.DISCHARGE
    )
    assert below_discharge.discharge_mode is PriceDischargeMode.SELF_CONSUMPTION
    assert above_discharge.discharge_mode is PriceDischargeMode.EXPORT
    assert above_discharge.service == "demandresponse"
    assert above_discharge.values["pacref"] < 0


def test_grid_import_cost_is_avoided_for_self_consumption_but_not_export() -> None:
    self_consumption = plan(
        greenely(1.0, 1.1, 2.0),
        export_allowed=False,
        grid_import_cost=1.2,
    )
    export = plan(
        greenely(1.0, 1.1, 2.0),
        export_allowed=True,
        export_delta=0.5,
        grid_import_cost=1.2,
    )

    discharge = next(
        action
        for action in self_consumption.actions
        if action.direction is PriceDirection.DISCHARGE
    )
    assert discharge.discharge_mode is PriceDischargeMode.SELF_CONSUMPTION
    assert export.actions
    assert all(action.discharge_mode is not PriceDischargeMode.EXPORT for action in export.actions)


@pytest.mark.parametrize("high,qualifies", [(1.92, False), (1.93, True)])
def test_minimum_self_profit_is_after_grid_loss_and_cycle_cost(high, qualifies):
    result = plan(greenely(.5, high), initial_soc=10,
                  grid_import_cost=.65, cycle_cost=.3, minimum_profit=1)
    discharges = [a for a in result.actions if a.direction is PriceDirection.DISCHARGE]
    assert bool(discharges) is qualifies
    if qualifies:
        action = discharges[0]
        expected_net = high + .65 - (.5 + .65) / .9 - .3
        assert action.net_profit_sek_per_kwh == pytest.approx(expected_net)
        assert action.expected_margin_sek == pytest.approx(
            action.power_kw * action.duration_seconds / 3600 * expected_net
        )
        assert result.expected_margin_sek == pytest.approx(action.expected_margin_sek)


@pytest.mark.parametrize("high,qualifies", [(3.57, False), (3.58, True)])
def test_export_has_independent_net_profit_floor(high, qualifies):
    result = plan(greenely(.5, high), initial_soc=10, export_allowed=True,
                  export_delta=2, minimum_profit=10, grid_import_cost=.65, cycle_cost=.3)
    discharges = [a for a in result.actions if a.direction is PriceDirection.DISCHARGE]
    assert bool(discharges) is qualifies
    if qualifies:
        assert discharges[0].discharge_mode is PriceDischargeMode.EXPORT
        assert discharges[0].net_profit_sek_per_kwh == pytest.approx(
            high - (.5 + .65) / .9 - .3
        )


def test_export_permission_cannot_suppress_qualifying_self_consumption():
    result = plan(greenely(.5, 2), initial_soc=10, export_allowed=True,
                  export_delta=2, minimum_profit=1, grid_import_cost=.65, cycle_cost=.3)
    discharge = next(a for a in result.actions if a.direction is PriceDirection.DISCHARGE)
    assert discharge.discharge_mode is PriceDischargeMode.SELF_CONSUMPTION
    assert discharge.net_profit_sek_per_kwh >= 1


def test_battery_cannot_charge_for_price_arbitrage_with_discharge_disabled():
    assert plan(greenely(.5, 4), initial_soc=10, discharge_kw=0).actions == ()


def test_plan_does_not_charge_for_a_peak_beyond_external_energy_boundary():
    series = greenely(.5, 1, 4)
    result = plan(series, initial_soc=10, minimum_profit=1,
                  end_before=series.intervals[-1].start)
    assert result.ready
    assert result.actions == ()


def test_discharge_uses_actual_simulated_purchase_price_not_a_missed_low():
    series = greenely(.1, .5, 1)
    # Only the final expensive interval is in the future. Do not value the
    # initial energy using the much cheaper historical minimum .1.
    result = build_price_plan(
        series, now=series.intervals[1].start + timedelta(minutes=5),
        horizon_hours=12, minimum_lead_minutes=5, usable_soc_percent=90,
        usable_capacity_kwh=10, minimum_soc_percent=10, maximum_soc_percent=90,
        maximum_charge_kw=2, maximum_discharge_kw=2,
        minimum_difference_sek_per_kwh=.6, export_allowed=False,
        export_minimum_difference_sek_per_kwh=2, round_trip_efficiency_percent=90,
        cycle_cost_sek_per_kwh=0, grid_import_cost_sek_per_kwh=0,
    )
    assert result.actions == ()


def test_subminimum_charge_does_not_create_phantom_discharge_energy():
    result = plan(greenely(.5, 4), initial_soc=10, charge_kw=.049)
    assert result.actions == ()


def test_plan_respects_usable_soc_and_power_limits() -> None:
    result = plan(greenely(0.5, 0.6, 4.0, 4.0), export_allowed=True, export_delta=1.0)

    assert result.ready
    assert result.minimum_projected_soc >= 10
    assert result.maximum_projected_soc <= 90
    assert all(action.power_kw <= 2 for action in result.actions)


def test_plan_waits_for_the_cheapest_future_charge_interval() -> None:
    result = plan(greenely(1.0, 0.5, 3.0), export_allowed=True, export_delta=1.0)

    charge = next(action for action in result.actions if action.direction is PriceDirection.CHARGE)
    assert charge.price_sek_per_kwh == 0.5


@pytest.mark.parametrize("initial_soc", [0, 5, 10, 90, 95, 100])
def test_feature_limits_constrain_each_direction_without_blocking_other_initial_soc(initial_soc):
    result = plan(greenely(0.5, 4), initial_soc=initial_soc, charge_kw=100, discharge_kw=100)
    assert result.ready
    assert result.actions
    for item in result.actions:
        if item.direction is PriceDirection.CHARGE:
            assert item.projected_usable_soc_percent <= 90 + 1e-6
        else:
            assert item.projected_usable_soc_percent >= 10 - 1e-6
    if initial_soc >= 90:
        assert all(item.direction is PriceDirection.DISCHARGE for item in result.actions)
        # Preserve all real usable energy; do not silently clamp 100% to the
        # feature's 90% ceiling before calculating available discharge.
        delivered_kwh = sum(item.power_kw * item.duration_seconds / 3600
                            for item in result.actions)
        assert delivered_kwh == pytest.approx(10 * (initial_soc - 10) / 100 * (0.9 ** 0.5))
    if initial_soc <= 10:
        assert result.actions[0].direction is PriceDirection.CHARGE


@pytest.mark.parametrize("initial_soc,charge_kw,discharge_kw", [(5, 0, 100), (95, 100, 0)])
def test_no_forced_recovery_or_wrong_direction_when_price_or_power_prevents_it(
    initial_soc, charge_kw, discharge_kw,
):
    result = plan(greenely(0.5, 4), initial_soc=initial_soc,
                  charge_kw=charge_kw, discharge_kw=discharge_kw)
    assert result.ready
    assert result.actions == ()


@pytest.mark.parametrize("initial_soc", [-1, 101])
def test_soc_outside_the_physical_usable_range_is_still_invalid(initial_soc):
    with pytest.raises(ValueError, match="price_usable_soc_invalid"):
        plan(greenely(0.5, 4), initial_soc=initial_soc)


def test_config_requires_sensor_entity_and_explicit_optional_source() -> None:
    config = PriceOptimizationConfig.from_mapping(
        {"price_entity_id": "sensor.greenely_prices", "managed_source_id": 7}
    )
    assert config.price_entity_id == "sensor.greenely_prices"
    assert config.managed_source_id == 7
    with pytest.raises(ValueError, match="price_entity_invalid"):
        PriceOptimizationConfig.from_mapping({"price_entity_id": "switch.wrong"})


def _hour(index: int) -> datetime:
    return datetime(2026, 10, 15, 22, tzinfo=UTC) + timedelta(hours=index)


def test_plan_continues_past_known_target_soc_window_from_its_target() -> None:
    window = ExternalWindow(_hour(1), _hour(2), 90.0)

    result = plan(greenely(0.5, 1.0, 0.5, 3.0), windows=(window,))

    assert result.ready
    assert not any(a.start < window.end and window.start < a.end for a in result.actions)
    discharge = [a for a in result.actions if a.direction is PriceDirection.DISCHARGE]
    assert discharge and discharge[-1].start == _hour(3)
    # Energy after the window is the target, bought at the window's price.
    assert discharge[-1].reference_price_sek_per_kwh == pytest.approx(1.0)


def test_unknown_external_energy_still_ends_the_plan_at_its_start() -> None:
    window = ExternalWindow(_hour(1), _hour(2), None)

    result = plan(greenely(0.5, 1.0, 0.5, 3.0), windows=(window,))

    assert all(a.end <= window.start for a in result.actions)


def test_stored_energy_is_only_sold_before_a_target_window_above_its_refill_price() -> None:
    prices = greenely(0.4, 0.4, 2.0, 3.0, 0.5)
    window = ExternalWindow(_hour(3), _hour(4), 90.0)

    unconstrained = plan(prices, initial_soc=80, end_before=_hour(3))
    constrained = plan(prices, initial_soc=80, windows=(window,))

    sold = [a for a in unconstrained.actions if a.direction is PriceDirection.DISCHARGE]
    assert sold and sold[0].reference_price_sek_per_kwh < 3.0
    for action in constrained.actions:
        assert action.end <= window.start or action.start >= window.end
        if action.direction is PriceDirection.DISCHARGE and action.end <= window.start:
            # Anything sold here must have been charged here, never the
            # stored energy that the window would rebuy at 3.0.
            assert action.reference_price_sek_per_kwh < 3.0
            assert any(a.direction is PriceDirection.CHARGE and a.end <= action.start
                       for a in constrained.actions)


def test_held_window_keeps_energy_and_reports_its_planned_soc() -> None:
    window = ExternalWindow(_hour(2), _hour(3), 90.0, adjustable=True)

    result = plan(greenely(0.5, 3.0, 1.0, 1.0, 0.2, 3.0), initial_soc=60, windows=(window,))

    assert not any(a.start < window.end and window.start < a.end for a in result.actions)
    assert result.window_socs and result.window_socs[0][0] == window.start
    held = result.window_socs[0][1]
    assert held < 60  # discharged before the window and not refilled by it
    after = [a for a in result.actions if a.start >= window.end]
    assert any(a.direction is PriceDirection.CHARGE for a in after)


def _dst_day(broken):
    """Sun 25 Oct 2026, 100 quarter hours in UTC, written as Nord Pool does."""
    stockholm = ZoneInfo("Europe/Stockholm")
    first = datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    items = []
    for index in range(100):
        start = first + timedelta(minutes=15 * index)
        items.append({
            "start": start.astimezone(stockholm).isoformat(),
            "end": (start + timedelta(minutes=15)).astimezone(stockholm).isoformat(),
            "value": 0.1 + index / 1000,
        })
    return broken(items) or items


@pytest.mark.parametrize("broken", [
    lambda items: None,
    # The last summer-time quarter ends "02:00" on the wrong offset: before it starts.
    lambda items: items[19].update(end="2026-10-25T02:00:00+02:00"),
    # A quarter whose end equals its start.
    lambda items: items[20].update(end=items[20]["start"]),
    # The repeated hour written twice with the same offset.
    lambda items: [
        *items[:24],
        *[{**item, "start": items[16 + n]["start"], "end": items[16 + n]["end"]}
          for n, item in enumerate(items[24:28])],
        *items[28:],
    ],
    # The repeated hour missing.
    lambda items: [*items[:20], *items[24:]],
])
def test_the_25_hour_day_never_stops_planning(broken) -> None:
    series = normalize_price_series({"raw_today": _dst_day(broken)}, "SEK/kWh")
    intervals = series.intervals
    assert intervals[0].start == datetime(2026, 10, 24, 22, 0, tzinfo=UTC)
    assert intervals[-1].end == datetime(2026, 10, 25, 23, 0, tzinfo=UTC)
    assert all(a.end == b.start for a, b in zip(intervals, intervals[1:], strict=False))
    assert all(item.end > item.start for item in intervals)
