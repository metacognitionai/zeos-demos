"""A scripted run: what the operator says, and when somebody reaches in.

ZEOS ships a schedule format of its own -- a pipe and the text to deliver to it, at a
time. This one is narrower on purpose. A disturbance written that way is the *result* of a
hand moving a block, worked out in advance:

    {"at_ns": 12000000, "pipe": "table.report.s1", "text": "g1"}
    {"at_ns": 12000000, "pipe": "table.report.s3", "text": "y1,r1"}

which says what the table will look like rather than what happened, and silently stops
being true the moment the plan changes. Here the same event is written as

    {"at": 12, "hand": {"block": "r1", "to": "s3"}}

and goes through the same ``Session.disturb`` a person dragging a block on the page goes
through. One path for a hand, whoever the hand belongs to.

``at`` is in ticks rather than nanoseconds because a tick is one token boundary, which is
the unit a run is actually measured in: "twelve decodes in" is a thing a reader can check
against the transcript.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

__all__ = ["Entry", "load_schedule"]


@dataclass(frozen=True, slots=True)
class Entry:
    """One thing that happens at one tick."""

    at: int
    say: str | None = None
    hand: tuple[str, str] | None = None

    @staticmethod
    def of(raw: dict[str, object], *, source: str, line: int) -> Entry:
        at = raw.get("at")
        if not isinstance(at, int):
            raise ValueError(f"{source}:{line}: every entry needs an integer 'at' in ticks")
        say = raw.get("say")
        hand = raw.get("hand")
        if (say is None) == (hand is None):
            raise ValueError(f"{source}:{line}: an entry is exactly one of 'say' or 'hand'")
        if hand is not None:
            if not isinstance(hand, dict) or "block" not in hand or "to" not in hand:
                raise ValueError(f"{source}:{line}: 'hand' needs a 'block' and a 'to'")
            return Entry(at=at, hand=(str(hand["block"]), str(hand["to"])))
        return Entry(at=at, say=str(say))


def load_schedule(path: Path) -> tuple[Entry, ...]:
    """Read a schedule. Sorted by tick, stably, so the file order settles a tie."""
    entries: list[Entry] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        entries.append(Entry.of(json.loads(stripped), source=str(path), line=number))
    return tuple(sorted(entries, key=lambda e: e.at))


def due(schedule: Sequence[Entry], tick: int) -> Iterator[Entry]:
    """Entries falling on this tick."""
    return (e for e in schedule if e.at == tick)
