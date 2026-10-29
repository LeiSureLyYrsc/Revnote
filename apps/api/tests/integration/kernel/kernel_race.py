"""Race orchestration and constraint assertions for the concurrent kernel cases.

Imported only by the two commit-3 modules (``test_concurrency.py``, ``test_atomicity.py``):
a single-connection test needs no barrier, no ``pg_blocking_pids`` wait and no
constraint-violation assertion, so they stay out of the commit-2 helper.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Any

import pytest
from kernel_support import KernelDB, Quads, make_command, sqlstate
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from revnote_api.kernel import errors as kernel_errors


def service_writer(session_factory: Any) -> Callable[[Any], Awaitable[Any]]:
    """A standalone ``async (command) -> WriteResult`` over its **own** service instance.

    Races and retries must not share a service object: plan section 6 gives every call an
    independent ``AsyncSession``, and a shared instance would make a green test say nothing
    about that promise.
    """
    from kernel_service import SyncWriteService

    service = SyncWriteService(session_factory)

    async def perform(command: Any) -> Any:
        return await service.execute(command)

    return perform


LOCK_WAIT_TIMEOUT_SECONDS = 20.0
RACE_TIMEOUT_SECONDS = 120.0
DENSE_SEQ_PRODUCERS = 8
DENSE_SEQ_PER_PRODUCER = 20
DENSE_SEQ_TOTAL = DENSE_SEQ_PRODUCERS * DENSE_SEQ_PER_PRODUCER
MAX_BIGINT = 2**63 - 1
BEYOND_FLOAT_EXACT = 2**53 + 1
UNIQUE_EVENTS_WS_SEQ = "change_events(workspace_id,seq)"
UNIQUE_EVENTS_WS_OP = "change_events(workspace_id,op_id)"
UNIQUE_LEDGER_PK = "op_ledger(workspace_id,op_id)"
UNIQUE_LEDGER_SEQ = "op_ledger(workspace_id,result_seq)"
_CONSTRAINT_NAMES: dict[str, str] = {
    UNIQUE_EVENTS_WS_SEQ: "pk_change_events",
    UNIQUE_EVENTS_WS_OP: "uq_change_events_workspace_id_op_id",
    UNIQUE_LEDGER_PK: "pk_op_ledger",
    UNIQUE_LEDGER_SEQ: "uq_op_ledger_workspace_id_result_seq",
}


class RaceTimeout(pytest.fail.Exception):
    """A bounded wait that never completed: a failure, never a hang."""


@dataclasses.dataclass(slots=True)
class Outcome:
    """One racer's result — a value or an error, never silently swallowed."""

    index: int
    label: str
    value: Any = None
    error: BaseException | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    def describe(self) -> str:
        if self.error is not None:
            return (
                f"[{self.index}:{self.label}] raised {error_name(self.error)} "
                f"({type(self.error).__name__}: {self.error})"
            )
        return f"[{self.index}:{self.label}] -> {self.value!r}"


Racer = Callable[[asyncio.Barrier, int], Awaitable[Any]]


def error_name(exc: BaseException) -> str:
    """The contract error's own class name."""
    base: type[BaseException] = kernel_errors.SyncKernelError
    for cls in type(exc).__mro__:
        if issubclass(cls, base) and cls is not base:
            return cls.__name__
    return type(exc).__name__


async def run_race(
    racers: Sequence[Racer],
    *,
    labels: Sequence[str] | None = None,
    budget: float = RACE_TIMEOUT_SECONDS,
) -> list[Outcome]:
    """Start every racer behind one barrier; return all values *and* errors.

    A racer must take its session/connection before ``await barrier.wait()`` (see
    :meth:`KernelDB.hold`), so a ``NullPool`` ceiling cannot stall the race.
    """
    names = list(labels) if labels else [str(index) for index in range(len(racers))]
    barrier = asyncio.Barrier(len(racers))

    async def runner(index: int, racer: Racer) -> Outcome:
        try:
            return Outcome(index=index, label=names[index], value=await racer(barrier, index))
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:
            return Outcome(index=index, label=names[index], error=exc)

    try:
        outcomes = await asyncio.wait_for(
            asyncio.gather(*(runner(index, racer) for index, racer in enumerate(racers))),
            timeout=budget,
        )
    except TimeoutError as exc:
        raise RaceTimeout(f"race of {len(racers)} tasks exceeded {budget}s") from exc
    return list(outcomes)


def describe_race(outcomes: Sequence[Outcome]) -> str:
    return "\n".join(f"    {outcome.describe()}" for outcome in outcomes)


def winners(outcomes: Sequence[Outcome]) -> list[Outcome]:
    return [outcome for outcome in outcomes if outcome.ok]


def losers(outcomes: Sequence[Outcome]) -> list[Outcome]:
    return [outcome for outcome in outcomes if outcome.error is not None]


def error_names(outcomes: Sequence[Outcome]) -> list[str]:
    return [error_name(outcome.error) for outcome in losers(outcomes) if outcome.error is not None]


def only(outcomes: Sequence[Outcome]) -> Outcome:
    """The single success; a wrong count prints the whole race."""
    succeeded = winners(outcomes)
    if len(succeeded) != 1:
        raise AssertionError(
            f"expected exactly one successful write, got {len(succeeded)} of "
            f"{len(outcomes)}:\n{describe_race(outcomes)}"
        )
    return succeeded[0]


async def _waiters_of(db: KernelDB, pid: int) -> tuple[int, ...]:
    """Sessions currently blocked by *pid*, straight from the server's lock graph."""
    rows = await db.rows(
        "SELECT pid FROM pg_stat_activity "
        "WHERE datname = current_database() AND :pid = any(pg_blocking_pids(pid))",
        pid=pid,
    )
    return tuple(int(row["pid"]) for row in rows)


async def _blocked_report(db: KernelDB) -> list[dict[str, Any]]:
    """Every waiting session in this database, for diagnostics when a wait times out."""
    return await db.rows(
        "SELECT pid, state, wait_event_type, wait_event, pg_blocking_pids(pid) AS blockers "
        "FROM pg_stat_activity WHERE datname = current_database() "
        "AND cardinality(pg_blocking_pids(pid)) > 0 ORDER BY pid"
    )


async def await_waiters(
    db: KernelDB, blocker_pid: int, *, budget: float = LOCK_WAIT_TIMEOUT_SECONDS
) -> tuple[int, ...]:
    """Wait until something is blocked *by* ``blocker_pid``.

    The proof is the server's lock graph, never a guessed sleep; a timeout means the write
    did not wait where the plan requires it to.
    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        waiters = await _waiters_of(db, blocker_pid)
        if waiters:
            return waiters
        await asyncio.sleep(0.02)
    raise RaceTimeout(
        f"nothing was blocked by pid {blocker_pid} within {budget}s. "
        f"Lock graph: {await _blocked_report(db)}"
    )


@contextlib.asynccontextmanager
async def raw_workspace_lock(
    db: KernelDB, workspace_id: uuid.UUID
) -> AsyncIterator[tuple[AsyncSession, int]]:
    """Hold the workspace row lock from a raw session; yields (session, backend pid)."""
    async with db.hold() as (session, pid):
        await session.execute(
            text("SELECT id FROM workspaces WHERE id = :ws FOR UPDATE"), {"ws": workspace_id}
        )
        try:
            yield session, pid
        finally:
            await session.rollback()


@contextlib.asynccontextmanager
async def uncommitted_write(
    db: KernelDB,
    workspace_id: uuid.UUID,
    *,
    entity_id: uuid.UUID,
    op_id: uuid.UUID,
    name: str,
    seq: int = 1,
) -> AsyncIterator[tuple[AsyncSession, int]]:
    """Stage a complete but **uncommitted** create: object, event, ledger and cursor.

    This is a real transaction, not a simulation: it locks the workspace row, allocates
    ``seq``, writes the folder row at ``rev=1``, appends the event and the ledger row, and
    leaves everything uncommitted so another connection can be shown to block on it and a
    third can be shown not to see it.

    Yields ``(session, backend_pid)`` so the caller decides the outcome — ``commit()`` makes
    the allocation durable, ``rollback()`` releases the seq back to the pool. The deferred
    object→event FK is validated at COMMIT, exactly as the service relies on.
    """
    from revnote_api.kernel.fingerprint import command_request_hash

    async with db.hold() as (session, pid):
        await session.execute(
            text("SELECT id FROM workspaces WHERE id = :ws FOR UPDATE"), {"ws": workspace_id}
        )
        await session.execute(
            text("UPDATE workspaces SET last_seq = :seq WHERE id = :ws"),
            {"ws": workspace_id, "seq": seq},
        )
        await session.execute(
            text(
                "INSERT INTO folders "
                "(id, workspace_id, parent_id, name, rev, updated_seq, updated_at) "
                "VALUES (:id, :ws, NULL, :name, 1, :seq, now())"
            ),
            {"id": entity_id, "ws": workspace_id, "name": name, "seq": seq},
        )
        await session.execute(
            text(
                "INSERT INTO change_events "
                "(workspace_id, seq, entity_type, entity_id, op, rev, op_id) "
                "VALUES (:ws, :seq, 'folder', :entity, 'create', 1, :op)"
            ),
            {"ws": workspace_id, "seq": seq, "entity": entity_id, "op": op_id},
        )
        # The ledger needs a real 32-byte fingerprint; derive it from the equivalent command.
        staged = make_command(
            "folder_create", workspace_id, name=name, entity_id=entity_id, op_id=op_id
        )
        await session.execute(
            text(
                "INSERT INTO op_ledger (workspace_id, op_id, request_hash, result_seq) "
                "VALUES (:ws, :op, :hash, :seq)"
            ),
            {"ws": workspace_id, "op": op_id, "hash": command_request_hash(staged), "seq": seq},
        )
        try:
            yield session, pid
        finally:
            with contextlib.suppress(Exception):
                await session.rollback()


async def assert_dense_seq(
    db: KernelDB, workspace_id: uuid.UUID, *, expected_count: int, context: str = ""
) -> Quads:
    """``1..N`` with no hole and no duplicate; cursor, events and ledger all agree."""
    quads = await db.quads(workspace_id)
    suffix = f" [{context}]" if context else ""
    assert quads.events == expected_count, (
        f"expected {expected_count} events{suffix}, found {quads.events}: {quads.describe()}"
    )
    assert quads.ledger == expected_count, (
        f"expected {expected_count} ledger rows{suffix}, found {quads.ledger}: {quads.describe()}"
    )
    assert quads.last_seq == expected_count, (
        f"last_seq={quads.last_seq} != {expected_count} allocated seqs{suffix}: {quads.describe()}"
    )
    assert list(quads.event_seqs) == list(range(1, expected_count + 1)), (
        f"seq must be exactly 1..{expected_count} with no hole{suffix}: {quads.describe()}"
    )
    return quads


def constraint_name(exc: BaseException) -> str | None:
    """The violated constraint's real name, from the driver's exception chain."""
    node: object | None = exc
    for _ in range(5):
        if node is None:
            return None
        name = getattr(node, "constraint_name", None)
        if isinstance(name, str) and name:
            return name
        node = getattr(node, "orig", None)
    return None


def assert_unique_violation(exc: BaseException, identity: str) -> None:
    """Strong assertion: the database rejected exactly the named unique key."""
    assert sqlstate(exc) == "23505", f"expected a unique violation, got {sqlstate(exc)!r}: {exc}"
    expected = _CONSTRAINT_NAMES[identity]
    assert constraint_name(exc) == expected, (
        f"expected the {identity} key ({expected}) to be violated, "
        f"got {constraint_name(exc)!r}: {exc}"
    )
