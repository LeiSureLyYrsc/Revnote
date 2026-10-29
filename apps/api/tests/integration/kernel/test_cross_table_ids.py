"""A note and a folder may share a UUID: cross-table equality is not a cycle.

Plan D3 constrains the **folder** adjacency tree. A note's ``folder_id`` points into
``folders`` while the note's own ``id`` points into ``notes``, so two equal UUIDs are two
unrelated rows in two unrelated tables. Treating that equality as a self-parent would reject
a perfectly legal command, so these cases pin the distinction in both directions:

* a note whose ``entity_id`` equals the target ``folder_id`` must be movable/fileable;
* a folder with ``parent_id == id`` must still be refused (the real self-cycle);
* a note pointing at a *nonexistent* folder of its own UUID is ``InvalidParent``, not
  ``FolderCycle`` -- i.e. the note path performs no ancestor walk at all;
* the two rows really are independent: mutating one leaves the other's ``rev`` alone.
"""

from __future__ import annotations

import uuid

import pytest
from kernel_support import (
    ENTITY_FOLDER,
    ENTITY_NOTE,
    KernelDB,
    assert_one_success,
    expect,
    make_command,
    new_entity_id,
    new_op_id,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_note_can_share_a_uuid_with_the_folder_it_lives_in(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """``note.id == folder.id`` across tables is legal and the move must succeed.

    The same UUID is used for a folder and then for a note, and the note is moved into that
    folder. Nothing about that is a tree cycle: ``folders.parent_id`` and ``notes.folder_id``
    are different columns in different tables.
    """
    _, workspace = await seed()
    shared = new_entity_id()

    folder = await write(
        make_command("folder_create", workspace.id, name="Holder", entity_id=shared)
    )
    note = await write(
        make_command(
            "note_create",
            workspace.id,
            title="t",
            body_md="b",
            entity_id=shared,  # == folder_id, but a different table
            op_id=new_op_id(),
        )
    )
    assert folder.seq == 1 and note.seq == 2, (
        f"setup expects seq 1 then 2, got {folder.seq}/{note.seq}"
    )

    folder_row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, shared)
    note_row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, shared)
    assert folder_row["id"] == note_row["id"], "the setup needs one UUID in both tables"
    assert folder_row["parent_id"] is None
    assert note_row["folder_id"] is None

    moved = await write(
        make_command(
            "note_move",
            workspace.id,
            entity_id=shared,
            base_rev=note.rev,
            folder_id=shared,
            op_id=new_op_id(),
        )
    )

    await assert_one_success(kernel_db, moved, expected_seq=note.seq + 1)
    assert moved.op == "move"
    assert moved.rev == note.rev + 1
    after = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, shared)
    assert after["folder_id"] == shared, (
        "the note must be filed into the folder that happens to share its UUID"
    )
    assert int(after["rev"]) == moved.rev

    # The folder is untouched: equal ids are not a shared row.
    folder_after = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, shared)
    assert int(folder_after["rev"]) == int(folder_row["rev"]), (
        "moving the note must not change the folder's revision"
    )
    assert folder_after["parent_id"] is None
    assert int(folder_after["updated_seq"]) == int(folder_row["updated_seq"])


async def test_note_can_be_created_directly_into_a_folder_with_its_own_uuid(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The same equality is legal on the create path, not just on move."""
    _, workspace = await seed()
    shared = new_entity_id()
    await write(make_command("folder_create", workspace.id, name="Holder", entity_id=shared))

    note = await write(
        make_command(
            "note_create",
            workspace.id,
            title="t",
            body_md="b",
            entity_id=shared,
            folder_id=shared,
            op_id=new_op_id(),
        )
    )

    await assert_one_success(kernel_db, note, expected_seq=2)
    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, shared)
    assert row["folder_id"] == shared
    assert int(row["rev"]) == 1


async def test_a_real_folder_self_parent_is_still_refused(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The folder case is the opposite rule: ``parent_id == id`` is never allowed.

    Kept next to the note case so the distinction cannot be "fixed" by deleting the folder
    check: the command layer rejects it before any transaction opens.
    """
    _, workspace = await seed()
    folder = await write(make_command("folder_create", workspace.id, name="Self"))

    with expect("InvalidCommand"):
        await write(
            make_command(
                "folder_move",
                workspace.id,
                entity_id=folder.entity_id,
                base_rev=folder.rev,
                parent_id=folder.entity_id,
            )
        )

    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == (folder.seq,), (
        f"a refused self-parent move must write nothing: {quads.describe()}"
    )
    assert await kernel_db.folder_parent(workspace.id, folder.entity_id) is None


async def test_note_pointing_at_a_missing_folder_of_its_own_uuid_is_invalid_parent(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """With no such folder, the note path answers ``InvalidParent`` -- never ``FolderCycle``.

    This is the observable difference between "the note's parent reference is missing" and
    "the folder would become its own ancestor": the note path must not consult the folder
    ancestry at all.
    """
    _, workspace = await seed()
    lonely = new_entity_id()
    note = await write(
        make_command("note_create", workspace.id, title="t", body_md="b", entity_id=lonely)
    )
    assert await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, lonely) is None, (
        "the setup requires no folder with this UUID"
    )

    with expect("InvalidParent"):
        await write(
            make_command(
                "note_move",
                workspace.id,
                entity_id=lonely,
                base_rev=note.rev,
                folder_id=lonely,
                op_id=new_op_id(),
            )
        )

    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, lonely)
    assert row["folder_id"] is None, "a rejected move must not file the note"
    assert int(row["rev"]) == 1
    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == (note.seq,), f"no write may follow the rejection: {quads.describe()}"


async def test_note_and_folder_sharing_a_uuid_keep_independent_revisions(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Every operation on one must leave the other's ``rev``/``updated_seq`` untouched."""
    _, workspace = await seed()
    shared = new_entity_id()
    folder = await write(make_command("folder_create", workspace.id, name="F", entity_id=shared))
    note = await write(
        make_command(
            "note_create",
            workspace.id,
            title="t",
            body_md="b",
            entity_id=shared,
            op_id=new_op_id(),
        )
    )
    folder_stamp = (
        int((await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, shared))["rev"]),
        int((await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, shared))["updated_seq"]),
    )

    # Two writes on the note (move in, rename content), then one on the folder.
    moved = await write(
        make_command(
            "note_move",
            workspace.id,
            entity_id=shared,
            base_rev=note.rev,
            folder_id=shared,
            op_id=new_op_id(),
        )
    )
    updated = await write(
        make_command(
            "note_update",
            workspace.id,
            entity_id=shared,
            base_rev=moved.rev,
            title="t2",
            body_md="b2",
            op_id=new_op_id(),
        )
    )
    renamed = await write(
        make_command(
            "folder_update",
            workspace.id,
            entity_id=shared,
            base_rev=folder.rev,
            name="F2",
            op_id=new_op_id(),
        )
    )

    assert (moved.rev, updated.rev) == (2, 3)
    assert renamed.rev == folder.rev + 1

    note_row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, shared)
    folder_row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, shared)
    assert int(note_row["rev"]) == 3, f"the note's rev chain is its own: {note_row['rev']}"
    assert (int(folder_row["rev"]), int(folder_row["updated_seq"])) == (
        folder.rev + 1,
        renamed.seq,
    ), "the folder keeps its own rev/updated_seq after the note writes"
    assert (int(folder_row["rev"]), int(folder_row["updated_seq"])) != folder_stamp
    assert note_row["title"] == "t2"
    assert folder_row["name"] == "F2"

    quads = await kernel_db.quads(workspace.id)
    expected = [folder.seq, note.seq, moved.seq, updated.seq, renamed.seq]
    assert list(quads.event_seqs) == sorted(expected), (
        f"all five writes share the workspace cursor: {quads.describe()}"
    )
    assert quads.ledger == len(expected)
    assert quads.last_seq == max(expected)


async def test_note_under_a_folder_with_the_same_uuid_is_not_a_cycle_at_depth(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A deeper tree containing the shared UUID stays legal and consistent."""
    _, workspace = await seed()
    shared = new_entity_id()

    root = await write(make_command("folder_create", workspace.id, name="root"))
    child = await write(
        make_command("folder_create", workspace.id, name="child", parent_id=root.entity_id)
    )
    holder = await write(
        make_command("folder_create", workspace.id, name="holder", entity_id=shared)
    )
    note = await write(
        make_command(
            "note_create",
            workspace.id,
            title="t",
            body_md="b",
            entity_id=shared,
            folder_id=root.entity_id,
            op_id=new_op_id(),
        )
    )

    # Move the note from the root into the shared-UUID folder: no cycle is possible.
    moved = await write(
        make_command(
            "note_move",
            workspace.id,
            entity_id=shared,
            base_rev=note.rev,
            folder_id=shared,
            op_id=new_op_id(),
        )
    )
    await assert_one_success(kernel_db, moved)

    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, shared)
    assert row["folder_id"] == holder.entity_id
    assert await kernel_db.folder_parent(workspace.id, holder.entity_id) is None, (
        "the holder folder must not gain a parent because a note points at it"
    )
    assert await kernel_db.folder_parent(workspace.id, child.entity_id) == root.entity_id
    assert await kernel_db.live_children(workspace.id, holder.entity_id) == 1, (
        "the moved note counts as the holder folder's live child"
    )


async def test_folder_cycle_check_is_unaffected_by_a_same_uuid_note(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A same-UUID note must not mask a genuine folder cycle.

    The folder ancestry walk reads ``folders`` only, so an unrelated note sharing a UUID
    cannot satisfy or break it. A real self-parent still fails, and a real indirect cycle
    still fails.
    """
    _, workspace = await seed()
    shared = new_entity_id()
    parent = await write(
        make_command("folder_create", workspace.id, name="parent", entity_id=shared)
    )
    await write(
        make_command(
            "note_create",
            workspace.id,
            title="t",
            body_md="b",
            entity_id=shared,
            op_id=new_op_id(),
        )
    )
    child = await write(
        make_command("folder_create", workspace.id, name="child", parent_id=parent.entity_id)
    )

    with expect("FolderCycle"):
        await write(
            make_command(
                "folder_move",
                workspace.id,
                entity_id=parent.entity_id,
                base_rev=parent.rev,
                parent_id=child.entity_id,
                op_id=new_op_id(),
            )
        )

    assert await kernel_db.folder_parent(workspace.id, parent.entity_id) is None
    assert await kernel_db.folder_parent(workspace.id, child.entity_id) == parent.entity_id


async def test_note_move_between_two_folders_sharing_uuids_with_notes(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Two folders, each also used as a note id: both moves stay legal and independent."""
    _, workspace = await seed()
    left_id, right_id = new_entity_id(), new_entity_id()
    left = await write(make_command("folder_create", workspace.id, name="left", entity_id=left_id))
    right = await write(
        make_command("folder_create", workspace.id, name="right", entity_id=right_id)
    )
    # A note reusing the left folder's UUID, filed under the right folder.
    shared_note = await write(
        make_command(
            "note_create",
            workspace.id,
            title="t",
            body_md="b",
            entity_id=left_id,
            folder_id=right.entity_id,
            op_id=new_op_id(),
        )
    )
    note_row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, left_id)
    assert note_row["folder_id"] == right.entity_id

    moved = await write(
        make_command(
            "note_move",
            workspace.id,
            entity_id=left_id,
            base_rev=shared_note.rev,
            folder_id=left.entity_id,  # its own UUID, a folder that exists
            op_id=new_op_id(),
        )
    )
    await assert_one_success(kernel_db, moved)

    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, left_id)
    assert row["folder_id"] == left.entity_id
    assert await kernel_db.live_children(workspace.id, left.entity_id) == 1
    assert await kernel_db.live_children(workspace.id, right.entity_id) == 0
    assert int((await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, left_id))["rev"]) == 1
    assert int((await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, right_id))["rev"]) == 1


async def test_uuid_collision_across_tables_is_not_an_id_conflict(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """``EntityIdConflict`` is per table: a note may reuse a folder's UUID (and vice versa).

    The primary keys are ``folders.id`` and ``notes.id`` separately, so the "ids are never
    reused" rule applies within a table. A cross-table duplicate is a normal, supported
    shape -- which is exactly why the tree rules must not assume a shared id space.
    """
    _, workspace = await seed()
    shared = new_entity_id()

    folder = await write(make_command("folder_create", workspace.id, name="f", entity_id=shared))
    note = await write(
        make_command(
            "note_create",
            workspace.id,
            title="t",
            body_md="b",
            entity_id=shared,
            op_id=new_op_id(),
        )
    )
    assert folder.entity_id == note.entity_id == shared

    # Reusing the UUID *within* one table is still the documented conflict.
    with expect("EntityIdConflict"):
        await write(
            make_command(
                "folder_create",
                workspace.id,
                name="again",
                entity_id=shared,
                op_id=new_op_id(),
            )
        )
    with expect("EntityIdConflict"):
        await write(
            make_command(
                "note_create",
                workspace.id,
                title="again",
                body_md="again",
                entity_id=shared,
                op_id=new_op_id(),
            )
        )

    assert int((await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, shared))["rev"]) == 1
    assert int((await kernel_db.entity_row(workspace.id, ENTITY_NOTE, shared))["rev"]) == 1
    assert (await kernel_db.quads(workspace.id)).event_seqs == (1, 2)


async def test_random_uuid_equality_is_always_treated_as_two_rows(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Repeating the shape over several shared UUIDs: never a cycle, always two rows."""
    _, workspace = await seed()
    for index in range(4):
        shared = new_entity_id()
        folder = await write(
            make_command("folder_create", workspace.id, name=f"f{index}", entity_id=shared)
        )
        note = await write(
            make_command(
                "note_create",
                workspace.id,
                title=f"t{index}",
                body_md="b",
                entity_id=shared,
                op_id=new_op_id(),
            )
        )
        moved = await write(
            make_command(
                "note_move",
                workspace.id,
                entity_id=shared,
                base_rev=note.rev,
                folder_id=shared,
                op_id=new_op_id(),
            )
        )
        assert moved.rev == 2, f"iteration {index}: {moved!r}"
        assert await kernel_db.note_folder(workspace.id, shared) == shared
        assert await kernel_db.folder_parent(workspace.id, folder.entity_id) is None

    assert (await kernel_db.quads(workspace.id)).last_seq == 12


async def test_note_cannot_be_its_own_parent_only_within_its_own_table(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A note id equal to a note's own ``folder_id`` still needs a *folder* at that UUID.

    With such a folder absent the command is ``InvalidParent``: the note path never treats
    ``entity_id`` as an ancestor, so there is no "self-parent" error for notes at all.
    """
    _, workspace = await seed()
    lonely = new_entity_id()
    note = await write(
        make_command("note_create", workspace.id, title="t", body_md="b", entity_id=lonely)
    )

    with expect("InvalidParent"):
        await write(
            make_command(
                "note_move",
                workspace.id,
                entity_id=lonely,
                base_rev=note.rev,
                folder_id=lonely,
                op_id=new_op_id(),
            )
        )

    # And with a folder present it is legal -- the same command shape, opposite outcome.
    await write(make_command("folder_create", workspace.id, name="now exists", entity_id=lonely))
    moved = await write(
        make_command(
            "note_move",
            workspace.id,
            entity_id=lonely,
            base_rev=note.rev,
            folder_id=lonely,
            op_id=uuid.uuid4(),
        )
    )
    await assert_one_success(kernel_db, moved)
    assert await kernel_db.note_folder(workspace.id, lonely) == lonely
