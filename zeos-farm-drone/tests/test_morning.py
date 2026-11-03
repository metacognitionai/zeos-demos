"""The canonical morning on the farm, asserted as journal properties.

Each test is one sentence of the story -- a finding interrupts the patrol, the close
look takes the zoom drone and the patrol carries on in the other, the flight rules stop
what no behaviour may do, the farmer's words become a job or a refusal -- stated as a
structural fact about the journal rather than as anything a behaviour said.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from zeos.core.events import (
    CompilationRefused,
    Disembodied,
    Embodied,
    Event,
    FaultRaised,
    GateAnswered,
    GateConsulted,
    Injected,
    JobSpawned,
    PipeWritten,
    ReportAttributed,
    UtteranceCompiled,
    VectorFired,
)
from zeos.core.ids import FaultKind, JobId, PrincipalId

from zeos_farm_drone.run import RunResult, run
from zeos_farm_drone.sim import COMMAND_PIPE
from zeos_farm_drone.story import check


@pytest.fixture(scope="module")
def result() -> RunResult:
    return run()


def of[E: Event](events: Sequence[Event], cls: type[E]) -> list[E]:
    return [e for e in events if isinstance(e, cls)]


def job_of(result: RunResult, name: str) -> JobId:
    return next(e.job for e in of(result.events, JobSpawned) if e.descriptor == name)


def flown_by(result: RunResult, name: str) -> list[str]:
    """The commands of one behaviour that reached a drone."""
    job = job_of(result, name)
    return [
        " ".join(e.text)
        for e in of(result.events, PipeWritten)
        if e.pipe == COMMAND_PIPE and e.job == job
    ]


def answers(result: RunResult, pipe: str) -> list[str]:
    return [" ".join(e.text) for e in of(result.events, PipeWritten) if e.pipe == pipe]


def test_the_story_holds(result: RunResult) -> None:
    failed = [v.claim for v in check(result) if not v.held]
    assert not failed


def test_dawn_starts_the_patrol_on_the_first_free_drone(result: RunResult) -> None:
    fired = of(result.events, VectorFired)[0]
    assert (fired.pipe, fired.handler) == ("clock.dawn", "patrol")
    first = of(result.events, Embodied)[0]
    assert (first.job, first.platform) == (job_of(result, "patrol"), "kestrel")


def test_a_finding_is_an_interrupt_the_patrol_raised_itself(result: RunResult) -> None:
    patrol = job_of(result, "patrol")
    events = list(result.events)
    fired = next(e for e in of(events, VectorFired) if e.pipe == "findings.vehicle")
    written = next(e for e in of(events, PipeWritten) if e.pipe == "findings.vehicle")
    assert written.job == patrol
    assert events.index(written) < events.index(fired)
    assert fired.handler == "vehicle-check"


def test_the_close_look_takes_the_drone_and_the_patrol_carries_on(result: RunResult) -> None:
    patrol = job_of(result, "patrol")
    events = list(result.events)
    taken = next(
        e for e in of(events, Disembodied) if e.job == patrol and "vehicle-check" in e.reason
    )
    assert taken.platform == "kestrel"
    back = next(e for e in of(events[events.index(taken) :], Embodied) if e.job == patrol)
    assert back.platform == "kestrel"
    # It carries on from the paddock after the one it was at, never observing twice.
    flown = flown_by(result, "patrol")
    assert flown.index("fly dam") > flown.index("fly creek")
    observed = [p for p, _ in result.sim.observed]
    assert len(observed) == len(set(observed))


def test_every_command_is_put_to_the_flight_rules(result: RunResult) -> None:
    consulted = [e.payload for e in of(result.events, GateConsulted)]
    landed = answers(result, str(COMMAND_PIPE))
    vetoed = [e for e in of(result.events, GateAnswered) if not e.allowed]
    assert len(consulted) == len(landed) + len(vetoed)


def test_the_flight_rules_stop_what_no_behaviour_may_do(result: RunResult) -> None:
    vetoes = {
        (g.payload, a.reason)
        for g, a in zip(
            of(result.events, GateConsulted), of(result.events, GateAnswered), strict=True
        )
        if not a.allowed
    }
    assert vetoes == {
        ("fly road", "road is outside the farm boundary"),
        ("climb 180", "180 m is above the 120 m ceiling"),
    }
    landed = answers(result, str(COMMAND_PIPE))
    assert "fly road" not in landed and "climb 180" not in landed


def test_a_veto_is_a_fault_the_behaviour_recovers_from(result: RunResult) -> None:
    vetoed = [e for e in of(result.events, FaultRaised) if e.fault is FaultKind.GATE]
    assert {e.job for e in vetoed} == {
        job_of(result, "vehicle-check"),
        job_of(result, "fire-watch"),
    }
    assert "fly boundary-west" in flown_by(result, "vehicle-check")
    assert "climb 120" in flown_by(result, "fire-watch")


def test_the_fire_takes_the_drone_at_an_action_boundary(result: RunResult) -> None:
    """The patrol is `at-action-boundary`, and every point along a flight is one: the
    fire takes the drone on its way to the dam, the patrol is told where it stopped,
    and the fire watch's first command is the next thing the drone does."""
    patrol, fire = job_of(result, "patrol"), job_of(result, "fire-watch")
    events = list(result.events)
    taken = next(e for e in of(events, Disembodied) if e.job == patrol and "fire-watch" in e.reason)
    assert taken.platform == "kestrel"
    told = next(
        e for e in of(events[events.index(taken) :], PipeWritten) if e.pipe == "drone.done.patrol"
    )
    assert " ".join(told.text) == "interrupted fly dam over creek"
    after = [e.job for e in of(events[events.index(taken) :], ReportAttributed)]
    assert after[0] == fire


def test_the_farmers_words_become_a_job_or_a_refusal(result: RunResult) -> None:
    """The words compile to a job the farmer owns, which raises the search's interrupt
    and does nothing else; the search itself is the kernel's, started by the vector."""
    compiled = of(result.events, UtteranceCompiled)
    assert [c.artifact for c in compiled] == ["spawning ask-find(item=motorbike)"]
    relay = next(e for e in of(result.events, JobSpawned) if e.descriptor == "ask-find")
    assert relay.owner == PrincipalId("farmer")
    raised = next(e for e in of(result.events, PipeWritten) if e.pipe == "requests.find")
    assert (raised.job, " ".join(raised.text)) == (relay.job, "item=motorbike")
    fired = next(e for e in of(result.events, VectorFired) if e.pipe == "requests.find")
    assert fired.handler == "find-item"
    search = next(e for e in of(result.events, JobSpawned) if e.descriptor == "find-item")
    assert search.owner == PrincipalId("kernel")
    refused = of(result.events, CompilationRefused)
    assert [r.reason for r in refused] == ["no-compilation-target"]
    assert result.replies[-1].startswith("cannot:")


def test_the_herd_the_farmer_expects_reaches_the_patrol(result: RunResult) -> None:
    """The calf check updates `herd.expected`; the patrol, which reads it, is told."""
    patrol = job_of(result, "patrol")
    notices = [
        " ".join(e.text)
        for e in of(result.events, Injected)
        if e.job == patrol and e.text[:1] == ("<RESUME>",)
    ]
    assert any("back-block=22 -> " in n and "back-block=24" in n for n in notices)


def test_no_behaviour_commands_a_drone_it_is_not_flying(result: RunResult) -> None:
    refused = [
        a
        for pipe in ("patrol", "vehicle", "livestock", "fire", "find")
        for a in answers(result, f"drone.done.{pipe}")
        if a.startswith("refused")
    ]
    assert not refused


def test_the_morning_replays_byte_for_byte(tmp_path: Path) -> None:
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    run(journal=first)
    run(journal=second)
    assert first.read_bytes() == second.read_bytes()
