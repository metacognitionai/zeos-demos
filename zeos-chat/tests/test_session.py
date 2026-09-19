"""The loop a conversation runs in, and the two things it must get right.

Both were found by running the thing rather than by reading it, which is why they have
tests now: a delivery must be seen at the clock the loop has advanced to, and it must
not touch the kernel until the stepping thread says so.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from zeos.core.events import PipeWritten, VectorFired
from zeos.core.ids import PipeName

from zeos_chat.session import Session

CASE = Path(__file__).resolve().parents[1] / "cases" / "chat"
MESSAGES = PipeName("user.messages")
ARRIVALS = PipeName("user.arrivals")
REPLIES = PipeName("user.replies")

#: Far enough ahead that a stale clock is unmistakable.
LATER_NS = 1_000_000_000


@pytest.fixture
def session(make_session) -> Session:
    s, _, _ = make_session()
    s.boot()
    return s


def test_a_delivery_is_seen_at_the_clock_the_loop_advanced_to(session: Session) -> None:
    """The regression this exists for.

    Delivering before advancing left the kernel reading the previous tick, so a vector's
    throttle measured the gap between two messages as far shorter than it was, and a
    message comfortably outside `min_interval` was deferred. Nothing in this case declares
    one any more -- for reasons of its own -- but the loop must still be honest about
    when something arrived, because every other time-keyed decision reads the same clock.
    """
    session.skip_to(LATER_NS)
    session.deliver(ARRIVALS, "message")
    session.step()

    fired = [e for e in session.events if isinstance(e, VectorFired)]
    assert [e.clock.virtual_ns for e in fired] == [LATER_NS]


def test_a_delivery_does_not_reach_the_kernel_until_the_next_step(session: Session) -> None:
    """``deliver`` is the one method safe from another thread, and it is safe because it
    only puts on a queue. If it touched the kernel, an HTTP handler would be re-entering
    it mid-decode."""
    before = len(session.events)
    session.deliver(MESSAGES, "hello")
    assert len(session.events) == before, "the kernel moved without being stepped"
    assert session.kernel.pipes.get(MESSAGES).available == 0

    session.step()
    assert session.kernel.pipes.get(MESSAGES).available > 0


def test_replies_arrive_through_the_callback(make_session, run) -> None:
    """What the web application will hand to the browser: a drained sink, as text."""
    replies: list[tuple[PipeName, str]] = []
    s, adapter, _ = make_session(on_reply=lambda p, t: replies.append((p, t)))
    s.boot()
    s.deliver(MESSAGES, "when is the train")
    s.deliver(ARRIVALS, "message")
    run(s, adapter)

    assert replies, "nothing ever reached the person"
    assert {p for p, _ in replies} == {REPLIES}


def test_events_are_reported_once_and_in_order(make_session) -> None:
    """The panel in the browser folds this stream, so a repeat would draw a line twice."""
    seen: list[str] = []
    s, _, _ = make_session(on_event=lambda new: seen.extend(type(e).__name__ for e in new))
    s.boot()
    for _ in range(200):
        s.step()

    assert seen == [type(e).__name__ for e in s.events]


def test_a_delivery_too_large_for_its_pipe_is_refused_whole(session: Session) -> None:
    """All or nothing, as a job's write is. The loop records it rather than raising into
    whatever thread happened to call ``deliver``."""
    session.deliver(PipeName("user.cancel"), " ".join(["stop"] * 500))
    session.step()

    assert [p for p, _ in session.refused] == [PipeName("user.cancel")]
    assert not [e for e in session.events if isinstance(e, PipeWritten) and e.pipe == "user.cancel"]


def test_the_clock_only_moves_forward(session: Session) -> None:
    session.skip_to(LATER_NS)
    session.skip_to(0)
    assert session.now_ns == LATER_NS


def _replies_of(events: Sequence[object]) -> list[str]:
    return [" ".join(e.text) for e in events if isinstance(e, PipeWritten) and e.pipe == REPLIES]
