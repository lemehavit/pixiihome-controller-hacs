"""Home Assistant MQTT subscription transport.

This module subscribes only to Pixii telemetry. The CommandManager owns the
per-entry ACK subscription and any future control publish through HA MQTT APIs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from homeassistant.components.mqtt.client import async_subscribe
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.components.mqtt.util import async_wait_for_mqtt_client
from homeassistant.core import HomeAssistant, callback

from .const import STATUS_FAMILIES, TOPIC_TEMPLATES
from .exceptions import MqttParseError, MqttUnavailable
from .protocol.models import ParsedMqttMessage
from .protocol.mqtt import parse_message

MessageHandler = Callable[[ParsedMqttMessage], None]


@dataclass(slots=True)
class PixiiMqttTransport:
    """Subscriptions through HA's existing MQTT integration only."""

    hass: HomeAssistant
    serial: str
    on_message: MessageHandler
    _unsubscribers: list[Callable[[], None]] = field(default_factory=list)

    async def start(self) -> None:
        if not await async_wait_for_mqtt_client(self.hass):
            raise MqttUnavailable("Home Assistant MQTT is not connected")
        topics = [
            template.format(serial=self.serial, family=family)
            for template in TOPIC_TEMPLATES
            for family in STATUS_FAMILIES
        ]
        for topic in topics:
            unsub = await async_subscribe(
                self.hass,
                topic,
                self._message_received,
                qos=0,
                encoding=None,
            )
            self._unsubscribers.append(unsub)

    async def stop(self) -> None:
        while self._unsubscribers:
            unsubscribe = self._unsubscribers.pop()
            unsubscribe()

    @callback
    def _message_received(self, message: ReceiveMessage) -> None:
        payload = message.payload
        try:
            parsed = parse_message(message.topic, payload)
        except MqttParseError:
            return
        if parsed.serial == self.serial:
            self.on_message(parsed)
