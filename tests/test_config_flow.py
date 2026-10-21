from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from homeassistant.helpers import selector
from voluptuous import Invalid

from custom_components.pixiihome_controller import config_flow as config_flow_module
from custom_components.pixiihome_controller.config_flow import (
    _USER_SCHEMA,
    PixiihomeControllerOptionsFlow,
    _scheduler_options_schema,
    _scheduler_values,
    normalize_serial,
)
from custom_components.pixiihome_controller.const import (
    CONF_AUTOMATION_POLICY,
    CONF_PRODUCTION_CONTROL_ENABLED,
    CONF_RUNTIME_MODE,
    CONF_SCHEDULER_BOOTSTRAP_ID,
    CONF_SCHEDULER_CA_COMPATIBILITY,
    CONF_SCHEDULER_CA_FILENAME,
    CONF_SCHEDULER_ENABLED,
    CONF_SCHEDULER_PASSWORD,
    CONF_SCHEDULER_URL,
    CONF_SCHEDULER_USERNAME,
    CONF_SERIAL,
)
from custom_components.pixiihome_controller.control.models import RuntimeMode
from custom_components.pixiihome_controller.control.policy import AutomationPolicy


def _valid_policy(*, revision: int = 1) -> dict[str, Any]:
    return {
        "policy_id": "test-policy",
        "schema_version": 1,
        "revision": revision,
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


class _TestOptionsFlow(PixiihomeControllerOptionsFlow):
    def async_show_menu(self, **kwargs: object) -> dict[str, object]:
        return {"type": "menu", **kwargs}

    def async_create_entry(self, *, title: str, data: dict[str, object]) -> dict[str, object]:
        return {"type": "create_entry", "title": title, "data": data}

    def async_show_form(self, **kwargs: object) -> dict[str, object]:
        return {"type": "form", **kwargs}


def _options_flow(
    *,
    data: dict[str, object] | None = None,
    options: dict[str, object] | None = None,
) -> _TestOptionsFlow:
    return _TestOptionsFlow(
        SimpleNamespace(data=data or {}, options=options or {})  # type: ignore[arg-type]
    )


def test_initial_config_flow_selects_production_or_simulator():
    assert _USER_SCHEMA(
        {
            CONF_RUNTIME_MODE: RuntimeMode.PRODUCTION.value,
            CONF_SERIAL: "SERIAL",
        }
    ) == {
        CONF_RUNTIME_MODE: RuntimeMode.PRODUCTION.value,
        CONF_SERIAL: "SERIAL",
    }
    assert _USER_SCHEMA({CONF_RUNTIME_MODE: RuntimeMode.SIMULATOR.value}) == {
        CONF_RUNTIME_MODE: RuntimeMode.SIMULATOR.value
    }
    with pytest.raises(Invalid):
        _USER_SCHEMA(
            {
                CONF_RUNTIME_MODE: RuntimeMode.PRODUCTION.value,
                CONF_SERIAL: "SERIAL",
                CONF_SCHEDULER_URL: "https://gateway.example",
            }
        )


def test_scheduler_settings_require_https_origin_and_credentials():
    values, errors = _scheduler_values(
        {
            CONF_SCHEDULER_ENABLED: True,
            CONF_SCHEDULER_URL: "http://gateway.example",
            CONF_SCHEDULER_USERNAME: "operator",
            CONF_SCHEDULER_PASSWORD: "password",
        }
    )

    assert values == {}
    assert errors[CONF_SCHEDULER_URL] == "invalid_scheduler_url"


@pytest.mark.parametrize("bootstrap_id", ("", "short", "A" * 31, "A" * 33, "A_" * 16))
def test_scheduler_settings_require_exact_owner_pinned_bootstrap_id(
    bootstrap_id: str,
) -> None:
    values, errors = _scheduler_values(
        {
            CONF_SCHEDULER_ENABLED: True,
            CONF_SCHEDULER_URL: "https://gateway.example",
            CONF_SCHEDULER_USERNAME: "operator",
            CONF_SCHEDULER_PASSWORD: "password",
            CONF_SCHEDULER_BOOTSTRAP_ID: bootstrap_id,
        }
    )

    assert values == {}
    assert errors[CONF_SCHEDULER_BOOTSTRAP_ID] == "bootstrap_id_required"


def test_scheduler_options_never_default_stored_credentials():
    schema = _scheduler_options_schema(
        {
            CONF_SCHEDULER_URL: "https://gateway.example",
            CONF_SCHEDULER_USERNAME: "operator",
            CONF_SCHEDULER_PASSWORD: "stored-secret",
            CONF_SCHEDULER_BOOTSTRAP_ID: "B" * 32,
        }
    )
    assert "stored-secret" not in repr(schema)
    assert "B" * 32 not in repr(schema)


def test_blank_password_preserves_existing_and_new_password_replaces_it():
    common = {
        CONF_SCHEDULER_ENABLED: True,
        CONF_SCHEDULER_URL: "https://gateway.example/",
        CONF_SCHEDULER_USERNAME: "operator",
        CONF_SCHEDULER_BOOTSTRAP_ID: "A" * 32,
    }
    preserved, errors = _scheduler_values(
        {**common, CONF_SCHEDULER_PASSWORD: ""},
        existing_password="stored-secret",
    )
    replaced, replace_errors = _scheduler_values(
        {**common, CONF_SCHEDULER_PASSWORD: "new-secret"},
        existing_password="stored-secret",
    )

    assert errors == {}
    assert replace_errors == {}
    assert preserved[CONF_SCHEDULER_PASSWORD] == "stored-secret"
    assert replaced[CONF_SCHEDULER_PASSWORD] == "new-secret"


def test_blank_bootstrap_id_preserves_existing_pin() -> None:
    values, errors = _scheduler_values(
        {
            CONF_SCHEDULER_ENABLED: True,
            CONF_SCHEDULER_URL: "https://gateway.example",
            CONF_SCHEDULER_USERNAME: "operator",
            CONF_SCHEDULER_PASSWORD: "password",
            CONF_SCHEDULER_BOOTSTRAP_ID: "",
        },
        existing_bootstrap_id="B" * 32,
    )

    assert errors == {}
    assert values[CONF_SCHEDULER_BOOTSTRAP_ID] == "B" * 32


def test_disabling_scheduler_access_removes_all_scheduler_options():
    values, errors = _scheduler_values(
        {
            CONF_SCHEDULER_ENABLED: False,
            CONF_SCHEDULER_URL: "https://gateway.example",
            CONF_SCHEDULER_USERNAME: "operator",
            CONF_SCHEDULER_PASSWORD: "",
        },
        existing_password="stored-secret",
    )

    assert values == {CONF_SCHEDULER_ENABLED: False}
    assert errors == {}


def test_existing_serial_validation_remains_independent():
    assert normalize_serial({CONF_SERIAL: "SERIAL"}) is None
    assert normalize_serial("SERIAL_01") == "SERIAL_01"


def test_scheduler_ca_compatibility_defaults_false_and_requires_safe_filename():
    common = {
        CONF_SCHEDULER_ENABLED: True,
        CONF_SCHEDULER_URL: "https://gateway.example",
        CONF_SCHEDULER_USERNAME: "operator",
        CONF_SCHEDULER_PASSWORD: "password",
        CONF_SCHEDULER_BOOTSTRAP_ID: "A" * 32,
    }
    schema = _scheduler_options_schema(common)
    assert cast(dict[str, Any], schema(common))[CONF_SCHEDULER_CA_COMPATIBILITY] is False

    missing, missing_errors = _scheduler_values({**common, CONF_SCHEDULER_CA_COMPATIBILITY: True})
    assert missing == {}
    assert missing_errors[CONF_SCHEDULER_CA_FILENAME] == "ca_filename_required"

    valid, valid_errors = _scheduler_values(
        {
            **common,
            CONF_SCHEDULER_CA_COMPATIBILITY: True,
            CONF_SCHEDULER_CA_FILENAME: "pixii-ca.crt",
        }
    )
    assert valid_errors == {}
    assert valid[CONF_SCHEDULER_CA_FILENAME] == "pixii-ca.crt"


@pytest.mark.parametrize(
    "filename",
    ("../pixii.crt", "/tmp/pixii.crt", "C:\\\\pixii.crt", "subdir/pixii.crt"),
)
def test_scheduler_ca_filename_traversal_is_rejected(filename: str):
    values, errors = _scheduler_values(
        {
            CONF_SCHEDULER_ENABLED: True,
            CONF_SCHEDULER_URL: "https://gateway.example",
            CONF_SCHEDULER_USERNAME: "operator",
            CONF_SCHEDULER_PASSWORD: "password",
            CONF_SCHEDULER_CA_COMPATIBILITY: True,
            CONF_SCHEDULER_CA_FILENAME: filename,
        }
    )
    assert values == {}
    assert errors[CONF_SCHEDULER_CA_FILENAME] == "invalid_ca_filename"


def _result_data(result: dict[str, object]) -> dict[str, object]:
    return cast(dict[str, object], result["data"])


@pytest.mark.asyncio
async def test_load_balancing_options_menu_and_preserved_authority():
    from custom_components.pixiihome_controller.control.load_balancing import CONF_LOAD_BALANCING

    existing = {CONF_PRODUCTION_CONTROL_ENABLED: False, CONF_SCHEDULER_PASSWORD: "keep-private"}
    flow = _options_flow(options=existing)
    menu = await flow.async_step_init()
    assert menu["menu_options"] == [
        "load_balancing",
        "price_optimization",
        "scheduler",
    ]
    result = await flow.async_step_load_balancing(
        {
            "enabled": True,
            "source": "pixii",
            "phases": "3",
            "fuse_l1": 20,
            "fuse_l2": 25,
            "fuse_l3": 16,
        }
    )
    saved = _result_data(result)
    assert saved[CONF_PRODUCTION_CONTROL_ENABLED] is False
    assert saved[CONF_SCHEDULER_PASSWORD] == "keep-private"
    load_balancing = cast(dict[str, Any], saved[CONF_LOAD_BALANCING])
    assert load_balancing["fuse_l2"] == 25
    assert load_balancing["phases"] == 3


@pytest.mark.asyncio
async def test_price_optimization_options_are_canonical_and_preserve_other_options():
    from custom_components.pixiihome_controller.control.price_optimization import (
        CONF_PRICE_OPTIMIZATION,
    )

    flow = _options_flow(options={CONF_SCHEDULER_PASSWORD: "keep-private"})
    result = await flow.async_step_price_optimization(
        {
            "price_entity_id": "sensor.greenely_prices",
            "managed_source_id": "7",
            "planning_horizon_hours": 36,
            "minimum_lead_minutes": 5,
            "refresh_minutes": 60,
        }
    )

    saved = _result_data(result)
    assert saved[CONF_SCHEDULER_PASSWORD] == "keep-private"
    assert saved[CONF_PRICE_OPTIMIZATION] == {
        "price_entity_id": "sensor.greenely_prices",
        "managed_source_id": 7,
        "planning_horizon_hours": 36,
        "minimum_lead_minutes": 5,
        "refresh_minutes": 60,
    }


@pytest.mark.asyncio
async def test_load_balancing_entity_step_requires_all_used_phases():
    flow = _options_flow()
    result = await flow.async_step_load_balancing({"enabled": True, "source": "entities"})
    assert result["step_id"] == "load_balancing_meter"
    invalid = await flow.async_step_load_balancing_meter({"current_l1": "sensor.test"})
    assert invalid["errors"] == {"base": "invalid_meter_entities"}
    fields = {
        f"{prefix}_l{i}": f"sensor.{prefix}_l{i}"
        for prefix in ("current", "voltage")
        for i in (1, 2, 3)
    }
    result = await flow.async_step_load_balancing_meter(fields)
    assert result["type"] == "create_entry"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values,reason",
    [
        ({"margin_a": 20}, "margin_exceeds_fuse"),
        ({"poll_seconds": 20}, "stale_interval_too_short"),
        ({"poll_seconds": float("inf")}, "invalid_load_balancing"),
    ],
)
async def test_load_balancing_form_errors_preserve_input(values, reason):
    result = await _options_flow().async_step_load_balancing(values)
    assert result["errors"] == {"base": reason}


def test_automation_policy_options_schema_uses_serializable_object_selector():
    schema = _scheduler_options_schema()
    policy_validator = next(
        value
        for key, value in schema.schema.items()
        if getattr(key, "schema", None) == CONF_AUTOMATION_POLICY
    )

    assert isinstance(policy_validator, selector.ObjectSelector)
    assert policy_validator.serialize()["selector"]["object"].get("multiple", False) is False


@pytest.mark.asyncio
async def test_options_flow_defaults_missing_policy_to_disabled():
    result = await _options_flow().async_step_init({CONF_SCHEDULER_ENABLED: False})

    assert result["type"] == "create_entry"
    assert _result_data(result)[CONF_AUTOMATION_POLICY] == AutomationPolicy.disabled().as_dict()


@pytest.mark.asyncio
async def test_options_flow_preserves_existing_valid_policy():
    policy = _valid_policy()
    result = await _options_flow(options={CONF_AUTOMATION_POLICY: policy}).async_step_init(
        {CONF_SCHEDULER_ENABLED: False}
    )

    assert result["type"] == "create_entry"
    assert (
        _result_data(result)[CONF_AUTOMATION_POLICY]
        == AutomationPolicy.from_mapping(policy).as_dict()
    )


@pytest.mark.asyncio
async def test_options_flow_round_trips_valid_policy_through_canonical_model():
    policy = _valid_policy()
    result = await _options_flow().async_step_init(
        {
            CONF_SCHEDULER_ENABLED: False,
            CONF_AUTOMATION_POLICY: policy,
        }
    )

    assert result["type"] == "create_entry"
    assert (
        _result_data(result)[CONF_AUTOMATION_POLICY]
        == AutomationPolicy.from_mapping(policy).as_dict()
    )


@pytest.mark.asyncio
async def test_options_flow_rejects_invalid_policy_fail_closed():
    result = await _options_flow().async_step_init(
        {
            CONF_SCHEDULER_ENABLED: False,
            CONF_AUTOMATION_POLICY: {"policy_id": "invalid"},
        }
    )

    assert result["type"] == "form"
    assert result["errors"] == {CONF_AUTOMATION_POLICY: "invalid_policy"}


@pytest.mark.asyncio
async def test_options_flow_rejects_existing_malformed_policy():
    result = await _options_flow(
        options={CONF_AUTOMATION_POLICY: {"enabled": True}}
    ).async_step_init({CONF_SCHEDULER_ENABLED: False})

    assert result["type"] == "form"
    assert result["errors"] == {CONF_AUTOMATION_POLICY: "invalid_policy"}


@pytest.mark.asyncio
async def test_options_flow_does_not_change_production_control_gate():
    result = await _options_flow(options={CONF_PRODUCTION_CONTROL_ENABLED: False}).async_step_init(
        {
            CONF_SCHEDULER_ENABLED: False,
            CONF_AUTOMATION_POLICY: _valid_policy(),
        }
    )

    assert result["type"] == "create_entry"
    assert _result_data(result)[CONF_PRODUCTION_CONTROL_ENABLED] is False


@pytest.mark.asyncio
async def test_options_flow_makes_no_mqtt_or_scheduler_calls(monkeypatch):
    def fail_if_called(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("options flow made an external call")

    monkeypatch.setattr(
        config_flow_module,
        "async_wait_for_mqtt_client",
        fail_if_called,
    )
    monkeypatch.setattr(config_flow_module, "normalize_scheduler_url", fail_if_called)

    result = await _options_flow().async_step_init({CONF_SCHEDULER_ENABLED: False})

    assert result["type"] == "create_entry"
