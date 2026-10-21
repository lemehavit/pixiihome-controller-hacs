"""Production scheduler command transport through CommandManager ACK routing."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from ..command import (
    CommandManager,
    CommandStatus,
    CorrelatedCommandRequest,
)
from ..exceptions import CommandValidationError
from ..protocol.models import MqttAck
from ..protocol.scheduler_commands import SchedulerCommand
from .mutations import (
    SchedulerCommandExecutor,
    SchedulerExecutionResult,
    SchedulerExecutionStatus,
)


@dataclass(slots=True)
class HomeAssistantSchedulerCommandExecutor(SchedulerCommandExecutor):
    """Adapt scheduler commands to CommandManager's one ACK subscription.

    This adapter owns no scheduler identifier or ownership lifecycle. It only
    converts the scheduler command envelope to a pre-correlated CommandManager
    request and maps the typed transport result back to the scheduler protocol.
    """

    command_manager: CommandManager
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or not isfinite(self.timeout_seconds):
            raise CommandValidationError("scheduler transport timeout must be finite and positive")

    async def execute_mutation(self, command: SchedulerCommand) -> SchedulerExecutionResult:
        """Publish one SET or DELETE and await its exact ACK."""
        if not command.state_changing:
            return SchedulerExecutionResult(
                SchedulerExecutionStatus.EXECUTION_ERROR,
                reason="scheduler_mutation_requires_state_changing_command",
            )
        result = await self.command_manager.execute_correlated(self._request(command))
        if result.ack is not None:
            if result.status is CommandStatus.SUCCESS:
                return SchedulerExecutionResult(
                    SchedulerExecutionStatus.ACK_SUCCESS,
                    ack=result.ack,
                )
            return SchedulerExecutionResult(
                SchedulerExecutionStatus.ACK_REJECTED,
                ack=result.ack,
                reason="gateway_rejected",
            )
        if result.status is CommandStatus.TIMED_OUT:
            return SchedulerExecutionResult(
                SchedulerExecutionStatus.TIMED_OUT,
                reason="scheduler_transport_timeout",
            )
        if result.status is CommandStatus.DELIVERY_UNKNOWN:
            return SchedulerExecutionResult(
                SchedulerExecutionStatus.DELIVERY_UNKNOWN,
                reason="scheduler_publish_outcome_unknown",
            )
        reason = (
            result.error.code.value if result.error is not None else "scheduler_transport_error"
        )
        return SchedulerExecutionResult(
            SchedulerExecutionStatus.EXECUTION_ERROR,
            reason=reason,
        )

    async def execute_get(self, command: SchedulerCommand) -> MqttAck:
        """Publish one keyed GET and return only its exactly matched typed ACK."""
        if command.method.value != "get":
            raise CommandValidationError("scheduler GET transport requires method=get")
        result = await self.command_manager.execute_correlated(self._request(command))
        if result.ack is not None:
            return result.ack
        if result.status is CommandStatus.TIMED_OUT:
            raise TimeoutError("scheduler keyed GET timed out")
        reason = (
            result.error.code.value if result.error is not None else "scheduler_transport_error"
        )
        raise RuntimeError(reason)

    def _request(self, command: SchedulerCommand) -> CorrelatedCommandRequest:
        # Observed Gateway firmware returns an uncorrelatable response=5
        # ("missing service type") for rev 1.7's service-less keyed GET.
        # Include only the caller's already-known service; never guess one.
        service = command.service
        if (
            command.method.value in {"get", "delete"}
            and service is None
            and len(command.expected_services) == 1
        ):
            service = command.expected_services[0]
        return CorrelatedCommandRequest(
            method=command.method,
            message_id=command.m_id,
            service=service,
            fields=command.fields,
            expected_services=command.expected_services,
            timeout_seconds=self.timeout_seconds,
        )


__all__ = ["HomeAssistantSchedulerCommandExecutor"]
