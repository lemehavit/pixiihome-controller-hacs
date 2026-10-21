"""Transport-free, bidirectional phase limits and short lived corrections.

Positive power charges. The controller never recreates a native activation.
References must describe today's live intent, not a remembered former command.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, sqrt

from .load_balancing import LoadBalancingConfig, PhaseSample, finite


@dataclass(frozen=True)
class PhasePlan:
    powers_w: tuple[int, ...]
    floors_w: tuple[float, ...]
    ceilings_w: tuple[float, ...]
    correction_required: bool
    duration_seconds: int


def plan_phases(
    config: LoadBalancingConfig,
    samples: tuple[PhaseSample, ...],
    desired_w: tuple[float, ...],
    *,
    usable_soc: float,
    charge_limit_w: float,
    discharge_limit_w: float,
    phase_limit_w: float,
    duration_seconds: int,
    previous_w: tuple[float, ...] | None = None,
    elapsed_seconds: float = 0,
) -> PhasePlan:
    """Clamp each phase and shared budgets, retaining required corrections.

    Reactive current is held constant for this estimate. RMS currents must
    still be checked after application. Refuse an impossible envelope rather
    than send a command that cannot fit the measured phase limits.
    """
    n = config.phases
    if len(samples) != n or len(desired_w) != n:
        raise ValueError("phase_data_missing")
    if not all(finite(x) for x in (*desired_w, usable_soc, charge_limit_w,
                                  discharge_limit_w, phase_limit_w, elapsed_seconds)):
        raise ValueError("phase_data_invalid")
    if not 0 <= usable_soc <= 100 or min(charge_limit_w, discharge_limit_w) < 0:
        raise ValueError("battery_limits_invalid")
    if phase_limit_w <= 0 or elapsed_seconds < 0:
        raise ValueError("phase_data_invalid")
    if type(duration_seconds) is not int or not 1 <= duration_seconds <= 15:
        raise ValueError("lease_duration_invalid")
    if previous_w is not None and (len(previous_w) != n or
                                  not all(finite(x) for x in previous_w)):
        raise ValueError("phase_data_invalid")
    charge = min(charge_limit_w, config.max_charge_kw * 1000)
    discharge = min(discharge_limit_w, config.max_discharge_kw * 1000)
    if usable_soc <= config.min_soc:
        discharge = 0
    if usable_soc >= 100:
        charge = 0
    bounds = []
    for s, fuse in zip(samples, config.fuses, strict=True):
        if not all(finite(x) for x in vars(s).values()):
            raise ValueError("phase_data_invalid")
        if not 80 <= s.voltage_v <= 300 or s.current_a < 0:
            raise ValueError("phase_data_invalid")
        apparent = s.voltage_v * s.current_a
        if abs(s.grid_power_w) > apparent + max(50, apparent * .1):
            raise ValueError("phase_measurements_inconsistent")
        q2 = max(0, apparent**2 - s.grid_power_w**2)
        capacity2 = ((fuse - config.margin_a) * s.voltage_v)**2 - q2
        if capacity2 < 0:
            raise ValueError("reactive_current_exceeds_limit")
        capacity = sqrt(capacity2)
        base = s.grid_power_w - s.battery_power_w
        # Whole watts are rounded inwards, never outside a protection bound.
        low = max(-capacity - base, -phase_limit_w, -discharge)
        high = min(capacity - base, phase_limit_w, charge)
        low, high = -floor(-low), floor(high)
        if low > high:
            raise ValueError("insufficient_capacity")
        bounds.append((low, high))
    if sum(max(0, lo) for lo, _ in bounds) > charge:
        raise ValueError("insufficient_charge_capacity")
    if sum(max(0, -hi) for _, hi in bounds) > discharge:
        raise ValueError("insufficient_discharge_capacity")
    targets = [max(lo, min(hi, want))
               for (lo, hi), want in zip(bounds, desired_w, strict=True)]
    if previous_w is not None:
        step = config.recovery_step_w * elapsed_seconds / config.poll_seconds
        for i, (old, want) in enumerate(zip(previous_w, desired_w, strict=True)):
            # Recovery is movement toward live intent in either direction.
            if abs(targets[i] - want) < abs(old - want):
                targets[i] = old + max(-step, min(step, targets[i] - old))
                targets[i] = max(bounds[i][0], min(bounds[i][1], targets[i]))
    # Remove discretionary power first. Never scale required support to zero.
    for sign, budget in ((1, charge), (-1, discharge)):
        excess = sum(max(0, sign * t) for t in targets) - budget
        for i in sorted(range(n), key=lambda j: sign * targets[j], reverse=True):
            if excess <= 0:
                break
            required = max(0, bounds[i][0] if sign == 1 else -bounds[i][1])
            removable = max(0, sign * targets[i] - required)
            remove = min(excess, removable)
            targets[i] -= sign * remove
            excess -= remove
        if excess > .01:
            raise ValueError("insufficient_capacity")
    powers = tuple(int(t) for t in targets)
    return PhasePlan(
        powers, tuple(lo for lo, _ in bounds), tuple(hi for _, hi in bounds),
        (any(abs(t - want) >= 1 for t, want in zip(powers, desired_w, strict=True))
         or any(not lo <= want <= hi for (lo, hi), want in
                zip(bounds, desired_w, strict=True))),
        duration_seconds,
    )


class RunDeadline:
    """Shorten a verified activation deadline; never extend an old activation."""

    def __init__(self) -> None:
        self._deadlines: dict[str, float] = {}

    def bind(self, identity: str, *, now: float, deadline: float) -> float:
        if (not isinstance(identity, str) or not identity or
                not finite(now) or not finite(deadline)):
            raise ValueError("active_mode_unverified")
        if identity not in self._deadlines and len(self._deadlines) >= 128:
            raise ValueError("mode_history_full")
        end = min(self._deadlines.get(identity, deadline), deadline)
        self._deadlines[identity] = end
        if floor(end - now) < 1:
            raise ValueError("mode_expired")
        return end
