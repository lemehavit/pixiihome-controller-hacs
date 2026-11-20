"""Smart EV charging: price windows, charger current and the controller's actions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.pixiihome_controller.control.ev_charging import (
    EvChargingConfig,
    car_limit_for_target,
    charger_current,
    energy_needed_kwh,
    next_deadline,
    plan_charging,
)
from custom_components.pixiihome_controller.control.load_balancing import PhaseSample
from custom_components.pixiihome_controller.control.settings import OperationalSettings
from custom_components.pixiihome_controller.devices import PixiiFeature, setting_feature
from custom_components.pixiihome_controller.ev_charging_controller import (
    EvChargingController,
)

NOW = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Price:
    start: datetime
    end: datetime
    price_sek_per_kwh: float


def hourly(start: datetime, prices: list[float]) -> list[Price]:
    return [
        Price(start + timedelta(hours=i), start + timedelta(hours=i + 1), p)
        for i, p in enumerate(prices)
    ]


def plan(**overrides: Any):
    values: dict[str, Any] = {
        "now": NOW,
        "soc": 60.0,
        "target_soc": 80.0,
        "min_soc": 20.0,
        "deadline": NOW + timedelta(hours=13),
        "capacity_kwh": 77.0,
        "efficiency_percent": 90.0,
        "power_kw": 11.0,
        "prices": hourly(NOW, [3.0, 2.5, 2.0, 1.5, 1.0, 0.5, 0.4, 0.6, 1.2, 1.8, 2.2, 2.6, 3.1]),
    }
    values.update(overrides)
    return plan_charging(**values)


# ----- pure planning ---------------------------------------------------------------------


def test_energy_need_includes_losses() -> None:
    assert energy_needed_kwh(60, 80, 77, 90) == pytest.approx(17.111, rel=1e-3)
    assert energy_needed_kwh(85, 80, 77, 90) == 0


def test_cheapest_hours_before_deadline_are_chosen() -> None:
    result = plan()
    # 17.1 kWh / 11 kW * 1.15 = 1.79 h -> the two cheapest hours (0.4 and 0.5).
    assert not result.charge and result.reason == "waiting_for_cheap_price"
    starts = [slot.start for slot in result.slots]
    assert starts == [NOW + timedelta(hours=5), NOW + timedelta(hours=6)]
    inside = plan(now=NOW + timedelta(hours=5, minutes=10))
    assert inside.charge and inside.reason == "cheap_price"
    attrs = inside.attributes()
    assert attrs["planned_slots"][0]["end"] == (NOW + timedelta(hours=7)).isoformat()


def test_waits_for_tomorrows_prices_when_the_unpriced_tail_can_hold_the_charge() -> None:
    result = plan(prices=hourly(NOW, [3.0, 2.0]), deadline=NOW + timedelta(hours=13))
    assert not result.charge and result.reason == "waiting_for_prices"
    short = plan(prices=hourly(NOW, [3.0, 2.0, 1.0, 0.5]), deadline=NOW + timedelta(hours=5))
    # Only 1 h is unpriced but 1.79 h are needed: commit the cheapest known hour.
    assert [s.price for s in short.slots] == [0.5]


def test_minimum_level_deadline_and_target_override_price() -> None:
    assert plan(soc=10.0).reason == "below_minimum" and plan(soc=10.0).charge
    late = plan(deadline=NOW + timedelta(hours=1))
    assert late.charge and late.reason == "deadline"
    done = plan(soc=80.0)
    assert not done.charge and done.reason == "target_reached"
    assert plan(soc=None).reason == "car_soc_unknown"


def test_planned_battery_discharge_hours_are_avoided() -> None:
    discharge = (NOW + timedelta(hours=6), NOW + timedelta(hours=7))
    result = plan(battery_discharges=[discharge])
    assert [s.start.hour for s in result.slots] == [23, 1]  # 0.5 and 0.6 instead of 0.4
    allowed = plan(battery_discharges=[discharge], allow_battery=True)
    assert [s.start.hour for s in allowed.slots] == [23, 0]
    during = plan(
        battery_discharges=[discharge], now=NOW + timedelta(hours=6, minutes=5),
        committed_until=NOW + timedelta(hours=6, minutes=30),
    )
    assert not during.charge and during.reason == "battery_discharging"
    forced = plan(battery_discharges=[(NOW, NOW + timedelta(hours=1))], soc=10.0)
    assert forced.charge


def test_started_interval_is_finished() -> None:
    later = plan(
        soc=79.5, now=NOW + timedelta(minutes=20), committed_until=NOW + timedelta(hours=1)
    )
    assert later.charge and later.reason == "finishing_interval"


def test_deadline_is_next_local_hour() -> None:
    assert next_deadline(NOW, 7) == datetime(2026, 11, 21, 7, tzinfo=UTC)
    assert next_deadline(NOW.replace(hour=5), 7) == datetime(2026, 11, 20, 7, tzinfo=UTC)


def test_car_limit_rounds_up_into_the_car_range() -> None:
    assert car_limit_for_target(40) == 50
    assert car_limit_for_target(75) == 80
    assert car_limit_for_target(80) == 80
    assert car_limit_for_target(100) == 100


# ----- charger current -------------------------------------------------------------------


def test_charger_current_keeps_the_tightest_phase_under_the_fuse() -> None:
    decision = charger_current(
        phase_currents_a=[19.0, 12.0, 11.0], ev_current_a=10.0, fuse_a=20, margin_a=2,
        min_current_a=6, max_current_a=16,
    )
    assert decision.limit_a == 9 and not decision.pause
    roomy = charger_current(
        phase_currents_a=[4.0, 4.0, 4.0], ev_current_a=0.0, fuse_a=20, margin_a=2,
        min_current_a=6, max_current_a=16,
    )
    assert roomy.limit_a == 14
    full = charger_current(
        phase_currents_a=[1.0, 1.0, 1.0], ev_current_a=0.0, fuse_a=20, margin_a=2,
        min_current_a=6, max_current_a=16,
    )
    assert full.limit_a == 16


def test_charger_pauses_when_below_minimum_and_battery_charge_yields() -> None:
    tight = charger_current(
        phase_currents_a=[21.0, 21.0, 21.0], ev_current_a=6.0, fuse_a=20, margin_a=2,
        min_current_a=6, max_current_a=16,
    )
    assert tight.pause and tight.limit_a == 6
    yielded = charger_current(
        phase_currents_a=[21.0, 21.0, 21.0], ev_current_a=6.0, fuse_a=20, margin_a=2,
        min_current_a=6, max_current_a=16, battery_credit_a=[10.0, 10.0, 10.0],
    )
    assert not yielded.pause and yielded.limit_a == 13


def test_config_validates_domains_and_fuse() -> None:
    with pytest.raises(ValueError):
        EvChargingConfig(car_limit_entity="sensor.limit")
    with pytest.raises(ValueError):
        EvChargingConfig(fuse_a=10, margin_a=10)
    config = EvChargingConfig.from_mapping(
        {"charger_status_entity": "sensor.easee_status", "phases": "1", "min_current_a": 7.0}
    )
    assert config.configured and config.phases == 1 and config.min_current_a == 7


def test_ev_settings_default_off_and_group_under_ev_charging() -> None:
    settings = OperationalSettings.defaults()
    assert settings.get("ev_smart_charging_enabled") is False
    assert settings.get("ev_load_balancing_enabled") is False
    assert settings.get("ev_ready_by_hour") == 7
    assert setting_feature("ev_target_soc_percent") is PixiiFeature.EV_CHARGING


# ----- controller ------------------------------------------------------------------------


class FakeStates:
    def __init__(self) -> None:
        self.values: dict[str, SimpleNamespace] = {}

    def set(self, entity_id: str, state: object, **attributes: object) -> None:
        self.values[entity_id] = SimpleNamespace(state=str(state), attributes=attributes)

    def get(self, entity_id: str) -> SimpleNamespace | None:
        return self.values.get(entity_id)


class FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def async_call(self, domain, service, data, blocking=False, context=None):
        assert context is not None
        self.calls.append((domain, service, dict(data)))


class FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def async_fire(self, event, data, context=None):
        self.events.append((event, data))


def make_controller(
    settings: dict[str, Any], options: dict[str, Any] | None = None, prices=None
) -> tuple[EvChargingController, SimpleNamespace, list[float]]:
    states, services, bus = FakeStates(), FakeServices(), FakeBus()
    hass = SimpleNamespace(states=states, services=services, bus=bus)
    operational = OperationalSettings.defaults()
    for key, value in settings.items():
        operational = operational.set(key, value)
    config = {
        "charger_status_entity": "sensor.easee_status",
        "charger_power_entity": "sensor.easee_power",
        "charger_limit_entity": "sensor.easee_limit",
        "car_soc_entity": "sensor.car_soc",
        "car_limit_entity": "number.car_limit",
        "current_l1": "sensor.l1",
        "current_l2": "sensor.l2",
        "current_l3": "sensor.l3",
        **(options or {}),
    }
    forecast = SimpleNamespace(price_intervals=lambda: tuple(prices or ()))
    runtime = SimpleNamespace(
        hass=hass,
        serial="test",
        entry=SimpleNamespace(options={"ev_charging": config}, entry_id="e"),
        operational_settings=operational,
        energy_forecast=forecast,
        price_optimization_controller=None,
        load_balancing_observer=None,
        meter_limit_load_balancer=None,
        controller_state=SimpleNamespace(load_balancing_enabled=False),
    )
    tick = [0.0]
    controller = EvChargingController(
        runtime,  # type: ignore[arg-type]
        clock_fn=lambda: NOW + timedelta(seconds=tick[0]),
        monotonic=lambda: tick[0],
    )
    controller._charger_device_id = lambda config: "easee-device"  # type: ignore[method-assign]
    states.set("number.car_limit", 80, min=50, max=100, step=10)
    states.set("sensor.easee_limit", 16)
    for phase in ("l1", "l2", "l3"):
        states.set(f"sensor.{phase}", 3.0)
    return controller, hass, tick


@pytest.mark.asyncio
async def test_smart_charging_starts_in_a_cheap_hour_and_pauses_in_an_expensive_one() -> None:
    cheap_now = hourly(NOW, [0.2, 3.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0])
    controller, hass, tick = make_controller({"ev_smart_charging_enabled": True}, prices=cheap_now)
    hass.states.set("sensor.easee_status", "awaiting_start")
    hass.states.set("sensor.car_soc", 70)
    await controller.tick()
    assert hass.services.calls == [
        ("easee", "action_command", {"device_id": "easee-device", "action_command": "resume"})
    ]
    messages = [data.get("message") for event, data in hass.bus.events]
    assert "Car charging started" in messages
    causes = [data["message"] for event, data in hass.bus.events if event.endswith("_cause")]
    assert any("cheapest" in cause for cause in causes)
    # Rate limited: no second command within a minute.
    await controller.tick()
    assert len(hass.services.calls) == 1

    hass.services.calls.clear()
    tick[0] = 3700  # this hour costs 3.0, later ones 1.0
    hass.states.set("sensor.easee_status", "charging")
    hass.states.set("sensor.car_soc", 72)
    await controller.tick()
    assert ("easee", "action_command",
            {"device_id": "easee-device", "action_command": "pause"}) in hass.services.calls
    assert controller.paused_by_us


@pytest.mark.asyncio
async def test_car_limit_follows_target_and_target_below_car_minimum_pauses() -> None:
    controller, hass, _ = make_controller(
        {"ev_smart_charging_enabled": True, "ev_target_soc_percent": 40.0}
    )
    hass.states.set("sensor.easee_status", "charging")
    hass.states.set("sensor.car_soc", 41)
    await controller.tick()
    assert ("number", "set_value", {"entity_id": "number.car_limit", "value": 50.0}) in (
        hass.services.calls
    )
    assert ("easee", "action_command",
            {"device_id": "easee-device", "action_command": "pause"}) in hass.services.calls
    assert controller.status == "target_reached"


@pytest.mark.asyncio
async def test_load_balancing_lowers_current_and_restores_on_unplug() -> None:
    controller, hass, tick = make_controller({"ev_load_balancing_enabled": True})
    hass.states.set("sensor.easee_status", "charging")
    hass.states.set("sensor.easee_power", 6.9, unit_of_measurement="kW")  # 10 A on 3 phases
    hass.states.set("sensor.l1", 20.0)
    await controller.tick()
    call = hass.services.calls[-1]
    assert call[:2] == ("easee", "set_circuit_dynamic_limit")
    assert call[2]["current_p1"] == 8  # 10 A + (18 - 20) A
    assert controller.limit_owned

    tick[0] = 20
    hass.services.calls.clear()
    hass.states.set("sensor.easee_status", "disconnected")
    hass.states.set("sensor.easee_limit", 8)
    await controller.tick()
    assert hass.services.calls[-1][2]["current_p1"] == 6


@pytest.mark.asyncio
async def test_load_balancing_pauses_for_load_without_phase_data_holds_minimum() -> None:
    controller, hass, tick = make_controller({"ev_load_balancing_enabled": True})
    hass.states.set("sensor.easee_status", "charging")
    hass.states.set("sensor.easee_power", 4.14, unit_of_measurement="kW")  # 6 A
    for phase in ("l1", "l2", "l3"):
        hass.states.set(f"sensor.{phase}", 22.0)
    await controller.tick()
    assert controller.lb_paused and controller.status == "paused_for_load"
    assert ("easee", "action_command",
            {"device_id": "easee-device", "action_command": "pause"}) in hass.services.calls

    other, hass2, _ = make_controller({"ev_load_balancing_enabled": True})
    hass2.states.set("sensor.easee_status", "charging")
    hass2.states.set("sensor.l1", "unavailable")
    await other.tick()
    assert hass2.services.calls[-1][2]["current_p1"] == 6


@pytest.mark.asyncio
async def test_switching_smart_charging_off_undoes_its_pause() -> None:
    controller, hass, _ = make_controller({})
    controller.paused_by_us = True
    hass.states.set("sensor.easee_status", "awaiting_start")
    hass.states.set("sensor.car_soc", 50)
    await controller.tick()
    assert hass.services.calls == [
        ("easee", "action_command", {"device_id": "easee-device", "action_command": "resume"})
    ]


@pytest.mark.asyncio
async def test_nothing_happens_when_both_switches_are_off() -> None:
    controller, hass, _ = make_controller({})
    hass.states.set("sensor.easee_status", "charging")
    hass.states.set("sensor.car_soc", 50)
    await controller.tick()
    assert hass.services.calls == [] and controller.status == "idle"


@pytest.mark.asyncio
async def test_battery_charge_current_yields_to_the_car_only_with_pixii_balancing() -> None:
    controller, hass, _ = make_controller(
        {"ev_load_balancing_enabled": True},
        options={"current_l1": "", "current_l2": "", "current_l3": ""},
    )
    observer = SimpleNamespace(
        samples=tuple(PhaseSample(21.0, 230.0, 4830.0, 2300.0) for _ in range(3)),
        snapshot={"observed_at": NOW.isoformat()},
    )
    controller.runtime.load_balancing_observer = observer
    controller.runtime.meter_limit_load_balancer = SimpleNamespace(status="active")
    controller.runtime.controller_state = SimpleNamespace(load_balancing_enabled=True)
    hass.states.set("sensor.easee_status", "charging")
    hass.states.set("sensor.easee_power", 4.14, unit_of_measurement="kW")  # 6 A
    await controller.tick()
    # 6 A + (18 - 21) A + 10 A battery = 13 A; the start limit was 16.
    assert hass.services.calls[-1][2]["current_p1"] == 13


@pytest.mark.asyncio
async def test_chosen_start_pause_and_current_entities_replace_easee_actions() -> None:
    controller, hass, tick = make_controller(
        {"ev_smart_charging_enabled": True, "ev_load_balancing_enabled": True},
        options={
            "charger_start_entity": "switch.charger",
            "charger_pause_entity": "switch.charger",
            "charger_current_entity": "number.charger_current",
            "car_plugged_entity": "binary_sensor.plug",
            "price_entity": "sensor.price",
        },
    )
    controller._charger_device_id = lambda config: None  # type: ignore[method-assign]
    hass.states.set(
        "sensor.price", 1.0, unit_of_measurement="SEK/kWh",
        raw_today=[
            {"start": (NOW + timedelta(hours=h)).isoformat(),
             "end": (NOW + timedelta(hours=h + 1)).isoformat(),
             "value": 0.1 if h == 0 else 3.0}
            for h in range(24)
        ],
    )
    hass.states.set("binary_sensor.plug", "on")
    hass.states.set("sensor.easee_status", "idle")
    hass.states.set("sensor.easee_power", 0.0, unit_of_measurement="kW")
    hass.states.set("sensor.car_soc", 70)
    await controller.tick()
    assert ("switch", "turn_on", {"entity_id": "switch.charger"}) in hass.services.calls
    assert any(
        call[:2] == ("number", "set_value") and call[2]["entity_id"] == "number.charger_current"
        for call in hass.services.calls
    )
    assert not any(call[0] == "easee" for call in hass.services.calls)
    hass.states.set("binary_sensor.plug", "off")
    hass.services.calls.clear()
    tick[0] = 30
    await controller.tick()
    assert controller.status == "car_not_connected"


def test_plan_uses_the_power_available_per_interval() -> None:
    # Fuse room halves the power in the two cheapest hours, so a third hour is needed.
    cheap = {NOW + timedelta(hours=5), NOW + timedelta(hours=6)}

    def power_at(start, end):
        hour = start.replace(minute=0, second=0, microsecond=0)
        return 5.5 if hour in cheap else 11.0

    result = plan(power_at=power_at)
    assert [s.start.hour for s in result.slots] == [23, 0, 1]
    assert result.attributes()["planned_slots"][0]["power_kw"] == pytest.approx(7.33, abs=0.01)
    # With almost no power left before the deadline, it charges now.
    starved = plan(power_at=lambda start, end: 1.0)
    assert starved.charge and starved.reason == "deadline"


@pytest.mark.asyncio
async def test_controller_power_follows_fuse_house_forecast_and_car() -> None:
    controller, hass, tick = make_controller({"ev_load_balancing_enabled": True})
    controller.runtime.energy_forecast.load_kw = lambda start, end: 3.45  # 5 A/phase * 1.5
    later = NOW + timedelta(hours=3)
    # 18 A room - 7.5 A house = 10 A on three phases
    assert controller.power_at(later, later + timedelta(hours=1)) == pytest.approx(6.9)
    # A car drawing 3.7 kW (one phase) for two minutes caps every interval.
    hass.states.set("sensor.easee_status", "charging")
    hass.states.set("sensor.easee_power", 3.7, unit_of_measurement="kW")
    await controller.tick()
    tick[0] = 130
    await controller.tick()
    assert controller.car_max_kw == pytest.approx(3.7)
    assert controller.power_at(later, later + timedelta(hours=1)) == pytest.approx(3.7)
    hass.states.set("sensor.easee_status", "disconnected")
    await controller.tick()
    assert controller.car_max_kw is None


EASEE = [
    ("sensor.eh8pt4lg_status", None),
    ("sensor.eh8pt4lg_power", "power"),
    ("sensor.eh8pt4lg_dynamic_circuit_limit", "current"),
    ("switch.eh8pt4lg_smart_charging", None),
]
SKODA = [
    ("sensor.skoda_enyaq_target_battery_percentage", None),
    ("sensor.skoda_enyaq_battery_percentage", "battery"),
    ("number.skoda_enyaq_charge_limit", "battery"),
    ("switch.skoda_enyaq_charging", "switch"),
    ("switch.skoda_enyaq_battery_care", "switch"),
    ("binary_sensor.skoda_enyaq_charger_connected", "plug"),
]


def test_entities_are_found_on_the_chosen_devices_and_explicit_ones_win() -> None:
    from custom_components.pixiihome_controller.control.ev_charging import (
        entities_from_devices,
    )

    config = EvChargingConfig(
        charger_device="easee", car_device="skoda", car_soc_entity="sensor.my_soc"
    )
    assert config.configured
    found = entities_from_devices(config, EASEE, SKODA)
    assert found.charger_status_entity == "sensor.eh8pt4lg_status"
    assert found.charger_power_entity == "sensor.eh8pt4lg_power"
    assert found.charger_limit_entity == "sensor.eh8pt4lg_dynamic_circuit_limit"
    assert found.car_soc_entity == "sensor.my_soc"
    assert found.car_limit_entity == "number.skoda_enyaq_charge_limit"
    assert found.car_charging_switch == "switch.skoda_enyaq_charging"
    assert found.car_plugged_entity == "binary_sensor.skoda_enyaq_charger_connected"
    soc = entities_from_devices(EvChargingConfig(car_device="skoda"), [], SKODA)
    assert soc.car_soc_entity == "sensor.skoda_enyaq_battery_percentage"


@pytest.mark.asyncio
async def test_devices_alone_drive_the_easee_actions() -> None:
    controller, hass, _ = make_controller(
        {"ev_smart_charging_enabled": True},
        options={
            "charger_status_entity": "", "charger_power_entity": "",
            "charger_limit_entity": "", "car_soc_entity": "", "car_limit_entity": "",
            "charger_device": "easee-dev", "car_device": "skoda-dev",
        },
        prices=hourly(NOW, [0.1] + [3.0] * 13),
    )
    del controller._charger_device_id  # use the real lookup: the chosen device
    controller._device_entries = lambda device: {  # type: ignore[method-assign]
        "easee-dev": EASEE, "skoda-dev": SKODA,
    }.get(device, [])
    hass.states.set("sensor.eh8pt4lg_status", "awaiting_start")
    hass.states.set("binary_sensor.skoda_enyaq_charger_connected", "on")
    hass.states.set("sensor.skoda_enyaq_battery_percentage", 60)
    hass.states.set("number.skoda_enyaq_charge_limit", 80, min=50, max=100, step=10)
    await controller.tick()
    assert ("easee", "action_command",
            {"device_id": "easee-dev", "action_command": "resume"}) in hass.services.calls


# ----- solar charging --------------------------------------------------------------------


def test_solar_current_follows_the_surplus() -> None:
    from custom_components.pixiihome_controller.control.ev_charging import solar_current

    # 3 kW exported while the car draws nothing: 4.3 A on three phases, below 6 A.
    assert solar_current(export_w=3000, ev_power_w=0, phases=3, voltage_v=230,
                         min_current_a=6, max_current_a=16)[0] is None
    # One phase: 13 A.
    assert solar_current(export_w=3000, ev_power_w=0, phases=1, voltage_v=230,
                         min_current_a=6, max_current_a=16)[0] == 13
    # The car already draws 4.14 kW (6 A) and 1.4 kW is still exported: 8 A.
    assert solar_current(export_w=1400, ev_power_w=4140, phases=3, voltage_v=230,
                         min_current_a=6, max_current_a=16)[0] == 8
    # Importing 1 kW while charging at 6 A: 4.5 A, unless 1 kW grid import is allowed.
    assert solar_current(export_w=-1000, ev_power_w=4140, phases=3, voltage_v=230,
                         min_current_a=6, max_current_a=16)[0] is None
    assert solar_current(export_w=-1000, ev_power_w=4140, phases=3, voltage_v=230,
                         min_current_a=6, max_current_a=16, grid_allowance_w=1000)[0] == 6


def test_forecast_sun_makes_an_interval_cheaper_than_the_same_grid_price() -> None:
    prices = hourly(NOW, [1.0] * 13)
    sunny = NOW + timedelta(hours=8)

    def solar(start: datetime, end: datetime) -> float:
        return 11.0 if start >= sunny and end <= sunny + timedelta(hours=2) else 0.0

    result = plan(soc=70.0, prices=prices, solar_kw_at=solar, grid_extra_cost=0.65,
                  sell_price=2.0)
    assert [s.start for s in result.slots] == [sunny]
    assert result.slots[0].solar_kw == 11.0
    assert result.slots[0].price == pytest.approx(1.0)
    # At or above the sell-instead price the surplus is sold: no discount.
    sold = plan(soc=70.0, prices=prices, solar_kw_at=solar, grid_extra_cost=0.65,
                sell_price=1.0)
    assert all(s.solar_kw == 0 for s in sold.slots)


def test_grid_export_needs_import_too() -> None:
    with pytest.raises(ValueError, match="grid_import_and_export_required"):
        EvChargingConfig(grid_export_entity="sensor.export")
    assert EvChargingConfig(
        grid_export_entity="sensor.export", grid_import_entity="sensor.import"
    ).grid_export_entity


def test_solar_settings_default_off() -> None:
    settings = OperationalSettings.defaults()
    assert settings.get("ev_solar_charging_enabled") is False
    assert settings.get("ev_solar_car_before_battery") is False
    assert setting_feature("ev_solar_charging_enabled") is PixiiFeature.EV_CHARGING


SOLAR_OPTIONS = {
    "grid_export_entity": "sensor.export",
    "grid_import_entity": "sensor.import",
}


@pytest.mark.asyncio
async def test_solar_charging_starts_after_the_delay_follows_and_pauses_without_sun() -> None:
    controller, hass, tick = make_controller(
        {"ev_solar_charging_enabled": True}, SOLAR_OPTIONS, prices=hourly(NOW, [0.5] * 24)
    )
    hass.states.set("sensor.easee_status", "awaiting_start")
    hass.states.set("sensor.car_soc", 60)
    hass.states.set("sensor.export", 5000, unit_of_measurement="W")
    hass.states.set("sensor.import", 0, unit_of_measurement="W")
    await controller.tick()
    # The car limit follows the solar target (100 %); nothing starts before the delay.
    assert hass.services.calls == [
        ("number", "set_value", {"entity_id": "number.car_limit", "value": 100.0})
    ]
    assert controller.solar_reason == "sun_confirming"

    hass.services.calls.clear()
    tick[0] = 181
    await controller.tick()
    assert hass.services.calls[0][:2] == ("easee", "set_circuit_dynamic_limit")
    assert hass.services.calls[0][2]["current_p1"] == 7  # 5 kW / 690 V
    assert hass.services.calls[1] == (
        "easee", "action_command", {"device_id": "easee-device", "action_command": "resume"}
    )
    causes = [data["message"] for event, data in hass.bus.events if event.endswith("_cause")]
    assert any("solar surplus" in cause for cause in causes)

    # Charging at 7 A (4.83 kW) with 1.4 kW still exported: up to 9 A after a minute.
    hass.services.calls.clear()
    tick[0] = 260
    hass.states.set("sensor.easee_status", "charging")
    hass.states.set("sensor.easee_limit", 7)
    hass.states.set("sensor.easee_power", 4.83, unit_of_measurement="kW")
    hass.states.set("sensor.export", 1400, unit_of_measurement="W")
    await controller.tick()
    assert hass.services.calls[-1][2]["current_p1"] == 9
    assert controller.status == "solar_charging"

    # A cloud: importing 3 kW. The minimum current is held through the stop delay.
    hass.services.calls.clear()
    hass.states.set("sensor.easee_limit", 9)
    hass.states.set("sensor.easee_power", 6.2, unit_of_measurement="kW")
    hass.states.set("sensor.export", 0, unit_of_measurement="W")
    hass.states.set("sensor.import", 3000, unit_of_measurement="W")
    tick[0] = 300
    await controller.tick()
    assert hass.services.calls[-1][2]["current_p1"] == 6
    assert controller.solar_reason == "solar_dipping"
    hass.services.calls.clear()
    tick[0] = 601
    hass.states.set("sensor.easee_limit", 6)
    await controller.tick()
    assert ("easee", "action_command",
            {"device_id": "easee-device", "action_command": "pause"}) in hass.services.calls
    assert not controller.solar_active


@pytest.mark.asyncio
async def test_solar_surplus_is_sold_at_or_above_the_sell_price() -> None:
    controller, hass, tick = make_controller(
        {"ev_solar_charging_enabled": True, "ev_solar_start_delay_minutes": 0},
        SOLAR_OPTIONS, prices=hourly(NOW, [1.5] * 24),
    )
    hass.states.set("sensor.easee_status", "charging")
    hass.states.set("sensor.car_soc", 60)
    hass.states.set("sensor.export", 8000, unit_of_measurement="W")
    hass.states.set("sensor.import", 0, unit_of_measurement="W")
    await controller.tick()
    assert controller.solar_reason == "selling_is_better"
    assert ("easee", "action_command",
            {"device_id": "easee-device", "action_command": "pause"}) in hass.services.calls


@pytest.mark.asyncio
async def test_solar_reads_pixii_meter_when_no_grid_entity_is_chosen() -> None:
    controller, hass, tick = make_controller(
        {"ev_solar_charging_enabled": True, "ev_solar_start_delay_minutes": 0},
        prices=hourly(NOW, [0.5] * 24),
    )

    class Telemetry:
        def value(self, family: str, key: str) -> Any:
            return -6000 if (family, key) == ("meter", "meter_w") else None

        def is_fresh(self, family: str, **kwargs: Any) -> bool:
            return family == "meter"

    controller.runtime.telemetry = Telemetry()
    hass.states.set("sensor.easee_status", "awaiting_start")
    hass.states.set("sensor.car_soc", 60)
    await controller.tick()
    assert controller.solar_active
    assert controller.solar_limit_a == 8  # 6 kW / 690 V
    assert controller.attributes()["grid_power_source"] == "Pixii meter"
