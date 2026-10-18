# What this demo shows of ZEOS

One robot and four things for it to do is the smallest system in which scheduling stops
being a convenience and becomes the design. Stretch can do one thing at a time, every
behaviour wants the body, and the person at the page decides when the coffee is wanted and
when something is burning. Whether the morning comes out right is a question about the
kernel's decisions, and the journal answers it.

This file is an inventory. Each entry names the feature, what it looks like in this tree,
and what you would have to write by hand without it. Everything listed is exercised by the
running demonstration, and nothing here is aspirational.

---

## Priorities and interrupts

**Priority is one integer per behaviour.** `smoke-response` 5, `deliver-coffee` 30,
`brew-coffee` 40, `tidy-atrium` 80. "Smoke beats coffee beats tidying" is those four
numbers and is written nowhere else; no prompt mentions another behaviour. Without them,
each behaviour's prose would have to describe every other behaviour and when to defer to
it, and a model would have to weigh those paragraphs correctly under pressure.

**Interrupts are pipe writes.** `system/vectors.yaml` binds three device pipes to three
handlers: a coffee order, the coffee machine finishing, and the smoke sensor. Clicking a
button on the page is a write to one of those pipes, and the write starts the handler at
its priority. Nothing polls a sensor or a queue.

**`queue` for orders, `coalesce` for smoke.** Two coffees ordered behind a busy handler
are two handlers with their own desks; three alarms behind one are one response, because
an alarm is a level, not a count.

**A behaviour that waits does nothing.** A behaviour that has sent the body a command
reads its answer pipe and blocks, running no forward passes until the body answers. The
coffee's brewing time is not a wait inside any behaviour at all: `brew-coffee` exits once
the machine is running, the machine's own interrupt starts `deliver-coffee`, and the
tidying has the body in between.

---

## The body is a lease

**`requires:` turns the robot into a resource.** `platforms/stretch.yaml` declares one
body, and each behaviour that moves it requires a gripper. Wearing the body is holding its
lease; a behaviour without it cannot command it, and if it runs it waits on the lease like
any claimant. That is what stops a delivery from countermanding the smoke handler while
the handler is driving, with nothing in either prompt about the other.

**A more urgent behaviour takes the body, under the wearer's release policy.** When a
claimant outranks the wearer, the kernel books a revocation for after the grace of the
wearer's `release_policy` and then takes the body back. `tidy-atrium` is
`at-action-boundary`, so the item in hand is put away first; `deliver-coffee` is
`immediate`, so an alarm stops it mid-move. The journal records each takeover in
`fleet.disembodied` with its reason, and `story.py` checks both policies were kept.

**Preemption lands within a token boundary.** No token is decoded between one behaviour
losing the body and the next taking it; the test asserts exactly that.

**The body is told who to obey.** `fleet.embodied` and `fleet.disembodied` are how the
simulated VLA learns its wearer, and when to stop the action in hand. Without the lease,
the body's driver would have to arbitrate between behaviours itself, which is the
scheduler written a second time in the wrong place.

---

## Resuming into a world that moved

**`reads:` is what a resume notice is made of.** Each behaviour names the objects whose
change would alter what it does next: where the body is, what it is holding, where the
coffee is. When it gets the body back the kernel diffs those against what changed while it
was away and injects a RESUME notice, or nothing if nothing did.

**`Suspended` and `Waited` are both told.** A behaviour preempted mid-turn and a behaviour
asleep on its answer pipe both resume with a notice; the second is how the tidying learns
the body went to the coffee machine while it waited.

**Reports made on a behaviour's behalf stay out of its own notice.** The body reports the
state after each action on actuators, and attributes each report to the behaviour whose
command caused it (`Kernel.deliver(..., on_behalf_of=job)`). A behaviour is never told
about its own moves; every other behaviour is. Where the body stopped when an alarm cut a
move short is the body's own report, attributed to nobody, because it is news to the
behaviour that asked for the move. Without attribution, every answer from the body would
wake the behaviour that asked for it with a notice announcing its own action as news.

---

## The world as the behaviours' memory

**Actuators are the world changing.** `report.position`, `report.coffee` and the rest are
declared with a `world_object:`; a delivery to one latches the object and refreshes every
status region that shows it.

**Status regions are what a behaviour knows.** `deliver-coffee` maps three objects (where
the coffee is going, where it is, where the body is) and its prose is a set of rules over
those three lines, so each command follows from what is true now. A RESUME notice is only
the signal that something moved; the status lines say what. A behaviour keeps no table of
its own, so nothing it said earlier can be out of date.

**One pipe per answer.** Each behaviour's answers arrive on its own `stretch.done.*`, so
the body's reply wakes the behaviour that asked and nobody else. Commands leave on one
sink, `stretch.command`, which the driver drains and nothing inside the system reads.

**Capabilities are declared.** Each behaviour that moves the body declares its capability
on `stretch.command`, and that closes its table: a write to any pipe it did not declare is
a `capability_fault`.

---

## The model in the seat

**A seat makes a model a machine.** One decode per word, a parser that turns a closed
command into a kernel request, and a transcript rebuilt from the context the kernel keeps.
The same case runs under the descriptors' own tapes and under Claude, and the kernel
cannot tell which.

**The default syscall ABI.** `write PIPE TEXT;`, `read PIPE;`, `say TEXT;`, `exit;`, naming
only the pipes a behaviour binds. There is no verb for yielding; a behaviour gives up the
machine by reading an empty pipe.

**A reply that is not a command is a fault, not a guess.** It reaches the kernel as
`MALFORMED`, and `on_fault: retry` answers it with a notice and another turn.

---

## Determinism and the journal

**A scripted morning replays byte for byte.** Same case, same events, same journal, which
is what the tests pin.

**A session with a model replays too.** A recording keeps every step's time, every click,
every command and the reply it came from, and a copy of the case. Replaying it gives
Claude's commands back to the same kernel and reproduces the journal byte for byte;
[`recordings/claude-morning`](recordings/claude-morning) is one, and needs no key.

**The story is judged on the journal.** `story.py` states what the morning must come to:
the table cleared and sorted, the coffee at the desk that ordered it, the hotplate off, no
fault, and both release policies kept. Each claim is a fact the journal or the world
records, not anything a behaviour said.

**`lint` before running.** `zeos-stretch lint` checks the case tree, from a vector naming a
handler that does not exist to two behaviours of one priority writing one object, and the demonstration
keeps it at zero errors and zero warnings.
