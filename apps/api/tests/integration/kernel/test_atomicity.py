"""Atomicity, rollback and the two "unknown outcome" cases (commit 3, ``concurrency``).

Plan section 8's remaining rows, all of which need a second connection because the proof is
"a *new* session sees the original state":

* **fault matrix** -- fail after the seq update / object write / event / ledger / before
  COMMIT: a cold connection observes the untouched quadruple, and a retry leaves no hole;
* **ledger uniqueness backstop** -- a complete ledger INSERT that returns no row (or hits the
  real ``(workspace_id, op_id)`` unique violation) must roll the *whole* candidate back and
  then replay/conflict from a new transaction, leaving no orphan event;
* **different workspaces do not interlock** -- a failing write in A cannot block or corrupt B;
* **lost COMMIT acknowledgement** -- the write *is* durable; that is ``CommitOutcomeUnknown``,
  which must never be reported as a rollback, and a same-``op_id`` retry returns the first
  result instead of applying a second event.

The injection mechanism is test-side only: :class:`Failpoint` wraps the real
``SyncRepository`` method, lets the real statement run inside the service's transaction and
then raises, so the rollback is exercised for real. Production has no debug endpoint and no
environment-controlled failpoint (plan section 8).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import pytest
import sqlalchemy.exc
from kernel_race import (
    BEYOND_FLOAT_EXACT,
    MAX_BIGINT,
    UNIQUE_EVENTS_WS_OP,
    UNIQUE_EVENTS_WS_SEQ,
    UNIQUE_LEDGER_PK,
    UNIQUE_LEDGER_SEQ,
    assert_unique_violation,
    await_waiters,
    constraint_name,
    describe_race,
    error_name,
    error_names,
    losers,
    run_race,
    service_writer,
    uncommitted_write,
)
from kernel_service import SyncWriteService, call
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
    sqlstate,
    variant,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from revnote_api.kernel.errors import SyncKernelError
from revnote_api.services.sync_repository import SyncRepository

pytestmark = [pytest.mark.anyio, pytest.mark.integration, pytest.mark.concurrency]


# --------------------------------------------------------------------------- #
# injection helpers (test-side only)
# --------------------------------------------------------------------------- #


class Injected(RuntimeError):
    """Marker raised after a real repository statement ran inside the transaction."""


class InjectedAfterLedger(Injected):
    """Raised after the ledger INSERT succeeded, i.e. immediately before COMMIT."""


def _wrap(
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    *,
    exc: type[BaseException] = Injected,
    on_calls: tuple[int, ...] | None = None,
    replace_return: Any = ...,
) -> dict[str, int]:
    """Patch ``SyncRepository.<method>`` to run the real statement first, then misbehave.

    ``on_calls`` selects the *1-based call numbers* that misbehave (``None`` = every call
    from the first one on). Selecting a specific call is what makes two important
    constructions possible:

    * ``find_ledger`` returning ``None`` once, which models "another transaction recorded
      this op between our lookup and our insert" -- the only way to reach plan step 8's
      uniqueness backstop, since the workspace lock normally makes the race unreachable;
    * letting earlier statements pass so a *later* step (for example the ledger INSERT
      after a successful event INSERT) is the one that fails.

    ``replace_return`` substitutes the return value instead of raising; ``...`` means
    "raise".
    """
    original = getattr(SyncRepository, method)
    counter = {"calls": 0}

    async def patched(self: SyncRepository, *args: Any, **kwargs: Any) -> Any:
        result = await original(self, *args, **kwargs)  # the real SQL executes first
        counter["calls"] += 1
        call_number = counter["calls"]
        fires = on_calls is None or call_number in on_calls
        if not fires:
            return result
        if replace_return is not ...:
            return replace_return
        raise exc(f"injected failure after SyncRepository.{method}")

    monkeypatch.setattr(SyncRepository, method, patched)
    return counter


def _skip_lookup_once(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Make the *first* ``find_ledger`` call see no row (a stale/missed read).

    Plan section 6 relies on the ledger unique constraint as a backstop precisely because a
    lookup *can* miss a concurrently recorded op. Reproducing that here is what makes the
    backstop branch reachable in a test at all: the workspace row lock serialises normal
    writers, so the collision is otherwise impossible to provoke.
    """
    return _wrap(monkeypatch, "find_ledger", on_calls=(1,), replace_return=None)


def _spy_constraint_failures(
    monkeypatch: pytest.MonkeyPatch, *methods: str
) -> list[tuple[str, BaseException]]:
    """Record which statements the database actually rejected, then re-raise.

    Used to prove *which* idempotency key fired (the event or the ledger one) instead of
    assuming it, and to keep the recovery assertion honest about the observed cause.
    """
    seen: list[tuple[str, BaseException]] = []
    for method in methods:
        original = getattr(SyncRepository, method)

        def make(step: str, real: Any) -> Any:
            async def wrapper(self: SyncRepository, *args: Any, **kwargs: Any) -> Any:
                try:
                    return await real(self, *args, **kwargs)
                except BaseException as exc:
                    seen.append((step, exc))
                    raise

            return wrapper

        monkeypatch.setattr(SyncRepository, method, make(method, original))
    return seen


# --------------------------------------------------------------------------- #
# fault matrix: every step, cold-connection verification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("failing_step", ["advance_seq", "apply_object", "insert_event"])
async def test_failure_after_a_write_step_rolls_the_whole_transaction_back(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    write: object,
    monkeypatch: pytest.MonkeyPatch,
    failing_step: str,
) -> None:
    """A raise after any step leaves object/event/ledger/last_seq untouched.

    The verification session is created *after* the failure, so it cannot see uncommitted
    rows: the quadruple is read as a cold, independent transaction would read it (plan
    section 8 "new connection four-tuple unchanged").
    """
    _, workspace = await seed()
    anchor = await write(make_command("folder_create", workspace.id, name="Anchor"))
    before: Quads = await kernel_db.quads(workspace.id)

    counter = _wrap(monkeypatch, failing_step)
    command = make_command(
        "note_create", workspace.id, title="doomed", body_md="body", folder_id=None
    )

    service = SyncWriteService(session_factory)

    with pytest.raises(SyncKernelError) as caught:
        await call(service, command)

    assert error_name(caught.value) == "StorageFailure", (
        "an unexpected storage error must surface as StorageFailure, got "
        f"{error_name(caught.value)}: {caught.value!r}"
    )
    assert counter["calls"] >= 1, f"the failpoint on {failing_step} never fired"
    monkeypatch.undo()

    after = await kernel_db.quads(workspace.id)
    assert_quads_unchanged(before, after, context=f"failure after {failing_step}")
    assert (
        await kernel_db.entity_row_or_none(workspace.id, ENTITY_NOTE, command.entity_id) is None
    ), f"the object must not survive a failure after {failing_step}"
    assert await kernel_db.last_seq(workspace.id) == anchor.seq, (
        "the cursor must not keep a seq that was never committed"
    )


async def test_failure_after_the_ledger_insert_rolls_back_event_and_ledger(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    write: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failing right after the ledger row means no event and no ledger survive either.

    This is the last possible failure point before COMMIT, so it exercises the full
    undo of steps 6-9 together with the deferred ``updated_seq -> change_events`` FK.
    """
    _, workspace = await seed()
    before = await kernel_db.quads(workspace.id)
    command = make_command("folder_create", workspace.id, name="Almost")

    counter = _wrap(monkeypatch, "insert_ledger", exc=InjectedAfterLedger)

    with pytest.raises(SyncKernelError) as caught_ledger:
        await call(SyncWriteService(session_factory), command)
    assert error_name(caught_ledger.value) == "StorageFailure", (
        "a storage failure after the ledger INSERT must surface as StorageFailure, got "
        f"{error_name(caught_ledger.value)}"
    )
    assert counter["calls"] == 1, "the ledger INSERT must have been the failing step"
    monkeypatch.undo()

    after = await kernel_db.quads(workspace.id)
    assert_quads_unchanged(before, after, context="failure after the ledger INSERT")
    assert (
        await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, command.entity_id) is None
    )
    assert await kernel_db.ledger_for_op(workspace.id, command.op_id) is None, (
        "the ledger row that was inserted must be rolled back"
    )
    assert await kernel_db.events_for_op(workspace.id, command.op_id) == []


@pytest.mark.parametrize("failing_step", ["advance_seq", "apply_object", "insert_ledger"])
async def test_a_failed_write_leaves_no_gap_for_the_next_one(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    write: object,
    monkeypatch: pytest.MonkeyPatch,
    failing_step: str,
) -> None:
    """After a rollback the retry gets the very next seq: no hole, no reuse (plan §6)."""
    _, workspace = await seed()
    first = await write(make_command("folder_create", workspace.id, name="One"))
    doomed = make_command("folder_create", workspace.id, name="doomed")

    counter = _wrap(monkeypatch, failing_step)

    with pytest.raises(SyncKernelError) as caught_step:
        await call(SyncWriteService(session_factory), doomed)
    assert error_name(caught_step.value) == "StorageFailure"
    assert counter["calls"] >= 1, f"the failpoint on {failing_step} never fired"
    monkeypatch.undo()

    retried = await call(SyncWriteService(session_factory), doomed)
    assert retried.seq == first.seq + 1, (
        f"the retry must take the next seq after {first.seq}, got {retried.seq}"
    )
    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == (1, 2), (
        f"no hole and no duplicate after a rolled-back write: {quads.describe()}"
    )
    assert quads.ledger == 2
    assert quads.last_seq == 2


# --------------------------------------------------------------------------- #
# the ledger uniqueness backstop (plan section 6, step 8)
# --------------------------------------------------------------------------- #
#
# Reaching step 8's backstop takes two deliberate constructions, both test-side only:
#
# 1. ``find_ledger`` must *miss* once. With the workspace row locked the service can never
#    collide with a committed op, so a fake first miss is the only way to model the race the
#    plan describes ("another transaction recorded this op between our lookup and our
#    insert"). It is a read of the ledger, so faking it changes no stored state.
# 2. the *event* copy of the op-id key must be bypassed. ``change_events`` and ``op_ledger``
#    are both unique on ``(workspace_id, op_id)`` and the event is written first, so a
#    candidate would otherwise be rejected at the event and never reach the ledger. The
#    patched ``insert_event`` still runs the real INSERT, only stamping a temporary op id, so
#    the candidate is a genuine row that must be rolled back.
#
# With those two in place ``insert_ledger`` is left completely **unpatched**: its real
# ``ON CONFLICT (workspace_id, op_id) DO NOTHING RETURNING`` finds the pre-existing row and
# returns ``False`` -- the production code path, not a simulated return value.


def _miss_first_lookup(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Let the real ``find_ledger`` run, but report "not recorded" on its first call."""
    return _wrap(monkeypatch, "find_ledger", on_calls=(1,), replace_return=None)


def _bypass_event_op_id_key(monkeypatch: pytest.MonkeyPatch) -> list[uuid.UUID]:
    """Real event INSERT, but stamped with a throwaway op id.

    Returns the list of temporary op ids as they are used, so a test can prove the row was
    inserted and then rolled back (it must not survive under either op id).
    """
    original = SyncRepository.insert_event
    temporary: list[uuid.UUID] = []

    async def with_temporary_op_id(self: SyncRepository, **kwargs: Any) -> Any:
        temp = new_op_id()
        temporary.append(temp)
        return await original(self, **{**kwargs, "op_id": temp})

    monkeypatch.setattr(SyncRepository, "insert_event", with_temporary_op_id)
    return temporary


async def test_ledger_backstop_returning_false_replays_the_recorded_result(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real ledger INSERT collides and returns ``False``; the replay must win.

    Setup: an op is already recorded (event + ledger + object row). The service is then made
    to miss that ledger row once and to stamp its candidate event with a temporary op id, so
    the candidate really writes an object row, a seq and an event, and only the *ledger*
    INSERT discovers the collision -- returning ``False`` from
    ``ON CONFLICT (workspace_id, op_id) DO NOTHING RETURNING``.

    Required outcome (plan section 6, step 8):

    * the whole candidate is rolled back -- object row, allocated ``seq``/``rev`` and the
      temporary event all vanish;
    * the ledger is re-read in a **new** transaction and returns the recorded first result;
    * nothing dirty is committed and no constraint is removed or bypassed to make it pass.
    """
    _, workspace = await seed()

    # -- the pre-existing successful op -------------------------------------- #
    recorded = make_command("folder_create", workspace.id, name="Recorded first")
    recorded_seq = await kernel_db.forge_recorded_op(recorded, rev=1)
    ledger_before = await kernel_db.ledger_rows(workspace.id)
    before: Quads = await kernel_db.quads(workspace.id)
    assert before.last_seq == recorded_seq, (
        f"setup must place the recorded op at the cursor, got {before.describe()}"
    )

    # -- the two constructions, then a completely real ledger INSERT ---------- #
    lookup = _miss_first_lookup(monkeypatch)
    temporary = _bypass_event_op_id_key(monkeypatch)

    replayed = await service_writer(session_factory)(recorded)
    monkeypatch.undo()

    # -- the replay ---------------------------------------------------------- #
    assert lookup["calls"] >= 1, "the ledger lookup must have run"
    assert temporary, "the candidate event must have been inserted with a temporary op id"
    assert replayed.seq == recorded_seq, (
        f"the replay must report the recorded seq {recorded_seq}, got {replayed.seq}"
    )
    assert replayed.rev == 1, f"the replay must report the recorded rev 1, got {replayed.rev}"
    assert replayed.op_id == recorded.op_id
    assert replayed.entity_id == recorded.entity_id
    assert replayed.op == "create"

    # -- cold-connection proof that the candidate left nothing behind -------- #
    after: Quads = await kernel_db.quads(workspace.id)
    assert_quads_unchanged(before, after, context="ledger backstop, real INSERT returned False")

    for temp in temporary:
        assert await kernel_db.events_for_op(workspace.id, temp) == [], (
            "the candidate's temporary event survived the rollback"
        )
        assert await kernel_db.ledger_for_op(workspace.id, temp) is None
    assert (
        await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, recorded.entity_id) is None
    ), "the candidate's object row must be rolled back; only the recorded event exists"
    assert await kernel_db.ledger_rows(workspace.id) == ledger_before, (
        "the ledger must be byte-identical to the pre-state: "
        f"{ledger_before} -> {await kernel_db.ledger_rows(workspace.id)}"
    )

    # The cursor never advanced, so the next genuine write takes the next seq.
    follow_up = await service_writer(session_factory)(
        make_command("folder_create", workspace.id, name="After the race")
    )
    assert follow_up.seq == recorded_seq + 1, (
        f"the rolled-back candidate must not consume a seq, got {follow_up.seq}"
    )


async def test_ledger_backstop_returning_false_with_a_different_hash_conflicts(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same real ``False`` from the ledger INSERT, but the payload differs -> ``OpIdConflict``.

    The replay branch must never hand back another command's result: once the candidate is
    rolled back and the ledger is re-read, a mismatching fingerprint is the documented
    conflict, and nothing may be written.
    """
    _, workspace = await seed()
    recorded = make_command("note_create", workspace.id, title="recorded", body_md="body")
    recorded_seq = await kernel_db.forge_recorded_op(recorded, rev=1)
    before: Quads = await kernel_db.quads(workspace.id)
    ledger_before = await kernel_db.ledger_rows(workspace.id)

    challenger = variant(recorded, title="different payload")

    _miss_first_lookup(monkeypatch)
    temporary = _bypass_event_op_id_key(monkeypatch)

    with expect("OpIdConflict"):
        await service_writer(session_factory)(challenger)
    monkeypatch.undo()

    assert temporary, "the candidate event must have been inserted before the collision"
    after = await kernel_db.quads(workspace.id)
    assert_quads_unchanged(before, after, context="ledger backstop, different hash")
    assert await kernel_db.ledger_rows(workspace.id) == ledger_before

    for temp in temporary:
        assert await kernel_db.events_for_op(workspace.id, temp) == [], (
            "the candidate event must be rolled back, not left as an orphan"
        )
    assert (
        await kernel_db.entity_row_or_none(workspace.id, ENTITY_NOTE, recorded.entity_id) is None
    ), "the conflicting candidate must not create the note"

    # The recorded op still replays its own result afterwards.
    assert (await service_writer(session_factory)(recorded)).seq == recorded_seq


async def test_event_op_id_key_collision_also_rolls_back_and_replays(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The *event* copy of the op-id key is the natural collision point; same requirement.

    ``change_events`` is written before ``op_ledger``, so with only the fake lookup miss the
    database rejects the candidate at the event INSERT and raises a real unique violation.
    The service must classify that as the ledger race too (it is the same idempotency key
    under a different name) and replay from a new transaction.
    """
    _, workspace = await seed()
    recorded = make_command("folder_create", workspace.id, name="Event collision")
    recorded_seq = await kernel_db.forge_recorded_op(recorded, rev=1)
    before: Quads = await kernel_db.quads(workspace.id)

    _miss_first_lookup(monkeypatch)
    failures = _spy_constraint_failures(monkeypatch, "insert_event")

    replayed = await service_writer(session_factory)(recorded)
    monkeypatch.undo()

    assert failures, (
        "the candidate had to collide on the event's op-id key; nothing collided, so this "
        "test proved nothing"
    )
    assert replayed.seq == recorded_seq
    assert_quads_unchanged(
        before, await kernel_db.quads(workspace.id), context="event key collision"
    )
    assert (
        await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, recorded.entity_id) is None
    )


async def test_no_orphan_event_survives_a_ledger_backstop(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither op-id key may leave an event behind after the candidate is rolled back."""
    _, workspace = await seed()
    recorded = make_command("folder_create", workspace.id, name="Orphan check")
    await kernel_db.forge_recorded_op(recorded, rev=1)

    _miss_first_lookup(monkeypatch)
    temporary = _bypass_event_op_id_key(monkeypatch)

    await service_writer(session_factory)(recorded)
    monkeypatch.undo()

    assert temporary
    assert await kernel_db.events_without_ledger(workspace.id) == [], (
        "an event without a ledger row survived the rolled-back candidate"
    )
    assert await kernel_db.ledger_without_event(workspace.id) == []
    quads = await kernel_db.quads(workspace.id)
    assert quads.events == quads.ledger == 1, (
        f"only the recorded op may exist after the backstop: {quads.describe()}"
    )


# --------------------------------------------------------------------------- #
# cross-workspace independence under failure
# --------------------------------------------------------------------------- #


async def test_a_failing_workspace_does_not_interlock_another(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    write: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure injected for A only: B must complete concurrently and stay dense."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    failing = make_command("folder_create", workspace_a.id, name="will fail")
    healthy = make_command("folder_create", workspace_b.id, name="will succeed")

    original = SyncRepository.apply_object

    async def only_for_a(self: SyncRepository, command: Any, **kwargs: Any) -> int:
        result = await original(self, command, **kwargs)
        if command.workspace_id == workspace_a.id:
            raise Injected("injected failure after apply_object for workspace A")
        return result

    monkeypatch.setattr(SyncRepository, "apply_object", only_for_a)

    async def blocked(barrier: asyncio.Barrier, index: int) -> Any:
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            return await perform(failing if index == 0 else healthy)

    outcomes = await run_race([blocked, blocked], labels=["ws-a", "ws-b"])
    outcomes_by_label = {outcome.label: outcome for outcome in outcomes}
    assert not outcomes_by_label["ws-a"].ok, "workspace A's write had to fail"
    assert outcomes_by_label["ws-b"].ok, (
        f"workspace B must not be affected by A's failure: {describe_race(outcomes)}"
    )
    monkeypatch.undo()

    a_quads = await kernel_db.quads(workspace_a.id)
    assert (a_quads.events, a_quads.ledger, a_quads.last_seq) == (0, 0, 0), (
        f"A must be untouched: {a_quads.describe()}"
    )
    b_quads = await kernel_db.quads(workspace_b.id)
    assert b_quads.event_seqs == (1,), f"B must have exactly one write: {b_quads.describe()}"
    assert (
        await kernel_db.entity_row_or_none(workspace_b.id, ENTITY_FOLDER, healthy.entity_id)
        is not None
    )


async def test_two_workspaces_can_be_written_while_one_fails_repeatedly(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    write: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated failures in A never disturb B's numbering (per-workspace cursors)."""
    _, workspace_a = await seed()
    _, workspace_b = await seed()
    b_writes = [
        await write(make_command("folder_create", workspace_b.id, name=f"b{index}"))
        for index in range(2)
    ]

    for attempt in range(3):
        doomed = make_command("folder_create", workspace_a.id, name=f"a{attempt}")
        counter = _wrap(monkeypatch, "insert_event")

        with pytest.raises(SyncKernelError) as caught_attempt:
            await call(SyncWriteService(session_factory), doomed)
        assert error_name(caught_attempt.value) == "StorageFailure"
        assert counter["calls"] == 1
        monkeypatch.undo()

    b_quads = await kernel_db.quads(workspace_b.id)
    assert b_quads.event_seqs == (1, 2), f"B's chain must be untouched: {b_quads.describe()}"
    a_quads = await kernel_db.quads(workspace_a.id)
    assert (a_quads.events, a_quads.last_seq) == (0, 0), (
        f"A must have no committed write: {a_quads.describe()}"
    )
    assert b_writes[0].seq == 1 and b_writes[1].seq == 2


# --------------------------------------------------------------------------- #
# lost COMMIT acknowledgement != rollback
# --------------------------------------------------------------------------- #


async def test_lost_commit_ack_is_durable_and_retry_replays(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    write: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost acknowledgement leaves the write **committed**; a retry replays it.

    Plan sections 6/11: this must be ``CommitOutcomeUnknown`` and must never be reported as
    a rollback. The proof has two halves: a *cold* connection sees the committed row, and a
    retry of the identical command returns the first result without a second event.
    """
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="Committed anyway")

    from sqlalchemy.ext.asyncio import AsyncSession

    real_commit = AsyncSession.commit
    state = {"raised": 0}

    async def commit_then_lose_ack(session: AsyncSession) -> None:
        await real_commit(session)  # the real COMMIT happens
        state["raised"] += 1
        raise ConnectionError("injected loss of the COMMIT acknowledgement")

    monkeypatch.setattr(AsyncSession, "commit", commit_then_lose_ack)

    service = SyncWriteService(session_factory)
    with expect("CommitOutcomeUnknown"):
        await call(service, command)
    assert state["raised"] >= 1, "the commit wrapper never fired"
    monkeypatch.undo()

    # Cold-connection evidence: the write is durable despite the reported error.
    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == (1,), (
        "a lost acknowledgement must NOT be treated as a rollback; the row must be visible "
        f"to a new session: {quads.describe()}"
    )
    assert quads.ledger == 1
    assert quads.last_seq == 1
    row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, command.entity_id)
    assert row["name"] == "Committed anyway"
    assert int(row["rev"]) == 1

    # Recovery is a same-op_id retry, which must replay rather than apply twice.
    replayed = await call(SyncWriteService(session_factory), command)
    assert replayed.seq == 1, f"the retry must return the first result, got {replayed!r}"
    assert replayed.rev == 1
    assert await kernel_db.count_events(workspace.id) == 1, (
        "the confirming retry must not append a second event"
    )
    assert await kernel_db.count_ledger(workspace.id) == 1


async def test_lost_commit_ack_is_distinguishable_from_a_real_rollback(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    write: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two adjacent cases: a pre-commit failure rolls back, a lost ack does not.

    This pins the distinction the plan insists on, in one test: the same command shape
    produces ``StorageFailure`` + empty state before COMMIT, and ``CommitOutcomeUnknown`` +
    durable state after COMMIT.
    """
    _, workspace_rollback = await seed()
    _, workspace_lost = await seed()
    from sqlalchemy.ext.asyncio import AsyncSession

    rolled_back = make_command("folder_create", workspace_rollback.id, name="rolled back")
    counter = _wrap(monkeypatch, "insert_ledger", exc=InjectedAfterLedger)
    with pytest.raises(SyncKernelError) as caught_before_commit:
        await call(SyncWriteService(session_factory), rolled_back)
    assert error_name(caught_before_commit.value) == "StorageFailure"
    assert counter["calls"] == 1
    monkeypatch.undo()
    rolled_back_quads = await kernel_db.quads(workspace_rollback.id)
    assert (rolled_back_quads.events, rolled_back_quads.ledger) == (0, 0), (
        f"a failure before COMMIT must leave nothing: {rolled_back_quads.describe()}"
    )

    lost = make_command("folder_create", workspace_lost.id, name="maybe committed")
    real_commit = AsyncSession.commit

    async def commit_then_lose(session: AsyncSession) -> None:
        await real_commit(session)
        raise ConnectionError("injected loss of the COMMIT acknowledgement")

    monkeypatch.setattr(AsyncSession, "commit", commit_then_lose)
    with expect("CommitOutcomeUnknown"):
        await call(SyncWriteService(session_factory), lost)
    monkeypatch.undo()

    lost_quads = await kernel_db.quads(workspace_lost.id)
    assert lost_quads.event_seqs == (1,), (
        "a lost acknowledgement must leave the write committed, which is exactly why the "
        f"error is CommitOutcomeUnknown and not a rollback: {lost_quads.describe()}"
    )


async def test_commit_failure_with_a_lost_connection_is_not_a_rollback_claim(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connection-class failure at COMMIT maps to ``CommitOutcomeUnknown``.

    The transaction is left uncommitted here (the wrapper raises *before* the real commit),
    so the assertion is about the *classification*: the caller is told the outcome is
    unknown, which is the only honest answer when the acknowledgement never arrives.
    """
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="unknown outcome")

    from sqlalchemy.ext.asyncio import AsyncSession

    async def commit_dies(session: AsyncSession) -> None:
        raise sqlalchemy.exc.OperationalError(
            "COMMIT", {}, ConnectionError("server closed the connection unexpectedly")
        )

    monkeypatch.setattr(AsyncSession, "commit", commit_dies)

    with expect("CommitOutcomeUnknown"):
        await call(SyncWriteService(session_factory), command)
    monkeypatch.undo()

    # Nothing was committed (the wrapper never called the real commit) and no ledger row
    # claims otherwise: the state is "unknown", not "half written".
    quads = await kernel_db.quads(workspace.id)
    assert (quads.events, quads.ledger, quads.last_seq) == (0, 0, 0), (
        f"an uncommitted transaction must leave no rows: {quads.describe()}"
    )
    assert await kernel_db.events_without_ledger(workspace.id) == []


# --------------------------------------------------------------------------- #
# counter overflow and BIGINT edges
# --------------------------------------------------------------------------- #


async def test_seq_overflow_is_refused_without_writing(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
) -> None:
    """``last_seq`` at the BIGINT ceiling cannot advance: ``CounterOverflow``, zero writes."""

    _, workspace = await seed(last_seq=MAX_BIGINT)
    before = await kernel_db.quads(workspace.id)
    command = make_command("folder_create", workspace.id, name="overflow")

    with expect("CounterOverflow"):
        await call(SyncWriteService(session_factory), command)

    after = await kernel_db.quads(workspace.id)
    assert after.last_seq == MAX_BIGINT, (
        f"the cursor must not wrap or overflow: {before.describe()} -> {after.describe()}"
    )
    assert after.events == before.events
    assert after.ledger == before.ledger
    assert (
        await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, command.entity_id) is None
    )
    assert await kernel_db.ledger_for_op(workspace.id, command.op_id) is None, (
        "no ledger row may be recorded for a refused op"
    )


async def test_values_beyond_float_precision_are_preserved(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    write: object,
) -> None:
    """D2: ``seq``/``rev`` are ints, so values past 2**53 survive exactly."""

    _, workspace = await seed(last_seq=BEYOND_FLOAT_EXACT - 1)
    command = make_command("folder_create", workspace.id, name="beyond float")

    result = await call(SyncWriteService(session_factory), command)

    assert result.seq == BEYOND_FLOAT_EXACT, f"a seq beyond 2**53 must be exact, got {result.seq!r}"
    stored = await kernel_db.last_seq(workspace.id)
    assert stored == BEYOND_FLOAT_EXACT, f"the stored cursor must be exact, got {stored}"
    event = await kernel_db.event_at(workspace.id, result.seq)
    assert event is not None
    assert int(event["seq"]) == BEYOND_FLOAT_EXACT


async def test_rev_overflow_is_refused_without_writing(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    write: object,
) -> None:
    """``rev`` at the BIGINT ceiling cannot advance: ``CounterOverflow``, no state change.

    The counterpart of the ``seq`` guard, and the reason it is a separate case: the two
    counters are checked at different points (``seq`` from the workspace row, ``rev`` from
    the object row), so a service that guards one and not the other would pass a
    seq-only test.

    Construction: create the folder normally, then pin its ``rev`` to ``MAX_BIGINT`` with
    raw SQL (the service would never produce that), so the *next* update must be refused by
    the rev guard rather than by the database's ``rev > 0`` CHECK.
    """
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="rev ceiling"))

    # Pin the revision at the ceiling; nothing else about the row changes.
    await kernel_db.execute(
        "UPDATE folders SET rev = :rev WHERE workspace_id = :ws AND id = :id",
        rev=MAX_BIGINT,
        ws=workspace.id,
        id=created.entity_id,
    )
    object_before = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, created.entity_id)
    before = await kernel_db.quads(workspace.id)
    assert object_before["rev"] == MAX_BIGINT

    command = make_command(
        "folder_update",
        workspace.id,
        entity_id=created.entity_id,
        base_rev=MAX_BIGINT,
        name="cannot advance",
    )
    with expect("CounterOverflow"):
        await call(SyncWriteService(session_factory), command)

    # All four parts of the state must be exactly as they were.
    assert_quads_unchanged(before, await kernel_db.quads(workspace.id), context="rev overflow")
    object_after = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, created.entity_id)
    assert object_after["rev"] == MAX_BIGINT, (
        f"rev must not wrap or overflow, got {object_after['rev']}"
    )
    assert object_after["name"] == object_before["name"], "the refused update must not rename"
    assert object_after["updated_seq"] == object_before["updated_seq"]
    assert await kernel_db.ledger_for_op(workspace.id, command.op_id) is None, (
        "a refused op must leave no ledger row"
    )
    assert await kernel_db.events_for_op(workspace.id, command.op_id) == [], (
        "a refused op must leave no event"
    )


# --------------------------------------------------------------------------- #
# isolation under a held lock (three-connection visibility)
# --------------------------------------------------------------------------- #


async def test_three_connection_visibility_then_commit(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
) -> None:
    """T1 stages a FULL uncommitted write; T2 blocks; T3 sees nothing; T1 commits → T2 = 2.

    The real matrix from plan section 8, with no simulation:

    * **T1** runs a complete SQL transaction — workspace row locked ``FOR UPDATE``, cursor set
      to seq 1, folder row at rev 1, the event and the ledger row — and holds it open. All
      four parts of the quadruple exist but are uncommitted.
    * **T2** is a genuine service write of a *different* command on the same workspace. It
      must be observed blocked via ``pg_blocking_pids``.
    * **T3** is an independent connection: it must read the OLD quadruple (``last_seq=0``, no
      event, no ledger) and must not see the staged folder row.
    * **T1 commits**; the waiting T2 then allocates the NEXT seq (2), so the committed cursor
      plus T2's write produced 1..2 with no hole and no reuse.
    """
    _, workspace = await seed()
    t2_command = make_command("folder_create", workspace.id, name="t2-writer")
    staged_entity = new_op_id()  # any fresh uuid: this row is never referenced again

    t2_task: asyncio.Task[Any] | None = None
    async with uncommitted_write(
        kernel_db,
        workspace.id,
        entity_id=staged_entity,
        op_id=new_op_id(),
        name="t1-uncommitted",
        seq=1,
    ) as (t1_session, t1_pid):
        try:
            # -- T2: a real service write, which must queue behind T1's lock -------- #
            t2_task = asyncio.ensure_future(service_writer(session_factory)(t2_command))
            waiters = await await_waiters(kernel_db, t1_pid)
            assert waiters, "T2 never blocked on the workspace row lock held by T1"

            # -- T3: an independent connection sees the OLD state only -------------- #
            quads = await kernel_db.quads(workspace.id)
            assert quads.last_seq == 0, f"T3 read an uncommitted cursor: {quads.describe()}"
            assert quads.events == 0, f"T3 read an uncommitted event: {quads.describe()}"
            assert quads.ledger == 0, f"T3 read an uncommitted ledger row: {quads.describe()}"
            assert (
                await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, staged_entity)
                is None
            ), "T3 saw T1's uncommitted folder row"
            assert not t2_task.done(), "T2 finished while T1's transaction was still open"

            # -- T1 commits the staged write --------------------------------------- #
            await t1_session.commit()
        except BaseException:
            # Never leave a queued writer running after a failed assertion.
            if t2_task is not None and not t2_task.done():
                t2_task.cancel()
                with contextlib.suppress(BaseException):
                    await t2_task
            raise

    assert t2_task is not None
    t2_result = await t2_task

    # T1's committed write is visible, and T2 took the next seq.
    assert t2_result.seq == 2, (
        f"T2 must allocate seq 2 after T1 committed seq 1, got {t2_result.seq}"
    )
    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == (1, 2), (
        f"the committed pair must be exactly 1..2 with no hole: {quads.describe()}"
    )
    assert quads.last_seq == 2
    assert quads.ledger == 2
    assert await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, staged_entity) is not None, (
        "T1's committed folder row must exist after the commit"
    )
    assert (
        await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, t2_command.entity_id)
        is not None
    ), "T2's own folder row must exist"


async def test_three_connection_visibility_then_rollback_reuses_the_seq(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
) -> None:
    """Same staging as above, but T1 **rolls back**: T2 then reuses seq 1.

    The complement of the commit case: a candidate seq held by an aborted transaction must
    become available again, so the workspace cursor never keeps a number that was never
    committed, and T3 still never saw T1's provisional rows.
    """
    _, workspace = await seed()
    t2_command = make_command("folder_create", workspace.id, name="t2-after-rollback")
    staged_entity = new_op_id()

    t2_task: asyncio.Task[Any] | None = None
    async with uncommitted_write(
        kernel_db,
        workspace.id,
        entity_id=staged_entity,
        op_id=new_op_id(),
        name="t1-aborted",
        seq=1,
    ) as (t1_session, t1_pid):
        try:
            t2_task = asyncio.ensure_future(service_writer(session_factory)(t2_command))
            assert await await_waiters(kernel_db, t1_pid), (
                "T2 never blocked on the workspace row lock held by T1"
            )

            quads = await kernel_db.quads(workspace.id)
            assert (quads.last_seq, quads.events, quads.ledger) == (0, 0, 0), (
                f"T3 must not see T1's provisional rows: {quads.describe()}"
            )
            assert (
                await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, staged_entity)
                is None
            )

            # -- T1 rolls back instead of committing -------------------------------- #
            await t1_session.rollback()
        except BaseException:
            if t2_task is not None and not t2_task.done():
                t2_task.cancel()
                with contextlib.suppress(BaseException):
                    await t2_task
            raise

    assert t2_task is not None
    t2_result = await t2_task

    assert t2_result.seq == 1, f"T2 must reuse seq 1 after T1 rolled back, got {t2_result.seq}"
    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == (1,), (
        f"exactly one event must remain after the rollback: {quads.describe()}"
    )
    assert quads.last_seq == 1
    assert quads.ledger == 1
    assert await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, staged_entity) is None, (
        "T1's rolled-back folder row must not survive"
    )
    assert (
        await kernel_db.entity_row_or_none(workspace.id, ENTITY_FOLDER, t2_command.entity_id)
        is not None
    )


# --------------------------------------------------------------------------- #
# failure is per operation, not per workspace
# --------------------------------------------------------------------------- #


async def test_a_rejected_op_does_not_block_the_same_workspace(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    write: object,
) -> None:
    """After a rejected command the workspace is immediately writable again."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="base"))

    with expect("RevConflict"):
        await write(
            make_command(
                "folder_update",
                workspace.id,
                entity_id=created.entity_id,
                base_rev=created.rev + 5,
                name="nope",
            )
        )

    accepted = await write(
        make_command(
            "folder_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            name="yes",
        )
    )
    assert accepted.seq == 2
    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == (1, 2)


async def test_error_classification_of_a_backstop_conflict_is_stable(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recovered backstop reports a contract error name, never a raw DBAPI type."""
    _, workspace = await seed()
    base = make_command("folder_create", workspace.id, name="recorded")
    await kernel_db.forge_recorded_op(base)
    challenger = variant(base, name="different")

    _wrap(monkeypatch, "insert_ledger", replace_return=False)

    with pytest.raises(Exception) as caught:
        await call(SyncWriteService(session_factory), challenger)
    monkeypatch.undo()

    name = error_name(caught.value)
    assert name == "OpIdConflict", (
        f"the backstop must surface a kernel contract error, got {name}: {caught.value!r}"
    )
    assert not isinstance(caught.value, sqlalchemy.exc.DBAPIError), (
        "a driver exception must never escape the service"
    )
    assert caught.value.__class__.__module__.startswith("revnote_api.kernel.errors"), (
        "the recovered backstop must raise a kernel contract error, not a storage one"
    )


async def test_run_race_reports_every_outcome(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
) -> None:
    """Sanity check on the race harness itself: one winner, one captured error."""
    _, workspace = await seed()
    created_command = make_command("folder_create", workspace.id, name="contended")

    created = await call(SyncWriteService(session_factory), created_command)
    commands = [
        make_command(
            "folder_update",
            workspace.id,
            entity_id=created.entity_id,
            base_rev=created.rev,
            name=f"n{index}",
        )
        for index in range(2)
    ]

    async def racer(barrier: asyncio.Barrier, index: int) -> Any:
        perform = service_writer(session_factory)
        async with kernel_db.hold():
            await barrier.wait()
            return await perform(commands[index])

    outcomes = await run_race([racer, racer])
    assert len(losers(outcomes)) == 1, describe_race(outcomes)
    assert error_names(outcomes) == ["RevConflict"], describe_race(outcomes)
    assert (await kernel_db.quads(workspace.id)).event_seqs == (1, 2), (
        "exactly one update may be recorded"
    )


async def test_new_op_id_for_each_retry_attempt_allocates_each_time(
    kernel_db: KernelDB,
    seed: object,
    session_factory: Any,
    write: object,
) -> None:
    """A *changed* command (new op_id) is a new operation, so each attempt allocates."""
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="v1"))

    base = created
    for index in range(2, 5):
        base = await write(
            make_command(
                "folder_update",
                workspace.id,
                entity_id=created.entity_id,
                base_rev=base.rev,
                name=f"v{index}",
                op_id=new_op_id(),
            )
        )
        assert base.seq == index

    quads = await kernel_db.quads(workspace.id)
    assert quads.event_seqs == (1, 2, 3, 4), quads.describe()
    assert await kernel_db.rev(workspace.id, ENTITY_FOLDER, created.entity_id) == 4


async def test_quad_snapshot_helper_detects_a_moved_cursor(
    kernel_db: KernelDB,
    seed: object,
    write: object,
) -> None:
    """The support assertion itself must fail loudly when a cursor moved.

    A guard against a silently weak invariant: the four-tuple comparison is the core
    assertion of every rollback case, so it gets its own negative test.
    """
    _, workspace = await seed()
    before: Quads = await kernel_db.quads(workspace.id)
    await write(make_command("folder_create", workspace.id, name="moved"))
    after = await kernel_db.quads(workspace.id)

    with pytest.raises(AssertionError) as caught:
        assert_quads_unchanged(before, after, context="negative control")
    assert "roll back" in str(caught.value), (
        f"the quadruple assertion must explain the rollback rule: {caught.value}"
    )


# --------------------------------------------------------------------------- #
# the constraints themselves, proven with bare SQL (no service involved)
# --------------------------------------------------------------------------- #


async def test_raw_sql_proves_the_idempotency_keys_are_unique(
    kernel_db: KernelDB, seed: object, write: object
) -> None:
    """Both idempotency keys reject a duplicate even without the service.

    Plan section 4/6: idempotency is enforced by the **database**, not by a read-then-write
    check in the application. This case bypasses the service entirely, so a future
    "optimisation" that drops the unique constraints fails here.

    Each assertion names the exact index PostgreSQL must reject, by SQLSTATE and constraint
    name -- there is no name-tolerant matching.
    """
    _, workspace = await seed()
    created = await write(make_command("folder_create", workspace.id, name="Anchor"))

    # 1. duplicate (workspace_id, seq): the per-workspace cursor primary key
    with pytest.raises(IntegrityError) as duplicate_seq:
        await kernel_db.raw_insert_event(
            workspace_id=workspace.id,
            seq=created.seq,
            entity_type=ENTITY_FOLDER,
            entity_id=uuid.uuid4(),
            op="create",
            rev=1,
            op_id=new_op_id(),
        )
    assert_unique_violation(duplicate_seq.value, UNIQUE_EVENTS_WS_SEQ)

    # 2. duplicate (workspace_id, op_id) on the event table
    with pytest.raises(IntegrityError) as duplicate_op:
        await kernel_db.raw_insert_event(
            workspace_id=workspace.id,
            seq=created.seq + 100,
            entity_type=ENTITY_FOLDER,
            entity_id=uuid.uuid4(),
            op="create",
            rev=1,
            op_id=created.op_id,
        )
    assert_unique_violation(duplicate_op.value, UNIQUE_EVENTS_WS_OP)

    # 3. duplicate (workspace_id, op_id) on the ledger table
    with pytest.raises(IntegrityError) as duplicate_ledger:
        await kernel_db.raw_insert_ledger(
            workspace_id=workspace.id,
            op_id=created.op_id,
            request_hash_value=request_hash(make_command("folder_create", workspace.id, name="x")),
            result_seq=created.seq,
        )
    assert_unique_violation(duplicate_ledger.value, UNIQUE_LEDGER_PK)


async def test_raw_sql_proves_a_ledger_row_cannot_claim_a_missing_event(
    kernel_db: KernelDB, seed: object
) -> None:
    """``op_ledger.result_seq`` has an immediate FK: a phantom seq is rejected on INSERT."""

    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="phantom")

    with pytest.raises(IntegrityError) as caught:
        await kernel_db.raw_insert_ledger(
            workspace_id=workspace.id,
            op_id=command.op_id,
            request_hash_value=request_hash(command),
            result_seq=1,  # no change_events row at seq 1
        )
    assert sqlstate(caught.value) == "23503", (
        f"a ledger row must not claim a non-existent event: {caught.value}"
    )
    assert await kernel_db.ledger_for_op(workspace.id, command.op_id) is None

    # The same table also forbids two ledger rows sharing one result_seq.
    _, workspace2 = await seed()
    first = make_command("folder_create", workspace2.id, name="first")
    await kernel_db.raw_insert_event(
        workspace_id=workspace2.id,
        seq=1,
        entity_type=ENTITY_FOLDER,
        entity_id=first.entity_id,
        op="create",
        rev=1,
        op_id=first.op_id,
    )
    await kernel_db.raw_insert_ledger(
        workspace_id=workspace2.id,
        op_id=first.op_id,
        request_hash_value=request_hash(first),
        result_seq=1,
    )
    with pytest.raises(IntegrityError) as duplicate_result:
        await kernel_db.raw_insert_ledger(
            workspace_id=workspace2.id,
            op_id=new_op_id(),
            request_hash_value=request_hash(first),
            result_seq=1,
        )
    assert_unique_violation(duplicate_result.value, UNIQUE_LEDGER_SEQ)


async def test_raw_sql_proves_the_request_hash_length_check(
    kernel_db: KernelDB, seed: object
) -> None:
    """``octet_length(request_hash) = 32`` is enforced by a named CHECK."""
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="short hash")
    await kernel_db.raw_insert_event(
        workspace_id=workspace.id,
        seq=1,
        entity_type=ENTITY_FOLDER,
        entity_id=command.entity_id,
        op="create",
        rev=1,
        op_id=command.op_id,
    )

    with pytest.raises(IntegrityError) as caught:
        await kernel_db.raw_insert_ledger(
            workspace_id=workspace.id,
            op_id=command.op_id,
            request_hash_value=b"\x00" * 16,  # too short
            result_seq=1,
            allow_bad_hash=True,
        )
    assert sqlstate(caught.value) == "23514", (
        f"a short request_hash must violate the length CHECK: {caught.value}"
    )
    assert constraint_name(caught.value) == "ck_op_ledger_request_hash_len", (
        f"expected the request_hash length CHECK, got {constraint_name(caught.value)!r}"
    )


async def test_raw_sql_proves_the_deferred_object_event_fk(
    kernel_db: KernelDB, seed: object
) -> None:
    """An object row whose ``updated_seq`` has no event commits only if the event appears.

    Plan section 4: the ``(workspace_id, updated_seq) -> change_events`` FK is
    ``DEFERRABLE INITIALLY DEFERRED``, so the object may be written first. This proves the
    *deferral*: the same INSERT pair fails on COMMIT when the event is missing, and succeeds
    when it is present -- via raw SQL, so the service is not part of the argument.
    """
    _, workspace = await seed()

    async with kernel_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO folders "
                "(id, workspace_id, parent_id, name, rev, updated_seq, updated_at) "
                "VALUES (:id, :ws, NULL, 'deferred', 1, 1, now())"
            ),
            {"id": uuid.uuid4(), "ws": workspace.id},
        )
        # No event at seq 1 yet: the deferred FK must fail at COMMIT, not at INSERT.
        with pytest.raises(IntegrityError) as caught:
            await session.commit()
        await session.rollback()

    assert sqlstate(caught.value) == "23503", (
        f"the deferred object->event FK must reject an object without its event: {caught.value}"
    )
    assert constraint_name(caught.value) == "fk_folders_workspace_id_updated_seq_change_events", (
        f"expected the deferred updated_seq FK, got {constraint_name(caught.value)!r}"
    )

    # Now provide the event in the same transaction: the pair commits.
    folder_id = uuid.uuid4()
    op_id = new_op_id()
    async with kernel_db.session() as session:
        await session.execute(
            text(
                "INSERT INTO folders "
                "(id, workspace_id, parent_id, name, rev, updated_seq, updated_at) "
                "VALUES (:id, :ws, NULL, 'deferred ok', 1, 1, now())"
            ),
            {"id": folder_id, "ws": workspace.id},
        )
        await session.execute(
            text(
                "INSERT INTO change_events "
                "(workspace_id, seq, entity_type, entity_id, op, rev, op_id) "
                "VALUES (:ws, 1, 'folder', :id, 'create', 1, :op)"
            ),
            {"ws": workspace.id, "id": folder_id, "op": op_id},
        )
        await session.execute(
            text("UPDATE workspaces SET last_seq = 1 WHERE id = :ws"), {"ws": workspace.id}
        )
        await session.commit()

    row = await kernel_db.entity_row(workspace.id, ENTITY_FOLDER, folder_id)
    assert int(row["updated_seq"]) == 1
    assert await kernel_db.count_events(workspace.id) == 1
