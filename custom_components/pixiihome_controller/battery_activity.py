"""What the battery is doing right now, who asked for it and why.

Read-only: telemetry, the last scheduler inventory and the controllers' own
state are combined into one activity description. Every change is written to
the Home Assistant logbook on the Battery activity sensor. Nothing here reads
the Gateway or sends commands.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

from .activity import log_activity, span
from .protocol.enums import service_type_label

STATUS_KEY = "battery_activity"
SAMPLE_SECONDS = 15
# A new charge/discharge/idle direction must hold this long before it is logged,
# so a house load flickering around zero does not flood the logbook.
DIRECTION_HOLD = timedelta(minutes=3)
THRESHOLD_W = 200.0
RECENT_LIMIT = 30

OWNER_NAMES = {
    "automatic_price_optimization": "price optimization",
    "automatic_solar": "solar charging",
    "adaptive_peak_shaving": "adaptive peak shaving",
}
SERVICE_NUMBERS = {
    "demandresponse": 102,
    "peakshaving": 104,
    "adaptivepeakshaving": 105,
    "targetsoc": 107,
}


@dataclass(frozen=True, slots=True)
class Cause:
    """Who started the running service and why, as far as the evidence shows."""

    source: str
    detail: str
    window: str | None = None

    def text(self) -> str:
        parts = [self.source]
        if self.window:
            parts.append(self.window)
        if self.detail:
            parts.append(self.detail)
        return ", ".join(parts)


def direction_of(power_w: float | None) -> str | None:
    if power_w is None:
        return None
    if power_w > THRESHOLD_W:
        return "charging"
    if power_w < -THRESHOLD_W:
        return "discharging"
    return "idle"


class BatteryActivityTracker:
    """Samples the battery every 15 s and logs each change with its cause."""

    def __init__(self, runtime: Any, clock_fn: Callable[[], datetime] | None = None) -> None:
        self.runtime = runtime
        self.clock = clock_fn or (lambda: datetime.now(UTC))
        self.service: int | None = None
        self.direction: str | None = None
        self.power_w: float | None = None
        self.soc: float | None = None
        self.grid_w: float | None = None
        self.cause: Cause | None = None
        self.since: datetime | None = None
        self.recent: deque[tuple[str, str]] = deque(maxlen=RECENT_LIMIT)
        self._pending: tuple[str, datetime] | None = None
        self._external: dict[tuple[int, int, int], tuple[str, int]] | None = None
        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task[None] | None = None

    # ----- lifecycle ---------------------------------------------------------------

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def start(self) -> None:
        if self._task is None:
            self._task = self.runtime.hass.async_create_background_task(
                self._run(), "pixiihome_controller battery activity"
            )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                self.sample()
            except Exception:  # noqa: BLE001 - activity logging must never stop
                pass
            await asyncio.sleep(SAMPLE_SECONDS)

    # ----- sampling ----------------------------------------------------------------

    def sample(self) -> None:
        now = self.clock()
        telemetry = self.runtime.telemetry
        battery = self.runtime.battery
        raw_service = telemetry.value("core", "srv_type")
        service = raw_service if type(raw_service) is int else None
        fresh = telemetry.is_fresh("battery")
        self.power_w = _number(battery.power_w) if fresh else None
        self.soc = _number(battery.usable_soc) if fresh else None
        meter = telemetry.value("meter", "meter_w")
        self.grid_w = _number(meter) if telemetry.is_fresh("meter") else None
        changed = False
        self._track_external_schedules(now)
        direction = direction_of(self.power_w)
        if service not in (None, 0) and self._snapshot() is None:
            # Right after a start the schedule list is not read yet; naming
            # the cause now would wrongly say "not started by this integration".
            service = None
        if service is not None and service != self.service:
            first = self.service is None
            self.service = service
            self.cause = self._cause(service, now)
            self.since = now
            # A service change explains the new direction; no separate entry.
            if direction is not None:
                self.direction, self._pending = direction, None
            if not first or service != 0:
                self._log(now, self._service_text(service), self._why())
            changed = True
        if direction is not None and direction != self.direction:
            if self.direction is None:
                self.direction = direction
                changed = True
            elif self._pending is None or self._pending[0] != direction:
                self._pending = (direction, now)
            elif now - self._pending[1] >= DIRECTION_HOLD:
                self.direction = direction
                self._pending = None
                if self.service is not None:
                    self.cause = self._cause(self.service, now)
                self._log(now, self._direction_text(direction), self._why())
                changed = True
        elif direction == self.direction:
            self._pending = None
        if changed:
            self._notify()

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def _log(self, now: datetime, message: str, cause: str | None = None) -> None:
        self.recent.appendleft((now.isoformat(), message))
        log_activity(self.runtime, STATUS_KEY, message, cause)

    def _why(self) -> str:
        """Who made the battery do this and why, in full sentences."""
        measured = self._measurements()
        tail = f" Measured: {measured}." if measured else ""
        if self.service in (None, 0):
            return "No service is running, so the battery follows no command." + tail
        label = service_type_label(self.service)
        cause = self.cause
        if cause is None:
            return f"The battery runs {label}; the cause is unknown." + tail
        window = f" ({cause.window})" if cause.window else ""
        if cause.source.startswith("external") or cause.source.startswith("schedule of"):
            return (
                f"Who: {cause.source}{window}, {cause.detail}. Why: that schedule runs {label} "
                "now. It was not created by this integration, which leaves it untouched."
                + tail
            )
        if cause.source == "not started by this integration":
            return (
                f"Who: unknown. Why: the battery runs {label}, but no schedule from this "
                "integration or in the Gateway list covers this time. It may be the "
                "Gateway's own control, the aggregator directly or a manual command." + tail
            )
        detail = f" Why: {cause.detail}." if cause.detail else ""
        return f"Who: {cause.source}{window} (this integration).{detail}" + tail

    # ----- texts -------------------------------------------------------------------

    def _measurements(self) -> str:
        parts = []
        if self.power_w is not None:
            parts.append(f"battery {self.power_w / 1000:+.1f} kW")
        if self.soc is not None:
            parts.append(f"SOC {self.soc:.0f} %")
        if self.grid_w is not None:
            parts.append(f"grid {self.grid_w / 1000:+.1f} kW")
        return "; ".join(parts)

    def _service_text(self, service: int) -> str:
        measured = self._measurements()
        tail = f" ({measured})" if measured else ""
        if service == 0:
            return f"Battery is idle: no service running{tail}"
        cause = self.cause.text() if self.cause else "unknown cause"
        return f"Battery runs {service_type_label(service)} ({service}): {cause}{tail}"

    def _direction_text(self, direction: str) -> str:
        measured = self._measurements()
        tail = f" ({measured})" if measured else ""
        if self.service in (None, 0):
            why = "no service running"
        else:
            label = service_type_label(self.service)
            why = f"{label}, " + (self.cause.text() if self.cause else "unknown cause")
        verb = {"charging": "started charging", "discharging": "started discharging",
                "idle": "stopped charging and discharging"}[direction]
        return f"Battery {verb}: {why}{tail}"

    def state(self) -> str:
        if self.service is None:
            return "unknown"
        if self.service == 0 and self.direction in (None, "idle"):
            return "Idle"
        direction = {"charging": "Charging", "discharging": "Discharging",
                     "idle": "Holding"}.get(self.direction or "", "Running")
        source = self.cause.source if self.cause else service_type_label(self.service)
        text = f"{direction} – {source}"
        return text[:255]

    def attributes(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "service_name": None if self.service is None else service_type_label(self.service),
            "direction": self.direction,
            "cause": None if self.cause is None else self.cause.text(),
            "source": None if self.cause is None else self.cause.source,
            "since": None if self.since is None else self.since.isoformat(),
            "battery_power_w": None if self.power_w is None else round(self.power_w),
            "usable_soc_percent": self.soc,
            "grid_power_w": None if self.grid_w is None else round(self.grid_w),
            "recent": [{"time": time, "message": message} for time, message in self.recent],
        }

    # ----- cause ---------------------------------------------------------------------

    def _cause(self, service: int, now: datetime) -> Cause | None:
        if service == 0:
            return None
        runtime = self.runtime
        solar = getattr(runtime, "solar_charging_controller", None)
        if service == 105 and solar is not None and _owns(solar):
            surplus = getattr(solar, "potential_surplus_w", None)
            detail = "storing solar surplus"
            if isinstance(surplus, int | float) and isfinite(surplus):
                detail += f" of about {surplus / 1000:.1f} kW"
            return Cause("solar charging", detail)
        aps = getattr(runtime, "adaptive_peak_shaving_controller", None)
        if service == 105 and aps is not None and _owns(aps):
            return Cause("adaptive peak shaving", "keeping grid power inside the set limits")
        balancer = getattr(runtime, "meter_limit_load_balancer", None)
        if service in (119, 120, 129) and balancer is not None and getattr(
            balancer, "status", None
        ) == "active":
            return Cause("load balancing", "limiting phase current")
        own = self._own_record(service, now)
        if own is not None:
            return own
        external = self._external_schedule(service, now)
        if external is not None:
            return external
        return Cause(
            "not started by this integration",
            "no matching schedule in the Gateway list (Gateway's own control, "
            "the aggregator or a manual command)",
        )

    def _own_record(self, service: int, now: datetime) -> Cause | None:
        ownership = getattr(self.runtime, "scheduler_ownership", None)
        records = getattr(ownership, "records", None)
        if not isinstance(records, Mapping):
            return None
        from .price_controller import _record_start

        for record in records.values():
            if getattr(record.lifecycle, "value", None) not in {"owned", "provisional"}:
                continue
            if SERVICE_NUMBERS.get(record.service) != service:
                continue
            start = _record_start(record)
            duration = record.intended_state.get("duration")
            if start is None or type(duration) is not int:
                continue
            end = start + timedelta(seconds=duration)
            if not start <= now < end:
                continue
            owner = OWNER_NAMES.get(record.owner or "", record.owner or "this integration")
            detail = ""
            if record.owner == "automatic_price_optimization":
                detail = self._price_detail(now)
            return Cause(owner, detail, span(start, end))
        return None

    def _price_detail(self, now: datetime) -> str:
        controller = getattr(self.runtime, "price_optimization_controller", None)
        plan = getattr(controller, "plan", None)
        for action in getattr(plan, "actions", ()):
            if action.start <= now < action.end:
                mode = ""
                if action.discharge_mode is not None:
                    mode = f" ({action.discharge_mode.value.replace('_', ' ')})"
                return (
                    f"planned {action.direction.value}{mode} at "
                    f"{action.price_sek_per_kwh:.2f} kr/kWh, expected net profit "
                    f"{action.net_profit_sek_per_kwh:.2f} kr/kWh"
                )
        return "scheduled by the price plan"

    def _external_schedule(self, service: int, now: datetime) -> Cause | None:
        snapshot = self._snapshot()
        if snapshot is None:
            return None
        epoch = int(now.timestamp())
        for schedule in snapshot.schedules:
            if not schedule.enabled or schedule.service_type != service:
                continue
            if not schedule.start_epoch <= epoch < schedule.end_epoch:
                continue
            if schedule.ownership.ownership.value == "integration_owned":
                continue
            if own_schedule_owner(self.runtime, schedule) is not None:
                continue
            return Cause(
                _source_name(self.runtime, schedule),
                f"Gateway schedule #{schedule.sch_index}",
                _epoch_span(schedule.start_epoch, schedule.end_epoch),
            )
        return None

    def _snapshot(self) -> Any:
        coordinator = getattr(self.runtime, "scheduler_coordinator", None)
        if coordinator is None:
            return None
        snapshot = coordinator.last_good_snapshot
        return snapshot if snapshot is not None else None

    # ----- external schedules ----------------------------------------------------------

    def _track_external_schedules(self, now: datetime) -> None:
        """Log external schedules that appear, or disappear before they end."""
        snapshot = self._snapshot()
        if snapshot is None:
            return
        epoch = int(now.timestamp())
        current: dict[tuple[int, int, int], tuple[str, int]] = {}
        for schedule in snapshot.schedules:
            if schedule.ownership.ownership.value == "integration_owned":
                continue
            if own_schedule_owner(self.runtime, schedule) is not None:
                continue
            if schedule.end_epoch <= epoch or not schedule.enabled:
                continue
            key = (schedule.sch_index, schedule.start_epoch, schedule.service_type)
            current[key] = (
                f"{service_type_label(schedule.service_type)} "
                f"{_epoch_span(schedule.start_epoch, schedule.end_epoch)} "
                f"({_source_name(self.runtime, schedule)})",
                schedule.end_epoch,
            )
        previous, self._external = self._external, current
        if previous is None:
            return
        for key, (text, _end) in current.items():
            if key not in previous:
                self._log(
                    now, f"New external schedule in the Gateway: {text}",
                    "Who: created outside this integration (for example by the aggregator, "
                    "the Pixii app or the installer). Why: unknown to this integration; it "
                    "was found in the Gateway's schedule list and will run at that time "
                    "unless it is removed. This integration never changes it.",
                )
        controller = getattr(self.runtime, "price_optimization_controller", None)
        deleted = getattr(controller, "deleted_external", set())
        for key, (text, end) in previous.items():
            if key not in current and end > epoch:
                if (key[0], key[1]) in deleted:
                    self._log(
                        now, f"Aggregator schedule removed by price optimization: {text}",
                        "Who: price optimization, with Allow changing aggregator schedules "
                        "on. Why: its plan uses this time and earns more; see the price "
                        "optimization entry at the same time.",
                    )
                    continue
                self._log(
                    now, f"External schedule removed from the Gateway: {text}",
                    "Who: removed outside this integration before it ended. This "
                    "integration did not remove it.",
                )


def own_schedule_owner(runtime: Any, schedule: Any) -> str | None:
    """Feature name when a Gateway schedule has the exact shape of one of our records.

    Display only: the Gateway list does not carry our identity, so the start,
    duration and service of an owned or provisional record are compared.
    Control code never uses this to decide ownership.
    """
    ownership = getattr(runtime, "scheduler_ownership", None)
    records = getattr(ownership, "records", None)
    if not isinstance(records, Mapping):
        return None
    from .price_controller import _record_start

    for record in records.values():
        if getattr(record.lifecycle, "value", None) not in {"owned", "provisional"}:
            continue
        start = _record_start(record)
        if (
            start is not None
            and int(start.timestamp()) == schedule.start_epoch
            and record.intended_state.get("duration") == schedule.duration_seconds
            and SERVICE_NUMBERS.get(record.service) == schedule.service_type
        ):
            return OWNER_NAMES.get(record.owner or "", "this integration")
    return None


def _owns(controller: Any) -> bool:
    owns = getattr(controller, "owns_live_lease", None)
    try:
        return bool(owns()) if callable(owns) else False
    except Exception:  # noqa: BLE001 - attribution is best effort
        return False


def _source_name(runtime: Any, schedule: Any) -> str:
    """Who created an external schedule, as far as the Gateway tells us."""
    source_id = getattr(schedule, "source_id", None)
    controller = getattr(runtime, "price_optimization_controller", None)
    try:
        managed = getattr(getattr(controller, "config", None), "managed_source_id", None)
    except Exception:  # noqa: BLE001 - naming is best effort
        managed = None
    details = [
        part for part in (
            schedule.ownership.source_label,
            None if source_id is None else f"source {source_id}",
        ) if part
    ]
    if source_id is not None and source_id == managed:
        name = "aggregator schedule"
    elif source_id is not None or schedule.ownership.ownership.value == "external":
        name = "external schedule"
    else:
        return "schedule of unknown source"
    return f"{name} ({', '.join(details)})" if details else name


def _epoch_span(start: int, end: int) -> str:
    return span(datetime.fromtimestamp(start, UTC), datetime.fromtimestamp(end, UTC))


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if isfinite(number) else None


__all__ = ["BatteryActivityTracker", "Cause", "direction_of", "own_schedule_owner"]
