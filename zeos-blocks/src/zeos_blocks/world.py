"""The table: blocks, positions, and the rules about what may be moved where.

This is the one place the rules live. It holds no ZEOS concepts -- no kernel, no pipes,
no jobs -- and is testable on its own, which is deliberate: the kernel checks the *shape*
of a request and whether the job may make it at all, and it knows nothing about blocks.
Whether a block is clear is a fact about a table, and a kernel that knew it would be a
kernel with blocks world compiled into it.

So a move that breaks a rule here is not a kernel fault. It is an answer the arm gives,
and the job is free to ask for something else.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "COLOURS",
    "EMPTY",
    "Move",
    "Refused",
    "Table",
    "colour_of",
    "position_names",
]

#: What a position with nothing on it reports. A status region renders an object the
#: store has no value for as ``(unset)``, so an empty position has to say something
#: rather than nothing, or "empty" and "no such position" would read alike.
EMPTY = "-"

#: A block's name is a colour letter and a number, so the name carries the colour and
#: nothing has to maintain a table mapping one to the other. This is what lets the
#: workspace grow to any number of blocks without a second declaration anywhere.
COLOURS = {
    "r": "red",
    "g": "green",
    "b": "blue",
    "y": "yellow",
    "o": "orange",
    "p": "purple",
}


def colour_of(block: str) -> str:
    """The colour a block's name declares, or the bare letter if it is not one we name."""
    return COLOURS.get(block[:1], block[:1])


def position_names(count: int) -> tuple[str, ...]:
    return tuple(f"s{i}" for i in range(1, count + 1))


class Refused(Exception):
    """A move the table will not make. Carries the reason the job is told."""


@dataclass(frozen=True, slots=True)
class Move:
    block: str
    to: str


@dataclass
class Table:
    """Positions in order, each a list of blocks from the bottom up."""

    stacks: dict[str, list[str]]

    @staticmethod
    def of(layout: dict[str, str]) -> Table:
        """Build from the world-store form, where a position is a comma-separated string.

        ``EMPTY`` and ``(unset)`` are what an empty position and an undeclared one look
        like in the store; neither is a block.
        """
        return Table(
            {
                k: [b for b in v.split(",") if b and b not in (EMPTY, "(unset)")]
                for k, v in layout.items()
            }
        )

    def layout(self) -> dict[str, str]:
        """The world-store form: what gets delivered to the actuators."""
        return {k: ",".join(v) for k, v in self.stacks.items()}

    def blocks(self) -> tuple[str, ...]:
        return tuple(sorted(b for stack in self.stacks.values() for b in stack))

    def position_of(self, block: str) -> str | None:
        return next((p for p, stack in self.stacks.items() if block in stack), None)

    def is_clear(self, block: str) -> bool:
        stack = self.stacks[self.position_of(block) or ""]
        return bool(stack) and stack[-1] == block

    def resolve(self, colour: str, position: str) -> str | None:
        """The block of that colour at that position, topmost first.

        Colours need only be unique within a stack, which is what lets a phrase name a
        block in a workspace holding several of that colour.
        """
        letter = next((k for k, v in COLOURS.items() if v == colour), colour[:1])
        stack = self.stacks.get(position, [])
        return next((b for b in reversed(stack) if b.startswith(letter)), None)

    def apply(self, move: Move) -> tuple[str, ...]:
        """Make the move, or raise ``Refused``. Returns the positions that changed.

        Returning the changed positions rather than the whole table is what keeps the
        delivery narrow: only the two lines that moved are written back, so only those
        two appear in a job's diff.
        """
        source = self.position_of(move.block)
        if source is None:
            raise Refused(f"there is no block {move.block}")
        if move.to not in self.stacks:
            raise Refused(f"there is no position {move.to}")
        if not self.is_clear(move.block):
            above = self.stacks[source][self.stacks[source].index(move.block) + 1 :]
            raise Refused(f"{move.block} is not clear: {','.join(above)} on it")
        if source == move.to:
            raise Refused(f"{move.block} is already on {move.to}")
        self.stacks[source].pop()
        self.stacks[move.to].append(move.block)
        return (source, move.to)

    def render(self) -> str:
        return " | ".join(f"{p}: {','.join(s) or EMPTY}" for p, s in self.stacks.items())
