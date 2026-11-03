"""Rewrite each handler's tape from what the rules say in the canonical morning.

    uv run python scripts/regenerate_tapes.py

A tape (a descriptor's ``script:``) is the rules' answers for one schedule,
``events.jsonl``, command for command. When the farm's device changes what happens in
that morning -- a flight cut where it used to finish -- the rules answer differently
and the tapes follow. Every job of a descriptor plays the same tape from its start, so
the jobs of one descriptor must have said the same thing; if not, this stops.
"""

from __future__ import annotations

import re
import sys

from zeos.machine.seat import Turn

from zeos_farm_drone.policies import RulesSource
from zeos_farm_drone.run import CASE, run

#: Descriptors whose turns are not a tape's: the guard is always answered by the rules.
NOT_TAPED = {"flight-rules"}


class Taping(RulesSource):
    """The rules, noting what each job said at each of its turns."""

    def __init__(self) -> None:
        self.said: dict[tuple[str, int], dict[int, str]] = {}

    def next_command(self, turn: Turn) -> str:
        command = super().next_command(turn)
        self.said.setdefault((turn.descriptor, int(turn.job)), {})[turn.issued] = command
        return command


def tapes(source: Taping) -> dict[str, list[str]]:
    by_descriptor: dict[str, list[str]] = {}
    for (descriptor, job), said in sorted(source.said.items()):
        if descriptor in NOT_TAPED:
            continue
        tape = [said[i] for i in sorted(said)]
        known = by_descriptor.setdefault(descriptor, tape)
        if known != tape:
            sys.exit(f"{descriptor}: job {job} said {tape}, an earlier job {known}")
    return by_descriptor


def rewrite(descriptor: str, tape: list[str]) -> bool:
    path = next(CASE.rglob(f"{descriptor}.md"))
    text = path.read_text()
    found = re.search(r"^script:\n(  #[^\n]*\n)*((  - emit: [^\n]*\n)+)", text, flags=re.M)
    if found is None:
        sys.exit(f"{path}: no script: block of emits")
    emits = "".join(f'  - emit: "{command}"\n' for command in tape)
    if found[2] == emits:
        return False
    path.write_text(text[: found.start(2)] + emits + text[found.end(2) :])
    return True


def main() -> None:
    source = Taping()
    run(CASE, source=source)
    for descriptor, tape in sorted(tapes(source).items()):
        changed = rewrite(descriptor, tape)
        print(f"{descriptor:16} {len(tape):3} commands{'  (rewritten)' if changed else ''}")


if __name__ == "__main__":
    main()
