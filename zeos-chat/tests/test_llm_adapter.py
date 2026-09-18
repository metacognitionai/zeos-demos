"""The model on the end of a pipe, and the claim that buys.

Not "the adapter calls the model" -- that is plumbing. What is worth holding it to is the
property the old design could not have at any speed: **the kernel keeps running while a
job waits for an answer.** A seat that calls the model from inside `decode` is the whole
kernel, stopped, for as long as the model takes.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from zeos.core.events import JobBlocked, JobSpawned, PipeWritten, VectorFired
from zeos.core.ids import PipeName
from zeos.descriptor.loader import load_case

from zeos_chat.build import build_session
from zeos_chat.llm import END, LLM_PIPES, Ask, LlmAdapter, StubModel
from zeos_chat.session import Session

CASE = Path(__file__).resolve().parents[1] / "cases" / "chat-scripted"
ASK, HEAR = LLM_PIPES["converse"]


def adapter(model, delivered: list[tuple[PipeName, str]] | None = None) -> LlmAdapter:
    record = delivered if delivered is not None else []
    return LlmAdapter(model, lambda pipe, text: record.append((pipe, text)))


def until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


# -- routing ----------------------------------------------------------------


def test_a_request_is_answered_on_the_pipe_that_asked() -> None:
    """A pair per descriptor, so a reply finds the job that asked with no correlation id:
    the pipe is the address."""
    delivered: list[tuple[PipeName, str]] = []
    llm = adapter(StubModel({"converse": "an answer"}), delivered)
    llm.ask(ASK, "a question")

    # An answer is a stream now, so the device says where it ends. Both go to the pipe
    # that asked: a reply finds the job that wanted it with no correlation id.
    assert until(lambda: delivered == [(HEAR, "an answer"), (HEAR, END)])


def test_the_model_is_told_which_descriptor_asked() -> None:
    """Which is what decides the persona it is answered with."""
    stub = StubModel()
    llm = adapter(stub)
    llm.ask(LLM_PIPES["deep-research"][0], "look into flights")

    assert until(lambda: len(stub.asked) == 1)
    assert stub.asked[0].descriptor == "deep-research"
    assert stub.asked[0].prompt == "look into flights"


def test_only_the_model_pipes_are_claimed() -> None:
    llm = adapter(StubModel())
    assert llm.handles(ASK)
    assert not llm.handles(PipeName("user.replies"))


# -- and a failure comes back rather than parking the job for ever ----------


def test_a_model_that_raises_answers_with_the_failure() -> None:
    """The job is parked on that read. A worker that died quietly would park it for ever,
    so the error goes back down the same pipe an answer would."""
    seen: list[tuple[Ask, Exception]] = []
    delivered: list[tuple[PipeName, str]] = []

    def broken(ask: Ask) -> str:
        raise RuntimeError("no network")

    LlmAdapter(
        broken,
        lambda pipe, text: delivered.append((pipe, text)),
        on_error=lambda ask, exc: seen.append((ask, exc)),
    ).ask(ASK, "a question")

    assert until(lambda: len(delivered) == 2)
    assert delivered[0][0] == HEAR
    assert "no network" in delivered[0][1]
    assert delivered[1][1] == END, "a failure must still end the stream, or the job parks"
    assert [type(exc).__name__ for _, exc in seen] == ["RuntimeError"]


# -- the claim ---------------------------------------------------------------


def test_the_kernel_can_still_dispatch_while_a_job_waits_for_the_model() -> None:
    """The property the old design could not have at any speed.

    A slow model is held open. The conversation is parked on its reply and there is
    genuinely nothing else for the kernel to run -- that is what "waiting is free" looks
    like, and an idle kernel is the correct picture of it. The claim is not that events
    keep accumulating; it is that the kernel is *available*. So a reflex is fired while
    the model is still thinking, and it runs.

    Under a seat that calls the model from inside `decode`, this cannot happen at all: the
    kernel is inside the call, and the reflex waits for the model it knows nothing about.
    """
    answered = threading.Event()

    def slow(ask: Ask) -> str:
        answered.wait(timeout=5)
        return "at last"

    session, _, _ = build_session(load_case(CASE), model=slow)
    session.boot()
    session.deliver(PipeName("user.messages"), "hello")
    session.deliver(PipeName("user.arrivals"), "message")
    for _ in range(80):
        session.step()

    parked = [e for e in session.events if isinstance(e, JobBlocked)]
    assert any(str(e.pipe) == "llm.converse.replies" for e in parked), (
        "the conversation should be waiting on the model, not holding the machine"
    )

    # The model has still not answered. Fire the reflex anyway.
    before = len(session.events)
    session.deliver(PipeName("user.cancel"), "stop")
    for _ in range(40):
        session.step()

    fired = [e for e in session.events[before:] if isinstance(e, VectorFired)]
    assert [str(e.handler) for e in fired] == ["cancel"], (
        "the kernel could not dispatch while the model was thinking"
    )
    answered.set()


def test_a_parked_job_costs_nothing_and_is_named_in_the_journal() -> None:
    """`JobBlocked` on the reply pipe is the whole of "waiting is free": no forward pass
    happens for it, and the journal says which pipe it is waiting on."""
    session, _, _ = build_session(load_case(CASE))
    session.boot()
    for _ in range(40):
        session.step()

    blocked = [e for e in session.events if isinstance(e, JobBlocked)]
    assert blocked, "nothing ever parked"
    assert [str(e.descriptor) for e in session.events if isinstance(e, JobSpawned)] == ["converse"]


def _writes(session: Session) -> list[PipeWritten]:
    return [e for e in session.events if isinstance(e, PipeWritten)]


@pytest.mark.parametrize("descriptor", sorted(LLM_PIPES))
def test_every_model_pipe_pair_is_declared_by_the_case(descriptor: str) -> None:
    """A pipe the adapter routes to but the case never declares would default to ring 2
    and peer_job, quietly, which is exactly the provenance guesswork pipes.yaml exists to
    prevent."""
    declared = {p.name: p for p in load_case(CASE).pipes}
    requests, replies = LLM_PIPES[descriptor]
    assert declared[requests].sink, f"{requests} must be a sink for the driver to drain it"
    assert declared[replies].device, f"{replies} must be a device pipe for deliver to land on"


def test_an_inline_model_answers_before_ask_returns() -> None:
    """Determinism by construction rather than by luck.

    A worker keeps the tick short when the model is slow, and it also means the reply
    lands on whichever tick the thread happens to finish on. A replay should not depend on
    that, so a model fast enough not to need the thread does not get one.
    """
    delivered: list[tuple[PipeName, str]] = []
    llm = LlmAdapter(
        StubModel({"converse": "at once"}),
        lambda pipe, text: delivered.append((pipe, text)),
        inline=True,
    )
    llm.ask(ASK, "a question")

    assert delivered == [(HEAR, "at once"), (HEAR, END)], "the stream had not arrived"
    assert llm.in_flight == 0
