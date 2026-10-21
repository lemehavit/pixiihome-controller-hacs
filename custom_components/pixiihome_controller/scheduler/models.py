"""Normalized scheduler read models with fail-closed ownership."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from ..protocol.enums import service_type_label
from ..protocol.models import SchedulerDetail, SchedulerEntry


class Ownership(StrEnum):
    INTEGRATION_OWNED = "integration_owned"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class SchedulerOwnership:
    ownership: Ownership = Ownership.UNKNOWN
    source_label: str | None = None
    source_confidence: str = "unverified"
    editable: bool = False
    deletable: bool = False
    protected: bool = True


def default_ownership() -> SchedulerOwnership:
    """Unknown source IDs are never treated as ownership evidence."""
    return SchedulerOwnership()


class ScheduleCompleteness(StrEnum):
    LIST_ONLY = "list_only"
    DETAIL_ONLY = "detail_only"
    COMPLETE = "complete"


class ScheduleConsistency(StrEnum):
    CONSISTENT = "consistent"
    LIST_ONLY = "list_only"
    DETAIL_ONLY = "detail_only"
    MISMATCH = "mismatch"


@dataclass(frozen=True, slots=True)
class NormalizedRecurrence:
    """Opaque recurrence evidence without interpreting its protocol semantics."""

    recurring: bool | None
    recurrence_detail: tuple[tuple[str, object], ...] | None


@dataclass(frozen=True, slots=True)
class NormalizedSchedule:
    """Immutable, read-only schedule data merged from cached LIST and DETAIL."""

    sch_index: int
    start_epoch: int
    duration_seconds: int
    end_epoch: int
    service_no: int
    service_type: int
    enabled: bool
    service_priority: int | None
    asset_id: str | None
    source_id: int | None
    recurrence: NormalizedRecurrence
    ownership: SchedulerOwnership
    completeness: ScheduleCompleteness
    consistency: ScheduleConsistency

    def __post_init__(self) -> None:
        if (
            isinstance(self.start_epoch, bool)
            or not isinstance(self.start_epoch, int)
            or isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, int)
            or self.end_epoch != self.start_epoch + self.duration_seconds
        ):
            raise ValueError("normalized schedule epochs are invalid")

    @property
    def service_type_label(self) -> str:
        """Return the existing label while preserving unknown numeric types."""
        return service_type_label(self.service_type)


@dataclass(frozen=True, slots=True)
class SchedulerView:
    """A normalized read view that keeps LIST, DETAIL, and ownership separate."""

    sch_index: int
    entry: SchedulerEntry | None = None
    detail: SchedulerDetail | None = None
    ownership: SchedulerOwnership = field(default_factory=default_ownership)


def normalize_scheduler_view(view: SchedulerView) -> NormalizedSchedule:
    """Merge one cached view with DETAIL precedence and mismatch preservation."""
    entry = view.entry
    detail = view.detail
    if entry is None and detail is None:
        raise ValueError("scheduler view must contain LIST or DETAIL data")

    if entry is not None and detail is not None:
        completeness = ScheduleCompleteness.COMPLETE
        overlapping_values = (
            entry.start_epoch == detail.start_epoch,
            entry.duration_seconds == detail.duration_seconds,
            entry.service_no == detail.service_no,
            entry.service_type == detail.service_type,
            entry.enabled == detail.service_enabled,
        )
        consistency = (
            ScheduleConsistency.CONSISTENT
            if all(overlapping_values)
            else ScheduleConsistency.MISMATCH
        )
    elif entry is not None:
        completeness = ScheduleCompleteness.LIST_ONLY
        consistency = ScheduleConsistency.LIST_ONLY
    else:
        completeness = ScheduleCompleteness.DETAIL_ONLY
        consistency = ScheduleConsistency.DETAIL_ONLY

    if detail is not None:
        start_epoch = detail.start_epoch
        duration_seconds = detail.duration_seconds
        service_no = detail.service_no
        service_type = detail.service_type
        enabled = detail.service_enabled
        service_priority = detail.service_priority
        asset_id = detail.asset_id
        source_id = detail.source_id
    else:
        assert entry is not None
        start_epoch = entry.start_epoch
        duration_seconds = entry.duration_seconds
        service_no = entry.service_no
        service_type = entry.service_type
        enabled = entry.enabled
        service_priority = None
        asset_id = None
        source_id = None

    recurrence_detail = None
    if detail is not None:
        recurrence_detail = tuple(
            (str(key), _freeze_recurrence_value(value))
            for key, value in sorted(detail.recurrence.items(), key=lambda item: str(item[0]))
        )

    return NormalizedSchedule(
        sch_index=view.sch_index,
        start_epoch=start_epoch,
        duration_seconds=duration_seconds,
        end_epoch=_calculate_end_epoch(start_epoch, duration_seconds),
        service_no=service_no,
        service_type=service_type,
        enabled=enabled,
        service_priority=service_priority,
        asset_id=asset_id,
        source_id=source_id,
        recurrence=NormalizedRecurrence(
            recurring=entry.recurrence if entry is not None else None,
            recurrence_detail=recurrence_detail,
        ),
        ownership=view.ownership,
        completeness=completeness,
        consistency=consistency,
    )


def _calculate_end_epoch(start_epoch: object, duration_seconds: object) -> int:
    if (
        isinstance(start_epoch, bool)
        or not isinstance(start_epoch, int)
        or isinstance(duration_seconds, bool)
        or not isinstance(duration_seconds, int)
    ):
        raise ValueError("normalized schedule epochs are invalid")
    return start_epoch + duration_seconds


def _freeze_recurrence_value(value: object) -> object:
    if isinstance(value, Mapping):
        return tuple(
            (str(key), _freeze_recurrence_value(item))
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        )
    if isinstance(value, list | tuple):
        return tuple(_freeze_recurrence_value(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_freeze_recurrence_value(item) for item in value), key=repr))
    if value is None or isinstance(value, bool | int | float | str):
        return value
    return repr(value)
