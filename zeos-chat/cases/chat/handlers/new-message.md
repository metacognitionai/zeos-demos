---
name: new-message
priority: 10
# Resident, so that dispatching it later costs only the payload. Pinning a *vector*
# handler is not the same mechanism as pinning a goal: `Kernel.spawn` only parks a job
# in PINNED_IDLE when it has no vector, so this one is READY the moment the vector fires.
# What pinning buys here is the prefill, and what it does not buy is genuine residency
# across a real serving stack -- that is roadmap rather than repository.
pinned: true
budget:
  # A couple of hundred tokens. A handler that can outgrow this is not a handler.
  tokens: 256
on_fault: retry
# **A handler cannot choose its completion policy per firing.**
# What a message ought to do to a half-written answer depends on the message: carry on,
# drop the follow-up, or abandon the answer entirely -- `return`, `cancel-below:1` and
# `replace-with: converse` respectively. A descriptor declares one `on_complete`, and
# nothing in the kernel lets a handler choose at run time, so this file picks the
# common case and `cancel.md` carries the other.
#
# `return` is the follow-up: let the answer that is already going out finish, and resume
# it with a RESUME diff naming what changed. The conversation then decides for itself
# whether the new message voided what it was writing -- which is where that decision
# belongs, since it is a judgement about meaning and this handler is not allowed to
# make one.
on_complete: return
---

# Task: notice that the person has spoken, and get out of the way

You are an interrupt handler, and you have no words.

You exist because somebody spoke while something else was running. Being dispatched is
what took the machine away from the job that was running -- that is your whole effect,
and it has already happened by the time anything here could be read. The operating system
then resumes what you interrupted and tells it what changed.

**You do not answer the message.** You cannot even see it: the words went to the
conversation, which is the job that will read them. Deciding whether a half-written
answer has been made pointless by a new one is a judgement about what a person meant, and
it belongs to the job holding that answer, not to you.

This file exists so that a reader of the tree can see what is at priority 10 and why.
Nothing needs to read it at run time.
