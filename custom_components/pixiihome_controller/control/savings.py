"""Measured battery savings against the same hour without the battery.

Each sample compares the measured grid flow with the flow the house would
have had without the battery (grid minus battery power). Import costs the
price plus the variable import cost; export earns the price, the same
economics the price planner uses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from .energy_forecast import STOCKHOLM

HISTORY_DAYS = 31


def grid_cost_sek(grid_w: float, hours: float, price: float, import_cost: float) -> float:
    """Cost of one grid flow (W, + import) over ``hours``."""
    energy = grid_w / 1000 * hours
    return energy * (price + import_cost) if energy > 0 else energy * price


@dataclass(slots=True)
class SavingsTracker:
    day: str | None = None
    today_sek: float = 0.0
    history: list[tuple[str, float]] = field(default_factory=list)

    def add(
        self,
        instant: datetime,
        grid_w: float,
        battery_w: float,
        price: float,
        import_cost: float,
        hours: float,
    ) -> tuple[str, float] | None:
        """Record one sample; returns (day, savings) when a day was completed."""
        if not all(isfinite(value) for value in (grid_w, battery_w, price, import_cost, hours)):
            return None
        if hours <= 0 or hours > 0.25:
            return None
        finished = self.roll(instant)
        without_battery = grid_w - battery_w
        self.today_sek += grid_cost_sek(without_battery, hours, price, import_cost) - (
            grid_cost_sek(grid_w, hours, price, import_cost)
        )
        return finished

    def roll(self, instant: datetime) -> tuple[str, float] | None:
        day = instant.astimezone(STOCKHOLM).date().isoformat()
        if self.day == day:
            return None
        finished = None
        if self.day is not None:
            finished = (self.day, self.today_sek)
            self.history = [item for item in self.history if item[0] != self.day]
            self.history.append(finished)
            self.history = self.history[-HISTORY_DAYS:]
        self.day, self.today_sek = day, 0.0
        return finished

    @property
    def yesterday_sek(self) -> float | None:
        return self.history[-1][1] if self.history else None

    def week_sek(self, instant: datetime) -> float:
        """Monday to today, local time."""
        local = instant.astimezone(STOCKHOLM).date()
        monday = (local - timedelta(days=local.weekday())).isoformat()
        total = sum(value for day, value in self.history if day >= monday)
        return total + (self.today_sek if self.day and self.day >= monday else 0.0)

    def month_sek(self, instant: datetime) -> float:
        month = instant.astimezone(STOCKHOLM).strftime("%Y-%m")
        total = sum(value for day, value in self.history if day.startswith(month))
        return total + (self.today_sek if self.day and self.day.startswith(month) else 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day,
            "today_sek": round(self.today_sek, 4),
            "history": [[day, round(value, 4)] for day, value in self.history],
        }

    @classmethod
    def from_dict(cls, value: object) -> SavingsTracker:
        if not isinstance(value, Mapping):
            return cls()
        try:
            day = value.get("day")
            return cls(
                str(day) if day else None,
                float(value.get("today_sek", 0.0)),
                [(str(item[0]), float(item[1])) for item in value.get("history", [])][
                    -HISTORY_DAYS:
                ],
            )
        except (TypeError, ValueError, IndexError):
            return cls()


__all__ = ["SavingsTracker", "grid_cost_sek"]
