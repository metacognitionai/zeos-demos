"""The Claude API in the seat: each behaviour's next command, asked for one at a time.

A ``CommandSeat`` owns everything that makes a model a machine -- one word per decode,
the parser that turns a closed command into a kernel request, the transcript rebuilt
from the context the kernel keeps. This source only answers the seat's question: here
is the job's context, what is its next command?

The model is told the syscall ABI in prose and the pipes its behaviour binds. An API
seat cannot constrain sampling to a grammar, so a reply is searched for its one command,
and anything that is not one reaches the kernel as ``MALFORMED`` and is answered with a
fault notice. The flight-rules guard is never asked here: ``Routed`` answers it with the
rules, so a model never judges its own flight.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol, cast

from zeos.machine.abi import DEFAULT, SyscallABI
from zeos.machine.seat import Turn

__all__ = [
    "DEFAULT_MODEL",
    "ClaudeSource",
    "check_credentials",
    "one_command",
    "prompt_for",
    "system_for",
]

DEFAULT_MODEL = "claude-opus-5-5"

#: One reply is a few words, but a command chosen after a resume notice is a judgement
#: about what moved -- which drone this is now, where it is -- and that is where lower
#: effort loses the thread. Set explicitly: this model's default is lower.
EFFORT = "high"

SYSTEM = """\
You are one behaviour of a cattle farm's drone system, running as a job under ZEOS, a
transformer operating system. The farm has one drone, and while you run you are flying
it; something more urgent may take it from you for a while. Your context is your own: it opens with your task, and everything after it
is something you said or something that arrived on a pipe. Lines the operating system
writes are framed in angle brackets: a STATUS line is kept current in place, a RESUME
notice lists what changed while you were not running, and a FAULT notice says a command
of yours was refused and why.

Reply with exactly ONE command and nothing else. Every command ends with `{terminator}`.

{commands}

The only pipes you may name are: {aliases}. Naming any other is refused by the kernel.

You only ever read `stdin`. A write to any other pipe -- a finding, an alert to the
farmer, a record -- is complete once the operating system takes it: there is no answer
to it, and reading one of those pipes is refused.

`write stdout ...;` hands one command to the drone and returns at once: the drone has
not done it yet. Its answer -- `done ...`, `observed ...`, `zoomed ...`, `found ...`,
`interrupted ...` or `refused ...` -- arrives on stdin. So after every `write stdout
...;` your next command is `read stdin;`, unless a FAULT notice says the command was
refused before it reached the drone: then there is no answer to read, and you choose
again. Every answer from the drone says where the drone is. `interrupted ...` means the
drone was taken from you before the command was done and stopped over the place named:
the command is not done, so send it again if you still need it.

Every drone command passes the farm's flight rules first: nothing flies off the
property, and nothing climbs above 120 m. A refused command arrives as a FAULT notice.

A `read` costs you the machine: the kernel runs other behaviours until your pipe has
something, and what arrives appears in your context. That is how you wait for the
drone to finish an action -- there is no other way, and waiting costs nothing.

Say nothing except the one command. No explanation, no formatting, no quotes.\
"""

#: What the job has just done, said plainly: without it a model reissues its last
#: command, because its own working is words in the same run of text as its task. It
#: says the kernel took the command, never that it is "done": to a drone that reads as
#: the action being finished, and a model told so moves on without reading the answer.
_DONE = (
    "Your last command was `{last}{terminator}` and the operating system has taken it. "
    "Do not issue it again."
)
_ASK = "You have the machine. Your next command:"


class _Messages(Protocol):
    def create(self, **kwargs: Any) -> Any: ...


class _Beta(Protocol):
    @property
    def messages(self) -> _Messages: ...


class Client(Protocol):
    """The part of ``anthropic.Anthropic`` this source uses."""

    @property
    def beta(self) -> _Beta: ...


def system_for(aliases: Sequence[str], *, abi: SyscallABI = DEFAULT) -> str:
    return SYSTEM.format(
        terminator=abi.terminator,
        commands=abi.prose(),
        aliases=", ".join(aliases) or "none",
    )


def prompt_for(turn: Turn, *, abi: SyscallABI = DEFAULT) -> str:
    done = f"{_DONE.format(last=turn.last, terminator=abi.terminator)}\n\n" if turn.last else ""
    return f"{turn.transcript}\n\n{done}{_ASK}"


def one_command(text: str, *, last: str = "", abi: SyscallABI = DEFAULT) -> str:
    """The command a reply means, or the whole reply as spoken if it holds none.

    A seat takes one command a turn, so a reply holding several is read for the one it
    means now: the first, since a model that plans ahead writes the next step first
    (``write stdout observe; read stdin;``) -- unless the first is the command the job
    has just completed, which a model that explains itself quotes before answering
    ("`fly creek;` is done, so: observe;")."""
    commands = [f"{m.group(1).lower()}{m.group(2).rstrip()}" for m in abi.pattern().finditer(text)]
    if not commands:
        return " ".join(text.split()) or "nothing"
    fresh = [c for c in commands if " ".join(c.split()) != " ".join(last.split())]
    return fresh[0] if fresh else commands[0]


def check_credentials() -> str | None:
    """Why the Claude API cannot be reached, or None when it can.

    Asked once, before anything starts: the first command would otherwise fail the same
    way, after the page is open and the patrol has begun. Listing one model resolves
    credentials the way every later request will and costs no tokens.
    """
    import anthropic

    hint = "put ANTHROPIC_API_KEY in .env, export it, or sign in with `ant auth login`"
    try:
        anthropic.Anthropic(max_retries=1).models.list(limit=1)
    except TypeError as exc:
        # How the SDK says it found no credentials at all.
        if "authentication method" not in str(exc):
            raise
        return f"no Claude API credentials were found; {hint}"
    except anthropic.AuthenticationError:
        return f"the Claude API rejected these credentials; {hint}"
    except anthropic.PermissionDeniedError:
        return "these credentials may not use the Claude API"
    except anthropic.APIConnectionError:
        return "the Claude API could not be reached; check the network"
    return None


class ClaudeSource:
    """A command source that asks the Claude API for each command."""

    def __init__(
        self,
        *,
        aliases: Mapping[str, Sequence[str]],
        model: str = DEFAULT_MODEL,
        abi: SyscallABI = DEFAULT,
        client: Client | None = None,
    ) -> None:
        if client is None:
            from anthropic import Anthropic

            # One call per command, so a morning makes dozens and a transient 529
            # somewhere in the middle is routine.
            # The SDK's client is what the protocol describes; its overloaded
            # signatures are more than a Protocol can be checked against.
            client = cast(Client, Anthropic(max_retries=6))
        self._client: Client = client
        self._model = model
        self._abi = abi
        self._aliases = {k: tuple(v) for k, v in aliases.items()}
        #: The model's last reply in full, before a command was picked out of it.
        self.last_reply: str | None = None

    def system(self, descriptor: str) -> str:
        return system_for(self._aliases.get(descriptor, ()), abi=self._abi)

    def next_command(self, turn: Turn) -> str:
        # Server-side fallback: a declined request is re-run on a fallback model inside
        # the same call rather than grounding a drone.
        response = self._client.beta.messages.create(
            model=self._model,
            max_tokens=16000,
            output_config={"effort": EFFORT},
            system=self.system(turn.descriptor),
            messages=[{"role": "user", "content": prompt_for(turn, abi=self._abi)}],
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"the model refused to produce a command for job {turn.job}")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise RuntimeError(
                f"no text in the response for job {turn.job} (stop_reason={response.stop_reason})"
            )
        self.last_reply = text
        return one_command(text.strip(), last=turn.last, abi=self._abi)
