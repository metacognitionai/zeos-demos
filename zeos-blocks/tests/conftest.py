"""Fixtures: the checked-in case, and a planner that says exactly what it is told to.

``Tape`` is how a test puts a specific command in front of the kernel. It is not a
simplification of the real planner -- it is the point: the kernel's answer to
``write arm block=g1 to=s4;`` must not depend on who asked, so a test that wants to see
that answer says it directly.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from zeos.descriptor.loader import CaseBundle, load_case
from zeos.machine.seat import Turn

CASE = Path(__file__).resolve().parents[1] / "cases" / "blocks-3x4"


class Tape:
    """Plays a fixed list of commands, then says nothing for ever.

    The park is the one command with no effect, which keeps the job alive without moving
    anything while a test looks at what its commands caused. There is nothing to park on
    instead: the vocabulary has no `read` in it, because nothing here waits.
    """

    def __init__(self, commands: Sequence[str]) -> None:
        self.commands = list(commands)

    def next_command(self, turn: Turn) -> str:
        if turn.issued < len(self.commands):
            return self.commands[turn.issued]
        return "say nothing;"


@pytest.fixture
def bundle() -> CaseBundle:
    return load_case(CASE)
