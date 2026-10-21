"""Automatic price optimization through owned, future Pixii schedules."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import ceil, isfinite
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.event import async_track_state_change_event

from .activity import log_activity
from .activity import span as time_span
from .control.price_optimization import (
    CONF_PRICE_OPTIMIZATION,
    ExternalWindow,
    PriceDirection,
    PriceOptimizationConfig,
    PricePlan,
    PricePlanAction,
    PriceSeries,
    build_price_plan,
    normalize_price_series,
)
from .price_guard import PriceSocGuard
from .protocol.scheduler_readback import parse_scheduler_starttime
from .scheduler.models import Ownership, ScheduleCompleteness, ScheduleConsistency
from .scheduler.mutations import (
    SchedulerCreateRequest,
    SchedulerTransactionStatus,
)
from .scheduler.ownership import OwnershipLifecycle, SchedulerOwnershipRecord

if TYPE_CHECKING:
    from .runtime import RuntimeData


PRICE_OWNER = "automatic_price_optimization"
_SERVICE_TYPE = {
    "demandresponse": 102,
    "peakshaving": 104,
    "adaptivepeakshaving": 105,
    "targetsoc": 107,
}


class PriceOptimizationController:
    """Reconcile one deterministic price plan with integration-owned schedules."""

    def __init__(self, runtime: RuntimeData) -> None:
        self.runtime = runtime
        self.soc_guard = PriceSocGuard(runtime)
        self.status = "disabled"
        self.reason: str | None = None
        self.plan: PricePlan | None = None
        self.last_planned_at: datetime | None = None
        self.last_applied_at: datetime | None = None
        self.price_source_format: str | None = None
        self.price_interval_count = 0
        self.next_day_prices_available: bool | None = None
        self.current_price_sek_per_kwh: float | None = None
        self.best_planned_spread_sek_per_kwh: float | None = None
        self.price_last_updated: datetime | None = None
        self.owned_schedule_count = 0
        self.external_conflict_count = 0
        self.create_count = 0
        self.delete_count = 0
        self.external_move_count = 0
        self.current_service: int | None = None
        self._source_fingerprint: str | None = None
        self._external_windows: tuple[ExternalWindow, ...] = ()
        self._target_cache: dict[tuple[int, int, int, int], float] = {}
        # Foreign Target SOC schedules aligned with ``_external_windows``.
        self._external_meta: tuple[tuple[Any, float | None], ...] = ()
        # Original references of foreign targets this controller lowered.
        self._lowered: dict[tuple[int, int, int, int], float] = {}
        self.external_target_adjust_count = 0
        self.external_target_delete_count = 0
        self.busy_until: datetime | None = None
        self._planned_settings: dict[str, Any] | None = None
        self._planned_policy: dict[str, Any] | None = None
        self._external_move_times: deque[datetime] = deque()
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribers: list[Callable[[], None]] = []

    @property
    def config(self) -> PriceOptimizationConfig:
        entry = self.runtime.entry
        value = None if entry is None else entry.options.get(CONF_PRICE_OPTIMIZATION)
        return PriceOptimizationConfig.from_mapping(value)

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

    def attributes(self) -> dict[str, Any]:
        from .price_timeline import price_timeline

        config = self.config
        settings = self.runtime.operational_settings
        plan = self.plan
        return {
            **self.soc_guard.attributes(),
            "reason": self.reason,
            "price_entity_id": config.price_entity_id,
            "managed_external_source_id": config.managed_source_id,
            "planning_horizon_hours": config.planning_horizon_hours,
            "refresh_minutes": config.refresh_minutes,
            "export_allowed": settings.get("price_export_allowed"),
            "schedule_writes_enabled": settings.get("price_schedule_writes_enabled"),
            "minimum_difference_sek_per_kwh": settings.get(
                "price_min_difference_sek_per_kwh"
            ),
            "minimum_net_profit_sek_per_kwh": settings.get("price_min_difference_sek_per_kwh"),
            "export_minimum_net_profit_sek_per_kwh": settings.get(
                "price_export_min_difference_sek_per_kwh"
            ),
            "profit_basis": "per_delivered_ac_kwh_after_grid_losses_and_cycle_cost",
            "initial_energy_cost_basis": "latest_price_estimate_not_measured_acquisition_cost",
            "export_minimum_difference_sek_per_kwh": settings.get(
                "price_export_min_difference_sek_per_kwh"
            ),
            "grid_import_cost_sek_per_kwh": settings.get(
                "price_grid_import_cost_sek_per_kwh"
            ),
            "price_source_format": self.price_source_format,
            "price_interval_count": self.price_interval_count,
            "next_day_prices_available": self.next_day_prices_available,
            "current_price_sek_per_kwh": self.current_price_sek_per_kwh,
            "best_planned_spread_sek_per_kwh": self.best_planned_spread_sek_per_kwh,
            "price_last_updated": (
                self.price_last_updated.isoformat()
                if self.price_last_updated is not None
                else None
            ),
            "planned_action_count": len(plan.actions) if plan is not None else 0,
            "plan_blockers": list(plan.blockers) if plan is not None else [],
            "plan_fingerprint": plan.fingerprint if plan is not None else None,
            "expected_margin_sek": plan.expected_margin_sek if plan is not None else None,
            "minimum_projected_usable_soc": (
                plan.minimum_projected_soc if plan is not None else None
            ),
            "maximum_projected_usable_soc": (
                plan.maximum_projected_soc if plan is not None else None
            ),
            "owned_schedule_count": self.owned_schedule_count,
            "external_conflict_count": self.external_conflict_count,
            "created_schedule_count": self.create_count,
            "deleted_schedule_count": self.delete_count,
            "managed_external_move_count": self.external_move_count,
            "current_service": self.current_service,
            "adjusts_external_target_soc": bool(
                settings.get("price_adjust_external_target_soc")
            ),
            "external_target_adjust_count": self.external_target_adjust_count,
            "external_target_delete_count": self.external_target_delete_count,
            "lowered_external_targets": len(self._lowered),
            "busy_until": self.busy_until.isoformat() if self.busy_until else None,
            "external_windows": [
                {"start": w.start.isoformat(), "end": w.end.isoformat(),
                 "target_usable_soc_percent": w.target_usable_soc_percent}
                for w in self._external_windows
            ],
            "last_planned_at": (
                self.last_planned_at.isoformat() if self.last_planned_at is not None else None
            ),
            "last_applied_at": (
                self.last_applied_at.isoformat() if self.last_applied_at is not None else None
            ),
            "actions": [action.as_dict() for action in plan.actions] if plan else [],
            "schedule_timeline": price_timeline(self, datetime.now(UTC)),
            "next_action": next(iter(price_timeline(self, datetime.now(UTC))), None),
        }

    async def prepare(self) -> None:
        """Validate every required binding before enabling live reconciliation."""
        config = self.config
        if config.price_entity_id is None:
            raise ValueError("price_entity_required")
        price_state = self.runtime.hass.states.get(config.price_entity_id)
        if price_state is None or price_state.state in {"unknown", "unavailable"}:
            raise ValueError("price_entity_unavailable")
        series = normalize_price_series(
            price_state.attributes,
            price_state.attributes.get("unit_of_measurement"),
        )
        self.price_source_format = series.source_format
        self.price_interval_count = len(series.intervals)
        self.price_last_updated = price_state.last_updated.astimezone(UTC)
        if self.runtime.scheduler_mutation_manager is None:
            raise ValueError("price_scheduler_mutation_unavailable")
        if self.runtime.scheduler_coordinator is None:
            raise ValueError("price_scheduler_inventory_unavailable")
        if self.runtime.scheduler_ownership is None:
            raise ValueError("price_scheduler_ownership_unavailable")
        if not self.runtime.production_control_enabled():
            raise ValueError("production_control_disabled")
        policy = self.runtime._automation_policy()
        if (
            not policy.enabled
            or "automatic" not in policy.allowed_operation_classes
            or not {"demandresponse", "peakshaving"}.issubset(
                policy.allowed_service_classes
            )
        ):
            raise ValueError("price_automation_policy_not_authorized")
        self._validated_limits()
        self._publish("ready")

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            if self.config.price_entity_id is not None:
                self._unsubscribers.append(async_track_state_change_event(
                    self.runtime.hass, [self.config.price_entity_id],
                    lambda event: self.request_refresh(),
                ))
            if self.runtime.scheduler_coordinator is not None:
                self._unsubscribers.append(
                    self.runtime.scheduler_coordinator.add_listener(self.request_refresh)
                )
            self._task = self.runtime.hass.async_create_background_task(
                self._run(),
                "pixiihome_controller automatic price optimization",
            )

    async def stop(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers.clear()
        if self._task is not None:
            self._stopping = True
            self._wake.set()
            async with self._lock:
                pass
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._publish("disabled")

    async def disable(self) -> None:
        """Stop planning and remove only future price-owned schedules."""
        await self.stop()
        async with self._lock:
            manager = self.runtime.scheduler_mutation_manager
            if manager is None:
                self._publish("blocked", "price_scheduler_mutation_unavailable")
                return
            now = datetime.now(UTC)
            for record in self._price_records():
                start = _record_start(record)
                if start is None or start <= now:
                    continue
                result = await manager.delete(record.m_id)
                if result.status is not SchedulerTransactionStatus.SUCCESS:
                    self._publish("blocked", f"price_delete_{result.reason}")
                    return
                self.delete_count += 1
                log_activity(self.runtime, PRICE_STATUS, f"Removed {_describe(record)} "
                             "(price optimization turned off)")
            self._publish("disabled")

    async def release_for_authority_off(self) -> list[str]:
        """Remove every live price-owned effect while Gateway writes are still allowed.

        Future identities are deleted; an interval already in progress uses the
        verified exact price stop. Failures are reported, never retried here.
        """
        await self.stop()
        problems: list[str] = []
        async with self._lock:
            manager = self.runtime.scheduler_mutation_manager
            if manager is None:
                problems.append("price_scheduler_mutation_unavailable")
            else:
                now = datetime.now(UTC)
                for record in self._price_records():
                    start = _record_start(record)
                    duration = record.intended_state.get("duration")
                    if start is None or type(duration) is not int or duration <= 0:
                        problems.append(f"price_{record.m_id}_interval_unknown")
                        continue
                    if start + timedelta(seconds=duration) <= now:
                        continue
                    if start > now:
                        result = await manager.delete(record.m_id)
                        stopped = result.status is SchedulerTransactionStatus.SUCCESS
                    else:
                        result = await manager.stop_owned_price(record.m_id)
                        stopped = result.status in {
                            SchedulerTransactionStatus.SUCCESS,
                            SchedulerTransactionStatus.OUTPUT_STOPPED,
                        }
                    if stopped:
                        self.delete_count += 1
                        log_activity(
                            self.runtime, PRICE_STATUS,
                            f"{'Removed' if start > now else 'Stopped'} {_describe(record)} "
                            "(control authority turned off)",
                        )
                    else:
                        problems.append(f"price_{record.m_id}_{result.reason}")
            if problems:
                self._publish("blocked", problems[0])
            else:
                self._publish("disabled", "production_control_disabled")
        return problems

    def request_refresh(self) -> None:
        """Wake the planner after a setting or price-entity change."""
        self._wake.set()

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._publish("blocked", "price_controller_failed")
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self.config.refresh_minutes * 60,
                )
            except TimeoutError:
                pass
            self._wake.clear()
            if not self._stopping:
                await asyncio.sleep(5)  # coalesce source/inventory changes

    async def tick(self) -> None:
        """Build and safely reconcile one fresh price plan."""
        async with self._lock:
            await self._tick_locked()

    async def _tick_locked(self) -> None:
        if not self.runtime.controller_state.automatic_price_optimization_enabled:
            self._publish("disabled")
            return
        try:
            await self.prepare()
        except ValueError as error:
            reason = str(error)
            self._publish(
                "waiting_prices"
                if reason
                in {
                    "price_entity_unavailable",
                    "price_series_empty",
                    "future_price_horizon_incomplete",
                }
                else "blocked",
                reason,
            )
            return
        # A stopped-but-unreadable identity stays reserved across restarts.
        # Only GET plus fresh physical idle evidence may end its quarantine;
        # this path never resends DELETE or releases an unidentified ID.
        mutation_manager = self.runtime.scheduler_mutation_manager
        ownership = self.runtime.scheduler_ownership
        assert mutation_manager is not None and ownership is not None
        for reserved in tuple(ownership.records.values()):
            if reserved.lifecycle is OwnershipLifecycle.STOPPED_RESERVED:
                expiry = await mutation_manager.reconcile_expired_price(reserved.m_id)
                if expiry.status is not SchedulerTransactionStatus.SUCCESS:
                    self._publish("waiting_for_priority", "price_stop_quarantine_active")
                    return
        config = self.config
        assert config.price_entity_id is not None
        state = self.runtime.hass.states.get(config.price_entity_id)
        if state is None or state.state in {"unknown", "unavailable"}:
            self._publish("waiting_prices", "price_entity_unavailable")
            return
        if datetime.now(UTC) - state.last_updated.astimezone(UTC) > timedelta(hours=36):
            self._publish("waiting_prices", "price_entity_stale")
            return
        try:
            series = normalize_price_series(
                state.attributes,
                state.attributes.get("unit_of_measurement"),
            )
            self.price_source_format = series.source_format
            self.price_interval_count = len(series.intervals)
            self.price_last_updated = state.last_updated.astimezone(UTC)
            self._source_fingerprint = series.fingerprint
            # Plan with the remaining known prices instead of idling until the
            # next day is published. Publication changes the source, which
            # wakes this loop and replaces future owned schedules that no
            # longer belong to the better plan.
            self.next_day_prices_available = _daily_future_prices_available(
                series, state.attributes
            )
            limits = self._validated_limits()
            self._planned_settings = self.runtime.operational_settings.as_dict()
            self._planned_policy = self.runtime._automation_policy().as_dict()
            battery = self.runtime.battery
            health = self.runtime.plant_health()
            if not (
                health.telemetry_fresh
                and health.system_safe
                and health.battery_safe
                and health.grid_safe
                and health.system_events_clear
            ):
                self._publish("waiting_telemetry", "plant_telemetry_not_safe")
                return
            if (
                battery.usable_soc is None
                or not isfinite(battery.usable_soc)
                or battery.charge_power_limit_w is None
                or battery.discharge_power_limit_w is None
                or not isfinite(battery.charge_power_limit_w)
                or not isfinite(battery.discharge_power_limit_w)
            ):
                raise ValueError("price_usable_soc_unavailable")
            policy = self.runtime._automation_policy()
            plan = self._build_plan(series, limits)
        except ValueError as error:
            self._publish("blocked", str(error))
            return
        self.plan = plan
        self.last_planned_at = datetime.now(UTC)
        current_time = datetime.now(UTC)
        current_interval = next(
            (
                interval
                for interval in series.intervals
                if interval.start <= current_time < interval.end
            ),
            None,
        )
        self.current_price_sek_per_kwh = (
            current_interval.price_sek_per_kwh if current_interval is not None else None
        )
        self.best_planned_spread_sek_per_kwh = max(
            (action.spread_sek_per_kwh for action in plan.actions),
            default=None,
        )
        if not plan.ready:
            self._publish("waiting_prices", plan.blockers[0])
            return
        if any(action.duration_seconds > policy.max_duration_seconds for action in plan.actions):
            self._publish("blocked", "price_action_exceeds_policy_duration")
            return
        service = self.runtime.telemetry.value("core", "srv_type")
        self.current_service = service if type(service) is int else None
        if self.current_service is None:
            self._publish("waiting_for_priority", "running_service_unavailable")
            return
        if not await self._priority_allows_mutation():
            self._publish("waiting_for_priority", "non_idle_service_active")
            return
        coordinator = self.runtime.scheduler_coordinator
        if coordinator is None:
            self._publish("blocked", "price_scheduler_inventory_unavailable")
            return
        snapshot = await coordinator.async_refresh()
        if snapshot is None or snapshot.data_status != "current":
            self._publish("blocked", "price_scheduler_inventory_incomplete")
            return
        manager = self.runtime.scheduler_manager
        if manager is None:
            self._publish("blocked", "price_scheduler_inventory_unavailable")
            return
        schedules = manager.normalized_schedules
        if any(
            _record_has_schedule_shape(record, schedule)
            and not _record_matches_schedule(record, schedule)
            for record in self._price_records()
            for schedule in schedules if schedule.enabled
        ):
            self._publish("blocked", "price_schedule_identity_ambiguous")
            return
        if any(item.enabled and item.recurrence.recurring is not False for item in schedules):
            self._publish("blocked", "price_recurring_schedule_scope_unknown")
            return
        if not bool(
            self.runtime.operational_settings.get("price_schedule_writes_enabled")
        ):
            await self._load_external_windows(plan.actions)
            self.plan = self._build_plan(series, limits, windows=self._external_windows)
            self._publish("preview", "price_schedule_writes_disabled")
            return
        move_count_before = self.external_move_count
        if not await self._move_one_managed_external_charge(series, plan.actions):
            return
        if self.external_move_count > move_count_before:
            await coordinator.async_refresh()
        await self._load_external_windows(plan.actions)
        adjust = bool(
            self.runtime.operational_settings.get("price_adjust_external_target_soc")
        )
        windows = (
            self._with_adjustable_targets(series, limits) if adjust else self._external_windows
        )
        self.plan = self._build_plan(series, limits, windows=windows)
        if not self.plan.ready:
            self._publish("waiting_for_priority", "price_external_schedule_energy_unknown")
            return
        actions = self.plan.actions
        failed_from = await self._apply_external_targets(windows, self.plan, limits)
        if failed_from is not None:
            # The plan relied on that window holding; nothing after it is safe.
            actions = tuple(a for a in actions if a.end <= failed_from)
        if self.busy_until is not None:
            # Written while a known external interval runs: never anything
            # that could start before that interval has ended.
            actions = tuple(a for a in actions if a.start >= self.busy_until)
        # Only apply the first cycle. Self-consumption, background controllers
        # and support interruptions can change the real energy before another
        # cycle. Later cycles remain proposals until fresh SoC is measured.
        first_discharge = next(
            (a for a in actions if a.direction is PriceDirection.DISCHARGE), None,
        )
        if first_discharge is not None:
            actions = tuple(a for a in actions if a.end <= first_discharge.end)
        if not await self._reconcile(actions):
            return
        self.last_applied_at = datetime.now(UTC)
        self._publish("active" if actions else "ready")

    async def _priority_allows_mutation(self) -> bool:
        """Allow idle, our verified background APS, or writes after a known external end.

        A running external service is only tolerated when the scheduler
        inventory shows exactly which enabled schedule it is and when it ends;
        then only schedules starting after that end may be written.
        """
        service = self.runtime.telemetry.value("core", "srv_type")
        self.current_service = service if type(service) is int else None
        self.busy_until = None
        if self.current_service == 0:
            return True
        if self.current_service != 105:
            self.busy_until = self._known_external_end(self.current_service)
            return self.busy_until is not None
        for name in ("solar_charging_controller", "adaptive_peak_shaving_controller"):
            controller = getattr(self.runtime, name, None)
            if controller is not None and controller.owns_live_lease():
                message_id = controller._message_id
                if message_id is not None:
                    result = await self.runtime.async_verify_aps_lease(message_id)
                    return result.get("successful") is True
        return False

    async def _mutation_context_safe(self) -> bool:
        """Recheck source, mode and authority before every individual mutation."""
        entity_id = self.config.price_entity_id
        state = self.runtime.hass.states.get(entity_id) if entity_id else None
        health = self.runtime.plant_health()
        if not (
            self.runtime.controller_state.automatic_price_optimization_enabled
            and self.runtime.production_control_enabled()
            and bool(self.runtime.operational_settings.get("price_schedule_writes_enabled"))
            and health.telemetry_fresh and health.system_safe and health.battery_safe
            and health.grid_safe and health.system_events_clear
            and state is not None and state.state not in {"unknown", "unavailable"}
            and self.runtime.operational_settings.as_dict() == self._planned_settings
            and self.runtime._automation_policy().as_dict() == self._planned_policy
        ):
            self._publish("blocked", "price_mutation_context_changed")
            return False
        try:
            series = normalize_price_series(
                state.attributes, state.attributes.get("unit_of_measurement")
            )
        except ValueError:
            self._publish("waiting_prices", "price_mutation_context_changed")
            return False
        if series.fingerprint != self._source_fingerprint:
            self._publish("waiting_prices", "price_mutation_context_changed")
            return False
        if not await self._priority_allows_mutation():
            self._publish("waiting_for_priority", "non_idle_service_active")
            return False
        return True

    def _build_plan(
        self,
        series: PriceSeries,
        limits: dict[str, float],
        end_before: datetime | None = None,
        *,
        windows: tuple[ExternalWindow, ...] = (),
    ) -> PricePlan:
        config = self.config
        battery = self.runtime.battery
        policy = self.runtime._automation_policy()
        assert battery.usable_soc is not None
        assert battery.charge_power_limit_w is not None
        assert battery.discharge_power_limit_w is not None
        # Unknown future energy or a closed authorization window ends this
        # simulation. Do not fund an earlier charge with an unavailable peak.
        for interval in series.intervals:
            if interval.start <= datetime.now(UTC):
                continue
            duration = int((interval.end - interval.start).total_seconds())
            if policy.bounded_duration(interval.start, duration) < duration:
                end_before = min(end_before, interval.start) if end_before else interval.start
                break
        return build_price_plan(
            series,
            now=datetime.now(UTC),
            horizon_hours=config.planning_horizon_hours,
            minimum_lead_minutes=config.minimum_lead_minutes,
            usable_soc_percent=battery.usable_soc,
            usable_capacity_kwh=limits["capacity"],
            minimum_soc_percent=max(limits["min_soc"], policy.soc_min_percent),
            maximum_soc_percent=min(limits["max_soc"], policy.soc_max_percent),
            maximum_charge_kw=min(
                limits["charge_kw"],
                policy.max_power_kw,
                max(0.0, battery.charge_power_limit_w / 1000),
            ),
            maximum_discharge_kw=min(
                limits["discharge_kw"],
                policy.max_power_kw,
                max(0.0, battery.discharge_power_limit_w / 1000),
            ),
            minimum_difference_sek_per_kwh=limits["difference"],
            export_allowed=bool(
                self.runtime.operational_settings.get("price_export_allowed")
            ),
            export_minimum_difference_sek_per_kwh=limits["export_difference"],
            round_trip_efficiency_percent=limits["efficiency"],
            cycle_cost_sek_per_kwh=limits["cycle_cost"],
            grid_import_cost_sek_per_kwh=limits["grid_import_cost"],
            end_before=end_before,
            external_windows=windows,
        )

    def _validated_limits(self) -> dict[str, float]:
        settings = self.runtime.operational_settings
        result = {
            "difference": float(settings.get("price_min_difference_sek_per_kwh")),
            "export_difference": float(
                settings.get("price_export_min_difference_sek_per_kwh")
            ),
            "charge_kw": float(settings.get("price_max_charge_kw")),
            "discharge_kw": float(settings.get("price_max_discharge_kw")),
            "min_soc": float(settings.get("price_min_soc_percent")),
            "max_soc": float(settings.get("price_max_soc_percent")),
            "capacity": float(settings.get("price_usable_capacity_kwh")),
            "efficiency": float(settings.get("price_round_trip_efficiency_percent")),
            "cycle_cost": float(settings.get("price_cycle_cost_sek_per_kwh")),
            "grid_import_cost": float(
                settings.get("price_grid_import_cost_sek_per_kwh")
            ),
        }
        if result["min_soc"] >= result["max_soc"]:
            raise ValueError("price_soc_limits_invalid")
        if result["capacity"] <= 0:
            raise ValueError("price_usable_capacity_required")
        return result

    async def _move_one_managed_external_charge(
        self,
        series: PriceSeries,
        actions: tuple[PricePlanAction, ...],
    ) -> bool:
        """Move at most one proven external charge into a cheaper planned interval."""
        config = self.config
        source_id = config.managed_source_id
        gateway = self.runtime.gateway_scheduler
        manager = self.runtime.scheduler_manager
        battery = self.runtime.battery
        if (
            source_id is None
            or gateway is None
            or manager is None
            or battery.raw_soc is None
            or battery.effective_min_soc is None
            or battery.effective_max_soc is None
            or battery.effective_max_soc <= battery.effective_min_soc
        ):
            return True
        now = datetime.now(UTC)
        cutoff = now - timedelta(hours=24)
        while self._external_move_times and self._external_move_times[0] < cutoff:
            self._external_move_times.popleft()
        if len(self._external_move_times) >= 4:
            self._publish("blocked", "price_aggregator_conflict_limit")
            return False
        charge_actions = sorted(
            (
                action
                for action in actions
                if action.direction is PriceDirection.CHARGE
                and action.start > now + timedelta(minutes=config.minimum_lead_minutes)
            ),
            key=lambda action: (action.price_sek_per_kwh, action.start),
        )
        if not charge_actions:
            return True
        schedules = sorted(manager.normalized_schedules, key=lambda item: item.start_epoch)
        source_targets = [
            schedule
            for schedule in schedules
            if schedule.source_id == source_id
            and schedule.service_type == 107
            and schedule.enabled
            and schedule.start_epoch > int(now.timestamp())
        ]
        if any(
            schedule.recurrence.recurring is not False
            or schedule.completeness is not ScheduleCompleteness.COMPLETE
            or schedule.consistency is not ScheduleConsistency.CONSISTENT
            for schedule in source_targets
        ):
            return True
        baseline_soc = float(battery.raw_soc)
        # Only the next source target can be compared with measured current SoC.
        # A future lower target is not evidence that it will actually discharge
        # (support, solar and limits may intervene). Re-evaluate later targets
        # after the preceding event has finished and new SoC is available.
        for schedule in source_targets[:1]:
            try:
                target = await gateway.client.read_target_soc(schedule.service_no)
                reference = float(target["reference"])
            except Exception:
                self._publish("blocked", "price_managed_target_soc_read_failed")
                return False
            is_charge = reference > baseline_soc + 0.5
            if not is_charge:
                continue
            target_usable_soc = min(
                100.0,
                max(
                    0.0,
                    (reference - battery.effective_min_soc)
                    * 100
                    / (battery.effective_max_soc - battery.effective_min_soc),
                ),
            )
            original_price = _price_at_epoch(series, schedule.start_epoch)
            if original_price is None:
                continue
            destination = next(
                (
                    action
                    for action in charge_actions
                    if action.duration_seconds >= schedule.duration_seconds
                    and target_usable_soc
                    >= action.projected_usable_soc_percent - 0.5
                    and not any(
                        other.sch_index != schedule.sch_index
                        and other.enabled
                        and other.end_epoch > int(now.timestamp())
                        and other.start_epoch
                        < int(action.start.timestamp()) + schedule.duration_seconds
                        for other in schedules
                    )
                    # Moving the same charge does not add a cycle. Express its
                    # purchase saving per delivered kWh, like the net-profit UI.
                    and (original_price - action.price_sek_per_kwh)
                    / (float(self.runtime.operational_settings.get(
                        "price_round_trip_efficiency_percent"
                    )) / 100)
                    >= float(
                        self.runtime.operational_settings.get(
                            "price_min_difference_sek_per_kwh"
                        )
                    )
                    and int(action.start.timestamp()) != schedule.start_epoch
                ),
                None,
            )
            if destination is None:
                continue
            if not await self._mutation_context_safe():
                return False
            try:
                result = await gateway.move_managed_target_soc(
                    sch_index=schedule.sch_index,
                    expected_source_id=source_id,
                    expected_start_epoch=schedule.start_epoch,
                    expected_duration_seconds=schedule.duration_seconds,
                    expected_reference=reference,
                    new_start_epoch=int(destination.start.timestamp()),
                )
            except Exception:
                self._publish("blocked", "price_managed_schedule_move_failed")
                return False
            if result.get("status") != "reconciled":
                self._publish("blocked", "price_managed_schedule_move_recovery_required")
                return False
            self._external_move_times.append(now)
            self.external_move_count += 1
            return True
        return True

    def _price_records(self) -> list[SchedulerOwnershipRecord]:
        ownership = self.runtime.scheduler_ownership
        if ownership is None:
            return []
        records = [
            record
            for record in ownership.records.values()
            if record.owner == PRICE_OWNER and record.lifecycle is OwnershipLifecycle.OWNED
        ]
        self.owned_schedule_count = len(records)
        return records

    def _known_external_end(self, service: int | None) -> datetime | None:
        """End of the single enabled foreign schedule explaining ``service`` now."""
        manager = self.runtime.scheduler_manager
        if manager is None or service is None:
            return None
        owned = self._price_records()
        now = int(datetime.now(UTC).timestamp())
        running = [
            schedule for schedule in manager.normalized_schedules
            if schedule.enabled
            and schedule.service_type == service
            and schedule.start_epoch <= now < schedule.end_epoch
            and schedule.recurrence.recurring is False
            and not any(_record_matches_schedule(record, schedule) for record in owned)
        ]
        if len(running) != 1:
            return None
        return datetime.fromtimestamp(running[0].end_epoch, UTC)

    async def _load_external_windows(self, actions: tuple[PricePlanAction, ...]) -> None:
        """Describe every protected foreign schedule for the planner.

        A Target SOC window carries its expected resulting usable SOC, read from
        the Gateway; any other service, or an unreadable target, has unknown
        resulting energy and ends planning at its start.
        """
        manager = self.runtime.scheduler_manager
        owned = self._price_records()
        schedules = () if manager is None else tuple(manager.normalized_schedules)
        now = int(datetime.now(UTC).timestamp())
        battery = self.runtime.battery
        gateway = self.runtime.gateway_scheduler
        span = (
            battery.effective_max_soc - battery.effective_min_soc
            if battery.effective_min_soc is not None and battery.effective_max_soc is not None
            else None
        )
        windows: list[ExternalWindow] = []
        meta: list[tuple[Any, float | None] | None] = []
        keys: set[tuple[int, int, int, int]] = set()
        for schedule in sorted(schedules, key=lambda item: item.start_epoch):
            if (
                not schedule.enabled
                or schedule.end_epoch <= now
                or any(_record_matches_schedule(record, schedule) for record in owned)
            ):
                continue
            target: float | None = None
            reference: float | None = None
            if (
                schedule.service_type == _SERVICE_TYPE["targetsoc"]
                and gateway is not None
                and span is not None
                and span > 0
                and battery.effective_min_soc is not None
            ):
                key = (schedule.sch_index, schedule.service_no,
                       schedule.start_epoch, schedule.duration_seconds)
                keys.add(key)
                reference = self._target_cache.get(key)
                if reference is None:
                    try:
                        value = float(
                            (await gateway.client.read_target_soc(schedule.service_no))[
                                "reference"
                            ]
                        )
                    except Exception:  # noqa: BLE001 - unreadable means unknown energy
                        value = float("nan")
                    if isfinite(value):
                        reference = self._target_cache[key] = value
                if reference is not None:
                    target = min(
                        100.0,
                        max(0.0, (reference - battery.effective_min_soc) * 100 / span),
                    )
            windows.append(ExternalWindow(
                datetime.fromtimestamp(schedule.start_epoch, UTC),
                datetime.fromtimestamp(schedule.end_epoch, UTC),
                target,
            ))
            meta.append(
                (schedule, reference)
                if schedule.service_type == _SERVICE_TYPE["targetsoc"]
                and schedule.recurrence.recurring is False
                else None
            )
        for stale in set(self._target_cache) - keys:
            del self._target_cache[stale]
        self._external_windows = tuple(windows)
        self._external_meta = tuple(meta)  # type: ignore[assignment]
        unknown_from = min((window.start for window in windows if not window.known), default=None)
        self.external_conflict_count = sum(
            1 for action in actions
            if (unknown_from is not None and action.end > unknown_from)
            or any(window.start < action.end and action.start < window.end for window in windows)
        )

    def _with_adjustable_targets(
        self, series: PriceSeries, limits: dict[str, float]
    ) -> tuple[ExternalWindow, ...]:
        """Mark foreign Target SOC windows between own discharge and recharge.

        A first plan treats every future, readable Target SOC window as one the
        controller may hold. Only windows from the first planned discharge up to
        the next planned charge (or the last discharge) stay adjustable; the
        rest keep refilling the battery as the aggregator intends.
        """
        lead = datetime.now(UTC) + timedelta(minutes=self.config.minimum_lead_minutes)
        eligible = [
            meta is not None and meta[1] is not None and window.start > lead
            for window, meta in zip(self._external_windows, self._external_meta, strict=True)
        ]
        trial = tuple(
            replace(window, adjustable=flag)
            for window, flag in zip(self._external_windows, eligible, strict=True)
        )
        if not any(eligible):
            return self._external_windows
        first = self._build_plan(series, limits, windows=trial)
        discharges = [a for a in first.actions if a.direction is PriceDirection.DISCHARGE]
        if not first.ready or not discharges:
            return self._external_windows
        recharge = next(
            (a for a in first.actions
             if a.direction is PriceDirection.CHARGE and a.start >= discharges[0].end),
            None,
        )
        until = recharge.start if recharge is not None else max(a.end for a in discharges)
        return tuple(
            replace(window, adjustable=flag and discharges[0].start <= window.start < until)
            for window, flag in zip(self._external_windows, eligible, strict=True)
        )

    async def _apply_external_targets(
        self,
        windows: tuple[ExternalWindow, ...],
        plan: PricePlan,
        limits: dict[str, float],
    ) -> datetime | None:
        """Lower held foreign targets to the plan; restore those no longer held.

        Returns the start of the first window that could not be held, if any.
        """
        gateway = self.runtime.gateway_scheduler
        battery = self.runtime.battery
        setter: Any = getattr(gateway, "set_external_target_soc", None)
        held = {window.start for window in windows if window.adjustable}
        if (
            not callable(setter)
            or battery.effective_min_soc is None
            or battery.effective_max_soc is None
            or battery.effective_max_soc <= battery.effective_min_soc
        ):
            return min(held, default=None)
        span = battery.effective_max_soc - battery.effective_min_soc
        planned = dict(plan.window_socs)
        policy = self.runtime._automation_policy()
        floor = max(limits["min_soc"], policy.soc_min_percent)
        writes = 0
        failed_from: datetime | None = None
        lead = datetime.now(UTC) + timedelta(minutes=self.config.minimum_lead_minutes)
        for window, meta in zip(windows, self._external_meta, strict=True):
            if meta is None or window.start <= lead:
                continue
            schedule, reference = meta[0], meta[1]
            if reference is None:
                continue
            key = (schedule.sch_index, schedule.service_no,
                   schedule.start_epoch, schedule.duration_seconds)
            if window.adjustable:
                usable = max(floor, planned.get(window.start, floor))
                desired = float(min(100, ceil(battery.effective_min_soc + usable * span / 100)))
                if desired >= reference - 0.5:
                    continue
                allow_delete = True
            elif key in self._lowered:
                desired, allow_delete = self._lowered[key], False
                if desired <= reference + 0.5:
                    self._lowered.pop(key, None)
                    continue
            else:
                continue
            # One foreign write per planning pass keeps every change reviewable.
            if writes >= 1 or not await self._mutation_context_safe():
                # Stop here; a held window that was not adjusted ends the plan.
                return (failed_from or window.start) if window.adjustable else failed_from
            writes += 1
            try:
                apply_target: Any = setter
                result = await apply_target(
                    sch_index=schedule.sch_index,
                    expected_service_no=schedule.service_no,
                    expected_start_epoch=schedule.start_epoch,
                    expected_duration_seconds=schedule.duration_seconds,
                    expected_reference=reference,
                    new_reference=desired,
                    allow_delete=allow_delete,
                )
            except Exception:  # noqa: BLE001 - refused before send; plan around it
                result = {"status": "refused"}
            status = result.get("status")
            self._target_cache.pop(key, None)
            when = time_span(window.start, window.end)
            if status == "adjusted":
                if window.adjustable:
                    self._lowered.setdefault(key, reference)
                    self.external_target_adjust_count += 1
                    log_activity(self.runtime, PRICE_STATUS,
                                 f"Lowered aggregator Target SOC {when} from {reference:.0f} % "
                                 f"to {desired:.0f} % to keep the planned discharge")
                else:
                    self._lowered.pop(key, None)
                    log_activity(self.runtime, PRICE_STATUS,
                                 f"Restored aggregator Target SOC {when} to {desired:.0f} %")
            elif status == "deleted":
                self._lowered.pop(key, None)
                self.external_target_delete_count += 1
                log_activity(self.runtime, PRICE_STATUS,
                             f"Deleted aggregator Target SOC {when} ({reference:.0f} %); "
                             "the Gateway kept the old target")
            elif window.adjustable:
                log_activity(self.runtime, PRICE_STATUS,
                             f"Could not lower aggregator Target SOC {when} ({status}); "
                             "nothing after it was scheduled")
            if status not in {"adjusted", "deleted"} and window.adjustable:
                failed_from = window.start if failed_from is None else failed_from
                if status == "recovery_required":
                    self._publish("blocked", "price_external_target_recovery_required")
                    return failed_from
        return failed_from

    async def _reconcile(self, actions: tuple[PricePlanAction, ...]) -> bool:
        manager = self.runtime.scheduler_mutation_manager
        if manager is None:
            self._publish("blocked", "price_scheduler_mutation_unavailable")
            return False
        desired = {_action_key(action): action for action in actions}
        existing = {_record_key(record): record for record in self._price_records()}
        now = datetime.now(UTC)
        for key, record in tuple(existing.items()):
            if key in desired:
                continue
            start = _record_start(record)
            if start is None or start <= now:
                continue
            if not await self._mutation_context_safe():
                return False
            result = await manager.delete(record.m_id)
            if result.status is not SchedulerTransactionStatus.SUCCESS:
                self._publish("blocked", f"price_delete_{result.reason}")
                return False
            self.delete_count += 1
            log_activity(self.runtime, PRICE_STATUS,
                         f"Removed {_describe(record)} (no longer in the plan)")
        for key, action in desired.items():
            if key in existing:
                continue
            lead_limit = datetime.now(UTC) + timedelta(minutes=self.config.minimum_lead_minutes)
            if action.start <= lead_limit:
                self._publish("blocked", "price_action_lead_time_elapsed")
                return False
            if not await self._mutation_context_safe():
                return False
            result = await manager.create(
                SchedulerCreateRequest(
                    starttime=action.start,
                    duration_seconds=action.duration_seconds,
                    values=action.values,
                    service=action.service,
                    owner=PRICE_OWNER,
                )
            )
            if result.status is not SchedulerTransactionStatus.SUCCESS:
                self._publish("blocked", f"price_create_{result.reason}")
                return False
            self.create_count += 1
            log_activity(
                self.runtime, PRICE_STATUS,
                f"Scheduled {action.direction.value} {time_span(action.start, action.end)} "
                f"at {action.power_kw:.1f} kW, price {action.price_sek_per_kwh:.2f} kr/kWh, "
                f"expected net profit {action.net_profit_sek_per_kwh:.2f} kr/kWh",
            )
        return True


def _action_key(action: PricePlanAction) -> tuple[object, ...]:
    return (
        action.service,
        action.start.astimezone(UTC).replace(microsecond=0),
        action.duration_seconds,
        _freeze(action.values),
    )


def _record_key(record: SchedulerOwnershipRecord) -> tuple[object, ...]:
    state = record.intended_state
    start = _record_start(record)
    return (
        record.service,
        start,
        state.get("duration"),
        _freeze(state.get("values", {})),
    )


PRICE_STATUS = "price_optimization_status"


def _describe(record: SchedulerOwnershipRecord) -> str:
    """``price charge 14:00–15:00`` for an owned price record."""
    start = _record_start(record)
    duration = record.intended_state.get("duration")
    values = record.intended_state.get("values", {})
    direction = "discharge" if (
        record.service == "peakshaving"
        or (isinstance(values, Mapping) and float(values.get("pacref", 0) or 0) < 0)
    ) else "charge"
    if start is None or type(duration) is not int:
        return f"price {direction}"
    return f"price {direction} {time_span(start, start + timedelta(seconds=duration))}"


def _record_start(record: SchedulerOwnershipRecord) -> datetime | None:
    value = record.intended_state.get("starttime")
    if not isinstance(value, str):
        return None
    try:
        return parse_scheduler_starttime(value)
    except Exception:
        return None


def _record_matches_schedule(record: SchedulerOwnershipRecord, schedule: Any) -> bool:
    return (
        # A matching time/service shape cannot prove ownership: a different
        # actor may have posted an identical interval with different values.
        # service_no and MQTT m_id belong to different identity namespaces.
        # Numeric equality is insufficient without independently proven ownership.
        schedule.ownership.ownership is Ownership.INTEGRATION_OWNED
        and schedule.service_no == record.m_id
        and _record_has_schedule_shape(record, schedule)
        and schedule.recurrence.recurring is False
        and schedule.completeness is ScheduleCompleteness.COMPLETE
        and schedule.consistency is ScheduleConsistency.CONSISTENT
    )


def _record_has_schedule_shape(record: SchedulerOwnershipRecord, schedule: Any) -> bool:
    start = _record_start(record)
    return (
        start is not None and int(start.timestamp()) == schedule.start_epoch
        and record.intended_state.get("duration") == schedule.duration_seconds
        and _SERVICE_TYPE.get(record.service) == schedule.service_type
    )


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple(sorted((str(key), _freeze(item)) for key, item in value.items()))
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _price_at_epoch(series: PriceSeries, epoch: int) -> float | None:
    instant = datetime.fromtimestamp(epoch, UTC)
    for interval in series.intervals:
        if interval.start <= instant < interval.end:
            return interval.price_sek_per_kwh
    return None


def _daily_future_prices_available(
    series: PriceSeries,
    attributes: Mapping[str, Any],
) -> bool:
    if series.source_format == "greenely_day_arrays":
        return bool(attributes.get("next_day"))
    if series.source_format == "timestamped_day_arrays":
        return bool(attributes.get("raw_tomorrow"))
    return True


__all__ = ["PRICE_OWNER", "PriceOptimizationController"]
