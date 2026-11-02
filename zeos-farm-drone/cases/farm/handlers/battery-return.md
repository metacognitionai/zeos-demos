---
name: battery-return
priority: 5
on_fault: retry
requires:
  locomotion: rotor
# A drone whose battery is about to fail stops whatever it is doing.
release_policy: immediate
pipes:
  stdout: drone.command
  stdin: drone.done.battery
  farmer: farmer.alerts
capabilities:
  - pipe: drone.command
    min_integrity: 2
  - pipe: farmer.alerts
    min_integrity: 2
    schema: alert
script:
  # The canonical morning has no flat battery; these are the commands of one.
  - emit: "write stdout return;"
  - emit: "read stdin;"
  - emit: "write farmer kind=battery where=shed photo=none detail=battery-12%-home-at-shed;"
  - emit: "exit;"
---

# Task: bring the drone home on a flat battery

The drone's battery is critically low. What is known arrived with you: the drone and
its charge, such as `kestrel battery=12%`.

1. Fly straight back to the launch pad in the shed: `write stdout return;` then
   `read stdin;`. Do nothing else first.
2. Tell the farmer: `write farmer kind=battery where=shed photo=none detail=WORDS;`,
   with the words joined by hyphens and at most 24 characters in all, such as
   `battery-12%-home-at-shed`.

Then `exit;`. The battery is swapped on the pad, and whatever the drone was doing
before carries on.
