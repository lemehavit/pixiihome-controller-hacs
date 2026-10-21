"""Home Assistant device grouping for Pixii controller functions."""

from __future__ import annotations

from enum import StrEnum

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import ChildDeviceInfo, DeviceInfo

from .const import DOMAIN


class PixiiFeature(StrEnum):
    """Logical controller functions shown as child devices in Home Assistant."""

    MANUAL_CONTROL = "manual_control"
    SCHEDULE_PLANNER = "schedule_planner"
    LOAD_BALANCING = "load_balancing"
    SOLAR_CHARGING = "solar_charging"
    ADAPTIVE_PEAK_SHAVING = "adaptive_peak_shaving"
    PRICE_OPTIMIZATION = "price_optimization"


FEATURE_NAMES: dict[PixiiFeature, str] = {
    PixiiFeature.MANUAL_CONTROL: "Manual control",
    PixiiFeature.SCHEDULE_PLANNER: "Schedule planner",
    PixiiFeature.LOAD_BALANCING: "Load balancing",
    PixiiFeature.SOLAR_CHARGING: "Automatic solar charging",
    PixiiFeature.ADAPTIVE_PEAK_SHAVING: "Adaptive peak shaving",
    PixiiFeature.PRICE_OPTIMIZATION: "Price optimization",
}


def gateway_device_info(serial: str) -> DeviceInfo:
    """Return the stable physical Gateway identity."""
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        name=f"Pixii Gateway {serial}",
        manufacturer="Pixii",
    )


def ensure_gateway_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    serial: str,
) -> str | None:
    """Ensure the parent Gateway exists and return its registry id.

    Lightweight unit tests may call platform setup without initializing Home
    Assistant's device registry. Returning ``None`` keeps those presentation-only
    tests usable; a fully initialized Home Assistant always registers the parent.
    """
    try:
        registry = dr.async_get(hass)
    except RuntimeError:
        return None
    parent = registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, serial)},
        name=f"Pixii Gateway {serial}",
        manufacturer="Pixii",
    )
    return parent.id


def feature_device_info(
    serial: str,
    feature: PixiiFeature,
    parent_device_id: str | None,
) -> DeviceInfo | ChildDeviceInfo:
    """Return one logical function identity linked to the physical Gateway."""
    identity = {(DOMAIN, f"{serial}_{feature.value}")}
    name = f"{FEATURE_NAMES[feature]} ({serial})"
    if parent_device_id is None:
        return DeviceInfo(identifiers=identity, name=name, manufacturer="Pixii")
    return ChildDeviceInfo(
        identifiers=identity,
        name=name,
        parent_device_id=parent_device_id,
    )


def setting_feature(key: str) -> PixiiFeature:
    """Map every persisted operational setting to its functional device."""
    if key.startswith(("manual_", "target_soc_")):
        return PixiiFeature.MANUAL_CONTROL
    if key.startswith("planner_"):
        return PixiiFeature.SCHEDULE_PLANNER
    if key.startswith("load_balancing_"):
        return PixiiFeature.LOAD_BALANCING
    if key.startswith("solar_"):
        return PixiiFeature.SOLAR_CHARGING
    if key.startswith("aps_"):
        return PixiiFeature.ADAPTIVE_PEAK_SHAVING
    if key.startswith("price_"):
        return PixiiFeature.PRICE_OPTIMIZATION
    raise ValueError(f"Operational setting has no device group: {key}")


__all__ = [
    "FEATURE_NAMES",
    "PixiiFeature",
    "ensure_gateway_device",
    "feature_device_info",
    "gateway_device_info",
    "setting_feature",
]
