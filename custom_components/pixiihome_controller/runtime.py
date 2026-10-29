"""Per-config-entry runtime state."""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from time import time
from typing import Any, cast
from uuid import uuid4

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store

from .allocator import MessageIdAllocator
from .aps_controller import AdaptivePeakShavingController, build_aps_lease_request
from .battery import BatteryModel, BatteryReading
from .battery_activity import BatteryActivityTracker
from .capabilities import CapabilityRegistry
from .command import (
    CommandManager,
    CommandMethod,
    CommandStatus,
    CorrelatedCommandRequest,
)
from .const import (
    BATTERY_CALIBRATION_UNTIL,
    CONF_AUTOMATION_POLICY,
    CONF_COMMAND_STATE,
    CONF_CONTROL_JOURNAL_INITIALIZED,
    CONF_SCHEDULER_BOOTSTRAP_ID,
    CONF_SCHEDULER_CA_COMPATIBILITY,
    CONF_SCHEDULER_CA_FILENAME,
    CONF_SCHEDULER_ENABLED,
    CONF_SCHEDULER_OWNERSHIP_STATE,
    CONF_SCHEDULER_PASSWORD,
    CONF_SCHEDULER_TIMEOUT,
    CONF_SCHEDULER_URL,
    CONF_SCHEDULER_USERNAME,
    DOMAIN,
    LIVE_WRITES_ENABLED,
    LOWERED_EXTERNAL_TARGETS,
    RESUME_AFTER_RECOVERY,
)
from .control.controllers import (
    ControllerArbitrator,
    ControllerDecision,
    ControllerInputs,
)
from .control.journal import ControlJournal, JournalPhase, JournalTargetIdentity
from .control.load_balancing import CONF_LOAD_BALANCING
from .control.models import (
    ControlOperation,
    ControlPolicyInput,
    ExternalControlState,
    Initiative,
    OperationImpact,
    OperatorMode,
    PlantHealth,
    ReleaseState,
    RuntimeMode,
    ScheduleOrigin,
)
from .control.policy import AutomationPolicy
from .control.price_optimization import CONF_PRICE_OPTIMIZATION
from .control.production import (
    ProductionAdapterRequest,
    ProductionControlAdapter,
    ProductionControlAuthority,
)
from .control.settings import ControllerState, OperationalSettings
from .energy_forecast_service import CONF_ENERGY_FORECAST, EnergyForecastService
from .exceptions import SchedulerClientError, SchedulerConfigurationError
from .load_balancing_observer import LoadBalancingObserver
from .meter_limit_controller import MeterLimitLoadBalancer
from .mqtt import PixiiMqttTransport
from .price_controller import PriceOptimizationController
from .price_stop import verify_price_stopped
from .protocol.enums import SERVICE_TYPE_LABELS, STATUS_LABELS
from .protocol.models import ParsedMqttMessage
from .protocol.scheduler_commands import ScheduledUpdate
from .scheduler.client import SchedulerClientConfig
from .scheduler.coordinator import SchedulerInventoryCoordinator
from .scheduler.gateway import AiohttpSchedulerGatewayClient, GatewaySchedulerController
from .scheduler.manager import SCHEDULER_LIST_ENTRY_LIMIT, SchedulerRefreshManager
from .scheduler.mutations import SchedulerCreateRequest, SchedulerMutationManager
from .scheduler.ownership import OwnershipLifecycle, SchedulerOwnershipStore
from .scheduler.tls import (
    build_scheduler_ssl_context,
    default_scheduler_tls_diagnostics,
    scheduler_tls_diagnostics,
)
from .scheduler.transport import HomeAssistantSchedulerCommandExecutor
from .scheduler.validation import SchedulerValidationRoundtrip
from .solar_controller import SolarChargingController, build_solar_lease_request
from .telemetry import TelemetryStore


def _scheduler_snapshot(manager: SchedulerRefreshManager) -> list[dict[str, object]]:
    """Return the bounded normalized scheduler state used for apply binding."""
    return [
        {
            "sch_index": schedule.sch_index,
            "start_epoch": schedule.start_epoch,
            "end_epoch": schedule.end_epoch,
            "duration_seconds": schedule.duration_seconds,
            "service_no": schedule.service_no,
            "service_type": schedule.service_type,
            "service_type_label": schedule.service_type_label,
            "enabled": schedule.enabled,
            "service_priority": schedule.service_priority,
            "asset_id": schedule.asset_id,
            "source_id": schedule.source_id,
            "origin": schedule.ownership.ownership.value,
            "source_label": schedule.ownership.source_label,
            "editable": schedule.ownership.editable,
            "deletable": schedule.ownership.deletable,
            "protected": schedule.ownership.protected,
            "completeness": schedule.completeness.value,
            "consistency": schedule.consistency.value,
            "recurrence": {
                "recurring": schedule.recurrence.recurring,
                "details": dict(schedule.recurrence.recurrence_detail or ()),
            },
        }
        for schedule in manager.normalized_schedules[:SCHEDULER_LIST_ENTRY_LIMIT]
    ]


@dataclass(slots=True)
class ReleaseGateState:
    """Release status kept separate from health and operation impact."""

    live_writes_enabled: bool = LIVE_WRITES_ENABLED
    infrastructure_status: str = "partially_live_validated"
    blocked_reasons: set[str] = field(
        default_factory=lambda: {
            "release_writes_disabled",
            "state_changing_control_not_live_validated",
        }
    )


@dataclass(slots=True)
class RuntimeData:
    hass: HomeAssistant
    serial: str
    telemetry: TelemetryStore = field(default_factory=TelemetryStore)
    capabilities: CapabilityRegistry = field(default_factory=CapabilityRegistry)
    scheduler_options_signature: tuple[object, ...] = field(init=False, default=())
    control_journal: ControlJournal | None = field(init=False, default=None)
    control_store: Store[dict[str, Any]] | None = field(init=False, default=None)
    control_backup_store: Store[dict[str, Any]] | None = field(init=False, default=None)
    _control_journal_lock: asyncio.Lock = field(
        init=False,
        repr=False,
        default_factory=asyncio.Lock,
    )
    _controller_transition_lock: asyncio.Lock = field(
        init=False, repr=False, default_factory=asyncio.Lock,
    )
    _pending_allocator_state: dict[str, Any] | None = field(
        init=False,
        repr=False,
        default=None,
    )
    _allocator_state_generation: int = field(init=False, repr=False, default=0)
    _allocator_flush_task: asyncio.Task[None] | None = field(
        init=False,
        repr=False,
        default=None,
    )
    _allocator_flush_error: Exception | None = field(init=False, repr=False, default=None)
    control_state_error: str | None = field(init=False, default=None)
    scheduler_mutation_config_error: str | None = field(init=False, default=None)
    scheduler_ownership: SchedulerOwnershipStore | None = field(init=False, default=None)
    scheduler_executor: HomeAssistantSchedulerCommandExecutor | None = field(
        init=False, default=None
    )
    scheduler_mutation_manager: SchedulerMutationManager | None = field(init=False, default=None)
    battery_model: BatteryModel = field(default_factory=BatteryModel)
    release_gate: ReleaseGateState = field(default_factory=ReleaseGateState)
    production_authority: ProductionControlAuthority = field(
        default_factory=ProductionControlAuthority
    )
    operational_settings: OperationalSettings = field(default_factory=OperationalSettings.defaults)
    controller_state: ControllerState = field(default_factory=ControllerState)
    controller_arbitrator: ControllerArbitrator = field(default_factory=ControllerArbitrator)
    last_controller_decision: ControllerDecision | None = field(init=False, default=None)
    load_balancing_observer: LoadBalancingObserver | None = field(init=False, default=None)
    meter_limit_load_balancer: MeterLimitLoadBalancer | None = field(init=False, default=None)
    solar_charging_controller: SolarChargingController | None = field(init=False, default=None)
    adaptive_peak_shaving_controller: AdaptivePeakShavingController | None = field(
        init=False, default=None
    )
    price_optimization_controller: PriceOptimizationController | None = field(
        init=False, default=None
    )
    energy_forecast: EnergyForecastService | None = field(init=False, default=None)
    battery_activity: BatteryActivityTracker | None = field(init=False, default=None)
    operational_settings_error: str | None = field(init=False, default=None)
    # Epoch when a manually started battery calibration ends; the automatic
    # controllers hold off until then.
    battery_calibration_until: float | None = field(init=False, default=None)
    # Aggregator Target SOC schedules price optimization lowered, with the
    # aggregator's own reference, so they can be raised back after a restart.
    lowered_external_targets: dict[tuple[int, int, int, int], float] = field(
        init=False, default_factory=dict
    )
    _production_previews: dict[str, dict[str, Any]] = field(
        init=False, default_factory=dict, repr=False
    )
    transport: PixiiMqttTransport | None = None
    entry: ConfigEntry | None = None
    command_manager: CommandManager = field(init=False)
    production_adapter: ProductionControlAdapter = field(init=False)
    scheduler_manager: SchedulerRefreshManager | None = field(init=False, default=None)
    gateway_scheduler: GatewaySchedulerController | None = field(init=False, default=None)
    scheduler_target_soc: dict[int, dict[str, Any]] = field(init=False, default_factory=dict)
    scheduler_coordinator: SchedulerInventoryCoordinator | None = field(init=False, default=None)
    scheduler_config_error: str | None = field(init=False, default=None)
    scheduler_validation_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    scheduler_validation_running: bool = field(init=False, default=False)
    scheduler_validation_attempt_consumed: bool = field(init=False, default=False)
    scheduler_validation_last_result: dict[str, object] | None = field(init=False, default=None)
    scheduler_tls_state: dict[str, object] = field(
        init=False, default_factory=default_scheduler_tls_diagnostics
    )

    def __post_init__(self) -> None:
        self.transport = PixiiMqttTransport(self.hass, self.serial, self.handle_message)
        allocator = self._build_allocator()
        self.command_manager = CommandManager(
            self.hass,
            self.serial,
            self.capabilities,
            self.plant_health,
            operation_impact=self.operation_impact,
            allocator=allocator,
            on_ack_observed=self.capabilities.observe,
            production_control_enabled=self.production_control_enabled,
            state_changing_lock=self._control_journal_lock,
        )
        self.production_adapter = ProductionControlAdapter(self.command_manager)
        settings = self._scheduler_settings()
        self.scheduler_options_signature = tuple(
            settings.get(key)
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
        if settings.get(CONF_SCHEDULER_CA_COMPATIBILITY) is True:
            self.scheduler_tls_state.update(
                {
                    "tls_mode": "pixii_ca_compatibility",
                    "custom_ca_configured": True,
                }
            )

    def production_control_enabled(self) -> bool:
        """Return the current fail-closed production authority state."""
        return self.production_authority.enabled

    def _fail_closed_control_state(self, reason: str) -> None:
        """Disable live writes when durable controller state cannot be trusted."""
        self.control_journal = None
        self.command_manager.allocator = MessageIdAllocator()
        self.production_authority = ProductionControlAuthority()
        self.operational_settings = OperationalSettings.defaults()
        self.controller_state = ControllerState()
        self.release_gate.live_writes_enabled = False
        self.release_gate.blocked_reasons = {
            "control_journal_invalid",
            "production_control_disabled",
            "state_changing_control_not_live_validated",
        }
        self.control_state_error = reason

    def _scheduler_settings(self) -> dict[str, object]:
        settings: dict[str, object] = {}
        if self.entry is not None:
            settings.update(self.entry.data)
            settings.update(self.entry.options)
        return settings

    async def async_initialize_control_state(self) -> None:
        """Load or migrate durable identity state before enabling mutation infrastructure."""
        entry = self.entry
        if entry is None:
            return
        store: Store[dict[str, Any]] = Store(
            self.hass,
            1,
            f"{DOMAIN}.{entry.entry_id}.control_journal",
        )
        backup_store: Store[dict[str, Any]] = Store(
            self.hass,
            1,
            f"{DOMAIN}.{entry.entry_id}.control_journal_backup",
        )
        self.control_store = store
        self.control_backup_store = backup_store
        try:
            initialized = entry.data.get(CONF_CONTROL_JOURNAL_INITIALIZED) is True

            def validate_journal(
                journal: ControlJournal,
            ) -> tuple[ControlJournal, MessageIdAllocator, bool]:
                recovery_journal = journal.mark_unfinished_recovery_required()
                recovery_required = recovery_journal is not journal
                journal = recovery_journal
                SchedulerOwnershipStore.from_state(
                    dict(journal.ownership_state) or None,
                    config_entry_id=entry.entry_id,
                    gateway_serial=self.serial,
                )
                allocator = MessageIdAllocator.from_state(
                    journal.command_state or None,
                    persist=self._persist_allocator_state,
                )
                return journal, allocator, recovery_required

            def validate_exact_state(
                state: Mapping[str, Any],
            ) -> tuple[ControlJournal, MessageIdAllocator, bool]:
                journal = ControlJournal.from_state(
                    state,
                    config_entry_id=entry.entry_id,
                    gateway_serial=self.serial,
                )
                if journal.recovered_from_backup:
                    raise RuntimeError("journal state requires embedded rollback")
                return validate_journal(journal)

            primary_failed = False
            try:
                primary_state = await store.async_load()
            except Exception:
                primary_state = None
                primary_failed = True

            validated: tuple[ControlJournal, MessageIdAllocator, bool] | None = None
            if primary_state is not None:
                try:
                    validated = validate_exact_state(primary_state)
                except Exception:
                    primary_failed = True

            backup_failed = False
            try:
                backup_state = await backup_store.async_load()
            except Exception:
                backup_state = None
                backup_failed = True

            journal_store_ambiguous = (
                validated is not None and initialized and (backup_state is None or backup_failed)
            )
            loaded_from_backup = False
            if validated is not None and backup_state is not None:
                try:
                    backup_validated = validate_exact_state(backup_state)
                except Exception:
                    backup_validated = None
                    backup_failed = True
                if backup_validated is not None:
                    primary_journal = validated[0]
                    backup_journal = backup_validated[0]
                    journal_store_ambiguous = (
                        primary_journal.revision != backup_journal.revision
                        or primary_journal.snapshot()["checksum"]
                        != backup_journal.snapshot()["checksum"]
                    )
            elif validated is None and backup_state is not None:
                try:
                    validated = validate_exact_state(backup_state)
                    loaded_from_backup = True
                except Exception:
                    backup_failed = True

            if validated is not None and initialized and backup_failed:
                journal_store_ambiguous = True
            created_from_legacy = False
            if validated is None:
                if initialized or primary_failed or backup_failed:
                    raise RuntimeError("initialized control journal is unreadable")
                created_from_legacy = True
                validated = validate_journal(
                    ControlJournal.migrate_legacy(
                        config_entry_id=entry.entry_id,
                        gateway_serial=self.serial,
                        command_state=entry.data.get(CONF_COMMAND_STATE),
                        ownership_state=entry.data.get(CONF_SCHEDULER_OWNERSHIP_STATE),
                    )
                )
            journal, allocator, recovery_required = validated
            recovery_blocked = bool(journal.unresolved_transactions)
            original_controller_state = dict(journal.controller_state)
            stored_authority = ProductionControlAuthority.from_mapping(
                original_controller_state.get("production_control_authority")
            )
            authority = stored_authority
            operational_settings = OperationalSettings.from_mapping(
                original_controller_state.get("operational_settings")
            )
            controller_state = ControllerState.from_mapping(
                original_controller_state.get("controller_state")
            )
            authority_forced_closed = (
                loaded_from_backup or recovery_blocked or journal_store_ambiguous
            )
            resume = original_controller_state.get(RESUME_AFTER_RECOVERY)
            calibration_until = original_controller_state.get(BATTERY_CALIBRATION_UNTIL)
            self.lowered_external_targets = _lowered_targets_from(
                original_controller_state.get(LOWERED_EXTERNAL_TARGETS)
            )
            self.battery_calibration_until = (
                float(calibration_until)
                if isinstance(calibration_until, int | float)
                and not isinstance(calibration_until, bool)
                and calibration_until > time()
                else None
            )
            if authority_forced_closed:
                # Only an unfinished command closes control here; remember the
                # user's switches so they come back once it is settled. A
                # backup or ambiguous store never resumes on its own.
                if (
                    recovery_blocked
                    and not (loaded_from_backup or journal_store_ambiguous)
                    and not isinstance(resume, dict)
                    and stored_authority.enabled
                ):
                    resume = controller_state.as_dict()
                elif loaded_from_backup or journal_store_ambiguous:
                    resume = None
                authority = ProductionControlAuthority(revision=stored_authority.revision)
                controller_state = ControllerState()
            elif not authority.enabled and (
                controller_state.automatic_solar_charging_enabled
                or controller_state.load_balancing_enabled
                or controller_state.automatic_price_optimization_enabled
            ):
                controller_state = ControllerState()
            normalized_controller_state = dict(original_controller_state)
            normalized_controller_state["production_control_authority"] = authority.as_dict()
            normalized_controller_state.pop(RESUME_AFTER_RECOVERY, None)
            if isinstance(resume, dict) and not authority.enabled:
                normalized_controller_state[RESUME_AFTER_RECOVERY] = resume
            if "operational_settings" in original_controller_state:
                normalized_controller_state["operational_settings"] = operational_settings.as_dict()
            if (
                "controller_state" in original_controller_state
                or controller_state != ControllerState()
            ):
                normalized_controller_state["controller_state"] = controller_state.as_dict()
            state_changed = normalized_controller_state != original_controller_state
            if state_changed:
                journal = journal.with_product_state(controller_state=normalized_controller_state)
            self.production_authority = authority
            self.operational_settings = operational_settings
            self.controller_state = controller_state
            gate_open = authority.enabled and not authority_forced_closed
            self.release_gate.live_writes_enabled = gate_open
            self.release_gate.blocked_reasons = (
                set()
                if gate_open
                else {
                    "production_control_disabled",
                    "state_changing_control_not_live_validated",
                    *({"control_recovery_required"} if recovery_blocked else set()),
                    *({"control_journal_ambiguous"} if journal_store_ambiguous else set()),
                }
            )
            self.control_journal = journal
            self.command_manager.allocator = allocator
            if (
                created_from_legacy
                or loaded_from_backup
                or journal.migrated_from_legacy
                or journal.recovered_from_backup
                or recovery_required
                or state_changed
            ):
                await self._save_control_journal_verified(journal)

            if (
                not initialized
                or CONF_COMMAND_STATE in entry.data
                or CONF_SCHEDULER_OWNERSHIP_STATE in entry.data
            ):
                data = dict(entry.data)
                data[CONF_CONTROL_JOURNAL_INITIALIZED] = True
                data.pop(CONF_COMMAND_STATE, None)
                data.pop(CONF_SCHEDULER_OWNERSHIP_STATE, None)
                self.hass.config_entries.async_update_entry(entry, data=data)
        except Exception:  # noqa: BLE001 - corrupt durable state fails closed
            self._fail_closed_control_state("control_journal_invalid")

    async def async_initialize_scheduler(self) -> None:
        """Initialize the optional scheduler client without blocking Home Assistant."""
        settings = self._scheduler_settings()
        try:
            scheduler_config = SchedulerClientConfig.from_mapping(settings)
            if scheduler_config is None:
                return

            ca_file = None
            if scheduler_config.pixii_ca_compatibility:
                ca_filename = scheduler_config.ca_filename
                if ca_filename is None:
                    raise SchedulerConfigurationError("ca_filename_required")
                try:
                    ca_file = self.hass.config.path("ssl", ca_filename)
                except (AttributeError, TypeError):
                    raise SchedulerConfigurationError("ca_file_missing") from None

            build_ssl_context = partial(
                build_scheduler_ssl_context,
                pixii_ca_compatibility=scheduler_config.pixii_ca_compatibility,
                ca_file=ca_file,
            )
            ssl_context = await self.hass.async_add_executor_job(build_ssl_context)
            self.scheduler_tls_state = scheduler_tls_diagnostics(
                ssl_context,
                pixii_ca_compatibility=scheduler_config.pixii_ca_compatibility,
            )
            scheduler_client = AiohttpSchedulerGatewayClient(
                async_get_clientsession(self.hass),
                scheduler_config,
                ssl_context=ssl_context,
            )
            self.scheduler_manager = SchedulerRefreshManager(scheduler_client)
            self.gateway_scheduler = GatewaySchedulerController(self, scheduler_client)
            self.scheduler_coordinator = SchedulerInventoryCoordinator(
                self.hass, self.scheduler_manager
            )
            self._initialize_scheduler_mutation()
        except SchedulerConfigurationError as err:
            self.scheduler_config_error = err.code
        except SchedulerClientError as err:
            self.scheduler_config_error = type(err).__name__

    def _initialize_scheduler_mutation(self) -> None:
        """Wire passive scheduler mutation infrastructure after read setup succeeds."""
        entry = self.entry
        if (
            self.scheduler_manager is None
            or entry is None
            or self.control_journal is None
            or self.control_state_error is not None
        ):
            if self.scheduler_manager is not None and entry is not None:
                self.scheduler_mutation_config_error = (
                    self.control_state_error or "control_journal_unavailable"
                )
            return
        try:
            ownership = SchedulerOwnershipStore.from_state(
                dict(self.control_journal.ownership_state) or None,
                config_entry_id=entry.entry_id,
                gateway_serial=self.serial,
            )
        except Exception:  # noqa: BLE001 - invalid persisted state fails closed
            self.scheduler_mutation_config_error = "ownership_state_invalid"
            return

        self.scheduler_ownership = ownership
        self.scheduler_executor = HomeAssistantSchedulerCommandExecutor(self.command_manager)
        self.scheduler_mutation_manager = SchedulerMutationManager(
            allocator=self.command_manager.allocator,
            ownership=ownership,
            policy_provider=self.scheduler_control_context,
            executor=self.scheduler_executor,
            persist_ownership=self._persist_scheduler_ownership,
            journal=self,
            transaction_id_factory=lambda: f"scheduler-{uuid4().hex}",
            verify_price_stopped=partial(verify_price_stopped, self),
            allow_reserved_price_stop=True,
        )

    async def _save_control_journal_verified(self, journal: ControlJournal) -> None:
        """Persist and reload one exact journal revision before acknowledging commit."""
        primary = self.control_store
        backup = self.control_backup_store
        entry = self.entry
        if primary is None or backup is None or entry is None:
            raise RuntimeError("control journal stores are unavailable")
        if getattr(self.hass, "state", None) in {
            CoreState.stopping,
            CoreState.final_write,
            CoreState.stopped,
        }:
            raise RuntimeError("control journal commits are disabled during shutdown")
        expected = journal.snapshot()
        try:
            for store in (backup, primary):
                await store.async_save(expected)
                persisted = await store.async_load()
                if persisted is None:
                    raise RuntimeError("control journal commit was not durable")
                verified = ControlJournal.from_state(
                    persisted,
                    config_entry_id=entry.entry_id,
                    gateway_serial=self.serial,
                )
                if (
                    verified.revision != journal.revision
                    or verified.snapshot()["checksum"] != expected["checksum"]
                ):
                    raise RuntimeError("control journal commit verification failed")
        except Exception:
            await self._persist_fail_closed_journal(journal)
            raise

    async def _persist_fail_closed_journal(self, journal: ControlJournal) -> None:
        """Best-effort persist a disabled authority after any commit failure."""
        primary = self.control_store
        backup = self.control_backup_store
        if primary is None or backup is None:
            return
        stored_authority = ProductionControlAuthority.from_mapping(
            journal.controller_state.get("production_control_authority")
        )
        state = dict(journal.controller_state)
        state["production_control_authority"] = ProductionControlAuthority(
            revision=stored_authority.revision
        ).as_dict()
        state["controller_state"] = ControllerState().as_dict()
        expected = journal.with_product_state(controller_state=state).snapshot()
        for store in (primary, backup):
            try:
                await store.async_save(expected)
            except Exception:
                continue

    async def _require_admin(self, user_id: str | None) -> None:
        """Resolve the HA caller and reject anonymous/non-admin access."""
        if user_id is None:
            raise HomeAssistantError("Administrator access is required")
        auth = getattr(self.hass, "auth", None)
        get_user = getattr(auth, "async_get_user", None)
        if not callable(get_user):
            raise HomeAssistantError("Administrator access is required")
        user = await cast(Any, get_user)(user_id)
        if user is None or not user.is_admin:
            raise HomeAssistantError("Administrator access is required")

    async def async_set_production_control_enabled(
        self,
        enabled: bool,
        user_id: str | None,
    ) -> None:
        """Persist the explicit production-control authority decision."""
        await self._require_admin(user_id)
        if type(enabled) is not bool:
            raise HomeAssistantError("Production authority state is invalid")
        cleanup_problems: list[str] = []
        if not enabled and self.production_control_enabled():
            # Gateway writes are still authorized here. Remove what the
            # integration created before closing the gate; afterwards even the
            # SOC guard could no longer stop an already planned price interval.
            cleanup_problems = await self._async_release_owned_control()
        if (
            not enabled
            and self.meter_limit_load_balancer is not None
            and self.meter_limit_load_balancer.lease is not None
        ):
            try:
                await self.meter_limit_load_balancer.deactivate()
            except Exception as error:  # noqa: BLE001 - persist authority off despite cleanup failure
                reason = (
                    str(error)
                    if isinstance(error, ValueError)
                    else "meter_limitation_restore_failed"
                )
                cleanup_problems.append(f"load_balancing ({reason})")
        async with self._control_journal_lock:
            journal = self.control_journal
            if journal is None:
                raise HomeAssistantError("ControllerStore is unavailable")
            authority = ProductionControlAuthority(
                enabled=enabled,
                revision=self.production_authority.revision + 1,
            )
            controller_state = self.controller_state
            if not enabled:
                controller_state = ControllerState()
            state = {
                **journal.controller_state,
                "production_control_authority": authority.as_dict(),
                "controller_state": controller_state.as_dict(),
            }
            # An explicit choice replaces any switches waiting for recovery.
            state.pop(RESUME_AFTER_RECOVERY, None)
            candidate = journal.with_product_state(controller_state=state)
            try:
                await self._save_control_journal_verified(candidate)
            except Exception:
                self._fail_closed_control_state("control_journal_persistence_failed")
                raise
            self.control_journal = candidate
            self.production_authority = authority
            self.controller_state = controller_state
            self.release_gate.live_writes_enabled = enabled
            self.release_gate.blocked_reasons = (
                set()
                if enabled
                else {"production_control_disabled", "state_changing_control_not_live_validated"}
            )
        if not enabled:
            from .activity import log_activity

            log_activity(
                self, "price_optimization_status",
                "Control authority turned off"
                + (f"; could not remove: {', '.join(cleanup_problems)}" if cleanup_problems
                   else "; integration-owned schedules and leases were removed first"),
                cause="Administrator disabled production control; "
                + ("cleanup needs recovery" if cleanup_problems else "cleanup completed"),
            )
        if cleanup_problems:
            from homeassistant.components import persistent_notification

            persistent_notification.async_create(
                self.hass,
                "Production control authority was turned off, but these "
                "integration-owned Pixii effects could not be removed and may "
                "still run: " + ", ".join(cleanup_problems),
                title="Pixii: cleanup incomplete",
                notification_id=f"pixiihome_controller_{self.serial}_authority_cleanup",
            )

    async def _async_release_owned_control(self) -> list[str]:
        """Stop integration-owned leases and price schedules; report, never raise."""
        problems: list[str] = []
        async with self._controller_transition_lock:
            for name in ("adaptive_peak_shaving_controller", "solar_charging_controller"):
                controller = getattr(self, name, None)
                if controller is None:
                    continue
                try:
                    await controller.stop()
                except Exception:  # noqa: BLE001 - authority off must still proceed
                    problems.append(f"{name}_stop_failed")
                    continue
                if controller.owns_live_lease():
                    # The bounded lease still expires by itself.
                    problems.append(f"{name}_lease_still_active")
            if self.price_optimization_controller is not None:
                try:
                    problems.extend(
                        await self.price_optimization_controller.release_for_authority_off()
                    )
                except Exception:  # noqa: BLE001 - authority off must still proceed
                    problems.append("price_release_failed")
        return problems

    async def _persist_product_controller_state(
        self,
        *,
        operational_settings: OperationalSettings | None = None,
        controller_state: ControllerState | None = None,
    ) -> None:
        """Persist one canonical settings/controller snapshot in the journal store."""
        async with self._control_journal_lock:
            await self._persist_product_controller_state_locked(
                operational_settings=operational_settings,
                controller_state=controller_state,
            )

    async def async_resume_after_recovery(self) -> bool:
        """Turn the remembered switches back on once no command is unsettled.

        A restart with an unfinished command closes control and remembers
        what was on. Nothing is resumed while any transaction is unresolved.
        Each controller comes back through its normal switch path, so its own
        checks still apply.
        """
        async with self._control_journal_lock:
            journal = self.control_journal
            if journal is None or journal.unresolved_transactions:
                return False
            resume = journal.controller_state.get(RESUME_AFTER_RECOVERY)
            if not isinstance(resume, dict) or self.production_control_enabled():
                return False
            authority = ProductionControlAuthority(
                enabled=True, revision=self.production_authority.revision + 1,
            )
            state = {
                **journal.controller_state,
                "production_control_authority": authority.as_dict(),
            }
            state.pop(RESUME_AFTER_RECOVERY, None)
            candidate = journal.with_product_state(controller_state=state)
            try:
                await self._save_control_journal_verified(candidate)
            except Exception:
                self._fail_closed_control_state("control_journal_persistence_failed")
                raise
            self.control_journal = candidate
            self.production_authority = authority
            self.release_gate.live_writes_enabled = True
            self.release_gate.blocked_reasons = set()
        restored: list[str] = []
        failed: list[str] = []
        async with self._controller_transition_lock:
            for key, value in ControllerState.from_mapping(resume).as_dict().items():
                if not value:
                    continue
                name = key.removesuffix("_enabled").replace("_", " ")
                try:
                    await self._async_set_controller_enabled_locked(key, True)
                except Exception as error:  # noqa: BLE001 - report, keep the rest
                    failed.append(f"{name} ({error})")
                else:
                    restored.append(name)
        from .activity import log_activity

        log_activity(
            self, "price_optimization_status",
            "Control authority turned back on"
            + (f"; switched on again: {', '.join(restored)}" if restored else "")
            + (f"; could not switch on: {', '.join(failed)}" if failed else ""),
            "Who: the integration. Why: these switches were on before the restart. "
            "They were held off until an unfinished command was settled by a "
            "read-only check, which has now happened.",
        )
        return True

    async def _persist_product_controller_state_locked(
        self,
        *,
        operational_settings: OperationalSettings | None = None,
        controller_state: ControllerState | None = None,
    ) -> None:
        """Persist one product snapshot while the caller owns the journal lock."""
        journal = self.control_journal
        if journal is None:
            raise HomeAssistantError("ControllerStore is unavailable")
        state = dict(journal.controller_state)
        if operational_settings is not None:
            state["operational_settings"] = operational_settings.as_dict()
        if controller_state is not None:
            state["controller_state"] = controller_state.as_dict()
        candidate = journal.with_product_state(controller_state=state)
        try:
            await self._save_control_journal_verified(candidate)
        except Exception:
            self._fail_closed_control_state("control_journal_persistence_failed")
            raise
        self.control_journal = candidate

    def _automation_policy(self) -> AutomationPolicy:
        """Return the canonical fail-closed policy snapshot."""
        if self.entry is None:
            return AutomationPolicy.disabled()
        return AutomationPolicy.from_mapping(self.entry.options.get(CONF_AUTOMATION_POLICY))

    def _automation_policy_valid(self) -> bool:
        """Return whether the configured policy explicitly authorizes automation."""
        policy = self._automation_policy()
        return (
            policy.enabled
            and "automatic" in policy.allowed_operation_classes
            and bool(policy.allowed_service_classes)
            and policy.max_duration_seconds > 0
            and policy.max_power_kw > 0
            and policy.soc_min_percent < policy.soc_max_percent
        )

    def evaluate_automatic_controllers(
        self,
        inputs: ControllerInputs,
    ) -> ControllerDecision:
        """Evaluate automatic controller proposals without performing transport I/O."""
        decision = self.controller_arbitrator.evaluate(
            self.operational_settings,
            self.controller_state,
            self._automation_policy(),
            inputs,
        )
        self.last_controller_decision = decision
        return decision

    async def async_apply_production_preview(
        self,
        preview_id: str,
        user_id: str | None,
    ) -> dict[str, Any]:
        """Consume one current scheduler preview through typed mutation APIs."""
        await self._require_admin(user_id)
        preview = self._production_previews.pop(preview_id, None)
        if preview is None:
            raise HomeAssistantError("preview_missing_or_consumed")
        if preview.get("user_id") != user_id or time() > preview.get("expires_at", 0):
            raise HomeAssistantError("preview_missing_or_consumed")
        if not self.production_control_enabled():
            raise HomeAssistantError("production_control_disabled")
        current_revision = (
            self.control_journal.revision if self.control_journal is not None else None
        )
        if preview.get("authority_revision") != self.production_authority.revision:
            raise HomeAssistantError("preview_state_changed")
        if preview.get("journal_revision") != current_revision:
            raise HomeAssistantError("preview_state_changed")
        if preview.get("blocked_reasons"):
            raise HomeAssistantError("preview_blocked")
        manager = self.scheduler_mutation_manager
        if manager is None or self.scheduler_manager is None:
            raise HomeAssistantError("scheduler_mutation_unavailable")
        evidence = self.scheduler_manager.list_visibility_diagnostics()
        evidence_binding = {
            key: evidence.get(key)
            for key in (
                "invalidated",
                "from_epoch",
                "to_epoch",
                "last_successful_at",
            )
        }
        if preview.get("scheduler_evidence_binding") != evidence_binding:
            raise HomeAssistantError("preview_state_changed")
        preview_evidence = preview.get("scheduler_evidence")
        if not isinstance(preview_evidence, dict):
            raise HomeAssistantError("preview_state_changed")
        from_epoch = preview_evidence.get("from_epoch")
        to_epoch = preview_evidence.get("to_epoch")
        if (
            isinstance(from_epoch, bool)
            or not isinstance(from_epoch, int)
            or isinstance(to_epoch, bool)
            or not isinstance(to_epoch, int)
        ):
            raise HomeAssistantError("preview_state_changed")
        try:
            entries = await self.scheduler_manager.refresh_list(from_epoch, to_epoch)
            if preview["operation"] == ControlOperation.CREATE.value:
                for item in entries:
                    await self.scheduler_manager.refresh_detail(item.sch_index)
        except Exception as err:
            raise HomeAssistantError("preview_state_changed") from err
        if preview.get("scheduler_snapshot") != _scheduler_snapshot(self.scheduler_manager):
            raise HomeAssistantError("preview_state_changed")
        operation = ControlOperation(preview["operation"])
        intent = preview.get("intent", {})
        target = preview.get("target", {})
        if operation is ControlOperation.CREATE:
            result = await manager.create(
                SchedulerCreateRequest(
                    starttime=datetime.fromtimestamp(intent["start_epoch"], UTC),
                    duration_seconds=intent["duration_seconds"],
                    service=preview["service"],
                    values=(
                        {"socref": intent["socref"]}
                        if preview["service"] == "targetsoc"
                        else intent["values"]
                    ),
                )
            )
        elif operation is ControlOperation.UPDATE:
            m_id = target.get("m_id")
            if isinstance(m_id, bool) or not isinstance(m_id, int):
                raise HomeAssistantError("scheduler_target_m_id_required")
            result = await manager.update(
                ScheduledUpdate(
                    m_id=m_id,
                    service="targetsoc",
                    values={"socref": intent["socref"]} if "socref" in intent else None,
                    duration_seconds=intent.get("duration_seconds"),
                )
            )
        elif operation is ControlOperation.DELETE:
            m_id = target.get("m_id")
            if isinstance(m_id, bool) or not isinstance(m_id, int):
                raise HomeAssistantError("scheduler_target_m_id_required")
            result = await manager.delete(m_id)
        elif operation is ControlOperation.RECOVERY_APPLY:
            transaction_id = intent.get("transaction_id")
            if not isinstance(transaction_id, str):
                raise HomeAssistantError("recovery_transaction_required")
            transaction = (
                self.control_journal.transaction(transaction_id)
                if self.control_journal is not None
                else None
            )
            m_id = transaction.target_identity.m_id if transaction is not None else None
            if m_id is None:
                raise HomeAssistantError("recovery_target_m_id_required")
            result = await manager.recover_provisional(m_id)
        else:
            raise HomeAssistantError("production_preview_operation_unsupported")
        return {
            "status": result.status.value,
            "operation": result.operation.value,
            "m_id": result.m_id,
            "reason": result.reason,
            "blocked_reasons": list(result.blocked_reasons),
        }

    async def async_set_operational_setting(
        self,
        key: str,
        value: object,
        user_id: str | None,
    ) -> None:
        """Validate and persist one canonical operational setting."""
        await self._require_admin(user_id)
        async with self._control_journal_lock:
            try:
                updated = self.operational_settings.set(key, value)
            except (KeyError, ValueError) as err:
                raise HomeAssistantError("Operational setting is invalid") from err
            await self._persist_product_controller_state_locked(operational_settings=updated)
            self.operational_settings = updated
        if (
            key.startswith(("price_", "solar_smart_"))
            and self.price_optimization_controller is not None
        ):
            self.price_optimization_controller.request_refresh()

    async def async_set_controller_enabled(
        self,
        key: str,
        enabled: bool,
        user_id: str | None,
    ) -> None:
        """Persist an integration-owned automatic-controller enabled state."""
        await self._require_admin(user_id)
        async with self._controller_transition_lock:
            await self._async_set_controller_enabled_locked(key, enabled)

    async def _async_set_controller_enabled_locked(self, key: str, enabled: bool) -> None:
        """Serialize mode checks, Gateway preparation and durable transitions."""
        if key not in {
            "automatic_solar_charging_enabled",
            "adaptive_peak_shaving_enabled",
            "load_balancing_enabled",
            "automatic_price_optimization_enabled",
        }:
            raise HomeAssistantError("Unknown automatic controller")
        if type(enabled) is not bool:
            raise HomeAssistantError("Controller enabled state is invalid")
        controller = self.meter_limit_load_balancer
        solar_controller = self.solar_charging_controller
        aps_controller = self.adaptive_peak_shaving_controller
        price_controller = self.price_optimization_controller
        if key == "automatic_solar_charging_enabled":
            if solar_controller is None:
                raise HomeAssistantError("solar_charging_controller_unavailable")
            try:
                if enabled:
                    if not self.production_control_enabled() or not self._automation_policy_valid():
                        raise ValueError("Production authority and AutomationPolicy are required")
                    if self.controller_state.adaptive_peak_shaving_enabled:
                        raise ValueError("solar_conflicts_with_adaptive_peak_shaving")
                    await solar_controller.prepare()
                else:
                    await solar_controller.stop()
            except ValueError as error:
                solar_controller.status, solar_controller.reason = "blocked", str(error)
                raise HomeAssistantError(str(error)) from error
        if key == "adaptive_peak_shaving_enabled":
            if aps_controller is None:
                raise HomeAssistantError("adaptive_peak_shaving_controller_unavailable")
            try:
                if enabled:
                    if not self.production_control_enabled() or not self._automation_policy_valid():
                        raise ValueError("Production authority and AutomationPolicy are required")
                    await aps_controller.prepare()
                else:
                    await aps_controller.stop()
            except ValueError as error:
                aps_controller.status, aps_controller.reason = "blocked", str(error)
                raise HomeAssistantError(str(error)) from error
        if key == "load_balancing_enabled":
            if controller is None:
                raise HomeAssistantError("load_balancing_configuration_required")
            try:
                if enabled:
                    if not self.production_control_enabled() or not self._automation_policy_valid():
                        raise ValueError("Production authority and AutomationPolicy are required")
                    await controller.prepare()
                else:
                    await controller.deactivate()
            except ValueError as error:
                controller.status, controller.reason = "blocked", str(error)
                raise HomeAssistantError(str(error)) from error
        if key == "automatic_price_optimization_enabled":
            if price_controller is None:
                raise HomeAssistantError("price_optimization_controller_unavailable")
            try:
                if enabled:
                    if not self.production_control_enabled() or not self._automation_policy_valid():
                        raise ValueError("Production authority and AutomationPolicy are required")
                    await price_controller.prepare()
                else:
                    await price_controller.disable()
            except ValueError as error:
                price_controller.status, price_controller.reason = "blocked", str(error)
                raise HomeAssistantError(str(error)) from error
        async with self._control_journal_lock:
            if enabled and (
                not self.production_control_enabled() or not self._automation_policy_valid()
            ):
                raise HomeAssistantError("Production authority and AutomationPolicy are required")
            updated = ControllerState.from_mapping(
                {**self.controller_state.as_dict(), key: enabled}
            )
            await self._persist_product_controller_state_locked(controller_state=updated)
            self.controller_state = updated
        if key == "automatic_solar_charging_enabled" and enabled:
            assert solar_controller is not None
            solar_controller.start()
        if key == "adaptive_peak_shaving_enabled" and enabled:
            assert aps_controller is not None
            aps_controller.start()
        if key == "load_balancing_enabled" and enabled:
            assert controller is not None
            try:
                await controller.activate()
            except Exception as error:
                async with self._control_journal_lock:
                    rolled_back = ControllerState.from_mapping(
                        {**self.controller_state.as_dict(), key: False}
                    )
                    await self._persist_product_controller_state_locked(
                        controller_state=rolled_back
                    )
                    self.controller_state = rolled_back
                try:
                    await controller.deactivate()
                except Exception:
                    pass
                raise HomeAssistantError("load_balancing_activation_failed") from error
        if key == "automatic_price_optimization_enabled" and enabled:
            assert price_controller is not None
            price_controller.start()

    async def async_execute_native(
        self,
        operation: str,
        user_id: str | None,
    ) -> dict[str, Any]:
        """Execute one exact native command through the typed production adapter."""
        await self._require_admin(user_id)
        if not self.production_control_enabled():
            raise HomeAssistantError("production_control_disabled")
        if operation in {"manual_stop", "target_soc_stop", "aps_stop"}:
            raise HomeAssistantError("service_specific_stop_semantics_unverified")
        if operation == "charge":
            request = ProductionAdapterRequest(
                ControlOperation.DIRECT_DEMAND_RESPONSE,
                {
                    "power_w": float(self.operational_settings.get("manual_power_kw")) * 1000,
                    "duration_seconds": self.operational_settings.get(
                        "manual_power_duration_seconds"
                    ),
                },
            )
        elif operation == "discharge":
            request = ProductionAdapterRequest(
                ControlOperation.DIRECT_DEMAND_RESPONSE,
                {
                    "power_w": -float(self.operational_settings.get("manual_power_kw")) * 1000,
                    "duration_seconds": self.operational_settings.get(
                        "manual_power_duration_seconds"
                    ),
                },
            )
        elif operation == "target_soc_start":
            request = ProductionAdapterRequest(
                ControlOperation.DIRECT_TARGET_SOC,
                {
                    "soc_percent": self.operational_settings.get("target_soc_percent"),
                    "duration_seconds": self.operational_settings.get(
                        "target_soc_duration_seconds"
                    ),
                },
            )
        elif operation == "aps_start":
            request = ProductionAdapterRequest(
                ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING,
                {
                    "export_limit_w": float(self.operational_settings.get("aps_export_limit_kw"))
                    * 1000,
                    "import_limit_w": float(self.operational_settings.get("aps_import_limit_kw"))
                    * 1000,
                    "charge_max_w": float(self.operational_settings.get("aps_max_charge_kw"))
                    * 1000,
                    "discharge_max_w": float(self.operational_settings.get("aps_max_discharge_kw"))
                    * 1000,
                    "keep_active": self.operational_settings.get("aps_keep_active"),
                    "max_limit_change_w": float(
                        self.operational_settings.get("aps_max_limit_change_kw")
                    )
                    * 1000,
                    "duration_seconds": self.operational_settings.get(
                        "manual_power_duration_seconds"
                    ),
                },
            )
        elif operation == "calibration_start":
            # Pixii doc 13880 rev 1.7, 4.2.5: a prioritised service that first
            # discharges the battery, then charges it to endpoint_soc.
            request = ProductionAdapterRequest(
                ControlOperation.DIRECT_BATTERY_CALIBRATION,
                {
                    "soc_percent": self.operational_settings.get(
                        "calibration_endpoint_soc_percent"
                    ),
                    "duration_seconds": self.operational_settings.get(
                        "calibration_duration_seconds"
                    ),
                },
            )
            self._require_calibration_window(int(request.payload["duration_seconds"]))
        else:
            raise HomeAssistantError("Unknown native control operation")
        if self.control_journal is not None and self.control_journal.unresolved_transactions:
            raise HomeAssistantError("control_recovery_required")
        transaction_id = f"native-{uuid4().hex}"
        if self.control_journal is not None:
            await self.begin_control_transaction(
                transaction_id=transaction_id,
                operation=f"native_{operation}",
                target_identity=JournalTargetIdentity(asset_id=self.serial),
                intent=request.payload,
            )
            await self.transition_control_transaction(transaction_id, JournalPhase.COMMAND_SENT)
        try:
            result = await self.production_adapter.execute(request)
        except Exception:
            if self.control_journal is not None:
                await self.transition_control_transaction(
                    transaction_id,
                    JournalPhase.RECOVERY_REQUIRED,
                    reason="adapter_exception",
                )
            raise
        lifecycle = "reconciled"
        readback: Any = None
        if result.successful:
            expected_status = {
                ControlOperation.DIRECT_DEMAND_RESPONSE: {102},
                ControlOperation.DIRECT_TARGET_SOC: {107},
                ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING: {105},
                ControlOperation.DIRECT_BATTERY_CALIBRATION: {116},
            }[request.operation]
            try:
                readback = await self.production_adapter.readback()
            except Exception:
                readback = None
            verified = (
                readback is not None
                and readback.successful
                and readback.ack is not None
                and readback.ack.service_status in expected_status
            )
            if self.control_journal is not None:
                await self.transition_control_transaction(
                    transaction_id,
                    JournalPhase.RECONCILED if verified else JournalPhase.RECOVERY_REQUIRED,
                    reason="readback_verified" if verified else "readback_unverified",
                )
            lifecycle = "reconciled" if verified else "readback_unverified"
        elif result.status.value in {"delivery_unknown", "timed_out"}:
            lifecycle = "recovery_required"
            if self.control_journal is not None:
                await self.transition_control_transaction(
                    transaction_id,
                    JournalPhase.RECOVERY_REQUIRED,
                    reason=result.status.value,
                )
        elif self.control_journal is not None:
            lifecycle = "failed"
            await self.transition_control_transaction(
                transaction_id,
                JournalPhase.FAILED,
                reason=result.status.value,
            )
        if request.operation is ControlOperation.DIRECT_BATTERY_CALIBRATION and result.successful:
            await self._start_calibration_hold(
                int(request.payload["duration_seconds"]),
                float(request.payload["soc_percent"]),
                lifecycle,
                user_id,
            )
        output = result.as_dict()
        output["successful"] = lifecycle == "reconciled"
        output["lifecycle"] = lifecycle
        output["readback"] = readback.as_dict() if readback is not None else None
        output["authority_revision"] = self.production_authority.revision
        return output

    def battery_calibration_active(self) -> bool:
        """Whether a manually started calibration may still be running."""
        until = self.battery_calibration_until
        return until is not None and time() < until

    def _require_calibration_window(self, duration_seconds: int) -> None:
        """Refuse a calibration that would override another service or schedule.

        Calibration is prioritised, so it would push aside whatever runs in its
        window. Only an aggregator Target SOC may be overridden, and only when
        changing aggregator schedules is allowed. Everything else must be idle
        and the complete schedule list must cover the whole window.
        """
        settings = self.operational_settings
        allow_aggregator = bool(settings.get("price_adjust_external_target_soc"))
        price = self.price_optimization_controller
        aggregator_source = price.config.managed_source_id if price is not None else None
        service = self.telemetry.value("core", "srv_type")
        own_lease = any(
            controller is not None and controller.owns_live_lease()
            for controller in (
                self.solar_charging_controller,
                self.adaptive_peak_shaving_controller,
            )
        )
        if not (
            service == 0
            or (service == 105 and own_lease)
            or (service == 107 and allow_aggregator and aggregator_source is not None)
        ):
            raise HomeAssistantError(
                "Kalibrering nekad: en annan tjänst körs just nu "
                "(battery_calibration_service_running)"
            )
        coordinator = self.scheduler_coordinator
        snapshot = None if coordinator is None else coordinator.last_good_snapshot
        now = int(time())
        end = now + duration_seconds
        if (
            snapshot is None
            or snapshot.data_status != "current"
            or snapshot.window_start_epoch > now
            or snapshot.window_end_epoch < end
        ):
            raise HomeAssistantError(
                "Kalibrering nekad: schemalistan från Gateway är inte aktuell "
                "(battery_calibration_schedule_list_unavailable)"
            )
        for schedule in snapshot.schedules:
            if not schedule.enabled:
                continue
            if schedule.recurrence.recurring is not False:
                # Repeats are not expanded in the list; their times are unknown.
                raise HomeAssistantError(
                    "Kalibrering nekad: ett återkommande schema finns "
                    "(battery_calibration_recurring_schedule)"
                )
            if not (schedule.start_epoch < end and now < schedule.end_epoch):
                continue
            if (
                allow_aggregator
                and aggregator_source is not None
                and schedule.service_type == 107
                and schedule.source_id == aggregator_source
            ):
                continue
            starts = datetime.fromtimestamp(schedule.start_epoch, UTC).astimezone()
            raise HomeAssistantError(
                f"Kalibrering nekad: ett schema kl {starts:%H:%M} ligger inom "
                "kalibreringstiden (battery_calibration_conflicts_with_schedule)"
            )

    async def async_save_lowered_targets(
        self, lowered: dict[tuple[int, int, int, int], float]
    ) -> None:
        """Remember lowered aggregator targets across restarts (best effort)."""
        self.lowered_external_targets = dict(lowered)
        async with self._control_journal_lock:
            journal = self.control_journal
            if journal is None:
                return
            candidate = journal.with_product_state(
                controller_state={
                    **journal.controller_state,
                    LOWERED_EXTERNAL_TARGETS: [
                        [*key, reference] for key, reference in sorted(lowered.items())
                    ],
                }
            )
            try:
                await self._save_control_journal_verified(candidate)
                self.control_journal = candidate
            except Exception:  # noqa: BLE001 - kept in memory; raising waits for evidence
                pass

    async def _start_calibration_hold(
        self,
        duration_seconds: int,
        endpoint_soc: float,
        lifecycle: str,
        user_id: str | None,
    ) -> None:
        """Hold the automatic controllers off for the calibration window."""
        until = time() + duration_seconds
        self.battery_calibration_until = until
        async with self._control_journal_lock:
            journal = self.control_journal
            if journal is not None:
                candidate = journal.with_product_state(
                    controller_state={
                        **journal.controller_state,
                        BATTERY_CALIBRATION_UNTIL: until,
                    }
                )
                try:
                    await self._save_control_journal_verified(candidate)
                    self.control_journal = candidate
                except Exception:  # noqa: BLE001 - the hold still applies in memory
                    pass
        from .activity import log_activity

        ends = datetime.fromtimestamp(until, UTC).astimezone()
        log_activity(
            self,
            "price_optimization_status",
            f"Battery calibration started: discharges, then charges to {endpoint_soc:g} %; "
            f"automatic control waits until {ends:%H:%M}"
            + ("" if lifecycle == "reconciled" else " (start not confirmed by readback)"),
            cause=f"Started by hand (user {user_id})" if user_id else "Started by hand",
        )

    async def async_verify_aps_lease(self, message_id: int) -> dict[str, Any]:
        """Read back one integration-owned APS lease by its exact Pixii m_id."""
        result = await self.command_manager.execute_correlated(
            CorrelatedCommandRequest(
                method=CommandMethod.GET,
                message_id=message_id,
                expected_services=("adaptivepeakshaving",),
            )
        )
        ack = result.ack
        verified = (
            result.status is CommandStatus.SUCCESS
            and ack is not None
            and ack.service == "adaptivepeakshaving"
            and ack.service_status in {3, 5, 6, 7, 8, 9, 10, 11}
            and ack.service_duration_left is not None
            and ack.service_duration_left > 0
        )
        return {
            "successful": verified,
            "status": result.status.value,
            "message_id": message_id,
            "service_status": ack.service_status if ack is not None else None,
            "service_duration_left": (
                ack.service_duration_left if ack is not None else None
            ),
        }

    async def async_verify_solar_lease(self, message_id: int) -> dict[str, Any]:
        """Compatibility wrapper for the solar controller's APS lease."""
        return await self.async_verify_aps_lease(message_id)

    def release_aps_lease_identity(self, message_id: int) -> None:
        """Release a locally owned, expired APS identity without a Gateway write."""
        self.command_manager.allocator.retire(message_id, release_ownership=True)

    def release_solar_lease_identity(self, message_id: int) -> None:
        """Compatibility wrapper for the solar controller's APS identity."""
        self.release_aps_lease_identity(message_id)

    async def async_execute_solar_lease(
        self,
        message_id: int | None = None,
        *,
        stop: bool = False,
    ) -> dict[str, Any]:
        """Start, renew, or stop one bounded surplus-only APS lease."""
        if not self.production_control_enabled():
            raise HomeAssistantError("production_control_disabled")
        if stop:
            if message_id is None:
                raise HomeAssistantError("solar_lease_identity_required")
            request = ProductionAdapterRequest(
                ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING,
                {
                    "export_limit_w": 0,
                    "import_limit_w": 10_000,
                    "charge_max_w": 0,
                    "discharge_max_w": 0,
                    "keep_active": True,
                    "max_limit_change_w": 1_000,
                    "duration_seconds": 0,
                },
            )
        else:
            policy = self._automation_policy()
            if not self._automation_policy_valid():
                raise HomeAssistantError("automation_policy_not_authorized")
            charge_capacity_w = self.battery.charge_power_limit_w
            if charge_capacity_w is None:
                raise HomeAssistantError("solar_charge_capacity_unavailable")
            request = build_solar_lease_request(
                self.operational_settings,
                policy,
                charge_capacity_w=charge_capacity_w,
            )
        return await self._async_execute_owned_aps_lease(
            request,
            message_id=message_id,
            stop=stop,
            owner="automatic_solar",
        )

    async def async_execute_peak_shaving_lease(
        self,
        message_id: int | None = None,
        *,
        request: ProductionAdapterRequest | None = None,
        stop: bool = False,
    ) -> dict[str, Any]:
        """Start, renew, or stop one bounded two-way APS lease."""
        if not self.production_control_enabled():
            raise HomeAssistantError("production_control_disabled")
        if stop:
            if message_id is None:
                raise HomeAssistantError("aps_lease_identity_required")
            request = ProductionAdapterRequest(
                ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING,
                {
                    "export_limit_w": 0,
                    "import_limit_w": 0,
                    "charge_max_w": 0,
                    "discharge_max_w": 0,
                    "keep_active": False,
                    "max_limit_change_w": 1_000,
                    "duration_seconds": 0,
                },
            )
        elif request is None:
            policy = self._automation_policy()
            battery = self.battery
            if (
                not self._automation_policy_valid()
                or battery.usable_soc is None
                or battery.charge_power_limit_w is None
                or battery.discharge_power_limit_w is None
            ):
                raise HomeAssistantError("aps_battery_capacity_unavailable")
            request = build_aps_lease_request(
                self.operational_settings,
                policy,
                usable_soc=battery.usable_soc,
                charge_capacity_w=battery.charge_power_limit_w,
                discharge_capacity_w=battery.discharge_power_limit_w,
            )
        assert request is not None
        return await self._async_execute_owned_aps_lease(
            request,
            message_id=message_id,
            stop=stop,
            owner="adaptive_peak_shaving",
        )

    async def _async_execute_owned_aps_lease(
        self,
        request: ProductionAdapterRequest,
        *,
        message_id: int | None,
        stop: bool,
        owner: str,
    ) -> dict[str, Any]:
        """Execute one finite APS lease with exact identity and keyed readback."""
        if self.control_journal is not None and self.control_journal.unresolved_transactions:
            raise HomeAssistantError("control_recovery_required")
        transaction_id = f"{owner}-lease-{uuid4().hex}"
        if self.control_journal is not None:
            await self.begin_control_transaction(
                transaction_id=transaction_id,
                operation=(
                    f"native_{owner}_stop"
                    if stop
                    else f"native_{owner}_lease"
                ),
                target_identity=(JournalTargetIdentity(m_id=message_id) if message_id is not None
                                 else JournalTargetIdentity(asset_id=self.serial)),
                intent=request.payload,
            )
            await self.transition_control_transaction(transaction_id, JournalPhase.COMMAND_SENT)
        try:
            result = await self.production_adapter.execute(
                request,
                message_id=message_id,
                persist_message_id=message_id is None,
            )
        except Exception:
            if self.control_journal is not None:
                await self.transition_control_transaction(
                    transaction_id,
                    JournalPhase.RECOVERY_REQUIRED,
                    reason="adapter_exception",
                )
            raise
        actual_message_id = result.message_id
        readback: Any = None
        verified = False
        lifecycle = result.status.value
        if result.successful:
            if self.control_journal is not None:
                await self.transition_control_transaction(
                    transaction_id,
                    JournalPhase.ACKNOWLEDGED,
                    reason="finite_lease_acknowledged",
                )
            if stop:
                verified = True
            elif actual_message_id is not None:
                # Gateway applies updates within one second. A keyed GET is
                # unambiguous and proves both ownership and remaining lease.
                await asyncio.sleep(1)
                try:
                    readback = await self.async_verify_aps_lease(actual_message_id)
                except Exception:
                    readback = None
                verified = bool(readback and readback.get("successful") is True)
            if self.control_journal is not None:
                await self.transition_control_transaction(
                    transaction_id,
                    JournalPhase.RECONCILED,
                    reason=(
                        "readback_verified"
                        if verified
                        else "finite_lease_acknowledged_not_active"
                    ),
                )
            lifecycle = "reconciled" if verified else "acknowledged_not_active"
        elif result.status.value in {"delivery_unknown", "timed_out"}:
            lifecycle = "recovery_required"
            if self.control_journal is not None:
                await self.transition_control_transaction(
                    transaction_id,
                    JournalPhase.RECOVERY_REQUIRED,
                    reason=result.status.value,
                )
        elif self.control_journal is not None:
            await self.transition_control_transaction(
                transaction_id,
                JournalPhase.FAILED,
                reason=result.status.value,
            )
        output = result.as_dict()
        output.update(
            {
                "successful": verified,
                "command_acknowledged": result.successful,
                "lifecycle": lifecycle,
                "readback": readback,
                "duration_seconds": request.payload["duration_seconds"],
                "charge_max_w": request.payload["charge_max_w"],
                "discharge_max_w": request.payload["discharge_max_w"],
                "message_id": actual_message_id,
                "authority_revision": self.production_authority.revision,
            }
        )
        if stop and result.successful and actual_message_id is not None:
            self.release_aps_lease_identity(actual_message_id)
        return output

    async def begin_control_transaction(
        self,
        *,
        transaction_id: str,
        operation: str,
        target_identity: JournalTargetIdentity,
        intent: Mapping[str, Any] | None = None,
        command_state: Mapping[str, Any] | None = None,
        ownership_state: Mapping[str, Any] | None = None,
    ) -> None:
        """Durably record operation intent and optional identity state atomically."""
        async with self._control_journal_lock:
            allocator_generation = self._allocator_state_generation
            journal = self._journal_with_pending_allocator()
            if operation.startswith("native_") and journal.unresolved_transactions:
                raise HomeAssistantError("control_recovery_required")
            current_command_state = (
                self.command_manager.allocator.snapshot() if command_state is not None else None
            )
            candidate = journal.begin_transaction(
                transaction_id=transaction_id,
                operation=operation,
                target_identity=target_identity,
                intent=intent,
                command_state=current_command_state,
                ownership_state=ownership_state,
            )
            await self._save_control_journal_verified(candidate)
            self.control_journal = candidate
            if allocator_generation == self._allocator_state_generation:
                self._pending_allocator_state = None

    async def transition_control_transaction(
        self,
        transaction_id: str,
        phase: JournalPhase,
        *,
        reason: str | None = None,
    ) -> None:
        """Durably advance one transaction through an allowed phase transition."""
        async with self._control_journal_lock:
            allocator_generation = self._allocator_state_generation
            journal = self._journal_with_pending_allocator()
            candidate = journal.transition_transaction(
                transaction_id,
                phase,
                reason=reason,
            )
            await self._save_control_journal_verified(candidate)
            self.control_journal = candidate
            if allocator_generation == self._allocator_state_generation:
                self._pending_allocator_state = None

    async def find_recovery_transaction_id(
        self,
        *,
        operation: str,
        target_identity: JournalTargetIdentity,
    ) -> str | None:
        """Return one exact recovery transaction without changing durable state."""
        async with self._control_journal_lock:
            journal = self.control_journal
            if journal is None:
                return None
            matches = [
                item.transaction_id
                for item in journal.transactions
                if item.operation == operation
                and item.target_identity == target_identity
                and item.phase is JournalPhase.RECOVERY_REQUIRED
            ]
            return matches[0] if len(matches) == 1 else None

    async def reconcile_control_transaction(
        self,
        transaction_id: str,
        *,
        command_state: dict[str, Any],
        ownership_state: dict[str, Any],
        reason: str | None = None,
    ) -> None:
        """Atomically persist recovered identity state and its terminal phase."""
        async with self._control_journal_lock:
            journal = self.control_journal
            if journal is None:
                raise RuntimeError("control journal is unavailable")
            transaction = journal.transaction(transaction_id)
            if transaction is None or transaction.target_identity.m_id is None:
                raise RuntimeError("control recovery transaction has no allocator identity")
            target_m_id = transaction.target_identity.m_id
            desired_allocator = MessageIdAllocator.from_state(command_state)
            allocator = self.command_manager.allocator
            target_remains_owned = target_m_id in desired_allocator.owned_ids
            if target_remains_owned and target_m_id not in allocator.owned_ids:
                raise RuntimeError("control recovery allocator identity is unavailable")
            allocator_generation = self._allocator_state_generation
            current_command_state = allocator.snapshot()
            if not target_remains_owned:
                current_command_state["owned_m_ids"] = [
                    value for value in current_command_state["owned_m_ids"] if value != target_m_id
                ]
            candidate = journal.reconcile_transaction(
                transaction_id,
                command_state=current_command_state,
                ownership_state=ownership_state,
                reason=reason,
            )
            await self._save_control_journal_verified(candidate)
            self.control_journal = candidate
            if not target_remains_owned:
                allocator.active_ids.discard(target_m_id)
                allocator.owned_ids.discard(target_m_id)
            if allocator_generation == self._allocator_state_generation:
                self._pending_allocator_state = None
            elif not target_remains_owned:
                self._pending_allocator_state = allocator.snapshot()

    async def _persist_scheduler_ownership(self, state: dict[str, Any]) -> None:
        """Atomically persist allocator identity and ownership before live mutation."""
        async with self._control_journal_lock:
            if self.control_journal is None or self.control_store is None:
                raise RuntimeError("control journal is unavailable")
            allocator_generation = self._allocator_state_generation
            candidate = self.control_journal.with_states(
                command_state=self.command_manager.allocator.snapshot(),
                ownership_state=state,
            )
            await self._save_control_journal_verified(candidate)
            self.control_journal = candidate
            if allocator_generation == self._allocator_state_generation:
                self._pending_allocator_state = None

    async def persist_scheduler_ownership(self, state: dict[str, Any]) -> None:
        """Persist validation ownership transitions through the durable journal."""
        await self._persist_scheduler_ownership(state)

    def _journal_with_pending_allocator(self) -> ControlJournal:
        journal = self.control_journal
        if journal is None:
            raise RuntimeError("control journal is unavailable")
        if self._pending_allocator_state is None:
            return journal
        return journal.with_states(command_state=self._pending_allocator_state)

    def _persist_allocator_state(self, state: dict[str, Any]) -> None:
        """Coalesce allocator updates into serialized, verified journal commits."""
        if self.control_journal is None:
            raise RuntimeError("control journal is unavailable")
        if getattr(self.hass, "state", None) in {
            CoreState.stopping,
            CoreState.final_write,
            CoreState.stopped,
        }:
            raise RuntimeError("allocator persistence is disabled during shutdown")
        self._pending_allocator_state = {
            "next_m_id": state["next_m_id"],
            "owned_m_ids": list(state["owned_m_ids"]),
        }
        self._allocator_state_generation += 1
        task = self._allocator_flush_task
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(
                self._flush_pending_allocator_state(),
                name=f"{DOMAIN}-allocator-journal",
            )
            self._allocator_flush_task = task
            task.add_done_callback(self._allocator_flush_done)

    def _allocator_flush_done(self, task: asyncio.Task[None]) -> None:
        if self._allocator_flush_task is task:
            self._allocator_flush_task = None
        try:
            task.result()
        except Exception as err:  # noqa: BLE001 - surfaced by the next control operation
            self._allocator_flush_error = err
            self._fail_closed_control_state("control_journal_persistence_failed")

    async def _flush_pending_allocator_state(self) -> None:
        while True:
            async with self._control_journal_lock:
                state = self._pending_allocator_state
                if state is None:
                    return
                generation = self._allocator_state_generation
                journal = self.control_journal
                if journal is None:
                    raise RuntimeError("control journal is unavailable")
                candidate = journal.with_states(command_state=state)
                try:
                    await self._save_control_journal_verified(candidate)
                except Exception:
                    self._fail_closed_control_state("control_journal_persistence_failed")
                    raise
                self.control_journal = candidate
                if generation == self._allocator_state_generation:
                    self._pending_allocator_state = None
                    return

    async def async_flush_control_journal(self) -> None:
        """Drain coalesced allocator state before reload or shutdown."""
        task = self._allocator_flush_task
        if task is not None:
            await task
        elif self._pending_allocator_state is not None:
            await self._flush_pending_allocator_state()
        if self._allocator_flush_error is not None:
            raise RuntimeError(
                "control journal allocator flush failed"
            ) from self._allocator_flush_error

    def _build_allocator(self) -> MessageIdAllocator:
        return MessageIdAllocator()

    async def start(self) -> None:
        assert self.transport is not None
        await self.transport.start()
        await self.command_manager.start()

    async def stop(self) -> None:
        if self.battery_activity is not None:
            await self.battery_activity.stop()
        if self.energy_forecast is not None:
            await self.energy_forecast.stop()
        if self.price_optimization_controller is not None:
            await self.price_optimization_controller.soc_guard.stop()
            await self.price_optimization_controller.stop()
        if self.adaptive_peak_shaving_controller is not None:
            await self.adaptive_peak_shaving_controller.stop()
        if self.solar_charging_controller is not None:
            await self.solar_charging_controller.stop()
        if self.load_balancing_observer is not None:
            await self.load_balancing_observer.stop()
        if self.meter_limit_load_balancer is not None:
            await self.meter_limit_load_balancer.stop()
        if self.scheduler_coordinator is not None:
            await self.scheduler_coordinator.async_stop()
        await self.async_flush_control_journal()
        await self.command_manager.stop()
        if self.transport is not None:
            await self.transport.stop()

    def handle_message(self, message: ParsedMqttMessage) -> None:
        previous_service = self.telemetry.value("core", "srv_type")
        self.capabilities.observe(message)
        self.telemetry.apply(message)
        price = self.price_optimization_controller
        if price is not None and (
            previous_service != self.telemetry.value("core", "srv_type")
            or (price.status == "waiting_telemetry" and self.plant_health().healthy)
        ):
            price.request_refresh()

    def plant_health(self) -> PlantHealth:
        """Return technical plant health without treating active service as a fault."""
        core = self.telemetry.payload("core")
        battery = self.telemetry.payload("battery")
        reasons: list[str] = []

        core_fresh = self.telemetry.is_fresh("core")
        battery_fresh = self.telemetry.is_fresh("battery")
        telemetry_fresh = core_fresh and battery_fresh
        if not core_fresh:
            reasons.append("core_telemetry_stale")
        if not battery_fresh:
            reasons.append("battery_telemetry_stale")

        def status_value(payload: dict[str, object], field_name: str) -> int | None:
            value = payload.get(field_name)
            if value is None:
                reasons.append(f"{field_name}_missing")
                return None
            if isinstance(value, bool) or not isinstance(value, int):
                reasons.append(f"{field_name}_malformed")
                return None
            if value == 0:
                reasons.append(f"{field_name}_undefined")
            elif value not in STATUS_LABELS:
                reasons.append(f"{field_name}_unknown")
            elif value != 1:
                reasons.append(f"{field_name}_unsafe")
            return value

        system_status = status_value(core, "sys_status")
        battery_status = status_value(battery, "batt_status")
        grid_status = status_value(core, "grid_status")
        system_events = core.get("sys_events")
        system_events_clear = False
        if system_events is None:
            reasons.append("sys_events_missing")
        elif (
            isinstance(system_events, bool)
            or not isinstance(system_events, int)
            or system_events < 0
        ):
            reasons.append("sys_events_malformed")
        elif system_events != 0:
            reasons.append("sys_events_unsafe")
        else:
            system_events_clear = True

        return PlantHealth(
            telemetry_fresh=telemetry_fresh,
            system_safe=system_status == 1,
            battery_safe=battery_status == 1,
            grid_safe=grid_status == 1,
            system_events_clear=system_events_clear,
            reasons=tuple(reasons),
            system_status=system_status,
            battery_status=battery_status,
            grid_status=grid_status,
            system_events=system_events,
        )

    def operation_impact(self) -> OperationImpact:
        """Return active-service consequences independently from plant health."""
        service = self.telemetry.payload("core").get("srv_type")
        reasons: list[str] = []
        if service is None:
            reasons.append("service_type_missing")
            service_type = None
        elif isinstance(service, bool) or not isinstance(service, int):
            reasons.append("service_type_malformed")
            service_type = None
        elif service not in SERVICE_TYPE_LABELS:
            reasons.append("service_type_unknown")
            service_type = service
        else:
            service_type = service

        immediate_effect_known = service_type in {0, 102, 105, 107}
        protected_service_active = service_type not in {None, 0, 102, 105, 107}
        return OperationImpact(
            active_service=service_type,
            protected_service_active=protected_service_active,
            immediate_effect_known=immediate_effect_known,
            exact_target=False,
            reasons=tuple(reasons),
        )

    def scheduler_control_context(
        self,
        operation: ControlOperation,
        *,
        validation_authorized: bool = False,
    ) -> ControlPolicyInput:
        """Return the canonical fail-closed context for a scheduler operation."""
        manager = self.scheduler_manager
        ownership = self.scheduler_ownership
        journal_recovery_pending = self.control_journal is not None and bool(
            self.control_journal.unresolved_transactions
        )
        ownership_store_valid = (
            ownership is not None
            and self.scheduler_mutation_config_error is None
            and self.control_state_error is None
            and not journal_recovery_pending
        )
        ownership_ambiguous = (
            self.scheduler_mutation_config_error is not None
            or self.control_state_error is not None
            or journal_recovery_pending
            or (ownership is not None and ownership.has_unresolved_records())
        )
        return ControlPolicyInput(
            operation=operation,
            runtime_mode=RuntimeMode.PRODUCTION,
            operator_mode=OperatorMode.LOCAL_CONTROL,
            external_control=ExternalControlState.UNKNOWN,
            origin=ScheduleOrigin.LOCAL_CREATED,
            initiative=Initiative.MANUAL,
            release_state=(
                ReleaseState.LIVE_RELEASE_OPEN
                if self.production_control_enabled()
                else ReleaseState.LIVE_RELEASE_CLOSED
            ),
            plant_health=self.plant_health(),
            operation_impact=self.operation_impact(),
            scheduler_visibility_fresh=(manager is not None and manager.list_visibility_fresh()),
            owned_background_aps=any(
                controller is not None and controller.owns_live_lease()
                for controller in (self.solar_charging_controller,
                                   self.adaptive_peak_shaving_controller)
            ),
            ownership_store_valid=ownership_store_valid,
            ownership_ambiguous=ownership_ambiguous,
            mqtt_connected=self.command_manager.ready,
            pending_operations=self.command_manager.pending_count > 0,
            validation_authorized=validation_authorized,
        )

    async def async_run_scheduler_validation_roundtrip(self) -> dict[str, object]:
        """Run the explicit one-shot validation transaction when requested."""
        result = await SchedulerValidationRoundtrip(self).run()
        self.scheduler_validation_last_result = result
        return result

    def scheduler_mutation_diagnostics(self) -> dict[str, object]:
        """Return safe aggregate scheduler mutation wiring diagnostics."""
        manager = self.scheduler_mutation_manager
        ownership = self.scheduler_ownership
        lifecycle_counts = {lifecycle.value: 0 for lifecycle in OwnershipLifecycle}
        if ownership is not None:
            lifecycle_counts = ownership.lifecycle_counts()
        if self.scheduler_mutation_config_error is not None:
            status = self.scheduler_mutation_config_error
        elif manager is not None:
            status = "wired_disabled"
        else:
            status = "unavailable"
        transaction_diagnostics: dict[str, object] | None = None
        if manager is not None:
            transaction_diagnostics = manager.diagnostics_snapshot()
            transaction_diagnostics["last_result"] = None
        return {
            "status": status,
            "live_writes_enabled": self.release_gate.live_writes_enabled,
            "manager_constructed": manager is not None,
            "executor": (
                "command_manager_shared_ack" if self.scheduler_executor is not None else None
            ),
            "ownership_counts": lifecycle_counts,
            "transaction_manager": transaction_diagnostics,
            "list_visibility": (
                self.scheduler_manager.list_visibility_diagnostics()
                if self.scheduler_manager is not None
                else None
            ),
            "validation": {
                "running": self.scheduler_validation_running,
                "attempt_consumed": self.scheduler_validation_attempt_consumed,
                "last_result": self.scheduler_validation_last_result,
            },
            "production_adapter_wired": manager is not None and self.scheduler_executor is not None,
        }

    @property
    def battery(self) -> BatteryReading:
        return self.battery_model.reading(
            self.telemetry.payload("battery"), self.telemetry.payload("energy")
        )


def _lowered_targets_from(value: object) -> dict[tuple[int, int, int, int], float]:
    """Saved ``[sch_index, service_no, start, duration, reference]`` rows."""
    lowered: dict[tuple[int, int, int, int], float] = {}
    if not isinstance(value, list):
        return lowered
    now = time()
    for row in value:
        if not (
            isinstance(row, list | tuple) and len(row) == 5
            and all(isinstance(part, int) and not isinstance(part, bool) for part in row[:4])
            and isinstance(row[4], int | float) and not isinstance(row[4], bool)
        ):
            continue
        if row[2] + row[3] <= now:
            continue  # that schedule has ended
        lowered[(row[0], row[1], row[2], row[3])] = float(row[4])
    return lowered
