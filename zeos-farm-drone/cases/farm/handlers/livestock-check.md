---
name: livestock-check
priority: 30
on_fault: retry
requires:
  sensors: [zoom]
release_policy: at-action-boundary
reads:
  - herd.expected
maps:
  - object: herd.expected
    mode: ro
    region: status
pipes:
  stdout: drone.command
  stdin: drone.done.livestock
  tools: herd.expected.set
  farmer: farmer.alerts
capabilities:
  - pipe: drone.command
    min_integrity: 2
  - pipe: herd.expected.set
    min_integrity: 2
  - pipe: farmer.alerts
    min_integrity: 2
    schema: alert
script:
  # The canonical morning (events.jsonl), command for command.
  - emit: "write stdout fly back-block;"
  - emit: "read stdin;"
  - emit: "write stdout zoom cattle;"
  - emit: "read stdin;"
  - emit: "write farmer kind=livestock where=back-block photo=p-0004 detail=2-new-calves;"
  - emit: "write tools house=12 north-flat=40 ridge=25 creek=30 dam=18 back-block=24;"
  - emit: "exit;"
---

# Task: find out why a paddock's count is wrong

The patrol counted a different number of cattle in a paddock than the farmer expects.
Which paddock, and both numbers, arrived with you: for example
`back-block counted=24 expected=22`.

1. `write stdout fly PADDOCK;` then `read stdin;`.
2. `write stdout zoom cattle;` then `read stdin;`. The answer says how many head there
   are, how many of them are calves, and whether any are strays, and names a photo.
3. Tell the farmer: `write farmer kind=livestock where=PADDOCK photo=PHOTO
   detail=WORDS;`, with the words joined by hyphens and at most 24 characters in all.
4. If calves explain the difference, the farmer now expects the new count there:
   write the whole of your STATUS line `herd.expected` back with that one paddock
   changed, as `write tools house=12 north-flat=40 ...;`. Strays do not change it.

An alert always has all four fields, `kind`, `where`, `photo` and `detail`; use
`photo=none` when there is no photo.

Then `exit;`.
