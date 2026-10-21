"""Home Assistant config flow for Pixiihome Controller."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.components.mqtt.util import async_wait_for_mqtt_client
from homeassistant.helpers import selector

from .const import (
    CONF_AUTOMATION_POLICY,
    CONF_RUNTIME_MODE,
    CONF_SCHEDULER_BOOTSTRAP_ID,
    CONF_SCHEDULER_CA_COMPATIBILITY,
    CONF_SCHEDULER_CA_FILENAME,
    CONF_SCHEDULER_ENABLED,
    CONF_SCHEDULER_PASSWORD,
    CONF_SCHEDULER_TIMEOUT,
    CONF_SCHEDULER_URL,
    CONF_SCHEDULER_USERNAME,
    CONF_SERIAL,
    DOMAIN,
    NAME,
)
from .control.load_balancing import CONF_LOAD_BALANCING, LoadBalancingConfig
from .control.models import RuntimeMode
from .control.policy import AutomationPolicy
from .control.price_optimization import CONF_PRICE_OPTIMIZATION, PriceOptimizationConfig
from .energy_forecast_service import CONF_ENERGY_FORECAST, EnergyForecastConfig
from .scheduler.client import normalize_scheduler_bootstrap_id, normalize_scheduler_url
from .scheduler.tls import normalize_scheduler_ca_filename

_SERIAL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_USER_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_RUNTIME_MODE,
            default=RuntimeMode.PRODUCTION.value,
        ): vol.In(
            {
                RuntimeMode.PRODUCTION.value: "Production Gateway",
                RuntimeMode.SIMULATOR.value: "Local Simulator",
            }
        ),
        vol.Optional(CONF_SERIAL): str,
    }
)


def normalize_serial(value: Any) -> str | None:
    """Return a safe Gateway serial or None for invalid input."""
    if not isinstance(value, str):
        return None
    serial = value.strip()
    return serial if _SERIAL_RE.fullmatch(serial) else None


def _password_selector() -> selector.TextSelector:
    """Return the HA-supported password-capable text selector."""
    return selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
    )


def _scheduler_options_schema(values: dict[str, object] | None = None) -> vol.Schema:
    """Build technical Options without duplicating operational settings."""
    current = values or {}
    return vol.Schema(
        {
            vol.Optional(
                CONF_AUTOMATION_POLICY,
                default=current.get(
                    CONF_AUTOMATION_POLICY,
                    AutomationPolicy.disabled().as_dict(),
                ),
            ): selector.ObjectSelector(),
            vol.Optional(
                CONF_SCHEDULER_ENABLED,
                default=bool(current.get(CONF_SCHEDULER_URL)),
            ): bool,
            vol.Optional(
                CONF_SCHEDULER_URL,
                default=current.get(CONF_SCHEDULER_URL, ""),
            ): str,
            vol.Optional(
                CONF_SCHEDULER_USERNAME,
                default=current.get(CONF_SCHEDULER_USERNAME, ""),
            ): str,
            vol.Optional(CONF_SCHEDULER_PASSWORD, default=""): _password_selector(),
            vol.Optional(
                CONF_SCHEDULER_BOOTSTRAP_ID,
                default="",
            ): _password_selector(),
            vol.Optional(
                CONF_SCHEDULER_TIMEOUT,
                default=current.get(CONF_SCHEDULER_TIMEOUT, 10.0),
            ): vol.All(vol.Coerce(float), vol.Range(min=1.0, max=60.0)),
            vol.Optional(
                CONF_SCHEDULER_CA_COMPATIBILITY,
                default=bool(current.get(CONF_SCHEDULER_CA_COMPATIBILITY, False)),
            ): bool,
            vol.Optional(
                CONF_SCHEDULER_CA_FILENAME,
                default=current.get(CONF_SCHEDULER_CA_FILENAME, ""),
            ): str,
        }
    )


def _scheduler_values(
    user_input: dict[str, Any],
    *,
    existing_password: object = None,
    existing_bootstrap_id: object = None,
) -> tuple[dict[str, object], dict[str, str]]:
    """Validate optional scheduler settings without contacting the Gateway."""
    if not bool(user_input.get(CONF_SCHEDULER_ENABLED, True)):
        return {CONF_SCHEDULER_ENABLED: False}, {}

    compatibility = user_input.get(CONF_SCHEDULER_CA_COMPATIBILITY, False)
    raw_filename = user_input.get(CONF_SCHEDULER_CA_FILENAME)
    ca_filename = (
        normalize_scheduler_ca_filename(raw_filename) if raw_filename not in (None, "") else None
    )
    errors: dict[str, str] = {}
    if compatibility and raw_filename in (None, ""):
        errors[CONF_SCHEDULER_CA_FILENAME] = "ca_filename_required"
    elif raw_filename not in (None, "") and ca_filename is None:
        errors[CONF_SCHEDULER_CA_FILENAME] = "invalid_ca_filename"

    raw_url = user_input.get(CONF_SCHEDULER_URL)
    username = user_input.get(CONF_SCHEDULER_USERNAME)
    password = user_input.get(CONF_SCHEDULER_PASSWORD)
    raw_bootstrap_id = user_input.get(CONF_SCHEDULER_BOOTSTRAP_ID)
    if not any(
        isinstance(value, str) and value.strip()
        for value in (raw_url, username, password, raw_bootstrap_id)
    ):
        return ({}, errors) if errors else ({}, {})

    base_url = normalize_scheduler_url(raw_url)
    if base_url is None:
        errors[CONF_SCHEDULER_URL] = "invalid_scheduler_url"
    bootstrap_id = normalize_scheduler_bootstrap_id(raw_bootstrap_id)
    if bootstrap_id is None and raw_bootstrap_id in (None, ""):
        bootstrap_id = normalize_scheduler_bootstrap_id(existing_bootstrap_id)
    if bootstrap_id is None:
        errors[CONF_SCHEDULER_BOOTSTRAP_ID] = "bootstrap_id_required"
    clean_username = username.strip() if isinstance(username, str) else ""
    clean_password = (
        password
        if isinstance(password, str) and password
        else existing_password
        if isinstance(existing_password, str) and existing_password
        else None
    )
    if not clean_username or not clean_password:
        errors["base"] = "scheduler_credentials_required"
    if errors:
        return {}, errors

    timeout = float(user_input.get(CONF_SCHEDULER_TIMEOUT, 10.0))
    values: dict[str, object] = {
        CONF_SCHEDULER_ENABLED: True,
        CONF_SCHEDULER_URL: base_url,
        CONF_SCHEDULER_USERNAME: clean_username,
        CONF_SCHEDULER_PASSWORD: clean_password,
        CONF_SCHEDULER_BOOTSTRAP_ID: bootstrap_id,
        CONF_SCHEDULER_TIMEOUT: timeout,
    }
    if CONF_SCHEDULER_CA_COMPATIBILITY in user_input:
        values[CONF_SCHEDULER_CA_COMPATIBILITY] = bool(compatibility)
    if compatibility and ca_filename is not None:
        values[CONF_SCHEDULER_CA_FILENAME] = ca_filename
    return values, {}


class PixiihomeControllerConfigFlow(  # pyright: ignore[reportGeneralTypeIssues, reportCallIssue]
    config_entries.ConfigFlow,
    domain=DOMAIN,  # pyright: ignore[reportCallIssue]
):
    """Configure one production Gateway or one isolated local simulator."""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Return the optional scheduler settings flow."""
        return PixiihomeControllerOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            runtime_mode = user_input.get(
                CONF_RUNTIME_MODE,
                RuntimeMode.PRODUCTION.value,
            )
            if runtime_mode == RuntimeMode.SIMULATOR.value:
                await self.async_set_unique_id(RuntimeMode.SIMULATOR.value)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{NAME} (Simulator)",
                    data={CONF_RUNTIME_MODE: RuntimeMode.SIMULATOR.value},
                )
            serial = normalize_serial(user_input.get(CONF_SERIAL))
            if serial is None:
                errors[CONF_SERIAL] = "invalid_serial"
            elif not await async_wait_for_mqtt_client(self.hass):
                errors["base"] = "mqtt_unavailable"
            else:
                await self.async_set_unique_id(serial)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{NAME} ({serial})",
                    data={
                        CONF_RUNTIME_MODE: RuntimeMode.PRODUCTION.value,
                        CONF_SERIAL: serial,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_USER_SCHEMA,
            errors=errors,
        )


class PixiihomeControllerOptionsFlow(config_entries.OptionsFlow):
    """Update optional scheduler settings without exposing stored credentials."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return await self.async_step_scheduler(user_input)
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "load_balancing", "price_optimization", "energy_forecast", "scheduler",
            ],
        )

    async def async_step_price_optimization(
        self, user_input: dict[str, Any] | None = None
    ):
        """Configure the price source and scheduler-specific price bindings."""
        current = self._config_entry.options.get(CONF_PRICE_OPTIMIZATION)
        try:
            config = PriceOptimizationConfig.from_mapping(current)
        except ValueError:
            config = PriceOptimizationConfig()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                config = PriceOptimizationConfig.from_mapping(user_input)
            except (TypeError, ValueError):
                errors["base"] = "invalid_price_optimization"
            else:
                options = {
                    **self._config_entry.options,
                    CONF_PRICE_OPTIMIZATION: config.as_dict(),
                }
                return self.async_create_entry(title="", data=options)
        values = config.as_dict()
        return self.async_show_form(
            step_id="price_optimization",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "price_entity_id",
                        default=values["price_entity_id"],
                    ): selector.EntitySelector(
                        selector.EntitySelectorConfig(filter={"domain": "sensor"})
                    ),
                    vol.Optional(
                        "managed_source_id",
                        default=(
                            ""
                            if values["managed_source_id"] is None
                            else str(values["managed_source_id"])
                        ),
                    ): selector.TextSelector(),
                    vol.Required(
                        "planning_horizon_hours",
                        default=values["planning_horizon_hours"],
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=12,
                            max=72,
                            step=1,
                            unit_of_measurement="h",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        "minimum_lead_minutes",
                        default=values["minimum_lead_minutes"],
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=1,
                            max=60,
                            step=1,
                            unit_of_measurement="min",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                    vol.Required(
                        "refresh_minutes",
                        default=values["refresh_minutes"],
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=5,
                            max=360,
                            step=1,
                            unit_of_measurement="min",
                            mode=selector.NumberSelectorMode.BOX,
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def async_step_energy_forecast(self, user_input: dict[str, Any] | None = None):
        """Select the solar forecast, solar power and temperature entities."""
        current = self._config_entry.options.get(CONF_ENERGY_FORECAST)
        try:
            config = EnergyForecastConfig.from_mapping(current)
        except ValueError:
            config = EnergyForecastConfig()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                config = EnergyForecastConfig.from_mapping(user_input)
            except (TypeError, ValueError):
                errors["base"] = "invalid_energy_forecast"
            else:
                options = {**self._config_entry.options, CONF_ENERGY_FORECAST: config.as_dict()}
                return self.async_create_entry(title="", data=options)
        schema: dict[Any, Any] = {
            vol.Optional(
                "solar_forecast_entity_ids",
                default=list(config.solar_forecast_entity_ids),
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(filter={"domain": "sensor"}, multiple=True)
            ),
        }
        for key, value, domains in (
            ("solar_power_entity_id", config.solar_power_entity_id, ["sensor"]),
            ("temperature_entity_id", config.temperature_entity_id, ["sensor", "weather"]),
        ):
            marker = (
                vol.Optional(key, description={"suggested_value": value})
                if value else vol.Optional(key)
            )
            schema[marker] = selector.EntitySelector(
                selector.EntitySelectorConfig(filter={"domain": domains})
            )
        return self.async_show_form(
            step_id="energy_forecast", data_schema=vol.Schema(schema), errors=errors,
        )

    async def async_step_load_balancing(self, user_input: dict[str, Any] | None = None):
        current = self._config_entry.options.get(CONF_LOAD_BALANCING, {})
        current = current if isinstance(current, dict) else {}
        values = {**LoadBalancingConfig().as_dict(), **current}
        errors = {}
        if user_input is not None:
            values.update(user_input)
            try:
                for key in ("phases", "poll_seconds", "stale_seconds"):
                    if float(values[key]) != int(values[key]):
                        raise ValueError("invalid_config")
                    values[key] = int(values[key])
                # The entity selection step supplies required meter bindings.
                LoadBalancingConfig.from_mapping({**values, "enabled": False})
                self._load_balancing_values = values
                if values["source"] == "entities" and values["enabled"]:
                    return await self.async_step_load_balancing_meter()
                return self._save_load_balancing(values)
            except (TypeError, ValueError, OverflowError) as error:
                errors["base"] = (
                    str(error)
                    if str(error)
                    in {
                        "margin_exceeds_fuse",
                        "stale_interval_too_short",
                    }
                    else "invalid_load_balancing"
                )
        fields: dict[Any, Any] = {
            vol.Required("enabled", default=values["enabled"]): bool,
            vol.Required("source", default=values["source"]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "pixii", "label": "Pixii Gateway"},
                        {"value": "entities", "label": "Home Assistant"},
                    ]
                )
            ),
            vol.Required("phases", default=str(values["phases"])): selector.SelectSelector(
                selector.SelectSelectorConfig(options=["1", "3"])
            ),
        }
        limits = {
            "fuse_l1": (1, 1000, "A"),
            "fuse_l2": (1, 1000, "A"),
            "fuse_l3": (1, 1000, "A"),
            "margin_a": (0, 100, "A"),
            "min_soc": (0, 100, "%"),
            "max_charge_kw": (0, 1000, "kW"),
            "max_discharge_kw": (0, 1000, "kW"),
            "poll_seconds": (2, 60, "s"),
            "stale_seconds": (5, 120, "s"),
            "recovery_step_w": (10, 10000, "W"),
        }
        for key, (low, high, unit) in limits.items():
            fields[vol.Required(key, default=values[key])] = selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=low,
                    max=high,
                    step=1,
                    unit_of_measurement=unit,
                    mode=selector.NumberSelectorMode.BOX,
                )
            )
        return self.async_show_form(
            step_id="load_balancing", data_schema=vol.Schema(fields), errors=errors
        )

    async def async_step_load_balancing_meter(self, user_input: dict[str, Any] | None = None):
        values = self._load_balancing_values
        errors = {}
        if user_input is not None:
            values = {**values, **user_input}
            try:
                return self._save_load_balancing(values)
            except (TypeError, ValueError):
                errors["base"] = "invalid_meter_entities"
        fields = {}
        for prefix in ("current", "voltage", "power"):
            device_class = {"current": "current", "voltage": "voltage", "power": "power"}[prefix]
            for phase in range(1, values["phases"] + 1):
                key = f"{prefix}_l{phase}"
                marker = vol.Optional if prefix == "power" else vol.Required
                field = marker(key, default=values[key]) if values.get(key) else marker(key)
                fields[field] = selector.EntitySelector(
                    selector.EntitySelectorConfig(
                        filter={"domain": "sensor", "device_class": device_class}
                    )
                )
        return self.async_show_form(
            step_id="load_balancing_meter", data_schema=vol.Schema(fields), errors=errors
        )

    def _save_load_balancing(self, values: dict[str, Any]):
        config = LoadBalancingConfig.from_mapping(values)
        options = {**self._config_entry.options, CONF_LOAD_BALANCING: config.as_dict()}
        return self.async_create_entry(title="", data=options)

    async def async_step_scheduler(self, user_input: dict[str, Any] | None = None):
        current = {**self._config_entry.data, **self._config_entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            scheduler_data, errors = _scheduler_values(
                user_input,
                existing_password=current.get(CONF_SCHEDULER_PASSWORD),
                existing_bootstrap_id=current.get(CONF_SCHEDULER_BOOTSTRAP_ID),
            )
            if not errors:
                policy = user_input.get(
                    CONF_AUTOMATION_POLICY,
                    current.get(
                        CONF_AUTOMATION_POLICY,
                        AutomationPolicy.disabled().as_dict(),
                    ),
                )
                if not isinstance(policy, dict):
                    errors[CONF_AUTOMATION_POLICY] = "invalid_policy"
                else:
                    parsed_policy = AutomationPolicy.from_mapping(policy)
                    default_policy = AutomationPolicy.disabled().as_dict()
                    required_policy_keys = set(default_policy)
                    if not required_policy_keys.issubset(policy) or (
                        parsed_policy.as_dict() == default_policy and policy != default_policy
                    ):
                        errors[CONF_AUTOMATION_POLICY] = "invalid_policy"
                    else:
                        previous_policy = AutomationPolicy.from_mapping(
                            current.get(CONF_AUTOMATION_POLICY)
                        )
                        if (
                            parsed_policy.as_dict() != previous_policy.as_dict()
                            and parsed_policy.revision <= previous_policy.revision
                        ):
                            errors[CONF_AUTOMATION_POLICY] = "policy_revision_required"
                        else:
                            options = dict(self._config_entry.options)
                            options.update(scheduler_data)
                            options[CONF_AUTOMATION_POLICY] = parsed_policy.as_dict()
                            return self.async_create_entry(title="", data=options)
        return self.async_show_form(
            step_id="scheduler",
            data_schema=_scheduler_options_schema(current),
            errors=errors,
        )
