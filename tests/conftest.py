from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def load_json(relative: str):
    return json.loads((FIXTURES / relative).read_text(encoding="utf-8"))
