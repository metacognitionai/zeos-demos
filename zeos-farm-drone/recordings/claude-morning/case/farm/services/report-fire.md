---
name: report-fire
priority: 15
on_fault: retry
pipes:
  tools: alerts.fire
capabilities:
  - pipe: alerts.fire
    min_integrity: 2
utterances:
  - "there is a fire to the {direction}"
  - "check for smoke to the {direction}"
script:
  - emit: "write tools smoke unknown east;"
  - emit: "exit;"
---

# Task: pass on the farmer's fire report

The farmer reported a fire, and the direction arrived with you as the value of
`direction`, such as `east`. Raise the fire alert with the distance unknown:
`write tools smoke unknown DIRECTION;`, then `exit;`.
