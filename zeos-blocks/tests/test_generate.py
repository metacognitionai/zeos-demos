"""A generated workspace is the same behaviour at a different size.

The claim the generator exists to support is that the *behaviour* does not change with the
size of the table and only the configuration does. That is worth a test rather than a
sentence in the README, because it is the sort of claim that quietly stops being true.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import CASE
from zeos.descriptor.lint import Severity, lint
from zeos.descriptor.loader import load_case

from zeos_blocks.abi import BLOCKS
from zeos_blocks.generate import layout_for, write_case
from zeos_blocks.planner import StubPlanner
from zeos_blocks.session import Session
from zeos_blocks.world import EMPTY, Table


def body_of(descriptor: Path) -> str:
    return descriptor.read_text(encoding="utf-8").split("---\n", 2)[2]


def test_the_descriptor_body_does_not_change_with_the_size_of_the_table(tmp_path) -> None:
    write_case(tmp_path, positions=5, blocks=12)
    assert body_of(tmp_path / "goals" / "stacker.md") == body_of(CASE / "goals" / "stacker.md")


def test_a_generated_case_lints_clean(tmp_path) -> None:
    write_case(tmp_path, positions=6, blocks=20)
    bundle = load_case(tmp_path)
    findings = lint(
        bundle.descriptors,
        pipes=bundle.pipes,
        scripts=bundle.scripts,
        vectors=bundle.vectors,
        resources=bundle.resources,
        platforms=bundle.platforms,
        principals=bundle.principals,
        gates=bundle.gates,
        abi=BLOCKS,
    )
    assert [f.render() for f in findings if f.severity is Severity.ERROR] == []


def test_a_generated_case_runs(tmp_path) -> None:
    write_case(tmp_path, positions=5, blocks=12)
    bundle = load_case(tmp_path)
    session = Session(bundle, StubPlanner())
    session.say("move the blue block from stack 3 to stack 1")
    for _ in range(300):
        session.step()
    table = Table.of(session.positions())
    assert table.position_of("b1") == "s1"


def test_one_position_is_always_left_free(tmp_path) -> None:
    """A table with nowhere to put anything is one on which almost nothing can be done."""
    for positions, blocks in ((3, 4), (5, 12), (6, 20)):
        layout = layout_for(positions, blocks)
        assert EMPTY in layout.values()


def test_a_table_too_small_to_clear_anything_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="at least two positions"):
        write_case(tmp_path, positions=1, blocks=2)
