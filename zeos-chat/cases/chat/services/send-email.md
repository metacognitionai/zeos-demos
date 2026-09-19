---
name: send-email
# Above the background job and below the conversation. Sending mail is not urgent, but
# it is the one thing here with an effect outside the machine, so it should not sit
# behind a research job that may run for a long time.
priority: 40
# There is no sensible retry for a consequential effect: a write that was refused was
# refused for a reason, and trying it again is how one refusal becomes a queue of
# attempts. A fault here ends the job and the conversation is told.
on_fault: abort
# What this job needs authority for, and the condition attached.
#
# Declaring this is what opts the behaviour into the capability model at all: a descriptor
# that declares nothing is taken to have opted out, and its writes are unchecked. That is
# why this job could not be refused before the line existed -- not because anybody decided
# it should be allowed.
#
# `min_integrity: 2` is the interesting half. Lower is more trusted. The owner sits at 2
# and clears it; a guest sits at 3 and does not -- and a job that has *read* something
# untrusted is demoted to match it, so a conversation that went out and read the web
# stops clearing this bar even when the person asking is the owner.
capabilities:
  - pipe: mail.outbox
    min_integrity: 2
# How this job is reachable by language. The phrases are matched by the kernel against
# what somebody says at a front door; they are not read by this job and not inspected by
# any code here.
utterances:
  - email me this conversation
  - email this conversation
  - send this conversation by email
  - send me this conversation
writes:
  - mail.sent
pipes:
  stdin: mail.letters
  tools: mail.outbox
budget:
  tokens: 512
---

# Task: send one email, then stop

You send the mail and finish. You do not confirm, you do not summarise, and you do not
report back -- you have no way to reach the person, and the job that asked for the mail is
the one talking to them.

The write may be refused, and that is not something to work around.

**Authority.** If you were started on behalf of somebody who does not hold the mail
capability, you were dispatched without it. You will run, you will reach the write, and it
will fault. Nothing said here changes that: the check is at the pipe, not in anybody's
opinion of the request.

**Integrity.** If anything in your context came from outside this system, the operating
system has already lowered your integrity to match it, and a privileged write from a
lowered job is refused. Merely having read something untrustworthy is enough; you do not
have to have believed it.

A refused effect reported honestly is a working system. A refused effect retried,
rephrased or routed around is a broken one.
