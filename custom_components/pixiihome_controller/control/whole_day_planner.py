"""Whole-horizon battery plan by dynamic programming over stored energy.

Unlike the interval-by-interval rule in ``price_optimization``, this planner
weighs every price, the household load forecast and the solar forecast at the
same time. Its output uses the same ``PricePlan`` shape,
so it can be shown next to the standard plan or reconciled the same way.

Economics per interval, with battery AC power ``b`` (+ charge, − discharge)
and forecast net load ``n`` = load − solar:

* grid = n + b; import costs price + variable import cost, export earns price;
* energy charged from the grid must earn the minimum net profit, so each such
  kWh carries that profit as a decision cost;
* battery energy exported beyond house load must additionally reach the export
  minimum; every discharged kWh pays the cycle cost;
* without export permission the battery never discharges beyond house load.

Known external Target SOC windows reset stored energy to their target; the
missing energy is bought at the window's price. Adjustable windows are
planned as free time: their target is lowered to the planned level, or the
window is deleted when the plan uses its time. Energy left at the end is valued at the
horizon's mean import price after discharge losses.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import ceil, floor, isfinite, sqrt

from .price_optimization import (
    ExternalWindow,
    PriceDirection,
    PriceDischargeMode,
    PriceInterval,
    PricePlan,
    PricePlanAction,
    PriceSeries,
)

ENERGY_LEVELS = 100


@dataclass(frozen=True, slots=True)
class WholeDaySlot:
    """Planned battery flow for one price interval (diagnostics)."""

    start: datetime
    end: datetime
    price_sek_per_kwh: float
    load_kw: float
    solar_kw: float
    battery_kw: float
    usable_soc_percent: float
    blocked: bool

    @property
    def kind(self) -> str:
        surplus = max(0.0, self.solar_kw - self.load_kw)
        if self.blocked:
            return "external"
        if self.battery_kw > 0.05:
            return "solar" if self.battery_kw <= surplus + 0.05 else "grid_charge"
        if self.battery_kw < -0.05:
            return (
                "self_consumption"
                if -self.battery_kw <= max(0.0, self.load_kw - self.solar_kw) + 0.05
                else "export"
            )
        return "idle"

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "price": round(self.price_sek_per_kwh, 4),
            "load_kw": round(self.load_kw, 2),
            "solar_kw": round(self.solar_kw, 2),
            "battery_kw": round(self.battery_kw, 2),
            "soc": round(self.usable_soc_percent, 1),
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class WholeDayResult:
    plan: PricePlan
    slots: tuple[WholeDaySlot, ...]
    baseline_cost_sek: float
    planned_cost_sek: float


def build_whole_day_plan(
    series: PriceSeries,
    *,
    now: datetime,
    horizon_hours: int,
    minimum_lead_minutes: int,
    usable_soc_percent: float,
    usable_capacity_kwh: float,
    minimum_soc_percent: float,
    maximum_soc_percent: float,
    maximum_charge_kw: float,
    maximum_discharge_kw: float,
    minimum_difference_sek_per_kwh: float,
    export_allowed: bool,
    export_minimum_difference_sek_per_kwh: float,
    round_trip_efficiency_percent: float,
    cycle_cost_sek_per_kwh: float,
    grid_import_cost_sek_per_kwh: float,
    forecast: Callable[[datetime, datetime], tuple[float, float] | None],
    end_before: datetime | None = None,
    external_windows: Sequence[ExternalWindow] = (),
    levels: int = ENERGY_LEVELS,
) -> WholeDayResult:
    """Plan the cheapest battery trajectory; ``forecast`` gives (load_kw, solar_kw)."""
    if now.tzinfo is None:
        raise ValueError("price_plan_now_naive")
    if not 0 <= minimum_soc_percent < maximum_soc_percent <= 100:
        raise ValueError("price_soc_limits_invalid")
    if usable_capacity_kwh <= 0 or maximum_charge_kw < 0 or maximum_discharge_kw < 0:
        raise ValueError("price_power_or_capacity_invalid")
    if not 0 < round_trip_efficiency_percent <= 100:
        raise ValueError("price_efficiency_or_cycle_cost_invalid")
    current = now.astimezone(UTC)
    start_limit = current + timedelta(minutes=minimum_lead_minutes)
    end_limit = current + timedelta(hours=horizon_hours)
    if end_before is not None:
        end_limit = min(end_limit, end_before.astimezone(UTC))
    windows = sorted(
        (window for window in external_windows if window.end > current),
        key=lambda window: window.start,
    )
    for window in windows:
        if not window.known and not window.adjustable:
            end_limit = min(end_limit, max(window.start, current))
            break
    windows = [
        window for window in windows
        if (window.known or window.adjustable) and window.start < end_limit
    ]
    candidates = tuple(
        item for item in series.intervals if item.start >= start_limit and item.end <= end_limit
    )
    if not candidates:
        return _blocked(series, "future_price_horizon_empty", usable_soc_percent)
    if any(a.end != b.start for a, b in zip(candidates, candidates[1:], strict=False)):
        return _blocked(series, "future_price_horizon_incomplete", usable_soc_percent)
    flows: list[tuple[float, float]] = []
    for item in candidates:
        value = forecast(item.start, item.end)
        if value is None or not all(isfinite(part) and part >= 0 for part in value):
            return _blocked(series, "energy_forecast_unavailable", usable_soc_percent)
        flows.append(value)

    eta = sqrt(round_trip_efficiency_percent / 100)
    rt = round_trip_efficiency_percent / 100
    gic = grid_import_cost_sek_per_kwh
    step = usable_capacity_kwh / levels
    idx_min = min(levels, max(0, ceil(minimum_soc_percent / 100 * levels - 1e-9)))
    idx_max = min(levels, max(0, floor(maximum_soc_percent / 100 * levels + 1e-9)))
    start_index = min(levels, max(0, round(usable_soc_percent / 100 * levels)))
    export_extra = max(0.0, export_minimum_difference_sek_per_kwh - minimum_difference_sek_per_kwh)

    count = len(candidates)
    blocked = [
        any(w.start < item.end and item.start < w.end for w in windows if not w.adjustable)
        for item in candidates
    ]
    # reset[k] = (target index, refill price) applied at the start of slot k.
    resets: dict[int, tuple[int, float]] = {}
    for window in windows:
        if window.adjustable:
            continue
        assert window.target_usable_soc_percent is not None
        k = next((i for i, item in enumerate(candidates) if item.start >= window.end), count)
        inside = [item.price_sek_per_kwh for item in series.intervals
                  if item.start < window.end and window.start < item.end]
        fallback = candidates[min(k, count - 1)].price_sek_per_kwh
        price = sum(inside) / len(inside) if inside else fallback
        target = min(levels, max(0, round(window.target_usable_soc_percent / 100 * levels)))
        resets[k] = (target, price)

    mean_price = sum(item.price_sek_per_kwh for item in candidates) / count
    terminal_value = max(0.0, (mean_price + gic) * eta - cycle_cost_sek_per_kwh)

    def slot_cost(index: int, delta: int, *, decision: bool) -> tuple[float, float]:
        """(cost, battery_kw) of moving ``delta`` energy levels in slot ``index``."""
        item = candidates[index]
        hours = item.duration_hours
        load, solar = flows[index]
        net = load - solar
        if delta > 0:
            battery = delta * step / (eta * hours)
        elif delta < 0:
            battery = delta * step * eta / hours
        else:
            battery = 0.0
        grid = net + battery
        price = item.price_sek_per_kwh
        cost = grid * hours * (price + gic) if grid > 0 else grid * hours * price
        if battery < 0:
            cost += -battery * hours * cycle_cost_sek_per_kwh
        if decision:
            if battery > 0:
                cost += max(0.0, battery - max(0.0, -net)) * hours * rt * (
                    minimum_difference_sek_per_kwh
                )
            elif battery < 0:
                cost += max(0.0, -battery - max(0.0, net)) * hours * export_extra
        return cost, battery

    moves: list[list[tuple[int, float]]] = []
    for index, item in enumerate(candidates):
        if blocked[index]:
            moves.append([(0, 0.0)])
            continue
        hours = item.duration_hours
        load, solar = flows[index]
        up = floor(maximum_charge_kw * hours * eta / step + 1e-9)
        discharge_kw = maximum_discharge_kw
        if not export_allowed:
            discharge_kw = min(discharge_kw, max(0.0, load - solar))
        down = floor(discharge_kw * hours / eta / step + 1e-9)
        moves.append([
            (delta, slot_cost(index, delta, decision=True)[0]) for delta in range(-down, up + 1)
        ])

    infinity = float("inf")
    value = [
        -terminal_value * max(0, level - idx_min) * step for level in range(levels + 1)
    ]
    choice: list[list[int]] = [[0] * (levels + 1) for _ in range(count)]
    for index in range(count - 1, -1, -1):
        if index + 1 in resets:
            target, price = resets[index + 1]
            after = value[target]
            value = [
                after + max(0, target - level) * step / eta * (price + gic)
                for level in range(levels + 1)
            ]
        updated = [infinity] * (levels + 1)
        for level in range(levels + 1):
            ceiling = max(level, idx_max)
            floor_level = min(level, idx_min)
            best = infinity
            best_delta = 0
            for delta, cost in moves[index]:
                nxt = level + delta
                if nxt < 0 or nxt > levels:
                    continue
                if delta > 0 and nxt > ceiling:
                    continue
                if delta < 0 and nxt < floor_level:
                    continue
                total = cost + value[nxt]
                if total < best - 1e-12:
                    best, best_delta = total, delta
            updated[level] = best
            choice[index][level] = best_delta
        value = updated

    level = resets[0][0] if 0 in resets else start_index
    slots: list[WholeDaySlot] = []
    actions: list[PricePlanAction] = []
    baseline = planned = 0.0
    charged_energy = charged_cost = 0.0
    margin_total = 0.0
    first_energy = level * step
    window_socs: dict[datetime, float] = {}
    for index, item in enumerate(candidates):
        for window in windows:
            if window.adjustable and window.start not in window_socs and (
                window.start <= item.start
            ):
                window_socs[window.start] = level * step / usable_capacity_kwh * 100
        delta = choice[index][level]
        cost, battery = slot_cost(index, delta, decision=False)
        base_cost, _ = slot_cost(index, 0, decision=False)
        baseline += base_cost
        planned += cost
        level += delta
        if index + 1 in resets:
            # The external Target SOC window that ends here sets the energy.
            level = resets[index + 1][0]
        load, solar = flows[index]
        soc = level * step / usable_capacity_kwh * 100
        slots.append(WholeDaySlot(
            item.start, item.end, item.price_sek_per_kwh, load, solar, battery, soc,
            blocked[index],
        ))
        margin = base_cost - cost
        margin_total += margin
        action = _action(
            item, load, solar, battery, soc, margin,
            maximum_discharge_kw=maximum_discharge_kw,
            step_kw=step / (eta * item.duration_hours),
            reference=(charged_cost / charged_energy) if charged_energy > 0 else None,
        )
        net = load - solar
        if battery > max(0.0, -net) + 0.05:
            grid_part = (battery - max(0.0, -net)) * item.duration_hours
            charged_energy += grid_part
            charged_cost += grid_part * item.price_sek_per_kwh
        if action is not None:
            actions.append(action)
    margin_total += terminal_value * (level * step - first_energy)
    for window in windows:
        if window.adjustable:
            window_socs.setdefault(window.start, level * step / usable_capacity_kwh * 100)
    socs = [usable_soc_percent, *(slot.usable_soc_percent for slot in slots)]
    payload = {
        "series": series.fingerprint,
        "engine": "whole_day_dp_v1",
        "actions": [action.as_dict() for action in actions],
        "held": sorted(w.start.isoformat() for w in windows if w.adjustable),
    }
    fingerprint = sha256(repr(payload).encode()).hexdigest()
    plan = PricePlan(
        tuple(actions), (), fingerprint, min(socs), max(socs), margin_total,
        tuple(sorted(window_socs.items())),
    )
    return WholeDayResult(plan, tuple(slots), baseline, planned)


def _action(
    item: PriceInterval,
    load: float,
    solar: float,
    battery: float,
    soc: float,
    margin: float,
    *,
    maximum_discharge_kw: float,
    step_kw: float,
    reference: float | None,
) -> PricePlanAction | None:
    net = load - solar
    hours = item.duration_hours
    price = item.price_sek_per_kwh
    if battery > max(0.0, -net) + 0.05:
        power = round(battery, 1)
        if power < 0.1:
            return None
        return PricePlanAction(
            item.start, item.end, PriceDirection.CHARGE, power, "demandresponse",
            {"pacref": round(power * 1000, 3)}, None, price, price, 0.0, soc, margin,
            margin / (battery * hours),
        )
    if battery < -0.05:
        delivered = -battery * hours
        basis = reference if reference is not None else price
        # Less than one energy step beyond the house load is rounding in the
        # energy grid, not a planned export: follow the house load instead of
        # forcing a fixed export power.
        if -battery <= max(0.0, net) + max(0.05, step_kw):
            # Following the whole house load: let the battery cover it as it
            # varies. Covering only part of it: cap the discharge.
            power = (
                maximum_discharge_kw if -battery >= 0.9 * max(0.0, net) - 0.05
                else round(-battery, 1)
            )
            if power < 0.1:
                return None
            return PricePlanAction(
                item.start, item.end, PriceDirection.DISCHARGE, power, "peakshaving",
                {
                    "exportlim": 0.0,
                    "importlim": 0.0,
                    "charge_max": 0.0,
                    "discharge_max": round(power * 1000, 3),
                    "keep_active": True,
                },
                PriceDischargeMode.SELF_CONSUMPTION, price, basis, price - basis, soc, margin,
                margin / delivered,
            )
        power = round(-battery, 1)
        return PricePlanAction(
            item.start, item.end, PriceDirection.DISCHARGE, power, "demandresponse",
            {"pacref": round(-power * 1000, 3)}, PriceDischargeMode.EXPORT, price, basis,
            price - basis, soc, margin, margin / delivered,
        )
    return None


def _blocked(series: PriceSeries, blocker: str, soc: float) -> WholeDayResult:
    plan = PricePlan((), (blocker,), series.fingerprint, soc, soc, 0.0)
    return WholeDayResult(plan, (), 0.0, 0.0)


__all__ = ["WholeDayResult", "WholeDaySlot", "build_whole_day_plan"]
