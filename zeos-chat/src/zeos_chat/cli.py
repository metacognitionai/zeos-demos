"""``zeos-chat``: lint and run the chat case.

Two commands, and the reason they exist rather than ``zeos lint`` and ``zeos run``
serving is the same in both cases: this case declares its own ABI, and neither of those
takes one. ``zeos lint`` would read the chat vocabulary in a descriptor body against the
default vocabulary and report every ``spawn`` as an unknown verb; ``zeos run --machine
seat`` would build a seat that cannot parse one.

What is printed is the journal's account, not the machine's: who wrote what, who parked
on which pipe, which vector fired, which fault landed. A transcript would show the same
run and prove nothing -- a structural claim is only checkable against structure.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Sequence
from pathlib import Path

from zeos.core.events import (
    Event,
    FaultRaised,
    JobBlocked,
    JobPreempted,
    JobResumed,
    JobSpawned,
    PipeWritten,
    VectorFired,
    VectorThrottled,
)
from zeos.core.ids import JobId, PipeName
from zeos.descriptor.lint import Severity, lint
from zeos.descriptor.loader import CaseBundle, load_case
from zeos.descriptor.schema import DescriptorError
from zeos.driver import load_schedule

from zeos_chat.abi import CHAT
from zeos_chat.build import MODELS, build_session
from zeos_chat.mail import MailAdapter

__all__ = ["main"]

CASE = Path(__file__).resolve().parents[2] / "cases" / "chat-scripted"


def findings(bundle: CaseBundle):
    """Every lint finding for this bundle, read against the chat ABI."""
    return lint(
        bundle.descriptors,
        pipes=bundle.pipes,
        scripts=bundle.scripts,
        vectors=bundle.vectors,
        resources=bundle.resources,
        platforms=bundle.platforms,
        principals=bundle.principals,
        gates=bundle.gates,
        abi=CHAT,
    )


def _cmd_lint(args: argparse.Namespace) -> int:
    bundle = load_case(Path(args.case))
    found = findings(bundle)
    for finding in found:
        print(finding.render())
    errors = sum(1 for f in found if f.severity is Severity.ERROR)
    print(
        f"{len(bundle.descriptors)} descriptors, {len(bundle.vectors)} vectors: "
        f"{errors} error(s), {len(found) - errors} warning(s)"
    )
    return 1 if errors else 0


def _model_choice(args: argparse.Namespace) -> str | int:
    """Which model answers the pipe, or an exit code saying why none can."""
    if args.model != "claude":
        return args.model
    from zeos_chat import config

    read = config.load_env()
    if not config.api_key_is_set():
        where = f" (read {read})" if read else " (no .env found)"
        print(
            f"no ANTHROPIC_API_KEY{where}; copy .env.example to .env, or use --model stub",
            file=sys.stderr,
        )
        return 2
    try:
        import zeos_chat.claude  # noqa: F401
    except ModuleNotFoundError:
        print("the claude model needs the SDK: uv sync --extra claude", file=sys.stderr)
        return 2
    return "claude"


def _cmd_run(args: argparse.Namespace) -> int:
    bundle = load_case(Path(args.case))
    blocking = [f for f in findings(bundle) if f.severity is Severity.ERROR]
    if blocking and not args.force:
        for finding in blocking:
            print(finding.render(), file=sys.stderr)
        print("refusing to run a tree that does not lint (--force to override)", file=sys.stderr)
        return 1

    from zeos_chat import config

    config.load_env()  # same reason as in `_cmd_serve`: settings are not model settings

    model = _model_choice(args)
    if isinstance(model, int):
        return model

    names: dict[JobId, str] = {}

    def who(job: JobId | None) -> str:
        return f"{names.get(job, f'job {job}') if job is not None else 'device':<13}"

    def on_reply(pipe: PipeName, text: str) -> None:
        """What the person actually sees. In the web app this is the reply arriving."""
        print(f"{'you':<13} <== {text}", flush=True)

    def on_event(new: Sequence[Event]) -> None:
        """The facts only the kernel knows, in the order it recorded them."""
        for event in new:
            if isinstance(event, JobSpawned):
                names[event.job] = str(event.descriptor)
                print(f"{who(event.job)} spawned at priority {int(event.priority)}", flush=True)
            elif isinstance(event, PipeWritten):
                print(f"{who(event.job)} ==> {event.pipe:<17} {' '.join(event.text)}", flush=True)
            elif isinstance(event, JobBlocked):
                print(f"{who(event.job)} ... waiting on {event.pipe}", flush=True)
            elif isinstance(event, VectorFired):
                print(
                    f"{'kernel':<13} !!! {event.vector} fired {event.handler} "
                    f"at priority {int(event.priority)}",
                    flush=True,
                )
            elif isinstance(event, VectorThrottled):
                print(f"{'kernel':<13} !!! {event.vector} throttled", flush=True)
            elif isinstance(event, JobPreempted):
                print(
                    f"{who(event.job)} <<< preempted by {who(event.by_job).strip()} "
                    f"(stack depth {event.stack_depth})",
                    flush=True,
                )
            elif isinstance(event, JobResumed):
                changed = (
                    ", ".join(f"{d.obj}: {d.before} -> {d.after}" for d in event.dirty)
                    or "nothing it reads changed"
                )
                print(f"{who(event.job)} >>> resumed, {changed}", flush=True)
            elif isinstance(event, FaultRaised):
                print(f"{who(event.job)} !!! {event.fault}: {event.detail}", flush=True)

    session, adapter, _ = build_session(
        bundle,
        model=model,
        on_reply=on_reply,
        on_event=on_event,
        journal=Path(args.journal) if args.journal else None,
        seed=args.seed,
        block_size=args.block_size,
        max_ticks=args.max_ticks,
    )
    session.boot()

    pending = list(load_schedule(Path(args.events))) if args.events else []
    ticks = 0
    while ticks < args.max_ticks:
        while pending and pending[0].at_ns <= session.now_ns:
            event = pending.pop(0)
            session.deliver(event.pipe, event.text)
        if session.step():
            ticks += 1
        elif pending:
            session.skip_to(pending[0].at_ns)
        elif adapter.in_flight:
            # Quiescent is no longer the same as finished. A job parked on the model's
            # reply leaves the kernel with nothing to run, which is exactly the point --
            # but the answer is still coming, so the loop waits for the device rather than
            # concluding the conversation is over.
            time.sleep(0.01)
        else:
            break

    session.close()
    print(f"\n{bundle.name}: quiescent after {ticks} ticks, {len(session.events)} journal events")
    return 0


def _mail_line(mail: MailAdapter) -> str:
    """What the console says about mail, which is the one thing worth being loud about.

    A run that would really send needs to say so before anybody clicks, and a run that
    would not needs to say that too -- otherwise "it didn't arrive" and "it was never
    going to" look the same.
    """
    if mail.mode == "desktop":
        where = f" addressed to {mail.config.to}" if mail.config.to else ""
        return (
            f"mail: opens a draft{where} in your mail client; "
            f"the letter is also written to {mail.config.outbox}{os.sep}"
        )
    if mail.mode == "live":
        return f"mail: LIVE -- a send will really go to {mail.config.to}"
    if mail.config.transport == "smtp" and not mail.config.configured:
        return "mail: MAIL_TRANSPORT=smtp but the account is incomplete -- sends are simulated"
    if mail.config.transport == "smtp":
        return f"mail: smtp configured for {mail.config.to}, simulated (MAIL_LIVE=1 to send)"
    return "mail: simulated -- nothing is sent and nothing is opened"


def _cmd_serve(args: argparse.Namespace) -> int:
    """Hold the conversation in a browser. This is the demonstration proper."""
    import webbrowser

    from zeos_chat import config
    from zeos_chat.web.server import serve

    # Before anything reads a setting, and not only when a key is wanted: `_model_choice`
    # loads the file too, but only on the way to the Claude model, so a stub run silently
    # ignored every MAIL_* line in it. Mail has nothing to do with which model answers.
    config.load_env()

    bundle = load_case(Path(args.case))
    blocking = [f for f in findings(bundle) if f.severity is Severity.ERROR]
    if blocking and not args.force:
        for finding in blocking:
            print(finding.render(), file=sys.stderr)
        print("refusing to run a tree that does not lint (--force to override)", file=sys.stderr)
        return 1

    model = _model_choice(args)
    if isinstance(model, int):
        return model

    # Built here so the console can say whether a send would be real before anybody
    # clicks. `serve` is what asks it to report outcomes to the page.
    mail = MailAdapter()

    session, adapter, source = build_session(
        bundle,
        model=model,
        journal=Path(args.journal) if args.journal else None,
        mail=mail,
        seed=args.seed,
        block_size=args.block_size,
    )
    chat = serve(session, adapter, source, mail, host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}"
    print(f"{bundle.name}: {url}")
    print("ctrl-c to stop.")
    print(_mail_line(mail))
    if args.open:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print()
        print("stopping")
    chat.stop()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    # A model writes em-dashes and curly quotes, and a Windows console is cp1252 by
    # default, where they become replacement characters or raise. The kernel is unaffected
    # -- what a job wrote is on the pipe as it wrote it -- but the person watching should
    # see what the person chatting would.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_lint = sub.add_parser("lint", help="typecheck the descriptor tree against the chat ABI")
    p_lint.add_argument("case", nargs="?", default=str(CASE))
    p_lint.set_defaults(func=_cmd_lint)

    p_run = sub.add_parser("run", help="run the case through the seat, playing its tapes")
    p_run.add_argument("case", nargs="?", default=str(CASE))
    p_run.add_argument(
        "--model",
        choices=MODELS,
        default="stub",
        help="who answers the model pipe. The jobs are Python either way -- the kernel, "
        "the case and the control flow do not change",
    )
    p_run.add_argument("--events", default=None, help="a JSONL event schedule to inject")
    p_run.add_argument("--journal", default=None, help="where to write the journal")
    p_run.add_argument("--seed", type=int, default=0)
    p_run.add_argument("--block-size", type=int, default=16)
    p_run.add_argument("--max-ticks", type=int, default=4000)
    p_run.add_argument("--force", action="store_true", help="run despite lint errors")
    p_run.set_defaults(func=_cmd_run)

    p_serve = sub.add_parser("serve", help="hold the conversation in a browser")
    p_serve.add_argument("case", nargs="?", default=str(CASE))
    p_serve.add_argument(
        "--model", choices=MODELS, default="claude", help="who answers the model pipe"
    )
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8181)
    p_serve.add_argument("--journal", default=None, help="where to write the journal")
    p_serve.add_argument("--seed", type=int, default=0)
    p_serve.add_argument("--block-size", type=int, default=16)
    p_serve.add_argument("--open", action="store_true", help="open a browser at it")
    p_serve.add_argument("--force", action="store_true", help="serve despite lint errors")
    p_serve.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except DescriptorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
