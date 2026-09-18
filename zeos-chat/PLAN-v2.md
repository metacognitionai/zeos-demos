# The model is a device, not the machine

A redesign of how the chat case is *implemented*. The case's contract does not change:
same priorities, same pipes, same vectors, same capability boundaries. What changes is
who decides the control flow, and where the model sits.

## Why

Measured on the built demo: a turn cost four API calls at ~2.2s each, ~7.4s before the
person saw a word, and **the kernel was frozen inside `decode()` for all of it**. The
model was not just writing the replies, it was deciding when to read, when to write and
when to record the topic -- which is the opposite of what ZEOS claims, that scheduling
belongs to a kernel and not to the model.

## The two changes

**1. Jobs are Python programs.** A `ProgramSource` holds one generator per descriptor and
advances it once per `next_command`. Control flow becomes microseconds.

**2. The model is reached through a pipe.** A job writes a request and blocks on a reply;
a driver adapter services the request on a worker thread. This is not an invention -- core
§4.2 already says *"a tool call **is** a pipe write plus a blocking read; the job is
descheduled and costs nothing"*. It is the first time this demo has taken it literally.

The second matters more than the first. A job waiting on the model becomes `JobBlocked`,
so the kernel keeps ticking, other jobs run, and the conversation stays live while it
waits. Today nothing can happen at all during an API call.

**Neither needs a kernel change.** Sink pipes and `deliver` are the whole mechanism, and
both already exist.

## The three-way split

- **frontmatter** -- the contract the kernel enforces: priority, pipes, capabilities,
  maps, children. Unchanged.
- **body** -- the persona handed to the model for *content*. No syscall vocabulary at all;
  the bodies get shorter and much better.
- **Python** -- the control flow. Deterministic, instant, and testable without a key.

The cost, stated rather than glossed: a behaviour now spans a contract and an
implementation. "One behaviour, one file" becomes "one behaviour, one contract, one
implementation".

## What each job becomes

```python
def converse(job):
    while True:
        yield "read stdin;"  # parked; costs nothing
        yield f"write ask {job.arrival};"  # ask the model
        yield "read hear;"  # parked again; the kernel runs on
        answer = job.arrival
        yield f"write tools {answer.topic};"
        for paragraph in answer.paragraphs:
            yield f"write stdout {paragraph};"


def new_message(job):
    yield "exit;"  # no model call; microseconds


def cancel(job):
    yield "exit;"  # likewise; its whole effect is its on_complete
```

`deep-research` stays model-driven and therefore slow -- which is the point. At priority
90 it can grind through several calls while `converse` stays responsive, and the demo
finally shows the background story it was built to show.

## The wiring

```
system/pipes.yaml     llm.converse.requests  (sink)     llm.converse.replies  (device)
                      llm.research.requests  (sink)     llm.research.replies  (device)
src/zeos_chat/jobs/   converse.py, handlers.py, research.py, the ProgramSource
src/zeos_chat/llm.py  the adapter: drains a request sink, calls the model on a worker
                      thread, delivers the answer back through the kernel
```

One request/reply pair per descriptor rather than one shared pair, so replies route by
pipe and it keeps working while `converse` and `deep-research` are both waiting.

`Session` needs almost nothing: `on_reply(pipe, text)` already fires for every drained
sink, and `deliver` is already safe from another thread. Routing is a two-line branch.

## What it bought, measured

| | before | after |
| --- | --- | --- |
| model calls per turn | 4 | **1** |
| first words | ~7.4s | **2.2s** |
| ticks for the scripted run | 333 | **95** |
| handlers | 2 calls, ~4s | **0 calls** |

One caveat worth stating: a turn is now *entirely* the model's own latency, and that
varies. Of two consecutive real turns, one answered in 2.2s and the next took 7.5s for
the same single call. The structural overhead is gone; what is left is not ours to
control.

## What it bought that is not a number

The kernel is awake while the model thinks. A reflex fired during generation is acted on
at once rather than after the model has finished composing something nobody wants -- and
there is a test for exactly that, which could not have been written before.

The demonstration also stops lying about `JobBlocked`. The conversation genuinely spends
almost its whole life parked, so "waiting is free" is visible rather than asserted. One
consequence to be honest about: **preemption of the conversation is now rare**, because a
job that is parked is not a job that can be preempted. The interesting interruption is no
longer "the handler took the machine" but "the reflex acted while the model was still
thinking".

## What goes

**Model-as-machine mode is discarded.** With it go the ABI-as-prose system prompt,
`one_command`, the `malformed_request` fault class in practice, and the body-versus-ABI
lint rules having anything to check. That is a real loss: it was a genuine demonstration
of the ZEOS seat, and the note's §4 is largely about it. It is being spent deliberately,
for a demo that a person can stand in front of.

**The tapes go too.** `script:` blocks exist to stand in for a model that is not there;
with a Python implementation and a stub model there is nothing left for them to stand in
for, and determinism comes from the stub instead. The descriptors get much shorter.

## Steps

1. **The adapter.** `llm.py` plus the pipes, provable against a stub with no key.
2. **The Python jobs.** `ProgramSource` and the generators; drop `TapeSource` and the
   `script:` blocks.
3. **The bodies.** Rewrite as personas rather than as syscall tutorials.

Each step keeps the case lint-clean and the suite green.

## Done

All three. 47 tests, lint clean, and the scripted run still replays byte-identically --
though that last one needed a fix of its own: the reply arrives from a worker thread, so
it lands on whichever tick the thread finishes on, and a run was reproducible only by luck
of scheduling. A stub answers in line instead, which makes it reproducible by
construction. The worker is kept for anything slow enough to need it.


## A gap this found: a reflex cannot cancel a blocked job

Pressing stop did not stop. Three symptoms, one cause.

`on_complete: replace-with: converse` reads exactly right -- the half-written answer is
void, so replace the conversation with a fresh one. It clears the **suspension** stack,
and `Scheduler.clear_stack` is only that stack. A job parked on a pipe was descheduled by
*blocking*, not by preemption, so it is not there. A conversation is parked almost always.

Measured, before the fix:

- pressing stop while idle **spawned a second conversation and left the first alive** --
  three live `converse` jobs after two presses, all blocked on the same pipe, none
  cancelled;
- pressing stop while the model was thinking left the request in flight, so the logo kept
  animating and **the answer was delivered to the person after they had declined it**;
- the only case that worked was stop during a write, and only because the preempted job
  happened to be on the suspension stack.

**The workaround, which is what a driver does with an aborted read.** The adapter forgets
the request at once -- so the busy signal stops counting it and the late answer is
discarded -- and completes the reply pipe with `ABANDONED`, which wakes the job so it can
give up its turn. A job that is *running* is not waiting on anything and cannot be woken,
so it reads a flag between writes instead. `cancel` drops to `on_complete: return`,
because a replacement it cannot remove is worse than none.

**The real fix is a kernel one** and is worth a number of its own: a stack policy that can
reach a job which is blocked rather than suspended, or an explicit "cancel this job"
available to a handler. Every application whose jobs spend their lives blocked on a device
has this, and a chat application is only the first to notice.

Verified live: stop pressed two seconds into a long generation takes the busy signal out
at once and delivers nothing.
