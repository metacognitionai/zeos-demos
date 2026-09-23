"""``zeos-blocks``: lint the case, run it, serve it, or generate a bigger one."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from zeos.core.events import CompilationRefused, Event, FaultRaised, JobSpawned, WorldWritten
from zeos.descriptor.lint import Severity, lint
from zeos.descriptor.loader import load_case
from zeos.machine.seat import CommandSource, seat_maps

from zeos_blocks import config
from zeos_blocks.abi import BLOCKS
from zeos_blocks.generate import write_case
from zeos_blocks.schedule import Entry, due, load_schedule
from zeos_blocks.session import Session
from zeos_blocks.world import Table

__all__ = ["main"]

DEFAULT_CASE = "cases/blocks-3x4"
#: What `--planner claude` asks for unless ANTHROPIC_MODEL or --model says otherwise.
DEFAULT_MODEL = "claude-opus-5"


def _findings(case: Path) -> list:
    bundle = load_case(case)
    return list(
        lint(
            bundle.descriptors,
            pipes=bundle.pipes,
            scripts=bundle.scripts,
            vectors=bundle.vectors,
            resources=bundle.resources,
            platforms=bundle.platforms,
            principals=bundle.principals,
            gates=bundle.gates,
            # This case's own vocabulary, not the default. It is what the
            # `unknown-body-verb` and `unbound-body-pipe` rules read the bodies against,
            # and linting with the default would pass a body naming a verb this ABI does
            # not have.
            abi=BLOCKS,
        )
    )


def _planner(
    name: str,
    model: str | None = None,
    descriptors: dict[str, tuple[str, ...]] | None = None,
    max_tokens: int = 512,
) -> CommandSource:
    if name == "stub":
        from zeos_blocks.planner import StubPlanner

        return StubPlanner()

    # Said here rather than left to the SDK. Without a key the failure surfaces several
    # frames down as an authentication error, which reads like a broken demo rather than
    # an unconfigured one, and quietly falling back to the stub would be worse still: a
    # planner working things out in Python while you believed a model was.
    if not config.api_key_is_set():
        raise SystemExit(
            f"--planner claude needs {config.KEY}.\n"
            f"  put it in {config.ENV_FILENAME} beside the project, or export it,\n"
            f"  or drop the flag and use the stub planner, which needs neither."
        )
    from zeos_blocks.planner import ClaudePlanner

    return ClaudePlanner(
        model=config.model(model or DEFAULT_MODEL),
        descriptors=descriptors,
        max_tokens=max_tokens,
    )


def _cmd_lint(args: argparse.Namespace) -> int:
    findings = _findings(Path(args.case))
    for finding in findings:
        print(finding.render(), file=sys.stderr)
    errors = sum(1 for f in findings if f.severity is Severity.ERROR)
    warnings = len(findings) - errors
    print(f"{args.case}: {errors} error(s), {warnings} warning(s)")
    return 1 if errors else 0


def _describe(event: Event) -> str | None:
    """One journal event as a line, or None for the ones a person need not see."""
    match event:
        case JobSpawned():
            return f"  job {event.job} spawned: {event.descriptor} at priority {event.priority}"
        case WorldWritten():
            return f"  {event.obj}: {event.before} -> {event.after}"
        case FaultRaised():
            return f"  fault: {event.fault} -- {event.detail}"
        case CompilationRefused():
            return f"  refused: {event.reason}"
        case _:
            return None


def _cmd_run(args: argparse.Namespace) -> int:
    case = Path(args.case)
    blocking = [f for f in _findings(case) if f.severity is Severity.ERROR]
    if blocking and not args.force:
        for finding in blocking:
            print(finding.render(), file=sys.stderr)
        print("refusing to run a tree that does not lint (--force to override)", file=sys.stderr)
        return 1

    bundle = load_case(case)
    schedule = list(load_schedule(Path(args.events))) if args.events else []
    if args.say:
        # A tick or two in, so the kernel has started before anybody speaks.
        schedule.append(Entry(at=2, say=args.say))
        schedule.sort(key=lambda e: e.at)

    # A case that boots nothing and is told nothing will sit there, print the table it
    # started with, and stop. That is not a failure anybody can see, so it is said out
    # loud: it looked exactly like a broken model the first time it happened.
    if not bundle.boot and not schedule:
        print(
            f"{case} boots no jobs, and nothing was said to it, so nothing will happen.\n"
            f'  add --say "move the green block from stack 1 to stack 2"\n'
            f"  or --events {case}/events.jsonl",
            file=sys.stderr,
        )
        return 1

    descriptors, _valued = seat_maps(bundle.descriptors, bundle.pipes)
    session = Session(
        bundle,
        _planner(args.planner, args.model, descriptors, args.max_tokens),
        on_reply=lambda text: print(f"operator  <-- {text}", flush=True),
        on_command=(
            None if args.quiet else lambda who, line: print(f"{who:<9} {line}", flush=True)
        ),
        on_event=(
            None
            if not args.journal_lines
            else lambda events: [
                print(line, flush=True) for e in events if (line := _describe(e)) is not None
            ]
        ),
        journal=Path(args.journal) if args.journal else None,
        seed=args.seed,
    )

    print(f"{bundle.name}: {Table.of(session.positions()).render()}\n", flush=True)
    idle = 0
    for tick in range(args.max_ticks):
        for entry in due(schedule, tick):
            if entry.say is not None:
                print(f"operator  --> {entry.say}", flush=True)
                session.say(entry.say)
            else:
                assert entry.hand is not None
                # One line, because a hand is an instant: the world changes and the
                # doorbell rings in the same call. A drag on the page is a span only
                # because a person takes time, and `Session.hold` covers that.
                print(f"  ~~ a hand: {session.disturb(*entry.hand)}", flush=True)
        ran = session.step()
        pending = ran or session.busy or any(True for _ in due(schedule, tick))
        idle = 0 if pending else idle + 1
        if idle >= 3 and tick > (max((e.at for e in schedule), default=0)):
            break

    print(f"\n{Table.of(session.positions()).render()}")
    session.close()
    if args.journal:
        print(f"journal written to {args.journal}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from zeos_blocks.web.server import serve

    bundle = load_case(Path(args.case))
    descriptors, _valued = seat_maps(bundle.descriptors, bundle.pipes)
    return serve(
        Path(args.case),
        _planner(args.planner, args.model, descriptors, args.max_tokens),
        host=args.host,
        port=args.port,
        open_browser=args.open,
        # A model on the end of an API call paces the run by taking a second to answer.
        # Adding to that would only make a slow run slower, so the pause between ordinary
        # steps is for the planner that answers instantly. The pause for a move being
        # drawn is not: it is measured from the top of the step, so a slow planner has
        # already spent it and waits no longer.
        pace=0.0 if args.planner == "claude" else args.pace / 1000,
        settle=args.settle / 1000,
        journal=Path(args.journal) if args.journal else None,
    )


def _cmd_new(args: argparse.Namespace) -> int:
    target = Path(args.into or f"cases/blocks-{args.positions}x{args.blocks}")
    write_case(target, positions=args.positions, blocks=args.blocks, tidy=args.tidy)
    print(f"wrote {target}: {args.positions} positions, {args.blocks} blocks")
    # `serve`, not `run`: a generated case boots nothing and ships no event file, so `run`
    # would print the opening table and sit there with nothing to do.
    how = "serve" if not args.tidy else "run"
    print(f"try: uv run zeos-blocks {how} --case {target}" + ("" if args.tidy else " --open"))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="zeos-blocks", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_lint = sub.add_parser("lint", help="typecheck the case against this ABI, without running")
    p_lint.add_argument("case", nargs="?", default=DEFAULT_CASE)
    p_lint.set_defaults(func=_cmd_lint)

    p_run = sub.add_parser("run", help="run the case in the terminal")
    p_run.add_argument("--case", default=DEFAULT_CASE)
    p_run.add_argument("--events", default=None, help="a schedule of what the operator does")
    p_run.add_argument(
        "--say",
        default=None,
        help="one thing for the operator to say, instead of writing a schedule for it",
    )
    p_run.add_argument("--planner", choices=("stub", "claude"), default="stub")
    p_run.add_argument("--model", default=None, help=f"overrides {config.MODEL}")
    p_run.add_argument(
        "--max-tokens",
        type=int,
        default=512,
        help="room the model has to answer in; too little and it is cut off before the command",
    )
    p_run.add_argument("--journal", default=None, help="where to write the kernel's journal")
    p_run.add_argument(
        "--journal-lines",
        action="store_true",
        help="print the kernel's own record beside the run",
    )
    p_run.add_argument("--quiet", action="store_true", help="do not print each command")
    p_run.add_argument("--seed", type=int, default=0)
    p_run.add_argument("--max-ticks", type=int, default=600)
    p_run.add_argument("--force", action="store_true", help="run despite lint errors")
    p_run.set_defaults(func=_cmd_run)

    p_serve = sub.add_parser("serve", help="watch the table in a browser")
    p_serve.add_argument("--case", default=DEFAULT_CASE)
    p_serve.add_argument("--planner", choices=("stub", "claude"), default="stub")
    p_serve.add_argument("--model", default=None, help=f"overrides {config.MODEL}")
    p_serve.add_argument(
        "--max-tokens",
        type=int,
        default=512,
        help="room the model has to answer in; too little and it is cut off before the command",
    )
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8800)
    p_serve.add_argument("--open", action="store_true", help="open a browser at it")
    p_serve.add_argument(
        "--journal",
        default=None,
        help="write the kernel's journal on exit, to step through with `zeos debug`",
    )
    p_serve.add_argument(
        "--settle",
        type=float,
        default=800,
        help="milliseconds a move is given to be drawn before the next one starts "
        "(default 800); nothing runs while a block is in the air",
    )
    p_serve.add_argument(
        "--pace",
        type=float,
        default=200,
        help="milliseconds per token boundary for the stub planner, so a person can "
        "watch (default 200); ignored with --planner claude, which paces itself",
    )
    p_serve.set_defaults(func=_cmd_serve)

    p_new = sub.add_parser("new", help="generate a case for a bigger workspace")
    p_new.add_argument("--positions", type=int, default=5)
    p_new.add_argument("--blocks", type=int, default=10)
    p_new.add_argument("--into", default=None)
    p_new.add_argument(
        "--tidy",
        action="store_true",
        help="add the low-priority background goal, and boot it",
    )
    p_new.set_defaults(func=_cmd_new)

    args = parser.parse_args(argv)
    # Before anything reads the environment. A flag still wins, because a flag is read
    # from `args` and never from here.
    config.load_env()
    return int(args.func(args))
