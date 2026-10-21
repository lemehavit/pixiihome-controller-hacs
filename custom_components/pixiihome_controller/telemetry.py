"""Push-based normalized telemetry state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .const import STALE_AFTER_SECONDS
from .protocol.models import ParsedMqttMessage

Listener = Callable[[], None]


@dataclass(slots=True)
class TelemetryStore:
    """Stores the latest payload per family and notifies entities on changes."""

    payloads: dict[str, dict[str, Any]] = field(default_factory=dict)
    received_at: dict[str, datetime] = field(default_factory=dict)
    topics: dict[str, str] = field(default_factory=dict)
    revision: int = 0
    _listeners: list[Listener] = field(default_factory=list)

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    def apply(self, message: ParsedMqttMessage) -> None:
        if not isinstance(message.payload, dict):
            return
        self.payloads[message.family] = dict(message.payload)
        self.received_at[message.family] = message.received_at
        self.topics[message.family] = message.topic
        self.revision += 1
        for listener in tuple(self._listeners):
            listener()

    def payload(self, family: str) -> dict[str, Any]:
        return self.payloads.get(family, {})

    def value(self, family: str, key: str) -> Any:
        return self.payload(family).get(key)

    def age(self, family: str, now: datetime | None = None) -> timedelta | None:
        received = self.received_at.get(family)
        if received is None:
            return None
        now = now or datetime.now(UTC)
        if received.tzinfo is None:
            received = received.replace(tzinfo=UTC)
        return max(now - received, timedelta(0))

    def is_fresh(
        self,
        family: str,
        *,
        max_age_seconds: float = STALE_AFTER_SECONDS,
        now: datetime | None = None,
    ) -> bool:
        age = self.age(family, now)
        return age is not None and age.total_seconds() <= max_age_seconds

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {family: dict(payload) for family, payload in self.payloads.items()}
