"""A live session, kept so it can be played again exactly.

The kernel is deterministic in what it is given, so a session is reproduced by giving it
the same things in the same order: the time of every step, what arrived and what the
person did at each, and every command a behaviour's source answered with. A model is
not asked again on replay; its answers are read back, keyed by job and turn.

A recording directory holds:

- ``meta.json``: the case, the machine, the model, when it started;
- ``steps.jsonl``: one line per step, ``{"t": ns}`` plus ``"in"`` (scheduled arrivals)
  and ``"a"`` (what the person did) when there were any;
- ``commands.jsonl``: one line per command a source gave, with its job and turn;
- ``journal.jsonl``: the journal, written as the session ran.

Replaying the steps with the commands produces the journal again byte for byte, which is
what makes a recording evidence rather than a transcript.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from zeos.core.events import Event
from zeos.core.ids import PipeName
from zeos.journal.codec import to_line
from zeos.machine.seat import Turn

from zeos_stretch.run import CASE, Atrium, RunResult

__all__ = ["Recorder", "Recording", "RecordedSource", "Step", "case_name", "replay"]

PROJECT_ROOT = CASE.parents[1]


def case_name(case: Path) -> str:
    """How a recording names its case: relative to the project."""
    return str(case.resolve().relative_to(PROJECT_ROOT))


@dataclass(frozen=True, slots=True)
class Step:
    at_ns: int
    arrivals: tuple[tuple[PipeName, str], ...] = ()
    actions: tuple[tuple[str, str], ...] = ()


class Recorder:
    """Writes a session down as it happens, flushing every step so a killed server
    still leaves a replayable recording behind."""

    def __init__(self, directory: Path, meta: dict[str, Any], *, case: Path) -> None:
        directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        # The case as it was played. Editing a descriptor afterwards changes what the
        # kernel would inject, so a recording replays from its own copy, not the tree's.
        # It keeps the case's own name, which the journal records when the kernel starts.
        shutil.copytree(case, directory / "case" / case.name)
        (directory / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
        self._steps: IO[str] = (directory / "steps.jsonl").open("w", encoding="utf-8")
        self._commands: IO[str] = (directory / "commands.jsonl").open("w", encoding="utf-8")
        self._journal: IO[str] = (directory / "journal.jsonl").open(
            "w", encoding="utf-8", newline="\n"
        )
        self._written = 0

    def step(self, step: Step) -> None:
        line: dict[str, Any] = {"t": step.at_ns}
        if step.arrivals:
            line["in"] = [list(a) for a in step.arrivals]
        if step.actions:
            line["a"] = [list(a) for a in step.actions]
        self._steps.write(json.dumps(line) + "\n")

    def command(self, turn: Turn, command: str, reply: str | None = None) -> None:
        """One command a source gave, and the reply it was picked from when a model gave
        it: the reply is what shows why a command was what it was."""
        line = {"job": int(turn.job), "issued": turn.issued, "descriptor": turn.descriptor}
        if reply is not None:
            line["reply"] = reply
        self._commands.write(json.dumps({**line, "command": command}) + "\n")
        self._commands.flush()

    def journal(self, events: Sequence[Event]) -> None:
        for seq in range(self._written, len(events)):
            self._journal.write(to_line(seq, events[seq]) + "\n")
        self._written = len(events)
        self._steps.flush()
        self._journal.flush()

    def close(self) -> None:
        for fh in (self._steps, self._commands, self._journal):
            fh.close()


@dataclass(frozen=True, slots=True)
class Recording:
    directory: Path
    meta: dict[str, Any]
    steps: tuple[Step, ...]
    commands: dict[tuple[int, int], str] = field(default_factory=dict[tuple[int, int], str])

    @property
    def case(self) -> Path:
        """The case played: the recording's own copy, or for a recording made before
        recordings kept one, the tree it names relative to the project."""
        named = PROJECT_ROOT / self.meta["case"]
        kept = self.directory / "case" / named.name
        return kept if kept.is_dir() else named

    @staticmethod
    def load(directory: Path) -> Recording:
        meta = json.loads((directory / "meta.json").read_text())
        steps: list[Step] = []
        for raw in (directory / "steps.jsonl").read_text().splitlines():
            if not raw.strip():
                continue
            line = json.loads(raw)
            steps.append(
                Step(
                    at_ns=int(line["t"]),
                    arrivals=tuple((PipeName(p), str(t)) for p, t in line.get("in", ())),
                    actions=tuple((str(a), str(x)) for a, x in line.get("a", ())),
                )
            )
        commands: dict[tuple[int, int], str] = {}
        for raw in (directory / "commands.jsonl").read_text().splitlines():
            if raw.strip():
                line = json.loads(raw)
                commands[(int(line["job"]), int(line["issued"]))] = str(line["command"])
        return Recording(directory=directory, meta=meta, steps=tuple(steps), commands=commands)


@dataclass
class RecordedSource:
    """Answers each turn with the command the recorded session's source gave for it."""

    recording: Recording

    def next_command(self, turn: Turn) -> str:
        key = (int(turn.job), turn.issued)
        if key not in self.recording.commands:
            raise LookupError(
                f"{self.recording.directory} has no command {turn.issued + 1} for job "
                f"{turn.job} ({turn.descriptor}); the replay has left the recorded session"
            )
        return self.recording.commands[key]


def replay_step(atrium: Atrium, step: Step) -> bool:
    arrivals = list(step.arrivals)
    for action, arg in step.actions:
        arrivals.extend(atrium.act(step.at_ns, action, arg))
    return atrium.step(step.at_ns, arrivals)


def replay(recording: Recording, *, echo: bool = False) -> tuple[RunResult, bool]:
    """Play a recording to its end. Returns the run and whether its journal came out
    byte for byte as recorded."""
    atrium = Atrium(
        recording.case,
        source=RecordedSource(recording),
        show_commands=True,
        on_line=(lambda line: print(line.render())) if echo else None,
    )
    atrium.start()
    for step in recording.steps:
        replay_step(atrium, step)
    produced = "".join(to_line(seq, e) + "\n" for seq, e in enumerate(atrium.sink))
    recorded = (recording.directory / "journal.jsonl").read_text()
    result = RunResult(
        kernel=atrium.kernel,
        events=tuple(atrium.sink),
        body=atrium.body,
        lines=tuple(line.render() for line in atrium.lines),
    )
    return result, produced == recorded
