from __future__ import annotations

from math import inf, nan

import pytest

from custom_components.pixiihome_controller.battery import BatteryModel


def test_calculate_usable_soc_clamps_and_rejects_invalid_limits():
    model = BatteryModel()

    assert model.calculate_usable_soc(0, 10, 90) == 0
    assert model.calculate_usable_soc(100, 10, 90) == 100
    assert model.calculate_usable_soc(50, 90, 90) is None
    assert model.calculate_usable_soc(50, 100, 10) is None


def test_reading_prefers_gateway_native_usable_soc():
    reading = BatteryModel().reading(
        {
            "batt_soc": 89.85,
            "batt_soc_min": 88.2,
            "batt_cfg_soc_min": 10,
            "batt_soc_max": 91.5,
            "batt_cfg_soc_max": 90,
            "batt_soc_usable": 99.8125,
        },
        {},
    )

    assert reading.raw_soc == 89.85
    assert reading.usable_soc == 99.8125
    assert reading.usable_soc_source == "gateway_native"
    assert reading.effective_min_soc == 10
    assert reading.effective_max_soc == 90
    assert reading.reported_min_soc == 88.2
    assert reading.reported_max_soc == 91.5


def test_reading_falls_back_to_reserved_configured_limits():
    reading = BatteryModel().reading(
        {
            "batt_soc": 89.85,
            "batt_soc_min": 88.2,
            "batt_cfg_soc_min": 10,
            "batt_soc_max": 91.5,
            "batt_cfg_soc_max": 90,
        },
        {},
    )

    assert reading.raw_soc == 89.85
    assert reading.usable_soc == pytest.approx(99.8125)
    assert reading.usable_soc_source == "configured_limits"
    assert reading.effective_min_soc == 10
    assert reading.effective_max_soc == 90
    assert reading.reported_min_soc == 88.2
    assert reading.reported_max_soc == 91.5


def test_reading_fallback_clamps_below_and_above_reserved_limits():
    model = BatteryModel()

    below = model.reading(
        {"batt_soc": -10, "batt_cfg_soc_min": 10, "batt_cfg_soc_max": 90},
        {},
    )
    above = model.reading(
        {"batt_soc": 110, "batt_cfg_soc_min": 10, "batt_cfg_soc_max": 90},
        {},
    )

    assert below.usable_soc == 0
    assert above.usable_soc == 100


def test_reading_does_not_use_reported_soc_bounds_for_fallback():
    reading = BatteryModel().reading(
        {
            "batt_soc": 50,
            "batt_soc_min": 5,
            "batt_soc_max": 95,
        },
        {},
    )

    assert reading.raw_soc == 50
    assert reading.usable_soc is None
    assert reading.effective_min_soc is None
    assert reading.effective_max_soc is None
    assert reading.reported_min_soc == 5
    assert reading.reported_max_soc == 95


def test_reading_keeps_raw_soc_when_reserved_limits_are_missing():
    reading = BatteryModel().reading({"batt_soc": 50}, {})

    assert reading.raw_soc == 50
    assert reading.usable_soc is None
    assert reading.usable_soc_source is None


def test_reading_rejects_inverted_or_equal_reserved_limits():
    model = BatteryModel()

    inverted = model.reading(
        {"batt_soc": 50, "batt_cfg_soc_min": 90, "batt_cfg_soc_max": 10},
        {},
    )
    equal = model.reading(
        {"batt_soc": 50, "batt_cfg_soc_min": 50, "batt_cfg_soc_max": 50},
        {},
    )

    assert inverted.usable_soc is None
    assert equal.usable_soc is None


def test_reading_rejects_malformed_and_nonfinite_usable_inputs():
    reading = BatteryModel().reading(
        {
            "batt_soc": 50,
            "batt_soc_usable": "not-a-number",
            "batt_cfg_soc_min": nan,
            "batt_cfg_soc_max": inf,
        },
        {},
    )

    assert reading.raw_soc == 50
    assert reading.usable_soc is None
    assert reading.usable_soc_source is None
