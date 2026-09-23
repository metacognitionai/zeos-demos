"""The browser as a device adapter.

Everything the kernel refuses to do, a driver does; on the web the driver is this. A
sentence typed into the page becomes ``Session.say``, a drained sink becomes a line in the
page, and a block dragged across the table becomes ``Session.disturb``. Nothing here
decides anything about blocks -- it cannot, because it has no way to: the only verbs it
has are say, disturb and watch.

**One thread owns the kernel.** ``Session.step`` runs on it and nothing else touches the
kernel at all; HTTP handlers only call ``say`` and ``disturb``, which queue. The table and
the journal go out through a fan-out queue per connected browser, so a slow reader cannot
stall the run.

Stdlib only: a ``ThreadingHTTPServer``, the assets served from ``static/``, and
Server-Sent Events for the stream. The traffic is one sentence in and a few short events
out, which SSE carries without a framework or a websocket library.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import webbrowser
from collections.abc import Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from zeos.core.events import (
    CompilationRefused,
    Event,
    FaultRaised,
    JobBlocked,
    JobCompleted,
    JobPreempted,
    JobSpawned,
    JobWoken,
    VectorFired,
    WorldWritten,
)
from zeos.core.ids import JobState
from zeos.descriptor.loader import load_case
from zeos.machine.seat import CommandSource

from zeos_blocks.session import Session

__all__ = ["BlocksServer", "serve"]

STATIC = Path(__file__).resolve().parent / "static"


def _journal_line(event: Event) -> dict[str, str] | None:
    """One journal event as a line for the panel, or None for the ones nobody needs.

    The key is ``tag`` rather than ``kind`` because the envelope ``_push`` builds already
    carries a ``kind``, and a payload key of the same name would quietly replace it --
    every journal line would arrive claiming to be whatever it was about.

    Deliberately a projection of the kernel's own record rather than a commentary written
    alongside it. "A capability fault named this pipe" is a structural fact; "the stacker
    decided to clear the block" is not, and does not appear here.
    """
    match event:
        case JobSpawned():
            return {"tag": "spawn", "text": f"job {event.job} spawned: {event.descriptor}"}
        case JobBlocked():
            return {"tag": "block", "text": f"job {event.job} waiting on {event.pipe}"}
        case JobPreempted():
            return {
                "tag": "preempt",
                "text": f"job {event.job} preempted by job {event.by_job} "
                f"at priority {event.by_priority}",
            }
        case VectorFired():
            return {
                "tag": "vector",
                "text": f"vector {event.vector} fired on {event.pipe}: {event.handler}",
            }
        case JobWoken():
            return {"tag": "wake", "text": f"job {event.job} woken"}
        case JobCompleted():
            return {"tag": "done", "text": f"job {event.job} finished"}
        case WorldWritten():
            return {"tag": "world", "text": f"{event.obj}: {event.before} -> {event.after}"}
        case FaultRaised():
            return {"tag": "fault", "text": f"{event.fault}: {event.detail}"}
        case CompilationRefused():
            return {"tag": "refused", "text": f"nothing compiled: {event.reason}"}
        case _:
            return None


class BlocksServer:
    """A workspace, a page, and the thread that turns the kernel."""

    def __init__(
        self,
        case: Path,
        source: CommandSource,
        *,
        pace: float = 0.09,
        settle: float = 0.8,
        journal: Path | None = None,
    ) -> None:
        bundle = load_case(case)
        self._watchers: list[queue.Queue[str]] = []
        self._lock = threading.Lock()
        #: Seconds per token boundary, and zero is a real setting.
        #:
        #: The kernel reads no clock and does not care; this is purely about a person
        #: being able to watch. It is worth having when the thing deciding moves is a
        #: few lines of Python, which answers faster than anything can be seen. It is
        #: worth nothing when the answer comes from an API call, which already takes
        #: longer than any pause worth adding, so `cli.py` passes zero for that planner
        #: rather than making a slow run slower.
        self._pace = pace
        #: Seconds a move is given to be drawn before the next one is allowed to start.
        #:
        #: This is the whole of what "the arm takes time" means now, and it is spent here
        #: rather than in the kernel because it is not the kernel's. A move is applied in
        #: the step it is asked for; how long a person needs to see a block travel is a
        #: property of the browser, so the thread that turns the kernel simply does not
        #: turn it while the block is in the air.
        #:
        #: Measured from the top of the step, so a planner that already took longer than
        #: this to answer -- a model on the end of an API call -- waits no extra time at
        #: all. Nothing is added to a run that was already slower than the animation.
        self._settle = settle
        #: What was alive when the page was last told. Pushed on change rather than on a
        #: timer, so a workspace sitting still sends nothing at all.
        self._activity: dict[str, Any] = {}
        #: Set when the kernel journals a world write, cleared when the page is told.
        #: Not set when the *delivery* is made: a delivery is queued and lands on the
        #: following step, so a page drawn at delivery time is always one move behind --
        #: and the last move is never drawn at all.
        self._table_changed = False
        self.session = Session(
            bundle,
            source,
            on_reply=lambda text: self._push("reply", {"text": text}),
            on_command=lambda who, line: self._push("command", {"text": line, "who": who}),
            on_event=self._on_events,
            journal=journal,
        )
        #: What is deciding the moves. Read off the source rather than passed in, so a
        #: server built directly rather than through the CLI still says something true.
        #:
        #: Worth showing for the same reason the journal panel is: the kernel cannot tell
        #: which of these it is running, and a person watching should be able to.
        self.planner = str(getattr(source, "label", type(source).__name__))
        #: Whether this case has a background goal to ask for. The page hides the button
        #: when it does not, rather than offering something that would compile to nothing.
        self.has_tidy = "tidy" in {str(name) for name in bundle.descriptors}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._turn, daemon=True)

    # -- the kernel's thread ------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def _turn(self) -> None:
        """Turn the kernel, and rest when it has nothing to do.

        The pause matters. A workspace nobody has spoken to has *no jobs at all*, so a
        loop that stepped as fast as it could would spend a whole core establishing that
        there is nothing to run. The sleep is the driver's, not the kernel's: a blocked
        job already costs nothing, and this is only about not asking so often.
        """
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                ran = self.session.step()
                if self._table_changed:
                    self._push("table", self.state())
                    self._table_changed = False
                    # Nothing runs while the block is in the air. Jobs are not waiting for
                    # the arm -- the move is already made and the world already says so --
                    # they are waiting for a person to be able to follow what happened.
                    time.sleep(max(0.0, self._settle - (time.monotonic() - started)))
                if (now := self.activity()) != self._activity:
                    self._activity = now
                    self._push("activity", now)
                # Idle is always worth resting on, however the run is paced: a workspace
                # nobody has spoken to has no jobs at all, and asking a hundred times a
                # second whether that is still true costs a core to learn nothing.
                time.sleep(self._pace if ran else max(self._pace, 0.02))
        except Exception as failure:  # noqa: BLE001 - see below
            # Anything raised here reaches nobody otherwise. This is a daemon thread, so
            # the exception kills it and takes the kernel with it while the HTTP server
            # goes on serving a page that will never update again: a workspace that has
            # stopped for good and looks merely quiet.
            #
            # Broad on purpose. What is being caught is not a class of error, it is the
            # boundary of a thread, and the answer to any of them is the same: say so on
            # the page and on the console, and stop.
            self._stop.set()
            print(f"the workspace stopped: {failure}", file=sys.stderr)
            self._push("stopped", {"text": str(failure)})

    def _on_events(self, events: Sequence[Event]) -> None:
        for event in events:
            if isinstance(event, WorldWritten):
                self._table_changed = True
            if (line := _journal_line(event)) is not None:
                self._push("journal", line)

    # -- what a page is told ------------------------------------------------

    def state(self) -> dict[str, Any]:
        """The table as the kernel has it, plus who moved what since the last push.

        ``caused`` comes from the session rather than from the journal, and that is not a
        shortcut. A device delivery is journalled with no job against it whoever made it,
        so the arm's own move and a hand reaching in are *identical* in the record -- as
        they should be, since the stacker asked for a move and did not perform one. Only
        the thing that made the delivery knows why, and that is the session.
        """
        return {
            "stacks": self.session.positions(),
            "by": self.session.last_cause,
        }

    def activity(self) -> dict[str, Any]:
        """Each job that has not finished, by name.

        Names only. Whether a job is *waiting* would be worth showing in a workspace where
        jobs block, because a sleeping job gives up the machine and a background goal
        running in the gap reads as it taking over. Nothing here waits for anything, so
        the word would be a label that is never true.
        """
        return {
            "live": [
                str(job.descriptor.name)
                for job in self.session.kernel.sched.jobs()
                if job.state not in (JobState.DONE, JobState.FAULTED)
            ]
        }

    def _push(self, kind: str, payload: dict[str, Any]) -> None:
        message = f"data: {json.dumps({'kind': kind, **payload})}\n\n"
        with self._lock:
            watchers = list(self._watchers)
        for watcher in watchers:
            watcher.put(message)

    def watch(self) -> queue.Queue[str]:
        watcher: queue.Queue[str] = queue.Queue()
        with self._lock:
            self._watchers.append(watcher)
        return watcher

    def unwatch(self, watcher: queue.Queue[str]) -> None:
        with self._lock:
            if watcher in self._watchers:
                self._watchers.remove(watcher)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self.session.close()


def _handler(server: BlocksServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: Any) -> None:
            """Quiet. The journal panel is the interesting log and this would bury it."""

        def _send(self, code: int, body: bytes, kind: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            if self.path in ("/", "/index.html"):
                self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/table.css":
                self._send(200, (STATIC / "table.css").read_bytes(), "text/css; charset=utf-8")
            elif self.path == "/table.js":
                self._send(
                    200, (STATIC / "table.js").read_bytes(), "text/javascript; charset=utf-8"
                )
            elif self.path == "/state":
                self._send(
                    200, json.dumps(server.state()).encode(), "application/json; charset=utf-8"
                )
            elif self.path == "/stream":
                self._stream()
            else:
                self._send(404, b"no", "text/plain; charset=utf-8")

        def _stream(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            watcher = server.watch()
            try:
                # Sent once, on connect: the table as it stands, what is alive, and
                # what is deciding. Only the first two ever change.
                for first in (
                    {"kind": "planner", "label": server.planner, "tidy": server.has_tidy},
                    {"kind": "table", **server.state()},
                    {"kind": "activity", **server.activity()},
                ):
                    self.wfile.write(f"data: {json.dumps(first)}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        self.wfile.write(watcher.get(timeout=15).encode())
                    except queue.Empty:
                        # A comment frame, so an idle workspace does not look like a
                        # dropped connection. An idle workspace is the normal state here:
                        # with no job spawned the kernel has nothing to run at all.
                        self.wfile.write(b": still here\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                server.unwatch(watcher)

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/say":
                # Not inspected. Whether this is a move, whether a stacker is already
                # running, whether the table can satisfy it -- all three are the kernel's,
                # and a page that pre-screened would be the application-level branch a
                # descriptor tree exists to remove.
                server.session.say(str(body.get("text", "")))
                self._send(200, b'{"ok":true}', "application/json")
            elif self.path == "/hand/start":
                # A drag has begun. Nothing is interrupted yet, because nothing has
                # happened yet: the world is unchanged until the block is dropped. What
                # this does is stop the clock, so the workspace is not rearranging itself
                # under a hand that is part way across it.
                server.session.hold()
                self._send(200, b'{"said":"a hand reached in"}', "application/json")
            elif self.path == "/hand":
                # The drop. The world changes and the doorbell rings in one call, and the
                # clock starts again so the kernel can act on both.
                said = server.session.disturb(str(body.get("block")), str(body.get("to")))
                server.session.release()
                self._send(200, json.dumps({"said": said}).encode(), "application/json")
            elif self.path == "/hand/cancel":
                # A drag that ended on nothing. The workspace is unchanged, but the clock
                # has to be started again or it never runs at all.
                server.session.release()
                self._send(200, b'{"said":"nothing moved"}', "application/json")
            elif self.path == "/tidy":
                # A control, not a sentence. The front door takes language and compiles
                # it; this takes a descriptor name and has no room to mean anything else.
                # The job is still owned by the operator, so it is clamped to the same
                # ceiling and holds the same capabilities a spoken-for job would.
                server.session.start_job("tidy")
                self._send(200, b'{"ok":true}', "application/json")
            else:
                self._send(404, b"no", "text/plain; charset=utf-8")

    return Handler


def serve(
    case: Path,
    source: CommandSource,
    *,
    host: str = "127.0.0.1",
    port: int = 8800,
    open_browser: bool = False,
    pace: float = 0.09,
    settle: float = 0.8,
    journal: Path | None = None,
) -> int:
    blocks = BlocksServer(case, source, pace=pace, settle=settle, journal=journal)
    blocks.start()
    httpd = ThreadingHTTPServer((host, port), _handler(blocks))
    url = f"http://{host}:{port}/"
    print(f"blocks: {url}  (ctrl-c to stop)")
    if journal is not None:
        # Written when the server stops, which is what `Session.close` is for. Until then
        # the events are in memory; a journal half-written by a process still running is
        # not one the debugger can step through.
        print(f"journal: {journal} on exit; step through it with `zeos debug {case}`")
    if open_browser:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        httpd.server_close()
        blocks.close()
    return 0
