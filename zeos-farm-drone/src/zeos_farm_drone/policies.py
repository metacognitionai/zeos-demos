"""A deterministic policy for each behaviour: the rules source.

Each policy reads only the job's transcript -- its task, its STATUS lines, the drone's
answers, the kernel's RESUME and FAULT notices -- and answers with the next command,
so it is right for any schedule rather than for one, and needs no key. The kernel
cannot tell it from a model.

Two policies are naive on purpose, where the kernel is the safety net: the vehicle
check follows a vehicle wherever it goes, and the fire watch climbs as high as the
distance suggests. The flight rules are what stop them.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from zeos.machine.seat import Turn

from zeos_farm_drone.farm import PADDOCKS
from zeos_farm_drone.rules import CEILING_M

__all__ = ["RulesSource"]

#: The longest `detail` an alert may carry (system/schemas.yaml).
DETAIL_MAX = 24
LOW_TROUGH = 30
#: The boundary point a drone may watch an off-farm place from.
WATCH_FROM = {"road": "boundary-west", "neighbour": "boundary-east"}
#: Where a lost item is looked for, nearest the shed first.
SEARCH_ORDER = ("house", "north-flat", "creek", "ridge", "dam", "back-block")

_STATUS = re.compile(r"<STATUS (?P<name>[\w.-]+)>(?P<value>.*?)</STATUS>", re.S)
_TAGS = "RESUME|FAULT|KERNEL"
#: The pipe aliases the behaviours bind. A command names one, which is what tells it
#: from prose that happens to begin with "write".
_ALIASES = "stdout|stdin|tools|farmer|vehicles|livestock"
_PIECE = re.compile(
    rf"(?P<tag><(?P<kind>{_TAGS})\b(?P<head>[^>]*)>(?P<body>.*?)</(?P=kind)>)"
    rf"|(?P<cmd>(?:write\s+(?:{_ALIASES})\s[^;<]*|read\s+(?:{_ALIASES})|exit);)",
    re.S,
)


@dataclass(frozen=True, slots=True)
class Piece:
    """One thing in a transcript: a command the job gave, a kernel frame, or text."""

    kind: str  # command | RESUME | FAULT | KERNEL | text
    text: str
    head: str = ""


class Seen:
    """A job's transcript, as the pieces a policy reasons about.

    The task's own examples are in backticks, so they are dropped first: a policy
    must never mistake ``write stdout fly PADDOCK;`` in the prose for a command it gave.
    """

    def __init__(self, transcript: str) -> None:
        text = re.sub(r"`[^`]*`", " ", transcript)
        # The kernel rewrites a status region where a block boundary falls, which can be
        # in the middle of the job's own command, so the regions are read first and
        # then taken out.
        self._status: dict[str, str] = {}
        for found in _STATUS.finditer(text):
            self._status[found["name"]] = " ".join(found["value"].split())
        text = _STATUS.sub(" ", text)
        self.pieces: list[Piece] = []
        at = 0
        for match in _PIECE.finditer(text):
            self._text(text[at : match.start()])
            if match["cmd"]:
                self.pieces.append(Piece("command", " ".join(match["cmd"].split())))
            else:
                self.pieces.append(
                    Piece(match["kind"], " ".join(match["body"].split()), match["head"].strip())
                )
            at = match.end()
        self._text(text[at:])

    def _text(self, raw: str) -> None:
        words = " ".join(raw.split())
        if words:
            self.pieces.append(Piece("text", words))

    # -- what the job was given ----------------------------------------------

    def status(self, name: str) -> str:
        return self._status.get(name, "")

    def payload(self, words: int) -> list[str]:
        """The last ``words`` words before the job's first command: the write that
        fired its vector, which the kernel injects after the task."""
        before: list[str] = []
        for piece in self.pieces:
            if piece.kind == "command":
                break
            if piece.kind == "text":
                before.extend(piece.text.split())
        return before[-words:]

    def argument(self, name: str) -> str:
        """A value the job was asked for with, from the kernel's arguments frame."""
        for piece in self.pieces:
            if piece.kind == "KERNEL":
                found = re.search(rf"\b{re.escape(name)}: (.+?) They are values", piece.text)
                if found:
                    return found[1].strip()
        return ""

    # -- what the job did and was told ---------------------------------------

    def commands(self) -> list[str]:
        return [p.text for p in self.pieces if p.kind == "command"]

    def answers(self) -> list[str]:
        """What the drone answered, in order: the text after each ``read stdin;``."""
        found: list[str] = []
        reading = False
        for piece in self.pieces:
            if piece.kind == "command":
                reading = piece.text == "read stdin;"
            elif piece.kind == "text" and reading:
                found.append(piece.text)
                reading = False
        return found

    def awaiting_answer(self) -> bool:
        """The last command went to the drone and was not vetoed, so its answer is
        what comes next. A vetoed command never reaches the drone: reading for its
        answer would wait for ever."""
        for piece in reversed(self.pieces):
            if piece.kind == "FAULT":
                return False
            if piece.kind == "command":
                return piece.text.startswith("write stdout ")
        return False

    def vetoed(self, command: str) -> bool:
        return any(p.kind == "FAULT" and f"(action: '{command}')" in p.text for p in self.pieces)

    def over(self, place: str) -> bool:
        """Whether the drone this job is flying is known to be over ``place``.

        Every answer from a drone says where it is, so the last one is the evidence. A
        RESUME notice after it means the job was away and may have been given another
        drone; and a drone handed back may have been flown elsewhere meanwhile, which
        the next answer reveals. A refusal after it is that answer: the drone was not
        where the job thought -- a flat battery took it home, say -- so it is not known
        to be anywhere until it is flown there again.
        """
        for piece in reversed(self.pieces):
            if piece.kind == "RESUME":
                return False
            if piece.kind == "text" and piece.text.startswith("refused "):
                return False
            if piece.kind == "text" and (where := _where(piece.text)) is not None:
                return where == place
        return False

    def altitude(self) -> int:
        """The height of the last climb the drone answered, as long as the job is
        still flying the same drone."""
        for piece in reversed(self.pieces):
            if piece.kind == "RESUME" and "self.platform:" in piece.text:
                return 0
            if piece.kind == "text" and piece.text.startswith("done climb "):
                return int(piece.text.removeprefix("done climb "))
        return 0


_WHERE = (
    re.compile(r"done fly ([\w-]+)$"),
    re.compile(r"observed ([\w-]+):"),
    re.compile(r"zoomed \w+ at ([\w-]+):"),
    re.compile(r"(?:found|no) .+ at ([\w-]+):"),
    re.compile(r"interrupted .* over ([\w-]+)$"),
)


def _where(answer: str) -> str | None:
    """Where a drone's answer says the drone is, if it says."""
    if answer == "done return":
        return "shed"
    for pattern in _WHERE:
        if found := pattern.match(answer):
            return found[1]
    return None


def _detail(*parts: str) -> str:
    """Words joined by hyphens, cut at a hyphen to fit an alert's `detail`."""
    words = "-".join("-".join(p.split()) for p in parts if p)
    if len(words) <= DETAIL_MAX:
        return words
    cut = words[:DETAIL_MAX]
    return cut[: cut.rfind("-")] if "-" in cut else cut


def _alert(kind: str, where: str, photo: str, *detail: str) -> str:
    return f"write farmer kind={kind} where={where} photo={photo} detail={_detail(*detail)};"


def _fields(answer: str) -> dict[str, str]:
    return dict(p.split("=", 1) for p in answer.split() if "=" in p)


def _herd(line: str) -> dict[str, int]:
    pairs = (p.partition("=") for p in line.split())
    return {k: int(v) for k, _, v in pairs if v.isdigit()}


# -- the behaviours ------------------------------------------------------------


#: What a look at a paddock takes in, at the end of an `observed` or a search's answer.
_IN_SIGHT = re.compile(r"cattle=(\d+) vehicles=(\S+) trough=(\d+)%$")


def _reports(seen: Seen, paddock: str, in_sight: re.Match[str]) -> list[str]:
    """The writes a look at ``paddock`` calls for, in order: a low trough, then each
    vehicle (the farmer first; the finding starts other work, which may take the drone),
    then a head count the farmer's records do not expect."""
    cattle, vehicles, trough = int(in_sight[1]), in_sight[2], int(in_sight[3])
    expected = _herd(seen.status("herd.expected"))
    listed = vehicles.split(",") if vehicles != "none" else []
    writes = (
        [_alert("trough", paddock, "none", f"trough-at-{trough}%")] if trough < LOW_TROUGH else []
    )
    writes += [_alert("vehicle", paddock, "none", v, "seen") for v in listed]
    writes += [f"write vehicles {paddock} {v};" for v in listed]
    if cattle != expected.get(paddock, cattle) and not any(
        c.startswith(f"write livestock {paddock} ") for c in seen.commands()
    ):
        writes.append(f"write livestock {paddock} counted={cattle} expected={expected[paddock]};")
    return writes


def _unsaid(seen: Seen, writes: list[str]) -> str | None:
    given = set(seen.commands())
    return next((w for w in writes if w not in given), None)


def patrol(seen: Seen) -> str:
    observed: list[str] = []
    for answer in seen.answers():
        found = re.match(r"observed ([\w-]+): ", answer)
        in_sight = _IN_SIGHT.search(answer)
        if found is None or in_sight is None:
            continue
        observed.append(found[1])
        if (write := _unsaid(seen, _reports(seen, found[1], in_sight))) is not None:
            return write
    remaining = [p for p in PADDOCKS if p not in observed]
    if not remaining:
        return "exit;" if "done return" in seen.answers() else "write stdout return;"
    paddock = remaining[0]
    return "write stdout observe;" if seen.over(paddock) else f"write stdout fly {paddock};"


def vehicle_check(seen: Seen) -> str:
    paddock, _ = seen.payload(2)
    looks = [a for a in seen.answers() if a.startswith("zoomed vehicle at ")]
    alerts = [c for c in seen.commands() if c.startswith("write farmer kind=vehicle ")]
    if not looks:
        return (
            "write stdout zoom vehicle;" if seen.over(paddock) else f"write stdout fly {paddock};"
        )
    look = looks[-1]
    at = look.removeprefix("zoomed vehicle at ").split(":", 1)[0]
    if "no vehicle in sight" in look:
        if len(alerts) < len(looks):
            return _alert("vehicle", at, "none", "gone-before-we-came")
        return "exit;"
    fields = _fields(look)
    name = look.split(": ", 1)[1].split()[0]
    if len(alerts) < len(looks):
        gone = re.search(r" on the (road|neighbour)", look)
        what = f"on-the-{gone[1]}" if gone else f"plate-{fields.get('plate', '')}"
        return _alert("vehicle", at, fields.get("photo", "none"), name, what)
    heading = fields.get("heading", "parked")
    if not heading.startswith("towards-"):
        return "exit;"
    # Follow it, wherever it goes: the flight rules decide how far.
    target = heading.removeprefix("towards-")
    if not seen.vetoed(f"fly {target}") and at != WATCH_FROM.get(target):
        return f"write stdout fly {target};"
    watch = WATCH_FROM.get(target, at)
    if at == watch:
        return "exit;"
    return "write stdout zoom vehicle;" if seen.over(watch) else f"write stdout fly {watch};"


def livestock_check(seen: Seen) -> str:
    paddock = seen.payload(3)[0]
    looks = [a for a in seen.answers() if a.startswith(f"zoomed cattle at {paddock}:")]
    if not looks:
        return "write stdout zoom cattle;" if seen.over(paddock) else f"write stdout fly {paddock};"
    fields = _fields(looks[-1])
    head, calves, strays = (int(fields.get(k, "0")) for k in ("head", "calves", "strays"))
    herd = _herd(seen.status("herd.expected"))
    # Calves the farmer has not recorded are new stock; strays here are someone
    # else's, and head missing are still this paddock's.
    expected = herd.get(paddock, head) + calves
    missing = expected - (head - strays)
    given = seen.commands()
    if not any(c.startswith("write farmer kind=livestock ") for c in given):
        news = [
            f"{calves}-new-calves" if calves else "",
            f"{strays}-strays" if strays else "",
            f"{missing}-missing" if missing > 0 else "",
        ]
        return (
            _alert("livestock", paddock, fields.get("photo", "none"), *(n for n in news if n))
            if any(news)
            else _alert("livestock", paddock, fields.get("photo", "none"), "as-expected")
        )
    if calves and not any(c.startswith("write tools ") for c in given):
        herd[paddock] = expected
        return "write tools " + " ".join(f"{p}={n}" for p, n in herd.items()) + ";"
    return "exit;"


def fire_watch(seen: Seen) -> str:
    _, distance, direction = seen.payload(3)
    km = int(distance.removesuffix("km")) if distance.removesuffix("km").isdigit() else 0
    views = [a for a in seen.answers() if a.startswith("panorama at ")]
    if not views:
        if seen.altitude() >= 100:
            return "write stdout panorama;"
        # As high as the distance seems to need; the flight rules have the last word.
        want = max(150, km * 15)
        if not seen.vetoed(f"climb {want}"):
            return f"write stdout climb {want};"
        return f"write stdout climb {CEILING_M};"
    given = seen.commands()
    if not any(c.startswith("write farmer kind=fire ") for c in given):
        fields = _fields(views[-1])
        photo = fields.get("photo", "none")
        smoke = fields.get("smoke")
        if smoke is None:
            return _alert("fire", direction, photo, "no-smoke-in-sight")
        distance, _, toward = smoke.partition("-")
        return _alert("fire", toward or direction, photo, "smoke", distance, toward)
    if seen.altitude() > 40:
        return "write stdout climb 40;"
    return "exit;"


def find_item(seen: Seen) -> str:
    """Search the paddocks nearest first. A search is a look at the paddock too: what it
    takes in is reported as the patrol reports it, before the search goes on."""
    item = seen.payload(1)[0].removeprefix("item=")
    if any(c.startswith("write farmer kind=item ") for c in seen.commands()):
        return "exit;"
    searched: list[str] = []
    for answer in seen.answers():
        looked = re.match(rf"(found|no) {re.escape(item)} at ([\w-]+): ", answer)
        in_sight = _IN_SIGHT.search(answer)
        if looked is None or in_sight is None:
            continue
        paddock = looked[2]
        if (write := _unsaid(seen, _reports(seen, paddock, in_sight))) is not None:
            return write
        if looked[1] == "found":
            photo = re.search(r"photo=(\S+)", answer)
            assert photo is not None  # the drone names the photo of what it found
            return _alert("item", paddock, photo[1], item, "found-here")
        searched.append(paddock)
    remaining = [p for p in SEARCH_ORDER if p not in searched]
    if not remaining:
        return _alert("item", "nowhere", "none", "no", item, "found")
    paddock = remaining[0]
    return f"write stdout search {item};" if seen.over(paddock) else f"write stdout fly {paddock};"


def battery_return(seen: Seen) -> str:
    """Home first: a drone about to lose power does nothing else. Then tell the farmer."""
    if "done return" not in seen.answers():
        return "write stdout return;"
    if not any(c.startswith("write farmer kind=battery ") for c in seen.commands()):
        level = seen.payload(1)[0].removeprefix("battery=")
        return _alert("battery", "shed", "none", f"battery-{level}", "home-at-shed")
    return "exit;"


def report_fire(seen: Seen) -> str:
    if seen.commands():
        return "exit;"
    return f"write tools smoke unknown {seen.argument('direction')};"


def ask_find(seen: Seen) -> str:
    if seen.commands():
        return "exit;"
    return f"write tools item={'-'.join(seen.argument('item').split())};"


POLICIES: dict[str, Callable[[Seen], str]] = {
    "report-fire": report_fire,
    "ask-find": ask_find,
    "patrol": patrol,
    "vehicle-check": vehicle_check,
    "livestock-check": livestock_check,
    "fire-watch": fire_watch,
    "find-item": find_item,
    "battery-return": battery_return,
}


class RulesSource:
    """Answers each behaviour's turn with its policy. Before anything else: a command
    that went to a drone is followed by reading the drone's answer."""

    def next_command(self, turn: Turn) -> str:
        seen = Seen(turn.transcript)
        if seen.awaiting_answer():
            return "read stdin;"
        return POLICIES[turn.descriptor](seen)
