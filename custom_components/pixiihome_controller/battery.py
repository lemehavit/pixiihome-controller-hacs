"""Battery telemetry normalization and usable-SoC calculation."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _percentage(value: Any) -> float | None:
    number = _number(value)
    if number is None or not 0.0 <= number <= 100.0:
        return None
    return number


def _clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


@dataclass(frozen=True, slots=True)
class BatteryReading:
    raw_soc: float | None = None
    usable_soc: float | None = None
    stored_energy_kwh: float | None = None
    power_w: float | None = None
    current_a: float | None = None
    soh: float | None = None
    effective_min_soc: float | None = None
    effective_max_soc: float | None = None
    reported_min_soc: float | None = None
    reported_max_soc: float | None = None
    usable_soc_source: str | None = None
    charge_power_limit_w: float | None = None
    discharge_power_limit_w: float | None = None


class BatteryModel:
    """Converts one battery/energy payload pair into stable typed values."""

    @staticmethod
    def calculate_usable_soc(
        raw_soc: Any,
        minimum: Any,
        maximum: Any,
    ) -> float | None:
        soc = _number(raw_soc)
        low = _number(minimum)
        high = _number(maximum)
        if soc is None or low is None or high is None or high <= low:
            return None
        return _clamp((soc - low) * 100.0 / (high - low), 0.0, 100.0)

    def reading(
        self,
        battery: dict[str, Any] | None,
        energy: dict[str, Any] | None,
    ) -> BatteryReading:
        battery = battery or {}
        energy = energy or {}
        raw_soc = _number(battery.get("batt_soc"))
        configured_min = _number(battery.get("batt_cfg_soc_min"))
        configured_max = _number(battery.get("batt_cfg_soc_max"))
        reported_min = _number(battery.get("batt_soc_min"))
        reported_max = _number(battery.get("batt_soc_max"))
        native_usable = _percentage(battery.get("batt_soc_usable"))
        configured_limits_valid = (
            configured_min is not None
            and configured_max is not None
            and configured_max > configured_min
        )
        if native_usable is not None:
            usable_soc = native_usable
            usable_soc_source = "gateway_native"
        else:
            usable_soc = self.calculate_usable_soc(
                raw_soc, configured_min, configured_max
            )
            usable_soc_source = (
                "configured_limits" if usable_soc is not None else None
            )
        return BatteryReading(
            raw_soc=_clamp(raw_soc, 0.0, 100.0) if raw_soc is not None else None,
            usable_soc=usable_soc,
            stored_energy_kwh=_number(energy.get("batt_soc_kwh")),
            power_w=_number(battery.get("batt_w")),
            current_a=_number(battery.get("batt_a")),
            soh=_number(battery.get("batt_soh")),
            effective_min_soc=(
                configured_min if configured_limits_valid else None
            ),
            effective_max_soc=(
                configured_max if configured_limits_valid else None
            ),
            reported_min_soc=reported_min,
            reported_max_soc=reported_max,
            usable_soc_source=usable_soc_source,
            charge_power_limit_w=_number(battery.get("batt_cap_w_ch")),
            discharge_power_limit_w=_number(battery.get("batt_cap_w_disch")),
        )
