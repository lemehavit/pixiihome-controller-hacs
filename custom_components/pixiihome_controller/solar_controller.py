"""Automatic surplus-only solar charging through finite Pixii APS leases."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import isfinite
from time import monotonic
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .activity import log_activity
from .control.models import ControlOperation
from .control.phase_gateway import PhaseGateway
from .control.policy import AutomationPolicy
from .control.production import ProductionAdapterRequest
from .control.settings import OperationalSettings
from .control.smart_solar import SmartSolarDecision, evaluate_deferral
from .reasons import reason_text

if TYPE_CHECKING:
    from .runtime import RuntimeData


APS_SERVICE = 105
_STOCKHOLM = ZoneInfo("Europe/Stockholm")
IDLE_SERVICE = 0
SCHEDULER_SERVICE = 101


def build_solar_lease_request(
    settings: OperationalSettings,
    policy: AutomationPolicy,
    *,
    charge_capacity_w: float,
) -> ProductionAdapterRequest:
    """Build the exact surplus-only APS request; discharge is a hard zero."""
    lease_seconds = policy.bounded_duration(
        datetime.now(UTC), int(settings.get("solar_lease_seconds")),
    )
    if lease_seconds <= 0:
        raise ValueError("solar_lease_not_authorized")
    charge_max_w = min(
        float(settings.get("solar_max_charge_power_kw")) * 1000,
        charge_capacity_w,
        policy.max_power_kw * 1000,
    )
    if not isfinite(charge_max_w) or charge_max_w <= 0:
        raise ValueError("solar_charge_capacity_unavailable")
    return ProductionAdapterRequest(
        ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING,
        {
            "export_limit_w": -float(settings.get("solar_export_buffer_kw")) * 1000,
            "import_limit_w": 10_000,
            "charge_max_w": charge_max_w,
            "discharge_max_w": 0,
            "keep_active": True,
            "max_limit_change_w": float(settings.get("solar_max_limit_change_kw"))
            * 1000,
            "duration_seconds": lease_seconds,
        },
    )


class SolarChargingController:
    """Renew short APS leases only while verified surplus and ownership remain."""

    def __init__(
        self,
        runtime: RuntimeData,
        *,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        self.runtime = runtime
        self.clock = clock
        self.status = "disabled"
        self.reason: str | None = None
        self.priority_verified = False
        self.priority_configured = False
        self.priority_list: tuple[int, ...] = ()
        self.potential_surplus_w: float | None = None
        self.grid_power_w: float | None = None
        self.battery_power_w: float | None = None
        self.current_service: int | None = None
        self.writes = 0
        self.renewals = 0
        self._surplus_since: float | None = None
        self._shortage_since: float | None = None
        self._lease_deadline = 0.0
        self._message_id: int | None = None
        self._retiring = False
        self._stop_attempted = False
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self.smart_decision: SmartSolarDecision | None = None
        self._smart_checked_at: float | None = None
        self._smart_hold_until: datetime | None = None
        self._smart_blocked_day: str | None = None
        self._smart_deferring = False

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _publish(self, status: str, reason: str | None = None) -> None:
        if self.status == "active" and status not in {"active", "starting"}:
            why = reason_text(reason) or status.replace("_", " ")
            log_activity(self.runtime, "solar_charging_status", f"Stopping solar charging: {why}",
                         f"Who: automatic solar charging. Why: {why}.")
        self.status, self.reason = status, reason
        for listener in tuple(self._listeners):
            listener()

    def attributes(self) -> dict[str, Any]:
        now = self.clock()
        settings = self.runtime.operational_settings
        return {
            "reason": self.reason,
            "reason_text": reason_text(self.reason) if self.reason else None,
            "grid_power_w": self.grid_power_w,
            "battery_power_w": self.battery_power_w,
            "potential_surplus_w": self.potential_surplus_w,
            "usable_soc_percent": self.runtime.battery.usable_soc,
            "maximum_usable_soc_percent": settings.get("solar_max_soc_percent"),
            "current_service": self.current_service,
            "priority_verified": self.priority_verified,
            "priority_configured": self.priority_configured,
            "service_priority": list(self.priority_list),
            "scheduler_priority": self._priority(SCHEDULER_SERVICE),
            "adaptive_peak_shaving_priority": self._priority(APS_SERVICE),
            "lease_seconds_remaining": max(0, round(self._lease_deadline - now)),
            "owned_message_id": self._message_id,
            "control_writes": self.writes,
            "lease_renewals": self.renewals,
            "target_export_buffer_w": float(settings.get("solar_export_buffer_kw")) * 1000,
            "discharge_max_w": 0,
            "surplus_formula": "max(0, battery_power_w - grid_power_w)",
            "schedule_precedence_required": True,
            "smart_solar_charging": bool(settings.get("solar_smart_enabled")),
            **(self.smart_decision.as_dict() if self.smart_decision is not None else {}),
            "smart_hold_until": (
                self._smart_hold_until.isoformat() if self._smart_hold_until else None
            ),
        }

    def _priority(self, service: int) -> int | None:
        try:
            return self.priority_list.index(service) + 1
        except ValueError:
            return None

    def owns_live_lease(self) -> bool:
        """Local identity only; callers must additionally verify keyed readback."""
        return self._message_id is not None and self.clock() < self._lease_deadline

    @staticmethod
    def validate_priority(used_list: object) -> tuple[int, ...]:
        if (
            not isinstance(used_list, list)
            or any(type(item) is not int for item in used_list)
            or SCHEDULER_SERVICE not in used_list
            or APS_SERVICE not in used_list
            or used_list.index(SCHEDULER_SERVICE) >= used_list.index(APS_SERVICE)
        ):
            raise ValueError("solar_scheduler_priority_unverified")
        return tuple(used_list)

    @staticmethod
    def priority_with_aps(used_list: object) -> tuple[tuple[int, ...], bool]:
        """Return an exact priority list with APS directly below Scheduler."""
        if (
            not isinstance(used_list, list)
            or not used_list
            or len(used_list) > 32
            or len(set(used_list)) != len(used_list)
            or any(type(item) is not int or not 101 <= item <= 129 for item in used_list)
            or SCHEDULER_SERVICE not in used_list
        ):
            raise ValueError("solar_scheduler_priority_unverified")
        if APS_SERVICE in used_list:
            return SolarChargingController.validate_priority(used_list), False
        configured = list(used_list)
        configured.insert(configured.index(SCHEDULER_SERVICE) + 1, APS_SERVICE)
        return SolarChargingController.validate_priority(configured), True

    async def prepare(self) -> None:
        """Verify that schedules can preempt APS before the switch is persisted."""
        if self.runtime.controller_state.adaptive_peak_shaving_enabled:
            raise ValueError("solar_conflicts_with_adaptive_peak_shaving")
        scheduler = self.runtime.gateway_scheduler
        if scheduler is None:
            raise ValueError("solar_gateway_connection_required")
        gateway = PhaseGateway(scheduler.client)
        priority = await gateway.read(100)
        if priority.get("enabled") != 1:
            raise ValueError("solar_service_priority_disabled")
        priority_list, must_configure = self.priority_with_aps(priority.get("used_list"))
        if must_configure:
            await gateway.priority(list(priority_list))
        self.priority_list = priority_list
        self.priority_configured = must_configure
        self.priority_verified = True
        self._publish("ready")

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = self.runtime.hass.async_create_background_task(
                self._run(),
                "pixiihome_controller automatic solar charging",
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._stopping = True
            # Do not cancel a Gateway write halfway through its journaled command.
            async with self._lock:
                pass
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._message_id is not None and self.clock() < self._lease_deadline:
            try:
                result = await self.runtime.async_execute_solar_lease(
                    self._message_id,
                    stop=True,
                )
            except Exception:
                result = None
            if result is not None and result.get("successful") is True:
                self._message_id = None
                self._lease_deadline = 0.0
        self.disable()

    def disable(self) -> None:
        """Stop renewals; the bounded Gateway lease expires without a stop write."""
        self._surplus_since = None
        self._shortage_since = None
        self._retiring = True
        self._publish("disabled", "finite_lease_allowed_to_expire")

    async def _run(self) -> None:
        while True:
            if self._stopping:
                return
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._publish("blocked", "solar_controller_failed")
            interval = int(
                self.runtime.operational_settings.get("solar_update_interval_seconds")
            )
            await asyncio.sleep(interval)

    async def tick(self) -> None:
        """Evaluate telemetry once and start or renew only an owned finite lease."""
        async with self._lock:
            await self._tick_locked()

    async def _tick_locked(self) -> None:
        now = self.clock()
        if self._message_id is not None and now >= self._lease_deadline:
            self.runtime.release_solar_lease_identity(self._message_id)
            self._message_id = None
        enabled = self.runtime.controller_state.automatic_solar_charging_enabled
        if not enabled:
            self.disable()
            return
        if self.runtime.controller_state.adaptive_peak_shaving_enabled:
            self._retiring = True
            self._publish("blocked", "solar_conflicts_with_adaptive_peak_shaving")
            return
        if not self.runtime.production_control_enabled():
            self._publish("blocked", "production_control_disabled")
            return
        policy = self.runtime._automation_policy()
        if (
            not policy.enabled
            or "automatic" not in policy.allowed_operation_classes
            or policy.max_duration_seconds <= 0
            or policy.max_power_kw <= 0
        ):
            self._publish("blocked", "automation_policy_not_authorized")
            return
        if not self.priority_verified:
            try:
                await self.prepare()
            except Exception as error:
                self._publish("blocked", str(error) if isinstance(error, ValueError)
                              else "solar_scheduler_priority_unverified")
                return
        health = self.runtime.plant_health()
        if not (
            health.telemetry_fresh
            and health.system_safe
            and health.battery_safe
            and health.grid_safe
            and health.system_events_clear
            and self.runtime.telemetry.is_fresh("meter")
        ):
            self._publish("waiting_telemetry", "plant_or_meter_telemetry_not_safe")
            return
        battery = self.runtime.battery
        meter = self.runtime.telemetry.value("meter", "meter_w")
        service = self.runtime.telemetry.value("core", "srv_type")
        if (
            battery.power_w is None
            or battery.usable_soc is None
            or battery.charge_power_limit_w is None
            or isinstance(meter, bool)
            or not isinstance(meter, (int, float))
            or not all(
                isfinite(value)
                for value in (
                    battery.power_w,
                    battery.usable_soc,
                    battery.charge_power_limit_w,
                    float(meter),
                )
            )
            or type(service) is not int
        ):
            self._publish("waiting_telemetry", "solar_telemetry_incomplete")
            return
        self.grid_power_w = float(meter)
        self.battery_power_w = battery.power_w
        self.current_service = service
        self.potential_surplus_w = max(0.0, battery.power_w - float(meter))
        settings = self.runtime.operational_settings
        max_soc = min(float(settings.get("solar_max_soc_percent")), policy.soc_max_percent)
        if battery.usable_soc >= max_soc:
            self._retiring = True
            self._surplus_since = None
            if service == APS_SERVICE and self.owns_live_lease():
                await self._retire_at_soc_limit()
            self._publish("soc_limit", "maximum_usable_soc_reached")
            return
        if battery.charge_power_limit_w <= 0:
            self._retiring = True
            self._publish("blocked", "solar_charge_capacity_unavailable")
            return
        owns_active_aps = service == APS_SERVICE and now < self._lease_deadline
        if service == APS_SERVICE and not owns_active_aps:
            self._publish("waiting_for_idle", "foreign_adaptive_peak_shaving_active")
            return
        if service not in {IDLE_SERVICE, APS_SERVICE}:
            self._surplus_since = None
            self._shortage_since = None
            self._retiring = True
            self._publish(
                "waiting_for_idle",
                "scheduler_active" if service == SCHEDULER_SERVICE else "another_service_active",
            )
            return
        if service == IDLE_SERVICE and now < self._lease_deadline:
            self._publish("starting", "waiting_for_adaptive_peak_shaving_readback")
            return
        start_w = float(settings.get("solar_export_start_threshold_kw")) * 1000
        stop_w = float(settings.get("solar_export_stop_threshold_kw")) * 1000
        if owns_active_aps:
            if self._retiring:
                self._publish("lease_expiring", "solar_lease_not_renewed")
                return
            if self._smart_defers(max_soc):
                self._retiring = True
                self._publish("lease_expiring", "smart_solar_deferring")
                return
            if self.potential_surplus_w < stop_w:
                if self._shortage_since is None:
                    self._shortage_since = now
                if now - self._shortage_since >= int(
                    settings.get("solar_stop_debounce_seconds")
                ):
                    self._retiring = True
                    self._publish("lease_expiring", "solar_surplus_below_stop_threshold")
                    return
            else:
                self._shortage_since = None
            lease_seconds = int(settings.get("solar_lease_seconds"))
            if self._lease_deadline - now <= lease_seconds / 2:
                if self._message_id is None:
                    self._retiring = True
                    self._publish("lease_expiring", "solar_lease_identity_missing")
                    return
                ownership = await self.runtime.async_verify_solar_lease(self._message_id)
                if ownership.get("successful") is not True:
                    self._retiring = True
                    self._publish("lease_expiring", "solar_lease_ownership_lost")
                    return
                await self._write_lease(renewal=True)
            else:
                self._publish("active")
            return
        self._retiring = False
        self._stop_attempted = False
        self._shortage_since = None
        if self.potential_surplus_w < start_w:
            self._surplus_since = None
            self._publish("waiting_surplus")
            return
        if self._smart_defers(max_soc):
            self._surplus_since = None
            self._publish("deferring", "smart_solar_export_now_charge_later")
            return
        if self._surplus_since is None:
            self._surplus_since = now
        if now - self._surplus_since < int(settings.get("solar_start_debounce_seconds")):
            self._publish("waiting_surplus", "solar_start_debounce")
            return
        await self._write_lease(renewal=False)

    def _smart_defers(self, max_soc: float) -> bool:
        """Whether smart solar charging lets the surplus be exported for now.

        Any missing input, a solar shortfall today or a margin that is no longer
        enough answers False (charge now). After deferring ends for lack of
        margin, charging is held on for 30 minutes to avoid flapping.
        """
        settings = self.runtime.operational_settings
        forecast = getattr(self.runtime, "energy_forecast", None)
        if not bool(settings.get("solar_smart_enabled")) or forecast is None:
            self.smart_decision = None
            return self._smart_transition(False, "off")
        clock_now = self.clock()
        if (
            self._smart_checked_at is not None
            and clock_now - self._smart_checked_at < 60
            and self.smart_decision is not None
        ):
            return self.smart_decision.defer
        self._smart_checked_at = clock_now
        now = datetime.now(UTC)
        today = now.astimezone(_STOCKHOLM).date().isoformat()
        battery = self.runtime.battery
        policy = self.runtime._automation_policy()
        if self._smart_blocked_day == today:
            self.smart_decision = SmartSolarDecision(False, "solar_below_forecast_today")
            return self._smart_transition(False, self.smart_decision.reason)
        if forecast.config.solar_power_entity_id:
            ratio = forecast.correction.ratio(now, timedelta(minutes=30))
            if ratio is not None and ratio < 0.6:
                self._smart_blocked_day = today
                self.smart_decision = SmartSolarDecision(False, "solar_below_forecast_today")
                return self._smart_transition(False, self.smart_decision.reason)
        if self._smart_hold_until is not None and now < self._smart_hold_until:
            self.smart_decision = SmartSolarDecision(False, "charging_held_after_deferral")
            return self._smart_transition(False, self.smart_decision.reason)
        if battery.usable_soc is None or battery.charge_power_limit_w is None:
            self.smart_decision = SmartSolarDecision(False, "battery_state_unknown")
            return self._smart_transition(False, self.smart_decision.reason)

        def surplus(start: datetime, end: datetime) -> float | None:
            solar = forecast.solar_forecast_kw(start, end, conservative=True)
            if solar is None:
                return None
            return solar - forecast.load_kw(start, end, high=True)

        decision = evaluate_deferral(
            now=now,
            usable_soc_percent=float(battery.usable_soc),
            target_soc_percent=max_soc,
            capacity_kwh=float(settings.get("price_usable_capacity_kwh")),
            charge_kw=min(
                float(settings.get("solar_max_charge_power_kw")),
                max(0.0, float(battery.charge_power_limit_w) / 1000),
                policy.max_power_kw,
            ),
            round_trip_efficiency_percent=float(
                settings.get("price_round_trip_efficiency_percent")
            ),
            safety_margin_percent=float(settings.get("solar_smart_safety_margin_percent")),
            minimum_advantage_sek_per_kwh=float(
                settings.get("solar_smart_min_advantage_sek_per_kwh")
            ),
            prices=forecast.price_intervals(),
            surplus_kw=surplus,
        )
        self.smart_decision = decision
        if (
            not decision.defer
            and self._smart_deferring
            and decision.reason in {"surplus_too_small_later", "forecast_unavailable"}
        ):
            self._smart_hold_until = now + timedelta(minutes=30)
        return self._smart_transition(decision.defer, decision.reason)

    def _smart_transition(self, defer: bool, reason: str) -> bool:
        if defer and not self._smart_deferring:
            decision = self.smart_decision
            assert decision is not None
            log_activity(
                self.runtime, "solar_charging_status",
                "Smart solar charging is exporting now at "
                f"{decision.export_price_now or 0:.2f} kr/kWh and will charge later at about "
                f"{decision.charge_price or 0:.2f} kr/kWh (conservative surplus later "
                f"{decision.available_later_kwh or 0:.1f} kWh, needed "
                f"{decision.need_kwh or 0:.1f} kWh)",
            )
        elif not defer and self._smart_deferring:
            log_activity(self.runtime, "solar_charging_status",
                         f"Smart solar charging stopped waiting ({reason}); charging normally")
        self._smart_deferring = defer
        return defer

    async def _write_lease(self, *, renewal: bool) -> None:
        try:
            result = await self.runtime.async_execute_solar_lease(
                self._message_id if renewal else None
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._surplus_since = self.clock()
            self._publish("blocked", "solar_lease_command_failed")
            return
        if result.get("successful") is not True:
            message_id = result.get("message_id")
            duration = result.get("duration_seconds")
            if (
                result.get("command_acknowledged") is True
                and not isinstance(message_id, bool)
                and isinstance(message_id, int)
                and not isinstance(duration, bool)
                and isinstance(duration, int)
                and duration > 0
            ):
                # The write may be live even when its keyed readback failed.
                # Track its finite expiry and never retry or renew it blindly.
                self._message_id = message_id
                self._lease_deadline = self.clock() + duration
                self._retiring = True
                self._publish("lease_expiring", "solar_lease_verification_failed")
                return
            self._surplus_since = self.clock()
            self._publish("blocked", str(result.get("lifecycle", "solar_lease_failed")))
            return
        duration = int(result["duration_seconds"])
        message_id = result.get("message_id")
        if isinstance(message_id, bool) or not isinstance(message_id, int):
            self._publish("blocked", "solar_lease_identity_missing")
            return
        self._message_id = message_id
        self._lease_deadline = self.clock() + duration
        self._retiring = False
        self.writes += 1
        if renewal:
            self.renewals += 1
        else:
            log_activity(self.runtime, "solar_charging_status",
                         f"Started solar charging of surplus ({duration // 60} min lease, renewed "
                         "while surplus lasts)",
                         "Who: automatic solar charging. Why: export to the grid stayed above "
                         "the start threshold for the debounce time, so the surplus is stored "
                         "instead of sold.")
        self._publish("active")

    async def _retire_at_soc_limit(self) -> None:
        """Stop our exact lease once; uncertain delivery falls back to expiry."""
        if self._stop_attempted or self._message_id is None:
            return
        self._stop_attempted = True
        try:
            evidence = await self.runtime.async_verify_solar_lease(self._message_id)
            if evidence.get("successful") is not True:
                return
            result = await self.runtime.async_execute_solar_lease(self._message_id, stop=True)
            if result.get("successful") is True:
                self._message_id = None
                self._lease_deadline = 0.0
        except Exception:
            # Never retry a possibly delivered stop or replace an unknown lease.
            return


__all__ = [
    "APS_SERVICE",
    "SCHEDULER_SERVICE",
    "SolarChargingController",
    "build_solar_lease_request",
]
