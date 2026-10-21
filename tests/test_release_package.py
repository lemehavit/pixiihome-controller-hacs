"""Check that the distributable has complete runtime assets and no private reports."""

from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

from scripts.build_release import build, validate


def test_release_archive_is_complete_private_data_free_and_reproducible(tmp_path):
    version = validate()
    archive = build(tmp_path, f"v{version}")
    digest = sha256(archive.read_bytes()).hexdigest()
    with ZipFile(archive) as release:
        names = set(release.namelist())
        assert {"manifest.json", "__init__.py", "brand/icon.png", "translations/en.json",
                "frontend/pixiihome-lovelace.js", "services.yaml", "LICENSE"} <= names
        assert all(not Path(name).is_absolute() and ".." not in Path(name).parts for name in names)
        assert not any(name.startswith(("docs/", "tests/", ".")) or "__pycache__" in name
                       for name in names)
        assert release.testzip() is None
    assert sha256(build(tmp_path, version).read_bytes()).hexdigest() == digest
