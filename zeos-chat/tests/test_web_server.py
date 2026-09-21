"""The browser half, driven over real HTTP with a tape behind it.

No key and no browser: a socket and the tape are enough to hold the whole adapter to
what it claims. What is being tested is that a message typed into a page becomes two
deliveries on the right pipes, that a drained sink comes back out, and that the thread
that owns the kernel is the only one that ever touches it.
"""

from __future__ import annotations

import itertools
import json
import queue
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from zeos.core.events import JobCompleted, PipeWritten
from zeos.core.ids import PipeName
from zeos.descriptor.loader import load_case

from zeos_chat.build import build_session
from zeos_chat.llm import Ask
from zeos_chat.web.server import ChatServer, Report, page, serve

CASE = Path(__file__).resolve().parents[1] / "cases" / "chat"
MESSAGES = PipeName("user.messages")
ARRIVALS = PipeName("user.arrivals")
CANCEL = PipeName("user.cancel")


@pytest.fixture
def chat() -> Iterator[tuple[ChatServer, str]]:
    session, adapter, _ = build_session(load_case(CASE))
    # Port 0: the OS picks a free one, so the suite never collides with a real server.
    server = serve(session, adapter, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    try:
        yield server, base
    finally:
        server.stop()
        server.httpd.shutdown()


def post(base: str, path: str, body: dict[str, object] | None = None) -> int:
    request = urllib.request.Request(
        f"{base}{path}",
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return int(response.status)


#: How long a gated fake model waits before giving up and answering anyway. Long enough
#: that it never expires inside a test: an `Event.wait` that times out *releases* the job
#: it was holding, so a short gate turns a deterministic test into a race against itself.
#: Every test that uses one sets it in a `finally`, so nothing actually waits this long.
HELD = 120.0


def until(predicate, timeout: float = 5.0) -> bool:
    """Wait for the kernel thread to get there. The loop is asynchronous by design."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# -- the page ---------------------------------------------------------------


def test_the_page_is_one_file_with_its_assets_inlined() -> None:
    built = page()
    assert "/*CSS*/" not in built and "/*JS*/" not in built
    assert "EventSource" in built, "the script did not make it in"
    assert "@keyframes rally" in built, "the stylesheet did not make it in"


def test_the_logo_is_the_metacognition_mark() -> None:
    """The bars and the dot are the mark's own geometry: it is already a Pong board."""
    built = page()
    for rect in ('x="12" y="2"', 'x="37" y="31"', 'x="66" y="18"'):
        assert rect in built


def test_the_logo_animates_only_while_a_job_holds_the_machine() -> None:
    """The resting state must be the mark itself, and twice it was not.

    Declared-and-paused was wrong in two separate ways: `animation-play-state: paused` in
    a rule of its own is silently reset by any `animation:` shorthand below it, so the
    logo span for ever; and a paused animation still applies its first keyframe, so even
    once stopped it rested with the ball shifted to one side rather than in the middle.

    Declaring the animation only under `.busy` removes both. This asserts that: no
    animation may be attached to a logo part by any rule that is not `.logo.busy`.
    """
    css = (Path(__file__).resolve().parents[1] / "src/zeos_chat/web/static/chat.css").read_text(
        encoding="utf-8"
    )
    # Comments out first: they are prose and may mention anything. Then, for each
    # `animation` declaration, the selector is whatever sits between the previous brace
    # and its own -- which survives the nested blocks a naive rule split does not.
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    animated: list[str] = []
    for declaration in re.finditer(r"animation\s*:\s*([^;]+);", bare):
        if "none" in declaration.group(1):
            continue
        head = bare[: declaration.start()]
        opened = head.rindex("{")
        previous = max(head.rfind("}", 0, opened), head.rfind("{", 0, opened))
        selector = head[previous + 1 : opened].strip()
        # Only the logo's own parts. Other things on the page may animate for their own
        # reasons, and this rule is about the mark resting when the kernel does rather
        # than about motion in general.
        if any(part in selector for part in (".logo", ".paddle", ".ball")):
            animated.append(selector)
    assert animated, "the logo has no animation at all"
    assert all(".logo.busy" in selector for selector in animated), (
        f"an animation is attached outside .busy, so the logo moves at rest: {animated}"
    )


def test_the_logo_keyframes_begin_and_end_at_rest() -> None:
    """A cycle that starts anywhere else makes stopping a jump, and pausing a lie."""
    css = (Path(__file__).resolve().parents[1] / "src/zeos_chat/web/static/chat.css").read_text(
        encoding="utf-8"
    )
    for name in ("rally", "volley-left", "volley-right"):
        block = re.search(rf"@keyframes {re.escape(name)}\s*\{{(.+?)^\}}", css, re.S | re.M)
        assert block, f"no keyframes for {name}"
        for edge in ("0%", "100%"):
            frame = re.search(rf"{re.escape(edge)}\s*\{{([^}}]*)\}}", block.group(1))
            assert frame, f"{name} has no {edge} frame"
            assert "(0)" in frame.group(1), f"{name} does not rest at the mark at {edge}"


def test_the_page_is_served(chat: tuple[ChatServer, str]) -> None:
    _, base = chat
    with urllib.request.urlopen(f"{base}/", timeout=5) as response:
        assert response.status == 200
        assert b"ZEOS Chat" in response.read()


# -- a message becomes two deliveries ---------------------------------------


def test_saying_something_delivers_the_words_and_rings_the_doorbell(
    chat: tuple[ChatServer, str],
) -> None:
    """The correction `pipes.yaml` explains: the content and the event are separate
    lines, and only one of them carries the message."""
    server, base = chat
    assert post(base, "/say", {"text": "hello there"}) == 200

    assert until(lambda: any(e.pipe == ARRIVALS for e in _writes(server)))
    landed = {str(e.pipe): " ".join(e.text) for e in _writes(server)}
    assert landed[str(MESSAGES)] == "hello there"
    assert landed[str(ARRIVALS)] == "message", "the doorbell carried the message itself"


def test_an_empty_message_is_not_delivered(chat: tuple[ChatServer, str]) -> None:
    server, base = chat
    assert post(base, "/say", {"text": "   "}) == 200
    time.sleep(0.1)
    assert not [e for e in _writes(server) if e.pipe == MESSAGES]


def test_stop_fires_the_reflex(chat: tuple[ChatServer, str]) -> None:
    server, base = chat
    assert post(base, "/stop") == 200
    assert until(lambda: any(e.pipe == CANCEL for e in _writes(server)))


# -- and a reply comes back -------------------------------------------------


def test_a_reply_reaches_a_watcher(chat: tuple[ChatServer, str]) -> None:
    """End to end: a message in over HTTP, a drained sink out over the event stream."""
    server, base = chat
    seen: list[dict[str, object]] = []
    stream = server.watch()

    def collect() -> None:
        while True:
            line = stream.get()
            seen.append(json.loads(line.removeprefix("data: ").strip()))

    threading.Thread(target=collect, daemon=True).start()
    post(base, "/say", {"text": "when is the train"})

    assert until(lambda: any(m["kind"] == "reply" for m in seen)), "no reply arrived"
    assert any(m["kind"] == "said" and m["text"] == "when is the train" for m in seen), (
        "the page is told about its own message over the stream, so a second tab sees it too"
    )
    kernel_lines = [m for m in seen if m["kind"] == "kernel"]
    assert kernel_lines, "the panel was told nothing"
    assert all("class" in m for m in kernel_lines), "the envelope's kind overwrote the class"


def test_a_watcher_that_goes_away_does_not_stall_the_conversation(
    chat: tuple[ChatServer, str],
) -> None:
    """A browser nobody is reading must not be able to hold up the kernel: each watcher
    has its own queue, and nothing waits on it."""
    server, base = chat
    abandoned = server.watch()
    post(base, "/say", {"text": "when is the train"})

    assert until(lambda: any(e.pipe == PipeName("user.replies") for e in _writes(server)))
    assert abandoned.qsize() > 0, "the abandoned queue should simply fill"


def _writes(server: ChatServer) -> list[PipeWritten]:
    return [e for e in list(server.session.events) if isinstance(e, PipeWritten)]


# -- the logo says what the kernel is doing ---------------------------------


def drain(stream: queue.Queue[str]) -> list[dict[str, object]]:
    """Every frame waiting right now, decoded. Nothing blocks."""
    frames: list[dict[str, object]] = []
    while True:
        try:
            frames.append(json.loads(stream.get_nowait().removeprefix("data: ").strip()))
        except queue.Empty:
            return frames


def busy_states(seen: list[dict[str, object]]) -> list[bool]:
    return [bool(m["busy"]) for m in seen if m["kind"] == "busy"]


def test_busy_is_published_the_moment_a_message_is_queued() -> None:
    """Without a kernel thread at all, so only ``say`` can have published it.

    The first tick of a reply is one decode, and under an API seat a decode is a call
    taking seconds. Reporting busy after that tick left the page still for exactly the
    span a person is watching for a sign that anything is happening.
    """
    server = ChatServer(*build_session(load_case(CASE))[:3])
    stream = server.watch()
    # Drained rather than counted: `watch` opens with the state a new tab would otherwise
    # wait for a change to learn, and how many frames that takes is its business.
    assert busy_states(drain(stream)) == [False]

    server.say("hello")
    assert busy_states(drain(stream)) == [True], "the page was not told until a tick had run"


def test_a_watcher_is_told_the_current_state_when_it_connects() -> None:
    """A tab opened while a reply is being written should animate at once, rather than
    wait for a change it has no way to anticipate."""
    server = ChatServer(*build_session(load_case(CASE))[:3])
    server.say("hello")

    late = server.watch()
    assert json.loads(late.get_nowait().removeprefix("data: "))["busy"] is True


def test_the_logo_rests_once_the_conversation_is_parked(chat: tuple[ChatServer, str]) -> None:
    """The other half of the claim: an idle conversation is a still logo, which is the
    honest picture of a job blocked on `read stdin;` costing nothing."""
    server, base = chat
    seen: list[dict[str, object]] = []
    stream = server.watch()

    def collect() -> None:
        while True:
            seen.append(json.loads(stream.get().removeprefix("data: ").strip()))

    threading.Thread(target=collect, daemon=True).start()
    post(base, "/say", {"text": "when is the train"})

    # In that order: the stream opens with the current state, so waiting for "the last
    # state is False" is satisfied by the frame that arrived before anything happened.
    assert until(lambda: True in busy_states(seen), timeout=8), "the logo never started"
    assert until(lambda: busy_states(seen)[-1] is False, timeout=8), (
        f"the logo never stopped: {busy_states(seen)}"
    )


def test_the_logo_keeps_animating_while_the_model_is_thinking() -> None:
    """The regression that moving the model behind a pipe introduced.

    "Busy" used to mean a job holds the machine, which was true enough when the model was
    the machine. Now a job waiting for an answer is *parked*: nothing is runnable, the
    scheduler is quiescent, and reading only the scheduler says idle -- at exactly the
    moment a person is watching an empty page for a sign of life. The kernel is not idle,
    it is waiting on a device, and the signal has to say so.
    """
    answered = threading.Event()

    def slow(ask: Ask) -> str:
        answered.wait(timeout=5)
        return "at last"

    session, adapter, _ = build_session(load_case(CASE), model=slow)
    server = serve(session, adapter, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    seen: list[dict[str, object]] = []
    stream = server.watch()

    def collect() -> None:
        while True:
            seen.append(json.loads(stream.get().removeprefix("data: ").strip()))

    threading.Thread(target=collect, daemon=True).start()
    try:
        post(base, "/say", {"text": "a question that takes thinking about"})
        assert until(lambda: adapter.in_flight > 0, timeout=5), "the model was never asked"

        # The model has not answered. The kernel has nothing runnable. The logo must not
        # have gone out.
        assert until(lambda: True in busy_states(seen), timeout=5), "the logo never started"
        time.sleep(0.2)
        assert busy_states(seen)[-1] is True, (
            f"the logo rested while the model was still thinking: {busy_states(seen)}"
        )

        answered.set()
        assert until(lambda: busy_states(seen)[-1] is False, timeout=8), (
            "and it never rested once the answer arrived"
        )
    finally:
        answered.set()
        server.stop()
        server.httpd.shutdown()


# -- an interruption is visible in the transcript, not only in the journal ---


def marks(seen: list[dict[str, object]]) -> list[str]:
    return [str(m["mark"]) for m in seen if m["kind"] == "mark"]


def interruptions(seen: list[dict[str, object]]) -> list[str]:
    """The marks that say a turn did not finish. A research hand-off is also a mark, and a
    test about interruption should not fail because one was asked for."""
    return [m for m in marks(seen) if m != "research"]


def watch(server: ChatServer) -> list[dict[str, object]]:
    seen: list[dict[str, object]] = []
    stream = server.watch()

    def collect() -> None:
        while True:
            seen.append(json.loads(stream.get().removeprefix("data: ").strip()))

    threading.Thread(target=collect, daemon=True).start()
    return seen


def test_a_message_sent_mid_answer_is_marked_as_an_interruption() -> None:
    """The panel shows the vector firing and the job it preempted. The transcript should
    show the consequence, in the place a person is actually looking."""
    released = threading.Event()

    def slow(ask: Ask) -> str:
        released.wait(timeout=5)
        return "an answer"

    session, adapter, source = build_session(load_case(CASE), model=slow)
    server = serve(session, adapter, source, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    seen = watch(server)
    try:
        post(base, "/say", {"text": "first"})
        assert until(lambda: adapter.in_flight > 0, timeout=5), "the model was never asked"
        post(base, "/say", {"text": "second, while it was still answering"})

        assert until(lambda: marks(seen) == ["barge-in"], timeout=5), f"marks: {marks(seen)}"
        # And the mark precedes the message that caused it, so it reads in order.
        kinds = [m["kind"] for m in seen if m["kind"] in ("mark", "said")]
        assert kinds == ["said", "mark", "said"], kinds
    finally:
        released.set()
        server.stop()
        server.httpd.shutdown()


def test_barging_in_abandons_the_answer_it_interrupted() -> None:
    """Found in a browser, not by a test: barge-in drew its mark and then let the old
    answer keep writing itself out underneath it, so the page showed the tail of the
    abandoned reply as if it were the reply to the new message.

    The vector firing is not enough on its own. A conversation parked on the model is not
    on the suspension stack, so nothing the kernel does to that stack reaches it -- the
    device has to give the request up and the job has to be told, which is what stop
    already did and this did not.
    """
    released = threading.Event()
    calls = itertools.count()

    def slow(ask: Ask) -> Iterator[str]:
        # Distinct answers per call, because the reply to the *new* message is a second
        # call to the same model and would otherwise be indistinguishable from the first.
        if next(calls) > 0:
            yield "answering the new question"
            return
        yield "the start of"
        released.wait(timeout=5)
        yield "words nobody asked for any more"

    session, adapter, source = build_session(load_case(CASE), model=slow)
    server = serve(session, adapter, source, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    seen = watch(server)
    try:
        post(base, "/say", {"text": "first"})
        assert until(lambda: adapter.in_flight > 0, timeout=5), "the model was never asked"
        post(base, "/say", {"text": "second, while it was still answering"})

        assert until(lambda: adapter.in_flight == 0, timeout=5), (
            "the request was left in flight, so the answer was still on its way"
        )
        released.set()
        time.sleep(0.3)
        said = " ".join(str(m["text"]) for m in seen if m["kind"] == "reply")
        assert "nobody asked for" not in said, (
            f"the abandoned answer went on writing after the barge-in: {said!r}"
        )
    finally:
        released.set()
        server.stop()
        server.httpd.shutdown()


def test_a_message_sent_to_an_idle_conversation_is_not_marked(
    chat: tuple[ChatServer, str],
) -> None:
    """Nothing was interrupted, so there is nothing to say. A mark for every message would
    make the one that means something invisible."""
    server, base = chat
    seen = watch(server)
    post(base, "/say", {"text": "hello"})

    assert until(lambda: any(m["kind"] == "reply" for m in seen), timeout=8)
    assert marks(seen) == []


def test_stopping_something_is_marked_and_stopping_nothing_is_not() -> None:
    """The second half matters: a mark the kernel never had an event for is the page
    inventing one."""
    released = threading.Event()

    def slow(ask: Ask) -> str:
        released.wait(timeout=5)
        return "too late"

    session, adapter, source = build_session(load_case(CASE), model=slow)
    server = serve(session, adapter, source, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    seen = watch(server)
    try:
        post(base, "/stop")  # nothing is happening
        time.sleep(0.2)
        assert marks(seen) == [], "stopping an idle conversation was marked"

        post(base, "/say", {"text": "a question"})
        assert until(lambda: adapter.in_flight > 0, timeout=5)
        post(base, "/stop")  # now there is something to stop
        assert until(lambda: marks(seen) == ["stopped"], timeout=5), f"marks: {marks(seen)}"
    finally:
        released.set()
        server.stop()
        server.httpd.shutdown()


# -- the consequential effect, over HTTP ------------------------------------


def test_asking_to_email_delivers_the_letter_then_the_doorbell() -> None:
    """The server's whole part in a send: two deliveries, in that order, and no decision.

    It does not check whether mail is configured or whether this conversation may send.
    That check is at the pipe, and making it here in advance would be making the decision
    the capability check exists to make.
    """
    from zeos_chat.mail import MailAdapter, MailSettings, SimulatedSender

    sender = SimulatedSender()
    mail = MailAdapter(MailSettings(recipient="a@b.c"), sender)
    session, adapter, source = build_session(load_case(CASE), mail=mail)
    server = serve(session, adapter, source, mail, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    seen = watch(server)
    try:
        assert post(base, "/email", {"subject": "Kyoto", "body": "you: hi"}) == 200

        assert until(lambda: bool(sender.sent), timeout=5), "nothing reached the outbox"
        assert sender.sent[0].subject == "Kyoto"
        assert "you: hi" in sender.sent[0].body

        # And the outcome reaches the page, because a refusal has to be visible there.
        assert until(lambda: any(m["kind"] == "mail" for m in seen), timeout=5)
        told = next(m for m in seen if m["kind"] == "mail")
        assert told["to"] == "a@b.c"
        assert "simulated" in str(told["outcome"])
    finally:
        server.stop()
        server.httpd.shutdown()


def test_an_empty_request_sends_nothing() -> None:
    """A click with nothing on the page must not become a letter."""
    from zeos_chat.mail import MailAdapter, MailSettings, SimulatedSender

    sender = SimulatedSender()
    mail = MailAdapter(MailSettings(recipient="a@b.c"), sender)
    session, adapter, source = build_session(load_case(CASE), mail=mail)
    server = serve(session, adapter, source, mail, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    try:
        assert post(base, "/email", {"subject": "s", "body": "   "}) == 200
        time.sleep(0.3)
        assert sender.sent == []
    finally:
        server.stop()
        server.httpd.shutdown()


def test_a_new_tab_is_told_what_a_press_would_actually_do() -> None:
    """A draft you confirm and a mail that has already gone are different promises, so the
    page is told which one the button is making rather than guessing."""
    from zeos_chat.mail import MailAdapter, MailSettings, SimulatedSender

    mail = MailAdapter(MailSettings(recipient="a@b.c"), SimulatedSender())
    server = ChatServer(*build_session(load_case(CASE), mail=mail)[:3], mail)
    told = [m for m in drain(server.watch()) if m["kind"] == "mailer"]

    assert told and told[0]["mode"] == "simulated"
    assert told[0]["to"] == "a@b.c"


def test_the_default_transport_opens_a_draft_rather_than_sending(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The default has to be the one that cannot send on its own, so a fresh clone can
    press the button without an account and without mailing anybody."""
    from zeos_chat.mail import MailAdapter, MailSettings

    mail = MailAdapter(MailSettings(recipient="a@b.c", outbox=tmp_path))
    server = ChatServer(*build_session(load_case(CASE), mail=mail)[:3], mail)
    told = [m for m in drain(server.watch()) if m["kind"] == "mailer"]

    assert told and told[0]["mode"] == "desktop"


def test_the_email_button_styling_cannot_reach_the_transcript() -> None:
    """The button and the transcript's mail line share the class `mail`, so a rule written
    as `.mail` paints both. It did: a solid green block appeared behind the "mail — ..."
    text in the conversation. Button styling is selected by id for that reason."""
    css = (Path(__file__).resolve().parents[1] / "src/zeos_chat/web/static/chat.css").read_text(
        encoding="utf-8"
    )
    bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    for block in re.finditer(r"([^{}]+)\{([^{}]*)\}", bare):
        selector, body = block.group(1).strip(), block.group(2)
        if ".mail" not in selector or selector.startswith("@"):
            continue
        assert "#messages" in selector or selector.startswith("#email"), (
            f"`{selector}` uses the shared class and would style the transcript too"
        )
        assert "background" not in body, f"`{selector}` puts a background on transcript text"


# -- background work is visible on the page ---------------------------------


def tasks(seen: list[dict[str, object]]) -> list[list[str]]:
    """Each task frame as the list of jobs it reported."""
    return [[str(t) for t in m["tasks"]] for m in seen if m["kind"] == "task"]  # type: ignore[union-attr]


def test_the_page_is_told_what_a_background_job_is_doing() -> None:
    """Read from `session.pending_task`, the world object the long job latches through its
    own actuator -- the same line the conversation maps read-only. The page learns about
    dispatched work the way the conversation does, rather than the server keeping a
    second account of it."""
    import threading

    release = threading.Event()

    def model(ask: Ask) -> str:
        if ask.descriptor == "deep-research":
            release.wait(timeout=HELD)
            return "what it found"
        return "an answer"

    session, adapter, source = build_session(load_case(CASE), model=model)
    server = serve(session, adapter, source, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    seen = watch(server)
    try:
        post(base, "/say", {"text": "research the history of Kyoto"})

        assert until(lambda: any(frame for frame in tasks(seen)), timeout=8), (
            f"the page was never told: {tasks(seen)}"
        )
        running = next(frame for frame in tasks(seen) if frame)
        assert len(running) == 1
        assert "the history of Kyoto" in running[0]

        release.set()
        assert until(lambda: tasks(seen)[-1] == [], timeout=8), (
            f"the strip was left saying a finished job is still running: {tasks(seen)}"
        )
    finally:
        release.set()
        server.stop()
        server.httpd.shutdown()


def test_a_new_tab_is_told_the_current_task() -> None:
    """A tab opened while a background job is running should say so at once rather than
    wait for a change it has no way to anticipate."""
    server = ChatServer(*build_session(load_case(CASE))[:3])
    assert tasks(drain(server.watch())) == [[]], "an idle system must report no jobs"


# -- background work is not an interruption ---------------------------------


def _research_running(model):  # type: ignore[no-untyped-def]
    """A server whose long job is parked on a model that will not answer yet."""
    session, adapter, source = build_session(load_case(CASE), model=model)
    server = serve(session, adapter, source, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    seen = watch(server)
    post(base, "/say", {"text": "research the history of Kyoto"})
    assert until(lambda: adapter.in_flight_for("deep-research") > 0, timeout=8), (
        "the research never reached the model"
    )
    return server, base, seen


def test_talking_during_research_is_not_marked_as_an_interruption() -> None:
    """The conversation was not answering, so nothing was interrupted. Judged by "is
    anything in flight" it was, because the long job is -- and the page drew "you spoke
    while the answer was still arriving" over a conversation that had been idle."""
    release = threading.Event()

    def model(ask) -> str:  # type: ignore[no-untyped-def]
        if ask.descriptor == "deep-research":
            release.wait(timeout=HELD)
            return "what it found"
        return "an ordinary answer"

    server, base, seen = _research_running(model)
    try:
        post(base, "/say", {"text": "meanwhile, name one temple"})
        assert until(lambda: any(m["kind"] == "reply" for m in seen), timeout=8)
        time.sleep(0.2)
        assert interruptions(seen) == [], (
            f"a message during background work was marked: {marks(seen)}"
        )
    finally:
        release.set()
        server.stop()
        server.httpd.shutdown()


def test_talking_during_research_does_not_cut_the_next_answer_short() -> None:
    """The worse half of the same mistake. Treating it as a barge-in ran the abandon path
    against an idle conversation, setting the give-up flag it reads between writes -- so
    the answer to the new question stopped part way through.

    The words are spaced out on purpose. A blocking read takes everything waiting on the
    pipe, so chunks produced back to back arrive in one read and are all written before
    the flag is next looked at -- which hides the bug rather than fixing it.
    """
    release = threading.Event()

    def model(ask) -> Iterator[str]:  # type: ignore[no-untyped-def]
        if ask.descriptor == "deep-research":
            release.wait(timeout=HELD)
            yield "what it found"
            return
        for word in ("first ", "second ", "third"):
            time.sleep(0.15)
            yield word

    server, base, seen = _research_running(model)
    try:
        post(base, "/say", {"text": "meanwhile, name one temple"})
        assert until(
            lambda: "third" in " ".join(str(m.get("text", "")) for m in seen), timeout=8
        ), f"the answer was cut short: {[m.get('text') for m in seen if m['kind'] == 'reply']}"
    finally:
        release.set()
        server.stop()
        server.httpd.shutdown()


def test_barging_in_on_the_conversation_is_still_marked() -> None:
    """The control, so the fix above does not simply switch the mark off."""
    release = threading.Event()

    def model(ask) -> str:  # type: ignore[no-untyped-def]
        release.wait(timeout=HELD)
        return "a slow answer"

    session, adapter, source = build_session(load_case(CASE), model=model)
    server = serve(session, adapter, source, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    seen = watch(server)
    try:
        post(base, "/say", {"text": "name three temples"})
        assert until(lambda: adapter.in_flight_for("converse") > 0, timeout=8)
        post(base, "/say", {"text": "actually make it Osaka"})
        assert until(lambda: marks(seen) == ["barge-in"], timeout=8), f"marks: {marks(seen)}"
    finally:
        release.set()
        server.stop()
        server.httpd.shutdown()


def test_every_running_job_is_shown_not_just_the_latest() -> None:
    """`session.pending_task` cannot answer this on its own: an actuator holds a *value*,
    so a second job overwrites the first in it. The page is keyed by job instead.

    Driven through the bookkeeping rather than through two live research jobs, because
    two of those do not currently work -- they share one reply pipe and take each other's
    answers, so a test built on them is a race. What is under test here is the indicator.
    """
    from zeos.core.ids import JobId

    server = ChatServer(*build_session(load_case(CASE))[:3])
    stream = server.watch()
    drain(stream)

    server._note_task(JobId(3), "looking into the history of Kyoto")  # pyright: ignore[reportPrivateUsage]
    server._note_task(JobId(5), "looking into Osaka street food")  # pyright: ignore[reportPrivateUsage]

    shown = tasks(drain(stream))[-1]
    assert len(shown) == 2, f"only one job was shown: {shown}"
    assert "the history of Kyoto" in shown[0] and "Osaka street food" in shown[1]


def test_one_job_finishing_does_not_clear_the_others() -> None:
    """The failure the shared world object produces: the first job to finish writes
    `none` into the one line and the strip goes dark while the other is still working."""
    from zeos.core.ids import JobId

    server = ChatServer(*build_session(load_case(CASE))[:3])
    stream = server.watch()
    drain(stream)
    server._note_task(JobId(3), "looking into the history of Kyoto")  # pyright: ignore[reportPrivateUsage]
    server._note_task(JobId(5), "looking into Osaka street food")  # pyright: ignore[reportPrivateUsage]
    drain(stream)

    server._note_task(JobId(3), "none")  # pyright: ignore[reportPrivateUsage]
    shown = tasks(drain(stream))[-1]
    assert shown == ["looking into Osaka street food"], f"wrong job left showing: {shown}"

    server._note_task(JobId(5), "none")  # pyright: ignore[reportPrivateUsage]
    assert tasks(drain(stream))[-1] == [], "the strip outlived the last job"


def test_a_job_that_ends_without_clearing_its_line_is_still_removed() -> None:
    """`deep-research` clears `session.pending_task` on the way out, but a job that faults
    or is cancelled does not -- and that is exactly when a strip saying it is still
    working would be worst."""
    from zeos.core.ids import JobId

    server = ChatServer(*build_session(load_case(CASE))[:3])
    stream = server.watch()
    drain(stream)

    server._note_task(JobId(7), "looking into something")  # pyright: ignore[reportPrivateUsage]
    assert tasks(drain(stream))[-1] == ["looking into something"]

    server._on_event([JobCompleted(clock=0, job=JobId(7), tokens_used=1)])  # pyright: ignore[reportPrivateUsage]
    assert tasks(drain(stream))[-1] == []


# -- two jobs speaking into one transcript ----------------------------------


def reports(seen: list[dict[str, object]]) -> list[dict[str, object]]:
    return [m for m in seen if m["kind"] == "report"]


def test_the_long_jobs_findings_do_not_enter_the_conversation() -> None:
    """They arrive as a document instead. Run into the transcript they bury whatever the
    conversation is doing and read as though the chatbot had started rambling -- and they
    are an answer to something asked many turns ago, not a turn."""

    def model(ask) -> Iterator[str]:  # type: ignore[no-untyped-def]
        if ask.descriptor == "deep-research":
            yield "FINDINGS about temples"
            return
        yield "ANSWER"

    session, adapter, source = build_session(load_case(CASE), model=model)
    server = serve(session, adapter, source, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    seen = watch(server)
    try:
        post(base, "/say", {"text": "research the history of Kyoto"})
        assert until(lambda: reports(seen), timeout=8), "no report was offered"

        spoken = " ".join(str(m["text"]) for m in seen if m["kind"] == "reply")
        assert "FINDINGS" not in spoken, f"the findings leaked into the transcript: {spoken}"
        assert "research" in marks(seen), "the hand-off should be marked in the transcript"

        offered = reports(seen)[0]
        assert "the history of Kyoto" in str(offered["subject"])
        assert int(offered["words"]) > 0
    finally:
        server.stop()
        server.httpd.shutdown()


def test_a_report_can_be_opened_as_a_text_file() -> None:
    """The point of the icon: it is a document, and clicking it fetches the document."""

    def model(ask) -> Iterator[str]:  # type: ignore[no-untyped-def]
        yield "FINDINGS about temples" if ask.descriptor == "deep-research" else "ANSWER"

    session, adapter, source = build_session(load_case(CASE), model=model)
    server = serve(session, adapter, source, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    seen = watch(server)
    try:
        post(base, "/say", {"text": "research the history of Kyoto"})
        assert until(lambda: reports(seen), timeout=8)

        with urllib.request.urlopen(f"{base}/report/{reports(seen)[0]['id']}", timeout=5) as r:
            body = r.read().decode("utf-8")
        assert "FINDINGS about temples" in body
        assert "=====" not in body, (
            "the file repeated its own name: the chip above it already carries the subject"
        )

        with pytest.raises(urllib.error.HTTPError, match="404"):
            urllib.request.urlopen(f"{base}/report/nonesuch", timeout=5)
    finally:
        server.stop()
        server.httpd.shutdown()


def test_stop_halts_the_long_job_too() -> None:
    """Pressing stop while the long job worked did nothing at all, because the only thing
    being abandoned was a conversation that was not speaking. What the job managed before
    being stopped is still offered, partial: the words were produced and paid for."""

    def model(ask) -> Iterator[str]:  # type: ignore[no-untyped-def]
        if ask.descriptor == "deep-research":
            for i in range(200):
                time.sleep(0.02)
                yield f"R{i} "
            return
        yield "an answer"

    session, adapter, source = build_session(load_case(CASE), model=model)
    server = serve(session, adapter, source, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    seen = watch(server)
    try:
        post(base, "/say", {"text": "research the history of Kyoto"})
        assert until(lambda: adapter.in_flight_for("deep-research") == 1, timeout=8)
        time.sleep(0.5)  # let some of it actually be written

        post(base, "/stop")
        assert until(lambda: adapter.in_flight == 0, timeout=5), "the request was left in flight"
        assert until(lambda: reports(seen), timeout=5), "the partial report was never offered"
        assert interruptions(seen) == ["stopped"], f"marks: {marks(seen)}"

        # Stopped part way, so it must be short of the two hundred it would have written.
        assert int(reports(seen)[0]["words"]) < 200, "the job ran to completion after stop"
    finally:
        server.stop()
        server.httpd.shutdown()


def test_barging_in_does_not_bin_the_research_you_asked_for() -> None:
    """Stop means stop everything; typing does not. A question asked while the long job
    works is not a reason to throw away the work."""

    def model(ask) -> str:  # type: ignore[no-untyped-def]
        if ask.descriptor == "deep-research":
            time.sleep(HELD)
            return "found"
        return "an answer"

    session, adapter, source = build_session(load_case(CASE), model=model)
    server = serve(session, adapter, source, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    try:
        post(base, "/say", {"text": "research the history of Kyoto"})
        assert until(lambda: adapter.in_flight_for("deep-research") == 1, timeout=8)
        post(base, "/say", {"text": "meanwhile name one temple"})
        time.sleep(0.5)
        assert adapter.in_flight_for("deep-research") == 1, "the research was abandoned"
    finally:
        server.stop()
        server.httpd.shutdown()


def test_stopping_clears_every_background_job_from_the_strip() -> None:
    """Two research jobs share one reply pipe, so the sentinel meant to wake them both is
    taken whole by whichever reads first: one exits, the other stays blocked for ever.
    Measured before the fix -- the second job's line sat in the strip claiming to be
    working long after it had been given up on.

    Nothing in the kernel can reach a job that is blocked rather than suspended, so it
    cannot be cancelled. What the driver can do is stop reporting work it has abandoned.
    """

    def model(ask) -> Iterator[str]:  # type: ignore[no-untyped-def]
        if ask.descriptor == "deep-research":
            for i in range(400):
                time.sleep(0.02)
                yield f"R{i} "
            return
        yield "an answer"

    session, adapter, source = build_session(load_case(CASE), model=model)
    server = serve(session, adapter, source, port=0)
    base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
    seen = watch(server)
    try:
        post(base, "/say", {"text": "research the history of Kyoto"})
        assert until(lambda: adapter.in_flight_for("deep-research") == 1, timeout=8)
        post(base, "/say", {"text": "look into Osaka street food"})
        assert until(lambda: len(tasks(seen)[-1]) == 2, timeout=8), f"{tasks(seen)[-1]}"

        post(base, "/stop")
        assert until(lambda: tasks(seen)[-1] == [], timeout=8), (
            f"a job the driver gave up on is still shown as working: {tasks(seen)[-1]}"
        )
    finally:
        server.stop()
        server.httpd.shutdown()


def test_a_job_stopped_before_it_wrote_anything_offers_no_document() -> None:
    """An empty report is a chip that opens on nothing."""
    from zeos.core.ids import JobId

    server = ChatServer(*build_session(load_case(CASE))[:3])
    stream = server.watch()
    drain(stream)

    server._descriptor_of[JobId(9)] = "deep-research"  # pyright: ignore[reportPrivateUsage]
    server._note_task(JobId(9), "looking into nothing much")  # pyright: ignore[reportPrivateUsage]
    server._reports["9"] = Report(job="9", subject="nothing much")  # pyright: ignore[reportPrivateUsage]
    drain(stream)

    server._retire_background()  # pyright: ignore[reportPrivateUsage]
    frames = drain(stream)
    assert not [m for m in frames if m["kind"] == "report"], "an empty document was offered"
    assert tasks(frames)[-1] == [], "the strip kept a job that produced nothing"


# -- who is asking ----------------------------------------------------------


def _outbox_server():  # type: ignore[no-untyped-def]
    from zeos_chat.mail import MailAdapter, MailSettings, SimulatedSender

    sender = SimulatedSender()
    mail = MailAdapter(MailSettings(recipient="you@example.com"), sender)
    session, adapter, source = build_session(load_case(CASE), mail=mail)
    server = serve(session, adapter, source, mail, port=0)
    return server, f"http://127.0.0.1:{server.httpd.server_address[1]}", sender


def test_the_same_press_sends_for_the_owner_and_is_refused_for_a_guest() -> None:
    """The barrier, over HTTP. The request is identical but for which door it goes
    through, and the server does not test the speaker against anything -- it cannot, it
    only has two pipe names."""
    for speaker, expect_sent, expect_marks in (("owner", 1, []), ("guest", 0, ["refused"])):
        server, base, sender = _outbox_server()
        seen = watch(server)
        try:
            post(base, "/email", {"subject": "Kyoto", "body": "you: hello", "speaker": speaker})
            assert until(lambda: bool(sender.sent) or marks(seen) == ["refused"], timeout=8), (
                f"{speaker}: nothing happened at all"
            )
            time.sleep(0.3)
            assert len(sender.sent) == expect_sent, f"{speaker}: sent {len(sender.sent)}"
            assert marks(seen) == expect_marks, f"{speaker}: marks {marks(seen)}"
        finally:
            server.stop()
            server.httpd.shutdown()


def test_the_door_answers_into_the_journal_rather_than_the_conversation() -> None:
    """The kernel echoes what it did with an utterance through the same sink a reply uses.
    Left alone, the page shows "spawning send-email(); priority 40 requested, running at
    60" as something the chatbot said."""
    server, base, _ = _outbox_server()
    seen = watch(server)
    try:
        post(base, "/email", {"subject": "Kyoto", "body": "you: hello", "speaker": "owner"})
        assert until(lambda: any(m["kind"] == "mail" for m in seen), timeout=8)

        spoken = " ".join(str(m["text"]) for m in seen if m["kind"] == "reply")
        assert "spawning" not in spoken, f"the door's answer reached the transcript: {spoken}"
        assert any(m["kind"] == "kernel" and m.get("class") == "door" for m in seen), (
            "the door's answer went nowhere at all"
        )
    finally:
        server.stop()
        server.httpd.shutdown()
