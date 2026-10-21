"""Persistent two-way peak shaving through finite Pixii APS leases."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from math import isfinite
from time import monotonic
from typing import TYPE_CHECKING, Any

from .activity import log_activity
from .control.models import ControlOperation
from .control.phase_gateway import PhaseGateway
from .control.policy import AutomationPolicy
from .control.production import ProductionAdapterRequest
from .control.settings import OperationalSettings
from .solar_controller import APS_SERVICE, IDLE_SERVICE, SCHEDULER_SERVICE

if TYPE_CHECKING:
    from .runtime import RuntimeData

DEMAND_RESPONSE_SERVICE = 102


def build_aps_lease_request(
    settings: OperationalSettings,
    policy: AutomationPolicy,
    *,
    usable_soc: float,
    charge_capacity_w: float,
    discharge_capacity_w: float,
) -> ProductionAdapterRequest:
    """Build a bounded APS guardrail that yields while it is between limits."""
    export_limit_w = float(settings.get("aps_export_limit_kw")) * 1000
    import_limit_w = float(settings.get("aps_import_limit_kw")) * 1000
    minimum_soc = max(float(settings.get("aps_min_soc_percent")), policy.soc_min_percent)
    maximum_soc = min(float(settings.get("aps_max_soc_percent")), policy.soc_max_percent)
    if not all(
        isfinite(value)
        for value in (
            export_limit_w,
            import_limit_w,
            usable_soc,
            charge_capacity_w,
            discharge_capacity_w,
            minimum_soc,
            maximum_soc,
        )
    ):
        raise ValueError("aps_settings_invalid")
    if export_limit_w > import_limit_w:
        raise ValueError("aps_limits_reversed")
    if minimum_soc >= maximum_soc:
        raise ValueError("aps_soc_limits_invalid")
    lease_seconds = policy.bounded_duration(
        datetime.now(UTC), int(settings.get("aps_lease_seconds")),
    )
    if lease_seconds <= 0:
        raise ValueError("aps_lease_not_authorized")
    charge_max_w = min(
        float(settings.get("aps_max_charge_kw")) * 1000,
        max(0.0, charge_capacity_w),
        policy.max_power_kw * 1000,
    )
    discharge_max_w = min(
        float(settings.get("aps_max_discharge_kw")) * 1000,
        max(0.0, discharge_capacity_w),
        policy.max_power_kw * 1000,
    )
    if usable_soc >= maximum_soc:
        charge_max_w = 0
    if usable_soc <= minimum_soc:
        discharge_max_w = 0
    if charge_max_w <= 0 and discharge_max_w <= 0:
        raise ValueError("aps_battery_capacity_unavailable")
    return ProductionAdapterRequest(
        ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING,
        {
            "export_limit_w": export_limit_w,
            "import_limit_w": import_limit_w,
            "charge_max_w": charge_max_w,
            "discharge_max_w": discharge_max_w,
            # False is deliberate: APS protects the limits, but lower-priority
            # services may run while the meter remains inside the safe band.
            "keep_active": False,
            "max_limit_change_w": float(settings.get("aps_max_limit_change_kw"))
            * 1000,
            "duration_seconds": lease_seconds,
        },
    )


class AdaptivePeakShavingController:
    """Keep a finite native APS guardrail active without owning other services."""

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
        self.current_service: int | None = None
        self.writes = 0
        self.renewals = 0
        self._lease_deadline = 0.0
        self._message_id: int | None = None
        self._applied_payload: tuple[tuple[str, object], ...] | None = None
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def _publish(self, status: str, reason: str | None = None) -> None:
        self.status, self.reason = status, reason
        for listener in tuple(self._listeners):
            listener()

    def _priority(self, service: int) -> int | None:
        try:
            return self.priority_list.index(service) + 1
        except ValueError:
            return None

    def owns_live_lease(self) -> bool:
        """Local identity only; callers must additionally verify keyed readback."""
        return self._message_id is not None and self.clock() < self._lease_deadline

    def attributes(self) -> dict[str, Any]:
        settings = self.runtime.operational_settings
        now = self.clock()
        return {
            "reason": self.reason,
            "current_service": self.current_service,
            "usable_soc_percent": self.runtime.battery.usable_soc,
            "minimum_usable_soc_percent": settings.get("aps_min_soc_percent"),
            "maximum_usable_soc_percent": settings.get("aps_max_soc_percent"),
            "charge_below_grid_power_w": float(settings.get("aps_export_limit_kw")) * 1000,
            "discharge_above_grid_power_w": float(settings.get("aps_import_limit_kw")) * 1000,
            "maximum_charge_w": float(settings.get("aps_max_charge_kw")) * 1000,
            "maximum_discharge_w": float(settings.get("aps_max_discharge_kw")) * 1000,
            "maximum_limit_change_w": float(settings.get("aps_max_limit_change_kw")) * 1000,
            "keep_active": False,
            "priority_verified": self.priority_verified,
            "priority_configured": self.priority_configured,
            "service_priority": list(self.priority_list),
            "scheduler_priority": self._priority(SCHEDULER_SERVICE),
            "demand_response_priority": self._priority(DEMAND_RESPONSE_SERVICE),
            "adaptive_peak_shaving_priority": self._priority(APS_SERVICE),
            "lease_seconds_remaining": max(0, round(self._lease_deadline - now)),
            "owned_message_id": self._message_id,
            "control_writes": self.writes,
            "lease_renewals": self.renewals,
            "lower_priority_services_allowed_between_limits": True,
        }

    @staticmethod
    def priority_with_aps(used_list: object) -> tuple[tuple[int, ...], bool]:
        """Place APS below Scheduler and Demand Response while preserving order."""
        if (
            not isinstance(used_list, list)
            or not used_list
            or len(used_list) > 32
            or len(set(used_list)) != len(used_list)
            or any(type(item) is not int or not 101 <= item <= 129 for item in used_list)
            or SCHEDULER_SERVICE not in used_list
        ):
            raise ValueError("aps_service_priority_unverified")
        configured = list(used_list)
        must_configure = APS_SERVICE not in configured
        if must_configure:
            protected = [configured.index(SCHEDULER_SERVICE)]
            if DEMAND_RESPONSE_SERVICE in configured:
                protected.append(configured.index(DEMAND_RESPONSE_SERVICE))
            configured.insert(max(protected) + 1, APS_SERVICE)
        aps_index = configured.index(APS_SERVICE)
        if configured.index(SCHEDULER_SERVICE) >= aps_index or (
            DEMAND_RESPONSE_SERVICE in configured
            and configured.index(DEMAND_RESPONSE_SERVICE) >= aps_index
        ):
            raise ValueError("aps_service_priority_unverified")
        return tuple(configured), must_configure

    async def prepare(self) -> None:
        """Validate parameters and prove that higher-level services preempt APS."""
        if self.runtime.controller_state.automatic_solar_charging_enabled:
            raise ValueError("aps_conflicts_with_automatic_solar_charging")
        policy = self.runtime._automation_policy()
        if "adaptivepeakshaving" not in policy.allowed_service_classes:
            raise ValueError("automation_policy_not_authorized")
        self._build_request()
        scheduler = self.runtime.gateway_scheduler
        if scheduler is None:
            raise ValueError("aps_gateway_connection_required")
        gateway = PhaseGateway(scheduler.client)
        priority = await gateway.read(100)
        if priority.get("enabled") != 1:
            raise ValueError("aps_service_priority_disabled")
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
                "pixiihome_controller adaptive peak shaving",
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._stopping = True
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
                result = await self.runtime.async_execute_peak_shaving_lease(
                    self._message_id,
                    stop=True,
                )
            except Exception:
                result = None
            if result is not None and result.get("successful") is True:
                self._message_id = None
                self._lease_deadline = 0.0
        self._applied_payload = None
        self._publish("disabled")

    async def _run(self) -> None:
        while True:
            if self._stopping:
                return
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._publish("blocked", "aps_controller_failed")
            await asyncio.sleep(
                int(self.runtime.operational_settings.get("aps_update_interval_seconds"))
            )

    def _build_request(self) -> ProductionAdapterRequest:
        battery = self.runtime.battery
        if (
            battery.usable_soc is None
            or battery.charge_power_limit_w is None
            or battery.discharge_power_limit_w is None
        ):
            raise ValueError("aps_battery_capacity_unavailable")
        return build_aps_lease_request(
            self.runtime.operational_settings,
            self.runtime._automation_policy(),
            usable_soc=battery.usable_soc,
            charge_capacity_w=battery.charge_power_limit_w,
            discharge_capacity_w=battery.discharge_power_limit_w,
        )

    async def tick(self) -> None:
        async with self._lock:
            await self._tick_locked()

    async def _tick_locked(self) -> None:
        now = self.clock()
        if self._message_id is not None and now >= self._lease_deadline:
            self.runtime.release_aps_lease_identity(self._message_id)
            self._message_id = None
            self._applied_payload = None
        if not self.runtime.controller_state.adaptive_peak_shaving_enabled:
            self._publish("disabled")
            return
        if self.runtime.controller_state.automatic_solar_charging_enabled:
            self._publish("blocked", "aps_conflicts_with_automatic_solar_charging")
            return
        if not self.runtime.production_control_enabled():
            self._publish("blocked", "production_control_disabled")
            return
        policy = self.runtime._automation_policy()
        if (
            not policy.enabled
            or "automatic" not in policy.allowed_operation_classes
            or "adaptivepeakshaving" not in policy.allowed_service_classes
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
                              else "aps_service_priority_unverified")
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
        service = self.runtime.telemetry.value("core", "srv_type")
        if type(service) is not int:
            self._publish("waiting_telemetry", "aps_telemetry_incomplete")
            return
        self.current_service = service
        try:
            request = self._build_request()
        except ValueError as error:
            self._publish("blocked", str(error))
            return
        desired_payload = tuple(sorted(request.payload.items()))
        owns_active_aps = service == APS_SERVICE and now < self._lease_deadline
        if service == APS_SERVICE and not owns_active_aps:
            self._publish("waiting_for_priority", "foreign_adaptive_peak_shaving_active")
            return
        if service not in {IDLE_SERVICE, APS_SERVICE}:
            self._publish(
                "waiting_for_priority",
                "scheduler_active" if service == SCHEDULER_SERVICE else "higher_service_active",
            )
            return
        if service == IDLE_SERVICE and now < self._lease_deadline:
            self._publish("starting", "waiting_for_adaptive_peak_shaving_readback")
            return
        if owns_active_aps:
            lease_seconds = int(request.payload["duration_seconds"])
            changed = desired_payload != self._applied_payload
            if changed or self._lease_deadline - now <= lease_seconds / 2:
                if self._message_id is None:
                    self._publish("lease_expiring", "aps_lease_identity_missing")
                    return
                ownership = await self.runtime.async_verify_aps_lease(self._message_id)
                if ownership.get("successful") is not True:
                    self._publish("lease_expiring", "aps_lease_ownership_lost")
                    return
                await self._write_lease(request, renewal=True)
            else:
                self._publish("active")
            return
        await self._write_lease(request, renewal=False)

    async def _write_lease(
        self,
        request: ProductionAdapterRequest,
        *,
        renewal: bool,
    ) -> None:
        try:
            result = await self.runtime.async_execute_peak_shaving_lease(
                self._message_id if renewal else None,
                request=request,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._publish("blocked", "aps_lease_command_failed")
            return
        message_id = result.get("message_id")
        duration = result.get("duration_seconds")
        if result.get("successful") is not True:
            if (
                result.get("command_acknowledged") is True
                and not isinstance(message_id, bool)
                and isinstance(message_id, int)
                and not isinstance(duration, bool)
                and isinstance(duration, int)
                and duration > 0
            ):
                self._message_id = message_id
                self._lease_deadline = self.clock() + duration
                self._publish("lease_expiring", "aps_lease_verification_failed")
                return
            self._publish("blocked", str(result.get("lifecycle", "aps_lease_failed")))
            return
        if (
            isinstance(message_id, bool)
            or not isinstance(message_id, int)
            or isinstance(duration, bool)
            or not isinstance(duration, int)
            or duration <= 0
        ):
            self._publish("blocked", "aps_lease_identity_missing")
            return
        self._message_id = message_id
        self._lease_deadline = self.clock() + duration
        previous = dict(self._applied_payload or ())
        self._applied_payload = tuple(sorted(request.payload.items()))
        limits = {k: v for k, v in request.payload.items() if k != "duration_seconds"}
        if not renewal:
            log_activity(self.runtime, "adaptive_peak_shaving_status",
                         "Started adaptive peak shaving: " + _limits_text(limits))
        elif {k: v for k, v in previous.items() if k != "duration_seconds"} != limits:
            log_activity(self.runtime, "adaptive_peak_shaving_status",
                         "Changed adaptive peak shaving limits: " + _limits_text(limits))
        self.writes += 1
        if renewal:
            self.renewals += 1
        self._publish("active")


def _limits_text(limits: dict[str, Any]) -> str:
    parts = []
    for key, value in sorted(limits.items()):
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        label = key.removesuffix("_w").replace("_", " ")
        parts.append(f"{label} {value / 1000:.1f} kW" if key.endswith("_w") else f"{label} {value}")
    return ", ".join(parts) or "no limits"


__all__ = ["AdaptivePeakShavingController", "build_aps_lease_request"]
