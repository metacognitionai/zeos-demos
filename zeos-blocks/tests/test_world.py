"""The rules of the table, on their own.

No kernel here, which is the point: whether a block is clear is a fact about a table, and
the kernel knows nothing about it.
"""

from __future__ import annotations

import pytest

from zeos_blocks.planner import next_command
from zeos_blocks.world import EMPTY, Move, Refused, Table, colour_of


def table() -> Table:
    return Table.of({"s1": "g1,r1,y1", "s2": "b1", "s3": EMPTY})


def test_an_empty_marker_is_not_a_block() -> None:
    assert table().stacks["s3"] == []
    assert Table.of({"s1": "(unset)"}).stacks["s1"] == []


def test_only_the_top_block_is_clear() -> None:
    t = table()
    assert t.is_clear("y1")
    assert not t.is_clear("r1")
    assert not t.is_clear("g1")


def test_a_move_reports_the_positions_it_changed() -> None:
    t = table()
    assert t.apply(Move("y1", "s3")) == ("s1", "s3")
    assert t.layout() == {"s1": "g1,r1", "s2": "b1", "s3": "y1"}


def test_a_buried_block_will_not_move() -> None:
    with pytest.raises(Refused, match="g1 is not clear"):
        table().apply(Move("g1", "s2"))


def test_a_move_to_nowhere_is_refused() -> None:
    with pytest.raises(Refused, match="no position s4"):
        table().apply(Move("y1", "s4"))


def test_a_move_to_where_it_already_is_is_refused() -> None:
    """Otherwise a run could mark progress it did not make."""
    with pytest.raises(Refused, match="already on s1"):
        table().apply(Move("y1", "s1"))


def test_a_refused_move_changes_nothing() -> None:
    t = table()
    before = t.layout()
    for move in (Move("g1", "s2"), Move("y1", "s4"), Move("y1", "s1")):
        with pytest.raises(Refused):
            t.apply(move)
    assert t.layout() == before


def test_a_colour_names_the_topmost_block_of_that_colour_on_that_stack() -> None:
    """Colours need only be unique within a stack, which is what lets the table grow."""
    t = Table.of({"s1": "r1,g1,r2", "s2": "b1"})
    assert t.resolve("red", "s1") == "r2"
    assert t.resolve("green", "s1") == "g1"
    assert t.resolve("red", "s2") is None


def test_a_block_carries_its_colour_in_its_name() -> None:
    assert colour_of("g1") == "green"
    assert colour_of("r12") == "red"


def test_an_instruction_names_the_topmost_block_of_that_colour() -> None:
    """Not the lowest-numbered one, which is what the naming invites.

    `g1,g2` bottom-to-top means `g2` is on top, so *the green block* is `g2`. Reading the
    number as an order rather than an identity gets this backwards, and the model reads the
    same prose a person does, so the rule is stated in the descriptor body as well.
    """
    for stack, wanted in (
        ("g1,g2", "g2"),
        ("g1,r1,g2", "g2"),
        ("g1,g2,g3", "g3"),
    ):
        table = Table.of({"s1": stack, "s2": "b1", "s3": EMPTY})
        assert table.resolve("green", "s1") == wanted, stack
        assert next_command(table, "green", "s2", "s1") == f"write arm block={wanted} to=s2;"


def test_a_buried_topmost_is_still_the_one_meant() -> None:
    """The block named is the topmost *of that colour*, not the topmost of the stack, so
    anything above it has to be cleared before it can move."""
    table = Table.of({"s1": "g1,g2,r1", "s2": "b1", "s3": EMPTY})
    assert table.resolve("green", "s1") == "g2"
    assert next_command(table, "green", "s2", "s1") == "write arm block=r1 to=s3;"
