from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.calendar import CalendarEvent
from homeassistant.exceptions import HomeAssistantError

import custom_components.pixiihome_controller.calendar as calendar_module
from custom_components.pixiihome_controller.calendar import (
    PixiiSchedulerCalendar,
    async_setup_entry,
)
from custom_components.pixiihome_controller.const import DOMAIN, LIVE_WRITES_ENABLED, PLATFORMS
from custom_components.pixiihome_controller.exceptions import (
    SchedulerAuthenticationError,
    SchedulerAuthenticationReason,
    SchedulerAuthenticationStage,
    SchedulerProtocolError,
)
from custom_components.pixiihome_controller.protocol.models import SchedulerEntry
from custom_components.pixiihome_controller.scheduler.models import (
    SchedulerView,
    normalize_scheduler_view,
)


class FakeSchedulerManager:
    def __init__(self, schedules: tuple[Any, ...] = ()) -> None:
        self.normalized_schedules = schedules
        self.list_calls: list[tuple[int, int]] = []
        self.detail_calls = 0
        self.error: Exception | None = None

    async def refresh_list(self, from_epoch: int, to_epoch: int) -> tuple[Any, ...]:
        self.list_calls.append((from_epoch, to_epoch))
        if self.error is not None:
            raise self.error
        return ()

    async def refresh_detail(self, sch_index: int) -> Any:
        del sch_index
        self.detail_calls += 1
        raise AssertionError("calendar reads must not call DETAIL")


def schedule(
    sch_index: int,
    *,
    start_epoch: int,
    duration_seconds: int,
    service_type: int = 107,
    enabled: bool = True,
    recurrence: bool = False,
) -> Any:
    entry = SchedulerEntry(
        entry=sch_index,
        duration_seconds=duration_seconds,
        enabled=enabled,
        sch_index=sch_index,
        recurrence=recurrence,
        start_epoch=start_epoch,
        service_no=2000 + sch_index,
        service_type=service_type,
    )
    return normalize_scheduler_view(SchedulerView(sch_index=sch_index, entry=entry))


def entity_for(manager: FakeSchedulerManager) -> PixiiSchedulerCalendar:
    runtime = cast(Any, SimpleNamespace(serial="GATEWAY", scheduler_manager=manager))
    return PixiiSchedulerCalendar(runtime)


def freeze_now(monkeypatch: pytest.MonkeyPatch, epoch: int) -> None:
    now = datetime.fromtimestamp(epoch, UTC)
    monkeypatch.setattr(calendar_module.dt_util, "now", lambda timezone=None: now)


def test_calendar_platform_is_declared() -> None:
    assert "calendar" in PLATFORMS


@pytest.mark.asyncio
async def test_disabled_scheduler_creates_no_calendar_entity() -> None:
    hass = SimpleNamespace(data={DOMAIN: {"entry-a": SimpleNamespace(scheduler_manager=None)}})
    added: list[Any] = []

    def add_entities(new_entities: Any, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        added.extend(new_entities)

    await async_setup_entry(
        cast(Any, hass),
        cast(Any, SimpleNamespace(entry_id="entry-a")),
        add_entities,
    )
    assert added == []


@pytest.mark.asyncio
async def test_configured_scheduler_creates_one_calendar_without_http() -> None:
    manager = FakeSchedulerManager()
    hass = SimpleNamespace(
        data={DOMAIN: {"entry-a": SimpleNamespace(serial="GATEWAY", scheduler_manager=manager)}}
    )
    added: list[Any] = []

    def add_entities(new_entities: Any, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        added.extend(new_entities)

    await async_setup_entry(
        cast(Any, hass),
        cast(Any, SimpleNamespace(entry_id="entry-a")),
        add_entities,
    )

    assert len(added) == 1
    entity = cast(PixiiSchedulerCalendar, added[0])
    device_info = cast(dict[str, Any], entity.device_info)
    assert device_info["identifiers"] == {(DOMAIN, "GATEWAY_schedule_planner")}
    assert device_info["manufacturer"] == "Pixii"

    assert entity.supported_features == 0
    assert manager.list_calls == []
    assert manager.detail_calls == 0


@pytest.mark.asyncio
async def test_calendar_query_performs_one_list_and_zero_detail_with_correct_bounds() -> None:
    manager = FakeSchedulerManager((schedule(1, start_epoch=100, duration_seconds=60),))
    entity = entity_for(manager)
    start = datetime(1970, 1, 1, 1, 0, tzinfo=ZoneInfo("Europe/Stockholm"))
    end = datetime(1970, 1, 1, 3, 0, tzinfo=ZoneInfo("Europe/Stockholm"))

    events = await entity.async_get_events(cast(Any, SimpleNamespace()), start, end)

    assert manager.list_calls == [
        (
            int(calendar_module.dt_util.as_utc(start).timestamp()),
            int(calendar_module.dt_util.as_utc(end).timestamp()),
        )
    ]
    assert manager.detail_calls == 0
    assert len(events) == 1


@pytest.mark.asyncio
async def test_event_mapping_is_timezone_aware_and_uses_normalized_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_now(monkeypatch, 150)
    manager = FakeSchedulerManager((schedule(7, start_epoch=100, duration_seconds=60),))
    entity = entity_for(manager)

    await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(200, UTC),
    )
    event = entity.event

    assert event is not None
    assert isinstance(event, CalendarEvent)
    assert isinstance(event.start, datetime)
    assert isinstance(event.end, datetime)
    assert event.summary == "Target SoC"
    assert event.start == datetime.fromtimestamp(100, UTC)
    assert event.end == datetime.fromtimestamp(160, UTC)
    assert event.start.tzinfo is UTC
    assert event.end.tzinfo is UTC
    assert event.uid == "GATEWAY_scheduler_7"


@pytest.mark.asyncio
async def test_calendar_query_is_dst_safe_and_returns_utc_aware_events() -> None:
    manager = FakeSchedulerManager((schedule(8, start_epoch=1774744200, duration_seconds=3600),))
    entity = entity_for(manager)
    start = datetime(2026, 3, 29, 1, 0, tzinfo=ZoneInfo("Europe/Stockholm"))
    end = datetime(2026, 3, 29, 5, 0, tzinfo=ZoneInfo("Europe/Stockholm"))

    events = await entity.async_get_events(cast(Any, SimpleNamespace()), start, end)

    assert manager.list_calls == [
        (
            int(calendar_module.dt_util.as_utc(start).timestamp()),
            int(calendar_module.dt_util.as_utc(end).timestamp()),
        )
    ]
    assert len(events) == 1
    assert events[0].start == datetime.fromtimestamp(1774744200, UTC)
    assert isinstance(events[0].start, datetime)
    assert events[0].start.tzinfo is UTC


@pytest.mark.asyncio
async def test_calendar_query_filters_exclusive_interval_overlap_and_orders_events() -> None:
    manager = FakeSchedulerManager(
        (
            schedule(4, start_epoch=300, duration_seconds=10),
            schedule(3, start_epoch=250, duration_seconds=100, enabled=False),
            schedule(2, start_epoch=90, duration_seconds=10),
            schedule(1, start_epoch=90, duration_seconds=20, service_type=999),
        )
    )
    entity = entity_for(manager)

    events = await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(100, UTC),
        datetime.fromtimestamp(300, UTC),
    )

    assert [(event.uid, event.summary) for event in events] == [
        ("GATEWAY_scheduler_1", "Unknown (999)"),
        ("GATEWAY_scheduler_3", "Target SoC"),
    ]


@pytest.mark.asyncio
async def test_historical_query_does_not_overwrite_current_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_now(monkeypatch, 150)
    manager = FakeSchedulerManager(
        (schedule(1, start_epoch=100, duration_seconds=100),)
    )
    entity = entity_for(manager)

    await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(400, UTC),
    )
    assert entity.event is not None
    assert entity.event.uid == "GATEWAY_scheduler_1"

    manager.normalized_schedules = (
        schedule(9, start_epoch=10, duration_seconds=20),
    )
    historical_events = await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(50, UTC),
    )

    assert [event.uid for event in historical_events] == ["GATEWAY_scheduler_9"]
    assert entity.event is not None
    assert entity.event.uid == "GATEWAY_scheduler_1"
    assert (entity._state_window_start_epoch, entity._state_window_end_epoch) == (
        0,
        400,
    )
    assert len(manager.list_calls) == 2
    assert manager.detail_calls == 0


@pytest.mark.asyncio
async def test_future_query_does_not_overwrite_current_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_now(monkeypatch, 150)
    manager = FakeSchedulerManager(
        (schedule(1, start_epoch=100, duration_seconds=100),)
    )
    entity = entity_for(manager)

    await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(400, UTC),
    )
    assert entity.event is not None
    assert entity.event.uid == "GATEWAY_scheduler_1"

    manager.normalized_schedules = (
        schedule(9, start_epoch=420, duration_seconds=20),
    )
    future_events = await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(400, UTC),
        datetime.fromtimestamp(500, UTC),
    )

    assert [event.uid for event in future_events] == ["GATEWAY_scheduler_9"]
    assert entity.event is not None
    assert entity.event.uid == "GATEWAY_scheduler_1"
    assert (entity._state_window_start_epoch, entity._state_window_end_epoch) == (
        0,
        400,
    )
    assert len(manager.list_calls) == 2
    assert manager.detail_calls == 0


@pytest.mark.asyncio
async def test_current_query_replaces_state_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_now(monkeypatch, 150)
    manager = FakeSchedulerManager(
        (schedule(1, start_epoch=100, duration_seconds=100),)
    )
    entity = entity_for(manager)

    await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(400, UTC),
    )
    assert entity.event is not None
    assert entity.event.uid == "GATEWAY_scheduler_1"

    manager.normalized_schedules = (
        schedule(2, start_epoch=120, duration_seconds=40, service_type=999),
    )
    current_events = await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(400, UTC),
    )

    assert [event.uid for event in current_events] == ["GATEWAY_scheduler_2"]
    assert entity.event is not None
    assert entity.event.uid == "GATEWAY_scheduler_2"
    assert (entity._state_window_start_epoch, entity._state_window_end_epoch) == (
        0,
        400,
    )
    assert len(manager.list_calls) == 2
    assert manager.detail_calls == 0


@pytest.mark.asyncio
async def test_expired_state_window_returns_no_event_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_now(monkeypatch, 150)
    manager = FakeSchedulerManager(
        (schedule(1, start_epoch=100, duration_seconds=100),)
    )
    entity = entity_for(manager)

    await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(200, UTC),
    )
    assert entity.event is not None

    freeze_now(monkeypatch, 200)
    assert entity.event is None
    assert len(manager.list_calls) == 1
    assert manager.detail_calls == 0


@pytest.mark.asyncio
async def test_disabled_schedule_remains_visible() -> None:
    manager = FakeSchedulerManager(
        (schedule(5, start_epoch=100, duration_seconds=60, enabled=False),)
    )
    entity = entity_for(manager)

    events = await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(200, UTC),
    )

    assert len(events) == 1
    assert events[0].summary == "Target SoC"


@pytest.mark.asyncio
async def test_empty_current_query_clears_state_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_now(monkeypatch, 150)
    manager = FakeSchedulerManager(
        (schedule(6, start_epoch=100, duration_seconds=60),)
    )
    entity = entity_for(manager)

    await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(200, UTC),
    )
    assert entity.event is not None

    manager.normalized_schedules = ()
    events = await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(200, UTC),
    )

    assert events == []
    assert entity.event is None
    assert len(manager.list_calls) == 2
    assert manager.detail_calls == 0


@pytest.mark.asyncio
async def test_recurrence_is_not_interpreted_or_expanded() -> None:
    manager = FakeSchedulerManager(
        (schedule(9, start_epoch=100, duration_seconds=60, recurrence=True),)
    )
    entity = entity_for(manager)

    events = await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(200, UTC),
    )

    assert [event.uid for event in events] == ["GATEWAY_scheduler_9"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        SchedulerAuthenticationError(
            SchedulerAuthenticationReason.SESSION_NOT_AUTHORIZED,
            SchedulerAuthenticationStage.LIST,
        ),
        SchedulerProtocolError("raw Gateway response Sessid ALevel https://secret.example"),
    ],
)
async def test_calendar_read_errors_are_sanitized(error: Exception) -> None:
    manager = FakeSchedulerManager()
    manager.error = error
    entity = entity_for(manager)

    with pytest.raises(
        HomeAssistantError, match="Unable to read the Pixii scheduler calendar"
    ) as raised:
        await entity.async_get_events(
            cast(Any, SimpleNamespace()),
            datetime.fromtimestamp(0, UTC),
            datetime.fromtimestamp(200, UTC),
        )

    assert "Sessid" not in str(raised.value)
    assert "ALevel" not in str(raised.value)
    assert "secret.example" not in str(raised.value)
    assert manager.detail_calls == 0


@pytest.mark.asyncio
async def test_event_property_prefers_active_then_next_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze_now(monkeypatch, 150)
    manager = FakeSchedulerManager(
        (
            schedule(3, start_epoch=300, duration_seconds=30),
            schedule(1, start_epoch=100, duration_seconds=100),
        )
    )
    entity = entity_for(manager)

    await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(400, UTC),
    )
    assert entity.event is not None
    assert entity.event.uid == "GATEWAY_scheduler_1"

    freeze_now(monkeypatch, 250)
    assert entity.event is not None
    assert entity.event.uid == "GATEWAY_scheduler_3"
    assert len(manager.list_calls) == 1
    assert manager.detail_calls == 0


@pytest.mark.asyncio
async def test_calendar_reads_never_publish_mqtt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from homeassistant.components.mqtt import client as mqtt_client

    called = False

    async def unexpected_publish(*args: Any, **kwargs: Any) -> None:
        nonlocal called
        del args, kwargs
        called = True
        raise AssertionError("calendar reads must not publish MQTT")

    monkeypatch.setattr(mqtt_client, "async_publish", unexpected_publish)
    manager = FakeSchedulerManager((schedule(10, start_epoch=100, duration_seconds=60),))
    entity = entity_for(manager)

    await entity.async_get_events(
        cast(Any, SimpleNamespace()),
        datetime.fromtimestamp(0, UTC),
        datetime.fromtimestamp(200, UTC),
    )

    assert called is False


def test_empty_cache_event_is_none_and_no_http_occurs() -> None:
    manager = FakeSchedulerManager()
    entity = entity_for(manager)

    assert entity.event is None
    assert manager.list_calls == []
    assert manager.detail_calls == 0


def test_calendar_is_read_only_and_writes_remain_disabled() -> None:
    entity = entity_for(FakeSchedulerManager())

    assert entity.supported_features == 0
    assert LIVE_WRITES_ENABLED is False


@pytest.mark.asyncio
async def test_calendar_removal_cleans_coordinator_then_ha_resources_once() -> None:
    entity = entity_for(FakeSchedulerManager())
    cleanup_order: list[str] = []

    def remove_coordinator_listener() -> None:
        cleanup_order.append("coordinator")

    def remove_calendar_alarm() -> None:
        cleanup_order.append("alarm")

    class DebouncerProbe:
        def async_cancel(self) -> None:
            cleanup_order.append("debouncer")

    setattr(entity, "_remove_coordinator_listener", remove_coordinator_listener)
    setattr(entity, "_alarm_unsubs", [remove_calendar_alarm])
    setattr(entity, "_event_listener_debouncer", DebouncerProbe())

    await entity.async_will_remove_from_hass()

    assert cleanup_order == ["coordinator", "alarm", "debouncer"]
    assert entity._remove_coordinator_listener is None
    assert entity._alarm_unsubs is None
    assert entity._event_listener_debouncer is None

    await entity.async_will_remove_from_hass()

    assert cleanup_order == ["coordinator", "alarm", "debouncer"]


@pytest.mark.asyncio
async def test_calendar_removal_cleans_ha_resources_without_coordinator_listener() -> None:
    entity = entity_for(FakeSchedulerManager())
    cleanup_order: list[str] = []

    def remove_calendar_alarm() -> None:
        cleanup_order.append("alarm")

    class DebouncerProbe:
        def async_cancel(self) -> None:
            cleanup_order.append("debouncer")

    assert entity.coordinator is None
    assert entity._remove_coordinator_listener is None
    setattr(entity, "_alarm_unsubs", [remove_calendar_alarm])
    setattr(entity, "_event_listener_debouncer", DebouncerProbe())

    await entity.async_will_remove_from_hass()

    assert cleanup_order == ["alarm", "debouncer"]
    assert entity._alarm_unsubs is None
    assert entity._event_listener_debouncer is None

    await entity.async_will_remove_from_hass()

    assert cleanup_order == ["alarm", "debouncer"]
