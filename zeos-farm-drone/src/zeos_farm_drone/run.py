"""Run the farm: the kernel, the simulated drones, and the morning's events.

``zeos.driver.Driver`` delivers a fixed schedule. The drones' answers are not fixed --
they arrive when an action finishes -- so this loop is the driver's loop with the drones
added: after every tick it hands the new journal events to the simulator, and when the
kernel has nothing runnable it jumps virtual time to whatever is due next, from the
schedule or from a drone.

A schedule line is either a delivery, as ``zeos`` reads them, or something that happens
on the farm, which is what the page's controls do too:

    {"at_ns": 0, "pipe": "clock.dawn", "text": "dawn"}
    {"at_ns": 5000000000, "act": "vehicle-enters", "args": {"paddock": "creek"}}
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from zeos.core.events import (
    CompilationRefused,
    Disembodied,
    Embodied,
    Event,
    FaultRaised,
    GateAnswered,
    GateConsulted,
    Injected,
    JobCompleted,
    JobSpawned,
    JobStateChanged,
    UtteranceCompiled,
    VectorFired,
)
from zeos.core.ids import DescriptorName, JobId, JobState, PipeName
from zeos.core.kernel import Kernel, KernelConfig
from zeos.descriptor.loader import load_case
from zeos.descriptor.schema import Descriptor
from zeos.driver import build_kernel
from zeos.journal.writer import Journal
from zeos.machine.base import MachineRequest
from zeos.machine.seat import CommandSeat, CommandSource, TapeSource

from zeos_farm_drone.farm import S
from zeos_farm_drone.rules import GUARD, Routed
from zeos_farm_drone.sim import BATTERY_PIPE, COMMAND_PIPE, FarmSim

if TYPE_CHECKING:
    from zeos_farm_drone.camera import Camera
    from zeos_farm_drone.px4 import Link
    from zeos_farm_drone.scene import Scene

__all__ = [
    "CASE",
    "Arrival",
    "Farm",
    "Line",
    "RunResult",
    "Ruling",
    "aliases_of",
    "load_events",
    "run",
]

CASE = Path(__file__).resolve().parents[2] / "cases" / "farm"

#: Virtual cost of one token boundary: a forward pass, the only thing the kernel's own
#: time is spent on. Everything slow on the farm is the drones'.
NS_PER_TICK = 10_000_000

ALERTS = PipeName("farmer.alerts")
REPLIES = PipeName("farmer.replies")


@dataclass(frozen=True, slots=True)
class Arrival:
    """One line of a schedule: a delivery to a pipe, or something done on the farm."""

    at_ns: int
    pipe: str = ""
    text: str = ""
    act: str = ""
    args: Mapping[str, str] = field(default_factory=dict[str, str])


def load_events(path: Path) -> list[Arrival]:
    arrivals: list[Arrival] = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        spec = json.loads(line)
        arrivals.append(
            Arrival(
                at_ns=int(spec["at_ns"]),
                pipe=str(spec.get("pipe", "")),
                text=str(spec.get("text", "")),
                act=str(spec.get("act", "")),
                args={str(k): str(v) for k, v in spec.get("args", {}).items()},
            )
        )
    return sorted(arrivals, key=lambda a: a.at_ns)


@dataclass(frozen=True, slots=True)
class RunResult:
    kernel: Kernel
    events: tuple[Event, ...]
    sim: FarmSim
    alerts: tuple[str, ...]
    replies: tuple[str, ...]
    lines: tuple[str, ...]


def aliases_of(descriptors: Mapping[DescriptorName, Descriptor]) -> dict[str, list[str]]:
    """The pipe names each behaviour may say, which is what a model is told it can name."""
    return {
        str(name): [a for a in ("stdin", "stdout", "tools") if d.pipes.resolve(a)]
        + sorted(d.pipes.extra)
        for name, d in sorted(descriptors.items())
    }


@dataclass(frozen=True, slots=True)
class Ruling:
    """One answer of the flight rules to a command a behaviour gave."""

    at_ns: int
    behaviour: str
    command: str
    allowed: bool
    reason: str


@dataclass(frozen=True, slots=True)
class Line:
    """One line of the narration: what the kernel, a drone or the farm did."""

    at_ns: int
    kind: str  # kernel | drone | interrupt | farm | command | fault | resume | gate | farmer
    text: str

    def render(self) -> str:
        return f"{self.at_ns / S:6.1f}s  {self.text}"


class Farm:
    """One morning on the farm: the kernel, the drones, and the narration of both.

    Advanced one step at a time by whoever owns the clock -- ``run`` jumps virtual time
    from event to event, a live server follows the wall clock -- so both see the same
    sequence of kernel ticks for the same sequence of arrivals.
    """

    def __init__(
        self,
        case: Path = CASE,
        *,
        source: CommandSource | None = None,
        show_commands: bool = False,
        on_line: Callable[[Line], None] | None = None,
        link: Link | None = None,
        scene: Scene | None = None,
        camera: Camera | None = None,
    ) -> None:
        self.bundle = load_case(case)
        self.sink: list[Event] = []
        self.names: dict[JobId, DescriptorName] = {}
        self.states: dict[JobId, JobState] = {}
        #: Which job is flying each drone.
        self.pilots: dict[str, JobId | None] = {}
        self.notices: dict[DescriptorName, str] = {}
        self.alerts: list[tuple[int, str]] = []
        self.replies: list[tuple[int, str]] = []
        self.lines: list[Line] = []
        #: Every answer of the flight rules, in order.
        self.rulings: list[Ruling] = []
        self._held: dict[JobId, str] = {}
        self._show_commands = show_commands
        self._on_line = on_line
        self._seen = 0
        self._shown = 0
        seat = CommandSeat(
            source=Routed(source if source is not None else TapeSource(self.bundle.scripts)),
            on_command=self._heard,
        )
        self.kernel, _ = build_kernel(
            self.bundle,
            machine=seat,
            journal_sink=self.sink,
            config=KernelConfig(case=self.bundle.name),
        )
        sensors = {p.name: frozenset(p.sensors) for p in self.bundle.platforms}
        if link is None:
            self.sim: FarmSim = FarmSim(self.kernel, sensors=sensors, truth=case / "farm.yaml")
        else:
            from zeos_farm_drone.px4 import Px4Farm

            self.sim = Px4Farm(
                self.kernel,
                sensors=sensors,
                truth=case / "farm.yaml",
                link=link,
                scene=scene,
                camera=camera,
            )

    # -- driving ----------------------------------------------------------------

    def start(self) -> None:
        self.kernel.start()
        for name in self.bundle.boot:
            self.kernel.spawn(name)
        self._settle(0)

    def step(self, now_ns: int, arrivals: Sequence[tuple[PipeName, str]] = ()) -> bool:
        """Deliver what arrived by ``now_ns``, let the drones catch up, run one tick.
        Returns whether the kernel had anything to run."""
        self.kernel.advance_time(now_ns)
        for pipe, text in arrivals:
            self.say(now_ns, "farm", f"farm       {pipe} ◀── {text}")
            self.kernel.deliver(pipe, text)
        self.sim.advance(now_ns)
        self._settle(now_ns)
        ran = self.kernel.tick()
        self._settle(now_ns)
        self.sim.advance(now_ns)
        self._settle(now_ns)
        return ran

    def settled(self) -> bool:
        """Nothing for a drone to do and nothing one owes an answer to."""
        return not self.sim.busy()

    def result(self) -> RunResult:
        return RunResult(
            kernel=self.kernel,
            events=tuple(self.sink),
            sim=self.sim,
            alerts=tuple(text for _, text in self.alerts),
            replies=tuple(text for _, text in self.replies),
            lines=tuple(line.render() for line in self.lines),
        )

    def live(self, name: str) -> bool:
        """Whether a job of this behaviour is running or waiting to."""
        return any(
            str(self.names[job]) == name and not state.is_terminal
            for job, state in self.states.items()
        )

    def act(self, now_ns: int, action: str, args: Mapping[str, str]) -> list[tuple[PipeName, str]]:
        """Something that happened on the farm. Returns the pipe writes it amounts to, for
        the next step to deliver; the rest changes the farm directly."""
        self.kernel.advance_time(now_ns)
        sim = self.sim
        match action:
            case "dawn":
                return [(PipeName("clock.dawn"), "dawn")]
            case "fire":
                km = int(args.get("km", "12"))
                direction = args.get("direction", "north-west")
                sim.start_fire(now_ns, direction, km)
                return [(PipeName("alerts.fire"), f"smoke {km}km {direction}")]
            case "battery":
                alarm = sim.battery_fails(now_ns)
                self._settle(now_ns)
                return [(BATTERY_PIPE, alarm)] if alarm is not None else []
            case "say":
                self.say(now_ns, "farmer", f"farmer     “{args['text']}”")
                return [(PipeName("farmer.says"), args["text"])]
            case "vehicle-enters":
                sim.vehicle_enters(
                    now_ns,
                    args["paddock"],
                    args.get("name", "white-ute"),
                    args.get("plate", "XYZ123"),
                )
            case "vehicle-leaves":
                sim.vehicle_leaves(now_ns, args.get("towards", "road"))
            case "calves":
                sim.calves_born(now_ns, args["paddock"], int(args.get("count", "1")))
            case "stray":
                sim.stray(now_ns, args["from"], args["to"])
            case "trough":
                sim.trough_fault(now_ns, args["paddock"], int(args.get("level", "10")))
            case "park":
                sim.park(now_ns, args.get("item", "motorbike"), args["paddock"])
            case _:
                raise ValueError(f"nothing called {action!r} can happen on the farm")
        self._settle(now_ns)
        return []

    # -- narration ----------------------------------------------------------------

    def say(self, now_ns: int, kind: str, text: str) -> None:
        line = Line(at_ns=now_ns, kind=kind, text=text)
        self.lines.append(line)
        if self._on_line is not None:
            self._on_line(line)

    def _heard(self, job: JobId, line: str, request: MachineRequest) -> None:
        if self._show_commands and self.names.get(job) != GUARD:
            name = self.names.get(job, job)
            self.say(self.kernel.clock.virtual_ns, "command", f"{name!s:<15} » {line}")

    def _settle(self, now_ns: int) -> None:
        while self._seen < len(self.sink):
            event = self.sink[self._seen]
            self._seen += 1
            self.sim.observe(event, now_ns)
            self._narrate(event, now_ns)
        for report in self.sim.reports[self._shown :]:
            self.say(report.at_ns, "drone", report.text)
        self._shown = len(self.sim.reports)
        pipes = self.kernel.pipes
        if pipes.get(COMMAND_PIPE).available:
            self.kernel.drain(COMMAND_PIPE)
        if pipes.get(ALERTS).available:
            # A drain takes every write since the last one; each alert begins `kind=`.
            words = [str(t) for t in self.kernel.drain(ALERTS)]
            starts = [i for i, w in enumerate(words) if w.startswith("kind=")] or [0]
            for start, end in zip(starts, [*starts[1:], len(words)], strict=True):
                self.alerts.append((now_ns, " ".join(words[start:end])))
        if pipes.get(REPLIES).available:
            self.replies.append((now_ns, " ".join(str(t) for t in self.kernel.drain(REPLIES))))
        for job_id in self.kernel.unreaped():
            self.kernel.reap(job_id)

    def _narrate(self, event: Event, now_ns: int) -> None:
        names = self.names
        match event:
            case JobSpawned(job=job, descriptor=descriptor):
                names[job] = descriptor
                self.states[job] = JobState.READY
            case JobStateChanged(job=job, to_state=state):
                self.states[job] = state
            case VectorFired(pipe=pipe, handler=handler, priority=priority):
                self.say(
                    now_ns, "interrupt", f"interrupt  {pipe} -> {handler} (priority {priority})"
                )
            case Embodied(job=job, platform=platform):
                self.pilots[platform] = job
                self.say(now_ns, "kernel", f"kernel     {names[job]} is flying {platform}")
            case Disembodied(job=job, platform=platform, reason=reason):
                if self.pilots.get(platform) == job:
                    self.pilots[platform] = None
                self.say(now_ns, "kernel", f"kernel     {names[job]} lost {platform}: {reason}")
            case GateConsulted(job=job, payload=payload) if job is not None:
                self._held[job] = payload
            case GateAnswered(job=job, allowed=allowed, reason=reason) if job is not None:
                command = self._held.pop(job, "")
                self.rulings.append(Ruling(now_ns, str(names[job]), command, allowed, reason))
                if not allowed:
                    self.say(now_ns, "gate", f"rules      vetoed {names[job]}: {reason}")
            case UtteranceCompiled(artifact=artifact):
                self.say(now_ns, "farmer", f"kernel     the farmer's words compiled: {artifact}")
            case CompilationRefused(reason=reason):
                self.say(now_ns, "farmer", f"kernel     the farmer's words were refused: {reason}")
            case Injected(job=job, text=text) if text and text[0] == "<RESUME>":
                self.notices[names[job]] = " ".join(text)
                self.say(now_ns, "resume", f"kernel     {names[job]} ◀── {' '.join(text)}")
            case JobCompleted(job=job) if names[job] != GUARD:
                self.say(now_ns, "kernel", f"kernel     {names[job]} finished")
            case FaultRaised(job=job, detail=detail):
                self.say(now_ns, "fault", f"FAULT      {names.get(job, job)}: {detail}")
            case _:
                return


def _running(farm: Farm) -> bool:
    """Whether any behaviour is still running or waiting to."""
    return any(not state.is_terminal for state in farm.states.values())


def run(
    case: Path = CASE,
    *,
    events: Path | None = None,
    journal: Path | None = None,
    source: CommandSource | None = None,
    echo: bool = False,
    show_commands: bool = False,
    link: Link | None = None,
    scene: Scene | None = None,
    camera: Camera | None = None,
) -> RunResult:
    """Run the morning to its end in virtual time. ``source`` answers each behaviour's
    turn: the descriptors' own tapes by default. The flight rules always answer the
    guard, whatever ``source`` is."""
    farm = Farm(
        case,
        source=source,
        show_commands=show_commands,
        on_line=(lambda line: print(line.render())) if echo else None,
        link=link,
        scene=scene,
        camera=camera,
    )
    schedule = load_events(events if events is not None else case / "events.jsonl")
    farm.start()
    now = 0
    ticks = 0
    started = time.monotonic()
    while ticks < farm.kernel.config.max_ticks:
        if farm.sim.realtime:
            # A real airframe sets the pace: the farm's clock is the wall clock.
            now = int((time.monotonic() - started) * S)
        arrivals: list[tuple[PipeName, str]] = []
        while schedule and schedule[0].at_ns <= now:
            arrived = schedule.pop(0)
            if arrived.act:
                arrivals.extend(farm.act(now, arrived.act, arrived.args))
            else:
                arrivals.append((PipeName(arrived.pipe), arrived.text))
        ran = farm.step(now, arrivals)
        if farm.sim.realtime:
            # Only the kernel's own ticks count against its limit; waiting for the
            # airframe is not the kernel running.
            ticks += int(ran)
            if not ran:
                if not schedule and farm.settled() and not _running(farm):
                    break
                time.sleep(0.05)
            continue
        ticks += 1
        if ran:
            now += NS_PER_TICK
            continue
        upcoming = [
            t for t in (farm.sim.next_due_ns(), schedule[0].at_ns if schedule else None) if t
        ]
        if not upcoming and farm.settled():
            break
        now = max(now, min(upcoming)) if upcoming else now
    out = Journal(journal)
    out.extend(farm.sink)
    out.close()
    return farm.result()
