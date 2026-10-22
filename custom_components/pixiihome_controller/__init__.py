"""Pixiihome Controller integration entry point."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import voluptuous as vol
from homeassistant.components.mqtt.util import async_wait_for_mqtt_client
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .aps_controller import AdaptivePeakShavingController
from .battery_activity import BatteryActivityTracker
from .const import (
    CONF_AUTOMATION_POLICY,
    CONF_ENTRY_ID,
    CONF_EXPECTED_SERVICE_REFERENCE_DELAY,
    CONF_METER_LIMITATION_TRIAL_DIRECTION,
    CONF_METER_LIMITATION_TRIAL_MAX_CORRECTION,
    CONF_RUNTIME_MODE,
    CONF_SCHEDULER_BOOTSTRAP_ID,
    CONF_SCHEDULER_CA_COMPATIBILITY,
    CONF_SCHEDULER_CA_FILENAME,
    CONF_SCHEDULER_DAYS_BACK,
    CONF_SCHEDULER_DAYS_FORWARD,
    CONF_SCHEDULER_ENABLED,
    CONF_SCHEDULER_INDEX,
    CONF_SCHEDULER_PASSWORD,
    CONF_SCHEDULER_TIMEOUT,
    CONF_SCHEDULER_URL,
    CONF_SCHEDULER_USERNAME,
    CONF_SERIAL,
    DOMAIN,
    PLATFORMS,
    SCHEDULER_MAX_DAYS,
    SERVICE_GENERATE_DASHBOARD_CONFIG,
    SERVICE_QUERY_RUNNING_SERVICE,
    SERVICE_REFRESH_SCHEDULER_DETAIL,
    SERVICE_REFRESH_SCHEDULER_LIST,
    SERVICE_RUN_METER_LIMITATION_SOFT_LIMIT_TRIAL,
    SERVICE_RUN_SCHEDULER_VALIDATION,
    SERVICE_SET_SERVICE_REFERENCE_DELAY,
)
from .control.load_balancing import CONF_LOAD_BALANCING
from .control.models import RuntimeMode
from .control.price_optimization import CONF_PRICE_OPTIMIZATION
from .dashboard import generate_dashboard_config, resolve_dashboard_entity_ids
from .energy_forecast_service import CONF_ENERGY_FORECAST, EnergyForecastService
from .entity_cleanup import remove_retired_entities
from .exceptions import (
    SchedulerAuthenticationError,
    SchedulerClientError,
    SchedulerProtocolError,
)
from .load_balancing_observer import LoadBalancingObserver
from .meter_limit_controller import MeterLimitLoadBalancer
from .panel import async_register_control_api
from .price_controller import PriceOptimizationController
from .protocol.commands import RunningServiceGet
from .runtime import RuntimeData
from .simulator_entry import SimulatorEntryRuntime
from .solar_controller import SolarChargingController

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_SERVICE_SCHEMA = vol.Schema({vol.Optional(CONF_ENTRY_ID): cv.string})


def _strict_nonnegative_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise vol.Invalid("value must be a non-negative integer")
    return value


def _strict_scheduler_day(value: object) -> int:
    value = _strict_nonnegative_int(value)
    if value > SCHEDULER_MAX_DAYS:
        raise vol.Invalid(f"value must be between 0 and {SCHEDULER_MAX_DAYS}")
    return value


def _strict_reference_delay(value: object) -> int:
    value = _strict_nonnegative_int(value)
    if value > 60:
        raise vol.Invalid("value must be between 0 and 60")
    return value


_SCHEDULER_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENTRY_ID): cv.string,
        vol.Required(CONF_SCHEDULER_DAYS_BACK): _strict_scheduler_day,
        vol.Required(CONF_SCHEDULER_DAYS_FORWARD): _strict_scheduler_day,
    }
)
_SCHEDULER_DETAIL_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENTRY_ID): cv.string,
        vol.Required(CONF_SCHEDULER_INDEX): _strict_nonnegative_int,
    }
)
_REFERENCE_DELAY_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENTRY_ID): cv.string,
        vol.Required(CONF_EXPECTED_SERVICE_REFERENCE_DELAY): _strict_reference_delay,
    }
)


_METER_LIMITATION_TRIAL_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENTRY_ID): cv.string,
        vol.Required(CONF_METER_LIMITATION_TRIAL_DIRECTION): vol.In(("exp", "gen")),
        vol.Required(CONF_METER_LIMITATION_TRIAL_MAX_CORRECTION): vol.All(
            _strict_nonnegative_int, vol.Range(min=50, max=300)
        ),
    }
)


def _select_runtime(call: ServiceCall) -> RuntimeData | SimulatorEntryRuntime:
    """Select one loaded config entry without creating implicit setup."""
    runtimes: dict[str, RuntimeData | SimulatorEntryRuntime] = call.hass.data.get(DOMAIN, {})
    requested_entry_id = call.data.get(CONF_ENTRY_ID)
    if requested_entry_id is not None:
        runtime = runtimes.get(requested_entry_id)
        if runtime is None:
            raise HomeAssistantError("The requested Pixii config entry is not loaded")
    elif len(runtimes) == 1:
        runtime = next(iter(runtimes.values()))
    elif not runtimes:
        raise HomeAssistantError("No Pixii config entry is loaded")
    else:
        raise HomeAssistantError(
            "config_entry_id is required when multiple Pixii config entries are loaded"
        )
    return runtime


def _scheduler_options_signature(entry: ConfigEntry) -> tuple[object, ...]:
    values = {**entry.data, **entry.options}
    return tuple(
        values.get(key)
        for key in (
            CONF_SCHEDULER_ENABLED,
            CONF_SCHEDULER_URL,
            CONF_SCHEDULER_USERNAME,
            CONF_SCHEDULER_PASSWORD,
            CONF_SCHEDULER_BOOTSTRAP_ID,
            CONF_SCHEDULER_TIMEOUT,
            CONF_SCHEDULER_CA_COMPATIBILITY,
            CONF_SCHEDULER_CA_FILENAME,
            CONF_AUTOMATION_POLICY,
            CONF_LOAD_BALANCING,
            CONF_PRICE_OPTIMIZATION,
            CONF_ENERGY_FORECAST,
        )
    )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload only when scheduler options changed, not for allocator state writes."""
    runtime: RuntimeData | SimulatorEntryRuntime | None = hass.data.get(DOMAIN, {}).get(
        entry.entry_id
    )
    if (
        runtime is None
        or isinstance(runtime, SimulatorEntryRuntime)
        or runtime.scheduler_options_signature == _scheduler_options_signature(entry)
    ):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_query_running_service(call: ServiceCall) -> dict[str, Any]:
    """Query one configured Gateway through its read-only CommandManager path."""
    await _async_require_admin(call)
    runtime = _select_runtime(call)
    if isinstance(runtime, SimulatorEntryRuntime):
        raise HomeAssistantError(
            "Query running service is available only for production Gateway entries"
        )
    result = await runtime.command_manager.execute(RunningServiceGet().build())
    return result.as_dict()


def _scheduler_authenticated(manager: Any) -> bool:
    diagnostics = manager.diagnostics()
    client = diagnostics.get("client")
    return isinstance(client, dict) and client.get("sessid_in_memory") is True


def _scheduler_failure_classification(error: Exception) -> str:
    if isinstance(error, SchedulerAuthenticationError):
        return "authentication"
    if isinstance(error, SchedulerProtocolError):
        return "protocol"
    if isinstance(error, SchedulerClientError):
        return "transport"
    return "unknown"


def _scheduler_detail_failure_classification(error: Exception) -> str:
    if isinstance(error, ValueError):
        return "protocol"
    return _scheduler_failure_classification(error)


async def _async_refresh_scheduler_detail(call: ServiceCall) -> dict[str, Any]:
    """Perform one explicit, read-only scheduler DETAIL action."""
    runtime = _select_runtime(call)
    manager = getattr(runtime, "scheduler_manager", None)
    if manager is None:
        return {
            "status": "error",
            "authenticated": False,
            "error": "configuration",
        }

    sch_index = call.data[CONF_SCHEDULER_INDEX]
    try:
        await manager.refresh_detail(sch_index)
    except Exception as err:  # noqa: BLE001 - action returns sanitized failure classes
        response: dict[str, Any] = {
            "status": "error",
            "authenticated": (
                False
                if isinstance(err, SchedulerAuthenticationError)
                else _scheduler_authenticated(manager)
            ),
            "error": _scheduler_detail_failure_classification(err),
        }
        if isinstance(err, SchedulerAuthenticationError):
            response["authentication_reason"] = err.reason.value
            response["authentication_stage"] = err.stage.value
        return response
    return {
        "status": "success",
        "authenticated": _scheduler_authenticated(manager),
        "sch_index_match": True,
        "parsed": True,
    }


async def _async_refresh_scheduler_list(call: ServiceCall) -> dict[str, Any]:
    """Perform one explicit, read-only scheduler LIST action."""
    runtime = _select_runtime(call)
    manager = getattr(runtime, "scheduler_manager", None)
    if manager is None:
        return {
            "status": "error",
            "entry_count": 0,
            "authenticated": False,
            "error": "configuration",
        }

    now = datetime.now(UTC)
    from_epoch = int((now - timedelta(days=int(call.data[CONF_SCHEDULER_DAYS_BACK]))).timestamp())
    to_epoch = int((now + timedelta(days=int(call.data[CONF_SCHEDULER_DAYS_FORWARD]))).timestamp())
    try:
        entries = await manager.refresh_list(from_epoch, to_epoch)
    except Exception as err:  # noqa: BLE001 - action returns sanitized failure classes
        response: dict[str, Any] = {
            "status": "error",
            "entry_count": 0,
            "authenticated": _scheduler_authenticated(manager),
            "error": _scheduler_failure_classification(err),
        }
        if isinstance(err, SchedulerAuthenticationError):
            response["authentication_reason"] = err.reason.value
            response["authentication_stage"] = err.stage.value
        return response
    return {
        "status": "success",
        "entry_count": len(entries),
        "authenticated": _scheduler_authenticated(manager),
    }


async def _async_require_admin(call: ServiceCall) -> None:
    """Require an authenticated Home Assistant administrator for validation I/O."""
    user_id = call.context.user_id
    if user_id is None or not hasattr(call.hass, "auth"):
        raise HomeAssistantError("Administrator access is required")
    user = await call.hass.auth.async_get_user(user_id)
    if user is None or not user.is_admin:
        raise HomeAssistantError("Administrator access is required")


async def _async_run_scheduler_validation_roundtrip(call: ServiceCall) -> dict[str, Any]:
    """Run the explicit one-shot scheduler validation transaction."""
    await _async_require_admin(call)
    runtime = _select_runtime(call)
    if isinstance(runtime, SimulatorEntryRuntime):
        raise HomeAssistantError(
            "Scheduler validation is available only for production Gateway entries"
        )
    if not runtime.release_gate.live_writes_enabled:
        return {
            "status": "blocked",
            "phase": "preflight",
            "reason": "release_gate_closed",
            "preflight_blockers": ["release_gate_closed"],
        }
    return await runtime.async_run_scheduler_validation_roundtrip()


async def _async_set_service_reference_delay(call: ServiceCall) -> dict[str, Any]:
    """Apply the documented AMS prerequisite through exact Gateway readback."""
    await _async_require_admin(call)
    runtime = _select_runtime(call)
    if isinstance(runtime, SimulatorEntryRuntime):
        raise HomeAssistantError(
            "Service reference delay is available only for production Gateway entries"
        )
    controller = runtime.gateway_scheduler
    if controller is None:
        raise HomeAssistantError("Gateway scheduler client is unavailable")
    result = await controller.set_service_reference_delay(
        call.data[CONF_EXPECTED_SERVICE_REFERENCE_DELAY]
    )
    observer = runtime.load_balancing_observer
    observer_cache_updated = False
    if observer is not None:
        observer.apply_verified_reference_delay(result["after"])
        observer_cache_updated = True
    return {
        **result,
        "observer_cache_updated": observer_cache_updated,
    }


async def _async_run_meter_limitation_soft_limit_trial(
    call: ServiceCall,
) -> dict[str, Any]:
    """Run one bounded native soft-limit trial with mandatory rollback."""
    await _async_require_admin(call)
    runtime = _select_runtime(call)
    if isinstance(runtime, SimulatorEntryRuntime):
        raise HomeAssistantError(
            "Meter limitation trial is available only for production Gateway entries"
        )
    controller = runtime.gateway_scheduler
    if controller is None:
        raise HomeAssistantError("Gateway scheduler client is unavailable")
    return await controller.run_meter_limitation_soft_limit_trial(
        call.data[CONF_METER_LIMITATION_TRIAL_DIRECTION],
        call.data[CONF_METER_LIMITATION_TRIAL_MAX_CORRECTION],
    )


async def _async_generate_dashboard_config(call: ServiceCall) -> dict[str, Any]:
    """Return a native-entity Lovelace dashboard template without side effects."""
    await _async_require_admin(call)
    runtime = _select_runtime(call)
    entity_ids = resolve_dashboard_entity_ids(
        er.async_get(call.hass),
        serial=runtime.serial,
        platform=DOMAIN,
    )
    return generate_dashboard_config(entity_ids=entity_ids)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration domain; entries are configured through the UI."""
    del config
    if not hass.services.has_service(DOMAIN, "recover_expired_price_validation"):
        hass.services.async_register(
            DOMAIN, "recover_expired_price_validation", _async_recover_expired_price_validation,
            schema=_SERVICE_SCHEMA.extend({
                vol.Required("evidence"): dict,
                vol.Optional("dry_run", default=True): bool,
            }), supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, "run_price_guard_validation"):
        hass.services.async_register(
            DOMAIN, "run_price_guard_validation", _async_run_price_guard_validation,
            schema=_SERVICE_SCHEMA.extend({
                vol.Optional("duration_seconds", default=40): vol.All(
                    vol.Coerce(int), vol.In([40, 120])
                ),
            }), supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_METER_LIMITATION_SOFT_LIMIT_TRIAL):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_METER_LIMITATION_SOFT_LIMIT_TRIAL,
            _async_run_meter_limitation_soft_limit_trial,
            schema=_METER_LIMITATION_TRIAL_SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    if hasattr(hass, "http"):
        await async_register_control_api(hass)
    hass.data.setdefault(DOMAIN, {})
    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH_SCHEDULER_DETAIL):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH_SCHEDULER_DETAIL,
            _async_refresh_scheduler_detail,
            schema=_SCHEDULER_DETAIL_SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_REFRESH_SCHEDULER_LIST):
        hass.services.async_register(
            DOMAIN,
            SERVICE_REFRESH_SCHEDULER_LIST,
            _async_refresh_scheduler_list,
            schema=_SCHEDULER_SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_QUERY_RUNNING_SERVICE):
        hass.services.async_register(
            DOMAIN,
            SERVICE_QUERY_RUNNING_SERVICE,
            _async_query_running_service,
            schema=_SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_RUN_SCHEDULER_VALIDATION):
        hass.services.async_register(
            DOMAIN,
            SERVICE_RUN_SCHEDULER_VALIDATION,
            _async_run_scheduler_validation_roundtrip,
            schema=_SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_GENERATE_DASHBOARD_CONFIG):
        hass.services.async_register(
            DOMAIN,
            SERVICE_GENERATE_DASHBOARD_CONFIG,
            _async_generate_dashboard_config,
            schema=_SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    if not hass.services.has_service(DOMAIN, SERVICE_SET_SERVICE_REFERENCE_DELAY):
        hass.services.async_register(
            DOMAIN,
            SERVICE_SET_SERVICE_REFERENCE_DELAY,
            _async_set_service_reference_delay,
            schema=_REFERENCE_DELAY_SERVICE_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
    return True


async def _async_recover_expired_price_validation(call: ServiceCall) -> dict[str, Any]:
    """Audit one expired own validation without sending any battery mutation."""
    from .price_stop_recovery import recover_expired_price_validation

    await _async_require_admin(call)
    runtime = _select_runtime(call)
    if isinstance(runtime, SimulatorEntryRuntime):
        raise HomeAssistantError("price_recovery_requires_production")
    try:
        return await recover_expired_price_validation(
            runtime, call.data["evidence"], dry_run=call.data.get("dry_run", True),
        )
    except (KeyError, ValueError, TypeError) as error:
        raise HomeAssistantError("price_recovery_rejected: " + str(error)) from error


async def _async_run_price_guard_validation(call: ServiceCall) -> dict[str, Any]:
    """Explicitly validate own SOC-stop on an idle real battery at at most 300 W."""
    from .price_guard_trial import run_price_guard_validation

    await _async_require_admin(call)
    runtime = _select_runtime(call)
    if isinstance(runtime, SimulatorEntryRuntime):
        raise HomeAssistantError("price_guard_trial_requires_production")
    try:
        return await run_price_guard_validation(
            runtime, duration_seconds=call.data.get("duration_seconds", 40)
        )
    except ValueError as error:
        raise HomeAssistantError(str(error)) from error


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one production Gateway or one isolated local simulator."""
    runtime_mode = entry.data.get(
        CONF_RUNTIME_MODE,
        RuntimeMode.PRODUCTION.value,
    )
    if runtime_mode == RuntimeMode.SIMULATOR.value:
        simulator = SimulatorEntryRuntime(hass=hass, entry=entry)
        await simulator.async_initialize()
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = simulator
        forward_setups = getattr(
            getattr(hass, "config_entries", None),
            "async_forward_entry_setups",
            None,
        )
        if callable(forward_setups):
            await cast(Any, forward_setups)(
                entry,
                [
                    Platform(platform)
                    for platform in PLATFORMS
                    if platform != Platform.CALENDAR.value
                ],
            )
        return True
    if runtime_mode != RuntimeMode.PRODUCTION.value:
        raise ConfigEntryNotReady("Unsupported Pixiihome Controller runtime mode")
    if not await async_wait_for_mqtt_client(hass):
        raise ConfigEntryNotReady("Home Assistant MQTT is not connected")

    runtime = RuntimeData(hass=hass, serial=entry.data[CONF_SERIAL], entry=entry)
    await runtime.async_initialize_control_state()
    await runtime.async_initialize_scheduler()
    try:
        await runtime.start()
    except Exception as err:
        await runtime.stop()
        raise ConfigEntryNotReady("Unable to subscribe to Home Assistant MQTT") from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    runtime.load_balancing_observer = LoadBalancingObserver(runtime)
    runtime.meter_limit_load_balancer = MeterLimitLoadBalancer(runtime)
    runtime.solar_charging_controller = SolarChargingController(runtime)
    runtime.adaptive_peak_shaving_controller = AdaptivePeakShavingController(runtime)
    runtime.price_optimization_controller = PriceOptimizationController(runtime)
    runtime.price_optimization_controller.soc_guard.start()
    runtime.energy_forecast = EnergyForecastService(runtime)
    runtime.battery_activity = BatteryActivityTracker(runtime)
    remove_retired_entities(hass, entry, runtime.serial)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    if runtime.scheduler_coordinator is not None:
        await runtime.scheduler_coordinator.async_start()
    if runtime.controller_state.load_balancing_enabled:
        try:
            await runtime.meter_limit_load_balancer.prepare()
            await runtime.meter_limit_load_balancer.activate()
        except Exception as error:  # fail closed and expose the reason on the sensor
            runtime.meter_limit_load_balancer.status = "blocked"
            runtime.meter_limit_load_balancer.reason = str(error)
    if runtime.controller_state.automatic_solar_charging_enabled:
        try:
            await runtime.solar_charging_controller.prepare()
            runtime.solar_charging_controller.start()
        except Exception as error:  # fail closed and expose the reason on the sensor
            runtime.solar_charging_controller.status = "blocked"
            runtime.solar_charging_controller.reason = str(error)
            runtime.solar_charging_controller.start()
    if runtime.controller_state.adaptive_peak_shaving_enabled:
        try:
            await runtime.adaptive_peak_shaving_controller.prepare()
            runtime.adaptive_peak_shaving_controller.start()
        except Exception as error:  # fail closed and expose the reason on the sensor
            runtime.adaptive_peak_shaving_controller.status = "blocked"
            runtime.adaptive_peak_shaving_controller.reason = str(error)
            runtime.adaptive_peak_shaving_controller.start()
    if runtime.controller_state.automatic_price_optimization_enabled:
        try:
            await runtime.price_optimization_controller.prepare()
            runtime.price_optimization_controller.start()
        except Exception as error:  # fail closed and expose the reason on the sensor
            runtime.price_optimization_controller.status = "blocked"
            runtime.price_optimization_controller.reason = str(error)
            runtime.price_optimization_controller.start()
    elif runtime.price_optimization_controller.needs_post_check():
        # Switched off (for example held off by a restart), but an own
        # schedule's removal is unconfirmed: run only the read-only check.
        runtime.price_optimization_controller.start()
    runtime.load_balancing_observer.start()
    try:
        await runtime.energy_forecast.start()
    except Exception as error:  # noqa: BLE001 - forecasts are optional; never block setup
        runtime.energy_forecast.status = "error"
        runtime.energy_forecast.reason = str(error) or "forecast_start_failed"
    try:
        runtime.battery_activity.start()
    except Exception:  # noqa: BLE001 - the activity log is optional; never block setup
        pass
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload one entry without leaving callbacks or persisted state behind."""
    runtime: RuntimeData | SimulatorEntryRuntime | None = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None
    )
    if runtime is not None:
        await runtime.stop()
    platforms = [
        Platform(platform)
        for platform in PLATFORMS
        if not (isinstance(runtime, SimulatorEntryRuntime) and platform == Platform.CALENDAR.value)
    ]
    return await hass.config_entries.async_unload_platforms(entry, platforms)
