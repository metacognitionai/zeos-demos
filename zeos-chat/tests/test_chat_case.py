"""What the chat case must be true of, asserted on the journal rather than the transcript.

"The reflex preempted the answer within one token boundary" is a structural fact; "the
reply mentioned Kyoto" is a coincidence of whatever the model said. Every assertion here
is of the first kind, which is also what lets the model be swapped for a stub without
rewriting any of them.
"""

from __future__ import annotations

import threading
import time

from zeos.core.events import (
    Decoded,
    JobBlocked,
    JobPreempted,
    JobSpawned,
    PipeDrained,
    PipeWritten,
    VectorFired,
)
from zeos.core.ids import DescriptorName, JobState, PipeName

from zeos_chat.jobs import PROGRAMS
from zeos_chat.llm import Ask, StubModel

MESSAGES = PipeName("user.messages")
ARRIVALS = PipeName("user.arrivals")
CANCEL = PipeName("user.cancel")
REPLIES = PipeName("user.replies")
HEAR = PipeName("llm.converse.replies")


# -- the contract -----------------------------------------------------------


def test_the_tree_typechecks(bundle) -> None:
    """Load-time rejection is the assurance a prose prompt cannot offer, so the tree
    having nothing to report is itself the test."""
    from zeos_chat.cli import findings

    assert [f.render() for f in findings(bundle)] == []


def test_the_conversation_is_the_only_job_at_boot(bundle) -> None:
    assert bundle.boot == (DescriptorName("converse"),)


def test_every_descriptor_has_a_program(bundle) -> None:
    """A behaviour now spans a contract and an implementation, which is the cost of this
    design. A descriptor the kernel can dispatch with nothing to run is the failure that
    split creates, so it is checked rather than remembered."""
    assert set(PROGRAMS) == {str(name) for name in bundle.descriptors}


def test_the_reflex_outranks_the_message_handler(bundle) -> None:
    """A person saying stop while typing a follow-up means stop. Lower is more urgent."""
    by_name = {v.handler: v.priority for v in bundle.vectors}
    assert int(by_name[DescriptorName("cancel")]) < int(by_name[DescriptorName("new-message")])


# -- a turn -----------------------------------------------------------------


def test_a_turn_costs_one_model_call(make_session, run) -> None:
    """The whole point of the redesign, as a number.

    Every command used to be an API call: a turn was the handler's two, the topic, the
    reply and the closing read. Now the control flow is Python and only the content is
    asked for, so a turn is *one* call however many paragraphs come back.
    """
    asked: list[Ask] = []

    def model(ask: Ask):
        asked.append(ask)
        yield from ("the", "sleeper", "leaves", "at", "nine")

    session, adapter, _ = make_session(model=model)
    session.boot()
    session.deliver(MESSAGES, "when is the train")
    session.deliver(ARRIVALS, "message")
    run(session, adapter)

    assert len(asked) == 1, f"a turn cost {len(asked)} model calls"
    assert asked[0].descriptor == "converse"
    # However the chunks were batched on the way through, all of them arrive.
    said = " ".join(
        " ".join(e.text)
        for e in session.events
        if isinstance(e, PipeWritten) and str(e.pipe) == str(REPLIES)
    )
    assert said.split() == ["the", "sleeper", "leaves", "at", "nine"]


def test_the_handlers_cost_nothing(make_session, run) -> None:
    """`new-message` and `cancel` do their whole work in their frontmatter. A command
    apiece would be seconds of a person's time spent saying so."""
    asked: list[Ask] = []

    def model(ask: Ask) -> str:
        asked.append(ask)
        return "answer"

    session, adapter, _ = make_session(model=model)
    session.boot()
    session.deliver(CANCEL, "stop")
    run(session, adapter)

    assert [str(e.handler) for e in session.events if isinstance(e, VectorFired)] == ["cancel"]
    assert asked == [], "a handler asked the model something"


def test_the_topic_is_recorded_before_the_answer_is_asked_for(make_session, run) -> None:
    """The recording is what survives an interrupt; a half-written answer is not."""
    session, adapter, _ = make_session()
    session.boot()
    session.deliver(MESSAGES, "plan a week in Kyoto")
    session.deliver(ARRIVALS, "message")
    run(session, adapter)

    order = [
        str(e.pipe)
        for e in session.events
        if isinstance(e, JobBlocked) or getattr(e, "pipe", None) is not None
    ]
    topic = order.index("actuators.topic")
    asked = order.index("llm.converse.requests")
    assert topic < asked, "the subject was recorded after the answer was asked for"


# -- and the kernel's own claims --------------------------------------------


def test_a_message_preempts_a_reply_that_is_going_out(make_session, run) -> None:
    """Barge-in, structurally: the handler takes the machine rather than queueing behind
    the reply in progress.

    The window in which there is anything to preempt is now narrow, and that is a real
    consequence of the redesign rather than a flaw in the test: the conversation spends
    almost its whole life parked, so a message usually arrives at a job that is blocked
    rather than running. The one span it *is* running is while it writes its paragraphs
    out, so the message is delivered exactly there -- after the first has landed.
    """
    paragraphs = " | ".join(f"paragraph {n}" for n in range(1, 7))
    session, adapter, _ = make_session(model=lambda ask: paragraphs)
    session.boot()
    session.deliver(MESSAGES, "one")
    session.deliver(ARRIVALS, "message")

    for _ in range(400):
        session.step()
        landed = [e for e in session.events if isinstance(e, PipeDrained) and e.pipe == REPLIES]
        if landed:
            break
    session.deliver(MESSAGES, "two")
    session.deliver(ARRIVALS, "message")
    run(session, adapter)

    fired = [e for e in session.events if isinstance(e, VectorFired) and e.vector == "user-spoke"]
    assert len(fired) == 2, "both messages should fire; neither coalesced away"
    handlers = {
        e.job for e in session.events if isinstance(e, JobSpawned) and e.descriptor == "new-message"
    }
    assert [e for e in session.events if isinstance(e, JobPreempted) and e.by_job in handlers], (
        "the handler never took the machine from the reply in progress"
    )


def test_the_conversation_parks_rather_than_polling(make_session, run) -> None:
    """Waiting is a kernel state. The conversation blocks on its two reads and runs no
    forward passes at all in between."""
    session, adapter, _ = make_session()
    session.boot()
    run(session, adapter)

    parked = {str(e.pipe) for e in session.events if isinstance(e, JobBlocked)}
    assert parked == {str(MESSAGES)}, f"parked somewhere unexpected: {parked}"


def test_stop_does_not_leak_a_conversation(make_session, run) -> None:
    """What `on_complete: replace-with` did here, and why it is gone.

    It reads exactly right -- the half-written answer is void, so replace the conversation
    -- but it clears the *suspension* stack, and a conversation is almost never on it: a
    job parked on a pipe was descheduled by blocking, not by preemption. So the
    replacement was spawned and the original left alive, one extra conversation per press,
    every one of them blocked on the same pipe.
    """
    session, adapter, _ = make_session()
    session.boot()
    for _ in range(3):
        session.deliver(CANCEL, "stop")
        run(session, adapter)

    spawned = [e for e in session.events if isinstance(e, JobSpawned)]
    assert [str(e.descriptor) for e in spawned].count("converse") == 1, (
        "stop spawned a conversation it could not remove"
    )
    live = [
        job
        for job in session.kernel.sched.jobs()
        if str(job.name) == "converse" and job.state is not JobState.DONE
    ]
    assert len(live) == 1, f"{len(live)} conversations are alive"


def test_the_reflex_lands_while_the_model_is_still_thinking(make_session, run) -> None:
    """The interruption that matters, and the one the old design could not show at all.

    The conversation is parked on the model's reply -- not holding the machine, not
    spinning. A person says stop, and it is acted on immediately rather than after the
    model has finished composing something nobody wants.
    """
    answered = threading.Event()

    def slow(ask: Ask) -> str:
        answered.wait(timeout=5)
        return "too late"

    session, adapter, _ = make_session(model=slow)
    session.boot()
    session.deliver(MESSAGES, "a long question")
    session.deliver(ARRIVALS, "message")
    for _ in range(80):
        session.step()

    assert any(isinstance(e, JobBlocked) and str(e.pipe) == str(HEAR) for e in session.events), (
        "the conversation was not waiting on the model"
    )

    session.deliver(CANCEL, "stop")
    for _ in range(40):
        session.step()

    assert [str(e.handler) for e in session.events if isinstance(e, VectorFired)][-1] == "cancel"
    answered.set()


def test_the_persona_a_model_is_given_is_the_descriptor_body(bundle) -> None:
    """The one place a behaviour is described stays the descriptor.

    A persona kept in Python beside the tree would be a second copy of the same intent,
    free to drift from the file a reader is looking at. The body *is* the persona, passed
    through unchanged -- not summarised, not reformatted, not prefixed.
    """
    from zeos_chat.build import personas_for

    personas = personas_for(bundle)
    assert set(personas) == {str(name) for name in bundle.descriptors}
    for name, descriptor in bundle.descriptors.items():
        assert personas[str(name)] == descriptor.body


def test_a_body_describes_the_behaviour_rather_than_the_vocabulary(bundle) -> None:
    """The bodies used to teach the syscall ABI, because the model was issuing the
    commands. It is not any more, so a body that still explained `write stdout` would be
    teaching a model about a machine it never touches."""
    for name, descriptor in bundle.descriptors.items():
        for command in ("write stdout", "read stdin;", "write tools"):
            assert command not in descriptor.body, f"{name} still teaches the ABI"


# -- the granularity the kernel schedules at ---------------------------------


def test_a_program_still_decodes_one_token_at_a_time(make_session, run) -> None:
    """Moving control flow into Python did not coarsen preemption.

    A program yields a whole *command*; the seat still spends it one word per decode, so a
    tick is still a token and the kernel's boundaries are where they always were. If a
    program's command arrived as a single token, a job would become unpreemptible for the
    whole of it -- including the span a person is watching a reply appear.
    """
    session, adapter, _ = make_session(model=StubModel({"converse": "one two three four"}))
    session.boot()
    session.deliver(MESSAGES, "go")
    session.deliver(ARRIVALS, "message")
    run(session, adapter)

    decoded = [e for e in session.events if isinstance(e, Decoded)]
    assert decoded, "nothing decoded at all"
    assert {e.tokens for e in decoded} == {1}, "a decode emitted more than one token"


def paced(words: list[str], gap: float = 0.01):
    """A model that writes at a human sort of pace.

    Needed because a blocking read takes everything waiting on the pipe: a model that
    yields instantly has its whole answer coalesced into one read, and one write, and
    looks exactly like the unstreamed version. Streaming is only observable when the
    writer is slower than the reader, which is the real case.
    """

    def model(ask: Ask):
        for word in words:
            time.sleep(gap)
            yield word

    return model


def test_an_answer_reaches_the_person_as_it_is_written(make_session, run) -> None:
    """The point of streaming: the words arrive in pieces rather than in one go at the end."""
    session, adapter, _ = make_session(model=paced([f"word{n}" for n in range(1, 12)]))
    session.boot()
    session.deliver(MESSAGES, "go")
    session.deliver(ARRIVALS, "message")
    run(session, adapter, steps=4000)

    drained = [e for e in session.events if isinstance(e, PipeDrained) and e.pipe == REPLIES]
    assert len(drained) > 1, "the whole answer reached the person in one piece"


def test_stop_stops_the_rest_of_a_reply(make_session, run) -> None:
    """Stop, while a reply is streaming out.

    The cut is now at a word rather than a paragraph, which is the point of streaming: a
    person keeps what had arrived when they stopped it, and is spared the rest. How much
    that is depends on where the stop lands, so what is asserted is that some arrived and
    some did not.
    """
    words = [f"word{n}" for n in range(1, 40)]
    session, adapter, source = make_session(model=paced(words))
    session.boot()
    session.deliver(MESSAGES, "go")
    session.deliver(ARRIVALS, "message")

    def sent() -> int:
        return len([e for e in session.events if isinstance(e, PipeDrained) and e.pipe == REPLIES])

    # Time-based, not step-based: the kernel turns in microseconds and the model is the
    # slow one, so counting ticks would race past the stream without ever waiting for it.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and sent() < 3:
        if not session.step():
            time.sleep(0.002)
    assert sent() >= 3, "the reply never got under way"
    part_way = sent()

    # Exactly what the web server does when the person presses stop.
    session.deliver(CANCEL, "stop")
    adapter.abandon("converse")
    source.abandon("converse")
    run(session, adapter)

    assert part_way <= sent() < len(words), (
        f"sent {sent()} of {len(words)} words; stop did not stop the rest"
    )


def test_stop_discards_an_answer_the_person_no_longer_wants(make_session, run) -> None:
    """Stop, while the model is still thinking -- the state a conversation is usually in.

    The request is abandoned, so the answer is dropped when it eventually arrives rather
    than written out after the person has declined it. Before this, it was delivered: the
    job was parked on the reply pipe, nothing could wake it, and the reflex could not
    remove it either.
    """
    released = threading.Event()

    def slow(ask: Ask) -> str:
        released.wait(timeout=5)
        return "the answer nobody wants any more"

    session, adapter, source = make_session(model=slow)
    session.boot()
    session.deliver(MESSAGES, "a question")
    session.deliver(ARRIVALS, "message")
    for _ in range(400):
        session.step()
    assert adapter.in_flight == 1, "the model was never asked"

    session.deliver(CANCEL, "stop")
    adapter.abandon("converse")
    source.abandon("converse")
    assert adapter.in_flight == 0, "the request was still counted after it was abandoned"

    released.set()
    run(session, adapter)

    said = [
        " ".join(e.text)
        for e in session.events
        if isinstance(e, PipeWritten) and str(e.pipe) == str(REPLIES)
    ]
    assert said == [], f"an abandoned answer reached the person: {said}"


def test_a_later_turn_is_asked_with_the_earlier_ones(make_session, run) -> None:
    """The bug this guards: the chatbot could not carry on a conversation.

    `converse` writes only the new message -- it must, because a job's writes are decoded
    into its own window, so writing the history out each turn would make the window grow
    by its own length every time. The history therefore comes from the kernel's window,
    which is where it already was.
    """
    asks: list[Ask] = []

    def model(ask: Ask) -> str:
        asks.append(ask)
        return "noted"

    session, adapter, _ = make_session(model=model)
    session.boot()
    for message in ("what is the capital of France", "and its population"):
        session.deliver(MESSAGES, message)
        session.deliver(ARRIVALS, "message")
        run(session, adapter)

    assert len(asks) == 2, "expected one model call per turn"
    # The window at the moment of asking already holds the message being answered -- it
    # arrived through `read stdin;` before the job asked. What it must not hold is a turn
    # that has not happened yet.
    assert "population" not in asks[0].window, "the first turn saw the future"
    assert "capital of France" in asks[1].window, "the second turn forgot the first"
    assert "noted" in asks[1].window, "its own earlier answer was not in the window"


def test_a_job_asks_with_the_new_message_only(make_session, run) -> None:
    """The other half, and the reason the history comes from the window rather than the
    job: what a job writes is decoded into its own context, so a job that wrote the
    conversation out each turn would double its own window every time."""
    asks: list[Ask] = []
    session, adapter, _ = make_session(model=lambda ask: (asks.append(ask), "x")[1])
    session.boot()
    session.deliver(MESSAGES, "a short question")
    session.deliver(ARRIVALS, "message")
    run(session, adapter)

    assert asks[0].prompt == "a short question", (
        f"the job wrote more than the message: {asks[0].prompt!r}"
    )


def test_a_read_returns_what_a_pipe_delivered_and_not_a_kernel_frame() -> None:
    """A program's `arrival` must mean "what my read produced", not "the last thing put
    into my window" -- the kernel puts its own frames there too.

    Found the hard way. A resume notice lands after a preemption, so once a second job
    existed in the system the conversation was preempted around its read, resumed, and
    took `<RESUME> Waited 29ms. Changed state you depend on:` for the person's message:
    it set that as the topic and asked the model about it.
    """
    from zeos.core.ids import JobId

    from zeos_chat.jobs import JobContext, from_the_kernel

    ctx = JobContext(descriptor="converse", job=JobId(1))
    ctx.arrivals.append("research the history of Kyoto")
    ctx.arrivals.append("<RESUME> Waited 29ms. Changed state you depend on: Revalidate.")
    assert ctx.arrival == "research the history of Kyoto"

    ctx.arrivals.append("<STATUS session.topic> the history of Kyoto </STATUS>")
    assert ctx.arrival == "research the history of Kyoto", "a status refresh was taken as input"

    ctx.arrivals.append("and what about Nara")
    assert ctx.arrival == "and what about Nara"

    assert from_the_kernel("<FAULT> something") and not from_the_kernel("a fault, I think")


def test_a_job_with_only_kernel_frames_has_read_nothing() -> None:
    """The degenerate case, which must not fall back to the frame: a program that acts on
    the kernel's own words as though a person had said them is the bug above."""
    from zeos.core.ids import JobId

    from zeos_chat.jobs import JobContext

    ctx = JobContext(descriptor="converse", job=JobId(1))
    ctx.arrivals.append("<RESUME> Suspended 1ms.")
    assert ctx.arrival == ""
