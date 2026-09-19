# Conventions

How code and prose are written in this repository. `zeos` itself carries its own
`AGENTS.md`; this is the demos side, and where the two overlap they agree.

## Licensing

`zeos_demos` is **MIT**. `zeos` is **AGPL-3.0-only** and imported as a dependency, never
vendored.

## Prose

**Australian English**, in documentation and in comments.

**Document what exists.** Not what is planned, not what was removed, and not how the work
came to be the way it is. A reader arrives wanting the system as it stands; the history is
the log's business.

**The code and the cases are public, and must stand on their own.** A descriptor, a
comment or a test should be understandable without reference to any private design
document. Cite a section of one and the reader you were writing for cannot follow it.

## Tests

**Determinism is the acceptance gate.** A scripted run replays byte-identically from the
same schedule and seed. Where that is at risk, fix the cause rather than loosening the
assertion — a stub model answers in line rather than on a worker thread precisely so a run
is reproducible by construction and not by luck of scheduling.

**Assert on journal properties, never on transcript text.** That a privilege fault
occurred, which gate answered, that no letter left — these are structural facts the kernel
records. A test that greps a model's words is testing the model.

**A test should be able to fail.** Assertions that hold whatever the system does are
worse than no test, because they are counted.

## Code

**No guards "just in case".** A check that never fires looks exactly like a check that
passes, and the two are told apart only by a case where the check is *expected* to refuse.
If a condition cannot happen, do not test for it; if it can, there should be a test where
it does.

**Comments say why, not what.** The interesting comment is the one recording a constraint
that is not visible from the code — an ordering that matters, a threading rule, a thing
that was tried and does not work. Don't document development history, that belongs in the
git commit messages.

**All I/O at the edge.** The kernel is not re-entrant: one thread owns it, and anything
arriving from elsewhere is queued and drained between ticks.
