"""Somebody reaching into the workspace takes the machine away from the planner.

The thing being tested is a *scheduling* fact, so every assertion here is on the journal:
which vector fired, what was dispatched, what was preempted, what resumed. None of it is
on what any job said.
"""

from __future__ import annotations

from conftest import CASE
from zeos.core.events import JobPreempted, JobResumed, JobSpawned, VectorFired
from zeos.core.ids import JobState

from zeos_blocks.planner import StubPlanner
from zeos_blocks.session import Session

GOAL = "move the green block from stack 1 to stack 2"


def _run(bundle, *, hand_at: int, ticks: int = 200):
    """A hand, which is an instant rather than a span.

    The world changes and the doorbell rings in the same call. A drag on the page is a
    span only because a person takes time, and `Session.hold` covers that separately.
    """
    session = Session(bundle, StubPlanner())
    session.say(GOAL)
    state = None
    for tick in range(ticks):
        if tick == hand_at:
            jobs = session.kernel.sched.jobs()
            state = jobs[0].state if jobs else None
            session.disturb("g1", "s2")
        session.step()
    return session, state


def _of(session, kind):
    return [e for e in session.events if isinstance(e, kind)]


def test_the_case_declares_one_vector(bundle) -> None:
    assert [(str(v.source), str(v.handler), int(v.priority)) for v in bundle.vectors] == [
        ("table.disturbed", "noticed", 10)
    ]


def test_a_hand_fires_the_vector_and_dispatches_the_reflex(bundle) -> None:
    session, _ = _run(bundle, hand_at=7)
    assert [str(e.vector) for e in _of(session, VectorFired)] == ["hand-in-workspace"]
    assert "noticed" in [str(e.descriptor) for e in _of(session, JobSpawned)]


def test_a_hand_preempts_a_planner_that_is_running(bundle) -> None:
    """The one that matters.

    Preemption takes the machine from a job that is holding it, and a blocked job is not.
    There is nothing to sleep on here, so a live planner is a running planner and the
    machine is always taken off something.
    """
    session, state = _run(bundle, hand_at=7)
    assert state is JobState.RUNNING
    assert _of(session, JobPreempted), "a running planner should have been preempted"
    assert _of(session, JobResumed), "and then given the machine back"


def test_the_reflex_outranks_the_planner(bundle) -> None:
    spawned = {
        str(e.descriptor): int(e.priority) for e in _of(_run(bundle, hand_at=7)[0], JobSpawned)
    }
    assert spawned["noticed"] < spawned["stacker"]


def test_the_planner_finishes_after_being_interrupted(bundle) -> None:
    """Preemption is not cancellation: the goal is still reached."""
    session, _ = _run(bundle, hand_at=7)
    assert session.positions()["s2"].endswith("g1")


def test_the_reflex_has_nothing_to_wait_for(bundle) -> None:
    """By the time the reflex is dispatched the hand's move is already in world state.

    The actuator write goes in before the doorbell that fires the vector, so there is
    nothing left for the reflex to fetch. It could not usefully block anyway: a blocked
    job has given up the machine by definition, so blocking would hand the planner back
    the very machine the dispatch took from it.
    """
    noticed = bundle.descriptors["noticed"]
    assert noticed.pipes.resolve("stdin") is None
    assert [str(e.descriptor) for e in _of(_run(bundle, hand_at=7)[0], JobSpawned)].count(
        "noticed"
    ) == 1


# -- the hand's own interlock, which is the display's and not the kernel's ----


def test_a_hand_in_the_workspace_stops_the_clock(bundle) -> None:
    """Nothing advances while somebody is part way through a drag.

    This is not the scheduler and not the arm. It is the one thread that decides when the
    kernel's clock moves declining to move it, which is the honest place for it: how long
    a hand hovers over a table is wall-clock time, and the kernel has no opinion about
    wall-clock time.
    """
    session = Session(bundle, StubPlanner())
    session.say(GOAL)
    for _ in range(4):
        session.step()
    session.hold()
    moves = len(session.arm.log)
    events = len(session.events)
    for _ in range(50):
        assert session.step() is False
    assert len(session.arm.log) == moves, "the arm moved while a hand was in the workspace"
    assert len(session.events) == events, "and the kernel was asked nothing at all"
    assert session.busy, "the run is not over; it is waiting for a person"


def test_releasing_starts_it_again(bundle) -> None:
    """Every path that holds has to release, which is why the page sends a cancel on a
    drag that ended on nothing."""
    session = Session(bundle, StubPlanner())
    session.say(GOAL)
    session.hold()
    for _ in range(30):
        session.step()
    assert session.arm.log == []
    session.release()
    for _ in range(120):
        session.step()
    assert session.positions()["s2"].endswith("g1")


def test_no_descriptor_mentions_being_interrupted(bundle) -> None:
    """Interruption is the kernel's business, and the bodies are free to be about blocks.

    If this ever fails it means somebody has written the behaviour into the prose, where a
    model can decline it, instead of leaving it in `vectors.yaml` where it is enforced.
    """
    body = (CASE / "goals" / "stacker.md").read_text(encoding="utf-8").split("---\n", 2)[2]
    for word in ("interrupt", "preempt", "priority", "reflex", "vector"):
        assert word not in body.lower(), f"the stacker's body mentions {word!r}"
