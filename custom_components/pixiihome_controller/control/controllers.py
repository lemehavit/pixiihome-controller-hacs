"""Deterministic arbitration for integration-owned automatic controllers."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import ControlOperation
from .policy import AutomationPolicy
from .settings import ControllerState, OperationalSettings


class ControllerDecisionStatus(StrEnum):
    IDLE = "idle"
    PROPOSAL = "proposal"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class ControllerInputs:
    """Bounded telemetry and external-control facts used by arbitration."""

    soc_percent: float
    solar_surplus_kw: float = 0.0
    grid_import_kw: float = 0.0
    phase_current_a: float = 0.0
    price_signal: float | None = None
    price_confidence: float = 0.0
    elapsed_since_transition_seconds: int = 0
    solar_active: bool = False
    manual_control_active: bool = False
    external_control_active: bool = False
    telemetry_fresh: bool = True
    system_safe: bool = True
    battery_safe: bool = True
    grid_safe: bool = True


@dataclass(frozen=True, slots=True)
class ControllerProposal:
    controller: str
    operation: ControlOperation
    power_kw: float | None = None
    target_soc_percent: float | None = None
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ControllerDecision:
    status: ControllerDecisionStatus
    proposal: ControllerProposal | None = None
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "proposal": None
            if self.proposal is None
            else {
                "controller": self.proposal.controller,
                "operation": self.proposal.operation.value,
                "power_kw": self.proposal.power_kw,
                "target_soc_percent": self.proposal.target_soc_percent,
                "reason": self.proposal.reason,
            },
            "reasons": list(self.reasons),
        }


class ControllerArbitrator:
    """Apply fixed safety, manual, and controller precedence without I/O."""

    def evaluate(
        self,
        settings: OperationalSettings,
        controller_state: ControllerState,
        policy: AutomationPolicy,
        inputs: ControllerInputs,
    ) -> ControllerDecision:
        if not policy.enabled:
            return ControllerDecision(
                ControllerDecisionStatus.BLOCKED,
                reasons=("policy_disabled",),
            )
        if not inputs.telemetry_fresh:
            return ControllerDecision(
                ControllerDecisionStatus.BLOCKED,
                reasons=("telemetry_stale",),
            )
        if not (inputs.system_safe and inputs.battery_safe and inputs.grid_safe):
            return ControllerDecision(
                ControllerDecisionStatus.BLOCKED,
                reasons=("plant_unsafe",),
            )
        if inputs.external_control_active:
            return ControllerDecision(
                ControllerDecisionStatus.BLOCKED,
                reasons=("external_control_active",),
            )
        if inputs.manual_control_active:
            return ControllerDecision(
                ControllerDecisionStatus.IDLE,
                reasons=("manual_control_priority",),
            )

        candidates: list[ControllerProposal] = []
        if controller_state.load_balancing_enabled:
            phase_limit = float(settings.get("load_balancing_max_phase_current_a"))
            margin = float(settings.get("load_balancing_safety_margin_a"))
            if inputs.phase_current_a > phase_limit - margin:
                # An unsigned maximum phase current cannot determine safe power,
                # direction, live mode identity or its original deadline.
                return ControllerDecision(
                    ControllerDecisionStatus.BLOCKED,
                    reasons=("load_balancing_observation_only",),
                )

        if controller_state.automatic_solar_charging_enabled:
            start = float(settings.get("solar_export_start_threshold_kw"))
            stop = float(settings.get("solar_export_stop_threshold_kw"))
            threshold = stop if inputs.solar_active else start
            if (
                inputs.solar_surplus_kw >= threshold
                and inputs.soc_percent < float(settings.get("solar_max_soc_percent"))
                and inputs.elapsed_since_transition_seconds
                >= int(
                    settings.get(
                        "solar_stop_debounce_seconds"
                        if inputs.solar_active
                        else "solar_start_debounce_seconds"
                    )
                )
            ):
                candidates.append(
                    ControllerProposal(
                        "automatic_solar_charging",
                        ControlOperation.DIRECT_DEMAND_RESPONSE,
                        power_kw=min(
                            inputs.solar_surplus_kw,
                            float(settings.get("solar_max_charge_power_kw")),
                        ),
                        reason="solar_surplus_hysteresis",
                    )
                )

        if controller_state.automatic_price_optimization_enabled:
            if (
                inputs.price_signal is not None
                and inputs.price_confidence >= 0.7
                and inputs.elapsed_since_transition_seconds
                >= int(settings.get("load_balancing_debounce_seconds"))
            ):
                candidates.append(
                    ControllerProposal(
                        "automatic_price_optimization",
                        ControlOperation.DIRECT_TARGET_SOC,
                        target_soc_percent=float(settings.get("planner_reserve_soc_percent")),
                        reason="price_signal_confident",
                    )
                )

        if not candidates:
            return ControllerDecision(ControllerDecisionStatus.IDLE)
        return ControllerDecision(
            ControllerDecisionStatus.PROPOSAL,
            proposal=candidates[0],
            reasons=("deterministic_precedence",),
        )


__all__ = [
    "ControllerArbitrator",
    "ControllerDecision",
    "ControllerDecisionStatus",
    "ControllerInputs",
    "ControllerProposal",
]
