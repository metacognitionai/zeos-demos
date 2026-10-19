"""The Claude seat, with the API replaced by a stand-in that talks too much.

No test here reaches the network. The stand-in answers each turn with the command the
tape would have given, wrapped in the kind of chatter a model adds, so what is under
test is everything between the kernel and the API: the system prompt, the request, the
picking of one command out of a reply, and the whole morning coming out the same.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from zeos.core.ids import JobId
from zeos.descriptor.loader import load_case
from zeos.machine.seat import Turn

from zeos_stretch.claude import ClaudeSource, one_command
from zeos_stretch.run import CASE, aliases_of, run
from zeos_stretch.story import check

#: Which behaviour a prompt belongs to, told apart by its task heading.
TASKS = {
    "# Task: clear the table": "tidy-atrium",
    "# Task: start a coffee": "brew-coffee",
    "# Task: bring the brewed coffee": "deliver-coffee",
    "# Task: cut the power": "smoke-response",
}


@dataclass
class Chatty:
    """Stands in for ``anthropic.Anthropic``: replays each behaviour's tape, talkatively."""

    tapes: dict[str, list[str]]
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    stop_reason: str = "end_turn"

    @property
    def beta(self) -> Any:
        return SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        prompt = kwargs["messages"][0]["content"]
        name = next(d for heading, d in TASKS.items() if heading in prompt)
        command = self.tapes[name].pop(0)
        text = f"Looking at the status line, the next step is clear.\n\n{command}\n\nDone."
        return SimpleNamespace(
            stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)]
        )


def tapes() -> dict[str, list[str]]:
    bundle = load_case(CASE)
    return {str(n): [step.emit for step in s.steps] for n, s in bundle.scripts.items()}


def source(client: Chatty) -> ClaudeSource:
    return ClaudeSource(aliases=aliases_of(load_case(CASE).descriptors), client=client)


def test_a_model_that_says_the_tape_tells_the_same_story() -> None:
    client = Chatty(tapes())
    result = run(source=source(client))
    assert all(v.held for v in check(result)), [v.claim for v in check(result) if not v.held]
    assert all(not remaining for remaining in client.tapes.values()), "every turn was asked"


def test_each_behaviour_is_told_only_the_pipes_it_binds() -> None:
    claude = source(Chatty(tapes()))
    assert "The only pipes you may name are: stdin, stdout, tools." in claude.system("brew-coffee")
    assert "The only pipes you may name are: stdin, stdout." in claude.system("tidy-atrium")


def test_the_request_asks_for_one_command_with_fallback() -> None:
    client = Chatty(tapes())
    run(source=source(client))
    first = client.calls[0]
    assert first["model"] == "claude-opus-5"
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["fallbacks"] == "default"
    assert "put ITEM CONTAINER" in first["messages"][0]["content"]


def test_one_command_is_picked_out_of_chatter() -> None:
    reply = "Sure. I'll put it away: write stdout put apple basket; then wait."
    assert one_command(reply) == "write stdout put apple basket"
    assert one_command("I am not sure what to do") == "I am not sure what to do"


def test_a_reply_that_quotes_its_last_command_is_read_for_its_answer() -> None:
    """Found live: a reply explaining that `navigate desk-5` was done, before answering
    `place coffee`, had its quoted command picked out and the move sent again."""
    reply = "`write stdout navigate desk-5;` is done and the body is at desk-5, so: write stdout place coffee;"
    assert one_command(reply) == "write stdout place coffee"


def test_a_refusal_stops_the_run_rather_than_being_guessed_past() -> None:
    claude = source(Chatty(tapes(), stop_reason="refusal"))
    turn = Turn(
        job=JobId(1), descriptor="tidy-atrium", transcript="# Task: clear the table", issued=0
    )
    with pytest.raises(RuntimeError, match="refused"):
        claude.next_command(turn)


def test_the_model_is_never_told_its_last_command_is_done() -> None:
    """To a robot, "done" reads as the body having finished, and a model told so moves
    on to its next command without reading the body's answer."""
    from zeos_stretch.claude import prompt_for

    turn = Turn(
        job=JobId(1),
        descriptor="tidy-atrium",
        transcript="x",
        issued=1,
        last="write stdout put apple basket",
    )
    prompt = prompt_for(turn)
    assert "it is done" not in prompt and "has taken it" in prompt
    claude = source(Chatty(tapes()))
    assert "your next command is `read stdin;`" in claude.system("tidy-atrium")


def test_missing_credentials_are_reported_before_anything_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import anthropic

    from zeos_stretch.claude import check_credentials

    class NoCredentials:
        def __init__(self, **_: Any) -> None:
            self.models = SimpleNamespace(list=self.list)

        def list(self, **_: Any) -> None:
            raise TypeError('"Could not resolve authentication method. Expected one of api_key"')

    monkeypatch.setattr(anthropic, "Anthropic", NoCredentials)
    problem = check_credentials()
    assert problem is not None and "ANTHROPIC_API_KEY" in problem
