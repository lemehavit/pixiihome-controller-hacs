"""Smart solar charging: wait only when the target is still safely reachable."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.pixiihome_controller.control.price_optimization import PriceInterval
from custom_components.pixiihome_controller.control.smart_solar import evaluate_deferral

from .test_solar_charging import FakeRuntime, _controller

# 09:00 Stockholm (CEST) on a summer day.
NOW = datetime(2026, 6, 1, 7, 0, tzinfo=UTC)


def _prices(morning: float, midday: float) -> tuple[PriceInterval, ...]:
    start = NOW.replace(hour=0)
    return tuple(
        PriceInterval(
            start + timedelta(hours=hour),
            start + timedelta(hours=hour + 1),
            morning if hour < 9 else midday,
        )
        for hour in range(24)
    )


def _surplus(kw: float, until_hour: int = 16):
    def surplus(start: datetime, end: datetime) -> float | None:
        return kw if start.hour < until_hour else -0.5
    return surplus


def _evaluate(**overrides: Any):
    arguments: dict[str, Any] = dict(
        now=NOW,
        usable_soc_percent=40.0,
        target_soc_percent=90.0,
        capacity_kwh=10.0,
        charge_kw=5.0,
        round_trip_efficiency_percent=90.0,
        safety_margin_percent=25.0,
        minimum_advantage_sek_per_kwh=0.2,
        prices=_prices(1.5, 0.2),
        surplus_kw=_surplus(4.0),
    )
    arguments.update(overrides)
    return evaluate_deferral(**arguments)


def test_defers_when_later_surplus_covers_the_target_with_margin() -> None:
    decision = _evaluate()
    assert decision.defer
    assert decision.reason == "export_now_charge_later"
    assert decision.need_kwh == pytest.approx(0.5 * 10 / 0.9 ** 0.5)
    assert decision.available_later_kwh is not None
    assert decision.available_later_kwh >= decision.need_kwh * 1.25
    assert decision.charge_price == pytest.approx(0.2)
    assert decision.reachable_soc_percent == 100.0


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"surplus_kw": _surplus(0.6)}, "surplus_too_small_later"),
        ({"prices": _prices(0.3, 0.2)}, "no_price_advantage"),
        ({"prices": _prices(-0.1, -0.2)}, "export_price_not_positive"),
        ({"capacity_kwh": 0.0}, "capacity_unknown"),
        ({"usable_soc_percent": 90.0}, "target_reached"),
        ({"surplus_kw": lambda start, end: None}, "forecast_unavailable"),
        ({"prices": ()}, "prices_unavailable"),
    ],
)
def test_anything_uncertain_charges_now(overrides: dict[str, Any], reason: str) -> None:
    decision = _evaluate(**overrides)
    assert not decision.defer
    assert decision.reason == reason


def test_charge_power_limits_what_the_later_surplus_can_store() -> None:
    # 9 kW of surplus for two hours, but the battery takes at most 1 kW.
    decision = _evaluate(surplus_kw=_surplus(9.0, until_hour=9), charge_kw=1.0)
    assert not decision.defer
    assert decision.reason == "surplus_too_small_later"


class Forecast:
    def __init__(self, solar_kw: float | None) -> None:
        self.solar_kw = solar_kw
        self.config = SimpleNamespace(solar_power_entity_id=None)
        self.correction = SimpleNamespace(ratio=lambda now, window=None: None)

    def solar_forecast_kw(self, start, end, *, conservative=False, corrected=True):
        if self.solar_kw is None:
            return None
        local_hour = (start.hour + 2) % 24
        return self.solar_kw if 9 <= local_hour < 17 else 0.0

    def load_kw(self, start, end, *, high=False):
        return 0.5

    def price_intervals(self):
        local_midnight = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        now = datetime.now(UTC)
        return tuple(
            PriceInterval(
                local_midnight + timedelta(hours=hour),
                local_midnight + timedelta(hours=hour + 1),
                3.0 if local_midnight + timedelta(hours=hour) <= now < local_midnight
                + timedelta(hours=hour + 1) else 0.1,
            )
            for hour in range(-24, 48)
        )


@pytest.mark.asyncio
async def test_controller_skips_the_lease_while_deferring_and_charges_when_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.pixiihome_controller import solar_controller
    from custom_components.pixiihome_controller.control.smart_solar import SmartSolarDecision

    def defer(**_: Any) -> SmartSolarDecision:
        return SmartSolarDecision(
            True, "export_now_charge_later", 5.0, 20.0, 100.0, None, 3.0, 0.1
        )

    monkeypatch.setattr(solar_controller, "evaluate_deferral", defer)
    runtime = FakeRuntime()
    runtime.operational_settings = (
        runtime.operational_settings.set("solar_smart_enabled", True)
        .set("price_usable_capacity_kwh", 10.0)
        .set("solar_start_debounce_seconds", 0)
    )
    runtime.energy_forecast = Forecast(solar_kw=8.0)  # type: ignore[attr-defined]
    now = [1_000.0]
    controller = _controller(runtime, now)
    await controller.tick()
    assert controller.status == "deferring"
    assert controller.smart_decision is not None and controller.smart_decision.defer
    assert runtime.writes == []
    # Turning the switch off always returns to normal solar charging.
    runtime.operational_settings = runtime.operational_settings.set("solar_smart_enabled", False)
    now[0] += 61
    await controller.tick()
    assert runtime.writes


@pytest.mark.asyncio
async def test_missing_solar_forecast_never_defers() -> None:
    runtime = FakeRuntime()
    runtime.operational_settings = (
        runtime.operational_settings.set("solar_smart_enabled", True)
        .set("price_usable_capacity_kwh", 10.0)
        .set("solar_start_debounce_seconds", 0)
    )
    runtime.energy_forecast = Forecast(solar_kw=None)  # type: ignore[attr-defined]
    now = [1_000.0]
    controller = _controller(runtime, now)
    await controller.tick()
    assert controller.status == "active"
    assert controller.smart_decision is not None
    assert controller.smart_decision.reason == "forecast_unavailable"
