---
name: vehicle-check
priority: 20
on_fault: retry
requires:
  sensors: [zoom]
release_policy: immediate
maps:
  - object: kestrel.position
    mode: ro
    region: status
pipes:
  stdout: drone.command
  stdin: drone.done.vehicle
  farmer: farmer.alerts
capabilities:
  - pipe: drone.command
    min_integrity: 2
  - pipe: farmer.alerts
    min_integrity: 2
    schema: alert
script:
  # The canonical morning (events.jsonl), command for command.
  - emit: "write stdout fly creek;"
  - emit: "read stdin;"
  - emit: "write stdout zoom vehicle;"
  - emit: "read stdin;"
  - emit: "write farmer kind=vehicle where=creek photo=p-0001 detail=white-ute-plate-XYZ123;"
  - emit: "write stdout fly road;"
  - emit: "write stdout fly boundary-west;"
  - emit: "read stdin;"
  - emit: "write stdout zoom vehicle;"
  - emit: "read stdin;"
  - emit: "write farmer kind=vehicle where=boundary-west photo=p-0002 detail=white-ute-on-the-road;"
  - emit: "exit;"
---

# Task: take a closer look at a vehicle

The patrol saw a vehicle that should not be in a paddock. Where, and what it looked
like, arrived with you: it is the text just after this task, such as `creek white-ute`.

1. `write stdout fly PADDOCK;` then `read stdin;`.
2. `write stdout zoom vehicle;` then `read stdin;`. The answer describes the vehicle,
   its plate and which way it is going, and names a photo such as `photo=p-0007`.
3. Tell the farmer: `write farmer kind=vehicle where=PLACE photo=PHOTO detail=WORDS;`,
   with the words joined by hyphens and at most 24 characters in all, such as `detail=white-ute-plate-XYZ123`.
4. If the vehicle is moving, follow it with `write stdout fly PLACE;` and look again. You may not
   leave the farm: if a flight is refused, go to the nearest boundary point
   (`boundary-west` or `boundary-east`) and look from there.

An alert always has all four fields, `kind`, `where`, `photo` and `detail`; use
`photo=none` when there is no photo.

Then `exit;`.
