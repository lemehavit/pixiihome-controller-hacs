"""Learn household load, read solar forecasts and measure battery savings.

The service only reads: Pixii telemetry, the configured solar forecast,
solar power and temperature entities, the price entity and Home Assistant's
long-term statistics. It never sends a command. Planners ask it for load and
solar forecasts; when it has none, they keep their previous behaviour.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .activity import log_activity
from .const import DOMAIN
from .control.energy_forecast import (
    STOCKHOLM,
    LoadModel,
    SolarCorrection,
    SolarInterval,
    average_kw,
    parse_solar_forecast,
)
from .control.price_optimization import CONF_PRICE_OPTIMIZATION, normalize_price_series
from .control.savings import SavingsTracker

if TYPE_CHECKING:
    from .runtime import RuntimeData

CONF_ENERGY_FORECAST = "energy_forecast"
FORECAST_STATUS = "energy_forecast_status"
SAMPLE_SECONDS = 60
BOOTSTRAP_DAYS = 70
STORE_VERSION = 1
MAX_SOLAR_FORECAST_AGE = timedelta(hours=26)
ERROR_DAYS = 14


@dataclass(frozen=True, slots=True)
class EnergyForecastConfig:
    """Entities that feed the forecasts (all optional)."""

    solar_forecast_entity_ids: tuple[str, ...] = ()
    solar_power_entity_id: str | None = None
    temperature_entity_id: str | None = None

    @classmethod
    def from_mapping(cls, value: object) -> EnergyForecastConfig:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("energy_forecast_config_invalid")
        raw = value.get("solar_forecast_entity_ids") or ()
        if isinstance(raw, str):
            raw = (raw,)
        if not isinstance(raw, list | tuple) or not all(
            isinstance(item, str) and item.startswith("sensor.") for item in raw
        ):
            raise ValueError("solar_forecast_entity_invalid")
        power = value.get("solar_power_entity_id") or None
        if power is not None and (not isinstance(power, str) or not power.startswith("sensor.")):
            raise ValueError("solar_power_entity_invalid")
        temperature = value.get("temperature_entity_id") or None
        if temperature is not None and (
            not isinstance(temperature, str)
            or not temperature.startswith(("sensor.", "weather."))
        ):
            raise ValueError("temperature_entity_invalid")
        return cls(tuple(dict.fromkeys(raw)), power, temperature)

    def as_dict(self) -> dict[str, object]:
        return {
            "solar_forecast_entity_ids": list(self.solar_forecast_entity_ids),
            "solar_power_entity_id": self.solar_power_entity_id,
            "temperature_entity_id": self.temperature_entity_id,
        }

    @property
    def signature(self) -> str:
        # Only the solar power binding changes what a learned load means.
        return f"load=grid-battery+solar:{self.solar_power_entity_id or 'none'}"


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


class EnergyForecastService:
    """Sample once a minute; expose load, solar and savings to planners and sensors."""

    def __init__(self, runtime: RuntimeData, *, clock: Callable[[], datetime] | None = None):
        self.runtime = runtime
        self.clock = clock or (lambda: datetime.now(UTC))
        self.model = LoadModel()
        self.correction = SolarCorrection()
        self.savings = SavingsTracker()
        self.status = "starting"
        self.reason: str | None = None
        self.load_w: float | None = None
        self.solar_w: float | None = None
        self.temperature_c: float | None = None
        self.bootstrap_hours = 0
        self.bootstrap_signature: str | None = None
        self.daily: dict[str, dict[str, float]] = {}
        self.errors: list[dict[str, Any]] = []
        self._quarter: datetime | None = None
        self._quarter_sum = 0.0
        self._quarter_count = 0
        self._quarter_temperature: list[float] = []
        self._solar_cache: tuple[tuple[Any, ...], tuple[SolarInterval, ...]] | None = None
        self._price_cache: tuple[Any, Any] | None = None
        self._last_sample: datetime | None = None
        self._store: Store[dict[str, Any]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._listeners: list[Callable[[], None]] = []

    # ----- configuration and lifecycle -------------------------------------------------

    @property
    def config(self) -> EnergyForecastConfig:
        entry = self.runtime.entry
        try:
            return EnergyForecastConfig.from_mapping(
                None if entry is None else entry.options.get(CONF_ENERGY_FORECAST)
            )
        except ValueError:
            return EnergyForecastConfig()

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 - presentation must not stop sampling
                continue

    async def start(self) -> None:
        entry = self.runtime.entry
        if entry is not None:
            try:
                store: Store[dict[str, Any]] = Store(
                    self.runtime.hass, STORE_VERSION,
                    f"{DOMAIN}.{entry.entry_id}.energy_forecast",
                )
                self._store = store
                stored = await store.async_load()
            except Exception:  # noqa: BLE001 - a broken file only restarts learning
                stored = None
            self._restore(stored)
        if self._task is None:
            self._task = self.runtime.hass.async_create_background_task(
                self._run(), "pixiihome_controller energy forecast"
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self.async_save()

    def _restore(self, stored: object) -> None:
        if not isinstance(stored, Mapping):
            return
        signature = stored.get("signature")
        if signature == self.config.signature:
            self.model = LoadModel.from_dict(stored.get("model"))
            self.bootstrap_hours = int(stored.get("bootstrap_hours", 0) or 0)
            self.bootstrap_signature = signature
        self.savings = SavingsTracker.from_dict(stored.get("savings"))
        daily = stored.get("daily")
        if isinstance(daily, Mapping):
            self.daily = {
                str(day): {str(k): float(v) for k, v in values.items()
                           if isinstance(v, int | float)}
                for day, values in daily.items() if isinstance(values, Mapping)
            }
        errors = stored.get("errors")
        if isinstance(errors, list):
            self.errors = [item for item in errors if isinstance(item, dict)][-ERROR_DAYS:]

    def _data(self) -> dict[str, Any]:
        return {
            "signature": self.config.signature,
            "model": self.model.as_dict(),
            "bootstrap_hours": self.bootstrap_hours,
            "savings": self.savings.as_dict(),
            "daily": self.daily,
            "errors": self.errors,
        }

    async def async_save(self) -> None:
        if self._store is not None:
            try:
                await self._store.async_save(self._data())
            except Exception:  # noqa: BLE001 - saving is best effort
                return

    async def _run(self) -> None:
        if self.bootstrap_signature != self.config.signature:
            try:
                await asyncio.wait_for(self.async_bootstrap(), timeout=120)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - history is optional; live learning goes on
                self.reason = "history_unavailable"
        saved = self.clock()
        while True:
            try:
                self.sample()
            except Exception:  # noqa: BLE001 - one bad sample must not stop the service
                self.status, self.reason = "error", "sample_failed"
            if self.clock() - saved >= timedelta(minutes=10):
                saved = self.clock()
                await self.async_save()
            await asyncio.sleep(SAMPLE_SECONDS)

    # ----- history bootstrap -----------------------------------------------------------

    async def async_bootstrap(self) -> None:
        """Seed the load model from Home Assistant's hourly long-term statistics."""
        from homeassistant.components.recorder.statistics import statistics_during_period
        from homeassistant.helpers import entity_registry as er
        from homeassistant.helpers.recorder import get_instance

        config = self.config
        registry = er.async_get(self.runtime.hass)
        serial = self.runtime.serial
        meter = registry.async_get_entity_id("sensor", DOMAIN, f"{serial}_meter_power")
        battery = registry.async_get_entity_id("sensor", DOMAIN, f"{serial}_battery_power")
        self.model = LoadModel()
        self.bootstrap_hours = 0
        self.bootstrap_signature = config.signature
        if meter is None or battery is None:
            self.reason = "history_entities_missing"
            return
        ids = {meter, battery}
        if config.solar_power_entity_id:
            ids.add(config.solar_power_entity_id)
        temperature_id = config.temperature_entity_id
        if temperature_id and temperature_id.startswith("sensor."):
            ids.add(temperature_id)
        end = self.clock().replace(minute=0, second=0, microsecond=0)
        start = end - timedelta(days=BOOTSTRAP_DAYS)
        rows = await get_instance(self.runtime.hass).async_add_executor_job(
            statistics_during_period, self.runtime.hass, start, end, ids, "hour",
            {"power": "W", "temperature": "°C"}, {"mean"},
        )
        hours = learn_from_hourly_statistics(
            self.model, rows, meter, battery, config.solar_power_entity_id,
            temperature_id if temperature_id and temperature_id.startswith("sensor.") else None,
        )
        self.bootstrap_hours = hours
        if hours:
            log_activity(self.runtime, FORECAST_STATUS,
                         f"Learned consumption from {hours} hours of Home Assistant history")
        await self.async_save()

    # ----- sampling --------------------------------------------------------------------

    def _entity_number(self, entity_id: str | None, *, power: bool = False) -> float | None:
        if not entity_id:
            return None
        state = self.runtime.hass.states.get(entity_id)
        if state is None:
            return None
        if entity_id.startswith("weather."):
            return _number(state.attributes.get("temperature"))
        value = _number(state.state)
        if value is None:
            return None
        if power:
            unit = str(state.attributes.get("unit_of_measurement") or "W")
            if unit == "kW":
                value *= 1000
            elif unit == "MW":
                value *= 1_000_000
            value = max(0.0, value)
        return value

    def sample(self) -> None:
        now = self.clock()
        config = self.config
        telemetry = self.runtime.telemetry
        battery = self.runtime.battery
        meter = telemetry.value("meter", "meter_w")
        meter_w = _number(meter) if telemetry.is_fresh("meter") else None
        battery_w = battery.power_w if telemetry.is_fresh("battery") else None
        solar_w = self._entity_number(config.solar_power_entity_id, power=True)
        self.solar_w = solar_w
        self.temperature_c = self._entity_number(config.temperature_entity_id)
        hours = 0.0
        if self._last_sample is not None:
            hours = min(0.25, (now - self._last_sample).total_seconds() / 3600)
        self._last_sample = now
        if meter_w is None or battery_w is None or (
            config.solar_power_entity_id and solar_w is None
        ):
            self.load_w = None
            self.status, self.reason = "waiting_telemetry", "load_measurement_incomplete"
            self._notify()
            return
        load = meter_w - battery_w + (solar_w or 0.0)
        self.load_w = max(0.0, load) if load > -300 else None
        if solar_w is not None:
            forecast = self.solar_forecast_kw(now - timedelta(minutes=1), now, corrected=False)
            if forecast is not None:
                self.correction.add(now, solar_w / 1000, forecast)
        self._track_daily(now, hours)
        if self.load_w is not None:
            self._accumulate(now)
        price = self.price_at(now)
        if price is not None and hours > 0:
            finished = self.savings.add(
                now, meter_w, battery_w, price,
                float(self.runtime.operational_settings.get("price_grid_import_cost_sek_per_kwh")),
                hours,
            )
            if finished is not None:
                log_activity(self.runtime, "battery_savings_today",
                             f"Battery saved {finished[1]:.2f} kr on {finished[0]}")
        else:
            self.savings.roll(now)
        self.status = "learning" if self.model.learned_days() < 7 else "ready"
        self.reason = None if self.has_solar_forecast() or not config.solar_forecast_entity_ids \
            else "solar_forecast_unavailable"
        self._notify()

    def _accumulate(self, now: datetime) -> None:
        quarter = now.replace(minute=now.minute - now.minute % 15, second=0, microsecond=0)
        if self._quarter is not None and quarter != self._quarter:
            if self._quarter_count >= 8:
                mean = self._quarter_sum / self._quarter_count
                temperature = (
                    sum(self._quarter_temperature) / len(self._quarter_temperature)
                    if self._quarter_temperature else None
                )
                self.model.update(self._quarter + timedelta(minutes=7, seconds=30), mean,
                                  temperature)
            self._quarter_sum, self._quarter_count = 0.0, 0
            self._quarter_temperature = []
        self._quarter = quarter
        assert self.load_w is not None
        self._quarter_sum += self.load_w
        self._quarter_count += 1
        if self.temperature_c is not None:
            self._quarter_temperature.append(self.temperature_c)

    def _track_daily(self, now: datetime, hours: float) -> None:
        """Issue a forecast for each day at its start; score it when the day ends."""
        day = now.astimezone(STOCKHOLM).date().isoformat()
        if day not in self.daily:
            for old_day, values in sorted(self.daily.items()):
                if old_day < day and values.get("actual_hours", 0) >= 22:
                    self.errors.append({
                        "day": old_day,
                        "load_forecast_kwh": round(values.get("load_forecast_kwh", 0.0), 2),
                        "load_actual_kwh": round(values.get("load_kwh", 0.0), 2),
                        "solar_forecast_kwh": round(values.get("solar_forecast_kwh", -1), 2),
                        "solar_actual_kwh": round(values.get("solar_kwh", 0.0), 2),
                    })
            self.errors = self.errors[-ERROR_DAYS:]
            self.daily = {day: self._issue_day(now)}
        values = self.daily[day]
        if hours > 0 and self.load_w is not None:
            values["load_kwh"] = values.get("load_kwh", 0.0) + self.load_w / 1000 * hours
            values["actual_hours"] = values.get("actual_hours", 0.0) + hours
        if hours > 0 and self.solar_w is not None:
            values["solar_kwh"] = values.get("solar_kwh", 0.0) + self.solar_w / 1000 * hours

    def _issue_day(self, now: datetime) -> dict[str, float]:
        local = now.astimezone(STOCKHOLM)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        end = (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
        end_utc = end.astimezone(UTC)
        values: dict[str, float] = {
            "load_forecast_kwh": self.load_energy_kwh(start, end_utc),
        }
        solar = self.solar_energy_kwh(start, end_utc, corrected=False)
        if solar is not None:
            values["solar_forecast_kwh"] = solar
        return values

    # ----- prices ----------------------------------------------------------------------

    def price_intervals(self) -> tuple[Any, ...]:
        """Current price series intervals, empty when the source is unusable."""
        self.price_at(self.clock())
        return () if self._price_cache is None else tuple(self._price_cache[1].intervals)

    def price_at(self, instant: datetime) -> float | None:
        entry = self.runtime.entry
        raw = None if entry is None else entry.options.get(CONF_PRICE_OPTIMIZATION)
        entity_id = raw.get("price_entity_id") if isinstance(raw, Mapping) else None
        if not isinstance(entity_id, str):
            return None
        state = self.runtime.hass.states.get(entity_id)
        if state is None or state.state in {"unknown", "unavailable"}:
            return None
        key = (entity_id, state.last_updated)
        if self._price_cache is None or self._price_cache[0] != key:
            try:
                series = normalize_price_series(
                    state.attributes, state.attributes.get("unit_of_measurement")
                )
            except ValueError:
                return None
            self._price_cache = (key, series)
        series = self._price_cache[1]
        for interval in series.intervals:
            if interval.start <= instant < interval.end:
                return interval.price_sek_per_kwh
        return None

    # ----- forecasts -------------------------------------------------------------------

    def solar_intervals(self) -> tuple[SolarInterval, ...]:
        ids = self.config.solar_forecast_entity_ids
        states = [self.runtime.hass.states.get(entity_id) for entity_id in ids]
        now = self.clock()
        fresh = [
            state for state in states
            if state is not None and state.state not in {"unknown", "unavailable"}
            and now - state.last_updated.astimezone(UTC) <= MAX_SOLAR_FORECAST_AGE
        ]
        key = tuple((state.entity_id, state.last_updated) for state in fresh)
        if self._solar_cache is None or self._solar_cache[0] != key:
            self._solar_cache = (key, parse_solar_forecast(state.attributes for state in fresh))
        return self._solar_cache[1]

    def has_solar_forecast(self) -> bool:
        now = self.clock()
        intervals = self.solar_intervals()
        return bool(intervals) and intervals[-1].end > now + timedelta(hours=6)

    def solar_forecast_kw(
        self,
        start: datetime,
        end: datetime,
        *,
        conservative: bool = False,
        corrected: bool = True,
    ) -> float | None:
        if not self.config.solar_forecast_entity_ids:
            return None
        value = average_kw(self.solar_intervals(), start, end, conservative=conservative)
        if value is None:
            return None
        if corrected and self.config.solar_power_entity_id:
            value *= self.correction.factor(self.clock(), start + (end - start) / 2)
        return value

    def solar_energy_kwh(
        self, start: datetime, end: datetime, *, conservative: bool = False,
        corrected: bool = True,
    ) -> float | None:
        value = self.solar_forecast_kw(start, end, conservative=conservative, corrected=corrected)
        return None if value is None else value * (end - start).total_seconds() / 3600

    def load_kw(self, start: datetime, end: datetime, *, high: bool = False) -> float:
        """Mean forecast household load over [start, end) in kW."""
        total = 0.0
        weight = 0.0
        cursor = start
        while cursor < end:
            step_end = min(end, cursor + timedelta(minutes=15))
            middle = cursor + (step_end - cursor) / 2
            seconds = (step_end - cursor).total_seconds()
            value = (
                self.model.predict_high_w(middle, self.temperature_c)
                if high else self.model.predict_w(middle, self.temperature_c)
            )
            total += value * seconds
            weight += seconds
            cursor = step_end
        return total / weight / 1000 if weight else 0.0

    def load_energy_kwh(self, start: datetime, end: datetime, *, high: bool = False) -> float:
        return self.load_kw(start, end, high=high) * (end - start).total_seconds() / 3600

    def net_forecast(
        self, start: datetime, end: datetime, *, high: bool = False
    ) -> tuple[float, float] | None:
        """(load_kw, solar_kw); solar 0 when no forecast entity is configured."""
        load = self.load_kw(start, end, high=high)
        if not self.config.solar_forecast_entity_ids:
            return load, 0.0
        solar = self.solar_forecast_kw(start, end)
        return None if solar is None else (load, solar)

    def net_load_kw(self, start: datetime, end: datetime, *, high: bool = False) -> float | None:
        flows = self.net_forecast(start, end, high=high)
        return None if flows is None else max(0.0, flows[0] - flows[1])

    # ----- presentation ----------------------------------------------------------------

    def load_error_percent(self) -> float | None:
        rows = [row for row in self.errors[-7:] if row.get("load_actual_kwh", 0) > 1]
        if not rows:
            return None
        return 100 * sum(
            abs(row["load_forecast_kwh"] - row["load_actual_kwh"]) for row in rows
        ) / sum(row["load_actual_kwh"] for row in rows)

    def solar_error_percent(self) -> float | None:
        rows = [row for row in self.errors[-7:]
                if row.get("solar_forecast_kwh", -1) >= 0 and row.get("solar_actual_kwh", 0) > 1]
        if not rows:
            return None
        return 100 * sum(
            abs(row["solar_forecast_kwh"] - row["solar_actual_kwh"]) for row in rows
        ) / sum(row["solar_actual_kwh"] for row in rows)

    def load_forecast_attributes(self) -> dict[str, Any]:
        now = self.clock()
        start = now.replace(minute=0, second=0, microsecond=0)
        hourly = []
        for hour in range(24):
            slot = start + timedelta(hours=hour)
            hourly.append({
                "start": slot.isoformat(),
                "kw": round(self.load_kw(slot, slot + timedelta(hours=1)), 3),
                "high_kw": round(self.load_kw(slot, slot + timedelta(hours=1), high=True), 3),
            })
        return {
            "reason": self.reason,
            "current_household_load_w": None if self.load_w is None else round(self.load_w),
            "learned_days": self.model.learned_days(),
            "learned_samples": self.model.samples,
            "history_hours_used": self.bootstrap_hours,
            "heating_w_per_degree": round(self.model.heating_w_per_c, 1),
            "temperature_c": self.temperature_c,
            "solar_power_measured": bool(self.config.solar_power_entity_id),
            "mean_abs_error_w": None if self.model.mae_w is None else round(self.model.mae_w),
            "forecast_error_percent_7d": _rounded(self.load_error_percent()),
            "daily_scores": self.errors[-7:],
            "forecast": hourly,
        }

    def solar_forecast_attributes(self) -> dict[str, Any]:
        now = self.clock()
        end_of_day = (
            now.astimezone(STOCKHOLM).replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        ).astimezone(UTC)
        ratio = self.correction.ratio(now)
        return {
            "reason": self.reason,
            "forecast_entities": list(self.config.solar_forecast_entity_ids),
            "forecast_available": self.has_solar_forecast(),
            "remaining_today_p10_kwh": _rounded(
                self.solar_energy_kwh(now, end_of_day, conservative=True)
            ),
            "remaining_today_uncorrected_kwh": _rounded(
                self.solar_energy_kwh(now, end_of_day, corrected=False)
            ),
            "intraday_correction": None if ratio is None else round(ratio, 3),
            "measured_solar_w": None if self.solar_w is None else round(self.solar_w),
            "forecast_error_percent_7d": _rounded(self.solar_error_percent()),
        }

    def remaining_solar_today_kwh(self) -> float | None:
        now = self.clock()
        end_of_day = (
            now.astimezone(STOCKHOLM).replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1)
        ).astimezone(UTC)
        return self.solar_energy_kwh(now, end_of_day)


def learn_from_hourly_statistics(
    model: LoadModel,
    rows: Mapping[str, Sequence[Mapping[str, Any]]],
    meter_id: str,
    battery_id: str,
    solar_id: str | None,
    temperature_id: str | None,
) -> int:
    """Feed hourly means (load = grid − battery + solar) into ``model``."""

    def by_start(statistic_id: str | None) -> dict[float, float]:
        if statistic_id is None:
            return {}
        result: dict[float, float] = {}
        for row in rows.get(statistic_id, []):
            start = row.get("start")
            mean = row.get("mean")
            if isinstance(start, int | float) and isinstance(mean, int | float):
                result[float(start)] = float(mean)
        return result

    meter = by_start(meter_id)
    battery = by_start(battery_id)
    solar = by_start(solar_id)
    temperature = by_start(temperature_id)
    used = 0
    for start in sorted(meter):
        if start not in battery or (solar_id is not None and start not in solar):
            continue
        load = meter[start] - battery[start] + max(0.0, solar.get(start, 0.0))
        if load < -300:
            continue
        instant = datetime.fromtimestamp(start, UTC) + timedelta(minutes=30)
        if model.update(instant, max(0.0, load), temperature.get(start), hours=1.0):
            used += 1
    return used


def _rounded(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


__all__ = [
    "CONF_ENERGY_FORECAST",
    "EnergyForecastConfig",
    "EnergyForecastService",
    "learn_from_hourly_statistics",
]
