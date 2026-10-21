"""Decide whether solar charging may wait for cheaper export hours.

Charging can wait only when a conservative solar forecast (Solcast P10, or
70 % of the forecast, scaled by today's measured shortfall) minus a high
household-load forecast still fills the battery to its target later today,
with a safety margin, and when exporting now is worth clearly more than
exporting in the hours the battery would then charge in. Anything uncertain
answers "charge now", which is the normal solar-charging behaviour.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from .energy_forecast import STOCKHOLM
from .price_optimization import PriceInterval

QUARTER = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class SmartSolarDecision:
    defer: bool
    reason: str
    need_kwh: float | None = None
    available_later_kwh: float | None = None
    reachable_soc_percent: float | None = None
    deadline: datetime | None = None
    export_price_now: float | None = None
    charge_price: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "deferring": self.defer,
            "decision": self.reason,
            "energy_needed_kwh": _round(self.need_kwh),
            "conservative_surplus_later_kwh": _round(self.available_later_kwh),
            "reachable_usable_soc_percent": _round(self.reachable_soc_percent, 1),
            "last_charge_quarter": self.deadline.isoformat() if self.deadline else None,
            "export_price_now_sek_per_kwh": _round(self.export_price_now, 3),
            "planned_charge_price_sek_per_kwh": _round(self.charge_price, 3),
        }


def evaluate_deferral(
    *,
    now: datetime,
    usable_soc_percent: float,
    target_soc_percent: float,
    capacity_kwh: float,
    charge_kw: float,
    round_trip_efficiency_percent: float,
    safety_margin_percent: float,
    minimum_advantage_sek_per_kwh: float,
    prices: Sequence[PriceInterval],
    surplus_kw: Callable[[datetime, datetime], float | None],
) -> SmartSolarDecision:
    """``surplus_kw`` = conservative solar minus high load for an interval."""
    if capacity_kwh <= 0:
        return SmartSolarDecision(False, "capacity_unknown")
    efficiency = (round_trip_efficiency_percent / 100) ** 0.5
    need = max(0.0, target_soc_percent - usable_soc_percent) / 100 * capacity_kwh / efficiency
    if need <= 0.05:
        return SmartSolarDecision(False, "target_reached", need)
    current_start = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
    end_of_day = (
        now.astimezone(STOCKHOLM).replace(hour=0, minute=0, second=0, microsecond=0)
        + timedelta(days=1)
    ).astimezone(now.tzinfo)
    quarters: list[tuple[datetime, float, float | None]] = []
    cursor = current_start + QUARTER
    while cursor < end_of_day:
        surplus = surplus_kw(cursor, cursor + QUARTER)
        if surplus is None:
            return SmartSolarDecision(False, "forecast_unavailable", need)
        absorbed = min(max(0.0, surplus), charge_kw) * 0.25
        quarters.append((cursor, absorbed, _price(prices, cursor)))
        cursor += QUARTER
    later = sum(item[1] for item in quarters)
    now_surplus = surplus_kw(now, current_start + QUARTER) or 0.0
    now_part = min(max(0.0, now_surplus), charge_kw) * (
        (current_start + QUARTER - now).total_seconds() / 3600
    )
    reachable = min(
        100.0,
        usable_soc_percent + (later + now_part) * efficiency / capacity_kwh * 100,
    )
    useful = [item for item in quarters if item[1] > 0.01]
    deadline = useful[-1][0] + QUARTER if useful else None
    margin = 1 + safety_margin_percent / 100
    if later < need * margin:
        return SmartSolarDecision(
            False, "surplus_too_small_later", need, later, reachable, deadline,
        )
    price_now = _price(prices, now)
    if price_now is None or any(item[2] is None for item in useful):
        return SmartSolarDecision(False, "prices_unavailable", need, later, reachable, deadline)
    if price_now <= 0:
        return SmartSolarDecision(False, "export_price_not_positive", need, later, reachable,
                                  deadline, price_now)
    # The battery would then charge in the cheapest later quarters that cover
    # the need (with margin). Their export value is what charging there costs.
    remaining = need * margin
    cost = energy = 0.0
    for _, absorbed, price in sorted(useful, key=lambda item: (item[2], item[0])):
        assert price is not None
        take = min(absorbed, remaining)
        cost += take * price
        energy += take
        remaining -= take
        if remaining <= 1e-9:
            break
    charge_price = cost / energy if energy > 0 else None
    if charge_price is None or price_now - charge_price < minimum_advantage_sek_per_kwh:
        return SmartSolarDecision(False, "no_price_advantage", need, later, reachable, deadline,
                                  price_now, charge_price)
    return SmartSolarDecision(True, "export_now_charge_later", need, later, reachable, deadline,
                              price_now, charge_price)


def _price(prices: Sequence[PriceInterval], instant: datetime) -> float | None:
    for interval in prices:
        if interval.start <= instant < interval.end:
            return interval.price_sek_per_kwh
    return None


def _round(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


__all__ = ["SmartSolarDecision", "evaluate_deferral"]
