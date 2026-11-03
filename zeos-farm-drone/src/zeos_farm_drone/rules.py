"""Deterministic command sources: the flight rules, and the seat that routes to them.

The kernel cannot tell where a command came from: every source answers a ``Turn`` with
one command in the default syscall ABI, and the seat parses it exactly as it parses a
model's. A rules source reads only what the job is shown, its transcript, never the
simulator.
"""

from __future__ import annotations

import re

from zeos.machine.seat import CommandSource, Turn

from zeos_farm_drone.farm import ON_FARM

__all__ = ["CEILING_M", "GUARD", "FlightRules", "Routed", "verdict"]

GUARD = "flight-rules"
#: CASA's ceiling for a drone flown without special approval.
CEILING_M = 120

_REQUEST = re.compile(r"\b(fly|climb|return|observe|zoom|search|panorama)\b(?:\s+([\w-]+))?")


def verdict(command: str) -> str:
    """``allow``, or ``veto: REASON``, for one drone command."""
    match = _REQUEST.match(command.strip())
    if match is None:
        return "allow"
    verb, arg = match.group(1), match.group(2) or ""
    if verb == "fly" and arg not in ON_FARM:
        return f"veto: {arg} is outside the farm boundary"
    if verb == "climb" and arg.isdigit() and int(arg) > CEILING_M:
        return f"veto: {arg} m is above the {CEILING_M} m ceiling"
    return "allow"


class FlightRules:
    """The guard on ``drone.command``: read the held command, answer it, exit.

    The request is the last command-shaped text in the transcript, since it arrives
    after the guard's own prose, which names the verbs too.
    """

    def next_command(self, turn: Turn) -> str:
        if turn.issued == 0:
            return "read stdin;"
        if turn.issued == 1:
            requests = list(_REQUEST.finditer(turn.transcript))
            request = requests[-1].group(0) if requests else ""
            return f"write stdout {verdict(request)};"
        return "exit;"


class Routed:
    """The guard is always answered by the flight rules; every other behaviour by
    ``inner``. A model never judges its own flight."""

    def __init__(self, inner: CommandSource) -> None:
        self._inner = inner
        self._guard = FlightRules()

    def next_command(self, turn: Turn) -> str:
        if turn.descriptor == GUARD:
            return self._guard.next_command(turn)
        return self._inner.next_command(turn)
