"""The syscall vocabulary a stacker job speaks.

A case declares its own ABI, and this is the blocks one. It differs from the default in
two ways: a payload capped at four words, because a move is `block=g1 to=s2` and a cap is
what stops one command carrying a whole plan, and no `read`.

**There is no `read` because nothing in this workspace takes time.** A move is applied by
the device that receives it and the status regions are rewritten before the job's next
decode, so a job that asked the arm for something and then slept would be sleeping for an
answer it had already been given. A verb the vocabulary declares is a verb the model will
eventually emit, and one that could only ever name a pipe no descriptor binds is a fault
raised on the model's behalf for doing what it was told.

One declaration, three renderings: the prose a model is told (`prose()`), the pattern its
reply is searched with (`pattern()`), and the grammar a sampler could be constrained by.
The descriptor bodies are typechecked against it by the `unknown-body-verb` and
`unbound-body-pipe` lint rules, which is why `cli.py` lints with this ABI rather than with
the default -- a body naming a verb the vocabulary does not have would otherwise teach the
model a word it can never use, and the failure would surface nowhere near the body.

Nothing here changes with the size of the workspace.
"""

from __future__ import annotations

from zeos.machine.abi import SyscallABI, Verb
from zeos.machine.base import OpKind

__all__ = ["BLOCKS"]

BLOCKS = SyscallABI(
    verbs=(
        Verb("say", text=True, doc="think out loud. No effect, and nobody reads it"),
        Verb(
            "write",
            OpKind.WRITE,
            pipe=True,
            text=True,
            doc="put text on a pipe. This is how anything happens",
        ),
        Verb("exit", OpKind.EXIT, doc="finish"),
    ),
    max_text=4,
)
