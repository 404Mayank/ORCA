"""Load ``.env`` into the process environment. Tiny, dependency-free.

Existing environment variables win over the file, so a key exported in the
shell is never silently overridden by a stale ``.env``. Blank values in the
file are ignored, which lets ``.env.example`` be copied wholesale without every
empty line clobbering something.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_dotenv", "ROOT"]

ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path | None = None) -> int:
    """Load ``KEY=value`` lines. Returns how many variables were set."""
    env_path = path or ROOT / ".env"
    if not env_path.exists():
        return 0
    loaded = 0
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if not value or key in os.environ:
            continue
        os.environ[key] = value
        loaded += 1
    return loaded


load_dotenv()
