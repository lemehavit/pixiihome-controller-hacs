"""Smart EV charging and charger load balancing through Home Assistant integrations.

The charger (Easee) is started/paused with ``easee.action_command`` and its
current is set with ``easee.set_circuit_dynamic_limit``. The car (for example
MySkoda) gets its own charge limit through its ``number`` entity. Nothing here
talks to the Pixii Gateway.

Every action is written to the logbook with who did it and why, sharing the
context with the service call so the charger's own logbook lines say the same.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .activity import clock, log_activity
from .const import DOMAIN
from .control.energy_forecast import average_kw, parse_solar_forecast
from .control.ev_charging import (
    CONF_EV_CHARGING,
    EvChargingConfig,
    EvPlan,
    car_limit_for_target,
    charger_current,
    entities_from_devices,
    next_deadline,
    plan_charging,
    solar_current,
)
from .control.price_optimization import normalize_price_series

if TYPE_CHECKING:
    from .runtime import RuntimeData

EV_STATUS = "ev_charging_status"
TICK_SECONDS = 10
COMMAND_INTERVAL_SECONDS = 60
COMMAND_RETRIES = 3
COMMAND_BACKOFF_SECONDS = 900
LIMIT_DECREASE_INTERVAL_SECONDS = 15
LIMIT_INCREASE_INTERVAL_SECONDS = 60
LIMIT_INCREASE_STEP_A = 4
LB_RESUME_MARGIN_A = 2
LB_RESUME_HOLD_SECONDS = 120
CAR_LIMIT_RETRY_SECONDS = 900
CAR_SWITCH_RETRY_SECONDS = 900
OBSERVER_MAX_AGE_SECONDS = 30
RECENT_ACTIONS = 15
DEVICE_RESOLVE_SECONDS = 60
# Phase currents are rarely even; the forecast house load is spread as if the
# busiest phase carries this much more than an even share.
HOUSE_PHASE_IMBALANCE = 1.5
# The car limits the power itself when it draws clearly less than the charger allows.
CAR_LIMITED_SHARE = 0.85
CAR_LIMIT_LEARN_SECONDS = 120

DISCONNECTED = {"disconnected", "unplugged", "not_connected", "unknown", "unavailable", ""}
CHARGING = {"charging", "on"}
COMPLETED = {"completed", "finished", "full"}
# Charger power above this means the car is drawing current, whatever the status says.
CHARGING_POWER_KW = 0.5
# Canonical charger phases used by the decisions below.
PAUSED = {"paused"}
# Pixii's grid meter older than this is not used for solar surplus.
METER_MAX_AGE_SECONDS = 30


class EvChargingController:
    """Decide every ten seconds; act only when something has to change."""

    def __init__(
        self,
        runtime: RuntimeData,
        *,
        clock_fn: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.runtime = runtime
        self.clock = clock_fn or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.status = "disabled"
        self.reason: str | None = None
        self.plan: EvPlan | None = None
        self.charger_state: str | None = None
        self.charger_phase = "disconnected"
        self.car_soc: float | None = None
        self.car_soc_at: datetime | None = None
        self.applied_limit_a: int | None = None
        self.headroom_a: float | None = None
        self.balance_detail: str | None = None
        self.paused_by_us = False
        self.limit_owned = False
        self.lb_paused = False
        self.available_a: int | None = None
        self.car_max_kw: float | None = None
        self._car_limited_since: float | None = None
        self.committed_until: datetime | None = None
        self.recent: list[dict[str, str]] = []
        self._lb_ok_since: float | None = None
        self._last_command: float | None = None
        self._command_attempts = 0
        self._command_wanted: str | None = None
        self._backoff_until: float | None = None
        self._last_limit_write: float | None = None
        self._car_limit_attempt: tuple[float, float] | None = None
        self._car_switch_attempt: float | None = None
        self._last_logged: dict[str, str] = {}
        self._store: Store[dict[str, Any]] | None = None
        self._price_cache: tuple[Any, tuple[Any, ...]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._listeners: list[Callable[[], None]] = []
        self._resolved: tuple[EvChargingConfig, float, EvChargingConfig] | None = None
        self.solar_active = False
        self.solar_reason: str | None = None
        self.solar_detail = ""
        self.solar_export_w: float | None = None
        self.solar_available_w: float | None = None
        self.solar_limit_a: int | None = None
        self._solar_ok_since: float | None = None
        self._solar_low_since: float | None = None
        self._solar_driving = False
        self._solar_cache: tuple[Any, tuple[Any, ...]] | None = None

    # ----- configuration and lifecycle -------------------------------------------------

    @property
    def config(self) -> EvChargingConfig:
        """Configured entities, with empty ones filled from the chosen devices."""
        entry = self.runtime.entry
        try:
            config = EvChargingConfig.from_mapping(
                None if entry is None else entry.options.get(CONF_EV_CHARGING)
            )
        except (TypeError, ValueError):
            return EvChargingConfig()
        if not (config.charger_device or config.car_device):
            return config
        now = self.monotonic()
        if (
            self._resolved is not None
            and self._resolved[0] == config
            and now - self._resolved[1] < DEVICE_RESOLVE_SECONDS
        ):
            return self._resolved[2]
        resolved = entities_from_devices(
            config,
            self._device_entries(config.charger_device),
            self._device_entries(config.car_device),
        )
        self._resolved = (config, now, resolved)
        return resolved

    def _device_entries(self, device_id: str) -> list[tuple[str, str | None]]:
        if not device_id:
            return []
        from homeassistant.helpers import entity_registry as er

        registry = er.async_get(self.runtime.hass)
        return [
            (entry.entity_id, entry.device_class or entry.original_device_class)
            for entry in er.async_entries_for_device(registry, device_id)
            if entry.disabled_by is None
        ]

    def _setting(self, key: str) -> Any:
        try:
            return self.runtime.operational_settings.get(key)
        except (AttributeError, KeyError):
            return None

    @property
    def smart_enabled(self) -> bool:
        return self._setting("ev_smart_charging_enabled") is True

    @property
    def balancing_enabled(self) -> bool:
        return self._setting("ev_load_balancing_enabled") is True

    @property
    def solar_enabled(self) -> bool:
        return self._setting("ev_solar_charging_enabled") is True

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
            except Exception:  # noqa: BLE001 - presentation must not stop control
                continue

    def request_tick(self) -> None:
        self._wake.set()

    async def start(self) -> None:
        entry = self.runtime.entry
        if entry is not None and self._store is None:
            store: Store[dict[str, Any]] = Store(
                self.runtime.hass, 1, f"{DOMAIN}.{entry.entry_id}.ev_charging"
            )
            self._store = store
            try:
                saved = await store.async_load()
            except Exception:  # noqa: BLE001 - a broken file only forgets ownership
                saved = None
            if isinstance(saved, Mapping):
                self.paused_by_us = saved.get("paused_by_us") is True
                self.limit_owned = saved.get("limit_owned") is True
        if self._task is None:
            self._task = self.runtime.hass.async_create_background_task(
                self._run(), "pixiihome_controller ev charging"
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _save(self) -> None:
        if self._store is None:
            return
        try:
            await self._store.async_save(
                {"paused_by_us": self.paused_by_us, "limit_owned": self.limit_owned}
            )
        except Exception:  # noqa: BLE001 - ownership is also visible on the sensor
            return

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - one bad pass must not stop control
                self.status, self.reason = "error", str(error) or type(error).__name__
                self._log_once("error", "EV charging control failed",
                               f"Who: EV charging. Why: {self.reason}")
            self._notify()
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=TICK_SECONDS)
            except TimeoutError:
                pass

    # ----- reading Home Assistant -------------------------------------------------------

    def _state(self, entity_id: str) -> Any:
        return self.runtime.hass.states.get(entity_id) if entity_id else None

    def _number(self, entity_id: str) -> float | None:
        state = self._state(entity_id)
        if state is None:
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        return value if value == value and abs(value) != float("inf") else None

    def _charger_device_id(self, config: EvChargingConfig) -> str | None:
        if config.charger_device:
            return config.charger_device
        if not config.charger_status_entity:
            return None
        from homeassistant.helpers import entity_registry as er

        entry = er.async_get(self.runtime.hass).async_get(config.charger_status_entity)
        return None if entry is None else entry.device_id

    def _phase(self, config: EvChargingConfig) -> str:
        """Charger state as disconnected, charging, completed or paused."""
        raw = (self.charger_state or "").lower()
        if config.car_plugged_entity:
            plug = self._state(config.car_plugged_entity)
            if plug is None or plug.state != "on":
                return "disconnected"
        elif raw in DISCONNECTED:
            return "disconnected"
        power = self._number(config.charger_power_entity)
        unit_state = self._state(config.charger_power_entity)
        unit = None if unit_state is None else unit_state.attributes.get("unit_of_measurement")
        power_kw = None if power is None else (power / 1000 if unit == "W" else power)
        if raw in CHARGING or (power_kw is not None and power_kw > CHARGING_POWER_KW):
            return "charging"
        if raw in COMPLETED:
            return "completed"
        return "paused"

    def _ev_current_a(self, config: EvChargingConfig) -> float:
        """Current the car draws per phase; 0 when unknown (the cautious choice)."""
        power = self._number(config.charger_power_entity)
        if power is None or power <= 0:
            return 0.0
        state = self._state(config.charger_power_entity)
        unit = None if state is None else state.attributes.get("unit_of_measurement")
        watts = power if unit == "W" else power * 1000
        return watts / (config.voltage_v * config.phases)

    def _phase_currents(self, config: EvChargingConfig) -> tuple[list[float], list[float]]:
        """Grid currents per phase and the battery's yieldable charge current per phase."""
        observer = getattr(self.runtime, "load_balancing_observer", None)
        samples = tuple(getattr(observer, "samples", ()) or ())
        fresh = False
        if observer is not None and samples:
            observed = observer.snapshot.get("observed_at")
            try:
                age = (self.clock() - datetime.fromisoformat(observed)).total_seconds()
                fresh = age <= OBSERVER_MAX_AGE_SECONDS
            except (TypeError, ValueError):
                fresh = False
        meter = getattr(self.runtime, "meter_limit_load_balancer", None)
        battery_yields = (
            fresh
            and meter is not None
            and getattr(meter, "status", None) == "active"
            and self.runtime.controller_state.load_balancing_enabled
        )
        credit: list[float] = (
            [max(0.0, s.battery_power_w) / s.voltage_v for s in samples if s.voltage_v > 0]
            if battery_yields else []
        )
        if config.current_entities:
            currents = [self._number(entity) for entity in config.current_entities]
            if any(value is None for value in currents):
                raise ValueError("ev_phase_current_unavailable")
            return [abs(float(v)) for v in currents if v is not None], credit
        if not fresh:
            raise ValueError("ev_phase_current_unavailable")
        return [abs(s.current_a) for s in samples], credit

    def _power_w(self, entity_id: str) -> float | None:
        value = self._number(entity_id)
        if value is None:
            return None
        state = self._state(entity_id)
        unit = None if state is None else state.attributes.get("unit_of_measurement")
        return value * 1000 if unit == "kW" else value / 1000 if unit == "mW" else value

    def _export_w(self, config: EvChargingConfig) -> tuple[float, str]:
        """Net power leaving the house now (negative = import) and where it was read."""
        if config.grid_power_entity:
            value = self._power_w(config.grid_power_entity)
            if value is None:
                raise ValueError("ev_grid_power_unavailable")
            return -value, config.grid_power_entity
        if config.grid_export_entity:
            export = self._power_w(config.grid_export_entity)
            imported = self._power_w(config.grid_import_entity)
            if export is None or imported is None:
                raise ValueError("ev_grid_power_unavailable")
            return abs(export) - abs(imported), (
                f"{config.grid_export_entity} - {config.grid_import_entity}"
            )
        telemetry = getattr(self.runtime, "telemetry", None)
        meter = None if telemetry is None else telemetry.value("meter", "meter_w")
        if (
            telemetry is None
            or not telemetry.is_fresh("meter", max_age_seconds=METER_MAX_AGE_SECONDS,
                                      now=self.clock())
            or isinstance(meter, bool)
            or not isinstance(meter, (int, float))
            or meter != meter
        ):
            raise ValueError("ev_grid_power_unavailable")
        return -float(meter), "Pixii meter"

    def _battery_solar_w(self) -> float:
        """Solar power the home battery charges with, when the car may take it first."""
        if self._setting("ev_solar_car_before_battery") is not True:
            return 0.0
        solar = getattr(self.runtime, "solar_charging_controller", None)
        battery = getattr(self.runtime, "battery", None)
        power = None if battery is None else getattr(battery, "power_w", None)
        if solar is None or not solar.owns_live_lease() or not isinstance(power, (int, float)):
            return 0.0
        return max(0.0, float(power))

    def _price_now(self, config: EvChargingConfig) -> float | None:
        now = self.clock()
        for interval in self._prices(config):
            if interval.start <= now < interval.end:
                price = interval.price_sek_per_kwh
                return float(price) if isinstance(price, (int, float)) else None
        return None

    def solar_surplus_kw(self, start: datetime, end: datetime) -> float | None:
        """Forecast solar minus forecast house load in a period."""
        config = self.config
        forecast = getattr(self.runtime, "energy_forecast", None)
        solar: float | None
        if config.solar_forecast_entities:
            states = [self._state(e) for e in config.solar_forecast_entities]
            fresh = [
                s for s in states if s is not None and s.state not in {"unknown", "unavailable"}
            ]
            key = tuple((s.entity_id, s.last_updated) for s in fresh)
            if self._solar_cache is None or self._solar_cache[0] != key:
                self._solar_cache = (key, parse_solar_forecast(s.attributes for s in fresh))
            solar = average_kw(self._solar_cache[1], start, end)
        elif forecast is not None:
            try:
                solar = forecast.solar_forecast_kw(start, end)
            except Exception:  # noqa: BLE001 - no forecast only means no solar discount
                solar = None
        else:
            solar = None
        if solar is None:
            return None
        try:
            load = 0.0 if forecast is None else float(forecast.load_kw(start, end))
        except Exception:  # noqa: BLE001 - without a load forecast all solar counts
            load = 0.0
        return max(0.0, solar - load)

    def _prices(self, config: EvChargingConfig) -> tuple[Any, ...]:
        """The EV's own price entity, else the one price optimization uses."""
        if not config.price_entity:
            forecast = getattr(self.runtime, "energy_forecast", None)
            return () if forecast is None else tuple(forecast.price_intervals())
        state = self._state(config.price_entity)
        if state is None or state.state in {"unknown", "unavailable"}:
            return ()
        key = (config.price_entity, getattr(state, "last_updated", None))
        if self._price_cache is not None and self._price_cache[0] == key:
            return self._price_cache[1]
        try:
            series = normalize_price_series(
                state.attributes, state.attributes.get("unit_of_measurement")
            )
        except ValueError:
            return ()
        intervals = tuple(series.intervals)
        self._price_cache = (key, intervals)
        return intervals

    def _battery_discharges(self) -> list[tuple[datetime, datetime]]:
        controller = getattr(self.runtime, "price_optimization_controller", None)
        if controller is None:
            return []
        actions = list(getattr(controller, "created_actions", {}).values())
        plan = getattr(controller, "plan", None)
        if plan is not None:
            actions.extend(getattr(plan, "actions", ()))
        return [
            (action.start, action.end) for action in actions
            if getattr(getattr(action, "direction", None), "value", None) == "discharge"
        ]

    # ----- actions -------------------------------------------------------------------

    def _remember(self, message: str) -> None:
        self.recent.append({"at": self.clock().isoformat(), "action": message})
        del self.recent[:-RECENT_ACTIONS]

    def _log_once(self, key: str, message: str, cause: str) -> None:
        """Log a state (not an action) only when it changes."""
        if self._last_logged.get(key) == message:
            return
        self._last_logged[key] = message
        log_activity(self.runtime, EV_STATUS, message, cause)

    async def _call(
        self, domain: str, service: str, data: dict[str, Any], message: str, cause: str
    ) -> None:
        from homeassistant.core import Context

        context = Context()
        log_activity(self.runtime, EV_STATUS, message, cause, context=context)
        self._remember(message)
        await self.runtime.hass.services.async_call(
            domain, service, data, blocking=True, context=context
        )

    async def _command(self, device_id: str | None, command: str, why: str) -> None:
        now = self.monotonic()
        if self._backoff_until is not None and now < self._backoff_until:
            return
        if self._last_command is not None and now - self._last_command < COMMAND_INTERVAL_SECONDS:
            return
        if self._command_wanted == command:
            self._command_attempts += 1
        else:
            self._command_wanted, self._command_attempts = command, 1
        if self._command_attempts > COMMAND_RETRIES:
            self._backoff_until = now + COMMAND_BACKOFF_SECONDS
            self._command_attempts = 0
            self._log_once(
                "command", f"Charger did not {command} after {COMMAND_RETRIES} tries",
                f"Who: EV charging. Why: the charger status stayed {self.charger_state}; "
                f"trying again in {COMMAND_BACKOFF_SECONDS // 60} minutes",
            )
            return
        self._last_command = now
        verb = {"pause": "paused", "resume": "started"}[command]
        config = self.config
        entity = config.charger_start_entity if command == "resume" else config.charger_pause_entity
        if entity:
            domain = entity.split(".", 1)[0]
            service = (
                "press" if domain == "button"
                else "turn_on" if domain == "script" or command == "resume"
                else "turn_off"
            )
            await self._call(
                domain, service, {"entity_id": entity},
                f"Car charging {verb}", f"Who: EV charging. Why: {why}",
            )
        else:
            if device_id is None:
                raise ValueError("charger_device_unknown")
            await self._call(
                "easee", "action_command", {"device_id": device_id, "action_command": command},
                f"Car charging {verb}", f"Who: EV charging. Why: {why}",
            )
        self.paused_by_us = command == "pause"
        await self._save()

    async def _write_limit(
        self, device_id: str | None, limit: int, why: str, who: str = "Who: EV load balancing"
    ) -> None:
        previous = self.applied_limit_a
        self._last_limit_write = self.monotonic()
        message = f"Charger current {previous if previous is not None else '?'} A → {limit} A"
        cause = f"{who}. Why: {why}"
        entity = self.config.charger_current_entity
        if entity:
            domain = entity.split(".", 1)[0]
            await self._call(domain, "set_value", {"entity_id": entity, "value": limit},
                             message, cause)
        else:
            if device_id is None:
                raise ValueError("charger_device_unknown")
            await self._call(
                "easee", "set_circuit_dynamic_limit",
                {"device_id": device_id, "current_p1": limit, "current_p2": limit,
                 "current_p3": limit, "time_to_live": 0},
                message, cause,
            )
        self.applied_limit_a = limit
        if not self.limit_owned:
            self.limit_owned = True
            await self._save()

    # ----- one pass ------------------------------------------------------------------

    async def tick(self) -> None:
        config = self.config
        if not config.configured:
            self.status, self.reason = "not_configured", None
            return
        smart, balancing = self.smart_enabled, self.balancing_enabled
        state = self._state(config.charger_status_entity)
        self.charger_state = None if state is None else str(state.state)
        device_id = self._charger_device_id(config)
        needs_device = (smart and not config.charger_start_entity) or (
            balancing and not config.charger_current_entity
        )
        if device_id is None and needs_device:
            self.status, self.reason = "blocked", "charger_device_unknown"
            return
        self.charger_phase = self._phase(config)
        connected = self.charger_phase != "disconnected"
        soc = self._number(config.car_soc_entity)
        if soc is not None and 0 <= soc <= 100:
            self.car_soc, self.car_soc_at = soc, self.clock()
        limit_sensor = self._number(config.charger_limit_entity)
        if limit_sensor is not None:
            self.applied_limit_a = int(round(limit_sensor))
        self._learn_car_power(config)

        if not connected:
            await self._disconnected(config, device_id, balancing)
            return

        want_charge: bool | None = None
        why = ""
        solar = self.solar_enabled
        if smart or solar:
            await self._sync_car_limit(config)
        if smart:
            self.plan = self._plan(config)
            want_charge, why = self.plan.charge, self.plan.detail
        else:
            self.plan = None
            self.committed_until = None
        plan_charging = want_charge is True
        solar_on = self._solar_step(config) if solar else self._solar_reset()
        self._solar_driving = solar_on and not plan_charging
        if solar and not plan_charging:
            if solar_on:
                want_charge, why = True, self.solar_detail
            elif want_charge is None:
                want_charge, why = False, self.solar_detail
        lb_limit, lb_detail = (
            await self._balance(config, device_id) if balancing else (None, "")
        )
        if self._solar_driving and self.solar_limit_a is not None:
            target = self.solar_limit_a if lb_limit is None else min(lb_limit, self.solar_limit_a)
            await self._apply_limit(
                config, device_id, target,
                self.solar_detail + ("" if lb_limit is None or lb_limit >= self.solar_limit_a
                                     else f"; the fuse allows only {lb_limit} A"),
                "Who: EV solar charging",
            )
        elif lb_limit is not None:
            await self._apply_limit(config, device_id, lb_limit, lb_detail,
                                    "Who: EV load balancing")
        elif self.limit_owned:
            await self._release_limit(config, device_id)
        if self.lb_paused:
            want_charge, why = False, self.balance_detail or "the fuse leaves too little current"
        elif not smart and not solar and self.paused_by_us:
            want_charge, why = True, "smart charging is off, so the pause it made is undone"
        if want_charge is None:
            self.status, self.reason = "idle", None
            return
        await self._apply(config, device_id, want_charge, why)

    def _solar_reset(self) -> bool:
        if self.solar_active:
            self._log_once("solar", "EV solar charging stopped",
                           "Who: EV solar charging. Why: solar charging was switched off")
        self.solar_active, self.solar_limit_a = False, None
        self.solar_reason, self.solar_detail = None, ""
        self._solar_ok_since = self._solar_low_since = None
        return False

    def _solar_stop(self, reason: str, detail: str) -> bool:
        if self.solar_active:
            self._log_once("solar", "EV solar charging stopped",
                           f"Who: EV solar charging. Why: {detail}")
        self.solar_active, self.solar_limit_a = False, None
        self.solar_reason, self.solar_detail = reason, detail
        self._solar_ok_since = self._solar_low_since = None
        return False

    def _solar_step(self, config: EvChargingConfig) -> bool:
        """Whether the solar surplus should charge the car now, and at what current."""
        target = float(self._setting("ev_solar_target_soc_percent"))
        if self.car_soc is not None and self.car_soc >= target:
            return self._solar_stop(
                "solar_target_reached",
                f"the car is at {self.car_soc:.0f} %, the solar charging target is {target:.0f} %",
            )
        sell = float(self._setting("ev_solar_sell_price_sek_per_kwh"))
        price = self._price_now(config)
        if price is not None and price >= sell:
            return self._solar_stop(
                "selling_is_better",
                f"the price now is {price:.2f} kr/kWh, at or above the sell-instead price "
                f"{sell:.2f} kr/kWh, so the surplus is sold",
            )
        try:
            export, source = self._export_w(config)
            credit = self._battery_solar_w()
            ev_kw = self._measured_kw(config) if self.charger_phase == "charging" else 0.0
            amps, available = solar_current(
                export_w=export + credit,
                ev_power_w=max(0.0, (ev_kw or 0.0) * 1000),
                phases=config.phases,
                voltage_v=config.voltage_v,
                min_current_a=config.min_current_a,
                max_current_a=config.max_current_a,
                grid_allowance_w=float(self._setting("ev_solar_grid_allowance_kw")) * 1000,
            )
        except ValueError as error:
            self.solar_export_w = self.solar_available_w = None
            return self._solar_stop(
                "solar_data_unavailable",
                f"the grid power is unavailable ({error}), so solar surplus is unknown",
            )
        self.solar_export_w, self.solar_available_w = export, available
        battery = "" if credit <= 0 else f" plus {credit / 1000:.1f} kW the home battery gives up"
        now = self.monotonic()
        if amps is None:
            self._solar_ok_since = None
            minimum_kw = config.phases * config.voltage_v * config.min_current_a / 1000
            detail = (
                f"the solar surplus gives {available / 1000:.1f} kW for the car{battery} "
                f"({source}), below the {minimum_kw:.1f} kW the minimum "
                f"{config.min_current_a} A needs"
            )
            if not self.solar_active:
                self.solar_reason, self.solar_detail = "waiting_for_sun", detail
                self.solar_limit_a = None
                return False
            if self._solar_low_since is None:
                self._solar_low_since = now
            delay = 60 * float(self._setting("ev_solar_stop_delay_minutes"))
            if now - self._solar_low_since >= delay:
                return self._solar_stop("waiting_for_sun", detail)
            self.solar_reason = "solar_dipping"
            self.solar_detail = (
                detail + f"; it keeps the minimum current for up to {delay / 60:.0f} min"
            )
            self.solar_limit_a = config.min_current_a
            return True
        self._solar_low_since = None
        detail = (
            f"the solar surplus gives {available / 1000:.1f} kW for the car{battery} ({source}), "
            f"enough for {amps} A"
            + ("" if price is None else f"; the price {price:.2f} kr/kWh is below the "
               f"sell-instead price {sell:.2f} kr/kWh")
        )
        if not self.solar_active:
            if self._solar_ok_since is None:
                self._solar_ok_since = now
            delay = 60 * float(self._setting("ev_solar_start_delay_minutes"))
            if now - self._solar_ok_since < delay:
                self.solar_reason = "sun_confirming"
                self.solar_detail = detail + f"; starting once it has held for {delay / 60:.0f} min"
                self.solar_limit_a = None
                return False
            self.solar_active = True
            self._last_logged.pop("solar", None)
            self._log_once("solar", "EV solar charging started",
                           f"Who: EV solar charging. Why: {detail}")
        self.solar_reason, self.solar_detail, self.solar_limit_a = "solar_surplus", detail, amps
        return True

    def _measured_kw(self, config: EvChargingConfig) -> float | None:
        power = self._number(config.charger_power_entity)
        if power is None:
            return None
        state = self._state(config.charger_power_entity)
        unit = None if state is None else state.attributes.get("unit_of_measurement")
        return power / 1000 if unit == "W" else power

    def _learn_car_power(self, config: EvChargingConfig) -> None:
        """Remember the power the car itself accepts (one phase, cold battery, taper)."""
        measured = self._measured_kw(config)
        if self.charger_phase != "charging" or measured is None or measured <= 0:
            self._car_limited_since = None
            return
        limit = min(config.max_current_a, self.applied_limit_a or config.max_current_a)
        allowed = config.phases * config.voltage_v * limit / 1000
        if measured < allowed * CAR_LIMITED_SHARE:
            now = self.monotonic()
            if self._car_limited_since is None:
                self._car_limited_since = now
            if now - self._car_limited_since >= CAR_LIMIT_LEARN_SECONDS:
                self.car_max_kw = (
                    measured if self.car_max_kw is None
                    else 0.8 * self.car_max_kw + 0.2 * measured
                )
        else:
            self._car_limited_since = None
            if self.car_max_kw is not None and measured > self.car_max_kw:
                self.car_max_kw = measured

    def power_at(self, start: datetime, end: datetime) -> float:
        """Charging power available in a period: fuse room, house load and the car."""
        config = self.config
        amps: float = config.max_current_a
        if self.balancing_enabled:
            now = self.clock()
            if start <= now < end and self.available_a is not None:
                amps = min(amps, self.available_a)
            else:
                forecast = getattr(self.runtime, "energy_forecast", None)
                try:
                    house_kw = None if forecast is None else forecast.load_kw(start, end)
                except Exception:  # noqa: BLE001 - no forecast means the fuse alone decides
                    house_kw = None
                if house_kw is not None and house_kw >= 0:
                    house_a = house_kw * 1000 / (config.voltage_v * 3) * HOUSE_PHASE_IMBALANCE
                    amps = min(amps, int(config.fuse_a - config.margin_a - house_a))
        if amps < config.min_current_a:
            return 0.0
        power = config.phases * config.voltage_v * amps / 1000
        if self.car_max_kw is not None:
            power = min(power, self.car_max_kw)
        return power

    def _plan(self, config: EvChargingConfig) -> EvPlan:
        now = self.clock()
        target = float(self._setting("ev_target_soc_percent"))
        minimum = float(self._setting("ev_min_soc_percent"))
        ready = int(self._setting("ev_ready_by_hour"))
        deadline = next_deadline(dt_util.as_local(now), ready).astimezone(UTC)
        prices = self._prices(config)
        plan = plan_charging(
            now=now,
            soc=self.car_soc,
            target_soc=target,
            min_soc=minimum,
            deadline=deadline,
            capacity_kwh=config.battery_capacity_kwh,
            efficiency_percent=config.charge_efficiency_percent,
            power_kw=config.max_power_kw,
            prices=prices,
            battery_discharges=self._battery_discharges(),
            allow_battery=self._setting("ev_allow_battery_discharge") is True,
            committed_until=self.committed_until,
            power_at=self.power_at,
            solar_kw_at=self.solar_surplus_kw if self.solar_enabled else None,
            grid_extra_cost=float(self._setting("price_grid_import_cost_sek_per_kwh") or 0.0),
            sell_price=float(self._setting("ev_solar_sell_price_sek_per_kwh")),
        )
        if plan.charge and plan.reason == "cheap_price":
            current = next((s for s in plan.slots if s.start <= now < s.end), None)
            self.committed_until = None if current is None else current.end
        elif not plan.charge:
            self.committed_until = None
        return plan

    async def _apply(
        self, config: EvChargingConfig, device_id: str | None, want_charge: bool, why: str
    ) -> None:
        state = self.charger_phase
        plan = self.plan
        if want_charge:
            self.status = (
                ("solar_charging" if self._solar_driving else "charging") if state in CHARGING
                else "starting" if state in PAUSED
                else state or "unknown"
            )
            self.reason = (
                self.solar_reason if self._solar_driving
                else None if plan is None else plan.reason
            )
            if state in PAUSED:
                await self._command(device_id, "resume", why)
            elif state in COMPLETED:
                await self._restart_car(config, why)
            elif state in CHARGING:
                self._command_attempts = 0
            return
        self.status = "paused_for_load" if self.lb_paused else (
            "target_reached" if plan is not None and plan.reason == "target_reached"
            else "waiting"
        )
        self.reason = (
            plan.reason if plan is not None and plan.reason != "target_reached"
            else self.solar_reason or (None if plan is None else plan.reason)
        )
        if state in CHARGING:
            await self._command(device_id, "pause", why)
        elif state in PAUSED:
            self._command_attempts = 0

    async def _restart_car(self, config: EvChargingConfig, why: str) -> None:
        """The charger says completed; the car stopped at its own limit."""
        car_limit = self._number(config.car_limit_entity)
        if (
            not config.car_charging_switch
            or self.car_soc is None
            or (car_limit is not None and self.car_soc >= car_limit - 1)
        ):
            return
        now = self.monotonic()
        if self._car_switch_attempt is not None and (
            now - self._car_switch_attempt < CAR_SWITCH_RETRY_SECONDS
        ):
            return
        self._car_switch_attempt = now
        await self._call(
            "switch", "turn_on", {"entity_id": config.car_charging_switch},
            "Car asked to start charging",
            f"Who: EV charging. Why: the charger reports completed while the car is at "
            f"{self.car_soc:.0f} %, below its limit; {why}",
        )

    async def _sync_car_limit(self, config: EvChargingConfig) -> None:
        if not config.car_limit_entity:
            return
        state = self._state(config.car_limit_entity)
        if state is None:
            return
        attrs = state.attributes
        target = max(
            float(self._setting("ev_target_soc_percent")) if self.smart_enabled else 0.0,
            float(self._setting("ev_solar_target_soc_percent")) if self.solar_enabled else 0.0,
        )
        try:
            desired = car_limit_for_target(
                target,
                float(attrs.get("min", 50)),
                float(attrs.get("max", 100)),
                float(attrs.get("step", 10)),
            )
        except (TypeError, ValueError):
            return
        current = self._number(config.car_limit_entity)
        if current is not None and abs(current - desired) < 0.5:
            return
        now = self.monotonic()
        if self._car_limit_attempt is not None:
            at, value = self._car_limit_attempt
            if value == desired and now - at < CAR_LIMIT_RETRY_SECONDS:
                return
        self._car_limit_attempt = (now, desired)
        note = (
            "" if desired <= target + 1e-9
            else f"; the car cannot go below {desired:.0f} %, so charging pauses at "
                 f"{target:.0f} % instead"
        )
        await self._call(
            "number", "set_value", {"entity_id": config.car_limit_entity, "value": desired},
            f"Car charge limit {current if current is None else round(current)} % → "
            f"{desired:.0f} %",
            f"Who: EV charging. Why: the highest EV target (smart or solar charging) is "
            f"{target:.0f} %{note}",
        )

    async def _balance(
        self, config: EvChargingConfig, device_id: str | None
    ) -> tuple[int, str]:
        """The fuse's charger current; writes nothing except the safe minimum."""
        state = self.charger_phase
        try:
            currents, credit = self._phase_currents(config)
            decision = charger_current(
                phase_currents_a=currents,
                ev_current_a=self._ev_current_a(config) if state in CHARGING else 0.0,
                fuse_a=config.fuse_a,
                margin_a=config.margin_a,
                min_current_a=config.min_current_a,
                max_current_a=config.max_current_a,
                battery_credit_a=credit,
            )
        except ValueError as error:
            # Without phase data the safe current is the minimum.
            self.headroom_a, self.balance_detail = None, str(error)
            self.available_a = config.min_current_a
            self._log_once("balance", "EV load balancing has no phase currents",
                           f"Who: EV load balancing. Why: {error}; the charger is held at "
                           f"{config.min_current_a} A")
            if self.applied_limit_a is None or self.applied_limit_a > config.min_current_a:
                if self._limit_write_allowed(decrease=True):
                    await self._write_limit(device_id, config.min_current_a,
                                            "phase currents are unavailable")
            return config.min_current_a, "phase currents are unavailable"
        self._last_logged.pop("balance", None)
        self.headroom_a, self.balance_detail = decision.headroom_a, decision.detail
        self.available_a = 0 if decision.pause else decision.limit_a
        now = self.monotonic()
        if decision.pause:
            self._lb_ok_since = None
            if not self.lb_paused and state in CHARGING:
                self.lb_paused = True
                self._remember("Paused for load: " + decision.detail)
        elif self.lb_paused:
            if decision.limit_a >= config.min_current_a + LB_RESUME_MARGIN_A:
                if self._lb_ok_since is None:
                    self._lb_ok_since = now
                if now - self._lb_ok_since >= LB_RESUME_HOLD_SECONDS:
                    self.lb_paused, self._lb_ok_since = False, None
                    self._remember("Load allows charging again: " + decision.detail)
            else:
                self._lb_ok_since = None
        return decision.limit_a, decision.detail

    async def _apply_limit(
        self, config: EvChargingConfig, device_id: str | None, target: int, detail: str,
        who: str,
    ) -> None:
        current = self.applied_limit_a
        if current is None or target < current:
            if self._limit_write_allowed(decrease=True):
                await self._write_limit(device_id, target, detail, who)
        elif target > current and self.charger_phase in CHARGING:
            target = min(target, current + LIMIT_INCREASE_STEP_A)
            if self._limit_write_allowed(decrease=False):
                await self._write_limit(device_id, target, detail, who)

    def _limit_write_allowed(self, *, decrease: bool) -> bool:
        if self._last_limit_write is None:
            return True
        wait = LIMIT_DECREASE_INTERVAL_SECONDS if decrease else LIMIT_INCREASE_INTERVAL_SECONDS
        return self.monotonic() - self._last_limit_write >= wait

    async def _release_limit(self, config: EvChargingConfig, device_id: str | None) -> None:
        if self.applied_limit_a != config.max_current_a:
            await self._write_limit(
                device_id, config.max_current_a,
                "neither EV load balancing nor EV solar charging controls the current now, "
                "so the charger gets its full current back",
                "Who: EV charging",
            )
        self.limit_owned = False
        self.lb_paused = False
        await self._save()

    async def _disconnected(
        self, config: EvChargingConfig, device_id: str | None, balancing: bool
    ) -> None:
        self.plan, self.committed_until, self.lb_paused = None, None, False
        self.car_max_kw, self._car_limited_since = None, None
        self._solar_driving = False
        if self.solar_active:
            self._solar_stop("car_not_connected", "the car was unplugged")
        self._solar_ok_since = self._solar_low_since = None
        self._command_attempts, self._command_wanted = 0, None
        if self.paused_by_us:
            self.paused_by_us = False
            await self._save()
        self.status, self.reason = "car_not_connected", None
        if balancing:
            # Start the next session low, like a fresh plug-in should.
            if (
                self.applied_limit_a is not None
                and self.applied_limit_a != config.min_current_a
                and self._limit_write_allowed(decrease=True)
            ):
                await self._write_limit(
                    device_id, config.min_current_a,
                    "the car is unplugged, so the next session starts at the minimum current "
                    "and rises as the fuse allows",
                )
        elif self.limit_owned:
            await self._release_limit(config, device_id)

    # ----- presentation ----------------------------------------------------------------

    def attributes(self) -> dict[str, Any]:
        config = self.config
        plan = self.plan
        return {
            "reason": self.reason,
            "smart_charging_enabled": self.smart_enabled,
            "load_balancing_enabled": self.balancing_enabled,
            "charger_status": self.charger_state,
            "car_soc_percent": self.car_soc,
            "car_soc_updated": None if self.car_soc_at is None else self.car_soc_at.isoformat(),
            "target_soc_percent": self._setting("ev_target_soc_percent"),
            "min_soc_percent": self._setting("ev_min_soc_percent"),
            "ready_by_hour": self._setting("ev_ready_by_hour"),
            "battery_may_charge_car": self._setting("ev_allow_battery_discharge") is True,
            "price_entity": config.price_entity or "price optimization's",
            "used_entities": {
                key: getattr(config, key)
                for key in (
                    "charger_status_entity", "charger_power_entity", "charger_limit_entity",
                    "car_soc_entity", "car_limit_entity", "car_charging_switch",
                    "car_plugged_entity",
                )
            },
            "charger_device_id": self._charger_device_id(config),
            "charger_current_limit_a": self.applied_limit_a,
            "phase_headroom_a": None if self.headroom_a is None else round(self.headroom_a, 1),
            "load_balancing_detail": self.balance_detail,
            "paused_for_load": self.lb_paused,
            "paused_by_integration": self.paused_by_us,
            "fuse_a": config.fuse_a,
            "margin_a": config.margin_a,
            "max_charge_power_kw": round(config.max_power_kw, 2),
            "charge_power_now_kw": round(
                self.power_at(self.clock(), self.clock() + timedelta(minutes=1)), 2
            ),
            "car_max_power_kw": None if self.car_max_kw is None else round(self.car_max_kw, 2),
            "solar_charging_enabled": self.solar_enabled,
            "solar_charging_active": self.solar_active,
            "solar_reason": self.solar_reason,
            "solar_detail": self.solar_detail,
            "solar_export_now_w": (
                None if self.solar_export_w is None else round(self.solar_export_w)
            ),
            "solar_power_for_car_w": (
                None if self.solar_available_w is None else round(self.solar_available_w)
            ),
            "solar_current_a": self.solar_limit_a,
            "solar_target_soc_percent": self._setting("ev_solar_target_soc_percent"),
            "sell_instead_price_sek_per_kwh": self._setting("ev_solar_sell_price_sek_per_kwh"),
            "car_before_home_battery": self._setting("ev_solar_car_before_battery") is True,
            "grid_power_source": (
                config.grid_power_entity
                or (config.grid_export_entity and
                    f"{config.grid_export_entity} - {config.grid_import_entity}")
                or "Pixii meter"
            ),
            "solar_forecast_source": (
                list(config.solar_forecast_entities) or "energy forecast's"
            ),
            "recent_actions": list(self.recent),
            **({} if plan is None else plan.attributes()),
            "next_planned_start": next(
                (clock(s.start) for s in (plan.slots if plan else ()) if s.end > self.clock()),
                None,
            ),
        }


__all__ = ["EV_STATUS", "EvChargingController"]
