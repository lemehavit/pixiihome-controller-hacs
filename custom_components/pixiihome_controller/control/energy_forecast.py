"""Pure household-load and solar forecast models.

Household load is learned from this house's own measurements only. Each hour
of the week keeps the hourly means of its last six weeks; the forecast is the
median of those values, blended with the median of the same clock hour over
the last two weeks while few weeks are known. Medians ignore single unusual
days. A separate daily regression of mean load against heating degrees
(below 17 °C) moves every stored hour to today's temperature before the median
is taken. A high (80th percentile) value lets planning keep energy for heavier
days.

Solar forecasts are read from Home Assistant forecast entities (Solcast
``detailedForecast`` or Open-Meteo Solar Forecast ``watts``). A short intraday
correction compares measured production with the forecast; below-forecast
output is assumed to persist for the rest of the day, above-forecast output
fades back to the forecast within two hours.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from math import isfinite
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

STOCKHOLM = ZoneInfo("Europe/Stockholm")
HOURS_OF_WEEK = 7 * 24
WEEKS_KEPT = 6
RECENT_DAYS = 14
DAILY_KEPT = 60
COMPLETE_DAY_HOURS = 20
DEGREE_BASE_C = 17.0
# The regression needs enough cold days, spread over a real temperature range.
REGRESSION_MIN_DAYS = 10
REGRESSION_MIN_RANGE_C = 4.0
SLOPE_LIMIT_W_PER_DEGREE = 2000.0
# Without any measurement the forecast is flat: no assumed daily shape.
DEFAULT_LOAD_W = 1000.0
HIGH_QUANTILE = 0.8
LOAD_MODEL_VERSION = 2


def hour_of_week(instant: datetime, zone: ZoneInfo = STOCKHOLM) -> int:
    local = instant.astimezone(zone)
    return local.weekday() * 24 + local.hour


def heating_degrees(temperature_c: float | None) -> float | None:
    if temperature_c is None or not isfinite(temperature_c):
        return None
    return max(0.0, DEGREE_BASE_C - temperature_c)


def quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@dataclass(frozen=True, slots=True)
class HourSample:
    """Mean load of one finished local hour and its outdoor temperature."""

    day: str
    load_w: float
    temperature_c: float | None


@dataclass(slots=True)
class _OpenHour:
    index: int
    day: str
    load_sum: float = 0.0
    weight: float = 0.0
    temperature_sum: float = 0.0
    temperature_weight: float = 0.0


@dataclass(slots=True)
class LoadModel:
    """Learned household load, excluding battery and solar flows."""

    hours: list[list[HourSample]] = field(
        default_factory=lambda: [[] for _ in range(HOURS_OF_WEEK)]
    )
    daily: list[tuple[str, float, float | None]] = field(default_factory=list)
    slope_w_per_degree: float = 0.0
    last_temperature_c: float | None = None
    samples: int = 0
    last_sample: datetime | None = None
    mae_w: float | None = None
    zone: ZoneInfo = STOCKHOLM
    _open: _OpenHour | None = None
    _day: dict[str, Any] = field(default_factory=dict)

    # ----- learning --------------------------------------------------------------------

    def update(
        self,
        instant: datetime,
        load_w: float,
        temperature_c: float | None = None,
        *,
        hours: float = 0.25,
    ) -> bool:
        """Add one measured interval mean. Returns False for rejected samples."""
        if not isfinite(load_w) or load_w < 0 or hours <= 0:
            return False
        if self.last_sample is not None and instant <= self.last_sample:
            return False
        if temperature_c is not None and isfinite(temperature_c):
            self.last_temperature_c = temperature_c
        else:
            temperature_c = None
        error = load_w - self.predict_w(instant, temperature_c)
        self.mae_w = abs(error) if self.mae_w is None else 0.95 * self.mae_w + 0.05 * abs(error)
        index = hour_of_week(instant, self.zone)
        day = instant.astimezone(self.zone).date().isoformat()
        current = self._open
        if current is None or current.index != index or current.day != day:
            self._close_hour()
            current = self._open = _OpenHour(index, day)
        current.load_sum += load_w * hours
        current.weight += hours
        if temperature_c is not None:
            current.temperature_sum += temperature_c * hours
            current.temperature_weight += hours
        self.samples += 1
        self.last_sample = instant
        return True

    def _close_hour(self) -> None:
        current, self._open = self._open, None
        if current is None or current.weight <= 0:
            return
        load = current.load_sum / current.weight
        temperature = (
            current.temperature_sum / current.temperature_weight
            if current.temperature_weight > 0 else None
        )
        bucket = self.hours[current.index]
        bucket[:] = [item for item in bucket if item.day != current.day]
        bucket.append(HourSample(current.day, load, temperature))
        del bucket[:-WEEKS_KEPT]
        self._add_to_day(current.day, load, temperature)

    def _add_to_day(self, day: str, load: float, temperature: float | None) -> None:
        if self._day.get("day") != day:
            self._close_day()
            self._day = {"day": day, "load": 0.0, "hours": 0, "temperature": 0.0, "t_hours": 0}
        self._day["load"] += load
        self._day["hours"] += 1
        if temperature is not None:
            self._day["temperature"] += temperature
            self._day["t_hours"] += 1

    def _close_day(self) -> None:
        day = self._day
        if not day or day["hours"] < COMPLETE_DAY_HOURS:
            return
        temperature = day["temperature"] / day["t_hours"] if day["t_hours"] else None
        self.daily = [item for item in self.daily if item[0] != day["day"]]
        self.daily.append((day["day"], day["load"] / day["hours"], temperature))
        del self.daily[:-DAILY_KEPT]
        self.slope_w_per_degree = self._fit_slope()

    def _fit_slope(self) -> float:
        """Least squares: daily mean load = a + b × heating degrees, b ≥ 0."""
        points = [
            (degrees, load)
            for _, load, temperature in self.daily
            if (degrees := heating_degrees(temperature)) is not None
        ]
        if len(points) < REGRESSION_MIN_DAYS:
            return 0.0
        xs = [x for x, _ in points]
        if max(xs) - min(xs) < REGRESSION_MIN_RANGE_C:
            return 0.0
        mean_x = sum(xs) / len(xs)
        mean_y = sum(y for _, y in points) / len(points)
        spread = sum((x - mean_x) ** 2 for x in xs)
        if spread <= 0:
            return 0.0
        slope = sum((x - mean_x) * (y - mean_y) for x, y in points) / spread
        return min(SLOPE_LIMIT_W_PER_DEGREE, max(0.0, slope))

    # ----- forecasting -----------------------------------------------------------------

    @property
    def heating_w_per_c(self) -> float:
        return self.slope_w_per_degree

    def learned_days(self) -> int:
        """Complete days measured so far (at most the days kept)."""
        return len(self.daily)

    def _samples_for(self, instant: datetime) -> tuple[list[HourSample], list[HourSample]]:
        index = hour_of_week(instant, self.zone)
        same_week_hour = list(self.hours[index])
        hour = index % 24
        today = instant.astimezone(self.zone).date()
        cutoff = (today - timedelta(days=RECENT_DAYS)).isoformat()
        same_hour = [
            item
            for weekday in range(7)
            for item in self.hours[weekday * 24 + hour]
            if item.day >= cutoff
        ]
        return same_week_hour, same_hour

    def _adjusted(self, items: Iterable[HourSample], temperature_c: float | None) -> list[float]:
        target = heating_degrees(temperature_c)
        values = []
        for item in items:
            value = item.load_w
            observed = heating_degrees(item.temperature_c)
            if target is not None and observed is not None:
                value += self.slope_w_per_degree * (target - observed)
            values.append(max(0.0, value))
        return values

    def _fallback(self) -> float:
        if self.daily:
            return median(load for _, load, _ in self.daily[-RECENT_DAYS:])
        return DEFAULT_LOAD_W

    def _estimate(self, instant: datetime, temperature_c: float | None, q: float | None) -> float:
        if temperature_c is None or not isfinite(temperature_c):
            temperature_c = self.last_temperature_c
        weekly_items, recent_items = self._samples_for(instant)
        weekly = self._adjusted(weekly_items, temperature_c)
        recent = self._adjusted(recent_items, temperature_c)

        def pick(values: list[float]) -> float:
            return quantile(values, q) if q is not None else median(values)

        if not weekly and not recent:
            base = self._fallback()
            return base * 1.3 if q is not None else base
        if not weekly:
            return pick(recent)
        if not recent:
            return pick(weekly)
        weight = len(weekly) / (len(weekly) + 2)
        return weight * pick(weekly) + (1 - weight) * pick(recent)

    def predict_w(self, instant: datetime, temperature_c: float | None = None) -> float:
        return max(0.0, self._estimate(instant, temperature_c, None))

    def predict_high_w(self, instant: datetime, temperature_c: float | None = None) -> float:
        typical = self.predict_w(instant, temperature_c)
        return max(typical, self._estimate(instant, temperature_c, HIGH_QUANTILE))

    # ----- persistence -----------------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": LOAD_MODEL_VERSION,
            "hours": [
                [[item.day, round(item.load_w, 1), item.temperature_c] for item in bucket]
                for bucket in self.hours
            ],
            "daily": [[day, round(load, 1), temperature] for day, load, temperature in self.daily],
            "slope_w_per_degree": self.slope_w_per_degree,
            "last_temperature_c": self.last_temperature_c,
            "samples": self.samples,
            "last_sample": self.last_sample.isoformat() if self.last_sample else None,
            "mae_w": self.mae_w,
        }

    @classmethod
    def from_dict(cls, value: object, zone: ZoneInfo = STOCKHOLM) -> LoadModel:
        """Restore a saved model; anything unreadable starts a fresh model."""
        model = cls(zone=zone)
        if not isinstance(value, Mapping) or value.get("version") != LOAD_MODEL_VERSION:
            return model
        try:
            raw_hours = value["hours"]
            if not isinstance(raw_hours, list) or len(raw_hours) != HOURS_OF_WEEK:
                return cls(zone=zone)
            for bucket, raw in zip(model.hours, raw_hours, strict=True):
                for day, load, temperature in raw[-WEEKS_KEPT:]:
                    date.fromisoformat(str(day))
                    if not isfinite(float(load)) or float(load) < 0:
                        continue
                    bucket.append(HourSample(
                        str(day), float(load),
                        float(temperature) if isinstance(temperature, int | float) else None,
                    ))
            for day, load, temperature in value.get("daily", [])[-DAILY_KEPT:]:
                model.daily.append((
                    str(day), float(load),
                    float(temperature) if isinstance(temperature, int | float) else None,
                ))
            model.slope_w_per_degree = min(
                SLOPE_LIMIT_W_PER_DEGREE, max(0.0, float(value.get("slope_w_per_degree", 0.0)))
            )
            temperature = value.get("last_temperature_c")
            model.last_temperature_c = (
                float(temperature) if isinstance(temperature, int | float) else None
            )
            model.samples = int(value.get("samples", 0))
            last = value.get("last_sample")
            model.last_sample = datetime.fromisoformat(last) if isinstance(last, str) else None
            mae = value.get("mae_w")
            model.mae_w = float(mae) if isinstance(mae, int | float) else None
        except (KeyError, TypeError, ValueError):
            return cls(zone=zone)
        return model


@dataclass(frozen=True, slots=True)
class SolarInterval:
    """Forecast mean AC power (kW) over one interval."""

    start: datetime
    end: datetime
    mean_kw: float
    p10_kw: float | None = None
    p90_kw: float | None = None


def parse_solar_forecast(attribute_sets: Iterable[Mapping[str, Any]]) -> tuple[SolarInterval, ...]:
    """Merge Solcast ``detailedForecast`` and Open-Meteo ``watts`` attributes."""
    found: dict[datetime, SolarInterval] = {}
    for attributes in attribute_sets:
        for interval in _solcast_intervals(attributes):
            found[interval.start] = interval
        for interval in _watts_intervals(attributes):
            found.setdefault(interval.start, interval)
    ordered = sorted(found.values(), key=lambda item: item.start)
    return tuple(ordered)


def _instant(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _kw(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if isfinite(number) and number >= 0 else None


def _solcast_intervals(attributes: Mapping[str, Any]) -> list[SolarInterval]:
    for key, minutes in (("detailedForecast", 30), ("detailedHourly", 60)):
        rows = attributes.get(key)
        if not isinstance(rows, list) or not rows:
            continue
        result: list[SolarInterval] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            start = _instant(row.get("period_start"))
            mean = _kw(row.get("pv_estimate"))
            if start is None or mean is None:
                continue
            result.append(SolarInterval(
                start, start + timedelta(minutes=minutes), mean,
                _kw(row.get("pv_estimate10")), _kw(row.get("pv_estimate90")),
            ))
        if result:
            return result
    return []


def _watts_intervals(attributes: Mapping[str, Any]) -> list[SolarInterval]:
    watts = attributes.get("watts")
    if not isinstance(watts, Mapping) or not watts:
        return []
    points = sorted(
        (instant, value / 1000)
        for raw_instant, raw_value in watts.items()
        if (instant := _instant(raw_instant)) is not None
        and (value := _kw(raw_value)) is not None
    )
    result: list[SolarInterval] = []
    for index, (start, mean) in enumerate(points):
        if index + 1 < len(points):
            step = points[index + 1][0] - start
            if step > timedelta(hours=1):
                step = timedelta(hours=1)
        else:
            step = timedelta(hours=1) if len(points) < 2 else points[-1][0] - points[-2][0]
        result.append(SolarInterval(start, start + step, mean))
    return result


def average_kw(
    intervals: Sequence[SolarInterval],
    start: datetime,
    end: datetime,
    *,
    conservative: bool = False,
) -> float | None:
    """Time-weighted mean forecast over [start, end); None outside coverage."""
    total = end - start
    if total.total_seconds() <= 0:
        return None
    covered = 0.0
    energy = 0.0
    for interval in intervals:
        overlap_start = max(start, interval.start)
        overlap_end = min(end, interval.end)
        seconds = (overlap_end - overlap_start).total_seconds()
        if seconds <= 0:
            continue
        value = interval.mean_kw
        if conservative:
            value = interval.p10_kw if interval.p10_kw is not None else interval.mean_kw * 0.7
        energy += value * seconds
        covered += seconds
    if covered < 0.9 * total.total_seconds():
        return None
    return energy / covered


@dataclass(slots=True)
class SolarCorrection:
    """Intraday measured/forecast ratio from one-minute samples."""

    samples: list[tuple[datetime, float, float]] = field(default_factory=list)
    window: timedelta = timedelta(minutes=90)

    def add(self, instant: datetime, measured_kw: float, forecast_kw: float) -> None:
        if not (isfinite(measured_kw) and isfinite(forecast_kw)):
            return
        self.samples.append((instant, max(0.0, measured_kw), max(0.0, forecast_kw)))
        cutoff = instant - self.window
        self.samples = [item for item in self.samples if item[0] >= cutoff]

    def ratio(self, now: datetime, window: timedelta | None = None) -> float | None:
        """Measured over forecast energy, None while the forecast is too small."""
        cutoff = now - (window or self.window)
        rows = [item for item in self.samples if item[0] >= cutoff]
        forecast = sum(item[2] for item in rows)
        if not rows or forecast / 60 < 0.15:  # < 0.15 kWh forecast in the window
            return None
        return min(1.5, max(0.2, sum(item[1] for item in rows) / forecast))

    def factor(self, now: datetime, at: datetime) -> float:
        ratio = self.ratio(now)
        if ratio is None:
            return 1.0
        if ratio < 1:
            return ratio if at.astimezone(STOCKHOLM).date() == now.astimezone(
                STOCKHOLM
            ).date() else 1.0
        fade = max(0.0, 1 - (at - now).total_seconds() / 7200)
        return 1 + (ratio - 1) * fade


__all__ = [
    "HOURS_OF_WEEK",
    "HourSample",
    "LoadModel",
    "SolarCorrection",
    "SolarInterval",
    "average_kw",
    "heating_degrees",
    "hour_of_week",
    "parse_solar_forecast",
    "quantile",
]
