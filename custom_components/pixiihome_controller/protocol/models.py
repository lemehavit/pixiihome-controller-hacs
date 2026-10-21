"""Typed protocol boundary models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ParsedMqttMessage:
    """One validated Pixii MQTT message."""

    serial: str
    family: str
    payload: Any
    topic: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class MqttAck:
    """Typed fields shared by documented and observed Gateway ACKs."""

    method: str
    response: int
    service: str
    m_id: int | str | None
    source_id: int | None
    response_text: str | None
    service_status: int | None
    service_time_left: int | None
    service_duration_left: int | None
    duration: int | None
    recurrence: dict[str, Any] | None
    socref: int | float | None
    values: dict[str, Any]

    @property
    def time_left(self) -> int | None:
        """Return the Gateway's remaining-service field under either name."""
        return (
            self.service_time_left
            if self.service_time_left is not None
            else self.service_duration_left
        )


@dataclass(frozen=True, slots=True)
class SchedulerListRequest:
    from_epoch: int
    to_epoch: int
    message: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SchedulerEntry:
    entry: int
    duration_seconds: int
    enabled: bool
    sch_index: int
    recurrence: bool
    start_epoch: int
    service_no: int
    service_type: int


@dataclass(frozen=True, slots=True)
class SchedulerDetailRequest:
    sch_index: int
    message: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class SchedulerDetail:
    sch_index: int
    asset_id: str | None
    duration_seconds: int
    recurrence: dict[str, Any]
    service_enabled: bool
    service_no: int
    service_priority: int
    service_type: int
    source_id: int | None
    start_epoch: int
