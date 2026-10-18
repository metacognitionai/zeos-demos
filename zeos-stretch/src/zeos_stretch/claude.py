"""The Claude API in the seat: each behaviour's next command, asked for one at a time.

A ``CommandSeat`` owns everything that makes a model a machine -- one word per decode,
the parser that turns a closed command into a kernel request, the transcript rebuilt
from the context the kernel keeps. This source only answers the seat's question: here
is the job's context, what is its next command?

The model is told the syscall ABI in prose and the pipes its behaviour binds. An API
seat cannot constrain sampling to a grammar, so a reply is searched for its one command,
and anything that is not one reaches the kernel as ``MALFORMED`` and is answered with a
fault notice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from zeos.machine.abi import DEFAULT, SyscallABI
from zeos.machine.seat import Turn

__all__ = [
    "ClaudeSource",
    "DEFAULT_MODEL",
    "check_credentials",
    "one_command",
    "prompt_for",
    "system_for",
]

DEFAULT_MODEL = "claude-opus-5"

#: One reply is a few words, but a command chosen after a resume notice is a judgement
#: about what moved, and that is where lower effort loses the thread.
EFFORT = "high"

SYSTEM = """\
You are one behaviour of Stretch, a mobile robot, running as a job under ZEOS, a
transformer operating system. Your context is your own: it opens with your task, and
everything after it is something you said or something that arrived on a pipe. Lines
the operating system writes are framed in angle brackets: a STATUS line is kept
current in place, and a RESUME notice lists what changed while you were not running.

Reply with exactly ONE command and nothing else. Every command ends with `{terminator}`.

{commands}

The only pipes you may name are: {aliases}. Naming any other is refused by the kernel.

`write stdout ...;` hands one command to the robot's body and returns at once: the body
has not done it yet. Its answer -- `done`, `interrupted` or `refused` -- arrives on stdin.
So after every `write stdout ...;` your next command is `read stdin;`. The body takes
one command from you at a time and refuses a second before you have read the answer to
the first.

A `read` costs you the machine: the kernel runs other behaviours until your pipe has
something, and what arrives appears in your context. That is how you wait for the
robot to finish an action -- there is no other way, and waiting costs nothing.

Say nothing except the one command. No explanation, no formatting, no quotes.\
"""

#: What the job has just done, said plainly: without it a model reissues its last
#: command, because its own working is words in the same run of text as its task. It
#: says the kernel took the command, never that it is "done": to a robot that reads as
#: the body having finished, and a model told so moves on without reading the answer.
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


def one_command(text: str, *, abi: SyscallABI = DEFAULT) -> str:
    """The command a reply ends on, or the whole reply as spoken if it holds none.

    The last, not the first: a model that explains itself before answering quotes the
    command it has just completed -- "`navigate desk-5` is done, so: place coffee;" --
    and the first match would send that command again."""
    matches = list(abi.pattern().finditer(text))
    if not matches:
        return " ".join(text.split()) or "nothing"
    last = matches[-1]
    return f"{last.group(1).lower()}{last.group(2).rstrip()}"


def check_credentials() -> str | None:
    """Why the Claude API cannot be reached, or None when it can.

    Asked once, before anything starts: the first command would otherwise fail the same
    way, after the page is open and the tidying has begun. Listing one model resolves
    credentials the way every later request will and costs no tokens.
    """
    import anthropic

    hint = "export ANTHROPIC_API_KEY in this terminal, or sign in with `ant auth login`"
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

            # One call per command, so a run makes dozens and a transient 529 somewhere
            # in the middle is routine.
            client = Anthropic(max_retries=6)
        self._client = client
        self._model = model
        self._abi = abi
        self._aliases = {k: tuple(v) for k, v in aliases.items()}
        #: Every reply as the model gave it, before the command was picked out of it.
        self.replies: list[tuple[str, str]] = []
        #: The model's last reply in full, before a command was picked out of it.
        self.last_reply: str | None = None

    def system(self, descriptor: str) -> str:
        return system_for(self._aliases.get(descriptor, ()), abi=self._abi)

    def next_command(self, turn: Turn) -> str:
        # Server-side fallback is enabled so that a declined request is re-run on a
        # fallback model inside the same call rather than stopping the robot.
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
        self.replies.append((turn.descriptor, text))
        self.last_reply = text
        return one_command(text.strip(), abi=self._abi)
