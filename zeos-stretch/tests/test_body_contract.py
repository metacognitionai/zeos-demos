"""The body takes one command from a behaviour at a time, and never fails its driver.

Found live: under Claude, `deliver-coffee` wrote its next command without reading the
answer to the last, the seat having told it that its last command was "done". The body
queued every one, the smoke alarm took the body back, and each queued command was
answered `interrupted` on a pipe nobody was reading until it overflowed, which raised
``PipeFull`` into the stepping thread and stopped the atrium.
"""

from __future__ import annotations

from zeos.core.events import PipeWritten
from zeos.descriptor.loader import load_case
from zeos.machine.seat import Turn

from zeos_stretch.run import CASE, RunResult, run


class Eager:
    """Plays the tapes, except that `deliver-coffee` never reads its answers."""

    def __init__(self) -> None:
        self.tapes = {
            str(n): [s.emit for s in sc.steps] for n, sc in load_case(CASE).scripts.items()
        }
        self.tapes["deliver-coffee"] = [
            c for c in self.tapes["deliver-coffee"] if c != "read stdin;"
        ]

    def next_command(self, turn: Turn) -> str:
        return self.tapes[turn.descriptor][turn.issued]


def answers(result: RunResult, pipe: str) -> list[str]:
    return [
        " ".join(e.text) for e in result.events if isinstance(e, PipeWritten) and e.pipe == pipe
    ]


def test_a_behaviour_that_never_reads_does_not_stop_the_atrium() -> None:
    result = run(source=Eager())
    assert result.kernel.world  # the run reached its end rather than raising


def test_a_second_command_before_the_answer_is_refused() -> None:
    replies = answers(run(source=Eager()), "stretch.done.deliver")
    assert (
        replies[0] == "refused pick coffee: still doing navigate coffee-machine; read stdin first"
    )


def test_an_answer_with_no_room_is_dropped_and_said() -> None:
    result = run(source=Eager())
    assert any("could not answer deliver-coffee" in line for line in result.lines)
