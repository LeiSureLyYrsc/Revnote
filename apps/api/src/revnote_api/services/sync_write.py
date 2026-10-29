"""``SyncWriteService`` - the single public write entry point of the kernel.

One command in, one immutable :class:`~revnote_api.kernel.results.WriteResult`
out. Each call owns **one** independent session/connection and **one** transaction:
the workspace row is locked first, a ``seq`` is allocated from it, the object and
its ``change_events`` row are written, the ``op_ledger`` row is inserted, and only
a successful ``COMMIT`` produces a result (plan section 6).

The write path in order
-----------------------

1. structural validation + request hash - outside the transaction, no I/O.
2. ``BEGIN``; ``SET TRANSACTION ISOLATION LEVEL READ COMMITTED`` (the first
   statement) and a bounded ``SET LOCAL lock_timeout``.
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
from revnote_api.kernel.results import WriteResult
from revnote_api.services.sync_repository import EntityRow, EventRow, SyncRepository

__all__ = ["LOCK_TIMEOUT_MS", "SyncWriteService"]

logger = logging.getLogger(__name__)

#: Bound on the workspace lock wait (plan section 11: bounded, and deliberately not
#: a new configuration surface). On expiry PostgreSQL cancels the statement and the
#: caller sees :class:`WriteBusy` with nothing written.
LOCK_TIMEOUT_MS = 5_000

#: Pinning isolation per transaction - rather than trusting an engine default that
#: someone could have widened to REPEATABLE READ - is what makes the documented
#: "after the lock wait, READ COMMITTED sees the freshly committed row" guarantee
#: hold, which the entity/parent reads of step 5 depend on.
_SET_READ_COMMITTED = text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
_SET_LOCK_TIMEOUT = text(f"SET LOCAL lock_timeout = {int(LOCK_TIMEOUT_MS)}")

#: Engine defaults this service accepts. ``None`` means "the driver default", which
#: for PostgreSQL is READ COMMITTED anyway; anything wider would violate the
#: kernel's documented isolation, so it is rejected before any write happens.
_ACCEPTED_ENGINE_ISOLATION = frozenset({None, "READ COMMITTED"})

#: SQLSTATEs meaning "nothing was written, re-send the identical command".
_RETRYABLE_SQLSTATES = frozenset(
    {
        "55P03",  # lock_not_available - our lock_timeout fired
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
        """
        self._validate_structure(command)
        metrics = _Metrics()
        started = time.perf_counter()
        try:
            try:
                return await self._run(command, metrics)
            except _LedgerRace:
                metrics.category = "replayed_after_race"
                return await self._replay_after_race(command)
        except SyncKernelError as exc:
            metrics.category = type(exc).__name__
            raise
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            # Cancellation is never wrapped or reclassified (plan section 6).
            metrics.category = "cancelled"
            raise
        except Exception as exc:  # translated, never swallowed
            metrics.category = "StorageFailure"
            raise self._as_storage_failure(exc, command) from exc
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
    async def _run(self, command: Command, metrics: _Metrics) -> WriteResult:
        request_hash = command_hash(command)
        async with self._session_factory() as session:
            self._check_engine_isolation(session)
            try:
                await session.execute(_SET_READ_COMMITTED)
                await session.execute(_SET_LOCK_TIMEOUT)
                repository = SyncRepository(session)

                # -- step 3: workspace lock, before any business row -------- #
                lock_started = time.perf_counter()
                workspace = await repository.lock_workspace(command.workspace_id)
                metrics.lock_wait_ms = (time.perf_counter() - lock_started) * 1000.0
                if workspace is None:
                    raise WorkspaceNotFound(command.workspace_id)

                # -- step 4: replay before CAS ----------------------------- #
                replayed = await self._replay_if_recorded(repository, command, request_hash)
                if replayed is not None:
                    metrics.category = "replayed"
                    # The workspace lock is held and nothing was written: end the read
                    # transaction explicitly rather than letting ``close()`` decide.
                    await self._end_read_only(session)
                    return replayed

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
                await self._commit(session, command)
                metrics.category = "committed"
                return result
            except BaseException:
                # Release local transaction state. After an unknown COMMIT outcome,
                # rollback cannot undo a transaction the server already committed;
                # the original CommitOutcomeUnknown must still reach the caller.
                with contextlib.suppress(Exception):
                    await session.rollback()
                raise

    async def _commit(self, session: AsyncSession, command: Command) -> None:
        """``COMMIT`` with an honest classification of its failure modes.

        ``AsyncSession.commit`` is used (not the transaction handle) so a test can wrap
        the one and only commit call and prove durability without production growing a
        hook. Everything raised here is either "definitely rejected" (a server error
        with a SQLSTATE) or "we do not know whether it committed" (a lost
        acknowledgement, a dead connection). Catching broadly is deliberate: an
        unclassified commit exception must become
        :class:`CommitOutcomeUnknown`, never an implied rollback.
        """
        try:
            await session.commit()
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # classified below, never swallowed
            raise _classify_commit_failure(exc, command) from exc

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

    async def _replay_after_race(self, command: Command) -> WriteResult:
        """Re-read the ledger in a **new** transaction after losing the op-id race.

        A single pass: this is the conflict resolution mandated by plan section 6
        step 8, not a retry loop. If the winner recorded a different fingerprint the
        caller gets ``OpIdConflict``; if nothing is visible the stored state is
        inconsistent.
        """
        request_hash = command_hash(command)
        async with self._session_factory() as session:
            self._check_engine_isolation(session)
            try:
                await session.execute(_SET_READ_COMMITTED)
                await session.execute(_SET_LOCK_TIMEOUT)
                repository = SyncRepository(session)
                workspace = await repository.lock_workspace(command.workspace_id)
                if workspace is None:
                    raise WorkspaceNotFound(command.workspace_id)
                replayed = await self._replay_if_recorded(repository, command, request_hash)
                if replayed is None:
                    raise StorageFailure(
                        f"op {command.op_id} collided on the idempotency key but no ledger row "
                        "is visible; stored state is inconsistent"
                    )
            except BaseException:
                with contextlib.suppress(Exception):
                    await session.rollback()
                raise
            # Success on a read-only path: end the transaction explicitly. Never commit
            # one that wrote nothing, and never leave it open for ``close()`` to decide.
            await self._end_read_only(session)
            return replayed

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
    """Map a failed ``COMMIT`` without ever claiming a rollback we cannot prove."""
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
