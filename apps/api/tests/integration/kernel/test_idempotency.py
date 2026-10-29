"""Idempotency, replay and ``op_id`` conflicts (commit 2).

Plan sections 5/6/8: the ledger is consulted **after** the workspace lock and **before**
CAS, so a retry of an already-applied command returns the first result even when the
object has moved on or has since been deleted. ``(workspace_id, op_id)`` is one command
forever: the same key with different content is ``OpIdConflict`` and writes nothing.

Everything here is single-connection. The concurrent version of "same op / same key"
(simultaneous arrival at the barrier) lives in ``test_concurrency.py`` (commit 3).
"""

from __future__ import annotations

import pytest
from kernel_support import (
    ENTITY_FOLDER,
    ENTITY_NOTE,
    KernelDB,
    assert_quads_unchanged,
    expect,
    make_command,
    new_op_id,
    request_hash,
    variant,
    with_op_id,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# --------------------------------------------------------------------------- #
# same command, same op_id -> identical result, no new event
# --------------------------------------------------------------------------- #


async def test_replaying_a_create_returns_the_identical_result(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A create retried with the same op_id replays instead of creating a second object."""
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="Once")

    first = await write(command)
    second = await write(command)

    assert second == first, (
        f"a replay must return an identical WriteResult (first={first!r}, second={second!r})"
    )
    event = await kernel_db.event_at(workspace.id, first.seq)
    assert event is not None
    assert await kernel_db.count_events(workspace.id) == 1, (
        "a replay must not append a second event"
    )
    assert await kernel_db.count_ledger(workspace.id) == 1, (
        "a replay must not append a second ledger row"
    )
    last = await kernel_db.last_seq(workspace.id)
    assert last == 1, f"a replay must not advance the cursor, got last_seq={last}"


async def test_replay_reports_the_first_result_not_the_current_row(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """After further updates the original command still replays to its own seq/rev (§5)."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="v1"))
    original = make_command(
        "folder_update",
        workspace.id,
        entity_id=created.entity_id,
        base_rev=created.rev,
        name="v2",
    )
    applied = await write(original)
    # Move the object on twice more; the recorded result must not follow the live row.
    base = applied
    for name in ("v3", "v4"):
        base = await write(
            make_command(
                "folder_update",
                workspace.id,
                entity_id=created.entity_id,
                base_rev=base.rev,
                name=name,
            )
        )

    replayed = await write(original)

    assert replayed == applied, (
        "the replay must be rebuilt from the recorded event, not from the live row: "
        f"applied={applied!r} replayed={replayed!r}"
    )
    assert replayed.seq == applied.seq
    assert replayed.rev == applied.rev, (
        f"a replay must keep its historical rev {applied.rev}, got {replayed.rev}"
    )
    current = await kernel_db.rev(workspace.id, "folder", created.entity_id)
    assert current == 4, f"the live row is at rev 4 by now, got {current}"
    quads = await kernel_db.quads(workspace.id)
    assert quads.events == 4
    assert quads.last_seq == 4


async def test_replay_precedes_cas(kernel_db: KernelDB, seed: object, write: object) -> None:
    """A stale ``base_rev`` replays when the op_id is recorded: ledger wins over CAS."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Original"))
    command = make_command(
        "folder_update",
        workspace.id,
        entity_id=created.entity_id,
        base_rev=created.rev,  # will be stale after the first application
        name="Applied",
    )
    applied = await write(command)
    # Bump the revision so the command's base_rev is now out of date.
    await write(
        make_command(
            "folder_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=applied.rev,
            name="Later",
        )
    )

    replayed = await write(command)

    assert replayed == applied, (
        "plan step 3: the ledger lookup runs before the CAS check, so a recorded op "
        f"replays even with a stale base_rev. applied={applied!r} replayed={replayed!r}"
    )


async def test_replay_precedes_the_deleted_check(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The original delete replays even after the object is a tombstone (§5/D7)."""
    _, workspace = await seed()
    created = await write(make_command("note_create", workspace.id, title="t", body_md="b"))
    delete = make_command(
        "note_delete", workspace.id, entity_id=created.entity_id, base_rev=created.rev
    )
    applied = await write(delete)

    replayed = await write(delete)

    assert replayed == applied, (
        f"replaying the original delete must return its first result, got {replayed!r}"
    )
    assert await kernel_db.count_events(workspace.id) == 2
    assert await kernel_db.deleted_at(workspace.id, ENTITY_NOTE, created.entity_id) is not None


async def test_replay_of_an_update_after_delete_still_returns_first_result(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """An update recorded before the delete replays from its event, not ``EntityDeleted``."""
    _, workspace = await seed()
    created = await write(make_command("note_create", workspace.id, title="a", body_md="a"))
    update = make_command(
        "note_update",
        workspace.id,
        entity_id=created.entity_id,
        base_rev=created.rev,
        title="b",
        body_md="b",
    )
    applied = await write(update)
    await write(
        make_command("note_delete", workspace.id, entity_id=created.entity_id, base_rev=applied.rev)
    )

    replayed = await write(update)

    assert replayed == applied, (
        "the ledger is consulted before the deleted check (plan step 3 before step 4): "
        f"applied={applied!r} replayed={replayed!r}"
    )


async def test_replay_is_stable_over_many_attempts(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Repeated replays neither drift nor allocate: the quadruple is pinned."""
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="Idempotent")
    first = await write(command)
    before = await kernel_db.quads(workspace.id)

    results = [await write(command) for _ in range(5)]

    assert all(result == first for result in results), (
        f"every replay must return the same value: {[repr(r) for r in results]}"
    )
    after = await kernel_db.quads(workspace.id)
    assert_quads_unchanged(before, after, context="five replays of one recorded op")


async def test_replay_with_a_new_service_instance(
    kernel_db: KernelDB, seed: object, write: object, new_write: object
) -> None:
    """Idempotency comes from the database, so it survives a brand-new service object."""
    _, workspace = await seed()
    command = make_command("note_create", workspace.id, title="t", body_md="b")
    first = await write(command)

    fresh = new_write()
    second = await fresh(command)

    assert second == first, "a new service instance must still find the ledger row"
    assert await kernel_db.count_events(workspace.id) == 1


# --------------------------------------------------------------------------- #
# same op_id, different content -> OpIdConflict
# --------------------------------------------------------------------------- #


async def test_same_op_id_with_different_content_is_a_conflict(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """One op_id means one command forever (D4)."""
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="First")
    applied = await write(command)
    before = await kernel_db.quads(workspace.id)

    with expect("OpIdConflict"):
        await write(variant(command, name="Second"))

    after = await kernel_db.quads(workspace.id)
    assert_quads_unchanged(before, after, context="OpIdConflict on a reused op_id")
    row = await kernel_db.entity_row(workspace.id, "folder", applied.entity_id)
    assert row["name"] == "First", "the conflicting command must not overwrite the object"


async def test_conflicting_hash_is_detected_on_a_stale_op(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The conflict is decided by the 32-byte hash, and only the first result survives."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Base"))
    command = make_command(
        "folder_update",
        workspace.id,
        entity_id=created.entity_id,
        base_rev=created.rev,
        name="Accepted",
    )
    applied = await write(command)
    ledger_before = await kernel_db.ledger_rows(workspace.id)

    # Same op_id, different business payload AND a different base_rev.
    with expect("OpIdConflict"):
        await write(variant(command, name="Rejected", base_rev=created.rev + 7))

    ledger_after = await kernel_db.ledger_rows(workspace.id)
    assert ledger_after == ledger_before, (
        f"an OpIdConflict must leave the ledger byte-identical: {ledger_before} -> {ledger_after}"
    )
    recorded = await kernel_db.ledger_for_op(workspace.id, command.op_id)
    assert recorded is not None
    assert recorded["result_seq"] == applied.seq, (
        "the recorded result must keep pointing at the accepted event"
    )

    # And the original command still replays cleanly after the failed attempt.
    assert await write(command) == applied


async def test_a_changed_base_rev_needs_a_new_op_id(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """``base_rev`` is part of the hash, so bumping it under the same op_id conflicts."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Base"))
    command = make_command(
        "folder_update",
        workspace.id,
        entity_id=created.entity_id,
        base_rev=created.rev,
        name="First",
    )
    await write(command)

    with expect("OpIdConflict"):
        await write(variant(command, base_rev=created.rev + 1, name="Second"))

    # The supported way forward is a fresh op_id with the *current* base_rev.
    applied = await write(
        variant(command, base_rev=created.rev + 1, op_id=new_op_id(), name="Second")
    )
    assert applied.rev == created.rev + 2, (
        "the first update already moved the row to rev+1, so the corrected command lands "
        f"at rev+2; got {applied!r}"
    )
    assert applied.op_id != command.op_id
    assert await kernel_db.rev(workspace.id, "folder", created.entity_id) == applied.rev


async def test_conflicting_op_does_not_allocate_a_seq(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A rejected duplicate leaves the cursor alone; the next real op takes the next seq."""
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="Kept")
    await write(command)

    with expect("OpIdConflict"):
        await write(variant(command, name="Discarded"))

    nxt = await write(make_command("folder_create", workspace.id, name="Next"))
    assert nxt.seq == 2, f"no seq may be burnt by the conflict, got {nxt.seq}"
    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == (1, 2)


async def test_delete_then_same_op_id_with_new_base_rev_conflicts(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Base rev is hashed, so even a delete "retry" with another base_rev is a conflict."""
    _, workspace = await seed()
    created = await write(make_command("note_create", workspace.id, title="t", body_md="b"))
    delete = make_command(
        "note_delete", workspace.id, entity_id=created.entity_id, base_rev=created.rev
    )
    applied = await write(delete)

    with expect("OpIdConflict"):
        await write(variant(delete, base_rev=created.rev + 5))

    assert await write(delete) == applied


# --------------------------------------------------------------------------- #
# distinct op_ids are independent
# --------------------------------------------------------------------------- #


async def test_different_op_ids_with_identical_content_are_two_ops(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Two creates with the same payload but distinct caller ids are two operations.

    ``entity_id`` is part of the fingerprint *and* an independent primary key, so two
    creates must differ in both ids; what makes them separate operations here is the
    ``(workspace_id, op_id)`` ledger key, not the payload.
    """
    _, workspace = await seed()
    command = make_command("note_create", workspace.id, title="same", body_md="same")
    twin = make_command("note_create", workspace.id, title="same", body_md="same")

    assert command.op_id != twin.op_id, "each create must carry its own op_id"
    first = await write(command)
    second = await write(twin)

    assert first.entity_id != second.entity_id
    assert (first.seq, second.seq) == (1, 2)
    assert await kernel_db.count_ledger(workspace.id) == 2
    assert await kernel_db.count_events(workspace.id) == 2
    assert first.rev == second.rev == 1


async def test_same_payload_same_entity_under_two_op_ids_conflicts(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A second *create* of an existing entity id is ``EntityIdConflict``, not a new op.

    Ids are never reused (plan section 6), so a retried create must resend the *same*
    ``op_id`` -- that path replays; a fresh ``op_id`` would be a different command and is
    rejected by the id rule.
    """
    _, workspace = await seed()
    command = make_command("note_create", workspace.id, title="same", body_md="same")
    await write(command)
    before = await kernel_db.quads(workspace.id)

    with expect("EntityIdConflict"):
        await write(with_op_id(command))

    assert_quads_unchanged(before, await kernel_db.quads(workspace.id), context="id reuse")
    assert await write(command) is not None


async def test_forging_a_ledger_row_makes_the_service_replay(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A pre-existing ledger row is authoritative even when the object never existed.

    Rewritten entirely through SQL (plan section 6: the database arbitrates
    ``(workspace_id, op_id)``), which is why an orphaned event/ledger pair must be
    rejected as inconsistent rather than silently ignored. Note the result cannot be
    reconstructed from the live row here, because there is no live row at all.
    """
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="Recorded elsewhere")
    recorded_seq = await kernel_db.forge_recorded_op(command, rev=1)

    replayed = await write(command)

    assert replayed.seq == recorded_seq, (
        f"a replay rebuilds from op_ledger.result_seq ({recorded_seq}), got {replayed.seq}"
    )
    assert replayed.entity_id == command.entity_id
    assert replayed.op_id == command.op_id, "the replay echoes the command's own op_id"
    assert replayed.rev == 1
    assert await kernel_db.count_events(workspace.id) == 1, "a replayed op appends no event"
    assert (
        await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, command.entity_id) is None
    ), "a replay must not create the object: the recorded event is the only evidence"
    assert await kernel_db.count_ledger(workspace.id) == 1


async def test_forged_ledger_for_another_entity_is_a_storage_failure(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A ledger that points at an event describing something else is stored-state corruption.

    Plan section 6: "an orphan event without a valid ledger is a consistency error" --
    here the mirror case, a ledger whose event does not describe this command.
    """
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="Mismatched")
    forged_seq = await kernel_db.forge_ledger_only(
        command, seq=await kernel_db.next_free_seq(workspace.id)
    )
    before = await kernel_db.quads(workspace.id)

    with expect("StorageFailure"):
        await write(command)

    assert_quads_unchanged(before, await kernel_db.quads(workspace.id), context="corrupt ledger")
    assert forged_seq == before.last_seq, (
        "the forged pair sits at the seq this helper chose; the failed read must not move "
        f"the cursor (forged={forged_seq}, last_seq={before.last_seq})"
    )


async def test_forged_ledger_with_a_different_hash_conflicts(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A forged ledger row with another hash produces ``OpIdConflict``, not a replay."""
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="Mine")
    await kernel_db.forge_recorded_op(command)
    before = await kernel_db.quads(workspace.id)

    with expect("OpIdConflict"):
        await write(variant(command, name="Different"))

    assert_quads_unchanged(before, await kernel_db.quads(workspace.id))


async def test_ledger_hash_is_stored_as_32_bytes(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The stored fingerprint is exactly the canonical command hash (§4 CHECK)."""
    _, workspace = await seed()
    command = make_command("note_create", workspace.id, title="t", body_md="body")

    result = await write(command)

    ledger = await kernel_db.ledger_for_op(workspace.id, result.op_id)
    assert ledger is not None
    stored = ledger["request_hash"]
    assert isinstance(stored, (bytes, bytearray))
    assert len(stored) == 32, f"op_ledger.request_hash must be 32 bytes, got {len(stored)}"
    assert bytes(stored) == request_hash(command), (
        "the ledger must store the canonical command hash, so a later retry can match it"
    )


async def test_ledger_and_event_agree_on_the_op_id(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """``change_events`` and ``op_ledger`` must name the same op and the same seq."""
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="Linked")

    result = await write(command)

    ledger = await kernel_db.ledger_for_op(workspace.id, command.op_id)
    assert ledger is not None
    event = await kernel_db.event_at(workspace.id, int(ledger["result_seq"]))
    assert event is not None
    assert event["op_id"] == command.op_id, (
        f"event.op_id={event['op_id']} != command.op_id={command.op_id}"
    )
    assert int(ledger["result_seq"]) == result.seq


async def test_replay_does_not_need_the_object_to_exist_anymore(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """A replay reads the event, so the tombstone's state is irrelevant to the result."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Gone"))
    await write(
        make_command(
            "folder_delete", workspace.id, entity_id=created.entity_id, base_rev=created.rev
        )
    )

    replayed = await write(
        make_command(
            "folder_create",
            workspace.id,
            name="Gone",
            op_id=created.op_id,
            entity_id=created.entity_id,
        )
    )

    assert replayed == created, (
        "replaying the original create must return the create result even though the "
        f"object is now a tombstone: created={created!r} replayed={replayed!r}"
    )
    assert await kernel_db.count_events(workspace.id) == 2


async def test_consistent_ledger_has_no_orphans(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Normal operation leaves no event without a ledger row and no ledger without an event."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="A"))
    await write(
        make_command(
            "folder_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            name="B",
        )
    )
    await write(make_command("note_create", workspace.id, title="t", body_md="b"))

    assert await kernel_db.events_without_ledger(workspace.id) == []
    assert await kernel_db.ledger_without_event(workspace.id) == []
    ledger = await kernel_db.ledger_rows(workspace.id)
    assert [int(row["result_seq"]) for row in ledger] == [1, 2, 3], (
        f"the ledger must cover every seq exactly once: {ledger}"
    )


async def test_replay_matches_under_a_fresh_connection(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """The replay is rebuilt from a committed row, so a new session sees the same value."""
    _, workspace = await seed()
    command = make_command("note_create", workspace.id, title="t", body_md="b")
    first = await write(command)

    # kernel_db opens a brand-new session (and connection) for every query.
    event = await kernel_db.event_at(workspace.id, first.seq)
    ledger = await kernel_db.ledger_for_op(workspace.id, first.op_id)
    assert event is not None and ledger is not None
    assert event["created_at"] == first.created_at

    replayed = await write(command)
    assert replayed == first
    assert replayed.seq == first.seq
    assert replayed.rev == first.rev
