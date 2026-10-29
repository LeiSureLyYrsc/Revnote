"""Small, single-statement storage helpers for the sync write path.

Deliberately **not** a generic repository framework (§3): every method is one
statement with one obvious meaning, because the interesting behaviour — lock
ordering, replay-before-CAS, tree rules, error mapping — belongs to
:mod:`revnote_api.services.sync_write`, not to SQL plumbing.

Two rules the whole module obeys:

* **No ORM entities are loaded.** Queries select columns and return the frozen
  value objects below, so nothing lands in an identity map and every read reflects
  what the current transaction really sees.
* **No engine/transaction management.** One :class:`~sqlalchemy.ext.asyncio.AsyncSession`
  is handed in by the service; every call reuses its single connection and its
  single open transaction, which is what keeps the workspace row lock alive until
  ``COMMIT``.

The methods are intentionally tiny and one-per-step, so a test can monkeypatch a
single step (raising *after* the real statement ran) without any production
debug hook or env failpoint (§8).
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from revnote_api.kernel import commands as cmd
from revnote_api.kernel.commands import Command
from revnote_api.kernel.errors import StorageFailure
from revnote_api.models import ChangeEvent, Folder, Note, OpLedger, Workspace

__all__ = [
    "EntityRow",
    "EventRow",
    "LedgerRow",
    "SyncRepository",
    "WorkspaceRow",
]

#: Ceiling for the recursive ancestry walk. A legitimate tree is bounded by how
#: many folders a workspace can hold; a cycle in *stored* data (only reachable via
#: SQL that bypasses the service) would otherwise recurse forever, so it is
#: reported instead.
_MAX_TREE_DEPTH = 1_000

_FOLDER_COLUMNS = (Folder.id, Folder.workspace_id, Folder.parent_id, Folder.rev, Folder.deleted_at)
_NOTE_COLUMNS = (Note.id, Note.workspace_id, Note.folder_id, Note.rev, Note.deleted_at)
_EVENT_COLUMNS = (
    ChangeEvent.workspace_id,
    ChangeEvent.seq,
    ChangeEvent.entity_type,
    ChangeEvent.entity_id,
    ChangeEvent.op,
    ChangeEvent.rev,
    ChangeEvent.op_id,
    ChangeEvent.created_at,
)


@dataclass(frozen=True, slots=True)
class WorkspaceRow:
    """The locked workspace row: the only sequence allocator."""

    id: uuid.UUID
    last_seq: int


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """An already recorded operation: its fingerprint and result position."""

    op_id: uuid.UUID
    request_hash: bytes
    result_seq: int


@dataclass(frozen=True, slots=True)
class EntityRow:
    """A folder or note row as the write path needs it.

    ``parent_id`` is the entity's parent reference: ``folders.parent_id`` for a
    folder, ``notes.folder_id`` for a note. A set ``deleted_at`` means tombstoned.
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    parent_id: uuid.UUID | None
    rev: int
    deleted_at: datetime.datetime | None


@dataclass(frozen=True, slots=True)
class EventRow:
    """An immutable ``change_events`` row — a recorded first result."""

    workspace_id: uuid.UUID
    seq: int
    entity_type: str
    entity_id: uuid.UUID
    op: str
    rev: int
    op_id: uuid.UUID
    created_at: datetime.datetime


class SyncRepository:
    """Statement-per-method access to the six kernel tables."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ #
    # step 3/6: the workspace row and its cursor
    # ------------------------------------------------------------------ #
    async def lock_workspace(self, workspace_id: uuid.UUID) -> WorkspaceRow | None:
        """``SELECT ... FOR UPDATE`` the workspace — always the first lock taken."""
        stmt = (
            sa.select(Workspace.id, Workspace.last_seq)
            .where(Workspace.id == workspace_id)
            .with_for_update()
        )
        row = (await self._session.execute(stmt)).first()
        return None if row is None else WorkspaceRow(id=row[0], last_seq=row[1])

    async def advance_seq(
        self, workspace_id: uuid.UUID, *, expected_last_seq: int, new_seq: int
    ) -> int:
        """Move ``workspaces.last_seq`` forward; returns the row count (must be 1).

        Conditional on the value just read under the lock: a mismatch means
        something is badly wrong with the stored state, and the caller aborts
        instead of allocating a duplicate or skipped sequence number.
        """
        stmt = (
            sa.update(Workspace)
            .where(Workspace.id == workspace_id, Workspace.last_seq == expected_last_seq)
            .values(last_seq=new_seq)
        )
        return (await self._session.execute(stmt)).rowcount

    # ------------------------------------------------------------------ #
    # steps 4/8: ledger and its recorded event
    # ------------------------------------------------------------------ #
    async def find_ledger(self, workspace_id: uuid.UUID, op_id: uuid.UUID) -> LedgerRow | None:
        """Look up a recorded operation by its idempotency key."""
        stmt = sa.select(OpLedger.op_id, OpLedger.request_hash, OpLedger.result_seq).where(
            OpLedger.workspace_id == workspace_id, OpLedger.op_id == op_id
        )
        row = (await self._session.execute(stmt)).first()
        if row is None:
            return None
        return LedgerRow(op_id=row[0], request_hash=row[1], result_seq=row[2])

    async def read_event(self, workspace_id: uuid.UUID, seq: int) -> EventRow | None:
        """Read one event; a replay is rebuilt from here, never from the live row."""
        stmt = sa.select(*_EVENT_COLUMNS).where(
            ChangeEvent.workspace_id == workspace_id, ChangeEvent.seq == seq
        )
        row = (await self._session.execute(stmt)).first()
        return None if row is None else EventRow(*row)

    # ------------------------------------------------------------------ #
    # step 5: entity reads
    # ------------------------------------------------------------------ #
    async def get_folder(self, workspace_id: uuid.UUID, folder_id: uuid.UUID) -> EntityRow | None:
        """Read a folder *inside* ``workspace_id`` (workspace-scoped, never global)."""
        return await self._read_row(_FOLDER_COLUMNS, Folder, workspace_id, folder_id)

    async def get_note(self, workspace_id: uuid.UUID, note_id: uuid.UUID) -> EntityRow | None:
        """Read a note *inside* ``workspace_id``."""
        return await self._read_row(_NOTE_COLUMNS, Note, workspace_id, note_id)

    async def find_folder_anywhere(self, folder_id: uuid.UUID) -> EntityRow | None:
        """Read a folder by primary key, ignoring the workspace (creates only).

        ``folders.id`` is globally unique, so an id already owned by *another*
        workspace is still unusable: reporting it as ``EntityIdConflict`` beats
        letting it surface as a raw primary-key violation.
        """
        return await self._read_row(_FOLDER_COLUMNS, Folder, None, folder_id)

    async def find_note_anywhere(self, note_id: uuid.UUID) -> EntityRow | None:
        """Read a note by primary key, ignoring the workspace (creates only)."""
        return await self._read_row(_NOTE_COLUMNS, Note, None, note_id)

    async def _read_row(
        self,
        columns: tuple[object, ...],
        table: type[Folder] | type[Note],
        workspace_id: uuid.UUID | None,
        entity_id: uuid.UUID,
    ) -> EntityRow | None:
        predicates = [table.id == entity_id]
        if workspace_id is not None:
            predicates.append(table.workspace_id == workspace_id)
        stmt = sa.select(*columns).where(*predicates)
        row = (await self._session.execute(stmt)).first()
        return None if row is None else EntityRow(*row)

    async def folder_ancestor_ids(
        self, workspace_id: uuid.UUID, folder_id: uuid.UUID
    ) -> frozenset[uuid.UUID]:
        """Every ancestor of ``folder_id`` (itself excluded), in one recursive CTE.

        Serves the indirect-cycle check of D3: a move is legal only when the new
        parent is not inside the moving folder's own subtree. Runs inside the
        workspace lock, so the ancestry cannot change underneath it.

        Both columns are explicitly typed, because asyncpg rejects a recursive
        CTE whose anchor column types it cannot infer (the ``depth`` counter in
        particular is a bare literal). The depth guard means a cycle already
        present in stored data is reported instead of looping forever.
        """
        walk = (
            sa.select(
                sa.cast(Folder.id, postgresql.UUID(as_uuid=True)).label("id"),
                sa.cast(Folder.parent_id, postgresql.UUID(as_uuid=True)).label("parent_id"),
                sa.cast(sa.literal(0), sa.Integer()).label("depth"),
            )
            .where(Folder.workspace_id == workspace_id, Folder.id == folder_id)
            .cte("folder_ancestors", recursive=True)
        )
        walk = walk.union_all(
            sa.select(
                sa.cast(Folder.id, postgresql.UUID(as_uuid=True)).label("id"),
                sa.cast(Folder.parent_id, postgresql.UUID(as_uuid=True)).label("parent_id"),
                sa.cast(walk.c.depth + 1, sa.Integer()).label("depth"),
            )
            .select_from(Folder)
            .join(walk, walk.c.parent_id == Folder.id)
            .where(Folder.workspace_id == workspace_id, walk.c.depth < _MAX_TREE_DEPTH)
        )
        rows = (
            await self._session.execute(
                sa.select(walk.c.parent_id, walk.c.depth).where(walk.c.parent_id.is_not(None))
            )
        ).all()
        if any(int(depth) >= _MAX_TREE_DEPTH for _parent, depth in rows):
            raise StorageFailure(
                f"the folder tree above {folder_id} exceeds {_MAX_TREE_DEPTH} levels: the "
                "stored tree is cyclic or corrupt"
            )
        return frozenset(parent_id for parent_id, _depth in rows)

    async def has_active_child_folders(self, workspace_id: uuid.UUID, folder_id: uuid.UUID) -> bool:
        """True when ``folder_id`` still has a live child folder (blocks deletion)."""
        stmt = (
            sa.select(sa.literal(1))
            .select_from(Folder)
            .where(
                Folder.workspace_id == workspace_id,
                Folder.parent_id == folder_id,
                Folder.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def has_active_child_notes(self, workspace_id: uuid.UUID, folder_id: uuid.UUID) -> bool:
        """True when ``folder_id`` still has a live note (blocks deletion)."""
        stmt = (
            sa.select(sa.literal(1))
            .select_from(Note)
            .where(
                Note.workspace_id == workspace_id,
                Note.folder_id == folder_id,
                Note.deleted_at.is_(None),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    # ------------------------------------------------------------------ #
    # step 7: the one object write
    # ------------------------------------------------------------------ #
    async def apply_object(
        self,
        command: Command,
        *,
        rev: int,
        seq: int,
        expected_rev: int | None,
        now: datetime.datetime,
    ) -> int:
        """Write the command's object; returns affected rows (creates return 1).

        A create is a plain ``INSERT`` at ``rev=1``. Everything else is a
        *conditional* ``UPDATE`` carrying ``rev = expected_rev`` and
        ``deleted_at IS NULL``, so the optimistic-concurrency check is part of the
        statement and not a read-then-write guess: a zero row count means the row
        moved and the caller must fail with ``RevConflict``/``EntityDeleted``.
        """
        if expected_rev is None:
            await self._insert(command, rev=rev, seq=seq, now=now)
            return 1
        return await self._cas_update(command, rev=rev, seq=seq, expected_rev=expected_rev, now=now)

    async def _insert(
        self, command: Command, *, rev: int, seq: int, now: datetime.datetime
    ) -> None:
        # ``target_parent_id`` resolves ``parent_id``/``folder_id`` per entity type and
        # yields ``None`` when a create places the object at the root / unfiled.
        parent = command.target_parent_id
        if command.entity_type == cmd.ENTITY_FOLDER:
            await self._session.execute(
                postgresql.insert(Folder).values(
                    id=command.entity_id,
                    workspace_id=command.workspace_id,
                    parent_id=parent,
                    name=command.name,
                    rev=rev,
                    updated_seq=seq,
                    updated_at=now,
                    deleted_at=None,
                )
            )
            return
        await self._session.execute(
            postgresql.insert(Note).values(
                id=command.entity_id,
                workspace_id=command.workspace_id,
                folder_id=parent,
                title=command.title,
                body_md=command.body_md,
                rev=rev,
                updated_seq=seq,
                updated_at=now,
                deleted_at=None,
            )
        )

    async def _cas_update(
        self,
        command: Command,
        *,
        rev: int,
        seq: int,
        expected_rev: int,
        now: datetime.datetime,
    ) -> int:
        """The single place a CAS predicate is spelled: ``workspace AND id AND rev AND live``."""
        table: type[Folder] | type[Note] = (
            Folder if command.entity_type == cmd.ENTITY_FOLDER else Note
        )
        values: dict[str, object] = {
            "rev": rev,
            "updated_seq": seq,
            "updated_at": now,
        }
        if command.op == cmd.OP_UPDATE:
            values.update(
                {"name": command.name}
                if table is Folder
                else {"title": command.title, "body_md": command.body_md}
            )
        elif command.op == cmd.OP_MOVE:
            values[command.parent_field] = command.target_parent_id
        else:  # delete: tombstone in place, the row is never physically removed
            values.update({"deleted_at": now, "updated_at": now})

        stmt = (
            sa.update(table)
            .where(
                table.workspace_id == command.workspace_id,
                table.id == command.entity_id,
                table.rev == expected_rev,
                table.deleted_at.is_(None),
            )
            .values(**values)
        )
        return (await self._session.execute(stmt)).rowcount

    # ------------------------------------------------------------------ #
    # steps 8/9: append the event, then the complete ledger row
    # ------------------------------------------------------------------ #
    async def insert_event(
        self,
        *,
        workspace_id: uuid.UUID,
        seq: int,
        entity_type: str,
        entity_id: uuid.UUID,
        op: str,
        rev: int,
        op_id: uuid.UUID,
    ) -> datetime.datetime:
        """Append the event and return its own ``created_at`` (the result timestamp)."""
        stmt = (
            postgresql.insert(ChangeEvent)
            .values(
                workspace_id=workspace_id,
                seq=seq,
                entity_type=entity_type,
                entity_id=entity_id,
                op=op,
                rev=rev,
                op_id=op_id,
            )
            .returning(ChangeEvent.created_at)
        )
        return (await self._session.execute(stmt)).scalar_one()

    async def insert_ledger(
        self,
        *,
        workspace_id: uuid.UUID,
        op_id: uuid.UUID,
        request_hash: bytes,
        result_seq: int,
    ) -> bool:
        """Insert the ledger row; ``False`` means the *op-id key* already claimed it.

        ``ON CONFLICT (workspace_id, op_id) DO NOTHING ... RETURNING`` makes the
        database the arbiter for exactly one collision: a returned row means this
        transaction won the op id, no row means another transaction already recorded
        it (roll the whole candidate back and read that result, §6 step 8).

        The conflict target is spelled out on purpose. A bare ``DO NOTHING`` would
        also swallow ``uq_op_ledger_workspace_id_result_seq`` — a *different*
        invariant — and turn a real bug into a silent "somebody else did it".
        """
        stmt = (
            postgresql.insert(OpLedger)
            .values(
                workspace_id=workspace_id,
                op_id=op_id,
                request_hash=request_hash,
                result_seq=result_seq,
            )
            .on_conflict_do_nothing(index_elements=["workspace_id", "op_id"])
            .returning(OpLedger.result_seq)
        )
        return (await self._session.execute(stmt)).first() is not None
