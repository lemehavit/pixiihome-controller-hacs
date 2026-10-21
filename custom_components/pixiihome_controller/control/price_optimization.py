"""Pure electricity-price normalization and bounded battery planning."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from math import isfinite, sqrt
from typing import Any
from zoneinfo import ZoneInfo

CONF_PRICE_OPTIMIZATION = "price_optimization"
STOCKHOLM = ZoneInfo("Europe/Stockholm")
SUPPORTED_PRICE_UNITS = frozenset({"SEK/kWh", "öre/kWh"})
_MONTHS = {
    name: index
    for index, name in enumerate(
        ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"),
        1,
    )
}


@dataclass(frozen=True, slots=True)
class PriceOptimizationConfig:
    """Technical bindings that cannot be represented by number entities."""

    price_entity_id: str | None = None
    managed_source_id: int | None = None
    planning_horizon_hours: int = 36
    minimum_lead_minutes: int = 5
    refresh_minutes: int = 60

    @classmethod
    def from_mapping(cls, value: object) -> PriceOptimizationConfig:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise ValueError("price_optimization_config_invalid")
        entity = value.get("price_entity_id")
        if entity in (None, ""):
            entity_id = None
        elif isinstance(entity, str) and entity.startswith("sensor."):
            entity_id = entity
        else:
            raise ValueError("price_entity_invalid")
        raw_source = value.get("managed_source_id")
        if raw_source in (None, ""):
            source_id = None
        elif isinstance(raw_source, bool):
            raise ValueError("managed_source_invalid")
        else:
            source_id = int(raw_source)
            if not 0 <= source_id <= 2_147_483_647:
                raise ValueError("managed_source_invalid")
        horizon = _integer(value.get("planning_horizon_hours", 36), 12, 72)
        lead = _integer(value.get("minimum_lead_minutes", 5), 1, 60)
        refresh = _integer(value.get("refresh_minutes", 60), 5, 360)
        return cls(entity_id, source_id, horizon, lead, refresh)

    def as_dict(self) -> dict[str, object]:
        return {
            "price_entity_id": self.price_entity_id or "",
            "managed_source_id": self.managed_source_id,
            "planning_horizon_hours": self.planning_horizon_hours,
            "minimum_lead_minutes": self.minimum_lead_minutes,
            "refresh_minutes": self.refresh_minutes,
        }


@dataclass(frozen=True, slots=True)
class PriceInterval:
    start: datetime
    end: datetime
    price_sek_per_kwh: float

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError("price_interval_invalid")
        if not isfinite(self.price_sek_per_kwh):
            raise ValueError("price_invalid")
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "end", self.end.astimezone(UTC))

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "price_sek_per_kwh": round(self.price_sek_per_kwh, 6),
        }


@dataclass(frozen=True, slots=True)
class PriceSeries:
    intervals: tuple[PriceInterval, ...]
    unit: str
    source_format: str
    fingerprint: str


class PriceDirection(StrEnum):
    CHARGE = "charge"
    DISCHARGE = "discharge"


class PriceDischargeMode(StrEnum):
    SELF_CONSUMPTION = "self_consumption"
    EXPORT = "export"


@dataclass(frozen=True, slots=True)
class PricePlanAction:
    start: datetime
    end: datetime
    direction: PriceDirection
    power_kw: float
    service: str
    values: Mapping[str, Any]
    discharge_mode: PriceDischargeMode | None
    price_sek_per_kwh: float
    reference_price_sek_per_kwh: float
    spread_sek_per_kwh: float
    projected_usable_soc_percent: float
    expected_margin_sek: float
    net_profit_sek_per_kwh: float = 0.0

    @property
    def duration_seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    def as_dict(self) -> dict[str, object]:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_seconds": self.duration_seconds,
            "direction": self.direction.value,
            "power_kw": round(self.power_kw, 3),
            "service": self.service,
            "values": dict(self.values),
            "discharge_mode": self.discharge_mode.value if self.discharge_mode else None,
            "price_sek_per_kwh": round(self.price_sek_per_kwh, 4),
            "reference_price_sek_per_kwh": round(self.reference_price_sek_per_kwh, 4),
            "spread_sek_per_kwh": round(self.spread_sek_per_kwh, 4),
            "projected_usable_soc_percent": round(self.projected_usable_soc_percent, 2),
            "expected_margin_sek": round(self.expected_margin_sek, 2),
            "net_profit_sek_per_kwh": round(self.net_profit_sek_per_kwh, 6),
        }


@dataclass(frozen=True, slots=True)
class PricePlan:
    actions: tuple[PricePlanAction, ...]
    blockers: tuple[str, ...]
    fingerprint: str
    minimum_projected_soc: float
    maximum_projected_soc: float
    expected_margin_sek: float

    @property
    def ready(self) -> bool:
        return not self.blockers

    def as_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "blockers": list(self.blockers),
            "fingerprint": self.fingerprint,
            "actions": [action.as_dict() for action in self.actions],
            "minimum_projected_soc": round(self.minimum_projected_soc, 2),
            "maximum_projected_soc": round(self.maximum_projected_soc, 2),
            "expected_margin_sek": round(self.expected_margin_sek, 2),
        }


def normalize_price_series(attributes: Mapping[str, Any], unit: object) -> PriceSeries:
    """Normalize supported Home Assistant price-entity attributes."""
    if not isinstance(unit, str) or unit not in SUPPORTED_PRICE_UNITS:
        raise ValueError("price_unit_unsupported")
    factor = 0.01 if unit == "öre/kWh" else 1.0
    raw_items: list[Mapping[str, Any]] = []
    source_format = ""
    for keys, candidate_format in (
        (("previous_day", "current_day", "next_day"), "greenely_day_arrays"),
        (("raw_today", "raw_tomorrow"), "timestamped_day_arrays"),
        (("prices",), "timestamped_prices"),
    ):
        collected: list[Mapping[str, Any]] = []
        for key in keys:
            value = attributes.get(key, [])
            if value is None:
                continue
            if not isinstance(value, Sequence) or isinstance(value, str | bytes):
                raise ValueError("price_series_invalid")
            if any(not isinstance(item, Mapping) for item in value):
                raise ValueError("price_series_invalid")
            collected.extend(value)  # type: ignore[arg-type]
        if collected:
            raw_items = collected
            source_format = candidate_format
            break
    if not raw_items:
        raise ValueError("price_series_empty")
    if source_format == "greenely_day_arrays":
        intervals = _greenely_intervals(raw_items, factor)
    else:
        intervals = _timestamped_intervals(raw_items, factor)
    ordered = tuple(sorted(intervals, key=lambda item: item.start))
    if len({item.start for item in ordered}) != len(ordered):
        raise ValueError("price_interval_duplicate")
    encoded = json.dumps(
        [item.as_dict() for item in ordered], sort_keys=True, separators=(",", ":")
    ).encode()
    return PriceSeries(ordered, unit, source_format, sha256(encoded).hexdigest())


def build_price_plan(
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
    end_before: datetime | None = None,
) -> PricePlan:
    """Plan using minimum net profit per AC kWh delivered by the battery.

    The legacy ``*_difference`` parameter names remain for persisted-entity
    compatibility. Their values are net-profit floors, after all costs.
    """
    if now.tzinfo is None:
        raise ValueError("price_plan_now_naive")
    numeric = (
        usable_soc_percent,
        usable_capacity_kwh,
        minimum_soc_percent,
        maximum_soc_percent,
        maximum_charge_kw,
        maximum_discharge_kw,
        minimum_difference_sek_per_kwh,
        export_minimum_difference_sek_per_kwh,
        round_trip_efficiency_percent,
        cycle_cost_sek_per_kwh,
        grid_import_cost_sek_per_kwh,
    )
    if not all(isfinite(value) for value in numeric):
        raise ValueError("price_plan_setting_invalid")
    if not 0 <= minimum_soc_percent < maximum_soc_percent <= 100:
        raise ValueError("price_soc_limits_invalid")
    if not 0 <= usable_soc_percent <= 100:
        raise ValueError("price_usable_soc_invalid")
    if usable_capacity_kwh <= 0 or maximum_charge_kw < 0 or maximum_discharge_kw < 0:
        raise ValueError("price_power_or_capacity_invalid")
    if minimum_difference_sek_per_kwh < 0 or export_minimum_difference_sek_per_kwh < 0:
        raise ValueError("price_difference_invalid")
    if (
        not 0 < round_trip_efficiency_percent <= 100
        or cycle_cost_sek_per_kwh < 0
        or grid_import_cost_sek_per_kwh < 0
    ):
        raise ValueError("price_efficiency_or_cycle_cost_invalid")

    current = now.astimezone(UTC)
    start_limit = current + timedelta(minutes=minimum_lead_minutes)
    end_limit = current + timedelta(hours=horizon_hours)
    if end_before is not None:
        if end_before.tzinfo is None:
            raise ValueError("price_plan_end_naive")
        end_limit = min(end_limit, end_before.astimezone(UTC))
    candidates = tuple(
        item for item in series.intervals if item.start >= start_limit and item.end <= end_limit
    )
    if not candidates:
        return _blocked_plan(series, ("future_price_horizon_empty",), usable_soc_percent)
    if any(a.end != b.start for a, b in zip(candidates, candidates[1:], strict=False)):
        return _blocked_plan(series, ("future_price_horizon_incomplete",), usable_soc_percent)

    charge_efficiency = sqrt(round_trip_efficiency_percent / 100)
    discharge_efficiency = charge_efficiency
    energy = usable_capacity_kwh * usable_soc_percent / 100
    minimum_energy = usable_capacity_kwh * minimum_soc_percent / 100
    maximum_energy = usable_capacity_kwh * maximum_soc_percent / 100
    # The acquisition cost of the initial stored energy is unknown. Use the
    # latest known price (or first candidate) as an explicit estimate, never a
    # cheap historical interval which this battery may not have charged in.
    known = [item for item in series.intervals if item.start <= current]
    acquisition_price = known[-1].price_sek_per_kwh if known else candidates[0].price_sek_per_kwh
    min_soc = max_soc = usable_soc_percent
    expected_margin = 0.0
    actions: list[PricePlanAction] = []

    for index, item in enumerate(candidates):
        price = item.price_sek_per_kwh
        hours = item.duration_hours
        future_high = max(
            (future.price_sek_per_kwh for future in candidates[index + 1 :]),
            default=price,
        )
        future_low = min(
            (future.price_sek_per_kwh for future in candidates[index:]),
            default=price,
        )
        future_peak = max(
            (future.price_sek_per_kwh for future in candidates[index:]),
            default=price,
        )
        charge_spread = future_high - price
        charge_mode, charge_net = _qualifying_discharge(
            price, future_high,
            efficiency=round_trip_efficiency_percent / 100,
            grid_cost=grid_import_cost_sek_per_kwh,
            cycle_cost=cycle_cost_sek_per_kwh,
            minimum_profit=minimum_difference_sek_per_kwh,
            export_allowed=export_allowed,
            export_minimum_profit=export_minimum_difference_sek_per_kwh,
        )
        # Feature limits constrain the direction, not the initial battery state.
        # Another controller may have charged above this feature's ceiling (or
        # discharged below its reserve). Keep the real energy; never clamp it.
        headroom = max(0.0, maximum_energy - energy)
        available = max(0.0, energy - minimum_energy)
        action: PricePlanAction | None = None

        if (
            maximum_charge_kw > 0
            and maximum_discharge_kw > 0
            and headroom > 1e-6
            and index + 1 < len(candidates)
            and price <= future_low + 1e-9
            and charge_mode is not None
        ):
            power = min(maximum_charge_kw, headroom / (hours * charge_efficiency))
            if power < 0.05:
                continue
            added_energy = power * hours * charge_efficiency
            acquisition_price = (energy * acquisition_price + added_energy * price) / (
                energy + added_energy
            )
            energy += added_energy
            projected_soc = energy / usable_capacity_kwh * 100
            action = PricePlanAction(
                item.start,
                item.end,
                PriceDirection.CHARGE,
                power,
                "demandresponse",
                {"pacref": round(power * 1000, 3)},
                None,
                price,
                future_high,
                charge_spread,
                projected_soc,
                0.0,
                charge_net,
            )
        else:
            mode, discharge_net = _qualifying_discharge(
                acquisition_price, price,
                efficiency=round_trip_efficiency_percent / 100,
                grid_cost=grid_import_cost_sek_per_kwh,
                cycle_cost=cycle_cost_sek_per_kwh,
                minimum_profit=minimum_difference_sek_per_kwh,
                export_allowed=export_allowed,
                export_minimum_profit=export_minimum_difference_sek_per_kwh,
            )
            if (
                maximum_discharge_kw > 0
                and available > 1e-6
                and price >= future_peak - 1e-9
                and mode is not None
            ):
                power = min(maximum_discharge_kw, available * discharge_efficiency / hours)
                if power < 0.05:
                    continue
                delivered = power * hours
                energy -= delivered / discharge_efficiency
                projected_soc = energy / usable_capacity_kwh * 100
                margin = delivered * discharge_net
                expected_margin += margin
                if mode is PriceDischargeMode.EXPORT:
                    service = "demandresponse"
                    values: Mapping[str, Any] = {"pacref": round(-power * 1000, 3)}
                else:
                    service = "peakshaving"
                    values = {
                        "exportlim": 0.0,
                        "importlim": 0.0,
                        "charge_max": 0.0,
                        "discharge_max": round(power * 1000, 3),
                        "keep_active": True,
                    }
                action = PricePlanAction(
                    item.start,
                    item.end,
                    PriceDirection.DISCHARGE,
                    power,
                    service,
                    values,
                    mode,
                    price,
                    acquisition_price,
                    price - acquisition_price,
                    projected_soc,
                    margin,
                    discharge_net,
                )
        if action is not None and action.power_kw >= 0.05:
            actions.append(action)
            min_soc = min(min_soc, action.projected_usable_soc_percent)
            max_soc = max(max_soc, action.projected_usable_soc_percent)

    payload = {
        "series": series.fingerprint,
        "actions": [action.as_dict() for action in actions],
        "settings": {
            "economics": "net_profit_per_delivered_ac_kwh_v1",
            "minimum_difference": minimum_difference_sek_per_kwh,
            "export_allowed": export_allowed,
            "export_minimum_difference": export_minimum_difference_sek_per_kwh,
            "grid_import_cost": grid_import_cost_sek_per_kwh,
            "minimum_soc": minimum_soc_percent,
            "maximum_soc": maximum_soc_percent,
        },
    }
    fingerprint = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PricePlan(tuple(actions), (), fingerprint, min_soc, max_soc, expected_margin)


def _qualifying_discharge(
    buy_price: float,
    sell_price: float,
    *,
    efficiency: float,
    grid_cost: float,
    cycle_cost: float,
    minimum_profit: float,
    export_allowed: bool,
    export_minimum_profit: float,
) -> tuple[PriceDischargeMode | None, float]:
    """Compare both uses after losses and costs, without double-counting grid fees."""
    cost = (buy_price + grid_cost) / efficiency + cycle_cost
    export_profit = sell_price - cost
    self_profit = sell_price + grid_cost - cost
    # Export has its own independent floor. If it fails, house consumption
    # remains eligible; allowing export must not disable profitable self-use.
    if export_allowed and export_profit > 1e-9 and export_profit + 1e-9 >= export_minimum_profit:
        return PriceDischargeMode.EXPORT, export_profit
    if self_profit > 1e-9 and self_profit + 1e-9 >= minimum_profit:
        return PriceDischargeMode.SELF_CONSUMPTION, self_profit
    return None, 0.0


def _blocked_plan(series: PriceSeries, blockers: tuple[str, ...], soc: float) -> PricePlan:
    payload = json.dumps(
        {"series": series.fingerprint, "blockers": blockers, "soc": soc},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return PricePlan((), blockers, sha256(payload).hexdigest(), soc, soc, 0.0)


def _greenely_intervals(
    raw_items: Sequence[Mapping[str, Any]], factor: float
) -> tuple[PriceInterval, ...]:
    parsed: list[tuple[datetime, float]] = []
    wall_times: list[datetime] = []
    for item in raw_items:
        date_value, time_value = item.get("date"), item.get("time")
        price = item.get("price")
        if not isinstance(date_value, str) or not isinstance(time_value, str):
            raise ValueError("price_greenely_time_invalid")
        naive = _greenely_wall_time(date_value, time_value)
        wall_times.append(naive)
        parsed.append((naive, _price(price) * factor))
    occurrences: Counter[datetime] = Counter()
    totals = Counter(wall_times)
    result: list[PriceInterval] = []
    for naive, price in parsed:
        occurrence = occurrences[naive]
        occurrences[naive] += 1
        valid = _valid_stockholm_instants(naive)
        if len(valid) > 1:
            if totals[naive] != len(valid) or occurrence >= len(valid):
                raise ValueError("price_wall_time_ambiguous")
            start = valid[occurrence]
        elif len(valid) == 1:
            start = valid[0]
        else:
            raise ValueError("price_wall_time_nonexistent")
        result.append(PriceInterval(start, start + timedelta(hours=1), price))
    return tuple(result)


def _timestamped_intervals(
    raw_items: Sequence[Mapping[str, Any]], factor: float
) -> tuple[PriceInterval, ...]:
    result: list[PriceInterval] = []
    for item in raw_items:
        start = _aware_datetime(item.get("start") or item.get("start_time"))
        raw_end = item.get("end") or item.get("end_time")
        end = _aware_datetime(raw_end) if raw_end is not None else start + timedelta(hours=1)
        raw_price = item.get("price", item.get("value"))
        result.append(PriceInterval(start, end, _price(raw_price) * factor))
    return tuple(result)


def _greenely_wall_time(date_value: str, time_value: str) -> datetime:
    parts = date_value.split()
    if len(parts) != 3 or parts[0] not in _MONTHS:
        raise ValueError("price_greenely_date_invalid")
    clock = time_value.split(":")
    if len(clock) != 2:
        raise ValueError("price_greenely_time_invalid")
    try:
        return datetime(
            int(parts[2]),
            _MONTHS[parts[0]],
            int(parts[1]),
            int(clock[0]),
            int(clock[1]),
        )
    except ValueError as error:
        raise ValueError("price_greenely_time_invalid") from error


def _valid_stockholm_instants(naive: datetime) -> list[datetime]:
    result: list[datetime] = []
    for fold in (0, 1):
        local = naive.replace(tzinfo=STOCKHOLM, fold=fold)
        utc = local.astimezone(UTC)
        if utc.astimezone(STOCKHOLM).replace(tzinfo=None) == naive and utc not in result:
            result.append(utc)
    return sorted(result)


def _aware_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("price_timestamp_invalid") from error
    else:
        raise ValueError("price_timestamp_invalid")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("price_timestamp_timezone_required")
    return parsed


def _price(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("price_value_invalid")
    number = float(value)
    if not isfinite(number):
        raise ValueError("price_value_invalid")
    return number


def _integer(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError("price_optimization_config_invalid")
    number = int(value)  # type: ignore[arg-type]
    if number != float(value) or not minimum <= number <= maximum:  # type: ignore[arg-type]
        raise ValueError("price_optimization_config_invalid")
    return number


__all__ = [
    "CONF_PRICE_OPTIMIZATION",
    "PriceDirection",
    "PriceDischargeMode",
    "PriceInterval",
    "PriceOptimizationConfig",
    "PricePlan",
    "PricePlanAction",
    "PriceSeries",
    "build_price_plan",
    "normalize_price_series",
]
