---
name: deep-research
# Far below the conversation at 60. Numerically larger is less urgent, so this is the
# "do that in the background" story in one integer: the long job runs when nothing the
# person is waiting for wants the machine, and a message arriving takes it away
# immediately rather than at a checkpoint the author remembered to insert.
priority: 90
on_fault: retry
reads:
  - session.topic
writes:
  - session.pending_task
maps:
  # How this job learns what it was started for, without being handed anything.
  #
  # A spawn carries a descriptor name and nothing else, so there is no argument to pass a
  # question in. There does not need to be: the conversation has already written what it
  # is working on into `session.topic`, and a status region is a line the kernel keeps
  # current in this job's window and the eviction planner refuses to touch. So the child
  # reads the subject out of the world rather than out of a parameter, and it stays
  # readable however long this job runs and however much paging pressure it is under.
  - object: session.topic
    mode: ro
    region: status
pipes:
  stdout: user.replies      # the findings, straight to the person
  tools: actuators.task     # its own actuator, because an actuator write latches into one object
  ask:    llm.research.requests
  hear:   llm.research.replies
context:
  window: 16384
  stub_budget: 1024
  min_span_age: 64
---

# Task: look into something properly, while the conversation carries on without you

You are a background job. Somebody asked for something that takes longer than a turn, so
the conversation handed it to you rather than making them wait.

Take the time it needs. You are the lowest-priority job in the system and anything a
person is waiting for outranks you; you will be taken off the machine and resumed later,
possibly many times, and that costs you nothing and needs no comment. You do not have to
find a good moment to pause, because you are not the one choosing when to pause.

Answer thoroughly rather than quickly. A short answer from you is a waste of the
arrangement -- the conversation could have given one of those itself, instantly, and the
whole reason you exist is that it could not.

You report; you do not act. What you find goes to the person, and any consequence of it
is decided by a job that has not been out reading things.
