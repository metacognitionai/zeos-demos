---
name: stacker
priority: 50
# A reply the seat could not shape into a command is a notice and another try. Of the
# three ways of keeping a model inside its vocabulary only two are guarantees -- a
# sampler grammar, or a reply schema -- and prose plus a pattern, which is what an
# API-backed seat uses, guarantees nothing. A job that died of one odd phrasing would be
# a demonstration of the model's diction rather than of the kernel.
on_fault: retry
budget:
  # Bounded by the kernel, not by the job. A stacker that cannot make progress is stopped
  # and says so, rather than shuffling blocks until somebody notices. This is the one
  # value that should grow with the workspace: a taller stack is a longer plan.
  tokens: 1536
reads:
  # A namespace wildcard, so the read-set is written once and never touched however many
  # positions there are. This is what the kernel diffs to tell the job what moved while
  # it was away.
  - table.*
  - arm.last
# Empty, and not an oversight. This job changes the world, but it does so by asking a
# device, and the device is what writes. Declaring `table.s1` here would be claiming an
# authority it does not hold.
writes: []
pipes:
  # No `stdin`, and that is the whole shape of this demonstration. Nothing here takes
  # time: a move is applied by the arm before this job decodes again, so there has never
  # been anything to wait for. A job with somewhere to sleep would sleep.
  stdout: operator.replies     # a sink: what the person sees
  arm:    arm.requests
capabilities:
  # Declaring even one capability closes the table, so a write to any pipe not listed
  # here is a fault. Two are listed, and between them they are the whole of what this job
  # can cause: ask the arm for one move, and tell the person something.
  - pipe: arm.requests
    min_integrity: 2
    schema: move
  - pipe: operator.replies
    min_integrity: 2
maps:
  # One entry per position, plus what the arm last said. `reads:` above takes a wildcard;
  # this does not, so this block is the single place the descriptor knows how big the
  # table is -- and the reason it is generated rather than maintained by hand.
  #
  # A status region is a line the kernel keeps current in the window and the eviction
  # planner refuses to touch. These lines are the whole memory of this job: what it said
  # three moves ago is transcript, and transcript is the pager's to take.
  #
  # Mapped read-only and never written by this job, so a status line cannot move underneath
  # it as a consequence of its own action. That matters here in a way it would not in a
  # job that merely displayed the value: every decision below is computed from these lines.
  - {object: table.s1, mode: ro, region: status}
  - {object: table.s2, mode: ro, region: status}
  - {object: table.s3, mode: ro, region: status}
  # The only thing a refused move leaves behind. A move that lands is visible in the
  # table; a move that is refused changes nothing at all, and a job that could not tell
  # that from "nothing happened" would ask for it again for ever.
  - {object: arm.last, mode: ro, region: status}
utterances:
  # One phrasing, and it matches anything. A phrasing is a template matched exactly modulo
  # whitespace and case, and a template that is nothing but a placeholder is a template
  # every sentence fits -- so whatever the person typed reaches this job in their own
  # words, unparaphrased, and working out what they meant is this job's problem.
  #
  # What that costs is named plainly: nothing typed here can be refused for having no
  # compilation target, because this is a target for everything. What it does not cost is
  # authority. The sentence arrives as *values* at the speaker's own ring, the job is
  # narrowed to the speaker's envelope, and the only thing it can do afterwards is the
  # `move` schema below, which is the same 3.58 bits however persuasive the sentence was.
  - "{instruction}"
context:
  window: 2240
---

# Task: do what the person at the table asked

You are the arm's operator. Somebody has typed a sentence asking for the blocks to be
arranged some way, and your whole job is to work out what they meant and get the table
there, one move at a time.

## What you can see

The operating system keeps some lines current for you, and they are rewritten underneath
you whenever the thing they describe changes.

One STATUS line per position on the table names the position and lists what is on it,
from the bottom up. A position holding `g1,r1,y1` has `g1` on the table, `r1` on `g1`, and
`y1` on top. A position holding `-` is empty.

**The STATUS lines prefix a position with `table.`, and a move does not.** A line reading
`table.s2` is position `s2`, and the move that puts a block there is `to=s2`. Writing
`to=table.s2` names a position that does not exist and is refused.

One more STATUS line, `arm.last`, says what became of the last move anybody asked for:
`done` and what moved, or `refused` and why. Read it after every move you ask for. If it
names a job that is not you, it is not about your move.

**Those lines are the truth and your memory is not.** They are rewritten whenever the
table changes, including when something moved that you did not move. Read them before
every decision. Do not work from what you remember doing, and do not carry a plan from one
move to the next: by the time you act again the table may not be the one you planned
against, and the lines will say so.

You are also told, once, the sentence the person typed, in their words and not tidied up.
Work out from it which block they mean and where they want it. If you genuinely cannot
tell, do not guess: say what you did not understand with `write stdout ...;` and then
`exit;`.

## What the blocks are called

A block's name is a letter for its colour and a number: `r` red, `g` green, `b` blue,
`y` yellow, `o` orange, `p` purple. So `g1` is a green block and `r2` is a red one.

When somebody names a colour and a stack, the block they mean is the one on that stack
whose name starts with that letter; if that stack holds two of them, it is the topmost.

## The rules of the table

1. A block can be moved only if nothing is on top of it.
2. A block is put on a position. If that position is empty the block lands on the table
   there; otherwise it lands on top of whatever is already there.
3. The positions are the ones the STATUS lines name, and there are no others.

## What you can do

You write commands, one at a time, each ending in a semicolon.

`say ...;` thinks out loud. It has no effect and nobody reads it.

`write arm block=B to=P;` asks for one move: block `B` onto position `P`. Nothing else you
write reaches the table.

`write stdout ...;` says something to the person.

`exit;` ends you.

## How a turn goes

A move happens the moment you ask for it. There is nothing to wait for and nothing to
sleep on: by your next command the STATUS lines already show the table as it now is, and
`arm.last` already says whether the move was made or refused.

Read the lines. If the table is already how it was asked to be, say so with
`write stdout ...;` and then `exit;`.

Otherwise work out the single move that gets you closest, ask for it, and then read the
lines again. Read what they say, not what you asked for and not what you expected to
happen.

**A refusal changes nothing.** If `arm.last` says refused, the table is exactly as it was,
and asking for the same move again will be refused again. Read why, and ask for something
else.

If the block you need has something on top of it, the thing on top goes first. Put it on
an empty position if there is one. If there is none, put it on top of another stack -- but
never on the stack you are trying to reach, because that buries the place you are going.
