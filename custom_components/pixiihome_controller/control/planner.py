"""Deterministic forecasting and explainable battery schedule planning."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import ceil, floor, isfinite
from statistics import fmean
from typing import Any

from .adapters import AdapterRequest
from .journal import JournalTargetIdentity
from .models import ControlOperation


@dataclass(frozen=True, slots=True)
class HistoricalEnergyPoint:
    timestamp: datetime
    load_kw: float
    solar_kw: float
    price_per_kwh: float

    def __post_init__(self) -> None:
        _aware(self.timestamp, "historical timestamp")
        _finite_nonnegative(self.load_kw, "historical load")
        _finite_nonnegative(self.solar_kw, "historical solar")
        _finite(self.price_per_kwh, "historical price")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    start: datetime
    duration_seconds: int
    load_kw: float
    solar_kw: float
    price_per_kwh: float
    confidence: float

    def __post_init__(self) -> None:
        _aware(self.start, "forecast start")
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, int)
            or self.duration_seconds <= 0
        ):
            raise ValueError("forecast duration must be a positive integer")
        _finite_nonnegative(self.load_kw, "forecast load")
        _finite_nonnegative(self.solar_kw, "forecast solar")
        _finite(self.price_per_kwh, "forecast price")
        if not isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("forecast confidence must be between zero and one")

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration_seconds)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start": self.start.isoformat(),
            "duration_seconds": self.duration_seconds,
            "load_kw": self.load_kw,
            "solar_kw": self.solar_kw,
            "price_per_kwh": self.price_per_kwh,
            "confidence": self.confidence,
        }


@dataclass(frozen=True, slots=True)
class ExternalInterval:
    start: datetime
    end: datetime
    identity: str

    def __post_init__(self) -> None:
        _aware(self.start, "external interval start")
        _aware(self.end, "external interval end")
        object.__setattr__(self, "start", self.start.astimezone(UTC))
        object.__setattr__(self, "end", self.end.astimezone(UTC))
        if self.end <= self.start:
            raise ValueError("external interval must have positive duration")
        if not isinstance(self.identity, str) or not self.identity.strip():
            raise ValueError("external interval requires an identity")
        object.__setattr__(self, "identity", self.identity.strip())


@dataclass(frozen=True, slots=True)
class BatteryPlanConstraints:
    asset_id: str
    capacity_kwh: float
    initial_soc_percent: float
    min_soc_percent: float
    max_soc_percent: float
    max_charge_kw: float
    max_discharge_kw: float
    round_trip_efficiency: float = 0.9
    minimum_confidence: float = 0.5
    maximum_actions: int = 96

    def __post_init__(self) -> None:
        if not isinstance(self.asset_id, str) or not self.asset_id.strip():
            raise ValueError("planner constraints require an asset ID")
        object.__setattr__(self, "asset_id", self.asset_id.strip())
        for name in ("capacity_kwh", "max_charge_kw", "max_discharge_kw"):
            value = getattr(self, name)
            if not isfinite(value) or value <= 0:
                raise ValueError(f"planner {name} must be finite and positive")
        if not 0 <= self.min_soc_percent < self.max_soc_percent <= 100:
            raise ValueError("planner SoC bounds are invalid")
        if not self.min_soc_percent <= self.initial_soc_percent <= self.max_soc_percent:
            raise ValueError("planner initial SoC is outside bounds")
        if not isfinite(self.round_trip_efficiency) or not 0 < self.round_trip_efficiency <= 1:
            raise ValueError("planner efficiency must be in (0, 1]")
        if not isfinite(self.minimum_confidence) or not 0 <= self.minimum_confidence <= 1:
            raise ValueError("planner minimum confidence must be between zero and one")
        if (
            isinstance(self.maximum_actions, bool)
            or not isinstance(self.maximum_actions, int)
            or self.maximum_actions <= 0
        ):
            raise ValueError("planner maximum actions must be a positive integer")


class PlanDirection(StrEnum):
    CHARGE = "charge"
    DISCHARGE = "discharge"


@dataclass(frozen=True, slots=True)
class PlanAction:
    m_id: int
    source_id: int
    start: datetime
    duration_seconds: int
    direction: PlanDirection
    power_kw: float
    target_soc_percent: float
    rationale: str

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration_seconds)

    def as_schedule(self) -> dict[str, Any]:
        return {
            "m_id": self.m_id,
            "source_id": self.source_id,
            "start": self.start.isoformat(),
            "duration_seconds": self.duration_seconds,
            "socref": round(self.target_soc_percent, 6),
            "planned_power_kw": round(self.power_kw, 6),
            "rationale": self.rationale,
        }


class PlanStatus(StrEnum):
    READY = "ready"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class PlanResult:
    status: PlanStatus
    fingerprint: str
    actions: tuple[PlanAction, ...] = ()
    blockers: tuple[str, ...] = ()
    baseline_cost: float = 0.0
    planned_cost: float = 0.0
    minimum_soc_percent: float | None = None
    maximum_soc_percent: float | None = None

    @property
    def expected_savings(self) -> float:
        return self.baseline_cost - self.planned_cost

    def as_adapter_request(self, *, asset_id: str) -> AdapterRequest:
        if self.status is not PlanStatus.READY:
            raise ValueError("blocked plans cannot be applied")
        return AdapterRequest(
            ControlOperation.PLANNER_APPLY,
            target_identity=JournalTargetIdentity(asset_id=asset_id),
            payload={"schedules": [action.as_schedule() for action in self.actions]},
        )


class ForecastSource(StrEnum):
    EXPLICIT = "explicit"
    WEEKDAY_EWMA = "weekday_ewma"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ForecastResult:
    source: ForecastSource
    points: tuple[ForecastPoint, ...] = ()
    reason: str | None = None

    @property
    def available(self) -> bool:
        return self.source is not ForecastSource.UNAVAILABLE and bool(self.points)


@dataclass(slots=True)
class DeterministicForecaster:
    """Explicit forecast, then 15-minute weekday EWMA, otherwise unavailable."""

    interval_seconds: int = 900
    minimum_weekday_samples: int = 2
    confidence_samples: int = 7
    ewma_alpha: float = 0.35

    def forecast(
        self,
        history: Sequence[HistoricalEnergyPoint],
        *,
        start: datetime,
        intervals: int,
        explicit_forecast: Sequence[ForecastPoint] | None = None,
    ) -> ForecastResult:
        _aware(start, "forecast horizon start")
        if isinstance(intervals, bool) or not isinstance(intervals, int) or intervals <= 0:
            raise ValueError("forecast interval count must be positive")
        if (
            self.interval_seconds != 900
            or self.minimum_weekday_samples <= 0
            or self.confidence_samples <= 0
            or not 0 < self.ewma_alpha <= 1
        ):
            raise ValueError("forecaster requires valid 15-minute EWMA configuration")

        current = start.astimezone(UTC)
        if explicit_forecast is not None:
            explicit = tuple(explicit_forecast)
            if len(explicit) < intervals:
                return ForecastResult(
                    ForecastSource.UNAVAILABLE,
                    reason="explicit_forecast_incomplete",
                )
            selected = explicit[:intervals]
            if not self._valid_horizon(selected, current):
                return ForecastResult(
                    ForecastSource.UNAVAILABLE,
                    reason="explicit_forecast_invalid",
                )
            return ForecastResult(ForecastSource.EXPLICIT, selected)

        by_slot: dict[tuple[int, int, int], list[HistoricalEnergyPoint]] = defaultdict(list)
        for point in sorted(history, key=lambda item: item.timestamp):
            by_slot[
                (
                    point.timestamp.weekday(),
                    point.timestamp.hour,
                    point.timestamp.minute,
                )
            ].append(point)

        result: list[ForecastPoint] = []
        for _ in range(intervals):
            matches = by_slot.get((current.weekday(), current.hour, current.minute), [])
            if len(matches) < self.minimum_weekday_samples:
                return ForecastResult(
                    ForecastSource.UNAVAILABLE,
                    reason="weekday_history_insufficient",
                )
            result.append(
                ForecastPoint(
                    start=current,
                    duration_seconds=self.interval_seconds,
                    load_kw=_ewma(
                        [point.load_kw for point in matches],
                        self.ewma_alpha,
                    ),
                    solar_kw=_ewma(
                        [point.solar_kw for point in matches],
                        self.ewma_alpha,
                    ),
                    price_per_kwh=_ewma(
                        [point.price_per_kwh for point in matches],
                        self.ewma_alpha,
                    ),
                    confidence=min(1.0, len(matches) / self.confidence_samples),
                )
            )
            current += timedelta(seconds=self.interval_seconds)
        return ForecastResult(ForecastSource.WEEKDAY_EWMA, tuple(result))

    def _valid_horizon(
        self,
        points: tuple[ForecastPoint, ...],
        start: datetime,
    ) -> bool:
        if not points or points[0].start != start:
            return False
        if any(point.duration_seconds != self.interval_seconds for point in points):
            return False
        return all(
            current.start == previous.end
            for previous, current in zip(points, points[1:], strict=False)
        )


@dataclass(slots=True)
class DeterministicPlanner:
    """Explainable quantile planner that never performs adapter I/O."""

    def plan(
        self,
        forecast: Sequence[ForecastPoint],
        constraints: BatteryPlanConstraints,
        *,
        external_intervals: Iterable[ExternalInterval] = (),
        first_m_id: int = 1_000_000_000_000,
    ) -> PlanResult:
        points = tuple(forecast)
        external = tuple(external_intervals)
        fingerprint = _fingerprint(points, constraints, external, first_m_id)
        blockers = self._validate_inputs(points, constraints, external)
        if blockers:
            return PlanResult(PlanStatus.BLOCKED, fingerprint, blockers=blockers)

        prices = sorted(point.price_per_kwh for point in points)
        low_price = prices[floor((len(prices) - 1) * 0.25)]
        high_price = prices[ceil((len(prices) - 1) * 0.75)]
        efficiency = constraints.round_trip_efficiency**0.5
        energy = constraints.capacity_kwh * constraints.initial_soc_percent / 100
        minimum_energy = constraints.capacity_kwh * constraints.min_soc_percent / 100
        maximum_energy = constraints.capacity_kwh * constraints.max_soc_percent / 100
        baseline_cost = 0.0
        planned_cost = 0.0
        soc_values = [constraints.initial_soc_percent]
        actions: list[PlanAction] = []
        source_seed = int(fingerprint[:8], 16) & 0x7FFFFFFF

        for index, point in enumerate(points):
            hours = point.duration_seconds / 3600
            net_load = max(point.load_kw - point.solar_kw, 0.0)
            solar_surplus = max(point.solar_kw - point.load_kw, 0.0)
            baseline_import = net_load
            baseline_cost += baseline_import * hours * point.price_per_kwh
            power_kw = 0.0
            rationale = ""

            headroom_kwh = maximum_energy - energy
            available_kwh = energy - minimum_energy
            if solar_surplus > 0 and headroom_kwh > 1e-9:
                power_kw = min(
                    solar_surplus,
                    constraints.max_charge_kw,
                    headroom_kwh / (hours * efficiency),
                )
                rationale = "capture_solar_surplus"
            elif (
                point.price_per_kwh <= low_price
                and any(
                    future.price_per_kwh > point.price_per_kwh
                    for future in points[index + 1 :]
                )
                and headroom_kwh > 1e-9
            ):
                power_kw = min(
                    constraints.max_charge_kw,
                    headroom_kwh / (hours * efficiency),
                )
                rationale = "charge_low_price"
            elif (
                point.price_per_kwh >= high_price
                and net_load > 0
                and available_kwh > 1e-9
            ):
                discharge_kw = min(
                    constraints.max_discharge_kw,
                    net_load,
                    available_kwh * efficiency / hours,
                )
                power_kw = -discharge_kw
                rationale = "discharge_high_price"

            if power_kw >= 0:
                energy += power_kw * hours * efficiency
                grid_charge = max(power_kw - solar_surplus, 0.0)
                grid_import = baseline_import + grid_charge
            else:
                energy += power_kw * hours / efficiency
                grid_import = max(0.0, baseline_import + power_kw)
            energy = min(maximum_energy, max(minimum_energy, energy))
            planned_cost += grid_import * hours * point.price_per_kwh
            soc = energy / constraints.capacity_kwh * 100
            soc_values.append(soc)

            if abs(power_kw) <= 1e-9:
                continue
            action = PlanAction(
                m_id=first_m_id + index,
                source_id=source_seed + index,
                start=point.start,
                duration_seconds=point.duration_seconds,
                direction=PlanDirection.CHARGE if power_kw > 0 else PlanDirection.DISCHARGE,
                power_kw=power_kw,
                target_soc_percent=soc,
                rationale=rationale,
            )
            if any(_overlaps(action.start, action.end, item.start, item.end) for item in external):
                return PlanResult(
                    PlanStatus.BLOCKED,
                    fingerprint,
                    blockers=("external_schedule_conflict",),
                    baseline_cost=baseline_cost,
                    planned_cost=planned_cost,
                    minimum_soc_percent=min(soc_values),
                    maximum_soc_percent=max(soc_values),
                )
            if len(actions) >= constraints.maximum_actions:
                return PlanResult(
                    PlanStatus.BLOCKED,
                    fingerprint,
                    blockers=("maximum_actions_exceeded",),
                    baseline_cost=baseline_cost,
                    planned_cost=planned_cost,
                    minimum_soc_percent=min(soc_values),
                    maximum_soc_percent=max(soc_values),
                )
            actions.append(action)

        return PlanResult(
            PlanStatus.READY,
            fingerprint,
            actions=tuple(actions),
            baseline_cost=baseline_cost,
            planned_cost=planned_cost,
            minimum_soc_percent=min(soc_values),
            maximum_soc_percent=max(soc_values),
        )

    @staticmethod
    def _validate_inputs(
        points: tuple[ForecastPoint, ...],
        constraints: BatteryPlanConstraints,
        external: tuple[ExternalInterval, ...],
    ) -> tuple[str, ...]:
        del external
        blockers: list[str] = []
        if not points:
            blockers.append("forecast_empty")
            return tuple(blockers)
        if any(point.duration_seconds != 900 for point in points):
            blockers.append("forecast_interval_not_15_minutes")
        if any(point.start.timestamp() % 900 != 0 for point in points):
            blockers.append("forecast_interval_not_utc_aligned")
        for previous, current in zip(points, points[1:], strict=False):
            if current.start != previous.end:
                blockers.append("forecast_not_contiguous")
                break
        if any(point.confidence < constraints.minimum_confidence for point in points):
            blockers.append("forecast_confidence_insufficient")
        return tuple(blockers)


def aggregate_forecast(
    forecast: Sequence[ForecastPoint],
    *,
    bucket_seconds: int,
) -> tuple[ForecastPoint, ...]:
    """Aggregate contiguous equal-duration points without hiding confidence minima."""
    points = tuple(forecast)
    if not points:
        return ()
    if bucket_seconds <= 0:
        raise ValueError("forecast bucket must be positive")
    base_duration = points[0].duration_seconds
    if bucket_seconds % base_duration:
        raise ValueError("forecast bucket must be a multiple of point duration")
    group_size = bucket_seconds // base_duration
    if any(point.duration_seconds != base_duration for point in points):
        raise ValueError("forecast aggregation requires equal point durations")
    if len(points) % group_size:
        raise ValueError("forecast length must fill complete aggregation buckets")
    for previous, current in zip(points, points[1:], strict=False):
        if current.start != previous.end:
            raise ValueError("forecast aggregation requires contiguous points")


    result: list[ForecastPoint] = []
    for index in range(0, len(points), group_size):
        group = points[index : index + group_size]
        result.append(
            ForecastPoint(
                start=group[0].start,
                duration_seconds=bucket_seconds,
                load_kw=fmean(point.load_kw for point in group),
                solar_kw=fmean(point.solar_kw for point in group),
                price_per_kwh=fmean(point.price_per_kwh for point in group),
                confidence=min(point.confidence for point in group),
            )
        )
    return tuple(result)


def _fingerprint(
    forecast: Sequence[ForecastPoint],
    constraints: BatteryPlanConstraints,
    external: Sequence[ExternalInterval],
    first_m_id: int,
) -> str:
    payload = {
        "forecast": [point.as_dict() for point in forecast],
        "constraints": {
            name: getattr(constraints, name)
            for name in constraints.__dataclass_fields__
        },
        "external": [
            {
                "start": item.start.isoformat(),
                "end": item.end.isoformat(),
                "identity": item.identity,
            }
            for item in external
        ],
        "first_m_id": first_m_id,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _ewma(values: Sequence[float], alpha: float) -> float:
    if not values:
        raise ValueError("EWMA requires at least one value")
    result = float(values[0])
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _overlaps(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    return start < other_end and other_start < end


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


def _finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
        raise ValueError(f"{name} must be finite")


def _finite_nonnegative(value: float, name: str) -> None:
    _finite(value, name)
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


__all__ = [
    "BatteryPlanConstraints",
    "DeterministicForecaster",
    "DeterministicPlanner",
    "ExternalInterval",
    "ForecastPoint",
    "ForecastResult",
    "ForecastSource",
    "HistoricalEnergyPoint",
    "PlanAction",
    "PlanDirection",
    "PlanResult",
    "PlanStatus",
    "aggregate_forecast",
]
