"""The model, asked for content and nothing else.

This used to be a ``CommandSource``: it answered the kernel's decodes, so the model chose
when to read, when to write and when to record the topic, at one API call each. That put
the model in the scheduler's chair -- the opposite of what ZEOS claims -- and made a
one-line answer cost four round trips and seven seconds.

Now it sits behind a pipe and is asked one question: *given this persona and this
conversation, what would you say?* The control flow is Python, the scheduling is the
kernel's, and this is the part neither of them can do.

What went with the change: the syscall ABI rendered as prose, the pattern a reply was
searched with, and `malformed_request` as a fault met in practice. Nothing here can
produce a malformed command any more, because nothing here produces commands.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from typing import Any

from anthropic import Anthropic

from zeos_chat.llm import Ask

__all__ = [
    "EVICTED",
    "ClaudeModel",
    "DEFAULT_MODEL",
    "PARAGRAPH_SEPARATOR",
    "Refused",
    "turns_of",
    "without_persona",
]

DEFAULT_MODEL = "claude-opus-5"

#: How the model is asked to separate paragraphs, so the job can write them one at a time.
#: A separator rather than a blank line because the seat splits on whitespace: a newline
#: does not survive the trip into a command, and this does.
PARAGRAPH_SEPARATOR = "|"

#: What the model is told about the shape of an answer -- not about the application. Who
#: it is answering *as* comes from the descriptor body, which is where a behaviour's
#: persona belongs.
SHAPE = """\
Answer in one to three short paragraphs, separated by the character {separator}.
Do not number them, do not use headings, and do not use the character {separator} for
anything else. Write plainly and stop when you have answered; nobody is paying by the
word.\
"""


#: What a stubbed-out stretch of window becomes when it is read back as a turn. The pager
#: replaces evicted text with a `<STUB ...>` header, and a model shown one will otherwise
#: fill the hole in: this says plainly that something is missing and is not recoverable.
EVICTED = "[earlier part of this conversation discarded by the operating system]"

#: The two things in a window that are conversation. `read stdin;` is followed by what the
#: person said, up to whatever command comes next; `write stdout ...;` is what was said
#: back. Everything else -- `write tools`, `write ask`, `read hear`, `<STATUS ...>` -- is
#: the job talking to the kernel, and belongs to the journal rather than to the transcript.
_TURN = re.compile(
    r"read stdin;(?P<user>.*?)(?=\bwrite\s|\bread\s|<STATUS|<STUB|\Z)"
    r"|write stdout (?P<said>.*?)(?:;|\Z)"
    r"|(?P<evicted><STUB\b.*?</STUB>)",
    re.S,
)


def turns_of(window: str) -> list[tuple[str, str]]:
    """The conversation inside a window, as ordinary chat turns.

    The window is not handed over raw, and that is a measured decision rather than a
    tidiness one. Raw, it is the persona followed by syscall-shaped text -- and the model
    *refuses* it: framings were compared, and every one carrying the commands and a gloss
    of what they mean was declined, reliably as the window grew. Text that teaches a model
    to read command syntax as conversation has the shape of a prompt injection, and is
    treated as one.

    So the same content is presented as what it actually is: who said what, in order. What
    this is not is a history kept in Python. Every turn here was read out of the kernel's
    window this instant, so the `context:` declaration still governs what the model can
    see -- when the pager evicts, turns vanish from here too, which is what ``EVICTED``
    marks. Only the rendering is ours; the remembering is still the kernel's.
    """
    turns: list[tuple[str, str]] = []
    for match in _TURN.finditer(window):
        if match.group("evicted") is not None:
            role, text = "assistant", EVICTED
        elif match.group("user") is not None:
            role, text = "user", match.group("user")
        else:
            role, text = "assistant", match.group("said")
        text = " ".join(text.split())
        if not text:
            continue
        # Runs of the same speaker join up. A streamed answer is many `write stdout`
        # commands and was one thing said, and the API wants roles to alternate anyway.
        if turns and turns[-1][0] == role:
            turns[-1] = (role, f"{turns[-1][1]} {text}")
        else:
            turns.append((role, text))
    return turns


class Refused(Exception):
    """The model declined to answer.

    Its own error, and it travels back down the reply pipe like any other failure. The
    alternative was met here in practice and is worse: the first streaming version checked
    nothing, so a refusal produced no text and the fallback below turned it into "I have
    nothing to add." A refusal reported as a shrug is a lie about what happened.
    """


def without_persona(window: str, persona: str) -> str:
    """The window with its opening persona removed.

    The kernel's window opens with the descriptor body, and that same body is the system
    prompt -- so sending the window whole puts a copy of the system instructions inside the
    user turn, followed by syscall-shaped text. That is the shape of a prompt injection and
    the model refuses it: measured, `body + commands` is declined where either half alone
    is answered. Nothing is lost by cutting it, because the model has already been told it.

    Compared word by word rather than as a string: the machine collapses whitespace on the
    way into a window, so the body there is not the body on disk character for character.
    """
    if not persona.strip():
        return window
    kept = window.split()
    for word in persona.split():
        if not kept or kept[0] != word:
            return window  # not the prefix we expected -- leave it alone
        kept.pop(0)
    return " ".join(kept)


class ClaudeModel:
    """Answers an ``Ask`` with the Claude API. One call, one answer, no commands."""

    def __init__(self, model: str | None = None) -> None:
        self._client = Anthropic(max_retries=4)
        self._model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
        #: Personas, by descriptor. Filled from the case's own bodies, so what the model
        #: is told to be is the same text a reader of the tree sees.
        self.personas: dict[str, str] = {}
        self.calls = 0

    def system_for(self, descriptor: str) -> str:
        persona = self.personas.get(descriptor, "You are a helpful assistant.")
        return f"{persona}\n\n{SHAPE.format(separator=PARAGRAPH_SEPARATOR)}"

    def _messages(self, ask: Ask) -> list[dict[str, Any]]:
        """The conversation as turns, then the thing to answer.

        The trailing user turn is dropped before the prompt is appended: the job records
        the subject and writes its request *after* reading, so by the time the model is
        asked, what the person just said is already the last thing in the window. Appending
        without dropping asked it twice.
        """
        history = turns_of(without_persona(ask.window, self.personas.get(ask.descriptor, "")))
        while history and history[-1][0] == "user":
            history.pop()
        messages: list[dict[str, Any]] = []
        for role, text in history:
            block: dict[str, Any] = {"type": "text", "text": text}
            messages.append({"role": role, "content": [block]})
        if messages:
            # The cache break goes on the last settled turn: everything before it is what
            # last turn's request already ended with, and the new question is what changed.
            messages[-1]["content"][-1]["cache_control"] = {"type": "ephemeral"}
        messages.append({"role": "user", "content": [{"type": "text", "text": ask.prompt}]})
        return messages

    def __call__(self, ask: Ask) -> Iterator[str]:
        """The answer as it is written, a word at a time.

        Streamed for two reasons, and the second is the better one. The words reach the
        person as they are produced rather than in one go at the end -- and leaving this
        iterator early *closes the connection*, so a person who presses stop stops the
        model rather than merely stopping the demo from showing what it said.

        Grouped into words rather than passed on raw: the deltas an SDK yields are often
        pieces of a word, and a pipe write per fragment is volume for nothing a person can
        see.
        """
        self.calls += 1
        said = False
        with self._client.messages.stream(
            model=self._model,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": self.system_for(ask.descriptor),
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=self._messages(ask),
        ) as stream:
            held = ""
            for delta in stream.text_stream:
                held += delta
                # Everything up to the last space is complete; the tail may be half a word
                # and waits for the next delta. Several words can go out together, which is
                # the right batching -- a write each would be volume for nothing visible.
                cut = held.rfind(" ")
                if cut == -1:
                    continue
                done, held = held[:cut].strip(), held[cut + 1 :]
                if done:
                    said = True
                    yield done
            if held.strip():
                said = True
                yield held.strip()
            if stream.get_final_message().stop_reason == "refusal":
                raise Refused(
                    "the model declined to answer"
                    if not said
                    else "the model stopped part way through and declined to go on"
                )
        if not said:
            yield "I have nothing to add."
