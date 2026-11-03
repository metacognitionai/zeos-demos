"""The farm's drone and the farm under it, simulated, as a device.

The kernel reaches the world only through pipes, so the drones are a device adapter
like a keyboard or a sensor. Commands arrive on the ``drone.command`` sink -- only the
ones the flight-rules gate allowed land there -- and each goes to the drone its
behaviour is flying, which the kernel announces in ``fleet.embodied``. A drone carries
out one command at a time in virtual time and answers on the commanding behaviour's own
``stdin``. Where it went goes back as reports on actuator pipes, attributed to the
behaviour whose command caused it.

Vision and flight are assumed perfect: a flight always arrives, and an observation
returns the facts. What a drone sees is this module's ground truth (``farm.yaml``),
which the people at the page change.

A behaviour has one command with its drone at a time; another before the answer to the
first is refused at once. When the kernel takes a drone back, the release policy of the
behaviour losing it says where the action in progress may stop. Under ``immediate`` it
stops where it is, whatever it is. Under ``at-action-boundary`` a look -- observe, zoom,
search, panorama -- finishes first, so nothing is left half-looked-at, and the new
holder's commands wait for it; a flight, climb or return stops where it is, since every
point along a flight is a boundary between actions. A stopped action is answered
``interrupted ... over PLACE``.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from zeos.core.allocator import ReleasePolicy
from zeos.core.events import Disembodied, Embodied, Event, JobSpawned, PipeWritten
from zeos.core.ids import DescriptorName, JobId, ObjectName, PipeName
from zeos.core.kernel import Kernel
from zeos.core.pipes import PipeFull

from zeos_farm_drone.farm import (
    OFF_FARM,
    PADDOCKS,
    PLACES,
    S,
    flight_s,
    nearest_on_farm,
    position_between,
)

__all__ = [
    "BATTERY_PIPE",
    "COMMAND_PIPE",
    "CRITICAL_PCT",
    "DRONES",
    "FarmSim",
    "Photo",
    "Report",
    "Vehicle",
]

COMMAND_PIPE = PipeName("drone.command")
DRONES = ("kestrel",)

#: How high a drone cruises once it has taken off, and the least a panorama needs.
CRUISE_M = 40
PANORAMA_MIN_M = 100
#: Seconds per 40 m of climb or descent, and for each kind of look.
CLIMB_S_PER_40M = 1
OBSERVE_S = 3
ZOOM_S = 2
SEARCH_S = 3
PANORAMA_S = 4

#: How long a vehicle takes from starting to leave a paddock to being off the farm.
LEAVING_S = 5

#: How fast a drone's battery runs down while it is in the air, in percent a second, and
#: the charge at which its own monitor raises the alarm. On its pad in the shed a drone's
#: battery is swapped for a full one.
DRAIN_PCT_PER_S = 0.3
CRITICAL_PCT = 15
#: Where a drone's battery monitor raises the alarm, if the case listens for one.
BATTERY_PIPE = PipeName("alerts.battery")

#: The commands that move the drone, which a flight controller carries out.
MOTIONS = frozenset({"fly", "climb", "return"})

#: Which off-farm place a boundary point overlooks.
OVERLOOKS = {"boundary-west": "road", "boundary-east": "neighbour"}


def _herd(line: str) -> dict[str, int]:
    pairs = (p.partition("=") for p in line.split())
    return {k: int(v) for k, _, v in pairs if v.isdigit()}


@dataclass(frozen=True, slots=True)
class Report:
    """One line of what a drone or the farm did, for the console."""

    at_ns: int
    text: str


@dataclass(frozen=True, slots=True)
class Photo:
    """A picture a drone took, which an alert can cite by id."""

    id: str
    drone: str
    place: str
    subject: str


@dataclass
class Vehicle:
    name: str
    plate: str
    place: str
    heading: str = "parked"


@dataclass
class _Action:
    job: JobId
    command: str
    started_ns: int
    due_ns: int
    #: For a flight: where it started and where it is going, so a cut-short flight
    #: knows where it stopped.
    origin: str
    target: str
    #: Position and altitude once the action is complete.
    outcome: dict[str, str]


@dataclass
class _Drone:
    name: str
    sensors: frozenset[str]
    position: str = "shed"
    altitude: int = 0
    #: Charge left, in percent, and whether the monitor has raised the alarm on it.
    battery: float = 100.0
    alarmed: bool = False
    wearer: JobId | None = None
    current: _Action | None = None
    queue: deque[tuple[JobId, str]] = field(default_factory=deque[tuple[JobId, str]])


@dataclass
class FarmSim:
    kernel: Kernel
    sensors: Mapping[str, frozenset[str]]
    truth: Path
    names: dict[JobId, DescriptorName] = field(default_factory=dict[JobId, DescriptorName])
    policies: dict[JobId, str] = field(default_factory=dict[JobId, str])
    drones: dict[str, _Drone] = field(default_factory=dict[str, _Drone])
    cattle: dict[str, int] = field(default_factory=dict[str, int])
    calves: dict[str, int] = field(default_factory=dict[str, int])
    strays: dict[str, int] = field(default_factory=dict[str, int])
    troughs: dict[str, int] = field(default_factory=dict[str, int])
    items: dict[str, str] = field(default_factory=dict[str, str])
    vehicles: list[Vehicle] = field(default_factory=list[Vehicle])
    #: The fire, if one is burning: where, and how far from the farm.
    fire: tuple[str, int] | None = None
    photos: list[Photo] = field(default_factory=list[Photo])
    reports: list[Report] = field(default_factory=list[Report])
    #: Every observation a drone completed, as (paddock, the answer), for the story.
    observed: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    #: Vehicles on their way off the farm: when each is gone, and where to.
    leaving: list[tuple[int, Vehicle, str]] = field(default_factory=list[tuple[int, Vehicle, str]])
    #: Calves born, as (when, paddock, how many); the morning's first are born at 0.
    births: list[tuple[int, str, int]] = field(default_factory=list[tuple[int, str, int]])
    #: Every close look at a paddock's cattle, as (when, paddock).
    cattle_looks: list[tuple[int, str]] = field(default_factory=list[tuple[int, str]])
    #: What the farmer expected in each paddock before anyone looked.
    expected_at_start: str = ""
    #: Up to when the batteries have been run down.
    drained_ns: int = 0

    #: Whether the farm must run at the wall clock's pace. The map's own drone can let
    #: time jump to whatever is due next; a flight controller cannot.
    realtime = False
    #: How fast the battery runs down in the air, fitted to how fast this drone flies.
    drain_pct_per_s = DRAIN_PCT_PER_S

    def __post_init__(self) -> None:
        for name in DRONES:
            drone = _Drone(name=name, sensors=self.sensors[name])
            drone.position = self.kernel.world.get(ObjectName(f"{name}.position")) or "shed"
            drone.altitude = int(self.kernel.world.get(ObjectName(f"{name}.altitude")) or 0)
            self.drones[name] = drone
        self.expected_at_start = self.kernel.world.get(ObjectName("herd.expected")) or ""
        raw = yaml.safe_load(self.truth.read_text()) or {}
        for paddock, spec in raw.get("paddocks", {}).items():
            self.cattle[paddock] = int(spec.get("cattle", 0))
            self.calves[paddock] = int(spec.get("calves", 0))
            if self.calves[paddock]:
                self.births.append((0, paddock, self.calves[paddock]))
            self.troughs[paddock] = int(spec.get("trough", 100))
        self.items = {str(k): str(v) for k, v in raw.get("items", {}).items()}

    # -- what the kernel did --------------------------------------------------

    def observe(self, event: Event, now_ns: int) -> None:
        match event:
            case JobSpawned(job=job, descriptor=descriptor):
                self.names[job] = descriptor
            case Embodied(job=job, platform=platform, release_policy=policy):
                self.drones[platform].wearer = job
                self.policies[job] = policy
            case Disembodied(job=job, platform=platform):
                drone = self.drones[platform]
                if drone.wearer == job:
                    drone.wearer = None
                action = drone.current
                if action is not None and action.job == job:
                    policy = self.policies.get(job)
                    moving = action.command.partition(" ")[0] in MOTIONS
                    if policy == ReleasePolicy.IMMEDIATE or (
                        policy == ReleasePolicy.ACTION_BOUNDARY and moving
                    ):
                        self._abort(drone, action, now_ns)
            case PipeWritten(pipe=pipe, job=job, text=text) if pipe == COMMAND_PIPE:
                if job is None:
                    return
                command = " ".join(text)
                drone = self.drone_of(job)
                if drone is None:
                    self._answer(job, f"refused {command}: this behaviour is not flying a drone")
                    return
                pending = self._outstanding(job)
                if pending is not None:
                    self._answer(job, f"refused {command}: still doing {pending}; read stdin first")
                    return
                drone.queue.append((job, command))
            case _:
                return

    def drone_of(self, job: JobId) -> _Drone | None:
        return next((d for d in self.drones.values() if d.wearer == job), None)

    def _outstanding(self, job: JobId) -> str | None:
        """The command this behaviour is still waiting on a drone for, if any."""
        for drone in self.drones.values():
            if drone.current is not None and drone.current.job == job:
                return drone.current.command
            queued = next((c for j, c in drone.queue if j == job), None)
            if queued is not None:
                return queued
        return None

    # -- time -----------------------------------------------------------------

    def next_due_ns(self) -> int | None:
        dues = [d.current.due_ns for d in self.drones.values() if d.current is not None]
        dues += [due for due, _, _ in self.leaving]
        return min(dues) if dues else None

    def busy(self) -> bool:
        return bool(self.leaving) or any(
            d.current is not None or d.queue for d in self.drones.values()
        )

    def advance(self, now_ns: int) -> None:
        """Finish whatever is due by ``now_ns``, then start each drone's next command."""
        self._drain(now_ns)
        for due, vehicle, towards in [entry for entry in self.leaving if entry[0] <= now_ns]:
            self.leaving.remove((due, vehicle, towards))
            vehicle.place = towards
            vehicle.heading = "away"
            self._log(now_ns, f"farm     the {vehicle.name} drove off the farm onto the {towards}")
        for drone in self.drones.values():
            if drone.current is not None and self._finished(drone, drone.current, now_ns):
                action = drone.current
                drone.current = None
                self._complete(drone, action, now_ns)
            while drone.current is None and drone.queue:
                job, command = drone.queue.popleft()
                self._start(drone, job, command, now_ns)

    # -- the drones -----------------------------------------------------------

    def _start(self, drone: _Drone, job: JobId, command: str, now_ns: int) -> None:
        if job != drone.wearer:
            self._answer(job, f"interrupted {command}: the drone was taken back before it began")
            return
        verb, _, arg = command.partition(" ")
        here = drone.position
        outcome: dict[str, str] = {}
        target = here
        seconds: int
        match verb:
            case "fly" if arg in PLACES:
                target = arg
                takeoff = CLIMB_S_PER_40M if drone.altitude == 0 else 0
                outcome = {"position": arg, "altitude": str(drone.altitude or CRUISE_M)}
                seconds = takeoff + flight_s(here, arg)
            case "return" if not arg:
                target = "shed"
                outcome = {"position": "shed", "altitude": "0"}
                seconds = flight_s(here, "shed") + self._climb_s(drone.altitude, 0)
            case "climb" if arg.isdigit():
                outcome = {"altitude": arg}
                seconds = self._climb_s(drone.altitude, int(arg))
            case "observe" if not arg:
                if here not in PADDOCKS:
                    return self._refuse(job, command, f"{drone.name} is over {here}, not a paddock")
                if drone.altitude == 0:
                    return self._refuse(job, command, f"{drone.name} is on the ground")
                seconds = OBSERVE_S
            case "zoom" if arg:
                if "zoom" not in drone.sensors:
                    return self._refuse(job, command, f"{drone.name} has no zoom camera")
                if drone.altitude == 0:
                    return self._refuse(job, command, f"{drone.name} is on the ground")
                seconds = ZOOM_S
            case "search" if arg:
                if here not in PADDOCKS:
                    return self._refuse(job, command, f"{drone.name} is over {here}, not a paddock")
                if drone.altitude == 0:
                    return self._refuse(job, command, f"{drone.name} is on the ground")
                seconds = SEARCH_S
            case "panorama" if not arg:
                if "panorama" not in drone.sensors:
                    return self._refuse(job, command, f"{drone.name} has no 360-degree camera")
                if drone.altitude < PANORAMA_MIN_M:
                    return self._refuse(
                        job,
                        command,
                        f"{drone.name} is at {drone.altitude} m; a panorama needs "
                        f"{PANORAMA_MIN_M} m",
                    )
                seconds = PANORAMA_S
            case _:
                return self._refuse(job, command, "not a command a drone knows")
        self._log(now_ns, f"{drone.name:<7} ▶ {command:<22} for {self.names[job]}")
        drone.current = _Action(
            job=job,
            command=command,
            started_ns=now_ns,
            due_ns=now_ns + max(seconds, 1) * S,
            origin=here,
            target=target,
            outcome=outcome,
        )
        self._begin(drone, drone.current)

    # -- motion: the part a flight controller replaces ---------------------------
    #
    # Here a motion takes the time the map says it does. A subclass that flies a real
    # (or simulated) airframe starts the motion in ``_begin``, says when it is over in
    # ``_finished``, and stops it where it is in ``_halt``; it may point a camera in
    # ``_begin`` too, when the action is a look.

    def _begin(self, drone: _Drone, action: _Action) -> None:
        """Start an action. Here time alone carries it out."""

    def _finished(self, drone: _Drone, action: _Action, now_ns: int) -> bool:
        return action.due_ns <= now_ns

    def _halt(self, drone: _Drone, action: _Action, now_ns: int) -> str:
        """Stop the action in hand where it is; returns the place it stopped over."""
        if action.target == action.origin:
            return drone.position
        span = action.due_ns - action.started_ns
        fraction = (now_ns - action.started_ns) / span if span else 1.0
        return nearest_on_farm(position_between(action.origin, action.target, fraction))

    def altitude_now(self, drone: _Drone) -> int:
        """How high the drone is at this moment, for the page. Here the map knows it
        only between actions; a flight controller reports it all the time."""
        return drone.altitude

    def progress(self, action: _Action, now_ns: int) -> float:
        """How far through the action in hand the drone is, from 0 to 1."""
        span = max(action.due_ns - action.started_ns, 1)
        return min(max((now_ns - action.started_ns) / span, 0.0), 1.0)

    # -- the batteries ----------------------------------------------------------------

    def airborne(self, drone: _Drone) -> bool:
        """Off the ground, or taking off: a motion from the pad is already flight."""
        if self.altitude_now(drone) > 0:
            return True
        action = drone.current
        return action is not None and action.command.partition(" ")[0] in MOTIONS

    def _drain(self, now_ns: int) -> None:
        """Run each flying drone's battery down for the time since the last step, and
        raise the alarm when one reaches critical. A drone on its pad gets a full one."""
        seconds = max(now_ns - self.drained_ns, 0) / S
        self.drained_ns = max(now_ns, self.drained_ns)
        for drone in self.drones.values():
            if self.airborne(drone):
                drone.battery = max(drone.battery - self.drain_pct_per_s * seconds, 0.0)
                if drone.battery <= CRITICAL_PCT and not drone.alarmed:
                    self._alarm(drone, now_ns)
            elif drone.position == "shed" and drone.battery < 100:
                drone.battery = 100.0
                drone.alarmed = False
                self._log(now_ns, f"farm     {drone.name}'s battery was swapped for a full one")

    def _alarm(self, drone: _Drone, now_ns: int) -> None:
        drone.alarmed = True
        self._log(now_ns, f"{drone.name:<7} ⚠ battery critically low ({round(drone.battery)}%)")
        # A case written before drones had batteries does not listen for the alarm.
        if self.kernel.pipes.has(BATTERY_PIPE):
            self.kernel.deliver(BATTERY_PIPE, f"{drone.name} battery={round(drone.battery)}%")

    @staticmethod
    def _climb_s(start: int, end: int) -> int:
        return math.ceil(abs(end - start) / 40) * CLIMB_S_PER_40M

    def _complete(self, drone: _Drone, action: _Action, now_ns: int) -> None:
        self._move(drone, action.outcome, by=action.job)
        verb, _, arg = action.command.partition(" ")
        match verb:
            case "observe":
                answer = self._observation(drone.position)
                self.observed.append((drone.position, answer))
            case "zoom":
                answer = self._zoom(drone, arg)
            case "search":
                answer = self._search(drone, arg)
            case "panorama":
                answer = self._panorama(drone)
            case _:
                answer = f"done {action.command}"
        self._log(now_ns, f"{drone.name:<7} ✓ {action.command}")
        self._answer(action.job, answer)

    def _abort(self, drone: _Drone, action: _Action, now_ns: int) -> None:
        """Stop where the drone is. Where it ended up is news to the behaviour that asked
        for the flight, so the report is the drone's own rather than attributed to it."""
        drone.current = None
        stopped = self._halt(drone, action, now_ns)
        self._move(drone, {"position": stopped}, by=None)
        self._log(now_ns, f"{drone.name:<7} ✕ {action.command} (stopped over {stopped})")
        self._answer(action.job, f"interrupted {action.command} over {stopped}")

    def _refuse(self, job: JobId, command: str, why: str) -> None:
        self._answer(job, f"refused {command}: {why}")

    # -- what a drone sees ----------------------------------------------------

    def _photo(self, drone: _Drone, subject: str) -> str:
        photo = Photo(
            id=f"p-{len(self.photos) + 1:04d}",
            drone=drone.name,
            place=drone.position,
            subject=subject,
        )
        self.photos.append(photo)
        return photo.id

    def _vehicles_seen_from(self, place: str) -> list[Vehicle]:
        seen = {place, OVERLOOKS.get(place, place)}
        return [v for v in self.vehicles if v.place in seen]

    def _observation(self, paddock: str) -> str:
        return f"observed {paddock}: {self._in_sight(paddock)}"

    def _in_sight(self, paddock: str) -> str:
        """What a look at ``paddock`` takes in, whatever it was for: the head of cattle,
        the vehicles, and the trough."""
        vehicles = ",".join(v.name for v in self._vehicles_seen_from(paddock)) or "none"
        return (
            f"cattle={self.cattle[paddock] + self.strays.get(paddock, 0)} "
            f"vehicles={vehicles} trough={self.troughs[paddock]}%"
        )

    def _zoom(self, drone: _Drone, target: str) -> str:
        here = drone.position
        if target == "vehicle":
            seen = self._vehicles_seen_from(here)
            if not seen:
                return f"zoomed vehicle at {here}: no vehicle in sight"
            vehicle = seen[0]
            photo = self._photo(drone, f"{vehicle.name} {vehicle.plate} at {vehicle.place}")
            where = "" if vehicle.place == here else f" on the {vehicle.place}"
            return (
                f"zoomed vehicle at {here}: {vehicle.name}{where} plate={vehicle.plate} "
                f"heading={vehicle.heading} photo={photo}"
            )
        if target == "cattle" and here in PADDOCKS:
            head = self.cattle[here] + self.strays.get(here, 0)
            photo = self._photo(drone, f"{head} head at {here}")
            self.cattle_looks.append((self.kernel.clock.virtual_ns, here))
            return (
                f"zoomed cattle at {here}: head={head} calves={self._unrecorded(here)} "
                f"strays={self.strays.get(here, 0)} photo={photo}"
            )
        if target == "trough" and here in PADDOCKS:
            photo = self._photo(drone, f"trough at {here}")
            return f"zoomed trough at {here}: level={self.troughs[here]}% photo={photo}"
        return f"zoomed {target} at {here}: nothing of that kind in sight"

    def _search(self, drone: _Drone, item: str) -> str:
        here = drone.position
        if self.items.get(item) == here:
            photo = self._photo(drone, f"{item} at {here}")
            return f"found {item} at {here}: photo={photo} {self._in_sight(here)}"
        return f"no {item} at {here}: {self._in_sight(here)}"

    def _panorama(self, drone: _Drone) -> str:
        if self.fire is None:
            photo = self._photo(drone, f"360 view from {drone.altitude} m")
            return f"panorama at {drone.altitude}m: no smoke in sight photo={photo}"
        direction, km = self.fire
        photo = self._photo(drone, f"smoke {km} km {direction}")
        return f"panorama at {drone.altitude}m: smoke={km}km-{direction} photo={photo}"

    def _unrecorded(self, paddock: str) -> int:
        """Calves in ``paddock`` that the farmer's records do not count yet: a calf at
        foot is untagged, and it is the count in the records that says it is known."""
        initial = _herd(self.expected_at_start).get(paddock, 0)
        recorded = _herd(self.kernel.world.get(ObjectName("herd.expected")) or "").get(
            paddock, initial
        )
        return max(0, self.calves.get(paddock, 0) - (recorded - initial))

    def calves_looked_at(self, paddock: str) -> int:
        """Calves born in ``paddock`` before the last close look at its cattle."""
        times = [at for at, p in self.cattle_looks if p == paddock]
        if not times:
            return 0
        return sum(n for at, p, n in self.births if p == paddock and at <= max(times))

    # -- the farm, changed by hand --------------------------------------------

    def vehicle_enters(self, now_ns: int, paddock: str, name: str, plate: str) -> None:
        self.vehicles.append(Vehicle(name=name, plate=plate, place=paddock))
        self._log(now_ns, f"farm     a {name} ({plate}) drove into {paddock}")

    def vehicle_leaves(self, now_ns: int, towards: str) -> None:
        """The most recent vehicle on the farm starts driving off it, onto the road or
        the neighbour's, and is gone a few seconds later."""
        leaving = {id(v) for _, v, _ in self.leaving}
        on_farm = [v for v in self.vehicles if v.place not in OFF_FARM and id(v) not in leaving]
        if not on_farm or towards not in OFF_FARM:
            return
        vehicle = on_farm[-1]
        vehicle.heading = f"towards-{towards}"
        self.leaving.append((now_ns + LEAVING_S * S, vehicle, towards))
        self._log(now_ns, f"farm     the {vehicle.name} started driving towards the {towards}")

    def calves_born(self, now_ns: int, paddock: str, count: int) -> None:
        self.cattle[paddock] += count
        self.calves[paddock] = self.calves.get(paddock, 0) + count
        self.births.append((now_ns, paddock, count))
        self._log(now_ns, f"farm     {count} calves born in {paddock}")

    def stray(self, now_ns: int, start: str, end: str) -> None:
        if self.cattle[start] == 0:
            return
        self.cattle[start] -= 1
        self.strays[end] = self.strays.get(end, 0) + 1
        self._log(now_ns, f"farm     a cow strayed from {start} into {end}")

    def trough_fault(self, now_ns: int, paddock: str, level: int) -> None:
        self.troughs[paddock] = level
        self._log(now_ns, f"farm     the trough in {paddock} dropped to {level}%")

    def park(self, now_ns: int, item: str, paddock: str) -> None:
        self.items[item] = paddock
        self._log(now_ns, f"farm     the {item} was left in {paddock}")

    def battery_fails(self, now_ns: int) -> str | None:
        """A cell fails in flight: the charge drops to critical at once. Returns what the
        drone's monitor raises the alarm with, unless it has raised it already."""
        drone = self.drones[DRONES[0]]
        drone.battery = min(drone.battery, CRITICAL_PCT - 3)
        self._log(now_ns, f"farm     a cell failed in {drone.name}'s battery")
        if drone.alarmed:
            return None
        drone.alarmed = True
        self._log(now_ns, f"{drone.name:<7} ⚠ battery critically low ({round(drone.battery)}%)")
        return f"{drone.name} battery={round(drone.battery)}%"

    def start_fire(self, now_ns: int, direction: str, km: int) -> None:
        self.fire = (direction, km)
        self._log(now_ns, f"farm     smoke {km} km to the {direction}")

    # -- back to the kernel ---------------------------------------------------

    def _move(self, drone: _Drone, changes: Mapping[str, str], *, by: JobId | None) -> None:
        for key in sorted(changes):
            value = changes[key]
            current = drone.position if key == "position" else str(drone.altitude)
            if current == value:
                continue
            if key == "position":
                drone.position = value
            else:
                drone.altitude = int(value)
            self.kernel.deliver(PipeName(f"report.{drone.name}.{key}"), value, on_behalf_of=by)

    def _answer(self, job: JobId, text: str) -> None:
        name = self.names[job]
        done = self.kernel.descriptors[name].pipes.resolve("stdin")
        if done is None:
            raise KeyError(f"{name} binds no stdin for a drone to answer on")
        try:
            self.kernel.deliver(done, text)
        except PipeFull:
            # A device cannot be parked, so a full pipe is the driver's to decide. The
            # behaviour already has answers it has not read, so its next read returns at
            # once and nothing waits on this one: it is dropped, and said so.
            self._log(
                self.kernel.clock.virtual_ns,
                f"farm     could not answer {name}: {done} is full of unread answers",
            )

    def _log(self, now_ns: int, text: str) -> None:
        self.reports.append(Report(at_ns=now_ns, text=text))
