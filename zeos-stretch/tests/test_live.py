"""The live page's side of the atrium: what a click becomes, stepped by hand.

No thread and no socket: the atrium is built and stepped here directly, so a click is
checked by what it put into the journal.
"""

from __future__ import annotations

from zeos.core.events import JobSpawned, PipeWritten
from zeos.core.ids import ObjectName
from zeos.descriptor.loader import load_case
from zeos.machine.seat import TapeSource

from zeos_stretch.live import Live
from zeos_stretch.run import CASE


def interactive() -> Live:
    live = Live(source=lambda: TapeSource(load_case(CASE).scripts), speed=1000.0)
    live.start()
    return live


def written(live: Live, pipe: str) -> list[str]:
    return [
        " ".join(e.text) for e in live.atrium.sink if isinstance(e, PipeWritten) and e.pipe == pipe
    ]


def test_an_order_is_an_interrupt_on_the_coffee_pipe() -> None:
    live = interactive()
    assert live.ask("order", "desk-3") is None
    live.once()
    assert written(live, "requests.coffee") == ["desk-3"]
    assert any(line.kind == "you" for line in live.atrium.lines)


def test_the_alarm_is_an_interrupt_on_the_smoke_sensor() -> None:
    live = interactive()
    assert live.ask("alarm") is None
    live.once()
    assert written(live, "sensors.smoke") == ["smoke near the hotplate"]


def test_a_desk_that_does_not_exist_is_refused() -> None:
    live = interactive()
    assert live.ask("order", "kitchen") is not None
    assert live.ask("order", "desk-12") is not None


def test_laying_the_table_again_starts_the_tidying_again() -> None:
    live = interactive()
    for _ in range(20000):
        live.once()
        if not live.atrium.live("tidy-atrium"):
            break
    assert live.atrium.kernel.world.get(ObjectName("table.items")) == "nothing"
    assert live.ask("mess") is None
    live.once()
    assert live.atrium.kernel.world.get(ObjectName("table.items")).startswith("apple")
    tidies = [
        e for e in live.atrium.sink if isinstance(e, JobSpawned) and e.descriptor == "tidy-atrium"
    ]
    assert len(tidies) == 2


def test_playback_takes_no_orders() -> None:
    live = Live(source=None, playback=True)
    live.start()
    assert live.ask("order", "desk-7") is not None
    assert live.ask("alarm") is not None
    assert live.ask("restart") is None


def test_the_snapshot_says_what_the_robot_is_doing() -> None:
    live = interactive()
    # The seat speaks one word per step, so the command closes a few steps in.
    for _ in range(50):
        live.once()
        if live.atrium.body.current is not None:
            break
    snap = live.snapshot()
    assert snap["robot"]["doing"]["says"] == "Putting the apple in the fruit basket."
    assert snap["thinking"] is None
    assert [b["name"] for b in snap["behaviours"]][0] == "smoke-response"
