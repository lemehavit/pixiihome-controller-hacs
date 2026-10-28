"""Automatic price optimization through owned, future Pixii schedules."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable, Iterable, Mapping
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
    merge_adjacent_actions,
    normalize_price_series,
)
from .control.whole_day_planner import WholeDayResult, build_whole_day_plan
from .price_guard import PriceSocGuard
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
        self._target_cache: dict[tuple[int, int, int, int], float] = {}
        # Foreign Target SOC schedules aligned with ``_external_windows``.
        self._external_meta: tuple[tuple[Any, float | None], ...] = ()
        # Original references of foreign targets this controller lowered.
        self._lowered: dict[tuple[int, int, int, int], float] = {}
        self._lowered_loaded = False
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
        self.whole_day: WholeDayResult | None = None
        self.standard_plan_margin_sek: float | None = None
        self.planner_engine = "standard"
        self._logged_plan: tuple[object, ...] | None = None
        self.problem_since: datetime | None = None
        self.problem_notified = False

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
        if status == "blocked" and reason != self._logged_block_reason:
            # Every new block is in the Home Assistant log at once, not only
            # after the notification limit.
            self._logged_block_reason = reason
            _LOGGER.warning("Price optimization blocked: %s (%s)", reason,
                            reason_text(reason) if reason else "")
        elif status != "blocked":
            self._logged_block_reason = None
        if status not in {"blocked", "waiting_telemetry"}:
            if self.problem_notified:
                _dismiss_notification(self.runtime)
                log_activity(self.runtime, PRICE_STATUS, "Price optimization is running again")
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
                     f"Price optimization has been stopped for over {hours} h: {text}")
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
                "cost_without_battery_sek": round(whole_day.baseline_cost_sek, 2),
                "planned_cost_sek": round(whole_day.planned_cost_sek, 2),
                "action_count": len(merge_adjacent_actions(whole_day.plan.actions)),
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
                    self._report_blocked(f"remove {_describe(record)}", result)
                    self._publish("blocked", f"price_delete_{result.reason}")
                    return
                self.delete_count += 1
                log_activity(self.runtime, PRICE_STATUS, f"Removed {_describe(record)} "
                             "(price optimization turned off)",
                             "Who: price optimization. Why: the switch Automatic price "
                             "optimization was turned off, so its own schedules are removed.")
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
            recovered = await mutation_manager.recover_conflicted_price_delete(
                conflicted.m_id,
                absent_from_inventory=self._absent_from_inventory(conflicted, seen),
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
        if not self.runtime.controller_state.automatic_price_optimization_enabled:
            self._publish("disabled")
            return
        # The simulator runtime has no calibration hold.
        calibrating = getattr(self.runtime, "battery_calibration_active", None)
        if calibrating is not None and calibrating():
            self._publish("waiting_for_priority", "battery_calibration_running")
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
        # cycle. Later cycles remain proposals until fresh SoC is measured.
        first_discharge = next(
            (a for a in actions if a.direction is PriceDirection.DISCHARGE), None,
        )
        if first_discharge is not None:
            actions = tuple(a for a in actions if a.end <= first_discharge.end)
        if not await self._reconcile(actions):
            return
        self.last_applied_at = datetime.now(UTC)
        self._reported_block = ()
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
        standard = build_price_plan(
            series,
            **arguments,
            external_windows=windows,
            net_load_kw=(
                None if forecast is None
                else lambda start, end: forecast.net_load_kw(start, end, high=high)
            ),
        )
        self.standard_plan_margin_sek = standard.expected_margin_sek
        plan = standard
        self.whole_day = None
        if forecast is not None:
            try:
                self.whole_day = build_whole_day_plan(
                    series,
                    **arguments,
                    external_windows=windows,
                    forecast=lambda start, end: forecast.net_forecast(start, end, high=high),
                )
            except ValueError:
                self.whole_day = None
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
            and _target_key(meta[0]) not in self._refused_targets
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
        if recharge is not None:
            until = recharge.start
        else:
            # No own charge: the battery is refilled by a later aggregator
            # window. Hold the windows up to the cheapest of them, so the
            # energy is bought back there instead of at the first, often
            # still expensive one.
            last = max(a.end for a in discharges)
            later = [w for w in self._external_windows if w.start >= last]
            until = (
                min(later, key=lambda w: (_window_price(series, w), w.start)).start
                if later else last
            )
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
        first_discharge = next(
            (a for a in plan.actions if a.direction is PriceDirection.DISCHARGE), None
        )
        replace_until = max(
            first_discharge.end if first_discharge is not None else lead,
            lead + timedelta(minutes=self.config.refresh_minutes * 1.5),
        )
        for window, meta in zip(windows, self._external_meta, strict=True):
            if meta is None or window.start <= lead:
                continue
            schedule, reference = meta[0], meta[1]
            if reference is None:
                continue
            key = _target_key(schedule)
            replace_window = (
                window.adjustable
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
                usable = max(floor, planned.get(window.start, floor))
                desired = float(min(100, ceil(battery.effective_min_soc + usable * span / 100)))
                original = self._lowered.get(key)
                if desired >= reference - 0.5:
                    # The plan keeps more energy than an earlier pass did: raise
                    # a target we lowered back towards, never above, the
                    # aggregator's own value.
                    if original is None:
                        continue
                    desired = min(desired, original)
                    if desired <= reference + 0.5:
                        continue
                    allow_delete = False
                else:
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
                                 f"to {desired:.0f} % to keep the planned discharge",
                                 "Who: price optimization, with Allow changing aggregator "
                                 "schedules on. Why: the aggregator target would recharge "
                                 "from the grid between a planned discharge and the next "
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
                if not result.blocked_reasons:
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
                f"at {action.power_kw:.1f} kW, price {action.price_sek_per_kwh:.2f} kr/kWh, "
                f"expected net profit {action.net_profit_sek_per_kwh:.2f} kr/kWh",
                _action_cause(action, self.planner_engine, self.runtime.operational_settings),
            )
        return True


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
    return (
        f"Who: price optimization, {planner} planner. Why: {why}. Net profit "
        f"{action.net_profit_sek_per_kwh:.2f} kr/kWh after losses and costs, your minimum is "
        f"{minimum:.2f} kr/kWh. Battery SOC after this interval about "
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
        if segments and segments[-1]["kind"] == kind and segments[-1]["end"] == slot.start:
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
