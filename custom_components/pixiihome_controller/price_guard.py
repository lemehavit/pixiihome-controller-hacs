"""Live, identity-bound SOC supervision for integration-owned price intervals."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Any

from .activity import log_activity
from .scheduler.mutations import SchedulerTransactionStatus
from .scheduler.ownership import OwnershipLifecycle, SchedulerOwnershipRecord

if TYPE_CHECKING:
    from .runtime import RuntimeData

PRICE_OWNER = "automatic_price_optimization"
GUARD_INTERVAL_SECONDS = 2
PREFLIGHT_SECONDS = 30


def price_direction(record: SchedulerOwnershipRecord) -> str | None:
    if record.owner != PRICE_OWNER or record.lifecycle is not OwnershipLifecycle.OWNED:
        return None
    values = record.intended_state.get("values", {})
    if record.service == "demandresponse":
        power = values.get("pacref")
        if type(power) in {int, float} and isfinite(power) and power != 0:
            return "charge" if power > 0 else "discharge"
    if record.service == "peakshaving" and values.get("charge_max") == 0:
        if type(values.get("discharge_max")) in {int, float} and values["discharge_max"] > 0:
            return "discharge"
    return None


class PriceSocGuard:
    """Supervise actual usable SOC independently of planner enable/writes gates.

    Disabling future planning must not leave a currently active owned interval
    unprotected. Foreign/manual schedules never enter this target set.
    """

    def __init__(self, runtime: RuntimeData) -> None:
        self.runtime = runtime
        self.status = "standby"
        self.reason: str | None = None
        self.stops = 0
        self.last_message_id: int | None = None
        self.last_stop_at: datetime | None = None
        self.last_stop_observation: dict[str, Any] | None = None
        self._attempted: set[int] = set()
        self._task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.validation_bounds: dict[int, tuple[float, float]] = {}
        self.validation_lock = asyncio.Lock()

    def attributes(self) -> dict[str, Any]:
        return {
            "soc_guard_status": self.status,
            "soc_guard_reason": self.reason,
            "soc_guard_poll_seconds": GUARD_INTERVAL_SECONDS,
            "soc_guard_stops": self.stops,
            "soc_guard_last_message_id": self.last_message_id,
            "soc_guard_last_stop_at": self.last_stop_at.isoformat() if self.last_stop_at else None,
            "soc_guard_last_stop_observation": self.last_stop_observation,
            "soc_guard_requires_home_assistant_running": True,
        }

    def start(self) -> None:
        if self._task is None and callable(
            getattr(self.runtime.hass, "async_create_background_task", None)
        ):
            self._task = self.runtime.hass.async_create_background_task(
                self._run(),
                "pixiihome_controller price SOC supervision",
            )

    async def stop(self) -> None:
        if self._task is not None:
            # Let a journaled stop finish before cancelling the task.
            async with self._lock:
                pass
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status, self.reason = "blocked", "price_soc_guard_failed"
            controller = self.runtime.price_optimization_controller
            if controller is not None:
                for listener in tuple(controller._listeners):
                    listener()
            await asyncio.sleep(GUARD_INTERVAL_SECONDS)

    async def tick(self, now: datetime | None = None) -> None:
        from .price_controller import _record_start

        async with self._lock:
            now = now or datetime.now(UTC)
            ownership = self.runtime.scheduler_ownership
            manager = self.runtime.scheduler_mutation_manager
            if ownership is None or manager is None:
                self.status, self.reason = "blocked", "price_scheduler_ownership_unavailable"
                return
            settings = self.runtime.operational_settings
            policy = self.runtime._automation_policy()
            low = max(float(settings.get("price_min_soc_percent")), policy.soc_min_percent)
            high = min(float(settings.get("price_max_soc_percent")), policy.soc_max_percent)
            soc = self.runtime.battery.usable_soc
            soc_valid = soc is not None and isfinite(soc) and 0 <= soc <= 100
            self.status, self.reason = "standby", None
            for record in tuple(ownership.records.values()):
                if (
                    record.owner == PRICE_OWNER
                    and record.lifecycle is OwnershipLifecycle.CONFLICTED
                    and record.m_id in self._attempted
                ):
                    self.status, self.reason = "recovery_required", "price_soc_stop_not_retried"
                    continue
                record_low, record_high = self.validation_bounds.get(record.m_id, (low, high))
                direction = price_direction(record)
                start = _record_start(record)
                duration = record.intended_state.get("duration")
                if direction is None or start is None or type(duration) is not int or duration <= 0:
                    continue
                end = start + timedelta(seconds=duration)
                if end <= now or start > now + timedelta(seconds=PREFLIGHT_SECONDS):
                    continue
                if record.m_id in self._attempted:
                    self.status, self.reason = "recovery_required", "price_soc_stop_not_retried"
                    continue
                # Stop slightly before the requested bound to cover meter
                # quantization and command/telemetry latency. Use actual or
                # requested power, whichever is larger, for this reserve.
                values = record.intended_state.get("values", {})
                power_w = abs(float(values.get("pacref", values.get("discharge_max", 0))))
                actual = self.runtime.battery.power_w
                if actual is not None and isfinite(actual):
                    power_w = max(power_w, abs(actual))
                age_reader = getattr(self.runtime.telemetry, "age", None)
                age = age_reader("battery", now) if callable(age_reader) else None
                age_seconds = age.total_seconds() if isinstance(age, timedelta) else 0
                capacity = float(settings.get("price_usable_capacity_kwh"))
                margin = 0.1 + (
                    power_w / 1000 * (15 + age_seconds) / 3600 / capacity * 100
                    if capacity > 0
                    else 1
                )
                reason = (
                    "maximum_usable_soc_reached"
                    if soc_valid
                    and soc is not None
                    and direction == "charge"
                    and soc >= record_high - margin
                    else "minimum_usable_soc_reached"
                    if soc_valid
                    and soc is not None
                    and direction == "discharge"
                    and soc <= record_low + margin
                    else None
                )
                if (
                    not soc_valid
                    or low >= high
                    or not self.runtime.plant_health().healthy
                    or callable(age_reader)
                    and (age is None or age_seconds > 15)
                ):
                    reason = "price_soc_guard_telemetry_not_safe"
                if reason is None:
                    self.status = "monitoring"
                    continue
                self.last_message_id = record.m_id
                self.last_stop_observation = None
                result = await manager.stop_owned_price(record.m_id)
                self.last_stop_observation = getattr(result, "price_stop_observation", None)
                if result.status in {
                    SchedulerTransactionStatus.SUCCESS, SchedulerTransactionStatus.OUTPUT_STOPPED,
                }:
                    self._attempted.add(record.m_id)
                    self.stops += 1
                    self.last_stop_at = now
                    self.status, self.reason = "stopped", reason
                    log_activity(
                        self.runtime, "price_optimization_status",
                        f"SOC guard stopped the price {direction} at {soc:.0f} % usable SOC "
                        f"({reason.replace('_', ' ')})"
                        if soc is not None and reason is not None
                        else f"SOC guard stopped the price {direction}",
                    )
                    controller = self.runtime.price_optimization_controller
                    if controller is not None:
                        controller.request_refresh()
                else:
                    self.status, self.reason = "blocked", result.reason
                    # These outcomes performed no write and may be re-evaluated.
                    if (
                        result.status is not SchedulerTransactionStatus.BLOCKED
                        and result.reason
                        not in {
                            "price_stop_readback_unavailable",
                            "price_stop_identity_unverified",
                        }
                    ):
                        self._attempted.add(record.m_id)
