"""The console's own layer: what a typed line means, and what the two threads promise.

The console is the one part of this demonstration that is *concurrent* -- the kernel is
turned on one thread while a person types on another -- so what is asserted here is the
contract between them rather than anything a job said: that the work happens at all, that
a hand typed at the prompt interrupts exactly as a hand dragged across the page does, that
a kernel thread dying reaches the main loop, and that the journal is whole afterwards.

``Script`` stands in for the person. It waits for the workspace to go quiet before typing
the next line, which is what makes the outcome the same however the two threads interleave.
"""

from __future__ import annotations

import io
import os
import time
from dataclasses import dataclass

import pytest
from conftest import CASE
from zeos.core.events import JobCompleted, JobPreempted, JobSpawned, VectorFired

from zeos_blocks.console import Console, History, Plain, Prompt, parse_line, table_cells
from zeos_blocks.planner import StubPlanner
from zeos_blocks.world import Table

GOAL = "move the green block from stack 1 to stack 2"
#: What a poll costs. A real display blocks for about this long in `read`, and the script
#: has to as well: a main loop that spins gives the kernel's thread no room to run between
#: polls, and the workspace looks quiet because nothing has had the chance to happen yet.
POLL = 0.01
#: Consecutive quiet polls before the script types again. A job woken by a delivery does
#: not run until the tick after it lands, so a single quiet poll is not yet a quiet
#: workspace.
SETTLED = 20
#: A bound, so a console wired up wrongly fails rather than hanging the suite.
GIVE_UP = 1500


@dataclass(frozen=True, slots=True)
class Line:
    """Something to type, and the moment to type it.

    ``running`` is the whole reason this is not just a list of strings. A hand typed once
    the workspace has gone quiet lands on a finished run and preempts nothing, which is
    the opposite of what a hand is for.
    """

    text: str
    when: str = "quiet"


class Script:
    """A display that types lines, each when the workspace is ready for it."""

    def __init__(self, console: Console, lines: list[Line | str]) -> None:
        self.console = console
        self.lines = [Line(x) if isinstance(x, str) else x for x in lines]
        self.notes: list[str] = []
        self.layouts: list[dict[str, str]] = []
        self.live: list[tuple[str, ...]] = []
        self.polls = 0
        self._quiet = 0

    def note(self, text: str) -> None:
        self.notes.append(text)

    def table(self, layout: dict[str, str]) -> None:
        self.layouts.append(dict(layout))

    def activity(self, live) -> None:
        self.live.append(tuple(live))

    def read(self) -> str | None:
        time.sleep(POLL)
        self.polls += 1
        if self.polls > GIVE_UP:
            return "/quit"
        if self.lines and self.lines[0].when == "running":
            return self.lines.pop(0).text if self.console.live() else None
        self._quiet = 0 if (self.console.session.busy or self.console.live()) else self._quiet + 1
        if self._quiet < SETTLED:
            return None
        self._quiet = 0
        return self.lines.pop(0).text if self.lines else "/quit"


def _drive(lines: list[Line | str], *, journal=None, pace: float = 0.0) -> tuple[Console, Script]:
    console = Console(CASE, StubPlanner(), pace=pace, settle=0.0, journal=journal)
    script = Script(console, lines)
    console.drive(script)
    return console, script


def _of(console: Console, kind) -> list:
    return [e for e in console.session.events if isinstance(e, kind)]


# -- what a typed line means ------------------------------------------------


def test_anything_not_a_command_is_spoken_uninspected() -> None:
    typed = parse_line("  could you get the green one onto stack 2  ")
    assert (typed.kind, typed.text) == ("say", "could you get the green one onto stack 2")


def test_a_hand_needs_a_block_and_a_position() -> None:
    assert parse_line("/hand g1 s3").hand == ("g1", "s3")
    assert parse_line("/hand g1").kind == "unknown"
    assert parse_line("/hand g1 s3 s2").kind == "unknown"


def test_the_commands_the_page_has_buttons_for() -> None:
    assert parse_line("/tidy").kind == "tidy"
    assert parse_line("/quit").kind == "quit"
    assert parse_line("/q").kind == "quit"
    assert parse_line("").kind == "nothing"
    assert parse_line("/").kind == "unknown"
    assert parse_line("/nonsense").kind == "unknown"


# -- the two threads --------------------------------------------------------


def test_a_line_typed_at_the_prompt_gets_the_work_done() -> None:
    console, script = _drive([GOAL])
    kinds = {type(e).__name__ for e in console.session.events}
    assert {"JobSpawned", "WorldWritten", "JobCompleted"} <= kinds
    assert "stacker" in [str(e.descriptor) for e in _of(console, JobSpawned)]
    assert _of(console, JobCompleted), "the stacker finished"
    # The world store, not anything the job said.
    assert "g1" in Table.of(script.layouts[-1]).stacks["s2"]


def test_a_hand_typed_at_the_prompt_interrupts_a_running_planner() -> None:
    """The same scheduling fact `test_interrupt.py` asserts, reached by typing instead.

    `/hand` is the console's whole answer to dragging a block across the page, so if it
    went anywhere other than `Session.disturb` the vector would not fire and nothing
    would be preempted.

    Paced rather than run flat out, so the hand lands on a stacker that is still working.
    At `pace=0` the stub finishes in less time than it takes to notice it started, and
    the test would be asserting preemption of a job that had already gone.
    """
    console, _ = _drive([GOAL, Line("/hand g1 s2", when="running")], pace=0.02)
    assert [str(e.vector) for e in _of(console, VectorFired)] == ["hand-in-workspace"]
    assert "noticed" in [str(e.descriptor) for e in _of(console, JobSpawned)]
    assert _of(console, JobPreempted), "the hand took the machine off the planner"


def test_an_unknown_command_is_answered_rather_than_spoken() -> None:
    """A mistyped command must not reach the front door, where it would compile."""
    console, _ = _drive(["/nonsense"])
    assert not _of(console, JobSpawned), "nothing was spawned by a mistyped command"


def test_the_table_is_pushed_as_it_changes_not_only_at_the_end() -> None:
    _console, script = _drive([GOAL])
    assert len(script.layouts) > 2, "a frame per move, not one at the end"


def test_a_kernel_thread_that_dies_reaches_the_main_loop() -> None:
    """A daemon thread taking the kernel with it would otherwise leave a still screen.

    The bound in `Script` would stop this too, eventually. Asserting the poll count is
    well under it is what tells the two apart: the loop ended because it was told, not
    because the test gave up.
    """
    console = Console(CASE, StubPlanner(), pace=0.0, settle=0.0)
    steps = {"n": 0}
    turning = console.session.step

    def explode() -> bool:
        steps["n"] += 1
        if steps["n"] > 5:
            raise RuntimeError("simulated kernel fault")
        return turning()

    console.session.step = explode  # type: ignore[method-assign]
    script = Script(console, [GOAL])
    console.drive(script)

    assert isinstance(console.failure, RuntimeError)
    assert str(console.failure) == "simulated kernel fault"
    assert script.polls < GIVE_UP / 10, "stopped because the kernel died, not on the bound"


def test_the_console_writes_a_journal_the_debugger_can_read(tmp_path) -> None:
    """Flushed by `drive`, after the kernel's thread has been joined and not before."""
    from zeos.journal.writer import read_journal

    path = tmp_path / "run.jsonl"
    console, _ = _drive([GOAL], journal=path)
    records = read_journal(path)
    kinds = {type(r.event).__name__ for r in records}

    assert len(records) > 100, "a whole run, not just the boot sequence"
    assert {"JobSpawned", "WorldWritten", "JobCompleted"} <= kinds
    assert [r.seq for r in records] == list(range(len(records))), "no gaps"
    assert len(records) == len(console.session.events)


# -- the table, drawn -------------------------------------------------------


def _picture(layout: dict[str, str], *, rows: int = 24, cols: int = 80) -> list[str]:
    """`table_cells` blitted onto a grid, which is what the screen does with them."""
    cells, _under = table_cells(layout, rows=rows, cols=cols)
    grid: dict[tuple[int, int], str] = {}
    for cell in cells:
        for offset, character in enumerate(cell.text):
            grid[(cell.row, cell.col + offset)] = character
    height = max((r for r, _ in grid), default=0) + 1
    span = max((c for _, c in grid), default=0) + 1
    return ["".join(grid.get((r, c), " ") for c in range(span)) for r in range(height)]


def test_a_stack_is_drawn_bottom_to_top() -> None:
    """`g1,r1` is `g1` on the table with `r1` on it, so `r1` is drawn above `g1`.

    The one thing a picture can get backwards, and the one thing about it a reader would
    take on trust.
    """
    picture = _picture({"s1": "g1,r1", "s2": "", "s3": ""})
    rows = {
        name: next(i for i, line in enumerate(picture) if name in line) for name in ("g1", "r1")
    }
    assert rows["r1"] < rows["g1"], "the block on top is drawn higher up the screen"


def test_an_empty_position_draws_no_block() -> None:
    """`-` is what an empty position reports, not something standing on it.

    Worth its own test because the marker is a perfectly good block name as far as a
    picture is concerned: a table would gain one every time a position was emptied.
    """
    picture = _picture({"s1": "-", "s2": "(unset)", "s3": "g1"})
    assert not any("-" in line and "─" not in line for line in picture[:-2]), picture
    assert sum(line.count("│") for line in picture) == 2, "one block on the table, so two walls"


def test_every_block_is_a_closed_box() -> None:
    """The arrangement the naming rules exist for, and the one a picture can lose.

    A block resting on another shows the floor of the one above and the lid of the one
    below, so `g1,g2` is two boxes and not one tall one. Nothing shares an edge, which is
    what keeps that true whatever colours the two happen to be.
    """
    picture = _picture({"s1": "g1,g2", "s2": "b1", "s3": ""})
    lids = sum(line.count("┌") for line in picture)
    floors = sum(line.count("└") for line in picture)
    assert lids == floors == 3, "a lid and a floor for each of the three blocks"
    assert not any("├" in line for line in picture), "no block shares an edge with another"


def test_the_position_labels_sit_under_the_surface() -> None:
    picture = _picture({"s1": "g1", "s2": "", "s3": ""})
    assert "━" in picture[-2]
    assert picture[-1].split() == ["s1", "s2", "s3"]


def test_a_picture_too_tall_for_the_window_falls_back_to_the_line() -> None:
    """`zeos-blocks new` makes workspaces a dozen blocks high.

    A drawing clipped to the window is worse than a line that is merely terse, so the
    fallback is the picture's own business rather than something the screen notices.
    """
    tall = {"s1": ",".join(f"g{i}" for i in range(1, 13)), "s2": "", "s3": ""}
    cells, under = table_cells(tall, rows=8, cols=80)
    assert [cell.text for cell in cells] == [Table.of(tall).render()]
    assert under == 1, "one row used, so the transcript starts under it"


def test_a_picture_too_wide_for_the_window_falls_back_to_the_line() -> None:
    wide = {f"s{i}": "g1" for i in range(1, 13)}
    cells, _under = table_cells(wide, rows=24, cols=40)
    assert [cell.text for cell in cells] == [Table.of(wide).render()]
    # The same table in a window that fits it is drawn, so the fallback is a decision
    # about the window and not a table this function simply cannot draw.
    assert len(table_cells(wide, rows=24, cols=140)[0]) > 1


def test_every_block_is_given_the_letter_that_colours_it() -> None:
    cells, _under = table_cells({"s1": "g1,r1", "s2": "b1", "s3": ""}, rows=24, cols=80)
    assert {c.letter for c in cells if not c.dim} == {"g", "r", "b"}
    # The surface and the position labels belong to no block, so they carry no colour.
    assert all(c.letter == "" for c in cells if c.dim)


# -- editing the line at the prompt -----------------------------------------


def _typed(text: str) -> Prompt:
    prompt = Prompt()
    for character in text:
        prompt.insert(character)
    return prompt


def test_a_character_goes_in_where_the_cursor_is() -> None:
    """The whole point of the left and right arrows: mend a line rather than retype it.

    A block's number left out, put back by walking the cursor to it.
    """
    prompt = _typed("/hand g s3")
    for _ in range(3):
        prompt.left()
    prompt.insert("1")
    assert (prompt.text, prompt.cursor) == ("/hand g1 s3", 8)


def test_backspace_takes_the_character_before_the_cursor() -> None:
    """Not the last one on the line, which is what it used to be and why this exists."""
    prompt = _typed("/hand g11 s3")
    for _ in range(4):
        prompt.left()
    prompt.backspace()
    assert (prompt.text, prompt.cursor) == ("/hand g1 s3", 7)


def test_the_cursor_stops_at_either_end() -> None:
    prompt = _typed("g1")
    for _ in range(5):
        prompt.left()
    assert prompt.cursor == 0
    prompt.backspace()
    assert prompt.text == "g1", "nothing to take out at the start of a line"
    for _ in range(5):
        prompt.right()
    assert prompt.cursor == 2


def test_delete_takes_the_character_under_the_cursor() -> None:
    """Backspace takes the one behind; delete takes the one in front."""
    prompt = _typed("/hand gx1 s3")
    prompt.home()
    for _ in range(7):
        prompt.right()
    prompt.delete()
    assert (prompt.text, prompt.cursor) == ("/hand g1 s3", 7), "the cursor stays where it is"


def test_delete_at_the_end_of_a_line_takes_nothing() -> None:
    prompt = _typed("/tidy")
    prompt.delete()
    assert (prompt.text, prompt.cursor) == ("/tidy", 5)


def test_home_and_end_go_to_either_end_of_the_line() -> None:
    prompt = _typed("/hand g1 s3")
    prompt.home()
    assert prompt.cursor == 0
    prompt.insert("!")
    assert prompt.text == "!/hand g1 s3"
    prompt.end()
    assert prompt.cursor == 12
    prompt.insert("?")
    assert prompt.text == "!/hand g1 s3?"


def test_a_recalled_line_is_ready_to_be_added_to() -> None:
    """What the up arrow leaves behind: the cursor after the line, not inside it."""
    prompt = _typed("half a thou")
    prompt.replace("/hand g1 s3")
    assert (prompt.text, prompt.cursor) == ("/hand g1 s3", 11)
    prompt.insert("!")
    assert prompt.text == "/hand g1 s3!"


def test_taking_the_line_leaves_the_prompt_empty() -> None:
    prompt = _typed("/tidy")
    prompt.left()
    assert prompt.take() == "/tidy"
    assert (prompt.text, prompt.cursor) == ("", 0), "the cursor comes back with the line"


def test_a_recalled_line_can_be_mended_and_sent() -> None:
    """The two together, which is the thing actually being asked for.

    The same hand again but the other block: walk back over `g1`, mend the number, send.
    """
    history = History()
    history.remember("/hand g1 s2")
    prompt = Prompt()
    prompt.replace(history.back(prompt.text))
    for _ in range(3):
        prompt.left()
    prompt.backspace()
    prompt.insert("2")
    assert prompt.take() == "/hand g2 s2"


# -- what the arrows recall -------------------------------------------------


def test_the_arrows_walk_back_through_what_was_sent() -> None:
    history = History()
    for line in ("first", "second", "third"):
        history.remember(line)
    assert history.back("") == "third"
    assert history.back("third") == "second"
    assert history.back("second") == "first"
    # Nothing older than the oldest: the arrow stops rather than wrapping round.
    assert history.back("first") == "first"


def test_coming_forward_again_gives_back_the_interrupted_line() -> None:
    """Reaching for history has to cost nothing, or it is not worth reaching for."""
    history = History()
    history.remember("move the green block from stack 1 to stack 2")
    assert history.back("half a thou") == "move the green block from stack 1 to stack 2"
    assert history.forward("move the green block from stack 1 to stack 2") == "half a thou"
    # Past the newest line there is nothing further forward to go to.
    assert history.forward("half a thou") == "half a thou"


def test_sending_a_line_puts_the_arrows_back_at_the_end() -> None:
    history = History()
    history.remember("first")
    history.remember("second")
    assert history.back("") == "second", "the newest, not wherever the arrows were left"
    history.remember("third")
    assert history.back("") == "third"


def test_a_line_repeated_straight_away_is_not_kept_twice() -> None:
    """Or the arrows walk over the same line twice to get past it.

    The older line is what tells the two apart: with the repeat kept, one press back from
    `/tidy` is `/tidy` again, and `first` is two presses away rather than one.
    """
    history = History()
    history.remember("first")
    history.remember("/tidy")
    history.remember("/tidy")
    assert history.back("") == "/tidy"
    assert history.back("/tidy") == "first"


def test_nothing_is_recalled_before_anything_has_been_sent() -> None:
    history = History()
    assert history.back("half a thought") == "half a thought"
    assert history.forward("half a thought") == "half a thought"


# -- the display that needs no terminal -------------------------------------


def test_the_plain_display_draws_the_table_only_when_it_changes() -> None:
    out = io.StringIO()
    plain = Plain(out, io.StringIO(), "header")
    plain.table({"s1": "g1", "s2": "", "s3": ""})
    plain.table({"s1": "g1", "s2": "", "s3": ""})
    plain.table({"s1": "", "s2": "g1", "s3": ""})
    assert out.getvalue().count("s1:") == 2


def test_the_plain_display_reads_a_line_then_reports_the_end_of_its_input() -> None:
    """A real pipe, because `select` wants a descriptor and that is the point of it here."""
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "w") as writing:
        writing.write(f"{GOAL}\n")
    with os.fdopen(read_fd) as reading:
        plain = Plain(io.StringIO(), reading, "header")
        assert plain.read() == GOAL
        with pytest.raises(EOFError):
            plain.read()


def test_a_schedule_piped_in_is_finished_rather_than_abandoned() -> None:
    """The end of the input is not the end of the work.

    `echo "..." | zeos-blocks console` delivers a line and immediately reaches EOF. A
    console that quit there would stop before the stacker it just asked for had run,
    which looks exactly like a planner that decided to do nothing.
    """
    read_fd, write_fd = os.pipe()
    with os.fdopen(write_fd, "w") as writing:
        writing.write(f"{GOAL}\n")
    console = Console(CASE, StubPlanner(), pace=0.0, settle=0.0)
    out = io.StringIO()
    with os.fdopen(read_fd) as reading:
        console.drive(Plain(out, reading, "header"))

    kinds = {type(e).__name__ for e in console.session.events}
    assert {"JobSpawned", "WorldWritten", "JobCompleted"} <= kinds
    assert "g1" in Table.of(console.session.positions()).stacks["s2"]
