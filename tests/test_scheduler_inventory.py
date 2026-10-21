from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from custom_components.pixiihome_controller.calendar import PixiiSchedulerCalendar
from custom_components.pixiihome_controller.const import LIVE_WRITES_ENABLED
from custom_components.pixiihome_controller.exceptions import SchedulerProtocolError
from custom_components.pixiihome_controller.protocol.models import SchedulerDetail, SchedulerEntry
from custom_components.pixiihome_controller.protocol.scheduler import parse_detail_response
from custom_components.pixiihome_controller.scheduler.coordinator import (
    SchedulerInventoryCoordinator,
    inventory_attributes,
)
from custom_components.pixiihome_controller.scheduler.manager import SchedulerRefreshManager
from custom_components.pixiihome_controller.scheduler.models import (
    SchedulerView,
    default_ownership,
    normalize_scheduler_view,
)
from custom_components.pixiihome_controller.sensor import PixiiSchedulerInventorySensor


class FakeClient:
    def __init__(
        self,
        entries: tuple[SchedulerEntry, ...],
        details: dict[int, SchedulerDetail],
    ) -> None:
        self.entries = entries
        self.details = details
        self.list_calls: list[tuple[int, int]] = []
        self.detail_calls: list[int] = []
        self.fail_list = False
        self.fail_details: set[int] = set()
        self.list_gate: asyncio.Event | None = None
        self.window_entries: dict[int, tuple[SchedulerEntry, ...]] = {}

    async def list_entries(self, from_epoch: int, to_epoch: int) -> list[SchedulerEntry]:
        self.list_calls.append((from_epoch, to_epoch))
        if self.list_gate is not None:
            await self.list_gate.wait()
        if self.fail_list:
            raise RuntimeError("LIST failed")
        return list(self.window_entries.get(from_epoch, self.entries))

    async def get_detail(self, sch_index: int) -> SchedulerDetail:
        self.detail_calls.append(sch_index)
        if sch_index in self.fail_details:
            raise RuntimeError("DETAIL failed")
        return self.details[sch_index]

    def diagnostics(self) -> dict[str, object]:
        return {"sessid_in_memory": False}


def make_entry(
    sch_index: int,
    *,
    start_epoch: int,
    duration_seconds: int = 60,
    enabled: bool = True,
    service_type: int = 107,
) -> SchedulerEntry:
    return SchedulerEntry(
        entry=sch_index,
        duration_seconds=duration_seconds,
        enabled=enabled,
        sch_index=sch_index,
        recurrence=False,
        start_epoch=start_epoch,
        service_no=2000 + sch_index,
        service_type=service_type,
    )


def make_detail(entry: SchedulerEntry, *, priority: int = 4) -> SchedulerDetail:
    return SchedulerDetail(
        sch_index=entry.sch_index,
        asset_id=f"asset-{entry.sch_index}",
        duration_seconds=entry.duration_seconds,
        recurrence={},
        service_enabled=entry.enabled,
        service_no=entry.service_no,
        service_priority=priority,
        service_type=entry.service_type,
        source_id=7,
        start_epoch=entry.start_epoch,
    )


def make_coordinator(
    entries: tuple[SchedulerEntry, ...],
    *,
    now_epoch: int = 1_000_000,
) -> tuple[SchedulerInventoryCoordinator, FakeClient, list[int]]:
    client = FakeClient(entries, {entry.sch_index: make_detail(entry) for entry in entries})
    manager = SchedulerRefreshManager(client)
    now = [now_epoch]
    coordinator = SchedulerInventoryCoordinator(
        SimpleNamespace(),
        manager,
        clock=lambda: datetime.fromtimestamp(now[0], UTC),
    )
    return coordinator, client, now


@pytest.mark.asyncio
async def test_empty_list_is_current_zero_and_sensor_available() -> None:
    coordinator, _client, _now = make_coordinator(())
    await coordinator.async_refresh()

    snapshot = coordinator.snapshot
    assert snapshot is not None
    assert snapshot.data_status == "current"
    assert snapshot.available is True
    assert snapshot.schedules == ()

    sensor = PixiiSchedulerInventorySensor(
        cast(
            Any,
            SimpleNamespace(serial="GATEWAY", scheduler_coordinator=coordinator),
        )
    )
    assert sensor.available is True
    assert sensor.native_value == 0
    assert sensor.extra_state_attributes["schedules"] == []


@pytest.mark.asyncio
async def test_failed_list_is_unavailable_and_never_zero() -> None:
    coordinator, client, _now = make_coordinator(())
    client.fail_list = True
    await coordinator.async_refresh()

    sensor = PixiiSchedulerInventorySensor(
        cast(
            Any,
            SimpleNamespace(serial="GATEWAY", scheduler_coordinator=coordinator),
        )
    )
    assert sensor.available is False
    assert sensor.native_value is None
    assert sensor.extra_state_attributes["data_status"] == "stale"


@pytest.mark.asyncio
async def test_previous_snapshot_becomes_stale_without_losing_bounded_records() -> None:
    entry = make_entry(1, start_epoch=1_000_100)
    coordinator, client, _now = make_coordinator((entry,))
    await coordinator.async_refresh()
    client.fail_list = True
    await coordinator.async_refresh()

    snapshot = coordinator.snapshot
    assert snapshot is not None
    assert snapshot.data_status == "stale"
    assert [schedule.sch_index for schedule in snapshot.schedules] == [1]


@pytest.mark.asyncio
async def test_detail_failure_keeps_list_only_record_and_partial_status() -> None:
    entry = make_entry(1, start_epoch=1_000_100)
    coordinator, client, _now = make_coordinator((entry,))
    client.fail_details.add(1)
    await coordinator.async_refresh()

    snapshot = coordinator.snapshot
    assert snapshot is not None
    assert snapshot.data_status == "partial"
    assert snapshot.schedules[0].completeness.value == "list_only"
    assert snapshot.schedules[0].ownership.protected is True
    assert snapshot.schedules[0].ownership.editable is False


@pytest.mark.asyncio
async def test_active_and_upcoming_recompute_without_second_list() -> None:
    entry = make_entry(1, start_epoch=1_000_100, duration_seconds=100)
    coordinator, client, now = make_coordinator((entry,), now_epoch=1_000_000)
    await coordinator.async_refresh()
    assert coordinator.snapshot is not None
    assert coordinator.snapshot.active_count == 0
    assert coordinator.snapshot.upcoming_24h_count == 1
    assert len(client.list_calls) == 1

    now[0] = 1_000_100
    await coordinator.async_recompute_boundaries()
    assert coordinator.snapshot is not None
    assert coordinator.snapshot.active_count == 1
    assert coordinator.snapshot.upcoming_24h_count == 0
    assert len(client.list_calls) == 1



@pytest.mark.asyncio
async def test_upcoming_count_includes_exact_24_hour_boundary() -> None:
    entry = make_entry(1, start_epoch=1_086_400)
    coordinator, _client, now = make_coordinator((entry,), now_epoch=1_000_000)
    await coordinator.async_refresh()
    assert coordinator.snapshot is not None
    assert coordinator.snapshot.upcoming_24h_count == 1
    old_bounds = (
        coordinator.snapshot.window_start_epoch,
        coordinator.snapshot.window_end_epoch,
    )
    now[0] = 1_000_900
    await coordinator.async_recompute_boundaries()
    assert (
        coordinator.snapshot.window_start_epoch,
        coordinator.snapshot.window_end_epoch,
    ) == old_bounds

@pytest.mark.asyncio
async def test_concurrent_refreshes_are_single_flight() -> None:
    entry = make_entry(1, start_epoch=1_000_100)
    coordinator, client, _now = make_coordinator((entry,))
    client.list_gate = asyncio.Event()
    first = asyncio.create_task(coordinator.async_refresh())
    second = asyncio.create_task(coordinator.async_refresh())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert len(client.list_calls) == 1
    client.list_gate.set()
    await asyncio.gather(first, second)
    assert len(client.list_calls) == 1


@pytest.mark.asyncio
async def test_custom_window_reads_serialize_with_canonical_refresh() -> None:
    first = make_entry(1, start_epoch=1_000_100)
    second = make_entry(2, start_epoch=1_000_200)
    coordinator, client, _now = make_coordinator((first,))
    client.details[2] = make_detail(second)
    client.window_entries[913_500] = (first,)
    client.window_entries[10] = (second,)
    client.list_gate = asyncio.Event()

    custom = asyncio.create_task(coordinator.async_read_window(10, 20))
    await asyncio.sleep(0)
    canonical = asyncio.create_task(coordinator.async_refresh())
    await asyncio.sleep(0)
    assert len(client.list_calls) == 1

    client.list_gate.set()
    custom_snapshot, canonical_snapshot = await asyncio.gather(custom, canonical)
    assert [schedule.sch_index for schedule in custom_snapshot.schedules] == [2]
    assert coordinator.snapshot is not None
    assert [schedule.sch_index for schedule in coordinator.snapshot.schedules] == [1]



@pytest.mark.asyncio
async def test_uncovered_current_window_reads_requested_bounds() -> None:
    entry = make_entry(1, start_epoch=1_000_100)
    coordinator, client, _now = make_coordinator((entry,))
    await coordinator.async_refresh()
    snapshot = await coordinator.async_read_window(999_900, 1_691_200)
    assert (snapshot.window_start_epoch, snapshot.window_end_epoch) == (999_900, 1_691_200)
    assert client.list_calls[-1] == (999_900, 1_691_200)


@pytest.mark.asyncio
async def test_custom_window_rejects_invalid_or_unbounded_ranges() -> None:
    coordinator, _client, _now = make_coordinator(())
    with pytest.raises(ValueError):
        await coordinator.async_read_window(0, 32 * 24 * 60 * 60)
    with pytest.raises(ValueError):
        await coordinator.async_read_window(20, 10)

@pytest.mark.asyncio
async def test_sensor_and_calendar_share_one_coordinator_snapshot() -> None:
    entry = make_entry(1, start_epoch=1_000_100)
    coordinator, client, _now = make_coordinator((entry,))
    manager = coordinator.manager
    await coordinator.async_refresh()
    sensor = PixiiSchedulerInventorySensor(
        cast(
            Any,
            SimpleNamespace(serial="GATEWAY", scheduler_coordinator=coordinator),
        )
    )
    calendar = PixiiSchedulerCalendar(
        cast(
            Any,
            SimpleNamespace(
                serial="GATEWAY",
                scheduler_manager=manager,
                scheduler_coordinator=coordinator,
            ),
        )
    )

    events = await calendar.async_get_events(
        SimpleNamespace(),
        datetime.fromtimestamp(1_000_000, UTC),
        datetime.fromtimestamp(1_000_200, UTC),
    )
    assert len(events) == 1
    assert sensor.native_value == len(events)
    assert len(client.list_calls) == 1


@pytest.mark.asyncio
async def test_calendar_event_is_rich_and_uid_stable() -> None:
    entry = make_entry(4192, start_epoch=1_000_100, duration_seconds=3600)
    coordinator, _client, _now = make_coordinator((entry,))
    await coordinator.async_refresh()
    calendar = PixiiSchedulerCalendar(
        cast(
            Any,
            SimpleNamespace(
                serial="GATEWAY",
                scheduler_manager=coordinator.manager,
                scheduler_coordinator=coordinator,
            ),
        )
    )
    events = await calendar.async_get_events(
        SimpleNamespace(),
        datetime.fromtimestamp(1_000_000, UTC),
        datetime.fromtimestamp(1_004_000, UTC),
    )

    assert events[0].summary == "Target SoC"
    assert events[0].uid == "GATEWAY_scheduler_4192"
    assert events[0].location is None
    assert "Scheduler ID: 4192" in (events[0].description or "")
    assert "Duration: 60 min" in (events[0].description or "")
    assert "Target SoC" not in (events[0].description or "service_parameters")


@pytest.mark.asyncio
async def test_inventory_attributes_are_json_safe_bounded_and_credential_free() -> None:
    entries = tuple(make_entry(index, start_epoch=1_000_100 + index) for index in range(100))
    coordinator, client, _now = make_coordinator(entries)
    for index, detail in client.details.items():
        client.details[index] = replace(
            detail,
            recurrence={
                **{f"key-{item}": "x" * 256 for item in range(31)},
                "number": 10**4000,
            },
        )
    await coordinator.async_refresh()
    snapshot = coordinator.snapshot
    assert snapshot is not None
    attributes = inventory_attributes(snapshot)
    encoded = json.dumps(attributes, separators=(",", ":"))
    schedules = attributes["schedules"]
    assert isinstance(schedules, list)
    assert len(schedules) == 100
    assert len(encoded) < 250_000
    assert all(secret not in encoded for secret in ("password", "Sessid", "ALevel", "cookies"))
    assert all("service_parameters" not in schedule for schedule in schedules)



def _detail_payload(
    *,
    asset_id: object = "mqtt_control",
    recurrence: object = None,
    source_id: object = 7,
    start_time: object = 1_000_100,
) -> dict[str, object]:
    return {
        "Omnibus": {
            "msg": [1, 1, 7, 3, 255, 2, 101, 200, 1, 33, 0, 0],
            "data": {
                "asset_id": asset_id,
                "duration": 60,
                "recurrence": {} if recurrence is None else recurrence,
                "service_enabled": 1,
                "service_no": 2001,
                "service_prio": 4,
                "service_type": 107,
                "source_id": source_id,
                "start_time": start_time,
            },
        }
    }


def test_detail_metadata_is_bounded_and_asset_ids_are_safe() -> None:
    oversized_recurrence = {str(index): index for index in range(129)}
    with pytest.raises(SchedulerProtocolError):
        parse_detail_response(_detail_payload(recurrence=oversized_recurrence))
    assert parse_detail_response(
        _detail_payload(asset_id="password-token")
    ).asset_id is None
    with pytest.raises(SchedulerProtocolError):
        parse_detail_response(_detail_payload(source_id=2_147_483_648))
    with pytest.raises(SchedulerProtocolError):
        parse_detail_response(_detail_payload(recurrence={"value": float("nan")}))
    with pytest.raises(SchedulerProtocolError):
        parse_detail_response(_detail_payload(recurrence={"value": 10**33}))
    with pytest.raises(SchedulerProtocolError):
        parse_detail_response(_detail_payload(start_time=1.5))
    with pytest.raises(SchedulerProtocolError):
        parse_detail_response(_detail_payload(start_time=10**4_000))
    invalid_message = _detail_payload()
    cast(dict[str, Any], invalid_message["Omnibus"])["msg"] = [float("inf")]
    with pytest.raises(SchedulerProtocolError):
        parse_detail_response(invalid_message)
    with pytest.raises(SchedulerProtocolError):
        parse_detail_response(
            _detail_payload(
                recurrence={f"{'k' * 250}{item}": "x" * 256 for item in range(32)}
            )
        )
    invalid_detail = _detail_payload()
    cast(dict[str, Any], invalid_detail["Omnibus"])["msg"][-1] = 99
    with pytest.raises(SchedulerProtocolError):
        parse_detail_response(invalid_detail)


@pytest.mark.asyncio
async def test_public_recurrence_metadata_filters_sensitive_keys() -> None:
    entry = make_entry(1, start_epoch=1_000_100)
    coordinator, client, _now = make_coordinator((entry,))
    client.details[1] = replace(
        client.details[1],
        recurrence={"access_token": "secret", "public_note": "password=secret"},
    )
    await coordinator.async_refresh()
    snapshot = coordinator.snapshot
    assert snapshot is not None
    encoded = json.dumps(inventory_attributes(snapshot))
    assert "access_token" not in encoded
    assert "password=secret" not in encoded
    assert "secret" not in encoded

@pytest.mark.asyncio
async def test_oversized_list_is_stale_and_not_truncated() -> None:
    entries = tuple(make_entry(index, start_epoch=1_000_100 + index) for index in range(101))
    coordinator, _client, _now = make_coordinator(entries)
    await coordinator.async_refresh()

    assert coordinator.snapshot is not None
    assert coordinator.snapshot.data_status == "stale"
    assert coordinator.snapshot.schedules == ()


@pytest.mark.asyncio
async def test_no_blind_retry_after_failure() -> None:
    coordinator, client, _now = make_coordinator(())
    client.fail_list = True
    await coordinator.async_refresh()
    await asyncio.sleep(0)
    assert len(client.list_calls) == 1
    assert coordinator.update_interval.total_seconds() == 900


@pytest.mark.asyncio
async def test_detail_cache_retention_is_targeted_by_list_fingerprint() -> None:
    first = make_entry(1, start_epoch=1_000_100)
    second = make_entry(2, start_epoch=1_000_200)
    client = FakeClient((first, second), {1: make_detail(first), 2: make_detail(second)})
    manager = SchedulerRefreshManager(client)

    await manager.refresh_list(0, 2_000_000)
    await manager.refresh_detail(1)
    await manager.refresh_detail(2)
    client.entries = (first, second)
    await manager.refresh_list(0, 2_000_000)
    assert manager.detail_for(1) is not None
    assert manager.detail_for(2) is not None

    changed_second = make_entry(2, start_epoch=1_000_300)
    client.entries = (first, changed_second)
    await manager.refresh_list(0, 2_000_000)
    assert manager.detail_for(1) is not None
    assert manager.detail_for(2) is None

    client.entries = (changed_second,)
    await manager.refresh_list(0, 2_000_000)
    assert manager.detail_for(1) is None


@pytest.mark.asyncio
async def test_configured_sensor_is_created_and_unconfigured_sensor_is_absent() -> None:
    coordinator, _client, _now = make_coordinator(())
    configured_runtime = SimpleNamespace(
        serial="GATEWAY",
        scheduler_coordinator=coordinator,
        telemetry=SimpleNamespace(),
        battery=SimpleNamespace(),
    )
    hass = SimpleNamespace(data={"pixiihome_controller": {"entry": configured_runtime}})
    configured: list[object] = []
    from custom_components.pixiihome_controller.sensor import async_setup_entry

    await async_setup_entry(
        hass,
        SimpleNamespace(entry_id="entry"),
        lambda entities: configured.extend(entities),
    )
    assert any(isinstance(entity, PixiiSchedulerInventorySensor) for entity in configured)

    disabled_runtime = SimpleNamespace(
        serial="GATEWAY",
        scheduler_coordinator=None,
        telemetry=SimpleNamespace(),
        battery=SimpleNamespace(),
    )
    hass.data["pixiihome_controller"]["entry"] = disabled_runtime
    disabled: list[object] = []
    await async_setup_entry(
        hass,
        SimpleNamespace(entry_id="entry"),
        lambda entities: disabled.extend(entities),
    )
    assert not any(isinstance(entity, PixiiSchedulerInventorySensor) for entity in disabled)


def test_inventory_is_observation_only() -> None:
    assert LIVE_WRITES_ENABLED is False


def test_matching_detail_enriches_without_duplicate_schedule() -> None:
    entry = make_entry(1, start_epoch=1_000_100)
    detail = make_detail(entry)
    view = normalize_scheduler_view(
        SchedulerView(
            sch_index=entry.sch_index,
            entry=entry,
            detail=detail,
            ownership=default_ownership(),
        )
    )
    assert view.sch_index == entry.sch_index
    assert view.completeness.value == "complete"
