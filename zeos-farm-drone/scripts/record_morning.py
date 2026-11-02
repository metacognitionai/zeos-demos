"""Play the README's morning on a live page, on a schedule: what was clicked for
``recordings/claude-morning``.

    uv run zeos-farm-drone serve --machine claude --no-open
    uv run python scripts/record_morning.py [http://127.0.0.1:8766]

Each event is sent when the farm's clock reaches its time, through the page's own
controls, so the page records it as it records a click. The battery fails halfway along
the patrol's flight to its last paddock, whenever that comes, so the drone is taken from
the patrol between two paddocks and given back on a fresh battery. The script returns once
the morning has come to rest, and prints what it came to; a morning that ended without the
battery failing is an error.

Under ``--machine claude`` the farm's clock waits while Claude chooses a command, so the
times are the farm's, not the wall's.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

#: Farm seconds, control, arguments.
SCHEDULE: list[tuple[float, str, dict[str, str]]] = [
    (9, "dawn", {}),
    (19, "vehicle-enters", {"paddock": "north-flat", "name": "white-ute", "plate": "XYZ123"}),
    (33, "stray", {"from": "dam", "to": "creek"}),
    (33, "stray", {"from": "dam", "to": "creek"}),
    (40, "trough", {"paddock": "back-block", "level": "8"}),
    (52, "park", {"item": "toolbox", "paddock": "house"}),
    (64, "say", {"text": "find my toolbox"}),
    (76, "vehicle-leaves", {"towards": "road"}),
    (80, "fire", {"direction": "north-west", "km": "12"}),
]
#: The patrol's flight the battery fails on, and how far along it counts as halfway.
BATTERY_ON = "fly back-block"
MID_FLIGHT = (0.3, 0.7)


def state(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(f"{url}/state", timeout=10) as response:
        return json.load(response)


def send(url: str, action: str, args: dict[str, str]) -> None:
    request = urllib.request.Request(
        f"{url}/{action}",
        data=json.dumps(args).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=10).close()
    except urllib.error.HTTPError as refused:
        sys.exit(f"the page refused {action} {args}: {refused.read().decode()}")


def mid_flight(now: dict[str, Any]) -> bool:
    drone = now["drones"][0]
    doing = drone["doing"] or {}
    return (
        drone["pilot"] == "patrol"
        and doing.get("command") == BATTERY_ON
        and MID_FLIGHT[0] <= float(doing.get("progress", 0.0)) <= MID_FLIGHT[1]
    )


def main() -> None:
    url = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8766").rstrip("/")
    pending = list(SCHEDULE)
    battery_failed = False
    while True:
        now = state(url)
        clock = float(now["clock"])
        while pending and clock >= pending[0][0]:
            _, action, args = pending.pop(0)
            send(url, action, args)
            print(f"{clock:7.1f}s  {action} {args or ''}")
        if not battery_failed and mid_flight(now):
            send(url, "battery", {})
            battery_failed = True
            print(f"{clock:7.1f}s  battery (patrol: {BATTERY_ON})")
        if now["story"]:
            for claim in now["story"]:
                print(f"  {'✓' if claim['held'] else '✗'} {claim['claim']}")
            if pending or not battery_failed:
                sys.exit("the morning came to rest before every event was sent")
            return
        time.sleep(0.2)


if __name__ == "__main__":
    main()
