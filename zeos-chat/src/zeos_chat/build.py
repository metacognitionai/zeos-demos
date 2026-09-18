"""Assembling a session: the jobs, the model behind its pipe, and the kernel between them.

One place, because the CLI and the web server want exactly the same thing and the wiring
is the part worth only writing once.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from zeos.core.events import Event
from zeos.core.ids import PipeName
from zeos.descriptor.loader import CaseBundle

from zeos_chat.jobs import ProgramSource
from zeos_chat.llm import Ask, LlmAdapter, StubModel
from zeos_chat.mail import MailAdapter
from zeos_chat.session import Session

__all__ = ["MODELS", "build_session", "personas_for"]

#: How the model is answered. `stub` needs no key and says the same thing twice, which is
#: what makes a run reproducible now that the tapes are gone -- the control flow is Python
#: and therefore fixed, so the model is the only thing left that could vary.
MODELS = ("claude", "stub")


def personas_for(bundle: CaseBundle) -> dict[str, str]:
    """Each descriptor's body, by name -- what the model is told to be.

    Pulled out so it can be checked without an SDK or a key, because the property worth
    checking is that nothing here rewrites or summarises a body on the way past.
    """
    return {str(name): descriptor.body for name, descriptor in bundle.descriptors.items()}


def _model(name: str | Callable[[Ask], str], bundle: CaseBundle) -> Callable[[Ask], str]:
    """A name, or a model itself -- a test supplies its own answers that way.

    A real model is handed the case's own bodies as personas. That is what keeps the
    descriptor the single place a behaviour is described: the text a reader of the tree
    sees is the text the model is told to be, rather than a copy of it kept in Python and
    free to drift.
    """
    if callable(name):
        return name
    if name == "stub":
        return StubModel()
    from zeos_chat.claude import ClaudeModel

    model = ClaudeModel()
    model.personas = personas_for(bundle)
    return model


def build_session(
    bundle: CaseBundle,
    *,
    model: str | Callable[[Ask], str] = "stub",
    on_reply: Callable[[PipeName, str], None] | None = None,
    on_event: Callable[[Sequence[Event]], None] | None = None,
    journal: Path | None = None,
    mail: MailAdapter | None = None,
    seed: int = 0,
    block_size: int = 16,
    max_ticks: int = 100_000,
) -> tuple[Session, LlmAdapter, ProgramSource]:
    """A session whose jobs are Python, and whose model and mailbox are devices."""
    source = ProgramSource()
    # The adapter delivers through the session's own queue, which is what keeps the reply
    # arriving on the kernel's thread rather than on the worker's.
    box: list[Session] = []
    chosen = _model(model, bundle)
    # A stub answers instantly, so it answers in line and a run is reproducible by
    # construction rather than by thread scheduling. Anything slower gets the worker,
    # which is what keeps the kernel's tick short while it thinks.
    adapter = LlmAdapter(
        chosen,
        lambda pipe, text: box[0].deliver(pipe, text),
        window_of=source.window_of,
        inline=isinstance(chosen, StubModel),
    )
    session = Session(
        bundle,
        source,
        on_reply=on_reply,
        on_event=on_event,
        on_arrival=source.note_arrival,
        llm=adapter,
        # Simulated unless MAIL_LIVE says otherwise -- see mail.py.
        mail=mail if mail is not None else MailAdapter(),
        journal=journal,
        seed=seed,
        block_size=block_size,
        max_ticks=max_ticks,
    )
    box.append(session)
    return session, adapter, source
