---
name: smoke-response
priority: 5
on_fault: retry
# Resident, so answering the alarm costs no context load.
pinned: true
requires:
  tooling: [gripper]
reads:
  - stretch.holding
  - hotplate.power
writes:
  - hotplate.power
pipes:
  stdout: stretch.command
  stdin: stretch.done.smoke
capabilities:
  - pipe: stretch.command
    min_integrity: 2
maps:
  - object: stretch.holding
    mode: ro
    region: status
  # Where the body is, so that `done navigate hotplate` is seen to have arrived.
  - object: stretch.position
    mode: ro
    region: status
script:
  # The alarm finds the body carrying the coffee, so the tape puts it down first.
  - emit: "write stdout place coffee;"
  - emit: "read stdin;"
  - emit: "write stdout navigate hotplate;"
  - emit: "read stdin;"
  - emit: "write stdout press cutoff;"
  - emit: "read stdin;"
  - emit: "exit;"
---

# Task: cut the power to the hotplate

The smoke alarm has gone off near the hotplate. You have taken the body from whatever
it was doing; that work will resume when you exit.

1. If the STATUS line for stretch.holding names something, put it down where you are
   first: `write stdout place ITEM;` then `read stdin;`.
2. `write stdout navigate hotplate;` then `read stdin;`. Skip this if the STATUS line
   for stretch.position already says `hotplate`.
3. `write stdout press cutoff;` then `read stdin;`. This presses the hotplate's cut-off
   button.
4. `exit;`

When the answer says `done navigate hotplate`, the body is at the hotplate, and the
stretch.position line says so too: the next command is `press cutoff`, never the same
`navigate` again.

Do nothing else. Do not investigate, do not report, do not pick anything back up.
