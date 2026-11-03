---
name: fire-watch
priority: 15
on_fault: retry
requires:
  sensors: [panorama]
release_policy: immediate
pipes:
  stdout: drone.command
  stdin: drone.done.fire
  farmer: farmer.alerts
capabilities:
  - pipe: drone.command
    min_integrity: 2
  - pipe: farmer.alerts
    min_integrity: 2
    schema: alert
script:
  # The canonical morning (events.jsonl), command for command.
  - emit: "write stdout climb 180;"
  - emit: "write stdout climb 120;"
  - emit: "read stdin;"
  - emit: "write stdout panorama;"
  - emit: "read stdin;"
  - emit: "write farmer kind=fire where=north-west photo=p-0003 detail=smoke-12km-north-west;"
  - emit: "write stdout climb 40;"
  - emit: "read stdin;"
  - emit: "exit;"
---

# Task: see how far away a fire is

A fire was reported near the farm. What is known arrived with you: an alert such as
`smoke 12km north-west`, or `smoke unknown east` when the farmer reported it and the
distance is not known.

1. Climb high enough to see over the ridges: `write stdout climb METRES;` then
   `read stdin;`. A 360-degree view needs at least 100 m.
2. `write stdout panorama;` then `read stdin;`. The answer says where the smoke is, how
   far, and which way it is moving, and names a photo.
3. Tell the farmer: `write farmer kind=fire where=DIRECTION photo=PHOTO detail=WORDS;`,
   with the words joined by hyphens and at most 24 characters in all.
4. Come back down to 40 m: `write stdout climb 40;` then `read stdin;`.

An alert always has all four fields, `kind`, `where`, `photo` and `detail`; use
`photo=none` when there is no photo.

Then `exit;`.
