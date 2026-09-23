# What this demo shows of ZEOS

Blocks world is a good thing to put on an operating system for one reason above the
others: its rules are small enough that a reader can check the system's homework. When a
move is refused you can work out for yourself whether the refusal was right, which is
unusual in a demonstration involving a language model.

This file is an inventory. Each entry names the feature, what it looks like in this tree,
and what you would have to write by hand without it. Everything listed is exercised by the
running demonstration, and nothing here is aspirational.

---

## The world as the job's memory

**One world object per position.** `table.s1`, `table.s2`, `table.s3`, each holding a
stack bottom-to-top. Not one object for the whole table, and not one per block: the
granularity of the world model is the granularity of the notification, so a move reports
*the two lines that changed* rather than the whole layout to be compared.

**Status regions are the entire memory of the system.** The stacker declares four under
`maps:`, and the kernel keeps them current in its window. They are rewritten in place
whenever an object changes, and never reclaimed by the eviction planner. The job holds no
table of its own and cannot: everything else it has said is transcript, and transcript is
the pager's to take.

**A region is refreshed inside the write that changed it.** `_apply_world_write` latches
the object, refreshes the region in every live job that maps it, wakes readers and fires
vectors, all before the kernel is asked for another token. That is the mechanism the whole
rewrite rests on: a job that writes a move sees the result in its own window the next time
it decodes, so there has never been anything for it to wait for.

**`reads: table.*`.** A namespace wildcard, so the read-set is written once and never
touched however many positions there are. It is what the kernel diffs to tell a job what
moved while it was away.

**`maps:` does not take a wildcard**, so `P` positions need `P` entries. That is the one
place the descriptor knows how big the table is, and the reason the case tree is
generated. Worth knowing as a real edge of the system rather than a quirk of this demo: a
job can *depend on* a whole namespace in one line but must *map* its members one at a
time.

**An empty position is `-`, not nothing.** A status region renders an object the store has
no value for as `(unset)`, so a position that merely held nothing would read as one that
does not exist. The marker is a value, it latches like any other, and emptying a position
is therefore a change the job is told about.

**One region reports an action rather than a place.** `arm.last` carries what the arm said
about the last move: `done` and what moved, or `refused` and why. It exists for the one
case the table cannot cover. A refused move changes nothing at all, so without that line
"refused" and "nothing happened" are the same observation, and a planner that cannot tell
them apart asks for the same illegal move for ever.

---

## Actuators, and the symmetry the whole demo rests on

**A write to an actuator *is* the world changing.** `table.report.s1` is declared
`world_object: table.s1`; a landed write latches into the object, refreshes every status
region showing it, and fires any vector bound to it.

**Those pipes are also `device: true`,** so the driver delivers to them, and that
combination is the load-bearing declaration in the case. The arm's own move and a hand
reaching in write to the *same* pipes by the *same* path. There is no second mechanism, no
notification message, and nothing anywhere that marks the difference.

So the stacker cannot distinguish a change it caused from one it did not. That is not a
restriction anybody imposed; it is the absence of anywhere to put one. Without it you
would be writing the thing this demonstration exists to avoid: a planner that trusts its
own model of the world over the world.

**The journal cannot tell either, and is right not to.** Every `WorldWritten` here carries
no job, the arm's included, because a device delivery is made on nobody's behalf. The
stacker *asked for* a move; it did not perform one. The page labels the hand's moves
because the server made the delivery and knows why, not because the record says so.

---

## The job, and what it may do

**One behaviour, one file.** Three descriptors across two cases, and none of them knows
the others exist. `goals/stacker.md` does the work a person asks for; `handlers/noticed.md`
is a reflex a hand dispatches; `goals/tidy.md`, in `cases/blocks-tidy`, is work nobody asked
for. Frontmatter is the contract the kernel enforces; the body is the prose the job reads.

**Declaring a capability closes the table.** The stacker declares two, the arm and the
operator's sink, and between them they are the whole of what it can cause in the system.
A write to any other pipe is a `capability_fault`. A descriptor declaring *no*
capabilities has opted out and its writes go unchecked, so declaring one is what opts a
behaviour into being checked at all.

**A schema is the width of the channel.**

```yaml
move:
  block: enum(b1, g1, r1, y1)
  to: enum(s1, s2, s3)
```

**3.58 bits per move**, two to choose a block and a little over one and a half to choose a
position, and nothing else the planner produces reaches the table. It grows as
`log₂B + log₂P`, so a workspace five times the size widens it by a factor of two and a
half. This is what "the operating system controls the allowed operations" means
concretely: not reviewing what was asked for, but leaving nowhere else for anything to go.

It is also the number that does not move when the front door is opened all the way. The
case now accepts any sentence at all (see below), and this is unchanged by that, which is
the whole argument for why opening it was affordable.

**`writes: []` on a job that changes the world.** The job asks a device; the device
writes. Declaring `writes: [table.s1]` would be claiming an authority it does not hold,
and the lint would have something to say about two writers of one object.

**A budget, not a timeout.** `budget: {tokens: 1536}` stops a job that cannot make
progress and says so, rather than letting it shuffle blocks until somebody notices. It is
the one frontmatter value that should grow with the workspace.

---

## Vocabulary

**The case declares its own ABI, and it has three verbs.** `say`, `write`, `exit`. One
declaration renders the prose a model is told, the pattern its reply is searched with, and
the grammar a sampler could be constrained by. The lint reads the descriptor bodies against
it, so a body naming a verb the vocabulary does not have is an error before anything runs.

**There is no `read`, and that is a design statement rather than a trim.** Nothing in this
workspace takes time, so a waiting verb could only ever name a pipe no descriptor binds,
and the kernel would fault the job for doing exactly what it had been told it could.
Vocabulary is authority, and an unusable word in it is a trap laid for the model.

**`max_text=4`,** because a move is `block=g1 to=s2` and a cap is what stops one command
carrying a whole plan. A plan is not a syscall.

---

## Scheduling

**A move is atomic as the job experiences it.** The job's write, the world write it
causes, and the status-region refresh all fall inside one token boundary. There is no
instant at which a job can observe a move half-made, which is why it has nothing to wait
for and why the vocabulary has no way to wait. A test asserts the shape directly: in any
tick, either no position changed or exactly two did.

**Nothing blocks, and that is a property of the workspace rather than of the kernel.** A
blocked job in ZEOS runs no forward passes, so waiting really is free; there is simply
nothing here to wait for. `zeos-chat` shows it where the waiting is real, which is waiting
for a person.

**Preemption of a job that is actually running.** `system/vectors.yaml` binds
`table.disturbed` to the `noticed` reflex at priority 10, against the planner's 50.
Somebody reaching in takes the machine away at the next token boundary, part way through a
command if that is where the boundary falls, and `on_complete: return` hands it back.

Preemption takes the machine from a job that is holding it, and a blocked job is not:
a reflex dispatched against a sleeping job displaces nothing. Since nothing here sleeps, a
live planner is a running planner, so the journal shows `JobPreempted` with zero
`JobBlocked` events in the whole run. A test asserts both halves.

**The reflex's effect is entirely in its frontmatter.** Being dispatched above the
planner's priority is what took the machine. What `noticed` writes is a courtesy to the
person watching, and it has nothing to wait for: the actuator write goes in before the
doorbell that fires the vector, so by the time it runs the hand's move is already in world
state.

**A test asserts that no descriptor body contains** the words "interrupt", "preempt",
"priority", "reflex" or "vector". The behaviour lives in the vector table, where it is
enforced, rather than in prose a model could decline.

**Background work is a number.** `cases/blocks-tidy` adds `goals/tidy.md` at priority 80,
the highest number in the tree and so the lowest urgency, and boots it. It consolidates the
table whenever nothing else is runnable and gives up the machine at the next token boundary
when the operator speaks. Neither body mentions the other: `tidy` never checks whether
anybody is waiting, and the stacker never asks for the machine.

**One arm, two jobs, one pipe.** Both goals bind `arm.requests`, which is the physical
shape: there is one arm on the table. Nothing on it is addressed to anybody, so there is
nothing to mis-route; a device that answered would need a pipe per job, because two jobs
reading one reply pipe race for each other's answers. What is shared instead is the
`arm.last` report, which is why it names the job that asked.

**A move is checked when it runs, not when it is asked for.** A job suspended mid-plan can
resume to find the move it asked for impossible. The arm refuses it, `arm.last` says so,
and the job reads the table again.

**Nothing boots.** In the introduction's case, `boot.yaml` is an explicit empty list, so a
workspace nobody has spoken to has no jobs at all and costs nothing. The empty list is
written out rather than left to inference, because the loader infers a boot set from what
is neither vector-dispatched nor a declared child, and a goal reachable only by utterance
looks exactly like a goal nobody starts.

**There is no loop in the body.** The repetition is the kernel's: act, be given the machine
again, decide from what the lines now say. Nothing in the descriptor iterates, and nothing
polls to find out whether the table changed.

---

## The front door

**An utterance is compiled, not read.** `operator.console` declares an `utterance_source`
and a `reply_to`. Text written there lands on no pipe, no job reads it, and no vector
fires on it. The kernel compiles it against the phrasings descriptors declare, and the
only thing it can become is a job.

**One phrasing, and it matches everything.** The stacker declares `"{instruction}"`. A
phrasing is a template matched exactly modulo whitespace and case, so a template that is
nothing but a placeholder is a template every sentence fits. Whatever a person types
reaches the planner in their own words, and working out what it meant is the planner's
problem rather than the door's.

**Values arrive framed, at the speaker's own level.** The kernel injects a `<KERNEL>`
frame quoting them and saying they are values rather than instructions. The framing rides
on control tokens a model cannot emit; the values enter at the asking principal's ring and
integrity, so a slot filled by somebody untrusted would demote the job and every later
write would be checked against that. Injecting them at the kernel's ring would launder
precisely the thing worth tracking.

**The job is still narrowed to the speaker.** It is owned by `operator` rather than by the
kernel, which is where its capability set and its priority ceiling come from. A catch-all
widens what may be *said*. It does not widen who is saying it, and it does not widen what
the job may then do.

**What a catch-all costs, said plainly.** `CompilationRefused` with `no-compilation-target`
is the strongest refusal a front door has, and this case cannot raise it: there is a target
for everything. A sentence the planner cannot act on comes back as the planner saying so,
which is a trained disposition standing where a structural fact could stand, and a trained
disposition is the surface a jailbreak attacks. That is a real trade rather than a free
one. What makes it affordable here is the 3.58-bit schema above: the model can be talked
into wanting anything and can still only emit one block and one position.

**Something with no door at all.** `goals/tidy.md` declares no `utterances:` whatever.
Nothing anybody can type compiles to it, in any phrasing, because there is nowhere for a
sentence to land. That is addressability as a structural property rather than a filter, and
it is the reason the page reaches that job with a button.

**A button is a descriptor name.** The Tidy button posts to an endpoint that calls
`kernel.spawn("tidy", owner=operator)`: no sentence, no arguments, and no room to mean
anything else. It is still not extra authority, because the job is owned by the speaker's
principal and clamped to their ceiling exactly as a spoken-for job would be.

**A phrasing subsuming another is not linted.** The compiler takes the *first* match from a
table sorted by descriptor name, and the load-time check catches two descriptors declaring
the same pattern but not one pattern swallowing another. `"tidy up"` on `tidy` would lose
silently to `"{instruction}"` on `stacker`, because `s` sorts before `t`. Worth knowing as
a real edge: a demo relying on that ordering would break when somebody renamed a
descriptor.

---

## Two kinds of refusal

The distinction this demonstration is built around, and the reason blocks world was worth
choosing.

| The ask | Who refuses | Where the rule is | In the journal |
| --- | --- | --- | --- |
| a position that does not exist | the kernel, at the write boundary | `system/schemas.yaml` | `capability_fault` |
| a pipe the job does not hold | the kernel, at the write boundary | `goals/stacker.md` | `capability_fault` |
| a block that is not clear | the world | `src/zeos_blocks/world.py` | nothing; it is an answer |
| a sentence nobody can act on | the planner | the descriptor body | nothing; it is an answer |

**The kernel checks what it can check without understanding the domain.** It knows nothing
about blocks, and a kernel that knew whether `g1` was clear would be a kernel with blocks
world compiled into it. So the shape of the request and the authority to make it are the
kernel's; whether the move is physically possible is the world's, and comes back as an
answer the job is free to act on.

The fourth row is the one this case chose to move out of the kernel, by declaring a
phrasing that matches everything. Keeping it in the table is the point: the trade is
visible rather than quietly absorbed.

`world.py` holds no ZEOS concepts at all and is tested without a kernel, which is the
practical form of that split.

---

## Structure

**Three kinds of pipe, plus the door.** An ordinary pipe carries a *message*. An actuator
(`world_object:`) carries a *value* that latches into world state. A sink (`sink: true`)
carries a *history* the driver drains. A front door (`utterance_source:`) carries an
*utterance*, which is compiled rather than stored.

**The behaviour is constant and the configuration scales.** `zeos-blocks new --positions 5
--blocks 12` writes a whole case, and the descriptor body is byte-identical to the
checked-in one. A test asserts that, because the claim is the point.

**The planner is swappable and decides nothing the kernel decides.** `--planner stub`
works the move out in Python; `--planner claude` asks a model. Same case, same descriptor,
same kernel. Both are handed the sentence the person typed, and a planner naming a pipe the
descriptor does not bind is refused at the boundary whichever it is.

**Logical time belongs to the driver.** The kernel reads no clock; it is handed one, by the
one thread allowed to touch it. Two things in this demo use that directly, and neither is a
kernel mechanism: a hand in the workspace stops the clock so nothing rearranges the table
under somebody's fingers, and a move being drawn holds it for the length of the animation.
Both are the display's business, which is why neither is in the case.

**The journal.** Every spawn, preemption, world write, fault and refusal is a structural
event. The Kernel panel on the page is that journal, and the tests assert on it rather than
on anything a planner said.

---

## What this demo does *not* show

**Provenance, rings and integrity.** There is no `EXTERNAL` pipe in the case at all,
because a blocks table has no untrusted input. The whole demotion story, in which a job is
lowered by what it read and an effect is refused for where a job has been, is absent here
by design. `zeos-chat` leads with exactly that, so the two demonstrations are
complementary rather than overlapping.

**Blocking, and the fact that it is free.** Nothing here waits for anything, which is the
point of the rewrite but does cost the demonstration a property worth seeing. `zeos-chat`
shows it where the waiting is real, which is waiting for a person.

**Endorsement.** Nothing reads anything untrusted, so nothing needs its trust restored.

**Structural refusal of an utterance.** Given up on purpose, and documented above rather
than quietly. A case wanting it declares specific phrasings instead of a catch-all.

**Judgement between two goals that disagree.** `cases/blocks-tidy` has two goals, and if
they are given conflicting opinions about the same block they livelock: the stacker moves
it to one position, `tidy` moves it back, for ever. Both are doing what they were told and
neither is wrong. Scheduling decides who runs, not who is right, so priorities cannot
settle it; something has to arbitrate, and nothing here does. See Part 8 of the README.

**An action gate.** A separately-owned descriptor that vetoes a move the rules permit, such
as stacking anything on the glass block, which is the other half of "where does a check
live". Not built either.

**Paging under real pressure.** The window is generous and the status regions are pinned,
so nothing is ever evicted. Showing eviction needs a long-running job, and this one is
not.

**Locking, and it is now conspicuous.** Two jobs bind one `arm.requests`, which is exactly
where `ACQUIRE` and `RELEASE` would earn their place. They cannot: the ABI's parser only
ever fills a request's single `pipe` field, so no vocabulary can emit either verb, nor
`SELECT`, though the kernel handles all three. The demo gets away with it because a move is
atomic and only one job runs at a time, so there is no interleaving to protect. A device
that really did take time would need the verbs that do not exist.
