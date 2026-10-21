# Contributing

Use GitHub issues for bugs and focused pull requests for fixes. Describe the
observable problem, resulting behavior and relevant validation. Do not include
installation credentials, real Gateway identifiers or private field reports.

## Local checks

Use Python 3.14.2 or newer; CI tests against Home Assistant 2026.9.3 on Python 3.14.

```sh
python -m venv .venv
# Activate .venv for your shell.
python -m pip install -e '.[test]'
python -m ruff check .
python -m pyright
python -m pytest
python scripts/build_release.py --check
python scripts/build_release.py
```

The tests use fakes and the simulator. They must not contact a real Gateway,
broker or Home Assistant instance. Anonymized regression projections live under
`tests/fixtures/price_stop/`; they are not deployable recovery evidence or full
field reports. Source observations are intentionally not shipped.

Keep all runtime assets under `custom_components/pixiihome_controller/`, including
the frontend, translations and brand images. The release builder copies only
reviewed runtime extensions and adds LICENSE; it excludes tests, caches and reports.

## Hardware and ownership boundaries

Code review and offline tests do not authorize a physical battery operation.
Never add automatic live tests to CI. Preserve exact identity correlation,
external-service priority, journal persistence and conservative uncertainty handling.
An idle output must not be reclassified as proof of schedule deletion.

## Release process

1. Update manifest, `const.VERSION`, project version and changelog together.
2. Run CI, HACS validation and Hassfest without ignored checks.
3. Tag the reviewed commit `v<version>` and push the tag. Release CI reruns the
   offline checks and builds a draft GitHub release with `pixiihome_controller.zip`.
4. Review the archive, release notes, compatibility and validation results, then
   publish the full release. HACS discovers full releases; a tag alone is not a release.

The default HACS catalog is a separate process. A maintainer can submit this
public repository to `hacs/default` after validation and a full release. Custom
repository installation does not imply catalog acceptance.
