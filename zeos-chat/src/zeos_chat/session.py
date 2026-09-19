"""One conversation: a kernel, a machine, and the loop that turns it.

``Driver.run`` is written for a schedule that ends. A conversation does not: it has to
keep turning while a person is expected, deliver what they say between ticks, and hand
replies out as they drain. So the driver's loop is unrolled here, once, rather than in
both the CLI and the server.

**The kernel is not re-entrant, and only the thread calling ``step`` may touch it.**
Everything else -- an HTTP handler, a console reader -- calls ``deliver``, which only
puts on a queue. That is the whole concurrency design, and it is why ``deliver`` is the
one method safe to call from anywhere.
"""

from __future__ import annotations

import queue
from collections.abc import Callable, Sequence
from pathlib import Path

from zeos.core.events import Event, PipeWritten
from zeos.core.ids import JobId, PipeName
from zeos.core.kernel import KernelConfig
from zeos.core.pipes import PipeFull
from zeos.descriptor.loader import CaseBundle
from zeos.driver import Driver, build_kernel
from zeos.journal.writer import Journal
from zeos.machine.base import render
from zeos.machine.seat import CommandSeat, CommandSource

from zeos_chat.abi import CHAT
from zeos_chat.llm import LlmAdapter
from zeos_chat.mail import MailAdapter
from zeos_chat.retrieval import RetrievalAdapter

MAIL_OUTBOX = PipeName("mail.outbox")

__all__ = ["MAIL_OUTBOX", "Session"]


class Session:
    """A running conversation, stepped one token boundary at a time."""

    def __init__(
        self,
        bundle: CaseBundle,
        source: CommandSource,
        *,
        on_reply: Callable[[PipeName, str], None] | None = None,
        on_event: Callable[[Sequence[Event]], None] | None = None,
        on_arrival: Callable[[JobId, str], None] | None = None,
        journal: Path | None = None,
        llm: LlmAdapter | None = None,
        mail: MailAdapter | None = None,
        retrieval: RetrievalAdapter | None = None,
        seed: int = 0,
        block_size: int = 16,
        max_ticks: int = 100_000,
        ns_per_tick: int = Driver.DEFAULT_NS_PER_TICK,
    ) -> None:
        self.bundle = bundle
        self.events: list[Event] = []
        self._on_reply = on_reply
        self._on_event = on_event
        self._llm = llm
        self._mail = mail
        self._web = retrieval
        self._reported = 0
        self._settled = 0
        self.now_ns = 0
        self.ns_per_tick = ns_per_tick
        #: Deliveries from any thread. Drained between ticks, never during one.
        self._inbound: queue.Queue[tuple[PipeName, str]] = queue.Queue()
        #: Deliveries the kernel refused whole because the pipe was full.
        self.refused: list[tuple[PipeName, str]] = []

        self.kernel, transport = build_kernel(
            bundle,
            machine=CommandSeat(
                source=source, abi=CHAT, block_size=block_size, on_arrival=on_arrival
            ),
            journal_sink=self.events,
            config=KernelConfig(seed=seed, case=bundle.name, max_ticks=max_ticks),
        )
        self.journal = Journal(journal)
        self.driver = Driver(self.kernel, transport=transport, journal=self.journal)

    # -- from any thread ----------------------------------------------------

    def deliver(self, pipe: PipeName, text: str) -> None:
        """Queue something for the kernel. Safe from any thread; lands on the next step."""
        self._inbound.put((pipe, text))

    @property
    def has_pending_input(self) -> bool:
        """Whether something is queued that no step has picked up yet. A caller showing
        the kernel's state needs this: between a delivery and the tick that acts on it
        there is work outstanding, and a quiescent tick does not mean an idle system."""
        return not self._inbound.empty()

    # -- from the stepping thread only --------------------------------------

    def boot(self) -> None:
        self.driver.boot(self.bundle.boot)
        self._report()

    def step(self) -> bool:
        """One token boundary. Returns whether a job ran.

        The clock is advanced *before* the queue is drained, which is not cosmetic: a
        vector's throttle is measured against the kernel's clock, so delivering while it
        still reads the previous tick makes a message comfortably outside `min_interval`
        look like one inside it. ``Driver.run`` advances before it delivers for the same
        reason, and this loop diverged from it once already.
        """
        self.kernel.advance_time(self.now_ns)
        self._drain_inbound()
        ran = self.kernel.tick()
        self._report()
        self._drain_sinks()
        self._settle_actuators()
        self.driver.reap_finished()
        self.now_ns += self.ns_per_tick
        return ran

    def skip_to(self, at_ns: int) -> None:
        """Jump the clock forward when nothing is runnable. A parked conversation costs
        nothing, so spending ticks on an idle kernel would only make that less obvious."""
        self.now_ns = max(self.now_ns, at_ns)

    def close(self) -> None:
        self.journal.extend(self.events[len(self.journal) :])
        self.journal.close()

    # -- internals ----------------------------------------------------------

    def _drain_inbound(self) -> None:
        while True:
            try:
                pipe, text = self._inbound.get_nowait()
            except queue.Empty:
                return
            try:
                self.kernel.deliver(pipe, text)
            except PipeFull:
                # All or nothing, as a job's write is. The driver drops rather than
                # retries; a caller that cares can watch ``refused``.
                self.refused.append((pipe, text))
            self._report()

    def _drain_sinks(self) -> None:
        for pipe in self.kernel.pipes.all():
            if pipe.spec.sink and pipe.available:
                tokens = self.kernel.drain(pipe.name)
                text = render(tokens)
                # A request to the model is a drained sink like any other; what makes it
                # different is only who is listening. The adapter answers off this thread
                # and delivers back through the queue, so the tick stays short.
                if self._llm is not None and self._llm.handles(pipe.name):
                    self._llm.ask(pipe.name, text)
                elif self._web is not None and self._web.handles(pipe.name):
                    # The same shape as the model: a drained request, answered elsewhere,
                    # delivered back through the door every device event uses.
                    self._web.ask(pipe.name, text)
                elif self._on_reply is not None:
                    self._on_reply(pipe.name, text)

    def _settle_actuators(self) -> None:
        """Let the outbound adapters act on what latched this tick.

        Not a drain, because an actuator is not a sink: a sink carries a history the
        driver empties, an actuator carries a value that latches into world state. So the
        signal is the kernel's own journal entry saying the world changed, and the events
        list is walked once with a watermark rather than re-scanned.
        """
        if self._mail is None:
            return
        while self._settled < len(self.events):
            event = self.events[self._settled]
            self._settled += 1
            if isinstance(event, PipeWritten) and event.latched and event.pipe == MAIL_OUTBOX:
                self._mail.actuated(" ".join(event.text))

    def _report(self) -> None:
        if self._on_event is not None and len(self.events) > self._reported:
            self._on_event(self.events[self._reported :])
        self._reported = len(self.events)
