# Contributing

This repository holds applications built on [ZEOS](https://github.com/metacognitionai/zeos),
a transformer operating system. Each demonstration is its own directory and its own
project (e.g. `zeos-chat`) and may include its own dependencies.

Contributions are made under the MIT licence in [LICENSE](LICENSE).

## Where a change belongs

A demonstration exists to show what the kernel already does, so a change that makes one work
by special pleading in Python has usually gone to the wrong place.

- **Here:** cases, descriptors, adapters, front ends, documentation.
- **In `zeos`:** anything the kernel does — scheduling, pipes, rings, capabilities,
  paging, the syscall ABI. If a demo cannot express something the design says it should,
  that is a kernel issue, not a workaround.

## Before you open a pull request

Each demonstration carries its own tooling and its own README that says how to run it.
Whatever the commands, four things must hold before a pull request will be accepted:

- the test suite passes;
- the linter and formatter are clean;
- for a demonstration with a case tree, `lint` reports zero errors and zero
  warnings over that tree;
- the change runs. A demo that passes its tests and is wrong in front of you has not
  been tested.

The case lint is the one people skip. Don't. It catches design errors rather than typos:
a speaker whose priority ceiling reaches the safety tier, two jobs writing one world
object in an order the kernel does not define, a body asking for a verb its ABI does not
declare. A tree that lints clean is a tree that can run.

CI runs the first three for every demonstration on every pull request. It finds the
demonstrations rather than listing them: a top-level directory with a `pyproject.toml`
is one, and is expected to be a uv project whose script of the same name has a `lint`
subcommand, as `zeos-chat lint` is. A new demonstration in that shape is covered the
moment it is committed.

## How the work should read

[AGENTS.md](AGENTS.md) is the full set of conventions and is worth reading once before a
first contribution.

## Commits and pull requests

Write the commit message for someone trying to understand the change a year from now. Say
what the code now does and why, rather than restating the diff. Where a change corrects
something, the commit is the right place for that history; it does not belong in the
code.

**Disclose AI assistance.** If a model wrote or substantially shaped a change, say so in
the pull request, and add a `Co-Authored-By:` trailer to the commits it worked on. Much of
this repository was written that way and the history records it per commit. A reviewer
deciding how closely to read something is entitled to know.

## Reporting a bug

A demonstration bug is worth reporting when you can say what you did and what you
expected instead. Name the sequence rather than the symptom: what happened before it is
usually what the bug is about.

Every ZEOS demonstration has a journal, and it is the most useful thing you can attach.
It records what the kernel actually did, which job held the machine, what blocked, what
preempted what, which fault landed, so it settles in one read what a description of the
symptom can only suggest.
