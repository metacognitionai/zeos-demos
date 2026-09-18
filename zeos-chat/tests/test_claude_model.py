"""The model, asked for content and nothing else.

What is left to test here is small on purpose. The model no longer decides when to read,
when to write or when to record a topic, so there is no command extraction, no pattern to
search, and no `malformed_request` to shape. It answers one question and the answer is
prose.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from zeos_chat.claude import (
    EVICTED,
    PARAGRAPH_SEPARATOR,
    TRUNCATED,
    ClaudeModel,
    Refused,
    turns_of,
    without_persona,
)
from zeos_chat.llm import Ask


class Canned:
    """The SDK's streaming interface, replaced by a list of deltas."""

    def __init__(self, deltas: list[str], stop: str = "end_turn") -> None:
        self._deltas, self._stop = deltas, stop
        self.systems: list[str] = []
        self.prompts: list[Any] = []
        self.calls: list[dict[str, Any]] = []

    class _Stream:
        def __init__(self, deltas: list[str], stop: str) -> None:
            self.text_stream = iter(deltas)
            self.closed = False
            self._stop = stop

        def get_final_message(self) -> Any:
            return SimpleNamespace(stop_reason=self._stop)

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: object) -> None:
            self.closed = True

    @property
    def messages(self) -> Any:
        return self

    def stream(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        self.systems.append(kwargs["system"][0]["text"])
        self.prompts.append(kwargs["messages"])
        self.last = Canned._Stream(list(self._deltas), self._stop)
        return self.last


def model(deltas: list[str], stop: str = "end_turn") -> tuple[ClaudeModel, Canned]:
    client = Canned(deltas, stop)
    m = ClaudeModel()
    m._client = client  # pyright: ignore[reportPrivateUsage]
    return m, client


def words(m: ClaudeModel, ask: Ask) -> list[str]:
    return list(m(ask))


def test_the_persona_comes_from_the_descriptor_and_the_shape_from_here() -> None:
    """Who the model is being is the case's business -- it is the descriptor body, which
    is the same text a reader of the tree sees. How an answer is shaped is this module's."""
    m, client = model(["hello "])
    m.personas["converse"] = "You are a ship's navigator."
    words(m, Ask(descriptor="converse", prompt="where are we", reply_to="x"))  # type: ignore[arg-type]

    assert "ship's navigator" in client.systems[0]
    assert PARAGRAPH_SEPARATOR in client.systems[0], "the separator must be explained"


def test_a_descriptor_with_no_persona_still_gets_one() -> None:
    m, client = model(["hello "])
    words(m, Ask(descriptor="unknown", prompt="hi", reply_to="x"))  # type: ignore[arg-type]
    assert client.systems[0].strip()


def test_the_conversation_reaches_the_model() -> None:
    """The bug this guards: the model was asked the latest message and nothing else, so
    the chatbot could not carry on a conversation.

    The history is the *kernel's* window rather than a list kept here, which is what makes
    the descriptor's `context:` block mean something -- when it fills, the pager evicts old
    turns to STUBs and what the model can see shrinks with it. What this module decides is
    only how that window is rendered, and it is rendered as turns.
    """
    m, client = model(["hello "])
    words(
        m,
        Ask(
            descriptor="converse",
            prompt="and its population",
            reply_to="x",  # type: ignore[arg-type]
            window=(
                "read stdin; what is the capital of France write tools capitals; "
                "write ask what is the capital of France; read hear; "
                "write stdout Paris.; read stdin; and its population"
            ),
        ),
    )
    sent = [(m["role"], str(m["content"][0]["text"])) for m in client.prompts[0]]
    assert sent == [
        ("user", "what is the capital of France"),
        ("assistant", "Paris."),
        ("user", "and its population"),
    ]


def test_the_question_is_not_asked_twice() -> None:
    """The job reads, records the subject, *then* asks -- so what the person just said is
    already the last thing in the window when the model is called. Appending the prompt
    without dropping that trailing turn sent it twice."""
    m, client = model(["hi "])
    words(
        m,
        Ask(
            descriptor="converse",
            prompt="and its population",
            reply_to="x",  # type: ignore[arg-type]
            window="read stdin; what is the capital write stdout Paris.; read stdin; and its population",
        ),
    )
    asked = [str(t["content"][0]["text"]) for t in client.prompts[0] if t["role"] == "user"]
    assert asked.count("and its population") == 1


def test_the_persona_is_not_repeated_into_the_conversation() -> None:
    """The window opens with the descriptor body, and that body is already the system
    prompt. Sent whole it put a copy of the system instructions inside the user turn,
    followed by syscall-shaped text -- and the model refused the request outright."""
    persona = "You are a ship's navigator."
    m, client = model(["hi "])
    m.personas["converse"] = persona
    words(
        m,
        Ask(
            descriptor="converse",
            prompt="where are we",
            reply_to="x",  # type: ignore[arg-type]
            window=f"{persona} read stdin; where are we",
        ),
    )
    whole = " ".join(str(t["content"][0]["text"]) for t in client.prompts[0])
    assert "navigator" not in whole, "the persona was sent again as conversation"


def test_a_refusal_is_reported_as_one() -> None:
    """It used to produce no text, and the empty-answer fallback below turned that into
    "I have nothing to add." -- a shrug where the truth was a refusal."""
    m, _ = model([], stop="refusal")
    with pytest.raises(Refused):
        words(m, Ask(descriptor="converse", prompt="q", reply_to="x"))  # type: ignore[arg-type]


def test_words_written_before_a_refusal_are_kept() -> None:
    m, _ = model(["part ", "of an "], stop="refusal")
    said: list[str] = []
    with pytest.raises(Refused):
        for piece in m(Ask(descriptor="converse", prompt="q", reply_to="x")):  # type: ignore[arg-type]
            said.append(piece)
    assert " ".join(said).split() == ["part", "of", "an"]


def test_an_evicted_stretch_is_named_rather_than_dropped() -> None:
    """A model shown a gap fills it in. Naming it is what stops the answer inventing the
    part of the conversation the pager threw away."""
    turns = turns_of(
        "read stdin; hello write stdout hi.; <STUB id=ab segment=3 ring=USER "
        "integrity=2 tokens=40></STUB> read stdin; still there?"
    )
    assert any(EVICTED in text for _, text in turns)
    assert [role for role, _ in turns] == ["user", "assistant", "user"], "roles must alternate"


def test_a_window_that_does_not_open_with_the_persona_is_left_alone() -> None:
    assert without_persona("read stdin; hi", "You are a navigator.") == "read stdin; hi"


def test_a_job_with_no_context_yet_asks_without_one() -> None:
    """An empty window is left out rather than sent as an empty heading."""
    m, client = model(["hello "])
    words(m, Ask(descriptor="converse", prompt="hello", reply_to="x"))  # type: ignore[arg-type]
    assert len(client.prompts[0]) == 1, "an empty window must not become an empty turn"
    assert "hello" in str(client.prompts[0][0]["content"][0]["text"])


def test_an_answer_is_streamed_a_word_at_a_time() -> None:
    """Deltas from an SDK are often pieces of a word. What leaves here is whole words, so
    nothing downstream has to reassemble one -- and a write per fragment would be volume
    for nothing a person can see."""
    m, _ = model(["Nov", "ember is ", "peak ", "foliage."])
    said = words(m, Ask(descriptor="converse", prompt="q", reply_to="x"))  # type: ignore[arg-type]

    assert " ".join(said).split() == ["November", "is", "peak", "foliage."]
    assert not any(piece.endswith(" ") for piece in said), "a chunk carried loose whitespace"


def test_an_empty_answer_still_says_something() -> None:
    """The job is parked waiting for words. Silence would be indistinguishable from a
    model that never answered."""
    m, _ = model([])
    assert words(m, Ask(descriptor="converse", prompt="q", reply_to="x")) == [  # type: ignore[arg-type]
        "I have nothing to add."
    ]


def test_leaving_the_stream_early_closes_it() -> None:
    """Which is what makes stop stop the *model* rather than only the demo: the connection
    is closed rather than left to run and be paid for."""
    m, client = model([f"word{n} " for n in range(50)])
    stream = m(Ask(descriptor="converse", prompt="q", reply_to="x"))  # type: ignore[arg-type]
    next(stream)
    stream.close()

    assert client.last.closed, "the stream was left open"


def test_the_long_job_is_given_room_to_think() -> None:
    """The budget covers the model's reasoning as well as its words. At 2048 the long job
    spent the whole allowance thinking and emitted nothing -- `stop_reason: max_tokens`,
    one thinking block, zero characters -- which the empty-answer fallback then reported
    as "I have nothing to add.\""""
    m, client = model(["hi "])
    words(m, Ask(descriptor="deep-research", prompt="q", reply_to="x"))  # type: ignore[arg-type]
    words(m, Ask(descriptor="converse", prompt="q", reply_to="x"))  # type: ignore[arg-type]

    budgets = [call["max_tokens"] for call in client.calls]
    assert budgets[0] > budgets[1], f"the long job got no more room than a turn: {budgets}"
    assert budgets[1] >= 4096, "a reply that ends mid-sentence is what too small a cap looks like"


def test_an_answer_cut_off_by_the_budget_says_so() -> None:
    """An answer that stops mid-sentence with no explanation reads as a bug in the
    conversation rather than as a limit being reached."""
    m, _ = model(["a long answer "], stop="max_tokens")
    said = words(m, Ask(descriptor="converse", prompt="q", reply_to="x"))  # type: ignore[arg-type]
    assert TRUNCATED in said


def test_an_answer_that_simply_finished_says_nothing_extra() -> None:
    m, _ = model(["all done "])
    assert TRUNCATED not in words(m, Ask(descriptor="converse", prompt="q", reply_to="x"))  # type: ignore[arg-type]


def test_the_long_job_is_not_told_to_be_brief() -> None:
    """Its persona says to take the time it needs and that a short answer wastes the
    arrangement. The default shape says three short paragraphs and nobody is paying by the
    word — two instructions with no way to satisfy both."""
    m, client = model(["hi "])
    m.personas["deep-research"] = "You are a background job."
    words(m, Ask(descriptor="deep-research", prompt="q", reply_to="x"))  # type: ignore[arg-type]
    words(m, Ask(descriptor="converse", prompt="q", reply_to="x"))  # type: ignore[arg-type]

    long_job, turn = client.systems
    assert "nobody is paying by the word" not in long_job
    assert "as fully as the question deserves" in long_job
    assert "one to three short paragraphs" in turn, "the conversation still answers briefly"
    assert PARAGRAPH_SEPARATOR in long_job, "both shapes must explain the separator"
