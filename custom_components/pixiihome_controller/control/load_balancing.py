"""Read-only phase calculations and expiry-bound mode previews. No transport."""

from __future__ import annotations

from dataclasses import dataclass
from math import floor, isfinite, sqrt
from typing import Any, TypeGuard

from ..battery import BatteryModel

CONF_LOAD_BALANCING = "load_balancing_observation"


@dataclass(frozen=True)
class LoadBalancingConfig:
    enabled: bool = False
    source: str = "pixii"
    phases: int = 3
    fuse_l1: float = 16.0
    fuse_l2: float = 16.0
    fuse_l3: float = 16.0
    margin_a: float = 2.0
    min_soc: float = 10.0
    max_charge_kw: float = 10.0
    max_discharge_kw: float = 10.0
    poll_seconds: int = 5
    stale_seconds: int = 15
    recovery_step_w: float = 500.0
    current_l1: str = ""
    current_l2: str = ""
    current_l3: str = ""
    voltage_l1: str = ""
    voltage_l2: str = ""
    voltage_l3: str = ""
    power_l1: str = ""
    power_l2: str = ""
    power_l3: str = ""

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool or self.source not in {"pixii", "entities"}:
            raise ValueError("invalid_config")
        if type(self.phases) is not int or self.phases not in (1, 3):
            raise ValueError("invalid_config")
        limits = {
            "fuse_l1": (1, 1000),
            "fuse_l2": (1, 1000),
            "fuse_l3": (1, 1000),
            "margin_a": (0, 100),
            "min_soc": (0, 100),
            "max_charge_kw": (0, 1000),
            "max_discharge_kw": (0, 1000),
            "poll_seconds": (2, 60),
            "stale_seconds": (5, 120),
            "recovery_step_w": (10, 10000),
        }
        for key, (low, high) in limits.items():
            value = getattr(self, key)
            if not finite(value) or not low <= value <= high:
                raise ValueError("invalid_config")
        if any(self.margin_a >= fuse for fuse in self.fuses):
            raise ValueError("margin_exceeds_fuse")
        if self.stale_seconds < self.poll_seconds * 2:
            raise ValueError("stale_interval_too_short")
        if type(self.poll_seconds) is not int or type(self.stale_seconds) is not int:
            raise ValueError("invalid_config")
        for prefix in ("current", "voltage", "power"):
            for phase in range(1, 4):
                entity = getattr(self, f"{prefix}_l{phase}")
                if not isinstance(entity, str) or (entity and not entity.startswith("sensor.")):
                    raise ValueError("invalid_entity")
        if self.enabled and self.source == "entities":
            ids = [
                getattr(self, f"{prefix}_l{i}")
                for prefix in ("current", "voltage")
                for i in range(1, self.phases + 1)
            ]
            selected = ids + [
                getattr(self, f"power_l{i}")
                for i in range(1, self.phases + 1)
                if getattr(self, f"power_l{i}")
            ]
            if not all(ids) or len(selected) != len(set(selected)):
                raise ValueError("meter_entities_required")

    @property
    def fuses(self) -> tuple[float, ...]:
        return (self.fuse_l1, self.fuse_l2, self.fuse_l3)[: self.phases]

    def as_dict(self) -> dict[str, Any]:
        return dict(vars(self))

    @classmethod
    def from_mapping(cls, value: object) -> LoadBalancingConfig:
        if not isinstance(value, dict):
            return cls()
        return cls(**{k: v for k, v in value.items() if k in cls.__dataclass_fields__})


def finite(value: object) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def usable_soc_evidence(payload: dict[str, Any], *, fresh: bool) -> dict[str, Any]:
    """Use the usable battery window, never reinterpret raw SoC as available SoC.

    The configured reserve is separate from dynamic pack minimum/maximum
    readings. Missing/stale reserves cannot silently become a 0–100% window.
    """
    empty = {
        "usable_soc_percent": None,
        "usable_soc_source": None,
        "raw_soc_percent": None,
        "reserved_min_soc_percent": None,
        "reserved_max_soc_percent": None,
        "soc_evidence_fresh": fresh,
    }
    if not fresh:
        return empty
    raw = payload.get("batt_soc")
    low, high = payload.get("batt_cfg_soc_min"), payload.get("batt_cfg_soc_max")
    native = payload.get("batt_soc_usable")
    if not finite(raw) or not 0 <= raw <= 100:
        return empty
    limits_valid = finite(low) and finite(high) and 0 <= low < high <= 100
    native_valid = finite(native) and 0 <= native <= 100
    usable = (
        native
        if native_valid
        else BatteryModel.calculate_usable_soc(raw, low, high)
        if limits_valid
        else None
    )
    return {
        **empty,
        "usable_soc_percent": usable,
        "raw_soc_percent": raw,
        "usable_soc_source": (
            "gateway_native" if native_valid else "configured_limits" if limits_valid else None
        ),
        "reserved_min_soc_percent": low if limits_valid else None,
        "reserved_max_soc_percent": high if limits_valid else None,
    }


@dataclass(frozen=True)
class PhaseSample:
    current_a: float
    voltage_v: float
    grid_power_w: float
    battery_power_w: float


def phase_envelope(
    config: LoadBalancingConfig,
    samples: tuple[PhaseSample, ...],
    usable_soc: float | None,
    *,
    charge_permitted: bool = True,
    discharge_permitted: bool = True,
) -> dict[str, Any]:
    """Estimate a symmetric AC-power envelope, holding reactive current constant.

    Positive battery AC power charges. Signed grid power is positive for import.
    This is a diagnostic estimate, never a fuse protection or a write permit.
    """
    if len(samples) != config.phases:
        raise ValueError("phase_data_missing")
    upper, lower = [], []
    phase_bounds = []
    overloaded = []
    for index, (sample, fuse) in enumerate(zip(samples, config.fuses, strict=True)):
        if not all(finite(v) for v in vars(sample).values()):
            raise ValueError("phase_data_invalid")
        if sample.current_a < 0 or not 80 <= sample.voltage_v <= 300:
            raise ValueError("phase_data_invalid")
        apparent = sample.current_a * sample.voltage_v
        # Allow meter rounding, but reject mismatched phases, scales or timestamps.
        if abs(sample.grid_power_w) > apparent + max(50, apparent * 0.1):
            raise ValueError("phase_measurements_inconsistent")
        reactive_sq = max(0, apparent**2 - sample.grid_power_w**2)
        capacity_sq = ((fuse - config.margin_a) * sample.voltage_v) ** 2
        if reactive_sq > capacity_sq:
            raise ValueError("reactive_current_exceeds_limit")
        active_capacity = sqrt(capacity_sq - reactive_sq)
        base = sample.grid_power_w - sample.battery_power_w
        upper.append((active_capacity - base) * config.phases)
        lower.append((-active_capacity - base) * config.phases)
        phase_bounds.append(
            {
                "phase": index + 1,
                "battery_floor_w": round(-active_capacity - base, 1),
                "battery_ceiling_w": round(active_capacity - base, 1),
                "required_discharge_w": round(max(0, base - active_capacity), 1),
            }
        )
        if sample.current_a > fuse - config.margin_a:
            overloaded.append(index + 1)
    import_ceiling = min(upper)
    export_floor = max(lower)
    soc_allows_discharge = finite(usable_soc) and config.min_soc < usable_soc <= 100
    discharge_available = soc_allows_discharge and discharge_permitted
    allowed_floor = max(export_floor, -config.max_discharge_kw * 1000 if discharge_available else 0)
    allowed_ceiling = min(import_ceiling, config.max_charge_kw * 1000 if charge_permitted else 0)
    return {
        "charge_headroom_w": round(max(0, allowed_ceiling), 1),
        "required_discharge_w": round(max(0, -import_ceiling), 1),
        "battery_floor_w": round(allowed_floor, 1),
        "battery_ceiling_w": round(allowed_ceiling, 1),
        "feasible": allowed_floor <= allowed_ceiling,
        "overloaded_phases": overloaded,
        "usable_soc_percent": usable_soc,
        "soc_basis": "usable",
        "discharge_available_by_soc": bool(soc_allows_discharge),
        "discharge_available": bool(discharge_available),
        "assumption": "symmetric_ac_power_constant_reactive_current",
        "hardware_limits_verified": False,
        "individual_phase_estimates": phase_bounds,
        "individual_phase_control_verified": False,
    }


@dataclass(frozen=True)
class ActiveModeIntent:
    """One verified occurrence, not a service type or a reusable schedule ID.

    Generation changes only for a separately verified new activation. The
    deadline is absolute and must never be recreated as now + original duration.
    """

    identity: str
    generation: str
    desired_power_w: float
    observed_at: float
    deadline: float | None
    continuous: bool = False
    verified: bool = False
    active: bool = True


class ModeBoundPreview:
    """Preview only against fresh live intent; never remember an old target."""

    def __init__(self) -> None:
        self._key: tuple[str, str] | None = None
        self._latest_key: tuple[str, str] | None = None
        self._deadline: float | None = None
        self._previous: float | None = None
        self._evaluated_at: float | None = None
        self._deadlines: dict[tuple[str, str], float | None] = {}
        self._retired: set[tuple[str, str]] = set()

    def reset(self) -> None:
        self._key = None
        self._deadline = self._previous = self._evaluated_at = None

    def evaluate(
        self,
        intent: ActiveModeIntent | None,
        *,
        now: float,
        current_power_w: float,
        floor_w: float,
        ceiling_w: float,
        max_age_seconds: float = 15,
        recovery_step_w: float = 500,
        recovery_interval_seconds: float = 5,
    ) -> dict[str, Any]:
        def blocked(reason: str) -> dict[str, Any]:
            self.reset()
            return {"status": reason, "target_power_w": None, "resume_allowed": False}

        if not all(
            finite(v)
            for v in (
                now,
                current_power_w,
                floor_w,
                ceiling_w,
                max_age_seconds,
                recovery_step_w,
                recovery_interval_seconds,
            )
        ):
            return blocked("invalid_evidence")
        if max_age_seconds <= 0 or recovery_interval_seconds <= 0 or recovery_step_w <= 0:
            return blocked("invalid_evidence")
        if intent is None or not intent.active:
            if self._latest_key is not None:
                self._retired.add(self._latest_key)
            if intent is not None and intent.identity and intent.generation:
                self._retired.add((intent.identity, intent.generation))
            return blocked("no_active_mode")
        if not intent.verified or not intent.identity or not intent.generation:
            return blocked("active_mode_unverified")
        if (
            not finite(intent.observed_at)
            or not finite(intent.desired_power_w)
            or not 0 <= now - intent.observed_at <= max_age_seconds
        ):
            return blocked("active_mode_stale")
        if intent.deadline is None and not intent.continuous:
            return blocked("mode_deadline_unknown")
        if intent.deadline is not None and not finite(intent.deadline):
            return blocked("mode_deadline_unknown")
        key = (intent.identity, intent.generation)
        if key in self._retired:
            return blocked("mode_expired_or_replaced")
        if self._latest_key is not None and key != self._latest_key:
            self._retired.add(self._latest_key)
        self._latest_key = key
        if key not in self._deadlines:
            if len(self._deadlines) >= 128:
                return blocked("mode_history_full")
            self._deadlines[key] = intent.deadline
        else:
            original = self._deadlines[key]
            if original is not None:
                if intent.deadline is None:
                    return blocked("mode_deadline_changed")
                self._deadlines[key] = min(original, intent.deadline)
            elif intent.deadline is not None:
                self._deadlines[key] = intent.deadline
        if key != self._key:
            if self._key is not None:
                self._retired.add(self._key)
            self.reset()
            self._key, self._deadline = key, self._deadlines[key]
            self._previous = current_power_w
        self._deadline = self._deadlines[key]
        if self._deadline is not None and floor(self._deadline - now) <= 0:
            self._retired.add(key)
            return blocked("mode_expired")
        if self._evaluated_at is not None and now < self._evaluated_at:
            return blocked("clock_changed")
        if floor_w > ceiling_w:
            return blocked("insufficient_capacity")
        target = min(ceiling_w, max(floor_w, intent.desired_power_w))
        previous = self._previous if self._previous is not None else current_power_w
        elapsed = 0 if self._evaluated_at is None else now - self._evaluated_at
        # Reduce immediately; recover only toward today's still-active intent.
        if target > previous:
            target = min(target, previous + recovery_step_w * elapsed / recovery_interval_seconds)
            target = max(floor_w, min(ceiling_w, target))
        self._previous, self._evaluated_at = target, now
        return {
            "status": "preview_only",
            "target_power_w": target,
            "resume_allowed": True,
            "identity": intent.identity,
            "generation": intent.generation,
            "deadline": self._deadline,
            "remaining_seconds": (
                floor(self._deadline - now) if self._deadline is not None else None
            ),
            "valid_until": min(
                intent.observed_at + max_age_seconds,
                self._deadline if self._deadline is not None else float("inf"),
            ),
        }
