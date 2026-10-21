"""Validate metadata and build a runtime-only Home Assistant release archive."""

from __future__ import annotations

import argparse
import ast
import json
import re
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/pixiihome_controller"
ALLOWED = {".py", ".json", ".yaml", ".js", ".png"}
ASSET = "pixiihome_controller.zip"


def validate(expected_version: str | None = None) -> str:
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    constants = ast.parse((COMPONENT / "const.py").read_text(encoding="utf-8"))
    version = next(ast.literal_eval(node.value) for node in constants.body
                   if isinstance(node, ast.Assign)
                   and any(isinstance(target, ast.Name) and target.id == "VERSION"
                           for target in node.targets))
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Use a full three-part release version")
    if manifest["version"] != version or project["project"]["version"] != version:
        raise ValueError("Manifest, project and diagnostics versions differ")
    if expected_version and expected_version.removeprefix("v") != version:
        raise ValueError("Tag differs from the integration version")
    for key in ("domain", "name", "version", "documentation", "issue_tracker", "codeowners"):
        if not manifest.get(key):
            raise ValueError(f"Required manifest metadata missing: {key}")
    if not hacs.get("homeassistant") or not hacs.get("name"):
        raise ValueError("HACS compatibility metadata missing")
    for name in ("manifest.json", "strings.json", "translations/en.json", "services.yaml",
                 "frontend/pixiihome-lovelace.js", "brand/icon.png"):
        if not (COMPONENT / name).is_file():
            raise ValueError(f"Required runtime asset missing: {name}")
    if not (ROOT / "LICENSE").is_file():
        raise ValueError("License missing")
    if not re.search(rf"^## {re.escape(version)}\b", (ROOT / "CHANGELOG.md").read_text(), re.M):
        raise ValueError("Changelog missing current version")
    return version


def build(output: Path, expected_version: str | None = None) -> Path:
    validate(expected_version)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / ASSET
    files = sorted(path for path in COMPONENT.rglob("*")
                   if path.is_file() and "__pycache__" not in path.parts
                   and not any(part.startswith(".") for part in path.relative_to(COMPONENT).parts)
                   and path.suffix in ALLOWED)
    # Fixed timestamps/modes make artifacts reproducible on Windows and Linux.
    with ZipFile(archive, "w", compression=ZIP_DEFLATED, compresslevel=9) as target:
        for path in [*files, ROOT / "LICENSE"]:
            name = "LICENSE" if path.name == "LICENSE" else path.relative_to(COMPONENT).as_posix()
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            target.writestr(info, path.read_bytes())
    return archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--version", help="Expected version or v-prefixed tag")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    if args.check:
        print(f"Metadata OK: {validate(args.version)}")
    else:
        print(build(args.output, args.version))
