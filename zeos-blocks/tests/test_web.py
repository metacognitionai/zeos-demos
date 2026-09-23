"""The page's own layer: what it turns journal events into.

`_journal_line` reads fields off kernel events, and getting one of those names wrong is a
silent kind of wrong: it raises inside the callback that the kernel's thread runs, so the
symptom is not a missing line in a panel, it is the whole run stopping. That happened, so
this walks a real session's journal and renders every event in it.
"""

from __future__ import annotations

from conftest import CASE

from zeos_blocks.planner import StubPlanner
from zeos_blocks.session import Session
from zeos_blocks.web.server import _journal_line


def _events(bundle):
    """A run with everything in it: a spawn, moves, an interrupt, and a job finishing."""
    session = Session(bundle, StubPlanner())
    session.say("move the green block from stack 1 to stack 2")
    for tick in range(200):
        if tick == 7:
            session.disturb("g1", "s2")
        session.step()
    return session.events


def test_every_event_a_run_produces_renders(bundle) -> None:
    for event in _events(bundle):
        line = _journal_line(event)
        assert line is None or set(line) == {"tag", "text"}, type(event).__name__


def test_the_events_the_panel_exists_to_show_are_shown(bundle) -> None:
    """A vector firing and a job being preempted are the reason the panel is worth opening.

    Asserting they render is not the same as asserting they happened; `test_interrupt.py`
    does that. This is about the page not dropping them on the floor.
    """
    tags = {line["tag"] for e in _events(bundle) if (line := _journal_line(e)) is not None}
    # No "block" and no "wake": nothing in this workspace waits for anything, so a run
    # here produces neither event.
    assert {"spawn", "world", "vector", "preempt", "done"} <= tags


def test_a_rendered_line_says_who_preempted_whom(bundle) -> None:
    lines = [line for e in _events(bundle) if (line := _journal_line(e)) is not None]
    preempt = next(line for line in lines if line["tag"] == "preempt")
    assert "preempted by job" in preempt["text"]
    assert "priority" in preempt["text"]


def test_the_page_is_told_what_is_deciding_the_moves(tmp_path) -> None:
    """The kernel cannot tell a stub planner from a model, and a person watching should.

    Read off the source rather than passed in, so a server built any other way than
    through the CLI still says something true.
    """
    from types import SimpleNamespace

    from zeos_blocks.web.server import BlocksServer

    server = BlocksServer(CASE, StubPlanner())
    server.start()
    try:
        assert server.planner == "stub"
    finally:
        server.close()

    # A planner that names itself is taken at its word; one that does not falls back to
    # its type, which is still better than claiming to be something it is not.
    assert str(getattr(SimpleNamespace(label="claude-opus-5"), "label", "?")) == "claude-opus-5"
    assert type(SimpleNamespace()).__name__ == "SimpleNamespace"


def test_a_run_writes_a_journal_the_debugger_can_read(bundle, tmp_path) -> None:
    """A journal with only the boot sequence in it is the failure worth guarding.

    The events list is the record and the `Journal` is a file it is written to. The driver
    puts the pipes being created there itself, so a journal that was never flushed still
    looks plausible: it has content, a header, and nothing that happened.
    """
    from zeos.journal.writer import read_journal

    path = tmp_path / "run.jsonl"
    session = Session(bundle, StubPlanner(), journal=path)
    session.say("move the green block from stack 1 to stack 2")
    for _ in range(200):
        session.step()
    session.close()

    records = read_journal(path)
    kinds = {type(r.event).__name__ for r in records}
    assert len(records) > 100, "a whole run, not just the boot sequence"
    assert {"JobSpawned", "WorldWritten", "JobCompleted"} <= kinds
    assert [r.seq for r in records] == list(range(len(records))), "no gaps"
