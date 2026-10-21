from __future__ import annotations

from types import SimpleNamespace

import pytest

from custom_components.pixiihome_controller.control.controllers import (
    ControllerArbitrator,
    ControllerDecisionStatus,
    ControllerInputs,
)
from custom_components.pixiihome_controller.control.models import ControlOperation
from custom_components.pixiihome_controller.control.policy import AutomationPolicy
from custom_components.pixiihome_controller.control.production import (
    ProductionAdapterRequest,
    ProductionControlAdapter,
)
from custom_components.pixiihome_controller.control.settings import (
    RETIRED_OPERATIONAL_SETTINGS,
    SETTING_SPECS,
    ControllerState,
    OperationalSettings,
)
from custom_components.pixiihome_controller.dashboard import (
    generate_dashboard_config,
    resolve_dashboard_entity_ids,
)


def _policy() -> AutomationPolicy:
    return AutomationPolicy.from_mapping(
        {
            "policy_id": "test-policy",
            "schema_version": 1,
            "revision": 1,
            "enabled": True,
            "allowed_operation_classes": ["automatic"],
            "allowed_service_classes": ["demandresponse", "targetsoc"],
            "time_window": {"start": "00:00", "end": "23:59", "days": list(range(7))},
            "max_duration_seconds": 3600,
            "soc_min_percent": 10,
            "soc_max_percent": 90,
            "max_power_kw": 10,
            "external_control_allowed": False,
        }
    )


def test_operational_settings_recover_corrupt_values_and_enforce_bounds() -> None:
    settings = OperationalSettings.from_mapping(
        {"values": {"manual_power_kw": float("nan"), "aps_keep_active": "yes"}}
    )

    assert settings.get("manual_power_kw") == 1.0
    assert settings.get("aps_keep_active") is False
    with pytest.raises(ValueError):
        settings.set("target_soc_percent", 101)


def test_automation_policy_is_versioned_and_fails_closed() -> None:
    policy = _policy()

    assert policy.enabled is True
    assert policy.revision == 1
    assert AutomationPolicy.from_mapping({"enabled": True}).enabled is False


def test_controller_arbitration_prioritizes_manual_and_safety() -> None:
    settings = OperationalSettings.defaults()
    state = ControllerState(
        automatic_solar_charging_enabled=True,
        load_balancing_enabled=True,
    )
    arbitrator = ControllerArbitrator()

    manual = arbitrator.evaluate(
        settings,
        state,
        _policy(),
        ControllerInputs(soc_percent=50, manual_control_active=True),
    )
    safety = arbitrator.evaluate(
        settings,
        state,
        _policy(),
        ControllerInputs(
            soc_percent=50,
            phase_current_a=20,
            elapsed_since_transition_seconds=60,
        ),
    )

    assert manual.status is ControllerDecisionStatus.IDLE
    assert manual.reasons == ("manual_control_priority",)
    assert safety.status is ControllerDecisionStatus.BLOCKED
    assert safety.proposal is None
    assert safety.reasons == ("load_balancing_observation_only",)


@pytest.mark.asyncio
async def test_production_adapter_keeps_aps_and_calibration_typed() -> None:
    commands: list[object] = []

    class Manager:
        async def execute(self, command: object) -> object:
            commands.append(command)
            return SimpleNamespace()

    adapter = ProductionControlAdapter(Manager())  # type: ignore[arg-type]
    await adapter.execute(
        ProductionAdapterRequest(
            ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING,
            {
                "export_limit_w": 0,
                "import_limit_w": 0,
                "charge_max_w": 1000,
                "discharge_max_w": 1000,
                "keep_active": True,
                "max_limit_change_w": 1000,
                "duration_seconds": 3600,
            },
        )
    )
    await adapter.execute(
        ProductionAdapterRequest(
            ControlOperation.DIRECT_BATTERY_CALIBRATION,
            {"soc_percent": 80, "duration_seconds": 3600},
        )
    )

    assert [getattr(command, "service") for command in commands] == [
        "adaptivepeakshaving",
        "batterycalibration",
    ]


def test_dashboard_config_resolves_registered_native_entities_only() -> None:
    class Registry:
        def async_get_entity_id(self, domain: str, platform: str, unique_id: str) -> str | None:
            del platform
            return (
                f"{domain}.gateway_{unique_id.removeprefix('GATEWAY_')}"
                if unique_id.endswith(("production_control", "scheduler_calendar"))
                else None
            )

    resolved = resolve_dashboard_entity_ids(
        Registry(),
        serial="GATEWAY",
        platform="pixiihome_controller",
    )
    config = generate_dashboard_config(entity_ids=resolved)

    assert resolved == {
        "switch.production_control": "switch.gateway_production_control",
        "sensor.production_control": "sensor.gateway_production_control",
        "calendar.scheduler_calendar": "calendar.gateway_scheduler_calendar",
    }
    assert config["views"][0]["cards"] == [
        {
            "type": "entities",
            "title": "Gateway status",
            "entities": [
                "switch.gateway_production_control",
                "sensor.gateway_production_control",
            ],
        },
        {
            "type": "entities",
            "title": "Schedule planner",
            "entities": ["calendar.gateway_scheduler_calendar"],
        },
    ]


def test_dashboard_orders_settings_by_function_and_workflow() -> None:
    resolved = {
        f"number.{key}": f"number.gateway_{key}"
        for key, spec in SETTING_SPECS.items()
        if not isinstance(spec.default, bool) and key not in RETIRED_OPERATIONAL_SETTINGS
    }
    resolved.update(
        {
            "switch.automatic_price_optimization": "switch.gateway_price_optimization",
            "sensor.price_optimization_status": "sensor.gateway_price_optimization_status",
            "switch.price_schedule_writes_enabled": "switch.gateway_price_schedule_writes",
            "switch.price_export_allowed": "switch.gateway_price_export_allowed",
        }
    )

    config = generate_dashboard_config(entity_ids=resolved)
    cards = config["views"][0]["cards"]

    assert [card["title"] for card in cards] == [
        "Manual control",
        "Automatic solar charging",
        "Adaptive peak shaving",
        "Price optimization",
    ]
    price_card = next(card for card in cards if card["title"] == "Price optimization")
    assert price_card["entities"][:4] == [
        "switch.gateway_price_optimization",
        "sensor.gateway_price_optimization_status",
        "switch.gateway_price_schedule_writes",
        "switch.gateway_price_export_allowed",
    ]
    configured_numbers = {
        entity for card in cards for entity in card["entities"] if entity.startswith("number.")
    }
    assert configured_numbers == set(resolved.values()) - {
        "switch.gateway_price_optimization",
        "sensor.gateway_price_optimization_status",
        "switch.gateway_price_schedule_writes",
        "switch.gateway_price_export_allowed",
    }
