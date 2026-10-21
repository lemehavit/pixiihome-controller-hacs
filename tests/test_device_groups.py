"""Tests for native Home Assistant grouping of Pixii functions."""

from custom_components.pixiihome_controller.const import DOMAIN
from custom_components.pixiihome_controller.control.settings import SETTING_SPECS
from custom_components.pixiihome_controller.devices import (
    PixiiFeature,
    feature_device_info,
    gateway_device_info,
    setting_feature,
)


def test_every_operational_setting_has_a_function_group() -> None:
    # The energy forecast device holds sensors only; it has no settings.
    assert {setting_feature(key) for key in SETTING_SPECS} == set(PixiiFeature) - {
        PixiiFeature.ENERGY_FORECAST
    }


def test_feature_device_is_a_stable_child_of_the_gateway() -> None:
    info = feature_device_info(
        "GATEWAY",
        PixiiFeature.PRICE_OPTIMIZATION,
        "parent-device-id",
    )

    assert info == {
        "identifiers": {(DOMAIN, "GATEWAY_price_optimization")},
        "name": "Price optimization (GATEWAY)",
        "parent_device_id": "parent-device-id",
    }


def test_gateway_identity_remains_unchanged() -> None:
    info = gateway_device_info("GATEWAY")

    assert info["identifiers"] == {(DOMAIN, "GATEWAY")}
    assert info["name"] == "Pixii Gateway GATEWAY"
    assert info["manufacturer"] == "Pixii"
