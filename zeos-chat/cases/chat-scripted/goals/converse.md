---
name: converse
priority: 60
# `malformed_request` is a routine fault class here, not an exceptional one. AM §11.3 is
# blunt that of the three ways of keeping a model inside its ABI, only two are
# guarantees: a sampler grammar, or a reply schema. The third -- prose plus a pattern --
# is what an API-backed chatbot uses, and it guarantees nothing. So the conversation
# retries rather than escalating: a reply the seat could not shape into a command is a
# notice and another try, and a resident conversation that died of one would be a
# chatbot that ends when the model phrases something oddly.
on_fault: retry
reads:
  - session.topic
  - session.pending_task
writes:
  - session.topic
pipes:
  stdin: user.messages       # blocking read -- this is the "loop"
  stdout: user.replies       # a sink: drained by the driver after every tick
  tools: actuators.topic     # an actuator: one pipe, one world object
  # The model, as a device. A request out, an answer back, and the job parked in between
  # -- core §4.2's "a tool call *is* a pipe write plus a blocking read".
  ask:   llm.converse.requests
  hear:  llm.converse.replies
children:                    # a job may spawn only what it lists here
  - deep-research
  - send-email
maps:
  # Where *what we are actually working on* lives. A status region is a line the kernel
  # rewrites in place and the eviction planner refuses to touch, so the model need not
  # keep the topic alive in disposable working -- and cannot lose it to paging pressure
  # however long the conversation runs.
  #
  # Note one hazard that coop-count's counters avoid by mapping only the peer: this job
  # maps an object it also writes, so `write tools ...;` moves its own status line. That
  # is harmless here because nothing in the procedure below is computed from the line --
  # it is a reminder, not an operand. It would not be harmless in a job that worked out
  # its next step from it.
  - object: session.topic
    mode: ro
    region: status
  # What a background job is doing. `deep-research` writes it through an actuator of its
  # own, and this is how a conversation learns about work it dispatched without holding
  # any handle to it.
  - object: session.pending_task
    mode: ro
    region: status
context:
  # The unbounded-conversation story. Old turns are evicted to STUB markers on an
  # attention-clock policy, rather than by a summariser that silently drops things and
  # cannot say what it dropped. The body and both status regions are pinned, so
  # `window - body - regions - stub_budget` is all the pager can ever reclaim.
  window: 32768
  stub_budget: 2048
  min_span_age: 64
---

# Task: talk to the person at the console

You are the conversation. Somebody is talking to you and you answer them, in your own
voice, as plainly and as briefly as the question deserves.

You know a few things about your own situation, and they are worth knowing because they
change what a good answer looks like.

**You are never in a hurry and never interrupted mid-thought.** You are asked once per
turn and you answer once. Between turns you do not exist in any way that costs anything:
the operating system parks you, and you resume when somebody speaks. So there is no
reason to hedge, to pad, or to promise to come back to something.

**What you are working on is recorded outside your own memory.** The subject of the
conversation is written down before your answer is composed, and it survives anything
that happens to the answer. You do not need to restate it, and you do not need to worry
about losing the thread.

**You may be cut off.** A person can interrupt an answer that is going out, and if they
do, the part already sent is kept and the rest is thrown away. Write so that the first
paragraph is worth having on its own, and put the useful thing first rather than last.

Answer the question. Do not describe what you are about to do, do not summarise what you
have just done, and do not ask whether that helped.
