"""Persistent Home Assistant config-entry wrapper for the local simulator."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import CONF_AUTOMATION_POLICY, DOMAIN
from .control.adapters import AdapterRequest
from .control.authorization import GrantRegistry
from .control.controllers import ControllerArbitrator, ControllerDecision, ControllerInputs
from .control.engine import EngineResult, InMemoryJournalPort
from .control.journal import ControlJournal, JournalPhase, JournalTargetIdentity
from .control.models import ControlOperation, RuntimeMode, ScheduleOrigin
from .control.policy import AutomationPolicy
from .control.settings import ControllerState, OperationalSettings
from .control.simulator import SimulatedSchedule, SimulatorRuntime
from .exceptions import CommandValidationError

SIMULATOR_SERIAL = "SIMULATOR"
SIMULATOR_SCENARIOS: tuple[str, ...] = (
    "direct_target_soc",
    "direct_demand_response",
    "scheduler_crud",
    "external_conflict",
    "deterministic_planner",
    "recovery_required",
    "partial_replacement",
    "timeout_delivery_unknown",
)


class _SimulatorTelemetry:
    def __init__(self, runtime: SimulatorRuntime) -> None:
        self._runtime = runtime

    def add_listener(self, callback: Any) -> Any:
        del callback
        return lambda: None

    def is_fresh(self, family: str) -> bool:
        return family in {"core", "battery", "meter", "energy"}

    def value(self, family: str, key: str) -> object:
        adapter = self._runtime.adapter
        if family == "core" and key == "sys_status":
            return 1
        if family == "core" and key == "grid_status":
            return 1
        if family == "core" and key == "srv_type":
            return adapter.active_service
        if family == "battery" and key == "batt_status":
            return 1
        if family == "meter" and key in {"building_ac_w", "meter_w"}:
            return adapter.battery.power_w
        return None

class _SimulatorCapabilities:
    messages_seen = 1


@dataclass(slots=True)
class SimulatorEntryRuntime:
    """One isolated, persisted simulator with no MQTT or production transport."""

    hass: HomeAssistant
    entry: ConfigEntry
    serial: str = field(init=False, default=SIMULATOR_SERIAL)
    runtime_mode: RuntimeMode = field(init=False, default=RuntimeMode.SIMULATOR)
    scheduler_options_signature: tuple[object, ...] = field(init=False, default=())
    simulator: SimulatorRuntime = field(init=False)
    _previews: dict[str, dict[str, Any]] = field(
        init=False,
        default_factory=dict,
        repr=False,
    )
    grant_registry: GrantRegistry = field(
        init=False,
        default_factory=GrantRegistry,
        repr=False,
    )
    _mutation_lock: asyncio.Lock = field(
        init=False,
        default_factory=asyncio.Lock,
        repr=False,
    )
    _store: Store[dict[str, Any]] = field(init=False, repr=False)
    operational_settings: OperationalSettings = field(
        default_factory=OperationalSettings.defaults
    )
    controller_state: ControllerState = field(default_factory=ControllerState)
    production_authority_enabled: bool = True
    production_authority_revision: int = 1
    controller_arbitrator: ControllerArbitrator = field(
        default_factory=ControllerArbitrator
    )
    last_controller_decision: ControllerDecision | None = field(
        init=False, default=None
    )
    def __post_init__(self) -> None:
        self._store = Store(
            self.hass,
            1,
            f"{DOMAIN}.{self.entry.entry_id}.simulator",
        )
        self.simulator = self._new_runtime()

    def _new_runtime(self) -> SimulatorRuntime:
        return SimulatorRuntime(
            journal=InMemoryJournalPort(
                ControlJournal.empty(
                    config_entry_id=self.entry.entry_id,
                    gateway_serial=self.serial,
                )
            )
        )
    def _restore_product_state(self) -> None:
        controller_state = self.simulator.journal.journal.controller_state
        authority = controller_state.get("production_control_authority")
        self.production_authority_enabled = (
            authority.get("enabled") is True if isinstance(authority, dict) else True
        )
        revision = authority.get("revision") if isinstance(authority, dict) else None
        self.production_authority_revision = (
            revision if isinstance(revision, int) and revision > 0 else 1
        )
        self.operational_settings = OperationalSettings.from_mapping(
            controller_state.get("operational_settings")
        )
        self.controller_state = ControllerState.from_mapping(
            controller_state.get("controller_state")
        )


    async def async_initialize(self) -> None:
        """Load only this entry's simulator state or persist a clean baseline."""
        state = await self._store.async_load()
        if state is not None:
            self.simulator = SimulatorRuntime.from_state(
                state,
                config_entry_id=self.entry.entry_id,
                gateway_serial=self.serial,
            )
            self._restore_product_state()
            return
        self._restore_product_state()
        await self.async_save()

    async def async_save(self) -> None:
        """Persist and verify the complete simulator state."""
        journal = self.simulator.journal.journal
        controller_state = {
            **journal.controller_state,
            "production_control_authority": {
                "enabled": self.production_authority_enabled,
                "revision": self.production_authority_revision,
            },
            "operational_settings": self.operational_settings.as_dict(),
            "controller_state": self.controller_state.as_dict(),
        }
        self.simulator.journal.journal = journal.with_product_state(
            controller_state=controller_state
        )
        expected = self.simulator.export_state()
        await self._store.async_save(expected)
        observed = await self._store.async_load()
        if _canonical(observed) != _canonical(expected):
            raise CommandValidationError("simulator state persistence verification failed")

    async def _require_admin(self, user_id: str | None) -> None:
        if user_id is None:
            raise CommandValidationError("administrator access is required")
        auth = getattr(self.hass, "auth", None)
        get_user = getattr(auth, "async_get_user", None)
        if not callable(get_user):
            raise CommandValidationError("administrator access is required")
        user = await cast(Any, get_user)(user_id)
        if user is None or not user.is_admin:
            raise CommandValidationError("administrator access is required")

    @property
    def battery(self) -> Any:
        return self.simulator.adapter.battery

    @property
    def telemetry(self) -> _SimulatorTelemetry:
        return _SimulatorTelemetry(self.simulator)

    @property
    def capabilities(self) -> _SimulatorCapabilities:
        return _SimulatorCapabilities()

    def evaluate_automatic_controllers(
        self,
        inputs: ControllerInputs,
    ) -> ControllerDecision:
        decision = self.controller_arbitrator.evaluate(
            self.operational_settings,
            self.controller_state,
            AutomationPolicy.from_mapping(
                getattr(self.entry, "options", {}).get(CONF_AUTOMATION_POLICY)
            ),
            inputs,
        )
        self.last_controller_decision = decision
        return decision


    def production_control_enabled(self) -> bool:
        """Simulator control remains isolated from production transport."""
        return self.production_authority_enabled
    def _automation_policy_valid(self) -> bool:
        """Return whether the configured policy explicitly authorizes automation."""
        policy = AutomationPolicy.from_mapping(
            getattr(self.entry, "options", {}).get(CONF_AUTOMATION_POLICY)
        )
        return (
            policy.enabled
            and "automatic" in policy.allowed_operation_classes
            and bool(policy.allowed_service_classes)
            and policy.max_duration_seconds > 0
            and policy.max_power_kw > 0
            and policy.soc_min_percent < policy.soc_max_percent
        )

    async def async_set_production_control_enabled(
        self,
        enabled: bool,
        user_id: str | None,
    ) -> None:
        await self._require_admin(user_id)
        if type(enabled) is not bool:
            raise CommandValidationError("production authority state is invalid")
        async with self._mutation_lock:
            self.production_authority_enabled = enabled
            self.production_authority_revision += 1
            if not enabled:
                self.controller_state = ControllerState()
            await self.async_save()

    async def async_set_operational_setting(
        self,
        key: str,
        value: object,
        user_id: str | None,
    ) -> None:
        await self._require_admin(user_id)
        async with self._mutation_lock:
            self.operational_settings = self.operational_settings.set(key, value)
            await self.async_save()

    async def async_set_controller_enabled(
        self,
        key: str,
        enabled: bool,
        user_id: str | None,
    ) -> None:
        await self._require_admin(user_id)
        if key not in {
            "automatic_solar_charging_enabled",
            "adaptive_peak_shaving_enabled",
            "load_balancing_enabled",
            "automatic_price_optimization_enabled",
        }:
            raise CommandValidationError("unknown automatic controller")
        if type(enabled) is not bool:
            raise CommandValidationError("controller enabled state is invalid")
        async with self._mutation_lock:
            if enabled and (
                not self.production_control_enabled() or not self._automation_policy_valid()
            ):
                raise CommandValidationError(
                    "Production authority and AutomationPolicy are required"
                )
            self.controller_state = ControllerState.from_mapping(
                {**self.controller_state.as_dict(), key: enabled}
            )
            await self.async_save()

    async def async_execute_native(
        self,
        operation: str,
        user_id: str | None,
    ) -> dict[str, Any]:
        await self._require_admin(user_id)
        if not self.production_authority_enabled:
            raise CommandValidationError("production control is disabled")
        target = JournalTargetIdentity(asset_id=self.simulator.adapter.battery.asset_id)
        if operation == "charge":
            request = AdapterRequest(
                ControlOperation.DIRECT_DEMAND_RESPONSE,
                service="demandresponse",
                target_identity=target,
                payload={
                    "power_w": float(self.operational_settings.get("manual_power_kw")) * 1000,
                    "duration_seconds": self.operational_settings.get(
                        "manual_power_duration_seconds"
                    ),
                },
            )
        elif operation == "discharge":
            request = AdapterRequest(
                ControlOperation.DIRECT_DEMAND_RESPONSE,
                service="demandresponse",
                target_identity=target,
                payload={
                    "power_w": -float(self.operational_settings.get("manual_power_kw")) * 1000,
                    "duration_seconds": self.operational_settings.get(
                        "manual_power_duration_seconds"
                    ),
                },
            )
        elif operation == "target_soc_start":
            request = AdapterRequest(
                ControlOperation.DIRECT_TARGET_SOC,
                service="targetsoc",
                target_identity=target,
                payload={
                    "socref": self.operational_settings.get("target_soc_percent"),
                    "duration_seconds": self.operational_settings.get(
                        "target_soc_duration_seconds"
                    ),
                },
            )
        elif operation == "aps_start":
            request = AdapterRequest(
                ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING,
                service="adaptivepeakshaving",
                target_identity=target,
                payload={
                    "export_limit_w": (
                        float(self.operational_settings.get("aps_export_limit_kw")) * 1000
                    ),
                    "import_limit_w": (
                        float(self.operational_settings.get("aps_import_limit_kw")) * 1000
                    ),
                    "charge_max_w": (
                        float(self.operational_settings.get("aps_max_charge_kw")) * 1000
                    ),
                    "discharge_max_w": (
                        float(self.operational_settings.get("aps_max_discharge_kw")) * 1000
                    ),
                    "keep_active": self.operational_settings.get("aps_keep_active"),
                    "max_limit_change_w": float(
                        self.operational_settings.get("aps_max_limit_change_kw")
                    ) * 1000,
                    "duration_seconds": self.operational_settings.get(
                        "manual_power_duration_seconds"
                    ),
                },
            )
        elif operation == "calibration_start":
            request = AdapterRequest(
                ControlOperation.DIRECT_BATTERY_CALIBRATION,
                service="batterycalibration",
                target_identity=target,
                payload={
                    "endpoint_soc": self.operational_settings.get("target_soc_percent"),
                    "duration_seconds": self.operational_settings.get(
                        "target_soc_duration_seconds"
                    ),
                },
            )
        elif operation in {"manual_stop", "target_soc_stop", "aps_stop"}:
            request = AdapterRequest(
                ControlOperation.STOP,
                service="demandresponse",
                target_identity=target,
            )
        else:
            raise CommandValidationError("simulator native operation is unsupported")
        result = await self.async_execute(request, grant_authorized=True)
        return {
            "status": result.status.value,
            "reason": result.reason,
            "transaction_id": result.transaction_id,
            "blocked_reasons": list(result.decision.blocked_reasons),
            "state": (
                dict(result.adapter_result.state)
                if result.adapter_result is not None
                else None
            ),
        }
    def _invalidate_previews(self) -> None:
        self._previews.clear()
        self.grant_registry.clear()

    async def async_execute(
        self,
        request: AdapterRequest,
        **authorization: Any,
    ) -> EngineResult:
        async with self._mutation_lock:
            return await self._async_execute_unlocked(request, **authorization)

    async def _async_execute_unlocked(
        self,
        request: AdapterRequest,
        **authorization: Any,
    ) -> EngineResult:
        result = await self.simulator.execute(request, **authorization)
        self._invalidate_previews()
        await self.async_save()
        return result

    async def async_advance(self, seconds: int | float) -> dict[str, Any]:
        async with self._mutation_lock:
            state = self.simulator.advance(seconds)
            self._invalidate_previews()
            await self.async_save()
            return state

    async def async_reset(self) -> dict[str, Any]:
        async with self._mutation_lock:
            self.simulator.reset()
            self._invalidate_previews()
            await self.async_save()
            return self.simulator.snapshot()

    async def async_load_scenario(self, scenario: str) -> dict[str, Any]:
        """Load deterministic preconditions for one approved demonstration path."""
        if scenario not in SIMULATOR_SCENARIOS:
            raise ValueError("unknown simulator scenario")
        async with self._mutation_lock:
            return await self._async_load_scenario_unlocked(scenario)

    async def _async_load_scenario_unlocked(self, scenario: str) -> dict[str, Any]:
        self._invalidate_previews()
        self.simulator.reset()
        start = self.simulator.adapter.clock.now + timedelta(minutes=15)
        target = JournalTargetIdentity(
            m_id=240000001,
            sch_index=1,
            service_no=110,
            source_id=7,
            asset_id="battery-1",
        )
        if scenario in {"scheduler_crud", "partial_replacement"}:
            await self.simulator.execute(
                AdapterRequest(
                    ControlOperation.CREATE,
                    service="targetsoc",
                    target_identity=target,
                    payload={
                        "start": start,
                        "duration_seconds": 3600,
                        "socref": 80,
                    },
                ),
                grant_authorized=True,
            )
        elif scenario == "external_conflict":
            self.simulator.adapter.seed_external_schedule(
                SimulatedSchedule(
                    m_id=target.m_id or 240000001,
                    start=start,
                    duration_seconds=3600,
                    socref=80,
                    origin=ScheduleOrigin.EXTERNAL_OBSERVED,
                    sch_index=target.sch_index,
                    service_no=target.service_no or 110,
                    source_id=target.source_id,
                )
            )
        elif scenario == "recovery_required":
            self.simulator.adapter.schedules[target.m_id or 240000001] = (
                SimulatedSchedule(
                    m_id=target.m_id or 240000001,
                    start=start,
                    duration_seconds=3600,
                    socref=70,
                    origin=ScheduleOrigin.LOCAL_CREATED,
                    sch_index=target.sch_index,
                    service_no=target.service_no or 110,
                    source_id=target.source_id,
                )
            )
            await self.simulator.journal.begin_control_transaction(
                transaction_id="scenario-recovery",
                operation=ControlOperation.CREATE.value,
                target_identity=target,
                intent={
                    "start": start.isoformat(),
                    "duration_seconds": 3600,
                    "socref": 70,
                },
            )
            await self.simulator.journal.transition_control_transaction(
                "scenario-recovery",
                JournalPhase.COMMAND_SENT,
            )
            self.simulator.journal.journal = (
                self.simulator.journal.journal.mark_unfinished_recovery_required()
            )
        if scenario == "partial_replacement":
            self.simulator.adapter.inject_fault("replace_pause_after_delete")
        elif scenario == "timeout_delivery_unknown":
            self.simulator.adapter.inject_fault("timeout")
        journal = self.simulator.journal.journal
        self.simulator.journal.journal = journal.with_product_state(
            controller_state={
                **journal.controller_state,
                "scenario": scenario,
            },
            audit_event={
                "at": self.simulator.adapter.clock.now.isoformat(),
                "event": "scenario_loaded",
                "scenario": scenario,
            },
        )
        await self.async_save()
        return self.simulator.snapshot()

    def cache_preview(self, preview_id: str, record: dict[str, Any]) -> None:
        self._previews[preview_id] = record
        while len(self._previews) > 32:
            evicted = self._previews.pop(next(iter(self._previews)))
            self.grant_registry.discard(evicted.get("grant_id"))

    async def async_apply_preview(
        self,
        preview_id: str,
    ) -> tuple[str, EngineResult | None]:
        """Atomically consume, validate, execute, and persist one preview."""
        async with self._mutation_lock:
            preview = self._previews.pop(preview_id, None)
            if preview is None:
                return "missing", None
            grant_id = preview.get("grant_id")
            if preview["state_fingerprint"] != self.simulator.state_fingerprint():
                self.grant_registry.discard(grant_id)
                return "stale", None
            grant_authorized = (
                isinstance(grant_id, str)
                and self.grant_registry.authorize(grant_id, **preview["grant_scope"])
            )
            result = await self._async_execute_unlocked(
                preview["request"],
                grant_authorized=grant_authorized,
                automation_authorized=False,
            )
            return "executed", result

    async def stop(self) -> None:
        async with self._mutation_lock:
            await self.async_save()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


__all__ = [
    "SIMULATOR_SCENARIOS",
    "SIMULATOR_SERIAL",
    "SimulatorEntryRuntime",
]
