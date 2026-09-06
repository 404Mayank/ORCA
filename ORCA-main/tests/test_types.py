"""The generated frontend types must not drift from the Pydantic schemas.

CLAUDE.md: *frontend TypeScript types are generated from the Pydantic schemas.
Do not hand-write them; they will drift.* Generating them once is not enough --
the file is checked in, so it goes stale the moment someone edits a schema and
forgets to re-run the generator. This test is what makes the rule stick.

Fix a failure with:  python scripts/gen_types.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GENERATED = ROOT / "frontend" / "src" / "types.ts"


@pytest.mark.skipif(not GENERATED.exists(), reason="frontend not present")
def test_generated_types_are_current():
    before = GENERATED.read_text(encoding="utf-8")
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_types.py")],
        cwd=ROOT, check=True, capture_output=True,
    )
    after = GENERATED.read_text(encoding="utf-8")
    assert before == after, (
        "frontend/src/types.ts is stale relative to core/schemas/. "
        "Run: python scripts/gen_types.py"
    )


@pytest.mark.skipif(not GENERATED.exists(), reason="frontend not present")
def test_generated_types_are_never_any():
    """An `any` here would defeat the point: it is drift that type-checks."""
    text = GENERATED.read_text(encoding="utf-8")
    assert ": any" not in text
    assert "any[]" not in text
