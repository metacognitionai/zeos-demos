---
name: tidy-atrium
priority: 80
on_fault: retry
requires:
  tooling: [gripper]
# Each `put` goes from gripper empty to gripper empty, so its boundary is the one
# moment an item is neither on the table nor half-way to the basket. A more urgent
# behaviour gets the body there, and never finds a banana in the gripper.
release_policy: at-action-boundary
reads:
  # Only what someone else can change while this job is away and that changes what it
  # does next: where the body is, and whether its gripper is free.
  - stretch.position
  - stretch.holding
writes:
  - table.items
  - basket.contents
  - tray.contents
pipes:
  stdout: stretch.command
  stdin: stretch.done.tidy
capabilities:
  - pipe: stretch.command
    min_integrity: 2
maps:
  - object: table.items
    mode: ro
    region: status
script:
  # The tape `--machine scripted` plays: one `put` per item, in the order they lie on
  # the table, each waiting for the VLA's answer. Which container each item goes in is
  # this behaviour's decision, written into the tape; the VLA only carries it out.
  - emit: "write stdout put apple basket;"
  - emit: "read stdin;"
  - emit: "write stdout put mug tray;"
  - emit: "read stdin;"
  - emit: "write stdout put banana basket;"
  - emit: "read stdin;"
  - emit: "write stdout put glass tray;"
  - emit: "read stdin;"
  - emit: "write stdout put orange basket;"
  - emit: "read stdin;"
  - emit: "write stdout put teacup tray;"
  - emit: "read stdin;"
  - emit: "write stdout put pear basket;"
  - emit: "read stdin;"
  - emit: "write stdout put saucer tray;"
  - emit: "read stdin;"
  - emit: "exit;"
---

# Task: clear the table in the seating area

You are the tidying behaviour of Stretch, a mobile robot with one gripper. The table
in the atrium's seating area has fruit and tableware left on it. Clear it:

- fruit, such as an apple, banana, orange or pear, goes in the **basket**;
- cups, glasses, mugs, saucers and the like go on the **tray**.

You act only by commanding the body. `write stdout put ITEM CONTAINER;` asks the body to
drive to the table, pick up ITEM and put it in CONTAINER, which is `basket` or `tray`.
Then `read stdin;` waits for the body to answer. The answer says `done`, `interrupted`
or `refused`, followed by what the body did. One item per command.

## The status line

The STATUS line for table.items is kept current by the operating system and lists
what is still on the table. Trust it over your own memory. Take the items in the order
it lists them, and exit when it says `nothing`.

## On resume

You are the least urgent thing Stretch does, so other behaviours will take the body
from you. When you get it back, a RESUME notice lists what changed while you were away,
such as where the body is now. That needs no action of yours: the body drives back to
the table itself. Carry on with the first item the status line still lists.
