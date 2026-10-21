"""Shared read-only scheduler inventory coordination and presentation helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from math import isfinite
from typing import Any, cast

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .manager import SCHEDULER_LIST_ENTRY_LIMIT, SchedulerRefreshManager
from .models import NormalizedSchedule

INVENTORY_REFRESH_INTERVAL = timedelta(minutes=15)
INVENTORY_WINDOW_BUCKET_SECONDS = int(INVENTORY_REFRESH_INTERVAL.total_seconds())
INVENTORY_WINDOW_START_SECONDS = 24 * 60 * 60
INVENTORY_WINDOW_END_SECONDS = 7 * 24 * 60 * 60
MAX_CUSTOM_WINDOW_SECONDS = 31 * 24 * 60 * 60
UPCOMING_WINDOW_SECONDS = 24 * 60 * 60
INITIAL_REFRESH_RETRY_DELAYS_SECONDS = (0, 15, 30, 60)

PUBLIC_METADATA_DEPTH = 4
PUBLIC_METADATA_ITEMS = 32
PUBLIC_METADATA_TEXT = 256
PUBLIC_METADATA_NUMBER_DIGITS = 32
PUBLIC_METADATA_TOTAL_BYTES = 16_384
SENSITIVE_METADATA_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "sessid",
        "session",
        "alevel",
        "cookie",
        "credential",
        "api_key",
        "apikey",
        "authorization",
        "auth",
    }
)

@dataclass(frozen=True, slots=True)
class SchedulerInventorySnapshot:
    """Immutable, bounded scheduler inventory state shared by entities."""

    data_status: str
    authenticated: bool | None
    window_start_epoch: int
    window_end_epoch: int
    schedules: tuple[NormalizedSchedule, ...]
    evaluated_at_epoch: int
    active_count: int
    upcoming_24h_count: int
    protected_count: int
    complete_count: int
    incomplete_count: int
    next_schedule_index: int | None
    snapshot_updated_at: datetime | None
    semantic_fingerprint: str

    @property
    def available(self) -> bool:
        """Return whether the snapshot is trustworthy for a state value."""
        return self.data_status in {"current", "partial"}

    @property
    def window_start(self) -> datetime:
        """Return the canonical window start as an aware UTC datetime."""
        return dt_util.utc_from_timestamp(self.window_start_epoch)

    @property
    def window_end(self) -> datetime:
        """Return the canonical window end as an aware UTC datetime."""
        return dt_util.utc_from_timestamp(self.window_end_epoch)


class SchedulerInventoryCoordinator:
    """Own one serialized scheduler read model for all read-only consumers."""

    def __init__(
        self,
        hass: HomeAssistant,
        manager: SchedulerRefreshManager,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.hass = hass
        self.manager = manager
        self._clock = clock or (lambda: dt_util.now(UTC))
        self._snapshot: SchedulerInventorySnapshot | None = None
        self._last_good_snapshot: SchedulerInventorySnapshot | None = None
        self._listeners: set[Callable[[], None]] = set()
        self._refresh_task: asyncio.Task[SchedulerInventorySnapshot] | None = None
        self._initial_refresh_task: asyncio.Task[SchedulerInventorySnapshot] | None = None
        self._periodic_unsub: Callable[[], None] | None = None
        self._boundary_unsub: Callable[[], None] | None = None
        self._started = False
        self._read_lock = asyncio.Lock()

    @property
    def snapshot(self) -> SchedulerInventorySnapshot | None:
        """Return the latest published inventory snapshot without performing I/O."""
        return self._snapshot

    @property
    def last_good_snapshot(self) -> SchedulerInventorySnapshot | None:
        """Return the latest successful LIST snapshot, including partial detail."""
        return self._last_good_snapshot

    @property
    def update_interval(self) -> timedelta:
        """Return the bounded periodic refresh interval."""
        return INVENTORY_REFRESH_INTERVAL

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to semantic snapshot changes."""
        self._listeners.add(listener)

        def remove() -> None:
            self._listeners.discard(listener)

        return remove

    async def async_start(self) -> None:
        """Start periodic and local boundary processing without blocking setup."""
        if self._started:
            return
        if not hasattr(self.hass, "loop") or not hasattr(self.hass, "async_create_task"):
            return
        self._started = True
        self._periodic_unsub = async_track_time_interval(
            self.hass, self._async_periodic_refresh, INVENTORY_REFRESH_INTERVAL
        )
        if self._initial_refresh_task is None:
            self._initial_refresh_task = self.hass.async_create_background_task(
                self._async_initial_refresh_with_retry(),
                "pixiihome_controller scheduler initial refresh",
            )

    async def _async_initial_refresh_with_retry(self) -> SchedulerInventorySnapshot:
        """Recover automatically from short Gateway login/session lockouts."""
        snapshot: SchedulerInventorySnapshot | None = None
        for delay in INITIAL_REFRESH_RETRY_DELAYS_SECONDS:
            if delay:
                await asyncio.sleep(delay)
            if not self._started:
                raise asyncio.CancelledError
            snapshot = await self.async_refresh()
            if snapshot.available:
                return snapshot
        assert snapshot is not None
        return snapshot

    async def async_stop(self) -> None:
        """Stop timers and cancel an in-flight read during config-entry unload."""
        self._started = False
        if self._periodic_unsub is not None:
            self._periodic_unsub()
            self._periodic_unsub = None
        if self._boundary_unsub is not None:
            self._boundary_unsub()
            self._boundary_unsub = None
        tasks = [self._initial_refresh_task, self._refresh_task]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(
                *(task for task in tasks if task is not None),
                return_exceptions=True,
            )
        self._initial_refresh_task = None
        self._refresh_task = None

    async def async_refresh(self) -> SchedulerInventorySnapshot:
        """Refresh the canonical rolling window, coalescing concurrent callers."""
        task = self._refresh_task
        if task is None or task.done():
            task = asyncio.create_task(self._async_refresh_canonical())
            self._refresh_task = task
        try:
            return await asyncio.shield(task)
        finally:
            if task.done() and self._refresh_task is task:
                self._refresh_task = None

    async def async_read_window(
        self,
        from_epoch: int,
        to_epoch: int,
    ) -> SchedulerInventorySnapshot:
        """Read one bounded calendar window through the same manager pipeline."""
        if (
            not isinstance(from_epoch, int)
            or isinstance(from_epoch, bool)
            or not isinstance(to_epoch, int)
            or isinstance(to_epoch, bool)
            or from_epoch >= to_epoch
            or to_epoch - from_epoch > MAX_CUSTOM_WINDOW_SECONDS
        ):
            raise ValueError("scheduler calendar window is invalid or too large")
        canonical = self._snapshot
        if (
            canonical is not None
            and canonical.data_status != "stale"
            and canonical.window_start_epoch <= from_epoch
            and to_epoch <= canonical.window_end_epoch
        ):
            return canonical
        return await self._async_refresh_window(from_epoch, to_epoch, publish=False)

    async def async_recompute_boundaries(self, now: datetime | None = None) -> None:
        """Recompute time-derived fields locally without scheduler network I/O."""
        snapshot = self._snapshot
        if snapshot is None or snapshot.data_status == "stale":
            return
        current = now or self._clock()
        self._publish(
            _snapshot_for_schedules(
                snapshot.schedules,
                data_status=snapshot.data_status,
                authenticated=snapshot.authenticated,
                window_start_epoch=snapshot.window_start_epoch,
                window_end_epoch=snapshot.window_end_epoch,
                now=current,
                previous=snapshot,
            )
        )
        self._schedule_next_boundary(self._snapshot)

    async def _async_periodic_refresh(self, _now: datetime) -> None:
        await self.async_refresh()

    async def _async_boundary_refresh(self, now: datetime) -> None:
        await self.async_recompute_boundaries(now)

    async def _async_refresh_canonical(self) -> SchedulerInventorySnapshot:
        now = self._clock()
        from_epoch, to_epoch = _window_bounds(now)
        return await self._async_refresh_window(from_epoch, to_epoch, publish=True)

    async def _async_refresh_window(
        self,
        from_epoch: int,
        to_epoch: int,
        *,
        publish: bool,
    ) -> SchedulerInventorySnapshot:
        async with self._read_lock:
            return await self._async_refresh_window_unlocked(
                from_epoch,
                to_epoch,
                publish=publish,
            )

    async def _async_refresh_window_unlocked(
        self,
        from_epoch: int,
        to_epoch: int,
        *,
        publish: bool,
    ) -> SchedulerInventorySnapshot:
        try:
            await self.manager.refresh_list(from_epoch, to_epoch)
            detail_failures = await self._async_refresh_missing_details()
            schedules = tuple(self.manager.normalized_schedules)
            authenticated = _authenticated(self.manager)
            status = "partial" if detail_failures else "current"
            snapshot = _snapshot_for_schedules(
                schedules,
                data_status=status,
                authenticated=authenticated,
                window_start_epoch=from_epoch,
                window_end_epoch=to_epoch,
                now=self._clock(),
                previous=self._snapshot if publish else None,
            )
        except Exception:
            previous = self._last_good_snapshot
            now = self._clock()
            snapshot = _snapshot_for_schedules(
                previous.schedules if previous is not None else (),
                data_status="stale",
                authenticated=_authenticated(self.manager),
                window_start_epoch=from_epoch,
                window_end_epoch=to_epoch,
                now=now,
                previous=self._snapshot if publish else None,
            )
            if publish:
                self._publish(snapshot)
            return snapshot

        if publish:
            self._last_good_snapshot = snapshot
            self._publish(snapshot)
            self._schedule_next_boundary(snapshot)
        return snapshot

    async def _async_refresh_missing_details(self) -> bool:
        """Enrich current LIST entries sequentially, retaining safe LIST-only views."""
        entries = getattr(self.manager, "entries", None)
        refresh_detail = getattr(self.manager, "refresh_detail", None)
        if entries is None or not callable(refresh_detail):
            return False
        detail_for = getattr(self.manager, "detail_for", None)
        refresh_detail_fn = cast(Callable[[int], Awaitable[Any]], refresh_detail)
        retained_details = {
            detail.sch_index
            for detail in getattr(self.manager, "details", ())
            if getattr(detail, "sch_index", None) is not None
        }
        failures = False
        for entry in entries[:SCHEDULER_LIST_ENTRY_LIMIT]:
            sch_index = entry.sch_index
            if sch_index in retained_details or (
                callable(detail_for) and detail_for(sch_index) is not None
            ):
                continue
            try:
                await refresh_detail_fn(sch_index)
            except Exception:  # noqa: BLE001 - preserve the safe LIST-only record
                failures = True
        return failures

    def _publish(self, snapshot: SchedulerInventorySnapshot) -> None:
        if self._snapshot is not None and (
            self._snapshot.semantic_fingerprint == snapshot.semantic_fingerprint
        ):
            return
        self._snapshot = snapshot
        for listener in tuple(self._listeners):
            listener()

    def _schedule_next_boundary(self, snapshot: SchedulerInventorySnapshot | None) -> None:
        if not self._started or snapshot is None or snapshot.data_status == "stale":
            return
        now_epoch = int(self._clock().timestamp())
        candidates = {
            boundary
            for schedule in snapshot.schedules
            if schedule.enabled
            for boundary in (
                schedule.start_epoch - UPCOMING_WINDOW_SECONDS,
                schedule.start_epoch,
                schedule.end_epoch,
            )
            if boundary > now_epoch
        }
        if not candidates:
            return
        boundary = dt_util.utc_from_timestamp(min(candidates))
        if self._boundary_unsub is not None:
            self._boundary_unsub()
        self._boundary_unsub = async_track_point_in_time(
            self.hass, self._async_boundary_refresh, boundary
        )


def inventory_attributes(snapshot: SchedulerInventorySnapshot) -> dict[str, object]:
    """Serialize one bounded snapshot for Home Assistant state attributes."""
    metadata_budget = [PUBLIC_METADATA_TOTAL_BYTES]
    return {
        "data_status": snapshot.data_status,
        "authenticated": snapshot.authenticated,
        "window_start": snapshot.window_start.isoformat(),
        "window_end": snapshot.window_end.isoformat(),
        "active_count": snapshot.active_count,
        "upcoming_24h_count": snapshot.upcoming_24h_count,
        "protected_count": snapshot.protected_count,
        "complete_count": snapshot.complete_count,
        "incomplete_count": snapshot.incomplete_count,
        "next_schedule": _next_schedule_payload(snapshot),
        "schedules": [
            schedule_payload(
                schedule,
                snapshot.evaluated_at_epoch,
                metadata_budget=metadata_budget,
            )
            for schedule in snapshot.schedules
        ],
        "snapshot_updated_at": (
            snapshot.snapshot_updated_at.isoformat()
            if snapshot.snapshot_updated_at is not None
            else None
        ),
    }
def schedule_payload(
    schedule: NormalizedSchedule,
    now_epoch: int,
    *,
    metadata_budget: list[int] | None = None,
) -> dict[str, object]:
    """Serialize protocol-supported normalized schedule evidence only."""
    if metadata_budget is None:
        metadata_budget = [PUBLIC_METADATA_TOTAL_BYTES]
    return {
        "sch_index": schedule.sch_index,
        "start": dt_util.utc_from_timestamp(schedule.start_epoch).isoformat(),
        "end": dt_util.utc_from_timestamp(schedule.end_epoch).isoformat(),
        "duration_seconds": schedule.duration_seconds,
        "service_no": schedule.service_no,
        "service_type": schedule.service_type,
        "service": schedule.service_type_label,
        "enabled": schedule.enabled,
        "active_now": (
            schedule.enabled
            and schedule.start_epoch <= now_epoch < schedule.end_epoch
        ),
        "service_priority": schedule.service_priority,
        "asset_id": schedule.asset_id,
        "source_id": schedule.source_id,
        "recurrence": {
            "recurring": schedule.recurrence.recurring,
            "detail": _json_value(
                schedule.recurrence.recurrence_detail,
                budget=metadata_budget,
            ),
        },
        "ownership": schedule.ownership.ownership.value,
        "source_label": schedule.ownership.source_label,
        "source_confidence": schedule.ownership.source_confidence,
        "protected": schedule.ownership.protected,
        "editable": schedule.ownership.editable,
        "deletable": schedule.ownership.deletable,
        "completeness": schedule.completeness.value,
        "consistency": schedule.consistency.value,
    }


def _next_schedule_payload(snapshot: SchedulerInventorySnapshot) -> dict[str, object] | None:
    if snapshot.next_schedule_index is None:
        return None
    schedule = next(
        (
            schedule
            for schedule in snapshot.schedules
            if schedule.sch_index == snapshot.next_schedule_index
        ),
        None,
    )
    if schedule is None:
        return None
    return {
        "sch_index": schedule.sch_index,
        "start": dt_util.utc_from_timestamp(schedule.start_epoch).isoformat(),
        "end": dt_util.utc_from_timestamp(schedule.end_epoch).isoformat(),
        "service": schedule.service_type_label,
        "active_now": False,
    }


def _snapshot_for_schedules(
    schedules: tuple[NormalizedSchedule, ...],
    *,
    data_status: str,
    authenticated: bool | None,
    window_start_epoch: int,
    window_end_epoch: int,
    now: datetime,
    previous: SchedulerInventorySnapshot | None,
) -> SchedulerInventorySnapshot:
    now_epoch = int(now.timestamp())
    active = tuple(
        schedule
        for schedule in schedules
        if schedule.enabled and schedule.start_epoch <= now_epoch < schedule.end_epoch
    )
    upcoming = tuple(
        schedule
        for schedule in schedules
        if (
            schedule.enabled
            and now_epoch < schedule.start_epoch <= now_epoch + UPCOMING_WINDOW_SECONDS
        )
    )
    next_schedule = next(
        (
            schedule
            for schedule in schedules
            if schedule.enabled and schedule.start_epoch > now_epoch
        ),
        None,
    )
    fingerprint = _semantic_fingerprint(
        schedules,
        data_status,
        authenticated,
        window_start_epoch,
        window_end_epoch,
        len(active),
        len(upcoming),
        next_schedule.sch_index if next_schedule is not None else None,
    )
    updated_at = (
        previous.snapshot_updated_at
        if previous is not None and previous.semantic_fingerprint == fingerprint
        else now.astimezone(UTC)
    )
    return SchedulerInventorySnapshot(
        data_status=data_status,
        authenticated=authenticated,
        window_start_epoch=window_start_epoch,
        window_end_epoch=window_end_epoch,
        schedules=schedules,
        evaluated_at_epoch=now_epoch,
        active_count=len(active),
        upcoming_24h_count=len(upcoming),
        protected_count=sum(schedule.ownership.protected for schedule in schedules),
        complete_count=sum(schedule.completeness.value == "complete" for schedule in schedules),
        incomplete_count=sum(schedule.completeness.value != "complete" for schedule in schedules),
        next_schedule_index=next_schedule.sch_index if next_schedule is not None else None,
        snapshot_updated_at=updated_at,
        semantic_fingerprint=fingerprint,
    )


def _semantic_fingerprint(
    schedules: tuple[NormalizedSchedule, ...],
    data_status: str,
    authenticated: bool | None,
    window_start_epoch: int,
    window_end_epoch: int,
    active_count: int,
    upcoming_24h_count: int,
    next_schedule_index: int | None,
) -> str:
    values = (
        data_status,
        authenticated,
        window_start_epoch,
        window_end_epoch,
        active_count,
        upcoming_24h_count,
        next_schedule_index,
        tuple(_schedule_fingerprint(schedule) for schedule in schedules),
    )
    return sha256(repr(values).encode("utf-8")).hexdigest()


def _schedule_fingerprint(schedule: NormalizedSchedule) -> tuple[object, ...]:
    return (
        schedule.sch_index,
        schedule.start_epoch,
        schedule.duration_seconds,
        schedule.end_epoch,
        schedule.service_no,
        schedule.service_type,
        schedule.enabled,
        schedule.service_priority,
        schedule.asset_id,
        schedule.source_id,
        schedule.recurrence.recurring,
        schedule.recurrence.recurrence_detail,
        schedule.ownership.ownership.value,
        schedule.ownership.source_label,
        schedule.ownership.source_confidence,
        schedule.ownership.protected,
        schedule.ownership.editable,
        schedule.ownership.deletable,
        schedule.completeness.value,
        schedule.consistency.value,
    )


def _window_bounds(now: datetime) -> tuple[int, int]:
    epoch = int(now.timestamp())
    bucket = epoch // INVENTORY_WINDOW_BUCKET_SECONDS * INVENTORY_WINDOW_BUCKET_SECONDS
    return (
        bucket - INVENTORY_WINDOW_START_SECONDS,
        bucket + INVENTORY_WINDOW_END_SECONDS,
    )


def _authenticated(manager: Any) -> bool | None:
    diagnostics = getattr(manager, "diagnostics", None)
    if not callable(diagnostics):
        return None
    try:
        evidence = diagnostics()
    except Exception:  # noqa: BLE001 - diagnostics are optional evidence
        return None
    if not isinstance(evidence, dict):
        return None
    client = evidence.get("client")
    if not isinstance(client, dict):
        return None
    value = client.get("sessid_in_memory")
    return value if isinstance(value, bool) else None


def _json_value(
    value: object,
    *,
    depth: int = 0,
    budget: list[int] | None = None,
) -> object:
    if budget is None:
        budget = [PUBLIC_METADATA_TOTAL_BYTES]
    if depth > PUBLIC_METADATA_DEPTH or budget[0] <= 0:
        return "[omitted]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if len(str(abs(value))) > PUBLIC_METADATA_NUMBER_DIGITS:
            return "[omitted]"
        return value if _consume_public_budget(str(value), budget) else "[omitted]"
    if isinstance(value, float):
        if not isfinite(value):
            return "[omitted]"
        text = repr(value)
        if len(text) > PUBLIC_METADATA_NUMBER_DIGITS:
            return "[omitted]"
        return value if _consume_public_budget(text, budget) else "[omitted]"
    if isinstance(value, str):
        if _sensitive_text(value):
            return "[omitted]"
        bounded = _bounded_public_text(value)
        return bounded if _consume_public_budget(bounded, budget) else "[omitted]"
    if isinstance(value, Mapping):
        if len(value) > PUBLIC_METADATA_ITEMS:
            return "[omitted]"
        result: dict[str, object] = {}
        for key, item in value.items():
            if budget[0] <= 0:
                break
            if _sensitive_key(key):
                continue
            bounded_key = _bounded_public_text(str(key))
            if not _consume_public_budget(bounded_key, budget):
                break
            result[bounded_key] = _json_value(item, depth=depth + 1, budget=budget)
        return result
    if isinstance(value, tuple):
        if (
            len(value) <= PUBLIC_METADATA_ITEMS
            and all(
                isinstance(item, tuple)
                and len(item) == 2
                and isinstance(item[0], str)
                for item in value
            )
        ):
            result = {}
            for key, item in value:
                if budget[0] <= 0:
                    break
                if _sensitive_key(key):
                    continue
                bounded_key = _bounded_public_text(key)
                if not _consume_public_budget(bounded_key, budget):
                    break
                result[bounded_key] = _json_value(
                    item,
                    depth=depth + 1,
                    budget=budget,
                )
            return result
        if len(value) > PUBLIC_METADATA_ITEMS:
            return "[omitted]"
        return [
            _json_value(item, depth=depth + 1, budget=budget) for item in value
        ]
    if isinstance(value, list):
        if len(value) > PUBLIC_METADATA_ITEMS:
            return "[omitted]"
        return [
            _json_value(item, depth=depth + 1, budget=budget) for item in value
        ]
    return "[omitted]"


def _consume_public_budget(value: str, budget: list[int]) -> bool:
    size = len(value.encode("utf-8"))
    if size > budget[0]:
        return False
    budget[0] -= size
    return True

def _bounded_public_text(value: str) -> str:
    return value.encode("utf-8")[:PUBLIC_METADATA_TEXT].decode("utf-8", "ignore")


def _sensitive_key(key: object) -> bool:
    if not isinstance(key, str):
        return True
    normalized = key.casefold().replace("-", "_")
    return normalized in SENSITIVE_METADATA_KEYS or any(
        marker in normalized for marker in SENSITIVE_METADATA_KEYS
    )


def _sensitive_text(value: str) -> bool:
    normalized = value.casefold().replace("-", "_")
    return any(marker in normalized for marker in SENSITIVE_METADATA_KEYS)
