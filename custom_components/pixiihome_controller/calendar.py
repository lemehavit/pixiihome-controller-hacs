"""Read-only Pixii scheduler calendar platform."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.components.calendar.const import CalendarEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .devices import PixiiFeature, ensure_gateway_device, feature_device_info
from .price_timeline import price_timeline
from .runtime import RuntimeData
from .scheduler.models import NormalizedSchedule


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one scheduler calendar only when scheduler access is configured."""
    runtime: RuntimeData = hass.data[DOMAIN][entry.entry_id]
    if runtime.scheduler_manager is None and getattr(
        runtime, "price_optimization_controller", None
    ) is None:
        return
    parent_device_id = ensure_gateway_device(hass, entry, runtime.serial)
    entities: list[CalendarEntity] = []
    if runtime.scheduler_manager is not None:
        entities.append(PixiiSchedulerCalendar(runtime, parent_device_id))
    if getattr(runtime, "price_optimization_controller", None) is not None:
        entities.append(PixiiPriceOptimizationCalendar(runtime, parent_device_id))
    async_add_entities(entities)


class PixiiPriceOptimizationCalendar(CalendarEntity):
    """A local timeline of price proposals and last-reconciled actual schedules."""

    _attr_has_entity_name = True
    _attr_translation_key = "price_optimization_calendar"
    _attr_supported_features = CalendarEntityFeature(0)

    def __init__(self, runtime, parent_device_id: str | None = None) -> None:
        self.runtime = runtime
        self.controller = runtime.price_optimization_controller
        self._attr_unique_id = f"{runtime.serial}_price_optimization_calendar"
        self._attr_device_info = feature_device_info(
            runtime.serial, PixiiFeature.PRICE_OPTIMIZATION, parent_device_id
        )
        self._remove_listener = None

    async def async_added_to_hass(self) -> None:
        self._remove_listener = self.controller.add_listener(self.schedule_update_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        await super().async_will_remove_from_hass()

    def _events(self, now: datetime) -> list[CalendarEvent]:
        events = []
        for item in price_timeline(self.controller, now):
            direction = "Laddning" if item["direction"] == "charge" else "Urladdning"
            status = {
                "scheduled": "Skapat i Pixii",
                "planned": "Planförslag",
                "unverified": "Efterkontroll krävs",
            }[item["schedule_status"]]
            mode = {
                "export": "Export till elnätet",
                "self_consumption": "Egen förbrukning, utan export: batteriet täcker husets "
                "förbrukning upp till maxeffekten, resten tas från elnätet",
                None: "Laddning från elnätet",
            }[item["discharge_mode"]]
            # Without export the power is a ceiling: the battery follows the
            # house load up to it. Export and charging run at that power.
            power = (
                f"max {item['power_kw']:g} kW"
                if item["discharge_mode"] == "self_consumption"
                else f"{item['power_kw']:g} kW"
            )
            profit = item.get("expected_profit_sek")
            summary = f"{direction} · {power}"
            if profit is not None:
                summary += f" · +{_kr(profit)} kr"
            events.append(CalendarEvent(
                start=datetime.fromisoformat(item["start"]),
                end=datetime.fromisoformat(item["end"]),
                summary=f"{summary} · {status}",
                description="\n".join((
                    f"Prisoptimering: {status}",
                    mode,
                    _economics_text(item),
                    "Tider visas i Home Assistants tidszon (Europe/Stockholm rekommenderas).",
                    "Skapat i Pixii betyder verifierat vid senaste efterkontrollen.",
                    f"Styrstatus: {self.controller.status}",
                    f"Orsak: {self.controller.reason or '–'}",
                    f"Schema-ID: {item['message_id'] or 'Inte skapat'}",
                )),
                uid=f"{self.runtime.serial}_{item['uid']}",
            ))
        return events

    @property
    def event(self) -> CalendarEvent | None:
        return next(iter(self._events(datetime.now(UTC))), None)

    async def async_get_events(self, hass, start_date: datetime, end_date: datetime):
        # Include the requested historical window while records remain in memory.
        return [
            event for event in self._events(start_date)
            if event.start < end_date and event.end > start_date
        ]


def _kr(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def _economics_text(item: dict) -> str:
    """Expected money for one interval, in the plan's own figures."""
    if "net_profit_sek_per_kwh" not in item:
        return (
            "Förväntad förtjänst: okänd (planen har räknats om sedan schemat skapades)"
        )
    price = _kr(item["price_sek_per_kwh"])
    net = _kr(item["net_profit_sek_per_kwh"])
    if item["direction"] == "charge":
        return (
            f"Köper ca {_kr(item['energy_kwh'])} kWh à {price} kr/kWh "
            f"(ca {_kr(item['charge_cost_sek'])} kr). Förtjänsten räknas på "
            f"urladdningen som använder energin; väntat netto {net} kr/kWh."
        )
    return (
        f"Förväntad förtjänst: {_kr(item['expected_profit_sek'])} kr "
        f"(netto {net} kr/kWh efter verkningsgrad och kostnader; "
        f"elpris {price} kr/kWh, energin kostade {_kr(item['reference_price_sek_per_kwh'])} "
        "kr/kWh)"
    )


class PixiiSchedulerCalendar(CalendarEntity):
    """Expose the shared normalized scheduler inventory as calendar events."""

    _attr_has_entity_name = True
    _attr_translation_key = "schedules"
    _attr_supported_features = CalendarEntityFeature(0)

    def __init__(self, runtime: RuntimeData, parent_device_id: str | None = None) -> None:
        """Initialize the read-only scheduler calendar."""
        self.runtime = runtime
        self.coordinator = getattr(runtime, "scheduler_coordinator", None)
        self._state_schedules: tuple[NormalizedSchedule, ...] = ()
        self._state_window_start_epoch: int | None = None
        self._state_window_end_epoch: int | None = None
        self._remove_coordinator_listener = None
        self._attr_unique_id = f"{runtime.serial}_scheduler_calendar"
        self._attr_device_info = feature_device_info(
            runtime.serial,
            PixiiFeature.SCHEDULE_PLANNER,
            parent_device_id,
        )

    async def async_added_to_hass(self) -> None:
        if self.coordinator is not None:
            self._remove_coordinator_listener = self.coordinator.add_listener(
                self._coordinator_changed
            )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_coordinator_listener is not None:
            self._remove_coordinator_listener()
            self._remove_coordinator_listener = None
        await super().async_will_remove_from_hass()

    def _coordinator_changed(self) -> None:
        self.schedule_update_ha_state()

    @property
    def event(self) -> CalendarEvent | None:
        """Return the active event, otherwise the next upcoming event."""
        snapshot = self.coordinator.snapshot if self.coordinator is not None else None
        if snapshot is not None:
            if snapshot.data_status == "stale":
                return None
            schedules = snapshot.schedules
            window_start_epoch = snapshot.window_start_epoch
            window_end_epoch = snapshot.window_end_epoch
        else:
            schedules = self._state_schedules
            window_start_epoch = self._state_window_start_epoch
            window_end_epoch = self._state_window_end_epoch

        now_epoch = _now_epoch()
        if (
            window_start_epoch is None
            or window_end_epoch is None
            or not window_start_epoch <= now_epoch < window_end_epoch
        ):
            return None
        for schedule in _ordered_schedules(schedules):
            if schedule.enabled and schedule.start_epoch <= now_epoch < schedule.end_epoch:
                return _calendar_event(self.runtime.serial, schedule)
        for schedule in _ordered_schedules(schedules):
            if schedule.enabled and schedule.start_epoch > now_epoch:
                return _calendar_event(self.runtime.serial, schedule)
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Read a bounded window through the shared scheduler pipeline."""
        del hass
        manager = self.runtime.scheduler_manager
        if manager is None:
            raise HomeAssistantError("Pixii scheduler access is not configured")

        from_epoch = int(dt_util.as_utc(start_date).timestamp())
        to_epoch = int(dt_util.as_utc(end_date).timestamp())
        try:
            if self.coordinator is not None:
                snapshot = await self.coordinator.async_read_window(from_epoch, to_epoch)
                if snapshot.data_status == "stale":
                    raise HomeAssistantError("Unable to read the Pixii scheduler calendar")
                schedules = _ordered_schedules(snapshot.schedules)
                current_snapshot = self.coordinator.snapshot
                if (
                    current_snapshot is snapshot
                    and from_epoch <= _now_epoch() < to_epoch
                ):
                    self._state_schedules = schedules
                    self._state_window_start_epoch = snapshot.window_start_epoch
                    self._state_window_end_epoch = snapshot.window_end_epoch
                    if self.hass is not None:
                        self.async_write_ha_state()
            else:
                await manager.refresh_list(from_epoch, to_epoch)
                schedules = _ordered_schedules(manager.normalized_schedules)
                now_epoch = _now_epoch()
                if from_epoch <= now_epoch < to_epoch:
                    self._state_schedules = schedules
                    self._state_window_start_epoch = from_epoch
                    self._state_window_end_epoch = to_epoch
                    if self.hass is not None:
                        self.async_write_ha_state()
        except Exception as err:  # noqa: BLE001 - expose only a sanitized read error
            raise HomeAssistantError("Unable to read the Pixii scheduler calendar") from err

        return [
            _calendar_event(self.runtime.serial, schedule)
            for schedule in schedules
            if schedule.end_epoch > from_epoch and schedule.start_epoch < to_epoch
        ]


def _now_epoch() -> int:
    return int(dt_util.now(UTC).timestamp())


def _ordered_schedules(
    schedules: tuple[NormalizedSchedule, ...],
) -> tuple[NormalizedSchedule, ...]:
    return tuple(sorted(schedules, key=lambda schedule: (schedule.start_epoch, schedule.sch_index)))


def _calendar_event(serial: str, schedule: NormalizedSchedule) -> CalendarEvent:
    return CalendarEvent(
        start=dt_util.utc_from_timestamp(schedule.start_epoch),
        end=dt_util.utc_from_timestamp(schedule.end_epoch),
        summary=schedule.service_type_label,
        description=_calendar_description(schedule),
        uid=f"{serial}_scheduler_{schedule.sch_index}",
    )


def _calendar_description(schedule: NormalizedSchedule) -> str:
    priority = (
        str(schedule.service_priority)
        if schedule.service_priority is not None
        else "Unknown"
    )
    return "\n".join(
        (
            f"Scheduler ID: {schedule.sch_index}",
            f"Duration: {_duration_label(schedule.duration_seconds)}",
            f"Enabled: {'Yes' if schedule.enabled else 'No'}",
            f"Priority: {priority}",
            f"Ownership: {schedule.ownership.ownership.value.replace('_', ' ').title()}",
            f"Protection: {'Protected' if schedule.ownership.protected else 'Unprotected'}",
            f"Completeness: {schedule.completeness.value.replace('_', ' ').title()}",
            f"Consistency: {schedule.consistency.value.replace('_', ' ').title()}",
        )
    )


def _duration_label(duration_seconds: int) -> str:
    minutes, seconds = divmod(duration_seconds, 60)
    if seconds:
        return f"{minutes} min {seconds} sec"
    return f"{minutes} min"
