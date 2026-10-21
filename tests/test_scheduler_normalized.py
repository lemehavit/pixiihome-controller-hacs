from __future__ import annotations

from typing import Any

import pytest

from custom_components.pixiihome_controller.const import LIVE_WRITES_ENABLED
from custom_components.pixiihome_controller.protocol.models import SchedulerDetail, SchedulerEntry
from custom_components.pixiihome_controller.scheduler.manager import SchedulerRefreshManager
from custom_components.pixiihome_controller.scheduler.models import (
    NormalizedSchedule,
    Ownership,
    ScheduleCompleteness,
    ScheduleConsistency,
    SchedulerView,
    default_ownership,
    normalize_scheduler_view,
)


def make_entry(
    sch_index: int = 1,
    *,
    start_epoch: int = 100,
    duration_seconds: int = 60,
    enabled: bool = True,
    service_no: int = 2005,
    service_type: int = 107,
    recurrence: bool = False,
) -> SchedulerEntry:
    return SchedulerEntry(
        entry=sch_index,
        duration_seconds=duration_seconds,
        enabled=enabled,
        sch_index=sch_index,
        recurrence=recurrence,
        start_epoch=start_epoch,
        service_no=service_no,
        service_type=service_type,
    )


def make_detail(
    sch_index: int = 1,
    *,
    start_epoch: int = 100,
    duration_seconds: int = 60,
    enabled: bool = True,
    service_no: int = 2005,
    service_type: int = 107,
    service_priority: int = 0,
    asset_id: str | None = "asset",
    source_id: int | None = 7,
    recurrence: dict[str, Any] | None = None,
) -> SchedulerDetail:
    return SchedulerDetail(
        sch_index=sch_index,
        asset_id=asset_id,
        duration_seconds=duration_seconds,
        recurrence={} if recurrence is None else recurrence,
        service_enabled=enabled,
        service_no=service_no,
        service_priority=service_priority,
        service_type=service_type,
        source_id=source_id,
        start_epoch=start_epoch,
    )


class CountingClient:
    def __init__(self) -> None:
        self.list_calls = 0
        self.detail_calls = 0

    async def list_entries(self, from_epoch: int, to_epoch: int) -> list[SchedulerEntry]:
        del from_epoch, to_epoch
        self.list_calls += 1
        return []

    async def get_detail(self, sch_index: int) -> SchedulerDetail:
        del sch_index
        self.detail_calls += 1
        raise AssertionError("normalized_schedules must not fetch DETAIL")


def test_list_only_normalization_and_end_epoch() -> None:
    normalized = normalize_scheduler_view(SchedulerView(sch_index=1, entry=make_entry()))

    assert normalized.completeness is ScheduleCompleteness.LIST_ONLY
    assert normalized.consistency is ScheduleConsistency.LIST_ONLY
    assert normalized.start_epoch == 100
    assert normalized.duration_seconds == 60
    assert normalized.end_epoch == 160
    assert normalized.service_priority is None
    assert normalized.asset_id is None
    assert normalized.source_id is None
    assert normalized.recurrence.recurring is False
    assert normalized.recurrence.recurrence_detail is None


def test_detail_only_normalization_preserves_opaque_recurrence() -> None:
    recurrence = {
        "type": "weekly",
        "days": [1, 2],
        "exceptions": {"date": "opaque"},
    }
    normalized = normalize_scheduler_view(
        SchedulerView(sch_index=2, detail=make_detail(sch_index=2, recurrence=recurrence))
    )

    assert normalized.completeness is ScheduleCompleteness.DETAIL_ONLY
    assert normalized.consistency is ScheduleConsistency.DETAIL_ONLY
    assert normalized.recurrence.recurring is None
    assert normalized.recurrence.recurrence_detail == (
        ("days", (1, 2)),
        ("exceptions", (("date", "opaque"),)),
        ("type", "weekly"),
    )
    assert normalized.service_type_label == "Target SoC"


def test_matching_list_and_detail_is_complete_with_detail_precedence() -> None:
    normalized = normalize_scheduler_view(
        SchedulerView(
            sch_index=3,
            entry=make_entry(sch_index=3, recurrence=True),
            detail=make_detail(
                sch_index=3,
                service_priority=4,
                recurrence={"type": "once"},
            ),
        )
    )

    assert normalized.completeness is ScheduleCompleteness.COMPLETE
    assert normalized.consistency is ScheduleConsistency.CONSISTENT
    assert normalized.enabled is True
    assert normalized.service_priority == 4
    assert normalized.asset_id == "asset"
    assert normalized.source_id == 7
    assert normalized.recurrence.recurring is True
    assert normalized.recurrence.recurrence_detail == (("type", "once"),)


def test_conflicting_overlap_is_mismatch_and_detail_is_display_precedence() -> None:
    entry = make_entry(
        sch_index=4,
        start_epoch=100,
        duration_seconds=60,
        enabled=True,
        service_no=2005,
        service_type=107,
    )
    detail = make_detail(
        sch_index=4,
        start_epoch=120,
        duration_seconds=30,
        enabled=False,
        service_no=2006,
        service_type=999,
    )
    view = SchedulerView(sch_index=4, entry=entry, detail=detail)

    normalized = normalize_scheduler_view(view)

    assert view.entry is entry
    assert view.detail is detail
    assert normalized.completeness is ScheduleCompleteness.COMPLETE
    assert normalized.consistency is ScheduleConsistency.MISMATCH
    assert normalized.start_epoch == 120
    assert normalized.duration_seconds == 30
    assert normalized.end_epoch == 150
    assert normalized.enabled is False
    assert normalized.service_no == 2006
    assert normalized.service_type == 999
    assert normalized.service_type_label == "Unknown (999)"


def test_manager_accessor_sorts_by_start_then_index_without_network() -> None:
    client = CountingClient()
    manager = SchedulerRefreshManager(client)
    manager._entries = (
        make_entry(sch_index=3, start_epoch=200),
        make_entry(sch_index=2, start_epoch=100),
        make_entry(sch_index=1, start_epoch=100),
    )

    schedules = manager.normalized_schedules

    assert isinstance(schedules, tuple)
    assert [(schedule.start_epoch, schedule.sch_index) for schedule in schedules] == [
        (100, 1),
        (100, 2),
        (200, 3),
    ]
    assert client.list_calls == 0
    assert client.detail_calls == 0


def test_diagnostics_report_only_normalized_aggregate_counts() -> None:
    manager = SchedulerRefreshManager(CountingClient())
    manager._entries = (make_entry(sch_index=1), make_entry(sch_index=2, start_epoch=200))
    manager._details = {
        1: make_detail(sch_index=1, start_epoch=101),
        3: make_detail(sch_index=3, start_epoch=300),
    }

    diagnostics = manager.diagnostics()

    assert diagnostics["normalized_schedule_count"] == 3
    assert diagnostics["complete_schedule_count"] == 1
    assert diagnostics["partial_schedule_count"] == 2
    assert diagnostics["mismatch_count"] == 1
    assert "asset_id" not in str(diagnostics)
    assert "source_id" not in str(diagnostics)
    assert "service_type" not in str(diagnostics)


def test_ownership_stays_unknown_protected_and_source_id_is_not_evidence() -> None:
    normalized = normalize_scheduler_view(
        SchedulerView(
            sch_index=5,
            detail=make_detail(sch_index=5, source_id=1),
            ownership=default_ownership(),
        )
    )

    assert normalized.ownership.ownership is Ownership.UNKNOWN
    assert normalized.ownership.protected is True
    assert normalized.ownership.editable is False
    assert normalized.source_id == 1
    assert normalized.sch_index == 5
    assert normalized.service_no == 2005


def test_empty_cache_accessor_and_diagnostics_are_pure_and_aggregate_only() -> None:
    client = CountingClient()
    manager = SchedulerRefreshManager(client)

    assert manager.normalized_schedules == ()
    diagnostics = manager.diagnostics()

    assert client.list_calls == 0
    assert client.detail_calls == 0
    assert diagnostics["normalized_schedule_count"] == 0
    assert diagnostics["complete_schedule_count"] == 0
    assert diagnostics["partial_schedule_count"] == 0
    assert diagnostics["mismatch_count"] == 0
    assert "sch_index" not in str(diagnostics)
    assert "source_id" not in str(diagnostics)
    assert "recurrence" not in str(diagnostics)


def test_normalized_schedule_rejects_inconsistent_end_epoch() -> None:
    with pytest.raises(ValueError, match="epochs are invalid"):
        NormalizedSchedule(
            sch_index=1,
            start_epoch=100,
            duration_seconds=60,
            end_epoch=100,
            service_no=2005,
            service_type=107,
            enabled=True,
            service_priority=None,
            asset_id=None,
            source_id=None,
            recurrence=normalize_scheduler_view(
                SchedulerView(sch_index=1, entry=make_entry())
            ).recurrence,
            ownership=default_ownership(),
            completeness=ScheduleCompleteness.LIST_ONLY,
            consistency=ScheduleConsistency.LIST_ONLY,
        )


def test_live_writes_remain_disabled() -> None:
    assert LIVE_WRITES_ENABLED is False
