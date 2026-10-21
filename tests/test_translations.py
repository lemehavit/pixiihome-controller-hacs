"""Translation catalog coverage tests."""

from __future__ import annotations

import json
from pathlib import Path

COMPONENT_DIR = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "pixiihome_controller"
)


def test_english_entity_catalog_covers_strings() -> None:
    """Every entity declared in strings.json must have an English runtime name."""
    source = json.loads((COMPONENT_DIR / "strings.json").read_text(encoding="utf-8"))
    english = json.loads(
        (COMPONENT_DIR / "translations" / "en.json").read_text(encoding="utf-8")
    )

    missing: dict[str, list[str]] = {}
    for platform, entities in source["entity"].items():
        translated = english.get("entity", {}).get(platform, {})
        absent = sorted(set(entities) - set(translated))
        if absent:
            missing[platform] = absent

    assert missing == {}
