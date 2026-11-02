"""Settings resolve as: ``.env`` file < environment variable < command-line flag.

The order the other ZEOS demos use: a key belongs in a file that is gitignored, an
override belongs in the environment, and what was typed just now wins over both.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["load_env", "model"]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODEL = "ANTHROPIC_MODEL"


def load_env() -> Path | None:
    """Load a ``.env`` into the environment, from the current directory or else the
    project root. Returns the file read, or None."""
    candidates = [Path.cwd() / ".env", PROJECT_ROOT / ".env"]
    found = next((p for p in candidates if p.is_file()), None)
    if found is None:
        return None
    for raw in found.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key, value = key.strip(), value.strip().strip("'\"")
        # An empty value is "not configured": a copy of `.env.example` must not blank out
        # a variable the environment already set.
        if value and key not in os.environ:
            os.environ[key] = value
    return found


def model(flag: str | None, default: str) -> str:
    return flag or os.environ.get(MODEL) or default
