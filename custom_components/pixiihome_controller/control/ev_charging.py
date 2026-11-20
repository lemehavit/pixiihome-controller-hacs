"""Pure EV charging decisions: price-based charge windows and charger current.

No Home Assistant calls. The controller in ``ev_charging_controller`` reads the
charger, the car and the meter, asks these functions what to do and performs
the service calls.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import ceil, floor, isfinite
from typing import Any

CONF_EV_CHARGING = "ev_charging"

# Planned charge time is padded so that a slightly lower real power (cold
# battery, voltage drop, taper near the top) still reaches the target.
PLAN_MARGIN = 1.15


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


@dataclass(frozen=True)
class EvChargingConfig:
    """Entity bindings and physical limits for one charger and one car."""

    charger_status_entity: str = ""
    charger_power_entity: str = ""
    charger_limit_entity: str = ""
    car_soc_entity: str = ""
    car_limit_entity: str = ""
    car_charging_switch: str = ""
    car_plugged_entity: str = ""
    charger_start_entity: str = ""
    charger_pause_entity: str = ""
    charger_current_entity: str = ""
    price_entity: str = ""
    current_l1: str = ""
    current_l2: str = ""
    current_l3: str = ""
    phases: int = 3
    battery_capacity_kwh: float = 77.0
    charge_efficiency_percent: float = 90.0
    min_current_a: int = 6
    max_current_a: int = 16
    fuse_a: float = 20.0
    margin_a: float = 2.0
    voltage_v: float = 230.0

    def __post_init__(self) -> None:
        domains = {
            "charger_status_entity": ("sensor.",),
            "charger_power_entity": ("sensor.",),
            "charger_limit_entity": ("sensor.",),
            "car_soc_entity": ("sensor.",),
            "car_limit_entity": ("number.",),
            "car_charging_switch": ("switch.",),
            "car_plugged_entity": ("binary_sensor.",),
            "charger_start_entity": ("button.", "script.", "switch.", "input_boolean."),
            "charger_pause_entity": ("button.", "script.", "switch.", "input_boolean."),
            "charger_current_entity": ("number.", "input_number."),
            "price_entity": ("sensor.",),
            "current_l1": ("sensor.",),
            "current_l2": ("sensor.",),
            "current_l3": ("sensor.",),
        }
        for key, prefixes in domains.items():
            value = getattr(self, key)
            if not isinstance(value, str) or (value and not value.startswith(prefixes)):
                raise ValueError("invalid_entity")
        if type(self.phases) is not int or self.phases not in (1, 3):
            raise ValueError("invalid_config")
        for key, low, high in (
            ("battery_capacity_kwh", 5, 250),
            ("charge_efficiency_percent", 50, 100),
            ("min_current_a", 6, 32),
            ("max_current_a", 6, 63),
            ("fuse_a", 6, 250),
            ("margin_a", 0, 20),
            ("voltage_v", 180, 260),
        ):
            value = getattr(self, key)
            if not _finite(value) or not low <= value <= high:
                raise ValueError("invalid_config")
        if type(self.min_current_a) is not int or type(self.max_current_a) is not int:
            raise ValueError("invalid_config")
        if self.min_current_a > self.max_current_a:
            raise ValueError("invalid_config")
        if bool(self.charger_start_entity) != bool(self.charger_pause_entity):
            raise ValueError("charger_start_and_pause_required")
        if self.margin_a >= self.fuse_a:
            raise ValueError("margin_exceeds_fuse")
        if self.current_entities and len(self.current_entities) != len(
            set(self.current_entities)
        ):
            raise ValueError("invalid_entity")

    @property
    def configured(self) -> bool:
        return bool(self.charger_status_entity)

    @property
    def current_entities(self) -> tuple[str, ...]:
        return tuple(e for e in (self.current_l1, self.current_l2, self.current_l3) if e)

    @property
    def max_power_kw(self) -> float:
        return self.phases * self.voltage_v * self.max_current_a / 1000

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))

    @classmethod
    def from_mapping(cls, value: object) -> EvChargingConfig:
        if not isinstance(value, Mapping):
            return cls()
        data: dict[str, Any] = {}
        for key, item in value.items():
            if key not in cls.__dataclass_fields__:
                continue
            if key in {"phases", "min_current_a", "max_current_a"} and _finite(item):
                if float(item) != int(item):
                    raise ValueError("invalid_config")
                item = int(item)
            elif key in {"phases"} and isinstance(item, str) and item.isdigit():
                item = int(item)
            elif item is None:
                item = "" if isinstance(getattr(cls, key, ""), str) else item
            data[key] = item
        return cls(**data)


# ----- price plan ------------------------------------------------------------------------


@dataclass(frozen=True)
class EvSlot:
    start: datetime
    end: datetime
    price: float
    power_kw: float = 0.0

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    @property
    def energy_kwh(self) -> float:
        return self.hours * self.power_kw


@dataclass(frozen=True)
class EvPlan:
    """What the charger should do right now, and why."""

    charge: bool
    reason: str
    need_kwh: float | None = None
    hours_needed: float | None = None
    deadline: datetime | None = None
    slots: tuple[EvSlot, ...] = ()
    average_price: float | None = None
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def attributes(self) -> dict[str, Any]:
        return {
            "plan_reason": self.reason,
            "plan_detail": self.detail,
            "need_kwh": None if self.need_kwh is None else round(self.need_kwh, 2),
            "hours_needed": None if self.hours_needed is None else round(self.hours_needed, 2),
            "ready_by": None if self.deadline is None else self.deadline.isoformat(),
            "planned_average_price_sek_per_kwh": (
                None if self.average_price is None else round(self.average_price, 3)
            ),
            "planned_slots": [
                {"start": s.start.isoformat(), "end": s.end.isoformat(),
                 "price": round(s.price, 3), "power_kw": round(s.power_kw, 2)}
                for s in _merge(self.slots)
            ],
            **self.extra,
        }


def next_deadline(now_local: datetime, ready_by_hour: int) -> datetime:
    """The next local ``ready_by_hour:00`` strictly after ``now_local``."""
    candidate = now_local.replace(hour=ready_by_hour, minute=0, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate


def energy_needed_kwh(
    soc: float, target: float, capacity_kwh: float, efficiency_percent: float
) -> float:
    """Energy drawn from the grid to lift the car battery from ``soc`` to ``target``."""
    if soc >= target:
        return 0.0
    return (target - soc) / 100 * capacity_kwh / (efficiency_percent / 100)


def plan_charging(
    *,
    now: datetime,
    soc: float | None,
    target_soc: float,
    min_soc: float,
    deadline: datetime,
    capacity_kwh: float,
    efficiency_percent: float,
    power_kw: float,
    prices: Sequence[Any],
    battery_discharges: Iterable[tuple[datetime, datetime]] = (),
    allow_battery: bool = False,
    committed_until: datetime | None = None,
    power_at: Callable[[datetime, datetime], float] | None = None,
) -> EvPlan:
    """Choose the cheapest price intervals before the deadline that cover the need.

    ``prices`` are objects with ``start``, ``end`` and ``price_sek_per_kwh``.
    ``committed_until`` keeps an interval in which charging already started,
    so a slowly rising SOC does not pause and resume within one interval.
    Intervals with a planned home-battery discharge are skipped unless
    ``allow_battery``; the minimum level and the deadline still charge in them.
    ``power_at(start, end)`` is the charging power available in that period
    (fuse room, house load, what the car accepts); ``power_kw`` is used when
    it is not given and for the time after the last known price.
    """
    if soc is None:
        return EvPlan(False, "car_soc_unknown", deadline=deadline,
                      detail="The car's battery level is unknown")
    if soc >= target_soc:
        return EvPlan(False, "target_reached", 0.0, 0.0, deadline,
                      detail=f"The car is at {soc:.0f} %, target {target_soc:.0f} %")
    need = energy_needed_kwh(soc, target_soc, capacity_kwh, efficiency_percent)
    power = power_at or (lambda _start, _end: power_kw)
    if power_kw <= 0:
        return EvPlan(False, "no_charge_power", need, None, deadline,
                      detail="The charger has no usable power")
    goal = need * PLAN_MARGIN
    hours = goal / power_kw
    if soc < min_soc:
        return EvPlan(True, "below_minimum", need, hours, deadline,
                      detail=f"The car is at {soc:.0f} %, below the minimum {min_soc:.0f} %, "
                             "so it charges now regardless of price")
    remaining = (deadline - now).total_seconds() / 3600
    reachable = _energy_until(now, deadline, power)
    if reachable <= goal:
        return EvPlan(True, "deadline", need, hours, deadline,
                      detail=f"About {goal:.1f} kWh is needed and the available power gives "
                             f"only {reachable:.1f} kWh in the {max(0.0, remaining):.1f} h "
                             "left until the ready-by time")
    discharges = tuple(battery_discharges)
    slots: list[EvSlot] = []
    known_end: datetime | None = None
    for interval in prices:
        start, end = interval.start, interval.end
        price = interval.price_sek_per_kwh
        if end <= now or start >= deadline or not _finite(price):
            continue
        start, end = max(start, now), min(end, deadline)
        if end <= start:
            continue
        if not allow_battery and any(a < end and start < b for a, b in discharges):
            known_end = end if known_end is None else max(known_end, end)
            continue
        slots.append(EvSlot(start, end, float(price), max(0.0, power(start, end))))
        known_end = end if known_end is None else max(known_end, end)
    if known_end is None:
        return EvPlan(False, "waiting_for_prices", need, hours, deadline,
                      detail="No prices are known before the ready-by time yet")
    unknown_hours = max(0.0, (deadline - known_end).total_seconds() / 3600)
    tail = _energy_until(known_end, deadline, power) if unknown_hours > 0 else 0.0
    # When the unpriced tail alone can hold the whole charge, wait for the next
    # day's prices instead of committing to today's known hours.
    if tail >= goal:
        return EvPlan(False, "waiting_for_prices", need, hours, deadline,
                      detail=f"Prices are known until {known_end.isoformat()}; the remaining "
                             f"{unknown_hours:.1f} h before the ready-by time can deliver "
                             f"{tail:.1f} kWh of the {goal:.1f} kWh needed, so it waits for "
                             "the next price list")
    to_cover = goal - tail
    chosen: list[EvSlot] = []
    covered = 0.0
    for slot in sorted(slots, key=lambda s: (s.price, s.start)):
        if covered >= to_cover - 1e-9:
            break
        if slot.energy_kwh <= 0:
            continue
        chosen.append(slot)
        covered += slot.energy_kwh
    chosen.sort(key=lambda s: s.start)
    weight = sum(s.hours for s in chosen)
    hours = weight + unknown_hours if tail else weight
    average = sum(s.price * s.hours for s in chosen) / weight if weight else None
    current = next((s for s in chosen if s.start <= now < s.end), None)
    in_discharge = not allow_battery and any(a <= now < b for a, b in discharges)
    if (
        current is None and committed_until is not None and now < committed_until
        and not in_discharge
    ):
        current_price = next(
            (s.price for s in slots if s.start <= now < s.end), None
        )
        return EvPlan(True, "finishing_interval", need, hours, deadline, tuple(chosen), average,
                      detail="Charging started in this price interval and finishes it"
                      + ("" if current_price is None else f" ({current_price:.2f} kr/kWh)"))
    if current is not None:
        return EvPlan(True, "cheap_price", need, hours, deadline, tuple(chosen), average,
                      detail=f"{current.price:.2f} kr/kWh is among the cheapest "
                             f"{weight:.1f} h before the ready-by time")
    upcoming = next((s for s in chosen if s.start > now), None)
    if in_discharge:
        return EvPlan(False, "battery_discharging", need, hours, deadline, tuple(chosen),
                      average,
                      detail="The home battery has a planned discharge now and charging the "
                             "car from the battery is not allowed")
    return EvPlan(False, "waiting_for_cheap_price", need, hours, deadline, tuple(chosen),
                  average,
                  detail="Waiting for a cheaper interval"
                  + ("" if upcoming is None
                     else f" from {upcoming.start.isoformat()} ({upcoming.price:.2f} kr/kWh)"))


def _energy_until(
    start: datetime, end: datetime, power: Callable[[datetime, datetime], float]
) -> float:
    """Energy the available power can deliver between two instants, per quarter hour."""
    total, cursor = 0.0, start
    step = timedelta(minutes=15)
    while cursor < end:
        stop = min(end, cursor + step)
        total += max(0.0, power(cursor, stop)) * (stop - cursor).total_seconds() / 3600
        cursor = stop
    return total


def _merge(slots: Sequence[EvSlot]) -> list[EvSlot]:
    merged: list[EvSlot] = []
    for slot in sorted(slots, key=lambda s: s.start):
        if merged and merged[-1].end == slot.start:
            last = merged[-1]
            hours = last.hours + slot.hours
            price = (last.price * last.hours + slot.price * slot.hours) / hours
            power = (last.energy_kwh + slot.energy_kwh) / hours
            merged[-1] = EvSlot(last.start, slot.end, price, power)
        else:
            merged.append(slot)
    return merged


def car_limit_for_target(
    target_soc: float, minimum: float = 50, maximum: float = 100, step: float = 10
) -> float:
    """The car's own charge limit: the target rounded up to the car's step and range.

    Below the car's minimum the integration pauses the charger itself when the
    target is reached; the car limit only stops the car at or above it.
    """
    step = step if _finite(step) and step > 0 else 1
    value = ceil(target_soc / step - 1e-9) * step
    return float(min(maximum, max(minimum, value)))


# ----- load balancing --------------------------------------------------------------------


@dataclass(frozen=True)
class CurrentDecision:
    """New dynamic charger current, or a pause because the fuse leaves too little."""

    limit_a: int
    pause: bool
    headroom_a: float
    detail: str


def charger_current(
    *,
    phase_currents_a: Sequence[float],
    ev_current_a: float,
    fuse_a: float,
    margin_a: float,
    min_current_a: int,
    max_current_a: int,
    battery_credit_a: Sequence[float] = (),
) -> CurrentDecision:
    """Largest charger current that keeps every phase under fuse minus margin.

    ``phase_currents_a`` are measured at the grid connection and already
    include the car. ``ev_current_a`` is what the car draws per phase now.
    The phase the car uses is not known, so the tightest phase decides.
    ``battery_credit_a`` is grid current per phase that the home battery's own
    load balancing will give up when the car needs it (battery charging).
    """
    if not phase_currents_a:
        raise ValueError("phase_data_missing")
    if not all(_finite(v) for v in phase_currents_a) or not _finite(ev_current_a):
        raise ValueError("phase_data_invalid")
    credits = list(battery_credit_a) + [0.0] * (len(phase_currents_a) - len(battery_credit_a))
    headrooms = [
        fuse_a - margin_a - current + max(0.0, credit if _finite(credit) else 0.0)
        for current, credit in zip(phase_currents_a, credits, strict=False)
    ]
    headroom = min(headrooms)
    wanted = floor(max(0.0, ev_current_a) + headroom)
    if wanted < min_current_a:
        return CurrentDecision(
            min_current_a, True, headroom,
            f"The tightest phase has {headroom:.1f} A left; the car would get {wanted} A, "
            f"below the charger minimum {min_current_a} A",
        )
    limit = min(max_current_a, wanted)
    return CurrentDecision(
        limit, False, headroom,
        f"The tightest phase has {headroom:.1f} A left with the car drawing "
        f"{ev_current_a:.1f} A, so the charger may use {limit} A",
    )


__all__ = [
    "CONF_EV_CHARGING",
    "CurrentDecision",
    "EvChargingConfig",
    "EvPlan",
    "EvSlot",
    "car_limit_for_target",
    "charger_current",
    "energy_needed_kwh",
    "next_deadline",
    "plan_charging",
]
