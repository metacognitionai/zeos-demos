"""The atrium, live: the morning runs against the wall clock and a page drives it.

One thread owns the atrium and steps it; the page asks for a snapshot every quarter of a
second and posts what the person at it did -- ordered a coffee, set off the alarm, laid
the table again. Posts go into an inbox the stepping thread empties, so the kernel is
only ever touched from one thread.

The atrium's clock is the wall clock, less the time spent waiting for a model. A seat is
synchronous: while a behaviour's command is being chosen the kernel cannot run, so the
atrium stands still rather than letting the body finish actions nobody is there to
answer. The page says so while it happens.
"""

from __future__ import annotations

import json
import re
import threading
import time
import traceback
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from zeos.core.ids import DescriptorName, JobId, JobState, ObjectName, PipeName
from zeos.driver import load_schedule
from zeos.machine.seat import CommandSource, TapeSource, Turn

from zeos_stretch.recording import RecordedSource, Recorder, Recording, Step, case_name, replay_step
from zeos_stretch.run import CASE, Atrium
from zeos_stretch.story import check
from zeos_stretch.vla import DESKS, S

__all__ = ["Live", "make_server", "serve"]

PAGE = Path(__file__).parent / "web" / "index.html"
DESK = re.compile(r"^desk-[1-9]$")

#: What each behaviour is about, in the robot's own voice.
BECAUSE = {
    "tidy-atrium": "I'm clearing the table.",
    "brew-coffee": "Someone ordered a coffee.",
    "deliver-coffee": "Your coffee is ready.",
    "smoke-response": "The smoke alarm is going off!",
}


def _saying(command: str, *, here: str) -> str:
    verb, _, rest = command.partition(" ")
    words = rest.split()
    match verb, words:
        case "put", [item, "basket"]:
            return f"Putting the {item} in the fruit basket."
        case "put", [item, "tray"]:
            return f"Putting the {item} on the tray."
        case "navigate", [place]:
            return f"Driving to the {place.replace('-', ' ')}."
        case "pick", ["coffee"]:
            return "Picking up the coffee."
        case "place", ["coffee"]:
            return "Handing over the coffee." if here in DESKS else "Putting the coffee down here."
        case "brew", []:
            return "Starting the coffee machine."
        case "press", ["cutoff"]:
            return "Pressing the hotplate's cut-off button."
        case _:
            return f"Doing: {command}."


@dataclass
class _Watched:
    """A command source that tells the page when it is waiting for a model, and how long."""

    inner: CommandSource
    on_wait: Callable[[str | None], None]
    on_command: Callable[[Turn, str, str | None], None] | None = None

    def next_command(self, turn: Turn) -> str:
        self.on_wait(turn.descriptor)
        try:
            command = self.inner.next_command(turn)
        finally:
            self.on_wait(None)
        if self.on_command is not None:
            # A model's source keeps its last reply; a tape has none to keep.
            self.on_command(turn, command, getattr(self.inner, "last_reply", None))
        return command


@dataclass
class Live:
    """A running atrium and what the page may ask of it."""

    source: Callable[[], CommandSource] | None
    case: Path = CASE
    speed: float = 1.0
    #: Replay the case's own schedule. A tape only fits that schedule, so without a
    #: model this is the only thing the page can show.
    playback: bool = False
    #: Where to record the session, so it can be replayed exactly.
    record: Path | None = None
    #: A recorded session to play back instead of taking clicks.
    replay: Recording | None = None
    meta: dict[str, Any] = field(default_factory=dict[str, Any])
    recorder: Recorder | None = None
    pending: list[Step] = field(default_factory=list[Step])
    _stopping: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _inbox: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    _snapshot: dict[str, Any] = field(default_factory=dict[str, Any])
    _waiting: tuple[str, float] | None = None
    _paused_s: float = 0.0
    _crash: str | None = None
    _t0: float = 0.0

    @property
    def interactive(self) -> bool:
        return self.source is not None and self.replay is None

    @property
    def mode(self) -> str:
        if self.replay is not None:
            return "replay"
        return "live" if self.interactive else "playback"

    # -- the stepping thread ------------------------------------------------------

    def _build(self) -> None:
        if self.replay is not None:
            inner: CommandSource = RecordedSource(self.replay)
            self.case = self.replay.case
            self.pending = list(self.replay.steps)
        elif self.source is not None:
            inner = self.source()
        else:
            inner = TapeSource(self._bundle_scripts())
        if self.recorder is not None:
            self.recorder.close()
            self.recorder = None
        if self.record is not None:
            self.recorder = Recorder(
                self._next_recording(),
                {"case": case_name(self.case), **self.meta},
                case=self.case,
            )
        self.atrium = Atrium(
            self.case,
            source=_Watched(inner, self._wait, self.recorder.command if self.recorder else None),
            show_commands=True,
        )
        self.schedule = list(load_schedule(self.case / "events.jsonl")) if self.playback else []
        self._t0 = time.monotonic()
        self._paused_s = 0.0
        self.atrium.start()
        if self.recorder is not None:
            self.recorder.journal(self.atrium.sink)

    def _next_recording(self) -> Path:
        """The recording directory for this build: a restart starts a new one beside it."""
        assert self.record is not None
        if not self.record.exists():
            return self.record
        n = 2
        while self.record.with_name(f"{self.record.name}-{n}").exists():
            n += 1
        return self.record.with_name(f"{self.record.name}-{n}")

    def _bundle_scripts(self) -> Any:
        from zeos.descriptor.loader import load_case

        return load_case(self.case).scripts

    def _wait(self, descriptor: str | None) -> None:
        with self._lock:
            if descriptor is not None:
                self._waiting = (descriptor, time.monotonic())
            elif self._waiting is not None:
                self._paused_s += time.monotonic() - self._waiting[1]
                self._waiting = None

    def _now_ns(self) -> int:
        with self._lock:
            paused = self._paused_s
        return int((time.monotonic() - self._t0 - paused) * self.speed * S)

    def spawn(self) -> threading.Thread:
        """Run the atrium on its own thread until ``close``."""
        self._thread = threading.Thread(target=self.loop, name="atrium", daemon=True)
        self._thread.start()
        return self._thread

    def loop(self) -> None:
        self._build()
        while not self._stopping.is_set():
            try:
                idle_s = self.once()
            except Exception as exc:
                # The atrium stops, and the page says why, rather than showing its last
                # frame as though nothing had happened.
                with self._lock:
                    self._crash = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
                return
            if idle_s:
                time.sleep(idle_s)

    def once(self) -> float:
        """One step at the wall clock's time. Returns how long there is nothing to do."""
        with self._lock:
            restart = ("restart", "") in self._inbox
            if restart:
                self._inbox = []
        if restart:
            self._build()
        if self.replay is not None:
            return self._replay_once()
        now = self._now_ns()
        arrivals: list[tuple[PipeName, str]] = []
        while self.schedule and self.schedule[0].at_ns <= now:
            due = self.schedule.pop(0)
            arrivals.append((due.pipe, due.text))
        scheduled = tuple(arrivals)
        with self._lock:
            asked, self._inbox = self._inbox, []
        for action, arg in asked:
            arrivals.extend(self.atrium.act(now, action, arg))
        if self.recorder is not None:
            self.recorder.step(Step(at_ns=now, arrivals=scheduled, actions=tuple(asked)))
        ran = self.atrium.step(now, arrivals)
        if self.recorder is not None:
            self.recorder.journal(self.atrium.sink)
        self._publish(now)
        if ran:
            return 0.0
        upcoming = [t for t in (self.atrium.body.next_due_ns(),) if t]
        if self.schedule:
            upcoming.append(self.schedule[0].at_ns)
        gap_s = (min(upcoming) - now) / S / self.speed if upcoming else 0.05
        return min(max(gap_s, 0.0), 0.05)

    def start(self) -> None:
        """Build the atrium without starting the thread, for a caller that steps it."""
        self._build()

    def _replay_once(self) -> float:
        """Play the next recorded step once the wall clock has reached it."""
        if not self.pending:
            self._publish(self.atrium.kernel.clock.virtual_ns)
            return 0.05
        step = self.pending[0]
        now = int((time.monotonic() - self._t0) * self.speed * S)
        if step.at_ns > now:
            self._publish(self.atrium.kernel.clock.virtual_ns)
            return min((step.at_ns - now) / S / self.speed, 0.05)
        self.pending.pop(0)
        replay_step(self.atrium, step)
        self._publish(step.at_ns)
        return 0.0

    def close(self) -> None:
        """Stop the atrium, then close the recording: never the other way round, or the
        last step would be written to a closed file."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self.recorder is not None:
            self.recorder.close()

    # -- what the page sees ---------------------------------------------------------

    def _publish(self, now: int) -> None:
        atrium = self.atrium
        world = atrium.kernel.world
        body = atrium.body

        def get(obj: str) -> str:
            return world.get(ObjectName(obj)) or ""

        action = body.current
        doing: dict[str, Any] | None = None
        if action is not None:
            span = max(action.due_ns - action.started_ns, 1)
            target = action.outcome.get("stretch.position", action.origin)
            doing = {
                "command": action.command,
                "behaviour": str(atrium.names[action.job]),
                "progress": min(max((now - action.started_ns) / span, 0.0), 1.0),
                "origin": action.origin,
                "target": target,
                "says": _saying(action.command, here=action.origin),
            }
        latest: dict[str, JobId] = {}
        for job, name in atrium.names.items():
            latest[str(name)] = job
        vectors = {str(v.handler): str(v.source) for v in atrium.bundle.vectors}
        behaviours = []
        for name, descriptor in sorted(
            atrium.bundle.descriptors.items(), key=lambda kv: kv[1].priority
        ):
            job = latest.get(str(name))
            state = atrium.states.get(job) if job is not None else None
            behaviours.append(
                {
                    "name": str(name),
                    "priority": int(descriptor.priority),
                    "state": _state_label(state),
                    "wearing": job is not None and atrium.wearer == job,
                    "trigger": vectors.get(str(name), "boot"),
                    "notice": atrium.notices.get(DescriptorName(name), ""),
                }
            )
        tidy_live = atrium.live("tidy-atrium")
        finished = (
            not self.schedule
            and atrium.settled()
            and not any(
                b["state"] in ("running", "ready", "waiting", "suspended") for b in behaviours
            )
        )
        snapshot = {
            "mode": self.mode,
            "machine": (self.replay.meta if self.replay else self.meta).get("machine", "tape"),
            "recording": str(self.recorder.directory) if self.recorder else None,
            "clock": now / S,
            "robot": {
                "position": get("stretch.position"),
                "holding": get("stretch.holding"),
                "doing": doing,
                "queued": len(body.queue),
            },
            "world": {
                "table": _items(get("table.items")),
                "basket": _items(get("basket.contents")),
                "tray": _items(get("tray.contents")),
                "coffee": get("coffee.location"),
                "machine": get("coffee.machine"),
                "hotplate": get("hotplate.power"),
                "order": get("coffee.order"),
            },
            "behaviours": behaviours,
            "controls": {
                "enabled": self.interactive,
                "table_empty": not _items(get("table.items")),
                "tidy_live": tidy_live,
                "hotplate_off": get("hotplate.power") == "off",
                "alarm_live": atrium.live("smoke-response"),
            },
            "story": [{"claim": v.claim, "held": v.held} for v in check(_Result(atrium))]
            if finished and (self.playback or (self.replay is not None and not self.pending))
            else [],
            "log": [
                {"t": line.at_ns / S, "kind": line.kind, "text": line.text}
                for line in atrium.lines[-120:]
            ],
        }
        with self._lock:
            self._snapshot = snapshot

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap = dict(self._snapshot)
            waiting = self._waiting
        if waiting is not None:
            snap["thinking"] = {"behaviour": waiting[0], "for": time.monotonic() - waiting[1]}
        else:
            snap["thinking"] = None
        snap["because"] = BECAUSE
        with self._lock:
            snap["crash"] = self._crash
        return snap

    def ask(self, action: str, arg: str = "") -> str | None:
        """Queue something the person did. Returns why it was refused, if it was."""
        if action != "restart" and self.replay is not None:
            return "this is a recorded session being played back; it takes no new orders"
        if action != "restart" and not self.interactive:
            return (
                "playback follows the case's own schedule; start with --machine claude to drive it"
            )
        if action == "order" and not DESK.match(arg):
            return f"{arg!r} is not a desk; pick desk-1 to desk-9"
        if action not in ("order", "alarm", "mess", "hotplate", "restart"):
            return f"nothing called {action!r}"
        with self._lock:
            self._inbox.append((action, arg))
        return None


@dataclass(frozen=True, slots=True)
class _Result:
    """The slice of a finished run that ``story.check`` reads."""

    atrium: Atrium

    @property
    def kernel(self) -> Any:
        return self.atrium.kernel

    @property
    def events(self) -> Any:
        return tuple(self.atrium.sink)


def _items(value: str) -> list[str]:
    return [] if value in ("", "nothing") else value.split()


def _state_label(state: JobState | None) -> str:
    match state:
        case None:
            return "idle"
        case JobState.BLOCKED:
            return "waiting"
        case JobState.PINNED_IDLE:
            return "idle"
        case _:
            return str(state.value)


def make_server(live: Live, *, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    """The page's server, bound but not yet serving; the atrium is not started."""
    page = PAGE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/":
                self._send(200, page, "text/html; charset=utf-8")
            elif self.path == "/state":
                body = json.dumps(live.snapshot()).encode()
                self._send(200, body, "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                data = {}
            refused = live.ask(self.path.strip("/"), str(data.get("desk", "")))
            if refused is None:
                self._send(202, b'{"ok": true}', "application/json")
            else:
                self._send(
                    409, json.dumps({"ok": False, "why": refused}).encode(), "application/json"
                )

    try:
        return ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        raise SystemExit(
            f"cannot serve on {host}:{port}: {exc.strerror}. "
            "Another zeos-stretch serve may be running; stop it or pass --port."
        ) from exc


def serve(
    live: Live, *, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True
) -> None:
    # Bind first: an atrium started for a page nobody can reach would spend API calls
    # choosing commands for nobody.
    server = make_server(live, host=host, port=port)
    live.spawn()
    url = f"http://{host}:{port}/"
    print(f"the atrium is at {url}  (ctrl-c to stop)")
    if open_browser:
        webbrowser.open(url)
    if live.record is not None:
        print(f"recording the session to {live.record}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        live.close()
