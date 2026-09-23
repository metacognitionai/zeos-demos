"""The API planner's half: what the model is told, and what is made of what it says.

None of this calls the API. What can go wrong without one is the prompt being wrong and
the reply being misread, and both are worth pinning because the symptom of either is a run
that looks like the model failing to understand.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from zeos.machine.seat import Turn, seat_maps

from zeos_blocks.abi import BLOCKS
from zeos_blocks.planner import (
    DEFAULT_MAX_TOKENS,
    EMPTY_LIMIT,
    ClaudePlanner,
    one_command,
)


def planner(descriptors=None) -> ClaudePlanner:
    """Built without touching the SDK, which needs a key we do not have here."""
    made = ClaudePlanner.__new__(ClaudePlanner)
    made._client = None
    made._model = "test"
    made._descriptors = {k: tuple(v) for k, v in (descriptors or {}).items()}
    made._aliases = ("stdout", "arm")
    made._max_tokens = DEFAULT_MAX_TOKENS
    made._empty = 0
    made._backoff = 0.0
    return made


def test_each_descriptor_is_told_only_the_pipes_it_binds(bundle) -> None:
    """A job told it may name a pipe its descriptor does not bind will name it, and be
    refused at the boundary for doing what it was told it could."""
    descriptors, _ = seat_maps(bundle.descriptors, bundle.pipes)
    made = planner(descriptors)
    assert "stdout, arm" in made.system("stacker")
    assert "stdout." in made.system("noticed")
    assert "arm" not in made.system("noticed").split("may name are:")[1].split("\n")[0]


def test_an_unknown_descriptor_falls_back_rather_than_crashing() -> None:
    assert "stdout, arm" in planner().system("something-else")


@pytest.mark.parametrize(
    ("reply", "wanted"),
    [
        ("write arm block=y1 to=s3;", "write arm block=y1 to=s3"),
        ("write arm block=y1 to=s3", "write arm block=y1 to=s3"),
        ("Looking at the table.\n\nwrite arm block=y1 to=s3;", "write arm block=y1 to=s3"),
        ("```\nwrite arm block=y1 to=s3;\n```", "write arm block=y1 to=s3"),
        ("WRITE STDOUT DONE;", "write stdout done"),
        ("write arm block=y1 to=s3.", "write arm block=y1 to=s3"),
    ],
)
def test_a_command_is_found_however_it_is_wrapped(reply, wanted) -> None:
    assert one_command(reply) == wanted


def test_a_reply_with_no_command_becomes_a_malformed_request() -> None:
    """Which is a fault the job is told about and retries, not a crash.

    Worth asserting rather than assuming: the alternative is the whole reply being spent
    as if it were a command, and the job never finding out.
    """
    reply = "I need more information about the table."
    assert BLOCKS.parse(one_command(reply)).op.value == "malformed"


def test_the_last_command_is_not_described_as_having_succeeded() -> None:
    """It may have been refused, or not been a command at all. Saying it is done is a lie
    the model then has to reason around, while the kernel's fault notice says otherwise."""
    made = planner()
    turn = Turn(job=1, descriptor="stacker", transcript="t", issued=1, last="write arm nonsense")
    sent = {}

    class Messages:
        def create(self, **kw):
            sent.update(kw)
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="exit;")],
                stop_reason="end_turn",
                usage=SimpleNamespace(output_tokens=3),
            )

    made._client = SimpleNamespace(messages=Messages())
    assert made.next_command(turn) == "exit"
    prompt = sent["messages"][0]["content"]
    assert "already been issued" in prompt
    assert "is done" not in prompt


def reply(text, *, stop="end_turn", out=8):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)] if text else [],
        stop_reason=stop,
        usage=SimpleNamespace(output_tokens=out),
    )


def test_an_empty_reply_is_reported_and_then_raises(capsys) -> None:
    """A reply with no content at all, retried and then given up on.

    Two things look like this. `max_tokens` too low cuts the model off before it writes
    anything; a refusal returns nothing with `stop_reason: refusal`, and that one is
    intermittent, since replaying a refused prompt unchanged gets a correct answer. The
    limit is generous for that reason, and the message names which happened.
    """
    made = planner()
    made._max_tokens = 64
    made._client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: reply("", stop="refusal", out=0))
    )
    turn = Turn(job=1, descriptor="stacker", transcript="t", issued=0, last="")
    for _ in range(EMPTY_LIMIT - 1):
        assert made.next_command(turn) == "say nothing;"
    with pytest.raises(RuntimeError, match="times running"):
        made.next_command(turn)
    # The message has to say which of the two happened: they want opposite responses.
    assert "intermittent" in capsys.readouterr().err


def test_a_good_reply_clears_the_count() -> None:
    """One truncated answer in a long run is not a broken device."""
    made = planner()
    replies = iter([reply("", stop="max_tokens", out=64), reply("exit;")])
    made._client = SimpleNamespace(messages=SimpleNamespace(create=lambda **kw: next(replies)))
    turn = Turn(job=1, descriptor="stacker", transcript="t", issued=0, last="")
    made.next_command(turn)
    assert made.next_command(turn) == "exit"
    assert made._empty == 0


def test_the_default_room_is_enough_for_a_command() -> None:
    """Measured, not guessed: the model needed about 90 output tokens to answer, and at
    64 it emitted no text at all."""
    assert DEFAULT_MAX_TOKENS >= 256


def test_a_truncated_reply_is_reported_differently_from_a_refusal(capsys) -> None:
    """The two want opposite responses: more room, or another try."""
    made = planner()
    made._client = SimpleNamespace(
        messages=SimpleNamespace(create=lambda **kw: reply("", stop="max_tokens", out=512))
    )
    made.next_command(Turn(job=1, descriptor="stacker", transcript="t", issued=0, last=""))
    assert "raise --max-tokens" in capsys.readouterr().err
