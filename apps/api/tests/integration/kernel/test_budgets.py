"""Timeouts, cancellation and connection discard against a **real** PostgreSQL (P2 #1/#2/#5).

The unit tier proves the classification wiring against a fake session; only a server can
prove the SQLSTATEs are the ones PostgreSQL actually raises, that the budget really
bounds a statement, that a cancelled ``COMMIT`` is never reported as a rollback, and that
the connection which took part in it never returns to the pool.

What is proven here:

* ``test_statement_timeout_real_57014`` — a genuinely slow statement on a real backend is
  cancelled by ``statement_timeout`` and surfaces as ``WriteBusy``, leaving the
  quadruple untouched and the same command retryable;
* ``test_lock_timeout_metrics`` — a lock held past ``lock_timeout`` is ``WriteBusy`` with
  a **non-zero** ``lock_wait_ms`` (taken in ``finally``, so timeout, cancellation and
  missing-workspace paths all still record it), plus the success-path counterpart;
* ``test_cancel_before_commit_...`` / ``test_cancel_during_commit_...`` — the
  ``cancel_before_and_during_commit`` pair: a cancellation before ``COMMIT`` propagates
  after a real rollback; one *during* ``COMMIT`` propagates as ``CancelledError`` while
  the connection is invalidated, not recycled.

Scope note: a ``COMMIT`` that really stays in flight cannot be arranged portably inside
one test, so the acknowledgement delay is injected at the ``AsyncSession.commit`` seam -
the same seam the committed Phase 2 durability tests use. The transaction, the rows, the
``COMMIT`` and the invalidation are all real; only the delay is synthetic.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from kernel_race import await_waiters, raw_workspace_lock
from kernel_support import KernelDB, Quads, assert_quads_unchanged, expect, make_command
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import revnote_api.services.sync_write as service_module
from revnote_api.services.sync_repository import SyncRepository
from revnote_api.services.sync_write import (
    LOCK_TIMEOUT_MS,
    STATEMENT_TIMEOUT_MS,
    SyncWriteService,
)

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def metrics_seen(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[service_module._Metrics]]:
    """Capture the per-call diagnostics the service logs but never returns.

    ``lock_wait_ms`` is log-only by design (it is not part of ``ExecutedWrite``), so a
    test that must assert on it records the instance the service builds. This patches
    the constructor rather than adding a production accessor: the public API stays
    exactly as shipped, and the call under test goes through the real
    ``execute_with_outcome`` - including its error translation.
    """
    seen: list[service_module._Metrics] = []
    original = service_module._Metrics

    def recording() -> service_module._Metrics:
        metrics = original()
        seen.append(metrics)
        return metrics

    monkeypatch.setattr(service_module, "_Metrics", recording)
    yield seen


async def _measured(
    factory: async_sessionmaker[AsyncSession],
    command: Any,
    seen: list[service_module._Metrics],
    **budget: int,
) -> tuple[Any, service_module._Metrics]:
    try:
        outcome = await SyncWriteService(factory).execute_with_outcome(command, **budget)
    except BaseException as exc:
        return exc, seen[-1]
    return outcome, seen[-1]


async def _read_timeout_gucs(session: AsyncSession) -> dict[str, int]:
    """Read the two timeout GUCs back as **milliseconds**, as the server sees them.

    ``SHOW`` renders PostgreSQL's own normalisation (``5s`` for 5000 ms), which would
    make the assertion depend on presentation. ``current_setting(..., true)`` returns
    the same normalised text; ``pg_settings.setting`` is in the setting's canonical
    unit (ms here), so the comparison is against real numbers.
    """
    result = await session.execute(
        text(
            "SELECT name, setting::bigint FROM pg_settings "
            "WHERE name IN ('statement_timeout', 'lock_timeout')"
        )
    )
    return {str(name): int(value) for name, value in result.all()}


# --------------------------------------------------------------------------- #
# statement_timeout: the server really raises 57014
# --------------------------------------------------------------------------- #


async def test_statement_timeout_real_57014(
    kernel_db: KernelDB,
    seed: object,
    session_factory: async_sessionmaker[AsyncSession],
    metrics_seen: list[service_module._Metrics],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A statement the server cancels arrives as ``WriteBusy`` and writes nothing.

    The budget is injected at 120 ms - *below* the module default - and the workspace
    lock statement is preceded by a real ``pg_sleep``, so the server cancels inside the
    service's own transaction with ``57014``.

    Proven from the outside: a cold connection still sees no event, no ledger row, an
    unmoved cursor and no object; the identical command then applies cleanly under the
    default budget - i.e. "busy" really means "nothing happened, retry is safe".
    """
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="slow statement")
    before: Quads = await kernel_db.quads(workspace.id)

    real_lock = SyncRepository.lock_workspace

    async def sleeping_lock(self: SyncRepository, workspace_id: uuid.UUID) -> Any:
        # A real backend executes and aborts this statement.
        await self._session.execute(text("SELECT pg_sleep(5)"))
        return await real_lock(self, workspace_id)

    monkeypatch.setattr(SyncRepository, "lock_workspace", sleeping_lock)
    error, metrics = await _measured(
        session_factory,
        command,
        metrics_seen,
        lock_timeout_ms=100,
        statement_timeout_ms=120,
    )

    assert type(error).__name__ == "WriteBusy", (
        f"57014 must map to WriteBusy, got {type(error).__name__}: {error}"
    )
    # A cancelled lock wait still recorded a non-zero measurement (P2 #2).
    assert metrics.lock_wait_ms > 0.0, metrics.lock_wait_ms

    after: Quads = await kernel_db.quads(workspace.id)
    assert_quads_unchanged(before, after, context="statement_timeout")
    assert await kernel_db.entity_row_or_none(workspace.id, "folder", command.entity_id) is None, (
        "a statement-timeout write must not create the object"
    )

    # The documented recovery: the identical command, now with a normal budget.
    retried = await SyncWriteService(session_factory).execute(command)
    assert retried.seq == 1, f"the retry must apply cleanly, got {retried!r}"
    assert (await kernel_db.quads(workspace.id)).event_seqs == (1,)


def test_the_budgets_are_ordered_and_bounded() -> None:
    """D17 defaults: a real bound, lock strictly below statement."""
    assert isinstance(STATEMENT_TIMEOUT_MS, int)
    assert 0 < LOCK_TIMEOUT_MS < STATEMENT_TIMEOUT_MS


async def test_the_injected_budgets_are_real_server_side_settings(
    kernel_db: KernelDB,
    seed: object,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``SET LOCAL`` really changes the session GUCs the server enforces.

    Sending the text is not the contract; the contract is that PostgreSQL *obeys* it.
    Inside the service's own transaction the test reads ``SHOW statement_timeout`` /
    ``SHOW lock_timeout`` back off the same connection and compares them with the
    injected budget - so a statement that is emitted but never applied (wrong scope,
    wrong order, executed outside the transaction) fails here instead of silently
    leaving an unbounded write.
    """
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="real gucs")
    observed: dict[str, int] = {}

    real_lock = SyncRepository.lock_workspace

    async def probe_then_lock(self: SyncRepository, workspace_id: uuid.UUID) -> Any:
        observed.update(await _read_timeout_gucs(self._session))
        return await real_lock(self, workspace_id)

    monkeypatch.setattr(SyncRepository, "lock_workspace", probe_then_lock)
    outcome = await SyncWriteService(session_factory).execute_with_outcome(
        command, lock_timeout_ms=321, statement_timeout_ms=654
    )

    assert outcome.result.seq == 1
    assert observed == {"statement_timeout": 654, "lock_timeout": 321}, observed


async def test_the_defaults_are_also_applied_as_real_server_settings(
    kernel_db: KernelDB,
    seed: object,
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default path is bounded too - an unbounded default would be the worst case."""
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="default gucs")
    observed: dict[str, int] = {}

    real_lock = SyncRepository.lock_workspace

    async def probe_then_lock(self: SyncRepository, workspace_id: uuid.UUID) -> Any:
        observed.update(await _read_timeout_gucs(self._session))
        return await real_lock(self, workspace_id)

    monkeypatch.setattr(SyncRepository, "lock_workspace", probe_then_lock)
    await SyncWriteService(session_factory).execute(command)

    assert observed == {
        "statement_timeout": STATEMENT_TIMEOUT_MS,
        "lock_timeout": LOCK_TIMEOUT_MS,
    }, observed


# --------------------------------------------------------------------------- #
# lock_timeout: WriteBusy with a measured wait (P2 #2)
# --------------------------------------------------------------------------- #


@pytest.mark.concurrency
async def test_lock_timeout_metrics(
    kernel_db: KernelDB,
    seed: object,
    session_factory: async_sessionmaker[AsyncSession],
    metrics_seen: list[service_module._Metrics],
) -> None:
    """A lock timeout is ``WriteBusy`` **and** reports a non-zero ``lock_wait_ms``.

    The row is held from a raw connection; the injected ``lock_timeout`` is 500 ms so
    the case stays quick but the waiter is reliably observed through the server's lock
    graph, and the wait is measured by ``_lock_workspace``'s ``finally``. A zero here
    would hide exactly the condition the metric exists to show, which is what P2 #2
    forbids.
    """
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="lock waits")
    before = await kernel_db.quads(workspace.id)

    async with raw_workspace_lock(kernel_db, workspace.id) as (_holder, holder_pid):
        waiter = asyncio.ensure_future(
            _measured(
                session_factory,
                command,
                metrics_seen,
                lock_timeout_ms=500,
                statement_timeout_ms=5000,
            )
        )
        # The server's own lock graph, never a guessed sleep.
        assert await await_waiters(kernel_db, holder_pid), "the write never blocked on the lock"
        error, metrics = await asyncio.wait_for(waiter, timeout=30)

    assert type(error).__name__ == "WriteBusy", (
        f"a lock timeout must be WriteBusy, got {type(error).__name__}: {error}"
    )
    assert metrics.lock_wait_ms >= 100.0, (
        f"a WriteBusy timeout path must record the wait in finally, got {metrics.lock_wait_ms}"
    )
    assert metrics.category == "WriteBusy", metrics.category

    after = await kernel_db.quads(workspace.id)
    assert_quads_unchanged(before, after, context="lock_timeout")
    assert await kernel_db.entity_row_or_none(workspace.id, "folder", command.entity_id) is None, (
        "a timed-out write must not create the object"
    )


async def test_lock_wait_is_measured_on_the_success_path(
    kernel_db: KernelDB,
    seed: object,
    session_factory: async_sessionmaker[AsyncSession],
    metrics_seen: list[service_module._Metrics],
) -> None:
    """An uncontended acquisition still records a real (non-negative) wait."""
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="no contention")

    outcome, metrics = await _measured(session_factory, command, metrics_seen)

    assert outcome.result.seq == 1
    assert metrics.lock_wait_ms >= 0.0
    assert metrics.category == "committed", metrics.category


async def test_a_missing_workspace_still_records_the_wait(
    kernel_db: KernelDB,
    session_factory: async_sessionmaker[AsyncSession],
    metrics_seen: list[service_module._Metrics],
) -> None:
    """``WorkspaceNotFound`` is an exception branch; the metric is still taken."""
    command = make_command("folder_create", uuid.uuid4(), name="ghost")

    error, metrics = await _measured(session_factory, command, metrics_seen)

    assert type(error).__name__ == "WorkspaceNotFound", error
    assert metrics.lock_wait_ms >= 0.0


# --------------------------------------------------------------------------- #
# cancellation: before and during COMMIT (P2 #5)
# --------------------------------------------------------------------------- #


@pytest.mark.concurrency
async def test_cancel_before_commit_rolls_back_and_propagates(
    kernel_db: KernelDB, seed: object, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A cancellation after the ledger INSERT, before COMMIT, leaves nothing behind.

    The cancel is delivered by cancelling the *task* while the write is parked at the
    commit boundary, so the ``CancelledError`` genuinely arrives from the event loop and
    the ``COMMIT`` never reaches the server.
    """
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="cancelled before commit")
    before = await kernel_db.quads(workspace.id)

    gate = asyncio.Event()
    real_commit = AsyncSession.commit
    state = {"paused": 0}
    reached = asyncio.Event()

    async def commit_after_gate(session: AsyncSession) -> None:
        # Park at the last possible moment still *inside* the service's transaction.
        state["paused"] += 1
        reached.set()
        await gate.wait()
        await real_commit(session)

    AsyncSession.commit = commit_after_gate  # type: ignore[method-assign]
    try:
        task = asyncio.ensure_future(SyncWriteService(session_factory).execute(command))
        await asyncio.wait_for(reached.wait(), timeout=30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        gate.set()
        AsyncSession.commit = real_commit  # type: ignore[method-assign]

    after = await kernel_db.quads(workspace.id)
    assert_quads_unchanged(before, after, context="cancelled before COMMIT")
    assert await kernel_db.entity_row_or_none(workspace.id, "folder", command.entity_id) is None, (
        "a write cancelled before COMMIT must leave no object"
    )


@pytest.mark.concurrency
async def test_cancel_during_commit_is_cancelled_and_discards_the_connection(
    kernel_db: KernelDB, seed: object, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """D18: a cancellation while ``COMMIT`` is in flight stays a cancellation.

    The real ``COMMIT`` is issued and then the acknowledgement is replaced by a
    ``CancelledError``. The service must (a) let that ``CancelledError`` through
    untouched - never wrap it in a kernel error an HTTP adapter would map to a status -
    and (b) invalidate the connection instead of returning it to the pool. Whether the
    write is durable is unknowable here, which is exactly why the client retries the
    same ``op_id``.
    """
    _, workspace = await seed()
    command = make_command("folder_create", workspace.id, name="cancelled during commit")

    real_commit = AsyncSession.commit
    real_invalidate = AsyncSession.invalidate
    state = {"invalidated": 0, "committed": 0}

    async def commit_then_cancel(session: AsyncSession) -> None:
        await real_commit(session)  # the server has definitely committed
        state["committed"] += 1
        raise asyncio.CancelledError

    async def counting_invalidate(session: AsyncSession) -> None:
        state["invalidated"] += 1
        await real_invalidate(session)

    AsyncSession.commit = commit_then_cancel  # type: ignore[method-assign]
    AsyncSession.invalidate = counting_invalidate  # type: ignore[method-assign]
    try:
        with pytest.raises(asyncio.CancelledError) as caught:
            await SyncWriteService(session_factory).execute_with_outcome(command)
            # The test must never get here; a returned value means the cancel was lost.
            pytest.fail("the cancelled commit returned normally")
    finally:
        AsyncSession.commit = real_commit  # type: ignore[method-assign]
        AsyncSession.invalidate = real_invalidate  # type: ignore[method-assign]

    assert type(caught.value) is asyncio.CancelledError, (
        f"the cancellation must not be replaced by a kernel error: {type(caught.value).__name__}"
    )
    assert state["committed"] == 1, "the COMMIT must have reached the server"
    assert state["invalidated"] == 1, "a suspect connection must be invalidated, not pooled"

    # The row is durable, and the same op_id replays it rather than applying a second
    # event - the documented recovery (D18).
    replay = await SyncWriteService(session_factory).execute(command)
    assert replay.seq == 1
    assert (await kernel_db.quads(workspace.id)).event_seqs == (1,)


async def test_a_lost_commit_acknowledgement_discards_and_leaves_the_factory_usable(
    kernel_db: KernelDB, seed: object, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """An unknown outcome invalidates its connection; the next write still works."""
    _, workspace = await seed()
    first = make_command("folder_create", workspace.id, name="ack lost")
    second = make_command("folder_create", workspace.id, name="after discard")

    real_commit = AsyncSession.commit
    real_invalidate = AsyncSession.invalidate
    state = {"invalidated": 0}

    async def commit_then_lose_ack(session: AsyncSession) -> None:
        await real_commit(session)
        raise ConnectionResetError("acknowledgement lost")

    async def counting_invalidate(session: AsyncSession) -> None:
        state["invalidated"] += 1
        await real_invalidate(session)

    AsyncSession.commit = commit_then_lose_ack  # type: ignore[method-assign]
    AsyncSession.invalidate = counting_invalidate  # type: ignore[method-assign]
    try:
        with expect("CommitOutcomeUnknown"):
            await SyncWriteService(session_factory).execute(first)
    finally:
        AsyncSession.commit = real_commit  # type: ignore[method-assign]
        AsyncSession.invalidate = real_invalidate  # type: ignore[method-assign]

    assert state["invalidated"] == 1, "a lost acknowledgement must discard the connection"

    outcome = await SyncWriteService(session_factory).execute(second)
    assert outcome.seq == 2, outcome
    assert (await kernel_db.quads(workspace.id)).event_seqs == (1, 2)
