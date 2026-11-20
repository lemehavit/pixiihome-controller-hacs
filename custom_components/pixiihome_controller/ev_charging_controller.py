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
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .activity import clock, log_activity
from .const import DOMAIN
from .control.ev_charging import (
    CONF_EV_CHARGING,
    EvChargingConfig,
    EvPlan,
    car_limit_for_target,
    charger_current,
    next_deadline,
    plan_charging,
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

DISCONNECTED = {"disconnected", "unplugged", "not_connected", "unknown", "unavailable", ""}
CHARGING = {"charging", "on"}
COMPLETED = {"completed", "finished", "full"}
# Charger power above this means the car is drawing current, whatever the status says.
CHARGING_POWER_KW = 0.5
# Canonical charger phases used by the decisions below.
PAUSED = {"paused"}


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

    # ----- configuration and lifecycle -------------------------------------------------

    @property
    def config(self) -> EvChargingConfig:
        entry = self.runtime.entry
        try:
            return EvChargingConfig.from_mapping(
                None if entry is None else entry.options.get(CONF_EV_CHARGING)
            )
        except (TypeError, ValueError):
            return EvChargingConfig()

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

    async def _write_limit(self, device_id: str | None, limit: int, why: str) -> None:
        previous = self.applied_limit_a
        self._last_limit_write = self.monotonic()
        message = f"Charger current {previous if previous is not None else '?'} A → {limit} A"
        cause = f"Who: EV load balancing. Why: {why}"
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

        if not connected:
            await self._disconnected(config, device_id, balancing)
            return

        want_charge: bool | None = None
        why = ""
        if smart:
            await self._sync_car_limit(config)
            self.plan = self._plan(config)
            want_charge, why = self.plan.charge, self.plan.detail
        else:
            self.plan = None
            self.committed_until = None
        if balancing:
            await self._balance(config, device_id)
        elif self.limit_owned:
            await self._release_limit(config, device_id)
        if self.lb_paused:
            want_charge, why = False, self.balance_detail or "the fuse leaves too little current"
        elif not smart and self.paused_by_us:
            want_charge, why = True, "smart charging is off, so the pause it made is undone"
        if want_charge is None:
            self.status, self.reason = "idle", None
            return
        await self._apply(config, device_id, want_charge, why)

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
                "charging" if state in CHARGING
                else "starting" if state in PAUSED
                else state or "unknown"
            )
            self.reason = None if plan is None else plan.reason
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
        self.reason = None if plan is None else plan.reason
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
        target = float(self._setting("ev_target_soc_percent"))
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
            f"Who: EV charging. Why: the EV target is {target:.0f} %{note}",
        )

    async def _balance(self, config: EvChargingConfig, device_id: str | None) -> None:
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
            self._log_once("balance", "EV load balancing has no phase currents",
                           f"Who: EV load balancing. Why: {error}; the charger is held at "
                           f"{config.min_current_a} A")
            if self.applied_limit_a is None or self.applied_limit_a > config.min_current_a:
                if self._limit_write_allowed(decrease=True):
                    await self._write_limit(device_id, config.min_current_a,
                                            "phase currents are unavailable")
            return
        self._last_logged.pop("balance", None)
        self.headroom_a, self.balance_detail = decision.headroom_a, decision.detail
        now = self.monotonic()
        if decision.pause:
            self._lb_ok_since = None
            if not self.lb_paused and state in CHARGING:
                self.lb_paused = True
                self._remember("Paused for load: " + decision.detail)
        elif self.lb_paused:
            if decision.limit_a >= config.min_current_a + LB_RESUME_MARGIN_A:
                self._lb_ok_since = self._lb_ok_since or now
                if now - self._lb_ok_since >= LB_RESUME_HOLD_SECONDS:
                    self.lb_paused, self._lb_ok_since = False, None
                    self._remember("Load allows charging again: " + decision.detail)
            else:
                self._lb_ok_since = None
        target = decision.limit_a
        current = self.applied_limit_a
        if current is None or target < current:
            if self._limit_write_allowed(decrease=True):
                await self._write_limit(device_id, target, decision.detail)
        elif target > current and state in CHARGING:
            target = min(target, current + LIMIT_INCREASE_STEP_A)
            if self._limit_write_allowed(decrease=False):
                await self._write_limit(device_id, target, decision.detail)

    def _limit_write_allowed(self, *, decrease: bool) -> bool:
        if self._last_limit_write is None:
            return True
        wait = LIMIT_DECREASE_INTERVAL_SECONDS if decrease else LIMIT_INCREASE_INTERVAL_SECONDS
        return self.monotonic() - self._last_limit_write >= wait

    async def _release_limit(self, config: EvChargingConfig, device_id: str | None) -> None:
        if self.applied_limit_a != config.max_current_a:
            await self._write_limit(
                device_id, config.max_current_a,
                "EV load balancing was switched off, so the charger gets its full current back",
            )
        self.limit_owned = False
        self.lb_paused = False
        await self._save()

    async def _disconnected(
        self, config: EvChargingConfig, device_id: str | None, balancing: bool
    ) -> None:
        self.plan, self.committed_until, self.lb_paused = None, None, False
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
            "price_entity": self.config.price_entity or "price optimization's",
            "charger_current_limit_a": self.applied_limit_a,
            "phase_headroom_a": None if self.headroom_a is None else round(self.headroom_a, 1),
            "load_balancing_detail": self.balance_detail,
            "paused_for_load": self.lb_paused,
            "paused_by_integration": self.paused_by_us,
            "fuse_a": config.fuse_a,
            "margin_a": config.margin_a,
            "max_charge_power_kw": round(config.max_power_kw, 2),
            "recent_actions": list(self.recent),
            **({} if plan is None else plan.attributes()),
            "next_planned_start": next(
                (clock(s.start) for s in (plan.slots if plan else ()) if s.end > self.clock()),
                None,
            ),
        }


__all__ = ["EV_STATUS", "EvChargingController"]
