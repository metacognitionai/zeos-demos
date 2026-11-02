"""The farm as Gazebo is shown it, against a stand-in for the gz command line.

``props`` is checked on the map's own farm (no PX4 is needed to say what the world
should hold); ``Scene.apply`` is checked against ``Gz``, which records each service
call and answers as gz-sim does.
"""

from __future__ import annotations

import re

import pytest

from zeos_farm_drone.farm import S
from zeos_farm_drone.run import Farm
from zeos_farm_drone.scene import CREATE_BATCH, Scene, props, xy
from zeos_farm_drone.sim import LEAVING_S


class Gz:
    """The gz command line, with a world that holds what it was told to."""

    def __init__(self, *, already: tuple[str, ...] = (), refuse: str = "") -> None:
        self.calls: list[tuple[str, str]] = []
        self.models = set(already)
        self.refuse = refuse

    def __call__(self, *args: str) -> str:
        service = args[args.index("-s") + 1].rsplit("/", 1)[-1]
        req = args[args.index("--req") + 1]
        if service == "info":
            return "".join(f'model {{\n  name: "{m}"\n  id: 9\n}}\n' for m in sorted(self.models))
        self.calls.append((service, req))
        if service == self.refuse:
            return "data: false\n"
        if service == "create_multiple":
            self.models |= set(re.findall(r'<model name="([^"]+)"', req))
        if service == "remove":
            self.models.discard(re.findall(r'name: "([^"]+)"', req)[0])
        return "data: true\n"


def shown(farm: Farm, gz: Gz, now_ns: int = 0) -> Scene:
    scene = Scene(run=gz)
    scene.apply(props(farm.sim, now_ns))
    scene.close()
    return scene


def test_every_head_of_cattle_is_in_the_world() -> None:
    wanted = props(Farm().sim, 0)
    cows = [n for n in wanted if n.startswith("cow-back-block-")]
    calves = [n for n in wanted if n.startswith("calf-back-block-")]
    assert (len(cows), len(calves)) == (22, 2)  # 24 head, two of them born overnight
    assert sum(n.startswith(("cow-", "calf-")) for n in wanted) == 149


def test_a_stray_leaves_its_herd_and_stands_apart_in_the_other_paddock() -> None:
    farm = Farm()
    before = props(farm.sim, 0)
    farm.sim.stray(0, "ridge", "creek")
    after = props(farm.sim, 0)
    assert set(before) - set(after) == {"cow-ridge-24"}
    assert set(after) - set(before) == {"stray-creek-0"}
    x, y = xy("creek")
    pose = after["stray-creek-0"].pose
    assert abs(pose.x - x) < 68 and abs(pose.y - y) < 68  # inside the creek's fences


def test_a_trough_running_dry_is_new_water_and_a_puddle() -> None:
    farm = Farm()
    farm.sim.trough_fault(0, "dam", 10)
    wanted = props(farm.sim, 0)
    assert "water-dam-10" in wanted and "water-dam-70" not in wanted
    assert "leak-dam" in wanted
    assert "leak-house" not in wanted  # 90% full


def test_a_vehicle_drives_off_towards_the_road() -> None:
    farm = Farm()
    farm.sim.vehicle_enters(0, "creek", "white-ute", "XYZ123")
    parked = props(farm.sim, 0)["vehicle-0-white-ute"].pose
    farm.sim.vehicle_leaves(10 * S, "road")
    half = props(farm.sim, 10 * S + LEAVING_S * S // 2)["vehicle-0-white-ute"].pose
    farm.sim.advance(11 * S + LEAVING_S * S)
    gone = props(farm.sim, 11 * S + LEAVING_S * S)["vehicle-0-white-ute"].pose
    assert gone.x < half.x < parked.x  # the road is west of the farm
    assert gone.x == pytest.approx(xy("road")[0] + 10)


def test_smoke_rises_off_the_farm_in_its_direction() -> None:
    farm = Farm()
    assert not any(n.startswith("smoke-") for n in props(farm.sim, 0))
    farm.sim.start_fire(0, "north-west", 12)
    smoke = props(farm.sim, 0)["smoke-north-west"].pose
    assert smoke.x < xy("house")[0] - 300 and smoke.y > xy("north-flat")[1] + 300


def test_gazebo_is_told_only_what_changed() -> None:
    gz = Gz()
    farm = Farm()
    scene = Scene(run=gz)
    scene.apply(props(farm.sim, 0))
    # Each made where it stands, a few at a time: Gazebo's clock, and PX4 with it,
    # stops while a batch loads.
    assert {s for s, _ in gz.calls} == {"create_multiple"}
    sizes = [req.count("<model name=") for _, req in gz.calls]
    assert sum(sizes) == len(props(farm.sim, 0)) and max(sizes) <= CREATE_BATCH
    gz.calls.clear()
    scene.apply(props(farm.sim, 0))
    assert gz.calls == []

    farm.sim.trough_fault(0, "dam", 10)
    farm.sim.park(0, "motorbike", "ridge")
    scene.apply(props(farm.sim, 0))
    scene.close()
    assert [s for s, _ in gz.calls] == ["remove", "create_multiple", "set_pose_vector"]
    assert 'name: "water-dam-70"' in gz.calls[0][1]
    assert "water-dam-10" in gz.calls[1][1] and "leak-dam" in gz.calls[1][1]
    assert 'name: "item-motorbike"' in gz.calls[2][1]


def test_a_restart_takes_over_what_gazebo_already_shows() -> None:
    gz = Gz()
    shown(Farm(), gz)
    gz.calls.clear()
    farm = Farm()
    farm.sim.stray(0, "ridge", "creek")
    shown(farm, gz)
    assert [s for s, _ in gz.calls] == ["remove", "create_multiple", "set_pose_vector"]
    assert 'name: "cow-ridge-24"' in gz.calls[0][1]
    assert {n for n in gz.models if n.startswith("stray-")} == {"stray-creek-0"}


def test_what_gazebo_refuses_reaches_the_kernels_thread() -> None:
    farm = Farm()
    scene = Scene(run=Gz(refuse="create_multiple"))
    scene.show(farm.sim, 0)
    scene._thread.join(10)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RuntimeError, match="Gazebo refused create_multiple"):
        scene.show(farm.sim, 0)
