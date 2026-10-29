"""Independent-connection concurrency invariants (commit 3, ``concurrency`` marker).

Plan section 8 requires *real* multi-connection evidence, not simulated ordering. Each
case therefore builds genuine backend sessions ``NullPool`` and orchestrates them with
``asyncio.Barrier`` plus ``pg_blocking_pids()`` -- never with ``sleep``-guessed timing.

What is proven here:

* **dense seq under contention** -- 8 producers x 20 creates on one workspace: every seq
  1..160 allocated exactly once, with cursor == event count == ledger count;
* **same op, simultaneous** -- identical command from N connections yields exactly one
  event/ledger row and identical results for the losers (no second write);
* **same op id, different payload** -- exactly one payload wins, the rest are
  ``OpIdConflict`` and allocate no seq;
* **CAS race** -- different op ids, same ``base_rev``: exactly one wins, the rest
  ``RevConflict``, and the object lands on the winner's revision;
* **opposite folder moves** -- two connections each try to make the other the parent; at
  most one may succeed and no cycle can exist afterwards;
* **per-workspace serialisation without a global lock** -- a locked workspace A blocks
  another A write (observed through ``pg_blocking_pids``) while a B write completes.

Every racer takes its connection **before** hitting the barrier (:meth:`KernelDB.hold`),
which is what keeps a pool ceiling from turning orchestration into a stall.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from kernel_race import (
    DENSE_SEQ_PER_PRODUCER,
    DENSE_SEQ_PRODUCERS,
    DENSE_SEQ_TOTAL,
    assert_dense_seq,
    await_waiters,
    describe_race,
    error_names,
    losers,
    only,
    raw_workspace_lock,
    run_race,
    service_writer,
    winners,
)
from kernel_support import (
    ENTITY_FOLDER,
    ENTITY_NOTE,
    KernelDB,
    Quads,
    expect,
    make_command,
    request_hash,
    variant,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration, pytest.mark.concurrency]


# --------------------------------------------------------------------------- #
# dense sequence allocation under contention
# --------------------------------------------------------------------------- #


async def test_eight_producers_allocate_a_dense_sequence(
    kernel_db: KernelDB, seed: object, session_factory: Any
) -> None:
    """8 connections x 20 creates on one workspace: seqs are exactly 1..160, no hole.

    The plan's core cursor claim: ``last_seq`` is handed out while the workspace row is
    locked, so concurrency narrows throughput but can never duplicate or skip a number.
    """
    _, workspace = await seed()

    async def producer(barrier: asyncio.Barrier, index: int) -> list[int]:
        # Take the session (and its real connection) *before* the barrier, so the
        # NullPool ceiling can never look like a deadlock.
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            seqs = []
            for step in range(DENSE_SEQ_PER_PRODUCER):
                result = await perform(
                    make_command("folder_create", workspace.id, name=f"p{index}-{step}")
                )
                seqs.append(result.seq)
            return seqs

    labels = [f"producer-{index}" for index in range(DENSE_SEQ_PRODUCERS)]
    outcomes = await run_race([producer] * DENSE_SEQ_PRODUCERS, labels=labels)
    assert not losers(outcomes), f"every producer must succeed:\n{describe_race(outcomes)}"

    allocated = [seq for outcome in outcomes for seq in outcome.value]
    assert len(allocated) == DENSE_SEQ_TOTAL, (
        f"expected {DENSE_SEQ_TOTAL} allocations, got {len(allocated)}"
    )
    assert len(set(allocated)) == DENSE_SEQ_TOTAL, (
        "a seq was handed out twice; the workspace lock is not exclusive. "
        f"duplicates={sorted({s for s in allocated if allocated.count(s) > 1})[:10]}"
    )
    assert sorted(allocated) == list(range(1, DENSE_SEQ_TOTAL + 1)), (
        "the allocated set must be exactly 1..N with no gap: "
        f"missing={sorted(set(range(1, DENSE_SEQ_TOTAL + 1)) - set(allocated))[:10]} "
        f"unexpected={sorted(set(allocated) - set(range(1, DENSE_SEQ_TOTAL + 1)))[:10]}"
    )

    # A *new* connection scans the whole range and agrees with the returned values.
    quads = await assert_dense_seq(
        kernel_db, workspace.id, expected_count=DENSE_SEQ_TOTAL, context="8x20 creates"
    )
    assert quads.last_seq == DENSE_SEQ_TOTAL
    assert len(set(quads.event_seqs)) == quads.events == DENSE_SEQ_TOTAL


async def test_every_dense_seq_is_paired_with_its_ledger_row(
    kernel_db: KernelDB, seed: object, session_factory: Any
) -> None:
    """Same 8x20 shape, but the full event/ledger pairing is checked afterwards."""
    _, workspace = await seed()
    total = DENSE_SEQ_TOTAL

    async def producer(barrier: asyncio.Barrier, index: int) -> int:
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            for step in range(DENSE_SEQ_PER_PRODUCER):
                await perform(
                    make_command("note_create", workspace.id, title=f"{index}-{step}", body_md="b")
                )
        return DENSE_SEQ_PER_PRODUCER

    outcomes = await run_race([producer] * DENSE_SEQ_PRODUCERS)
    assert not losers(outcomes), f"all producers must succeed:\n{describe_race(outcomes)}"

    await assert_dense_seq(kernel_db, workspace.id, expected_count=total, context="notes")
    assert await kernel_db.events_without_ledger(workspace.id) == [], (
        "an event was committed without its ledger row"
    )
    assert await kernel_db.ledger_without_event(workspace.id) == [], (
        "a ledger row was committed without its event"
    )
    ledger = await kernel_db.ledger_rows(workspace.id)
    assert [int(row["result_seq"]) for row in ledger] == list(range(1, total + 1))
    assert len({row["op_id"] for row in ledger}) == total


async def test_interleaved_create_and_update_stays_dense(
    kernel_db: KernelDB, seed: object, session_factory: Any, write: object
) -> None:
    """Mixed object/op types on one workspace still allocate 1..N (the cursor is shared)."""
    _, workspace = await seed()
    anchor = await write(make_command("note_create", workspace.id, title="t", body_md="b"))
    producers = 4
    per_producer = 5

    async def producer(barrier: asyncio.Barrier, index: int) -> None:
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            for step in range(per_producer):
                await perform(make_command("folder_create", workspace.id, name=f"{index}-{step}"))

    outcomes = await run_race([producer] * producers)
    assert not losers(outcomes), f"all producers must succeed:\n{describe_race(outcomes)}"

    expected = 1 + producers * per_producer
    await assert_dense_seq(kernel_db, workspace.id, expected_count=expected, context="mixed")
    assert await kernel_db.rev(workspace.id, ENTITY_NOTE, anchor.entity_id) == 1


# --------------------------------------------------------------------------- #
# the same command arriving simultaneously
# --------------------------------------------------------------------------- #


async def test_same_op_from_many_connections_writes_once(
    kernel_db: KernelDB, seed: object, session_factory: Any
) -> None:
    """Identical command, same op_id, N connections: one event, one ledger row.

    Plan section 8: "only one event/ledger row, results identical". The losers either
    replay (the common path, since the workspace lock serialises them) or are classified
    through the uniqueness fallback -- both must end at the same recorded result.
    """
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="ExactlyOnce")
    racers = 5

    async def racer(barrier: asyncio.Barrier, index: int) -> Any:
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            return await perform(command)

    outcomes = await run_race([racer] * racers, labels=[f"c{i}" for i in range(racers)])
    assert not losers(outcomes), (
        f"a duplicate op must never fail: it replays the recorded result. {describe_race(outcomes)}"
    )

    results = [outcome.value for outcome in outcomes]
    assert all(result == results[0] for result in results), (
        "every racer must return the identical first result: "
        f"{[repr(result) for result in results]}"
    )
    assert len({result.seq for result in results}) == 1
    assert len({result.rev for result in results}) == 1
    assert await kernel_db.count_events(workspace.id) == 1, (
        f"exactly one event must exist, found {await kernel_db.count_events(workspace.id)}"
    )
    assert await kernel_db.count_ledger(workspace.id) == 1
    assert await kernel_db.last_seq(workspace.id) == 1, "only one seq may be allocated"


async def test_same_op_with_different_payload_lets_exactly_one_win(
    kernel_db: KernelDB, seed: object, session_factory: Any
) -> None:
    """Same op_id, different content, simultaneous: one payload applies, the rest conflict."""
    _, workspace = await seed()
    base = make_command("folder_create", workspace.id, name="payload-0")
    payloads = [base] + [variant(base, name=f"payload-{index}") for index in range(1, 5)]

    async def racer(barrier: asyncio.Barrier, index: int) -> tuple[int, Any]:
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            return index, await perform(payloads[index])

    outcomes = await run_race(
        [racer] * len(payloads), labels=[f"payload-{i}" for i in range(len(payloads))]
    )
    winner = only(outcomes)
    kinds = sorted(error_names(outcomes))
    assert kinds == ["OpIdConflict"] * (len(payloads) - 1), (
        f"every loser must be OpIdConflict, got {kinds}:\n{describe_race(outcomes)}"
    )

    # Whatever won is the only thing recorded, and it consumed exactly one seq.
    assert await kernel_db.count_events(workspace.id) == 1, (
        "a losing payload must not append an event"
    )
    assert await kernel_db.count_ledger(workspace.id) == 1
    assert await kernel_db.last_seq(workspace.id) == 1, "an OpIdConflict must not allocate a seq"

    winning_index, winning_result = winner.value
    winning_payload = payloads[winning_index]
    assert winning_payload.op_id == base.op_id == winning_result.op_id
    row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, winning_result.entity_id)
    assert row["name"] == winning_payload.name, (
        f"the recorded object must hold the winning payload {winning_payload.name!r}, "
        f"got {row['name']!r}"
    )
    ledger = await kernel_db.ledger_for_op(workspace.id, base.op_id)
    assert ledger is not None
    assert int(ledger["result_seq"]) == winning_result.seq
    assert bytes(ledger["request_hash"]) == request_hash(winning_payload), (
        "the ledger must store the winning payload's fingerprint, so a later retry of it "
        "replays and a retry of a losing payload conflicts"
    )


async def test_same_op_across_two_services_with_new_op_ids_is_two_writes(
    kernel_db: KernelDB, seed: object, session_factory: Any
) -> None:
    """Two *different* op_ids race normally: both succeed and take two consecutive seqs."""
    _, workspace = await seed()
    commands = [
        make_command("folder_create", workspace.id, name="first"),
        make_command("folder_create", workspace.id, name="second"),
    ]

    async def racer(barrier: asyncio.Barrier, index: int) -> Any:
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            return await perform(commands[index])

    outcomes = await run_race([racer, racer])
    assert not losers(outcomes), f"both writes must succeed:\n{describe_race(outcomes)}"
    seqs = sorted(outcome.value.seq for outcome in outcomes)
    assert seqs == [1, 2], f"two writes must take 1 and 2, got {seqs}"
    await assert_dense_seq(kernel_db, workspace.id, expected_count=2, context="two ops")


# --------------------------------------------------------------------------- #
# CAS race
# --------------------------------------------------------------------------- #


async def test_cas_race_has_exactly_one_winner(
    kernel_db: KernelDB, seed: object, session_factory: Any, write: object
) -> None:
    """Different op ids, same ``base_rev``: one wins, the rest see ``RevConflict``."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Contended"))
    racers = 5
    commands = [
        make_command(
            "folder_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            name=f"winner-{index}",
        )
        for index in range(racers)
    ]

    async def racer(barrier: asyncio.Barrier, index: int) -> Any:
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            return await perform(commands[index])

    outcomes = await run_race([racer] * racers, labels=[f"upd-{i}" for i in range(racers)])
    winner = only(outcomes)
    assert sorted(error_names(outcomes)) == ["RevConflict"] * (racers - 1), (
        f"every loser must be RevConflict:\n{describe_race(outcomes)}"
    )

    assert winner.value.rev == created.rev + 1, (
        f"the single winner lands at rev {created.rev + 1}, got {winner.value.rev}"
    )
    current = await kernel_db.rev(workspace.id, ENTITY_FOLDER, created.entity_id)
    assert current == created.rev + 1, (
        f"the row must carry exactly the winner's revision, got {current}"
    )
    winner_command = next(c for c in commands if c.op_id == winner.value.op_id)
    row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, created.entity_id)
    assert row["name"] == winner_command.name, (
        f"the stored name must be the winner's payload {winner_command.name!r}, got {row['name']!r}"
    )
    # One create + one accepted update: no loser may have allocated anything.
    quads = await kernel_db.quads(workspace.id)
    assert quads.events == 2, f"losers must not append events: {quads.describe()}"
    assert quads.ledger == 2, f"losers must not append ledger rows: {quads.describe()}"
    assert quads.last_seq == 2, f"losers must not consume seqs: {quads.describe()}"
    assert quads.event_seqs == (1, 2)


async def test_cas_race_on_a_note_leaves_no_partial_content(
    kernel_db: KernelDB, seed: object, session_factory: Any, write: object
) -> None:
    """The losing content never lands, not even partially (single-row CAS update)."""
    _, workspace = await seed()
    created = await write(make_command("note_create", workspace.id, title="v1", body_md="body-1"))
    racers = 4
    commands = [
        make_command(
            "note_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            title=f"v-{index}",
            body_md=f"body-{index}",
        )
        for index in range(racers)
    ]

    async def racer(barrier: asyncio.Barrier, index: int) -> Any:
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            return await perform(commands[index])

    outcomes = await run_race([racer] * racers)
    winner = only(outcomes)
    winning = next(c for c in commands if c.op_id == winner.value.op_id)

    row = await kernel_db.entity_row(workspace.id, ENTITY_NOTE, created.entity_id)
    assert row["title"] == winning.title, (
        f"title must come from the single winner ({winning.title!r}), got {row['title']!r}"
    )
    assert row["body_md"] == winning.body_md, (
        f"body must come from the single winner ({winning.body_md!r}), got {row['body_md']!r}"
    )
    assert int(row["rev"]) == 2
    assert sorted(error_names(outcomes)) == ["RevConflict"] * (racers - 1)


async def test_concurrent_deletes_of_one_object_have_one_winner(
    kernel_db: KernelDB, seed: object, session_factory: Any, write: object
) -> None:
    """Two simultaneous deletes at the same base: one tombstones, the other loses."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Doomed"))
    commands = [
        make_command(
            "folder_delete",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
        )
        for _ in range(2)
    ]

    async def racer(barrier: asyncio.Barrier, index: int) -> Any:
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            return await perform(commands[index])

    outcomes = await run_race([racer, racer], labels=["delete-a", "delete-b"])
    winner = only(outcomes)
    assert winner.value.op == "delete"
    # The tombstone check runs before the CAS (plan section 6), so the second delete always
    # sees a deleted object rather than a revision clash.
    assert error_names(outcomes) == ["EntityDeleted"], (
        f"the loser must be EntityDeleted:\n{describe_race(outcomes)}"
    )
    assert await kernel_db.deleted_at(workspace.id, ENTITY_FOLDER, created.entity_id) is not None
    assert await kernel_db.rev(workspace.id, ENTITY_FOLDER, created.entity_id) == 2
    quads = await kernel_db.quads(workspace.id)
    assert quads.events == 2, f"one create + one delete: {quads.describe()}"
    assert quads.ledger == 2, f"the loser must not record anything: {quads.describe()}"


# --------------------------------------------------------------------------- #
# opposite moves (cycle safety under concurrency)
# --------------------------------------------------------------------------- #


async def test_opposite_moves_can_never_create_a_cycle(
    kernel_db: KernelDB, seed: object, session_factory: Any, write: object
) -> None:
    """A→under B and B→under A at the same time: at most one may succeed.

    If both succeeded the tree would contain a cycle. Because the workspace lock
    serialises the two, the loser sees the new parent state -- which either makes the
    move a cycle (``FolderCycle``) or a stale revision (``RevConflict``). Both are
    acceptable; *two* successes are not.
    """
    _, workspace = await seed()
    left = await write(make_command("folder_create", workspace.id, name="Left"))
    right = await write(make_command("folder_create", workspace.id, name="Right"))
    commands = [
        make_command(
            "folder_move",
            workspace.id,
            entity_id=left.entity_id,
            base_rev=left.rev,
            parent_id=right.entity_id,
        ),
        make_command(
            "folder_move",
            workspace.id,
            entity_id=right.entity_id,
            base_rev=right.rev,
            parent_id=left.entity_id,
        ),
    ]

    async def racer(barrier: asyncio.Barrier, index: int) -> Any:
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            return await perform(commands[index])

    outcomes = await run_race([racer, racer], labels=["left-under-right", "right-under-left"])
    assert len(winners(outcomes)) <= 1, (
        "both opposite moves succeeded, so the folder tree must contain a cycle:\n"
        f"{describe_race(outcomes)}"
    )
    assert len(winners(outcomes)) == 1, (
        "the second move must be rejected, not silently accepted:\n" + describe_race(outcomes)
    )
    assert error_names(outcomes) == ["FolderCycle"], (
        f"the loser must be FolderCycle:\n{describe_race(outcomes)}"
    )

    # Verify the real parent columns contain no cycle at all.
    parents = await kernel_db.rows(
        "SELECT id, parent_id FROM folders WHERE workspace_id = :ws", ws=workspace.id
    )
    edges = {row["id"]: row["parent_id"] for row in parents}
    for start in edges:
        seen = set()
        node = start
        while node is not None:
            assert node not in seen, (
                f"the folder tree contains a cycle reachable from {start}: {edges}"
            )
            seen.add(node)
            node = edges.get(node)
    assert await kernel_db.count_events(workspace.id) == 3, (
        f"2 creates + at most 1 accepted move: {await kernel_db.count_events(workspace.id)} events"
    )


async def test_three_way_mutual_moves_leave_a_forest(
    kernel_db: KernelDB, seed: object, session_factory: Any, write: object
) -> None:
    """A 3-cycle of moves: at most two can apply, and never a loop (plan section 6)."""
    _, workspace = await seed()
    folders = [
        await write(make_command("folder_create", workspace.id, name=f"n{index}"))
        for index in range(3)
    ]
    rotations = [
        (folders[0], folders[1]),
        (folders[1], folders[2]),
        (folders[2], folders[0]),
    ]
    commands = [
        make_command(
            "folder_move",
            workspace.id,
            entity_id=mover.entity_id,
            base_rev=mover.rev,
            parent_id=target.entity_id,
        )
        for mover, target in rotations
    ]

    async def racer(barrier: asyncio.Barrier, index: int) -> Any:
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            return await perform(commands[index])

    outcomes = await run_race([racer] * 3, labels=[f"rotate-{i}" for i in range(3)])
    assert len(winners(outcomes)) <= 2, (
        f"all three rotations applied, which closes a cycle:\n{describe_race(outcomes)}"
    )
    # Each loser is deterministic: the rotation that would close the loop is a cycle, and a
    # rotation whose mover already moved is a revision clash.
    for loser_name in error_names(outcomes):
        assert loser_name in {"FolderCycle", "RevConflict"}, (
            f"unexpected loser error {loser_name}:\n{describe_race(outcomes)}"
        )

    parents = {
        row["id"]: row["parent_id"]
        for row in await kernel_db.rows(
            "SELECT id, parent_id FROM folders WHERE workspace_id = :ws", ws=workspace.id
        )
    }
    for start in parents:
        seen, node = set(), start
        while node is not None:
            assert node not in seen, f"cycle detected from {start}: {parents}"
            seen.add(node)
            node = parents.get(node)


# --------------------------------------------------------------------------- #
# per-workspace serialisation, no global lock
# --------------------------------------------------------------------------- #


async def test_lock_is_per_workspace_and_not_global(
    kernel_db: KernelDB, seed: object, session_factory: Any
) -> None:
    """Holding A's workspace lock blocks A's next write but lets B finish.

    Plan section 8 "workspace has no global lock". Blocking is *observed* through
    ``pg_blocking_pids`` from the holder's side: the raw session holds
    ``SELECT ... FOR UPDATE``, then a real service write is required to appear as a
    waiter of that holder before B is allowed to complete. No sleep-based guessing.
    """
    _, workspace_a = await seed()
    _, workspace_b = await seed()

    async with raw_workspace_lock(kernel_db, workspace_a.id) as (_, holder_pid):
        a_writer = service_writer(session_factory)
        b_writer = service_writer(session_factory)

        blocked_task = asyncio.ensure_future(
            a_writer(make_command("folder_create", workspace_a.id, name="blocked"))
        )
        # The service must really be waiting on the workspace row lock. The holder's
        # session must not touch the database (a KernelDB probe opens another connection
        # on the same engine) while the waiter is being observed, so the lock is released
        # at the end of this block and the write is awaited afterwards.
        waiters_seen = await await_waiters(kernel_db, holder_pid)
        assert waiters_seen, "no session was blocked by the workspace lock holder"

        # While A waits, an unrelated workspace must complete.
        b_result = await b_writer(make_command("folder_create", workspace_b.id, name="independent"))
        assert b_result.seq == 1, (
            f"workspace B must not be blocked by A's lock, got seq {b_result.seq}"
        )
        assert not blocked_task.done(), (
            "A's write finished while its workspace row was still locked by another session"
        )
        assert (await kernel_db.quads(workspace_b.id)).events == 1

    # Releasing the lock lets A through, and only then does A allocate.
    await blocked_task

    quads_a = await kernel_db.quads(workspace_a.id)
    assert quads_a.event_seqs == (1,), (
        f"A's blocked write must land after the lock is released: {quads_a.describe()}"
    )
    quads_b = await kernel_db.quads(workspace_b.id)
    assert quads_b.event_seqs == (1,), f"B stayed independent: {quads_b.describe()}"
    assert await kernel_db.count_events(workspace_b.id) == 1


async def test_two_writers_on_two_workspaces_do_not_serialise(
    kernel_db: KernelDB, seed: object, session_factory: Any
) -> None:
    """Two workspaces written simultaneously both complete; neither waits for the other."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    rounds = 5

    async def producer(barrier: asyncio.Barrier, index: int) -> list[int]:
        workspace = workspace_a if index == 0 else workspace_b
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            seqs: list[int] = []
            for step in range(rounds):
                result = await perform(
                    make_command("folder_create", workspace.id, name=f"{index}-{step}")
                )
                seqs.append(result.seq)
            return seqs

    outcomes = await run_race([producer, producer], labels=["ws-a", "ws-b"])
    assert not losers(outcomes), f"both workspaces must complete:\n{describe_race(outcomes)}"
    for outcome in outcomes:
        assert sorted(outcome.value) == list(range(1, rounds + 1)), (
            f"{outcome.label} must allocate its own dense 1..{rounds}, got {sorted(outcome.value)}"
        )

    for workspace in (workspace_a, workspace_b):
        quads = await kernel_db.quads(workspace.id)
        assert quads.event_seqs == tuple(range(1, rounds + 1)), quads.describe()
        assert quads.last_seq == rounds


async def test_many_writers_mixed_workspaces_stay_consistent(
    kernel_db: KernelDB, seed: object, session_factory: Any
) -> None:
    """Four workspaces, two writers each: every workspace keeps its own dense chain."""
    seeded = [await seed() for _ in range(4)]
    workspaces = [workspace.id for _, workspace in seeded]
    per_writer = 3

    async def producer(barrier: asyncio.Barrier, index: int) -> tuple[uuid.UUID, list[int]]:
        workspace_id = workspaces[index % len(workspaces)]
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            seqs = []
            for step in range(per_writer):
                result = await perform(
                    make_command("folder_create", workspace_id, name=f"w{index}-{step}")
                )
                seqs.append(result.seq)
        return workspace_id, seqs

    outcomes = await run_race([producer] * 8, labels=[f"writer-{i}" for i in range(8)])
    assert not losers(outcomes), f"all writers must succeed:\n{describe_race(outcomes)}"

    collected: dict[uuid.UUID, list[int]] = {workspace_id: [] for workspace_id in workspaces}
    for outcome in outcomes:
        workspace_id, seqs = outcome.value
        collected[workspace_id].extend(seqs)

    for workspace_id in workspaces:
        expected = len(collected[workspace_id])
        quads = await assert_dense_seq(
            kernel_db, workspace_id, expected_count=expected, context="mixed workspaces"
        )
        assert set(collected[workspace_id]) == set(quads.event_seqs), (
            "the seqs the callers were told must be exactly the rows that exist: "
            f"returned={sorted(collected[workspace_id])} stored={list(quads.event_seqs)}"
        )


async def test_write_busy_when_lock_outlives_the_timeout(
    kernel_db: KernelDB, seed: object, session_factory: Any
) -> None:
    """A lock held past ``lock_timeout`` yields ``WriteBusy`` and writes nothing.

    Plan section 5/11: ``WriteBusy`` means "nothing was written; re-send the identical
    command". This holds the row lock from a raw session for longer than the service's
    bounded ``lock_timeout`` (read from the service module, not guessed) and then checks
    that the failed write left the quadruple untouched.
    """
    from revnote_api.services.sync_write import LOCK_TIMEOUT_MS

    _, workspace = await seed()
    perform = service_writer(session_factory)
    command = make_command("folder_create", workspace.id, name="too slow")
    before = await kernel_db.quads(workspace.id)

    async with raw_workspace_lock(kernel_db, workspace.id) as (session, holder_pid):
        task = asyncio.ensure_future(perform(command))
        # Observe from the holder's side: the service's own connection is a waiter.
        assert await await_waiters(kernel_db, holder_pid), (
            "the write never waited on the workspace row lock"
        )
        # Let PostgreSQL cancel the waiting statement (bounded lock_timeout), then release.
        with expect("WriteBusy"):
            await asyncio.wait_for(task, timeout=(LOCK_TIMEOUT_MS / 1000.0) * 6 + 30)
        await session.commit()

    after: Quads = await kernel_db.quads(workspace.id)
    assert after.last_seq == before.last_seq, (
        f"a WriteBusy must not move the cursor: {before.describe()} -> {after.describe()}"
    )
    assert after.events == before.events, after.describe()
    assert after.ledger == before.ledger, after.describe()
    assert (
        await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, command.entity_id) is None
    ), "a timed-out write must not create the object"

    # The documented recovery: re-send the identical command (same op_id/entity_id).
    retried = await service_writer(session_factory)(command)
    assert retried.seq == 1, f"the retry must apply cleanly, got {retried!r}"
    assert (await kernel_db.quads(workspace.id)).event_seqs == (1,)
