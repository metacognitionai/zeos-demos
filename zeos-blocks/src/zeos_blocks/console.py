"""The terminal as a device adapter.

The same job ``web/server.py`` does for a browser. A line typed at the prompt becomes
``Session.say``, a drained sink becomes a line in the transcript, and ``/hand g1 s3``
becomes ``Session.disturb``. Nothing here decides anything about blocks: the only verbs
it has are say, disturb, tidy and watch.

**Two threads, each owning one thing that may not be shared.** The kernel is not
re-entrant, so one thread calls ``Session.step`` and nothing else touches the kernel.
ncurses is not re-entrant either -- CPython links ``libncursesw``, not the reentrant
``libncursestw`` -- so the main thread makes every curses call and nothing else draws.
The two constraints line up: the session's callbacks fire on the kernel's thread and may
only *enqueue*, exactly as ``BlocksServer._push`` puts a message on a watcher's queue
rather than writing the socket from under the kernel.

**A dead kernel thread has to reach the main loop.** It is a daemon thread, so an
exception there would otherwise leave a screen that has stopped updating and looks merely
quiet, with the message on a stderr that curses has made invisible. ``_turn`` records the
failure and sets ``_stop`` whatever happens; the main loop is polling that flag anyway;
and the message is printed once the terminal has been given back.
"""

from __future__ import annotations

import curses
import locale
import queue
import select
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, TextIO

from zeos.core.events import Event, WorldWritten
from zeos.core.ids import JobState
from zeos.descriptor.loader import load_case
from zeos.machine.seat import CommandSource

from zeos_blocks.session import Session
from zeos_blocks.world import Table

__all__ = [
    "Cell",
    "Console",
    "History",
    "Plain",
    "Screen",
    "Typed",
    "console",
    "parse_line",
    "table_cells",
]

HELP = "type a move, or /hand g1 s3, /tidy, /quit"

#: How long a display waits for input before letting the main loop go round again. It is
#: also how quickly a kernel that has died is noticed, since that flag is polled here.
POLL = 0.1

#: Consecutive quiet polls before a console with no more input to read stops. A job woken
#: by a delivery does not run until the tick after it lands, so one quiet poll is not yet
#: a quiet workspace.
SETTLED = 5


# -- what a typed line means ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Typed:
    """One line from the prompt, as the console read it.

    Parsed apart from anything that draws or steps, because what ``/hand r1 s3`` means is
    the one thing here worth testing without a terminal and without a kernel.
    """

    kind: str
    text: str = ""
    hand: tuple[str, str] | None = None


def parse_line(line: str) -> Typed:
    """A line into what the console should do with it.

    Anything not starting with ``/`` is spoken, uninspected. Whether it is a move, whether
    a stacker is already running, whether the table can satisfy it -- all three are the
    kernel's, and a prompt that pre-screened would be the application-level branch a
    descriptor tree exists to remove.
    """
    stripped = line.strip()
    if not stripped:
        return Typed("nothing")
    if not stripped.startswith("/"):
        return Typed("say", stripped)
    words = stripped[1:].split()
    if not words:
        return Typed("unknown", HELP)
    verb, rest = words[0], words[1:]
    if verb in ("quit", "q"):
        return Typed("quit")
    if verb == "tidy":
        # Not checked against the case. A case without a background goal answers
        # "(there is no tidy in this case)" through the session, which is the same
        # answer arrived at in one place rather than two.
        return Typed("tidy")
    if verb == "hand":
        if len(rest) != 2:
            return Typed("unknown", "/hand takes a block and a position: /hand g1 s3")
        return Typed("hand", hand=(rest[0], rest[1]))
    return Typed("unknown", f"no such command: /{verb} -- {HELP}")


# -- the table, drawn ------------------------------------------------------


#: Narrowest a block is drawn, which is what a two-character name needs: `|`, a space
#: either side of the name, and `|`. A longer name widens every block rather than only
#: its own, so a stack stays a stack.
NARROWEST = 6
#: Columns of table between one position and the next.
GUTTER = 2


@dataclass(frozen=True, slots=True)
class Cell:
    """One run of characters to draw, and what colours it.

    ``letter`` is the first character of a block's name, which is what carries its colour
    -- the same rule the names themselves are built on. Empty for the table surface and
    the position labels, which belong to no block.
    """

    row: int
    col: int
    text: str
    letter: str = ""
    dim: bool = False


def table_cells(
    layout: dict[str, str], *, rows: int, cols: int, top: int = 0
) -> tuple[list[Cell], int]:
    """The table as blocks standing on a surface. Returns the cells and the row under them.

    Each block is a closed box of its own, so a block resting on another shows both the
    floor of the one above and the lid of the one below. Without a boundary of its own
    each way, two blocks of one colour stacked up read as a single tall one, which is
    exactly the arrangement the naming rules exist for.

    Falls back to the one-line form when the picture will not fit. `zeos-blocks new` makes
    workspaces a dozen blocks high and a dozen positions wide, and a drawing clipped to
    the window is worse than a line that is merely terse.
    """
    # Through `Table.of`, which is the one place that knows an empty position reports
    # `-` and an undeclared one `(unset)`. Splitting the strings here would draw both of
    # those as blocks, and a table would gain a block every time one was emptied.
    stacks = Table.of(layout).stacks
    longest = max((len(b) for stack in stacks.values() for b in stack), default=2)
    width = max(NARROWEST, longest + 4)
    tallest = max((len(s) for s in stacks.values()), default=0)
    span = len(stacks) * width + max(0, len(stacks) - 1) * GUTTER
    # Three rows per block -- lid, name, floor -- plus the surface they stand on and the
    # row of position names.
    needed = 3 * tallest + 2
    if span > cols - 1 or needed > rows:
        return [Cell(top, 0, Table(stacks).render())], top + 1

    cells: list[Cell] = []
    surface = top + 3 * tallest
    span_of = "─" * (width - 2)
    for index, (position, stack) in enumerate(stacks.items()):
        left = index * (width + GUTTER)
        cells.append(Cell(surface, left, "━" * width, dim=True))
        cells.append(Cell(surface + 1, left, position.center(width), dim=True))
        for height, block in enumerate(stack):
            # The floor of the bottom block rests on the surface, and each block above
            # stands on the lid of the one under it.
            floor = surface - 1 - 3 * height
            letter = block[:1]
            cells.append(Cell(floor - 2, left, f"┌{span_of}┐", letter))
            cells.append(Cell(floor - 1, left, f"│{block.center(width - 2)}│", letter))
            cells.append(Cell(floor, left, f"└{span_of}┘", letter))
    return cells, surface + 2


# -- the two displays -------------------------------------------------------


class Display(Protocol):
    """What the console needs of something a person is watching.

    ``read`` is also what paces the main loop: both implementations give up after
    ``POLL``, so a workspace nobody is typing at still notices a dead kernel promptly
    without spinning. It raises ``EOFError`` when there will be no more input ever.
    """

    def note(self, text: str) -> None: ...

    def table(self, layout: dict[str, str]) -> None: ...

    def activity(self, live: Sequence[str]) -> None: ...

    def read(self) -> str | None: ...


class History:
    """Lines already sent, and where the up and down arrows have got to in them.

    Apart from the screen because there is nothing about it that needs one, and because
    where the arrows are is the part with any behaviour in it worth checking.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []
        #: Where the arrows are, or None when the prompt holds a new line rather than a
        #: remembered one.
        self._at: int | None = None
        #: What was being typed when the arrows were first pressed. Arriving back past
        #: the newest line gives it back, so reaching for history costs nothing.
        self._draft = ""

    def remember(self, line: str) -> None:
        """Keep a line that was sent, and put the arrows back at the end."""
        # A line repeated straight away is already there, and keeping it twice makes the
        # arrows walk over the same line twice to get past it.
        if line and line != (self._lines[-1] if self._lines else None):
            self._lines.append(line)
        self._at, self._draft = None, ""

    def back(self, typed: str) -> str:
        """The line before this one, or what is at the prompt if there is nothing older."""
        if not self._lines:
            return typed
        if self._at is None:
            self._draft, self._at = typed, len(self._lines) - 1
        else:
            self._at = max(0, self._at - 1)
        return self._lines[self._at]

    def forward(self, typed: str) -> str:
        """The line after this one, then the draft that was interrupted to go looking."""
        if self._at is None:
            return typed
        if self._at >= len(self._lines) - 1:
            self._at = None
            return self._draft
        self._at += 1
        return self._lines[self._at]


class Prompt:
    """The line being typed, and where in it the cursor sits.

    Apart from the screen for the same reason ``History`` is: putting a character into
    the middle of a line is the part with behaviour in it, and none of that behaviour
    needs a terminal to have it checked.
    """

    def __init__(self) -> None:
        self.text = ""
        self.cursor = 0

    def insert(self, character: str) -> None:
        self.text = f"{self.text[: self.cursor]}{character}{self.text[self.cursor :]}"
        self.cursor += 1

    def backspace(self) -> None:
        """Take out the character before the cursor, and nothing at the start of a line."""
        if self.cursor:
            self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
            self.cursor -= 1

    def delete(self) -> None:
        """Take out the character under the cursor.

        At the end of a line there is nothing under it, and the slice that takes nothing
        out is the same slice, so this needs no asking first.
        """
        self.text = self.text[: self.cursor] + self.text[self.cursor + 1 :]

    def left(self) -> None:
        self.cursor = max(0, self.cursor - 1)

    def right(self) -> None:
        self.cursor = min(len(self.text), self.cursor + 1)

    def home(self) -> None:
        self.cursor = 0

    def end(self) -> None:
        self.cursor = len(self.text)

    def replace(self, text: str) -> None:
        """Put a whole line at the prompt, ready to be added to. What the arrows do."""
        self.text, self.cursor = text, len(text)

    def take(self) -> str:
        """The line, leaving the prompt empty."""
        line, self.text, self.cursor = self.text, "", 0
        return line


class Screen:
    """The full-screen display. **Every method here runs on the main thread.**

    The table stays where it is while the transcript scrolls underneath it, which is the
    whole reason to take the screen over: on a workspace being driven by hand the table is
    the thing being read, and in a scrolling log it is the thing that has just gone past.
    """

    def __init__(self, stdscr: Any, header: str) -> None:
        self._scr = stdscr
        self._header = header
        self._lines: list[str] = []
        self._layout: dict[str, str] = {}
        self._live: tuple[str, ...] = ()
        self._prompt = Prompt()
        self._history = History()
        self._rows, self._cols = stdscr.getmaxyx()
        self._colours = _colours()
        curses.halfdelay(max(1, round(POLL * 10)))  # tenths of a second; never blocks
        curses.curs_set(1)

    def note(self, text: str) -> None:
        self._lines.append(text)

    def table(self, layout: dict[str, str]) -> None:
        self._layout = layout

    def activity(self, live: Sequence[str]) -> None:
        self._live = tuple(live)

    def read(self) -> str | None:
        self._draw()
        # The arrows arrive as single keys rather than as the escape sequences that
        # carry them, because `curses.wrapper` puts the window in keypad mode.
        key = self._scr.getch()
        if key in (10, 13):
            line = self._prompt.take()
            self._history.remember(line)
            return line
        if key == curses.KEY_UP:
            self._prompt.replace(self._history.back(self._prompt.text))
        elif key == curses.KEY_DOWN:
            self._prompt.replace(self._history.forward(self._prompt.text))
        elif key == curses.KEY_LEFT:
            self._prompt.left()
        elif key == curses.KEY_RIGHT:
            self._prompt.right()
        elif key == curses.KEY_HOME:
            self._prompt.home()
        elif key == curses.KEY_END:
            self._prompt.end()
        elif key == curses.KEY_DC:
            self._prompt.delete()
        elif key in (curses.KEY_BACKSPACE, 127, 8):
            self._prompt.backspace()
        elif 32 <= key < 127:
            self._prompt.insert(chr(key))
        return None

    def _draw(self) -> None:
        # Read every frame rather than off `curses.LINES`, which is what makes a resize
        # need no handler: the next frame is simply drawn to the new size.
        self._rows, self._cols = self._scr.getmaxyx()
        self._scr.erase()
        self._put(0, 0, self._header, curses.A_BOLD)
        # Whatever is left once the header, the line saying what is running and the
        # prompt have had their rows.
        cells, under = table_cells(self._layout, rows=self._rows - 3, cols=self._cols, top=1)
        for cell in cells:
            attr = curses.A_DIM if cell.dim else self._colours.get(cell.letter, 0)
            self._put(cell.row, cell.col, cell.text, attr)
        self._put(under, 0, "running: " + (", ".join(self._live) or "nothing"), curses.A_DIM)
        room = self._rows - under - 3
        if room > 0:
            for offset, line in enumerate(self._lines[-room:]):
                self._put(under + 2 + offset, 0, line)
        self._put(self._rows - 1, 0, f"> {self._prompt.text}")
        self._scr.move(self._rows - 1, min(2 + self._prompt.cursor, self._cols - 1))
        self._scr.refresh()

    def _put(self, row: int, col: int, text: str, attr: int = 0) -> None:
        """Write what fits of it, where it fits.

        Not defensive. A window smaller than the table is an ordinary thing for somebody
        to make, and curses raises rather than wrapping when something is written past the
        last column or below the last row.
        """
        if not 0 <= row < self._rows:
            return
        room = self._cols - 1 - col
        if room > 0:
            self._scr.addstr(row, col, text[:room], attr)


def _colours() -> dict[str, int]:
    """A curses attribute per block-name letter, empty on a terminal without colour.

    The names read either way; colour is what lets *the green one* be found by eye rather
    than by spelling, which is how the page is read and most of what it is for.
    """
    if not curses.has_colors():
        return {}
    curses.use_default_colors()
    orange = 208 if curses.COLORS >= 256 else curses.COLOR_YELLOW
    wanted = {
        "r": curses.COLOR_RED,
        "g": curses.COLOR_GREEN,
        "b": curses.COLOR_BLUE,
        "y": curses.COLOR_YELLOW,
        "o": orange,
        "p": curses.COLOR_MAGENTA,
    }
    attrs = {}
    for index, (letter, colour) in enumerate(wanted.items(), start=1):
        curses.init_pair(index, colour, -1)
        attrs[letter] = curses.color_pair(index) | curses.A_BOLD
    return attrs


class Plain:
    """The scrolling display: output appended, input read a line at a time.

    Here because curses needs a terminal and a console is worth having where there is not
    one -- output redirected, a schedule piped in, `ssh host zeos-blocks console` with no
    pty asked for. It draws no prompt: the input it is given is usually not a person, and
    a terminal that has one echoes what is typed without help.
    """

    def __init__(self, stream: TextIO, stdin: TextIO, header: str) -> None:
        self._out = stream
        self._in = stdin
        self._rendered = ""
        print(header, file=self._out, flush=True)

    def note(self, text: str) -> None:
        print(text, file=self._out, flush=True)

    def table(self, layout: dict[str, str]) -> None:
        rendered = Table.of(layout).render()
        if rendered != self._rendered:
            self._rendered = rendered
            print(f"  {rendered}", file=self._out, flush=True)

    def activity(self, live: Sequence[str]) -> None:
        """Not shown. Every job that starts and finishes is already a line here."""

    def read(self) -> str | None:
        """A line, or None if none arrived. Raises ``EOFError`` at the end of the input."""
        ready, _, _ = select.select([self._in], [], [], POLL)
        if not ready:
            return None
        line = self._in.readline()
        if not line:
            raise EOFError
        return line.rstrip("\n")


# -- the console ------------------------------------------------------------


class Console:
    """A workspace, a display, and the thread that turns the kernel."""

    def __init__(
        self,
        case: Path,
        source: CommandSource,
        *,
        pace: float = 0.09,
        settle: float = 0.8,
        journal: Path | None = None,
    ) -> None:
        self.bundle = load_case(case)
        #: Everything the kernel's thread has to say, drained by the main thread. The one
        #: rule here: a callback may put on this and may not draw.
        self._paint: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._table_changed = False
        self._activity: tuple[str, ...] = ()
        self.session = Session(
            self.bundle,
            source,
            on_reply=lambda text: self._paint.put(("line", f"  <-- {text}")),
            on_command=lambda who, line: self._paint.put(("line", f"{who:<9} {line}")),
            on_event=self._on_events,
            journal=journal,
        )
        #: What is deciding the moves, read off the source, for the same reason the page
        #: shows it: the kernel cannot tell which of these it is running.
        self.planner = str(getattr(source, "label", type(source).__name__))
        self._pace = pace
        self._settle = settle
        self._stop = threading.Event()
        #: What killed the kernel's thread, said by the main thread once the terminal is
        #: back. Written by the kernel's thread and read after it has been joined.
        self.failure: BaseException | None = None
        self._thread = threading.Thread(target=self._turn, name="kernel", daemon=True)

    # -- the kernel's thread ------------------------------------------------

    def _turn(self) -> None:
        """Turn the kernel, and rest when it has nothing to do. **Draws nothing.**"""
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                ran = self.session.step()
                if self._table_changed:
                    self._paint.put(("table", self.session.positions()))
                    self._table_changed = False
                    # Nothing runs while the block is in the air. Jobs are not waiting for
                    # the arm -- the move is already made -- they are waiting for a person
                    # to be able to follow what happened.
                    time.sleep(max(0.0, self._settle - (time.monotonic() - started)))
                if (live := self.live()) != self._activity:
                    self._activity = live
                    self._paint.put(("activity", live))
                # A workspace nobody has spoken to has no jobs at all, and asking a
                # hundred times a second whether that is still true costs a core.
                time.sleep(self._pace if ran else max(self._pace, 0.02))
        except BaseException as failure:  # noqa: BLE001 - this is a thread boundary
            self.failure = failure
        finally:
            # Whatever happened, the main loop has to stop waiting. It polls this flag
            # between keystrokes, so a kernel that has died is noticed within one poll.
            self._stop.set()

    def _on_events(self, events: Sequence[Event]) -> None:
        for event in events:
            if isinstance(event, WorldWritten):
                # Set on the kernel journalling the write and not on the delivery being
                # made: a delivery lands on the following step, so a table drawn at
                # delivery time is always one move behind.
                self._table_changed = True

    def live(self) -> tuple[str, ...]:
        """Each job that has not finished, by name. Safe to read from any thread."""
        return tuple(
            str(job.descriptor.name)
            for job in self.session.kernel.sched.jobs()
            if job.state not in (JobState.DONE, JobState.FAULTED)
        )

    # -- the main thread ----------------------------------------------------

    def drive(self, display: Display) -> None:
        """Turn the display until somebody quits or the kernel stops. **Main thread.**"""
        self._thread.start()
        reading, quiet = True, 0
        try:
            display.table(self.session.positions())
            while not self._stop.is_set():
                self._drain(display)
                try:
                    line = display.read()
                except EOFError:
                    # A schedule piped in has run out, or the terminal has gone. What was
                    # already asked for is not abandoned: the console stops reading and
                    # keeps turning until the workspace has nothing left to do, which is
                    # what makes `echo "..." | zeos-blocks console` finish the work.
                    reading, line = False, None
                if line is not None and self._act(display, line):
                    break
                if not reading:
                    quiet = 0 if (self.session.busy or self.live()) else quiet + 1
                    if quiet >= SETTLED:
                        break
        finally:
            # Stop and join before closing. `Session.close` flushes whatever the
            # events list holds when it is called, so a step still in flight on the
            # kernel's thread is a step whose events never reach the file.
            self._stop.set()
            self._thread.join(timeout=2)
            self.session.close()

    def _drain(self, display: Display) -> None:
        while True:
            try:
                kind, payload = self._paint.get_nowait()
            except queue.Empty:
                return
            match kind:
                case "line":
                    display.note(payload)
                case "table":
                    display.table(payload)
                case "activity":
                    display.activity(payload)

    def _act(self, display: Display, line: str) -> bool:
        """Do what was typed. Returns whether the console should stop."""
        typed = parse_line(line)
        match typed.kind:
            case "quit":
                return True
            case "say":
                display.note(f"operator  --> {typed.text}")
                self.session.say(typed.text)
            case "hand":
                # One call, because a hand is an instant here: there is no drag to be part
                # way through. `Session.hold` covers a person dragging a block across a
                # page, which takes wall-clock time; typing a move takes none.
                assert typed.hand is not None
                display.note(f"  ~~ a hand: {self.session.disturb(*typed.hand)}")
            case "tidy":
                # A control, not a sentence: a descriptor name with no room to mean
                # anything else. The job is still the operator's, clamped to the
                # operator's ceiling, exactly as a spoken-for one would be.
                self.session.start_job("tidy")
            case "unknown":
                display.note(f"  {typed.text}")
        return False


def console(
    case: Path,
    source: CommandSource,
    *,
    plain: bool = False,
    pace: float = 0.09,
    settle: float = 0.8,
    journal: Path | None = None,
) -> int:
    """Run the workspace in this terminal. Returns the exit status."""
    driver = Console(case, source, pace=pace, settle=settle, journal=journal)
    header = f"{driver.bundle.name}  --  {driver.planner}  --  {HELP}"
    interrupted = False
    try:
        if plain or not sys.stdout.isatty():
            driver.drive(Plain(sys.stdout, sys.stdin, header))
        else:
            # Before curses starts. Python encodes what is drawn using the locale's
            # encoding, and the box the blocks are drawn with is not in ASCII.
            locale.setlocale(locale.LC_ALL, "")
            # Restores the terminal however the wrapped call ends, a signal included.
            # `cbreak` leaves ISIG on, so ctrl-c is a real SIGINT on this thread rather
            # than a keystroke, and lands here.
            curses.wrapper(lambda stdscr: driver.drive(Screen(stdscr, header)))
    except KeyboardInterrupt:
        interrupted = True
    # Said only now. Under curses there is no stderr a person can see, so anything
    # printed before the terminal was given back was printed into the screen being torn
    # down.
    if driver.failure is not None:
        print(f"the workspace stopped: {driver.failure}", file=sys.stderr)
        return 1
    print("stopping" if interrupted else "done")
    if journal is not None:
        print(f"journal written to {journal}")
    return 0
