# ZEOS-Chat — implementation plan

Built on the decisions in [`UNDERSTANDING.md`](UNDERSTANDING.md) §7: land C2 and C4 in
`zeos-internal` first, leave C3 open and state the single-tenancy limit, simulate email by
default with a real send behind a flag, and configure from a server-side `.env`.

Each phase says what it produces and what makes it done. Something runs at the end of
every phase, which is the ordering the spec's §6 asks for.

## 0. Architecture decisions

**Where the code lives.** Kernel work goes to `zeos-internal` as PRs, squash-merged, one
concern each, with all four gates green (`pytest`, `pytest -m determinism`, `pyright`,
`pre-commit`). Everything else lives here, in `zeos_demos/zeos-chat/`.

**The private/public repo workflow** (per *Private-public repo workflow management*)
constrains those PRs, and one rule is easy to break by accident. Every internal commit
reaches the public `zeos` history unchanged, so **an internal PR or issue number must not
appear in a commit message**: the `(#N)` GitHub appends on squash-merge has to be deleted
from the commit title, and `Closes #N` must not go in the body — on `zeos` it renders as a
link to an unrelated public PR. The tracking reference goes in the PR body, which never
crosses. Branch off `origin/main`, push to `origin`, PR into `zeos-internal` main, squash.
AI assistance is disclosed in the PR body. The internal→public sync itself, and the demos
action that gates it, are a person's to run — not ours.

Note that this document and the others here cite upstream numbers freely (`#44`, `#75`).
That is fine: these files live in `zeos_demos` and never become zeos commits.

**How the demo gets `zeos`.** C2 and C4 will not be in a release when we need them, so
during development:

```toml
[tool.uv.sources]
zeos = { path = "../../zeos-internal", editable = true }
```

That assumes the two repositories are checked out side by side, which is how they are
worked on. Swapped for a pinned `zeos>=0.2` from PyPI once the kernel work ships — noted
here so the temporary shape does not become permanent by accident.

**Web stack: stdlib only.** `ThreadingHTTPServer`, `POST /say` inbound, and
Server-Sent Events outbound, following the `demo/space-invaders/web/` precedent (static
page, assets inlined, all I/O at the edge). No framework, no websocket library — the
traffic is one message in and a stream of replies out, and SSE carries that. It also keeps
the demo installable with nothing but `zeos` and `anthropic`.

**Threading, and why it matters.** The kernel is not re-entrant, and `on_command` runs
mid-decode. So: the run loop owns the kernel on one thread and nothing else touches it.
HTTP handlers put inbound messages on a `queue.Queue`; the loop drains that queue
*between* ticks and calls `kernel.deliver`. `on_drain` pushes reply text onto a
per-session broadcast queue the SSE handler reads. This mirrors how
`demo/coop-count/.../cli.py` unrolls `Driver._run_until` — it has to, because a
conversation never ends on its own and the loop must keep turning while a person types.

**Single tenancy, made honest.** C3 stays open, so there is one `converse` job bound to
one `user.messages`. The server holds one session, and a second tab is simply another
window onto it: both see the same stream and either can type into the same conversation.
Stated in the CLI banner rather than hidden, and not faked.

## 1. Kernel: C2 — job parameters as a ring-0 segment

**Where the gap is.** `Kernel.spawn` (`kernel.py:416`) takes `parent`, `priority`,
`vector`, `owner`. `handle_utterance` (`kernel.py:~1741`) and `confirm` both call it
without `decision.artifact.invocation.arguments`, which `compiler.py:278` computed
correctly. "Send an email to Alice" spawns `send-email` knowing nothing about Alice.

**Shape.** The template already exists: `_start_job` injects `job.vector_payload` after
the body, at the *source pipe's* ring. Arguments work the same way.

- `spawn(..., arguments: Mapping[str, str] | None = None)`, stored on the `Job`.
- `_start_job` injects them after the body and before the status regions, as its own
  segment: the **framing** is kernel-issued at `Ring.KERNEL` via `frame_tokens`, the
  **values** carry the speaker's ring and integrity. That is the split the spec insists
  on, and it is what answers the obvious attack — a second instruction smuggled into an
  `{item}` slot is content at the speaker's ring, not kernel text.
- `_alarm_spoof` over the values, exactly as the vector payload path does, so a value
  spelling `<RESUME>` raises a spoof fault and is shown escaped.
- `handle_utterance` and `confirm` pass `invocation.arguments` through.

**Status: PR open** — `zeos-internal#104`, branch `feature/job-arguments`.

One correction to the shape above, and it matters. The note's "inject as a ring-0 segment"
cannot be taken literally: at ring 0 the values would be laundered to kernel trust and
every later capability check would pass on them. The note's own next sentence is the
coherent reading — the framing is the kernel's, the values are the speaker's — and MP §5.3
already provides the mechanism, since frames ride on CONTROL tokens a model cannot emit.
So the segment is `<KERNEL>`-framed on control tokens and carries ordinary tokens at the
asking principal's ring and integrity.

The property that fell out is the one the demo wants to show: the same descriptor, asked
for with the same words, is demoted 2 → 3 and refused at the actuator for a visitor and
not for an operator — with the journal naming the arguments segment as the cause. Nothing
inspects the text. That is the injection story running through the *arguments* path, and
it is a better demonstration than the retrieval path because it needs no web pipe.

## 2. Kernel: C4 — the NLI front door on a pipe

**Where the gap is.** `handle_utterance` is reachable only by a caller holding the
`Kernel`, and nothing in `src/` or `demo/` calls it. There is no route from *text arrived
on a pipe* to *compiled against the phrasing table*.

**Shape.** `Utterance` already carries `source_pipe` and `reply_pipe`, so most of the
plumbing exists.

- `utterance_source: true` on a `PipeSpec`, loaded by `_load_pipes`.
- `Kernel.deliver` on such a pipe builds an `Utterance` — the pipe's principal and ring,
  `at=self.clock` — and routes it to `handle_utterance` instead of landing tokens.
- Echo-back and confirmation write to `reply_pipe`, which C1 made possible: a sink now
  exists to write them to.
- The dispatcher stays a kernel-side function for now. Promoting it to a descriptor is
  what `nli/dispatcher.py` says the real design intends, but it adds a scheduling story
  without adding a safety one, and the demo needs the safety story. Noted as follow-on
  rather than done quietly.

**Status: PR open** — `zeos-internal#105`, branch `feature/nli-front-door`.

Landed close to the sketch, with one decision worth carrying into the demo: **who spoke
comes from the pipe, not from an argument and not from the text**. A pipe declares
`utterance_source: <principal>`, and a device that hears two people is two pipes. For
zeos-chat that means the browser session's identity is a property of the door the adapter
delivers to — so a guest view and an owner view are two pipes, not one pipe plus a flag
the adapter sets. That is a better shape than what phase 5 assumed.

A door must also declare `reply_to`, a sink, and the echo-back is now actually written
there rather than only journalled. The demo gets the echo for free: it is a drain like any
other reply, so it arrives in the browser through the same path.

## 2b. Kernel: C8 — capability schemas from a case directory, and C13 — a spawn target

**C8 is done** — PR open as `zeos-internal#106`, branch `feature/case-schemas`. It landed as
described below, with one addition worth carrying: `FieldSpec.capacity_bits` scored an
unbounded `string` as *zero* bits, because `(max_length or 0) * log2(95)` is arithmetic on
a missing bound. The widest field expressible looked like the narrowest, so
`wide-endorsement-schema` — the rule whose job is to say when endorsement has stopped
narrowing anything — had nothing to report. Fixed in the same PR, because loading schemas
from a case is what puts real trees behind that bound.

For the chat case this means `guards/retrieval-endorser.md` and
`guards/session-endorser.md` can now be written with real `schema:` declarations. Keep
them narrow: a bare list of permitted values where an actuator will do, and never an
unbounded string, which the lint will now say so about.

*Original entry:* `Descriptor.from_frontmatter` accepts a
`schemas=` mapping that `parse_descriptor_file` never passes, so `schema: narrow-summary`
on a capability fails at load with "unknown schema", and only in-memory descriptors can
use one. Both endorsers need a schema, and endorsement is the only integrity-raising
operation in the system — so this is what phase 6 stands on. Shape: a
`system/schemas.yaml`, threaded through `load_case`. The `write-up-without-schema` rule
is already a lint pointing at a door the loader keeps shut.

**C13 is new, found while building the case tree, and it is not in the design note's
§5.** A `spawn` verb cannot work through any ABI:

- `SyscallABI.parse` puts everything after the verb into `MachineRequest.payload`, and
  sets `.text` only for a `MALFORMED` command.
- The kernel's spawn path reads `str(request.text)` (`kernel.py`, `case OpKind.SPAWN`).

So a parsed `spawn deep-research;` arrives naming `"None"` and is refused as a capability
fault. Nothing has noticed because spawning has only ever been exercised from a script
step's explicit request (`- spawn: clear-bench`), which a *tape* cannot carry — a
`TapeSource` plays `emit` steps only. The consequence for this design is direct: §2.1's
claim that `children:` is load-bearing because "a conversation that dispatches work must
say in advance what work it can dispatch" is currently untestable, because no
conversation can dispatch anything.

**Status: PR open** — `zeos-internal#102`, branch `feature/abi-carries-spawn-target`.

The shape landed slightly differently from the sketch above. Rather than a `Verb` flag a
case author could misdeclare, the distinction is read off the op: `NAMES_A_TARGET` beside
`OpKind` records which ops take a name the kernel resolves, and `parse` routes the
argument to `text` for those and to `payload` for the rest. It is a fact about the ops,
not about a case's vocabulary, and `MachineRequest.text` already carried the comment
saying so. `NEED` is fixed by the same line.

Three consequences worth knowing when the demo picks this up: a naming verb with no name
is `MALFORMED` rather than a silent no-op; `prose()` renders `<name>` instead of
`<text>`, so the prompt the Claude source sends will describe `spawn` correctly without
any work here; and a verb declaring both a naming op and a pipe is refused at
declaration. `DEFAULT` gains no `spawn` verb, so nothing else in the tree changes.

**Once merged**, the case can turn the services on: teach `spawn` in `converse`'s body
and give the tapes a `spawn deep-research;`. Until then the tapes do not spawn, and
`children:` is a contract nothing exercises.

## 3. The case tree

`zeos-chat/cases/chat/`, laid out as `descriptor/loader.py` accepts and as spec
§2 specifies, with `script:` blocks and an `events.jsonl`.

```
goals/converse.md                 priority 60 — the resident conversation
handlers/new-message.md           priority 10, pinned — barge-in
handlers/cancel.md                priority  5, pinned — the reflex
services/deep-research.md         priority 90 — the long job
services/send-email.md            priority 40 — the consequential one
system/{pipes,vectors,world-state,boot}.yaml
```

Carrying the v0.3 corrections, which are easy to get wrong because v0.2 had them wrong:

- `converse` writes `session.topic` through `actuators.topic`. An actuator write latches
  into exactly **one** world object, so `session.pending_task` needs its own actuator and
  belongs to `deep-research`, not to `converse`.
- `children: [deep-research, send-email]` is load-bearing — the `undeclared-spawn` rule
  rejects a body that spawns outside it.
- No `capabilities:` block. A descriptor that declares none may write the pipes it binds
  and nothing else; capabilities are for *adding conditions*, not granting basic access.
- `policy: queue` on `user-spoke`, not `coalesce`: the third message from a person is not
  a restatement of the first.
- Pipe capacities sized generously. Both limits are hard: a job's write larger than the
  pipe is a capability fault (#75), and a device delivery that does not fit is refused
  whole (#58). A long reply needs a roomy sink; a long pasted message needs a roomy
  `user.messages`.
- `deadline:` values are recorded and measured against, never enforced. Written as budgets
  the journal can score, and described that way wherever the UI mentions them.

**Status: done.** 5 descriptors, 0 errors, 0 warnings across all 40 rules; the run
preempts twice and replays byte-identically (`zeos replay --assert-identical`, 866
events); 7 tests, all asserting on the journal.

Two corrections to the design note came out of building it, both of the kind the v0.3
pass was making:

- **§2.1 and §2.2 cannot both hold as written.** `converse.stdin` is `user.messages` and
  the `user-spoke` vector fires from that same pipe — but `_dispatch_handler` calls
  `take_write()` on a vector's source, so the firing *takes* the message and hands it to
  the handler. The conversation is woken by the same write, finds the pipe empty when it
  retries its read, and parks again having seen nothing. One pipe, and the message
  reaches the handler and nobody else.

  The case therefore separates the content from the event: `user.messages` carries the
  words and is what `converse` reads, and `user.arrivals` is a **doorbell** — the adapter
  writes one fixed token to it per message, saying only that somebody spoke. The message
  is never copied, so it lives in exactly one place and the two lines cannot disagree,
  which they could if the text were written twice and one delivery were refused for want
  of room. It is also the shape coop-count already uses (`keys.interrupt` for the event,
  `keys.number` for the content); what is unusual about a conversation is only that the
  same text would serve as both, which is what makes duplicating it tempting.

  Crucially, which mechanism matters on a given message stays the *kernel's* decision: a
  parked conversation simply wakes and answers, and a busy one is preempted at priority
  10. Nothing in the adapter asks "is it busy?", which is the application-level branch a
  descriptor tree exists to remove.

  The principled alternative is a kernel change — `consumes: false` on a `VectorSpec`, so
  a firing takes a *copy* and leaves the write for its reader, making the note's
  single-pipe tree run as written. Worth having only if the handler must see the content,
  which it need not while `on_complete` is static. Note it is not a one-line flag:
  `take_write()` pops the oldest write and a reader pops from the same front, so a
  non-consuming vector needs its own cursor over write boundaries, or "the write that
  fired" and "the write not yet read" drift apart under `policy: queue`.
- **§2.2's three stack policies are one static field.** The note has `new-message`
  resolving per firing to `return`, `cancel-below:1` or `replace-with: converse`.
  `on_complete` is declared once per descriptor and nothing lets a handler choose at run
  time. The case takes `return` for `new-message` (the follow-up; let the answer finish
  and resume it with a diff) and `replace-with: converse` for `cancel` (the half-written
  answer is void). `cancel-below:1` has no descriptor, since ending the conversation
  outright leaves nothing listening.

  The behaviour the note wants is still reachable, just located elsewhere: `converse` gets
  the RESUME diff and the new message when it wakes, so its body instructs it to abandon a
  half-written answer when the subject has moved. There is a case for that being more
  right than the note — judging whether a new message has made an answer pointless is a
  judgement about meaning, and the job holding that answer is better placed to make it
  than a 256-token handler that cannot see it.

  The principled alternative, if per-firing choice is wanted: declare the envelope and
  choose within it. A `may_complete:` list in frontmatter, `exit <policy>;` through the
  ABI, `_complete(job, policy=...)` defaulting to the descriptor's (the policy is read in
  one place, `kernel.py:3460`), and a policy outside the declared set refused as a
  capability fault. The structure mirrors priority ceilings: the contract is in the
  frontmatter, the choice is in the job, and the job cannot exceed what was declared. It
  depends on C13, since the reason `exit <policy>;` cannot work today is the same reason
  `spawn <descriptor>;` cannot.

  These two principled fixes are one piece of work or neither: a run-time choice of
  completion policy is only worth having if the handler can see the content it is choosing
  on, which is what `consumes: false` provides.

Guards (`retrieval-endorser`, `session-endorser`) are deferred to phase 6 — they need
schemas, which is C8.

Also worth recording: `--machine scripted` is *not* a second way to run this case.
`ScriptedMachine` needs steps carrying explicit requests and `TapeSource` needs
`emit`-only steps, and `TapeSource` raises on a step with a request — so one `script:`
block cannot feed both. §6's "runnable two ways" holds for the machine choice, not for a
single tape. This case is written for the seat, which is the closer rehearsal for a real
model anyway.

## 4. The Claude command source

`zeos-chat/src/zeos_chat/claude.py`, adapted from `demo/coop-count/.../claude.py`.

- A **chat ABI**: `SyscallABI` with `max_text` far above the default sixteen words, since
  a reply is the payload. The default is sized for `write tools 50`.
- The system prompt is rendered from the ABI — `prose()`, `pattern()` — never a
  hand-written copy. The `unknown-body-verb` and `unbound-body-pipe` lint rules typecheck
  descriptor bodies against the same declaration.
- `on_fault` set deliberately for `malformed_request`. AM §11.3 is blunt that prose plus a
  pattern guarantees nothing, and that is exactly what an API-backed chatbot uses. These
  faults are routine, not exceptional; the case should expect them rather than discover
  the fault class in front of an audience.

**Status: built.** `src/zeos_chat/claude.py`, `--machine claude`, and a `.env` read at
startup. 22 tests, none of which need a key: the client is swapped for canned replies, so
what is tested is that whatever the model says becomes exactly one command, and that the
case runs unchanged against it.

Three things worth carrying forward:

- **The system prompt teaches the ABI and nothing about chat**, and a test asserts it.
  If it started explaining how to be a chatbot, `converse.md` would have stopped being
  the only place that says so.
- **coop-count's "write a bare number" does not transfer.** Its actuators carry counts;
  ours carry a topic. What transfers is the *latching* — a write replaces rather than
  appends — so the prompt says that instead.
- **A reply with no command is truncated to twelve words** before becoming a
  `malformed_request`. Otherwise a model answering a chatbot in prose spends two hundred
  decodes transcribing an essay before the kernel can fault on it.

**Latency, measured rather than guessed.** One command is one API call at ~2.2s, so a
turn costs as many seconds as it costs commands. A one-line answer is four calls and the
person waits ~7s for the first words. `effort` does not move it: high measured 1.8-2.0s
against medium 2.3-2.7s and low 2.4s, so the only lever is fewer round trips.

Two changes came out of that. `new-message` now issues a single `exit;` -- it said a line
first, which was invisible to the person, absent from the kernel panel, and redundant
against `JobSpawned` and `JobPreempted`, and cost 2.3s on every message. That took a turn
from 5 calls and 11.0s to 4 calls and 9.5s, and first words from ~8.6s to 7.4s.

Prompt caching is also in, splitting the request where it stops being stable, and it is
worth being precise about what it bought: **cache reads land (818 tokens over 9 calls) and
the latency did not move.** Per-call time is generation and round trip, not prefill, and
the prefix only passes the model's cacheable minimum once a conversation has run a while.
It is a cost saving that grows with conversation length, not a speed-up.

What is left is `converse` writing its topic before answering, which costs a whole round
trip per turn. Worth keeping -- it is what survives an interrupt -- but it should be spent
knowingly.

One small thing found and deliberately not fixed: `SyscallABI.verb` looks up
case-insensitively but a pipe alias does not, so a shouted `WRITE STDOUT hi;` reaches the
kernel with the alias as written and is refused as unbound. It faults rather than writing
somewhere unexpected, which is the right failure; a test documents it.

## 5. The web application

`zeos-chat/src/zeos_chat/web/` — server, static page, and the device adapter.

- **Server.** `ThreadingHTTPServer`. `GET /` serves the page; `POST /say` enqueues a
  message; `GET /events` is the SSE stream.
- **Device adapter.** The queue drain calling `kernel.deliver(user.messages, text)`
  between ticks. This is the piece that replaces coop-count's POSIX-only keyboard
  adapter, which is why C12 costs this demo nothing.
- **Reply path.** `Driver(on_drain=...)` → broadcast queue → SSE → the page.
- **The page.** Classic chatbot: message list, user and assistant turns, input box, send
  button, busy state. The animated logo is the busy indicator — bars as paddles moving up
  and down, centre dot as the ball moving side to side, animating while a job holds the
  machine and resting when the kernel is quiescent. CSS animation over the existing SVG
  geometry; no canvas, no dependency.
- **A kernel panel**, collapsed by default. Journal events as they happen: which job
  holds the machine, what blocked, what preempted what, which fault landed. This is where
  the demo earns its keep — the chat is the familiar surface, the panel is the thing worth
  showing. It reads the journal, never the transcript.
- **Configuration.** `.env` read at startup, following
  `demo/space-invaders/utils/config.py`: `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, and the
  mail settings. A missing key is a clear startup error, not a runtime surprise.

**Status: built.** `zeos-chat serve`, driven in a browser and checked there: a message
in, five paragraphs back one write at a time, the vector firing visible in the panel, and
a second turn on top. No console errors. 14 tests over real HTTP with a tape behind them,
so none needs a key.

The loop came out of the CLI into `session.py` first, which is what made the ordering bug
found in stage 2 testable — a delivery must be seen at the clock the loop has advanced
to, and there is now a test that fails if it is not.

Two things worth keeping in mind:

- **`Session.deliver` is the only method safe from another thread**, and it is safe
  because it only queues. Everything else runs on the thread that calls `step`.
- **The busy signal drives the logo**, and its *latency* is the whole of it. Publishing
  after a tick completes looked right under a tape and was badly wrong under the API
  seat: the first tick of a reply is one decode, a decode is a call taking seconds, and
  the page therefore sat still for exactly the span a person watches for a sign of life.
  It is now published the moment a message is queued, cleared when the kernel is
  quiescent *and* nothing is waiting to be picked up, set under the lock because two
  threads write it, and sent to a browser as it connects so a tab opened mid-reply
  animates at once.

Single tenancy is stated in the CLI banner. A read-only view for a second visitor was
scoped here and has been **dropped**: it would be a second way of saying what C3 already
says, and a page that carefully polices who may type is a poor substitute for a kernel
that can bind two jobs to two pipes. When C3 lands the view is unnecessary; until it
does, the banner is the honest version.

## 6. Protection: ring 3, endorsers, and the front door

The part that is actually about ZEOS rather than about chat.

- `web.results` at ring `EXTERNAL`, with `deep-research` reading it. Under every backend
  in the tree the job is demoted **on provenance** the moment that content is in its
  window, attended or not — θ_read is unused (Appendix A). So the endorser is not
  hygiene, it is the only way the job can still act.
- `guards/retrieval-endorser.md` and `guards/session-endorser.md`. Both need `schema:`,
  which means **C8** — a `system/schemas.yaml` threaded through `load_case`. Options: land
  C8 as a third kernel PR, or build the guards as in-memory descriptors. C8 is small and
  the `write-up-without-schema` lint rule is already pointing at the shut door;
  recommending we land it.
- Taint through the world: `converse` maps `session.topic` as a status region, so a
  tainted job writing that object demotes `converse` and costs it its own sink. The
  session endorser is what stands in the way. Worth demonstrating explicitly — it is the
  hazard v0.1 of the spec missed entirely.
- `system/principals.yaml` plus `utterances:` on `send-email` only. `converse` and both
  safety handlers declare none and are therefore structurally **deaf**, not merely
  protected.
- `mail.outbox` is an actuator latching into `mail.sent`. Simulated by default: the page
  shows what would have been sent. `--send-email` plus SMTP settings in `.env` turns on a
  genuine send.

**Done when:** integration tests assert on **journal properties** — that a privilege
fault occurred, which gate answered, that the guest's email never left — and never on
transcript text.

## 7. Tests and documentation

- Unit: the ABI parse, the source's reply extraction, the case's lint cleanliness.
- Replay: one scripted conversation, byte-identical from the same schedule and seed.
- Integration, on the journal: barge-in preempts within one boundary; the injected page
  demotes `deep-research` and its mail write faults; the guest cannot send email.
- `README.md`: what this is, what it demonstrates, what it deliberately does not claim,
  and the two limits — single tenancy (C3) and the reply being one atomic write (C7), so
  barge-in lands at the end of the write rather than inside it.

## 8. Risks

| Risk | Handling |
| --- | --- |
| C7 open: barge-in is invisible *within* a reply | Keep replies short by prompt, and show the preemption in the kernel panel where it is structurally visible. Landing C7 is a possible later PR. |
| C3 open: one visitor at a time | Stated in the CLI banner. Do not fake concurrency, and do not build a read-only view to dress it up. |
| Per-command API latency: one call per word-ish command | Batch what the ABI allows; a `write` carries a whole reply in one command. Measure before optimising. |
| Model drifting out of the ABI | Expected, not exceptional — `on_fault` set for `malformed_request` from the start. |
| The demo overclaiming real-time | No millisecond language anywhere. "Preempts at the next token boundary" is the true claim and the useful one. |

## 9. Sequence

1. ~~Case tree + scripted run~~ — **done**, and it needed neither a key nor weights.
2. ~~C13 PR (a spawn target through the ABI)~~ — **open as `zeos-internal#102`**; unblocks the two services on merge.
3. ~~C2 PR (job parameters)~~ — **open as `zeos-internal#104`**.
4. ~~C4 PR (the NLI front door on a pipe)~~ — **open as `zeos-internal#105`**.
5. ~~Claude command source~~ — **built**; same case, no descriptor changes.
6. ~~Web app on top~~ — **built**.
7. ~~C8 PR~~ — **open as `zeos-internal#106`**; then ring 3, endorsers, principals, the barrier.
8. Tests, README, polish.

C13 moved to the front because it is the smallest of the four and the case tree is
already waiting on it: until it lands, `children:` is a contract nothing can exercise.
Phase 5 does not depend on any of the kernel work and can run in parallel.
