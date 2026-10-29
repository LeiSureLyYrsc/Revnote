"""Folder-tree rules: cycles, subtree moves, non-empty deletes, parent legality (commit 2).

Plan sections 2/6/8:

* ``parent_id`` is an adjacency tree; a move updates **only** the moved root and emits
  **one** ``move`` event -- descendants keep their ``rev``/``updated_seq`` untouched (D3);
* a self-parent or indirect cycle fails with ``FolderCycle`` and writes nothing;
* a non-empty folder cannot be deleted (``FolderNotEmpty``): no cascade, ever (D6);
* a target parent must exist, be live and be in the *same* workspace (``InvalidParent``).

Everything here is single-connection; the "opposite direction moves" race (at most one may
win) is a two-connection case and lives in ``test_concurrency.py`` (commit 3).
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
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# --------------------------------------------------------------------------- #
# cycle prevention
# --------------------------------------------------------------------------- #


async def test_pure_self_parent_is_rejected_structurally(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A folder can never be its own parent; the command layer refuses it first."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Self"))
    before = await kernel_db.quads(workspace.id)

    with expect("InvalidCommand"):
        await write(
            make_command(
                "folder_move",
                workspace.id,
                entity_id=created.entity_id,
                base_rev=created.rev,
                parent_id=created.entity_id,
            )
        )

    assert_quads_unchanged(before, await kernel_db.quads(workspace.id), context="self-parent move")
    assert await kernel_db.folder_parent(workspace.id, created.entity_id) is None


async def test_direct_cycle_parent_into_own_child_is_refused(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Moving a folder under its own child would close a loop: ``FolderCycle``, zero writes."""
    _, workspace = await seed()
    parent = await write(make_command("folder_create", workspace.id, name="Parent"))
    child = await write(
        make_command("folder_create", workspace.id, name="Child", parent_id=parent.entity_id)
    )
    before = await kernel_db.quads(workspace.id)

    with expect("FolderCycle"):
        await write(
            make_command(
                "folder_move",
                workspace.id,
                entity_id=parent.entity_id,
                base_rev=parent.rev,
                parent_id=child.entity_id,
            )
        )

    assert_quads_unchanged(before, await kernel_db.quads(workspace.id), context="direct cycle")
    assert await kernel_db.folder_parent(workspace.id, parent.entity_id) is None, (
        "the refused move must not have re-parented the folder"
    )


async def test_indirect_cycle_is_refused(kernel_db: KernelDB, seed: object, write: object) -> None:
    """A longer ancestor chain (A -> B -> C -> D, move A under D) is refused."""
    _, workspace = await seed()
    a = await write(make_command("folder_create", workspace.id, name="A"))
    b = await write(make_command("folder_create", workspace.id, name="B", parent_id=a.entity_id))
    c = await write(make_command("folder_create", workspace.id, name="C", parent_id=b.entity_id))
    d = await write(make_command("folder_create", workspace.id, name="D", parent_id=c.entity_id))
    before = await kernel_db.quads(workspace.id)

    with expect("FolderCycle"):
        await write(
            make_command(
                "folder_move",
                workspace.id,
                entity_id=a.entity_id,
                base_rev=a.rev,
                parent_id=d.entity_id,
            )
        )

    assert_quads_unchanged(before, await kernel_db.quads(workspace.id), context="indirect cycle")
    assert await kernel_db.folder_parent(workspace.id, a.entity_id) is None
    assert await kernel_db.folder_parent(workspace.id, d.entity_id) == c.entity_id, (
        "no node in the chain may move when the cycle is refused"
    )


async def test_cycle_check_runs_inside_the_workspace_scope(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The ancestor walk must not mistake another workspace's node for an ancestor.

    Two workspaces each own an unrelated tree; the reference implementation must scope its
    parent lookups by ``workspace_id`` (plan section 6 step 4 and the composite FKs).
    """
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    a_root = await write(make_command("folder_create", workspace_a.id, name="A root"))
    b_root = await write(make_command("folder_create", workspace_b.id, name="B root"))

    # Cross-workspace parents are impossible, so this fails as an invalid parent.
    with expect("InvalidParent"):
        await write(
            make_command(
                "folder_create",
                workspace_a.id,
                name="cross",
                parent_id=b_root.entity_id,
            )
        )
    assert await kernel_db.folder_parent(workspace_a.id, a_root.entity_id) is None


async def test_legal_move_after_a_refused_cycle(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A refused move must not disturb the state a later legal move depends on."""
    _, workspace = await seed()
    root = await write(make_command("folder_create", workspace.id, name="Root"))
    child = await write(
        make_command("folder_create", workspace.id, name="Child", parent_id=root.entity_id)
    )
    other = await write(make_command("folder_create", workspace.id, name="Other"))

    with expect("FolderCycle"):
        await write(
            make_command(
                "folder_move",
                workspace.id,
                entity_id=root.entity_id,
                base_rev=root.rev,
                parent_id=child.entity_id,
            )
        )

    moved = await write(
        make_command(
            "folder_move",
            workspace.id,
            entity_id=child.entity_id,
            base_rev=child.rev,
            parent_id=other.entity_id,
        )
    )

    await assert_one_success(kernel_db, moved)
    assert await kernel_db.folder_parent(workspace.id, child.entity_id) == other.entity_id
    assert await kernel_db.folder_parent(workspace.id, root.entity_id) is None


# --------------------------------------------------------------------------- #
# subtree moves: one event, descendants untouched (D3)
# --------------------------------------------------------------------------- #


async def test_subtree_move_emits_one_event_and_leaves_descendants_alone(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Moving a subtree root re-parents only that row; children keep rev/updated_seq."""
    _, workspace = await seed()
    destination = await write(make_command("folder_create", workspace.id, name="Dest"))
    root = await write(make_command("folder_create", workspace.id, name="Root"))
    child = await write(
        make_command("folder_create", workspace.id, name="Child", parent_id=root.entity_id)
    )
    grandchild = await write(
        make_command("folder_create", workspace.id, name="Grand", parent_id=child.entity_id)
    )
    note = await write(
        make_command("note_create", workspace.id, title="t", body_md="b", folder_id=child.entity_id)
    )

    descendants = {
        entity_id: (
            await kernel_db.rev(workspace.id, ENTITY_FOLDER, entity_id),
            await kernel_db.updated_seq(workspace.id, ENTITY_FOLDER, entity_id),
        )
        for entity_id in (child.entity_id, grandchild.entity_id)
    }
    note_before = (
        await kernel_db.rev(workspace.id, "note", note.entity_id),
        await kernel_db.updated_seq(workspace.id, "note", note.entity_id),
        await kernel_db.note_folder(workspace.id, note.entity_id),
    )
    before = await kernel_db.quads(workspace.id)

    moved = await write(
        make_command(
            "folder_move",
            workspace.id,
            entity_id=root.entity_id,
            base_rev=root.rev,
            parent_id=destination.entity_id,
        )
    )

    await assert_one_success(kernel_db, moved, expected_seq=before.last_seq + 1)
    after = await kernel_db.quads(workspace.id)
    assert after.events == before.events + 1, (
        f"a subtree move must add exactly one event, not one per descendant: "
        f"{before.describe()} -> {after.describe()}"
    )
    assert after.event_seqs == (*before.event_seqs, moved.seq)

    assert await kernel_db.folder_parent(workspace.id, root.entity_id) == destination.entity_id
    for entity_id, (rev, updated_seq) in descendants.items():
        assert await kernel_db.rev(workspace.id, ENTITY_FOLDER, entity_id) == rev, (
            f"descendant {entity_id} must keep its revision on a subtree move (D3)"
        )
        assert await kernel_db.updated_seq(workspace.id, ENTITY_FOLDER, entity_id) == updated_seq, (
            f"descendant {entity_id} must keep its updated_seq on a subtree move (D3)"
        )
    assert (
        await kernel_db.rev(workspace.id, "note", note.entity_id),
        await kernel_db.updated_seq(workspace.id, "note", note.entity_id),
        await kernel_db.note_folder(workspace.id, note.entity_id),
    ) == note_before, "notes inside a moved subtree are not re-stamped"


async def test_move_event_describes_the_root_only(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The single event names the moved root, its new rev and the ``move`` op."""
    _, workspace = await seed()
    destination = await write(make_command("folder_create", workspace.id, name="Dest"))
    root = await write(make_command("folder_create", workspace.id, name="Root"))
    await write(make_command("folder_create", workspace.id, name="Child", parent_id=root.entity_id))

    moved = await write(
        make_command(
            "folder_move",
            workspace.id,
            entity_id=root.entity_id,
            base_rev=root.rev,
            parent_id=destination.entity_id,
        )
    )

    event = await kernel_db.event_at(workspace.id, moved.seq)
    assert event is not None
    assert event["entity_id"] == root.entity_id
    assert event["entity_type"] == ENTITY_FOLDER
    assert event["op"] == "move"
    assert int(event["rev"]) == root.rev + 1


async def test_deep_subtree_move_keeps_a_single_event(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A five-level chain moves with one event; every descendant keeps its stamps."""
    _, workspace = await seed()
    top = await write(make_command("folder_create", workspace.id, name="L0"))
    chain = [top]
    for depth in range(1, 5):
        chain.append(
            await write(
                make_command(
                    "folder_create",
                    workspace.id,
                    name=f"L{depth}",
                    parent_id=chain[-1].entity_id,
                )
            )
        )
    stamps = {
        entity.entity_id: (
            await kernel_db.rev(workspace.id, ENTITY_FOLDER, entity.entity_id),
            await kernel_db.updated_seq(workspace.id, ENTITY_FOLDER, entity.entity_id),
        )
        for entity in chain
    }
    destination = await write(make_command("folder_create", workspace.id, name="Destination"))
    before = await kernel_db.quads(workspace.id)

    moved = await write(
        make_command(
            "folder_move",
            workspace.id,
            entity_id=top.entity_id,
            base_rev=top.rev,
            parent_id=destination.entity_id,
        )
    )

    after = await kernel_db.quads(workspace.id)
    assert after.events == before.events + 1, (
        f"one event for the whole subtree move, got {after.events - before.events}"
    )
    assert await kernel_db.rev(workspace.id, ENTITY_FOLDER, top.entity_id) == moved.rev
    for entity in chain[1:]:
        assert stamps[entity.entity_id] == (
            await kernel_db.rev(workspace.id, ENTITY_FOLDER, entity.entity_id),
            await kernel_db.updated_seq(workspace.id, ENTITY_FOLDER, entity.entity_id),
        ), f"descendant {entity.entity_id} was rewritten by the move"


async def test_descendants_remain_reachable_after_the_move(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Moving a root does not orphan its subtree: the CTE from the new parent finds it."""
    _, workspace = await seed()
    destination = await write(make_command("folder_create", workspace.id, name="Dest"))
    root = await write(make_command("folder_create", workspace.id, name="Root"))
    child = await write(
        make_command("folder_create", workspace.id, name="Child", parent_id=root.entity_id)
    )

    await write(
        make_command(
            "folder_move",
            workspace.id,
            entity_id=root.entity_id,
            base_rev=root.rev,
            parent_id=destination.entity_id,
        )
    )

    expected = sorted([root.entity_id, child.entity_id])
    assert await kernel_db.subtree_folders(workspace.id, destination.entity_id) == expected


# --------------------------------------------------------------------------- #
# non-empty folder delete (D6)
# --------------------------------------------------------------------------- #


async def test_non_empty_folder_with_a_child_folder_cannot_be_deleted(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A live child folder blocks the delete; nothing cascades."""
    _, workspace = await seed()
    parent = await write(make_command("folder_create", workspace.id, name="Parent"))
    child = await write(
        make_command("folder_create", workspace.id, name="Child", parent_id=parent.entity_id)
    )
    before = await kernel_db.quads(workspace.id)

    with expect("FolderNotEmpty"):
        await write(
            make_command(
                "folder_delete", workspace.id, entity_id=parent.entity_id, base_rev=parent.rev
            )
        )

    assert_quads_unchanged(before, await kernel_db.quads(workspace.id), context="non-empty folder")
    assert await kernel_db.deleted_at(workspace.id, ENTITY_FOLDER, parent.entity_id) is None
    assert await kernel_db.deleted_at(workspace.id, ENTITY_FOLDER, child.entity_id) is None, (
        "a refused delete must never cascade to the child"
    )


async def test_non_empty_folder_with_a_note_cannot_be_deleted(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A live note inside the folder blocks the delete too."""
    _, workspace = await seed()
    folder = await write(make_command("folder_create", workspace.id, name="HasNote"))
    note = await write(
        make_command(
            "note_create", workspace.id, title="t", body_md="b", folder_id=folder.entity_id
        )
    )

    with expect("FolderNotEmpty"):
        await write(
            make_command(
                "folder_delete", workspace.id, entity_id=folder.entity_id, base_rev=folder.rev
            )
        )

    assert await kernel_db.deleted_at(workspace.id, ENTITY_NOTE, note.entity_id) is None
    assert await kernel_db.deleted_at(workspace.id, ENTITY_FOLDER, folder.entity_id) is None


async def test_folder_becomes_deletable_once_children_are_deleted(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Only *live* children block: tombstoning them allows the parent to go."""
    _, workspace = await seed()
    parent = await write(make_command("folder_create", workspace.id, name="Parent"))
    child = await write(
        make_command("folder_create", workspace.id, name="Child", parent_id=parent.entity_id)
    )
    note = await write(
        make_command(
            "note_create", workspace.id, title="t", body_md="b", folder_id=parent.entity_id
        )
    )

    with expect("FolderNotEmpty"):
        await write(
            make_command(
                "folder_delete", workspace.id, entity_id=parent.entity_id, base_rev=parent.rev
            )
        )

    await write(
        make_command("folder_delete", workspace.id, entity_id=child.entity_id, base_rev=child.rev)
    )
    await write(
        make_command("note_delete", workspace.id, entity_id=note.entity_id, base_rev=note.rev)
    )

    deleted = await write(
        make_command("folder_delete", workspace.id, entity_id=parent.entity_id, base_rev=parent.rev)
    )
    await assert_one_success(kernel_db, deleted)
    assert await kernel_db.deleted_at(workspace.id, ENTITY_FOLDER, parent.entity_id) is not None


async def test_moving_the_last_child_out_makes_the_folder_empty(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Emptying a folder by moving its child out is enough to allow the delete."""
    _, workspace = await seed()
    parent = await write(make_command("folder_create", workspace.id, name="Parent"))
    child = await write(
        make_command("folder_create", workspace.id, name="Child", parent_id=parent.entity_id)
    )

    with expect("FolderNotEmpty"):
        await write(
            make_command(
                "folder_delete", workspace.id, entity_id=parent.entity_id, base_rev=parent.rev
            )
        )

    await write(
        make_command(
            "folder_move",
            workspace.id,
            entity_id=child.entity_id,
            base_rev=child.rev,
            parent_id=None,
        )
    )

    deleted = await write(
        make_command("folder_delete", workspace.id, entity_id=parent.entity_id, base_rev=parent.rev)
    )
    await assert_one_success(kernel_db, deleted)


# --------------------------------------------------------------------------- #
# invalid parents
# --------------------------------------------------------------------------- #


async def test_parent_must_exist(kernel_db: KernelDB, seed: object, write: object) -> None:
    """A create pointing at a missing folder is ``InvalidParent``, not a raw FK error."""
    _, workspace = await seed()
    before = await kernel_db.quads(workspace.id)
    ghost = uuid.uuid4()

    with expect("InvalidParent"):
        await write(make_command("folder_create", workspace.id, name="orphan", parent_id=ghost))

    assert_quads_unchanged(before, await kernel_db.quads(workspace.id), context="missing parent")


async def test_parent_in_another_workspace_is_invalid(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Cross-workspace parents are rejected by the service *and* the composite FK."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    foreign = await write(make_command("folder_create", workspace_b.id, name="Foreign"))
    before = await kernel_db.quads(workspace_a.id)

    with expect("InvalidParent"):
        await write(
            make_command("folder_create", workspace_a.id, name="mine", parent_id=foreign.entity_id)
        )

    assert_quads_unchanged(before, await kernel_db.quads(workspace_a.id), context="cross-ws parent")
    row = await kernel_db.entity_row(workspace_b.id, ENTITY_FOLDER, foreign.entity_id)
    assert int(row["rev"]) == 1, "the foreign parent must be untouched"

    # Re-parenting into another workspace is refused the same way.
    own = await write(make_command("folder_create", workspace_a.id, name="Own"))
    with expect("InvalidParent"):
        await write(
            make_command(
                "folder_move",
                workspace_a.id,
                entity_id=own.entity_id,
                base_rev=own.rev,
                parent_id=foreign.entity_id,
            )
        )
    assert await kernel_db.folder_parent(workspace_a.id, own.entity_id) is None


async def test_deleted_parent_cannot_receive_a_child(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A tombstoned folder is not a legal target for create or move."""
    _, workspace = await seed()
    doomed = await write(make_command("folder_create", workspace.id, name="Doomed"))
    await write(
        make_command("folder_delete", workspace.id, entity_id=doomed.entity_id, base_rev=doomed.rev)
    )
    survivor = await write(make_command("folder_create", workspace.id, name="Survivor"))
    before = await kernel_db.quads(workspace.id)

    with expect("InvalidParent"):
        await write(
            make_command("folder_create", workspace.id, name="child", parent_id=doomed.entity_id)
        )
    with expect("InvalidParent"):
        await write(
            make_command(
                "folder_move",
                workspace.id,
                entity_id=survivor.entity_id,
                base_rev=survivor.rev,
                parent_id=doomed.entity_id,
            )
        )

    assert_quads_unchanged(before, await kernel_db.quads(workspace.id), context="deleted parent")
    assert await kernel_db.folder_parent(workspace.id, survivor.entity_id) is None


async def test_note_cannot_be_filed_into_a_deleted_folder(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The note parent reference obeys the same liveness rule."""
    _, workspace = await seed()
    folder = await write(make_command("folder_create", workspace.id, name="Gone"))
    note = await write(make_command("note_create", workspace.id, title="t", body_md="b"))
    await write(
        make_command("folder_delete", workspace.id, entity_id=folder.entity_id, base_rev=folder.rev)
    )

    with expect("InvalidParent"):
        await write(
            make_command(
                "note_move",
                workspace.id,
                entity_id=note.entity_id,
                base_rev=note.rev,
                folder_id=folder.entity_id,
            )
        )

    assert await kernel_db.note_folder(workspace.id, note.entity_id) is None


async def test_note_parent_must_exist(kernel_db: KernelDB, seed: object, write: object) -> None:
    """Filing a note into a non-existent folder is ``InvalidParent``."""
    _, workspace = await seed()

    with expect("InvalidParent"):
        await write(
            make_command(
                "note_create", workspace.id, title="t", body_md="b", folder_id=uuid.uuid4()
            )
        )

    assert (await kernel_db.quads(workspace.id)).events == 0


async def test_updating_a_deleted_entity_is_entity_deleted(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """After the tombstone, a *new* op on the object is ``EntityDeleted``, not ``NotFound``."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Bye"))
    deleted = await write(
        make_command(
            "folder_delete", workspace.id, entity_id=created.entity_id, base_rev=created.rev
        )
    )
    before = await kernel_db.quads(workspace.id)

    for command in (
        make_command(
            "folder_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=deleted.rev,
            name="again",
        ),
        make_command(
            "folder_move",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=deleted.rev,
            parent_id=None,
        ),
        make_command(
            "folder_delete", workspace.id, entity_id=created.entity_id, base_rev=deleted.rev
        ),
    ):
        with expect("EntityDeleted"):
            await write(command)

    assert_quads_unchanged(
        before, await kernel_db.quads(workspace.id), context="new op after delete"
    )
    row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, created.entity_id)
    assert int(row["rev"]) == deleted.rev, "no new op may touch a tombstone"


async def test_deleted_note_rejects_new_operations(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Same for notes: update / move / delete after the tombstone are all ``EntityDeleted``."""
    _, workspace = await seed()
    folder = await write(make_command("folder_create", workspace.id, name="F"))
    created = await write(make_command("note_create", workspace.id, title="t", body_md="b"))
    deleted = await write(
        make_command("note_delete", workspace.id, entity_id=created.entity_id, base_rev=created.rev)
    )

    for command in (
        make_command(
            "note_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=deleted.rev,
            title="x",
            body_md="x",
        ),
        make_command(
            "note_move",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=deleted.rev,
            folder_id=folder.entity_id,
        ),
        make_command(
            "note_delete", workspace.id, entity_id=created.entity_id, base_rev=deleted.rev
        ),
    ):
        with expect("EntityDeleted"):
            await write(command)

    assert await kernel_db.note_body(workspace.id, created.entity_id) == "b"
