from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from custom_components.pixiihome_controller.capabilities import (
    MAX_CAPABILITY_FIELDS_PER_FAMILY,
    MAX_CAPABILITY_SERVICES,
    CapabilityRegistry,
)
from custom_components.pixiihome_controller.protocol.mqtt import parse_message
from custom_components.pixiihome_controller.telemetry import TelemetryStore


def test_store_tracks_freshness_and_capabilities():
    now = datetime.now(UTC)
    message = parse_message(
        "pixii/status/GATEWAY/battery",
        '{"batt_soc": 52, "batt_soc_min": 10}',
        received_at=now,
    )
    store = TelemetryStore()
    capabilities = CapabilityRegistry()
    capabilities.observe(message)
    store.apply(message)

    assert store.value("battery", "batt_soc") == 52
    assert store.is_fresh("battery", now=now)
    assert capabilities.supports_field("battery", "batt_soc")
    assert capabilities.supports_family("battery")
    assert not store.is_fresh("core", now=now)
    assert not store.is_fresh("battery", now=now + timedelta(seconds=181))


def test_capability_observations_have_hard_cardinality_bounds() -> None:
    capabilities = CapabilityRegistry()
    telemetry_payload = {
        f"field_{index}": index
        for index in range(MAX_CAPABILITY_FIELDS_PER_FAMILY + 20)
    }
    capabilities.observe(
        parse_message(
            "pixii/status/GATEWAY/battery",
            json.dumps(telemetry_payload),
        )
    )

    for index in range(MAX_CAPABILITY_SERVICES + 10):
        capabilities.observe(
            parse_message(
                "pixii/ack/GATEWAY",
                json.dumps(
                    {
                        "method": "set",
                        "service": f"service_{index}",
                        "response": 65535,
                    }
                ),
            )
        )

    assert len(capabilities.fields["battery"]) == MAX_CAPABILITY_FIELDS_PER_FAMILY
    assert len(capabilities.command_methods) == MAX_CAPABILITY_SERVICES
    assert capabilities.overflow_observations > 0
