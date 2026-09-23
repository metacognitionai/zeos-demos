"""The background goal: work nobody asked for, done when nothing else wants the machine.

This is the case that has two jobs in it, so it is the only place contention for the arm
can be tested at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from zeos.core.events import CompilationRefused, JobPreempted, JobResumed, JobSpawned
from zeos.descriptor.lint import Severity, lint
from zeos.descriptor.loader import load_case

from zeos_blocks.abi import BLOCKS
from zeos_blocks.planner import StubPlanner, tidiest, tidy_command
from zeos_blocks.session import Session
from zeos_blocks.world import Move, Table

CASE = Path(__file__).resolve().parents[1] / "cases" / "blocks-tidy"


@pytest.fixture
def tidy_bundle():
    return load_case(CASE)


def _run(bundle, *, say_at: int | None = None, ticks: int = 900):
    session = Session(bundle, StubPlanner())
    for tick in range(ticks):
        if tick == say_at:
            session.say("move the green block from stack 2 to stack 3")
        session.step()
    return session


# -- the tree ---------------------------------------------------------------


def test_it_lints_clean(tidy_bundle) -> None:
    findings = lint(
        tidy_bundle.descriptors,
        pipes=tidy_bundle.pipes,
        scripts=tidy_bundle.scripts,
        vectors=tidy_bundle.vectors,
        resources=tidy_bundle.resources,
        platforms=tidy_bundle.platforms,
        principals=tidy_bundle.principals,
        gates=tidy_bundle.gates,
        abi=BLOCKS,
    )
    assert [f.render() for f in findings if f.severity is Severity.ERROR] == []


def test_only_the_background_goal_boots(tidy_bundle) -> None:
    """Nobody asked for tidy, which is what makes it background work rather than a
    service. The stacker still exists only when somebody speaks."""
    assert [str(name) for name in tidy_bundle.boot] == ["tidy"]


def test_the_background_goal_holds_no_extra_authority(tidy_bundle) -> None:
    """Being low priority buys nothing and costs nothing. What a job may do is declared."""
    tidy = tidy_bundle.descriptors["tidy"]
    stacker = tidy_bundle.descriptors["stacker"]
    assert int(tidy.priority) > int(stacker.priority)
    assert {str(c.pipe) for c in tidy.capabilities} == {str(c.pipe) for c in stacker.capabilities}


def test_both_jobs_share_one_arm(tidy_bundle) -> None:
    """One table, one arm, and two jobs wanting it.

    Nothing on that pipe is addressed to anybody, so there is nothing to mis-route. A
    device that answered would need a pipe per job: two jobs reading one reply pipe race
    for each other's answers, and routing by pipe is what settles it.
    """
    tidy = tidy_bundle.descriptors["tidy"]
    stacker = tidy_bundle.descriptors["stacker"]
    assert str(tidy.pipes.resolve("arm")) == str(stacker.pipes.resolve("arm")) == "arm.requests"


def test_the_arm_says_whose_move_it_was(tidy_bundle) -> None:
    """The report is shared because the arm is, so it has to name the asker.

    Without the name the stacker reads a refusal that was tidy's and reasons about a move
    it never made.
    """
    session = _run(tidy_bundle, say_at=8, ticks=400)
    assert session.kernel.world.get("arm.last").startswith(("stacker:", "tidy:"))


# -- what it does ------------------------------------------------------------


def test_left_alone_it_consolidates_the_table(tidy_bundle) -> None:
    session = _run(tidy_bundle)
    occupied = [p for p, blocks in Table.of(session.positions()).stacks.items() if blocks]
    assert len(occupied) == 1


def test_it_loses_the_machine_when_the_operator_speaks(tidy_bundle) -> None:
    """The point of the whole case, and it is a scheduling fact rather than a courtesy.

    Nothing in either body mentions the other. `tidy` does not check whether anybody is
    waiting and `stacker` does not ask for the machine; one number in each frontmatter
    settles it.
    """
    session = _run(tidy_bundle, say_at=8)
    spawned = [str(e.descriptor) for e in session.events if isinstance(e, JobSpawned)]
    assert spawned == ["tidy", "stacker"]
    assert [e for e in session.events if isinstance(e, JobPreempted)]
    assert [e for e in session.events if isinstance(e, JobResumed)]


def test_it_is_resumed_and_finishes_the_job(tidy_bundle) -> None:
    """Preemption is not cancellation. The table still ends up tidy."""
    session = _run(tidy_bundle, say_at=8)
    occupied = [p for p, blocks in Table.of(session.positions()).stacks.items() if blocks]
    assert len(occupied) == 1


def test_every_move_asked_for_was_made(tidy_bundle) -> None:
    """Nothing is outstanding in the arm, ever, because a move is applied in the step it is
    asked for. A device that took time would need a queue, and a queue is a place requests
    get lost, so this is the assertion that would guard one."""
    session = _run(tidy_bundle, say_at=8)
    assert session.arm.log, "the arm should have moved something"
    assert not session.busy
    assert not session.kernel.pipes.get("arm.requests").readable


# -- the consolidation itself, without a kernel ------------------------------


def test_a_table_on_one_position_is_already_tidy() -> None:
    assert tidiest(Table.of({"s1": "a1,b1", "s2": "-", "s3": "-"})) is None
    assert tidy_command(Table.of({"s1": "a1", "s2": "-"})).startswith("write stdout")


def test_it_gathers_towards_the_largest_stack() -> None:
    """Towards, never away. Moving towards the smallest would undo itself for ever."""
    table = Table.of({"s1": "r1", "s2": "g1,g2,g3", "s3": "b1,b2"})
    assert tidiest(table) == "s2"
    assert tidy_command(table) == "write arm block=b2 to=s2;"


def test_consolidation_terminates() -> None:
    table = Table.of({"s1": "r1,y1,r2", "s2": "g1,o1,g2", "s3": "b1,p1,b2", "s4": "-"})
    for _ in range(50):
        command = tidy_command(table)
        if command.startswith("write stdout"):
            break
        fields = dict(part.split("=") for part in command.split() if "=" in part)
        table.apply(Move(fields["block"], fields["to"].rstrip(";")))
    else:
        pytest.fail("consolidation did not terminate")
    assert len([p for p, blocks in table.stacks.items() if blocks]) == 1


# -- how it is asked for -----------------------------------------------------


def test_nothing_anybody_can_type_reaches_it(tidy_bundle) -> None:
    """It declares no phrasing at all, which is what makes it unaddressable.

    That is a stronger property than it looks, and it is the reason the Tidy button is a
    button. The stacker's one phrasing matches every sentence, so a phrasing here would
    have to win a race against it -- and the compiler takes the *first* match from a
    table sorted by descriptor name, with no check that one pattern subsumes another. A
    standing instruction whose reachability depended on sorting before `stacker` would be
    a standing instruction that stopped being reachable when somebody renamed something.
    """
    assert tidy_bundle.descriptors["tidy"].utterances == ()
    session = Session(tidy_bundle, StubPlanner())
    for tick in range(300):
        if tick == 200:
            session.say("tidy up")
        session.step()
    # It compiles -- everything does -- but to the stacker, which does not understand it.
    spawned = [str(e.descriptor) for e in session.events if isinstance(e, JobSpawned)]
    assert spawned.count("tidy") == 1, "booted once, and not asked for by typing"
    assert "stacker" in spawned
    assert [e for e in session.events if isinstance(e, CompilationRefused)] == []


def test_the_button_asks_for_it_by_name(tidy_bundle) -> None:
    """A control rather than a sentence: a descriptor name, no arguments, nothing to mean
    something else. It boots once and exits when the table is tidy, so this is how it
    comes back after somebody has moved things about."""
    session = Session(tidy_bundle, StubPlanner())
    for tick in range(700):
        if tick == 400:
            session.start_job("tidy")
        session.step()
    spawned = [
        (str(e.descriptor), int(e.priority)) for e in session.events if isinstance(e, JobSpawned)
    ]
    assert spawned.count(("tidy", 80)) == 2, "booted once, then asked for once"


def test_the_button_is_no_more_authority_than_speaking(tidy_bundle) -> None:
    """Spawned as the operator, not as the kernel.

    A job started by the page inherits the page's principal, so it is clamped to the same
    ceiling and holds the same capabilities a job somebody spoke into being would. A
    button that spawned at kernel authority would be a hole with a label on it.
    """
    session = Session(tidy_bundle, StubPlanner())
    session.start_job("tidy")
    for _ in range(40):
        session.step()
    asked = [e for e in session.events if isinstance(e, JobSpawned) and str(e.descriptor) == "tidy"]
    assert [str(e.owner) for e in asked] == ["kernel", "operator"]
    assert all(int(e.priority) == 80 for e in asked), "asking does not make it more urgent"


def test_the_background_goal_never_preempts_the_job_you_asked_for(tidy_bundle) -> None:
    """Asking for tidy while a stacker runs reads like tidy taking over. It is not.

    Preemption only ever goes the other way, and this asserts the direction rather than
    the appearance.
    """
    session = Session(tidy_bundle, StubPlanner())
    for _ in range(600):
        session.step()
    session.say("move the blue block from stack 3 to stack 1")
    for tick in range(300):
        if tick == 30:
            session.start_job("tidy")
        session.step()

    by_priority = {e.job: int(e.priority) for e in session.events if isinstance(e, JobSpawned)}
    for event in session.events:
        if isinstance(event, JobPreempted):
            assert by_priority[event.by_job] < by_priority[event.job], (
                "a job was preempted by one less urgent than itself"
            )


def test_the_page_names_what_is_alive(tidy_bundle) -> None:
    """Two jobs alive at once is the state worth showing, and the only one this case has.

    Names only: whether a job is waiting would matter in a workspace where jobs block, and
    nothing here does, so the word would be a label that is never true.
    """
    from zeos_blocks.web.server import BlocksServer

    server = BlocksServer(CASE, StubPlanner())
    try:
        seen = set()
        for _ in range(200):
            server.session.step()
        server.session.say("move the blue block from stack 3 to stack 1")
        for tick in range(300):
            if tick == 30:
                server.session.start_job("tidy")
            server.session.step()
            seen.add(tuple(sorted(server.activity()["live"])))
    finally:
        server.session.close()

    assert ("stacker", "tidy") in seen, "both alive at once is the state worth showing"
