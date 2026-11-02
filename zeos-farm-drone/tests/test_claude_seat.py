"""The Claude seat, with the API replaced by a stand-in that talks too much.

No test here reaches the network. The stand-in answers each turn with the command the
rules would have given, wrapped in the kind of chatter a model adds, so what is under
test is everything between the kernel and the API: the system prompt, the request, the
picking of one command out of a reply, the guard never being asked, and whole mornings
coming out right.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from zeos.core.ids import JobId
from zeos.descriptor.loader import load_case
from zeos.machine.seat import Turn

from zeos_farm_drone.claude import ClaudeSource, one_command
from zeos_farm_drone.policies import RulesSource
from zeos_farm_drone.run import CASE, aliases_of, run
from zeos_farm_drone.story import check

#: Which behaviour a prompt belongs to, told apart by its task heading.
TASKS = {
    "# Task: the morning patrol": "patrol",
    "# Task: take a closer look at a vehicle": "vehicle-check",
    "# Task: find out why a paddock's count is wrong": "livestock-check",
    "# Task: see how far away a fire is": "fire-watch",
    "# Task: find something the farmer has lost": "find-item",
    "# Task: bring the drone home on a flat battery": "battery-return",
    "# Task: pass on the farmer's fire report": "report-fire",
    "# Task: pass on what the farmer wants found": "ask-find",
}


@dataclass
class Chatty:
    """Stands in for ``anthropic.Anthropic``: answers as the rules would, talkatively."""

    rules: RulesSource = field(default_factory=RulesSource)
    calls: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    stop_reason: str = "end_turn"

    @property
    def beta(self) -> Any:
        return SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        prompt: str = kwargs["messages"][0]["content"]
        name = next(d for heading, d in TASKS.items() if heading in prompt)
        # The prompt is the transcript and then the seat's own question to the model.
        transcript = prompt.split("\n\nYour last command was")[0].split("\n\nYou have the")[0]
        turn = Turn(job=JobId(0), descriptor=name, transcript=transcript, issued=0)
        command = self.rules.next_command(turn)
        last = (
            prompt.split("Your last command was `", 1)[1].split("`", 1)[0]
            if ("Your last command was `" in prompt)
            else ""
        )
        text = f"`{last}` is taken, so the next step is:\n\n{command}\n\nDone." if last else command
        return SimpleNamespace(
            stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)]
        )


def source(client: Chatty) -> ClaudeSource:
    return ClaudeSource(aliases=aliases_of(load_case(CASE).descriptors), client=client)


def test_a_model_that_answers_as_the_rules_tells_the_same_story() -> None:
    client = Chatty()
    result = run(source=source(client))
    assert [v.claim for v in check(result) if not v.held] == []
    assert client.calls, "every behaviour's turn went to the model"


def test_any_morning_comes_out_right_through_the_seat(tmp_path: Path) -> None:
    lines = [
        {"at_ns": 0, "pipe": "clock.dawn", "text": "dawn"},
        {"at_ns": 2_000_000_000, "act": "vehicle-enters", "args": {"paddock": "ridge"}},
        {"at_ns": 24_000_000_000, "act": "vehicle-leaves", "args": {"towards": "neighbour"}},
        {"at_ns": 40_000_000_000, "act": "say", "args": {"text": "there is a fire to the west"}},
        {"at_ns": 60_000_000_000, "act": "stray", "args": {"from": "dam", "to": "creek"}},
    ]
    events = tmp_path / "events.jsonl"
    events.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    result = run(events=events, source=source(Chatty()))
    assert [v.claim for v in check(result) if not v.held] == []


def test_the_flight_rules_are_never_asked_of_the_model() -> None:
    client = Chatty()
    run(source=source(client))
    asked = {c["messages"][0]["content"].split("\n", 1)[0] for c in client.calls}
    assert not any("decide whether one drone command may fly" in a for a in asked)


def test_each_behaviour_is_told_only_the_pipes_it_binds() -> None:
    claude = source(Chatty())
    patrol = claude.system("patrol")
    assert "The only pipes you may name are: stdin, stdout, farmer, livestock, vehicles." in patrol
    assert "The only pipes you may name are: stdin, stdout, farmer." in claude.system("fire-watch")
    assert "The only pipes you may name are: tools." in claude.system("ask-find")


def test_the_request_asks_for_one_command_at_high_effort_with_fallback() -> None:
    client = Chatty()
    run(source=source(client))
    first = client.calls[0]
    assert first["model"] == "claude-opus-5-5"
    assert first["output_config"] == {"effort": "high"}
    assert first["betas"] == ["server-side-fallback-2026-07-01"]
    assert first["fallbacks"] == "default"
    assert first["messages"][0]["content"].endswith("Your next command:")


def test_a_reply_is_read_for_the_command_it_means_now() -> None:
    # Planning ahead: the next step comes first, and the one after is dropped.
    assert one_command("write stdout observe; read stdin;", last="read stdin") == (
        "write stdout observe"
    )
    # Explaining first: the command just completed is quoted, then the new one.
    assert (
        one_command(
            "`write stdout fly creek;` is done, so: read stdin;", last="write stdout fly creek"
        )
        == "read stdin"
    )
    assert one_command("I'm not sure what to do.") == "I'm not sure what to do."


def test_a_refusal_stops_the_seat_loudly() -> None:
    claude = source(Chatty(stop_reason="refusal"))
    turn = Turn(
        job=JobId(1), descriptor="patrol", transcript="# Task: the morning patrol", issued=0
    )
    with pytest.raises(RuntimeError, match="refused"):
        claude.next_command(turn)


def test_the_model_is_told_only_stdin_is_ever_read() -> None:
    """A model told to read after a drone command generalises it to every write -- it
    read the farmer's alert sink, was refused, and sent the alert again."""
    assert "You only ever read `stdin`." in source(Chatty()).system("livestock-check")
