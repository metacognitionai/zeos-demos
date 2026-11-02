"""The flight rules, as the guard answers them."""

from __future__ import annotations

import pytest
from zeos.core.ids import JobId
from zeos.machine.seat import Turn

from zeos_farm_drone.rules import FlightRules, verdict


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("fly creek", "allow"),
        ("fly boundary-east", "allow"),
        ("fly road", "veto: road is outside the farm boundary"),
        ("fly neighbour", "veto: neighbour is outside the farm boundary"),
        ("climb 120", "allow"),
        ("climb 121", "veto: 121 m is above the 120 m ceiling"),
        ("observe", "allow"),
        ("return", "allow"),
    ],
)
def test_verdicts(command: str, expected: str) -> None:
    assert verdict(command) == expected


def test_the_guard_judges_the_request_not_its_own_prose() -> None:
    """The guard's prose names `fly PLACE` and `climb METRES` too; the request is the
    last command in the transcript."""
    transcript = (
        "fly PLACE is allowed only to a place on the farm. climb METRES is allowed up "
        "to 120 m. read stdin; climb 300"
    )
    turn = Turn(job=JobId(1), descriptor="flight-rules", transcript=transcript, issued=1)
    assert (
        FlightRules().next_command(turn) == "write stdout veto: 300 m is above the 120 m ceiling;"
    )
