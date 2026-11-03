"""Every control on the page, under the rules source.

The tapes are right for one schedule only. The rules are meant to be right for any, so
each control is exercised here on its own schedule, and the whole story is checked
every time, not just the part the control is about.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from zeos.core.events import (
    CompilationRefused,
    Event,
    GateAnswered,
    GateConsulted,
    JobSpawned,
    PipeWritten,
    VectorCoalesced,
)

from zeos_farm_drone import sim
from zeos_farm_drone.farm import PADDOCKS
from zeos_farm_drone.policies import RulesSource
from zeos_farm_drone.run import RunResult, run
from zeos_farm_drone.story import check, expected_herd

S = 1_000_000_000
DAWN = {"at_ns": 0, "pipe": "clock.dawn", "text": "dawn"}


def morning(tmp_path: Path, *lines: dict[str, object]) -> RunResult:
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in (DAWN, *lines)) + "\n")
    result = run(events=path, source=RulesSource())
    failed = [v.claim for v in check(result) if not v.held]
    assert not failed, failed
    return result


def act(at_s: float, action: str, **args: str) -> dict[str, object]:
    return {"at_ns": int(at_s * S), "act": action, "args": args}


def of[E: Event](events: Sequence[Event], cls: type[E]) -> list[E]:
    return [e for e in events if isinstance(e, cls)]


def vetoes(result: RunResult) -> list[str]:
    asked = {e.gate_job: e.payload for e in of(result.events, GateConsulted)}
    answered = of(result.events, GateAnswered)
    consulted = of(result.events, GateConsulted)
    return [c.payload for c, a in zip(consulted, answered, strict=True) if not a.allowed and asked]


def alerts(result: RunResult, kind: str) -> list[str]:
    return [a for a in result.alerts if a.startswith(f"kind={kind} ")]


def spawned(result: RunResult, name: str) -> int:
    return sum(1 for e in of(result.events, JobSpawned) if e.descriptor == name)


def test_the_case_morning_holds_under_the_rules() -> None:
    result = run(source=RulesSource())
    assert all(v.held for v in check(result))


def test_a_quiet_morning(tmp_path: Path) -> None:
    """Only dawn: the patrol reports the leaking trough and the calves, and nothing
    else is started that the farm did not need."""
    result = morning(tmp_path)
    assert spawned(result, "vehicle-check") == 0
    assert spawned(result, "fire-watch") == 0
    assert [a.split()[1] for a in alerts(result, "trough")] == ["where=ridge"]


def test_a_vehicle_driving_towards_the_neighbour_is_watched_from_the_boundary(
    tmp_path: Path,
) -> None:
    result = morning(
        tmp_path,
        act(2, "vehicle-enters", paddock="dam", name="red-truck", plate="ABC789"),
        act(38, "vehicle-leaves", towards="neighbour"),
    )
    assert "fly neighbour" in vetoes(result)
    assert any("where=boundary-east" in a for a in alerts(result, "vehicle"))


def test_a_vehicle_gone_before_the_close_look_is_still_reported(tmp_path: Path) -> None:
    """The patrol sees it, but it has left the farm by the time the close look is over
    the paddock. The farmer is told it was there and is gone."""
    result = morning(
        tmp_path,
        act(2, "vehicle-enters", paddock="house"),
        act(7.5, "vehicle-leaves", towards="road"),
    )
    reported = alerts(result, "vehicle")
    assert reported and all("photo=" in a for a in reported)


def test_calves_born_during_the_patrol_update_the_herd(tmp_path: Path) -> None:
    result = morning(tmp_path, act(3, "calves", paddock="dam", count="3"))
    herd = expected_herd(result.kernel.world.get("herd.expected") or "")  # type: ignore[arg-type]
    assert herd["dam"] == 18 + 3
    assert spawned(result, "livestock-check") == 2  # the dam and the back-block


def test_a_stray_is_reported_twice_and_changes_no_count(tmp_path: Path) -> None:
    """One cow strays from house into creek: house is one short and creek one over. Both
    are checked; neither is a new count for the farmer."""
    result = morning(tmp_path, act(1, "stray", **{"from": "house", "to": "creek"}))
    checked = [a for a in alerts(result, "livestock") if "where=house" in a or "where=creek" in a]
    assert len(checked) == 2
    herd = expected_herd(result.kernel.world.get("herd.expected") or "")  # type: ignore[arg-type]
    assert (herd["house"], herd["creek"]) == (12, 30)


def test_a_dry_trough_is_reported(tmp_path: Path) -> None:
    result = morning(tmp_path, act(1, "trough", paddock="creek", level="5"))
    assert any("where=creek" in a and "trough-at-5%" in a for a in alerts(result, "trough"))


def test_the_farmer_finds_what_was_parked(tmp_path: Path) -> None:
    result = morning(
        tmp_path,
        act(1, "park", item="motorbike", paddock="back-block"),
        act(70, "say", text="where did I park the motorbike"),
    )
    assert any("where=back-block" in a for a in alerts(result, "item"))


def test_something_nowhere_on_the_farm_is_reported_as_not_found(tmp_path: Path) -> None:
    result = morning(tmp_path, act(70, "say", text="find my dog"))
    assert any("where=nowhere" in a for a in alerts(result, "item"))


def test_the_farmer_can_ask_for_a_look_for_smoke(tmp_path: Path) -> None:
    """The farmer's words start the fire watch, as the alert would; with no fire there
    is no smoke to see, and the farmer is told so."""
    result = morning(tmp_path, act(70, "say", text="there is a fire to the east"))
    assert spawned(result, "fire-watch") == 1
    assert any("no-smoke-in-sight" in a for a in alerts(result, "fire"))


def test_words_nothing_answers_are_refused(tmp_path: Path) -> None:
    result = morning(tmp_path, act(70, "say", text="feed the dog"))
    assert [r.reason for r in of(result.events, CompilationRefused)] == ["no-compilation-target"]
    assert result.replies[-1].startswith("cannot:")


def test_a_fire_cuts_a_search_short_and_the_search_still_finishes(tmp_path: Path) -> None:
    """The search is `immediate`: a fire takes the drone from it. The search is told,
    waits, and when the drone is free again flies on and finds the bike."""
    result = morning(
        tmp_path,
        act(1, "say", text="find my motorbike"),
        act(3, "fire", direction="south", km="20"),
    )
    told = [" ".join(e.text) for e in of(result.events, PipeWritten) if e.pipe == "drone.done.find"]
    assert any(t.startswith("interrupted ") for t in told)
    assert any("where=north-flat" in a for a in alerts(result, "item"))


def test_alarms_behind_a_running_fire_watch_are_one_response(tmp_path: Path) -> None:
    result = morning(
        tmp_path,
        act(10, "fire", direction="west", km="9"),
        act(10.5, "fire", direction="west", km="9"),
    )
    assert spawned(result, "fire-watch") == 1
    assert of(result.events, VectorCoalesced)


@pytest.mark.parametrize("second", [140, 200])
def test_a_new_day_is_a_new_round(tmp_path: Path, second: int) -> None:
    result = morning(tmp_path, {"at_ns": second * S, "pipe": "clock.dawn", "text": "dawn"})
    assert spawned(result, "patrol") == 2
    observed = [p for p, _ in result.sim.observed]
    assert observed.count("creek") == 2


def test_a_paddock_with_everything_wrong_is_reported_in_full(tmp_path: Path) -> None:
    """A low trough, a vehicle and new calves in one paddock: the vehicle's finding takes
    the drone from the patrol at once, and the patrol still tells the farmer about the
    trough and raises the count when it is back."""
    result = morning(
        tmp_path,
        act(1, "trough", paddock="house", level="5"),
        act(1, "vehicle-enters", paddock="house", name="grey-van"),
        act(1, "calves", paddock="house", count="2"),
    )
    assert any("where=house" in a for a in alerts(result, "trough"))
    assert any("where=house" in a for a in alerts(result, "livestock"))
    assert any("where=house" in a for a in alerts(result, "vehicle"))


def test_a_flat_battery_brings_the_drone_home_and_the_patrol_carries_on(tmp_path: Path) -> None:
    """The battery's alarm outranks everything: the drone flies straight home, the farmer
    is told, and on a fresh battery the patrol finishes its round."""
    result = morning(tmp_path, act(20, "battery"))
    assert spawned(result, "battery-return") == 1
    told = [
        " ".join(e.text) for e in of(result.events, PipeWritten) if e.pipe == "drone.done.battery"
    ]
    assert told == ["done return"]
    assert [a.split()[1] for a in alerts(result, "battery")] == ["where=shed"]
    assert {p for p, _ in result.sim.observed} >= set(PADDOCKS)
    assert result.sim.drones["kestrel"].battery == 100


def test_a_battery_run_down_in_flight_raises_its_own_alarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No click: the charge runs down while the drone flies, and its monitor raises the
    alarm on reaching critical."""
    monkeypatch.setattr(sim.FarmSim, "drain_pct_per_s", 3.0)
    result = morning(tmp_path)
    assert spawned(result, "battery-return") >= 1
    assert alerts(result, "battery")


@pytest.mark.parametrize("at", [5, 11, 33, 40, 61])
def test_a_flat_battery_mid_flight_still_lets_the_round_finish(tmp_path: Path, at: int) -> None:
    """The return cuts a flight short: the behaviour it took the drone from must not
    trust where that flight said it stopped, since the drone has been flown home since."""
    result = morning(tmp_path, act(at, "battery"))
    assert spawned(result, "battery-return") == 1
