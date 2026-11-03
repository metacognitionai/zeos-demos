"""What happens on the farm, shown in Gazebo.

The PX4 drone flies over a Gazebo world (``scripts/gz_world.py``) whose paddocks,
fences and buildings never change. What does change -- the herds, calves born, a cow
that strayed, a trough running dry, a vehicle driving in and away, a lost motorbike,
smoke on the horizon -- is drawn from ``FarmSim``'s state: each step ``props`` says
what the world should hold, and ``Scene`` makes Gazebo hold it through gz-sim's
``create_multiple``, ``remove`` and ``set_pose_vector`` services.

It is scenery and nothing more. What a drone sees is still decided by the farm's
ground truth; nothing is read back from Gazebo. A prop's name says what it is and
its state (``water-ridge-20``), so a change of state is a remove and a create, and a
change of place is a pose.

The kernel's thread never waits on Gazebo: ``show`` hands the latest wish to a thread
of its own, which applies only the newest one it has not applied yet. A service call
Gazebo refuses raises on the next ``show``.
"""

from __future__ import annotations

import math
import random
import re
import subprocess
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from zeos_farm_drone.farm import PADDOCKS, S
from zeos_farm_drone.geo import offset_m
from zeos_farm_drone.sim import LEAVING_S, OVERLOOKS, FarmSim, _Drone

__all__ = [
    "PADDOCK_M",
    "Pose",
    "Prop",
    "Scene",
    "creek_x",
    "herd_centre",
    "props",
    "subject",
    "trough_at",
    "xy",
]

#: A paddock's side: the map's 150 m less a lane between neighbouring fences.
PADDOCK_M = 136.0
#: How far out smoke is drawn, whatever the reported distance: far enough to read as
#: off the farm, near enough to stay inside the 3D view.
SMOKE_M = 700.0
#: Moves smaller than this are not worth a service call.
MOVED_M = 0.05
#: Props created by one service call. Gazebo stops its clock while it loads what it is
#: asked to create, and PX4 SITL runs in lockstep with that clock: a whole herd at once
#: stalls the flight controller for over a second, long enough for a command to it to
#: time out. Twenty at a time keeps each stall to a fraction of that.
CREATE_BATCH = 20

_HEADING = {
    "north": (0, 1),
    "north-east": (1, 1),
    "east": (1, 0),
    "south-east": (1, -1),
    "south": (0, -1),
    "south-west": (-1, -1),
    "west": (-1, 0),
    "north-west": (-1, 1),
}


def xy(place: str) -> tuple[float, float]:
    """``place`` in Gazebo's frame: x east and y north of the shed, in metres."""
    north, east = offset_m(place)
    return east, north


def creek_x(y: float) -> float:
    """Where the creek runs at northing ``y``: down the creek paddock's east side, clear
    of the herd and of a parked vehicle, winding as a creek does."""
    return xy("creek")[0] + 35 + 9 * math.sin(y / 27)


def herd_centre(paddock: str) -> tuple[float, float]:
    """Where a paddock's herd grazes: north-west of the middle, by the trough."""
    x, y = xy(paddock)
    return x - 16, y + 26


def _grazing(paddock: str, i: int) -> Pose:
    """Cow ``i`` of a herd: a loose, unhurried cluster (a sunflower spiral, nudged), each
    facing its own way. The same cow always stands in the same place."""
    cx, cy = herd_centre(paddock)
    nudge = random.Random(f"{paddock}-{i}")
    r = 2.6 * math.sqrt(i + 0.5) + nudge.uniform(-0.6, 0.6)
    a = i * 2.39996 + nudge.uniform(-0.3, 0.3)
    return Pose(cx + r * math.cos(a), cy + r * math.sin(a), yaw=nudge.uniform(-math.pi, math.pi))


#: How many looks a cow may have (``scripts/gz_world.py`` draws one model for each).
COW_LOOKS = 6


def _coat(name: str) -> int:
    """Which of the cow models ``name`` is: the same cow always looks the same."""
    return random.Random(name).randrange(COW_LOOKS)


def trough_at(paddock: str) -> tuple[float, float]:
    """The trough sits inside the west fence, a little north of the middle."""
    x, y = xy(paddock)
    return x - PADDOCK_M / 2 + 8, y + 10


@dataclass(frozen=True, slots=True)
class Pose:
    x: float
    y: float
    z: float = 0.0
    yaw: float = 0.0

    def near(self, other: Pose) -> bool:
        return (
            math.dist((self.x, self.y, self.z), (other.x, other.y, other.z)) < MOVED_M
            and abs(self.yaw - other.yaw) < 0.01
        )


@dataclass(frozen=True, slots=True)
class Prop:
    """One thing Gazebo should show: its name, what it looks like, and where."""

    name: str
    kind: str
    pose: Pose


# -- what the farm should look like ------------------------------------------------


def props(sim: FarmSim, now_ns: int) -> dict[str, Prop]:
    """Everything that changes on the farm, where it is at ``now_ns``."""
    out: list[Prop] = []
    for paddock in PADDOCKS:
        x, y = xy(paddock)
        calves = sim.calves.get(paddock, 0)
        for i in range(sim.cattle.get(paddock, 0) - calves):
            name = f"cow-{paddock}-{i}"
            out.append(Prop(name, f"cow-{_coat(name)}", _grazing(paddock, i)))
        # A calf at foot: beside a cow of the herd, facing the way she does.
        for i in range(calves):
            mother = _grazing(paddock, i)
            out.append(
                Prop(
                    f"calf-{paddock}-{i}",
                    "calf",
                    Pose(
                        mother.x + 1.4 * math.cos(mother.yaw + 1.2),
                        mother.y + 1.4 * math.sin(mother.yaw + 1.2),
                        yaw=mother.yaw,
                    ),
                )
            )
        # A stray stands apart from the herd it has joined, by the south-east fence.
        for i in range(sim.strays.get(paddock, 0)):
            name = f"stray-{paddock}-{i}"
            out.append(Prop(name, f"cow-{_coat(name)}", Pose(x + 15 + i * 4, y - 45, yaw=0.8)))
        level = sim.troughs.get(paddock, 100)
        tx, ty = trough_at(paddock)
        out.append(Prop(f"water-{paddock}-{level}", f"water-{level}", Pose(tx, ty)))
        if level < 50:
            out.append(Prop(f"leak-{paddock}", "leak", Pose(tx + 2, ty - 5)))
    for i, (item, paddock) in enumerate(sorted(sim.items.items())):
        x, y = xy(paddock)
        out.append(Prop(f"item-{item}", item, Pose(x - 20 + i * 5, y - 30, yaw=0.4)))
    leaving = {id(vehicle): (due, towards) for due, vehicle, towards in sim.leaving}
    for i, vehicle in enumerate(sim.vehicles):
        parked = _parked(vehicle.place, i)
        pose = parked
        if id(vehicle) in leaving:
            due, towards = leaving[id(vehicle)]
            end = _parked(towards, i)
            done = min(max(1 - (due - now_ns) / (LEAVING_S * S), 0.0), 1.0)
            pose = Pose(
                parked.x + (end.x - parked.x) * done,
                parked.y + (end.y - parked.y) * done,
                yaw=math.atan2(end.y - parked.y, end.x - parked.x),
            )
        out.append(Prop(f"vehicle-{i}-{vehicle.name}", vehicle.name, pose))
    if sim.fire is not None:
        direction, _ = sim.fire
        dx, dy = _HEADING[direction]
        cx = sum(xy(p)[0] for p in PADDOCKS) / len(PADDOCKS)
        cy = sum(xy(p)[1] for p in PADDOCKS) / len(PADDOCKS)
        norm = math.hypot(dx, dy)
        out.append(
            Prop(
                f"smoke-{direction}",
                "smoke",
                Pose(cx + dx / norm * SMOKE_M, cy + dy / norm * SMOKE_M),
            )
        )
    return {p.name: p for p in out}


def subject(sim: FarmSim, drone: _Drone, command: str, now_ns: int) -> tuple[float, float, float]:
    """Where a look -- ``observe``, ``zoom``, ``search``, ``panorama`` -- points the
    camera: at the prop it is about, or straight down when there is none."""
    verb, _, arg = command.partition(" ")
    here = drone.position
    x, y = xy(here)
    wanted = props(sim, now_ns)
    match verb, arg:
        case "zoom", "vehicle":
            seen = {here, OVERLOOKS.get(here, here)}
            for i, vehicle in enumerate(sim.vehicles):
                if vehicle.place in seen:
                    pose = wanted[f"vehicle-{i}-{vehicle.name}"].pose
                    return pose.x, pose.y, 1.0
        case "zoom", "cattle":
            return *herd_centre(here), 0.0
        case "zoom", "trough":
            return *trough_at(here), 0.5
        case "search", item if sim.items.get(item) == here:
            pose = wanted[f"item-{item}"].pose
            return pose.x, pose.y, 0.5
        case "panorama", _ if sim.fire is not None:
            pose = wanted[f"smoke-{sim.fire[0]}"].pose
            return pose.x, pose.y, 120.0
        case "panorama", _:
            return x, y + SMOKE_M, 0.0  # the horizon to the north
        case _:
            pass
    return x, y, 0.0


def _parked(place: str, index: int) -> Pose:
    """Where vehicle ``index`` stands in ``place``: on a paddock's south side, or on
    the road or the neighbour's track."""
    x, y = xy(place)
    return Pose(x + 10 + index * 9, y - 10)


# -- what each kind of thing looks like --------------------------------------------

#: Kinds drawn by a model of their own: the farm's (``scripts/gz_world.py``) or one
#: downloaded from Gazebo Fuel (``scripts/fetch_assets.py``).
MODELS = {
    "calf": "farm_calf",
    "white-ute": "pickup",
    "red-truck": "hatchback_red",
    "grey-van": "TruckBox",
    "motorbike": "farm_motorbike",
    "quad-bike": "farm_quad_bike",
    "toolbox": "farm_toolbox",
    "smoke": "farm_smoke",
}
_WATER = (
    "<material><ambient>0.10 0.24 0.30 1</ambient><diffuse>0.10 0.24 0.30 1</diffuse>"
    "<pbr><metal><roughness>0.05</roughness><metalness>0.2</metalness></metal></pbr></material>"
)
_MUD = (
    "<material><ambient>0.20 0.15 0.10 1</ambient><diffuse>0.20 0.15 0.10 1</diffuse>"
    "<pbr><metal><roughness>0.2</roughness><metalness>0.0</metalness></metal></pbr></material>"
)


def body(kind: str) -> str:
    """What a prop of ``kind`` is made of, as SDF inside its model."""
    if kind.startswith("cow-"):
        return f'<include merge="true"><uri>model://farm_hereford_{kind[4:]}</uri></include>'
    if kind in MODELS:
        return f'<include merge="true"><uri>model://{MODELS[kind]}</uri></include>'
    if kind.startswith("water-"):
        depth = max(0.8 * int(kind.removeprefix("water-")) / 100, 0.02)
        geometry = f"<box><size>5.84 1.32 {depth:g}</size></box>"
        lift = 0.1 + depth / 2
        return (
            f'<link name="l"><visual name="v"><pose>0 0 {lift:g} 0 0 0</pose>'
            f"<geometry>{geometry}</geometry>{_WATER}</visual></link>"
        )
    if kind == "leak":
        return (
            '<link name="l"><visual name="v"><pose>0 0 0.06 0 0 0</pose>'
            "<geometry><cylinder><radius>5</radius><length>0.02</length></cylinder></geometry>"
            f"{_MUD}</visual></link>"
        )
    raise ValueError(f"no look for a prop of kind {kind!r}")


def model_sdf(prop: Prop) -> str:
    p = prop.pose
    return (
        f'<sdf version="1.9"><model name="{prop.name}"><static>true</static>'
        f"<pose>{p.x:.2f} {p.y:.2f} {p.z:.2f} 0 0 {p.yaw:.3f}</pose>"
        f"{body(prop.kind)}</model></sdf>"
    )


# -- making Gazebo show it ---------------------------------------------------------

#: Every prop's name starts with one of these; anything else in the world is not ours.
OURS = re.compile(r"^(cow|calf|stray|water|leak|item|vehicle|smoke)-")


def gz(*args: str) -> str:
    """Run the gz command line and return what it printed."""
    done = subprocess.run(["gz", *args], capture_output=True, text=True, timeout=30, check=True)
    return done.stdout


class Scene:
    """Gazebo's world ``world``, kept showing what ``props`` says."""

    def __init__(self, world: str = "farm", *, run: Callable[..., str] = gz) -> None:
        self.world = world
        self._run = run
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._wanted: dict[str, Prop] | None = None
        self._failed: BaseException | None = None
        self._stopping = False
        # Props a previous run left behind are taken over: their names say what they
        # are, and their poses are set again on the first apply.
        self.shown: dict[str, Pose | None] = {name: None for name in self._existing()}
        self._thread = threading.Thread(target=self._loop, name="gazebo", daemon=True)
        self._thread.start()

    def show(self, sim: FarmSim, now_ns: int) -> None:
        self.check()
        wanted = props(sim, now_ns)
        with self._wake:
            self._wanted = wanted
            self._wake.notify()

    def check(self) -> None:
        """Raise what Gazebo refused."""
        with self._lock:
            failed = self._failed
        if failed is not None:
            raise failed

    def close(self) -> None:
        """Stop applying. What Gazebo shows stays, for the next run to take over."""
        with self._wake:
            self._stopping = True
            self._wake.notify()
        self._thread.join(30)

    def _loop(self) -> None:
        while True:
            with self._wake:
                while self._wanted is None and not self._stopping:
                    self._wake.wait()
                if self._stopping:
                    return
                wanted, self._wanted = self._wanted, None
            assert wanted is not None
            try:
                self.apply(wanted)
            except BaseException as error:  # noqa: BLE001 -- handed to the kernel's thread
                with self._lock:
                    self._failed = error
                return

    def apply(self, wanted: Mapping[str, Prop]) -> None:
        """Remove what should not be there, create what is missing, move what moved."""
        for name in sorted(set(self.shown) - set(wanted)):
            self._call("remove", "gz.msgs.Entity", f'name: "{name}" type: MODEL')
            del self.shown[name]
        new = [wanted[n] for n in sorted(set(wanted) - set(self.shown))]
        for start in range(0, len(new), CREATE_BATCH):
            batch = new[start : start + CREATE_BATCH]
            sdfs = ", ".join(f"{{sdf: '{model_sdf(p)}'}}" for p in batch)
            self._call("create_multiple", "gz.msgs.EntityFactory_V", f"data: [{sdfs}]")
            self.shown.update({p.name: p.pose for p in batch})
        moved = [
            p
            for name, p in sorted(wanted.items())
            if (was := self.shown[name]) is None or not was.near(p.pose)
        ]
        if moved:
            poses = ", ".join(_pose_msg(p) for p in moved)
            self._call("set_pose_vector", "gz.msgs.Pose_V", f"pose: [{poses}]")
            self.shown.update({p.name: p.pose for p in moved})

    def _call(self, service: str, reqtype: str, req: str) -> None:
        out = self._run(
            "service", "-s", f"/world/{self.world}/{service}",
            "--reqtype", reqtype, "--reptype", "gz.msgs.Boolean",
            "--timeout", "10000", "--req", req,
        )  # fmt: skip
        if "data: true" not in out:
            raise RuntimeError(f"Gazebo refused {service}: {out.strip() or 'no answer'}")

    def _existing(self) -> list[str]:
        out = self._run(
            "service", "-s", f"/world/{self.world}/scene/info",
            "--reqtype", "gz.msgs.Empty", "--reptype", "gz.msgs.Scene",
            "--timeout", "10000", "--req", "",
        )  # fmt: skip
        names = re.findall(r'^model \{\n  name: "([^"]+)"', out, flags=re.MULTILINE)
        return sorted(n for n in names if OURS.match(n))


def _pose_msg(prop: Prop) -> str:
    p = prop.pose
    return (
        f'{{name: "{prop.name}", position: {{x: {p.x:.3f}, y: {p.y:.3f}, z: {p.z:.3f}}}, '
        f"orientation: {{w: {math.cos(p.yaw / 2):.5f}, z: {math.sin(p.yaw / 2):.5f}}}}}"
    )
