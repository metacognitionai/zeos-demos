"""``zeos-stretch``: lint the case, run the morning, or serve it live in a browser page."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from zeos.descriptor.loader import load_case

from zeos_stretch.run import CASE, aliases_of, run
from zeos_stretch.story import check

__all__ = ["main"]

ClaudeSourceFactory = Callable[[], Any]


def _lint(argv: list[str]) -> int:
    from zeos.descriptor.lint import Severity, lint

    parser = argparse.ArgumentParser(
        prog="zeos-stretch lint", description="check the case tree without running it"
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


def _claude(case: Path, model: str | None) -> tuple[ClaudeSourceFactory, str]:
    """A factory for the Claude seat, and the model it will ask."""
    from zeos_stretch.claude import DEFAULT_MODEL, ClaudeSource, check_credentials
    from zeos_stretch.config import load_env
    from zeos_stretch.config import model as configured

    load_env()
    model = configured(model, DEFAULT_MODEL)
    problem = check_credentials()
    if problem is not None:
        raise SystemExit(f"zeos-stretch: {problem}")
    aliases = aliases_of(load_case(case).descriptors)

    def make() -> ClaudeSource:
        return ClaudeSource(aliases=aliases, model=model)

    return make, model


RUNS = CASE.parents[1] / "runs"


def _recording(path: Path) -> Path:
    """A recording directory, or the newest one under ``runs/`` for ``latest``."""
    if str(path) != "latest":
        return path
    found = sorted(RUNS.glob("*/meta.json"), key=lambda m: m.stat().st_mtime)
    if not found:
        raise SystemExit(f"zeos-stretch: there are no recordings under {RUNS} yet")
    return found[-1].parent


def _tape(case: Path) -> ClaudeSourceFactory:
    from zeos.machine.seat import TapeSource

    scripts = load_case(case).scripts
    return lambda: TapeSource(scripts)


def _serve(argv: list[str]) -> int:
    from datetime import datetime

    from zeos_stretch.live import Live, serve
    from zeos_stretch.recording import Recording

    parser = argparse.ArgumentParser(
        prog="zeos-stretch serve",
        description="run the atrium live in a browser page, and drive it from there",
    )
    parser.add_argument("--case", type=Path, default=CASE)
    parser.add_argument(
        "--machine",
        choices=("scripted", "claude", "tape"),
        default="scripted",
        help="scripted replays the case's own morning; claude lets you drive it; tape takes "
        "your clicks but answers from the descriptors' tapes, which only fit the case's "
        "own timing",
    )
    parser.add_argument("--model", default=None, help="the Claude model, with --machine claude")
    parser.add_argument("--speed", type=float, default=1.0, help="atrium seconds per real second")
    parser.add_argument("--port", type=int, default=8765)
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
        interactive = args.machine in ("claude", "tape")
        record = None
        if interactive and not args.no_record:
            record = args.record or RUNS / datetime.now().strftime("%Y%m%d-%H%M%S")
        model = None
        source = None
        if args.machine == "claude":
            source, model = _claude(args.case, args.model)
        elif args.machine == "tape":
            source = _tape(args.case)
        live = Live(
            source=source,
            case=args.case,
            speed=args.speed,
            playback=not interactive,
            record=record,
            meta={
                "machine": args.machine,
                "model": model,
                "speed": args.speed,
                "started": datetime.now().isoformat(timespec="seconds"),
            },
        )
    serve(live, port=args.port, open_browser=not args.no_open)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["lint"]:
        return _lint(argv[1:])
    if argv[:1] == ["serve"]:
        return _serve(argv[1:])
    if argv[:1] == ["run"]:
        argv = argv[1:]
    parser = argparse.ArgumentParser(
        prog="zeos-stretch run",
        description="run the morning to its end and print what the kernel and the body did",
        epilog="`zeos-stretch lint` checks the case; `zeos-stretch serve` runs it live in a page",
    )
    parser.add_argument("--case", type=Path, default=CASE)
    parser.add_argument("--events", type=Path, default=None, help="defaults to the case's own")
    parser.add_argument("--journal", type=Path, default=None, help="where to write the journal")
    parser.add_argument(
        "--machine",
        choices=("scripted", "claude"),
        default="scripted",
        help="who answers each behaviour's turn: its tape, or the Claude API",
    )
    parser.add_argument("--model", default=None, help="the Claude model, with --machine claude")
    parser.add_argument(
        "--commands", action="store_true", help="print every command as the seat parsed it"
    )
    parser.add_argument(
        "--replay",
        type=Path,
        default=None,
        help="replay a recorded session and check its journal: a directory, or latest",
    )
    args = parser.parse_args(argv)

    if args.replay is not None:
        from zeos_stretch.recording import Recording, replay

        result, same = replay(Recording.load(_recording(args.replay)), echo=True)
        print(f"\n{len(result.events)} journal events")
        print(
            "the journal is byte for byte the recorded one"
            if same
            else "THE JOURNAL DIFFERS FROM THE RECORDED ONE"
        )
        print()
        for verdict in check(result):
            print(f"  {'\u2713' if verdict.held else '\u2717'} {verdict.claim}")
        return 0 if same else 1

    source = _claude(args.case, args.model)[0]() if args.machine == "claude" else None
    result = run(
        args.case,
        events=args.events,
        journal=args.journal,
        source=source,
        echo=True,
        show_commands=args.commands,
    )
    print(f"\n{len(result.events)} journal events")
    if args.journal:
        print(f"journal written to {args.journal}")
    print()
    verdicts = check(result)
    for verdict in verdicts:
        print(f"  {'\u2713' if verdict.held else '\u2717'} {verdict.claim}")
    return 0 if all(v.held for v in verdicts) else 1
