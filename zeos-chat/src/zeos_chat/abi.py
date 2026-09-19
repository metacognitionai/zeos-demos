"""The syscall vocabulary a chat job speaks.

A case declares its own ABI, and this is the chat one. Two things make it different
from ``zeos.machine.abi.DEFAULT``: a payload cap sized for a reply rather than for a
number, and a ``spawn`` verb, because a conversation that dispatches work needs a way
to ask for it.

One declaration, three renderings: the prose a model is told (``prose()``), the pattern
its reply is searched with (``pattern()``), and the grammar a sampler could be
constrained by. The descriptor bodies are typechecked against it by the
``unknown-body-verb`` and ``unbound-body-pipe`` lint rules, which is why the demo lints
with this ABI rather than with the default -- see ``cli.py``.
"""

from __future__ import annotations

from zeos.machine.abi import SyscallABI, Verb
from zeos.machine.base import OpKind

__all__ = ["CHAT"]

#: The vocabulary of a conversation.
#:
#: ``max_text`` is 512 rather than the default sixteen. The default is sized for
#: ``write tools 50``; here the payload *is* the reply, and a cap below the length of an
#: answer turns every long reply into a truncated one. Note that this is a declaration
#: the grammar and the prompt are rendered from, not a check the kernel performs: what
#: actually refuses an over-long write is the pipe's capacity, and a write larger than
#: the whole pipe is a capability fault rather than a wait (#75).
CHAT = SyscallABI(
    verbs=(
        Verb(
            "say",
            doc="think out loud. No effect, and nobody reads it",
            text=True,
        ),
        Verb(
            "write",
            OpKind.WRITE,
            pipe=True,
            text=True,
            doc="put text on a pipe. This is how anything happens",
        ),
        Verb(
            "read",
            OpKind.READ,
            pipe=True,
            doc="sleep until something arrives on that pipe",
        ),
        # How `converse` dispatches the long job. The target survives the parse, so the
        # kernel resolves it against `children:` and refuses anything not declared there
        # as a capability fault -- the check is the kernel's, not the parent's.
        Verb(
            "spawn",
            OpKind.SPAWN,
            text=True,
            doc="start one of the jobs this descriptor declares as a child",
        ),
        Verb("exit", OpKind.EXIT, doc="finish"),
    ),
    max_text=512,
)
