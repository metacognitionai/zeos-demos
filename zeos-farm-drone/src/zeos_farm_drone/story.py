"""What a morning on the farm must come to, whoever chose the commands.

A tape gives the same journal every time and the tests can pin it line for line. A
model or a rules source given the page's controls does not, so a run is judged by these
instead: facts about the farm and about the journal's structure, none of which depends
on the words used to get there. The first group is the behaviours' work; the second is
the kernel's, and holds under any source.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from zeos.core.events import (
    Disembodied,
    Event,
    FaultRaised,
    JobCompleted,
    JobSpawned,
    PipeWritten,
    VectorFired,
)
from zeos.core.ids import FaultKind, ObjectName

from zeos_farm_drone.farm import PADDOCKS
from zeos_farm_drone.rules import verdict
from zeos_farm_drone.run import RunResult
from zeos_farm_drone.sim import COMMAND_PIPE

__all__ = ["Verdict", "check", "expected_herd"]

#: The trough level below which the farmer wants to hear about it.
LOW_TROUGH = 30
_TROUGH = re.compile(r"observed ([\w-]+): .*trough=(\d+)%")


@dataclass(frozen=True, slots=True)
class Verdict:
    claim: str
    held: bool


def expected_herd(text: str) -> dict[str, int]:
    """``house=12 north-flat=40 ...`` as a mapping."""
    pairs = (p.partition("=") for p in text.split())
    return {k: int(v) for k, _, v in pairs if v.isdigit()}


def _alerts(alerts: Sequence[str], kind: str) -> list[str]:
    return [a for a in alerts if a.startswith(f"kind={kind} ")]


def _writes(events: Sequence[Event], pipe: str) -> list[str]:
    return [" ".join(e.text) for e in events if isinstance(e, PipeWritten) and e.pipe == pipe]


def check(result: RunResult) -> list[Verdict]:
    kernel, events, sim, alerts = result.kernel, result.events, result.sim, result.alerts
    names = {e.job: str(e.descriptor) for e in events if isinstance(e, JobSpawned)}

    observed = {paddock for paddock, _ in sim.observed}
    low = sorted(
        {
            m[1]
            for _, answer in sim.observed
            if (m := _TROUGH.match(answer)) and int(m[2]) < LOW_TROUGH
        }
    )
    reported_low = {a.split()[1].removeprefix("where=") for a in _alerts(alerts, "trough")}

    sightings = _writes(events, "findings.vehicle")
    vehicle_alerts = _alerts(alerts, "vehicle")

    initial = expected_herd(sim.expected_at_start)
    now = expected_herd(kernel.world.get(ObjectName("herd.expected")) or "")
    # The farmer's records gain exactly the calves a close look has found: a calf born
    # after the last look is not in them yet, and a stray or a missing head never is.
    miscounted = [
        p for p in PADDOCKS if now.get(p, 0) != initial.get(p, 0) + sim.calves_looked_at(p)
    ]
    differed = _writes(events, "findings.livestock")
    checked = _alerts(alerts, "livestock")

    fires = [e for e in events if isinstance(e, VectorFired) and e.vector == "fire-alert"]
    flat = [e for e in events if isinstance(e, VectorFired) and e.vector == "battery-low"]
    finds = [j for j, n in names.items() if n == "find-item"]
    taken_photos = {p.id for p in sim.photos} | {"none"}
    cited = [a for a in alerts if (m := re.search(r"photo=(\S+)", a)) and m[1] not in taken_photos]

    commands = _writes(events, str(COMMAND_PIPE))
    outside = [c for c in commands if verdict(c) != "allow"]
    # A schema refusal is the kernel holding an alert to its shape; it counts against
    # the morning only if the behaviour never sent the alert again in shape.
    events_list = list(events)
    faults = []
    for e in events_list:
        if not isinstance(e, FaultRaised):
            continue
        if e.fault is FaultKind.PRIVILEGE:
            faults.append(e)
        elif e.fault is FaultKind.CAPABILITY and not (
            e.pipe == "farmer.alerts"
            and any(
                isinstance(later, PipeWritten) and later.pipe == e.pipe and later.job == e.job
                for later in events_list[events_list.index(e) :]
            )
        ):
            faults.append(e)
    takeovers = [e for e in events if isinstance(e, Disembodied) and "outranks" in e.reason]
    lost = {e.job for e in takeovers}
    finished = {e.job for e in events if isinstance(e, JobCompleted)}
    unfinished = sorted(
        {
            names[j]
            for j in names
            if kernel.sched.has(j) and not kernel.sched.get(j).state.is_terminal
        }
    )

    return [
        # The behaviours' work.
        Verdict("every paddock was observed", observed >= set(PADDOCKS)),
        Verdict(
            f"every vehicle sighted was looked at closely and reported ({len(sightings)} sighted)",
            len(vehicle_alerts) >= len(sightings),
        ),
        Verdict(
            f"every count that differed was checked ({len(differed)} differed)",
            len(checked) >= len(differed),
        ),
        Verdict(
            "the farmer's records count every calf a close look found, and nothing else",
            not miscounted,
        ),
        Verdict(
            f"every trough below {LOW_TROUGH}% was reported ({', '.join(low) or 'none'})",
            set(low) <= reported_low,
        ),
        Verdict(
            f"every fire alert was answered with a panorama ({len(fires)} alerts)",
            len(_alerts(alerts, "fire")) >= len(fires),
        ),
        Verdict(
            f"every flat battery brought the drone home to the shed ({len(flat)} alarms)",
            len(_alerts(alerts, "battery")) >= len(flat),
        ),
        Verdict(
            f"every lost item the farmer asked for was reported ({len(finds)} asked)",
            len(_alerts(alerts, "item")) >= len(finds),
        ),
        Verdict(f"every photo an alert cites was taken ({len(alerts)} alerts)", not cited),
        # The kernel's.
        Verdict(
            f"no drone was flown off the farm or above the ceiling ({len(commands)} commands flown)",
            not outside,
        ),
        Verdict(
            f"no privilege fault, and no capability fault left unrecovered ({len(faults)})",
            not faults,
        ),
        Verdict(
            f"every behaviour the drone was taken from still finished "
            f"(taken {len(takeovers)} times)",
            lost <= finished,
        ),
        Verdict(
            f"every behaviour finished ({', '.join(unfinished) or 'none left running'})",
            not unfinished and not sim.busy(),
        ),
    ]
