"""Any morning the page's controls can make, under the rules source.

A person at the page clicks whatever they like, whenever they like. These are seeded
random mornings of that kind -- vehicles coming and going, calves, strays, troughs, the
farmer's requests, fires, another dawn -- and every one must come out right by the
whole story, and leave nothing running.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from zeos_farm_drone.farm import PADDOCKS
from zeos_farm_drone.policies import RulesSource
from zeos_farm_drone.run import run
from zeos_farm_drone.story import check

S = 1_000_000_000
DIRECTIONS = ("north", "north-east", "east", "south-east", "south", "south-west", "west")
SAYINGS = (
    "find my motorbike",
    "where is the dog",
    "there is a fire to the {direction}",
    "open the gate",
)


def morning(seed: int) -> list[dict[str, object]]:
    rng = random.Random(seed)
    lines: list[dict[str, object]] = [{"at_ns": 0, "pipe": "clock.dawn", "text": "dawn"}]
    for _ in range(rng.randint(2, 7)):
        at = int(rng.uniform(0.5, 150) * S)
        match rng.choice(
            (
                "vehicle-enters",
                "vehicle-leaves",
                "calves",
                "stray",
                "trough",
                "park",
                "say",
                "fire",
                "dawn",
            )
        ):
            case "dawn":
                lines.append({"at_ns": at, "pipe": "clock.dawn", "text": "dawn"})
                continue
            case "vehicle-enters":
                act = (
                    "vehicle-enters",
                    {"paddock": rng.choice(PADDOCKS), "name": "red-truck", "plate": "Q1"},
                )
            case "vehicle-leaves":
                act = "vehicle-leaves", {"towards": rng.choice(("road", "neighbour"))}
            case "calves":
                act = "calves", {"paddock": rng.choice(PADDOCKS), "count": str(rng.randint(1, 3))}
            case "stray":
                start, end = rng.sample(PADDOCKS, 2)
                act = "stray", {"from": start, "to": end}
            case "trough":
                act = "trough", {"paddock": rng.choice(PADDOCKS), "level": str(rng.randint(0, 25))}
            case "park":
                act = "park", {"item": "motorbike", "paddock": rng.choice(PADDOCKS)}
            case "say":
                text = rng.choice(SAYINGS).format(direction=rng.choice(DIRECTIONS))
                act = "say", {"text": text}
            case _:
                act = "fire", {"direction": rng.choice(DIRECTIONS), "km": str(rng.randint(2, 30))}
        lines.append({"at_ns": at, "act": act[0], "args": act[1]})
    return lines


@pytest.mark.parametrize("seed", range(40))
def test_any_morning_comes_out_right(tmp_path: Path, seed: int) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in morning(seed)) + "\n")
    result = run(events=path, source=RulesSource())
    failed = [v.claim for v in check(result) if not v.held]
    assert not failed, failed
