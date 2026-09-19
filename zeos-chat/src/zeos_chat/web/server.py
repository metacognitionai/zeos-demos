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
import re
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from zeos.core.events import (
    EchoedBack,
    Event,
    FaultRaised,
    IntegrityDemoted,
    JobBlocked,
    JobCancelled,
    JobCompleted,
    JobPreempted,
    JobResumed,
    JobSpawned,
    JobWoken,
    PipeWritten,
    VectorFired,
)
from zeos.core.ids import FaultKind, PipeName

from zeos_chat.jobs import ProgramSource
from zeos_chat.llm import LlmAdapter
from zeos_chat.mail import SUBJECT_SEPARATOR, Letter, MailAdapter
from zeos_chat.session import Session

__all__ = ["ChatServer", "Report", "page", "serve"]

STATIC = Path(__file__).resolve().parent / "static"

MESSAGES = PipeName("user.messages")
ARRIVALS = PipeName("user.arrivals")
CANCEL = PipeName("user.cancel")
CONSOLE = PipeName("owner.console")
INTERCOM = PipeName("guest.intercom")
TASK = PipeName("actuators.task")
REPLIES = PipeName("user.replies")
LETTERS = PipeName("mail.letters")
REPORT = PipeName("research.report")

#: What the page says at a door to ask for the conversation to be sent. It has to be
#: one of the phrasings `send-email` declares; anything else is refused by the kernel
#: with "nothing is listening", which is the front door working rather than failing.
ASKED = "email me this conversation"

#: And what it says to ask for the findings instead. A different phrasing, matched
#: by a different descriptor, reading a different pipe -- which is the whole reason
#: the two can end differently.
ASKED_REPORT = "email me the research"

#: Which door each speaker reaches. Identity is the door's, not the sentence's: there
#: is no field in the request saying who this is, because a field like that is a field
#: a sentence could fill in.
DOORS = {"owner": CONSOLE, "guest": INTERCOM}

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


@dataclass
class Report:
    """What a background job wrote, collected into one thing a person can open.

    The long job's findings do not belong in the conversation. They arrive over minutes,
    they are pages long, and they are an answer to a question asked ten turns ago -- run
    into the transcript they bury whatever the conversation is doing and read as though
    the chatbot had started rambling. Collected, they are what they actually are: a
    document that was produced, which you open when you want it.

    Atomic for the same reason. A reply is written a word at a time so an interruption can
    cut it mid-sentence; a report has no such need, because nobody is waiting on it with
    their next sentence half typed.
    """

    job: str
    subject: str
    pieces: list[str] = field(default_factory=list[str])
    done: bool = False

    @property
    def text(self) -> str:
        return " ".join(self.pieces)

    @property
    def words(self) -> int:
        return len(self.text.split())

    def as_file(self) -> str:
        """The report as plain text.

        No heading. The chip that opens it already carries the subject, immediately
        above, so a title inside the document said the same thing twice with a rule
        underneath it.
        """
        return f"{self.text}\n"


#: The faults that mean somebody was refused, as opposed to something breaking.
REFUSALS = (FaultKind.CAPABILITY, FaultKind.PRIVILEGE)


def _pipe_in(detail: str) -> str:
    """The pipe a capability fault names, pulled out of the kernel's own wording."""
    quoted = re.findall(r"'([^']+)'", detail)
    return quoted[-1] if quoted else "that pipe"


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
        #: What each background job says it is doing, by job id.
        #:
        #: Keyed by job rather than read from `session.pending_task` directly, because
        #: that world object holds a *value* and there can be more than one job. Two
        #: research jobs overwrite each other in it, and the first to finish writes
        #: `none` and clears the line while the other is still working. The world says
        #: what was last set; this says what is currently running, which is what the
        #: page is for.
        self._tasks: dict[Any, str] = {}
        #: The job whose words are currently going out, so a change of speaker can
        #: start a new bubble. Two jobs write `user.replies` and the drained sink says
        #: nothing about who wrote it -- the journal does.
        self._speaking: Any = None
        #: Which descriptor each job runs, from the journal. The only way to tell a
        #: long job's output from the conversation's once both are on one sink.
        self._descriptor_of: dict[Any, str] = {}
        #: Reports being collected, and finished ones the page can open, by job.
        self._reports: dict[str, Report] = {}
        #: What a door said back, waiting to be recognised when it comes out of the
        #: reply sink. The kernel echoes its decision to the speaker through the same
        #: pipe a reply uses, so without this the page renders "spawning send-email();
        #: priority 40 requested, running at 60" as something the chatbot said.
        self._echoed: list[str] = []
        #: Who last asked for something at a door. The fault that refuses them names
        #: the pipe but not the speaker, and "refused" on its own is a worse answer
        #: than "the guest was refused the outbox".
        self._asked_as = "owner"
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

    def _answering(self) -> bool:
        """Whether *the conversation* is mid-answer, which is not the same as busy.

        A background job holds the machine's attention too, so `_outstanding` is true
        while research runs -- correct for the logo, wrong for an interruption. Judged by
        it, a question asked during research was marked "you spoke while the answer was
        still arriving" when no answer was arriving, and, worse, the barge-in path
        abandoned an idle conversation, which cut its *next* reply short after one word.
        """
        return self._adapter is not None and self._adapter.in_flight_for("converse") > 0

    def stop(self) -> None:
        self._stop.set()

    # -- from any thread ----------------------------------------------------

    def say(self, text: str) -> None:
        """A message from the page. Two deliveries, and only one carries the words."""
        # Read before delivering: a message is a barge-in if an answer was still coming
        # when it was sent, and a moment later that is no longer knowable.
        interrupting = self._answering()
        if interrupting:
            # Barge-in abandons the answer in progress, exactly as stop does. Without this
            # the vector fired, the mark was drawn, and the old answer carried on writing
            # itself out underneath it -- so the page showed the tail of the abandoned
            # reply as though it were the reply to the new message.
            #
            # Before the message is delivered, not after: both go through the session's
            # queue in order, and the job has to be woken off the reply pipe before what
            # wakes it next is waiting on stdin.
            self._abandon("converse")
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

    def email(
        self, subject: str, body: str, speaker: str = "owner", what: str = "conversation"
    ) -> None:
        """Ask, as somebody, for the conversation to be sent.

        Note what is *not* here: no check that mail is configured, no composing, no
        sending, and no test of whether this speaker is allowed to ask. The request goes
        through the door belonging to whoever is speaking; the kernel compiles it, spawns
        `send-email`, and narrows that job to the speaker's authority. The write to the
        outbox is what may then be refused. A server that decided any of that in advance
        would be deciding the thing the capability check exists to decide.

        The speaker picks a *door*, not a field in the request. A field saying who this is
        would be a field a sentence could fill in.
        """
        self._asked_as = speaker if speaker in DOORS else "owner"
        if what == "report":
            if not self._findings_waiting():
                # A second press, with nothing left to hand over. Said here rather than
                # dispatched, because a job spawned now would block on an empty pipe for
                # ever and the press would go unanswered -- which is how this was found.
                #
                # This is not the page deciding the request. The *first* press went to
                # the kernel and the kernel refused it; what the page is reporting is the
                # documented behaviour of an ordinary pipe, checked against the pipe
                # itself rather than assumed.
                self._publish(
                    "mark",
                    {
                        "mark": "spent",
                        "text": (
                            "nothing left to send — an ordinary pipe carries a message "
                            "once, and the findings went to the job that was refused"
                        ),
                    },
                )
                return
            # Nothing to hand over: the findings are already on a pipe, written by the job
            # that went and read them and carrying that job's integrity. Passing them
            # through the page and back would launder exactly the provenance this exists
            # to demonstrate -- the page writes at its own integrity, so a re-delivered
            # report would clear the bar and the research would actually be sent.
            self.session.deliver(DOORS.get(speaker, CONSOLE), ASKED_REPORT)
            self._set_busy(True)
            return
        # The letter first, the request second. A job dispatched before the content is
        # there blocks on an empty pipe.
        self.session.deliver(LETTERS, SUBJECT_SEPARATOR.join((subject, body)))
        self.session.deliver(DOORS.get(speaker, CONSOLE), ASKED)
        self._set_busy(True)

    def _findings_waiting(self) -> bool:
        """Whether anything is on the report pipe for a job to read.

        Asked of the kernel rather than tracked here, so the answer cannot drift from
        what a spawned job would actually find.
        """
        pipe = self.session.kernel.pipes.get(REPORT)
        return pipe is not None and pipe.available > 0

    def interrupt(self) -> None:
        """Stop, in the three places it has to happen.

        The kernel-side reflex takes the machine at the next token boundary. The device
        abandons whatever the model was asked for, so nothing arrives later that the
        person has already declined. And the job is told, because a job part way through
        writing an answer is not waiting on anything and cannot be woken.
        """
        # Everything outstanding, not just the conversation: pressing stop while the long
        # job is pouring text onto the page did nothing at all, because the only thing
        # being abandoned was a conversation that was not speaking.
        was_working = self._outstanding()
        self.session.deliver(CANCEL, "stop")
        dropped = self._abandon()
        self._retire_background()
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
            # A tab opened while background jobs are running should say so at once.
            stream.put(self._frame("task", {"tasks": self._running_tasks()}))
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

    def report(self, name: str) -> Report | None:
        """A finished report, by id. Unfinished ones are not offered."""
        with self._lock:
            report = self._reports.get(name)
            return report if report is not None and report.done else None

    def unwatch(self, stream: queue.Queue[str]) -> None:
        with self._lock:
            if stream in self._watchers:
                self._watchers.remove(stream)

    # -- internals ----------------------------------------------------------

    #: What `_abandon` can give up on. Everything that answers through the model adapter,
    #: because "stop" means the words stop arriving -- a person watching the long job pour
    #: text onto the page does not care which job is producing it.
    ABANDONABLE = ("converse", "deep-research")

    def _retire_background(self) -> None:
        """Close out the background jobs just abandoned: stop claiming they are running,
        and offer whatever they managed to write.

        The kernel cannot be asked to do this, and that is the whole reason it is here.
        Two jobs of one descriptor share a reply pipe -- pipe names in frontmatter are
        literal, so a second job of the same descriptor binds the same ones -- and a
        blocking read takes everything waiting. The sentinel meant to wake both is
        therefore taken whole by whichever reads first, and the other stays blocked for
        ever. Measured: stop two research jobs and one exits, one does not, and its line
        sat in the strip claiming to be working long after it had been given up on.

        So the driver retires what it abandoned. The job is still parked in the kernel and
        this does not pretend otherwise; what it fixes is the page reporting work that will
        never produce anything.
        """
        with self._lock:
            jobs = [job for job in self._tasks if self._reporting(job)]
        for job in jobs:
            self._finish_report(job)
            self._note_task(job, "none")

    def _abandon(self, *descriptors: str) -> int:
        """Give up on answers in progress, in both places each is held.

        The device forgets the request -- so the stream is closed at the source rather than
        paid for and discarded -- and the job is told, because the two states a job can be
        caught in need different things. Parked on the reply pipe, it is woken by what the
        device sends back. Part way through writing an answer out, it is waiting on
        nothing and only the flag reaches it.
        """
        dropped = 0
        for descriptor in descriptors or self.ABANDONABLE:
            if self._adapter is not None:
                dropped += self._adapter.abandon(descriptor)
            if self._source is not None:
                self._source.abandon(descriptor)
        return dropped

    def posted(self, letter: Letter, outcome: str) -> None:
        """What became of a letter. Called by the adapter, on the kernel's thread."""
        self._publish("mail", {"to": letter.to, "subject": letter.subject, "outcome": outcome})
        self._set_busy(False)

    def _on_reply(self, pipe: PipeName, text: str) -> None:
        if text in self._echoed:
            # The door answering, not the conversation. It belongs with the other
            # structural facts rather than in the transcript, where it reads as the
            # chatbot narrating its own dispatch.
            self._echoed.remove(text)
            self._publish("kernel", {"class": "door", "text": text})
            return
        report = self._reports.get(str(self._speaking))
        if report is not None and not report.done:
            # Collected, not shown. What the long job writes is a document being built,
            # and a document being built is not a turn in a conversation.
            report.pieces.append(text)
            return
        self._publish("reply", {"text": text, "job": str(self._speaking)})

    def _finish_report(self, job: Any) -> None:
        """Close a report and offer it, once the job that was writing it has ended."""
        report = self._reports.get(str(job))
        if report is None or report.done:
            return
        report.done = True
        if not report.words:
            # A job that was stopped before it wrote anything has produced no document,
            # and an empty one is a chip that opens on nothing.
            return
        self._publish(
            "report",
            {
                "id": report.job,
                "subject": report.subject,
                "words": report.words,
            },
        )

    REPORTED_BY = "deep-research"

    def _reporting(self, job: Any) -> bool:
        """Whether this job's words are a report rather than conversation."""
        return self._descriptor_of.get(job) == self.REPORTED_BY

    def _on_event(self, new: Sequence[Event]) -> None:
        for event in new:
            if isinstance(event, EchoedBack):
                self._echoed.append(event.text)
            if isinstance(event, JobSpawned):
                self._descriptor_of[event.job] = str(event.descriptor)
            if isinstance(event, PipeWritten) and event.pipe == REPLIES and event.job is not None:
                # Who is about to be drained. The journal is reported before the sink is
                # emptied, and one tick is one job's command, so this is the writer of
                # whatever comes out next. Two jobs share `user.replies` and the drained
                # text says nothing about which wrote it; the page needs to know, or the
                # long job's findings run on into the conversation's bubble.
                self._speaking = event.job
            if isinstance(event, PipeWritten) and event.latched and event.pipe == TASK:
                # The write that latches `session.pending_task`, attributed to the job that
                # made it -- which is the part the world object itself cannot tell us.
                task = " ".join(event.text)
                self._note_task(event.job, task)
                if self._reporting(event.job) and task and task != "none":
                    # The subject, taken where the job itself records it rather than
                    # guessed from the text it goes on to write.
                    self._reports.setdefault(
                        str(event.job),
                        # Stripped once, here, so the chip and the file agree. The job
                        # writes "looking into X" because that line is what it is *doing*;
                        # the document is about X.
                        Report(
                            job=str(event.job),
                            subject=task.removeprefix("looking into ").strip() or task,
                        ),
                    )
            elif isinstance(event, FaultRaised) and event.fault in REFUSALS:
                # The barrier, in the transcript. The journal panel shows the fault as a
                # structural fact; this says what it meant to the person who asked.
                #
                # The two kinds are worth telling apart, because they answer different
                # questions. A capability fault is about *who asked*: this speaker was
                # never given that pipe. A privilege fault is about *what the job has
                # read*: the speaker holds the capability and the job still cannot use it,
                # because its integrity fell to the level of something it was exposed to.
                pipe = _pipe_in(event.detail)
                self._publish(
                    "mark",
                    {
                        "mark": "refused",
                        "text": (
                            f"refused — {self._asked_as} holds no capability for {pipe}"
                            if event.fault is FaultKind.CAPABILITY
                            else f"refused — the job read something untrusted, so it no "
                            f"longer clears the integrity {pipe} requires"
                        ),
                    },
                )
            elif isinstance(event, JobCompleted | JobCancelled):
                self._finish_report(event.job)
                # A job that ended without clearing its line would otherwise be shown as
                # working for ever. `deep-research` does clear it; a job that faults or is
                # cancelled does not, and that is exactly when it matters.
                self._note_task(event.job, "none")
            line = _kernel_line(event)
            if line is not None:
                self._publish("kernel", line)

    def _note_task(self, job: Any, task: str) -> None:
        """Record what one job is doing, and tell the page if the set changed."""
        with self._lock:
            before = self._running_tasks()
            if task and task != "none":
                self._tasks[job] = task
            else:
                self._tasks.pop(job, None)
            now = self._running_tasks()
            if now == before:
                return
            line = self._frame("task", {"tasks": now})
            for stream in self._watchers:
                stream.put(line)

    def _running_tasks(self) -> list[str]:
        """What the page shows, oldest job first. Call under the lock."""
        return [self._tasks[job] for job in sorted(self._tasks, key=str)]

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
            elif self.path.startswith("/report/"):
                report = server.report(self.path.removeprefix("/report/"))
                if report is None:
                    self._send(404, b"no such report", "text/plain; charset=utf-8")
                else:
                    self._send(200, report.as_file().encode("utf-8"), "text/plain; charset=utf-8")
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
                speaker = str(body.get("speaker", "owner")).strip()
                what = str(body.get("what", "conversation")).strip()
                if transcript or what == "report":
                    server.email(
                        subject or "Your ZEOS Chat conversation", transcript, speaker, what
                    )
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
