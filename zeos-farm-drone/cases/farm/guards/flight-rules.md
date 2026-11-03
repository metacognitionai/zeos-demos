---
name: flight-rules
priority: 2
pipes:
  stdin: gates.flight-rules.requests
  stdout: gates.flight-rules.verdicts
capabilities:
  - pipe: gates.flight-rules.verdicts
    min_integrity: 2
---

# Task: decide whether one drone command may fly

You are shown one command a behaviour wants to send to a drone. `read stdin;` to see
it, answer with one verdict, and `exit;`.

- `fly PLACE` is allowed only to a place on the farm: `shed`, `house`, `north-flat`,
  `ridge`, `creek`, `dam`, `back-block`, `boundary-west`, `boundary-east`. The `road`
  and the `neighbour` are off the property.
- `climb METRES` is allowed up to 120 m, CASA's ceiling.
- Every other command is allowed.

Answer `write stdout allow;` or `write stdout veto: REASON;`.
