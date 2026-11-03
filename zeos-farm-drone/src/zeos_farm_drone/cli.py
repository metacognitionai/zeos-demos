"""``zeos-farm-drone``: lint the case, run the morning, or serve it live in a browser page."""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from zeos.descriptor.loader import load_case
from zeos.machine.seat import CommandSource

from zeos_farm_drone.policies import RulesSource
from zeos_farm_drone.run import CASE, RunResult, aliases_of, run
from zeos_farm_drone.story import check

if TYPE_CHECKING:
    from zeos_farm_drone.camera import Camera
    from zeos_farm_drone.px4 import Link
    from zeos_farm_drone.scene import Scene

__all__ = ["main"]


def _lint(argv: list[str]) -> int:
    from zeos.descriptor.lint import Severity, lint

    parser = argparse.ArgumentParser(
        prog="zeos-farm-drone lint", description="check the case tree without running it"
    )
    parser.add_argument("case", nargs="?", type=Path, default=CASE)
    args = parser.parse_args(argv)
    bundle = load_case(args.case)
    findings = lint(
        bundle.descriptors,
        pipes=bundle.pipes,
        scripts=bundle.scripts,
        vectors=bundle.vectors,
        resources=bundle.resources,
        platforms=bundle.platforms,
        principals=bundle.principals,
        gates=bundle.gates,
    )
    for finding in findings:
        print(finding.render())
    errors = sum(1 for f in findings if f.severity is Severity.ERROR)
    print(
        f"{len(bundle.descriptors)} descriptors, {len(bundle.vectors)} vectors: "
        f"{errors} error(s), {len(findings) - errors} warning(s)"
    )
    return 1 if errors else 0


RUNS = CASE.parents[1] / "runs"


def _claude(case: Path, model: str | None) -> tuple[Callable[[], CommandSource], str]:
    """A factory for the Claude seat, and the model it will ask."""
    from zeos_farm_drone.claude import DEFAULT_MODEL, ClaudeSource, check_credentials
    from zeos_farm_drone.config import load_env
    from zeos_farm_drone.config import model as configured

    load_env()
    chosen = configured(model, DEFAULT_MODEL)
    problem = check_credentials()
    if problem is not None:
        raise SystemExit(f"zeos-farm-drone: {problem}")
    aliases = aliases_of(load_case(case).descriptors)

    def make() -> CommandSource:
        return ClaudeSource(aliases=aliases, model=chosen)

    return make, chosen


def _recording(path: Path) -> Path:
    """A recording directory, or the newest one under ``runs/`` for ``latest``."""
    if str(path) != "latest":
        return path
    found = sorted(RUNS.glob("*/meta.json"), key=lambda m: m.stat().st_mtime)
    if not found:
        raise SystemExit(f"zeos-farm-drone: there are no recordings under {RUNS} yet")
    return found[-1].parent


def _device_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--device",
        choices=("sim", "px4"),
        default="sim",
        help="what flies the drone: the map's own simulation (the default), or PX4 over "
        "MAVLink -- PX4 SITL in Gazebo, or a real airframe",
    )
    parser.add_argument(
        "--mavlink",
        default=None,
        help="where PX4's MAVLink stream arrives, with --device px4 "
        "(default udpin://0.0.0.0:14540, PX4 SITL's offboard port)",
    )
    parser.add_argument(
        "--gazebo",
        metavar="WORLD",
        default=None,
        help="with --device px4 in Gazebo: show what happens on the farm (herds, vehicles, "
        "troughs, smoke) in this Gazebo world, e.g. farm",
    )
    parser.add_argument(
        "--camera",
        metavar="MODEL",
        default=None,
        help="with --gazebo: take every photo with the gimbal camera of this Gazebo model "
        "(PX4's gz_x500_gimbal is x500_gimbal_0), pointed by PX4 at what each look is about",
    )


def _link(args: argparse.Namespace) -> Link | None:
    """Connect to the flight controller the options name, or None for the map's drone."""
    if args.device != "px4":
        return None
    from zeos_farm_drone.px4 import ADDRESS, Px4Link

    address = args.mavlink or ADDRESS
    print(f"connecting to PX4 on {address} ...", flush=True)
    link = Px4Link(address)
    print("PX4 connected; the farm runs at the airframe's pace", flush=True)
    return link


def _scene(args: argparse.Namespace) -> Scene | None:
    """Gazebo's world, kept showing the farm, if the options name one."""
    if args.gazebo is None:
        return None
    if args.device != "px4":
        raise SystemExit(
            "zeos-farm-drone: --gazebo shows the farm under PX4 SITL; add --device px4"
        )
    from zeos_farm_drone.scene import Scene

    return Scene(args.gazebo)


def _camera(args: argparse.Namespace, photos: Path) -> Camera | None:
    """The drone's camera in Gazebo, if the options name one."""
    if args.camera is None:
        return None
    if args.gazebo is None:
        raise SystemExit("zeos-farm-drone: --camera takes photos in Gazebo; add --gazebo WORLD")
    from zeos_farm_drone.camera import Camera, image_topic

    return Camera(image_topic(args.gazebo, args.camera), photos)


def _verdicts(result: RunResult) -> bool:
    print()
    print("The farmer's phone:")
    for alert in result.alerts:
        print(f"  {alert}")
    for reply in result.replies:
        print(f"  (reply) {reply}")
    print()
    verdicts = check(result)
    for verdict in verdicts:
        print(f"  {'\u2713' if verdict.held else '\u2717'} {verdict.claim}")
    return all(v.held for v in verdicts)


def _run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="zeos-farm-drone run",
        description="run the morning on the farm, as the kernel and the drones saw it",
        epilog="`zeos-farm-drone lint` checks the case; `zeos-farm-drone serve` runs it in a page",
    )
    parser.add_argument("--case", type=Path, default=CASE)
    parser.add_argument("--events", type=Path, default=None, help="the morning's schedule")
    parser.add_argument("--journal", type=Path, default=None, help="also write the journal")
    parser.add_argument(
        "--commands", action="store_true", help="show every command as it was parsed"
    )
    parser.add_argument(
        "--machine",
        choices=("rules", "claude", "tape"),
        default="rules",
        help="who chooses each command: a policy per behaviour (the default), the Claude "
        "API, or the descriptors' tapes, which are right only for the case's own schedule",
    )
    parser.add_argument("--model", default=None, help="the Claude model, with --machine claude")
    _device_options(parser)
    parser.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="replay a recorded session and check its journal: a directory, or latest",
    )
    args = parser.parse_args(argv)

    if args.replay is not None:
        from zeos_farm_drone.recording import Recording, replay

        result, same = replay(Recording.load(_recording(args.replay)), echo=True)
        print(f"\n{len(result.events)} journal events")
        print(
            "the journal is byte for byte the recorded one"
            if same
            else "THE JOURNAL DIFFERS FROM THE RECORDED ONE"
        )
        held = _verdicts(result)
        return 0 if same and held else 1

    source: CommandSource | None = None
    if args.machine == "rules":
        source = RulesSource()
    elif args.machine == "claude":
        source = _claude(args.case, args.model)[0]()
    link = _link(args)
    scene = _scene(args)
    camera = _camera(args, RUNS / datetime.now().strftime("%Y%m%d-%H%M%S") / "photos")
    try:
        result = run(
            args.case,
            events=args.events,
            journal=args.journal,
            source=source,
            echo=True,
            show_commands=args.commands,
            link=link,
            scene=scene,
            camera=camera,
        )
    finally:
        if camera is not None:
            camera.close()
        if scene is not None:
            scene.close()
        if link is not None:
            link.close()
    return 0 if _verdicts(result) else 1


def _serve(argv: list[str]) -> int:
    from zeos_farm_drone.live import Live, serve
    from zeos_farm_drone.recording import Recording

    parser = argparse.ArgumentParser(
        prog="zeos-farm-drone serve",
        description="run the farm live in a browser page, and drive it from there",
    )
    parser.add_argument("--case", type=Path, default=CASE)
    parser.add_argument(
        "--machine",
        choices=("rules", "claude", "tape"),
        default="rules",
        help="rules (the default) or claude let you drive the morning from the page; tape "
        "plays the case's own morning on the descriptors' tapes, and takes no clicks",
    )
    parser.add_argument("--model", default=None, help="the Claude model, with --machine claude")
    parser.add_argument("--speed", type=float, default=1.0, help="farm seconds per real second")
    _device_options(parser)
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="the address the page listens on; 0.0.0.0 inside a container whose port is "
        "published to the host's loopback only",
    )
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-open", action="store_true", help="do not open a browser")
    parser.add_argument(
        "--record",
        type=Path,
        default=None,
        help=f"where to record a live session (default: a new directory under {RUNS})",
    )
    parser.add_argument("--no-record", action="store_true", help="do not record the session")
    parser.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="play a recorded session back: a directory, or latest",
    )
    args = parser.parse_args(argv)

    if args.replay is not None:
        recording = _recording(args.replay)
        print(f"replaying {recording}")
        live = Live(source=None, replay=Recording.load(recording), speed=args.speed)
    else:
        interactive = args.machine in ("rules", "claude")
        record = None
        if interactive and not args.no_record:
            record = args.record or RUNS / datetime.now().strftime("%Y%m%d-%H%M%S")
        model = None
        factory: Callable[[], CommandSource] | None = RulesSource if interactive else None
        if args.machine == "claude":
            factory, model = _claude(args.case, args.model)
        if args.device == "px4" and args.speed != 1.0:
            raise SystemExit(
                "zeos-farm-drone: with --device px4 the airframe sets the pace; drop --speed"
            )
        live = Live(
            source=factory,
            case=args.case,
            speed=args.speed,
            playback=not interactive,
            record=record,
            link=_link(args),
            scene=_scene(args),
            # Each morning files its photos where Live says; this is only the start.
            camera=_camera(args, Path(tempfile.mkdtemp(prefix="zeos-farm-photos-"))),
            meta={
                "machine": args.machine,
                "model": model,
                "device": args.device,
                "speed": args.speed,
                "started": datetime.now().isoformat(timespec="seconds"),
            },
        )
    serve(live, host=args.host, port=args.port, open_browser=not args.no_open)
    return 0


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["lint"]:
        raise SystemExit(_lint(args[1:]))
    if args[:1] == ["serve"]:
        raise SystemExit(_serve(args[1:]))
    if args[:1] == ["run"]:
        args = args[1:]
    raise SystemExit(_run(args))
