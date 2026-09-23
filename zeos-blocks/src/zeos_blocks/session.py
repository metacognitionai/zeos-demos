"""One workspace: a kernel, a machine, a table, and the loop that turns it.

``Driver.run`` is written for a schedule that ends. A workspace somebody is watching does
not: it has to keep turning while a person is expected, deliver what they say between
ticks, and hand what drains out as it drains. So the driver's loop is unrolled here, once,
rather than in both the CLI and the server.

**The kernel is not re-entrant, and only the thread calling ``step`` may touch it.**
Everything else -- an HTTP handler, a scheduled disturbance, a person dragging a block --
calls ``deliver`` or ``start_job``, which only put on a queue. That is the whole
concurrency design, and it is why those are the methods safe to call from anywhere.

**Time here is logical, not wall-clock.** The kernel reads no clock; it is handed one, and
this is what hands it. Nothing in the workspace takes any number of ticks to happen
because it is slow -- the arm applies a move in the step it is asked -- so how long a
person watching sees it take is entirely the display's business. ``hold`` is the one place
that matters: while somebody's hand is in the workspace, logical time does not advance at
all.
"""

from __future__ import annotations

import queue
from collections.abc import Callable, Sequence
from pathlib import Path

from zeos.core.events import Event, PipeWritten, WorldWritten
from zeos.core.ids import DescriptorName, JobId, PipeName, PrincipalId
from zeos.core.kernel import KernelConfig
from zeos.core.pipes import PipeFull
from zeos.descriptor.loader import CaseBundle
from zeos.driver import Driver, build_kernel
from zeos.journal.writer import Journal
from zeos.machine.base import render
from zeos.machine.seat import CommandSeat, CommandSource

from zeos_blocks.abi import BLOCKS
from zeos_blocks.arm import ARM_REQUESTS, Arm, report_pipe
from zeos_blocks.world import EMPTY, Table

__all__ = ["CONSOLE", "DISTURBED", "OPERATOR", "REPLIES", "WORLD_PREFIX", "Session"]

#: World objects holding the table, one per position. ``arm.last`` is a world object too
#: and deliberately not under this prefix: it is what the arm said, not where a block is.
WORLD_PREFIX = "table."

CONSOLE = PipeName("operator.console")
REPLIES = PipeName("operator.replies")
#: The doorbell a hand rings. A write here fires the `hand-in-workspace` vector, which
#: dispatches a reflex above the planner's priority and so takes the machine from it.
DISTURBED = PipeName("table.disturbed")
#: Who the page speaks as. A job started from the page runs inside this envelope rather
#: than the kernel's, so a button is no more authority than typing would have been.
OPERATOR = PrincipalId("operator")


class Session:
    """A running workspace, stepped one token boundary at a time."""

    def __init__(
        self,
        bundle: CaseBundle,
        source: CommandSource,
        *,
        on_reply: Callable[[str], None] | None = None,
        on_event: Callable[[Sequence[Event]], None] | None = None,
        on_command: Callable[[str, str], None] | None = None,
        journal: Path | None = None,
        seed: int = 0,
        block_size: int = 16,
        max_ticks: int = 20_000,
    ) -> None:
        self.bundle = bundle
        self.events: list[Event] = []
        self._on_reply = on_reply
        self._on_event = on_event
        self._reported = 0
        self.now_ns = 0
        self.ns_per_tick = Driver.DEFAULT_NS_PER_TICK
        #: Deliveries from any thread. Drained between ticks, never during one.
        self._inbound: queue.Queue[tuple[PipeName, str]] = queue.Queue()
        #: Jobs asked for from any thread, by descriptor name. Same rule as deliveries:
        #: the kernel is touched on one thread and this is how everything else asks.
        self._wanted: queue.Queue[DescriptorName] = queue.Queue()

        self.kernel, transport = build_kernel(
            bundle,
            machine=CommandSeat(
                source=source,
                abi=BLOCKS,
                block_size=block_size,
                on_command=(
                    None
                    if on_command is None
                    else lambda job, line, _r: on_command(self.descriptor_of(job), line)
                ),
            ),
            journal_sink=self.events,
            config=KernelConfig(seed=seed, case=bundle.name, max_ticks=max_ticks),
        )
        self.journal = Journal(journal)
        self.driver = Driver(self.kernel, transport=transport, journal=self.journal)
        #: Who made the most recent delivery to the table -- "arm" or "hand".
        #:
        #: The journal cannot answer this: the arm's own moves and a hand reaching in are
        #: both device deliveries, so both are journalled with no job against them. Only
        #: the thing that made the delivery knows why it made it, which is this.
        self.last_cause: str | None = None
        self.arm = Arm(self.positions, self._deliver_now)
        #: Whether somebody's hand is in the workspace. While it is, nothing advances at
        #: all: see ``hold``.
        self._held = False
        # Starts the kernel and spawns the boot set, which for the smallest case is empty:
        # the workspace comes up with no jobs at all and waits to be spoken to.
        self.driver.boot(bundle.boot)

    # -- from any thread ----------------------------------------------------

    def deliver(self, pipe: PipeName, text: str) -> None:
        """Queue something for the kernel. Safe from any thread; lands on the next step."""
        self._inbound.put((pipe, text))

    def say(self, text: str) -> None:
        """What the operator typed, to the front door. Compiled by the kernel, or not."""
        self.deliver(CONSOLE, text)

    def start_job(self, name: str) -> None:
        """Ask for a job by name, as the operator. Safe from any thread.

        This is not a second front door. A front door takes *language* and compiles it;
        this takes a descriptor name and nothing else, which is what a button is: no
        sentence, no arguments, no room to mean something other than the one thing. The
        job it starts is owned by the operator, so it is clamped to the operator's
        ceiling and holds the operator's capabilities exactly as a job somebody spoke
        into being would be.
        """
        self._wanted.put(DescriptorName(name))

    def hold(self) -> None:
        """Somebody has started moving a block by hand. Stop the world.

        While a hand is in the workspace ``step`` does nothing: no job runs, the clock
        does not advance, and the kernel is not asked anything. A person part way through
        a drag is a person nothing should be racing, and the simplest way to not race
        somebody is to not move.

        This is the display's interlock and not the kernel's, which is the honest place
        for it. How long a hand hovers over a table is wall-clock time, and the kernel
        has no opinion about wall-clock time. What it costs the run is nothing: the ticks
        that did not happen are not ticks that were wasted waiting, they are ticks that
        were never needed.

        The *interrupt* is a separate thing and does not happen here. It happens on the
        drop, in ``disturb``, because that is when the world actually changes.
        """
        self._held = True

    def release(self) -> None:
        """The hand is out of the workspace, whether or not it moved anything.

        Every path that holds has to release, including a drag abandoned over nothing, or
        the workspace stays stopped for good.
        """
        self._held = False

    def disturb(self, block: str, to: str) -> str:
        """Somebody reaches in and moves a block. The one entry point for a hand.

        It writes to the same actuators the arm writes to, which is the whole point:
        nothing reading the table can tell this from a move the arm made, because there
        is no second mechanism for it to look different through.

        A hand is not bound by the rules a table imposes on an arm -- a person can lift a
        block out of the middle of a stack -- so this does not go through ``Table.apply``.
        What the stacker then sees is a table it did not expect, which it handles the way
        it handles every other table: by looking at it.

        Two deliveries follow, in this order. The actuators carry the change, which is how
        the world moves and how every job mapping the table comes to see it. ``DISTURBED``
        carries the *event*, which is what fires the vector and preempts whatever was
        planning. The state goes first on purpose: the handler that the doorbell dispatches
        would otherwise run against a table that had not changed yet.
        """
        table = Table.of(self.positions())
        source = table.position_of(block)
        if source is None:
            return f"there is no block {block}"
        if to not in table.stacks:
            return f"there is no position {to}"
        table.stacks[source].remove(block)
        table.stacks[to].append(block)
        layout = table.layout()
        self.last_cause = "hand"
        for position in (source, to):
            self.deliver(report_pipe(position), layout[position] or EMPTY)
        self.deliver(DISTURBED, f"a hand moved {block} from {source} to {to}")
        return f"{block} moved from {source} to {to}"

    # -- the kernel's thread only -------------------------------------------

    def step(self) -> bool:
        """One token boundary. Returns whether a job ran.

        The clock is advanced *before* the queue is drained rather than after. A
        delivery landing while the kernel still reads the previous tick is dated one
        tick early, and anything measured against the kernel's clock -- a vector's
        throttle, a deadline, the age of a world object -- then measures from the wrong
        instant. ``Driver.run`` advances first for the same reason.

        The arm is served *after* the tick and before the next one, which is what makes a
        move atomic as the job experiences it. The write lands inside the job's own
        operation; the arm applies it before the job decodes again; so the status regions
        the job reads next already show the result. There is no instant at which the job
        can observe a move half-done, which is why it has nothing to wait for.
        """
        if self._held:
            return False
        self.kernel.advance_time(self.now_ns)
        self._drain_inbound()
        self._start_wanted()
        first = len(self.events)
        ran = self.kernel.tick()
        self._serve_arm(first)
        self._drain_sinks()
        self.driver.reap_finished()
        self._report()
        self.now_ns += self.ns_per_tick
        return ran

    def run_until_quiet(self, limit: int = 4000) -> int:
        """Step until nothing runs and nothing is waiting to be delivered.

        The bound is not a timeout dressed up: a stacker that cannot finish is stopped by
        its own token budget and says so. This is here so a test that wires something up
        wrongly fails rather than hangs.
        """
        idle = 0
        for taken in range(limit):
            ran = self.step()
            idle = 0 if (ran or self.busy) else idle + 1
            # Two quiet ticks, because a job woken by a delivery does not run until the
            # tick after it lands.
            if idle >= 2:
                return taken
        return limit

    def _deliver_now(self, pipe: PipeName, text: str) -> None:
        """Land something in the kernel immediately. **The kernel's thread only.**

        This is what makes a move atomic as the job experiences it. ``deliver`` queues,
        and the queue is drained at the start of the *next* step, so a write made through
        it is not in the world until a tick later -- long enough for a hand reaching in to
        read a table the arm had already changed and overwrite the change with it.

        The arm is entitled to this and a hand is not, and the difference is not
        favouritism: the arm is driven from ``step`` and is therefore already on the
        kernel's thread, while a hand belongs to whatever thread a person is typing on.
        """
        try:
            self.kernel.deliver(pipe, text)
        except PipeFull:
            self._emit_reply(f"(dropped: {pipe} is full)")

    def _drain_inbound(self) -> None:
        while True:
            try:
                pipe, text = self._inbound.get_nowait()
            except queue.Empty:
                return
            try:
                self.kernel.deliver(pipe, text)
            except PipeFull:
                # A device does not stop producing, so the kernel holds nothing on its
                # behalf. Dropping knowingly is the driver's job, and saying so is this.
                self._emit_reply(f"(dropped: {pipe} is full)")

    def _start_wanted(self) -> None:
        while True:
            try:
                name = self._wanted.get_nowait()
            except queue.Empty:
                return
            if name in self.kernel.descriptors:
                self.kernel.spawn(name, owner=OPERATOR)
            else:
                self._emit_reply(f"(there is no {name} in this case)")

    def descriptor_of(self, job: JobId) -> str:
        """Which behaviour a job is running.

        Worth the lookup rather than assuming. Once a case has a second descriptor in it,
        captioning every command with the name of the first is not a cosmetic slip: a
        reflex that took the machine away from the planner would be shown as the planner
        talking to itself, which is the opposite of what happened.
        """
        found = next((j for j in self.kernel.sched.jobs() if j.job_id == job), None)
        return str(found.descriptor.name) if found is not None else "?"

    @property
    def busy(self) -> bool:
        """Whether anything is still going to happen without further input.

        Nothing is ever outstanding in the arm, because a move is applied in the step it
        is asked for. So what is left is a delivery that has not landed yet, and a hand in
        the workspace, which is not a run that is over but a run that is waiting for a
        person.
        """
        return self._held or not self._inbound.empty() or not self._wanted.empty()

    def positions(self) -> dict[str, str]:
        """The table by bare position -- `s1`, not `table.s1` -- which is what a move names.

        Read from the world store every time rather than kept alongside it. The store is
        the truth; a copy held here would drift the moment something moved a block
        without going through the arm, and that is the one event this demonstration is
        about.
        """
        return {k.removeprefix(WORLD_PREFIX): v for k, v in self.layout().items()}

    def _serve_arm(self, first: int) -> None:
        """The arm, driven between ticks. A device adapter and nothing more.

        Everything asked for during the tick is applied now, before the kernel is asked
        anything again. That is the whole of the arm's timing: there isn't any.

        ``first`` is where this step's events start, and it is here so the report can name
        the job that asked. Draining a pipe gives back tokens and not a writer, so who
        asked has to be read off the journal, where ``PipeWritten`` carries it. It matters
        once a background goal shares the arm: two jobs write this pipe, they both map the
        one report, and an unattributed refusal is one of them reasoning about the other's
        move.
        """
        if not self.kernel.pipes.get(ARM_REQUESTS).readable:
            return
        asked = [
            e.job
            for e in self.events[first:]
            if isinstance(e, PipeWritten) and e.pipe == ARM_REQUESTS and e.job is not None
        ]
        who = self.descriptor_of(asked[0]) if asked else ""
        for text in render(self.kernel.drain(ARM_REQUESTS)).split(";"):
            if text.strip():
                self.last_cause = "arm"
                self.arm.ask(text.strip(), who)

    def _drain_sinks(self) -> None:
        if not self.kernel.pipes.get(REPLIES).readable:
            return
        text = render(self.kernel.drain(REPLIES)).strip()
        if text:
            self._emit_reply(text)

    def _emit_reply(self, text: str) -> None:
        if self._on_reply is not None:
            self._on_reply(text)

    def _report(self) -> None:
        if self._on_event is not None and len(self.events) > self._reported:
            self._on_event(self.events[self._reported :])
        self._reported = len(self.events)

    # -- the world, for anything drawing it ---------------------------------

    def layout(self) -> dict[str, str]:
        """The table as the kernel has it -- the world store, not the arm's copy.

        Read from the store on purpose. The arm's table and the world objects agree
        because every move writes both, and reading the store is what would catch them
        disagreeing rather than hiding it.
        """
        return {
            str(obj): self.kernel.world.get(obj)
            for obj in sorted(self.kernel.world.objects())
            if str(obj).startswith(WORLD_PREFIX)
        }

    def world_writes(self, events: Sequence[Event]) -> list[WorldWritten]:
        """The table changing, as the kernel recorded it.

        Every one of these carries no job, the arm's included: a device delivery is made
        on nobody's behalf as far as the kernel is concerned, and that is correct -- the
        stacker asked for a move, it did not perform one. So the journal cannot say
        whether a change was asked for, and an observer that wants to know has to be told
        by whatever made the delivery. See ``last_cause``.
        """
        return [e for e in events if isinstance(e, WorldWritten)]

    def close(self) -> None:
        """Flush whatever the journal has not been given, then close it.

        The events list is the record; the `Journal` is a file it is written to. The
        driver puts the boot sequence there itself, and everything after it accumulates
        in the list, so closing without this wrote a journal containing the pipes being
        created and nothing that happened afterwards.
        """
        self.journal.extend(self.events[len(self.journal) :])
        self.journal.close()
