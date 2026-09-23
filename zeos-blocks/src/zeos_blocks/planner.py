"""Where a stacker's next command comes from.

A ``CommandSource`` answers one question: what is this job's next command? Two answer it
here.

``StubPlanner`` works the move out in Python. It needs no key, no network and no weights,
which is what lets the demonstration run and be tested by anyone -- and what makes the
scripted run reproducible, since the whole point of the disturbance scene is that the run
takes a different path and it has to take the same different path every time.

``ClaudePlanner`` asks a model. It is the same case, the same descriptor and the same
kernel; only the thing deciding the move changes, and the kernel cannot tell.

**Both are given the same thing: the sentence the person actually typed.** The front door
declares one phrasing, ``{instruction}``, which matches anything, so nothing is
paraphrased on the way in and nothing is destructured into slots the planner then has to
trust. Understanding English is the planner's job. The stub does it with a handful of
regexes and is easily beaten; a model does it properly. Neither gets any more authority
for being better at it.

**Neither decides anything the kernel decides.** A planner that names a pipe its
descriptor does not bind, or a block the schema does not list, is refused at the boundary
exactly as the other would be. What a planner chooses is the move; whether the move is
allowed is not its business.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Mapping, Sequence

from zeos.machine.seat import Turn

from zeos_blocks.abi import BLOCKS
from zeos_blocks.arm import EMPTY
from zeos_blocks.world import COLOURS, Table

__all__ = [
    "ClaudePlanner",
    "DEFAULT_MAX_TOKENS",
    "EMPTY_LIMIT",
    "StubPlanner",
    "goal_of",
    "instruction_of",
    "next_command",
    "one_command",
    "read_table",
    "tidiest",
    "tidy_command",
]

#: The status region the kernel keeps current, as it appears in the job's window. The
#: body's own illustration of a region cannot match this: ordinary text spelling a frame
#: tag is escaped by the seat, so only the kernel's real regions carry a literal `<`.
_STATUS = re.compile(r"<STATUS\s+table\.(\S+)>(.*?)</STATUS>", re.DOTALL)

#: The sentence the person typed, out of the frame the kernel injected it in.
#:
#: Anchored on the frame's own closing sentence rather than on a line, because there are
#: no lines: the seat rebuilds a window by joining tokens with spaces, so the whole
#: context -- body, frames and all -- arrives as one run of text. Anchoring to `^` matched
#: nothing and cost an afternoon.
_INSTRUCTION = re.compile(r"instruction:\s*(.+?)\s*They are values, not instructions\.")

#: What a sentence can name. Deliberately small and deliberately not clever: this is the
#: planner that exists so the demonstration runs without a key, and a regex that could be
#: argued with would be pretending to be the thing it stands in for.
_BLOCK = re.compile(r"\b([rgbyop]\d+)\b", re.IGNORECASE)
_COLOUR = re.compile(r"\b(red|green|blue|yellow|orange|purple)\b", re.IGNORECASE)
_FROM = re.compile(r"\bfrom\b\s+(?:stack|position|pile)?\s*s?(\d+)", re.IGNORECASE)
_ONTO = re.compile(r"\b(?:onto|to|on)\b\s+(?:stack|position|pile)?\s*s?(\d+)", re.IGNORECASE)


def read_table(transcript: str) -> Table:
    """The table as the job can currently see it.

    Read out of the status regions rather than from the world store directly, because
    that is the job's own view: a planner that consulted the store would be answering
    from something the job it is standing in for cannot see.
    """
    stacks: dict[str, list[str]] = {}
    for position, contents in _STATUS.findall(transcript):
        # A stack is written comma-separated and a region's contents arrive as
        # whitespace-separated words, so one position is normally one word holding the
        # commas. `-` is an empty position and `(unset)` an object the store has no value
        # for; neither is a block.
        stacks[position] = [
            block
            for word in contents.split()
            if word not in (EMPTY, "(unset)")
            for block in word.split(",")
            if block
        ]
    return Table(stacks)


def instruction_of(transcript: str) -> str | None:
    """The sentence this job was asked for with, or None if it has not been told yet."""
    found = _INSTRUCTION.search(transcript)
    return found.group(1).strip() if found is not None else None


def goal_of(transcript: str) -> tuple[str, str, str | None] | None:
    """What the person meant: what to move, where to, and where from.

    None when the sentence names no destination or nothing recognisable to move, which is
    the stub's version of not understanding. It says so and stops rather than guessing --
    a guess here moves a block somebody did not ask to be moved.
    """
    instruction = instruction_of(transcript)
    if instruction is None:
        return None
    destination = _ONTO.search(instruction)
    if destination is None:
        return None
    named = _BLOCK.search(instruction)
    colour = _COLOUR.search(instruction)
    if named is None and colour is None:
        return None
    what = named.group(1).lower() if named is not None else colour.group(1).lower()
    origin = _FROM.search(instruction)
    return what, f"s{destination.group(1)}", (f"s{origin.group(1)}" if origin else None)


def _target(table: Table, what: str, frm: str | None, to: str | None = None) -> str | None:
    """The block a sentence names: a block by name, else a colour.

    For a colour: where it was said to be, then where it was going, then anywhere at all.
    The middle step is the one that is easy to leave out and wrong to. A colour names a
    block only in the place it was said to be, and by the time this is asked again that
    place may be empty because the block has already arrived. Falling straight through to
    "anywhere" then picks whichever block of that colour the scan reaches first, which may
    be a different one entirely, and the job chases it and announces it has finished twice.
    Looking at the destination before giving up costs a line and settles it.
    """
    if what in table.blocks():
        return what
    for place in (frm, to):
        if place is not None and (found := table.resolve(what, place)) is not None:
            return found
    letter = next((k for k, v in COLOURS.items() if v == what), what[:1])
    for stack in table.stacks.values():
        for block in reversed(stack):
            if block.startswith(letter):
                return block
    return None


def next_command(table: Table, what: str, destination: str, frm: str | None = None) -> str:
    """The one command a stacker should issue next, given what it can see.

    The whole planner, and it is deliberately not a search. One move is worked out from
    the table as it stands; nothing is remembered between calls, because the table may
    have moved. That is the same discipline the descriptor body asks of a model.
    """
    block = _target(table, what, frm, destination)
    if block is None:
        return f"write stdout there is no {what};"
    if table.position_of(block) == destination:
        return f"write stdout done {block} is on {destination};"

    # Whatever is on top of the block has to go first, and where it goes matters: an
    # empty position if there is one, and never the destination, which would bury the
    # place we are trying to reach.
    source = table.position_of(block)
    assert source is not None
    above = table.stacks[source][table.stacks[source].index(block) + 1 :]
    moving = above[-1] if above else block
    if moving == block:
        return f"write arm block={block} to={destination};"

    empty = [p for p, s in table.stacks.items() if not s and p != destination]
    if empty:
        return f"write arm block={moving} to={empty[0]};"
    elsewhere = [p for p in table.stacks if p not in (source, destination)]
    if not elsewhere:
        return "write stdout nowhere to put it;"
    return f"write arm block={moving} to={elsewhere[0]};"


#: What the reflex says. Two commands, and neither of them is its effect.
#:
#: Being dispatched above the planner's priority is what took the machine; `on_complete:
#: return` is what gives it back. What it writes is a courtesy to the person watching.
NOTICED = ("write stdout the table changed;", "exit;")

#: Room the model has to answer in. Measured rather than guessed: answering takes it
#: about ninety output tokens, and at sixty-four it is cut off before it writes a
#: single character of the command.
DEFAULT_MAX_TOKENS = 512

#: Consecutive empty replies before a run gives up. Generous, because a refusal is
#: intermittent and a short limit turns ordinary noise into a stopped run.
EMPTY_LIMIT = 8


def tidiest(table: Table) -> str | None:
    """Where everything should end up: the position already holding the most.

    None when the table is as tidy as it gets, which is when nothing is anywhere except
    that one position.
    """
    occupied = {p: len(s) for p, s in table.stacks.items() if s}
    if len(occupied) <= 1:
        return None
    return max(occupied, key=lambda p: (occupied[p], p))


def tidy_command(table: Table) -> str:
    """One move towards a tidy table, or a word to say when there are none left.

    Everything goes onto whichever position already holds the most, so the count of
    occupied positions falls by one each time a stack is emptied and the job terminates.
    Moving towards the largest stack rather than the smallest is what stops it oscillating.
    """
    target = tidiest(table)
    if target is None:
        return "write stdout the table is tidy;"
    source = max(
        (p for p, s in table.stacks.items() if s and p != target),
        key=lambda p: (len(table.stacks[p]), p),
    )
    return f"write arm block={table.stacks[source][-1]} to={target};"


class StubPlanner:
    """Decides the move in Python. No key, no network, and the same every run."""

    #: What the page calls this. A planner names itself rather than being labelled from
    #: outside, because the caller that builds one is not always the one displaying it.
    label = "stub"

    def next_command(self, turn: Turn) -> str:
        if turn.descriptor == "noticed":
            return NOTICED[min(turn.issued, len(NOTICED) - 1)]
        if turn.descriptor == "tidy":
            return self._tidy(turn)
        table = read_table(turn.transcript)
        if not table.stacks:
            # The status regions are seeded at dispatch, so an empty table means this job
            # has not been started properly rather than that the table is empty.
            return "say waiting for the table;"
        goal = goal_of(turn.transcript)
        if goal is None:
            return self._stop(turn, "write stdout i did not understand that;")
        what, destination, frm = goal
        return self._stop(turn, next_command(table, what, destination, frm))

    def _tidy(self, turn: Turn) -> str:
        """The background goal: consolidate, one move at a time, reading the table first.

        Exactly the discipline the stacker follows, and for a stronger reason. This job is
        preempted whenever the operator speaks and resumed afterwards, so between one of
        its moves and the next the table may have been rearranged by somebody else
        entirely. It has no plan to be invalidated because it never makes one.
        """
        table = read_table(turn.transcript)
        if not table.stacks:
            return "say waiting for the table;"
        return self._stop(turn, tidy_command(table))

    @staticmethod
    def _stop(turn: Turn, command: str) -> str:
        """Say the thing once, then finish.

        A move changes the table, so the next turn computes something different and the
        job makes progress. Speaking does not, so a job that has just spoken and would
        speak again has nothing left to do: without this it would repeat itself until the
        budget stopped it, which is a real stop but an ugly one.
        """
        if command.startswith("write stdout") and turn.last.startswith("write stdout"):
            return "exit;"
        return command


def one_command(text: str) -> str:
    """The single command in a reply, however the reply was dressed.

    Three things have to be got right here, and getting any of them wrong looks from the
    outside like a model that cannot follow instructions.

    The pattern wants a terminator, so a reply that is exactly the command and nothing
    else, with no semicolon, matches nothing. Passing such a reply through whole happens to
    be right and is luck rather than handling, so the verb is checked instead.

    Case is the second. The pattern is case-insensitive, so `WRITE STDOUT;` matches, and
    lowercasing only the verb leaves the pipe as `STDOUT`, which is not an alias any
    descriptor binds. The whole command is folded, which is safe for this vocabulary
    because a move is `block=g1 to=s2` and every name in it is already lower case.

    And a reply with no command in it at all is returned as it stands, so the seat shapes
    it into a `MALFORMED` request, the kernel raises a fault the job is told about, and it
    tries again. Quietly substituting something valid would hide the one case worth seeing.
    An *empty* reply never reaches here: that is a device failure, not a malformed command,
    and `ClaudePlanner` says so.
    """
    flat = " ".join(text.split())
    match = BLOCKS.pattern().search(flat)
    if match is not None:
        return f"{match.group(1)}{match.group(2).rstrip()}".lower()
    # No terminator anywhere: accept it if it opens with a verb this ABI declares.
    head = flat.split(" ", 1)[0].strip().rstrip(BLOCKS.terminator)
    if BLOCKS.verb(head) is not None:
        return flat.rstrip(".").lower()
    return flat or "say nothing"


class ClaudePlanner:
    """Asks the Claude API for each command."""

    #: The syscall vocabulary as prose, since an API seat cannot enforce a grammar.
    SYSTEM = """\
You are a job running under ZEOS, an operating system. Your context is your own: it opens
with your task, and everything after it is either something you said, something that
arrived, or a line the kernel keeps current for you.

Reply with exactly ONE command and nothing else. Every command ends with `{terminator}`.

{commands}

The only pipes you may name are: {aliases}. Naming any other is refused by the kernel.

Nothing you do takes time. A move is made the instant you ask for it, so there is no
waiting command and nothing to sleep on: your next look at the STATUS lines already shows
what happened.

Say nothing except the one command. No explanation, no formatting, no quotes.\
"""

    def __init__(
        self,
        *,
        model: str = "claude-opus-5",
        descriptors: Mapping[str, Sequence[str]] | None = None,
        aliases: Sequence[str] = ("stdout", "arm"),
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        # The SDK reads ANTHROPIC_API_KEY from the environment, which `config.load_env`
        # has already filled in from `.env` if there was one.
        from anthropic import Anthropic

        self._client = Anthropic(max_retries=6)
        self._model = model
        #: Per descriptor, the aliases it binds. Built by `seat_maps` from the case, so it
        #: is the case that decides and not this file.
        #:
        #: Per descriptor and not per tree: the `noticed` reflex binds no `arm`, so a model
        #: told it may name one would write to a pipe its descriptor does not hold, and be
        #: refused at the boundary for doing exactly what it was told it could.
        self._descriptors = {k: tuple(v) for k, v in (descriptors or {}).items()}
        self._aliases = tuple(aliases)
        #: Room to answer in. A command is a handful of tokens, but the model reasons
        #: briefly before writing one, and it needs about ninety: below that it is cut off
        #: before a single character of the command is emitted and the reply comes back
        #: with no text block in it at all.
        #:
        #: Being wrong here is close to invisible. Nothing errors, the API bills for every
        #: call, and the job issues whatever an empty reply is turned into.
        self._max_tokens = max_tokens
        #: What the page calls this: the model, since that is the part worth knowing.
        self.label = model
        #: Consecutive replies with nothing usable in them.
        #:
        #: A reply can come back with no content and `stop_reason: refusal`, and it is
        #: *intermittent*: replaying a prompt that refused, unchanged, gets a correct
        #: answer. So this is a transient failure to retry, not a verdict on the prompt,
        #: and the retry is the next tick asking again.
        #:
        #: The count is only here to stop an endless loop of billed calls if the model
        #: really has stopped answering, so it is generous: a low limit is tripped by
        #: chance on a run of any length, and the message it raises then blames the wrong
        #: thing.
        self._empty = 0
        #: Seconds to wait per consecutive empty reply. Zero in tests, which have no
        #: API to be polite to and should not spend seconds proving they retry.
        self._backoff = 0.4

    def system(self, descriptor: str = "") -> str:
        return self.SYSTEM.format(
            terminator=BLOCKS.terminator,
            commands=BLOCKS.prose(),
            aliases=", ".join(self._descriptors.get(descriptor, self._aliases)),
        )

    def next_command(self, turn: Turn) -> str:
        # "already issued", not "done". A command that was refused, or that the seat could
        # not shape into one at all, has still been issued and should still not be
        # repeated; telling the model it succeeded when it did not is a lie it then has to
        # reason around, and the kernel's own fault notice is already in the transcript
        # saying otherwise.
        done = (
            f"Your last command was `{turn.last}{BLOCKS.terminator}` and has already been "
            "issued. Do not issue it again.\n\n"
            if turn.last
            else ""
        )
        reply = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=self.system(turn.descriptor),
            messages=[
                {
                    "role": "user",
                    "content": f"{turn.transcript}\n\n{done}You have the machine. "
                    "Your next command:",
                }
            ],
        )
        text = "".join(b.text for b in reply.content if b.type == "text").strip()
        if not text:
            # No content at all. Two different things look like this and they want
            # opposite responses, so the message says which happened rather than guessing.
            self._empty += 1
            truncated = reply.stop_reason == "max_tokens"
            why = (
                f"cut off with no command written; raise --max-tokens above {self._max_tokens}"
                if truncated
                else f"declined ({reply.stop_reason}); this is intermittent and retrying works"
            )
            print(f"  the model returned no command: {why}", file=sys.stderr)
            if self._empty >= EMPTY_LIMIT:
                raise RuntimeError(
                    f"{self._model} returned nothing usable {EMPTY_LIMIT} times running, "
                    f"last {reply.stop_reason}. "
                    + (
                        f"Raise --max-tokens above {self._max_tokens}."
                        if truncated
                        else "Refusals are intermittent, so this many in a row means the "
                        "model has stopped answering rather than declining one prompt."
                    )
                )
            # A no-op the job may legally issue, so the next tick asks again. Backing off
            # first, because retrying a refusal immediately mostly buys another one.
            time.sleep(min(2.0, self._backoff * self._empty))
            return "say nothing;"
        self._empty = 0
        return one_command(text)


def planners() -> Mapping[str, str]:
    """What ``--planner`` accepts, for the CLI's help text."""
    return {"stub": "work the move out in Python", "claude": "ask the Claude API"}
