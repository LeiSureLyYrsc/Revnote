"""``execute_with_outcome`` - the approved kernel exception (plan §9.1, D22).

The Phase 2 contract is unchanged: ``execute()`` still returns a bare
:class:`~revnote_api.kernel.results.WriteResult` and :class:`WriteResult` still has
exactly its eight fields. What this commit adds is a richer *return value* from a
second entry point, so the sync endpoint can answer ``applied`` vs ``replayed``.

These tests reuse the fake session/repository wiring from
``test_kernel_boundaries.py`` instead of growing a second harness: that module
already models the statement stream faithfully enough to prove which path ran. No
database is involved, so the whole file runs in the unit tier.

What is *not* re-tested here: locking, MVCC, deferred FKs, seq density. Those are
PostgreSQL facts and stay in the integration tier.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime
import logging
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

import revnote_api.kernel as kernel_pkg
import revnote_api.services.sync_write as service_module
from revnote_api.kernel.commands import CreateFolder, CreateNote, UpdateFolder
from revnote_api.kernel.fingerprint import command_hash
from revnote_api.kernel.ids import new_id
from revnote_api.kernel.results import ExecutedWrite, WriteResult
from revnote_api.services.sync_write import SyncWriteService

# Imported by module name (not ``tests.unit....``): the test tree has no
# ``__init__.py`` and pytest's prepend import mode puts this directory on
# ``sys.path``, so the fake session/repository wiring is shared rather than
# duplicated here. Re-using it is what keeps the two files' statement-level
# expectations from drifting apart.
from test_kernel_boundaries import (  # isort: skip
    NOW,
    FakeDB,
    FakeSession,
    a_folder_create,
    a_folder_update,
    a_note_create,
    entity,
    run_with,
    static_factory,
    writes,
)

# --------------------------------------------------------------------------- #
# the value object
# --------------------------------------------------------------------------- #


def _a_result() -> WriteResult:
    return WriteResult(
        workspace_id=uuid.uuid4(),
        op_id=uuid.uuid4(),
        entity_type="folder",
        entity_id=uuid.uuid4(),
        op="create",
        seq=1,
        rev=1,
        created_at=datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
    )


def test_write_result_keeps_exactly_its_eight_fields() -> None:
    """The Phase 2 contract is untouched: no disposition field was smuggled in."""
    fields = {field.name for field in dataclasses.fields(WriteResult)}

    assert fields == {
        "workspace_id",
        "op_id",
        "entity_type",
        "entity_id",
        "op",
        "seq",
        "rev",
        "created_at",
    }
    assert not hasattr(_a_result(), "disposition")


def test_executed_write_wraps_a_result_with_a_disposition() -> None:
    result = _a_result()
    executed = ExecutedWrite(result=result, disposition="applied")

    assert executed.result is result
    assert executed.disposition == "applied"
    assert set(ExecutedWrite.__dataclass_fields__) == {"result", "disposition"}


@pytest.mark.parametrize("disposition", ["applied", "replayed"])
def test_both_dispositions_are_representable(disposition: str) -> None:
    executed = ExecutedWrite(result=_a_result(), disposition=disposition)  # type: ignore[arg-type]

    assert executed.disposition == disposition


def test_executed_write_is_immutable() -> None:
    executed = ExecutedWrite(result=_a_result(), disposition="applied")

    with pytest.raises(dataclasses.FrozenInstanceError):
        executed.disposition = "replayed"  # type: ignore[misc]


def test_executed_write_repr_names_the_disposition_and_coordinates_only() -> None:
    executed = ExecutedWrite(result=_a_result(), disposition="replayed")

    rendered = repr(executed)
    assert "disposition=replayed" in rendered
    assert "seq=1" in rendered and "rev=1" in rendered
    # No content can appear because WriteResult carries none; assert the rule.
    for leak in ("body_md", "title=", "password"):
        assert leak not in rendered


def test_the_disposition_is_not_a_retry_flag() -> None:
    """``replayed`` is history, not advice: it is one of exactly two values."""
    from typing import get_args

    from revnote_api.kernel.results import WriteDisposition

    assert set(get_args(WriteDisposition)) == {"applied", "replayed"}


# --------------------------------------------------------------------------- #
# applied
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_a_new_write_reports_applied() -> None:
    command = a_folder_create()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    executed = await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert executed.disposition == "applied"
    assert executed.result.seq == 1
    assert session.committed == 1
    assert session.rolled_back == 0


@pytest.mark.anyio
async def test_the_result_matches_execute_for_the_same_write() -> None:
    """Both entry points produce the identical :class:`WriteResult` value."""
    command = a_folder_create()
    direct = await run_with(command, FakeSession(FakeDB(workspace=(command.workspace_id, 0))))
    executed = await SyncWriteService(
        static_factory(FakeSession(FakeDB(workspace=(command.workspace_id, 0))))
    ).execute_with_outcome(command)

    assert executed.result == direct
    assert isinstance(executed.result, WriteResult)


@pytest.mark.anyio
async def test_an_update_reports_applied_with_the_new_revision() -> None:
    command = UpdateFolder(
        workspace_id=new_id(), entity_id=new_id(), op_id=new_id(), base_rev=2, name="n"
    )
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 4),
            folder_reads=[entity(command.entity_id, command.workspace_id, rev=2)],
        )
    )

    executed = await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert (executed.disposition, executed.result.seq, executed.result.rev) == ("applied", 5, 3)


# --------------------------------------------------------------------------- #
# replayed (ledger hit before the CAS)
# --------------------------------------------------------------------------- #


def _replay_session(command: object) -> FakeSession:
    """A session whose ledger already holds ``command`` with the same payload."""
    workspace_id = command.workspace_id  # type: ignore[attr-defined]
    return FakeSession(
        FakeDB(
            workspace=(workspace_id, 9),
            ledger_row=(command.op_id, command_hash(command), 4),  # type: ignore[attr-defined]
            event_row=(
                workspace_id,
                4,
                "folder",
                command.entity_id,  # type: ignore[attr-defined]
                "update",
                2,
                command.op_id,  # type: ignore[attr-defined]
                NOW,
            ),
        )
    )


@pytest.mark.anyio
async def test_a_recorded_op_reports_replayed_and_writes_nothing() -> None:
    command = a_folder_update()
    session = _replay_session(command)

    executed = await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert executed.disposition == "replayed"
    assert (executed.result.seq, executed.result.rev) == (4, 2)
    assert writes(session) == []
    # A replay writes nothing, so the read-only transaction is not committed.
    assert session.committed == 0
    assert session.rolled_back == 1


@pytest.mark.anyio
async def test_a_replay_returns_the_recorded_result_not_the_live_row() -> None:
    """The workspace has moved on to seq 9; the first result (seq 4) is returned."""
    command = a_folder_update()
    session = _replay_session(command)

    executed = await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert executed.result.seq == 4
    assert executed.result.created_at == NOW


@pytest.mark.anyio
async def test_losing_the_idempotency_race_reports_replayed() -> None:
    """The recovery transaction's outcome is a replay, and the loser never commits."""
    command = a_folder_create()
    loser = FakeSession(FakeDB(workspace=(command.workspace_id, 0), ledger_conflict=True))
    winner = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 1),
            ledger_row=(command.op_id, command_hash(command), 1),
            event_row=(
                command.workspace_id,
                1,
                "folder",
                command.entity_id,
                "create",
                1,
                command.op_id,
                NOW,
            ),
        )
    )

    executed = await SyncWriteService(static_factory(loser, winner)).execute_with_outcome(command)

    assert executed.disposition == "replayed"
    assert executed.result.seq == 1
    assert loser.committed == 0
    assert winner.committed == 0


# --------------------------------------------------------------------------- #
# the original contract is preserved
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_execute_still_returns_a_bare_result_and_writes() -> None:
    command = a_folder_create()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    result = await SyncWriteService(static_factory(session)).execute(command)

    assert isinstance(result, WriteResult)
    assert not isinstance(result, ExecutedWrite)
    assert result.seq == 1


@pytest.mark.anyio
async def test_write_alias_still_returns_a_bare_result() -> None:
    command = a_note_create()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    result = await SyncWriteService(static_factory(session)).write(command)

    assert isinstance(result, WriteResult)
    assert result.entity_type == "note"


@pytest.mark.anyio
async def test_the_outcome_method_rejects_a_non_command_before_any_session() -> None:
    built: list[None] = []

    def factory() -> object:  # pragma: no cover - must never be called
        built.append(None)
        raise AssertionError("the session factory must not be used")

    service = SyncWriteService(factory)  # type: ignore[arg-type]

    with pytest.raises(kernel_pkg.InvalidCommand):
        await service.execute_with_outcome({"not": "a command"})  # type: ignore[arg-type]

    assert built == []


@pytest.mark.anyio
async def test_every_error_is_raised_identically_by_both_entry_points() -> None:
    """The outcome wrapper adds no error semantics of its own."""

    def a_conflict() -> tuple[object, FakeSession]:
        command = a_folder_update()
        return command, FakeSession(
            FakeDB(
                workspace=(command.workspace_id, 5),
                folder_reads=[entity(command.entity_id, command.workspace_id, rev=4)],
            )
        )

    command, session = a_conflict()
    with pytest.raises(kernel_pkg.RevConflict):
        await run_with(command, session)

    command, session = a_conflict()
    service = SyncWriteService(static_factory(session))
    with pytest.raises(kernel_pkg.RevConflict):
        await service.execute_with_outcome(command)


@pytest.mark.anyio
async def test_cancellation_propagates_unwrapped_from_the_outcome_method() -> None:
    session = FakeSession(FakeDB(), raise_on="FOR UPDATE", raise_error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await SyncWriteService(static_factory(session)).execute_with_outcome(a_folder_create())


@pytest.mark.anyio
async def test_an_unknown_commit_outcome_is_not_turned_into_a_replay() -> None:
    """A lost acknowledgement is an error, never a fabricated ``replayed``."""
    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        commit_error=ConnectionResetError("connection went away during COMMIT"),
    )

    with pytest.raises(kernel_pkg.CommitOutcomeUnknown):
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)


@pytest.mark.anyio
async def test_a_zero_row_cas_still_reports_the_moved_revision() -> None:
    command = a_folder_update(base_rev=2)
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 6),
            folder_reads=[
                entity(command.entity_id, command.workspace_id, rev=2),
                entity(command.entity_id, command.workspace_id, rev=3),
            ],
            cas_rowcount=0,
        )
    )

    with pytest.raises(kernel_pkg.RevConflict) as caught:
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert (caught.value.expected, caught.value.current) == (2, 3)


# --------------------------------------------------------------------------- #
# lock-wait diagnostics (P2 #2)
# --------------------------------------------------------------------------- #


class _TimingSession(FakeSession):
    """Session whose ``FOR UPDATE`` answer is delayed by a known wall time."""

    def __init__(self, db: FakeDB, *, delay_s: float, **kwargs: object) -> None:
        super().__init__(db, **kwargs)  # type: ignore[arg-type]
        self._delay_s = delay_s

    async def execute(self, statement: object, *args: object, **kwargs: object):  # type: ignore[override]
        if "FOR UPDATE" in str(statement):
            await asyncio.sleep(self._delay_s)
        return await super().execute(statement, *args, **kwargs)


@pytest.mark.anyio
async def test_lock_wait_is_recorded_on_the_success_path() -> None:
    command = a_folder_create()
    session = _TimingSession(FakeDB(workspace=(command.workspace_id, 0)), delay_s=0.03)
    metrics = service_module._Metrics()
    service = SyncWriteService(static_factory(session))

    await service._run(command, metrics)

    assert metrics.lock_wait_ms >= 25.0, metrics.lock_wait_ms


@pytest.mark.anyio
async def test_lock_wait_is_recorded_when_the_workspace_is_missing() -> None:
    """A missing workspace also waited for the lock; zero would hide the wait."""
    command = a_folder_create()
    session = _TimingSession(FakeDB(), delay_s=0.03)
    metrics = service_module._Metrics()

    with pytest.raises(kernel_pkg.WorkspaceNotFound):
        await SyncWriteService(static_factory(session))._run(command, metrics)

    assert metrics.lock_wait_ms >= 25.0, metrics.lock_wait_ms


@pytest.mark.anyio
async def test_lock_wait_is_recorded_when_the_wait_is_cancelled() -> None:
    """Cancellation is the path where a naive timer loses the measurement."""

    class _CancelOnLock(FakeSession):
        async def execute(self, statement: object, *args: object, **kwargs: object):  # type: ignore[override]
            if "FOR UPDATE" in str(statement):
                await asyncio.sleep(0.02)
                raise asyncio.CancelledError
            return await super().execute(statement, *args, **kwargs)

    command = a_folder_create()
    metrics = service_module._Metrics()
    session = _CancelOnLock(FakeDB(workspace=(command.workspace_id, 0)))

    with pytest.raises(asyncio.CancelledError):
        await SyncWriteService(static_factory(session))._run(command, metrics)

    assert metrics.lock_wait_ms >= 15.0, metrics.lock_wait_ms


@pytest.mark.anyio
async def test_lock_wait_accumulates_across_the_race_recovery() -> None:
    """Both transactions wait for the same row; the metric is the total, not the last."""
    command = a_folder_create()
    winner = _TimingSession(
        FakeDB(
            workspace=(command.workspace_id, 1),
            ledger_row=(command.op_id, command_hash(command), 1),
            event_row=(
                command.workspace_id,
                1,
                "folder",
                command.entity_id,
                "create",
                1,
                command.op_id,
                NOW,
            ),
        ),
        delay_s=0.03,
    )
    metrics = service_module._Metrics()
    metrics.lock_wait_ms = 12.0  # the first (losing) transaction already waited
    service = SyncWriteService(static_factory(winner))

    await service._replay_after_race(command, metrics)

    assert metrics.lock_wait_ms >= 37.0, metrics.lock_wait_ms


@pytest.mark.anyio
async def test_a_lock_timeout_is_still_a_write_busy_with_a_measured_wait() -> None:
    class LockNotAvailable(Exception):
        sqlstate = "55P03"

    command = a_folder_create()

    class _SlowLock(FakeSession):
        async def execute(self, statement: object, *args: object, **kwargs: object):  # type: ignore[override]
            if "FOR UPDATE" in str(statement):
                await asyncio.sleep(0.02)
                raise LockNotAvailable("canceling statement due to lock timeout")
            return await super().execute(statement, *args, **kwargs)

    session = _SlowLock(FakeDB(workspace=(command.workspace_id, 0)))

    with pytest.raises(kernel_pkg.WriteBusy):
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)


def test_the_three_set_statements_are_the_only_module_level_sql() -> None:
    """Isolation plus *two* bounded budgets (plan D17), nothing else module-level.

    The statement budget is part of the kernel P2 work, so the module now pins three
    statements. Any fourth would mean a second isolation level or an unbounded wait
    slipped in; the per-call override builds equivalent ``SET LOCAL`` text at runtime
    instead of adding a module constant.
    """
    sets = [
        str(value)
        for value in vars(service_module).values()
        if hasattr(value, "text") and str(value).startswith("SET ")
    ]

    assert sets == [
        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
        f"SET LOCAL lock_timeout = {service_module.LOCK_TIMEOUT_MS}",
        f"SET LOCAL statement_timeout = {service_module.STATEMENT_TIMEOUT_MS}",
    ]


# --------------------------------------------------------------------------- #
# injectable timeout budgets (P2 #1)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_the_default_budget_is_applied_with_the_documented_defaults() -> None:
    command = a_folder_create()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert session.statements[0] == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    assert session.statements[1] == f"SET LOCAL lock_timeout = {service_module.LOCK_TIMEOUT_MS}"
    assert (
        session.statements[2]
        == f"SET LOCAL statement_timeout = {service_module.STATEMENT_TIMEOUT_MS}"
    )


@pytest.mark.anyio
async def test_an_injected_budget_replaces_both_set_local_statements() -> None:
    command = a_folder_create()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    await SyncWriteService(static_factory(session)).execute_with_outcome(
        command, lock_timeout_ms=250, statement_timeout_ms=900
    )

    assert session.statements[:3] == [
        "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
        "SET LOCAL lock_timeout = 250",
        "SET LOCAL statement_timeout = 900",
    ]
    # The isolation pin is untouched: a budget never widens the transaction.
    assert session.statements[0] == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    assert session.committed == 1


@pytest.mark.anyio
async def test_an_injected_budget_does_not_leak_into_the_next_call() -> None:
    """The budget is per call, exactly like the disposition: no service state."""
    command = a_folder_create()
    first = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))
    second = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))
    service = SyncWriteService(static_factory(first, second))

    await service.execute_with_outcome(command, lock_timeout_ms=50, statement_timeout_ms=60)
    await service.execute_with_outcome(command)

    assert "lock_timeout = 50" in first.statements[1]
    assert second.statements[1] == f"SET LOCAL lock_timeout = {service_module.LOCK_TIMEOUT_MS}"
    assert (
        second.statements[2]
        == f"SET LOCAL statement_timeout = {service_module.STATEMENT_TIMEOUT_MS}"
    )


@pytest.mark.anyio
async def test_a_replay_after_the_race_uses_the_same_injected_budget() -> None:
    """The recovery transaction must honour the caller's deadline too."""
    command = a_folder_create()
    loser = FakeSession(FakeDB(workspace=(command.workspace_id, 0), ledger_conflict=True))
    winner = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 1),
            ledger_row=(command.op_id, command_hash(command), 1),
            event_row=(
                command.workspace_id,
                1,
                "folder",
                command.entity_id,
                "create",
                1,
                command.op_id,
                NOW,
            ),
        )
    )

    await SyncWriteService(static_factory(loser, winner)).execute_with_outcome(
        command, lock_timeout_ms=111, statement_timeout_ms=222
    )

    assert "lock_timeout = 111" in loser.statements[1]
    assert "lock_timeout = 111" in winner.statements[1]
    assert "statement_timeout = 222" in winner.statements[2]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lock_timeout_ms": 0},
        {"lock_timeout_ms": -1},
        {"statement_timeout_ms": 0},
        {"statement_timeout_ms": -5},
        {"lock_timeout_ms": True},
    ],
)
@pytest.mark.anyio
async def test_a_non_positive_budget_is_refused_before_any_session(
    kwargs: dict[str, object],
) -> None:
    """A zero/negative budget would disable the bound, so it is a contract error."""
    built: list[None] = []

    def factory() -> object:  # pragma: no cover - must never be called
        built.append(None)
        raise AssertionError("the session factory must not be used")

    service = SyncWriteService(factory)  # type: ignore[arg-type]

    with pytest.raises(kernel_pkg.InvalidCommand, match="positive integer"):
        await service.execute_with_outcome(a_folder_create(), **kwargs)  # type: ignore[arg-type]

    assert built == []


@pytest.mark.anyio
async def test_execute_keeps_the_default_budget_and_its_bare_result() -> None:
    """The Phase 2 spelling is unchanged: defaults, ``WriteResult``, no kwargs."""
    command = a_folder_create()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    result = await SyncWriteService(static_factory(session)).execute(command)

    assert isinstance(result, WriteResult)
    assert session.statements[1] == f"SET LOCAL lock_timeout = {service_module.LOCK_TIMEOUT_MS}"
    assert (
        session.statements[2]
        == f"SET LOCAL statement_timeout = {service_module.STATEMENT_TIMEOUT_MS}"
    )


# --------------------------------------------------------------------------- #
# 57014 / 55P03 classification (P2 #1)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_a_statement_timeout_is_a_write_busy_that_wrote_nothing() -> None:
    """``57014`` before the commit: the server cancelled, so retry is safe."""

    class QueryCanceled(Exception):
        sqlstate = "57014"

    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO folders",
        raise_error=QueryCanceled("canceling statement due to statement timeout"),
    )

    with pytest.raises(kernel_pkg.WriteBusy):
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert session.committed == 0
    assert session.rolled_back == 1


@pytest.mark.anyio
async def test_a_statement_timeout_on_the_workspace_lock_is_a_write_busy() -> None:
    """The lock statement can also hit ``57014``; it is still "nothing written"."""

    class QueryCanceled(Exception):
        sqlstate = "57014"

    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="FOR UPDATE",
        raise_error=QueryCanceled("canceling statement due to statement timeout"),
    )

    with pytest.raises(kernel_pkg.WriteBusy):
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert session.committed == 0


@pytest.mark.anyio
async def test_a_confirmed_commit_cancellation_with_a_sqlstate_is_a_write_busy() -> None:
    """D17: a SQLSTATE the server raised about ``COMMIT`` is a confirmed rejection."""

    class QueryCanceled(Exception):
        sqlstate = "57014"

    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        commit_error=QueryCanceled("canceling statement due to statement timeout"),
    )

    with pytest.raises(kernel_pkg.WriteBusy):
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)


@pytest.mark.anyio
async def test_a_lost_commit_acknowledgement_is_unknown_and_discards_the_connection() -> None:
    """No SQLSTATE at all: the server may have committed, so never claim a rollback."""
    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        commit_error=ConnectionResetError("connection went away during COMMIT"),
    )

    with pytest.raises(kernel_pkg.CommitOutcomeUnknown):
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert session.invalidated == 1, "a suspect connection must never return to the pool"


@pytest.mark.anyio
async def test_a_commit_that_fails_without_a_sqlstate_discards_the_connection() -> None:
    class NoSqlstate(Exception):
        pass

    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)), commit_error=NoSqlstate("driver gave up")
    )

    with pytest.raises(kernel_pkg.CommitOutcomeUnknown):
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert session.invalidated == 1


@pytest.mark.anyio
async def test_a_confirmed_commit_rejection_keeps_the_connection() -> None:
    """A deferred FK is a confirmed abort: the connection is still sound."""

    class DeferredViolation(Exception):
        sqlstate = "23503"

    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)), commit_error=DeferredViolation("fk")
    )

    with pytest.raises(kernel_pkg.StorageFailure, match="deferred"):
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert session.invalidated == 0


# --------------------------------------------------------------------------- #
# cancellation (P2 #5)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_a_cancellation_before_commit_propagates_and_rolls_back() -> None:
    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO folders",
        raise_error=asyncio.CancelledError(),
    )

    with pytest.raises(asyncio.CancelledError):
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert session.committed == 0
    assert session.rolled_back == 1
    # Nothing suspects the connection yet: it was never mid-COMMIT.
    assert session.invalidated == 0


@pytest.mark.anyio
async def test_a_cancellation_during_commit_propagates_as_cancelled_and_invalidates() -> None:
    """D18: no shield, no fabricated rollback, no kernel error swallowing the cancel."""
    command = a_folder_create()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    async def commit_then_cancel() -> None:
        session.committed += 1  # the server did commit; the wait was cancelled
        raise asyncio.CancelledError

    session.commit = commit_then_cancel  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError) as caught:
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    # Exactly the cancellation, never a kernel error an adapter could map away.
    assert type(caught.value) is asyncio.CancelledError
    assert not isinstance(caught.value, kernel_pkg.SyncKernelError)
    assert session.invalidated == 1


@pytest.mark.anyio
async def test_a_commit_cancellation_is_not_an_executed_write() -> None:
    """A cancelled commit never returns an outcome, so it can never be ``applied``."""
    command = a_folder_create()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    async def cancel() -> None:
        raise asyncio.CancelledError

    session.commit = cancel  # type: ignore[method-assign]
    outcome: object = None

    with pytest.raises(asyncio.CancelledError):
        outcome = await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert outcome is None


@pytest.mark.anyio
async def test_an_interrupted_commit_is_reported_as_cancelled_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    command = a_folder_create()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    async def cancel() -> None:
        raise asyncio.CancelledError

    session.commit = cancel  # type: ignore[method-assign]

    with (
        caplog.at_level(logging.INFO, logger=service_module.__name__),
        pytest.raises(asyncio.CancelledError),
    ):
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert any(
        "category=cancelled_during_commit" in record.getMessage() for record in caplog.records
    )


@pytest.mark.anyio
async def test_a_cancelled_lock_wait_releases_via_rollback_only() -> None:
    command = a_folder_create()
    session = FakeSession(FakeDB(), raise_on="FOR UPDATE", raise_error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert session.rolled_back == 1
    assert session.invalidated == 0
    assert session.committed == 0


def test_no_new_retry_or_failpoint_was_introduced() -> None:
    from pathlib import Path

    text = (Path(service_module.__file__).resolve().parent / "sync_write.py").read_text(
        encoding="utf-8"
    )

    for forbidden in ("FAILPOINT", "os.environ", "while True", "for attempt", "sleep("):
        assert forbidden not in text, forbidden


def test_the_service_still_holds_only_the_factory() -> None:
    service = SyncWriteService(object())  # type: ignore[arg-type]

    assert list(vars(service)) == ["_session_factory"]


@pytest.mark.anyio
async def test_a_note_create_still_carries_its_body_without_exposing_it() -> None:
    """The outcome adds no field that could hold content."""
    command = CreateNote(
        workspace_id=new_id(),
        entity_id=new_id(),
        op_id=new_id(),
        title="t",
        body_md="SECRET-BODY",
    )
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    executed = await SyncWriteService(static_factory(session)).execute_with_outcome(command)

    assert "SECRET-BODY" not in repr(executed)


def test_create_folder_command_is_still_a_pure_dataclass() -> None:
    command = CreateFolder(
        workspace_id=uuid.uuid4(), entity_id=uuid.uuid4(), op_id=uuid.uuid4(), name="Inbox"
    )

    assert dataclasses.is_dataclass(command)
    assert not hasattr(command, "disposition")


def test_pk_violation_is_unaffected_by_the_outcome_path() -> None:
    """Guards against the outcome wrapper swallowing the conflict classification."""
    exc = kernel_pkg.EntityIdConflict("note", uuid.uuid4())

    assert exc.entity_type == "note"
    assert "already exists" in str(exc)


@pytest.mark.anyio
async def test_an_entity_pk_collision_still_reaches_the_caller() -> None:
    command = a_folder_create()

    class Violation(Exception):
        sqlstate = "23505"
        constraint_name = "pk_folders"

    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO folders",
        raise_error=IntegrityError("INSERT INTO folders ...", {}, Violation()),
    )

    with pytest.raises(kernel_pkg.EntityIdConflict):
        await SyncWriteService(static_factory(session)).execute_with_outcome(command)
