"""Compare-and-set semantics of ``base_rev`` (commit 2).

Plan sections 5/6/8: every non-create command carries the revision it replaces; the
object update is a conditional statement on ``(workspace_id, id, rev, deleted_at IS
NULL)`` and its rowcount is checked. A stale ``base_rev`` is ``RevConflict`` carrying
``expected``/``current``, and it must leave *nothing* behind -- no object change, no
event, no ledger row, no cursor movement.

Single-connection scope: the simultaneous-CAS race (exactly one winner) is proven in
``test_concurrency.py`` (commit 3, ``concurrency`` marker).
"""

from __future__ import annotations

import pytest
from kernel_support import (
    ENTITY_FOLDER,
    ENTITY_NOTE,
    KernelDB,
    Quads,
    assert_quads_unchanged,
    expect,
    make_command,
    new_op_id,
    request_hash,
    variant,
)

from revnote_api.kernel.errors import RevConflict

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


async def test_stale_base_rev_is_a_rev_conflict(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Replaying an update with the *previous* revision loses, and says by how much."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="v1"))
    applied = await write(
        make_command(
            "folder_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            name="v2",
        )
    )
    stale = make_command(
        "folder_update",
        workspace.id,
        entity_id=created.entity_id,
        base_rev=created.rev,  # still 1 while the row is at 2
        name="v3",
    )

    with pytest.raises(RevConflict) as caught:
        await write(stale)

    error = caught.value
    assert error.expected == created.rev, (
        f"RevConflict.expected must be the command's base_rev: {error!r}"
    )
    assert error.current == applied.rev, f"RevConflict.current must be the live revision: {error!r}"
    assert error.entity_id == created.entity_id, "the conflict must name the object it lost against"


async def test_rev_conflict_leaves_no_trace(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The whole quadruple is untouched: this is the plan's "CAS failure => zero writes"."""
    _, workspace = await seed()
    created = await write(make_command("note_create", workspace.id, title="t", body_md="b"))
    before = await kernel_db.quads(workspace.id)
    losing = make_command(
        "note_update",
        workspace.id,
        entity_id=created.entity_id,
        base_rev=created.rev + 10,
        title="nope",
        body_md="nope",
    )

    with expect("RevConflict"):
        await write(losing)

    after: Quads = await kernel_db.quads(workspace.id)
    assert_quads_unchanged(before, after, context="RevConflict on a note update")
    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, created.entity_id)
    assert row["title"] == "t"
    assert row["body_md"] == "b"
    assert int(row["rev"]) == 1
    assert int(row["updated_seq"]) == created.seq
    assert await kernel_db.ledger_for_op(workspace.id, losing.op_id) is None
    assert await kernel_db.events_for_op(workspace.id, losing.op_id) == []


async def test_base_rev_must_be_one_higher_each_time(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The only accepted base is the revision the object currently holds."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="a"))
    current = created

    for name in ("b", "c", "d"):
        nxt = await write(
            make_command(
                "folder_update",
                workspace.id,
                entity_id=current.entity_id,
                base_rev=current.rev,
                name=name,
            )
        )
        assert nxt.rev == current.rev + 1
        current = nxt
        # Every older revision is now rejected.
        for older in range(1, current.rev):
            with expect("RevConflict"):
                await write(
                    make_command(
                        "folder_update",
                        workspace.id,
                        entity_id=current.entity_id,
                        base_rev=older,
                        name=f"stale-{older}",
                    )
                )
    assert current.rev == 4
    quads = await kernel_db.quads(workspace.id)
    assert quads.last_seq == 4, "only accepted writes may consume a seq"


async def test_move_with_stale_base_rev_fails(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A move is CAS-guarded too, and a rejected move changes no parent."""
    _, workspace = await seed()
    source = await write(make_command("folder_create", workspace.id, name="Source"))
    target = await write(make_command("folder_create", workspace.id, name="Target"))
    moving = await write(make_command("folder_create", workspace.id, name="Moving"))
    await write(
        make_command(
            "folder_update",
            workspace.id,
            entity_id=moving.entity_id,
            base_rev=moving.rev,
            name="Renamed",
        )
    )

    with expect("RevConflict"):
        await write(
            make_command(
                "folder_move",
                workspace.id,
                entity_id=moving.entity_id,
                base_rev=moving.rev,  # the rename advanced the rev past this
                parent_id=target.entity_id,
            )
        )

    assert await kernel_db.folder_parent(workspace.id, moving.entity_id) is None, (
        "the rejected move must not re-parent anything"
    )
    assert await kernel_db.folder_parent(workspace.id, source.entity_id) is None, (
        "no other folder may be touched by the rejected move"
    )
    assert await kernel_db.count_events(workspace.id) == 4


async def test_note_move_with_stale_base_rev_fails(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The note move path enforces the same predicate."""
    _, workspace = await seed()
    left = await write(make_command("folder_create", workspace.id, name="Left"))
    right = await write(make_command("folder_create", workspace.id, name="Right"))
    note = await write(
        make_command("note_create", workspace.id, title="t", body_md="b", folder_id=left.entity_id)
    )
    await write(
        make_command(
            "note_update",
            workspace.id,
            entity_id=note.entity_id,
            base_rev=note.rev,
            title="t2",
            body_md="b2",
        )
    )

    with expect("RevConflict"):
        await write(
            make_command(
                "note_move",
                workspace.id,
                entity_id=note.entity_id,
                base_rev=note.rev,
                folder_id=right.entity_id,
            )
        )

    assert await kernel_db.note_folder(workspace.id, note.entity_id) == left.entity_id
    assert await kernel_db.note_body(workspace.id, note.entity_id) == "b2"


async def test_delete_with_stale_base_rev_fails(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A delete that lost the race is rejected and the object stays active."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Live"))
    await write(
        make_command(
            "folder_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            name="StillLive",
        )
    )

    with expect("RevConflict"):
        await write(
            make_command(
                "folder_delete",
                workspace.id,
                entity_id=created.entity_id,
                base_rev=created.rev,
            )
        )

    assert await kernel_db.deleted_at(workspace.id, ENTITY_FOLDER, created.entity_id) is None, (
        "a rejected delete must not tombstone anything"
    )


async def test_zero_and_negative_base_rev_are_invalid_commands(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """``base_rev`` must be a positive integer (structural check, before any lock)."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="X"))
    before = await kernel_db.quads(workspace.id)

    for bad in (0, -1):
        with expect("InvalidCommand"):
            await write(
                make_command(
                    "folder_update",
                    workspace.id,
                    entity_id=created.entity_id,
                    base_rev=bad,
                    name=f"n{bad}",
                )
            )

    assert_quads_unchanged(before, await kernel_db.quads(workspace.id), context="invalid base_rev")


async def test_create_must_not_carry_base_rev(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Creates have no revision to replace; passing one is a structural error."""
    _, workspace = await seed()

    with expect("InvalidCommand"):
        await write(make_command("folder_create", workspace.id, name="X", base_rev=1))

    assert (await kernel_db.quads(workspace.id)).events == 0


async def test_conflicting_write_uses_no_seq_but_the_next_one_does(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Seq allocation happens after the CAS predicate is known to hold (§6 ordering)."""
    _, workspace = await seed()
    created = await write(make_command("note_create", workspace.id, title="t", body_md="b"))

    with expect("RevConflict"):
        await write(
            make_command(
                "note_update",
                workspace.id,
                entity_id=created.entity_id,
                base_rev=77,
                title="x",
                body_md="x",
            )
        )

    applied = await write(
        make_command(
            "note_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            title="y",
            body_md="y",
        )
    )
    assert applied.seq == 2, f"the rejected write must not consume seq 2: {applied.seq}"
    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == (1, 2)
    assert quads.ledger == 2


async def test_missing_entity_is_not_found_before_any_seq_work(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A non-existent object reports ``EntityNotFound`` with zero writes."""
    import uuid as uuid_module

    _, workspace = await seed()
    before = await kernel_db.quads(workspace.id)
    missing = uuid_module.uuid4()

    with expect("EntityNotFound"):
        await write(
            make_command(
                "folder_update",
                workspace.id,
                entity_id=missing,
                base_rev=1,
                name="ghost",
            )
        )

    assert_quads_unchanged(before, await kernel_db.quads(workspace.id), context="EntityNotFound")
    assert await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, missing) is None


async def test_a_rejected_command_leaves_no_ledger_row(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The ledger records successes only: a rejected op_id is still free for its real op.

    Plan section 4/5: no pending rows, no failure ledger.
    """
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Base"))
    rejected = make_command(
        "folder_update",
        workspace.id,
        entity_id=created.entity_id,
        base_rev=99,
        name="Loser",
    )

    with expect("RevConflict"):
        await write(rejected)

    assert await kernel_db.ledger_for_op(workspace.id, rejected.op_id) is None, (
        "op_ledger must not contain a row for a command that was rejected"
    )
    assert await kernel_db.events_for_op(workspace.id, rejected.op_id) == [], (
        "change_events must not contain a row for a rejected command"
    )

    # The exact same rejected command can never succeed (it is stale), but its op_id is
    # not reserved: a corrected command with a fresh op_id is accepted.
    accepted = await write(
        make_command(
            "folder_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            name="Winner",
            op_id=new_op_id(),
        )
    )
    assert accepted.seq == 2


async def test_hash_is_stable_for_the_same_command_object(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The replay decision depends on a deterministic hash of the same value object."""
    _, workspace = await seed()
    command = make_command("note_create", workspace.id, title="t", body_md="b")

    assert request_hash(command) == request_hash(variant(command))
    assert request_hash(command) != request_hash(variant(command, body_md="changed"))
    assert len(request_hash(command)) == 32

    applied = await write(command)
    assert await write(command) == applied, (
        "identical hash under the same op_id must replay rather than conflict"
    )
