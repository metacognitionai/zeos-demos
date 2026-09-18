"""One way to build a session for a test, since every test file wants the same one.

The stub model is what the `script:` tapes used to be: no key, no network, and the same
answer twice. Determinism now comes from the control flow being Python -- the only thing
left that could vary between two runs is what the model says, and the stub says one thing.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from zeos.descriptor.loader import CaseBundle, load_case

from zeos_chat.build import build_session
from zeos_chat.llm import Ask, LlmAdapter, StubModel
from zeos_chat.session import Session

CASE = Path(__file__).resolve().parents[1] / "cases" / "chat-scripted"


@pytest.fixture(scope="session")
def case() -> Path:
    return CASE


@pytest.fixture
def bundle() -> CaseBundle:
    return load_case(CASE)


@pytest.fixture
def make_session() -> Callable[..., tuple[Session, LlmAdapter, object]]:
    """Build a session on the real case, with a model of the test's choosing."""

    def build(
        model: str | Callable[[Ask], str] = "stub", **kwargs: Any
    ) -> tuple[Session, LlmAdapter, object]:
        return build_session(load_case(CASE), model=model, **kwargs)

    return build


@pytest.fixture
def run() -> Callable[..., None]:
    """Turn a session until it settles. A fixture rather than an import, because `tests`
    is not a package and need not become one."""
    return _run


def _run(session: Session, adapter: LlmAdapter, *, steps: int = 400) -> None:
    """Turn the kernel until it is quiescent *and* the model has nothing outstanding.

    The second half is the new part: a job parked on a reply leaves nothing runnable, which
    is the point of putting the model behind a pipe, so "no job ran" no longer means the
    conversation is over.
    """
    import time

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if session.step():
            continue
        if not adapter.in_flight:
            return
        # Nothing runnable and the model still owes an answer: wait for the device rather
        # than spinning, which is what the real loop does too.
        time.sleep(0.002)


def answers(**by_descriptor: str) -> StubModel:
    return StubModel(by_descriptor)


def texts(events: Sequence[Any], pipe: str) -> list[str]:
    from zeos.core.events import PipeWritten

    return [" ".join(e.text) for e in events if isinstance(e, PipeWritten) and str(e.pipe) == pipe]
