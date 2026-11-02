"""The drones' contract with the kernel, outside the canonical morning."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from zeos.core.events import Event, JobSpawned, PipeWritten, ReportAttributed
from zeos.core.ids import JobId

from zeos_farm_drone.policies import RulesSource
from zeos_farm_drone.run import run


def schedule(tmp_path: Path, lines: Sequence[dict[str, object]]) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    return path


def of[E: Event](events: Sequence[Event], cls: type[E]) -> list[E]:
    return [e for e in events if isinstance(e, cls)]


def test_an_immediate_holder_stops_where_it_is(tmp_path: Path) -> None:
    """The search is `immediate`: a fire alert takes the drone mid-flight, the flight
    stops over the nearest place, and where it stopped is reported on nobody's behalf,
    since it is news to the search."""
    result = run(
        events=schedule(
            tmp_path,
            [
                {"at_ns": 0, "act": "say", "args": {"text": "find my motorbike"}},
                {"at_ns": 2_500_000_000, "act": "fire", "args": {"direction": "west", "km": "8"}},
            ],
        )
    )
    events = list(result.events)
    told = next(
        e
        for e in of(events, PipeWritten)
        if e.pipe == "drone.done.find" and e.text[:1] == ("interrupted",)
    )
    answer = " ".join(told.text)
    stopped = answer.rsplit(" over ", 1)[1]
    # Where it stopped is reported on nobody's behalf, as the drone's own news.
    report = next(
        e
        for e in of(events, PipeWritten)
        if e.pipe == "report.kestrel.position" and e.text == (stopped,)
    )
    assert not isinstance(events[events.index(report) - 1], ReportAttributed)
    assert answer.startswith("interrupted fly house over ")
    # It is where the drone is (the fire watch only climbs), and nothing the drone
    # reported was put down to the search: where a cut-short flight stopped is not the
    # search's own doing.
    assert answer.endswith(f" over {stopped}")
    search = next(e.job for e in of(events, JobSpawned) if e.descriptor == "find-item")
    assert search not in {e.job for e in of(events[: events.index(told)], ReportAttributed)}


def answers_to_patrol(tmp_path: Path, fire_at_s: float) -> list[str]:
    """What the drone told the patrol, when a fire alert comes ``fire_at_s`` into the
    morning; the rules answer, since no tape is written for this schedule."""
    result = run(
        events=schedule(
            tmp_path,
            [
                {"at_ns": 0, "act": "dawn", "args": {}},
                {"at_ns": int(fire_at_s * 1e9), "act": "fire", "args": {"direction": "west"}},
            ],
        ),
        source=RulesSource(),
    )
    return [
        " ".join(e.text) for e in of(result.events, PipeWritten) if e.pipe == "drone.done.patrol"
    ]


def test_a_boundary_holder_is_cut_short_mid_flight(tmp_path: Path) -> None:
    """The patrol is `at-action-boundary`, and a flight is boundaries all the way: the
    fire takes the drone on the way to the house, and the patrol flies there again."""
    told = answers_to_patrol(tmp_path, 2.0)
    assert told[0].startswith("interrupted fly house over ")
    assert told[1] == "done fly house"


def test_a_boundary_holder_finishes_a_look(tmp_path: Path) -> None:
    """A look is one action: the fire arrives while the patrol observes the house, and
    the observation is finished and answered before the fire watch has the drone."""
    told = answers_to_patrol(tmp_path, 5.0)
    assert told[:2] == ["done fly house", told[1]]
    assert told[1].startswith("observed house: ")
    assert not any(a.startswith("interrupted") for a in told)


def test_a_search_reports_what_it_sees_on_the_way(tmp_path: Path) -> None:
    """A search is a look at the paddock too. A ute parked in the house paddock is
    reported by the search, the vehicle check (more urgent) takes the drone for a
    closer look, and the search then carries on and finds the motorbike."""
    result = run(
        events=schedule(
            tmp_path,
            [
                {"at_ns": 0, "act": "vehicle-enters", "args": {"paddock": "house"}},
                {"at_ns": 0, "act": "trough", "args": {"paddock": "house", "level": "12"}},
                {"at_ns": 0, "act": "say", "args": {"text": "find my motorbike"}},
            ],
        ),
        source=RulesSource(),
    )
    events = list(result.events)
    names: dict[JobId | None, str] = {e.job: str(e.descriptor) for e in of(events, JobSpawned)}
    written = [(names.get(e.job), " ".join(e.text)) for e in of(events, PipeWritten)]
    by_find = [text for who, text in written if who == "find-item" and "search" not in text]
    assert "kind=trough where=house photo=none detail=trough-at-12%" in by_find
    assert "kind=vehicle where=house photo=none detail=white-ute-seen" in by_find
    assert "house white-ute" in by_find
    assert by_find[-1].startswith("kind=item where=north-flat ")
    flown = [
        (names.get(e.job), " ".join(e.text))
        for e in of(events, PipeWritten)
        if e.pipe == "drone.command"
    ]
    # The vehicle check flew in for its closer look between the two searches.
    assert [c for who, c in flown if who == "vehicle-check"][-1] == "zoom vehicle"
    searches = [i for i, (who, c) in enumerate(flown) if c == "search motorbike"]
    zoom = next(i for i, step in enumerate(flown) if step == ("vehicle-check", "zoom vehicle"))
    assert searches[0] < zoom < searches[-1]
