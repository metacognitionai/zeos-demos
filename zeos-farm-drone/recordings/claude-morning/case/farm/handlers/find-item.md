---
name: find-item
priority: 40
on_fault: retry
requires:
  sensors: [rgb]
release_policy: immediate
reads:
  - herd.expected
maps:
  - object: herd.expected
    mode: ro
    region: status
pipes:
  stdout: drone.command
  stdin: drone.done.find
  vehicles: findings.vehicle
  livestock: findings.livestock
  farmer: farmer.alerts
capabilities:
  - pipe: drone.command
    min_integrity: 2
  - pipe: findings.vehicle
    min_integrity: 2
  - pipe: findings.livestock
    min_integrity: 2
  - pipe: farmer.alerts
    min_integrity: 2
    schema: alert
script:
  # The canonical morning (events.jsonl), command for command.
  - emit: "write stdout fly house;"
  - emit: "read stdin;"
  - emit: "write stdout search motorbike;"
  - emit: "read stdin;"
  - emit: "write stdout fly north-flat;"
  - emit: "read stdin;"
  - emit: "write stdout search motorbike;"
  - emit: "read stdin;"
  - emit: "write farmer kind=item where=north-flat photo=p-0005 detail=motorbike-found-here;"
  - emit: "exit;"
---

# Task: find something the farmer has lost

The farmer asked you to find something. What it is arrived with you, such as
`item=motorbike`; ITEM below is the part after `item=`.

Search the paddocks one at a time, nearest the shed first: `house`, `north-flat`,
`creek`, `ridge`, `dam`, `back-block`.

1. `write stdout fly PADDOCK;` then `read stdin;`.
2. `write stdout search ITEM;` then `read stdin;`. The answer is either `found ITEM at
   PADDOCK: photo=PHOTO ...` or `no ITEM at PADDOCK: ...`, and either way it ends with
   what the search took in of the paddock, such as `cattle=30 vehicles=none
   trough=85%`.
3. Whatever you were looking for, report what you saw, in this order:
   - if the trough is below 30%: `write farmer kind=trough where=PADDOCK photo=none
     detail=trough-at-N%;`;
   - for each vehicle listed, tell the farmer at once: `write farmer kind=vehicle
     where=PADDOCK photo=none detail=VEHICLE-seen;`;
   - for each vehicle listed: `write vehicles PADDOCK VEHICLE;`. Somebody else will
     fly in for a closer look, and may take the drone from you to do it;
   - if `cattle` differs from the paddock's count in your STATUS line `herd.expected`:
     `write livestock PADDOCK counted=N expected=M;`. Somebody else will check.
4. When it is found, tell the farmer: `write farmer kind=item where=PADDOCK
   photo=PHOTO detail=WORDS;`, with the words joined by hyphens and at most 24
   characters in all, and `exit;`.

If it is in none of them, tell the farmer so:
`write farmer kind=item where=nowhere photo=none detail=ITEM-not-found;`, then `exit;`.

An alert always has all four fields, `kind`, `where`, `photo` and `detail`; use
`photo=none` when there is no photo.
