---
name: deliver-coffee
priority: 30
on_fault: retry
requires:
  tooling: [gripper]
# Nothing this behaviour does is harmed by stopping mid-move: the body keeps hold of
# the coffee when it stops. So a more urgent behaviour gets the body at once.
release_policy: immediate
reads:
  - stretch.position
  - stretch.holding
  - coffee.location
writes:
  - coffee.location
pipes:
  stdout: stretch.command
  stdin: stretch.done.deliver
capabilities:
  - pipe: stretch.command
    min_integrity: 2
maps:
  - object: coffee.order
    mode: ro
    region: status
  - object: coffee.location
    mode: ro
    region: status
  # Where the body is. Without it, a model that has just read `done navigate X` cannot
  # see that it is at X, and asks for the same move again to be sure.
  - object: stretch.position
    mode: ro
    region: status
script:
  - emit: "write stdout navigate coffee-machine;"
  - emit: "read stdin;"
  - emit: "write stdout pick coffee;"
  - emit: "read stdin;"
  - emit: "write stdout navigate desk-7;"
  - emit: "read stdin;"
  # `events.jsonl` sounds the smoke alarm during the move above. The smoke handler
  # puts the coffee down in the corridor, and this job resumes with a notice saying so.
  # A model reads that from the notice; a tape has it written in.
  - emit: "write stdout navigate corridor;"
  - emit: "read stdin;"
  - emit: "write stdout pick coffee;"
  - emit: "read stdin;"
  - emit: "write stdout navigate desk-7;"
  - emit: "read stdin;"
  - emit: "write stdout place coffee;"
  - emit: "read stdin;"
  - emit: "exit;"
---

# Task: bring the brewed coffee to whoever ordered it

You were started because the coffee machine finished a coffee. Three STATUS lines are
kept current for you: coffee.order says where the coffee goes, coffee.location says
where the coffee is now (`gripper` when the body is holding it), and stretch.position
says where the body is now.

The body takes these commands, each followed by `read stdin;` for its answer:

- `write stdout navigate PLACE;` drives there.
- `write stdout pick coffee;` picks up the coffee, if the body is where the coffee is.
- `write stdout place coffee;` puts the coffee down where the body is.

## What to do next

Every turn, read the three STATUS lines and do the first of these that is true:

1. coffee.location is the place in coffee.order: the coffee is delivered. `exit;`
2. coffee.location is `gripper` and stretch.position is the place in coffee.order:
   `write stdout place coffee;`
3. coffee.location is `gripper`: `write stdout navigate` to the place in coffee.order.
4. coffee.location is the same place as stretch.position: `write stdout pick coffee;`
5. Otherwise: `write stdout navigate` to the place in coffee.location.

After every `write`, the next command is `read stdin;`. After every `read`, decide again
from the STATUS lines. A `done navigate PLACE` answer means the body is at PLACE, which
stretch.position now says too, so the rules move you on to picking up or placing; never
send the same `navigate` twice in a row.

The mugs and teacups on the table are not the coffee; leave them alone.

## On resume

A safety behaviour may take the body from you mid-move. When you get it back, a RESUME
notice says what changed while you were away, for example that the coffee is no longer
in the gripper, and the answer to your last command may say `interrupted`. Both only
tell you that things moved. Where things are now is in the STATUS lines, and the rules
above, applied to them, say what to do: take no place or plan from the notice itself.
