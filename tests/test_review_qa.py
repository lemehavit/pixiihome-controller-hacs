"""QA regressions for the reviewed price and solar planning behaviour."""

import pytest

from custom_components.pixiihome_controller.control.whole_day_planner import SolarCharging

from .test_whole_day_planner import _plan, _series


@pytest.mark.parametrize(
    ("enabled", "expected_soc"),
    [(False, 70.0), (True, 80.0)],
)
def test_solar_surplus_obeys_the_switch_and_soc_cap(enabled, expected_soc) -> None:
    """F5: only automatic solar charging may raise the displayed SOC here."""
    result = _plan(
        _series([1.0] * 4), lambda _start, _end: (1.0, 3.0),
        usable_soc_percent=70.0,
        minimum_difference_sek_per_kwh=10.0,
        solar_charging=SolarCharging(enabled, 5.0, 80.0),
    )

    assert result.plan.actions == ()
    assert result.slots[-1].usable_soc_percent == pytest.approx(expected_soc)
    assert result.plan.maximum_projected_soc == pytest.approx(expected_soc)
    if enabled:
        assert any(slot.performed_by == "automatic_solar_charging" for slot in result.slots)
        assert all(slot.battery_kw <= 2.0 + 1e-9 for slot in result.slots)
    else:
        assert all(slot.battery_kw == 0 for slot in result.slots)


@pytest.mark.parametrize("initial_soc", [20.0, 80.0])
def test_filtered_price_actions_leave_soc_and_economics_unchanged(initial_soc) -> None:
    """F7: the visible plan describes only actions that would be written."""
    result = _plan(
        _series([1.10, 1.10, 0.80, 0.80]),
        lambda _start, _end: (1.0, 0.0),
        usable_soc_percent=initial_soc,
        grid_import_cost_sek_per_kwh=0.0,
    )

    assert result.plan.actions == ()
    assert all(slot.usable_soc_percent == pytest.approx(initial_soc) for slot in result.slots)
    assert result.planned_cost_sek == pytest.approx(result.baseline_cost_sek)
    assert result.plan.expected_margin_sek == pytest.approx(0.0)
