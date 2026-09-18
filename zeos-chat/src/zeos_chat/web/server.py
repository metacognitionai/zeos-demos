"""The browser as a device adapter.

Everything the kernel refuses to do, a driver does; on the web the driver is this. A
message typed into a page becomes ``kernel.deliver(user.messages, ...)`` and a drained
sink becomes a line in the browser. Nothing here decides anything about the conversation
-- it cannot, because it has no way to: the only verbs it has are deliver and drain.

**One thread owns the kernel.** ``Session.step`` runs on it and nothing else touches the
kernel at all; HTTP handlers only call ``Session.deliver``, which queues. Replies and
journal events go out through a fan-out queue per connected browser, so a slow reader
cannot stall the conversation.

Stdlib only, on the ``demo/space-invaders/web/`` pattern: a `ThreadingHTTPServer`, the
assets inlined into one page, and Server-Sent Events for the stream. The traffic is one
message in and a line of text out, which SSE carries without a framework or a websocket
library.
"""

from __future__ import annotations

import json
import queue
import threading
import time
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from zeos.core.events import (
    Event,
    FaultRaised,
    IntegrityDemoted,
    JobBlocked,
    JobPreempted,
    JobResumed,
    JobSpawned,
    JobWoken,
    PipeWritten,
    VectorFired,
)
from zeos.core.ids import PipeName

from zeos_chat.jobs import ProgramSource
from zeos_chat.llm import LlmAdapter
from zeos_chat.mail import SUBJECT_SEPARATOR, Letter, MailAdapter
from zeos_chat.session import Session

__all__ = ["ChatServer", "page", "serve"]

STATIC = Path(__file__).resolve().parent / "static"

MESSAGES = PipeName("user.messages")
ARRIVALS = PipeName("user.arrivals")
CANCEL = PipeName("user.cancel")
MAIL = PipeName("mail.requests")
LETTERS = PipeName("mail.letters")

#: What the mail doorbell carries. One token, like `RING`: the letter goes to
#: `mail.letters` and this says only that somebody asked for it to be sent.
ASKED = "send"

#: What the doorbell carries. One token, and never the message: the words go to
#: `user.messages` and this says only that somebody spoke. See the case's pipes.yaml.
RING = "message"

#: How long a step loop rests when nothing is runnable. A parked conversation costs no
#: forward passes, and this is what stops it costing CPU either.
IDLE_SECONDS = 0.02


def page() -> str:
    """The single page, with its stylesheet and script inlined."""
    built = (STATIC / "index.html").read_text("utf-8")
    for marker, name in (("/*CSS*/", "chat.css"), ("/*JS*/", "chat.js")):
        built = built.replace(marker, (STATIC / name).read_text("utf-8"))
    return built


def _kernel_line(event: Event) -> dict[str, str] | None:
    """One journal event as the panel shows it, or None for the ones it does not.

    The panel reads the journal, never the transcript. "The reflex preempted the answer
    within one token boundary" is a structural fact; "the reply mentioned Kyoto" is not.
    """
    match event:
        case JobSpawned():
            return {
                "class": "spawn",
                "text": f"{event.descriptor} spawned at priority {int(event.priority)}",
            }
        case JobBlocked():
            return {"class": "block", "text": f"job {event.job} waiting on {event.pipe}"}
        case JobWoken():
            return {"class": "wake", "text": f"job {event.job} woken by {event.pipe}"}
        case VectorFired():
            return {
                "class": "vector",
                "text": f"{event.vector} fired {event.handler} at priority {int(event.priority)}",
            }
        case JobPreempted():
            return {
                "class": "preempt",
                "text": f"job {event.job} preempted by job {event.by_job}, stack depth {event.stack_depth}",
            }
        case JobResumed():
            changed = ", ".join(f"{d.obj}: {d.before} -> {d.after}" for d in event.dirty)
            return {
                "class": "resume",
                "text": f"job {event.job} resumed; {changed or 'nothing it reads changed'}",
            }
        case PipeWritten() if event.job is not None:
            return {"class": "write", "text": f"job {event.job} wrote {event.pipe}"}
        case IntegrityDemoted():
            return {
                "class": "demote",
                "text": f"job {event.job} demoted {int(event.from_integrity)} -> {int(event.to_integrity)}",
            }
        case FaultRaised():
            return {"class": "fault", "text": f"job {event.job}: {event.fault} -- {event.detail}"}
        case _:
            return None


class ChatServer:
    """One conversation, one page, and the thread between them."""

    def __init__(
        self,
        session: Session,
        adapter: LlmAdapter | None = None,
        source: ProgramSource | None = None,
        mail: MailAdapter | None = None,
    ) -> None:
        self.session = session
        #: The outbox, so the page can say whether a send would be real and report what
        #: happened to one. The adapter transmits; this only reads it.
        self._mail = mail
        #: Needed for the busy signal, and the reason is the redesign: a job waiting on
        #: the model is parked, so nothing is runnable and the kernel looks idle. It is
        #: not idle, it is waiting -- and that is exactly when a person is watching for a
        #: sign of life.
        self._adapter = adapter
        #: The programs, so a running job can be told to give up its turn.
        self._source = source
        self._watchers: list[queue.Queue[str]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._busy = False
        session._on_reply = self._on_reply  # pyright: ignore[reportPrivateUsage]
        session._on_event = self._on_event  # pyright: ignore[reportPrivateUsage]
        if mail is not None:
            # Same shape as the two above: the driver is what shows a result, so the
            # driver is what asks to be told.
            mail.on_result = self.posted

    # -- the kernel thread --------------------------------------------------

    def run(self) -> None:
        """Turn the conversation until asked to stop. Only this thread touches the kernel."""
        self.session.boot()
        while not self._stop.is_set():
            ran = self.session.step()
            self._set_busy(ran or self._outstanding())
            if not ran:
                # Nothing runnable: the conversation is parked on its read, which is the
                # state it spends most of its life in and costs nothing to be in.
                time.sleep(IDLE_SECONDS)
        self.session.close()

    def _outstanding(self) -> bool:
        """Whether there is work the person is waiting on, which is not the same as work
        the scheduler can run.

        Three things count. A queued message not yet picked up -- otherwise a message
        posted from an HTTP thread is un-marked by the very next quiescent tick. A job
        that ran. And **a request the model has not answered**: the job that asked is
        parked, so the kernel is quiescent and looks idle, when in fact the whole system
        is waiting on one device. Reading only the scheduler there is how the logo came to
        rest while a person watched an empty page.
        """
        if self.session.has_pending_input:
            return True
        return self._adapter is not None and self._adapter.in_flight > 0

    def stop(self) -> None:
        self._stop.set()

    # -- from any thread ----------------------------------------------------

    def say(self, text: str) -> None:
        """A message from the page. Two deliveries, and only one carries the words."""
        # Read before delivering: a message is a barge-in if an answer was still coming
        # when it was sent, and a moment later that is no longer knowable.
        interrupting = self._outstanding()
        if interrupting:
            # Barge-in abandons the answer in progress, exactly as stop does. Without this
            # the vector fired, the mark was drawn, and the old answer carried on writing
            # itself out underneath it -- so the page showed the tail of the abandoned
            # reply as though it were the reply to the new message.
            #
            # Before the message is delivered, not after: both go through the session's
            # queue in order, and the job has to be woken off the reply pipe before what
            # wakes it next is waiting on stdin.
            self._abandon()
        self.session.deliver(MESSAGES, text)
        self.session.deliver(ARRIVALS, RING)
        if interrupting:
            # Before the message itself, so the mark sits above it in the transcript.
            self._publish("mark", {"mark": "barge-in"})
        self._publish("said", {"text": text})
        # Busy from the moment there is work, not from the moment a tick finishes. The
        # first tick of a reply is one decode, and under an API seat a decode is a call
        # that takes seconds -- so reporting afterwards leaves the page still for exactly
        # the span the person is watching for a sign of life.
        self._set_busy(True)

    def email(self, subject: str, body: str) -> None:
        """A request to mail the conversation. One delivery, and the kernel does the rest.

        Note what is *not* here: no check that mail is configured, no composing, no
        sending. The write fires the `mail-requested` vector, the kernel dispatches
        `send-email` at priority 40, and that job's write to the outbox is what may be
        refused -- by authority, or because its integrity was lowered by something it
        read. A server that decided any of that in advance would be deciding the thing
        the capability check exists to decide.
        """
        # The letter first, the doorbell second -- the order the conversation's own two
        # lines use. A firing that arrived before the content would dispatch a job that
        # then blocks on an empty pipe.
        self.session.deliver(LETTERS, SUBJECT_SEPARATOR.join((subject, body)))
        self.session.deliver(MAIL, ASKED)
        self._set_busy(True)

    def interrupt(self) -> None:
        """Stop, in the three places it has to happen.

        The kernel-side reflex takes the machine at the next token boundary. The device
        abandons whatever the model was asked for, so nothing arrives later that the
        person has already declined. And the job is told, because a job part way through
        writing an answer is not waiting on anything and cannot be woken.
        """
        was_working = self._outstanding()
        self.session.deliver(CANCEL, "stop")
        dropped = self._abandon()
        if was_working or dropped:
            # Only when there was something to stop. A mark for a stop that interrupted
            # nothing would be the page inventing an event the kernel never had.
            self._publish("mark", {"mark": "stopped"})
        self._set_busy(False)

    def watch(self) -> queue.Queue[str]:
        """A stream for one browser, opening with the state it would otherwise have to
        wait for a change to learn -- a tab opened mid-reply should animate at once."""
        stream: queue.Queue[str] = queue.Queue()
        with self._lock:
            self._watchers.append(stream)
            stream.put(self._frame("busy", {"busy": self._busy}))
            # Whether a send would actually leave the machine. The page says so on the
            # button, because "simulated" is the difference between a demonstration and
            # an email somebody receives.
            stream.put(
                self._frame(
                    "mailer",
                    {
                        "ready": self._mail is not None,
                        # The mode, not a boolean: "opens a draft" and "sends" are
                        # different promises and the button has to make the right one.
                        "mode": "simulated" if self._mail is None else self._mail.mode,
                        "to": "" if self._mail is None else self._mail.config.to,
                    },
                )
            )
        return stream

    def unwatch(self, stream: queue.Queue[str]) -> None:
        with self._lock:
            if stream in self._watchers:
                self._watchers.remove(stream)

    # -- internals ----------------------------------------------------------

    def _abandon(self) -> int:
        """Give up on the answer in progress, in both places it is held.

        The device forgets the request -- so the stream is closed at the source rather than
        paid for and discarded -- and the job is told, because the two states a
        conversation can be caught in need different things. Parked on the reply pipe, it
        is woken by what the device sends back. Part way through writing an answer out, it
        is waiting on nothing and only the flag reaches it.
        """
        dropped = 0 if self._adapter is None else self._adapter.abandon("converse")
        if self._source is not None:
            self._source.abandon("converse")
        return dropped

    def posted(self, letter: Letter, outcome: str) -> None:
        """What became of a letter. Called by the adapter, on the kernel's thread."""
        self._publish("mail", {"to": letter.to, "subject": letter.subject, "outcome": outcome})
        self._set_busy(False)

    def _on_reply(self, pipe: PipeName, text: str) -> None:
        self._publish("reply", {"text": text})

    def _on_event(self, new: Sequence[Event]) -> None:
        for event in new:
            line = _kernel_line(event)
            if line is not None:
                self._publish("kernel", line)

    def _set_busy(self, busy: bool) -> None:
        """Whether the kernel has work. The logo animates on this and rests otherwise, so
        the animation means something rather than decorating a wait.

        Under the lock and published inside it, because two threads set this: the kernel
        thread as it ticks, and an HTTP thread the moment it queues a message."""
        with self._lock:
            if busy == self._busy:
                return
            self._busy = busy
            line = self._frame("busy", {"busy": busy})
            for stream in self._watchers:
                stream.put(line)

    def _frame(self, kind: str, payload: dict[str, Any]) -> str:
        """One SSE frame. ``kind`` is the envelope and goes on last, so a payload of its
        own cannot quietly take its place -- which is a mistake already made once."""
        return f"data: {json.dumps({**payload, 'kind': kind})}\n\n"

    def _publish(self, kind: str, payload: dict[str, Any]) -> None:
        line = self._frame(kind, payload)
        with self._lock:
            for stream in self._watchers:
                stream.put(line)


def _handler(server: ChatServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # noqa: A002 - the base class's name
            """Silent: the interesting log is the journal, and it is on the page."""

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - the base class's name
            if self.path in ("/", "/index.html"):
                self._send(200, page().encode("utf-8"), "text/html; charset=utf-8")
            elif self.path == "/events":
                self._stream()
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self) -> None:  # noqa: N802 - the base class's name
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/say":
                text = str(body.get("text", "")).strip()
                if text:
                    server.say(text)
            elif self.path == "/stop":
                server.interrupt()
            elif self.path == "/email":
                subject = str(body.get("subject", "")).strip()
                transcript = str(body.get("body", "")).strip()
                if transcript:
                    server.email(subject or "Your ZEOS Chat conversation", transcript)
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            self._send(200, b'{"ok":true}', "application/json")

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            stream = server.watch()
            try:
                while True:
                    try:
                        self.wfile.write(stream.get(timeout=15).encode("utf-8"))
                    except queue.Empty:
                        # A comment frame: keeps the connection open through a proxy, and
                        # tells us the browser has gone when it fails.
                        self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                server.unwatch(stream)

    return Handler


def serve(
    session: Session,
    adapter: LlmAdapter | None = None,
    source: ProgramSource | None = None,
    mail: MailAdapter | None = None,
    *,
    host: str = "127.0.0.1",
    port: int = 8181,
) -> ChatServer:
    """Start the kernel thread and the HTTP server. Returns once both are up."""
    chat = ChatServer(session, adapter, source, mail)
    threading.Thread(target=chat.run, name="kernel", daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), _handler(chat))
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    chat.httpd = httpd  # pyright: ignore[reportAttributeAccessIssue]
    return chat
