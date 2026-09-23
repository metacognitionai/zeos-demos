"""A case directory for any number of positions and blocks.

The behaviour does not change with the size of the workspace and the configuration does.
That split is what lets one descriptor body run a table of four blocks or forty, and this
is the half that scales: the pipes, the schema, the opening layout, and the one block of
frontmatter that has to name every position.

The checked-in `cases/blocks-3x4/` is this generator's output for the smallest interesting
workspace, commented afterwards by hand. A reader of the tutorial reads that file rather
than a build artefact; anyone wanting a bigger table runs `zeos-blocks new`.
"""

from __future__ import annotations

from pathlib import Path

from zeos_blocks.world import COLOURS, EMPTY, position_names

__all__ = ["layout_for", "write_case"]

_STACKER_HEAD = """---
name: stacker
priority: 50
on_fault: retry
budget:
  tokens: {budget}
reads:
  - table.*
  - arm.last
writes: []
pipes:
  stdout: operator.replies
  arm:    arm.requests
capabilities:
  - pipe: arm.requests
    min_integrity: 2
    schema: move
  - pipe: operator.replies
    min_integrity: 2
maps:
{maps}  - {{object: arm.last, mode: ro, region: status}}
utterances:
  - "{{instruction}}"
context:
  window: {window}
---
"""


_TIDY_HEAD = """---
name: tidy
# The highest number in the tree, so the lowest urgency. This is the whole of what makes
# it background work: it runs when nothing else is runnable, and gives up the machine at
# the next token boundary when anything else becomes runnable. No thread pool, no queue,
# and no decision made anywhere at run time.
priority: 80
on_fault: retry
budget:
  # Room for a long consolidation and no more. A standing instruction that cannot make
  # progress should stop and say so rather than shuffle the table indefinitely.
  tokens: {budget}
reads:
  - table.*
  - arm.last
writes: []
pipes:
  # The same arm the stacker writes. There is one arm on this table, so two jobs wanting
  # it are two jobs contending for one device -- which the scheduler settles by priority,
  # and which is the whole reason this descriptor is worth reading.
  stdout: operator.replies
  arm:    arm.requests
capabilities:
  # The same two the stacker holds, and the same schema. Being the background job buys no
  # extra authority and costs none: what a job may do is declared, not inherited from its
  # priority.
  - pipe: arm.requests
    min_integrity: 2
    schema: move
  - pipe: operator.replies
    min_integrity: 2
maps:
{maps}  - {{object: arm.last, mode: ro, region: status}}
# No `utterances:`, and that is this descriptor's point rather than an omission. It is a
# standing instruction, not a service: it boots, and the page's Tidy button asks the
# kernel for it by name. Nothing anybody can type compiles to it, so nothing anybody can
# type can redirect it.
context:
  window: {window}
---
"""


def layout_for(positions: int, blocks: int) -> dict[str, str]:
    """Deal ``blocks`` over ``positions``, leaving the last position empty.

    One position is always left free, because a table with nowhere to put anything is a
    table on which almost nothing can be done: clearing a block needs somewhere for the
    block on top of it to go.
    """
    names = position_names(positions)
    letters = list(COLOURS)
    stacks: dict[str, list[str]] = {name: [] for name in names}
    counts = dict.fromkeys(letters, 0)
    for index in range(blocks):
        letter = letters[index % len(letters)]
        counts[letter] += 1
        stacks[names[index % max(1, positions - 1)]].append(f"{letter}{counts[letter]}")
    return {name: ",".join(stack) or EMPTY for name, stack in stacks.items()}


def write_case(root: Path, *, positions: int, blocks: int, tidy: bool = False) -> Path:
    """Write a whole case. Returns the directory.

    ``tidy`` adds the background goal and boots it. It is off by default because a table
    that rearranges itself before anybody has typed anything is not the workspace the
    introduction describes.
    """
    if positions < 2:
        raise ValueError("a table needs at least two positions, or nothing can be cleared")
    if blocks < 1:
        raise ValueError("a table needs at least one block")
    if blocks > len(COLOURS) * 9:
        raise ValueError(f"at most {len(COLOURS) * 9} blocks are nameable")

    names = position_names(positions)
    layout = layout_for(positions, blocks)
    every_block = sorted(b for stack in layout.values() for b in stack.split(",") if b != EMPTY)

    (root / "goals").mkdir(parents=True, exist_ok=True)
    (root / "system").mkdir(parents=True, exist_ok=True)

    maps = "".join(f"  - {{object: table.{n}, mode: ro, region: status}}\n" for n in names)
    head = _STACKER_HEAD.format(
        budget=512 + 256 * blocks,
        maps=maps,
        # The body, the status regions and room to work. Regions are pinned and the body
        # is immutable, so this is what the pager may never reclaim plus what it may.
        window=2048 + 64 * positions,
    )
    body = (Path(__file__).resolve().parent / "stacker_body.md").read_text(encoding="utf-8")
    (root / "goals" / "stacker.md").write_text(head + body, encoding="utf-8")

    pipes = [
        "# Generated. One actuator per position, plus the operator's door and the arm.",
        "- name: operator.console",
        "  ring: TRUSTED",
        "  principal: user",
        "  device: true",
        "  utterance_source: operator",
        "  reply_to: operator.replies",
        "  capacity: 256",
        "",
        "- name: operator.replies",
        "  ring: TRUSTED",
        "  principal: user",
        "  sink: true",
        "  capacity: 2048",
        "",
        "# One arm, however many jobs want it. Nothing on this pipe is addressed to",
        "# anybody, so there is nothing to mis-route: a device that answered would need a",
        "# pipe per job, or two jobs would be handed each other's answers.",
        "- name: arm.requests",
        "  ring: TRUSTED",
        "  principal: device",
        "  sink: true",
        "  capacity: 32",
        "",
        "# What the arm said about the last move. Latched, so it is the current value and",
        "# not a log, and mapped by every planner as a status region: a refused move leaves",
        "# the table untouched, and this is the only thing that says so.",
        "- name: arm.report",
        "  ring: TRUSTED",
        "  principal: device",
        "  device: true",
        "  world_object: arm.last",
        "  capacity: 64",
        "",
        "# The doorbell a hand rings; the vector on it preempts whatever is planning.",
        "- name: table.disturbed",
        "  ring: TRUSTED",
        "  principal: user",
        "  device: true",
        "  capacity: 32",
        "",
    ]
    for name in names:
        pipes += [
            f"- name: table.report.{name}",
            "  ring: TRUSTED",
            "  principal: device",
            "  device: true",
            f"  world_object: table.{name}",
            "  capacity: 64",
            "",
        ]
    (root / "system" / "pipes.yaml").write_text("\n".join(pipes), encoding="utf-8")

    (root / "system" / "schemas.yaml").write_text(
        "# Generated. The whole channel from the model to the table.\n"
        "move:\n"
        f"  block: enum({', '.join(every_block)})\n"
        f"  to: enum({', '.join(names)})\n",
        encoding="utf-8",
    )
    (root / "system" / "world-state.yaml").write_text(
        "# Generated. The opening workspace, bottom-to-top.\ninitial:\n"
        + "".join(f'  table.{n}: "{layout[n]}"\n' for n in names)
        + '  arm.last: "nothing asked for yet"\n',
        encoding="utf-8",
    )
    # The operator holds what the job their words compile to needs, and nothing else. The
    # background goal needs no entry of its own: it writes the same arm, and the Tidy
    # button spawns it as the operator, so it runs inside this envelope rather than beside
    # it.
    (root / "system" / "principals.yaml").write_text(
        "# Generated. Who may ask for what.\n"
        "- id: operator\n"
        "  label: the person at the table\n"
        "  ring: TRUSTED\n"
        "  integrity: 2\n"
        "  ceiling: 50\n"
        "  capabilities:\n"
        "    - arm.requests\n"
        "    - operator.replies\n",
        encoding="utf-8",
    )
    (root / "handlers").mkdir(parents=True, exist_ok=True)
    (root / "handlers" / "noticed.md").write_text(
        (Path(__file__).resolve().parent / "noticed.md").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "system" / "vectors.yaml").write_text(
        "# Generated. Somebody reaching in preempts the planner.\n"
        "- vector: hand-in-workspace\n"
        "  source: table.disturbed\n"
        "  handler: noticed\n"
        "  priority: 10\n"
        "  policy: queue\n"
        "  deadline: 200ms\n",
        encoding="utf-8",
    )
    if tidy:
        (root / "goals" / "tidy.md").write_text(
            _TIDY_HEAD.format(budget=1024 + 256 * blocks, maps=maps, window=2048 + 64 * positions)
            + (Path(__file__).resolve().parent / "tidy_body.md").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
    (root / "system" / "boot.yaml").write_text(
        (
            "# One job boots, and it is the one nobody asked for. `tidy` is a standing\n"
            "# instruction rather than a request, so it is running before anybody speaks.\n"
            "# The stacker is still not booted: it exists only when somebody does, and when\n"
            "# it does it outranks this and takes the machine.\n- tidy\n"
        )
        if tidy
        else "# Nothing boots: the stacker exists only because somebody asked for it.\n[]\n",
        encoding="utf-8",
    )
    return root
