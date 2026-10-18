"""The atrium morning, asserted as journal properties.

Each test is one sentence of the story -- the coffee outranks the tidying, the tidying
fills the brew, the alarm takes the body mid-delivery, the delivery resumes knowing
where the coffee went -- stated as a structural fact about the journal rather than as
anything a behaviour said.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from zeos.core.events import (
    Decoded,
    Disembodied,
    Embodied,
    Event,
    FaultRaised,
    JobCompleted,
    JobResumed,
    JobSpawned,
    PipeWritten,
    VectorFired,
    WorldWritten,
)
from zeos.core.ids import DescriptorName, JobId, ObjectName, ResumeKind

from zeos_stretch.run import RunResult, run
from zeos_stretch.vla import COMMAND_PIPE


@pytest.fixture(scope="module")
def result() -> RunResult:
    return run()


def of[E: Event](events: Sequence[Event], cls: type[E]) -> list[E]:
    return [e for e in events if isinstance(e, cls)]


def job_of(result: RunResult, name: str) -> JobId:
    return next(e.job for e in of(result.events, JobSpawned) if e.descriptor == name)


def commands(result: RunResult, name: str) -> list[str]:
    job = job_of(result, name)
    return [
        " ".join(e.text)
        for e in of(result.events, PipeWritten)
        if e.pipe == COMMAND_PIPE and e.job == job
    ]


def dirty(result: RunResult, name: str) -> list[dict[str, tuple[str, str]]]:
    job = job_of(result, name)
    return [
        {str(d.obj): (d.before, d.after) for d in e.dirty}
        for e in of(result.events, JobResumed)
        if e.job == job and e.resume_kind is ResumeKind.DIRTY
    ]


def test_the_morning_ends_with_everything_done(result: RunResult) -> None:
    world = result.kernel.world
    assert world.get(ObjectName("table.items")) == "nothing"
    assert world.get(ObjectName("basket.contents")) == "apple banana orange pear"
    assert world.get(ObjectName("tray.contents")) == "mug glass teacup saucer"
    assert world.get(ObjectName("coffee.location")) == "desk-7"
    assert world.get(ObjectName("hotplate.power")) == "off"
    assert not of(result.events, FaultRaised)
    finished = [e.job for e in of(result.events, JobCompleted)]
    order = [job_of(result, n) for n in ("brew-coffee", "smoke-response", "deliver-coffee")]
    assert finished == [*order, job_of(result, "tidy-atrium")], "tidying finishes last"


def test_each_interrupt_starts_its_handler(result: RunResult) -> None:
    fired = [(str(e.pipe), str(e.handler)) for e in of(result.events, VectorFired)]
    assert fired == [
        ("requests.coffee", "brew-coffee"),
        ("coffee.ready", "deliver-coffee"),
        ("sensors.smoke", "smoke-response"),
    ]


def test_the_coffee_takes_the_body_from_the_tidying_at_its_boundary(result: RunResult) -> None:
    tidy = job_of(result, "tidy-atrium")
    first = next(e for e in of(result.events, Disembodied) if e.job == tidy)
    assert "brew-coffee" in first.reason, "the coffee is what takes the body"
    wore = next(e for e in of(result.events, Embodied) if e.job == tidy)
    assert wore.release_policy == "at-action-boundary"
    # The item in hand is put away before the coffee's first command runs.
    lines = [line.text for line in result.body.reports]
    assert lines.index("stretch ✓ put mug tray") < next(
        i for i, t in enumerate(lines) if "navigate coffee-machine" in t
    )


def test_the_tidying_fills_the_brew(result: RunResult) -> None:
    """The body is free while the coffee brews, so the tidying gets it back."""
    lines = [line.text for line in result.body.reports]
    brewed = lines.index("stretch ✓ brew")
    ready = lines.index("coffee machine: coffee ready")
    assert any("put " in t for t in lines[brewed:ready])


def test_the_alarm_takes_the_body_mid_delivery(result: RunResult) -> None:
    deliver, smoke = job_of(result, "deliver-coffee"), job_of(result, "smoke-response")
    lost = next(e for e in of(result.events, Disembodied) if e.job == deliver)
    assert "smoke-response" in lost.reason, "the alarm is what takes the body"
    took = next(e for e in of(result.events, Embodied) if e.job == smoke)
    # Preemption is promised within a token boundary: nothing is decoded between the
    # delivery losing the body and the alarm taking it. (Tokens may be injected meanwhile:
    # the body's report of where it stopped refreshes every status line that maps it.)
    between = result.events[result.events.index(lost) : result.events.index(took)]
    assert not of(between, Decoded)
    answers = [
        " ".join(e.text) for e in of(result.events, PipeWritten) if e.pipe == "stretch.done.deliver"
    ]
    assert answers[2].startswith("interrupted navigate desk-7"), "the move is cut short"
    assert commands(result, "smoke-response") == [
        "place coffee",
        "navigate hotplate",
        "press cutoff",
    ]


def test_the_delivery_resumes_knowing_where_the_coffee_went(result: RunResult) -> None:
    [notice] = dirty(result, "deliver-coffee")
    assert notice["coffee.location"] == ("gripper", "corridor")
    assert notice["stretch.holding"] == ("coffee", "nothing")
    assert notice["stretch.position"][1] == "hotplate"


def test_nobody_is_told_about_its_own_effects(result: RunResult) -> None:
    """Every change named in a resume notice was caused by some other job or device."""
    causes: dict[tuple[str, str], set[JobId | None]] = {}
    for write in of(result.events, WorldWritten):
        causes.setdefault((str(write.obj), write.after), set()).add(write.job)
    for name in ("tidy-atrium", "brew-coffee", "deliver-coffee", "smoke-response"):
        me = job_of(result, name)
        for notice in dirty(result, name):
            for obj, (_, after) in notice.items():
                assert causes[(obj, after)] != {me}, f"{name} was told about its own {obj}"


def test_the_tidying_is_told_where_the_body_went(result: RunResult) -> None:
    assert [n["stretch.position"] for n in dirty(result, "tidy-atrium")] == [
        ("table", "coffee-machine"),
        ("table", "desk-7"),
    ]


def test_the_tidying_never_gives_up_the_body_holding_something(result: RunResult) -> None:
    """`put` goes from gripper empty to gripper empty, so the tidying's boundary is a
    moment its hands are free: whoever takes the body next never inherits a banana."""
    tidy = job_of(result, "tidy-atrium")
    holding = "nothing"
    last_wearer: JobId | None = None
    handed_over: list[str] = []
    for event in result.events:
        if isinstance(event, WorldWritten) and event.obj == ObjectName("stretch.holding"):
            holding = event.after
        if isinstance(event, Disembodied):
            last_wearer = event.job
        if isinstance(event, Embodied) and last_wearer == tidy and event.job != tidy:
            handed_over.append(holding)
    assert len(handed_over) == 2, "the coffee takes the body from the tidying twice"
    assert set(handed_over) == {"nothing"}


def test_same_morning_same_journal(tmp_path: Path) -> None:
    first, second = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    run(journal=first)
    run(journal=second)
    assert first.read_bytes() == second.read_bytes()


def test_without_the_alarm_nothing_is_put_down(tmp_path: Path) -> None:
    quiet = tmp_path / "events.jsonl"
    quiet.write_text('{"at_ns": 8000000000, "pipe": "requests.coffee", "text": "desk-7"}\n')
    calm = run(events=quiet)
    assert DescriptorName("smoke-response") not in {
        e.descriptor for e in of(calm.events, JobSpawned)
    }
    assert calm.kernel.world.get(ObjectName("hotplate.power")) == "on"


def test_every_claim_of_the_story_holds(result: RunResult) -> None:
    from zeos_stretch.story import check

    failed = [v.claim for v in check(result) if not v.held]
    assert not failed


def test_every_job_s_state_record_is_unbroken(result: RunResult) -> None:
    """Each ``job.state`` starts where the job's previous one ended: the page folds these
    into what each behaviour is doing, and a gap shows a waiting job as running."""
    from zeos.core.events import JobStateChanged

    last: dict[JobId, str] = {}
    for event in of(result.events, JobStateChanged):
        if event.job in last:
            assert event.from_state == last[event.job], f"job {event.job} skipped a move"
        last[event.job] = event.to_state
