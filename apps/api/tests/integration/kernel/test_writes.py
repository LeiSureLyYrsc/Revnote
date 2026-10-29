"""Single-connection behaviour of the eight writes (commit 2).

Plan section 8, row "create/update/move/delete": every accepted command returns an
immutable ``WriteResult`` whose ``seq``/``rev``/object stamp/event/ledger all agree, and
the workspace cursor advances by exactly one.

One test class per operation family so a failure names the operation, plus the payload
cases the plan calls out explicitly: empty title/body legal, whitespace preserved
verbatim, no-op updates still succeed and bump ``rev``/``seq``, ``rev`` starts at 1.

These are all single-connection cases: no second writer, no barrier. The multi-connection
proofs live in ``test_concurrency.py``/``test_atomicity.py`` (commit 3).
"""

from __future__ import annotations

import uuid

import pytest
from kernel_support import (
    ENTITY_FOLDER,
    ENTITY_NOTE,
    OP_CREATE,
    OP_DELETE,
    OP_MOVE,
    OP_UPDATE,
    KernelDB,
    Quads,
    assert_one_success,
    assert_quads_unchanged,
    describe_command,
    expect,
    make_command,
    with_op_id,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# --------------------------------------------------------------------------- #
# folder create
# --------------------------------------------------------------------------- #


async def test_create_folder_writes_rev1_and_one_event(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A folder create is one seq, one rev-1 object row, one event, one ledger row."""
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="Inbox")

    result = await write(command)

    event = await assert_one_success(kernel_db, result, expected_seq=1)
    assert result.op == OP_CREATE
    assert result.entity_type == ENTITY_FOLDER
    assert result.entity_id == command.entity_id
    assert result.workspace_id == workspace.id
    assert result.op_id == command.op_id
    assert result.rev == 1, f"a create must land at rev 1, got {result.rev}"
    assert event["op"] == OP_CREATE
    assert event["entity_id"] == command.entity_id

    row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, command.entity_id)
    assert row["name"] == "Inbox"
    assert row["parent_id"] is None, "a create without parent_id must be a root folder"
    assert row["deleted_at"] is None
    assert int(row["rev"]) == 1

    quads = await kernel_db.quads(workspace.id)
    assert quads.last_seq == 1
    assert quads.events == 1
    assert quads.ledger == 1


async def test_create_folder_reports_its_own_created_at(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """``created_at`` is the event's own timestamp, not "now" from the caller side."""
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="Stamped")

    result = await write(command)

    event = await kernel_db.event_at(workspace.id, result.seq)
    assert event is not None
    assert event["created_at"] == result.created_at, (
        f"result.created_at={result.created_at!r} must be the event timestamp "
        f"{event['created_at']!r}"
    )


async def test_create_folder_inside_folder_records_parent(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A nested create stores the given parent and still allocates one seq."""
    _, workspace = await seed()
    parent = await write(make_command("folder_create", workspace.id, name="Parent"))

    child = await write(
        make_command("folder_create", workspace.id, name="Child", parent_id=parent.entity_id)
    )

    assert child.seq == parent.seq + 1
    row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, child.entity_id)
    assert row["parent_id"] == parent.entity_id
    assert int(row["rev"]) == 1
    last = await kernel_db.last_seq(workspace.id)
    assert last == 2


async def test_sibling_folders_may_share_a_name(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The plan allows duplicate names: naming is not an identity."""
    _, workspace = await seed()
    parent = await write(make_command("folder_create", workspace.id, name="Shared"))

    first = await write(
        make_command("folder_create", workspace.id, name="Same", parent_id=parent.entity_id)
    )
    second = await write(
        make_command("folder_create", workspace.id, name="Same", parent_id=parent.entity_id)
    )

    assert first.entity_id != second.entity_id
    assert (first.seq, second.seq) == (parent.seq + 1, parent.seq + 2)
    quads = await kernel_db.quads(workspace.id)
    assert quads.events == 3
    assert quads.last_seq == 3


# --------------------------------------------------------------------------- #
# note create
# --------------------------------------------------------------------------- #


async def test_create_note_keeps_body_verbatim(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """``body_md`` is stored exactly as sent: no trim, no newline or Unicode folding."""
    _, workspace = await seed()
    body = "  # Title\r\n\r\nline with trailing spaces   \n\u00e9\u0301 composed\n  "
    command = make_command("note_create", workspace.id, title=" Notes ", body_md=body)

    result = await write(command)

    await assert_one_success(kernel_db, result, expected_seq=1)
    assert result.entity_type == ENTITY_NOTE
    assert result.rev == 1
    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, command.entity_id)
    assert row["body_md"] == body, "the body must round-trip byte for byte"
    assert row["title"] == " Notes ", "titles are not trimmed either"
    assert row["folder_id"] is None, "a note without folder_id must be unfiled"
    assert row["deleted_at"] is None


async def test_create_note_allows_empty_title_and_body(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Empty strings are legal content (NOT NULL is not NOT-EMPTY)."""
    _, workspace = await seed()
    command = make_command("note_create", workspace.id, title="", body_md="")

    result = await write(command)

    await assert_one_success(kernel_db, result, expected_seq=1)
    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, command.entity_id)
    assert row["title"] == ""
    assert row["body_md"] == ""


async def test_create_note_filed_in_folder(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A note created inside a folder stores that folder and takes the next seq."""
    _, workspace = await seed()
    folder = await write(make_command("folder_create", workspace.id, name="Dest"))

    note = await write(
        make_command(
            "note_create", workspace.id, title="t", body_md="b", folder_id=folder.entity_id
        )
    )

    assert note.seq == folder.seq + 1
    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, note.entity_id)
    assert row["folder_id"] == folder.entity_id


# --------------------------------------------------------------------------- #
# updates
# --------------------------------------------------------------------------- #


async def test_update_folder_name_bumps_rev_and_seq(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A rename is ``rev + 1`` and a new seq; the previous rev is the CAS base."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Before"))

    renamed = await write(
        make_command(
            "folder_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            name="After",
        )
    )

    await assert_one_success(kernel_db, renamed, expected_seq=created.seq + 1)
    assert renamed.rev == created.rev + 1
    row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, created.entity_id)
    assert row["name"] == "After"
    assert int(row["rev"]) == created.rev + 1
    assert int(row["updated_seq"]) == renamed.seq
    assert renamed.op == OP_UPDATE


async def test_update_note_content_replaces_title_and_body(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A content update writes both fields, leaves the parent alone and bumps rev."""
    _, workspace = await seed()
    folder = await write(make_command("folder_create", workspace.id, name="F"))
    created = await write(
        make_command(
            "note_create", workspace.id, title="old", body_md="old body", folder_id=folder.entity_id
        )
    )

    updated = await write(
        make_command(
            "note_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            title="new",
            body_md="new body",
        )
    )

    await assert_one_success(kernel_db, updated, expected_seq=created.seq + 1)
    assert updated.rev == created.rev + 1
    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, created.entity_id)
    assert row["title"] == "new"
    assert row["body_md"] == "new body"
    assert row["folder_id"] == folder.entity_id, (
        "a content update must never move the note (content and move are separate commands)"
    )


async def test_noop_update_still_succeeds_and_advances(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Writing an identical value is a legal command: it still bumps rev and seq.

    The plan is explicit that a value-unchanged update is *not* deduplicated -- the
    event log records the intent, and CAS must see the new revision.
    """
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Unchanged"))

    again = await write(
        make_command(
            "folder_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            name="Unchanged",
        )
    )

    await assert_one_success(kernel_db, again, expected_seq=created.seq + 1)
    assert again.rev == created.rev + 1, "an unchanged value must still advance the revision"
    row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, created.entity_id)
    assert row["name"] == "Unchanged"
    assert int(row["rev"]) == 2
    assert int(row["updated_seq"]) == again.seq


async def test_noop_note_update_still_advances(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Same rule for notes: identical content is a new revision, not a silent no-op."""
    _, workspace = await seed()
    created = await write(make_command("note_create", workspace.id, title="t", body_md="b"))

    again = await write(
        make_command(
            "note_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            title="t",
            body_md="b",
        )
    )

    await assert_one_success(kernel_db, again, expected_seq=2)
    assert again.rev == 2
    quads = await kernel_db.quads(workspace.id)
    assert (quads.events, quads.ledger, quads.last_seq) == (2, 2, 2)


async def test_note_rev_chain_is_strictly_increasing(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Each accepted update returns exactly the previous rev + 1 (no gaps, no reuse)."""
    _, workspace = await seed()
    created = await write(make_command("note_create", workspace.id, title="v1", body_md="x"))
    revisions = [created.rev]

    base = created
    for index in range(2, 6):
        base = await write(
            make_command(
                "note_update",
                workspace.id,
                entity_id=created.entity_id,
                base_rev=base.rev,
                title=f"v{index}",
                body_md=f"body {index}",
            )
        )
        revisions.append(base.rev)

    assert revisions == list(range(1, len(revisions) + 1)), (
        f"rev must be a dense 1..N chain, got {revisions}"
    )
    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, created.entity_id)
    assert int(row["rev"]) == revisions[-1]
    assert row["title"] == "v5"
    quads = await kernel_db.quads(workspace.id)
    assert quads.last_seq == len(revisions)
    assert quads.event_seqs == tuple(range(1, len(revisions) + 1))


# --------------------------------------------------------------------------- #
# moves
# --------------------------------------------------------------------------- #


async def test_move_folder_changes_only_the_root(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A folder move re-parents just that folder and emits exactly one event."""
    _, workspace = await seed()
    left = await write(make_command("folder_create", workspace.id, name="Left"))
    right = await write(make_command("folder_create", workspace.id, name="Right"))
    moving = await write(make_command("folder_create", workspace.id, name="Moving"))

    moved = await write(
        make_command(
            "folder_move",
            workspace.id,
            entity_id=moving.entity_id,
            base_rev=moving.rev,
            parent_id=right.entity_id,
        )
    )

    await assert_one_success(kernel_db, moved, expected_seq=moving.seq + 1)
    assert moved.op == OP_MOVE
    assert moved.rev == moving.rev + 1
    assert await kernel_db.folder_parent(workspace.id, moving.entity_id) == right.entity_id
    assert await kernel_db.folder_parent(workspace.id, left.entity_id) is None
    quads = await kernel_db.quads(workspace.id)
    assert quads.events == 4, f"only the moved root gets an event: {quads.describe()}"
    assert quads.event_seqs == (1, 2, 3, 4)


async def test_move_folder_to_root_is_allowed(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """``parent_id=None`` is an explicit move to the root, not a missing field."""
    _, workspace = await seed()
    parent = await write(make_command("folder_create", workspace.id, name="Parent"))
    child = await write(
        make_command("folder_create", workspace.id, name="Child", parent_id=parent.entity_id)
    )

    moved = await write(
        make_command(
            "folder_move",
            workspace.id,
            entity_id=child.entity_id,
            base_rev=child.rev,
            parent_id=None,
        )
    )

    await assert_one_success(kernel_db, moved, expected_seq=child.seq + 1)
    assert await kernel_db.folder_parent(workspace.id, child.entity_id) is None
    assert moved.rev == child.rev + 1


async def test_move_note_between_folders(kernel_db: KernelDB, seed: object, write: object) -> None:
    """A note move rewrites ``folder_id`` and never touches the body."""
    _, workspace = await seed()
    source = await write(make_command("folder_create", workspace.id, name="Source"))
    target = await write(make_command("folder_create", workspace.id, name="Target"))
    note = await write(
        make_command(
            "note_create", workspace.id, title="t", body_md="body", folder_id=source.entity_id
        )
    )

    moved = await write(
        make_command(
            "note_move",
            workspace.id,
            entity_id=note.entity_id,
            base_rev=note.rev,
            folder_id=target.entity_id,
        )
    )

    await assert_one_success(kernel_db, moved, expected_seq=note.seq + 1)
    assert moved.op == OP_MOVE
    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, note.entity_id)
    assert row["folder_id"] == target.entity_id
    assert row["body_md"] == "body", "a move must not rewrite content"
    assert row["title"] == "t"


async def test_move_note_to_unfiled(kernel_db: KernelDB, seed: object, write: object) -> None:
    """``folder_id=None`` files the note at the top level."""
    _, workspace = await seed()
    folder = await write(make_command("folder_create", workspace.id, name="F"))
    note = await write(
        make_command(
            "note_create", workspace.id, title="t", body_md="b", folder_id=folder.entity_id
        )
    )

    moved = await write(
        make_command(
            "note_move",
            workspace.id,
            entity_id=note.entity_id,
            base_rev=note.rev,
            folder_id=None,
        )
    )

    await assert_one_success(kernel_db, moved, expected_seq=note.seq + 1)
    assert await kernel_db.note_folder(workspace.id, note.entity_id) is None


# --------------------------------------------------------------------------- #
# deletes (tombstones)
# --------------------------------------------------------------------------- #


async def test_delete_folder_keeps_the_row_as_a_tombstone(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Delete is a soft marker: the row survives with ``deleted_at`` and a new rev."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Doomed"))

    deleted = await write(
        make_command(
            "folder_delete", workspace.id, entity_id=created.entity_id, base_rev=created.rev
        )
    )

    await assert_one_success(kernel_db, deleted, expected_seq=created.seq + 1)
    assert deleted.op == OP_DELETE
    assert deleted.rev == created.rev + 1
    row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, created.entity_id)
    assert row["deleted_at"] is not None, "the tombstone marker must be set"
    assert int(row["rev"]) == deleted.rev
    assert int(row["updated_seq"]) == deleted.seq
    assert row["name"] == "Doomed", "the tombstone keeps the last known content"


async def test_delete_note_keeps_the_row_as_a_tombstone(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Notes are tombstoned the same way: no physical delete, body preserved."""
    _, workspace = await seed()
    created = await write(make_command("note_create", workspace.id, title="t", body_md="keep me"))

    deleted = await write(
        make_command("note_delete", workspace.id, entity_id=created.entity_id, base_rev=created.rev)
    )

    await assert_one_success(kernel_db, deleted, expected_seq=created.seq + 1)
    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, created.entity_id)
    assert row["deleted_at"] is not None
    assert row["body_md"] == "keep me", "a tombstone keeps the stored body"
    assert int(row["rev"]) == deleted.rev


async def test_delete_empty_folder_is_allowed(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A folder with no live children can be deleted (no cascade needed)."""
    _, workspace = await seed()
    parent = await write(make_command("folder_create", workspace.id, name="Parent"))
    child = await write(
        make_command("folder_create", workspace.id, name="Child", parent_id=parent.entity_id)
    )
    # Delete the child first, then the parent becomes empty and may go too.
    await write(
        make_command("folder_delete", workspace.id, entity_id=child.entity_id, base_rev=child.rev)
    )

    deleted = await write(
        make_command("folder_delete", workspace.id, entity_id=parent.entity_id, base_rev=parent.rev)
    )

    await assert_one_success(kernel_db, deleted)
    assert await kernel_db.deleted_at(workspace.id, ENTITY_FOLDER, parent.entity_id) is not None
    assert await kernel_db.live_children(workspace.id, parent.entity_id) == 0


# --------------------------------------------------------------------------- #
# cross-cutting single-connection invariants
# --------------------------------------------------------------------------- #


async def test_full_lifecycle_matches_the_quadruple(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A full folder+note lifecycle: every accepted write is one event, one ledger row."""
    _, workspace = await seed()
    accepted = []

    folder = await write(make_command("folder_create", workspace.id, name="Lifecycle"))
    accepted.append(folder)
    folder = await write(
        make_command(
            "folder_update",
            workspace.id,
            entity_id=folder.entity_id,
            base_rev=folder.rev,
            name="Renamed",
        )
    )
    accepted.append(folder)
    other = await write(make_command("folder_create", workspace.id, name="Other"))
    accepted.append(other)
    folder = await write(
        make_command(
            "folder_move",
            workspace.id,
            entity_id=folder.entity_id,
            base_rev=folder.rev,
            parent_id=other.entity_id,
        )
    )
    accepted.append(folder)
    note = await write(
        make_command("note_create", workspace.id, title="n", body_md="b", folder_id=other.entity_id)
    )
    accepted.append(note)
    note = await write(
        make_command(
            "note_update",
            workspace.id,
            entity_id=note.entity_id,
            base_rev=note.rev,
            title="n2",
            body_md="b2",
        )
    )
    accepted.append(note)
    note = await write(
        make_command(
            "note_move",
            workspace.id,
            entity_id=note.entity_id,
            base_rev=note.rev,
            folder_id=None,
        )
    )
    accepted.append(note)
    deleted = await write(
        make_command("note_delete", workspace.id, entity_id=note.entity_id, base_rev=note.rev)
    )
    accepted.append(deleted)

    expected = len(accepted)
    quads = await kernel_db.quads(workspace.id)
    assert [result.seq for result in accepted] == list(range(1, expected + 1)), (
        f"the accepted writes must consume seq 1..{expected} in order: {[r.seq for r in accepted]}"
    )
    assert quads.events == expected, f"one event per accepted write: {quads.describe()}"
    assert quads.ledger == expected, f"one ledger row per accepted write: {quads.describe()}"
    assert quads.last_seq == expected
    assert quads.event_seqs == tuple(range(1, expected + 1))
    assert await kernel_db.events_without_ledger(workspace.id) == [], (
        "every event must be paired with its ledger row"
    )
    assert await kernel_db.ledger_without_event(workspace.id) == []


async def test_each_write_returns_a_value_object_not_an_orm_row(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Plan section 5: results are immutable value objects, never ORM entities."""
    from revnote_api.kernel.results import WriteResult

    _, workspace = await seed()
    result = await write(make_command("folder_create", workspace.id, name="Value"))

    assert isinstance(result, WriteResult), (
        f"the write entrypoint must return WriteResult, got {type(result).__name__}"
    )
    assert not hasattr(type(result), "__mapper__"), "a result must not be a mapped class"
    assert not hasattr(result, "_sa_instance_state"), "a result must not be an ORM instance"
    with pytest.raises((AttributeError, TypeError)):
        result.seq = 999  # type: ignore[misc]  # frozen dataclass


async def test_default_workspace_counters(kernel_db: KernelDB, seed: object) -> None:
    """A fresh workspace starts at ``last_seq=0`` with ``min_retained_seq=1`` (D7)."""
    _, workspace = await seed()

    row = await kernel_db.workspace_row(workspace.id)
    assert int(row["last_seq"]) == 0, "no seq may be pre-allocated"
    assert int(row["min_retained_seq"]) == 1, "the retention floor starts at 1 and never moves"
    quads: Quads = await kernel_db.quads(workspace.id)
    assert (quads.events, quads.ledger, quads.last_seq) == (0, 0, 0)


async def test_seq_and_rev_are_plain_ints(kernel_db: KernelDB, seed: object, write: object) -> None:
    """D2: the kernel keeps ``seq``/``rev`` as ints (no string serialisation yet)."""
    _, workspace = await seed()
    result = await write(make_command("folder_create", workspace.id, name="Ints"))

    assert isinstance(result.seq, int) and not isinstance(result.seq, bool), (
        f"result.seq must be a plain int, got {type(result.seq).__name__}"
    )
    assert isinstance(result.rev, int) and not isinstance(result.rev, bool), (
        f"result.rev must be a plain int, got {type(result.rev).__name__}"
    )


async def test_storage_invariant_holds_across_several_entities(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """One workspace, many objects: the cursor is shared, so seqs stay dense across entities."""
    _, workspace = await seed()
    writes = [
        make_command("folder_create", workspace.id, name="a"),
        make_command("note_create", workspace.id, title="t1", body_md="b1"),
        make_command("folder_create", workspace.id, name="b"),
        make_command("note_create", workspace.id, title="t2", body_md="b2"),
    ]
    seqs = []
    for command in writes:
        result = await write(command)
        seqs.append(result.seq)

    assert seqs == [1, 2, 3, 4], f"the workspace cursor must be shared across entity types: {seqs}"
    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == (1, 2, 3, 4)
    assert quads.ledger == 4


async def test_failed_write_then_success_has_no_hole(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A rejected write must not consume a seq: the next success still gets the next number."""
    _, workspace = await seed()
    first = await write(make_command("folder_create", workspace.id, name="One"))
    before = await kernel_db.quads(workspace.id)

    with expect("RevConflict"):
        await write(
            make_command(
                "folder_update",
                workspace.id,
                entity_id=first.entity_id,
                base_rev=99,
                name="Never",
            )
        )
    after_failure = await kernel_db.quads(workspace.id)
    assert_quads_unchanged(before, after_failure, context="failed CAS before a success")

    second = await write(make_command("folder_create", workspace.id, name="Two"))
    assert second.seq == first.seq + 1, (
        f"a rolled-back write must not burn a seq: {first.seq} -> {second.seq}"
    )
    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == (1, 2)
    assert quads.last_seq == 2


async def test_repeated_writes_keep_the_cursor_dense(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Twenty sequential creates: seqs are 1..20 with no hole (single connection)."""
    _, workspace = await seed()
    results = []
    for index in range(20):
        results.append(await write(make_command("folder_create", workspace.id, name=f"f{index}")))

    assert [result.seq for result in results] == list(range(1, 21))
    assert [result.rev for result in results] == [1] * 20
    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == tuple(range(1, 21))
    assert (quads.events, quads.ledger, quads.last_seq) == (20, 20, 20)


async def test_entity_ids_are_never_reused(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A fresh uuid per create; the same id after delete is a conflict (plan section 6)."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Once"))
    await write(
        make_command(
            "folder_delete", workspace.id, entity_id=created.entity_id, base_rev=created.rev
        )
    )

    with expect("EntityIdConflict"):
        await write(
            make_command("folder_create", workspace.id, name="Twice", entity_id=created.entity_id)
        )

    row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, created.entity_id)
    assert row["name"] == "Once", "the conflict must not overwrite the tombstoned row"
    assert row["deleted_at"] is not None, "no resurrection through id reuse"


async def test_command_identity_is_echoed_exactly(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The result echoes the caller's ids (D5: caller owns entity_id and op_id)."""
    _, workspace = await seed()
    entity_id = uuid.uuid4()
    from kernel_support import new_op_id

    op_id = new_op_id()
    command = make_command(
        "note_create", workspace.id, title="t", body_md="b", entity_id=entity_id, op_id=op_id
    )

    result = await write(command)

    assert result.entity_id == entity_id, f"result must echo entity_id. {describe_command(command)}"
    assert result.op_id == op_id, "result must echo the caller's op_id"
    assert result.workspace_id == workspace.id
    await assert_one_success(kernel_db, result)


async def test_same_command_shape_twice_with_new_op_ids_is_two_operations(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Identical content under fresh op_ids is two independent operations."""
    _, workspace = await seed()
    first = make_command("folder_create", workspace.id, name="Twin")
    second = with_op_id(make_command("folder_create", workspace.id, name="Twin"))

    one = await write(first)
    two = await write(second)

    assert one.entity_id != two.entity_id
    assert (one.seq, two.seq) == (1, 2)
    quads = await kernel_db.quads(workspace.id)
    assert quads.ledger == 2
