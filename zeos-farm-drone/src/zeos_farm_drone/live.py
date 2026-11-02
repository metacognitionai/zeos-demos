"""The farm, live: the morning runs against the wall clock and a page drives it.

One thread owns the farm and steps it; the page asks for a snapshot every quarter of a
second and posts what the person at it did -- a new day, a vehicle at the gate, calves,
a fire, words for the farm. Posts go into an inbox the stepping thread empties, so the
kernel is only ever touched from one thread.

The farm's clock is the wall clock, less the time spent waiting for a command source. A
seat is synchronous: while a behaviour's command is being chosen the kernel cannot run,
so the farm stands still rather than letting the drones finish actions nobody is there
to answer. The page says so while it happens.
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import traceback
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any

from zeos.core.ids import DescriptorName, JobId, JobState, ObjectName, PipeName
from zeos.descriptor.loader import load_case
from zeos.machine.seat import CommandSource, TapeSource, Turn

from zeos_farm_drone.farm import OFF_FARM, PADDOCKS, S
from zeos_farm_drone.recording import (
    Action,
    RecordedSource,
    Recorder,
    Recording,
    Step,
    case_name,
    replay_step,
)
from zeos_farm_drone.run import CASE, Arrival, Farm, load_events
from zeos_farm_drone.sim import CRITICAL_PCT, DRONES

if TYPE_CHECKING:
    from zeos_farm_drone.camera import Camera
    from zeos_farm_drone.px4 import Link
    from zeos_farm_drone.scene import Scene
from zeos_farm_drone.story import check, expected_herd

__all__ = ["Live", "make_server", "serve"]

PAGE = Path(__file__).parent / "web" / "index.html"
#: The page's icons, served as they are.
ASSETS = PAGE.parent / "assets"
ASSET = re.compile(r"/assets/([a-z0-9-]+\.png)")
#: A photo, or the camera's latest frame, as the page asks for it.
PHOTO = re.compile(r"/photos/([A-Za-z0-9-]+)\.png")
DIRECTIONS = (
    "north",
    "north-east",
    "east",
    "south-east",
    "south",
    "south-west",
    "west",
    "north-west",
)
VEHICLES = ("white-ute", "red-truck", "grey-van")
ITEMS = ("motorbike", "quad-bike", "toolbox")
PLATES = ("XYZ123", "ABC789", "KLM456", "QRS246", "TUV802")

#: What each behaviour is about, in the drone's own voice.
BECAUSE = {
    "patrol": "The morning patrol: every paddock, once.",
    "vehicle-check": "A vehicle is in a paddock where none should be.",
    "livestock-check": "A paddock's count is not what the farmer expects.",
    "fire-watch": "A fire has been reported nearby!",
    "find-item": "The farmer asked me to find something.",
    "battery-return": "My battery is critically low: home to the pad first.",
}


def _saying(command: str, *, here: str) -> str:
    verb, _, rest = command.partition(" ")
    words = rest.split()
    place = here.replace("-", " ")
    match verb, words:
        case "fly", [target]:
            return f"Flying to the {target.replace('-', ' ')}."
        case "return", []:
            return "Heading back to the shed."
        case "observe", []:
            return f"Looking over the {place}: cattle, vehicles, the trough."
        case "zoom", ["vehicle"]:
            return "Zooming in on the vehicle and its plate."
        case "zoom", ["cattle"]:
            return f"Counting the cattle in the {place} up close."
        case "zoom", [target]:
            return f"Zooming in on the {target}."
        case "search", [item]:
            return f"Searching the {place} for the {item.replace('-', ' ')}."
        case "climb", [metres]:
            return f"Changing altitude to {metres} m."
        case "panorama", []:
            return "Taking a 360-degree panorama."
        case _:
            return f"Doing: {command}."


@dataclass
class _Watched:
    """A command source that tells the page when it is waiting for one, and how long."""

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
            # A model's source keeps its last reply; the rules and a tape have none.
            self.on_command(turn, command, getattr(self.inner, "last_reply", None))
        return command


@dataclass
class Live:
    """A running farm and what the page may ask of it."""

    source: Callable[[], CommandSource] | None
    case: Path = CASE
    speed: float = 1.0
    #: Replay the case's own schedule on the descriptors' tapes. A tape only fits that
    #: schedule, so this takes no clicks.
    playback: bool = False
    #: Where to record the session, so it can be replayed exactly.
    record: Path | None = None
    #: A recorded session to play back instead of taking clicks.
    replay: Recording | None = None
    meta: dict[str, Any] = field(default_factory=dict[str, Any])
    #: The flight controller flying the drone, or None for the map's own drone.
    link: Link | None = None
    #: Gazebo, shown what happens on the farm; only with a link.
    scene: Scene | None = None
    #: The drone's camera in Gazebo, whose frames the page shows; only with a scene.
    camera: Camera | None = None
    recorder: Recorder | None = None
    pending: list[Step] = field(default_factory=list[Step])
    _stopping: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _inbox: list[Action] = field(default_factory=list[Action])
    _restart: bool = False
    _snapshot: dict[str, Any] = field(default_factory=dict[str, Any])
    _waiting: tuple[str, float] | None = None
    _paused_s: float = 0.0
    _crash: str | None = None
    #: What the farm is waiting for before a morning can start, for the page.
    _waiting_for: str | None = None
    _t0: float = 0.0
    _plates: int = 0
    schedule: list[Arrival] = field(default_factory=list[Arrival])

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
            inner = TapeSource(load_case(self.case).scripts)
        if self.recorder is not None:
            self.recorder.close()
            self.recorder = None
        recording = self._next_recording() if self.record is not None else None
        if recording is not None:
            self.recorder = Recorder(
                recording, {"case": case_name(self.case), **self.meta}, case=self.case
            )
        if self.link is not None:
            # A morning starts with the drone on its pad, whatever the last one left.
            with self._lock:
                self._waiting_for = "kestrel is flying home to the shed before the morning starts"
            self.link.park()
            with self._lock:
                self._waiting_for = None
        if self.camera is not None:
            # A recorded morning keeps its photos beside its journal.
            self.camera.file_into(
                recording / "photos"
                if recording is not None
                else Path(tempfile.mkdtemp(prefix="zeos-farm-photos-"))
            )
        self.farm = Farm(
            self.case,
            source=_Watched(inner, self._wait, self.recorder.command if self.recorder else None),
            show_commands=True,
            link=self.link,
            scene=self.scene,
            camera=self.camera,
        )
        self.schedule = load_events(self.case / "events.jsonl") if self.playback else []
        self._t0 = time.monotonic()
        self._paused_s = 0.0
        self.farm.start()
        if self.recorder is not None:
            self.recorder.journal(self.farm.sink)

    def photos(self) -> Path | None:
        """Where this morning's photos are, if it has any."""
        if self.replay is not None:
            return self.replay.directory / "photos"
        return self.camera.out_dir if self.camera is not None else None

    def _next_recording(self) -> Path:
        """The recording directory for this build: a restart starts a new one beside it."""
        assert self.record is not None
        if not self.record.exists():
            return self.record
        n = 2
        while self.record.with_name(f"{self.record.name}-{n}").exists():
            n += 1
        return self.record.with_name(f"{self.record.name}-{n}")

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
        """Run the farm on its own thread until ``close``."""
        self._thread = threading.Thread(target=self.loop, name="farm", daemon=True)
        self._thread.start()
        return self._thread

    def loop(self) -> None:
        self._build()
        while not self._stopping.is_set():
            try:
                idle_s = self.once()
            except Exception as exc:
                # The farm stops, and the page says why, rather than showing its last
                # frame as though nothing had happened.
                with self._lock:
                    self._crash = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
                return
            if idle_s:
                time.sleep(idle_s)

    def start(self) -> None:
        """Build the farm without starting the thread, for a caller that steps it."""
        self._build()

    def once(self) -> float:
        """One step at the wall clock's time. Returns how long there is nothing to do."""
        with self._lock:
            restart, self._restart = self._restart, False
            if restart:
                self._inbox = []
        if restart:
            self._build()
        if self.replay is not None:
            return self._replay_once()
        now = self._now_ns()
        farm = self.farm
        arrivals: list[tuple[PipeName, str]] = []
        while self.schedule and self.schedule[0].at_ns <= now:
            due = self.schedule.pop(0)
            if due.act:
                arrivals.extend(farm.act(now, due.act, due.args))
            else:
                arrivals.append((PipeName(due.pipe), due.text))
        scheduled = tuple(arrivals)
        with self._lock:
            asked, self._inbox = self._inbox, []
        for action, args in asked:
            arrivals.extend(farm.act(now, action, args))
        if self.recorder is not None:
            self.recorder.step(Step(at_ns=now, arrivals=scheduled, actions=tuple(asked)))
        ran = farm.step(now, arrivals)
        if self.recorder is not None:
            self.recorder.journal(farm.sink)
        self._publish(now)
        if ran:
            return 0.0
        upcoming = [t for t in (farm.sim.next_due_ns(),) if t]
        if self.schedule:
            upcoming.append(self.schedule[0].at_ns)
        gap_s = (min(upcoming) - now) / S / self.speed if upcoming else 0.05
        return min(max(gap_s, 0.0), 0.05)

    def _replay_once(self) -> float:
        """Play the next recorded step once the wall clock has reached it."""
        if not self.pending:
            self._publish(self.farm.kernel.clock.virtual_ns)
            return 0.05
        step = self.pending[0]
        now = int((time.monotonic() - self._t0) * self.speed * S)
        if step.at_ns > now:
            self._publish(self.farm.kernel.clock.virtual_ns)
            return min((step.at_ns - now) / S / self.speed, 0.05)
        self.pending.pop(0)
        replay_step(self.farm, step)
        self._publish(step.at_ns)
        return 0.0

    def close(self) -> None:
        """Stop the farm, then close the recording: never the other way round, or the
        last step would be written to a closed file."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
        if self.recorder is not None:
            self.recorder.close()
        if self.camera is not None:
            self.camera.close()
        if self.scene is not None:
            self.scene.close()
        if self.link is not None:
            self.link.close()

    # -- what the page sees ---------------------------------------------------------

    def _publish(self, now: int) -> None:
        farm = self.farm
        sim = farm.sim
        names = farm.names

        def get(obj: str) -> str:
            return farm.kernel.world.get(ObjectName(obj)) or ""

        drones = []
        for name, drone in sorted(sim.drones.items()):
            action = drone.current
            doing: dict[str, Any] | None = None
            if action is not None:
                doing = {
                    "command": action.command,
                    "behaviour": str(names[action.job]),
                    "progress": sim.progress(action, now),
                    "origin": action.origin,
                    "target": action.target,
                    "says": _saying(action.command, here=action.origin),
                }
            pilot = farm.pilots.get(name)
            drones.append(
                {
                    "name": name,
                    "sensors": sorted(drone.sensors),
                    "position": drone.position,
                    "altitude": sim.altitude_now(drone),
                    "battery": round(drone.battery, 1),
                    "critical": drone.battery <= CRITICAL_PCT,
                    "pilot": str(names[pilot]) if pilot is not None else None,
                    "doing": doing,
                    "queued": len(drone.queue),
                }
            )

        latest: dict[str, JobId] = {}
        for job, name in names.items():
            latest[str(name)] = job
        triggers = {str(v.handler): str(v.source) for v in farm.bundle.vectors}
        flying = {str(names[j]): d for d, j in farm.pilots.items() if j is not None}
        behaviours = []
        for name, descriptor in sorted(
            farm.bundle.descriptors.items(), key=lambda kv: (kv[1].priority, str(kv[0]))
        ):
            if str(name) == "flight-rules" or not descriptor.requires:
                continue
            job = latest.get(str(name))
            state = farm.states.get(job) if job is not None else None
            behaviours.append(
                {
                    "name": str(name),
                    "priority": int(descriptor.priority),
                    "state": _state_label(state),
                    "drone": flying.get(str(name)),
                    "trigger": triggers.get(str(name), "the farmer"),
                    "notice": farm.notices.get(DescriptorName(name), ""),
                }
            )

        photos = {p.id: p for p in sim.photos}
        phone = []
        for at, text in farm.alerts:
            fields = dict(p.split("=", 1) for p in text.split() if "=" in p)
            photo = photos.get(fields.get("photo", ""))
            phone.append(
                {
                    "t": at / S,
                    "kind": fields.get("kind", ""),
                    "where": fields.get("where", ""),
                    "detail": fields.get("detail", "").replace("-", " "),
                    "photo": photo.id if photo else None,
                    "subject": photo.subject if photo else "",
                    "drone": photo.drone if photo else "",
                    "image": _image(self.photos(), photo.id) if photo else None,
                }
            )

        expected = expected_herd(get("herd.expected"))
        paddocks = {
            p: {
                "head": sim.cattle[p] + sim.strays.get(p, 0),
                "calves": sim.calves.get(p, 0),
                "strays": sim.strays.get(p, 0),
                "expected": expected.get(p, 0),
                "trough": sim.troughs[p],
            }
            for p in PADDOCKS
        }
        active = any(b["state"] in ("running", "ready", "waiting", "suspended") for b in behaviours)
        # Judged once something has happened and everything has come to rest: before the
        # first dawn there is no morning to judge.
        finished = (
            bool(farm.names)
            and not self.schedule
            and farm.settled()
            and not active
            and not self.pending
        )
        snapshot = {
            "mode": self.mode,
            "machine": (self.replay.meta if self.replay else self.meta).get("machine", "tape"),
            "recording": str(self.recorder.directory) if self.recorder else None,
            "clock": now / S,
            "drones": drones,
            "farm": {
                "paddocks": paddocks,
                "vehicles": [
                    {"name": v.name, "plate": v.plate, "place": v.place, "heading": v.heading}
                    for v in sim.vehicles
                ],
                "items": dict(sim.items),
                "fire": {"direction": sim.fire[0], "km": sim.fire[1]} if sim.fire else None,
            },
            "phone": phone,
            "replies": [{"t": at / S, "text": text} for at, text in farm.replies],
            "rulings": [
                {
                    "t": r.at_ns / S,
                    "behaviour": r.behaviour,
                    "command": r.command,
                    "allowed": r.allowed,
                    "reason": r.reason,
                }
                for r in farm.rulings[-40:]
            ],
            "behaviours": behaviours,
            "controls": {
                "enabled": self.interactive,
                "vehicle_on_farm": any(v.place not in OFF_FARM for v in sim.vehicles),
                "vehicles_on_farm": [v.name for v in sim.vehicles if v.place not in OFF_FARM],
                "leaving": bool(sim.leaving),
                "patrolling": farm.live("patrol"),
                "battery": self._battery_refusal(),
            },
            "story": [{"claim": v.claim, "held": v.held} for v in check(farm.result())]
            if finished
            else [],
            "log": [
                {"t": line.at_ns / S, "kind": line.kind, "text": line.text}
                for line in farm.lines[-150:]
            ],
        }
        with self._lock:
            self._snapshot = snapshot

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            snap = dict(self._snapshot)
            waiting = self._waiting
            crash = self._crash
            waiting_for = self._waiting_for
        snap["waiting_for"] = waiting_for
        snap["thinking"] = (
            {"behaviour": waiting[0], "for": time.monotonic() - waiting[1]}
            if waiting is not None and waiting[0] != "flight-rules"
            else None
        )
        snap["because"] = BECAUSE
        snap["camera"] = _image(self.photos(), "live") if self.camera is not None else None
        snap["crash"] = crash
        snap["choices"] = {
            "paddocks": list(PADDOCKS),
            "directions": list(DIRECTIONS),
            "vehicles": list(VEHICLES),
            "items": list(ITEMS),
        }
        return snap

    def _possible(self, action: str, args: Mapping[str, str]) -> str | None:
        """Why the farm as it is now cannot do this, if it cannot. Asked when the click
        arrives; a click queued behind a model's turn is judged by the farm it finds."""
        with self._lock:
            queued = list(self._inbox)
        match action:
            case "dawn" if self.farm.live("patrol") or any(a == "dawn" for a, _ in queued):
                return "the patrol is already out; a new day starts when it is back"
            case "vehicle-enters" if args.get("name") in _on_farm(self) or any(
                a == "vehicle-enters" and q.get("name") == args.get("name") for a, q in queued
            ):
                return f"the {args.get('name', '').replace('-', ' ')} is already on the farm"
            case "vehicle-leaves" if not _on_farm(self) or self.farm.sim.leaving:
                return "no vehicle on the farm is parked to drive off"
            case "battery" if any(a == "battery" for a, _ in queued):
                return "the battery alarm is already raised"
            case "battery":
                return self._battery_refusal()
            case _:
                return None

    def _battery_refusal(self) -> str | None:
        """Why a cell cannot fail now, if it cannot: the drone is on its pad, or already
        on its way home on a flat battery."""
        drone = self.farm.sim.drones[DRONES[0]]
        if drone.alarmed or self.farm.live("battery-return"):
            return f"{drone.name} is already flying home on a flat battery"
        if not self.farm.sim.airborne(drone):
            return f"{drone.name} is on its pad in the shed, being charged"
        return None

    def ask(self, action: str, args: Mapping[str, str]) -> str | None:
        """Queue something the person did. Returns why it was refused, if it was."""
        if action == "restart":
            with self._lock:
                self._restart = True
            return None
        if self.replay is not None:
            return "this is a recorded session being played back; it takes no new events"
        if not self.interactive:
            return (
                "playback follows the case's own schedule; serve with --machine rules to drive it"
            )
        problem = _check(action, args) or self._possible(action, args)
        if problem is not None:
            return problem
        args = dict(args)
        if action == "vehicle-enters":
            # A plate the person did not choose, but one every replay will see again.
            args.setdefault("plate", PLATES[self._plates % len(PLATES)])
            self._plates += 1
        with self._lock:
            self._inbox.append((action, args))
        return None


def _on_farm(live: Live) -> list[str]:
    """The vehicles still on the farm, including any on their way off it."""
    return [v.name for v in live.farm.sim.vehicles if v.place not in OFF_FARM]


def _check(action: str, args: Mapping[str, str]) -> str | None:
    """Why a control's arguments cannot be done on this farm, if they cannot."""

    def one_of(key: str, allowed: tuple[str, ...]) -> str | None:
        value = args.get(key, "")
        return None if value in allowed else f"{key} must be one of {', '.join(allowed)}"

    def number(key: str, low: int, high: int) -> str | None:
        value = args.get(key, "")
        if value.isdigit() and low <= int(value) <= high:
            return None
        return f"{key} must be a whole number from {low} to {high}"

    match action:
        case "dawn" | "battery":
            return None
        case "fire":
            return one_of("direction", DIRECTIONS) or number("km", 1, 50)
        case "vehicle-enters":
            return one_of("paddock", PADDOCKS) or one_of("name", VEHICLES)
        case "vehicle-leaves":
            return one_of("towards", tuple(sorted(OFF_FARM)))
        case "calves":
            return one_of("paddock", PADDOCKS) or number("count", 1, 5)
        case "stray":
            if args.get("from") == args.get("to"):
                return "a cow strays from one paddock into another"
            return one_of("from", PADDOCKS) or one_of("to", PADDOCKS)
        case "trough":
            return one_of("paddock", PADDOCKS) or number("level", 0, 100)
        case "park":
            return one_of("item", ITEMS) or one_of("paddock", PADDOCKS)
        case "say":
            text = args.get("text", "").strip()
            return None if 0 < len(text) <= 120 else "say something, in at most 120 characters"
        case _:
            return f"nothing called {action!r} can happen on the farm"


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


def _image(photos: Path | None, name: str) -> str | None:
    """The page's address of photo ``name``, once its frame is on disk."""
    if photos is None or not (photos / f"{name}.png").exists():
        return None
    return f"/photos/{name}.png"


def make_server(live: Live, *, host: str = "127.0.0.1", port: int = 8766) -> ThreadingHTTPServer:
    """The page's server, bound but not yet serving; the farm is not started."""
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
                self._send(200, json.dumps(live.snapshot()).encode(), "application/json")
            elif (icon := ASSET.fullmatch(self.path.split("?")[0])) and (
                ASSETS / icon[1]
            ).is_file():
                self._send(200, (ASSETS / icon[1]).read_bytes(), "image/png")
            elif (found := PHOTO.fullmatch(self.path.split("?")[0])) and (photos := live.photos()):
                frame = photos / f"{found[1]}.png"
                if frame.exists():
                    self._send(200, frame.read_bytes(), "image/png")
                else:
                    self._send(404, b"not taken yet", "text/plain")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                data = {}
            args = {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
            refused = live.ask(self.path.strip("/"), args)
            if refused is None:
                self._send(202, b'{"ok": true}', "application/json")
            else:
                body = json.dumps({"ok": False, "why": refused}).encode()
                self._send(409, body, "application/json")

    try:
        return ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        raise SystemExit(
            f"cannot serve on {host}:{port}: {exc.strerror}. "
            "Another zeos-farm-drone serve may be running; stop it or pass --port."
        ) from exc


def serve(
    live: Live, *, host: str = "127.0.0.1", port: int = 8766, open_browser: bool = True
) -> None:
    # Bind first: a farm started for a page nobody can reach would choose commands for
    # nobody.
    server = make_server(live, host=host, port=port)
    live.spawn()
    url = f"http://{host}:{port}/"
    print(f"the farm is at {url}  (ctrl-c to stop)")
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
