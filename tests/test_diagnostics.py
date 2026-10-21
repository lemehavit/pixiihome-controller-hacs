from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from custom_components.pixiihome_controller.const import DOMAIN
from custom_components.pixiihome_controller.diagnostics import (
    async_get_config_entry_diagnostics,
)


@pytest.mark.asyncio
async def test_diagnostics_redact_identity_and_omit_raw_payloads():
    runtime = SimpleNamespace(
        serial="TEST_GATEWAY_001",
        release_gate=SimpleNamespace(
            live_writes_enabled=False,
            infrastructure_status="partially_live_validated",
            blocked_reasons={
                "release_writes_disabled",
                "state_changing_control_not_live_validated",
            },
        ),
        telemetry=SimpleNamespace(
            payloads={"battery": {"token": "must not appear"}},
            is_fresh=lambda family, now: family == "battery",
        ),
        capabilities=SimpleNamespace(
            messages_seen=1,
            snapshot=lambda: {"families": ["battery"], "fields": {"battery": ["batt_soc"]}},
        ),
        command_manager=SimpleNamespace(
            diagnostics=lambda: {
                "ready": False,
                "write_gate_enabled": False,
                "control_infrastructure_status": "partially_live_validated",
                "read_only_command_path_status": "live_validated",
                "state_changing_command_path_status": "not_live_validated",
                "control_block_reasons": ["release_gate_closed"],
                "pending_command_count": 0,
                "last_sanitized_result": None,
                "last_error": None,
            }
        ),
        battery=SimpleNamespace(
            raw_soc=50.0,
            usable_soc=50.0,
            stored_energy_kwh=6.25,
            power_w=-1000.0,
            soh=99.2,
            effective_min_soc=10.0,
            effective_max_soc=90.0,
            reported_min_soc=8.0,
            reported_max_soc=92.0,
            usable_soc_source="gateway_native",
        ),
    )
    hass = SimpleNamespace(data={DOMAIN: {"entry-id": runtime}})
    entry = SimpleNamespace(entry_id="entry-id")

    diagnostics = await async_get_config_entry_diagnostics(cast(Any, hass), cast(Any, entry))

    assert diagnostics["config"] == {
        "entry_id": "**REDACTED**",
        "serial": "**REDACTED**",
    }
    assert diagnostics["battery"]["reported_min_soc"] == 8.0
    assert diagnostics["battery"]["reported_max_soc"] == 92.0
    assert diagnostics["battery"]["usable_soc_source"] == "gateway_native"
    assert "payloads" not in diagnostics["mqtt_telemetry"]
    assert diagnostics["mqtt_telemetry"]["fresh"] == {"battery": True}
    assert diagnostics["battery"]["raw_soc"] == 50.0
    assert diagnostics["commands"]["ready"] is False
    assert diagnostics["read_only"]["infrastructure_status"] == "partially_live_validated"
    assert diagnostics["read_only"]["read_only_command_path_status"] == "live_validated"
    assert diagnostics["read_only"]["state_changing_command_path_status"] == "not_live_validated"
    assert diagnostics["scheduler"]["status"] == "read_only_boundary_only"
    assert diagnostics["scheduler"]["mutation_operations_enabled"] is False
    assert diagnostics["scheduler_mutation"]["status"] == "unavailable"
    assert diagnostics["scheduler_mutation"]["manager_constructed"] is False
    assert "must not appear" not in str(diagnostics)
    assert "scheduler_ca_filename" not in str(diagnostics)
    assert "/config/ssl/" not in str(diagnostics)
    assert "certificate" not in str(diagnostics).lower()
