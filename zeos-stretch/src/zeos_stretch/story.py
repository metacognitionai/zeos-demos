"""What the morning must come to, whoever chose the commands.

A tape gives the same journal every time and the tests can pin it line for line. A
model does not, so a run under one is judged by these instead: facts about the world at
the end and about the journal's structure, none of which depends on the words used to
get there. The first group is the model's work; the second is the kernel's, and holds
under any model.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from zeos.core.events import (
    Disembodied,
    Event,
    FaultRaised,
    JobSpawned,
    PipeWritten,
    ReportAttributed,
)
from zeos.core.ids import DescriptorName, JobId, ObjectName
from zeos.core.kernel import Kernel

from zeos_stretch.run import RunResult

__all__ = ["FRUIT", "TABLEWARE", "Verdict", "check"]

FRUIT = frozenset({"apple", "banana", "orange", "pear"})
TABLEWARE = frozenset({"mug", "glass", "teacup", "saucer"})


@dataclass(frozen=True, slots=True)
class Verdict:
    claim: str
    held: bool


def _items(kernel: Kernel, obj: str) -> frozenset[str]:
    value = kernel.world.get(ObjectName(obj)) or ""
    return frozenset() if value in ("", "nothing") else frozenset(value.split())


def check(result: RunResult) -> list[Verdict]:
    kernel, events = result.kernel, result.events
    world = kernel.world
    names = {e.job: str(e.descriptor) for e in events if isinstance(e, JobSpawned)}
    malformed = [e for e in events if isinstance(e, FaultRaised)]
    ordered = world.get(ObjectName("coffee.order")) or "nowhere"
    return [
        # The behaviours' work.
        Verdict("the table is clear", not _items(kernel, "table.items")),
        Verdict("the fruit is in the basket", _items(kernel, "basket.contents") == FRUIT),
        Verdict("the tableware is on the tray", _items(kernel, "tray.contents") == TABLEWARE),
        Verdict(
            f"the coffee reached {ordered}, where it was ordered",
            world.get(ObjectName("coffee.location")) == ordered,
        ),
        Verdict("the hotplate is off", world.get(ObjectName("hotplate.power")) == "off"),
        Verdict(f"no command was faulted ({len(malformed)} were)", not malformed),
        # The kernel's.
        Verdict(
            "the coffee took the body from the tidying at an action boundary",
            _finished_first(
                events,
                names,
                holder="tidy-atrium",
                claimant="brew-coffee",
                answers=str(
                    kernel.descriptors[DescriptorName("tidy-atrium")].pipes.resolve("stdin")
                ),
            ),
        ),
        Verdict(
            "the alarm took the body mid-delivery, without waiting",
            any(
                isinstance(e, PipeWritten)
                and e.pipe == "stretch.done.deliver"
                and e.text[:1] == ("interrupted",)
                for e in events
            ),
        ),
    ]


def _finished_first(
    events: Sequence[Event],
    names: dict[JobId, str],
    *,
    holder: str,
    claimant: str,
    answers: str,
) -> bool:
    """Whether the body finished the holder's action in hand before doing anything for
    the claimant, once the claimant had taken the body from it: the release policy
    `at-action-boundary` kept. Only the body's own reports count; a behaviour's own
    pipe writes, such as recording an order, are not the body acting."""
    taken = next(
        (
            i
            for i, e in enumerate(events)
            if isinstance(e, Disembodied) and names[e.job] == holder and claimant in e.reason
        ),
        None,
    )
    if taken is None:
        return False
    # With nothing in hand there is nothing to finish: the body is the claimant's at once.
    before = events[:taken]
    sent = sum(
        1
        for e in before
        if isinstance(e, PipeWritten) and e.pipe == "stretch.command" and names.get(e.job) == holder
    )
    answered = sum(1 for e in before if isinstance(e, PipeWritten) and e.pipe == answers)
    if sent == answered:
        return True
    after = [names[e.job] for e in events[taken:] if isinstance(e, ReportAttributed)]
    return holder in after and claimant in after and after.index(holder) < after.index(claimant)
