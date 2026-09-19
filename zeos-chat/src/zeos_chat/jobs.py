"""The jobs, as Python programs.

A ``CommandSource`` answers one question -- what is this job's next command? Asking a
model that question is what made the demonstration slow: every read, every write and every
decision to record the topic cost an API call, and the model was deciding control flow as
well as content. Scheduling belongs to a kernel and content belongs to a model, and this
is the line drawn where it was always supposed to be.

Each descriptor has a generator. It yields commands; the kernel decides when it runs and
what it may do. Nothing here can cheat: a generator that yields ``write mail ...;`` for a
pipe its descriptor does not bind is refused at the boundary exactly as a model would be,
and the priorities, the preemption and the capability checks are the kernel's throughout.
What the generator removes is the guessing, not the rules.

Where judgement is genuinely needed -- what to say, what the research found -- the job
asks the model the way it would ask any device: write a request, block on a reply.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

from zeos.core.framing import FRAMES
from zeos.core.ids import JobId
from zeos.machine.seat import Turn

from zeos_chat.abi import CHAT
from zeos_chat.llm import ABANDONED, END

__all__ = [
    "JobContext",
    "PROGRAMS",
    "ProgramSource",
    "from_the_kernel",
    "payload",
    "research_subject",
    "status_of",
    "wants_research",
]


def payload(text: str) -> str:
    """One command's text, made safe to be one command.

    The terminator cannot appear inside a payload: the parser ends the command at the
    first one and reads the rest as another. A model writes semicolons, so something has
    to take them out, and doing it here means a job cannot accidentally issue two commands
    by quoting a person. Newlines go the same way -- the seat splits on whitespace, so a
    paragraph is one run of words however it was laid out.
    """
    return " ".join(text.replace(CHAT.terminator, ",").split()) or "(nothing)"


@dataclass
class JobContext:
    """What a program knows: which descriptor it is, and what has arrived for it.

    Deliberately thin. A program may not read the world store, the scheduler or another
    job's state -- it sees what the kernel put in its window, which is the same rule a
    model in the same seat lives under.
    """

    descriptor: str
    job: JobId
    #: Everything injected into this job's window since it started, in order.
    arrivals: list[str] = field(default_factory=list[str])
    #: Set by the driver when the person has said stop. Checked between writes, because
    #: a job that is *running* cannot be woken by a device -- it is not waiting on one --
    #: and the kernel's own stack policies cannot reach it either (see PLAN-v2.md).
    abandoned: bool = False
    #: The job's context as the kernel currently holds it, refreshed every turn.
    #:
    #: This is the conversation. Not a copy of it kept here -- the kernel's own window,
    #: which is what makes the `context:` block in the descriptor mean something: when it
    #: fills, the pager evicts old turns to STUB markers and what the model can see
    #: shrinks accordingly. A history kept in Python would grow for ever behind the
    #: kernel's back and those declarations would be decoration.
    window: str = ""

    @property
    def arrival(self) -> str:
        """What the read that just returned produced.

        The *last* arrival is not that, and assuming it was is a bug this found the hard
        way. The kernel injects its own frames into a window too -- a resume notice after
        a preemption, a status region it refreshed -- and those land in the same list. A
        second job in the system is enough to have `converse` preempted around its read
        and resumed afterwards, at which point the last arrival is `<RESUME> Waited 29ms
        ...` and the conversation cheerfully took that for the person's message.

        So the kernel's frames are skipped and the last thing an actual pipe delivered is
        returned. `arrivals` still holds everything, in order.
        """
        return next((a for a in reversed(self.arrivals) if not from_the_kernel(a)), "")


def from_the_kernel(text: str) -> bool:
    """Whether an arrival is the kernel talking, rather than something a pipe delivered.

    Matched against ``zeos.core.framing.FRAMES``, so a frame added there is skipped here
    without this module being edited. Written as a prefix test rather than a pattern
    because the obvious regex needs a word-boundary escape, and this one was silently
    mangled into a literal backspace on the way into the file -- a pattern that compiled,
    printed correctly, and matched nothing.
    """
    head = text.lstrip()
    return any(head.startswith(f"<{name}>") or head.startswith(f"<{name} ") for name in FRAMES)


def stream_piece(arrival: str) -> tuple[str, bool]:
    """The text in one arrival, and whether the stream ended inside it.

    A blocking read takes *everything* waiting on the pipe, not one write, so several
    chunks and the end marker can arrive together. The marker is therefore something to
    look for inside an arrival rather than a message to compare it against -- the first
    version compared, never matched, and left the job parked for ever on a stream that had
    already finished.
    """
    ended = END in arrival or ABANDONED in arrival
    text = arrival.replace(END, " ").replace(ABANDONED, " ").strip()
    return text, ended


#: What a person says when they want something looked into properly rather than answered
#: in a turn. A phrase table rather than a model call, because asking the model whether to
#: ask the model is a round trip spent on a decision a person has already made explicit.
#: Crude on purpose: it is the recogniser, not the researcher.
RESEARCH_PHRASES = (
    "research ",
    "look into ",
    "looking into ",
    "dig into ",
    "deep dive",
    "investigate ",
    "find out everything",
    "do some research",
)


def research_subject(message: str) -> str:
    """What this message wants looked into, or "" if it wants an ordinary answer.

    The trigger phrase is taken off the front of the subject. "research the history of
    Kyoto" is a request about the history of Kyoto, and a job whose status line reads
    "looking into research the history of Kyoto" is quoting the instruction back rather
    than naming the work.
    """
    said = " ".join(message.split())
    lowered = said.lower()
    found = [(lowered.find(p), p) for p in RESEARCH_PHRASES if p in lowered]
    if not found:
        return ""
    at, phrase = min(found)
    # Rejoined on whitespace: cutting a phrase out of the middle leaves the space
    # before it and the space after it, and a doubled space goes into the status line.
    subject = " ".join(f"{said[:at]} {said[at + len(phrase) :]}".split()).strip(" ,.:;-")
    # A bare "do some research" names nothing; the conversation's topic is the best
    # available subject, and that is what the child will read anyway.
    return subject or said


def wants_research(message: str) -> bool:
    """Whether this message is asking for the long job rather than for an answer."""
    return bool(research_subject(message))


def status_of(window: str, obj: str) -> str:
    """The current value of a status region, read out of the job's own window.

    This is how a spawned job learns what it was started for. A spawn carries a descriptor
    name and nothing else, so there is no argument to read -- but the kernel keeps this
    line current in the window, which means the child can read the subject out of the
    world instead. The last occurrence wins: a status region is rewritten in place, and if
    an older copy is still visible the current one is the later of the two.
    """
    opening = f"<STATUS {obj}>"
    start = window.rfind(opening)
    if start == -1:
        return ""
    rest = window[start + len(opening) :]
    end = rest.find("</STATUS>")
    return " ".join((rest if end == -1 else rest[:end]).split())


def topic_of(message: str, words: int = 8) -> str:
    """A short subject line for a message.

    Derived here rather than asked of the model, and that is a latency decision worth
    naming: asking would be a second round trip, and a round trip is seconds. What the
    status region needs is something a person would recognise as "what we are working on",
    not a good summary.
    """
    return payload(" ".join(message.split()[:words])) or "a conversation"


def converse(ctx: JobContext) -> Iterator[str]:
    """The resident conversation. One model call per turn, and parked the rest of the time.

    Note where the two reads are. Between them the job holds nothing: it is `JobBlocked`
    on the model's reply, so the scheduler is free, a message can still fire its vector
    and the reflex can still preempt. That is the difference between a model behind a pipe
    and a model inside the machine.
    """
    while True:
        yield "read stdin;"
        message = ctx.arrival

        # Record the subject *before* answering. The recording is what survives an
        # interrupt; a half-written answer is not -- and when the long job is started
        # below, this line is the only way it learns what it is for.
        # The subject, with any "research ..." framing taken off: this line is both what
        # the person sees as the topic and what a spawned child reads as its brief.
        subject = research_subject(message)
        # A longer budget for a brief than for a status line. Eight words is plenty to
        # remind a conversation what it is doing; it cut "the Edo period" to "the Edo"
        # and handed that to the job as the thing to go and research.
        topic = topic_of(subject, words=24) if subject else topic_of(message)
        yield f"write tools {topic};"

        if subject:
            # Hand it off and go straight back to listening. The acknowledgement is
            # composed here rather than asked of the model, which is the whole point of
            # the arrangement: the person gets an answer immediately and the slow work
            # happens underneath at priority 90, where anything they do outranks it.
            #
            # `children:` is what makes this legal. A spawn naming anything else is a
            # capability fault at the kernel, not a check in this function.
            yield "spawn deep-research;"
            yield (
                f"write stdout Looking into {topic} now. "
                f"That runs underneath this conversation, so carry on -- "
                f"I will add what it finds as it arrives.;"
            )
            continue

        yield f"write ask {payload(message)};"

        # The answer arrives as a *stream*: a sequence of writes on the reply pipe, ending
        # with a sentinel. So the job reads in a loop, and passes each piece straight on to
        # the person -- which is what makes the words appear as they are written rather
        # than in one go at the end.
        #
        # This does not pretend to be streaming: a single write is still atomic, and a
        # job still cannot hand out an answer as it composes one. What it does is make the
        # writes small, so the granularity a person sees, and the granularity an
        # interruption cuts at, are both a word rather than a paragraph.
        while True:
            yield "read hear;"
            text, ended = stream_piece(ctx.arrival)

            # Whatever arrived is passed on, even when the stream ended in the same read:
            # a person keeps the words that were written before they stopped it.
            if text:
                yield f"write stdout {payload(text)};"
            if ended or ctx.abandoned:
                ctx.abandoned = False
                break


def acknowledge(ctx: JobContext) -> Iterator[str]:
    """Both handlers, and they are the same program: exist, then stop.

    `new-message` and `cancel` do their whole work in their frontmatter -- one returns to
    the conversation and one replaces it. Being dispatched is what took the machine away
    from whatever was running, and that is the entire job. A command here would be seconds
    of a person's time spent saying so.
    """
    yield "exit;"


def deep_research(ctx: JobContext) -> Iterator[str]:
    """The long job, and the only one that stays genuinely slow.

    That is the point rather than a shortcoming: at priority 90 it can spend as long as it
    likes while the conversation stays responsive above it. A demonstration where
    everything is slow cannot show the contrast the design is about.

    Nothing is handed to this job when it starts. It reads what it is for out of
    `session.topic`, which the conversation wrote before spawning it and which the kernel
    keeps current in this job's window -- so the subject survives however long the job
    runs and whatever the pager does to the rest of its context.
    """
    subject = status_of(ctx.window, "session.topic") or "the current conversation"

    # The pending task, in the world where the conversation can see it. It maps
    # `session.pending_task` read-only, so this is how it learns about work it dispatched
    # without holding any handle to the job doing it.
    yield f"write tools {payload('looking into ' + subject)};"

    yield f"write ask research this thoroughly and report what you find: {payload(subject)};"

    # The heading waits for the first words rather than announcing them. Written up front
    # it said "Here is what I found on X." and was then followed by several minutes of
    # nothing while the model thought -- a promise the job had not yet kept.
    announced = False
    while True:
        yield "read hear;"
        text, ended = stream_piece(ctx.arrival)
        if text:
            if not announced:
                announced = True
                yield f"write stdout Here is what I found on {payload(subject)}.;"
            yield f"write stdout {payload(text)};"
        if ended or ctx.abandoned:
            ctx.abandoned = False
            break

    # Clear the pending task before finishing. A job that ends leaving "looking into X" in
    # the world tells the conversation it is still working, for ever.
    yield "write tools none;"
    yield "exit;"


def send_email(ctx: JobContext) -> Iterator[str]:
    """One write, then stop. No model call: what to send was decided by whoever asked.

    The write is the only consequential effect in the system, and therefore the one most
    likely to be refused -- by authority, or because this job has read something it should
    not have. A refusal is not something to work around; the job ends and the driver says
    so on the page.

    What to send is *read*, not handed over. The vector that dispatched this job carries
    the written text as a payload, but a payload injected at job start is the prompt
    rather than an arrival -- a model in the seat would read it, a program watching its
    arrivals never sees it, and this wrote "nothing was asked for". So the doorbell says
    only that somebody asked and the letter comes off a pipe, which is the same split
    `user.arrivals` and `user.messages` already make.
    """
    yield "read stdin;"
    yield f"write tools {payload(ctx.arrival)};"
    yield "exit;"


Program = Callable[[JobContext], Iterator[str]]

#: One program per descriptor. The counterpart of the descriptor file, and the reason a
#: behaviour now spans a contract and an implementation rather than living in one file.
PROGRAMS: Mapping[str, Program] = {
    "converse": converse,
    "new-message": acknowledge,
    "cancel": acknowledge,
    "deep-research": deep_research,
    "send-email": send_email,
}


class ProgramSource:
    """A command source whose next command comes from Python rather than from a model."""

    def __init__(self, programs: Mapping[str, Program] | None = None) -> None:
        self._programs = dict(programs or PROGRAMS)
        self._running: dict[JobId, tuple[JobContext, Iterator[str]]] = {}

    def window_of(self, descriptor: str) -> str:
        """The current context of the job running this descriptor.

        The adapter needs it, and the *job* must not be the one to supply it: a job's
        writes are decoded into its own window, so writing the conversation out each turn
        would make the window grow by its own length every time. The job asks with the new
        message only; the history comes from here.
        """
        for context, _ in self._running.values():
            if context.descriptor == descriptor:
                return context.window
        return ""

    def abandon(self, descriptor: str) -> None:
        """Tell this descriptor's job to give up its turn.

        Needed as well as the device's own answer, because the two states a conversation
        can be stopped in are different. Parked on the model, it is woken by the reply
        pipe carrying `ABANDONED`. Part way through writing an answer out, it is not
        waiting on anything and nothing can wake it -- so it reads this between writes.
        """
        for context, _ in self._running.values():
            if context.descriptor == descriptor:
                context.abandoned = True

    def note_arrival(self, job: JobId, text: str) -> None:
        """Called by the seat when the kernel injects something into this job's window.

        This is the only way a program learns anything. It cannot ask the kernel and it
        cannot read the world; what the kernel put in the window is what there is.
        """
        context, _ = self._running.get(job, (None, None))
        if context is not None:
            context.arrivals.append(text)

    def next_command(self, turn: Turn) -> str:
        program = self._programs.get(turn.descriptor)
        if program is None:
            raise KeyError(
                f"no program for descriptor {turn.descriptor!r}; every descriptor in the "
                f"case needs one in PROGRAMS, or the kernel will dispatch a job with "
                f"nothing to run"
            )
        if turn.job not in self._running:
            context = JobContext(descriptor=turn.descriptor, job=turn.job)
            self._running[turn.job] = (context, program(context))
        context, commands = self._running[turn.job]
        context.window = turn.transcript
        try:
            return next(commands)
        except StopIteration:
            # A program that runs off its end has not finished the job -- only `exit;`
            # does that -- so the kernel would ask again for ever. Loudly, at once.
            raise RuntimeError(
                f"the program for {turn.descriptor!r} returned without issuing 'exit;'"
            ) from None
