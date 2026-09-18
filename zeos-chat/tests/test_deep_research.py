"""The long job: dispatched by the conversation, and running underneath it.

What is worth holding here is not that research happens -- a stub says whatever it is
told to. It is that the conversation *hands the work off and stays available*, that the
child learns what it is for without being passed anything, and that the kernel is what
permits the dispatch rather than a check in a Python function.
"""

from __future__ import annotations

from pathlib import Path

from zeos.core.events import FaultRaised, JobSpawned, PipeWritten
from zeos.core.ids import JobState, PipeName
from zeos.descriptor.loader import load_case

from zeos_chat.build import build_session
from zeos_chat.jobs import research_subject, status_of, wants_research

CASE = Path(__file__).resolve().parents[1] / "cases" / "chat-scripted"
MESSAGES = PipeName("user.messages")
ARRIVALS = PipeName("user.arrivals")
TASK = PipeName("actuators.task")


def _run(message: str, ticks: int = 200) -> tuple[object, list[str]]:
    said: list[str] = []
    session, _, _ = build_session(load_case(CASE), on_reply=lambda p, t: said.append(t))
    session.boot()
    session.deliver(MESSAGES, message)
    session.deliver(ARRIVALS, "message")
    for _ in range(ticks):
        session.step()
    return session, said


def _spawned(session) -> list[str]:  # type: ignore[no-untyped-def]
    return [str(e.descriptor) for e in session.events if isinstance(e, JobSpawned)]


# -- recognising the request ------------------------------------------------


def test_the_long_job_is_asked_for_in_words_and_not_by_a_button() -> None:
    assert wants_research("research the history of Kyoto temples")
    assert wants_research("can you look into Japanese rail passes")
    assert not wants_research("name three temples in Kyoto")
    assert not wants_research("what is the weather")


def test_the_subject_loses_the_instruction_that_framed_it() -> None:
    """A status line reading "looking into research the history of Kyoto" quotes the
    instruction back instead of naming the work."""
    assert research_subject("research the history of Kyoto") == "the history of Kyoto"
    assert research_subject("please look into rail passes") == "please rail passes"
    assert research_subject("do some research") == "do some research", (
        "a bare ask still has a subject"
    )
    assert research_subject("name three temples") == ""


# -- the dispatch -----------------------------------------------------------


def test_the_conversation_spawns_the_long_job(caplog) -> None:  # type: ignore[no-untyped-def]
    session, _ = _run("research the history of Kyoto temples")
    assert "deep-research" in _spawned(session)
    assert not [e for e in session.events if isinstance(e, FaultRaised)], "the spawn faulted"


def test_an_ordinary_question_does_not_spawn_anything() -> None:
    """The control. A service started on every message would be a service nobody asked
    for, running at the one priority nothing else is below."""
    session, _ = _run("name three temples in Kyoto")
    assert "deep-research" not in _spawned(session)


def test_the_person_is_answered_before_the_research_reports() -> None:
    """The whole arrangement, as an ordering rather than a stopwatch: the acknowledgement
    is composed in Python and needs no model, so it reaches the person first and the long
    job's findings arrive behind it."""
    _, said = _run("research the history of Kyoto temples")

    assert said, "the person was told nothing at all"
    assert "Looking into" in said[0], f"the first thing said was {said[0]!r}"
    assert "the history of Kyoto temples" in said[0]
    findings = next(i for i, line in enumerate(said) if "what I found" in line)
    assert findings > 0, "the findings arrived before the acknowledgement"


def test_the_conversation_is_listening_again_immediately() -> None:
    """Handing work off must not cost the conversation its turn -- it parks back on the
    person rather than waiting for the job it started."""
    session, _ = _run("research the history of Kyoto temples")
    converse = next(j for j in session.kernel.sched.jobs() if str(j.name) == "converse")
    assert converse.state is JobState.BLOCKED
    assert str(converse.blocked_on) == "user.messages", f"parked on {converse.blocked_on}"


# -- what the child knows, and what it leaves behind ------------------------


def test_the_child_reads_its_brief_out_of_the_world(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A spawn carries a descriptor name and nothing else. The subject reaches the child
    through `session.topic`, which the kernel keeps current in its window -- so it needs
    no argument, and the brief survives whatever the pager does to the rest of its
    context."""
    session, _ = _run("research the history of Kyoto temples")
    written = [
        " ".join(e.text) for e in session.events if isinstance(e, PipeWritten) and e.pipe == TASK
    ]

    assert written, "the child never recorded what it was doing"
    assert "the history of Kyoto temples" in written[0], f"the brief did not arrive: {written[0]}"


def test_the_pending_task_is_cleared_when_the_job_ends() -> None:
    """A job that finishes leaving "looking into X" in the world tells the conversation it
    is still working, for ever."""
    session, _ = _run("research the history of Kyoto temples")
    written = [
        " ".join(e.text) for e in session.events if isinstance(e, PipeWritten) and e.pipe == TASK
    ]
    assert written[-1] == "none", f"left behind: {written}"


def test_a_status_region_is_read_back_from_the_window() -> None:
    """Last occurrence wins: a status region is rewritten in place, so where an older copy
    is still visible the current one is the later of the two."""
    window = (
        "<STATUS session.topic> an old subject </STATUS> read stdin; "
        "<STATUS session.topic> the current subject </STATUS>"
    )
    assert status_of(window, "session.topic") == "the current subject"
    assert status_of(window, "session.pending_task") == ""


# -- the contrast the arrangement exists to show ----------------------------


def test_the_conversation_is_answered_while_the_research_is_still_running() -> None:
    """This is the demonstration, and it is the one thing a chat loop cannot do.

    The long job is parked on a model that will not answer yet. Meanwhile a person asks
    an ordinary question and gets an ordinary answer. Nothing schedules that: the research
    job is `JobBlocked` on a device, so it is not holding the machine, and at priority 90
    it would lose it anyway.
    """
    import threading

    from zeos_chat.llm import Ask

    finish_research = threading.Event()

    def model(ask: Ask) -> str:
        if ask.descriptor == "deep-research":
            finish_research.wait(timeout=120)
            return "the long answer"
        return "the short answer"

    said: list[str] = []
    session, adapter, _ = build_session(
        load_case(CASE), model=model, on_reply=lambda p, t: said.append(t)
    )
    session.boot()
    session.deliver(MESSAGES, "research the history of Kyoto temples")
    session.deliver(ARRIVALS, "message")
    for _ in range(400):
        session.step()
        if adapter.in_flight:
            break
    assert adapter.in_flight, "the research never reached the model"

    # Now interrupt the arrangement with an ordinary question.
    session.deliver(MESSAGES, "and what is the weather")
    session.deliver(ARRIVALS, "message")
    for _ in range(4000):
        session.step()
        if any("the short answer" in line for line in said):
            break

    assert any("the short answer" in line for line in said), (
        f"the conversation was not answered while research was outstanding: {said}"
    )
    assert not any("the long answer" in line for line in said), (
        "the research finished, so this proves nothing about running underneath"
    )

    finish_research.set()
    for _ in range(4000):
        session.step()
        if any("the long answer" in line for line in said):
            break
    assert any("the long answer" in line for line in said), "the research never reported"


def test_the_findings_are_not_announced_before_there_are_any() -> None:
    """Written up front, the heading said "Here is what I found on X." and was followed by
    several minutes of nothing while the model thought. Measured: the acknowledgement
    landed at 0.8s and the first word of the findings at 274s."""
    import threading

    from zeos_chat.llm import Ask

    release = threading.Event()

    def model(ask: Ask) -> str:
        if ask.descriptor == "deep-research":
            release.wait(timeout=120)
            return "what it found"
        return "an answer"

    said: list[str] = []
    session, adapter, _ = build_session(
        load_case(CASE), model=model, on_reply=lambda p, t: said.append(t)
    )
    session.boot()
    session.deliver(MESSAGES, "research the history of Kyoto")
    session.deliver(ARRIVALS, "message")
    for _ in range(400):
        session.step()
        if adapter.in_flight:
            break
    for _ in range(200):
        session.step()

    assert not any("what I found" in line for line in said), (
        f"the findings were announced before the model had written any: {said}"
    )
    release.set()
    for _ in range(4000):
        session.step()
        if any("what it found" in line for line in said):
            break
    heading = next(i for i, line in enumerate(said) if "what I found" in line)
    body = next(i for i, line in enumerate(said) if "what it found" in line)
    assert heading < body, "the heading must still come before the findings it introduces"
