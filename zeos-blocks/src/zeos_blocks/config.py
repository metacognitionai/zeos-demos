"""Settings resolve as: ``.env`` file < environment variable < command-line flag.

The same order the other ZEOS demos use, and the same reason: a key belongs in a file that
is gitignored, an override belongs in the environment, and what you typed just now should
win over both.

Stdlib only, and deliberately so. A dependency for eleven lines of parsing would be a
dependency the stub planner drags in for nothing, and the stub planner is what most people
run.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["ENV_FILENAME", "api_key_is_set", "load_env", "model"]

ENV_FILENAME = ".env"
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: What the Claude planner needs, under the SDK's own names, so a shell already set up for
#: Claude needs no file at all.
KEY = "ANTHROPIC_API_KEY"
MODEL = "ANTHROPIC_MODEL"


def load_env(path: Path | None = None) -> Path | None:
    """Load a ``.env`` into the environment. Returns the file read, or None.

    The current directory first and then the project root, so running from a checkout and
    running from anywhere else both work.
    """
    candidates = (
        [path] if path is not None else [Path.cwd() / ENV_FILENAME, PROJECT_ROOT / ENV_FILENAME]
    )
    found = next((p for p in candidates if p is not None and p.is_file()), None)
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
        # An empty value means "not configured". `.env.example` ships valueless keys, and
        # a copy of it must not blank out a variable the environment already set.
        if value and key not in os.environ:
            os.environ[key] = value
    return found


def api_key_is_set() -> bool:
    return bool(os.environ.get(KEY))


def model(default: str) -> str:
    return os.environ.get(MODEL) or default
