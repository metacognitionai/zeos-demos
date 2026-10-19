"""Stretch's body and the atrium around it: the VLA, simulated, as a device.

The kernel reaches the world only through pipes, so the VLA is a device adapter like a
keyboard or a sensor. It takes commands from the ``stretch.command`` sink, carries them
out one at a time in virtual time, and answers on the commanding behaviour's own
``stdin``. What changed goes back as reports on actuator pipes, attributed to the
behaviour whose command caused it (``Kernel.deliver(..., on_behalf_of=...)``): a
behaviour is not told about its own effects when it resumes, and every other behaviour
is.

A behaviour has one command with the body at a time: another before the answer to the
first is refused at once, since the body cannot do two things and the answer to the
first has not been read. It obeys only the behaviour wearing the body, which the kernel announces in
``fleet.embodied`` and ``fleet.disembodied``. When the kernel takes the body back from a
wearer whose release policy is ``immediate``, the action in progress stops where it is;
under any other policy it finishes first, and the new wearer's commands wait for it.

Every command succeeds when its preconditions hold, as the demo assumes of the VLA; a
command whose preconditions do not hold is refused at once and changes nothing.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from zeos.core.allocator import ReleasePolicy
from zeos.core.events import Disembodied, Embodied, Event, JobSpawned, PipeWritten
from zeos.core.ids import DescriptorName, JobId, ObjectName, PipeName
from zeos.core.kernel import Kernel
from zeos.core.pipes import PipeFull

__all__ = ["COMMAND_PIPE", "SimulatedStretch", "Report"]

COMMAND_PIPE = PipeName("stretch.command")

S = 1_000_000_000

DEFAULT_TRAVEL_S = 3
#: The kitchen and the desks are joined by the corridor: a move between them takes
#: twice as long, and one cut short stops in the corridor.
KITCHEN = frozenset({"coffee-machine", "hotplate"})
CORRIDOR = "corridor"
PLACES = frozenset({"dock", "table", "coffee-machine", "corridor", "hotplate"})
#: The desks, `desk-1` to `desk-9`, any of which can order a coffee.
DESKS = tuple(f"desk-{n}" for n in range(1, 10))
#: What is on the table when it is set, in the order it lies there.
TABLE = "apple mug banana glass orange teacup pear saucer"
#: Where a tidied item can go. The body puts an item where it is told; knowing that an
#: apple belongs in the basket and a mug on the tray is the behaviour's job, not the VLA's.
CONTAINERS: dict[str, str] = {"basket": "basket.contents", "tray": "tray.contents"}
PUT_S = 4
HAND_S = 1
BREW_S = 10

#: The actuator each piece of physical state is reported on.
REPORT_PIPES: dict[str, PipeName] = {
    "stretch.position": PipeName("report.position"),
    "stretch.holding": PipeName("report.holding"),
    "coffee.location": PipeName("report.coffee"),
    "hotplate.power": PipeName("report.hotplate"),
    "table.items": PipeName("report.table"),
    "basket.contents": PipeName("report.basket"),
    "tray.contents": PipeName("report.tray"),
    "coffee.machine": PipeName("report.machine"),
}

#: How an empty list of items is spelled in world state.
NONE = "nothing"


def _items(value: str) -> list[str]:
    return [] if value in ("", NONE) else value.split()


def _spell(items: list[str]) -> str:
    return " ".join(items) or NONE


def is_place(name: str) -> bool:
    return name in PLACES or name in DESKS


def _across(start: str, end: str) -> bool:
    return (start in KITCHEN and end in DESKS) or (start in DESKS and end in KITCHEN)


def _travel_s(start: str, end: str) -> int:
    if start == end:
        return 0
    return 2 * DEFAULT_TRAVEL_S if _across(start, end) else DEFAULT_TRAVEL_S


def _stops_at(start: str, end: str) -> str:
    """Where a move from ``start`` to ``end`` is when it is cut short."""
    return CORRIDOR if _across(start, end) else start


@dataclass(frozen=True, slots=True)
class Report:
    """One line of what the body did, for the console."""

    at_ns: int
    text: str


@dataclass
class _Action:
    job: JobId
    command: str
    started_ns: int
    due_ns: int
    origin: str
    #: Physical state once the action is complete.
    outcome: dict[str, str]


@dataclass
class SimulatedStretch:
    kernel: Kernel
    state: dict[str, str] = field(default_factory=dict[str, str])
    names: dict[JobId, DescriptorName] = field(default_factory=dict[JobId, DescriptorName])
    policies: dict[JobId, str] = field(default_factory=dict[JobId, str])
    wearer: JobId | None = None
    queue: deque[tuple[JobId, str]] = field(default_factory=deque[tuple[JobId, str]])
    current: _Action | None = None
    brew_due_ns: int | None = None
    reports: list[Report] = field(default_factory=list[Report])

    def __post_init__(self) -> None:
        for obj in REPORT_PIPES:
            self.state[obj] = self.kernel.world.get(ObjectName(obj)) or ""

    # -- what the kernel did --------------------------------------------------

    def observe(self, event: Event, now_ns: int) -> None:
        match event:
            case JobSpawned(job=job, descriptor=descriptor):
                self.names[job] = descriptor
            case Embodied(job=job, release_policy=policy):
                self.wearer = job
                self.policies[job] = policy
            case Disembodied(job=job):
                if self.wearer == job:
                    self.wearer = None
                action = self.current
                if action is not None and action.job == job:
                    if self.policies.get(job) == ReleasePolicy.IMMEDIATE:
                        self._abort(action, now_ns)
            case PipeWritten(pipe=pipe, job=job, text=text) if pipe == COMMAND_PIPE:
                if job is None:
                    return
                command = " ".join(text)
                if job != self.wearer:
                    self._answer(job, f"refused {command}: this behaviour is not wearing the body")
                    return
                pending = self._outstanding(job)
                if pending is not None:
                    self._answer(job, f"refused {command}: still doing {pending}; read stdin first")
                    return
                self.queue.append((job, command))
            case _:
                return

    def _outstanding(self, job: JobId) -> str | None:
        """The command this behaviour is still waiting on the body for, if any."""
        if self.current is not None and self.current.job == job:
            return self.current.command
        return next((command for queued, command in self.queue if queued == job), None)

    # -- time -----------------------------------------------------------------

    def next_due_ns(self) -> int | None:
        dues = [t for t in (self.brew_due_ns, self.current and self.current.due_ns) if t]
        return min(dues) if dues else None

    def advance(self, now_ns: int) -> None:
        """Finish whatever is due by ``now_ns``, then start the next queued command."""
        if self.current is not None and self.current.due_ns <= now_ns:
            action = self.current
            self.current = None
            self._complete(action, now_ns)
        if self.brew_due_ns is not None and self.brew_due_ns <= now_ns:
            self.brew_due_ns = None
            self._report({"coffee.machine": "ready"}, by=None, now_ns=now_ns)
            self._log(now_ns, "coffee machine: coffee ready")
            self.kernel.deliver(PipeName("coffee.ready"), "coffee ready at coffee-machine")
        while self.current is None and self.queue:
            job, command = self.queue.popleft()
            self._start(job, command, now_ns)

    # -- the body -------------------------------------------------------------

    def _start(self, job: JobId, command: str, now_ns: int) -> None:
        if job != self.wearer:
            self._answer(job, f"interrupted {command}: the body was taken back before it began")
            return
        verb, _, arg = command.partition(" ")
        here = self.state["stretch.position"]
        outcome: dict[str, str]
        seconds: int
        match verb:
            case "navigate" if is_place(arg):
                outcome = {"stretch.position": arg}
                seconds = _travel_s(here, arg)
            case "put" if len(arg.split()) == 2:
                # One action from gripper empty to gripper empty: drive to the table,
                # pick the item up and put it in the container. Its boundary is
                # therefore always a moment when the body holds nothing.
                item, container = arg.split()
                on_table = _items(self.state["table.items"])
                if container not in CONTAINERS:
                    return self._refuse(job, command, f"there is no {container} here")
                if item not in on_table:
                    return self._refuse(job, command, f"there is no {item} on the table")
                if self.state["stretch.holding"] != NONE:
                    return self._refuse(job, command, "the gripper is not empty")
                inside = _items(self.state[CONTAINERS[container]])
                outcome = {
                    "stretch.position": "table",
                    "table.items": _spell([i for i in on_table if i != item]),
                    CONTAINERS[container]: _spell([*inside, item]),
                }
                seconds = _travel_s(here, "table") + PUT_S
            case "pick" if arg == "coffee":
                if self.state["stretch.holding"] != NONE:
                    return self._refuse(job, command, "the gripper is not empty")
                if self.state["coffee.location"] != here:
                    return self._refuse(
                        job, command, f"the coffee is at {self.state['coffee.location']}"
                    )
                outcome = {"stretch.holding": "coffee", "coffee.location": "gripper"}
                seconds = HAND_S
            case "place" if arg == "coffee":
                if self.state["stretch.holding"] != "coffee":
                    return self._refuse(job, command, "the gripper is not holding the coffee")
                outcome = {"stretch.holding": NONE, "coffee.location": here}
                seconds = HAND_S
            case "brew" if not arg:
                if here != "coffee-machine":
                    return self._refuse(job, command, f"the body is at {here}")
                if self.state["coffee.machine"] == "brewing":
                    return self._refuse(job, command, "the machine is already brewing")
                # A fresh cup under the spout: the last one, if any, has been delivered.
                outcome = {"coffee.machine": "brewing", "coffee.location": "coffee-machine"}
                seconds = HAND_S
            case "press" if arg == "cutoff":
                if here != "hotplate":
                    return self._refuse(job, command, f"the body is at {here}")
                outcome = {"hotplate.power": "off"}
                seconds = HAND_S
            case _:
                return self._refuse(job, command, "not a command this body knows")
        self._log(now_ns, f"stretch ▶ {command:<26} for {self.names[job]}")
        self.current = _Action(
            job=job,
            command=command,
            started_ns=now_ns,
            due_ns=now_ns + seconds * S,
            origin=here,
            outcome=outcome,
        )

    def _complete(self, action: _Action, now_ns: int) -> None:
        self._report(action.outcome, by=action.job, now_ns=now_ns)
        if action.command == "brew":
            self.brew_due_ns = now_ns + BREW_S * S
        self._log(now_ns, f"stretch ✓ {action.command}")
        self._answer(action.job, f"done {action.command}")

    def _abort(self, action: _Action, now_ns: int) -> None:
        """Stop where the body is. Where it ended up is news to the wearer, so the report
        is the body's own rather than attributed to the command that was cut short."""
        self.current = None
        target = action.outcome.get("stretch.position")
        stopped = action.origin
        if target is not None and target != action.origin:
            stopped = _stops_at(action.origin, target)
        self._report({"stretch.position": stopped}, by=None, now_ns=now_ns)
        self._log(now_ns, f"stretch ✕ {action.command} (stopped at {stopped})")
        self._answer(action.job, f"interrupted {action.command} at {stopped}")

    def _refuse(self, job: JobId, command: str, why: str) -> None:
        self._answer(job, f"refused {command}: {why}")

    # -- the world, changed by hand -------------------------------------------

    def set_table(self, now_ns: int) -> None:
        """Put everything back on the table, as somebody having lunch would."""
        self._report(
            {"table.items": TABLE, "basket.contents": NONE, "tray.contents": NONE},
            by=None,
            now_ns=now_ns,
        )
        self._log(now_ns, "somebody left fruit and cups on the table")

    def turn_hotplate_on(self, now_ns: int) -> None:
        self._report({"hotplate.power": "on"}, by=None, now_ns=now_ns)
        self._log(now_ns, "somebody turned the hotplate back on")

    # -- back to the kernel ---------------------------------------------------

    def _report(self, changes: dict[str, str], *, by: JobId | None, now_ns: int) -> None:
        for obj, value in sorted(changes.items()):
            if self.state[obj] == value:
                continue
            self.state[obj] = value
            self.kernel.deliver(REPORT_PIPES[obj], value, on_behalf_of=by)

    def _answer(self, job: JobId, text: str) -> None:
        name = self.names[job]
        done = self.kernel.descriptors[name].pipes.resolve("stdin")
        if done is None:
            raise KeyError(f"{name} binds no stdin for the body to answer on")
        try:
            self.kernel.deliver(done, text)
        except PipeFull:
            # A device cannot be parked, so a full pipe is the driver's to decide. The
            # behaviour already has answers it has not read, so its next read returns at
            # once and nothing waits on this one: it is dropped, and said so.
            self._log(
                self.kernel.clock.virtual_ns,
                f"stretch could not answer {name}: {done} is full of unread answers",
            )

    def _log(self, now_ns: int, text: str) -> None:
        self.reports.append(Report(at_ns=now_ns, text=text))
