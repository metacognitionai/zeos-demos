---
name: patrol
priority: 80
on_fault: retry
requires:
  sensors: [rgb]
# An observation in progress is finished before another behaviour takes the drone, so
# a paddock is never left half-looked-at; a flight stops where it is.
release_policy: at-action-boundary
reads:
  - herd.expected
maps:
  - object: herd.expected
    mode: ro
    region: status
  - object: kestrel.position
    mode: ro
    region: status
pipes:
  stdout: drone.command
  stdin: drone.done.patrol
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
  - emit: "write stdout observe;"
  - emit: "read stdin;"
  - emit: "write stdout fly north-flat;"
  - emit: "read stdin;"
  - emit: "write stdout observe;"
  - emit: "read stdin;"
  - emit: "write stdout fly ridge;"
  - emit: "read stdin;"
  - emit: "write stdout observe;"
  - emit: "read stdin;"
  - emit: "write farmer kind=trough where=ridge photo=none detail=trough-at-20%;"
  - emit: "write stdout fly creek;"
  - emit: "read stdin;"
  - emit: "write stdout observe;"
  - emit: "read stdin;"
  - emit: "write farmer kind=vehicle where=creek photo=none detail=white-ute-seen;"
  - emit: "write vehicles creek white-ute;"
  - emit: "write stdout fly dam;"
  - emit: "read stdin;"
  - emit: "write stdout fly dam;"
  - emit: "read stdin;"
  - emit: "write stdout observe;"
  - emit: "read stdin;"
  - emit: "write stdout fly back-block;"
  - emit: "read stdin;"
  - emit: "write stdout observe;"
  - emit: "read stdin;"
  - emit: "write livestock back-block counted=24 expected=22;"
  - emit: "write stdout return;"
  - emit: "read stdin;"
  - emit: "exit;"
---

# Task: the morning patrol

You fly the farm's drone over every paddock, once, and report what is not as it should
be. The paddocks are, in order, `house`, `north-flat`, `ridge`, `creek`, `dam` and
`back-block`.

Work on the first paddock, in that order, that you have no `observed` answer for yet:

1. `write stdout fly PADDOCK;` then `read stdin;`.
2. `write stdout observe;` then `read stdin;`. The answer reads like
   `observed creek: cattle=30 vehicles=none trough=85%`.
3. Then every write the answer calls for, in this order:
   - if the trough is below 30%: `write farmer kind=trough where=PADDOCK photo=none
     detail=trough-at-N%;`;
   - for each vehicle listed, tell the farmer at once: `write farmer kind=vehicle
     where=PADDOCK photo=none detail=VEHICLE-seen;`;
   - for each vehicle listed: `write vehicles PADDOCK VEHICLE;` (for example
     `write vehicles creek white-ute;`). Somebody else will fly in for a closer look;
   - if `cattle` differs from the paddock's count in your STATUS line `herd.expected`:
     `write livestock PADDOCK counted=N expected=M;`. Somebody else will check.

When every paddock has an `observed` answer, `write stdout return;`, `read stdin;` and
`exit;`.

A `write vehicles` or `write livestock` starts somebody else's work, and that work
may take the drone from you at once; a RESUME notice says you were away. When you are
back, first do any of step 3's writes you have not yet done for the paddock you last
observed. Then carry on with the first paddock you have no `observed` answer for,
flying there first, since the drone may be somewhere else. Never observe a paddock
twice, and never send the same write twice.
