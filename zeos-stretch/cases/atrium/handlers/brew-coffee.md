---
name: brew-coffee
priority: 40
on_fault: retry
requires:
  tooling: [gripper]
reads:
  - stretch.position
writes:
  - coffee.order
  - coffee.machine
pipes:
  stdout: stretch.command
  stdin: stretch.done.brew
  tools: coffee.order.set
capabilities:
  - pipe: stretch.command
    min_integrity: 2
  - pipe: coffee.order.set
    min_integrity: 2
script:
  # The order arrived with the interrupt that started this job: `desk-7`.
  - emit: "write tools desk-7;"
  - emit: "write stdout navigate coffee-machine;"
  - emit: "read stdin;"
  - emit: "write stdout brew;"
  - emit: "read stdin;"
  - emit: "exit;"
---

# Task: start a coffee for whoever asked

You were started because someone asked for a coffee. Where to bring it arrived with
you: it is the text just after this task.

1. `write tools PLACE;` records where the coffee is to go, so the behaviour that
   delivers it knows.
2. `write stdout navigate coffee-machine;` then `read stdin;`.
3. `write stdout brew;` then `read stdin;`. This starts the machine.
4. `exit;`

Do not wait for the coffee. Brewing takes a while, and while it brews the body is free
for other work. The coffee machine raises its own interrupt when the coffee is ready, and
a different behaviour picks it up.
