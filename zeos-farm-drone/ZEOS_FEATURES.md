# What this demo shows of ZEOS

One drone and a farm that keeps changing under it is the smallest system in which the
kernel has to decide, again and again, which behaviour flies the drone, what none of
them may do, and whose words become work. The person at the page decides when the
vehicle drives in, when the calves are born and when the fire service calls; whether the
morning comes out right is a question about the kernel's decisions, and the journal
answers it.

This file is an inventory. Each entry names the feature, what it looks like in this tree,
and what you would have to write by hand without it. Everything listed is exercised by
the running demonstration, and nothing here is aspirational.

---

## One drone, shared by six behaviours

**The drone is a lease.** `platforms/kestrel.yaml` declares the farm's one drone, and
every behaviour that flies it declares `requires:`. Flying it is holding its lease; a
behaviour without the lease cannot command it, and waits for it like any claimant.

**A more urgent behaviour takes the drone, under the holder's release policy.** When the
patrol sees a vehicle, the vehicle check (priority 20) outranks the patrol (80): the
kernel takes the drone back and the patrol suspends, keeping its place. When the close
look is done the patrol gets the drone back and carries on with the next paddock. A fire
watch takes it the same way, and so does a search for something the farmer lost.
Nothing in any behaviour's prose arranges any of it.

**Release policies decide how the drone is handed over.** The patrol and the calf check
are `at-action-boundary`: a look in progress (observe, zoom, search, panorama) finishes
first, so no paddock is left half-looked-at, while a flight, climb or return stops where
it is -- every point along a flight is a boundary between actions. The vehicle check,
the fire watch and the search are `immediate`: whatever is in progress stops. Either
way a cut-short flight stops over the nearest place, and the behaviour that asked for
it is told where.

**The drone is told who to obey.** `fleet.embodied` and `fleet.disembodied` are how the
simulator learns which behaviour is flying the drone, and when to stop the action in
hand. Without the lease, the drone's driver would have to arbitrate between behaviours
itself, which is the scheduler written a second time in the wrong place.

---

## A gate the model cannot talk past

**Every drone command is held until the flight-rules guard answers.**
`system/gates.yaml` puts a gate on `drone.command`: the kernel holds each write, spawns
one `flight-rules` job for it, and lands the command only on `allow`. Nothing flies off
the property; nothing climbs above 120 m.

**A veto is a fault the behaviour recovers from.** On `veto: REASON` the writer gets a
`gate_veto` fault, and `on_fault: retry` answers it with a FAULT notice and another
turn. The vehicle check that tries to follow a ute onto the road ends up watching it from
`boundary-west`; the fire watch that asks for 180 m settles for 120.

**The guard is not the model.** `Routed` answers the guard's turns with the rules, under
any command source, so a model never judges its own flight. Two behaviours are naive on
purpose, so that the gate is seen working. Without the gate, the limits would be a
paragraph in every prompt and a hope.

---

## Interrupts, from the world and from other jobs

**Priority is one integer per behaviour.** Fire watch 15, vehicle check 20, calf check 30,
search 40, patrol 80; nothing else says which wins.

**A finding is an interrupt the patrol raises.** The patrol writes `findings.vehicle` or
`findings.livestock`; `system/vectors.yaml` binds each to a handler, which starts at its
priority. The patrol never names the behaviour that answers it, and a new kind of finding
is a new pipe and a new handler, not an edit to the patrol.

**`queue` for findings and searches, `coalesce` for fires and dawns.** Two vehicles are
two close looks, one after the other; three alarms behind a running fire watch are one
watch; a second dawn while the patrol is out does not start a second patrol.

**Device interrupts.** `clock.dawn` and `alerts.fire` are written by the outside world,
whether the case's schedule or a click on the page.

---

## The farmer's words

**A front door compiles words into a job.** `farmer.says` is a front door whose speaker
is the `farmer` principal (`system/principals.yaml`). "find my motorbike" matches the
phrasing `find my {item}` and compiles to `ask-find(item=motorbike)`; the arguments are
injected as values, not instructions. "open the gate" matches nothing and is refused,
with the reason echoed to `farmer.replies`.

**Who spoke comes from the pipe, and so does what they may ask.** The farmer's
principal may write two pipes, `requests.find` and `alerts.fire`, and nothing else: the
jobs the farmer's words become are narrowed to that, so no request can command a drone.
A request is capped at priority 15.

**A request is a relay to an interrupt.** `ask-find` and `report-fire` write one line
and exit; the search and the fire watch they start are the kernel's, serialised by their
vectors. A farmer reporting a fire and the fire service reporting one land on the same
interrupt line. Two requests in a row are two searches, not two searches reading each
other's answers.

---

## The world as the behaviours' memory

**Actuators are the world changing.** The drone's position and altitude are reported on
actuator pipes, attributed to the behaviour whose command caused each change
(`Kernel.deliver(..., on_behalf_of=job)`), so a behaviour is never told about its own
moves and every other behaviour that reads them is.

**The farmer's records are world state.** `herd.expected` is a STATUS line for the patrol
and the calf check, and is written only by the calf check, through `herd.expected.set`,
when calves explain a count. The next patrol round compares against it, and a patrol out
at the time is told it changed.

**Status regions are what a behaviour knows now.** The kernel rewrites a STATUS line in
place wherever a block boundary falls, so a behaviour reads the current value rather than
a remembered one.

---

## Capabilities and schemas

**Capabilities close each behaviour's table.** A behaviour may write only the pipes it
declares; anything else is a `capability_fault`.

**An alert is endorsed by a narrow schema.** `system/schemas.yaml` declares `alert`:
`kind` and `where` are enumerations, `photo` six characters, `detail` twenty-four. A write
to `farmer.alerts` that is not an alert is refused, and the lint keeps the schema narrow
enough that little which is not an alert fits through it.

---

## The model in the seat

**A seat makes any source a machine.** One decode per word, a parser that turns a closed
command into a kernel request, and a transcript rebuilt from the context the kernel keeps.
The same case runs under the descriptors' tapes, under a deterministic rules source, and
under Claude, and the kernel cannot tell which.

**The default syscall ABI.** `write PIPE TEXT;`, `read PIPE;`, `say TEXT;` and `exit;`,
naming only the pipes a behaviour binds. A behaviour gives up the machine by reading an
empty pipe; waiting for a drone costs nothing.

---

## Determinism and the journal

**A scripted morning replays byte for byte.** Same case, same events, same journal, which
is what the tests pin.

**A session from the page replays byte for byte too.** A recording keeps every step's
time, every click and its arguments, every command and the reply it came from, and a copy
of the case. Replaying it reproduces the journal exactly, whoever chose the commands.

**The story is judged on the journal and the farm.** `story.py` states what any morning
must come to: every paddock observed, every vehicle looked at and reported, every count
that differed checked, the farmer's records counting the calves a close look found and
nothing else, every low trough and every fire reported, every lost item reported, every
cited photo taken, no command outside the flight rules ever reaching a drone, no
capability or privilege fault, and every behaviour that lost its drone still finishing.

**`lint` before running.** `zeos-farm-drone lint` checks the case tree -- the gate's
pipes, the front door's principal, the schema's width, each `requires:` against the fleet
-- and the demonstration keeps it at zero errors and zero warnings.
