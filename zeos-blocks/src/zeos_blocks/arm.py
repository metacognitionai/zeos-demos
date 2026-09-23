"""The arm: a device on the end of a pipe, and it takes no time.

It drains the moves asked for since the last tick, asks the table to make each one, and
delivers the positions that changed to their actuators -- all before the kernel's next
token boundary. A job that writes a move therefore sees the result of it in its own status
regions the next time it decodes. There is nothing to wait for and nothing to wake up
from.

The arm is a *name for a capability*, the thing a stacker is allowed to reach, rather than
a model of a gripper. How long a real one would take to swing is the display's business and
not the kernel's, and the browser animates it afterwards.

What the arm says about a move goes to `arm.last`, which both planners map as a status
region. Only refusals really need it -- when a move lands, the table itself says so -- but
a refused move leaves the table exactly as it was, and a planner with no way to tell
"refused" from "nothing happened" asks for the same illegal move for ever.

A hand reaching in delivers to those same actuators. Nothing here is involved, nothing
marks the difference, and there is no second path. That is what makes a disturbance
indistinguishable from the arm's own work to everything reading the table.
"""

from __future__ import annotations

from collections.abc import Callable

from zeos.core.ids import PipeName

from zeos_blocks.world import EMPTY, Move, Refused, Table

__all__ = ["ARM_LAST", "ARM_REPORT", "ARM_REQUESTS", "Arm", "parse_move", "report_pipe"]

#: The one pipe a planner may write a move to, and the whole of its authority over the
#: table. Shared: there is one arm, and two jobs asking for it are two jobs contending for
#: one device, which is what the scheduler is for.
ARM_REQUESTS = PipeName("arm.requests")
#: Where what the arm said about the last move is published.
ARM_REPORT = PipeName("arm.report")
#: The world object behind it, which is what the status region shows.
ARM_LAST = "arm.last"


def report_pipe(position: str) -> PipeName:
    return PipeName(f"table.report.{position}")


def parse_move(text: str) -> Move | None:
    """``block=g1 to=s2`` into a move, or None if it is not one.

    The kernel has already checked this against the capability schema by the time it
    reaches the pipe, so a payload arriving here malformed means the schema and this
    parser disagree -- worth returning None and reporting it rather than raising, so the
    disagreement shows up as a refusal in the journal instead of as a crashed driver.
    """
    fields = dict(
        part.split("=", 1) for part in text.split() if "=" in part and part.count("=") == 1
    )
    if "block" not in fields or "to" not in fields:
        return None
    return Move(block=fields["block"], to=fields["to"])


class Arm:
    """Applies moves to a table and publishes what happened.

    ``deliver`` is the session's; taking it as a callable rather than reaching for the
    kernel is what keeps this outside the kernel's thread rules -- the arm never touches
    the kernel, it hands work to the one thing that may.
    """

    def __init__(
        self,
        layout: Callable[[], dict[str, str]],
        deliver: Callable[[PipeName, str], None],
    ) -> None:
        #: Read fresh for every move rather than kept alongside. The world store is the
        #: truth, and a copy held here would drift the moment anything moved a block
        #: without going through the arm -- which is the one event this whole
        #: demonstration is about.
        self._layout = layout
        self._deliver = deliver
        #: Every move asked for, and what came of it. Read by the tests and the page.
        self.log: list[tuple[str, str]] = []

    def ask(self, text: str, asker: str = "") -> str:
        """One move, applied now. Returns what is published about it.

        ``asker`` names the job whose move this was, because the report is shared. With a
        background goal on the table two jobs write the same request pipe and read the same
        report, and a line saying only "refused" would have the stacker reasoning about a
        refusal that was tidy's.
        """
        move = parse_move(text)
        if move is None:
            answer = f"refused: {text!r} is not a move"
        else:
            table = Table.of(self._layout())
            try:
                changed = table.apply(move)
            except Refused as exc:
                answer = f"refused: {exc}"
            else:
                answer = f"done: {move.block} to {move.to}"
                self.publish(table, changed)
        self.log.append((text, answer))
        self._deliver(ARM_REPORT, f"{asker}: {answer}" if asker else answer)
        return answer

    def publish(self, table: Table, positions: tuple[str, ...]) -> None:
        """Write the named positions back to their actuators.

        Only the positions a move touched, never the whole table. That is what keeps a
        job's diff to the lines that actually moved -- delivering all of them would
        report every position as having changed on every move.

        An emptied position is published as ``EMPTY`` rather than as nothing: a status
        region shows an object with no value as ``(unset)``, which reads as "there is no
        such position" rather than "that position is empty".
        """
        layout = table.layout()
        for position in positions:
            self._deliver(report_pipe(position), layout[position] or EMPTY)
