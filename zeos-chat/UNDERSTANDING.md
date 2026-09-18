# ZEOS-Chat — shared understanding

Working brief for the demo, written before any code. Records what I take the task to be,
what I verified in the tree rather than assumed, and the decisions still open.

## 1. What this is

A **demonstration**, not a product, and not a claim that ZEOS is for chatbots. ZEOS is a
transformer operating system — a kernel that schedules, protects and pages LLM jobs. A
chatbot is chosen because it is the one application everybody already understands, so the
kernel mechanisms are visible against a familiar backdrop rather than competing with an
unfamiliar one for the viewer's attention. The demo showcases ZEOS features; the honest
framing throughout is *this is a small corner of what the kernel does*.

The specification is the Notion page **"ZEOS-Chat — A Chatbot as a Descriptor Tree"**
(design draft v0.3, 2026-09-18), revised against zeos commit `bae65d3`. Sections 1–4 are
the design; 5–7 are the prioritised kernel gaps.

## 2. Deliverable

A **fully functional chatbot in a web page**, classic look and feel: message list, user
turns and assistant turns, an input box, a send button, a visible busy state. Not a
terminal transcript dressed up — a chatbot as a person expects one.

Behind it, the conversation is a **ZEOS descriptor tree**, not a chat loop:

- waiting for the user is `JobBlocked` on `read stdin;` — zero forward passes;
- a message arriving mid-answer is an **interrupt vector** firing a pinned handler, not
  application-level cancellation;
- the reply leaves through a **sink pipe** drained by the driver;
- retrieval is a **ring-3 read**, so prompt injection is a capability fault at a pipe
  boundary rather than a prompt-engineering problem.

### Branding and the animated logo

The Metacognition logo is, structurally, already a Pong board: two tall vertical bars and
a small square between them.

```svg
<svg viewBox="0 0 88 88" fill="currentColor">
  <rect x="12" y="2"  width="9"  height="67"/>   <!-- left bar   -->
  <rect x="37" y="31" width="12" height="13"/>   <!-- centre dot -->
  <rect x="66" y="18" width="9"  height="68"/>   <!-- right bar  -->
</svg>
```

Animated: the two bars travel up and down as paddles, the centre dot travels side to side
as the ball, the paddles meeting it at each end. Used as the busy indicator, so the
animation means something — it runs while a job holds the machine, and rests when the
kernel is quiescent.

### Configuration

The app needs configuring with:

1. an **Anthropic API key** — the `CommandSource` behind the seat is the Claude API;
2. the **user's email account** — because `send-email` is the case's consequential
   actuator, the effect a demoted job is refused. A real send is what makes the
   capability check worth watching.

## 3. What the kernel already gives us (verified in the tree, not assumed)

Checked against `zeos-internal` at `209205f` — one commit past the `bae65d3` the spec was
revised against, and that commit is a run-all script, so the spec is current.

| Need | Mechanism | Where |
| --- | --- | --- |
| Reply out to the browser | `PipeSpec.sink`, `Kernel.drain`, `Driver(on_drain=...)` | `driver.py:_drain_sinks` |
| User message in | `Kernel.deliver(pipe, text)` from a device adapter | `kernel.py:627` |
| Barge-in | vector table, `policy: queue`, pinned handler | `core/vectors.py` |
| A model speaking syscalls | `CommandSeat` + a `CommandSource` | `machine/seat.py` |
| The command vocabulary | `SyscallABI`, rendered as prose / pattern / grammar | `machine/abi.py` |
| A Claude-backed source | `ClaudeSource` — already exists, in coop-count | `demo/coop-count/.../claude.py` |
| Case on disk | `load_case` over `goals/ handlers/ services/ guards/ system/` | `descriptor/loader.py` |
| Load-time typechecking | 40 lint rules, incl. `undeclared-spawn`, `confused-deputy` | `descriptor/lint.py` |
| A web front end precedent | static page + `ThreadingHTTPServer`, assets inlined | `demo/space-invaders/web/` |

So §4 of the spec holds: what remains in Python is a `CommandSource`, a device adapter
turning socket traffic into `kernel.deliver` calls, and an `on_drain` callback. Plus, for
this demo, the page itself and a run loop that keeps ticking while a person types.

## 4. The gaps that shape the demo

Verified open, not merely documented as open:

- **C2 — job parameters.** `Kernel.spawn` takes `parent`, `priority`, `vector`, `owner`
  and nothing else (`kernel.py:416`). A compiled invocation loses its arguments: "email
  Alice" spawns `send-email` with no idea who Alice is.
- **C3 — per-session instancing.** No `binds=` on `spawn`, and `pipes:` in frontmatter is
  literal names. **A ZEOS chatbot is single-tenant today** — one conversation, one
  visitor. For a web demo that is the load-bearing constraint, not a footnote.
- **C4 — the NLI front door.** `handle_utterance` (`kernel.py:1641`) is reachable only by
  a caller holding the `Kernel` object; nothing in `src/` or `demo/` calls it. There is no
  route from *text arrived on a ring-3 pipe* to *compiled against the phrasing table*.
- **C7 — streaming writes.** A reply is one atomic write, so the job is unpreemptible for
  exactly the span the user is watching. Barge-in is structurally real but lands at the
  end of the write rather than inside it.
- **C8 — `schemas.yaml`.** The loader never passes `schemas=`, so `schema: narrow-summary`
  on a capability fails at load. The endorsers in the spec's tree need this.
- **C12 — Windows console adapter.** `demo/coop-count/.../keyboard.py` imports `termios`
  and `tty`. **A web front end sidesteps this entirely** — the browser is the device
  adapter, so this gap costs us nothing and development can happen on Windows.

Two further honesty constraints from v0.3, to be reflected in whatever the page claims:

- **"Real-time" means token boundaries, not wall clock.** The kernel guarantees the most
  urgent runnable job dispatches next, and that preemption lands within one decode step.
  It reads no clock; a `deadline:` is recorded and measured against, never enforced. The
  page must not imply a millisecond promise.
- **Demotion is on provenance, not attention,** under every backend in the tree, because
  none can measure attention mass (θ_read is unused — Appendix A). A job is demoted the
  moment dirty text is *in* its window, read or not. The endorser therefore stops being
  hygiene and becomes the only way a job that touched the outside world can still act.

## 5. Staging (spec §6), as I read it for this demo

1. `cases/chat-scripted/` + `events.jsonl` — proves the wiring, replays byte-identically,
   needs no key and no weights. Runs two ways: `--machine scripted` and `--machine seat`.
2. Swap in a Claude `CommandSource`. Same case, no descriptor changes. Expect
   `malformed_request` faults; set `on_fault` for them deliberately.
3. Add `web.results` at ring 3 plus the retrieval endorser and the session endorser.
   Assert on the **journal** that a privilege fault occurred — a structural fact, never
   transcript text.
4. Add `principals.yaml` and `utterances:`. Port the barrier test: a guest cannot get an
   email sent, and the journal says which gate answered.

The web page arrives alongside step 2 and is what steps 3 and 4 are demonstrated *in*.

## 6. Repository conventions to respect

This repo (`zeos_demos`) is **MIT**; `zeos` itself is **AGPL-3.0-only** and imported as a
dependency. From `zeos-internal/AGENTS.md`, worth carrying across where it applies:
British English in prose, determinism as the acceptance gate, integration assertions
against journal properties rather than transcript text, no guards "just in case", and
document what exists rather than what is planned.

## 7. Decisions taken

Settled 2026-09-18. `PLAN.md` builds on these.

1. **Land C2 and C4 in `zeos-internal` first**, as the spec's §6 minimum viable pair: job
   parameters as a ring-0 segment at spawn, and the NLI front door wired to a pipe. Then
   build the demo on the kernel that results. **C3 stays open**, so the demo is
   single-tenant and says so — one conversation at a time.
2. **Email is simulated by default, real behind a flag.** The actuator write latches into
   `mail.sent` and the page shows what would have been sent; credentials plus a flag turn
   on a genuine send for a live showing.
3. **Configuration is a server-side `.env`**, the pattern the other zeos demos already
   use. The API key never reaches the browser.
