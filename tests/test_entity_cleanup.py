"""Retiring presentation entities must preserve settings and unrelated entries."""

from types import SimpleNamespace

import custom_components.pixiihome_controller.entity_cleanup as cleanup
from custom_components.pixiihome_controller.control.settings import OperationalSettings
from custom_components.pixiihome_controller.number import NUMBER_DESCRIPTIONS


def test_retired_settings_are_restored_but_never_exposed_as_live_controls():
    settings = OperationalSettings.from_mapping({
        "values": {"load_balancing_debounce_seconds": 45, "planner_reserve_soc_percent": 25}
    })
    assert settings.get("load_balancing_debounce_seconds") == 45
    assert settings.get("planner_reserve_soc_percent") == 25
    keys = {description.key for description in NUMBER_DESCRIPTIONS}
    assert "load_balancing_debounce_seconds" not in keys
    assert "planner_reserve_soc_percent" not in keys
    assert "solar_max_soc_percent" in keys
    assert "price_min_soc_percent" in keys


def test_registry_cleanup_removes_exact_retired_pixii_identities_only(monkeypatch):
    removed = []
    registry = SimpleNamespace(async_remove=removed.append)
    entries = [
        SimpleNamespace(entity_id="number.old", platform="pixiihome_controller",
                        domain="number", unique_id="GATEWAY_planner_reserve_soc_percent"),
        SimpleNamespace(entity_id="number.active", platform="pixiihome_controller",
                        domain="number", unique_id="GATEWAY_price_min_soc_percent"),
        SimpleNamespace(entity_id="number.other_gateway", platform="pixiihome_controller",
                        domain="number", unique_id="OTHER_planner_reserve_soc_percent"),
        SimpleNamespace(entity_id="number.other_platform", platform="other",
                        domain="number", unique_id="GATEWAY_planner_reserve_soc_percent"),
        SimpleNamespace(entity_id="button.old", platform="pixiihome_controller",
                        domain="button", unique_id="GATEWAY_stop_manual_power"),
    ]
    monkeypatch.setattr(cleanup.er, "async_get", lambda hass: registry)

    def for_entry(selected_registry, entry_id):
        assert selected_registry is registry
        assert entry_id == "entry"
        return entries

    monkeypatch.setattr(cleanup.er, "async_entries_for_config_entry", for_entry)
    cleanup.remove_retired_entities(None, SimpleNamespace(entry_id="entry"), "GATEWAY")
    assert removed == ["number.old", "button.old"]
