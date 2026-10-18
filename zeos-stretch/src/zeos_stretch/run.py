"""Run the atrium: the kernel, the simulated VLA, and the morning's events.

``zeos.driver.Driver`` delivers a fixed schedule. The VLA's answers are not fixed --
they arrive when an action it was given finishes -- so this loop is the driver's loop
with the VLA added: after every tick it hands the new journal events to the body, and
when the kernel has nothing runnable it jumps virtual time to whatever is due next,
from the schedule or from the body.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from zeos.core.events import (
    Disembodied,
    Embodied,
    Event,
    FaultRaised,
    Injected,
    JobCompleted,
    JobPreempted,
    JobSpawned,
    JobStateChanged,
    VectorFired,
)
from zeos.core.ids import DescriptorName, JobId, JobState, PipeName
from zeos.core.kernel import Kernel, KernelConfig
from zeos.descriptor.loader import load_case
from zeos.descriptor.schema import Descriptor
from zeos.driver import build_kernel, load_schedule
from zeos.journal.writer import Journal
from zeos.machine.base import MachineRequest
from zeos.machine.seat import CommandSeat, CommandSource, TapeSource

from zeos_stretch.vla import COMMAND_PIPE, S, SimulatedStretch

__all__ = ["CASE", "Atrium", "Line", "RunResult", "aliases_of", "run"]

CASE = Path(__file__).resolve().parents[2] / "cases" / "atrium"

#: Virtual cost of one token boundary: a forward pass, the only thing the kernel's own
#: time is spent on. Everything slow in the atrium is the body's.
NS_PER_TICK = 10_000_000


@dataclass(frozen=True, slots=True)
class RunResult:
    kernel: Kernel
    events: tuple[Event, ...]
    body: SimulatedStretch
    lines: tuple[str, ...]


def aliases_of(descriptors: Mapping[DescriptorName, Descriptor]) -> dict[str, list[str]]:
    """The pipe names each behaviour may say, which is what a model is told it can name."""
    return {
        str(name): [a for a in ("stdin", "stdout", "tools") if d.pipes.resolve(a)]
        + sorted(d.pipes.extra)
        for name, d in sorted(descriptors.items())
    }


@dataclass(frozen=True, slots=True)
class Line:
    """One line of the narration: what the kernel, the body or the world did."""

    at_ns: int
    kind: str  # kernel | body | interrupt | world | command | fault | resume
    text: str

    def render(self) -> str:
        return f"{self.at_ns / S:6.1f}s  {self.text}"


class Atrium:
    """One morning in the atrium: the kernel, the body, and the narration of both.

    Advanced one step at a time by whoever owns the clock -- ``run`` jumps virtual time
    from event to event, the live server follows the wall clock -- so both see the same
    sequence of kernel ticks for the same sequence of arrivals.
    """

    def __init__(
        self,
        case: Path = CASE,
        *,
        source: CommandSource | None = None,
        show_commands: bool = False,
        on_line: Callable[[Line], None] | None = None,
    ) -> None:
        self.bundle = load_case(case)
        self.sink: list[Event] = []
        self.names: dict[JobId, DescriptorName] = {}
        self.states: dict[JobId, JobState] = {}
        self.wearer: JobId | None = None
        self.notices: dict[DescriptorName, str] = {}
        self.lines: list[Line] = []
        self._show_commands = show_commands
        self._on_line = on_line
        self._seen = 0
        self._shown = 0
        seat = CommandSeat(
            source=source if source is not None else TapeSource(self.bundle.scripts),
            on_command=self._heard,
        )
        self.kernel, _ = build_kernel(
            self.bundle,
            machine=seat,
            journal_sink=self.sink,
            config=KernelConfig(case=self.bundle.name),
        )
        self.body = SimulatedStretch(self.kernel)

    # -- driving ----------------------------------------------------------------

    def start(self) -> None:
        self.kernel.start()
        for name in self.bundle.boot:
            self.kernel.spawn(name)
        self._settle(0)

    def step(self, now_ns: int, arrivals: Sequence[tuple[PipeName, str]] = ()) -> bool:
        """Deliver what arrived by ``now_ns``, let the body catch up, run one tick.
        Returns whether the kernel had anything to run."""
        self.kernel.advance_time(now_ns)
        for pipe, text in arrivals:
            self.say(now_ns, "world", f"world      {pipe} \u25c0\u2500\u2500 {text}")
            self.kernel.deliver(pipe, text)
        self.body.advance(now_ns)
        self._settle(now_ns)
        ran = self.kernel.tick()
        self._settle(now_ns)
        self.body.advance(now_ns)
        self._settle(now_ns)
        return ran

    def settled(self) -> bool:
        """Nothing for the body to do and nothing it owes an answer to."""
        return self.body.current is None and not self.body.queue

    def live(self, name: str) -> bool:
        """Whether a job of this behaviour is running or waiting to."""
        return any(
            str(self.names[job]) == name and not state.is_terminal
            for job, state in self.states.items()
        )

    def spawn(self, now_ns: int, name: str) -> None:
        self.kernel.advance_time(now_ns)
        self.kernel.spawn(DescriptorName(name))
        self._settle(now_ns)

    def act(self, now_ns: int, action: str, arg: str = "") -> list[tuple[PipeName, str]]:
        """What a person in the atrium did. Returns the pipe writes it amounts to, for
        the next step to deliver; the rest changes the room directly."""
        self.kernel.advance_time(now_ns)
        match action:
            case "order":
                self.say(now_ns, "you", f"you        asked for a coffee at {arg}")
                return [(PipeName("requests.coffee"), arg)]
            case "alarm":
                self.say(now_ns, "you", "you        set off the smoke alarm")
                return [(PipeName("sensors.smoke"), "smoke near the hotplate")]
            case "mess":
                self.body.set_table(now_ns)
                if not self.live("tidy-atrium"):
                    self.spawn(now_ns, "tidy-atrium")
            case "hotplate":
                self.body.turn_hotplate_on(now_ns)
            case _:
                raise ValueError(f"nothing called {action!r} can be done in the atrium")
        return []

    # -- narration ----------------------------------------------------------------

    def say(self, now_ns: int, kind: str, text: str) -> None:
        line = Line(at_ns=now_ns, kind=kind, text=text)
        self.lines.append(line)
        if self._on_line is not None:
            self._on_line(line)

    def _heard(self, job: JobId, line: str, request: MachineRequest) -> None:
        if self._show_commands:
            name = self.names.get(job, job)
            self.say(self.kernel.clock.virtual_ns, "command", f"{name!s:<15} \u00bb {line}")

    def _settle(self, now_ns: int) -> None:
        while self._seen < len(self.sink):
            event = self.sink[self._seen]
            self._seen += 1
            self.body.observe(event, now_ns)
            self._narrate(event, now_ns)
        for report in self.body.reports[self._shown :]:
            self.say(report.at_ns, "body", report.text)
        self._shown = len(self.body.reports)
        if self.kernel.pipes.get(COMMAND_PIPE).available:
            self.kernel.drain(COMMAND_PIPE)
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
            case Embodied(job=job):
                self.wearer = job
                self.say(now_ns, "kernel", f"kernel     {names[job]} is wearing the body")
            case Disembodied(job=job, reason=reason):
                if self.wearer == job:
                    self.wearer = None
                self.say(now_ns, "kernel", f"kernel     {names[job]} lost the body: {reason}")
            case JobPreempted(job=job, by_job=by):
                self.say(now_ns, "kernel", f"kernel     {names[job]} preempted by {names[by]}")
            case Injected(job=job, text=text) if text and text[0] == "<RESUME>":
                self.notices[names[job]] = " ".join(text)
                self.say(
                    now_ns, "resume", f"kernel     {names[job]} \u25c0\u2500\u2500 {' '.join(text)}"
                )
            case JobCompleted(job=job):
                self.say(now_ns, "kernel", f"kernel     {names[job]} finished")
            case FaultRaised(job=job, detail=detail):
                self.say(now_ns, "fault", f"FAULT      {names.get(job, job)}: {detail}")
            case _:
                return


def run(
    case: Path = CASE,
    *,
    events: Path | None = None,
    journal: Path | None = None,
    source: CommandSource | None = None,
    echo: bool = False,
    show_commands: bool = False,
) -> RunResult:
    """Run the morning to its end in virtual time. ``source`` answers each behaviour's
    turn: the descriptors' own tapes by default, or a model. Either way every command
    goes through the same seat, so the kernel cannot tell which it is."""
    atrium = Atrium(
        case,
        source=source,
        show_commands=show_commands,
        on_line=(lambda line: print(line.render())) if echo else None,
    )
    schedule = list(load_schedule(events if events is not None else case / "events.jsonl"))
    atrium.start()
    now = 0
    ticks = 0
    while ticks < atrium.kernel.config.max_ticks:
        arrivals: list[tuple[PipeName, str]] = []
        while schedule and schedule[0].at_ns <= now:
            arrived = schedule.pop(0)
            arrivals.append((arrived.pipe, arrived.text))
        ran = atrium.step(now, arrivals)
        ticks += 1
        if ran:
            now += NS_PER_TICK
            continue
        upcoming = [
            t for t in (atrium.body.next_due_ns(), schedule[0].at_ns if schedule else None) if t
        ]
        if not upcoming and atrium.settled():
            break
        now = max(now, min(upcoming)) if upcoming else now
    out = Journal(journal)
    out.extend(atrium.sink)
    out.close()
    return RunResult(
        kernel=atrium.kernel,
        events=tuple(atrium.sink),
        body=atrium.body,
        lines=tuple(line.render() for line in atrium.lines),
    )
