"""Offline-safe Pixii command and acknowledgement infrastructure."""

from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from time import monotonic
from typing import Any, Protocol

from homeassistant.components.mqtt.client import async_publish, async_subscribe
from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.components.mqtt.util import async_wait_for_mqtt_client
from homeassistant.core import HomeAssistant, callback

from .allocator import MessageIdAllocator, normalize_message_id
from .capabilities import CapabilityRegistry
from .const import ACK_TOPIC_TEMPLATE, CONTROL_TOPIC_TEMPLATE
from .control.models import (
    AdapterCapability,
    ControlOperation,
    ControlPolicyInput,
    OperationImpact,
    OperatorMode,
    PlantHealth,
    ReleaseState,
    RuntimeMode,
    ScheduleOrigin,
)
from .control.policy import evaluate_control_operation
from .exceptions import CommandValidationError, MqttParseError
from .protocol.commands import CommandMethod, PixiiCommand
from .protocol.models import MqttAck, ParsedMqttMessage
from .protocol.mqtt import parse_ack, parse_message


class CommandStatus(StrEnum):
    """Terminal command result states."""

    SUCCESS = "success"
    REJECTED = "rejected"
    FAILED = "failed"
    DELIVERY_UNKNOWN = "delivery_unknown"
    TIMED_OUT = "timed_out"


class CommandErrorCode(StrEnum):
    """Sanitized command result and observability error codes."""

    GATE_BLOCKED = "gate_blocked"
    MANAGER_STOPPED = "manager_stopped"
    PUBLISH_FAILED = "publish_failed"
    PUBLISH_OUTCOME_UNKNOWN = "publish_outcome_unknown"
    TIMEOUT = "timeout"
    GATEWAY_RESPONSE = "gateway_response"
    AMBIGUOUS_CORRELATION = "ambiguous_correlation"
    MALFORMED_ACK = "malformed_ack"
    WRONG_SERVICE = "wrong_service"
    WRONG_METHOD = "wrong_method"
    FOREIGN_M_ID = "foreign_m_id"
    DUPLICATE_ACK = "duplicate_ack"
    LATE_ACK = "late_ack"




@dataclass(frozen=True, slots=True)
class CommandError:
    """A structured error that never contains raw MQTT payloads."""

    code: CommandErrorCode
    message: str
    details: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": list(self.details),
        }


@dataclass(frozen=True, slots=True)
class AckSummary:
    """Safe ACK fields retained for result diagnostics."""

    method: str
    service: str
    response: int
    message_id: int | None
    source_id: int | None
    service_status: int | None
    time_left: int | None

    @classmethod
    def from_ack(cls, ack: MqttAck, message_id: int | None) -> AckSummary:
        return cls(
            method=ack.method,
            service=ack.service,
            response=ack.response,
            message_id=message_id,
            source_id=ack.source_id,
            service_status=ack.service_status,
            time_left=ack.time_left,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "service": self.service,
            "response": self.response,
            "message_id": self.message_id,
            "source_id": self.source_id,
            "service_status": self.service_status,
            "time_left": self.time_left,
        }


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Structured result returned for every command attempt."""

    command_id: str
    method: str
    service: str
    status: CommandStatus
    message_id: int | None = None
    response: int | None = None
    ack: AckSummary | None = None
    error: CommandError | None = None

    @property
    def successful(self) -> bool:
        return self.status is CommandStatus.SUCCESS

    def as_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "method": self.method,
            "service": self.service,
            "status": self.status.value,
            "message_id": self.message_id,
            "response": self.response,
            "ack": self.ack.as_dict() if self.ack else None,
            "error": self.error.as_dict() if self.error else None,
        }


@dataclass(frozen=True, slots=True)
class CorrelatedCommandRequest:
    """Neutral pre-correlated MQTT command request for injected adapters."""

    method: CommandMethod
    message_id: int
    service: str | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    expected_services: tuple[str, ...] = ()
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not isinstance(self.method, CommandMethod):
            raise CommandValidationError("correlated command method is invalid")
        object.__setattr__(self, "message_id", normalize_message_id(self.message_id))
        service = self.service.strip().lower() if self.service is not None else None
        if self.method is CommandMethod.SET and not service:
            raise CommandValidationError("correlated set requires a service")
        if self.method is not CommandMethod.SET and service is not None and (
            self.expected_services != (service,)
        ):
            raise CommandValidationError("correlated service must match its exact expected service")
        if not isinstance(self.fields, Mapping):
            raise CommandValidationError("correlated command fields must be an object")
        if self.timeout_seconds <= 0 or not isfinite(self.timeout_seconds):
            raise CommandValidationError("correlated command timeout must be finite and positive")
        expected_services = tuple(expected.strip().lower() for expected in self.expected_services)
        if any(not expected for expected in expected_services):
            raise CommandValidationError("correlated expected service must be non-empty")
        object.__setattr__(self, "service", service)
        object.__setattr__(self, "fields", dict(self.fields))
        object.__setattr__(self, "expected_services", expected_services)

    @property
    def state_changing(self) -> bool:
        """Return whether this request changes Gateway state."""
        return self.method is not CommandMethod.GET

    def to_payload(self) -> dict[str, Any]:
        """Return the canonical control payload without transport side effects."""
        payload: dict[str, Any] = {
            "method": self.method.value,
            "m_id": self.message_id,
        }
        if self.service is not None:
            payload["service"] = self.service
        payload.update(dict(self.fields))
        return payload


class SchedulerValidationAuthorization(Protocol):
    """Typed authorization boundary for one narrow scheduler validation."""

    def authorize(self, request: CorrelatedCommandRequest, serial: str) -> None:
        """Validate one exact request before any MQTT publish."""


@dataclass(frozen=True, slots=True)
class CorrelatedCommandResult:
    """Typed result returned by a pre-correlated MQTT command request."""

    status: CommandStatus
    message_id: int
    ack: MqttAck | None = None
    error: CommandError | None = None


class WriteGate:
    """Runtime production gate shared by every state-changing MQTT command."""

    def __init__(self, authority_enabled: Callable[[], bool] | None = None) -> None:
        self._authority_enabled = authority_enabled or (lambda: False)

    @property
    def global_enabled(self) -> bool:
        return bool(self._authority_enabled())

    def blocked_reasons(
        self,
        command: PixiiCommand | None,
        plant_health: PlantHealth,
        operation_impact: OperationImpact,
        *,
        manager_ready: bool,
        capability_supported: bool,
        pending: bool,
    ) -> tuple[str, ...]:
        if command is not None and not command.state_changing:
            reasons: list[str] = []
            if not capability_supported:
                reasons.append("read_only_command_unsupported")
            if not manager_ready:
                reasons.append("ack_manager_not_ready")
            if pending:
                reasons.append("unresolved_previous_get")
            return tuple(dict.fromkeys(reasons))

        operation = ControlOperation.DIRECT_CONTROL
        if command is not None and command.service == "targetsoc":
            operation = ControlOperation.DIRECT_TARGET_SOC
        elif command is not None and command.service == "demandresponse":
            operation = ControlOperation.DIRECT_DEMAND_RESPONSE
        elif command is not None and command.service == "adaptivepeakshaving":
            operation = ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING
        elif command is not None and command.service == "batterycalibration":
            operation = ControlOperation.DIRECT_BATTERY_CALIBRATION
        decision = evaluate_control_operation(
            ControlPolicyInput(
                operation=operation,
                requested_service=command.service if command is not None else None,
                runtime_mode=RuntimeMode.PRODUCTION,
                operator_mode=OperatorMode.LOCAL_CONTROL,
                origin=ScheduleOrigin.LOCAL_CREATED,
                adapter_capability=(
                    AdapterCapability.SUPPORTED
                    if capability_supported
                    else AdapterCapability.UNSUPPORTED
                ),
                release_state=(
                    ReleaseState.LIVE_RELEASE_OPEN
                    if self.global_enabled
                    else ReleaseState.LIVE_RELEASE_CLOSED
                ),
                plant_health=plant_health,
                operation_impact=operation_impact,
                mqtt_connected=manager_ready,
                pending_operations=pending,
            )
        )
        reasons = list(decision.blocked_reasons)
        if not self.global_enabled:
            reasons.append("production_control_disabled")
        return tuple(dict.fromkeys(reasons))


@dataclass(slots=True)
class _PendingCommand:
    command: PixiiCommand
    command_id: str
    message_id: int | None
    future: asyncio.Future[CommandResult]


@dataclass(slots=True)
class _PendingCorrelated:
    request: CorrelatedCommandRequest
    future: asyncio.Future[CorrelatedCommandResult]


@dataclass(frozen=True, slots=True)
class _TerminalAck:
    reason: str
    expires_at: float


class CommandManager:
    """Manage one Gateway's ACK subscription and serialized commands."""

    def __init__(
        self,
        hass: HomeAssistant,
        serial: str,
        capabilities: CapabilityRegistry,
        plant_health: Callable[[], PlantHealth],
        *,
        operation_impact: Callable[[], OperationImpact] | None = None,
        allocator: MessageIdAllocator | None = None,
        on_ack_observed: Callable[[ParsedMqttMessage], None] | None = None,
        production_control_enabled: Callable[[], bool] | None = None,
        state_changing_lock: asyncio.Lock | None = None,
        terminal_history_limit: int = 256,
        terminal_history_ttl_seconds: float = 300.0,
    ) -> None:
        if terminal_history_limit < 1:
            raise ValueError("terminal history limit must be positive")
        if terminal_history_ttl_seconds <= 0 or not isfinite(terminal_history_ttl_seconds):
            raise ValueError("terminal history TTL must be finite and positive")
        self.hass = hass
        self.serial = serial
        self.capabilities = capabilities
        self._plant_health = plant_health
        self._operation_impact = operation_impact or (
            lambda: OperationImpact(
                active_service=0,
                immediate_effect_known=True,
                exact_target=True,
            )
        )
        self.allocator = allocator or MessageIdAllocator()
        self._state_changing_lock = state_changing_lock
        self.gate = WriteGate(production_control_enabled)
        self._external_get_ack_count = 0
        self._pending_scheduler_mutations: dict[int, _PendingCorrelated] = {}
        self._pending_scheduler_gets: dict[int, _PendingCorrelated] = {}
        self._on_ack_observed = on_ack_observed
        self._unsubscribe: Callable[[], None] | None = None
        self._ready = False
        self._state_lock = asyncio.Lock()
        self._pending: dict[int, _PendingCommand] = {}
        self._pending_get: _PendingCommand | None = None
        self._terminal_ids: OrderedDict[int, _TerminalAck] = OrderedDict()
        self._terminal_history_limit = terminal_history_limit
        self._terminal_history_ttl_seconds = terminal_history_ttl_seconds
        self._command_sequence = 0
        self._read_only_publish_attempts = 0
        self._state_changing_publish_attempts = 0
        self._successful_publish_returns = 0
        self._successful_read_only_publish_returns = 0
        self._successful_state_changing_publish_returns = 0
        self._ambiguous_get_result_count = 0
        self._malformed_ack_count = 0
        self._unidentified_response_11_count = 0
        self._last_response_11_missing_fields: tuple[str, ...] = ()
        self._duplicate_ack_count = 0
        self._late_ack_count = 0
        self._ambiguous_ack_count = 0
        self._foreign_ack_count = 0
        self._wrong_service_count = 0
        self._scheduler_mutation_attempts = 0
        self._scheduler_keyed_get_attempts = 0
        self._scheduler_matched_acks = 0
        self._scheduler_get_matches = 0
        self._scheduler_transport_timeouts = 0
        self._wrong_method_count = 0
        self._last_result: CommandResult | None = None
        self._last_error: CommandError | None = None
        self._last_timeout_reason: str | None = None
        self._last_ambiguity_reason: str | None = None

    @property
    def ready(self) -> bool:
        """Return whether the ACK subscription is active."""
        return self._ready

    @property
    def pending_count(self) -> int:
        """Return the number of unresolved command requests."""
        return (
            len(self._pending)
            + (1 if self._pending_get is not None else 0)
            + len(self._pending_scheduler_mutations)
            + len(self._pending_scheduler_gets)
        )

    async def start(self) -> None:
        """Subscribe to this entry's ACK topic through Home Assistant MQTT."""
        if self._ready:
            return
        if not await async_wait_for_mqtt_client(self.hass):
            raise RuntimeError("Home Assistant MQTT is not connected")
        topic = ACK_TOPIC_TEMPLATE.format(serial=self.serial)
        self._unsubscribe = await async_subscribe(
            self.hass,
            topic,
            self._ack_received,
            qos=0,
            encoding=None,
        )
        self._ready = True

    async def stop(self) -> None:
        """Unsubscribe and resolve pending commands without publishing anything."""
        self._ready = False
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        error = CommandError(CommandErrorCode.MANAGER_STOPPED, "command manager stopped")
        for pending in tuple(self._pending.values()):
            self._resolve_pending(pending, self._result_for_error(pending, error))
        for pending in tuple(self._pending_scheduler_mutations.values()):
            self._resolve_correlated_pending(pending, error)
        for pending in tuple(self._pending_scheduler_gets.values()):
            self._resolve_correlated_pending(pending, error)
        if self._pending_get is not None:
            pending = self._pending_get
            self._pending_get = None
            if not pending.future.done():
                pending.future.set_result(self._result_for_error(pending, error))

    async def execute(self, command: PixiiCommand) -> CommandResult:
        """Validate the gate, then publish one command and await one ACK."""
        if command.state_changing:
            async with self._state_lock:
                return await self._execute_locked(command)
        return await self._execute_locked(command)

    async def execute_correlated(
        self,
        request: CorrelatedCommandRequest,
    ) -> CorrelatedCommandResult:
        """Publish one pre-correlated request through the existing ACK stream."""
        if request.state_changing:
            async with self._state_lock:
                return await self._execute_correlated_locked(request)
        return await self._execute_correlated_locked(request)

    async def execute_scheduler_validation_correlated(
        self,
        request: CorrelatedCommandRequest,
        authorization: SchedulerValidationAuthorization,
    ) -> CorrelatedCommandResult:
        """Run one typed validation request through the existing ACK stream."""
        async with self._state_lock:
            try:
                authorization.authorize(request, self.serial)
            except CommandValidationError:
                return self._correlated_error(
                    request,
                    CommandStatus.REJECTED,
                    CommandError(
                        CommandErrorCode.GATE_BLOCKED,
                        "scheduler validation authorization rejected",
                        ("scheduler_validation_permit_invalid",),
                    ),
                )
            return await self._execute_correlated_locked(request)

    async def _execute_correlated_locked(
        self,
        request: CorrelatedCommandRequest,
    ) -> CorrelatedCommandResult:
        if request.state_changing:
            self._scheduler_mutation_attempts += 1
            if not self.gate.global_enabled:
                return self._correlated_error(
                    request,
                    CommandStatus.REJECTED,
                    CommandError(
                        CommandErrorCode.GATE_BLOCKED,
                        "command blocked by production control authority",
                        ("release_gate_closed", "production_control_disabled"),
                    ),
                )
        else:
            self._scheduler_keyed_get_attempts += 1
        if not self._ready:
            return self._correlated_error(
                request,
                CommandStatus.REJECTED,
                CommandError(CommandErrorCode.MANAGER_STOPPED, "command manager is not ready"),
            )
        if (
            request.message_id in self._pending
            or request.message_id in self._pending_scheduler_mutations
            or request.message_id in self._pending_scheduler_gets
        ):
            return self._correlated_error(
                request,
                CommandStatus.REJECTED,
                CommandError(
                    CommandErrorCode.AMBIGUOUS_CORRELATION,
                    "the requested m_id already has a pending command",
                ),
            )

        future: asyncio.Future[CorrelatedCommandResult] = asyncio.get_running_loop().create_future()
        pending = _PendingCorrelated(request, future)
        pending_registry = (
            self._pending_scheduler_mutations
            if request.state_changing
            else self._pending_scheduler_gets
        )
        pending_registry[request.message_id] = pending
        publish_started = False
        try:
            publish_lock = self._state_changing_lock if request.state_changing else None
            if publish_lock is not None:
                await publish_lock.acquire()
            try:
                # Recheck authority while serialized with authority persistence and publish.
                if request.state_changing and not self.gate.global_enabled:
                    self._remove_correlated_pending(pending)
                    return self._correlated_error(
                        request,
                        CommandStatus.REJECTED,
                        CommandError(
                            CommandErrorCode.GATE_BLOCKED,
                            "command blocked by production control authority",
                            ("release_gate_closed", "production_control_disabled"),
                        ),
                    )
                publish_started = True
                if request.state_changing:
                    self._state_changing_publish_attempts += 1
                else:
                    self._read_only_publish_attempts += 1
                await async_publish(
                    self.hass,
                    CONTROL_TOPIC_TEMPLATE.format(serial=self.serial),
                    json.dumps(request.to_payload(), separators=(",", ":"), sort_keys=True),
                    qos=0,
                    retain=False,
                )
                self._successful_publish_returns += 1
                if request.state_changing:
                    self._successful_state_changing_publish_returns += 1
                else:
                    self._successful_read_only_publish_returns += 1
            finally:
                if publish_lock is not None:
                    publish_lock.release()
        except asyncio.CancelledError:
            self._remove_correlated_pending(pending)
            if publish_started:
                self._remember_terminal(
                    request.message_id,
                    CommandErrorCode.PUBLISH_OUTCOME_UNKNOWN.value,
                )
            raise
        except Exception:
            self._remove_correlated_pending(pending)
            self._remember_terminal(
                request.message_id,
                CommandErrorCode.PUBLISH_OUTCOME_UNKNOWN.value,
            )
            if request.state_changing:
                result = self._correlated_error(
                    request,
                    CommandStatus.DELIVERY_UNKNOWN,
                    CommandError(
                        CommandErrorCode.PUBLISH_OUTCOME_UNKNOWN,
                        "state-changing publish outcome is unknown; automatic retry is disabled",
                    ),
                )
            else:
                result = self._correlated_error(
                    request,
                    CommandStatus.FAILED,
                    CommandError(CommandErrorCode.PUBLISH_FAILED, "MQTT publish failed"),
                )
            self._last_error = result.error
            return result

        try:
            return await asyncio.wait_for(asyncio.shield(future), request.timeout_seconds)
        except asyncio.CancelledError:
            self._remove_correlated_pending(pending)
            self._remember_terminal(
                request.message_id,
                CommandErrorCode.PUBLISH_OUTCOME_UNKNOWN.value,
            )
            raise
        except TimeoutError:
            self._remove_correlated_pending(pending)
            self._remember_terminal(request.message_id, CommandErrorCode.TIMEOUT.value)
            self._scheduler_transport_timeouts += 1
            self._last_timeout_reason = "no exact correlated ACK before command timeout"
            return self._correlated_error(
                request,
                CommandStatus.TIMED_OUT,
                CommandError(CommandErrorCode.TIMEOUT, self._last_timeout_reason),
            )

    @staticmethod
    def _correlated_error(
        request: CorrelatedCommandRequest,
        status: CommandStatus,
        error: CommandError,
    ) -> CorrelatedCommandResult:
        return CorrelatedCommandResult(status, request.message_id, error=error)

    async def _execute_locked(self, command: PixiiCommand) -> CommandResult:
        self._command_sequence += 1
        command_id = f"command-{self._command_sequence}"
        plant_health = self._plant_health()
        operation_impact = self._operation_impact()
        required_families = ("core", "battery") if command.state_changing else ()
        capability = self.capabilities.command_capability(
            command.service,
            command.method.value,
            required_families=required_families,
        )
        pending_for_gate = self.pending_count > 0 if command.state_changing else False
        blocked = self.gate.blocked_reasons(
            command,
            plant_health,
            operation_impact,
            manager_ready=self._ready,
            capability_supported=capability.permitted,
            pending=pending_for_gate,
        )
        if blocked:
            result = CommandResult(
                command_id=command_id,
                method=command.method.value,
                service=command.service,
                status=CommandStatus.REJECTED,
                error=CommandError(
                    CommandErrorCode.GATE_BLOCKED,
                    "command blocked by the write gate",
                    blocked,
                ),
            )
            self._remember_result(result)
            return result

        message_id: int | None = None
        if command.state_changing:
            if command.message_id is None:
                message_id = self.allocator.allocate(
                    persistent=command.persist_message_id
                )
            else:
                message_id = normalize_message_id(command.message_id)
                if (
                    message_id not in self.allocator.owned_ids
                    or message_id in self.allocator.active_ids
                ):
                    result = CommandResult(
                        command_id=command_id,
                        method=command.method.value,
                        service=command.service,
                        status=CommandStatus.REJECTED,
                        message_id=message_id,
                        error=CommandError(
                            CommandErrorCode.FOREIGN_M_ID,
                            "state-changing command m_id is not an available "
                            "integration-owned identity",
                        ),
                    )
                    self._remember_result(result)
                    return result
                self.allocator.active_ids.add(message_id)
        owned_command = (
            command.with_message_id(message_id)
            if message_id is not None and command.message_id is None
            else command
        )
        payload = owned_command.to_payload()
        future: asyncio.Future[CommandResult] = asyncio.get_running_loop().create_future()
        pending = _PendingCommand(owned_command, command_id, message_id, future)
        if message_id is None:
            if self._pending_get is not None:
                result = self._result_for_error(
                    pending,
                    CommandError(
                        CommandErrorCode.AMBIGUOUS_CORRELATION,
                        "only one uncorrelatable GET may be pending",
                    ),
                )
                self._remember_result(result)
                return result
            self._pending_get = pending
        else:
            self._pending[message_id] = pending

        publish_started = False
        try:
            # This yield gives cancellation a safe pre-publish phase.
            await asyncio.sleep(0)
            publish_lock = self._state_changing_lock if command.state_changing else None
            if publish_lock is not None:
                await publish_lock.acquire()
            try:
                # Recheck authority while serialized with authority persistence and publish.
                if command.state_changing and not self.gate.global_enabled:
                    self._remove_pending(pending)
                    result = self._result_for_error(
                        pending,
                        CommandError(
                            CommandErrorCode.GATE_BLOCKED,
                            "command blocked by the write gate",
                            ("release_gate_closed", "production_control_disabled"),
                        ),
                    )
                    self._remember_result(result)
                    return result
                publish_started = True
                if command.state_changing:
                    self._state_changing_publish_attempts += 1
                else:
                    self._read_only_publish_attempts += 1
                await async_publish(
                    self.hass,
                    CONTROL_TOPIC_TEMPLATE.format(serial=self.serial),
                    json.dumps(payload, separators=(",", ":"), sort_keys=True),
                    qos=0,
                    retain=False,
                )
                self._successful_publish_returns += 1
                if command.state_changing:
                    self._successful_state_changing_publish_returns += 1
                else:
                    self._successful_read_only_publish_returns += 1
            finally:
                if publish_lock is not None:
                    publish_lock.release()
        except asyncio.CancelledError:
            self._remove_pending(pending)
            if message_id is not None and publish_started:
                self._remember_terminal(
                    message_id,
                    CommandErrorCode.PUBLISH_OUTCOME_UNKNOWN.value,
                )
            raise
        except Exception:  # noqa: BLE001 - transport errors are structured results
            self._remove_pending(pending)
            if command.state_changing:
                error = CommandError(
                    CommandErrorCode.PUBLISH_OUTCOME_UNKNOWN,
                    "state-changing publish outcome is unknown; automatic retry is disabled",
                )
                self._remember_terminal(
                    message_id,
                    CommandErrorCode.PUBLISH_OUTCOME_UNKNOWN.value,
                )
                result = self._result_for_error(
                    pending,
                    error,
                    status=CommandStatus.DELIVERY_UNKNOWN,
                )
            else:
                result = self._result_for_error(
                    pending,
                    CommandError(CommandErrorCode.PUBLISH_FAILED, "MQTT publish failed"),
                )
            self._last_error = result.error
            self._remember_result(result)
            return result

        try:
            result = await asyncio.wait_for(asyncio.shield(future), command.timeout_seconds)
        except asyncio.CancelledError:
            self._remove_pending(pending)
            if message_id is not None:
                self._remember_terminal(
                    message_id,
                    CommandErrorCode.PUBLISH_OUTCOME_UNKNOWN.value,
                )
            raise
        except TimeoutError:
            self._remove_pending(pending)
            if message_id is not None:
                self._remember_terminal(message_id, CommandErrorCode.TIMEOUT.value)
            self._last_timeout_reason = "no unambiguous ACK before command timeout"
            result = self._result_for_error(
                pending,
                CommandError(CommandErrorCode.TIMEOUT, self._last_timeout_reason),
                status=CommandStatus.TIMED_OUT,
            )
            self._remember_result(result)
            return result
        self._remember_result(result)
        return result

    @callback
    def _ack_received(self, message: ReceiveMessage) -> None:
        payload = message.payload
        try:
            parsed = parse_message(message.topic, payload)
        except MqttParseError as err:
            self._malformed_ack_count += 1
            self._last_error = CommandError(CommandErrorCode.MALFORMED_ACK, str(err))
            return
        self.handle_ack_message(parsed)

    @callback
    def handle_ack_message(self, message: ParsedMqttMessage) -> None:
        """Process one parsed ACK without retaining its raw payload."""
        if message.serial != self.serial or message.family != "ack":
            return
        self._observe_unidentified_response_11(message.payload)
        try:
            ack = parse_ack(message.payload)
        except MqttParseError as err:
            self._malformed_ack_count += 1
            self._last_error = CommandError(CommandErrorCode.MALFORMED_ACK, str(err))
            return
        if self._on_ack_observed is not None:
            self._on_ack_observed(message)
        if ack.method.lower() == CommandMethod.GET.value:
            self._handle_get_ack(ack)
            return
        self._handle_state_ack(ack)

    def _observe_unidentified_response_11(self, payload: Any) -> None:
        """Describe firmware's incomplete error frame without correlating it.

        Response 11 on a shared ACK topic does not identify a request. Keep
        only a counter and fixed field names; never copy raw values or attach
        this observation to a pending command, even when only one is pending.
        """
        if not isinstance(payload, Mapping):
            return
        values = payload.get("values", {})
        if not isinstance(values, Mapping):
            return

        def value(key: str) -> Any:
            return payload[key] if key in payload else values.get(key)

        response = value("response")
        if type(response) is not int or response != 11:
            return
        missing = tuple(
            key for key in ("method", "service", "m_id")
            if value(key) is None or value(key) == ""
        )
        if missing:
            self._unidentified_response_11_count += 1
            self._last_response_11_missing_fields = missing

    def _prune_terminal_ids(self) -> None:
        now = monotonic()
        while self._terminal_ids:
            _, entry = next(iter(self._terminal_ids.items()))
            if entry.expires_at > now:
                break
            self._terminal_ids.popitem(last=False)

    def _remember_terminal(self, message_id: int | None, reason: str) -> None:
        if message_id is None:
            return
        self._prune_terminal_ids()
        self._terminal_ids.pop(message_id, None)
        self._terminal_ids[message_id] = _TerminalAck(
            reason=reason,
            expires_at=monotonic() + self._terminal_history_ttl_seconds,
        )
        while len(self._terminal_ids) > self._terminal_history_limit:
            self._terminal_ids.popitem(last=False)

    def _terminal_reason(self, message_id: int) -> str | None:
        self._prune_terminal_ids()
        entry = self._terminal_ids.get(message_id)
        if entry is None:
            return None
        self._terminal_ids.move_to_end(message_id)
        return entry.reason

    def _handle_scheduler_get_ack(self, ack: MqttAck) -> bool:
        if ack.m_id is None:
            return False
        try:
            message_id = normalize_message_id(ack.m_id)
        except CommandValidationError:
            return False
        pending = self._pending_scheduler_gets.get(message_id)
        if pending is None:
            return False
        if ack.method.strip().lower() != CommandMethod.GET.value:
            self._wrong_method_count += 1
            return False
        service = ack.service.strip().lower()
        documented_absence = ack.response == 11 and service == "unknown"
        if (
            pending.request.expected_services
            and service not in pending.request.expected_services
            and not documented_absence
        ):
            self._wrong_service_count += 1
            return False
        self._remove_correlated_pending(pending)
        self._remember_terminal(message_id, "completed")
        self._scheduler_matched_acks += 1
        self._scheduler_get_matches += 1
        if ack.response == 65535:
            result = CorrelatedCommandResult(CommandStatus.SUCCESS, message_id, ack=ack)
        else:
            result = CorrelatedCommandResult(
                CommandStatus.FAILED,
                message_id,
                ack=ack,
                error=CommandError(
                    CommandErrorCode.GATEWAY_RESPONSE,
                    "Gateway rejected the command",
                    (f"response={ack.response}",),
                ),
            )
        if not pending.future.done():
            pending.future.set_result(result)
        return True

    def _handle_scheduler_mutation_ack(self, ack: MqttAck) -> bool:
        if ack.m_id is None:
            return False
        try:
            message_id = normalize_message_id(ack.m_id)
        except CommandValidationError:
            return False
        pending = self._pending_scheduler_mutations.get(message_id)
        if pending is None:
            return False
        if ack.method.strip().lower() != pending.request.method.value:
            self._wrong_method_count += 1
            return True
        if (
            pending.request.expected_services
            and ack.service.strip().lower() not in pending.request.expected_services
        ):
            self._wrong_service_count += 1
            return True
        self._remove_correlated_pending(pending)
        self._remember_terminal(message_id, "completed")
        self._scheduler_matched_acks += 1
        if ack.response == 65535:
            result = CorrelatedCommandResult(CommandStatus.SUCCESS, message_id, ack=ack)
        else:
            result = CorrelatedCommandResult(
                CommandStatus.FAILED,
                message_id,
                ack=ack,
                error=CommandError(
                    CommandErrorCode.GATEWAY_RESPONSE,
                    "Gateway rejected the command",
                    (f"response={ack.response}",),
                ),
            )
        if not pending.future.done():
            pending.future.set_result(result)
        return True

    def _handle_get_ack(self, ack: MqttAck) -> None:
        if self._handle_scheduler_get_ack(ack):
            return
        pending = self._pending_get
        if pending is None:
            self._external_get_ack_count += 1
            return
        running_service_query = (
            pending.command.method is CommandMethod.GET
            and pending.command.service == "runningservice"
        )
        if not running_service_query and ack.service not in pending.command.accepted_services:
            self._wrong_service_count += 1
            return
        self._pending_get = None
        self._ambiguous_ack_count += 1
        self._ambiguous_get_result_count += 1
        self._last_ambiguity_reason = (
            "GET ACK matches service and method but recurring external GET traffic prevents "
            "request correlation"
        )
        result = self._result_for_error(
            pending,
            CommandError(CommandErrorCode.AMBIGUOUS_CORRELATION, self._last_ambiguity_reason),
        )
        if not pending.future.done():
            pending.future.set_result(result)

    def _handle_state_ack(self, ack: MqttAck) -> None:
        if self._handle_scheduler_mutation_ack(ack):
            return
        if ack.m_id is None:
            matching = [
                pending
                for pending in self._pending.values()
                if ack.service in pending.command.accepted_services
            ]
            if matching:
                pending = matching[0]
                self._remove_pending(pending)
                self._ambiguous_ack_count += 1
                self._last_ambiguity_reason = "state-changing ACK omitted m_id"
                result = self._result_for_error(
                    pending,
                    CommandError(
                        CommandErrorCode.AMBIGUOUS_CORRELATION,
                        self._last_ambiguity_reason,
                    ),
                )
                self._remember_terminal(
                    pending.message_id,
                    CommandErrorCode.AMBIGUOUS_CORRELATION.value,
                )
                if not pending.future.done():
                    pending.future.set_result(result)
            else:
                self._ambiguous_ack_count += 1
            return
        try:
            message_id = normalize_message_id(ack.m_id)
        except CommandValidationError as err:
            self._malformed_ack_count += 1
            self._last_error = CommandError(CommandErrorCode.MALFORMED_ACK, str(err))
            return
        pending = self._pending.get(message_id)
        if pending is None:
            terminal = self._terminal_reason(message_id)
            if terminal == CommandErrorCode.TIMEOUT.value:
                self._late_ack_count += 1
                self._last_error = CommandError(
                    CommandErrorCode.LATE_ACK,
                    "ACK arrived after the command timed out",
                )
            elif terminal == CommandErrorCode.PUBLISH_OUTCOME_UNKNOWN.value:
                self._late_ack_count += 1
                self._last_error = CommandError(
                    CommandErrorCode.LATE_ACK,
                    "ACK arrived after publish delivery was unknown",
                    ("the command was not retried",),
                )
            elif terminal is not None:
                self._duplicate_ack_count += 1
                self._last_error = CommandError(
                    CommandErrorCode.DUPLICATE_ACK,
                    "duplicate ACK arrived after command completion",
                )
            elif self._pending:
                self._foreign_ack_count += 1
                self._last_error = CommandError(
                    CommandErrorCode.FOREIGN_M_ID,
                    "ACK m_id does not belong to the pending integration command",
                )
            else:
                self._late_ack_count += 1
                self._last_error = CommandError(
                    CommandErrorCode.LATE_ACK,
                    "ACK arrived without a pending command",
                )
            return
        if ack.method.lower() != pending.command.method.value:
            self._wrong_method_count += 1
            return
        if ack.service not in pending.command.accepted_services:
            self._wrong_service_count += 1
            return
        self._remove_pending(pending)
        self._remember_terminal(message_id, "completed")
        summary = AckSummary.from_ack(ack, message_id)
        if ack.response == 65535:
            result = CommandResult(
                command_id=pending.command_id,
                method=pending.command.method.value,
                service=pending.command.service,
                status=CommandStatus.SUCCESS,
                message_id=message_id,
                response=ack.response,
                ack=summary,
            )
        else:
            result = self._result_for_error(
                pending,
                CommandError(
                    CommandErrorCode.GATEWAY_RESPONSE,
                    "Gateway rejected the command",
                    (f"response={ack.response}",),
                ),
                status=CommandStatus.FAILED,
                ack=summary,
            )
        if not pending.future.done():
            pending.future.set_result(result)

    def _remove_pending(self, pending: _PendingCommand) -> None:
        if pending.message_id is None:
            if self._pending_get is pending:
                self._pending_get = None
        elif self._pending.get(pending.message_id) is pending:
            del self._pending[pending.message_id]
            self.allocator.retire(pending.message_id)

    def _remove_correlated_pending(self, pending: _PendingCorrelated) -> None:
        registry = (
            self._pending_scheduler_mutations
            if pending.request.state_changing
            else self._pending_scheduler_gets
        )
        if registry.get(pending.request.message_id) is pending:
            del registry[pending.request.message_id]

    def _resolve_correlated_pending(
        self,
        pending: _PendingCorrelated,
        error: CommandError,
    ) -> None:
        self._remove_correlated_pending(pending)
        self._remember_terminal(
            pending.request.message_id,
            CommandErrorCode.MANAGER_STOPPED.value,
        )
        if not pending.future.done():
            pending.future.set_result(
                CorrelatedCommandResult(
                    CommandStatus.FAILED,
                    pending.request.message_id,
                    error=error,
                )
            )

    def _resolve_pending(self, pending: _PendingCommand, result: CommandResult) -> None:
        self._remove_pending(pending)
        self._remember_terminal(
            pending.message_id,
            CommandErrorCode.MANAGER_STOPPED.value,
        )
        if not pending.future.done():
            pending.future.set_result(result)

    def _result_for_error(
        self,
        pending: _PendingCommand,
        error: CommandError,
        *,
        status: CommandStatus = CommandStatus.FAILED,
        ack: AckSummary | None = None,
    ) -> CommandResult:
        return CommandResult(
            command_id=pending.command_id,
            method=pending.command.method.value,
            service=pending.command.service,
            status=status,
            message_id=pending.message_id,
            response=ack.response if ack else None,
            ack=ack,
            error=error,
        )

    def _remember_result(self, result: CommandResult) -> None:
        self._last_result = result
        if result.error is not None:
            self._last_error = result.error

    def diagnostics(self) -> dict[str, Any]:
        """Return sanitized manager state suitable for Home Assistant diagnostics."""
        plant_health = self._plant_health()
        operation_impact = self._operation_impact()
        reasons = self.gate.blocked_reasons(
            None,
            plant_health,
            operation_impact,
            manager_ready=self._ready,
            capability_supported=True,
            pending=self.pending_count > 0,
        )
        self._prune_terminal_ids()
        return {
            "ready": self._ready,
            "write_gate_enabled": self.gate.global_enabled,
            "release_write_gate": "enabled" if self.gate.global_enabled else "disabled",
            "control_infrastructure_status": "partially_live_validated",
            "read_only_command_path_status": "live_validated",
            "state_changing_command_path_status": "not_live_validated",
            "plant_health": plant_health.as_dict(),
            "operation_impact": operation_impact.as_dict(),
            "control_block_reasons": list(reasons),
            "pending_command_count": self.pending_count,
            "pending_generic_command_count": len(self._pending),
            "pending_generic_get_count": 1 if self._pending_get is not None else 0,
            "pending_scheduler_mutation_count": len(self._pending_scheduler_mutations),
            "pending_scheduler_get_count": len(self._pending_scheduler_gets),
            "pending_get_count": 1 if self._pending_get is not None else 0,
            "pending_get_state": "pending" if self._pending_get is not None else "none",
            "scheduler_mutation_attempts": self._scheduler_mutation_attempts,
            "scheduler_keyed_get_attempts": self._scheduler_keyed_get_attempts,
            "scheduler_matched_acks": self._scheduler_matched_acks,
            "scheduler_get_matches": self._scheduler_get_matches,
            "scheduler_transport_timeouts": self._scheduler_transport_timeouts,
            "read_only_publish_attempts": self._read_only_publish_attempts,
            "state_changing_publish_attempts": self._state_changing_publish_attempts,
            "successful_publish_returns": self._successful_publish_returns,
            "successful_read_only_publish_returns": self._successful_read_only_publish_returns,
            "successful_state_changing_publish_returns": (
                self._successful_state_changing_publish_returns
            ),
            "ambiguous_get_result_count": self._ambiguous_get_result_count,
            "terminal_ack_history_size": len(self._terminal_ids),
            "terminal_ack_history_limit": self._terminal_history_limit,
            "terminal_ack_history_ttl_seconds": self._terminal_history_ttl_seconds,
            "last_sanitized_result": (
                self._last_result.as_dict() if self._last_result is not None else None
            ),
            "last_error": self._last_error.as_dict() if self._last_error else None,
            "last_timeout_reason": self._last_timeout_reason,
            "last_ambiguity_reason": self._last_ambiguity_reason,
            "malformed_ack_count": self._malformed_ack_count,
            "unidentified_response_11_count": self._unidentified_response_11_count,
            "last_response_11_missing_fields": list(self._last_response_11_missing_fields),
            "duplicate_ack_count": self._duplicate_ack_count,
            "late_ack_count": self._late_ack_count,
            "ambiguous_ack_count": self._ambiguous_ack_count,
            "foreign_ack_count": self._foreign_ack_count,
            "wrong_service_count": self._wrong_service_count,
            "wrong_method_count": self._wrong_method_count,
            "external_get_ack_count": self._external_get_ack_count,
            "observed_at": datetime.now(UTC).isoformat(),
        }
