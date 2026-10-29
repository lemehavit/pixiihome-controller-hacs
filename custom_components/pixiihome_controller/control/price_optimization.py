"""Pure electricity-price normalization and bounded battery planning."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
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
class ExternalWindow:
    """A protected external interval the plan must route around.

    ``target_usable_soc_percent`` is the usable SOC the external Target SOC
    service is expected to leave behind. ``None`` means the energy after the
    window is unknown, so planning ends at its start.
    """

    start: datetime
    end: datetime
    target_usable_soc_percent: float | None = None
    # An adjustable window's target will be lowered to the planned level, so
    # it holds the battery instead of refilling it: energy passes through. When
    # the plan uses its time, the window is deleted instead.
    adjustable: bool = False

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None or self.end <= self.start:
            raise ValueError("external_window_invalid")
        target = self.target_usable_soc_percent
        if target is not None and (not isfinite(target) or not 0 <= target <= 100):
            object.__setattr__(self, "target_usable_soc_percent", None)
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "end", self.end.astimezone(UTC))

    @property
    def known(self) -> bool:
        return self.target_usable_soc_percent is not None


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
    # Planned usable SOC at the start of each adjustable external window.
    window_socs: tuple[tuple[datetime, float], ...] = ()
    charge_demand_limited: bool = False

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
    external_windows: Sequence[ExternalWindow] = (),
    net_load_kw: Callable[[datetime, datetime], float | None] | None = None,
) -> PricePlan:
    """Plan using minimum net profit per AC kWh delivered by the battery.

    ``net_load_kw`` optionally forecasts household load minus solar for an
    interval. Zero-export self-consumption can only deliver that much, so the
    simulated energy and expected margin of such a discharge are capped by it
    (the schedule itself still allows the full power in case load is higher).

    The legacy ``*_difference`` parameter names remain for persisted-entity
    compatibility. Their values are net-profit floors, after all costs.

    External windows are never planned over. A window with a known Target SOC
    resets the simulated energy to that target afterwards, so the plan
    continues past it; the energy it buys is valued at the window's price.
    A window with unknown resulting energy ends the plan at its start.
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
    resets = [window for window in windows if not window.adjustable]
    held = [window for window in windows if window.adjustable]
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
    # A segment is the stretch between two known external windows. Energy is
    # only moved within one segment: the next Target SOC window overwrites the
    # state of charge, and any energy discharged before it is bought back at
    # that window's price.
    # An adjustable window is lowered to the planned level or, when the plan
    # uses its time, deleted; either way its time is the plan's.
    blocked = [
        any(window.start < item.end and item.start < window.end for window in resets)
        for item in candidates
    ]
    segment = [sum(1 for window in resets if window.end <= item.start) for item in candidates]
    refill_price = [_mean_price(series, window.start, window.end) for window in resets]
    window_socs: dict[datetime, float] = {}
    active_segment: int | None = None
    closing: float | None = None
    stored_price = acquisition_price
    charged_energy = charged_price = 0.0
    charge_demand_limited = False

    for index, item in enumerate(candidates):
        if segment[index] != active_segment:
            active_segment = segment[index]
            if active_segment > 0:
                reset = resets[active_segment - 1]
                assert reset.target_usable_soc_percent is not None
                energy = usable_capacity_kwh * reset.target_usable_soc_percent / 100
                bought_at = refill_price[active_segment - 1]
                if bought_at is not None:
                    acquisition_price = bought_at
            # Energy already stored when a segment starts and discharged before
            # its closing Target SOC window is bought back at that window's
            # price. Energy charged inside the segment keeps its own price and
            # is sold first, so a short charge/discharge cycle stays possible.
            closing = refill_price[active_segment] if active_segment < len(resets) else None
            if closing is not None:
                stored_price = max(acquisition_price, closing)
                charged_energy = charged_price = 0.0
        for window in held:
            if window.start not in window_socs and window.start <= item.start:
                window_socs[window.start] = energy / usable_capacity_kwh * 100
        if blocked[index]:
            continue
        price = item.price_sek_per_kwh
        hours = item.duration_hours
        reachable = [
            candidates[j].price_sek_per_kwh
            for j in range(index, len(candidates))
            if segment[j] == active_segment and not blocked[j]
        ]
        future_high = max(reachable[1:], default=price)
        future_low = min(reachable, default=price)
        future_peak = max(reachable, default=price)
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
            and len(reachable) > 1
            and price <= future_low + 1e-9
            and charge_mode is not None
        ):
            power = min(maximum_charge_kw, headroom / (hours * charge_efficiency))
            if net_load_kw is not None:
                future_delivery = 0.0
                forecast_known = True
                for future_index in range(index + 1, len(candidates)):
                    future = candidates[future_index]
                    if segment[future_index] != active_segment:
                        break
                    if blocked[future_index]:
                        continue
                    later_peak = max(
                        (candidates[j].price_sek_per_kwh
                         for j in range(future_index + 1, len(candidates))
                         if segment[j] == active_segment and not blocked[j]),
                        default=future.price_sek_per_kwh,
                    )
                    if future.price_sek_per_kwh < later_peak - 1e-9:
                        continue
                    future_mode, _ = _qualifying_discharge(
                        price, future.price_sek_per_kwh,
                        efficiency=round_trip_efficiency_percent / 100,
                        grid_cost=grid_import_cost_sek_per_kwh,
                        cycle_cost=cycle_cost_sek_per_kwh,
                        minimum_profit=minimum_difference_sek_per_kwh,
                        export_allowed=export_allowed,
                        export_minimum_profit=export_minimum_difference_sek_per_kwh,
                    )
                    if future_mode is PriceDischargeMode.EXPORT:
                        future_delivery += maximum_discharge_kw * future.duration_hours
                    elif future_mode is PriceDischargeMode.SELF_CONSUMPTION:
                        demand = net_load_kw(future.start, future.end)
                        if demand is None or not isfinite(demand):
                            forecast_known = False
                            break
                        future_delivery += (
                            min(maximum_discharge_kw, max(0.0, demand)) * future.duration_hours
                        )
                if forecast_known:
                    needed_stored_energy = future_delivery / discharge_efficiency
                    remaining_headroom = max(0.0, needed_stored_energy - available)
                    demand_power = remaining_headroom / (hours * charge_efficiency)
                    if demand_power < power - 1e-9:
                        charge_demand_limited = True
                    power = min(power, demand_power)
            if power < 0.05:
                continue
            added_energy = power * hours * charge_efficiency
            if closing is not None:
                charged_price = (charged_energy * charged_price + added_energy * price) / (
                    charged_energy + added_energy
                )
                charged_energy += added_energy
            else:
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
            basis = acquisition_price
            if closing is not None:
                if charged_energy > 1e-6:
                    basis, available = charged_price, min(available, charged_energy)
                else:
                    basis = stored_price
            mode, discharge_net = _qualifying_discharge(
                basis, price,
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
                if mode is PriceDischargeMode.SELF_CONSUMPTION and net_load_kw is not None:
                    demand = net_load_kw(item.start, item.end)
                    if demand is not None:
                        if min(power, demand) < 0.05:
                            continue
                        delivered = min(power, demand) * hours
                energy -= delivered / discharge_efficiency
                if closing is not None:
                    charged_energy = max(0.0, charged_energy - delivered / discharge_efficiency)
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
                    basis,
                    price - basis,
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
            "load_forecast": net_load_kw is not None,
        },
        "external_windows": [
            [window.start.isoformat(), window.end.isoformat(),
             window.target_usable_soc_percent, window.adjustable]
            for window in windows
        ],
    }
    fingerprint = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for window in held:
        window_socs.setdefault(window.start, energy / usable_capacity_kwh * 100)
    return PricePlan(
        tuple(actions), (), fingerprint, min_soc, max_soc, expected_margin,
        tuple(sorted(window_socs.items())), charge_demand_limited,
    )


def merge_adjacent_actions(
    actions: Sequence[PricePlanAction],
    max_duration_seconds: int | None = None,
) -> tuple[PricePlanAction, ...]:
    """Join back-to-back actions with identical native values into one schedule.

    Fewer, longer schedules mean fewer Gateway writes and readbacks. Prices are
    averaged by duration; margins add up; the projected SOC is the later one.
    """
    merged: list[PricePlanAction] = []
    for action in actions:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and previous.end == action.start
            and previous.direction is action.direction
            and previous.service == action.service
            and previous.discharge_mode is action.discharge_mode
            and _freeze_values(previous.values) == _freeze_values(action.values)
            and (
                max_duration_seconds is None
                or previous.duration_seconds + action.duration_seconds <= max_duration_seconds
            )
        ):
            first, second = previous.duration_seconds, action.duration_seconds
            total = first + second

            def weighted(a: float, b: float, first: int = first, second: int = second,
                         total: int = total) -> float:
                return (a * first + b * second) / total

            energy_first = previous.power_kw * first / 3600
            energy_second = action.power_kw * second / 3600
            margin = previous.expected_margin_sek + action.expected_margin_sek
            merged[-1] = PricePlanAction(
                previous.start,
                action.end,
                action.direction,
                action.power_kw,
                action.service,
                action.values,
                action.discharge_mode,
                weighted(previous.price_sek_per_kwh, action.price_sek_per_kwh),
                weighted(
                    previous.reference_price_sek_per_kwh, action.reference_price_sek_per_kwh
                ),
                weighted(previous.spread_sek_per_kwh, action.spread_sek_per_kwh),
                action.projected_usable_soc_percent,
                margin,
                (
                    previous.net_profit_sek_per_kwh * energy_first
                    + action.net_profit_sek_per_kwh * energy_second
                )
                / (energy_first + energy_second)
                if energy_first + energy_second > 0
                else action.net_profit_sek_per_kwh,
            )
            continue
        merged.append(action)
    return tuple(merged)


def _freeze_values(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze_values(item)) for key, item in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_freeze_values(item) for item in value)
    return value


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


def _mean_price(series: PriceSeries, start: datetime, end: datetime) -> float | None:
    """Time-weighted price over one window; ``None`` when no price covers it."""
    weighted = covered = 0.0
    for item in series.intervals:
        overlap = (min(item.end, end) - max(item.start, start)).total_seconds()
        if overlap > 0:
            weighted += item.price_sek_per_kwh * overlap
            covered += overlap
    return weighted / covered if covered > 0 else None


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
    rows: list[tuple[datetime, datetime, float]] = []
    for item in raw_items:
        start = _aware_datetime(item.get("start") or item.get("start_time"))
        raw_end = item.get("end") or item.get("end_time")
        end = _aware_datetime(raw_end) if raw_end is not None else start + timedelta(hours=1)
        raw_price = item.get("price", item.get("value"))
        rows.append((start.astimezone(UTC), end.astimezone(UTC), _price(raw_price) * factor))
    return _contiguous(rows)


def _contiguous(rows: list[tuple[datetime, datetime, float]]) -> tuple[PriceInterval, ...]:
    """Mend what a daylight-saving day can break in a price list.

    On the 25-hour day, price sources have produced intervals whose end is not
    after their start, the repeated hour twice at the same instant, or a gap
    where it should be. That stopped planning for the whole day. An interval
    without a valid end ends where the next begins (or after the usual
    length), a second interval at the same instant is dropped, overlaps are
    cut and a gap of at most an hour is covered by the interval before it.
    """
    rows = sorted(rows, key=lambda row: row[0])
    lengths = sorted(end - start for start, end, _ in rows if end > start)
    usual = lengths[len(lengths) // 2] if lengths else timedelta(hours=1)
    starts = [start for start, _, _ in rows]
    mended: list[list[Any]] = []
    for index, (start, end, price) in enumerate(rows):
        if mended and start == mended[-1][0]:
            continue
        following = next((later for later in starts[index + 1:] if later > start), None)
        if end <= start:
            end = following if following is not None and following - start <= usual \
                else start + usual
        elif following is not None and end > following:
            end = following
        if mended and timedelta(0) < start - mended[-1][1] <= timedelta(hours=1):
            mended[-1][1] = start
        mended.append([start, end, price])
    return tuple(PriceInterval(start, end, price) for start, end, price in mended)


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
    "ExternalWindow",
    "PriceDirection",
    "PriceDischargeMode",
    "PriceInterval",
    "PriceOptimizationConfig",
    "PricePlan",
    "PricePlanAction",
    "PriceSeries",
    "build_price_plan",
    "merge_adjacent_actions",
    "normalize_price_series",
]
