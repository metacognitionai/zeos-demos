"""Export a recorded session as one self-contained web page that plays it back.

The live page needs ``zeos-farm-drone serve`` behind it. This plays the recording once,
here, takes the page's state every tenth of a farm-second, and writes it into a copy of
``replay_template.html`` with a player in place of the controls: a page anyone can open,
with nothing to install and no server.

    uv run python scripts/export_replay.py [RECORDING] [OUT.html]

Lists that only grow -- the narration, the farmer's phone, the flight rules' answers --
are stored once, whole, and each frame says how much of them it shows. Farm states and
behaviour tables that repeat from frame to frame are stored once and referred to.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from zeos_farm_drone.farm import S
from zeos_farm_drone.live import BECAUSE, Live
from zeos_farm_drone.recording import Recording, replay_step
from zeos_farm_drone.story import check

HERE = Path(__file__).resolve().parents[1]
TEMPLATE = Path(__file__).with_name("replay_template.html")
RECORDING = HERE / "recordings" / "claude-morning"
#: Farm seconds between kept frames.
FRAME_S = 0.1
#: The moments a viewer can jump to: a label, and the narration that opens it.
CHAPTERS = (
    ("A vehicle", "interrupt  findings.vehicle"),
    ("A count", "interrupt  findings.livestock"),
    ("The farmer asks", "interrupt  requests.find"),
    ("Smoke", "interrupt  alerts.fire"),
)


class Table:
    """Each distinct value once; a frame holds its index."""

    def __init__(self) -> None:
        self.rows: list[Any] = []
        self._index: dict[str, int] = {}

    def ref(self, value: Any) -> int:
        key = json.dumps(value, sort_keys=True)
        if key not in self._index:
            self._index[key] = len(self.rows)
            self.rows.append(value)
        return self._index[key]


def export(recording: Path) -> dict[str, Any]:
    live = Live(source=None, replay=Recording.load(recording), speed=1.0)
    live.start()
    farm = live.farm
    farms, tables = Table(), Table()
    frames: list[dict[str, Any]] = []
    steps = list(live.pending)
    last = -1.0
    for i, step in enumerate(steps):
        replay_step(farm, step)
        live._publish(step.at_ns)  # pyright: ignore[reportPrivateUsage]
        t = step.at_ns / S
        if t - last < FRAME_S and i != len(steps) - 1:
            continue
        last = t
        snap = live._snapshot  # pyright: ignore[reportPrivateUsage]
        frames.append(
            {
                "t": round(t, 2),
                "d": snap["drones"],
                "f": farms.ref(snap["farm"]),
                "b": tables.ref(snap["behaviours"]),
                "n": [len(farm.lines), len(farm.alerts), len(farm.replies), len(farm.rulings)],
            }
        )
    end = live._snapshot  # pyright: ignore[reportPrivateUsage]
    lines = [{"t": round(x.at_ns / S, 2), "kind": x.kind, "text": x.text} for x in farm.lines]
    chapters = []
    for label, opens in CHAPTERS:
        line = next((x for x in lines if opens in x["text"]), None)
        if line is not None:
            chapters.append({"label": label, "t": max(0.0, line["t"] - 0.5)})
    return {
        "meta": Recording.load(recording).meta,
        "because": BECAUSE,
        "frames": frames,
        "farms": farms.rows,
        "tables": tables.rows,
        "lines": lines,
        "phone": _phone(live),
        "replies": [{"t": round(at / S, 2), "text": text} for at, text in farm.replies],
        "rulings": [
            {
                "t": round(r.at_ns / S, 2),
                "behaviour": r.behaviour,
                "command": r.command,
                "allowed": r.allowed,
                "reason": r.reason,
            }
            for r in farm.rulings
        ],
        "story": [{"claim": v.claim, "held": v.held} for v in check(farm.result())],
        "chapters": chapters,
        "end": round(frames[-1]["t"], 2),
        "drone_sensors": end["drones"][0]["sensors"],
    }


def _phone(live: Live) -> list[dict[str, Any]]:
    photos = {p.id: p for p in live.farm.sim.photos}
    phone = []
    for at, text in live.farm.alerts:
        fields = dict(p.split("=", 1) for p in text.split() if "=" in p)
        photo = photos.get(fields.get("photo", ""))
        phone.append(
            {
                "t": round(at / S, 2),
                "kind": fields.get("kind", ""),
                "where": fields.get("where", ""),
                "detail": fields.get("detail", "").replace("-", " "),
                "photo": photo.id if photo else None,
                "subject": photo.subject if photo else "",
                "drone": photo.drone if photo else "",
            }
        )
    return phone


def main() -> None:
    recording = Path(sys.argv[1]) if len(sys.argv) > 1 else RECORDING
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE / "runs" / "farm-replay.html"
    data = json.dumps(export(recording), separators=(",", ":")).replace("</", "<\\/")
    page = TEMPLATE.read_text().replace("/*__DATA__*/null", data)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"{out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
