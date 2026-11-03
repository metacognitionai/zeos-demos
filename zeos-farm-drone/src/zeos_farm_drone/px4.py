"""The drone flown by PX4: the farm's device, with a flight controller in place of the map.

``FarmSim`` carries out a flight by letting the map's time pass. ``Px4Farm`` hands each
flight, climb and return to PX4 over MAVLink -- PX4 SITL in Gazebo here, a real airframe
later -- and calls it done when PX4's own telemetry says the drone is there. Everything
else is ``FarmSim``'s: what is on the farm, what an observation sees, refusals, answers
and reports. A camera action takes its time over a place the drone has really reached.

The kernel's thread never waits on MAVLink. ``Px4Link`` runs MAVSDK's event loop on a
thread of its own; the farm starts a motion and then reads the latest telemetry each
step. A motion PX4 rejects raises on the next read, rather than leaving a behaviour
waiting for an answer that will not come.
"""

from __future__ import annotations

import asyncio
import ctypes
import math
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from zeos_farm_drone import geo
from zeos_farm_drone.scene import subject
from zeos_farm_drone.sim import CRUISE_M, MOTIONS, FarmSim, _Action, _Drone

if TYPE_CHECKING:
    from zeos_farm_drone.camera import Camera
    from zeos_farm_drone.scene import Scene

__all__ = ["ADDRESS", "Link", "Px4Farm", "Px4Link", "Telemetry"]

#: Where PX4 SITL's offboard MAVLink stream arrives.
ADDRESS = "udpin://0.0.0.0:14540"
#: How close counts as there: PX4 holds a waypoint to within a metre or two.
ARRIVED_M = 3.0
LEVEL_M = 1.5
#: A motion's answer waits for PX4 this long before the farm says it failed.
MOTION_TIMEOUT_S = 240.0
#: How long bringing the drone home before a morning may take.
PARK_TIMEOUT_S = 300.0
#: How long MAVSDK waits for PX4 to acknowledge a command. Its default, half a second,
#: is a real airframe's; PX4 SITL runs in lockstep with Gazebo's clock and stops when
#: Gazebo does (loading models, say), so an acknowledgement can arrive seconds late.
COMMAND_TIMEOUT_S = 3.0
#: A goto's yaw that leaves the heading as it is: MAVLink's DO_REPOSITION reads NaN so.
KEEP_HEADING = math.nan
#: Where the gimbal looks while the drone flies: ahead, and down at the ground.
LOOK_AHEAD_PITCH_DEG = -45.0


@dataclass(frozen=True, slots=True)
class Telemetry:
    """The drone as PX4 last reported it."""

    lat: float
    lon: float
    above_m: float
    in_air: bool


class Link(Protocol):
    """What the farm needs of a flight controller. ``Px4Link`` is PX4 over MAVSDK."""

    def telemetry(self) -> Telemetry: ...
    def fly_to(self, fix: geo.Fix, above_m: float) -> None: ...
    def climb_to(self, above_m: float) -> None: ...
    def hold(self) -> None: ...
    def return_home(self) -> None: ...
    def aim(self, fix: geo.Fix) -> None: ...
    def look_ahead(self) -> None: ...
    def park(self) -> None: ...
    def check(self) -> None: ...
    def close(self) -> None: ...


def _number(value: float | None, what: str) -> float:
    """A telemetry field MAVSDK 4.x types as optional, which PX4 always fills."""
    if value is None:
        raise ValueError(f"PX4 sent no {what}")
    return value


def _set_timeout(mavsdk: Any, seconds: float) -> None:
    """MAVSDK's command timeout. The C library has ``mavsdk_set_timeout_s``; the Python
    binding (4.x) does not expose it, so it is called on the binding's own handle."""
    core = mavsdk._mavsdk
    setter = core._lib.mavsdk_set_timeout_s
    setter.argtypes = [ctypes.c_void_p, ctypes.c_double]
    setter.restype = None
    setter(core._handle, seconds)


class Px4Link:
    """PX4 over MAVSDK, on a thread of its own."""

    def __init__(self, address: str = ADDRESS, *, connect_timeout_s: float = 120.0) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="px4", daemon=True)
        self._thread.start()
        self._lock = threading.Lock()
        self._latest: Telemetry | None = None
        self._pending: list[Future[None]] = []
        self._home_alt_m = geo.ORIGIN.alt_m
        self._gimbal: int | None = None
        self._heading_deg = 0.0
        self._submit(self._connect(address, connect_timeout_s)).result(connect_timeout_s + 10)

    # -- the MAVSDK side -------------------------------------------------------

    async def _connect(self, address: str, timeout_s: float) -> None:
        from mavsdk.asyncio import ComponentType, Configuration, Mavsdk
        from mavsdk.asyncio.plugins.action import ActionAsync
        from mavsdk.asyncio.plugins.telemetry import TelemetryAsync

        self._mavsdk = Mavsdk(
            Configuration.create_with_component_type(ComponentType.GROUND_STATION)
        )
        _set_timeout(self._mavsdk, COMMAND_TIMEOUT_S)
        await self._mavsdk.add_any_connection(address)
        system = await self._mavsdk.first_autopilot(timeout_s)
        if system is None:
            raise ConnectionError(f"no autopilot answered on {address} within {timeout_s:.0f} s")
        self._system = system
        self._telemetry = TelemetryAsync(system)
        self._action = ActionAsync(system)
        self._in_air = False
        self._following = [
            self._loop.create_task(self._follow_in_air()),
            self._loop.create_task(self._follow_position()),
            self._loop.create_task(self._follow_heading()),
        ]

        async def ready() -> None:
            async for health in self._telemetry.subscribe_health():
                if health.is_global_position_ok and health.is_home_position_ok:
                    return

        await asyncio.wait_for(ready(), timeout_s)
        home = await self._telemetry.home()
        self._home_alt_m = _number(home.absolute_altitude_m, "home altitude")
        while self._latest is None:
            await asyncio.sleep(0.05)

    async def _follow_position(self) -> None:
        async for p in self._telemetry.subscribe_position():
            with self._lock:
                self._latest = Telemetry(
                    lat=_number(p.latitude_deg, "latitude"),
                    lon=_number(p.longitude_deg, "longitude"),
                    above_m=_number(p.relative_altitude_m, "relative altitude"),
                    in_air=self._in_air,
                )

    async def _follow_heading(self) -> None:
        async for heading in self._telemetry.subscribe_heading():
            self._heading_deg = _number(heading.heading_deg, "heading")

    async def _follow_in_air(self) -> None:
        async for in_air in self._telemetry.subscribe_in_air():
            self._in_air = in_air

    async def _airborne(self, above_m: float) -> None:
        """Arm and take off to ``above_m`` if the drone is on the ground.

        The takeoff is over when PX4 leaves its takeoff mode, not at some height: PX4
        may call it done a metre or two short of the altitude asked for. Arming moves
        home to where the drone stands, so home's altitude is read again."""
        from mavsdk.asyncio.plugins.telemetry import FlightMode

        if self._in_air:
            return
        await self._action.set_takeoff_altitude(max(above_m, 5.0))
        await self._action.arm()
        home = await self._telemetry.home()
        self._home_alt_m = _number(home.absolute_altitude_m, "home altitude")
        await self._action.takeoff()
        taking_off = False
        async for mode in self._telemetry.subscribe_flight_mode():
            if mode == FlightMode.TAKEOFF:
                taking_off = True
            elif taking_off:
                return

    async def _fly_to(self, fix: geo.Fix, above_m: float) -> None:
        """Fly to ``fix`` nose first, so a camera looking ahead looks where the drone
        goes. Over the place already, the heading stays as it is."""
        await self._airborne(above_m)
        here = self.telemetry()
        yaw = (
            geo.bearing_deg(here.lat, here.lon, fix)
            if geo.metres_to(here.lat, here.lon, fix) >= ARRIVED_M
            else KEEP_HEADING
        )
        await self._action.goto_location(fix.lat, fix.lon, self._home_alt_m + above_m, yaw)

    async def _climb_to(self, above_m: float) -> None:
        await self._airborne(above_m)
        here = self.telemetry()
        await self._action.goto_location(
            here.lat, here.lon, self._home_alt_m + above_m, KEEP_HEADING
        )

    async def _aim(self, fix: geo.Fix) -> None:
        """Point the gimbal at ``fix`` from where the drone hovers, by angles relative to
        its nose.

        Not a region of interest: given one, PX4 v1.17 SITL computes a yaw from north
        and the simulated gimbal turns that far from the nose, so the camera ends up
        off by the drone's heading (measured: aimed north at heading 111, it looked
        east-south-east). Angles relative to the nose land where they should."""
        from mavsdk.asyncio.plugins.gimbal import GimbalMode, SendMode

        here = self.telemetry()
        drop = self._home_alt_m + here.above_m - fix.alt_m
        bearing = geo.bearing_deg(here.lat, here.lon, fix)
        yaw = (bearing - self._heading_deg + 540) % 360 - 180
        pitch = -math.degrees(math.atan2(drop, geo.metres_to(here.lat, here.lon, fix)))
        await self._take_gimbal()
        await self._gimbal_api.set_angles(
            self._gimbal, 0.0, pitch, yaw, GimbalMode.YAW_FOLLOW, SendMode.ONCE
        )

    async def _look_ahead(self) -> None:
        """The gimbal looks ahead and down, the way the drone is flying."""
        from mavsdk.asyncio.plugins.gimbal import GimbalMode, SendMode

        await self._take_gimbal()
        await self._gimbal_api.set_angles(
            self._gimbal, 0.0, LOOK_AHEAD_PITCH_DEG, 0.0, GimbalMode.YAW_FOLLOW, SendMode.ONCE
        )

    async def _take_gimbal(self) -> None:
        """Be the gimbal's primary control before commanding it. Taken once is not
        enough: when PX4's navigator clears the region of interest, as a return to
        launch does, the gimbal manager hands primary control back to the autopilot
        (``input_mavlink.cpp``), and a command from anyone else is refused."""
        from mavsdk.asyncio.plugins.gimbal import ControlMode, GimbalAsync

        if self._gimbal is None:
            gimbal = GimbalAsync(self._system)
            async for listed in gimbal.subscribe_gimbal_list():
                if listed.gimbals:
                    self._gimbal_api = gimbal
                    self._gimbal = listed.gimbals[0].gimbal_id
                    break
        await self._gimbal_api.take_control(self._gimbal, ControlMode.PRIMARY)

    def _submit(self, coroutine: Coroutine[Any, Any, None]) -> Future[None]:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def _do(self, step: Callable[[], Awaitable[object]]) -> None:
        async def run() -> None:
            await step()

        self._pending.append(self._submit(run()))

    # -- the farm's side --------------------------------------------------------

    def telemetry(self) -> Telemetry:
        with self._lock:
            latest = self._latest
        assert latest is not None  # connected means a position has arrived
        return latest

    def fly_to(self, fix: geo.Fix, above_m: float) -> None:
        self._do(lambda: self._fly_to(fix, above_m))

    def climb_to(self, above_m: float) -> None:
        self._do(lambda: self._climb_to(above_m))

    def hold(self) -> None:
        self._do(lambda: self._action.hold())

    def return_home(self) -> None:
        self._do(lambda: self._action.return_to_launch())

    def aim(self, fix: geo.Fix) -> None:
        self._do(lambda: asyncio.wait_for(self._aim(fix), 30))

    def look_ahead(self) -> None:
        self._do(lambda: asyncio.wait_for(self._look_ahead(), 30))

    def park(self) -> None:
        """Bring the drone home to the shed and land it, and return once it is down.

        A morning starts with the drone on its pad. One that is still up when a new
        morning begins -- the last one restarted mid-flight, or the server restarted --
        is still carrying out the old morning's last command; PX4's return to launch
        replaces it, and lands at home, which is the shed.

        Whatever the old morning still has in hand is cancelled first: a takeoff waits
        for PX4 to leave its takeoff mode and then sends its flight, so one left running
        would be set off by the return itself and fly the drone away from home."""
        for step in self._pending:
            step.cancel()
        self._pending = []
        if not self.telemetry().in_air:
            return
        self.return_home()
        deadline = time.monotonic() + PARK_TIMEOUT_S
        while True:
            self.check()
            here = self.telemetry()
            if not here.in_air and geo.metres_between(here.lat, here.lon, "shed") < 5.0:
                return
            if time.monotonic() > deadline:
                raise TimeoutError(f"PX4 did not bring the drone home in {PARK_TIMEOUT_S:.0f} s")
            time.sleep(0.5)

    def close(self) -> None:
        """Stop following telemetry and stop MAVSDK's loop. The drone keeps doing what
        PX4 last told it: closing the link lands nothing."""

        async def stop() -> None:
            for task in self._following:
                task.cancel()
            await asyncio.gather(*self._following, return_exceptions=True)

        self._submit(stop()).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)

    def check(self) -> None:
        """Raise what PX4 refused, once the command that asked has finished."""
        done = [f for f in self._pending if f.done()]
        self._pending = [f for f in self._pending if not f.done()]
        for future in done:
            future.result()


@dataclass
class Px4Farm(FarmSim):
    """``FarmSim`` with every flight, climb and return flown by PX4."""

    link: Link | None = None
    #: Gazebo, kept showing what happens on the farm; None leaves its world as it is.
    scene: Scene | None = None
    #: The gimbal camera, which every look points and every photo is taken with.
    camera: Camera | None = None
    #: Where each motion in hand started, for its progress and its timeout.
    _from: dict[int, Telemetry] = field(default_factory=dict[int, Telemetry])

    #: The farm runs at the pace of the airframe: its clock cannot jump ahead.
    realtime = True
    #: A real airframe flies a morning about thirty times slower than the map's drone:
    #: a patrol is some seven minutes in the air, and coming home from the far paddocks
    #: two and a half. This leaves the alarm at 15% enough charge to land on.
    drain_pct_per_s = 0.08

    def __post_init__(self) -> None:
        super().__post_init__()
        here = self._link().telemetry()
        # A drone already out over the farm, say after a restart, is where PX4 says.
        for drone in self.drones.values():
            drone.position = geo.nearest(here.lat, here.lon) if here.in_air else "shed"
            drone.altitude = round(here.above_m) if here.in_air else 0

    def advance(self, now_ns: int) -> None:
        super().advance(now_ns)
        if self.camera is not None:
            # Between photos the camera keeps the page's frame; a failure there must
            # stop the farm now, not wait for the next photo to be noticed.
            self.camera.check()
        if self.scene is not None:
            self.scene.show(self, now_ns)

    def _link(self) -> Link:
        assert self.link is not None  # a Px4Farm is always built with its link
        return self.link

    def next_due_ns(self) -> int | None:
        """A motion is over when PX4 says, not at a time; only camera work and vehicles
        leaving have one."""
        dues = [
            d.current.due_ns
            for d in self.drones.values()
            if d.current is not None and d.current.command.partition(" ")[0] not in MOTIONS
        ]
        dues += [due for due, _, _ in self.leaving]
        return min(dues) if dues else None

    def _begin(self, drone: _Drone, action: _Action) -> None:
        link = self._link()
        self._from[id(action)] = link.telemetry()
        verb, _, arg = action.command.partition(" ")
        if verb not in MOTIONS:
            if self.camera is not None:
                east, north, up = subject(self, drone, action.command, action.started_ns)
                link.aim(geo.at_metres(north, east, up))
            return
        if self.camera is not None:
            link.look_ahead()
        match verb:
            case "fly":
                above = float(action.outcome.get("altitude", CRUISE_M))
                link.fly_to(geo.at(action.target, above), above)
            case "climb":
                link.climb_to(float(arg))
            case "return":
                link.return_home()

    def _finished(self, drone: _Drone, action: _Action, now_ns: int) -> bool:
        verb, _, arg = action.command.partition(" ")
        if verb not in MOTIONS:
            return super()._finished(drone, action, now_ns)
        link = self._link()
        link.check()
        here = link.telemetry()
        if (now_ns - action.started_ns) / 1e9 > MOTION_TIMEOUT_S:
            raise TimeoutError(f"PX4 did not finish `{action.command}` in {MOTION_TIMEOUT_S:.0f} s")
        match verb:
            case "fly":
                above = float(action.outcome.get("altitude", CRUISE_M))
                return (
                    geo.metres_between(here.lat, here.lon, action.target) < ARRIVED_M
                    and abs(here.above_m - above) < LEVEL_M
                )
            case "climb":
                return abs(here.above_m - float(arg)) < LEVEL_M
            case _:  # return: home and on the ground
                return not here.in_air and geo.metres_between(here.lat, here.lon, "shed") < 5.0

    def _photo(self, drone: _Drone, subject: str) -> str:
        photo = super()._photo(drone, subject)
        if self.camera is not None:
            self.camera.snap(photo)
        return photo

    def _halt(self, drone: _Drone, action: _Action, now_ns: int) -> str:
        link = self._link()
        if action.command.partition(" ")[0] in MOTIONS:
            link.hold()
        here = link.telemetry()
        return geo.nearest(here.lat, here.lon)

    def altitude_now(self, drone: _Drone) -> int:
        return round(self._link().telemetry().above_m)

    def progress(self, action: _Action, now_ns: int) -> float:
        verb, _, arg = action.command.partition(" ")
        start = self._from.get(id(action))
        if verb not in MOTIONS or start is None:
            return super().progress(action, now_ns)
        here = self._link().telemetry()
        if verb == "climb":
            span = abs(float(arg) - start.above_m) or 1.0
            return min(max(abs(here.above_m - start.above_m) / span, 0.0), 1.0)
        total = geo.metres_between(start.lat, start.lon, action.target) or 1.0
        left = geo.metres_between(here.lat, here.lon, action.target)
        return min(max(1.0 - left / total, 0.0), 1.0)
