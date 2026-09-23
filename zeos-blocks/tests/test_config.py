"""Where settings come from, and which wins.

`.env` file < environment variable < command-line flag. Each of those is a claim that can
be wrong in a way nobody notices until a run costs money against the wrong account, so all
three are pinned here.
"""

from __future__ import annotations

import pytest

from zeos_blocks import config


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    monkeypatch.delenv(config.KEY, raising=False)
    monkeypatch.delenv(config.MODEL, raising=False)


def write(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_file_sets_what_the_environment_does_not(tmp_path) -> None:
    assert config.load_env(write(tmp_path, "ANTHROPIC_API_KEY=from-the-file\n")) is not None
    assert config.api_key_is_set()


def test_the_environment_wins_over_the_file(tmp_path, monkeypatch) -> None:
    """A key exported for this shell is the one meant, whatever a file left lying about says."""
    monkeypatch.setenv(config.KEY, "from-the-environment")
    config.load_env(write(tmp_path, "ANTHROPIC_API_KEY=from-the-file\n"))
    assert config.model("x") == "x"
    import os

    assert os.environ[config.KEY] == "from-the-environment"


def test_a_valueless_key_does_not_blank_out_the_environment(tmp_path, monkeypatch) -> None:
    """`.env.example` ships `ANTHROPIC_API_KEY=` with nothing after it.

    Copying it and forgetting to fill it in must leave an exported key alone rather than
    unset it, or the file is a trap rather than a template.
    """
    monkeypatch.setenv(config.KEY, "exported")
    config.load_env(write(tmp_path, "ANTHROPIC_API_KEY=\n"))
    import os

    assert os.environ[config.KEY] == "exported"


def test_comments_and_blank_lines_are_skipped(tmp_path) -> None:
    config.load_env(write(tmp_path, "# a comment\n\nANTHROPIC_MODEL=claude-sonnet-5\n"))
    assert config.model("claude-opus-5") == "claude-sonnet-5"


def test_quotes_are_stripped(tmp_path) -> None:
    config.load_env(write(tmp_path, 'ANTHROPIC_MODEL="claude-opus-5"\n'))
    assert config.model("other") == "claude-opus-5"


def test_a_missing_file_is_not_an_error(tmp_path) -> None:
    """Most runs have no `.env` at all, because the stub planner needs nothing."""
    assert config.load_env(tmp_path / "nothing-here") is None
    assert not config.api_key_is_set()


def test_the_example_is_a_valid_file_that_configures_nothing(tmp_path) -> None:
    """Shipping a template that would set a bogus key is worse than shipping none."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    config.load_env(write(tmp_path, example.read_text(encoding="utf-8")))
    assert not config.api_key_is_set()
    # It does carry a default model, which is a real setting and safe to have.
    assert config.model("fallback") == "claude-opus-5"
