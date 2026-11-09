"""Automatic price optimization through owned, future Pixii schedules."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from math import ceil, isfinite, sqrt
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.event import async_track_state_change_event

from .activity import clock, log_activity
from .activity import span as time_span
from .aps_priority import retire_recharged_context
from .control.price_optimization import (
    CONF_PRICE_OPTIMIZATION,
    ExternalWindow,
    PriceDirection,
    PriceOptimizationConfig,
    PricePlan,
    PricePlanAction,
    PriceSeries,
    build_price_plan,
    merge_adjacent_actions,
    normalize_price_series,
)
from .control.whole_day_planner import SolarCharging, WholeDayResult, build_whole_day_plan
from .price_guard import PriceSocGuard
from .protocol.enums import service_type_label
from .protocol.scheduler_readback import parse_scheduler_starttime
from .reasons import reason_text
from .scheduler.models import Ownership, ScheduleCompleteness, ScheduleConsistency
from .scheduler.mutations import (
    SchedulerCreateRequest,
    SchedulerTransactionStatus,
)
from .scheduler.ownership import OwnershipLifecycle, SchedulerOwnershipRecord

if TYPE_CHECKING:
    from .runtime import RuntimeData

_LOGGER = logging.getLogger(__name__)


PRICE_OWNER = "automatic_price_optimization"
_SERVICE_TYPE = {
    "demandresponse": 102,
    "peakshaving": 104,
    "adaptivepeakshaving": 105,
    "targetsoc": 107,
}
# Time a pass needs after one of our schedules ends before the next write is
# sent, on top of the lead time; a later action closer than this is chained.
_CHAIN_MARGIN_MINUTES = 5
# Wait before the read-only check of a removal the Gateway did not confirm.
_POST_CHECK_DELAY_SECONDS = 20
# Aggregator windows are replaced for a cycle whose first discharge starts
# within this many hours; until then they are only lowered.
_REPLACE_AHEAD_HOURS = 4
# Smallest change of a foreign Target SOC worth the pass's one external write
# (3 points of SOC is about 0.7 kWh with 18.4 kWh usable between 10 and 90 %).
_TARGET_DEADBAND_PERCENT = 3.0


class PriceOptimizationController:
    """Reconcile one deterministic price plan with integration-owned schedules."""

    def __init__(self, runtime: RuntimeData) -> None:
        self.runtime = runtime
        self.soc_guard = PriceSocGuard(runtime)
        self.delete_recovery_reason: str | None = None
        self._conflicts_seen: dict[int, datetime] = {}
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
        # (sch_index, start_epoch) of aggregator schedules this controller deleted.
        self.deleted_external: set[tuple[int, int]] = set()
        self.create_count = 0
        self.delete_count = 0
        # Plan figures of the schedules this controller created, by schedule
        # key, so their expected profit stays visible if the plan moves on.
        self.created_actions: dict[tuple[object, ...], PricePlanAction] = {}
        self.external_move_count = 0
        self.current_service: int | None = None
        self._source_fingerprint: str | None = None
        self._external_windows: tuple[ExternalWindow, ...] = ()
        # Last successful reads are used only to detect changes, never to plan.
        self._target_cache: dict[tuple[int, int, int, int], float] = {}
        # Foreign Target SOC schedules aligned with ``_external_windows``.
        self._external_meta: tuple[tuple[Any, float | None], ...] = ()
        # Original references of foreign targets this controller lowered.
        self._lowered: dict[tuple[int, int, int, int], float] = {}
        self._lowered_loaded = False
        self._aps_held_starts: set[datetime] = set()
        self.aps_recharge_reason: str | None = None
        self.aps_targets_within_lead_time = 0
        self._restoring_targets = False
        self._cleanup_only = False
        # Foreign targets the Gateway side refused to change, with the reason.
        # They are planned as fixed until they end, and logged once.
        self._refused_targets: dict[tuple[int, int, int, int], str] = {}
        # Refusals caused by a passing state (a safety check, a list being
        # refreshed) only hold for the pass they happened in.
        self._transient_refusals: set[tuple[int, int, int, int]] = set()
        self._logged_block_reason: str | None = None
        # Safety checks that last stopped a schedule write (logged once).
        self.blocked_details: list[str] = []
        self._reported_block: tuple[str, ...] = ()
        # Busy period (service, known end) a deferred create was logged for.
        self._create_wait_logged: tuple[int | None, datetime | None] | None = None
        self._low_soc_info_logged = False
        self.external_target_adjust_count = 0
        self.external_target_delete_count = 0
        self.busy_until: datetime | None = None
        self._planned_settings: dict[str, Any] | None = None
        self._planned_policy: dict[str, Any] | None = None
        self._external_move_times: deque[datetime] = deque()
        self._move_limit_logged: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._wake = asyncio.Event()
        self._stopping = False
        self._lock = asyncio.Lock()
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribers: list[Callable[[], None]] = []
        self.whole_day: WholeDayResult | None = None
        self.standard_plan_margin_sek: float | None = None
        self.planner_engine = "standard"
        self._logged_plan: tuple[object, ...] | None = None
        # The solar charging the whole-day plan last counted on (logged on change).
        self.solar_charging: SolarCharging | None = None
        self._solar_charging_logged = False
        self._resolution_limited_logged = False
        self.problem_since: datetime | None = None
        self.problem_notified = False
        # True while one planning pass runs. A passing non-problem status
        # inside the pass must not restart the problem timer; the status the
        # pass ends with decides.
        self._in_tick = False
        # Own verified schedules that start within the minimum lead time. The
        # planners cannot plan them again, so they are kept as fixed windows.
        self._binding_ids: frozenset[int] = frozenset()
        # Binding schedules already logged as kept (logged once per id).
        self._kept_logged: set[int] = set()

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
        self._track_problem(status, reason)
        for listener in tuple(self._listeners):
            listener()

    def _track_problem(self, status: str, reason: str | None) -> None:
        """Notify once when planning has been blocked longer than the set limit."""
        now = datetime.now(UTC)
        problem = status in {"blocked", "waiting_telemetry"}
        if self._in_tick and not problem:
            # Only the status a pass ends with may count as recovery.
            return
        if status == "blocked" and reason != self._logged_block_reason:
            # Every new block is in the Home Assistant log at once, not only
            # after the notification limit.
            self._logged_block_reason = reason
            _LOGGER.warning("Price optimization blocked: %s (%s)", reason,
                            reason_text(reason) if reason else "")
        elif status != "blocked":
            self._logged_block_reason = None
        if not problem:
            if self.problem_notified:
                _dismiss_notification(self.runtime)
                log_activity(self.runtime, PRICE_STATUS, "Price optimization is running again",
                             "Who: price optimization. Why: price optimization is no longer "
                             "blocked, so the earlier stop notification is removed.")
            self.problem_since, self.problem_notified = None, False
            return
        if self.problem_since is None:
            self.problem_since = now
        hours = int(self.runtime.operational_settings.get("price_blocked_notify_hours"))
        if hours <= 0 or self.problem_notified or now - self.problem_since < timedelta(hours=hours):
            return
        self.problem_notified = True
        text = reason_text(reason)
        log_activity(self.runtime, PRICE_STATUS,
                     f"Price optimization has been stopped for over {hours} h: {text}",
                     "Who: price optimization. Why: every planning pass has been blocked "
                     "or waiting for telemetry longer than the setting Notify when price "
                     "optimization is stopped for allows.")
        _create_notification(
            self.runtime,
            f"Prisoptimeringen har stått still i över {hours} h.\n\nOrsak: {text}\n\n"
            "Se sensorn Status för prisoptimering för detaljer.",
        )

    def _log_plan(self, plan: PricePlan, series: PriceSeries) -> None:
        """Log the plan when its intervals change, or why nothing is planned."""
        key = tuple(_action_key(action) for action in plan.actions)
        if not plan.actions:
            key = ("empty", self.planner_engine, datetime.now(UTC).date())
        if key == self._logged_plan:
            return
        self._logged_plan = key
        engine = "whole-day" if self.planner_engine == "whole_day" else "standard"
        if plan.actions:
            parts = [_action_text(action) for action in plan.actions[:6]]
            if len(plan.actions) > 6:
                parts.append(f"and {len(plan.actions) - 6} more")
            log_activity(self.runtime, PRICE_STATUS,
                         f"New plan ({engine} planner): " + "; ".join(parts)
                         + f". Expected profit {plan.expected_margin_sek:.2f} kr",
                         f"Who: price optimization, {engine} planner. Why: prices, SOC or the "
                         "forecast changed, so the plan was recalculated. Each interval is "
                         "only planned when its net profit after losses, grid cost and cycle "
                         "cost reaches your minimum.")
            return
        settings = self.runtime.operational_settings
        best = best_net_profit(
            series, datetime.now(UTC),
            float(settings.get("price_round_trip_efficiency_percent")) / 100,
            float(settings.get("price_grid_import_cost_sek_per_kwh")),
            float(settings.get("price_cycle_cost_sek_per_kwh")),
        )
        minimum = float(settings.get("price_min_difference_sek_per_kwh"))
        if best is None:
            log_activity(self.runtime, PRICE_STATUS,
                         f"Nothing planned ({engine} planner): no future prices to compare")
            return
        if best + 1e-9 >= minimum:
            # The prices would pay, so something else leaves no room for it.
            log_activity(
                self.runtime, PRICE_STATUS,
                f"Nothing planned ({engine} planner): the best charge-then-use price pair "
                f"would give about {best:.2f} kr/kWh (your minimum is {minimum:.2f}), but "
                "the battery's SOC limits, the expected house load or fixed aggregator "
                "Target SOC windows leave no room for it",
                "Who: price optimization. Why: a profitable price pair exists, but the "
                "battery cannot both take and use the energy around it: it is already at "
                "a SOC limit, the house would not use it in the expensive hours, or an "
                "aggregator Target SOC window that cannot be changed refills or holds the "
                "battery in between.",
            )
            return
        log_activity(
            self.runtime, PRICE_STATUS,
            f"Nothing planned ({engine} planner): the best charge-then-use price pair "
            f"gives about {best:.2f} kr/kWh net profit after losses, below your minimum "
            f"of {minimum:.2f} kr/kWh",
            "Who: price optimization. Why: no charge-then-use pair in the price horizon "
            "pays enough after losses and costs. Lower Minimum net profit for "
            "self-consumption, or wait for a day with a larger price difference.",
        )

    def attributes(self) -> dict[str, Any]:
        from .price_timeline import price_timeline

        config = self.config
        settings = self.runtime.operational_settings
        plan = self.plan
        whole_day = self.whole_day
        return {
            **self.soc_guard.attributes(),
            "delete_post_check_reason": self.delete_recovery_reason,
            "reason": self.reason,
            "reason_text": reason_text(self.reason) if self.reason else None,
            "blocked_details": self.blocked_details if self.status == "blocked" else [],
            "blocked_since": self.problem_since.isoformat() if self.problem_since else None,
            "planner_engine": self.planner_engine,
            "uses_energy_forecast": getattr(self.runtime, "energy_forecast", None) is not None,
            "plans_for_high_consumption": bool(settings.get("price_plan_high_consumption")),
            "standard_plan_expected_margin_sek": (
                None if self.standard_plan_margin_sek is None
                else round(self.standard_plan_margin_sek, 2)
            ),
            "whole_day_plan": None if whole_day is None else {
                "ready": whole_day.plan.ready,
                "blockers": list(whole_day.plan.blockers),
                "expected_margin_sek": round(whole_day.plan.expected_margin_sek, 2),
                # Compatibility name: the baseline excludes our price actions
                # but includes refills from fixed external SOC targets.
                "cost_without_battery_sek": round(whole_day.baseline_cost_sek, 2),
                "planned_cost_sek": round(whole_day.planned_cost_sek, 2),
                "action_count": len(merge_adjacent_actions(whole_day.plan.actions)),
                "energy_levels": whole_day.energy_levels,
                "resolution_limited": whole_day.resolution_limited,
                "segments": _segments(whole_day),
            },
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
            "aps_recharge_reason": self.aps_recharge_reason,
            "aps_targets_within_lead_time": self.aps_targets_within_lead_time,
            "aps_held_target_count": len(self._aps_held_starts),
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

    async def prepare(self, publish: bool = True) -> None:
        """Validate every required binding before enabling live reconciliation.

        The planning pass validates with ``publish=False``: it publishes its
        own status, and a passing ``ready`` would look like a recovery.
        """
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
        if publish:
            self._publish("ready")

    def start(self, *, cleanup_only: bool = False) -> None:
        self._cleanup_only = cleanup_only
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
        else:
            self.request_refresh()

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
            try:
                await self._restore_external_targets_locked()
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
                        self._report_blocked(f"remove {_describe(record)}", result)
                        self._publish("blocked", f"price_delete_{result.reason}")
                        return
                    self.delete_count += 1
                    log_activity(self.runtime, PRICE_STATUS, f"Removed {_describe(record)} "
                                 "(price optimization turned off)",
                                 "Who: price optimization. Why: the switch Automatic price "
                                 "optimization was turned off, so its own schedules are removed.")
                if self._lowered:
                    self._publish("blocked", "price_external_target_restore_pending")
                else:
                    self._publish("disabled")
            finally:
                if self._pending_originals():
                    self.start(cleanup_only=True)

    async def release_for_authority_off(self) -> list[str]:
        """Remove every live price-owned effect while Gateway writes are still allowed.

        Future identities are deleted; an interval already in progress uses the
        verified exact price stop. Failures are reported, never retried here.
        """
        await self.stop()
        problems: list[str] = []
        async with self._lock:
            await self._restore_external_targets_locked()
            if self._lowered:
                problems.append("price_external_target_restore_pending")
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

    def _pending_originals(self) -> bool:
        return bool(self._lowered or getattr(self.runtime, "lowered_external_targets", {}))

    async def _retire_resolved_originals(self, snapshot: Any) -> None:
        """Retire expired or provably absent one-shot identities, without writes."""
        saved = getattr(self.runtime, "lowered_external_targets", {})
        for key, reference in saved.items():
            self._lowered.setdefault(key, reference)
        self._lowered_loaded = True
        now = datetime.now(UTC).timestamp()
        evaluated = getattr(snapshot, "evaluated_at_epoch", 0)
        if (snapshot.data_status != "current" or not now - 60 <= evaluated <= now + 5):
            return
        before = dict(self._lowered)
        for key in before:
            index, service, start, duration = key
            end = start + duration
            scope = (getattr(snapshot, "window_start_epoch", now + 1) <= start
                     and getattr(snapshot, "window_end_epoch", 0) >= end)
            present = any(
                item.sch_index == index and item.service_no == service
                and item.start_epoch == start and item.duration_seconds == duration
                for item in snapshot.schedules
            )
            if end <= min(now, evaluated) or scope and not present:
                self._lowered.pop(key, None)
        if self._lowered != before:
            await self.runtime.async_save_lowered_targets(dict(self._lowered))

    async def restore_external_targets(self) -> None:
        """Undo remembered target changes without creating any new price effects."""
        async with self._lock:
            await self._restore_external_targets_locked()

    async def _restore_external_targets_locked(self) -> None:
        saved = getattr(self.runtime, "lowered_external_targets", {})
        if not self._lowered and not saved:
            return
        coordinator = self.runtime.scheduler_coordinator
        if coordinator is None:
            self._publish("blocked", "price_external_target_restore_pending")
            return
        snapshot = await coordinator.async_refresh()
        if snapshot is None or snapshot.data_status != "current":
            self._publish("blocked", "price_external_target_restore_pending")
            return
        await self._retire_resolved_originals(snapshot)
        if not self._lowered:
            return
        await self._load_external_windows(())
        self._restoring_targets = True
        try:
            # The existing mutation path retains identity, originals and lead
            # time. Its one-write bound applies to each restoration pass too.
            for _ in range(max(len(self._lowered), len(saved))):
                before = len(self._lowered) + len(saved)
                await self._apply_external_targets(
                    self._external_windows,
                    PricePlan((), (), "restore-original-targets", 0, 100, 0),
                    {"min_soc": self.runtime._automation_policy().soc_min_percent},
                )
                saved = getattr(self.runtime, "lowered_external_targets", {})
                if len(self._lowered) + len(saved) >= before:
                    break
        finally:
            self._restoring_targets = False
        self._aps_held_starts.clear()
        if self._lowered:
            self._publish("blocked", "price_external_target_restore_pending")

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
            self._in_tick = True
            try:
                await self._tick_locked()
            finally:
                self._in_tick = False
            # Only a pass that returned normally ends with a status of its
            # own. After an exception ``_run`` publishes the failure instead,
            # so the old status must not reset the problem timer first.
            self._track_problem(self.status, self.reason)
        await self._resume_after_recovery()

    async def check_unconfirmed_deletes(self) -> None:
        """Run the read-only post-check now, for example from the panel."""
        async with self._lock:
            await self._settle_unconfirmed_deletes()
        await self._resume_after_recovery()

    def needs_post_check(self) -> bool:
        """Whether an own schedule's removal still waits for its post-check."""
        ownership = self.runtime.scheduler_ownership
        return ownership is not None and any(
            record.owner == PRICE_OWNER and record.lifecycle is OwnershipLifecycle.CONFLICTED
            for record in ownership.records.values()
        )

    async def _resume_after_recovery(self) -> None:
        # Outside the planner lock: turning price optimization back on goes
        # through the normal switch path, which prepares and starts this loop.
        if hasattr(self.runtime, "async_resume_after_recovery"):
            if await self.runtime.async_resume_after_recovery():
                self.request_refresh()

    async def _settle_unconfirmed_deletes(self) -> None:
        # An own schedule whose DELETE outcome was never confirmed blocks all
        # writes. One keyed GET (nothing is sent) settles it: proven absence
        # retires the identity, an exact unchanged schedule is owned again.
        mutation_manager = self.runtime.scheduler_mutation_manager
        ownership = self.runtime.scheduler_ownership
        if mutation_manager is None or ownership is None:
            return
        for conflicted in tuple(ownership.records.values()):
            if (
                conflicted.owner != PRICE_OWNER
                or conflicted.lifecycle is not OwnershipLifecycle.CONFLICTED
                or conflicted.m_id in self.soc_guard._attempted
            ):
                continue
            seen = self._conflicts_seen.setdefault(conflicted.m_id, datetime.now(UTC))
            absent = self._absent_from_inventory(conflicted, seen)
            if not absent and await self._refresh_list_after(seen):
                # The fallback needs a list read after the removal was seen;
                # read it now instead of waiting for the next 15-min refresh.
                absent = self._absent_from_inventory(conflicted, seen)
            recovered = await mutation_manager.recover_conflicted_price_delete(
                conflicted.m_id,
                absent_from_inventory=absent,
            )
            if recovered.status is not SchedulerTransactionStatus.SUCCESS:
                self.delete_recovery_reason = recovered.reason
                continue
            self.delete_recovery_reason = None
            self._conflicts_seen.pop(conflicted.m_id, None)
            gone = recovered.lifecycle_after is OwnershipLifecycle.ORPHANED
            ended = recovered.lifecycle_after is OwnershipLifecycle.EXPIRED_RESERVED
            if gone:
                self.delete_count += 1
            log_activity(
                self.runtime, PRICE_STATUS,
                f"Post-check: {_describe(conflicted)} is "
                + ("confirmed removed" if gone
                   else "over, so its removal no longer matters" if ended
                   else "still in place and unchanged"),
                "Who: price optimization. Why: the Gateway never confirmed an earlier "
                "removal, so all price writes were paused. "
                + ("Its time has passed and a one-shot schedule cannot run again, so "
                   "its ID is kept reserved (never reused or re-sent) and the pause "
                   "is lifted." if ended
                   else "A read-only check of the schedule settled it"
                   + (" and the pause is lifted." if gone
                      else "; the plan decides what to do with it.")),
            )

    async def _refresh_list_after(self, seen: datetime) -> bool:
        """Read the schedule list (read-only) when the last one predates ``seen``."""
        coordinator = self.runtime.scheduler_coordinator
        refresh = getattr(coordinator, "async_refresh", None)
        snapshot = None if coordinator is None else coordinator.last_good_snapshot
        if refresh is None or (
            snapshot is not None and snapshot.evaluated_at_epoch > seen.timestamp()
        ):
            return False
        try:
            await refresh()
        except Exception:  # noqa: BLE001 - the regular refresh retries later
            return False
        return True

    def _wake_later(self, seconds: float) -> None:
        """Plan again after ``seconds`` (used to settle an unconfirmed removal)."""
        try:
            asyncio.get_running_loop().call_later(seconds, self._wake.set)
        except RuntimeError:
            return

    def _absent_from_inventory(self, record: SchedulerOwnershipRecord, seen: datetime) -> bool:
        """Whether a complete list read after ``seen`` has nothing in the record's place."""
        coordinator = self.runtime.scheduler_coordinator
        snapshot = None if coordinator is None else coordinator.last_good_snapshot
        start = _record_start(record)
        duration = record.intended_state.get("duration")
        if (
            snapshot is None
            or snapshot.data_status != "current"
            or snapshot.evaluated_at_epoch <= seen.timestamp()
            or start is None
            or type(duration) is not int
        ):
            return False
        begin = int(start.timestamp())
        end = begin + duration
        # The list must cover the schedule's whole time, which is still ahead.
        if not (snapshot.window_start_epoch <= begin and end <= snapshot.window_end_epoch):
            return False
        if begin <= snapshot.evaluated_at_epoch:
            return False
        return not any(
            schedule.start_epoch == begin
            or schedule.service_type == _SERVICE_TYPE.get(record.service)
            and schedule.start_epoch < end
            and begin < schedule.start_epoch + (schedule.duration_seconds or 0)
            for schedule in snapshot.schedules
        )

    async def _tick_locked(self) -> None:
        # Read-only, so it also runs while the switches are off: after a
        # restart they stay off until every unconfirmed outcome is settled.
        await self._settle_unconfirmed_deletes()
        recharged = await retire_recharged_context(self.runtime)
        if self._pending_originals() and (
            recharged
            or not self.runtime.operational_settings.get("price_schedule_writes_enabled")
            or not self.runtime.operational_settings.get("price_adjust_external_target_soc")
        ):
            await self._restore_external_targets_locked()
        if (self._cleanup_only
                or not self.runtime.controller_state.automatic_price_optimization_enabled):
            await self._restore_external_targets_locked()
            if self._lowered:
                return
            self._publish("disabled")
            return
        # The simulator runtime has no calibration hold.
        calibrating = getattr(self.runtime, "battery_calibration_active", None)
        if calibrating is not None and calibrating():
            self._publish("waiting_for_priority", "battery_calibration_running")
            return
        try:
            await self.prepare(publish=False)
        except ValueError as error:
            if self._pending_originals():
                await self._restore_external_targets_locked()
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
            if self._pending_originals():
                await self._restore_external_targets_locked()
            self._publish("waiting_prices", "price_entity_unavailable")
            return
        if datetime.now(UTC) - state.last_updated.astimezone(UTC) > timedelta(hours=36):
            if self._pending_originals():
                await self._restore_external_targets_locked()
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
            low_soc_info = bool(getattr(health, "low_soc_info", False))
            if low_soc_info != self._low_soc_info_logged:
                self._low_soc_info_logged = low_soc_info
                if low_soc_info:
                    log_activity(
                        self.runtime, PRICE_STATUS,
                        "Gateway shows its low-SOC notice (Info, event 65536); "
                        "planning continues",
                        "Who: price optimization. Why: the Gateway reports low SOC as "
                        "status Info with system event 65536 until the battery is "
                        "charged again. It is a notice, not a fault, so charging can "
                        "still be planned; the SOC limits keep discharges above your "
                        "minimum. Warning or any other event still stops planning.",
                    )
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
            if self._pending_originals():
                await self._restore_external_targets_locked()
            self._publish("blocked", str(error))
            return
        if self.plan is None or not plan.ready:
            # This first plan ignores the aggregator's windows; showing it
            # between passes made "next charge" flip between a time and unknown,
            # so the last complete plan stays visible until the new one is done.
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
            if self._pending_originals():
                await self._restore_external_targets_locked()
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
        schedules = snapshot.schedules
        if any(
            _record_has_schedule_shape(record, schedule)
            and not _record_matches_schedule(record, schedule, schedules)
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
            await self._restore_external_targets_locked()
            await self._load_external_windows(plan.actions)
            self.plan = self._build_plan(series, limits, windows=self._external_windows)
            self._log_plan(self.plan, series)
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
        self._prune_refused_targets()
        if not adjust:
            self._aps_held_starts.clear()
            self.aps_recharge_reason = "aggregator_changes_not_permitted"
        # A refused target is planned as fixed from then on, so plan again
        # around it instead of dropping everything after it.
        for _ in range(3):
            windows = (
                self._with_adjustable_targets(series, limits) if adjust
                else self._external_windows
            )
            self.plan = self._build_plan(series, limits, windows=windows)
            if not self.plan.ready:
                self._publish("waiting_for_priority", "price_external_schedule_energy_unknown")
                return
            refused_before = len(self._refused_targets)
            failed_from = await self._apply_external_targets(windows, self.plan, limits)
            if len(self._refused_targets) == refused_before:
                break
        self._log_plan(self.plan, series)
        actions = self.plan.actions
        if failed_from is not None:
            # The plan relied on that window holding; nothing after it is safe.
            actions = tuple(a for a in actions if a.end <= failed_from)
        if self.busy_until is not None:
            # Written while a known external interval runs: never anything
            # that could start before that interval has ended.
            actions = tuple(a for a in actions if a.start >= self.busy_until)
        # Only apply the first cycle. Self-consumption, background controllers
        # and support interruptions can change the real energy before another
        # cycle. Later cycles remain proposals until fresh SoC is measured. A
        # cycle includes actions that follow back to back: nothing can be
        # written while our own schedule runs, so they would come too late.
        actions, _ = _applied_cycle(actions, self._chain_gap())
        # An action inside an aggregator window that may not be replaced yet
        # waits (with everything after it) until the window may be replaced.
        replace_until = self._replace_until(self.plan.actions)
        deferred_from = min(
            (
                action.start for action in actions
                for window in windows
                if window.adjustable and window.start not in self._aps_held_starts
                and window.start >= replace_until
                and action.start < window.end and window.start < action.end
            ),
            default=None,
        )
        if deferred_from is not None:
            actions = tuple(a for a in actions if a.end <= deferred_from)
        if not await self._reconcile(actions, keep=self.plan.actions):
            return
        self.last_applied_at = datetime.now(UTC)
        self._reported_block = ()
        self._create_wait_logged = None
        self._publish("active" if actions else "ready")

    def _replace_until(self, actions: tuple[PricePlanAction, ...]) -> datetime:
        """Aggregator windows starting before this may be replaced (deleted).

        A deleted aggregator schedule cannot be restored, so windows are only
        replaced for the applied cycle once its first discharge is near, or
        when they start before the pass after next could reach them. Until
        then they are only lowered, which can be undone.
        """
        now = datetime.now(UTC)
        lead = now + timedelta(minutes=self.config.minimum_lead_minutes)
        until = lead + timedelta(minutes=self.config.refresh_minutes * 1.5)
        cycle, cycle_end = _applied_cycle(actions, self._chain_gap())
        first = next(
            (a for a in cycle if a.direction is PriceDirection.DISCHARGE), None,
        )
        if (
            cycle_end is not None and first is not None
            and first.start <= now + timedelta(hours=_REPLACE_AHEAD_HOURS)
        ):
            until = max(until, cycle_end)
        return until

    def _chain_gap(self) -> timedelta:
        """Largest gap after one of our schedules that still cannot be written in time."""
        return timedelta(minutes=self.config.minimum_lead_minutes + _CHAIN_MARGIN_MINUTES)

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
        if self._restoring_targets:
            health = self.runtime.plant_health()
            return bool(
                self.runtime.production_control_enabled()
                and health.telemetry_fresh and health.system_safe and health.battery_safe
                and health.grid_safe and health.system_events_clear
                and await self._priority_allows_mutation()
            )
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
        settings = self.runtime.operational_settings
        forecast = getattr(self.runtime, "energy_forecast", None)
        high = bool(settings.get("price_plan_high_consumption"))
        arguments: dict[str, Any] = dict(
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
        )
        net_load_kw = (
            None if forecast is None
            else lambda start, end: forecast.net_load_kw(start, end, high=high)
        )
        # Own schedules about to start are fixed: plan around them and count
        # on their energy instead of removing them just before they run.
        windows = (*windows, *self._binding_windows(arguments, limits, net_load_kw))
        standard = build_price_plan(
            series,
            **arguments,
            external_windows=windows,
            net_load_kw=net_load_kw,
        )
        self.standard_plan_margin_sek = standard.expected_margin_sek
        plan = standard
        self.whole_day = None
        if forecast is not None:
            solar_charging = self._solar_charging(policy)
            try:
                self.whole_day = build_whole_day_plan(
                    series,
                    **arguments,
                    external_windows=windows,
                    forecast=lambda start, end: forecast.net_forecast(start, end, high=high),
                    solar_charging=solar_charging,
                )
            except ValueError:
                self.whole_day = None
            limited = self.whole_day is not None and self.whole_day.resolution_limited
            if limited != getattr(self, "_resolution_limited_logged", False):
                self._resolution_limited_logged = limited
                if limited and bool(settings.get("price_whole_day_planner")):
                    assert self.whole_day is not None
                    log_activity(
                        self.runtime, PRICE_STATUS,
                        "Whole-day plan leaves out flows below its energy step",
                        f"Why: with {self.whole_day.energy_levels} energy levels for "
                        f"{float(arguments['usable_capacity_kwh']):.1f} kWh, some allowed "
                        "charge or discharge in an interval is smaller than one level, so "
                        "the plan leaves that flow out and may do less than the battery "
                        "could.",
                    )
        if (
            bool(settings.get("price_whole_day_planner"))
            and self.whole_day is not None
            and self.whole_day.plan.ready
        ):
            plan = self.whole_day.plan
            self.planner_engine = "whole_day"
        else:
            self.planner_engine = (
                "standard_whole_day_unavailable"
                if bool(settings.get("price_whole_day_planner")) else "standard"
            )
        return replace(
            plan, actions=merge_adjacent_actions(plan.actions, policy.max_duration_seconds)
        )

    def _solar_charging(self, policy: Any) -> SolarCharging:
        """The solar charging that runs without a schedule, as the solar controller does it."""
        settings = self.runtime.operational_settings
        battery = self.runtime.battery
        state = self.runtime.controller_state
        switch_on = bool(state.automatic_solar_charging_enabled)
        authorized = (
            bool(policy.enabled)
            and "automatic" in policy.allowed_operation_classes
            and policy.max_duration_seconds > 0
        )
        # The same run-time holds that stop the solar controller (solar_controller.py).
        calibrating = getattr(self.runtime, "battery_calibration_active", None)
        hold: str | None = None
        if calibrating is not None and calibrating():
            hold = "battery calibration is running, so the solar controller waits"
        elif state.adaptive_peak_shaving_enabled:
            hold = "adaptive peak shaving is on, which blocks automatic solar charging"
        elif not self.runtime.production_control_enabled():
            hold = "production control is disabled, so the solar controller is blocked"
        charge_limit_kw = (
            max(0.0, battery.charge_power_limit_w / 1000)
            if battery.charge_power_limit_w is not None else 0.0
        )
        defer_from: float | None = None
        defer_until: datetime | None = None
        solar = getattr(self.runtime, "solar_charging_controller", None)
        decision = getattr(solar, "smart_decision", None)
        if (
            bool(getattr(solar, "_smart_deferring", False))
            and decision is not None
            and decision.defer
            and decision.charge_price is not None
            and decision.deadline is not None
        ):
            defer_from = decision.charge_price + float(
                settings.get("solar_smart_min_advantage_sek_per_kwh")
            )
            defer_until = decision.deadline
        limit = SolarCharging(
            enabled=switch_on and authorized and hold is None,
            maximum_kw=min(
                float(settings.get("solar_max_charge_power_kw")),
                charge_limit_kw,
                float(policy.max_power_kw),
            ),
            maximum_soc_percent=min(
                float(settings.get("solar_max_soc_percent")), float(policy.soc_max_percent)
            ),
            defer_from_price=defer_from,
            defer_until=defer_until,
            minimum_surplus_kw=float(settings.get("solar_export_start_threshold_kw")),
            export_buffer_kw=float(settings.get("solar_export_buffer_kw")),
        )
        whole_day_enabled = bool(settings.get("price_whole_day_planner"))
        if not whole_day_enabled:
            self._solar_charging_logged = False
        if limit != self.solar_charging or (whole_day_enabled and not self._solar_charging_logged):
            if not switch_on:
                why = "automatic solar charging is switched off, so the surplus is exported"
            elif not authorized:
                why = "the automation policy does not allow automatic control"
            elif hold is not None:
                why = hold
            elif defer_until is not None:
                why = (
                    "smart solar charging exports now; no solar charging is counted before "
                    f"{clock(defer_until)} in intervals priced at {defer_from or 0:.2f} "
                    "kr/kWh or more"
                )
            else:
                why = (
                    f"automatic solar charging charges up to {limit.maximum_kw:.1f} kW "
                    f"and {limit.maximum_soc_percent:.0f} % usable SOC, from a surplus "
                    f"of {limit.minimum_surplus_kw:.1f} kW, and leaves "
                    f"{limit.export_buffer_kw:.1f} kW to the grid as export buffer; "
                    "it charges whenever no written schedule takes over, so the plan "
                    "cannot postpone it"
                )
            if whole_day_enabled:
                log_activity(
                    self.runtime, PRICE_STATUS,
                    "Whole-day plan counts on solar charging "
                    + ("up to its limits" if limit.enabled else "not at all"),
                    f"Why: {why}. Charging beyond this needs a written price schedule "
                    "that earns the minimum profit.",
                )
                self._solar_charging_logged = True
            self.solar_charging = limit
        return limit

    def _binding_windows(
        self,
        arguments: Mapping[str, Any],
        limits: Mapping[str, float],
        net_load_kw: Callable[[datetime, datetime], float | None] | None,
    ) -> tuple[ExternalWindow, ...]:
        """Fixed windows for own verified schedules starting within the lead time.

        Their expected resulting usable SOC is chained from the current SOC and
        each schedule's own energy. An unreadable power gives a window with
        unknown energy, so planning ends at its start. The ids are kept in
        ``self._binding_ids`` so the same schedules are never removed.
        """
        now: datetime = arguments["now"]
        lead = now + timedelta(minutes=int(arguments["minimum_lead_minutes"]))
        capacity = float(arguments["usable_capacity_kwh"])
        minimum = float(arguments["minimum_soc_percent"])
        maximum = float(arguments["maximum_soc_percent"])
        efficiency = sqrt(limits["efficiency"] / 100)
        # A schedule cannot move more power than the battery and limits allow now.
        charge_cap = float(arguments["maximum_charge_kw"])
        discharge_cap = float(arguments["maximum_discharge_kw"])
        binding: list[tuple[datetime, int, SchedulerOwnershipRecord]] = []
        for record in self._price_records():
            start = _record_start(record)
            duration = record.intended_state.get("duration")
            if (
                start is None or type(duration) is not int or duration <= 0
                or not now < start <= lead
            ):
                continue
            binding.append((start, duration, record))
        binding.sort(key=lambda item: (item[0], item[2].m_id))
        self._binding_ids = frozenset(record.m_id for _, _, record in binding)
        soc: float | None = float(arguments["usable_soc_percent"])
        windows: list[ExternalWindow] = []
        for start, duration, record in binding:
            end = start + timedelta(seconds=duration)
            hours = duration / 3600
            values = record.intended_state.get("values", {})
            values = values if isinstance(values, Mapping) else {}
            charge_kw: float | None = None
            delivered_kwh: float | None = None
            if record.service == "demandresponse":
                pacref = _finite_number(values.get("pacref"))
                if pacref is not None and pacref >= 0:
                    charge_kw = min(charge_cap, pacref / 1000)
                elif pacref is not None:
                    delivered_kwh = min(discharge_cap, -pacref / 1000) * hours
            elif record.service == "peakshaving":
                power = _finite_number(values.get("discharge_max"))
                if power is not None and power >= 0:
                    delivered_kw = min(discharge_cap, power / 1000)
                    demand = None if net_load_kw is None else net_load_kw(start, end)
                    if demand is not None and isfinite(demand):
                        delivered_kw = min(delivered_kw, max(0.0, demand))
                    delivered_kwh = delivered_kw * hours
            if soc is not None and charge_kw is not None:
                ceiling = max(maximum, soc)
                soc = min(ceiling, soc + charge_kw * hours * efficiency / capacity * 100)
            elif soc is not None and delivered_kwh is not None:
                floor = min(minimum, soc)
                soc = max(floor, soc - delivered_kwh / efficiency / capacity * 100)
            else:
                # Unknown energy: this and every later window end the plan.
                soc = None
            windows.append(ExternalWindow(start, end, soc))
        return tuple(windows)

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
        # Moving an aggregator schedule changes it, which needs the same
        # permission as adjusting its Target SOC.
        if (
            not self.runtime.operational_settings.get("price_adjust_external_target_soc")
            or source_id is None
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
            # The limit stops further moves only; price optimization goes on.
            first = self._external_move_times[0]
            if self._move_limit_logged != first:
                self._move_limit_logged = first
                log_activity(
                    self.runtime, PRICE_STATUS,
                    "Not moving more aggregator charges: 4 moves in the last 24 h",
                    "Who: price optimization. Why: at most four aggregator charges are "
                    f"moved per 24 hours; the next move is possible after "
                    f"{clock(first + timedelta(hours=24))}. Planning and your own "
                    "schedules continue as usual.",
                )
            return True
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
        schedules = sorted(self._inventory(), key=lambda item: item.start_epoch)
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

    def _inventory(self) -> tuple[Any, ...]:
        """Schedules of the last complete canonical inventory window.

        Never the scheduler manager's shared list: the calendar, the panel and
        each write's own observation replace that with narrower windows, which
        can hide a running or upcoming foreign schedule.
        """
        coordinator = self.runtime.scheduler_coordinator
        snapshot = None if coordinator is None else coordinator.last_good_snapshot
        return () if snapshot is None else tuple(snapshot.schedules)

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
        if service is None:
            return None
        owned = self._price_records()
        now = int(datetime.now(UTC).timestamp())
        schedules = self._inventory()
        running = [
            schedule for schedule in schedules
            if schedule.enabled
            and schedule.service_type == service
            and schedule.start_epoch <= now < schedule.end_epoch
            and schedule.recurrence.recurring is False
            and not any(
                _record_matches_schedule(record, schedule, schedules)
                for record in owned
            )
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
        owned = self._price_records()
        ownership = self.runtime.scheduler_ownership
        # Our own non-price schedules (for example a manual Target SOC) also
        # show up as foreign in the Gateway list: never lower or delete them.
        own_other = [
            record for record in (ownership.records.values() if ownership is not None else ())
            if record.owner != PRICE_OWNER
        ]
        schedules = self._inventory()
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
                or any(_record_matches_schedule(record, schedule, schedules) for record in owned)
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
                previous = self._target_cache.get(key)
                try:
                    value = float(
                        (await gateway.client.read_target_soc(schedule.service_no))[
                            "reference"
                        ]
                    )
                    failure = None if isfinite(value) else "non-finite reference"
                except Exception as error:  # noqa: BLE001 - unreadable means unknown energy
                    failure = f"{type(error).__name__}: {error}"
                    value = float("nan")
                if failure is None:
                    reference = value
                    self._target_cache[key] = value
                    if previous is not None and previous != value:
                        when = time_span(
                            datetime.fromtimestamp(schedule.start_epoch, UTC),
                            datetime.fromtimestamp(schedule.end_epoch, UTC),
                        )
                        log_activity(
                            self.runtime, PRICE_STATUS,
                            f"Aggregator Target SOC {when} changed from {previous:g} % "
                            f"to {value:g} %; the price plan uses the new target",
                            "Who: aggregator via the Gateway. Why: the Gateway returned "
                            "a different Target SOC for the same schedule; the price "
                            "plan uses the latest reading.",
                        )
                else:
                    self._target_cache.pop(key, None)
                    if previous is not None:
                        when = time_span(
                            datetime.fromtimestamp(schedule.start_epoch, UTC),
                            datetime.fromtimestamp(schedule.end_epoch, UTC),
                        )
                        log_activity(
                            self.runtime, PRICE_STATUS,
                            f"Aggregator Target SOC {when} is unreadable ({failure}); "
                            "the price plan treats its resulting energy as unknown",
                            "Who: price optimization. Why: the current Gateway Target "
                            f"SOC read failed ({failure}); planning stops at the start "
                            "of this window instead of using an earlier target.",
                        )
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
                and not any(_record_has_schedule_shape(r, schedule) for r in own_other)
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
        """Plan as if the aggregator's Target SOC windows were not there.

        Every future, readable Target SOC window is one the controller may
        hold: the plan finds every charge and discharge that meets your
        minimum profit, and each window is then lowered to the planned level
        (or replaced where the plan uses its time). Windows where the plan
        keeps at least the target need no change.
        """
        self._aps_held_starts = set()
        self.aps_recharge_reason = None
        lead = datetime.now(UTC) + timedelta(minutes=self.config.minimum_lead_minutes)
        self.aps_targets_within_lead_time = sum(
            meta is not None and window.start <= lead
            for window, meta in zip(self._external_windows, self._external_meta, strict=True)
        )
        eligible = [
            meta is not None and meta[1] is not None and window.start > lead
            and _target_key(meta[0]) not in self._refused_targets
            for window, meta in zip(self._external_windows, self._external_meta, strict=True)
        ]
        trial = tuple(
            replace(window, adjustable=flag)
            for window, flag in zip(self._external_windows, eligible, strict=True)
        )
        if not any(eligible):
            self.aps_recharge_reason = "target_active_or_within_lead_time"
            return self._external_windows
        first = self._build_plan(series, limits, windows=trial)
        discharges = [a for a in first.actions if a.direction is PriceDirection.DISCHARGE]
        second_plan: PricePlan | None = None
        refill_windows = trial
        if first.ready and not discharges:
            # With every window held and no own recharge needed (a full
            # battery), a discharge is valued at the horizon's mean price and
            # rarely meets the minimum. Try once more with the battery refilled
            # by the cheapest aggregator window instead, holding those before it.
            known = [w for w in self._external_windows if w.known]
            first_eligible = next(
                (w.start for w, flag in zip(self._external_windows, eligible, strict=True)
                 if flag),
                None,
            )
            later = [w for w in known if first_eligible is not None and w.start > first_eligible]
            if later:
                cut = min(later, key=lambda w: (_window_price(series, w), w.start)).start
                refill = tuple(
                    replace(window, adjustable=flag and window.start < cut)
                    for window, flag in zip(self._external_windows, eligible, strict=True)
                )
                second = self._build_plan(series, limits, windows=refill)
                second_discharges = [
                    a for a in second.actions if a.direction is PriceDirection.DISCHARGE
                ]
                if second.ready and second_discharges:
                    first, discharges = second, second_discharges
                    second_plan, refill_windows = second, refill
        if not first.ready or not discharges:
            return self._with_aps_targets(series, limits, self._external_windows, eligible, first)
        if first is not second_plan:
            # The plan is made as if no aggregator target existed; every
            # target it would collide with is lowered to the planned level
            # or, where the plan uses the time itself, replaced.
            held = trial
        else:
            held = refill_windows
        return self._with_aps_targets(series, limits, held, eligible, first)

    def _with_aps_targets(
        self, series: PriceSeries, limits: dict[str, float],
        windows: tuple[ExternalWindow, ...], eligible: list[bool], plan: PricePlan,
    ) -> tuple[ExternalWindow, ...]:
        """Hold expensive recharge only after real owned APS discharge.

        A later aggregator target must have enough time and charging power to
        recover the energy. Complete price coverage is required for both slots.
        """
        context = getattr(self.runtime, "aps_recharge_context", None)
        now = datetime.now(UTC).timestamp()
        battery = self.runtime.battery
        if not isinstance(context, dict) or context.get("owner") != "aps":
            self.aps_recharge_reason = "verified_aps_discharge_unavailable"
            return windows
        observed = _finite_number(context.get("observed_at"))
        expires = _finite_number(context.get("expires_at"))
        measured = _finite_number(context.get("usable_soc_percent"))
        if (observed is None or expires is None or measured is None
                or not 0 <= measured <= 100 or not observed <= now < expires
                or expires - observed > 24 * 3600
                or type(context.get("message_id")) is not int):
            self.aps_recharge_reason = "aps_discharge_context_expired_or_invalid"
            return windows
        fresh = getattr(self.runtime.telemetry, "is_fresh", None)
        if not callable(fresh) or not fresh("battery") or not fresh("core"):
            self.aps_recharge_reason = "aps_recharge_soc_not_fresh"
            return windows
        soc = battery.usable_soc
        if soc is not None and isfinite(soc) and soc > measured + 0.5:
            self.aps_recharge_reason = "aps_discharge_context_recharged"
            return windows
        if soc is None or not isfinite(soc) or not plan.ready:
            self.aps_recharge_reason = "aps_recharge_plan_unavailable"
            return windows
        policy = self.runtime._automation_policy()
        power = min(limits["charge_kw"], policy.max_power_kw,
                    max(0.0, (battery.charge_power_limit_w or 0) / 1000))
        efficiency = sqrt(limits["efficiency"] / 100)
        maximum = min(limits["max_soc"], policy.soc_max_percent)
        projected = dict(plan.window_socs)
        result = list(windows)
        self.aps_recharge_reason = "no_feasible_cheaper_recharge"
        for index in reversed(range(len(windows))):
            window, flag = windows[index], eligible[index]
            if not flag or window.adjustable:
                continue
            price = _covered_window_price(series, window)
            meta = self._external_meta[index]
            originals = getattr(self.runtime, "lowered_external_targets", {})
            original = None if meta is None else self._lowered.get(
                _target_key(meta[0]), originals.get(_target_key(meta[0]))
            )
            target = window.target_usable_soc_percent
            if (original is not None and battery.effective_min_soc is not None
                    and battery.effective_max_soc is not None
                    and battery.effective_max_soc > battery.effective_min_soc):
                target = (original - battery.effective_min_soc) * 100 / (
                    battery.effective_max_soc - battery.effective_min_soc
                )
            at_start = max(soc, projected.get(window.start, soc))
            if price is None or target is None or target <= at_start:
                continue
            for later in result[index + 1:]:
                later_price = _covered_window_price(series, later)
                later_target = later.target_usable_soc_percent
                if (later.adjustable or later.start < window.end
                        or later.end.timestamp() > min(
                            expires, now + self.config.planning_horizon_hours * 3600
                        )
                        or later_price is None
                        or later_target is None or later_target < min(target, maximum)
                        or price - later_price <= limits["difference"]):
                    continue
                hours = (later.end - later.start).total_seconds() / 3600
                recharge_soc = min(soc, at_start, projected.get(later.start, soc))
                need = max(0, min(target, maximum) - recharge_soc) / 100 * limits["capacity"]
                if power * hours * efficiency < need or policy.bounded_duration(
                    later.start, int(hours * 3600)
                ) < hours * 3600:
                    continue
                # Unknown intervening energy cannot underwrite delayed recharge.
                if any(not w.known for w in windows[index + 1:] if w.start < later.start):
                    continue
                result[index] = replace(window, adjustable=True)
                self._aps_held_starts.add(window.start)
                self.aps_recharge_reason = "verified_aps_discharge_cheaper_recharge_later"
                break
        return tuple(result)

    async def _apply_external_targets(
        self,
        windows: tuple[ExternalWindow, ...],
        plan: PricePlan,
        limits: dict[str, float],
    ) -> datetime | None:
        """Lower held foreign targets to the plan; restore those no longer held.

        Returns the start of the first window that could not be held, if any.
        The aggregator's own references of lowered targets are kept across
        restarts, so a target can be raised back when the plan keeps more.
        """
        if not self._lowered_loaded:
            self._lowered_loaded = True
            saved = getattr(self.runtime, "lowered_external_targets", None)
            if isinstance(saved, dict):
                for key, reference in saved.items():
                    self._lowered.setdefault(key, reference)
        before = dict(self._lowered)
        try:
            return await self._apply_external_targets_once(windows, plan, limits)
        finally:
            save: Any = getattr(self.runtime, "async_save_lowered_targets", None)
            if self._lowered != before and save is not None:
                await save(dict(self._lowered))

    async def _apply_external_targets_once(
        self,
        windows: tuple[ExternalWindow, ...],
        plan: PricePlan,
        limits: dict[str, float],
    ) -> datetime | None:
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
        # A deleted aggregator target cannot be restored, so only windows the
        # applied cycle uses, or that start before the pass after next could
        # still reach them, are replaced; later ones are held for now.
        replace_until = self._replace_until(plan.actions)
        for window, meta in zip(windows, self._external_meta, strict=True):
            if meta is None or window.start <= lead:
                continue
            schedule, reference = meta[0], meta[1]
            if reference is None:
                continue
            key = _target_key(schedule)
            replace_window = (
                window.adjustable
                and window.start not in self._aps_held_starts
                and window.start < replace_until
                and any(
                    action.start < window.end and window.start < action.end
                    for action in plan.actions
                )
            )
            if replace_window:
                # The plan uses this time itself: remove the aggregator target.
                desired, allow_delete = reference, True
            elif window.adjustable:
                fallback = (battery.usable_soc if window.start in self._aps_held_starts else floor)
                if fallback is None:
                    return window.start
                usable = max(floor, planned.get(window.start, fallback))
                desired = float(min(100, ceil(battery.effective_min_soc + usable * span / 100)))
                original = self._lowered.get(key)
                if desired > reference - _TARGET_DEADBAND_PERCENT:
                    # A change smaller than the deadband is not worth the pass's
                    # one external write. When the plan keeps more energy than an
                    # earlier pass did, raise a target we lowered back towards,
                    # never above, the aggregator's own value; a full restore
                    # always runs, a small partial raise waits.
                    if original is None:
                        continue
                    desired = min(desired, original)
                    if desired <= reference + 0.5 or (
                        desired < original - 0.5
                        and desired - reference < _TARGET_DEADBAND_PERCENT
                    ):
                        continue
                    allow_delete = False
                else:
                    allow_delete = window.start not in self._aps_held_starts
            elif key in self._lowered:
                if key in self._transient_refusals:
                    # Refused for a passing reason in this pass: it only stopped
                    # being held because of that, so nothing is restored now.
                    continue
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
            if window.start in self._aps_held_starts and desired < reference:
                self._lowered.setdefault(key, reference)
                save = getattr(self.runtime, "async_save_lowered_targets", None)
                if save is not None:
                    await save(dict(self._lowered))
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
                    delete=replace_window,
                )
            except Exception as error:  # noqa: BLE001 - refused before send; plan around it
                result = {"status": "refused", "reason": str(error) or type(error).__name__}
            status = result.get("status")
            self._target_cache.pop(key, None)
            when = time_span(window.start, window.end)
            if status == "adjusted":
                if window.adjustable and desired > reference:
                    original = self._lowered.get(key, desired)
                    if desired >= original - 0.5:
                        self._lowered.pop(key, None)
                    self.external_target_adjust_count += 1
                    log_activity(self.runtime, PRICE_STATUS,
                                 f"Raised aggregator Target SOC {when} from {reference:.0f} % "
                                 f"to {desired:.0f} % (the aggregator set {original:.0f} %)",
                                 "Who: price optimization, with Allow changing aggregator "
                                 "schedules on. Why: an earlier plan lowered this target, but "
                                 "the current plan keeps more energy in the battery, so the "
                                 "target is raised to the planned level, never above the "
                                 "aggregator's own value.")
                elif window.adjustable:
                    self._lowered.setdefault(key, reference)
                    self.external_target_adjust_count += 1
                    log_activity(self.runtime, PRICE_STATUS,
                                 f"Lowered aggregator Target SOC {when} from {reference:.0f} % "
                                 f"to {desired:.0f} % to postpone more expensive recharge",
                                 "Who: price optimization, with Allow changing aggregator "
                                 "schedules on. Why: the aggregator target would recharge "
                                 "from the grid after verified APS discharge or between a "
                                 "planned discharge and the next "
                                 "planned charge; it is lowered to the planned level, never "
                                 "below your minimum SOC.")
                else:
                    self._lowered.pop(key, None)
                    log_activity(self.runtime, PRICE_STATUS,
                                 f"Restored aggregator Target SOC {when} to {desired:.0f} %")
            elif status == "deleted" and replace_window:
                self.deleted_external.add((schedule.sch_index, schedule.start_epoch))
                self._lowered.pop(key, None)
                self.external_target_delete_count += 1
                log_activity(self.runtime, PRICE_STATUS,
                             f"Replaced aggregator Target SOC {when} ({reference:.0f} %) "
                             "with the price plan",
                             "Who: price optimization, with Allow changing aggregator "
                             "schedules on. Why: the price plan uses this time itself "
                             "(see the new plan) and earns more than the aggregator's "
                             "Target SOC, so the aggregator schedule was deleted.")
            elif status == "deleted":
                self.deleted_external.add((schedule.sch_index, schedule.start_epoch))
                self._lowered.pop(key, None)
                self.external_target_delete_count += 1
                log_activity(self.runtime, PRICE_STATUS,
                             f"Deleted aggregator Target SOC {when} ({reference:.0f} %); "
                             "the Gateway kept the old target")
            elif status == "refused" and window.adjustable:
                reason = str(result.get("reason"))
                # One entry per reason: the same cause usually hits every target.
                repeated = reason in self._refused_targets.values()
                self._refused_targets[key] = reason
                if not _lasting_refusal(reason):
                    self._transient_refusals.add(key)
                _LOGGER.warning(
                    "Aggregator Target SOC %s could not be changed: %s", when, reason
                )
                if not repeated:
                    log_activity(self.runtime, PRICE_STATUS,
                                 f"Aggregator Target SOC {when} cannot be changed "
                                 f"({reason}); it is kept and the plan is made around it"
                                 + ("" if _lasting_refusal(reason)
                                    else "; the next pass tries again"),
                                 "Who: price optimization, with Allow changing aggregator "
                                 "schedules on. Why: the change was refused before anything "
                                 "was sent, so the aggregator target stays as it is and the "
                                 "battery is planned to follow it. Later targets refused for "
                                 "the same reason are not logged again.")
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

    def _log_create_wait(self, action: PricePlanAction) -> None:
        """Say once per busy period that a new schedule waits for idle."""
        period = (self.current_service, self.busy_until)
        if period == self._create_wait_logged:
            return
        self._create_wait_logged = period
        until = f" (ends {clock(self.busy_until)})" if self.busy_until is not None else ""
        lead = self.config.minimum_lead_minutes
        log_activity(
            self.runtime, PRICE_STATUS,
            f"Waiting to schedule {_action_text(action)}: "
            f"{service_type_label(self.current_service)} is running{until}",
            "Who: price optimization. Why: another service or schedule runs on the "
            "battery, and the safety policy allows new schedules only while the "
            "battery is idle. Removals and aggregator target changes can still run. "
            "The schedule is written as soon as the battery is idle, if it then "
            f"still starts at least {lead} min later; otherwise the plan moves the "
            "energy to later intervals.",
        )

    def _report_blocked(self, what: str, result: Any) -> None:
        """Say once which safety checks stopped a write, until they change."""
        details = tuple(result.blocked_reasons)
        self.blocked_details = list(details)
        if details == self._reported_block:
            return
        self._reported_block = details
        if not details:
            return
        _LOGGER.warning("Price optimization could not %s: %s", what, ", ".join(details))
        log_activity(self.runtime, PRICE_STATUS,
                     f"Could not {what}: blocked by {', '.join(details)}",
                     "Who: price optimization. Why: a safety check stopped the write "
                     "before anything was sent; planning retries every pass and this "
                     "is logged again only when the reasons change.")

    def _prune_refused_targets(self) -> None:
        now = datetime.now(UTC).timestamp()
        self._refused_targets = {
            key: reason for key, reason in self._refused_targets.items()
            if key[2] + key[3] > now and key not in self._transient_refusals
        }
        self._transient_refusals.clear()

    async def _reconcile(
        self,
        actions: tuple[PricePlanAction, ...],
        keep: tuple[PricePlanAction, ...] = (),
    ) -> bool:
        """Write ``actions``; remove own schedules that are in neither list.

        ``keep`` is the whole plan: a schedule written earlier that the plan
        still contains stays, even when this pass writes only part of the plan
        (first cycle, a busy period or a refused aggregator target).
        """
        manager = self.runtime.scheduler_mutation_manager
        if manager is None:
            self._publish("blocked", "price_scheduler_mutation_unavailable")
            return False
        desired = {_action_key(action): action for action in actions}
        planned = set(desired) | {_action_key(action) for action in keep}
        existing = {_record_key(record): record for record in self._price_records()}
        # A written self-use discharge whose interval is unchanged stays when
        # the plan only changes its power cap (0.7 <-> 10 kW): it covers the
        # house load either way, and rewriting costs a removal the Gateway
        # never confirms plus a pause for the post-check.
        for key, action in tuple(desired.items()):
            if key in existing or not _self_use(action.service, action.values):
                continue
            same = next(
                (
                    (old_key, record) for old_key, record in existing.items()
                    if old_key not in planned
                    and record.service == action.service
                    and _record_start(record) == action.start.astimezone(UTC).replace(
                        microsecond=0
                    )
                    and record.intended_state.get("duration") == action.duration_seconds
                    and _self_use(record.service, record.intended_state.get("values"))
                ),
                None,
            )
            if same is None:
                continue
            old_key, record = same
            del desired[key]
            planned.add(old_key)
            if record.m_id not in self._kept_logged:
                self._kept_logged.add(record.m_id)
                log_activity(
                    self.runtime, PRICE_STATUS,
                    f"Kept {_describe(record)}: the plan only changed its power cap "
                    f"to {action.power_kw:.1f} kW",
                    "Who: price optimization. Why: a self-use discharge covers the "
                    "house load whatever its cap, so the written schedule is kept "
                    "instead of removing and rewriting it.",
                )
        creates = [action for key, action in desired.items() if key not in existing]
        if creates and self.current_service not in (0, 105):
            # The safety policy allows removals but no new schedule while
            # another service runs. Removing first would leave nothing booked
            # (8 Nov 14:35), so the whole change waits for idle.
            self._log_create_wait(creates[0])
            self._publish("waiting_for_priority", "price_create_waits_for_idle")
            return False
        self._kept_logged &= {record.m_id for record in existing.values()}
        now = datetime.now(UTC)
        for key, record in tuple(existing.items()):
            if key in planned:
                continue
            start = _record_start(record)
            if start is None or start <= now:
                continue
            if record.m_id in self._binding_ids:
                if record.m_id not in self._kept_logged:
                    self._kept_logged.add(record.m_id)
                    lead = self.config.minimum_lead_minutes
                    log_activity(
                        self.runtime, PRICE_STATUS,
                        f"Kept {_describe(record)}: it starts within {lead} min",
                        "Who: price optimization. Why: its own schedule starts within "
                        f"the minimum lead time ({lead} min), so it is no longer "
                        "replanned or removed; the plan counts on its energy and "
                        "plans around it. Turning the feature or control authority "
                        "off still removes it.",
                    )
                continue
            if not await self._mutation_context_safe():
                return False
            result = await manager.delete(record.m_id)
            if result.status is not SchedulerTransactionStatus.SUCCESS:
                if not result.blocked_reasons:
                    # Settle it with a read-only check soon, not at the next
                    # 15-min list refresh.
                    self._conflicts_seen.setdefault(record.m_id, datetime.now(UTC))
                    self._wake_later(_POST_CHECK_DELAY_SECONDS)
                    # Sent, but the outcome is unknown: say why it was sent; the
                    # read-only post-check reports how it ended.
                    log_activity(self.runtime, PRICE_STATUS,
                                 f"Removing {_describe(record)} (no longer in the plan): "
                                 f"the Gateway did not confirm it ({result.reason})",
                                 "Who: price optimization. Why: the plan was recalculated "
                                 "and this interval is no longer profitable or needed. "
                                 "Price writes pause until a read-only check shows "
                                 "whether it is gone; it is never sent again.")
                self._report_blocked(f"remove {_describe(record)}", result)
                self._publish("blocked", f"price_delete_{result.reason}")
                return False
            self.delete_count += 1
            log_activity(self.runtime, PRICE_STATUS,
                         f"Removed {_describe(record)} (no longer in the plan)",
                         "Who: price optimization. Why: the plan was recalculated with new "
                         "prices, SOC or forecast and this interval is no longer profitable "
                         "or needed; its own schedule was removed before it started.")
        for key, action in desired.items():
            if key in existing:
                continue
            lead_limit = datetime.now(UTC) + timedelta(minutes=self.config.minimum_lead_minutes)
            if action.start <= lead_limit:
                self._publish("blocked", "price_action_lead_time_elapsed")
                return False
            if not await self._mutation_context_safe():
                return False
            if self.current_service not in (0, 105):
                # The safety policy refuses every new schedule while another
                # service runs; wait for idle instead of reporting a block.
                self._log_create_wait(action)
                self._publish("waiting_for_priority", "price_create_waits_for_idle")
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
                self._report_blocked(f"create {_action_text(action)}", result)
                self._publish("blocked", f"price_create_{result.reason}")
                return False
            self.create_count += 1
            now = datetime.now(UTC)
            self.created_actions = {
                k: a for k, a in self.created_actions.items() if a.end > now
            }
            self.created_actions[key] = action
            log_activity(
                self.runtime, PRICE_STATUS,
                f"Scheduled {action.direction.value} {time_span(action.start, action.end)} "
                f"at {action.power_kw:.1f} kW, price {action.price_sek_per_kwh:.2f} kr/kWh"
                + (
                    "; the profit is counted on the discharge that uses it"
                    if action.direction is PriceDirection.CHARGE
                    else f", expected net profit {action.net_profit_sek_per_kwh:.2f} kr/kWh"
                ),
                _action_cause(action, self.planner_engine, self.runtime.operational_settings),
            )
        return True


def _self_use(service: object, values: object) -> bool:
    """A peak-shaving discharge without export (follows the house load)."""
    return (
        service == "peakshaving" and isinstance(values, Mapping)
        and values.get("exportlim") == 0 and values.get("charge_max") == 0
        and (values.get("discharge_max") or 0) > 0
    )


def _applied_cycle(
    actions: tuple[PricePlanAction, ...], gap: timedelta,
) -> tuple[tuple[PricePlanAction, ...], datetime | None]:
    """Actions written now and the end of the applied cycle.

    Everything up to the first discharge is written, plus every action that
    starts within ``gap`` of the previous one's end. Nothing may be written
    while one of our own schedules runs, so an action right after it would
    pass the lead time before it could be written and be lost. Without a
    discharge every action is written and the end is ``None``.
    """
    ordered = sorted(actions, key=lambda action: action.start)
    first = next(
        (i for i, a in enumerate(ordered) if a.direction is PriceDirection.DISCHARGE), None,
    )
    if first is None:
        return actions, None
    end = ordered[first].end
    for action in ordered[first + 1:]:
        if action.start > end + gap:
            break
        end = max(end, action.end)
    return tuple(a for a in actions if a.end <= end), end


def _action_text(action: PricePlanAction) -> str:
    mode = ""
    if action.discharge_mode is not None:
        mode = " " + action.discharge_mode.value.replace("_", " ")
    return (
        f"{action.direction.value}{mode} {time_span(action.start, action.end)} "
        f"{action.power_kw:.1f} kW at {action.price_sek_per_kwh:.2f} kr/kWh"
    )


def _action_cause(action: PricePlanAction, engine: str, settings: Any) -> str:
    planner = "whole-day" if engine == "whole_day" else "standard"
    minimum = float(settings.get("price_min_difference_sek_per_kwh"))
    if action.direction is PriceDirection.CHARGE:
        why = (
            f"charge now at {action.price_sek_per_kwh:.2f} kr/kWh to use the energy later "
            f"at about {action.reference_price_sek_per_kwh:.2f} kr/kWh"
        )
    else:
        mode = "covering the house" if action.discharge_mode is not None and (
            action.discharge_mode.value == "self_consumption"
        ) else "exporting to the grid"
        why = (
            f"use stored energy now at {action.price_sek_per_kwh:.2f} kr/kWh by {mode}; "
            f"it was charged at about {action.reference_price_sek_per_kwh:.2f} kr/kWh"
        )
    if action.direction is PriceDirection.CHARGE:
        profit = (
            "A charge earns nothing by itself: its profit is counted on the discharge "
            f"that uses the energy, which must net at least your minimum {minimum:.2f} "
            "kr/kWh after losses and costs"
        )
    else:
        profit = (
            f"Net profit {action.net_profit_sek_per_kwh:.2f} kr/kWh after losses and "
            f"costs, your minimum is {minimum:.2f} kr/kWh"
        )
    return (
        f"Who: price optimization, {planner} planner. Why: {why}. {profit}. "
        f"Battery SOC after this interval about "
        f"{action.projected_usable_soc_percent:.0f} %."
    )


def best_net_profit(
    series: PriceSeries, now: datetime, efficiency: float, import_cost: float, cycle_cost: float,
) -> float | None:
    """Highest self-consumption net profit per kWh: charge first, use later."""
    prices = [interval.price_sek_per_kwh for interval in series.intervals if interval.end > now]
    if len(prices) < 2 or efficiency <= 0:
        return None
    best: float | None = None
    cheapest = prices[0]
    for price in prices[1:]:
        profit = (price + import_cost) - (cheapest + import_cost) / efficiency - cycle_cost
        best = profit if best is None else max(best, profit)
        cheapest = min(cheapest, price)
    return best


# Refusals that stay true for the target's lifetime. Anything else (the
# safety policy, an ownership check after an unconfirmed removal, a list that
# could not be read) can clear by the next pass, so it is retried then.
_LASTING_TARGET_REFUSALS = frozenset({
    "scheduler_external_target_shared",
    "scheduler_external_target_identity_invalid",
    "scheduler_external_target_reference_invalid",
    "scheduler_soc_service_unsupported",
    "scheduler_soc_value_invalid",
    "scheduler_installer_access_required",
})


def _lasting_refusal(reason: str) -> bool:
    return any(part.strip() in _LASTING_TARGET_REFUSALS for part in reason.split(","))


def _covered_window_price(series: PriceSeries, window: ExternalWindow) -> float | None:
    """Duration-weighted price only when every second of a window is priced."""
    cursor, total = window.start, 0.0
    for item in sorted(series.intervals, key=lambda item: item.start):
        if item.end <= cursor:
            continue
        if item.start > cursor:
            return None
        end = min(window.end, item.end)
        total += (end - cursor).total_seconds() * item.price_sek_per_kwh
        cursor = end
        if cursor >= window.end:
            return total / (window.end - window.start).total_seconds()
    return None


def _window_price(series: PriceSeries, window: ExternalWindow) -> float:
    """Mean price while an external window runs (unknown prices sort last)."""
    inside = [item.price_sek_per_kwh for item in series.intervals
              if item.start < window.end and window.start < item.end]
    return sum(inside) / len(inside) if inside else float("inf")


def _target_key(schedule: Any) -> tuple[int, int, int, int]:
    return (schedule.sch_index, schedule.service_no,
            schedule.start_epoch, schedule.duration_seconds)


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


def _record_matches_schedule(
    record: SchedulerOwnershipRecord,
    schedule: Any,
    schedules: Iterable[Any] = (),
) -> bool:
    """Whether a Gateway list entry is this owned price record.

    The Gateway list carries no owner, so an explicit integration ownership is
    rarely available. The fallback accepts an entry only when the record is
    OWNED (its own keyed readback proved it exists), the entry is a complete,
    consistent one-shot with exactly the record's start, duration and service,
    and no other enabled entry in the list has that shape. Two identical
    entries stay ambiguous and keep planning blocked.
    """
    if not (
        _record_has_schedule_shape(record, schedule)
        and schedule.recurrence.recurring is False
        and schedule.completeness is ScheduleCompleteness.COMPLETE
        and schedule.consistency is ScheduleConsistency.CONSISTENT
    ):
        return False
    if (
        # Explicit proof: integration ownership and the same identity.
        schedule.ownership.ownership is Ownership.INTEGRATION_OWNED
        and schedule.service_no == record.m_id
    ):
        return True
    if record.lifecycle is not OwnershipLifecycle.OWNED:
        return False
    if schedule.ownership.ownership is Ownership.EXTERNAL:
        return False
    same_shape = [
        item for item in schedules
        if item.enabled and _record_has_schedule_shape(record, item)
    ]
    return len(same_shape) == 1 and same_shape[0] is schedule


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


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if isfinite(number) else None


def _price_at_epoch(series: PriceSeries, epoch: int) -> float | None:
    instant = datetime.fromtimestamp(epoch, UTC)
    for interval in series.intervals:
        if interval.start <= instant < interval.end:
            return interval.price_sek_per_kwh
    return None


def _segments(result: WholeDayResult) -> list[dict[str, object]]:
    """Compact the whole-day slots into runs of the same kind (for attributes)."""
    segments: list[dict[str, object]] = []
    for slot in result.slots:
        kind = slot.kind
        if (
            segments
            and segments[-1]["kind"] == kind
            and segments[-1]["performed_by"] == slot.performed_by
            and segments[-1]["end"] == slot.start
        ):
            last = segments[-1]
            last["end"] = slot.end
            last["energy_kwh"] = float(last["energy_kwh"]) + slot.battery_kw * (  # type: ignore[arg-type]
                (slot.end - slot.start).total_seconds() / 3600
            )
            last["end_soc"] = round(slot.usable_soc_percent, 1)
            continue
        segments.append({
            "start": slot.start,
            "end": slot.end,
            "kind": kind,
            "performed_by": slot.performed_by,
            "energy_kwh": slot.battery_kw * (slot.end - slot.start).total_seconds() / 3600,
            "end_soc": round(slot.usable_soc_percent, 1),
        })
    return [
        {
            **segment,
            "start": segment["start"].isoformat(),  # type: ignore[union-attr]
            "end": segment["end"].isoformat(),  # type: ignore[union-attr]
            "energy_kwh": round(float(segment["energy_kwh"]), 2),  # type: ignore[arg-type]
        }
        for segment in segments
        if segment["kind"] != "idle"
    ][:40]


def _notification_id(runtime: Any) -> str:
    return f"pixiihome_controller_{runtime.serial}_price_blocked"


def _create_notification(runtime: Any, message: str) -> None:
    try:
        from homeassistant.components import persistent_notification

        persistent_notification.async_create(
            runtime.hass, message, title="Pixii: prisoptimeringen står still",
            notification_id=_notification_id(runtime),
        )
    except Exception:  # noqa: BLE001 - a notification must never affect control
        return


def _dismiss_notification(runtime: Any) -> None:
    try:
        from homeassistant.components import persistent_notification

        persistent_notification.async_dismiss(runtime.hass, _notification_id(runtime))
    except Exception:  # noqa: BLE001 - a notification must never affect control
        return


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
