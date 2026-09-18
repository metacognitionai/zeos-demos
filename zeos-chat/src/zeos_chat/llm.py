"""The model as a device on the end of a pipe.

Core §4.2 says a tool call *is* a pipe write plus a blocking read, and that the job is
descheduled and costs nothing while it waits. This is that, with the model as the tool: a
job writes its request to a sink, the driver drains it, a worker thread asks the model,
and the answer comes back through ``deliver`` like any other device event.

What that buys is not fewer seconds per call -- the model takes what it takes -- but a
kernel that is *awake* for them. A job waiting on a reply is ``JobBlocked``: the scheduler
runs whatever else is runnable, a vector can still fire, and a reflex can still preempt.
Calling the model from inside ``decode`` instead, as a machine-driven seat must, freezes
the whole kernel for the duration.

Nothing here is ZEOS-specific cleverness. It is the ordinary device-adapter shape the
design has always described, applied to the one device this application cannot do without.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass

from zeos.core.ids import PipeName

__all__ = ["ABANDONED", "END", "LLM_PIPES", "Ask", "LlmAdapter", "StubModel", "request_pipes"]


@dataclass(frozen=True, slots=True)
class Ask:
    """One request, and where its answer goes back."""

    #: The descriptor that asked, which is what decides the persona it is answered with.
    descriptor: str
    #: What the job wrote. Prose, not a command -- the job is asking, not instructing.
    prompt: str
    reply_to: PipeName
    #: The asking job's context, as the kernel holds it. The conversation so far, paged
    #: and evicted by the kernel rather than accumulated here.
    window: str = ""


#: What the reply pipe carries when a request is abandoned rather than answered.
#:
#: A job parked on a device cannot be told to stop by anything except that device. So an
#: abandoned request is *completed*, with this instead of an answer -- which is what a
#: driver does with an aborted read, and what wakes the job so it can give up its turn.
#: Without it the job stays parked until the model answers, and stop does not stop.
ABANDONED = "zeos-chat:abandoned"

#: What the reply pipe carries when an answer is complete.
#:
#: A streamed answer is a *sequence* of writes on one pipe, so the job needs to be told
#: where it ends -- otherwise its next blocking read waits for a chunk that is never
#: coming. This is the device saying "that is all", which is the only thing that can.
END = "zeos-chat:end"

#: Which sink belongs to which descriptor, and where that descriptor listens. A pair per
#: descriptor rather than one shared pair: two jobs may be waiting at once, and a reply
#: has to find the one that asked for it. Routing by pipe needs no correlation id and no
#: bookkeeping -- the pipe *is* the address.
LLM_PIPES: Mapping[str, tuple[PipeName, PipeName]] = {
    "converse": (PipeName("llm.converse.requests"), PipeName("llm.converse.replies")),
    "deep-research": (PipeName("llm.research.requests"), PipeName("llm.research.replies")),
}


def request_pipes() -> Mapping[PipeName, Ask]:
    """The sinks the adapter answers, by name."""
    return {
        requests: Ask(descriptor=descriptor, prompt="", reply_to=replies)
        for descriptor, (requests, replies) in LLM_PIPES.items()
    }


class StubModel:
    """A model that answers from a list. No key, no network, and deterministic.

    Which makes it the replacement for the case's old ``script:`` tapes: the control flow
    is now Python and therefore fixed, so the only thing left that could vary between two
    runs is what the model says -- and this says the same thing twice.
    """

    def __init__(self, answers: Mapping[str, str] | None = None) -> None:
        self.answers = dict(answers or {})
        self.asked: list[Ask] = []

    def __call__(self, ask: Ask) -> str:
        self.asked.append(ask)
        return self.answers.get(
            ask.descriptor,
            "I can help with that. | Tell me a little more about what you need.",
        )


def chunks_of(answer: str | Iterable[str]) -> Iterator[str]:
    """Whatever a model returned, as a sequence of chunks.

    A model may stream -- yielding pieces as it writes them -- or simply return the
    finished text, which is one chunk. The adapter does not care which, and a test that
    wants one answer should not have to pretend to stream to get it.
    """
    if isinstance(answer, str):
        if answer.strip():
            yield answer
        return
    for chunk in answer:
        if chunk:
            yield chunk


class LlmAdapter:
    """Drains request sinks, answers them off the kernel's thread, delivers the reply.

    The worker matters. Draining happens on the thread that steps the kernel, so calling
    the model there would put the several seconds back into the tick and undo the whole
    point; handing it to a worker keeps the tick short and lets the reply arrive later
    through the same door every other device event uses.
    """

    def __init__(
        self,
        model: Callable[[Ask], str | Iterable[str]],
        deliver: Callable[[PipeName, str], None],
        *,
        on_error: Callable[[Ask, Exception], None] | None = None,
        window_of: Callable[[str], str] | None = None,
        inline: bool = False,
    ) -> None:
        self._model = model
        self._window_of = window_of
        #: Answer on the calling thread instead of a worker. A worker is what keeps the
        #: tick short when the model is slow, but it also means the reply lands on
        #: whichever tick the thread happens to finish on -- so a run is reproducible only
        #: by luck of scheduling. A model fast enough not to need the thread should not
        #: pay that, and a replay should not depend on it.
        self._inline = inline
        self._deliver = deliver
        self._on_error = on_error
        self._routes = {requests: replies for requests, replies in LLM_PIPES.values()}
        self._descriptors = {requests: name for name, (requests, _) in LLM_PIPES.items()}
        #: Requests asked and neither answered nor abandoned, by ticket. A worker delivers
        #: only if its own ticket is still here, so a reply the person no longer wants is
        #: dropped rather than written out after they asked for it to stop.
        self._outstanding: dict[int, Ask] = {}
        self._next_ticket = 0
        self._lock = threading.Lock()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return len(self._outstanding)

    def handles(self, pipe: PipeName) -> bool:
        return pipe in self._routes

    def ask(self, pipe: PipeName, prompt: str) -> None:
        """Service one drained request. Returns at once; the answer arrives later."""
        descriptor = self._descriptors[pipe]
        request = Ask(
            descriptor=descriptor,
            prompt=prompt,
            reply_to=self._routes[pipe],
            window="" if self._window_of is None else self._window_of(descriptor),
        )
        with self._lock:
            self._next_ticket += 1
            ticket = self._next_ticket
            self._outstanding[ticket] = request
        if self._inline:
            self._answer(ticket, request)
            return
        threading.Thread(
            target=self._answer,
            args=(ticket, request),
            name=f"llm:{request.descriptor}",
            daemon=True,
        ).start()

    def abandon(self, descriptor: str) -> int:
        """Give up on whatever this descriptor asked for. Returns how many were dropped.

        The request is forgotten at once -- so the busy signal stops counting it, and the
        answer is discarded when it eventually arrives -- and the reply pipe is completed
        with ``ABANDONED`` so the job parked on it wakes up and abandons its turn.
        """
        with self._lock:
            tickets = [t for t, ask in self._outstanding.items() if ask.descriptor == descriptor]
            dropped = [self._outstanding.pop(t) for t in tickets]
        for request in dropped:
            self._deliver(request.reply_to, ABANDONED)
        return len(dropped)

    def _wanted(self, ticket: int) -> bool:
        with self._lock:
            return ticket in self._outstanding

    def _answer(self, ticket: int, request: Ask) -> None:
        """Pull the answer, chunk by chunk, until it ends or nobody wants it any more.

        Checking between chunks is what makes stop stop *the model* and not just the
        conversation: leaving the iterator early closes the stream, so the request is
        abandoned at the source rather than paid for and thrown away.
        """
        try:
            for chunk in chunks_of(self._model(request)):
                if not self._wanted(ticket):
                    return
                self._deliver(request.reply_to, chunk)
        except Exception as exc:  # noqa: BLE001 - the worker must not die silently
            # A failure goes back down the same pipe as an answer would. The job is parked
            # on that read, and a worker that died quietly would park it for ever; a job
            # that can read the failure can at least tell the person.
            if self._on_error is not None:
                self._on_error(request, exc)
            if self._wanted(ticket):
                self._deliver(request.reply_to, f"the model did not answer: {exc}")
        with self._lock:
            finished = self._outstanding.pop(ticket, None) is not None
        if finished:
            # Last, and only if nobody abandoned it: `abandon` sends its own sentinel, and
            # two would leave the job reading a chunk that never comes.
            self._deliver(request.reply_to, END)
