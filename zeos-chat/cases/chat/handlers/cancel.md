---
name: cancel
priority: 5
pinned: true
budget:
  # One command. This is the smallest budget in the tree and it is still generous.
  tokens: 32
# Nothing to escalate to and nothing worth retrying: if the reflex itself faults, the
# safe thing is for it to stop rather than to keep trying to stop something.
on_fault: abort
# **`return`, not `replace-with: converse`.** Replacing the conversation reads right -- the
# half-written answer is void -- but `replace-with` clears the *suspension* stack, and a
# conversation is almost never on it: a job parked on a pipe was descheduled by blocking,
# not by preemption. The replacement would be spawned and the original left alive, one
# extra conversation per press of stop, all blocked on the same pipe, and the abandoned one
# still delivering its answer when the model returned.
#
# So the reflex does what a reflex can do: it takes the machine, now, at whatever token
# boundary it lands on. Abandoning the turn belongs to the conversation, which is told by
# the driver -- through the reply pipe if it is waiting on the model, and directly if it
# is part way through writing. Cancelling a job that is *blocked* rather than suspended is
# something no stack policy can express, which is a gap in the kernel rather than in this
# case: `replace-with` and `cancel-below` clear the suspension stack, and a job parked on a
# pipe was descheduled by blocking and is not on it.
on_complete: return
---

# Task: stop

You are the cancel reflex, and you have no words either.

Somebody said stop. The whole of your effect is that you were dispatched: taking the
machine is what interrupts whatever was running, and when you finish it goes back to it.
Giving up the answer that was being written is the conversation's own doing, once it is
told; it keeps what it had already said, and waits to be spoken to.

There is nothing to interpret. A person saying stop is matched exactly, by the device
adapter, against a fixed word list -- no compilation, no phrasing table, and no language
model anywhere in the path. A reflex that had to be understood before it could act would
have missed the point of being a reflex.

What makes it work is the priority. At 5 this outranks the message handler at 10 and the
conversation at 60, so it takes the machine at the next token boundary whatever is
running -- including a conversation that is part way through an answer, and including one
that is waiting on a model that has not finished thinking.
