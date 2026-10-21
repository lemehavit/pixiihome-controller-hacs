"""Household-load model, solar forecast parsing, correction and savings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from custom_components.pixiihome_controller.control.energy_forecast import (
    DEFAULT_LOAD_W,
    LoadModel,
    SolarCorrection,
    average_kw,
    hour_of_week,
    parse_solar_forecast,
)
from custom_components.pixiihome_controller.control.savings import (
    SavingsTracker,
    grid_cost_sek,
)
from custom_components.pixiihome_controller.energy_forecast_service import (
    EnergyForecastConfig,
    learn_from_hourly_statistics,
)

# Wall-clock arithmetic in local time keeps the hour of week across DST.
MONDAY = datetime(2026, 10, 5, 8, 0, tzinfo=ZoneInfo("Europe/Stockholm"))


def _feed_days(model: LoadModel, first: datetime, days: int, load, temperature=None) -> None:
    """One sample per hour; ``load`` and ``temperature`` take (day, hour)."""
    for day in range(days):
        for hour in range(24):
            instant = first + timedelta(days=day, hours=hour)
            value = load(day, hour)
            temp = temperature(day, hour) if temperature else None
            assert model.update(instant, value, temp, hours=1.0)


def test_new_model_forecasts_a_flat_default() -> None:
    model = LoadModel()
    assert hour_of_week(MONDAY) == 8
    assert model.predict_w(MONDAY) == pytest.approx(DEFAULT_LOAD_W)
    assert model.predict_high_w(MONDAY) > model.predict_w(MONDAY)


def test_weekly_hours_use_the_median_and_keep_a_high_value() -> None:
    model = LoadModel()
    start = MONDAY.replace(hour=0)
    # Mondays at 08 use 3 kW every third week, otherwise 1 kW; other hours 500 W.
    _feed_days(model, start, 43, lambda day, hour: (
        (3000.0 if day % 21 == 0 else 1000.0) if day % 7 == 0 and hour == 8 else 500.0
    ))
    monday_8 = MONDAY + timedelta(days=42)
    assert model.predict_w(monday_8) == pytest.approx(1000.0, rel=0.25)
    assert model.predict_high_w(monday_8) > model.predict_w(monday_8)
    assert model.predict_w(monday_8 + timedelta(hours=3)) == pytest.approx(500.0)
    assert model.learned_days() >= 40


def test_cold_days_raise_the_forecast_through_the_daily_regression() -> None:
    model = LoadModel()
    start = MONDAY.replace(hour=0)
    # Load = 800 W + 100 W per degree below 17 °C, temperatures from 15 to -5 °C.
    _feed_days(
        model, start, 21,
        lambda day, hour: 800.0 + 100.0 * (17 - (15 - day)) if 15 - day < 17 else 800.0,
        lambda day, hour: 15.0 - day,
    )
    assert model.heating_w_per_c == pytest.approx(100.0, rel=0.05)
    later = start + timedelta(days=22, hours=12)
    assert model.predict_w(later, -10.0) > model.predict_w(later, 10.0) + 1500


def test_rejects_invalid_and_out_of_order_samples() -> None:
    model = LoadModel()
    assert not model.update(MONDAY, float("nan"))
    assert not model.update(MONDAY, -5.0)
    assert model.update(MONDAY, 500.0)
    assert not model.update(MONDAY - timedelta(minutes=15), 500.0)


def test_model_round_trips_and_rejects_corrupt_state() -> None:
    model = LoadModel()
    _feed_days(model, MONDAY.replace(hour=0), 3, lambda day, hour: 800.0 + day, lambda d, h: 5.0)
    restored = LoadModel.from_dict(model.as_dict())
    assert restored.predict_w(MONDAY) == pytest.approx(model.predict_w(MONDAY))
    assert restored.samples == model.samples
    assert restored.daily == model.daily
    fresh = LoadModel.from_dict({"version": 2, "hours": [1, 2]})
    assert fresh.samples == 0
    assert LoadModel.from_dict({"version": 1, "buckets": []}).samples == 0


def test_solcast_and_open_meteo_forecasts_are_parsed_and_averaged() -> None:
    start = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    solcast = {
        "detailedForecast": [
            {"period_start": (start + timedelta(minutes=30 * i)).isoformat(),
             "pv_estimate": 4.0, "pv_estimate10": 2.0, "pv_estimate90": 5.0}
            for i in range(4)
        ]
    }
    meteo = {"watts": {(start + timedelta(hours=3, minutes=15 * i)).isoformat(): 1000
                       for i in range(4)}}
    intervals = parse_solar_forecast([solcast, meteo, {"unrelated": 1}])
    assert len(intervals) == 8
    assert average_kw(intervals, start, start + timedelta(hours=1)) == pytest.approx(4.0)
    assert average_kw(
        intervals, start, start + timedelta(hours=1), conservative=True
    ) == pytest.approx(2.0)
    later = start + timedelta(hours=3)
    assert average_kw(intervals, later, later + timedelta(minutes=30)) == pytest.approx(1.0)
    assert average_kw(intervals, start - timedelta(hours=2), start) is None


def test_solar_correction_persists_shortfall_and_fades_surplus() -> None:
    correction = SolarCorrection()
    now = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    for minute in range(60):
        correction.add(now - timedelta(minutes=60 - minute), 2.0, 4.0)
    assert correction.ratio(now) == pytest.approx(0.5)
    assert correction.factor(now, now + timedelta(hours=5)) == pytest.approx(0.5)
    sunny = SolarCorrection()
    for minute in range(60):
        sunny.add(now - timedelta(minutes=60 - minute), 6.0, 4.0)
    assert sunny.factor(now, now) == pytest.approx(1.5)
    assert sunny.factor(now, now + timedelta(hours=3)) == pytest.approx(1.0)
    assert SolarCorrection().factor(now, now) == 1.0


def test_history_bootstrap_uses_grid_minus_battery_plus_solar() -> None:
    model = LoadModel()
    start = MONDAY.timestamp()
    rows = {
        "sensor.meter": [{"start": start, "mean": -500.0}],
        "sensor.battery": [{"start": start, "mean": 1000.0}],
        "sensor.solar": [{"start": start, "mean": 3000.0}],
    }
    used = learn_from_hourly_statistics(
        model, rows, "sensor.meter", "sensor.battery", "sensor.solar", None
    )
    assert used == 1
    # The hour closes when the next one starts.
    model.update(MONDAY + timedelta(hours=2), 0.0)
    assert model.hours[hour_of_week(MONDAY)][-1].load_w == pytest.approx(1500.0)
    # Hours without solar data are skipped when solar is configured.
    assert learn_from_hourly_statistics(
        LoadModel(), {"sensor.meter": rows["sensor.meter"], "sensor.battery": rows[
            "sensor.battery"]}, "sensor.meter", "sensor.battery", "sensor.solar", None,
    ) == 0


def test_forecast_config_validates_entity_domains() -> None:
    config = EnergyForecastConfig.from_mapping({
        "solar_forecast_entity_ids": ["sensor.today", "sensor.tomorrow", "sensor.today"],
        "solar_power_entity_id": "sensor.inverter_active_power",
        "temperature_entity_id": "weather.home",
    })
    assert config.solar_forecast_entity_ids == ("sensor.today", "sensor.tomorrow")
    with pytest.raises(ValueError):
        EnergyForecastConfig.from_mapping({"solar_power_entity_id": "switch.x"})
    assert EnergyForecastConfig.from_mapping(None) == EnergyForecastConfig()


def test_savings_compare_with_the_same_hour_without_battery() -> None:
    assert grid_cost_sek(1000, 1, 1.0, 0.5) == pytest.approx(1.5)
    assert grid_cost_sek(-1000, 1, 1.0, 0.5) == pytest.approx(-1.0)
    tracker = SavingsTracker()
    instant = datetime(2026, 10, 5, 16, 0, tzinfo=UTC)
    # Battery discharges 2 kW covering the house: grid 0 instead of 2 kW import.
    tracker.add(instant, 0.0, -2000.0, 2.0, 0.5, 0.25)
    assert tracker.today_sek == pytest.approx(2 * 0.25 * 2.5)
    finished = tracker.add(instant + timedelta(days=1), 0.0, 0.0, 2.0, 0.5, 0.25)
    assert finished == ("2026-10-05", pytest.approx(1.25))
    assert tracker.yesterday_sek == pytest.approx(1.25)
    restored = SavingsTracker.from_dict(tracker.as_dict())
    assert restored.history == tracker.history
