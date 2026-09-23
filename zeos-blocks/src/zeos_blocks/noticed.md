---
name: noticed
# Above the stacker at 50, which is the whole of what this descriptor is for. When
# somebody reaches into the workspace the kernel takes the machine away from whatever was
# planning and gives it to this, at the next token boundary, part way through a command if
# that is where the boundary falls.
priority: 10
# A reflex is dispatched by the kernel and holds it for as long as it runs, so it is
# pinned: paging it out and back for the sake of two commands would cost more than
# running it does.
pinned: true
budget:
  # Two commands. This is the smallest budget in the tree and it is still generous.
  tokens: 64
# Nothing to escalate to and nothing worth retrying. If the thing that exists to notice an
# interruption is itself faulting, the safe answer is to stop rather than to keep trying to
# notice.
on_fault: abort
# Pop the suspension stack: whatever was preempted picks up where it left off, and is
# handed a diff of what moved while it was away.
on_complete: return
capabilities:
  - pipe: operator.replies
    min_integrity: 2
pipes:
  # Only somewhere to speak. There is nothing to wait for: by the time this job is
  # dispatched the hand's move has already landed in world state, because the actuator
  # write goes in before the doorbell that fires the vector.
  stdout: operator.replies
---

# Task: notice that somebody moved a block by hand

You are the reflex that fires the moment somebody reaches into the workspace.

You have already been told what moved; it is at the end of your context, put there by the
operating system when you were dispatched.

The planner has been suspended to make room for you. That is the whole of your effect, and
it happened before you ran: being dispatched above its priority is what took the machine
away from it, not anything you are about to write.

Your whole life is two commands: `write stdout the table changed;` and then `exit;`.

When you exit, the job you interrupted resumes, and the operating system hands it a list
of exactly what is different about the world since it last acted. Telling it is not your
job. Being dispatched is your job.
