"""The farm's map: where each place is, which are on the property, and how long a flight
between two of them takes.

```
            north-flat     ridge        back-block
   road  |  house          creek        dam            | neighbour
            shed
```
"""

from __future__ import annotations

import math

__all__ = [
    "BOUNDARY",
    "OFF_FARM",
    "ON_FARM",
    "PADDOCKS",
    "PLACES",
    "S",
    "flight_s",
    "nearest_on_farm",
    "position_between",
]

S = 1_000_000_000

#: The paddocks, in the order the patrol flies them.
PADDOCKS = ("house", "north-flat", "ridge", "creek", "dam", "back-block")
#: Where a drone can be told to go, as map coordinates in kilometres.
PLACES: dict[str, tuple[float, float]] = {
    "shed": (1.0, 1.6),
    "house": (1.0, 1.0),
    "north-flat": (1.0, 0.0),
    "ridge": (2.0, 0.0),
    "creek": (2.0, 1.0),
    "dam": (3.0, 1.0),
    "back-block": (3.0, 0.0),
    "boundary-west": (0.3, 0.5),
    "boundary-east": (3.7, 0.5),
    "road": (-0.4, 0.5),
    "neighbour": (4.4, 0.5),
}
BOUNDARY = ("boundary-west", "boundary-east")
OFF_FARM = frozenset({"road", "neighbour"})
ON_FARM = frozenset(PLACES) - OFF_FARM

#: Seconds of flight per kilometre, and the least any flight takes.
S_PER_KM = 4.0
MIN_FLIGHT_S = 2


def _distance(a: str, b: str) -> float:
    (ax, ay), (bx, by) = PLACES[a], PLACES[b]
    return math.hypot(ax - bx, ay - by)


def flight_s(start: str, end: str) -> int:
    if start == end:
        return 0
    return max(MIN_FLIGHT_S, math.ceil(_distance(start, end) * S_PER_KM))


def position_between(start: str, end: str, fraction: float) -> tuple[float, float]:
    (ax, ay), (bx, by) = PLACES[start], PLACES[end]
    return (ax + (bx - ax) * fraction, ay + (by - ay) * fraction)


def nearest_on_farm(point: tuple[float, float]) -> str:
    """The place on the property closest to a point, which is where a drone stopped
    mid-flight is said to be."""
    x, y = point
    return min(sorted(ON_FARM), key=lambda p: math.hypot(PLACES[p][0] - x, PLACES[p][1] - y))
