"""Whole-horizon battery plan by dynamic programming over stored energy.

Unlike the interval-by-interval rule in ``price_optimization``, this planner
weighs every price, the household load forecast and the solar forecast at the
same time. Its output uses the same ``PricePlan`` shape,
so it can be shown next to the standard plan or reconciled the same way.

Economics per interval, with battery AC power ``b`` (+ charge, − discharge)
and forecast net load ``n`` = load − solar:

* grid = n + b; import costs price + variable import cost, export earns price;
* every discharged kWh must earn the minimum net profit, so it carries that
  profit as a decision cost (charges carry none: they pay off only through the
  discharges they make possible, the same cycle the written-action filter checks);
* battery energy exported beyond house load must additionally reach the export
  minimum; every discharged kWh pays the cycle cost;
* without export permission the battery never discharges beyond house load.

Known external Target SOC windows reset stored energy to their target; the
missing energy is bought at the window's price. Adjustable windows are
planned as free time: their target is lowered to the planned level, or the
window is deleted when the plan uses its time. Energy left at the end is valued at the
horizon's mean import price after discharge losses; a discharge that no
charge or refill follows is checked against that same value.

Automatic solar charging is not a choice of the plan: when no written
schedule takes over, the solar controller charges whatever surplus it is
allowed to (after its export buffer). The plan counts on that flow and only
chooses what it can write. A written charge pays the import cost only for
the part that really comes from the grid. The number of energy levels is
chosen from capacity, interval length and power, so small flows stay visible.
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
# Finest grid used when small flows need it, and a bound on DP work so a plan
# stays quick on small Home Assistant hosts.
MAXIMUM_ENERGY_LEVELS = 400
_WORK_BUDGET = 1_600_000
# Without export, discharge follows the house load: resolve loads this small.
_HOUSE_LOAD_RESOLUTION_KW = 0.25
# Times the plan is made again after the minimum-profit filter removed a
# discharge that a planned charge was bought for.
_REPLAN_LIMIT = 3

PERFORMED_BY_SOLAR = "automatic_solar_charging"
PERFORMED_BY_PRICE = "price_schedule"
PERFORMED_BY_EXTERNAL = "external_schedule"
PERFORMED_BY_NONE = "none"


@dataclass(frozen=True, slots=True)
class SolarCharging:
    """What automatic solar charging really does, without any schedule.

    Only charging inside these limits is free and needs no written action.
    While smart solar charging defers, intervals before ``defer_until`` priced
    at ``defer_from_price`` or more get no solar charging (it exports then).
    Solar charging never starts on a surplus below ``minimum_surplus_kw``.
    The controller leaves ``export_buffer_kw`` to the grid, so only the
    surplus above the buffer can be stored.
    """

    enabled: bool
    maximum_kw: float
    maximum_soc_percent: float
    defer_from_price: float | None = None
    defer_until: datetime | None = None
    minimum_surplus_kw: float = 0.0
    export_buffer_kw: float = 0.0

    def defers(self, item: PriceInterval) -> bool:
        return (
            self.defer_from_price is not None
            and self.defer_until is not None
            and item.start < self.defer_until
            and item.price_sek_per_kwh >= self.defer_from_price
        )


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
    # Solar charging that runs without a schedule in this interval (AC kW).
    free_solar_kw: float
    # Who carries the flow out: automatic_solar_charging, price_schedule,
    # external_schedule or none.
    performed_by: str

    @property
    def kind(self) -> str:
        if self.blocked:
            return "external"
        if self.battery_kw > 0.05:
            return "solar" if self.battery_kw <= self.free_solar_kw + 0.05 else "grid_charge"
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
            "performed_by": self.performed_by,
        }


@dataclass(frozen=True, slots=True)
class WholeDayResult:
    plan: PricePlan
    slots: tuple[WholeDaySlot, ...]
    baseline_cost_sek: float
    planned_cost_sek: float
    # Energy grid used, and whether some allowed flow was still too small for
    # one level (the plan then leaves it out).
    energy_levels: int = ENERGY_LEVELS
    resolution_limited: bool = False


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
    levels: int | None = None,
    solar_charging: SolarCharging | None = None,
    _no_discharge: frozenset[datetime] = frozenset(),
    _replans: int = 0,
) -> WholeDayResult:
    """Plan the cheapest battery trajectory; ``forecast`` gives (load_kw, solar_kw).

    ``solar_charging`` describes the solar controller: the plan counts on its
    charging whenever no written action takes over, since nothing would stop
    it. ``None`` treats the whole surplus as optional free charging. Charging
    beyond it is a written charge that must earn the minimum profit.
    ``levels`` fixes the energy grid; ``None`` chooses it automatically.

    A discharge the minimum-profit filter leaves out is not written, but the
    charge planned for it would still be. The plan is then made again without
    a discharge in those intervals (``_no_discharge``, internal), a few times
    at most, so no charge is bought for a discharge that never runs.
    """
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
    if levels is None:
        levels = _energy_levels(
            candidates, flows, usable_capacity_kwh, maximum_charge_kw,
            maximum_discharge_kw, export_allowed, eta,
        )
    step = usable_capacity_kwh / levels
    idx_min = min(levels, max(0, ceil(minimum_soc_percent / 100 * levels - 1e-9)))
    idx_max = min(levels, max(0, floor(maximum_soc_percent / 100 * levels + 1e-9)))
    start_index = min(levels, max(0, round(usable_soc_percent / 100 * levels)))
    export_extra = max(0.0, export_minimum_difference_sek_per_kwh - minimum_difference_sek_per_kwh)
    # Solar charging that runs on its own, per interval (AC kW), up to idx_sol.
    solar_room: list[float] = []
    for index, item in enumerate(candidates):
        load, solar = flows[index]
        surplus = max(0.0, solar - load)
        if solar_charging is None:
            solar_room.append(surplus)
        elif (
            not solar_charging.enabled
            or solar_charging.defers(item)
            or surplus < solar_charging.minimum_surplus_kw
        ):
            solar_room.append(0.0)
        else:
            # The start threshold looks at the raw surplus; what can be stored
            # is the surplus the export buffer leaves.
            storable = max(0.0, surplus - max(0.0, solar_charging.export_buffer_kw))
            solar_room.append(min(storable, max(0.0, solar_charging.maximum_kw)))
    idx_sol = levels if solar_charging is None else min(levels, max(0, floor(
        solar_charging.maximum_soc_percent / 100 * levels + 1e-9
    )))

    def free_solar_kw(index: int, energy: float) -> float:
        """Solar charging (AC kW) that needs no schedule from stored ``energy``."""
        room = solar_room[index]
        if solar_charging is None or room <= 0:
            return room
        headroom = max(0.0, idx_sol * step - energy)
        return min(room, headroom / (eta * candidates[index].duration_hours))

    # A real solar controller charges on its own whenever nothing written
    # takes over; the plan cannot choose to wait or to take only part of it.
    passive_solar = solar_charging is not None

    def passive_steps(index: int, energy: float) -> int:
        """Energy levels the solar controller adds by itself from ``energy``."""
        if not passive_solar or blocked[index]:
            return 0
        hours = candidates[index].duration_hours
        return max(0, floor(free_solar_kw(index, energy) * eta * hours / step + 1e-9))

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
    # What a discharged kWh really costs: when a fixed aggregator Target SOC
    # window follows, it is bought back there; otherwise it is what the
    # plan's own grid charges paid, or what energy left at the end is worth.
    next_refill: list[float | None] = []
    upcoming: float | None = None
    for index in range(count - 1, -1, -1):
        if index + 1 in resets:
            upcoming = resets[index + 1][1]
        next_refill.append(upcoming)
    next_refill.reverse()
    # Energy kept at the end is worth what it would earn later (the mean
    # price after losses), but never more than buying it back at the
    # horizon's cheapest price would cost: a full battery is not kept for its
    # own sake when it can be refilled more cheaply.
    cheapest = min(item.price_sek_per_kwh for item in candidates)
    terminal_value = max(0.0, min(
        (mean_price + gic) * eta - cycle_cost_sek_per_kwh,
        (cheapest + gic) / eta,
    ))
    # Energy already stored, with no charge or refill planned for it, costs
    # what the plan values it at when kept (``terminal_value`` per stored kWh).
    # As a price basis: (basis + gic) / rt + cycle == terminal / eta + cycle.
    stored_basis = terminal_value * eta - gic

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
        # A charge's minimum-profit hurdle depends on the stored energy (the
        # solar SOC cap), so the DP adds it per level.
        # The minimum profit is a hurdle on what a discharge delivers, the same
        # thing the written-action filter checks. Charges carry none of their
        # own: they only pay off through the discharges they make possible.
        if decision and battery < 0:
            cost += -battery * hours * minimum_difference_sek_per_kwh
            cost += max(0.0, -battery - max(0.0, net)) * hours * export_extra
        return cost, battery

    moves: list[list[tuple[int, float, float]]] = []
    ups: list[int] = []
    resolution_limited = False
    for index, item in enumerate(candidates):
        if blocked[index]:
            moves.append([(0, 0.0, 0.0)])
            ups.append(0)
            continue
        hours = item.duration_hours
        load, solar = flows[index]
        up = floor(maximum_charge_kw * hours * eta / step + 1e-9)
        discharge_kw = maximum_discharge_kw
        if not export_allowed:
            discharge_kw = min(discharge_kw, max(0.0, load - solar))
        if item.start in _no_discharge:
            discharge_kw = 0.0
        down = floor(discharge_kw * hours / eta / step + 1e-9)
        # The house load is its own flow also when export is allowed.
        house_kw = min(maximum_discharge_kw, max(0.0, load - solar))
        if item.start in _no_discharge:
            house_kw = 0.0
        house_down = floor(house_kw * hours / eta / step + 1e-9)
        if (
            (up == 0 and maximum_charge_kw >= 0.1)
            or (down == 0 and discharge_kw >= 0.1)
            or (house_down == 0 and house_kw >= _HOUSE_LOAD_RESOLUTION_KW)
        ):
            resolution_limited = True
        ups.append(up)
        moves.append([
            (delta, *slot_cost(index, delta, decision=True)) for delta in range(-down, up + 1)
        ])

    infinity = float("inf")
    # Energy kept at the end is worth ``terminal_value``. Energy beyond what
    # the battery holds now was bought for nothing but keeping: it is worth
    # less by the minimum profit, so the plan never charges just to end fuller.
    base_level = max(idx_min, resets[0][0] if 0 in resets else start_index)
    bought_value = max(0.0, terminal_value - eta * minimum_difference_sek_per_kwh)
    value = [
        -(terminal_value * max(0, min(level, base_level) - idx_min)
          + bought_value * max(0, level - base_level)) * step
        for level in range(levels + 1)
    ]
    choice: list[list[int]] = [[0] * (levels + 1) for _ in range(count)]
    for index in range(count - 1, -1, -1):
        if index + 1 in resets:
            target, price = resets[index + 1]
            after = value[target]
            # The refill is bought at the window's price; the discharge that
            # emptied the battery already carries the minimum-profit hurdle.
            value = [
                after + max(0, target - level) * step * (price + gic) / eta
                for level in range(levels + 1)
            ]
        updated = [infinity] * (levels + 1)
        hours = candidates[index].duration_hours
        for level in range(levels + 1):
            ceiling = max(level, idx_max)
            floor_level = min(level, idx_min)
            free = free_solar_kw(index, level * step)
            passive = passive_steps(index, level * step)
            options = moves[index]
            if passive > ups[index]:
                options = [*options, (passive, *slot_cost(index, passive, decision=True))]
            best = infinity
            best_delta = 0
            for delta, cost, battery in options:
                if passive and 0 <= delta < passive:
                    # Holding back or taking only part of the solar charging
                    # needs a command nobody writes: it is not an option.
                    continue
                nxt = level + delta
                if nxt < 0 or nxt > levels:
                    continue
                if delta > 0 and nxt > ceiling and delta != passive:
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
    # Where the plan itself buys energy from the grid, so a discharge before a
    # planned charge is valued at that charge price, not at an average.
    grid_charge: list[bool] = []
    charge_import_share: list[float] = []
    probe = level
    for index in range(count):
        delta = choice[index][probe]
        _, battery = slot_cost(index, delta, decision=False)
        free = free_solar_kw(index, probe * step)
        grid_charge.append(battery > free + 0.05)
        charge_import_share.append(_import_share(battery, free, flows[index]))
        probe += delta
        if index + 1 in resets:
            probe = resets[index + 1][0]
    planned_refill: list[tuple[float, float] | None] = []
    upcoming_charge: tuple[float, float] | None = None
    for index in range(count - 1, -1, -1):
        if index + 1 in resets:
            upcoming_charge = None
        planned_refill.append(upcoming_charge)
        if grid_charge[index]:
            upcoming_charge = (candidates[index].price_sek_per_kwh, charge_import_share[index])
    planned_refill.reverse()
    slots: list[WholeDaySlot] = []
    actions: list[PricePlanAction] = []
    baseline = planned = 0.0
    charged_energy = charged_cost = charged_import = 0.0
    first_energy = level * step
    # The energy the battery really holds when only the written actions run: a
    # discharge kept back by the minimum profit stays in the battery. Lowered
    # aggregator targets follow this, never the unwritten trajectory, or the
    # aggregator's Target SOC would empty the battery for the plan.
    kept = first_energy
    baseline_energy = first_energy
    window_socs: dict[datetime, float] = {}
    filtered: set[datetime] = set()
    for index, item in enumerate(candidates):
        for window in windows:
            if window.adjustable and window.start not in window_socs and (
                window.start <= item.start
            ):
                window_socs[window.start] = kept / usable_capacity_kwh * 100
        delta = choice[index][level]
        # A filtered discharge can leave more energy than DP expected. A later
        # charge must fit the room that remains in the actual battery.
        actual_level = round(kept / step)
        candidate_delta = (
            min(delta, max(0, idx_max - actual_level)) if delta > 0
            else min(0, max(delta, idx_min - actual_level))
        )
        # A charge DP left to solar charging stays solar charging: it stops at
        # the solar SOC cap of the actual battery, never becoming a schedule.
        if delta > 0 and slot_cost(index, delta, decision=False)[1] <= (
            free_solar_kw(index, level * step) + 0.05
        ):
            solar_steps = floor(
                free_solar_kw(index, kept) * eta * item.duration_hours / step + 1e-9
            )
            candidate_delta = min(candidate_delta, max(0, idx_sol - actual_level), solar_steps)
        elif delta > 0:
            # A written charge only buys up to the level DP planned: energy a
            # filtered discharge left in the battery needs no refill.
            candidate_delta = min(candidate_delta, max(0, level + delta - actual_level))
        _, battery = slot_cost(index, candidate_delta, decision=False)
        base_cost, _ = slot_cost(index, 0, decision=False)
        baseline += base_cost
        level += delta
        load, solar = flows[index]
        free = free_solar_kw(index, kept)
        projected_soc = min(levels * step, max(0.0, kept + candidate_delta * step))
        projected_soc = projected_soc / usable_capacity_kwh * 100
        refill = planned_refill[index]
        fixed_refill = next_refill[index]
        if refill is None and fixed_refill is not None:
            refill = (fixed_refill, 1.0)
        reference, import_share = _basis(
            refill, charged_cost, charged_energy, charged_import, stored_basis,
        )
        action = _action(
            item, load, solar, battery, projected_soc,
            free_solar_kw=free,
            maximum_discharge_kw=maximum_discharge_kw,
            step_kw=step / (eta * item.duration_hours),
            reference=reference,
            reference_import_share=import_share,
            round_trip=rt,
            grid_import_cost=gic,
            cycle_cost=cycle_cost_sek_per_kwh,
        )
        written = action is not None and _meets_minimum(
            action, minimum_difference_sek_per_kwh, export_minimum_difference_sek_per_kwh
        )
        # Solar charging within its limits needs no schedule. A discharge, or
        # any other charge, only changes the battery if its schedule is written.
        # With a solar controller, whatever it may charge happens unless a
        # written action takes over, also where a discharge was filtered out.
        if written:
            effective_delta = candidate_delta
        elif passive_solar:
            effective_delta = min(passive_steps(index, kept), max(0, levels - round(kept / step)))
        else:
            effective_delta = (
                candidate_delta
                if candidate_delta > 0 and free > 0 and battery <= free + 1e-9
                else 0
            )
        effective_cost, effective_battery = slot_cost(index, effective_delta, decision=False)
        planned += effective_cost
        if effective_battery > free + 0.05:
            # A written charge: what it stores costs the price either way
            # (bought, or not sold), but only imported energy pays the import
            # cost.
            written_kwh = (effective_battery - free) * item.duration_hours
            charged_energy += written_kwh
            charged_cost += written_kwh * item.price_sek_per_kwh
            charged_import += written_kwh * _import_share(
                effective_battery, free, flows[index]
            )
        if written and action is not None:
            actions.append(action)
        elif action is not None and action.direction is PriceDirection.DISCHARGE:
            filtered.add(item.start)
        if effective_delta:
            kept = min(levels * step, max(0.0, kept + effective_delta * step))
        if index + 1 in resets:
            # The fixed external window refills from the energy actually left.
            target, refill_price = resets[index + 1]
            target_energy = target * step
            refill_cost = (refill_price + gic) / eta
            planned += max(0.0, target_energy - kept) * refill_cost
            baseline += max(0.0, target_energy - baseline_energy) * refill_cost
            kept = target_energy
            baseline_energy = target_energy
            level = target
        soc = kept / usable_capacity_kwh * 100
        if blocked[index]:
            performed_by = PERFORMED_BY_EXTERNAL
        elif not effective_delta:
            performed_by = PERFORMED_BY_NONE
        elif written:
            performed_by = PERFORMED_BY_PRICE
        else:
            performed_by = PERFORMED_BY_SOLAR
        slots.append(WholeDaySlot(
            item.start, item.end, item.price_sek_per_kwh, load, solar, effective_battery, soc,
            blocked[index], free, performed_by,
        ))
    if (
        filtered
        and _replans < _REPLAN_LIMIT
        and any(action.direction is PriceDirection.CHARGE for action in actions)
    ):
        return build_whole_day_plan(
            series, now=now, horizon_hours=horizon_hours,
            minimum_lead_minutes=minimum_lead_minutes,
            usable_soc_percent=usable_soc_percent, usable_capacity_kwh=usable_capacity_kwh,
            minimum_soc_percent=minimum_soc_percent, maximum_soc_percent=maximum_soc_percent,
            maximum_charge_kw=maximum_charge_kw, maximum_discharge_kw=maximum_discharge_kw,
            minimum_difference_sek_per_kwh=minimum_difference_sek_per_kwh,
            export_allowed=export_allowed,
            export_minimum_difference_sek_per_kwh=export_minimum_difference_sek_per_kwh,
            round_trip_efficiency_percent=round_trip_efficiency_percent,
            cycle_cost_sek_per_kwh=cycle_cost_sek_per_kwh,
            grid_import_cost_sek_per_kwh=grid_import_cost_sek_per_kwh,
            forecast=forecast, end_before=end_before, external_windows=external_windows,
            levels=levels, solar_charging=solar_charging,
            _no_discharge=_no_discharge | filtered, _replans=_replans + 1,
        )
    margin_total = baseline - planned + terminal_value * (kept - baseline_energy)
    for window in windows:
        if window.adjustable:
            window_socs.setdefault(window.start, kept / usable_capacity_kwh * 100)
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
    return WholeDayResult(
        plan, tuple(slots), baseline, planned, levels, resolution_limited,
    )


def _energy_levels(
    candidates: Sequence[PriceInterval],
    flows: Sequence[tuple[float, float]],
    capacity_kwh: float,
    maximum_charge_kw: float,
    maximum_discharge_kw: float,
    export_allowed: bool,
    eta: float,
) -> int:
    """Energy levels fine enough that the smallest relevant flow spans two.

    The relevant flow is the lower of the power limits and a small house
    load: a self-consumption discharge follows the house, with or without
    export permission. (With export allowed the grid used to stay at 100
    levels, 0.18 kWh for 18.4 kWh, so a quarter hour of house load below
    0.7 kW could not be planned; 12 Nov.) The grid never gets coarser than
    ``ENERGY_LEVELS`` and is bounded by the DP work.
    """
    hours = min(item.duration_hours for item in candidates)
    powers = [p for p in (maximum_charge_kw, maximum_discharge_kw) if p >= 0.1]
    if maximum_discharge_kw >= 0.1 and any(load - solar >= 0.1 for load, solar in flows):
        powers.append(_HOUSE_LOAD_RESOLUTION_KW)
    if not powers:
        return ENERGY_LEVELS
    step = min(powers) * hours * eta / 2
    levels = min(MAXIMUM_ENERGY_LEVELS, max(ENERGY_LEVELS, ceil(capacity_kwh / step - 1e-9)))
    while levels > ENERGY_LEVELS:
        moves = sum(
            (maximum_charge_kw * eta + (
                maximum_discharge_kw if export_allowed
                else min(maximum_discharge_kw, max(0.0, load - solar))
            ) / eta) * item.duration_hours * levels / capacity_kwh + 1
            for item, (load, solar) in zip(candidates, flows, strict=True)
        )
        if moves * (levels + 1) <= _WORK_BUDGET:
            break
        levels = max(ENERGY_LEVELS, int(levels * 0.9))
    return levels


def _import_share(battery: float, free: float, flow: tuple[float, float]) -> float:
    """Share of a written charge (beyond ``free`` solar charging) bought from the grid.

    Solar surplus that the solar controller does not store would otherwise
    be exported; a written charge stores that first, without import cost.
    """
    written = battery - free
    if written <= 1e-9:
        return 0.0
    load, solar = flow
    surplus_left = max(0.0, solar - load - free)
    return max(0.0, written - surplus_left) / written


def _action(
    item: PriceInterval,
    load: float,
    solar: float,
    battery: float,
    soc: float,
    *,
    free_solar_kw: float | None = None,
    maximum_discharge_kw: float,
    step_kw: float,
    reference: float,
    reference_import_share: float = 1.0,
    round_trip: float,
    grid_import_cost: float,
    cycle_cost: float,
) -> PricePlanAction | None:
    """One interval as an action, with the money shown for it.

    The plan's total (the per-slot savings summed) is the true saving against no
    battery. Per interval, though, a charge only spends and a discharge only
    saves, so both are shown as one cycle on the discharge: what it saves
    minus what its energy cost (``reference``, after losses and cycle cost).
    A charge shows no profit of its own. Only charging beyond the solar
    charging that runs on its own (``free_solar_kw``, the whole surplus when
    not given) needs a schedule.
    """
    net = load - solar
    hours = item.duration_hours
    price = item.price_sek_per_kwh
    free = max(0.0, -net) if free_solar_kw is None else free_solar_kw
    if battery > free + 0.05:
        power = round(battery, 1)
        if power < 0.1:
            return None
        return PricePlanAction(
            item.start, item.end, PriceDirection.CHARGE, power, "demandresponse",
            {"pacref": round(power * 1000, 3)}, None, price, price, 0.0, soc, 0.0, 0.0,
        )
    if battery < -0.05:
        delivered = -battery * hours
        basis = reference
        house = min(-battery, max(0.0, net)) * hours
        saved = house * (price + grid_import_cost) + (delivered - house) * price
        energy_cost = delivered * (
            (basis + grid_import_cost * reference_import_share) / round_trip + cycle_cost
        )
        margin = saved - energy_cost
        # Less than one energy step beyond the house load is rounding in the
        # energy grid, not a planned export: follow the house load instead of
        # forcing a fixed export power.
        if -battery <= max(0.0, net) + max(0.05, step_kw):
            # Following the whole house load: let the battery cover it as it
            # varies. A shortfall of less than one energy step is rounding in
            # the energy grid, not a choice to cover only part of the house
            # (written as "0.7 kW" on 7-8 Nov, so peaks were bought at the
            # expensive price). Covering clearly less: cap the discharge.
            house = max(0.0, net)
            power = (
                maximum_discharge_kw
                if -battery >= min(0.9 * house, house - step_kw) - 0.05
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


def _meets_minimum(action: PricePlanAction, minimum: float, export_minimum: float) -> bool:
    """A discharge is only written when its own cycle reaches your minimum.

    The energy is then simply kept; the battery ends up fuller than planned,
    never emptier.
    """
    if action.direction is not PriceDirection.DISCHARGE:
        return True
    required = export_minimum if action.discharge_mode is PriceDischargeMode.EXPORT else minimum
    return action.net_profit_sek_per_kwh + 1e-9 >= required


def _basis(
    refill: tuple[float, float] | None,
    charged_cost: float,
    charged_energy: float,
    charged_import: float,
    stored_basis: float,
) -> tuple[float, float]:
    """(price, share of it that pays the import cost) of the energy discharged."""
    if refill is not None:
        return refill
    if charged_energy > 0:
        return charged_cost / charged_energy, charged_import / charged_energy
    return stored_basis, 1.0


def _blocked(series: PriceSeries, blocker: str, soc: float) -> WholeDayResult:
    plan = PricePlan((), (blocker,), series.fingerprint, soc, soc, 0.0)
    return WholeDayResult(plan, (), 0.0, 0.0)


__all__ = [
    "PERFORMED_BY_EXTERNAL",
    "PERFORMED_BY_NONE",
    "PERFORMED_BY_PRICE",
    "PERFORMED_BY_SOLAR",
    "SolarCharging",
    "WholeDayResult",
    "WholeDaySlot",
    "build_whole_day_plan",
]
