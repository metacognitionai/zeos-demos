"""The PX4 drone's contract, against a stand-in flight controller.

No PX4 and no network: ``Steerable`` records what it was told and reports whatever
telemetry the test sets, so what is under test is the farm's side -- that a flight is
sent to the right coordinates, answered only once the drone is there, cut short where
it is, and that a refusal from the flight controller is not swallowed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from zeos.core.events import PipeWritten
from zeos.core.ids import PipeName

from zeos_farm_drone import geo
from zeos_farm_drone.camera import Camera
from zeos_farm_drone.px4 import Px4Farm, Telemetry
from zeos_farm_drone.run import Farm
from zeos_farm_drone.scene import xy
from zeos_farm_drone.sim import CRUISE_M

S = 1_000_000_000
DAWN = PipeName("clock.dawn")


@dataclass
class Steerable:
    """A flight controller whose telemetry the test sets."""

    now: Telemetry = field(
        default_factory=lambda: Telemetry(geo.ORIGIN.lat, geo.ORIGIN.lon, 0.0, in_air=False)
    )
    told: list[tuple[str, object]] = field(default_factory=list[tuple[str, object]])
    refuse: Exception | None = None

    def telemetry(self) -> Telemetry:
        return self.now

    def fly_to(self, fix: geo.Fix, above_m: float) -> None:
        self.told.append(("fly_to", (fix, above_m)))

    def climb_to(self, above_m: float) -> None:
        self.told.append(("climb_to", above_m))

    def hold(self) -> None:
        self.told.append(("hold", None))

    def return_home(self) -> None:
        self.told.append(("return_home", None))

    def aim(self, fix: geo.Fix) -> None:
        self.told.append(("aim", fix))

    def look_ahead(self) -> None:
        self.told.append(("look_ahead", None))

    def park(self) -> None:
        self.told.append(("park", None))
        self.over("shed", 0)

    def check(self) -> None:
        if self.refuse is not None:
            raise self.refuse

    def close(self) -> None:
        self.told.append(("close", None))

    def over(self, place: str, above_m: float) -> None:
        fix = geo.at(place, above_m)
        self.now = Telemetry(fix.lat, fix.lon, above_m, in_air=above_m > 0)


class Script:
    """Answers the patrol with fixed commands, so one flight is all that happens."""

    def __init__(self, *commands: str) -> None:
        self.commands = list(commands)

    def next_command(self, turn: object) -> str:
        return self.commands.pop(0) if self.commands else "read stdin;"


def farm_with(link: Steerable, *commands: str, camera: Camera | None = None) -> Farm:
    farm = Farm(source=Script(*commands), link=link, camera=camera)
    assert isinstance(farm.sim, Px4Farm)
    farm.start()
    return farm


def answers(farm: Farm) -> list[str]:
    return [
        " ".join(e.text)
        for e in farm.sink
        if isinstance(e, PipeWritten) and e.pipe == "drone.done.patrol"
    ]


def run_until(farm: Farm, start_s: float, until: Callable[[], bool], seconds: float = 30) -> float:
    t = start_s
    while t < start_s + seconds:
        farm.step(int(t * S), [])
        if until():
            return t
        t += 0.05
    raise AssertionError("never happened")


def test_the_map_is_metres_from_the_shed_near_hay() -> None:
    assert geo.offset_m("shed") == (0.0, 0.0)
    north, east = geo.offset_m("house")
    assert (round(north), round(east)) == (90, 0)  # 0.6 map units north of the shed
    assert round(geo.offset_m("dam")[1]) == 300  # two map units east
    assert -35 < geo.ORIGIN.lat < -34 and 144 < geo.ORIGIN.lon < 145
    fix = geo.at("house", 40)
    assert geo.nearest(fix.lat, fix.lon) == "house"
    assert fix.alt_m == geo.ORIGIN.alt_m + 40


def test_a_flight_is_answered_only_once_px4_says_the_drone_is_there() -> None:
    link = Steerable()
    farm = farm_with(link, "write stdout fly house;", "read stdin;")
    farm.kernel.deliver(DAWN, "dawn")
    run_until(farm, 0, lambda: bool(link.told))
    kind, told = link.told[0]
    assert kind == "fly_to"
    fix, above = told  # type: ignore[misc]
    assert isinstance(fix, geo.Fix) and above == CRUISE_M
    assert geo.nearest(fix.lat, fix.lon) == "house"

    # Half way, and still no answer: the map's own timing plays no part.
    link.over("shed", 20)
    for t in range(1, 40):
        farm.step(t * S, [])
    assert answers(farm) == []
    link.over("house", CRUISE_M)
    run_until(farm, 40, lambda: bool(answers(farm)))
    assert answers(farm) == ["done fly house"]
    assert farm.sim.drones["kestrel"].position == "house"


def test_a_flight_cut_short_holds_and_says_where_it_stopped() -> None:
    link = Steerable()
    farm = farm_with(link, "write stdout fly back-block;", "read stdin;")
    farm.kernel.deliver(DAWN, "dawn")
    run_until(farm, 0, lambda: bool(link.told))
    link.over("ridge", CRUISE_M)
    drone = farm.sim.drones["kestrel"]
    assert drone.current is not None
    farm.sim._abort(drone, drone.current, 5 * S)  # pyright: ignore[reportPrivateUsage]
    assert ("hold", None) in link.told
    assert answers(farm)[-1] == "interrupted fly back-block over ridge"


def test_a_refusal_from_px4_is_not_swallowed() -> None:
    link = Steerable(refuse=RuntimeError("COMMAND_DENIED: arming"))
    farm = farm_with(link, "write stdout fly house;", "read stdin;")
    farm.kernel.deliver(DAWN, "dawn")
    with pytest.raises(RuntimeError, match="COMMAND_DENIED"):
        run_until(farm, 0, lambda: False, seconds=5)


def test_px4_sets_the_pace() -> None:
    assert Px4Farm.realtime and not Farm().sim.realtime


def test_the_page_shows_the_altitude_px4_reports() -> None:
    """Between answers the map's drone keeps the altitude of its last action; a real
    one is somewhere on the way, and the page shows where."""
    link = Steerable()
    farm = farm_with(link)
    link.over("shed", 23.4)
    assert farm.sim.altitude_now(farm.sim.drones["kestrel"]) == 23


def test_a_look_points_the_gimbal_and_its_photo_is_a_frame(tmp_path: Path) -> None:
    """Before the zoom, PX4 is asked to point the gimbal at the herd; the photo the
    answer cites is a frame the camera kept under the same id."""
    link = Steerable()
    taken: list[str] = []

    def snap(topic: str, path: Path) -> None:
        taken.append(path.name)
        path.write_bytes(b"frame")

    camera = Camera("/cam", tmp_path, every_s=3600, snap=snap)
    farm = farm_with(link, "write stdout fly house;", "write stdout zoom cattle;", camera=camera)
    farm.kernel.deliver(DAWN, "dawn")
    run_until(farm, 0, lambda: bool(link.told))
    link.over("house", CRUISE_M)
    t = run_until(farm, 1, lambda: any(kind == "aim" for kind, _ in link.told))
    kinds = [kind for kind, _ in link.told]
    assert kinds[:2] == ["look_ahead", "fly_to"]  # flying, the camera looks ahead
    aimed = next(fix for kind, fix in link.told if kind == "aim")
    assert isinstance(aimed, geo.Fix)
    x, y = xy("house")
    north, east = geo.offset_m("house")
    assert aimed.lat > geo.at("house", 0).lat  # the herd grazes north of the middle
    assert (x, y) == (east, north)
    run_until(farm, t, lambda: any("photo=" in a for a in answers(farm)))
    camera.close()
    photo = answers(farm)[-1].split("photo=")[1]
    assert (tmp_path / f"{photo}.png").read_bytes() == b"frame"


def test_without_a_camera_no_gimbal_is_asked_for() -> None:
    link = Steerable()
    farm = farm_with(link, "write stdout fly house;", "write stdout zoom cattle;")
    farm.kernel.deliver(DAWN, "dawn")
    run_until(farm, 0, lambda: bool(link.told))
    link.over("house", CRUISE_M)
    run_until(farm, 1, lambda: any("photo=" in a for a in answers(farm)))
    assert all(kind not in ("aim", "look_ahead") for kind, _ in link.told)


def test_a_camera_that_fails_between_photos_stops_the_farm(tmp_path: Path) -> None:
    def broken(topic: str, path: Path) -> None:
        raise RuntimeError(f"no frame from {topic}")

    camera = Camera("/cam", tmp_path, every_s=3600, patience_s=0, snap=broken)
    camera._thread.join(10)  # pyright: ignore[reportPrivateUsage] -- its first frame failed
    farm = farm_with(Steerable(), camera=camera)
    with pytest.raises(RuntimeError, match="no frame from /cam"):
        farm.step(S, [])


def test_bearings_are_compass_bearings() -> None:
    shed = geo.at("shed", 0)
    assert round(geo.bearing_deg(shed.lat, shed.lon, geo.at("house", 0))) == 0  # north
    assert (
        round(geo.bearing_deg(shed.lat, shed.lon, geo.at("dam", 0))) == 73
    )  # east, a little north
    house = geo.at("house", 0)
    assert round(geo.bearing_deg(house.lat, house.lon, geo.at("shed", 0))) == 180
    assert (
        round(geo.bearing_deg(house.lat, house.lon, geo.at("road", 0))) == 290
    )  # west, a little north
    assert round(geo.metres_to(shed.lat, shed.lon, house)) == 90


def test_a_new_morning_starts_with_the_drone_home() -> None:
    """Restarting the morning while the drone is out over the farm: it is brought home
    before the new morning's farm is built, so the map and the airframe agree."""
    from zeos_farm_drone.live import Live
    from zeos_farm_drone.policies import RulesSource

    link = Steerable()
    link.over("creek", CRUISE_M)
    live = Live(source=RulesSource, link=link)
    live.start()
    assert ("park", None) in link.told
    drone = live.farm.sim.drones["kestrel"]
    assert (drone.position, drone.altitude) == ("shed", 0)
