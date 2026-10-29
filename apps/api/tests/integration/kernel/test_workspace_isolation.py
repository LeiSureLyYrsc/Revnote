"""Workspace scoping and isolation (commit 2, single connection).

Plan section 8, rows "ws isolation" and "workspace no global lock": a command only ever
sees its own workspace, and the real composite foreign keys make a cross-workspace
reference impossible even through raw SQL. Isolation is **not** authorization (there is
no auth in this phase) -- it is a data-scoping guarantee.

All cases here are single-connection. The two live-lock cases that need a second backend
(the "A is locked, B still completes" proof and the three-connection visibility matrix)
are marked ``concurrency`` and live in ``test_concurrency.py`` / ``test_atomicity.py``
(commit 3) so this file stays runnable with ``-m "integration and not concurrency"``.
"""

from __future__ import annotations

import uuid

import pytest
from kernel_support import (
    ENTITY_FOLDER,
    ENTITY_NOTE,
    KernelDB,
    assert_one_success,
    assert_quads_unchanged,
    expect,
    make_command,
    sqlstate,
)
from sqlalchemy.exc import IntegrityError

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# --------------------------------------------------------------------------- #
# the service only sees its own workspace
# --------------------------------------------------------------------------- #


async def test_a_command_cannot_touch_another_workspaces_entity(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A's command naming B's entity is ``EntityNotFound``: reads are workspace-scoped."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    foreign = await write(make_command("folder_create", workspace_b.id, name="B only"))
    before_a = await kernel_db.quads(workspace_a.id)
    before_b = await kernel_db.quads(workspace_b.id)

    with expect("EntityNotFound"):
        await write(
            make_command(
                "folder_update",
                workspace_a.id,
                entity_id=foreign.entity_id,
                base_rev=foreign.rev,
                name="hijacked",
            )
        )

    assert_quads_unchanged(before_a, await kernel_db.quads(workspace_a.id), context="ws A")
    assert_quads_unchanged(before_b, await kernel_db.quads(workspace_b.id), context="ws B")
    row = await kernel_db.entity_row(workspace_b.id, ENTITY_FOLDER, foreign.entity_id)
    assert row["name"] == "B only", "B's row must be untouched by A's command"
    assert int(row["rev"]) == 1


async def test_a_delete_cannot_reach_into_another_workspace(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Deleting "through" another workspace leaks nothing: ``EntityNotFound``, no tombstone."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    foreign = await write(make_command("note_create", workspace_b.id, title="t", body_md="b"))

    with expect("EntityNotFound"):
        await write(
            make_command(
                "note_delete",
                workspace_a.id,
                entity_id=foreign.entity_id,
                base_rev=foreign.rev,
            )
        )

    assert await kernel_db.deleted_at(workspace_b.id, ENTITY_NOTE, foreign.entity_id) is None


async def test_namespaced_cursors_do_not_interfere(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Each workspace owns its own ``last_seq``: A's writes never advance B's cursor."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()

    for index in range(3):
        await write(make_command("folder_create", workspace_a.id, name=f"a{index}"))
    await write(make_command("folder_create", workspace_b.id, name="b0"))

    quads_a = await kernel_db.quads(workspace_a.id)
    quads_b = await kernel_db.quads(workspace_b.id)
    assert quads_a.event_seqs == (1, 2, 3), f"A must own 1..3: {quads_a.describe()}"
    assert quads_b.event_seqs == (1,), f"B must start at 1 independently: {quads_b.describe()}"
    assert quads_a.last_seq == 3
    assert quads_b.last_seq == 1


async def test_same_op_id_in_two_workspaces_is_independent(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The ledger key is ``(workspace_id, op_id)``: the same op_id may exist in both."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    shared_op = uuid.uuid4()

    first = await write(make_command("folder_create", workspace_a.id, name="A", op_id=shared_op))
    second = await write(make_command("folder_create", workspace_b.id, name="B", op_id=shared_op))

    assert first.op_id == second.op_id == shared_op
    assert first.seq == 1 and second.seq == 1, (
        "both workspaces must allocate seq 1 for their own first op"
    )
    assert first.entity_id != second.entity_id
    ledger_a = await kernel_db.ledger_for_op(workspace_a.id, shared_op)
    ledger_b = await kernel_db.ledger_for_op(workspace_b.id, shared_op)
    assert ledger_a is not None and ledger_b is not None
    assert int(ledger_a["result_seq"]) == 1
    assert int(ledger_b["result_seq"]) == 1


async def test_replay_is_per_workspace(kernel_db: KernelDB, seed: object, write: object) -> None:
    """An op recorded in A does not replay in B: B applies it as a fresh command."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    shared_op = uuid.uuid4()

    in_a = await write(
        make_command("folder_create", workspace_a.id, name="Shared", op_id=shared_op)
    )
    in_b = await write(
        make_command("folder_create", workspace_b.id, name="Shared", op_id=shared_op)
    )

    assert in_a.seq == in_b.seq == 1, "each workspace starts its own cursor at 1"
    assert in_a.entity_id != in_b.entity_id
    assert await kernel_db.count_ledger(workspace_a.id) == 1
    assert await kernel_db.count_ledger(workspace_b.id) == 1
    assert await kernel_db.rev(workspace_a.id, ENTITY_FOLDER, in_a.entity_id) == 1
    assert await kernel_db.rev(workspace_b.id, ENTITY_FOLDER, in_b.entity_id) == 1


async def test_unknown_workspace_is_reported(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A command whose workspace does not exist is ``WorkspaceNotFound`` (plan §6 step 2)."""
    ghost = uuid.uuid4()

    with expect("WorkspaceNotFound"):
        await write(make_command("folder_create", ghost, name="nowhere"))


async def test_unknown_workspace_writes_nothing_anywhere(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The failed workspace lookup must not leave an event or ledger row in any workspace."""
    _, workspace = await seed()
    before = await kernel_db.quads(workspace.id)

    with expect("WorkspaceNotFound"):
        await write(make_command("note_create", uuid.uuid4(), title="t", body_md="b"))

    assert_quads_unchanged(before, await kernel_db.quads(workspace.id), context="bad workspace")


# --------------------------------------------------------------------------- #
# the database itself enforces the scoping (composite FKs)
# --------------------------------------------------------------------------- #


async def test_composite_fk_blocks_a_cross_workspace_parent_by_raw_sql(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Even bypassing the service, ``(workspace_id, parent_id)`` cannot cross workspaces."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    foreign = await write(make_command("folder_create", workspace_b.id, name="B parent"))
    anchor = await write(make_command("folder_create", workspace_a.id, name="A anchor"))

    with pytest.raises(IntegrityError) as caught:
        await kernel_db.execute(
            "INSERT INTO folders "
            "(id, workspace_id, parent_id, name, rev, updated_seq, updated_at) "
            "VALUES (:id, :ws, :parent, 'cross', 1, :seq, now())",
            id=uuid.uuid4(),
            ws=workspace_a.id,
            parent=foreign.entity_id,
            seq=anchor.seq,
        )

        assert sqlstate(caught.value) == "23503", (
            f"a cross-workspace parent must violate the composite FK: {caught.value}"
        )
    assert await kernel_db.folder_parent(workspace_a.id, anchor.entity_id) is None


async def test_composite_fk_blocks_a_cross_workspace_note_folder(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """``notes.(workspace_id, folder_id)`` is composite for the same reason."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    foreign = await write(make_command("folder_create", workspace_b.id, name="B folder"))
    anchor = await write(make_command("note_create", workspace_a.id, title="t", body_md="b"))

    with pytest.raises(IntegrityError) as caught:
        await kernel_db.execute(
            "INSERT INTO notes "
            "(id, workspace_id, folder_id, title, body_md, rev, updated_seq, updated_at) "
            "VALUES (:id, :ws, :folder, '', '', 1, :seq, now())",
            id=uuid.uuid4(),
            ws=workspace_a.id,
            folder=foreign.entity_id,
            seq=anchor.seq,
        )

    assert sqlstate(caught.value) == "23503", (
        f"cross-workspace note folder must fail the composite FK: {caught.value}"
    )


async def test_update_cannot_cross_workspaces_by_raw_sql(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A raw UPDATE scoped to A cannot see B's row: the predicate includes workspace_id."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    foreign = await write(make_command("folder_create", workspace_b.id, name="B row"))

    affected = await kernel_db.execute(
        "UPDATE folders SET name = 'hijacked' WHERE workspace_id = :ws AND id = :id",
        ws=workspace_a.id,
        id=foreign.entity_id,
    )

    assert affected == 0, (
        f"the workspace-scoped UPDATE must match no rows in another workspace, got {affected}"
    )
    row = await kernel_db.entity_row(workspace_b.id, ENTITY_FOLDER, foreign.entity_id)
    assert row["name"] == "B row"


async def test_events_and_ledger_are_workspace_scoped(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Both append-only tables carry ``workspace_id`` in their keys, so scans stay scoped."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    await write(make_command("folder_create", workspace_a.id, name="A"))
    await write(make_command("folder_create", workspace_b.id, name="B"))

    rows_a = await kernel_db.events(workspace_a.id)
    rows_b = await kernel_db.events(workspace_b.id)
    assert [(row["workspace_id"] == workspace_a.id) for row in rows_a] == [True], (
        f"A's scan must return only A's rows: {rows_a}"
    )
    assert [(row["workspace_id"] == workspace_b.id) for row in rows_b] == [True], (
        f"B's scan must return only B's rows: {rows_b}"
    )
    assert [int(row["seq"]) for row in rows_a] == [1]
    assert [int(row["seq"]) for row in rows_b] == [1], (
        "identical seq values in two workspaces are legal and expected"
    )

    ledger_a = await kernel_db.ledger_rows(workspace_a.id)
    ledger_b = await kernel_db.ledger_rows(workspace_b.id)
    assert [(row["workspace_id"] == workspace_a.id) for row in ledger_a] == [True]
    assert [(row["workspace_id"] == workspace_b.id) for row in ledger_b] == [True]
    assert len(await kernel_db.rows("SELECT workspace_id, op_id FROM op_ledger")) >= 2


async def test_a_failed_command_in_one_workspace_leaves_the_other_dense(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Rollbacks are per workspace: B's numbering is unaffected by A's rejection."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    a_first = await write(make_command("folder_create", workspace_a.id, name="A1"))
    b_first = await write(make_command("folder_create", workspace_b.id, name="B1"))

    with expect("RevConflict"):
        await write(
            make_command(
                "folder_update",
                workspace_a.id,
                entity_id=a_first.entity_id,
                base_rev=a_first.rev + 5,
                name="nope",
            )
        )

    b_second = await write(make_command("folder_create", workspace_b.id, name="B2"))
    assert b_second.seq == b_first.seq + 1 == 2
    assert (await kernel_db.quads(workspace_b.id)).event_seqs == (1, 2)
    assert (await kernel_db.quads(workspace_a.id)).event_seqs == (1,)


async def test_min_retained_seq_is_untouched_by_writes(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """D7: ``min_retained_seq`` is a placeholder that Phase 2 never advances."""
    _, workspace = await seed()
    assert await kernel_db.min_retained_seq(workspace.id) == 1

    for index in range(3):
        await write(make_command("folder_create", workspace.id, name=f"f{index}"))

    assert await kernel_db.min_retained_seq(workspace.id) == 1, (
        "no retention window is implemented in this phase; the floor must stay at 1"
    )


async def test_many_workspaces_stay_independent(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Several workspaces in one session: each keeps its own dense 1..N chain."""
    seeded = [await seed() for _ in range(4)]
    for index, (_, workspace) in enumerate(seeded):
        for step in range(index + 1):
            await write(make_command("folder_create", workspace.id, name=f"w{index}-{step}"))

    for index, (_, workspace) in enumerate(seeded):
        quads = await kernel_db.quads(workspace.id)
        expected = tuple(range(1, index + 2))
        assert quads.event_seqs == expected, (
            f"workspace {index} must own {expected}, got {quads.describe()}"
        )
        assert quads.last_seq == index + 1
        assert quads.ledger == index + 1


async def test_entity_id_reuse_across_workspaces_is_a_conflict(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Ids are globally unique (folders PK is ``id``): reuse in another ws is a conflict.

    Plan section 6: a create reusing an existing entity id is ``EntityIdConflict``; ids
    are never recycled, not even across workspaces, because the primary key is global.
    """
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    created = await write(make_command("folder_create", workspace_a.id, name="Global"))
    before = await kernel_db.quads(workspace_b.id)

    with expect("EntityIdConflict"):
        await write(
            make_command(
                "folder_create", workspace_b.id, name="Stolen", entity_id=created.entity_id
            )
        )

    assert_quads_unchanged(before, await kernel_db.quads(workspace_b.id), context="ws B conflict")
    assert (
        await kernel_db.entity_row_or_none(workspace_b.id, ENTITY_FOLDER, created.entity_id) is None
    ), "the conflicting create must not create a B-side row"
    row = await kernel_db.entity_row(workspace_a.id, ENTITY_FOLDER, created.entity_id)
    assert row["name"] == "Global"

    await assert_one_success(kernel_db, created, expected_seq=1)
