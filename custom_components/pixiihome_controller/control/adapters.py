"""Explicit production and simulator control adapter boundaries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from inspect import isawaitable
from types import MappingProxyType
from typing import Any, Protocol

from .journal import JournalTargetIdentity
from .models import AdapterCapability, ControlOperation


class AdapterExecutionStatus(StrEnum):
    SUCCESS = "success"
    REJECTED = "rejected"
    NOT_FOUND = "not_found"
    INVALID = "invalid"
    TIMED_OUT = "timed_out"
    DELIVERY_UNKNOWN = "delivery_unknown"


@dataclass(frozen=True, slots=True)
class AdapterRequest:
    """One typed adapter request with a non-collapsible target identity."""

    operation: ControlOperation
    service: str | None = None
    target_identity: JournalTargetIdentity | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.operation, ControlOperation):
            object.__setattr__(self, "operation", ControlOperation(self.operation))
        if self.service is not None:
            if not isinstance(self.service, str) or not self.service.strip():
                raise ValueError("adapter service must be a non-empty string")
            object.__setattr__(self, "service", self.service.strip().lower())
        if not isinstance(self.payload, Mapping):
            raise ValueError("adapter payload must be an object")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        if self.operation is not ControlOperation.READ_ONLY and self.target_identity is None:
            raise ValueError("state-changing adapter requests require typed target identity")


@dataclass(frozen=True, slots=True)
class AdapterResult:
    status: AdapterExecutionStatus
    state: Mapping[str, Any] = field(default_factory=dict)
    reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, AdapterExecutionStatus):
            object.__setattr__(self, "status", AdapterExecutionStatus(self.status))
        if not isinstance(self.state, Mapping):
            raise ValueError("adapter result state must be an object")
        object.__setattr__(self, "state", MappingProxyType(dict(self.state)))


class ControlAdapter(Protocol):
    """Side-effect boundary used only after canonical policy authorization."""

    def capability(
        self,
        operation: ControlOperation,
        service: str | None,
    ) -> AdapterCapability: ...

    async def execute(self, request: AdapterRequest) -> AdapterResult: ...


ReadProvider = Callable[[AdapterRequest], Mapping[str, Any] | Awaitable[Mapping[str, Any]]]


@dataclass(slots=True)
class ProductionReadOnlyAdapter:
    """Production boundary with no state-changing implementation."""

    read: ReadProvider

    def capability(
        self,
        operation: ControlOperation,
        service: str | None,
    ) -> AdapterCapability:
        del service
        if operation is ControlOperation.READ_ONLY:
            return AdapterCapability.SUPPORTED
        if operation in {
            ControlOperation.DIRECT_TARGET_SOC,
            ControlOperation.DIRECT_DEMAND_RESPONSE,
            ControlOperation.CREATE,
            ControlOperation.UPDATE,
            ControlOperation.DELETE,
            ControlOperation.REPLACE,
            ControlOperation.STOP,
            ControlOperation.PLANNER_APPLY,
            ControlOperation.RECOVERY_APPLY,
        }:
            return AdapterCapability.EVIDENCE_REQUIRED
        return AdapterCapability.UNSUPPORTED

    async def execute(self, request: AdapterRequest) -> AdapterResult:
        if request.operation is not ControlOperation.READ_ONLY:
            return AdapterResult(
                AdapterExecutionStatus.REJECTED,
                reason="production_adapter_is_read_only",
            )
        state = self.read(request)
        if isawaitable(state):
            state = await state
        return AdapterResult(AdapterExecutionStatus.SUCCESS, state=state)


__all__ = [
    "AdapterExecutionStatus",
    "AdapterRequest",
    "AdapterResult",
    "ControlAdapter",
    "ProductionReadOnlyAdapter",
]
