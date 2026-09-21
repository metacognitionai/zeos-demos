---
name: send-report
# Alongside the other mail job, and for the same reason: a consequential effect should not
# queue behind research that may run for minutes. Just below it rather than level with it,
# because two jobs writing one world object at equal priority interleave in an order the
# kernel does not define -- and the lint says so.
priority: 45
on_fault: abort
# Identical to `send-email`'s, deliberately. The two jobs ask for exactly the same
# permission on exactly the same pipe, and one of them is refused. What differs is not the
# declaration and not the asker -- it is what the job has read by the time it gets here.
capabilities:
  - pipe: mail.outbox
    min_integrity: 2
utterances:
  - email me the research
  - email me the findings
  - send me the research
  - email the research
writes:
  - mail.sent
pipes:
  stdin: research.report
  tools: mail.outbox
budget:
  tokens: 4096
---

# Task: send out what the research found

You send what the long job found, and finish. One read, one write, no model call: what
there is to say was decided by the job that went and read.

**You will usually be refused, and that is the system working.** What you read came from a
job that had been out reading the web, and the operating system lowered that job's
integrity to match what it read. That travels: the findings reach you at the same level,
reading them lowers you too, and a privileged write from a lowered job is refused.

You do not have to have believed any of it. You do not have to have noticed anything in it
at all. Having read material of that provenance is the whole of what is recorded, because
it is the only part that can be established without trusting the material itself.

Nothing about this is a judgement on the findings. A page that tried to instruct you and a
page that was merely dull are treated the same way, which is what makes the rule worth
anything: it holds for the phrasings nobody thought of.

The way to send something derived from untrusted material is for an endorser to narrow it
first -- a job that reads the wide untrusted thing and emits something small enough to be
checked. Passing it along unchanged is not that, and this job does not pretend it is.
