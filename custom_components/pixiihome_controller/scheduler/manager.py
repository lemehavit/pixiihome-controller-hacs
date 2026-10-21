"""Read-only scheduler refresh state around an injected protocol client."""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic, time

from ..protocol.models import SchedulerDetail, SchedulerEntry
from .client import SchedulerReadClient
from .models import (
    NormalizedSchedule,
    SchedulerView,
    default_ownership,
    normalize_scheduler_view,
)

FIRST_WRITE_LIST_MAX_AGE_SECONDS = 60.0
SCHEDULER_LIST_ENTRY_LIMIT = 100


class SchedulerListResponseTooLargeError(ValueError):
    """A sanitized cardinality failure at the scheduler trust boundary."""



class SchedulerRefreshManager:
    """Cache sanitized scheduler reads without creating transport or auth behavior."""

    def __init__(
        self,
        client: SchedulerReadClient,
        *,
        clock: Callable[[], float] = time,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._client = client
        self._clock = clock
        self._monotonic_clock = monotonic_clock
        self._entries: tuple[SchedulerEntry, ...] = ()
        self._entry_fingerprints: dict[int, tuple[object, ...]] = {}
        self._details: dict[int, SchedulerDetail] = {}
        self._list_refresh_attempts = 0
        self._list_refresh_successes = 0
        self._detail_refresh_attempts = 0
        self._detail_refresh_successes = 0
        self._refresh_failures = 0
        self._last_error: str | None = None
        self._last_refresh_at: float | None = None
        self._last_successful_list_at: float | None = None
        self._last_successful_list_monotonic: float | None = None
        self._last_successful_list_from_epoch: int | None = None
        self._last_successful_list_to_epoch: int | None = None
        self._list_visibility_invalidated = False

    async def refresh_list(self, from_epoch: int, to_epoch: int) -> tuple[SchedulerEntry, ...]:
        """Refresh and record one successful scheduler LIST evidence window."""
        self._list_refresh_attempts += 1
        try:
            entries = tuple(await self._client.list_entries(from_epoch, to_epoch))
            if len(entries) > SCHEDULER_LIST_ENTRY_LIMIT:
                raise SchedulerListResponseTooLargeError(
                    "scheduler LIST response exceeded the entry limit"
                )
        except Exception as err:  # noqa: BLE001 - diagnostics must not hide client failures
            self._record_failure(err)
            self._list_visibility_invalidated = True
            raise
        new_fingerprints = {
            entry.sch_index: _entry_fingerprint(entry) for entry in entries
        }
        retained_indices = {
            sch_index
            for sch_index, fingerprint in new_fingerprints.items()
            if self._entry_fingerprints.get(sch_index) == fingerprint
        }
        self._details = {
            sch_index: detail
            for sch_index, detail in self._details.items()
            if sch_index in retained_indices
        }
        self._entries = entries
        self._entry_fingerprints = new_fingerprints
        self._last_successful_list_at = self._clock()
        self._last_successful_list_monotonic = self._monotonic_clock()
        self._last_successful_list_from_epoch = from_epoch
        self._last_successful_list_to_epoch = to_epoch
        self._list_visibility_invalidated = False
        self._list_refresh_successes += 1
        return self._entries

    async def refresh_detail(self, sch_index: int) -> SchedulerDetail:
        """Refresh the evidenced scheduler DETAIL operation for one index."""
        self._detail_refresh_attempts += 1
        try:
            detail = await self._client.get_detail(sch_index)
        except Exception as err:  # noqa: BLE001 - diagnostics must not hide client failures
            self._record_failure(err)
            raise
        if detail.sch_index != sch_index:
            error = ValueError("scheduler DETAIL response index does not match the request")
            self._record_failure(error)
            raise error
        self._details[sch_index] = detail
        self._record_success()
        self._detail_refresh_successes += 1
        return detail

    @property
    def entries(self) -> tuple[SchedulerEntry, ...]:
        """Return the last sanitized LIST result."""
        return self._entries

    @property
    def details(self) -> tuple[SchedulerDetail, ...]:
        """Return the last sanitized DETAIL results."""
        return tuple(self._details[index] for index in sorted(self._details))
    def detail_for(self, sch_index: int) -> SchedulerDetail | None:
        """Return retained DETAIL evidence for one current LIST index."""
        return self._details.get(sch_index)

    @property
    def views(self) -> tuple[SchedulerView, ...]:
        """Return normalized read views with unknown ownership protected by default."""
        entries_by_index = {entry.sch_index: entry for entry in self._entries}
        indices = sorted(entries_by_index.keys() | self._details.keys())
        return tuple(
            SchedulerView(
                sch_index=sch_index,
                entry=entries_by_index.get(sch_index),
                detail=self._details.get(sch_index),
                ownership=default_ownership(),
            )
            for sch_index in indices
        )

    @property
    def normalized_schedules(self) -> tuple[NormalizedSchedule, ...]:
        """Return cached schedules without performing any network operation."""
        schedules = tuple(normalize_scheduler_view(view) for view in self.views)
        return tuple(
            sorted(schedules, key=lambda schedule: (schedule.start_epoch, schedule.sch_index))
        )

    def list_visibility_fresh(
        self,
        *,
        now_monotonic: float | None = None,
        max_age_seconds: float = FIRST_WRITE_LIST_MAX_AGE_SECONDS,
    ) -> bool:
        """Return whether successful LIST visibility is fresh and not invalidated."""
        if (
            self._list_visibility_invalidated
            or self._last_successful_list_monotonic is None
            or max_age_seconds < 0
        ):
            return False
        current = self._monotonic_clock() if now_monotonic is None else now_monotonic
        age = current - self._last_successful_list_monotonic
        return 0 <= age <= max_age_seconds

    def list_visibility_covers(self, from_epoch: int, to_epoch: int) -> bool:
        """Return whether the successful LIST range covers the requested interval."""
        return (
            self._last_successful_list_from_epoch is not None
            and self._last_successful_list_to_epoch is not None
            and self._last_successful_list_from_epoch <= from_epoch
            and to_epoch <= self._last_successful_list_to_epoch
        )

    def list_visibility_diagnostics(self) -> dict[str, object]:
        """Return safe LIST evidence without scheduler records or raw protocol data."""
        age: float | None = None
        if self._last_successful_list_monotonic is not None:
            age = self._monotonic_clock() - self._last_successful_list_monotonic
        return {
            "fresh": self.list_visibility_fresh(),
            "age_seconds": age,
            "invalidated": self._list_visibility_invalidated,
            "last_successful_at": self._last_successful_list_at,
            "from_epoch": self._last_successful_list_from_epoch,
            "to_epoch": self._last_successful_list_to_epoch,
        }

    def diagnostics(self) -> dict[str, object]:
        """Return counters and ownership status without raw protocol data."""
        views = self.views
        normalized_schedules = self.normalized_schedules
        diagnostics: dict[str, object] = {
            "status": "read_only_refresh_manager",
            "client_boundary": "injected_read_client",
            "mutation_operations_enabled": False,
            "list_entry_count": len(self._entries),
            "detail_count": len(self._details),
            "normalized_schedule_count": len(normalized_schedules),
            "complete_schedule_count": sum(
                schedule.completeness.value == "complete" for schedule in normalized_schedules
            ),
            "partial_schedule_count": sum(
                schedule.completeness.value in {"list_only", "detail_only"}
                for schedule in normalized_schedules
            ),
            "mismatch_count": sum(
                schedule.consistency.value == "mismatch" for schedule in normalized_schedules
            ),
            "view_count": len(views),
            "ownership": {
                "integration_owned": 0,
                "external": 0,
                "unknown_protected": len(views),
            },
            "list_refresh_attempts": self._list_refresh_attempts,
            "list_refresh_successes": self._list_refresh_successes,
            "detail_refresh_attempts": self._detail_refresh_attempts,
            "detail_refresh_successes": self._detail_refresh_successes,
            "refresh_failures": self._refresh_failures,
            "last_error": self._last_error,
            "last_refresh_at": self._last_refresh_at,
            "list_visibility": self.list_visibility_diagnostics(),
        }
        client_diagnostics = getattr(self._client, "diagnostics", None)
        if callable(client_diagnostics):
            diagnostics["client"] = client_diagnostics()
        return diagnostics

    def _record_success(self) -> None:
        self._last_error = None
        self._last_refresh_at = self._clock()

    def _record_failure(self, error: Exception) -> None:
        self._refresh_failures += 1
        self._last_error = (
            "response_too_large"
            if isinstance(error, SchedulerListResponseTooLargeError)
            or str(error) == "scheduler HTTP response was too large"
            else type(error).__name__
        )


def _entry_fingerprint(entry: SchedulerEntry) -> tuple[object, ...]:
    """Return the LIST fields that determine DETAIL cache validity."""
    return (
        entry.entry,
        entry.duration_seconds,
        entry.enabled,
        entry.sch_index,
        entry.recurrence,
        entry.start_epoch,
        entry.service_no,
        entry.service_type,
    )
