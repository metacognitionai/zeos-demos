"""A live session is recorded, and the recording replays to the same journal.

The session here is driven the way a person drives the page: it orders a coffee while
the tidying is putting the mug away, and sets the alarm off while the coffee is on its
way to desk 7. Its clock is the wall clock, so the steps land wherever this machine's
speed puts them; the replay has to reproduce them all the same.
"""

from __future__ import annotations

from pathlib import Path

from zeos.descriptor.loader import load_case
from zeos.machine.seat import TapeSource

from zeos_stretch.live import Live
from zeos_stretch.recording import Recording, replay
from zeos_stretch.run import CASE
from zeos_stretch.story import check


def drive(record: Path) -> Live:
    scripts = load_case(CASE).scripts
    live = Live(source=lambda: TapeSource(scripts), speed=2000.0, record=record)
    live.start()
    ordered = alarmed = False
    for _ in range(200_000):
        live.once()
        atrium, doing = live.atrium, live.atrium.body.current
        if not ordered and doing is not None and doing.command == "put mug tray":
            assert live.ask("order", "desk-7") is None
            ordered = True
        delivering = doing is not None and atrium.names[doing.job] == "deliver-coffee"
        if not alarmed and delivering and doing.command == "navigate desk-7":
            assert live.ask("alarm") is None
            alarmed = True
        if (
            alarmed
            and atrium.settled()
            and not any(atrium.live(n) for n in ("tidy-atrium", "deliver-coffee", "smoke-response"))
        ):
            break
    live.close()
    return live


def test_a_recorded_session_replays_byte_for_byte(tmp_path: Path) -> None:
    drive(tmp_path / "session")
    result, same = replay(Recording.load(tmp_path / "session"))
    assert same, "the replayed journal must be the recorded one"
    assert [v.claim for v in check(result) if not v.held] == []


def test_the_recording_holds_the_clicks_and_the_commands(tmp_path: Path) -> None:
    drive(tmp_path / "session")
    recording = Recording.load(tmp_path / "session")
    actions = [a for step in recording.steps for a in step.actions]
    assert actions == [("order", "desk-7"), ("alarm", "")]
    assert recording.commands[(1, 0)] == "write stdout put apple basket;"


def test_the_page_plays_a_recording_back_and_takes_no_orders(tmp_path: Path) -> None:
    drive(tmp_path / "session")
    live = Live(source=None, replay=Recording.load(tmp_path / "session"), speed=5000.0)
    live.start()
    assert live.ask("order", "desk-3") is not None
    for _ in range(200_000):
        live.once()
        if not live.pending:
            live.once()
            break
    snap = live.snapshot()
    assert snap["mode"] == "replay"
    assert snap["story"] and all(v["held"] for v in snap["story"])


def test_a_recording_replays_from_its_own_copy_of_the_case(tmp_path: Path) -> None:
    """A descriptor edited after the session changes what the kernel injects, so the
    recording carries the case as it was played and replays from that."""
    drive(tmp_path / "session")
    recording = Recording.load(tmp_path / "session")
    assert recording.case == tmp_path / "session" / "case" / "atrium"
    assert (recording.case / "handlers" / "deliver-coffee.md").read_text() == (
        CASE / "handlers" / "deliver-coffee.md"
    ).read_text()


def test_a_model_s_reply_is_recorded_beside_its_command(tmp_path: Path) -> None:
    from zeos.core.ids import JobId
    from zeos.machine.seat import Turn

    from zeos_stretch.recording import Recorder

    recorder = Recorder(tmp_path / "r", {"case": "cases/atrium"}, case=CASE)
    turn = Turn(job=JobId(3), descriptor="deliver-coffee", transcript="", issued=7)
    recorder.command(
        turn, "write stdout place coffee", reply="At desk-5, so: write stdout place coffee;"
    )
    recorder.close()
    line = (tmp_path / "r" / "commands.jsonl").read_text().strip()
    assert '"reply": "At desk-5, so: write stdout place coffee;"' in line


def test_the_claude_morning_still_comes_out_right() -> None:
    """The recorded Claude session the README walks through, replayed: Claude's own
    commands, given back to the kernel, still clear the table, deliver both coffees and
    keep both release policies. Byte equality is not asserted, because the journal it was
    recorded against belongs to one kernel and the kernel moves on."""
    result, _ = replay(Recording.load(CASE.parents[1] / "recordings" / "claude-morning"))
    assert [v.claim for v in check(result) if not v.held] == []
