"""Protection calculations, shared capacity and expiry behavior."""

from typing import Any

import pytest

from custom_components.pixiihome_controller.control.load_balancing import (
    LoadBalancingConfig,
    PhaseSample,
)
from custom_components.pixiihome_controller.control.phase_balancing import RunDeadline, plan_phases


def phase(grid, battery=0):
    return PhaseSample(abs(grid) / 230, 230, grid, battery)


def plan(samples, desired=(0, 0, 0), **kwargs):
    options: dict[str, Any] = dict(
        usable_soc=50,
        charge_limit_w=10000,
        discharge_limit_w=10000,
        phase_limit_w=3335,
        duration_seconds=12,
    )
    return plan_phases(
        LoadBalancingConfig(fuse_l1=20, fuse_l2=20, fuse_l3=20),
        samples,
        desired,
        **(options | kwargs),
    )


def test_import_and_export_correct_opposite_phases_individually():
    result = plan((phase(5000), phase(-5000), phase(500)))
    assert result.powers_w == (-860, 860, 0)
    assert result.correction_required
    for p, battery in zip((5000, -5000, 500), result.powers_w, strict=True):
        assert abs(p + battery) <= 18 * 230


@pytest.mark.parametrize("sign", (-1, 1))
def test_reduce_current_mode_before_reversing_direction(sign):
    result = plan(tuple(phase(sign * 5000, sign * 2000) for _ in range(3)), (sign * 2000,) * 3)
    assert result.powers_w == (sign * 1140,) * 3


def test_usable_reserve_disallows_discharge_but_still_reduces_charging():
    assert plan((phase(5000, 1000), phase(0), phase(0)), (1000, 0, 0), usable_soc=10).powers_w == (
        140,
        0,
        0,
    )
    with pytest.raises(ValueError, match="insufficient_capacity"):
        plan((phase(5000), phase(0), phase(0)), usable_soc=10)


def test_full_battery_cannot_absorb_solar_overload():
    with pytest.raises(ValueError, match="insufficient_capacity"):
        plan((phase(-5000), phase(0), phase(0)), usable_soc=100)


def test_shared_budget_preserves_support_on_overloaded_phase():
    result = plan((phase(5000), phase(0), phase(0)), (-2000, -2000, -2000), discharge_limit_w=1000)
    assert result.powers_w[0] <= -860
    assert sum(-min(0, p) for p in result.powers_w) <= 1000


def test_combined_required_support_cannot_exceed_shared_budget():
    with pytest.raises(ValueError, match="insufficient_discharge_capacity"):
        plan((phase(5000), phase(5000), phase(0)), discharge_limit_w=1000)


@pytest.mark.parametrize("sign", (-1, 1))
def test_recovery_is_slow_in_both_directions_and_new_limit_immediate(sign):
    result = plan(
        (phase(0),) * 3, (sign * 3000,) * 3, previous_w=(sign * 1000,) * 3, elapsed_seconds=5
    )
    assert result.powers_w == (sign * 1500,) * 3
    tight = plan(
        (phase(sign * 5000, sign * 2000),) * 3,
        (sign * 3000,) * 3,
        previous_w=(sign * 2000,) * 3,
        elapsed_seconds=0,
    )
    assert tight.powers_w == (sign * 1140,) * 3


@pytest.mark.parametrize(
    "sample,reason",
    (
        (PhaseSample(25, 230, 0, 0), "reactive_current_exceeds_limit"),
        (PhaseSample(1, 230, 3000, 0), "phase_measurements_inconsistent"),
        (PhaseSample(float("nan"), 230, 0, 0), "phase_data_invalid"),
    ),
)
def test_invalid_or_uncontrollable_measurements_are_rejected(sample, reason):
    with pytest.raises(ValueError, match=reason):
        plan((sample, phase(0), phase(0)))


def test_delayed_read_cannot_extend_old_activation_and_new_activation_is_distinct():
    guard = RunDeadline()
    assert guard.bind("102:7:occurrenceA", now=100, deadline=120) == 120
    assert guard.bind("102:7:occurrenceA", now=110, deadline=900) == 120
    with pytest.raises(ValueError, match="mode_expired"):
        guard.bind("102:7:occurrenceA", now=120, deadline=900)
    assert guard.bind("102:7:occurrenceB", now=120, deadline=140) == 140
