"""Documented values for manually scheduled MQTT services (MQTT guide 1.7)."""

from __future__ import annotations

from collections.abc import Mapping
from math import isfinite
from typing import Any

from ..exceptions import CommandValidationError

SCHEDULER_VALUE_KEYS = {
    "targetsoc": frozenset({"socref"}),
    "demandresponse": frozenset({"pacref"}),
    "peakshaving": frozenset(
        {"exportlim", "importlim", "charge_max", "discharge_max", "keep_active"}
    ),
    "adaptivepeakshaving": frozenset(
        {"exportlim", "importlim", "charge_max", "discharge_max", "keep_active", "max_lim_change"}
    ),
}
MANUAL_SCHEDULER_SERVICES = frozenset(SCHEDULER_VALUE_KEYS)


def validate_scheduler_values(service: str, values: Mapping[str, Any] | None) -> dict[str, Any]:
    """Require the complete documented schema without accepting arbitrary wire keys."""
    keys = SCHEDULER_VALUE_KEYS.get(service)
    if keys is None or not isinstance(values, Mapping) or set(values) != keys:
        raise CommandValidationError(f"{service} values do not match the documented schema")
    result = dict(values)
    for key, value in result.items():
        if key == "keep_active":
            if type(value) is not bool:
                raise CommandValidationError("keep_active must be boolean")
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise CommandValidationError(f"{key} must be a finite number")
    if service == "targetsoc" and not 0 <= result["socref"] <= 100:
        raise CommandValidationError("targetsoc socref must be between 0 and 100")
    if service in {"peakshaving", "adaptivepeakshaving"}:
        if result["exportlim"] > result["importlim"]:
            raise CommandValidationError("exportlim must not exceed importlim")
        if result["charge_max"] < 0 or result["discharge_max"] < 0:
            raise CommandValidationError("maximum charge and discharge power must be nonnegative")
        if service == "adaptivepeakshaving" and result["max_lim_change"] < 0:
            raise CommandValidationError("max_lim_change must be nonnegative")
    return result
