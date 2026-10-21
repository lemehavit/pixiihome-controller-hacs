"""Typed production control adapter over the single CommandManager writer."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ..command import CommandResult
from ..protocol.commands import (
    AdaptivePeakShaving,
    BatteryCalibration,
    DemandResponse,
    RunningServiceGet,
    TargetSoC,
)
from .models import ControlOperation

if TYPE_CHECKING:
    from ..command import CommandManager


@dataclass(frozen=True, slots=True)
class ProductionControlAuthority:
    """Fail-closed runtime authority with a durable generation."""

    enabled: bool = False
    revision: int = 0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("production control authority must be boolean")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int):
            raise ValueError("production control authority revision must be an integer")
        if self.revision < 0:
            raise ValueError("production control authority revision must be non-negative")

    @classmethod
    def from_mapping(cls, value: object) -> ProductionControlAuthority:
        if not isinstance(value, dict):
            return cls()
        enabled = value.get("enabled", False)
        revision = value.get("revision", 0)
        if type(enabled) is not bool or isinstance(revision, bool) or not isinstance(revision, int):
            return cls()
        try:
            return cls(enabled=enabled, revision=revision)
        except ValueError:
            return cls()

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "revision": self.revision}


@dataclass(frozen=True, slots=True)
class ProductionAdapterRequest:
    """One exact typed production request; arbitrary service payloads are rejected."""

    operation: ControlOperation
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        operation = ControlOperation(self.operation)
        object.__setattr__(self, "operation", operation)
        if operation is ControlOperation.DIRECT_DEMAND_RESPONSE:
            required = {"power_w", "duration_seconds"}
        elif operation is ControlOperation.DIRECT_TARGET_SOC:
            required = {"soc_percent", "duration_seconds"}
        elif operation is ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING:
            required = {
                "export_limit_w",
                "import_limit_w",
                "charge_max_w",
                "discharge_max_w",
                "keep_active",
                "max_limit_change_w",
                "duration_seconds",
            }
        elif operation is ControlOperation.DIRECT_BATTERY_CALIBRATION:
            required = {"soc_percent", "duration_seconds"}
        else:
            raise ValueError("production adapter operation is not typed")
        if set(self.payload) != required:
            raise ValueError("production adapter payload is not exact")
        object.__setattr__(self, "payload", dict(self.payload))


class ProductionControlAdapter:
    """Build typed commands and delegate every publish to CommandManager."""

    def __init__(self, command_manager: CommandManager) -> None:
        self._command_manager = command_manager

    async def execute(
        self,
        request: ProductionAdapterRequest,
        *,
        message_id: int | None = None,
        persist_message_id: bool = False,
    ) -> CommandResult:
        payload = request.payload
        if request.operation is ControlOperation.DIRECT_DEMAND_RESPONSE:
            command = DemandResponse(
                power_w=payload["power_w"],
                duration_seconds=payload["duration_seconds"],
            ).build()
        elif request.operation is ControlOperation.DIRECT_TARGET_SOC:
            command = TargetSoC(
                soc_percent=payload["soc_percent"],
                duration_seconds=payload["duration_seconds"],
            ).build()
        elif request.operation is ControlOperation.DIRECT_ADAPTIVE_PEAK_SHAVING:
            command = AdaptivePeakShaving(
                export_limit_w=payload["export_limit_w"],
                import_limit_w=payload["import_limit_w"],
                charge_max_w=payload["charge_max_w"],
                discharge_max_w=payload["discharge_max_w"],
                keep_active=payload["keep_active"],
                max_limit_change_w=payload["max_limit_change_w"],
                duration_seconds=payload["duration_seconds"],
            ).build()
        else:
            command = BatteryCalibration(
                endpoint_soc=payload["soc_percent"],
                duration_seconds=payload["duration_seconds"],
            ).build()
        if message_id is not None:
            command = replace(command, message_id=message_id)
        if persist_message_id:
            command = replace(command, persist_message_id=True)
        return await self._command_manager.execute(command)

    async def readback(self) -> CommandResult:
        """Read the Gateway current service through the same typed manager."""
        return await self._command_manager.execute(RunningServiceGet().build())


__all__ = [
    "ProductionAdapterRequest",
    "ProductionControlAdapter",
    "ProductionControlAuthority",
]
