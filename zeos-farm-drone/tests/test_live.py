"""The live page's side of the farm: what a click becomes, stepped by hand.

No thread and no socket: the farm is built and stepped here directly, so a click is
checked by what it put into the journal and onto the page.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from zeos.core.events import PipeWritten

from zeos_farm_drone.live import Live
from zeos_farm_drone.policies import RulesSource
from zeos_farm_drone.recording import Recording, replay
from zeos_farm_drone.story import check


def interactive(record: Path | None = None) -> Live:
    live = Live(source=RulesSource, speed=2000.0, record=record)
    live.start()
    return live


def written(live: Live, pipe: str) -> list[str]:
    return [
        " ".join(e.text) for e in live.farm.sink if isinstance(e, PipeWritten) and e.pipe == pipe
    ]


def settle(live: Live, steps: int = 200_000) -> None:
    """Step until everything started has finished."""
    for _ in range(steps):
        live.once()
        if (
            live.farm.names
            and live.farm.settled()
            and not any(not s.is_terminal for s in live.farm.states.values())
        ):
            return
    raise AssertionError("the farm never came to rest")


def test_a_new_day_is_an_interrupt_on_the_dawn_pipe() -> None:
    live = interactive()
    assert live.ask("dawn", {}) is None
    live.once()
    assert written(live, "clock.dawn") == ["dawn"]


def test_the_farmers_words_go_to_the_front_door() -> None:
    live = interactive()
    assert live.ask("say", {"text": "find my motorbike"}) is None
    live.once()
    assert written(live, "farmer.replies") == ["spawning ask-find(item=motorbike)"]


@pytest.mark.parametrize(
    ("action", "args"),
    [
        ("fire", {"direction": "up", "km": "12"}),
        ("fire", {"direction": "east", "km": "500"}),
        ("vehicle-enters", {"paddock": "the-moon", "name": "white-ute"}),
        ("vehicle-leaves", {"towards": "creek"}),
        ("calves", {"paddock": "dam", "count": "0"}),
        ("stray", {"from": "dam", "to": "dam"}),
        ("say", {"text": ""}),
        ("fly", {}),
    ],
)
def test_what_cannot_happen_on_the_farm_is_refused(action: str, args: dict[str, str]) -> None:
    assert interactive().ask(action, args) is not None


def test_playback_takes_no_clicks() -> None:
    live = Live(source=None, playback=True)
    live.start()
    assert live.ask("dawn", {}) is not None
    assert live.ask("restart", {}) is None


def test_the_snapshot_says_what_each_drone_is_doing() -> None:
    live = interactive()
    live.ask("dawn", {})
    for _ in range(200):
        live.once()
        if live.farm.sim.drones["kestrel"].current is not None:
            break
    snap = live.snapshot()
    kestrel = next(d for d in snap["drones"] if d["name"] == "kestrel")
    assert kestrel["doing"]["says"] == "Flying to the house."
    assert kestrel["pilot"] == "patrol"
    assert snap["story"] == []  # nothing is judged while the patrol is under way


def test_a_session_from_the_page_replays_byte_for_byte(tmp_path: Path) -> None:
    """Clicks land wherever this machine's speed puts them; the recording keeps when, so
    the replay reproduces the journal all the same."""
    live = interactive(tmp_path / "session")
    live.ask("dawn", {})
    live.ask("vehicle-enters", {"paddock": "dam", "name": "red-truck"})
    for _ in range(3000):
        live.once()
    live.ask("say", {"text": "where did i park the motorbike"})
    live.ask("fire", {"direction": "south", "km": "7"})
    settle(live)
    live.close()

    recording = Recording.load(tmp_path / "session")
    actions = [a for step in recording.steps for a, _ in step.actions]
    assert actions == ["dawn", "vehicle-enters", "say", "fire"]
    result, same = replay(recording)
    assert same, "the replayed journal must be the recorded one"
    assert [v.claim for v in check(result) if not v.held] == []


def test_a_click_the_farm_cannot_do_now_is_refused_with_the_reason() -> None:
    """A second dawn while the patrol is out, the same ute driving in twice, a vehicle
    driving off when none is parked: each would be accepted and then do nothing."""
    live = interactive()
    assert live.ask("vehicle-leaves", {"towards": "road"}) is not None
    assert live.ask("vehicle-enters", {"paddock": "creek", "name": "white-ute"}) is None
    assert live.ask("vehicle-enters", {"paddock": "dam", "name": "white-ute"}) is not None
    assert live.ask("dawn", {}) is None
    assert live.ask("dawn", {}) is not None, "one dawn is already queued"
    live.once()
    assert live.farm.live("patrol")
    assert live.ask("dawn", {}) == "the patrol is already out; a new day starts when it is back"
    assert live.ask("vehicle-leaves", {"towards": "road"}) is None
    live.once()
    assert live.ask("vehicle-leaves", {"towards": "road"}) is not None, "it is already leaving"


def test_a_cell_fails_only_in_flight() -> None:
    """On its pad the drone is being charged; in the air a cell can fail, once."""
    live = interactive()
    assert live.ask("battery", {}) == "kestrel is on its pad in the shed, being charged"
    live.ask("dawn", {})
    for _ in range(200):
        live.once()
        if live.farm.sim.airborne(live.farm.sim.drones["kestrel"]):
            break
    assert live.snapshot()["drones"][0]["battery"] <= 100
    assert live.ask("battery", {}) is None
    assert live.ask("battery", {}) == "the battery alarm is already raised"
    live.once()
    assert written(live, "alerts.battery") == ["kestrel battery=12%"]
    assert live.ask("battery", {}) == "kestrel is already flying home on a flat battery"
