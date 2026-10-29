"""``SyncWriteService`` - the single public write entry point of the kernel.

One command in, one immutable :class:`~revnote_api.kernel.results.WriteResult`
out. Each call owns **one** independent session/connection and **one** transaction:
the workspace row is locked first, a ``seq`` is allocated from it, the object and
its ``change_events`` row are written, the ``op_ledger`` row is inserted, and only
a successful ``COMMIT`` produces a result (plan section 6).

The write path in order
-----------------------

1. structural validation + request hash + timeout budgets - outside the
   transaction, no I/O.
2. ``BEGIN``; ``SET TRANSACTION ISOLATION LEVEL READ COMMITTED`` (the first
   statement), then the bounded ``SET LOCAL lock_timeout`` and
   ``SET LOCAL statement_timeout`` budgets (defaults 5 s / 10 s, injectable per
   call through :meth:`SyncWriteService.execute_with_outcome`).
3. ``SELECT ... FOR UPDATE`` on ``workspaces`` (always before any business row,
   and held until ``COMMIT``).
4. ledger lookup for ``(workspace_id, op_id)`` *before* the CAS: same hash means
   replay the recorded result; a different hash means ``OpIdConflict``.
5. workspace-scoped entity/parent reads, then in this order: existence,
   tombstone, ``base_rev``, parent/tree/emptiness rules.
6. BIGINT guard, then ``last_seq + 1`` as a conditional ``UPDATE``.
7. the object write: plain ``INSERT`` for a create, otherwise a conditional
   ``UPDATE ... WHERE rev = base_rev AND deleted_at IS NULL`` whose row count is
   checked.
8. ``INSERT`` the event and keep its ``RETURNING created_at`` as the result
   timestamp.
9. complete ``INSERT ... ON CONFLICT DO NOTHING RETURNING`` of the ledger row.
10. ``COMMIT``; the deferred object-to-event foreign key is validated here, so no
    result exists before the database has accepted the whole write.

Timeouts, cancellation and an unknown outcome
---------------------------------------------
Every transaction carries a bounded lock wait **and** a bounded statement budget
(``DB_LOCK_TIMEOUT_MS`` / ``DB_STATEMENT_TIMEOUT_MS`` by default, overridable per
call). PostgreSQL reports the first as ``55P03`` and the second as ``57014``; both
mean *the server cancelled the statement before it produced a result*, so both map
to :class:`WriteBusy` — nothing was committed and the identical command may be
retried. This mapping is deliberately restricted to an error the server actually
raised; a lost connection or a lost ``COMMIT`` acknowledgement has no SQLSTATE to
trust and stays :class:`CommitOutcomeUnknown`.

Cancellation is never converted. A :class:`asyncio.CancelledError` raised before
``COMMIT`` unwinds normally (the session rolls back, which is a local close and not
a claim about the server), and one that lands *during* ``COMMIT`` is re-raised
unchanged after the connection has been invalidated: the server may have committed,
so the caller must never treat it as a rollback, but a cancellation must also never
be dressed up as a :class:`SyncKernelError` - an adapter that catches kernel errors
would then swallow the cancellation and keep serving a request whose task is gone.
There is no ``shield`` around the business ``COMMIT``: shielding would pin the task
open past its own deadline. The **cleanup** around it is protected instead, so a
second cancellation cannot abandon an invalidation and hand a broken protocol state
back to the pool.

The one non-retry "second attempt"
----------------------------------
If the database's unique constraints reject our ledger/event row, another
transaction already owns that ``op_id``. The candidate is then rolled back **in
full** and a *new* transaction re-reads the ledger to either replay the recorded
first result or report ``OpIdConflict`` (plan section 6, step 8). There is no loop,
no retry of a rejected write, and a failed ``COMMIT`` is never swallowed.

Deliberate non-goals (plan sections 1 and 11): no HTTP, no auth, no business-error
retries, no ORM identity map in the read path, no monitoring client. Diagnostics
here are one log record with a category and two timings - never note text, titles
or e-mail addresses.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from revnote_api.kernel import commands as cmd
from revnote_api.kernel.commands import MAX_BIGINT, Command
from revnote_api.kernel.errors import (
    CommitOutcomeUnknown,
    CounterOverflow,
    EntityDeleted,
    EntityIdConflict,
    EntityNotFound,
    FolderCycle,
    FolderNotEmpty,
    InvalidCommand,
    InvalidParent,
    OpIdConflict,
    RevConflict,
    StorageFailure,
    SyncKernelError,
    WorkspaceNotFound,
    WriteBusy,
)
from revnote_api.kernel.fingerprint import command_hash
from revnote_api.kernel.results import ExecutedWrite, WriteResult
from revnote_api.services.sync_repository import (
    EntityRow,
    EventRow,
    SyncRepository,
    WorkspaceRow,
)

__all__ = ["LOCK_TIMEOUT_MS", "STATEMENT_TIMEOUT_MS", "SyncWriteService"]

logger = logging.getLogger(__name__)

#: Default bound on the workspace lock wait, matching ``DB_LOCK_TIMEOUT_MS``.
#: On expiry PostgreSQL cancels the statement with ``55P03`` and the caller sees
#: :class:`WriteBusy` with nothing written.
LOCK_TIMEOUT_MS = 5_000

#: Default bound on any single statement (and on the implicit ``COMMIT`` that
#: flushes the deferred FK), matching ``DB_STATEMENT_TIMEOUT_MS``. On expiry
#: PostgreSQL cancels with ``57014`` — also :class:`WriteBusy`, because the server
#: confirmed it did not complete the statement. Kept above the lock timeout so "the
#: row was busy" stays distinguishable from "the statement was too slow".
STATEMENT_TIMEOUT_MS = 10_000

#: Pinning isolation per transaction - rather than trusting an engine default that
#: someone could have widened to REPEATABLE READ - is what makes the documented
#: "after the lock wait, READ COMMITTED sees the freshly committed row" guarantee
#: hold, which the entity/parent reads of step 5 depend on.
_SET_READ_COMMITTED = text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
#: The two module-level statements keep the *defaults*; a call that injects its own
#: budgets emits equivalent ``SET LOCAL`` statements built by :func:`_set_budgets`.
_SET_LOCK_TIMEOUT = text(f"SET LOCAL lock_timeout = {int(LOCK_TIMEOUT_MS)}")
_SET_STATEMENT_TIMEOUT = text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}")

#: Engine defaults this service accepts. ``None`` means "the driver default", which
#: for PostgreSQL is READ COMMITTED anyway; anything wider would violate the
#: kernel's documented isolation, so it is rejected before any write happens.
_ACCEPTED_ENGINE_ISOLATION = frozenset({None, "READ COMMITTED"})

#: SQLSTATEs meaning "the server cancelled this statement, nothing was written,
#: re-send the identical command".
#:
#: ``57014`` (``query_canceled``) is what ``statement_timeout`` raises, and also what
#: an operator's ``pg_cancel_backend`` raises. It is only trusted *before* a result
#: exists (see ``_classify_commit_failure``): once the client has issued ``COMMIT``,
#: a cancellation cannot prove the server rejected the transaction, so it is
#: classified as unknown instead.
_RETRYABLE_SQLSTATES = frozenset(
    {
        "55P03",  # lock_not_available - our lock_timeout fired
        "57014",  # query_canceled - our statement_timeout fired
        "40001",  # serialization_failure
        "40P01",  # deadlock_detected
    }
)
#: SQLSTATEs that make a *commit* acknowledgement unknowable (plan sections 6, 11).
_UNKNOWN_COMMIT_SQLSTATES = frozenset(
    {
        "08000",  # connection_exception
        "08003",  # connection_does_not_exist
        "08006",  # connection_failure
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
        "58030",  # io_error
    }
)


class _LedgerRace(Exception):
    """Internal control flow: the database rejected our ledger/event row for this op.

    Never escapes :meth:`SyncWriteService.execute`; it only unwinds the candidate
    transaction so it is rolled back *before* the ledger is re-read.
    """


@dataclass
class _Metrics:
    """Per-call diagnostics (log-only; never part of the result)."""

    lock_wait_ms: float = 0.0
    category: str = "success"


@dataclass(frozen=True, slots=True)
class _TimeBudget:
    """The two PostgreSQL timeouts one transaction runs under, in milliseconds.

    Injected per call rather than stored on the service, so a single instance stays
    reusable and stateless: ``execute`` uses the module defaults, while a caller
    that owns a stricter outer deadline (the push lane's per-op slice of the batch
    budget) passes its own values.
    """

    lock_timeout_ms: int = LOCK_TIMEOUT_MS
    statement_timeout_ms: int = STATEMENT_TIMEOUT_MS

    def statements(self) -> tuple[Any, Any]:
        """The two ``SET LOCAL`` statements for this budget, in issue order.

        The module-level statements are reused for the defaults, so the common path
        builds nothing and the emitted SQL is byte-identical to the tested text.
        """
        if (self.lock_timeout_ms, self.statement_timeout_ms) == (
            LOCK_TIMEOUT_MS,
            STATEMENT_TIMEOUT_MS,
        ):
            return _SET_LOCK_TIMEOUT, _SET_STATEMENT_TIMEOUT
        return (
            text(f"SET LOCAL lock_timeout = {int(self.lock_timeout_ms)}"),
            text(f"SET LOCAL statement_timeout = {int(self.statement_timeout_ms)}"),
        )


def _validated_budget(lock_timeout_ms: int | None, statement_timeout_ms: int | None) -> _TimeBudget:
    """Resolve the caller's overrides, rejecting a budget PostgreSQL cannot honour.

    Only positivity is checked. The lock/statement *ordering* is deliberately not
    enforced here: a caller may reasonably inject a short statement budget to prove
    the ``57014`` path while the lock budget stays at its default, and PostgreSQL
    then simply reports whichever bound fires first.
    """
    lock = LOCK_TIMEOUT_MS if lock_timeout_ms is None else lock_timeout_ms
    statement = STATEMENT_TIMEOUT_MS if statement_timeout_ms is None else statement_timeout_ms
    for name, value in (("lock_timeout_ms", lock), ("statement_timeout_ms", statement)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise InvalidCommand(f"{name} must be a positive integer number of milliseconds")
    return _TimeBudget(lock_timeout_ms=lock, statement_timeout_ms=statement)


def _sqlstate(exc: BaseException) -> str:
    """Best-effort SQLSTATE of a SQLAlchemy/DBAPI error (``""`` when unknown)."""
    for candidate in (getattr(exc, "orig", None), exc):
        if candidate is None:
            continue
        for attr in ("sqlstate", "pgcode"):
            value = getattr(candidate, attr, None)
            if value:
                return str(value)
    return ""


def _constraint_name(exc: BaseException) -> str | None:
    """The violated constraint's real name, read from the driver error chain.

    ``str(exc)`` is never parsed: PostgreSQL's message text is both data-bearing and
    easy to mis-match (``"op_ledger"`` appears in the ``result_seq`` unique index too).
    The name is carried on the asyncpg exception, which sits below SQLAlchemy's
    ``IntegrityError`` and the dialect's adapt layer, so the ``.orig`` chain is walked.
    """
    node: object | None = exc
    for _ in range(5):
        if node is None:
            return None
        name = getattr(node, "constraint_name", None)
        if isinstance(name, str) and name:
            return name
        node = getattr(node, "orig", None)
    return None


#: The only unique keys that mean "this ``op_id`` was already applied".
#: ``pk_op_ledger`` is the ledger's ``(workspace_id, op_id)`` key;
#: ``uq_change_events_workspace_id_op_id`` is the event's copy of the same key.
#: Deliberately **excluded**: ``uq_op_ledger_workspace_id_result_seq`` (a different
#: invariant) and ``pk_change_events`` (a `(workspace_id, seq)` collision).
_IDEMPOTENCY_CONSTRAINTS = frozenset({"pk_op_ledger", "uq_change_events_workspace_id_op_id"})

#: Per-table primary keys whose violation means "this entity id is taken".
#: Deliberately excludes ``pk_op_ledger`` (the idempotency key) and
#: ``pk_change_events`` (the per-workspace cursor): neither is an ownership conflict.
_ENTITY_PK_CONSTRAINTS = frozenset({"pk_folders", "pk_notes"})


def _is_entity_pk_violation(exc: BaseException) -> bool:
    """True only when the violated unique key is a folder/note primary key.

    Ids are globally unique per table, so a create that loses this race lost to *another
    workspace* using the same UUID - the documented ``EntityIdConflict``. Every other
    unique key stays a storage failure; widening this would hide real bugs.
    """
    name = _constraint_name(exc)
    return name is not None and name in _ENTITY_PK_CONSTRAINTS


def _is_idempotency_violation(exc: BaseException) -> bool:
    """True only when the violated unique key is the op-id idempotency key.

    The constraint name is read from the driver's exception chain and matched against
    an allow-list. Message text is never consulted: PostgreSQL's text also contains
    ``op_ledger`` for the unrelated ``result_seq`` index, and a substring sweep would
    turn that real bug into a silent replay. When the driver reports no name the answer
    is ``False`` — an unidentifiable 23505 is not a licence to replay.
    """
    name = _constraint_name(exc)
    return name is not None and name in _IDEMPOTENCY_CONSTRAINTS


def _driver_error_ref(exc: BaseException) -> str:
    """The error *class* and SQLSTATE of a driver error — never the server's message.

    PostgreSQL's own text is data-bearing: a unique violation says
    ``Key (email)=(someone@example.com) already exists``, a CHECK violation names the
    row's columns, and a foreign-key violation echoes the offending key value. Putting
    that into an exception the caller may log or return would leak note content and
    e-mail addresses, which the diagnostics rules forbid (plan section 11). The class
    and SQLSTATE are enough to act on, and the full error stays on the chained
    ``__cause__`` for a local debugger.
    """
    original = getattr(exc, "orig", exc)
    sqlstate = _sqlstate(exc)
    name = type(original).__name__
    return f"{name}[sqlstate={sqlstate}]" if sqlstate else name


def _engine_default_isolation(session: AsyncSession) -> str | None:
    """The engine's default isolation level, if it pinned one at connect time.

    Two places can pin it (the ``create_async_engine(isolation_level=...)`` argument
    and an ``execution_options(isolation_level=...)`` engine), so both are read. A
    session whose bind cannot be introspected is treated as "driver default", which
    for PostgreSQL is READ COMMITTED anyway; the per-transaction ``SET TRANSACTION``
    is what actually guarantees the level.
    """
    try:
        bind = session.get_bind()
    except Exception:  # pragma: no cover - unbound session, defensive
        return None

    candidates: list[Any] = [
        getattr(getattr(bind, "dialect", None), "_on_connect_isolation_level", None)
    ]
    get_options = getattr(bind, "get_execution_options", None)
    if callable(get_options):
        # pragma: no cover - defensive against an exotic bind
        with contextlib.suppress(Exception):
            candidates.append(dict(get_options()).get("isolation_level"))

    for candidate in candidates:
        if candidate:
            return str(candidate).upper()
    return None


class SyncWriteService:
    """Transactional single-object writes, one workspace at a time.

    ``session_factory`` must hand out a **fresh** ``AsyncSession`` per call (the
    repo's :func:`revnote_api.db.session.get_sessionmaker` does). The service owns
    its transaction: it commits on success, rolls back on every failure, and must
    never be wrapped in a caller-managed ``session_scope``.

    Usage::

        from revnote_api.kernel.commands import CreateFolder
        from revnote_api.kernel.ids import new_entity_id, new_op_id
        from revnote_api.services.sync_write import SyncWriteService

        service = SyncWriteService(get_sessionmaker())
        result = await service.execute(
            CreateFolder(workspace_id=ws, entity_id=new_entity_id(),
                         op_id=new_op_id(), name="Inbox")
        )
        result.seq, result.rev, result.created_at
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------ #
    # public API
    # ------------------------------------------------------------------ #
    async def execute(self, command: Command) -> WriteResult:
        """Apply ``command`` exactly once and return its immutable result.

        Re-sending the same ``op_id`` with the same payload returns the recorded
        first result unchanged, even if the object has moved on since. Re-using it
        with a different payload raises :class:`OpIdConflict`.

        Safe to retry with the identical command on :class:`WriteBusy` and
        :class:`CommitOutcomeUnknown`. Every other error means the command was
        rejected and nothing was committed.

        This signature and return type are the Phase 2 contract and do not change.
        Callers that need to know whether *this* call wrote or replayed, or that
        must impose their own timeout budget, use :meth:`execute_with_outcome`.
        """
        executed = await self.execute_with_outcome(command)
        return executed.result

    async def execute_with_outcome(
        self,
        command: Command,
        *,
        lock_timeout_ms: int | None = None,
        statement_timeout_ms: int | None = None,
    ) -> ExecutedWrite:
        """Apply ``command`` and report the result **plus** how it was produced.

        The write path, the transaction boundaries, the replay rule and every
        error are identical to :meth:`execute` - this method is the real
        implementation and ``execute`` is its projection, so the two can never
        drift. What it adds is one bit of information the HTTP layer needs and the
        kernel deliberately does not store in a result: whether this call
        committed a new write (``applied``) or returned a recorded first result
        (``replayed``).

        ``lock_timeout_ms`` / ``statement_timeout_ms`` override the bounded
        ``SET LOCAL`` budgets for **this call only**; ``None`` means the module
        default. They are keyword-only and optional, so every existing call site
        keeps working, and they touch nothing but the two ``SET LOCAL`` statements:
        no fingerprint, seq allocation, CAS, ledger or object rule depends on them.

        The disposition is carried in the **return value**, never on the service:
        a service instance stays reusable and holds no per-call state. It is also
        not a retry signal - ``replayed`` means the ``op_id`` had already been
        accepted with exactly this payload, which is precisely what makes a
        client's retry-after-unknown-outcome safe.
        """
        self._validate_structure(command)
        budget = _validated_budget(lock_timeout_ms, statement_timeout_ms)
        metrics = _Metrics()
        started = time.perf_counter()
        try:
            try:
                return await self._run(command, metrics, budget)
            except _LedgerRace:
                metrics.category = "replayed_after_race"
                return await self._replay_after_race(command, metrics, budget)
        except SyncKernelError as exc:
            metrics.category = type(exc).__name__
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # Cancellation is never wrapped or reclassified (plan section 6). A more
            # specific category (e.g. an interrupted COMMIT) is kept: it is the one
            # diagnostic that distinguishes "cancelled before writing" from "cancelled
            # while the commit was on the wire".
            if metrics.category == "success":
                metrics.category = "cancelled"
            raise
        except Exception as exc:  # translated, never swallowed
            failure = self._as_storage_failure(exc, command)
            # Log the *contract* outcome, not the generic bucket: a lock/statement
            # timeout is a WriteBusy and an operator must be able to see that.
            metrics.category = type(failure).__name__
            raise failure from exc
        finally:
            logger.info(
                "sync_write category=%s entity_type=%s op=%s "
                "workspace_lock_wait_ms=%.1f transaction_duration_ms=%.1f",
                metrics.category,
                command.entity_type,
                command.op,
                metrics.lock_wait_ms,
                (time.perf_counter() - started) * 1000.0,
            )

    #: Spelling alias: ``service.write(cmd)`` is the same coroutine function.
    write = execute

    # ------------------------------------------------------------------ #
    # the candidate transaction
    # ------------------------------------------------------------------ #
    async def _run(
        self,
        command: Command,
        metrics: _Metrics,
        budget: _TimeBudget | None = None,
    ) -> ExecutedWrite:
        request_hash = command_hash(command)
        async with self._session_factory() as session:
            self._check_engine_isolation(session)
            try:
                await self._apply_budgets(session, budget or _TimeBudget())
                repository = SyncRepository(session)

                # -- step 3: workspace lock, before any business row -------- #
                workspace = await self._lock_workspace(repository, command, metrics)
                if workspace is None:
                    raise WorkspaceNotFound(command.workspace_id)

                # -- step 4: replay before CAS ----------------------------- #
                replayed = await self._replay_if_recorded(repository, command, request_hash)
                if replayed is not None:
                    metrics.category = "replayed"
                    # The workspace lock is held and nothing was written: end the read
                    # transaction explicitly rather than letting ``close()`` decide.
                    await self._end_read_only(session)
                    return ExecutedWrite(result=replayed, disposition="replayed")

                # -- step 5: business rules (all inside the lock) ---------- #
                current: EntityRow | None = None
                if command.op == cmd.OP_CREATE:
                    await self._check_create(repository, command)
                    new_rev = 1
                else:
                    current = await self._load_entity(repository, command)
                    self._check_rev(command, current)
                    await self._check_business_rules(repository, command)
                    new_rev = self._next_rev(current.rev)

                # -- step 6: overflow guard, then the cursor -------------- #
                new_seq = self._next_seq(workspace.last_seq)
                advanced = await repository.advance_seq(
                    command.workspace_id,
                    expected_last_seq=workspace.last_seq,
                    new_seq=new_seq,
                )
                if advanced != 1:
                    raise StorageFailure(
                        "workspaces.last_seq did not advance by exactly one row while its "
                        "row lock was held; stored workspace state is inconsistent"
                    )

                # -- step 7: the object (insert, or a conditional CAS) ----- #
                affected = await repository.apply_object(
                    command,
                    rev=new_rev,
                    seq=new_seq,
                    expected_rev=None if current is None else current.rev,
                    now=datetime.datetime.now(datetime.UTC),
                )
                if affected != 1:
                    await self._raise_after_cas_miss(
                        repository, command, expected_rev=current.rev if current else 0
                    )

                # -- steps 8/9: event, then the complete ledger row ------- #
                try:
                    created_at = await repository.insert_event(
                        workspace_id=command.workspace_id,
                        seq=new_seq,
                        entity_type=command.entity_type,
                        entity_id=command.entity_id,
                        op=command.op,
                        rev=new_rev,
                        op_id=command.op_id,
                    )
                    inserted = await repository.insert_ledger(
                        workspace_id=command.workspace_id,
                        op_id=command.op_id,
                        request_hash=request_hash,
                        result_seq=new_seq,
                    )
                except DBAPIError as exc:
                    if _sqlstate(exc) == "23505" and _is_idempotency_violation(exc):
                        # Someone else owns this op_id: the whole candidate is
                        # rolled back and the ledger is re-read in a new txn.
                        raise _LedgerRace from exc
                    raise
                if not inserted:
                    raise _LedgerRace

                result = WriteResult(
                    workspace_id=command.workspace_id,
                    op_id=command.op_id,
                    entity_type=command.entity_type,
                    entity_id=command.entity_id,
                    op=command.op,
                    seq=new_seq,
                    rev=new_rev,
                    created_at=created_at,
                )

                # -- step 10: COMMIT; the deferred FK is validated here ---- #
                await self._commit(session, command, metrics)
                metrics.category = "committed"
                return ExecutedWrite(result=result, disposition="applied")
            except BaseException:
                # Local cleanup only. A rollback here cannot undo a transaction the
                # server already committed, so it is never reported as one; after an
                # unknown outcome the connection has already been discarded and this
                # is a no-op. ``CancelledError`` is suppressed around the cleanup so a
                # cancelled task surfaces *its* cancellation, not the cleanup's.
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await session.rollback()
                raise

    async def _commit(
        self,
        session: AsyncSession,
        command: Command,
        metrics: _Metrics,
    ) -> None:
        """``COMMIT`` with an honest classification of its failure modes.

        ``AsyncSession.commit`` is used (not the transaction handle) so a test can wrap
        the one and only commit call and prove durability without production growing a
        hook. Everything raised here is either "definitely rejected" (a server error
        with a SQLSTATE, e.g. the deferred FK or a cancelled statement) or "we do not
        know whether it committed" (a lost acknowledgement, a dead connection, or a
        cancellation that landed while the commit was in flight). Catching broadly is
        deliberate: an unclassified commit exception becomes
        :class:`CommitOutcomeUnknown`, never an implied rollback.

        There is **no** ``shield`` here: shielding the business commit would keep a
        cancelled request alive past its deadline, which is exactly what the plan
        forbids. The honest handling is the opposite - stop waiting, tell the caller
        the outcome is unknown, and make sure the connection never goes back to the
        pool.
        """
        try:
            await session.commit()
        except asyncio.CancelledError:
            # The COMMIT request was already on the wire, so the server may well have
            # committed; the one thing we must not do is let the caller conclude the
            # write was rolled back. What we must also NOT do is replace the
            # cancellation with a SyncKernelError: an adapter that maps kernel errors
            # (`except SyncKernelError`) would then swallow a real cancellation and
            # keep serving a request whose task is already dead.
            #
            # So the original CancelledError is re-raised unchanged. The connection is
            # invalidated first, and the caller learns "unknown" from the fact that a
            # cancellation is never a rollback (plan D18: no shield, propagate up,
            # retry the same op_id).
            metrics.category = "cancelled_during_commit"
            await self._discard_connection(session)
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # classified below, never swallowed
            failure = _classify_commit_failure(exc, command)
            if isinstance(failure, CommitOutcomeUnknown):
                # No trustworthy answer from the server: the connection's state is
                # suspect, so it is invalidated rather than returned to the pool.
                await self._discard_connection(session)
            raise failure from exc

    @staticmethod
    async def _discard_connection(session: AsyncSession) -> None:
        """Invalidate a suspect connection so it can never be reused.

        ``AsyncSession.invalidate`` is *awaited*: touching the sync session directly on
        an async driver is a ``MissingGreenlet`` bug waiting to happen. It is a local
        close that tells the pool to discard the connection instead of recycling it,
        which is exactly what a half-finished ``COMMIT`` requires - a protocol state we
        can no longer trust must never serve the next writer.

        Only this **cleanup** is protected, never the business ``COMMIT``: the
        transaction is already lost, and the one thing worth defending is that a
        second cancellation cannot abandon the invalidation and quietly hand the
        suspect connection back to the pool. Nothing about the failure is logged -
        a discard error's cause chain can carry driver parameters, and no diagnostic
        here is worth a leak.
        """
        discard = asyncio.ensure_future(session.invalidate())
        try:
            await asyncio.shield(discard)
        except asyncio.CancelledError:
            # A second cancellation must not abandon the invalidation and let the
            # driver hand the half-committed connection back to the pool.
            with contextlib.suppress(BaseException):
                await discard
        except Exception:
            return  # already unusable; never mask the commit classification

    # ------------------------------------------------------------------ #
    # replay: step 4 and the uniqueness fallback of step 9
    # ------------------------------------------------------------------ #
    async def _replay_if_recorded(
        self,
        repository: SyncRepository,
        command: Command,
        request_hash: bytes,
    ) -> WriteResult | None:
        """The recorded result for this ``op_id``, or ``None`` when it is new."""
        ledger = await repository.find_ledger(command.workspace_id, command.op_id)
        if ledger is None:
            return None
        if bytes(ledger.request_hash) != request_hash:
            raise OpIdConflict(command.workspace_id, command.op_id)
        event = await repository.read_event(command.workspace_id, ledger.result_seq)
        if event is None:
            raise StorageFailure(
                f"op_ledger for op {command.op_id} points at seq {ledger.result_seq} but no "
                "change_event exists there; stored state is inconsistent"
            )
        return self._result_from_event(command, event)

    async def _replay_after_race(
        self,
        command: Command,
        metrics: _Metrics,
        budget: _TimeBudget | None = None,
    ) -> ExecutedWrite:
        """Re-read the ledger in a **new** transaction after losing the op-id race.

        A single pass: this is the conflict resolution mandated by plan section 6
        step 8, not a retry loop. If the winner recorded a different fingerprint the
        caller gets ``OpIdConflict``; if nothing is visible the stored state is
        inconsistent.

        The returned disposition is ``replayed``: this call wrote nothing and the
        result is the winner's recorded first result, so a client retrying an
        unknown outcome learns its op is already durable.

        The recovery transaction runs under the *same* injected budget as the candidate
        that lost, and its wait is added to ``lock_wait_ms``. A lost race therefore
        reports the total time this call spent queued for the workspace - the metric
        must not silently report only the second (usually instant) half.
        """
        request_hash = command_hash(command)
        async with self._session_factory() as session:
            self._check_engine_isolation(session)
            try:
                await self._apply_budgets(session, budget or _TimeBudget())
                repository = SyncRepository(session)
                workspace = await self._lock_workspace(repository, command, metrics)
                if workspace is None:
                    raise WorkspaceNotFound(command.workspace_id)
                replayed = await self._replay_if_recorded(repository, command, request_hash)
                if replayed is None:
                    raise StorageFailure(
                        f"op {command.op_id} collided on the idempotency key but no ledger row "
                        "is visible; stored state is inconsistent"
                    )
            except BaseException:
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await session.rollback()
                raise
            # Success on a read-only path: end the transaction explicitly. Never commit
            # one that wrote nothing, and never leave it open for ``close()`` to decide.
            await self._end_read_only(session)
            return ExecutedWrite(result=replayed, disposition="replayed")

    @staticmethod
    async def _apply_budgets(session: AsyncSession, budget: _TimeBudget) -> None:
        """Pin isolation, then this call's ``lock_timeout`` and ``statement_timeout``.

        All three are transaction-local and issued before the first row is touched, so
        no statement - including the workspace lock wait and the ``COMMIT`` that
        validates the deferred FK - can run unbounded. The order is fixed: a wider
        isolation level would invalidate the read-after-lock guarantee, and both
        timeouts must be in force before the lock is requested.
        """
        await session.execute(_SET_READ_COMMITTED)
        for statement in budget.statements():
            await session.execute(statement)

    @staticmethod
    async def _lock_workspace(
        repository: SyncRepository,
        command: Command,
        metrics: _Metrics | None,
    ) -> WorkspaceRow | None:
        """Take the workspace row lock and always record how long it took.

        ``lock_wait_ms`` is measured with ``try/finally``, so the value is recorded
        on **every** outcome - acquired, timed out, cancelled, or the workspace
        missing. A ``WriteBusy`` row that reported ``0.0`` would hide exactly the
        condition the metric exists to show (plan §5 P2 #2). The value accumulates
        across the two transactions of the lost-race path, so it stays "total time
        this call spent waiting for the workspace", never a per-attempt figure that
        silently replaces the wait that actually happened.
        """
        started = time.perf_counter()
        try:
            return await repository.lock_workspace(command.workspace_id)
        finally:
            if metrics is not None:
                metrics.lock_wait_ms += (time.perf_counter() - started) * 1000.0

    @staticmethod
    async def _end_read_only(session: AsyncSession) -> None:
        """Close a transaction that deliberately wrote nothing.

        Both the replay path and the post-race re-read take the workspace lock and
        read the ledger. That lock must be released *here*, not whenever the session
        context happens to close, so the contract is identical whether the caller's
        factory pools connections or not. A rollback is the honest verb: nothing was
        written and nothing is being published.
        """
        await session.rollback()

    def _result_from_event(self, command: Command, event: EventRow) -> WriteResult:
        """Rebuild the first result from the immutable event, never from the live row."""
        if (
            event.entity_type != command.entity_type
            or event.entity_id != command.entity_id
            or event.op_id != command.op_id
        ):
            raise StorageFailure(
                f"change_event at seq {event.seq} does not describe op {command.op_id} "
                f"({event.entity_type} {event.entity_id} {event.op}); stored state is inconsistent"
            )
        return WriteResult(
            workspace_id=event.workspace_id,
            op_id=event.op_id,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            op=event.op,
            seq=event.seq,
            rev=event.rev,
            created_at=event.created_at,
        )

    # ------------------------------------------------------------------ #
    # step 5: reads and business rules
    # ------------------------------------------------------------------ #
    async def _load_entity(self, repository: SyncRepository, command: Command) -> EntityRow:
        entity = await self._read_entity(repository, command)
        if entity is None:
            raise EntityNotFound(command.entity_type, command.entity_id)
        return entity

    async def _read_entity(self, repository: SyncRepository, command: Command) -> EntityRow | None:
        if command.entity_type == cmd.ENTITY_FOLDER:
            return await repository.get_folder(command.workspace_id, command.entity_id)
        return await repository.get_note(command.workspace_id, command.entity_id)

    def _check_rev(self, command: Command, entity: EntityRow) -> None:
        """Tombstone first, then CAS: a deleted row is never revived by a late write."""
        if entity.deleted_at is not None:
            raise EntityDeleted(command.entity_type, command.entity_id)
        if entity.rev != command.base_rev:
            raise RevConflict(
                command.entity_type,
                command.entity_id,
                expected=int(command.base_rev or 0),
                current=entity.rev,
            )

    async def _check_business_rules(
        self,
        repository: SyncRepository,
        command: Command,
    ) -> None:
        """Parent, tree and emptiness rules - all evaluated under the workspace lock."""
        if command.op == cmd.OP_MOVE:
            # Only a folder can be its own ancestor. A note's ``folder_id`` points into
            # ``folders`` while its own id points into ``notes``, so equal UUIDs are two
            # unrelated rows, never a loop: no ancestor walk applies to a note.
            await self._check_target_folder(
                repository,
                command,
                cycle_check=command.entity_type == cmd.ENTITY_FOLDER,
            )
        elif command.op == cmd.OP_DELETE and command.entity_type == cmd.ENTITY_FOLDER:
            non_empty = await repository.has_active_child_folders(
                command.workspace_id, command.entity_id
            ) or await repository.has_active_child_notes(command.workspace_id, command.entity_id)
            if non_empty:
                raise FolderNotEmpty(command.entity_id)

    async def _check_create(self, repository: SyncRepository, command: Command) -> None:
        """A create never reuses an id, and its optional parent must be usable."""
        existing = await (
            repository.find_folder_anywhere(command.entity_id)
            if command.entity_type == cmd.ENTITY_FOLDER
            else repository.find_note_anywhere(command.entity_id)
        )
        if existing is not None:
            raise EntityIdConflict(command.entity_type, command.entity_id)

        if command.target_parent_id is not None:
            await self._check_target_folder(
                repository,
                command,
                cycle_check=command.entity_type == cmd.ENTITY_FOLDER,
            )

    async def _check_target_folder(
        self,
        repository: SyncRepository,
        command: Command,
        *,
        cycle_check: bool,
    ) -> EntityRow | None:
        """The target folder must exist here and be live; folders also reject cycles.

        One method for both entity types on purpose: the only difference between a
        folder's parent and a note's folder is that a folder can be its own ancestor.
        ``cycle_check`` is the caller's entity-type decision, made explicit, so a note
        can never be pulled into the folder-only self/ancestor comparison.
        """
        folder_id = command.target_parent_id
        if folder_id is None:
            return None  # root folder / unfiled note: nothing to validate
        folder = await repository.get_folder(command.workspace_id, folder_id)
        if folder is None:
            raise InvalidParent(folder_id, reason="no such folder in this workspace")
        if folder.deleted_at is not None:
            raise InvalidParent(folder_id, reason="the folder is deleted")
        if not cycle_check:
            return folder
        if folder.id == command.entity_id:
            raise FolderCycle(command.entity_id, folder_id)
        ancestors = await repository.folder_ancestor_ids(command.workspace_id, folder_id)
        if command.entity_id in ancestors:
            raise FolderCycle(command.entity_id, folder_id)
        return folder

    # ------------------------------------------------------------------ #
    # steps 6/7 support: counters and post-CAS diagnostics
    # ------------------------------------------------------------------ #
    def _next_seq(self, last_seq: int) -> int:
        if last_seq >= MAX_BIGINT:
            raise CounterOverflow("seq")
        return last_seq + 1

    def _next_rev(self, current_rev: int) -> int:
        if current_rev >= MAX_BIGINT:
            raise CounterOverflow("rev")
        return current_rev + 1

    async def _raise_after_cas_miss(
        self,
        repository: SyncRepository,
        command: Command,
        expected_rev: int,
    ) -> None:
        """A 0-row CAS: report the real reason instead of a generic failure."""
        current = await self._read_entity(repository, command)
        if current is None:
            raise EntityNotFound(command.entity_type, command.entity_id)
        if current.deleted_at is not None:
            raise EntityDeleted(command.entity_type, command.entity_id)
        if current.rev != expected_rev:
            raise RevConflict(
                command.entity_type,
                command.entity_id,
                expected=expected_rev,
                current=current.rev,
            )
        raise StorageFailure(
            f"the conditional update of {command.entity_type} {command.entity_id} matched no "
            "row although it is live at the expected revision"
        )

    # ------------------------------------------------------------------ #
    # transaction plumbing / error classification
    # ------------------------------------------------------------------ #
    def _check_engine_isolation(self, session: AsyncSession) -> None:
        """Refuse a session factory whose engine cannot honour the write contract."""
        level = _engine_default_isolation(session)
        if level not in _ACCEPTED_ENGINE_ISOLATION:
            raise StorageFailure(
                f"the session factory is bound to an engine with isolation level {level!r}; "
                "the kernel needs READ COMMITTED transactions (set it on the engine, or "
                "leave it at the driver default)"
            )

    def _validate_structure(self, command: Command) -> None:
        """Step 1: reject a structurally invalid command before touching the database."""
        if not isinstance(command, Command):
            raise InvalidCommand(f"expected a kernel Command, got {type(command).__name__}")

    def _as_storage_failure(self, exc: BaseException, command: Command) -> SyncKernelError:
        """Translate a driver/database error into the documented kernel vocabulary.

        Only the SQLSTATE, the driver class name and the violated constraint's *name*
        are used. The server's message is never forwarded: a CHECK violation carries a
        ``Failing row contains (...)` detail in PostgreSQL, i.e. the note body verbatim.
        """
        sqlstate = _sqlstate(exc)
        if sqlstate in _RETRYABLE_SQLSTATES:
            return WriteBusy(f"sqlstate {sqlstate}")
        if sqlstate == "23505" and _is_entity_pk_violation(exc):
            # Another workspace created the same id before we inserted ours. The ids
            # are globally unique per table, so this is the documented conflict, not a
            # storage fault. Scoped to the entity PKs only: the op-id keys are the
            # ledger race (handled before this point) and event keys are bugs.
            return EntityIdConflict(command.entity_type, command.entity_id)
        if sqlstate == "23503":
            return StorageFailure(
                f"a foreign key rejected the write ({_driver_error_ref(exc)}); the parent or "
                "parent-workspace invariants were not satisfied"
            )
        if sqlstate == "23514":
            return StorageFailure(
                f"a CHECK constraint rejected the write ({_driver_error_ref(exc)})"
            )
        return StorageFailure(f"database error while writing ({_driver_error_ref(exc)})")


def _classify_commit_failure(exc: BaseException, command: Command) -> SyncKernelError:
    """Map a failed ``COMMIT`` without ever claiming a rollback we cannot prove.

    Two classes of failure are distinguishable, and the distinction decides whether
    the client may be told "nothing was written":

    * **A lost acknowledgement** - no SQLSTATE, a connection-class error, or any
      error the server did not raise about this transaction. The server may have
      committed, so the answer is :class:`CommitOutcomeUnknown`.
    * **A confirmed rejection** - the server answered the ``COMMIT`` with a SQLSTATE
      that aborts the transaction (a deferred FK, a lock/statement timeout, a
      serialization or deadlock abort). Those are :class:`WriteBusy` /
      :class:`StorageFailure`, and re-sending the identical command is safe
      (plan D17).

    The branch order matters: "no SQLSTATE at all" is checked first, so an error we
    cannot attribute to the server is never promoted into a retryable verdict.
    """
    sqlstate = _sqlstate(exc)
    if not sqlstate or sqlstate in _UNKNOWN_COMMIT_SQLSTATES or sqlstate.startswith("08"):
        # The acknowledgement was lost: the server may well have committed.
        return CommitOutcomeUnknown(command.op_id)
    if sqlstate in _RETRYABLE_SQLSTATES:
        return WriteBusy(f"commit rejected with sqlstate {sqlstate}")
    if sqlstate == "23503":
        return StorageFailure(
            "commit rejected: a deferred foreign key found no matching change_event "
            f"({_driver_error_ref(exc)})"
        )
    return StorageFailure(f"commit rejected by the database ({_driver_error_ref(exc)})")
