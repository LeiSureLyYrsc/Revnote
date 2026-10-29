"""Helpers for the kernel integration tests.

Small on purpose: only what the test modules call. Production symbols are imported and used
by their real names — no adapters, no reflection, no permissive comparison (asyncpg returns
native ``uuid``/``int``/``bytes``/``datetime``, so assertions use ``==``).
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from revnote_api.kernel import errors as kernel_errors
from revnote_api.kernel import ids as kernel_ids
from revnote_api.kernel.commands import (
    CreateFolder,
    CreateNote,
    DeleteFolder,
    DeleteNote,
    MoveFolder,
    MoveNote,
    UpdateFolder,
    UpdateNote,
)
from revnote_api.kernel.fingerprint import command_request_hash

# --------------------------------------------------------------------------- #
# constants
# --------------------------------------------------------------------------- #

#: Bound for an expected lock wait; real row-lock handoffs take milliseconds.
#: Bound for a whole race (the 8x20 matrix holds eight connections open).

#: Dense-seq matrix from plan section 8 (N=8, M=20); everything derives from these.


ENTITY_FOLDER = "folder"
ENTITY_NOTE = "note"

OP_CREATE = "create"
OP_UPDATE = "update"
OP_MOVE = "move"
OP_DELETE = "delete"

COMMAND_CLASSES: dict[str, type[Any]] = {
    "folder_create": CreateFolder,
    "folder_update": UpdateFolder,
    "folder_move": MoveFolder,
    "folder_delete": DeleteFolder,
    "note_create": CreateNote,
    "note_update": UpdateNote,
    "note_move": MoveNote,
    "note_delete": DeleteNote,
}

new_entity_id: Callable[[], uuid.UUID] = kernel_ids.new_entity_id
new_op_id: Callable[[], uuid.UUID] = kernel_ids.new_op_id
request_hash: Callable[[Any], bytes] = command_request_hash

#: Unique keys the constraint tests name, mapped to their PostgreSQL constraint names.


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def make_command(role: str, workspace_id: uuid.UUID, **fields: Any) -> Any:
    """Build one of the eight commands with caller-owned ``entity_id``/``op_id`` (D5)."""
    cls = COMMAND_CLASSES[role]
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "op_id": fields.pop("op_id", None) or new_op_id(),
        "entity_id": fields.pop("entity_id", None) or new_entity_id(),
    }
    values.update(fields)
    return cls(**values)


def variant(command: Any, **overrides: Any) -> Any:
    """Same ids, different content — the ``OpIdConflict`` trigger (D4)."""
    return dataclasses.replace(command, **overrides)


def with_op_id(command: Any, op_id: uuid.UUID | None = None) -> Any:
    """The identical intent under a fresh op_id: a new operation (section 5)."""
    return dataclasses.replace(command, op_id=op_id or new_op_id())


def describe_command(command: Any) -> str:
    """Identity-only description; never the body content (section 11)."""
    body = getattr(command, "body_md", None)
    return (
        f"{type(command).__name__}(ws={command.workspace_id}, entity={command.entity_id}, "
        f"op={command.op_id}, base_rev={command.base_rev}, "
        f"body_chars={0 if body is None else len(body)})"
    )


# --------------------------------------------------------------------------- #
# the invariant quadruple
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True, slots=True)
class Quads:
    """``last_seq`` + event/ledger counts + the exact seq list of one workspace."""

    workspace_id: uuid.UUID
    last_seq: int
    min_retained_seq: int
    events: int
    ledger: int
    event_seqs: tuple[int, ...]

    def describe(self) -> str:
        return (
            f"ws={str(self.workspace_id)[:8]} last_seq={self.last_seq} "
            f"min_retained={self.min_retained_seq} events={self.events} "
            f"ledger={self.ledger} seq_count={len(self.event_seqs)}"
        )


def assert_quads_unchanged(before: Quads, after: Quads, *, context: str = "") -> None:
    """A failed write must leave cursor, events and ledger exactly as they were (section 6)."""
    suffix = f" [{context}]" if context else ""
    for attribute in ("last_seq", "events", "ledger", "event_seqs"):
        assert getattr(before, attribute) == getattr(after, attribute), (
            f"{attribute} moved although the write had to roll back{suffix}: "
            f"{before.describe()} -> {after.describe()}"
        )


# --------------------------------------------------------------------------- #
# direct database access
# --------------------------------------------------------------------------- #


class KernelDB:
    """Direct SQL on the business database; every probe opens a new session (section 11)."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory

    @contextlib.asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[tuple[AsyncSession, int]]:
        """A session plus its backend pid, connection taken eagerly.

        Racing tasks enter before the barrier: on ``NullPool`` each task then owns a real
        server connection, so the barrier cannot starve on a pool ceiling.
        """
        async with self.session_factory() as session:
            pid = int((await session.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            yield session, pid

    # -- one-shot reads ------------------------------------------------------ #
    async def scalar(self, sql: str, **params: Any) -> Any:
        async with self.session_factory() as session:
            return (await session.execute(text(sql), params)).scalar()

    async def scalars(self, sql: str, **params: Any) -> list[Any]:
        async with self.session_factory() as session:
            return list((await session.execute(text(sql), params)).scalars())

    async def rows(self, sql: str, **params: Any) -> list[dict[str, Any]]:
        async with self.session_factory() as session:
            result = await session.execute(text(sql), params)
            return [dict(record._mapping) for record in result]

    async def execute(self, sql: str, **params: Any) -> int:
        """One statement in its own committed transaction; returns the rowcount."""
        async with self.session_factory() as session:
            result = await session.execute(text(sql), params)
            await session.commit()
            return int(result.rowcount or 0)

    # -- raw writes (states the service refuses to create) ------------------- #
    async def raw_insert_event(
        self,
        *,
        workspace_id: uuid.UUID,
        seq: int,
        entity_type: str,
        entity_id: uuid.UUID,
        op: str,
        rev: int,
        op_id: uuid.UUID,
    ) -> None:
        await self.execute(
            "INSERT INTO change_events "
            "(workspace_id, seq, entity_type, entity_id, op, rev, op_id) "
            "VALUES (:workspace_id, :seq, :entity_type, :entity_id, :op, :rev, :op_id)",
            workspace_id=workspace_id,
            seq=seq,
            entity_type=entity_type,
            entity_id=entity_id,
            op=op,
            rev=rev,
            op_id=op_id,
        )

    async def raw_insert_ledger(
        self,
        *,
        workspace_id: uuid.UUID,
        op_id: uuid.UUID,
        request_hash_value: bytes,
        result_seq: int,
        allow_bad_hash: bool = False,
    ) -> None:
        """The complete ledger INSERT of step 8. ``allow_bad_hash`` only for the CHECK test."""
        if not allow_bad_hash and len(request_hash_value) != 32:
            raise AssertionError("op_ledger.request_hash must be exactly 32 bytes")
        await self.execute(
            "INSERT INTO op_ledger (workspace_id, op_id, request_hash, result_seq) "
            "VALUES (:workspace_id, :op_id, :request_hash, :result_seq)",
            workspace_id=workspace_id,
            op_id=op_id,
            request_hash=request_hash_value,
            result_seq=result_seq,
        )

    async def forge_recorded_op(self, command: Any, *, rev: int = 1, seq: int | None = None) -> int:
        """Write a complete "this op already succeeded" pair (event + ledger)."""
        target = seq if seq is not None else await self.next_free_seq(command.workspace_id)
        await self.raw_insert_event(
            workspace_id=command.workspace_id,
            seq=target,
            entity_type=command.entity_type,
            entity_id=command.entity_id,
            op=command.op,
            rev=rev,
            op_id=command.op_id,
        )
        await self._advance_cursor(command.workspace_id, target)
        await self.raw_insert_ledger(
            workspace_id=command.workspace_id,
            op_id=command.op_id,
            request_hash_value=request_hash(command),
            result_seq=target,
        )
        return target

    async def forge_ledger_only(self, command: Any, *, seq: int) -> int:
        """Record an op whose event describes a *different* entity (corrupt stored state)."""
        await self.raw_insert_event(
            workspace_id=command.workspace_id,
            seq=seq,
            entity_type=command.entity_type,
            entity_id=new_entity_id(),
            op=command.op,
            rev=1,
            op_id=new_op_id(),
        )
        await self._advance_cursor(command.workspace_id, seq)
        await self.raw_insert_ledger(
            workspace_id=command.workspace_id,
            op_id=command.op_id,
            request_hash_value=request_hash(command),
            result_seq=seq,
        )
        return seq

    async def _advance_cursor(self, workspace_id: uuid.UUID, seq: int) -> None:
        await self.execute(
            "UPDATE workspaces SET last_seq = :seq WHERE id = :ws AND last_seq < :seq",
            ws=workspace_id,
            seq=seq,
        )

    async def next_free_seq(self, workspace_id: uuid.UUID) -> int:
        return await self.last_seq(workspace_id) + 1

    # -- workspace state ----------------------------------------------------- #
    async def workspace_row(self, workspace_id: uuid.UUID) -> dict[str, Any]:
        rows = await self.rows(
            "SELECT id, owner_id, last_seq, min_retained_seq FROM workspaces WHERE id = :ws",
            ws=workspace_id,
        )
        if not rows:
            raise AssertionError(f"workspace {workspace_id} does not exist")
        return rows[0]

    async def last_seq(self, workspace_id: uuid.UUID) -> int:
        return int((await self.workspace_row(workspace_id))["last_seq"])

    async def min_retained_seq(self, workspace_id: uuid.UUID) -> int:
        return int((await self.workspace_row(workspace_id))["min_retained_seq"])

    async def quads(self, workspace_id: uuid.UUID) -> Quads:
        row = await self.workspace_row(workspace_id)
        return Quads(
            workspace_id=workspace_id,
            last_seq=int(row["last_seq"]),
            min_retained_seq=int(row["min_retained_seq"]),
            events=await self.count_events(workspace_id),
            ledger=await self.count_ledger(workspace_id),
            event_seqs=await self.event_seqs(workspace_id),
        )

    async def count_events(self, workspace_id: uuid.UUID) -> int:
        return int(
            await self.scalar(
                "SELECT count(*) FROM change_events WHERE workspace_id = :ws", ws=workspace_id
            )
        )

    async def count_ledger(self, workspace_id: uuid.UUID) -> int:
        return int(
            await self.scalar(
                "SELECT count(*) FROM op_ledger WHERE workspace_id = :ws", ws=workspace_id
            )
        )

    async def event_seqs(self, workspace_id: uuid.UUID) -> tuple[int, ...]:
        return tuple(
            int(value)
            for value in await self.scalars(
                "SELECT seq FROM change_events WHERE workspace_id = :ws ORDER BY seq",
                ws=workspace_id,
            )
        )

    async def events(self, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
        return await self.rows(
            "SELECT workspace_id, seq, entity_type, entity_id, op, rev, op_id, created_at "
            "FROM change_events WHERE workspace_id = :ws ORDER BY seq",
            ws=workspace_id,
        )

    async def event_at(self, workspace_id: uuid.UUID, seq: int) -> dict[str, Any] | None:
        rows = await self.rows(
            "SELECT workspace_id, seq, entity_type, entity_id, op, rev, op_id, created_at "
            "FROM change_events WHERE workspace_id = :ws AND seq = :seq",
            ws=workspace_id,
            seq=seq,
        )
        return rows[0] if rows else None

    async def events_for_op(
        self, workspace_id: uuid.UUID, op_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        return await self.rows(
            "SELECT workspace_id, seq, entity_type, entity_id, op, rev, op_id, created_at "
            "FROM change_events WHERE workspace_id = :ws AND op_id = :op ORDER BY seq",
            ws=workspace_id,
            op=op_id,
        )

    async def ledger_rows(self, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
        return await self.rows(
            "SELECT workspace_id, op_id, request_hash, result_seq FROM op_ledger "
            "WHERE workspace_id = :ws ORDER BY result_seq",
            ws=workspace_id,
        )

    async def ledger_for_op(
        self, workspace_id: uuid.UUID, op_id: uuid.UUID
    ) -> dict[str, Any] | None:
        rows = await self.rows(
            "SELECT workspace_id, op_id, request_hash, result_seq FROM op_ledger "
            "WHERE workspace_id = :ws AND op_id = :op",
            ws=workspace_id,
            op=op_id,
        )
        return rows[0] if rows else None

    async def events_without_ledger(self, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
        return await self.rows(
            "SELECT e.seq, e.op_id FROM change_events e WHERE e.workspace_id = :ws "
            "AND NOT EXISTS (SELECT 1 FROM op_ledger l "
            "WHERE l.workspace_id = e.workspace_id AND l.result_seq = e.seq) ORDER BY e.seq",
            ws=workspace_id,
        )

    async def ledger_without_event(self, workspace_id: uuid.UUID) -> list[dict[str, Any]]:
        return await self.rows(
            "SELECT l.op_id, l.result_seq FROM op_ledger l WHERE l.workspace_id = :ws "
            "AND NOT EXISTS (SELECT 1 FROM change_events e "
            "WHERE e.workspace_id = l.workspace_id AND e.seq = l.result_seq)",
            ws=workspace_id,
        )

    # -- entity state -------------------------------------------------------- #
    @staticmethod
    def table_of(entity_type: str) -> str:
        if entity_type == ENTITY_FOLDER:
            return "folders"
        if entity_type == ENTITY_NOTE:
            return "notes"
        raise AssertionError(f"unknown entity_type {entity_type!r}")

    async def entity_row(
        self, workspace_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
    ) -> dict[str, Any]:
        row = await self.entity_row_or_none(workspace_id, entity_type, entity_id)
        if row is None:
            raise AssertionError(
                f"{entity_type} {entity_id} has no row in {self.table_of(entity_type)}; "
                "the kernel soft-deletes and never removes rows"
            )
        return row

    async def entity_row_or_none(
        self, workspace_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
    ) -> dict[str, Any] | None:
        rows = await self.rows(
            f"SELECT * FROM {self.table_of(entity_type)} WHERE workspace_id = :ws AND id = :id",
            ws=workspace_id,
            id=entity_id,
        )
        return rows[0] if rows else None

    async def rev(self, workspace_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID) -> int:
        return (await self.entity_row(workspace_id, entity_type, entity_id))["rev"]

    async def updated_seq(
        self, workspace_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
    ) -> int:
        return (await self.entity_row(workspace_id, entity_type, entity_id))["updated_seq"]

    async def deleted_at(
        self, workspace_id: uuid.UUID, entity_type: str, entity_id: uuid.UUID
    ) -> dt.datetime | None:
        return (await self.entity_row(workspace_id, entity_type, entity_id))["deleted_at"]

    async def folder_parent(
        self, workspace_id: uuid.UUID, folder_id: uuid.UUID
    ) -> uuid.UUID | None:
        return (await self.entity_row(workspace_id, ENTITY_FOLDER, folder_id))["parent_id"]

    async def note_folder(self, workspace_id: uuid.UUID, note_id: uuid.UUID) -> uuid.UUID | None:
        return (await self.entity_row(workspace_id, ENTITY_NOTE, note_id))["folder_id"]

    async def note_body(self, workspace_id: uuid.UUID, note_id: uuid.UUID) -> str:
        return (await self.entity_row(workspace_id, ENTITY_NOTE, note_id))["body_md"]

    async def live_children(self, workspace_id: uuid.UUID, folder_id: uuid.UUID) -> int:
        """Active child folders + notes: the non-empty-folder precondition."""
        folders = await self.scalar(
            "SELECT count(*) FROM folders "
            "WHERE workspace_id = :ws AND parent_id = :parent AND deleted_at IS NULL",
            ws=workspace_id,
            parent=folder_id,
        )
        notes = await self.scalar(
            "SELECT count(*) FROM notes "
            "WHERE workspace_id = :ws AND folder_id = :parent AND deleted_at IS NULL",
            ws=workspace_id,
            parent=folder_id,
        )
        return int(folders) + int(notes)

    async def subtree_folders(self, workspace_id: uuid.UUID, root_id: uuid.UUID) -> list[uuid.UUID]:
        """Every folder below *root_id* (recursive), excluding the root."""
        rows = await self.rows(
            "WITH RECURSIVE subtree AS ("
            " SELECT id FROM folders WHERE workspace_id = :ws AND parent_id = :root"
            " UNION ALL"
            " SELECT f.id FROM folders f JOIN subtree s ON f.parent_id = s.id"
            " WHERE f.workspace_id = :ws"
            ") SELECT id FROM subtree ORDER BY id",
            ws=workspace_id,
            root=root_id,
        )
        return [row["id"] for row in rows]

    # Lock observation (``waiters_of`` / ``blocked_report``) lives in ``kernel_race``: it is
    # only used by the concurrent cases, so the single-connection helper stays lean.


# --------------------------------------------------------------------------- #
# race orchestration
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# coherence assertions
# --------------------------------------------------------------------------- #


async def assert_one_success(
    db: KernelDB, result: Any, *, expected_seq: int | None = None
) -> dict[str, Any]:
    """One success = one event = one ledger row = one object stamp; all must agree."""
    from revnote_api.kernel.results import WriteResult

    assert isinstance(result, WriteResult), (
        f"the write entrypoint must return WriteResult, got {type(result).__name__}"
    )
    if expected_seq is not None:
        assert result.seq == expected_seq, (
            f"expected seq {expected_seq}, got {result.seq} for {result!r}"
        )

    event = await db.event_at(result.workspace_id, result.seq)
    assert event is not None, f"no change_events row at seq={result.seq} ({result!r})"
    for field in ("seq", "rev", "op", "entity_id", "op_id", "entity_type", "workspace_id"):
        assert event[field] == getattr(result, field), (
            f"change_events.{field}={event[field]!r} disagrees with the result "
            f"{getattr(result, field)!r}"
        )

    ledger = await db.ledger_for_op(result.workspace_id, result.op_id)
    assert ledger is not None, (
        f"a successful write left no op_ledger row for op_id={result.op_id} (step 8)"
    )
    assert ledger["result_seq"] == result.seq, (
        f"op_ledger.result_seq={ledger['result_seq']} != returned seq={result.seq}"
    )
    assert ledger["op_id"] == result.op_id

    object_row = await db.entity_row(result.workspace_id, result.entity_type, result.entity_id)
    assert object_row["rev"] == result.rev, (
        f"{result.entity_type}.rev={object_row['rev']} != result.rev={result.rev}"
    )
    assert object_row["updated_seq"] == result.seq, (
        f"{result.entity_type}.updated_seq={object_row['updated_seq']} != seq={result.seq}"
    )
    assert object_row["workspace_id"] == result.workspace_id
    assert await db.last_seq(result.workspace_id) >= result.seq
    return event


# --------------------------------------------------------------------------- #
# error / constraint assertions
# --------------------------------------------------------------------------- #


def expect(name: str) -> Any:
    """``pytest.raises`` for a contract error looked up by the plan's own name."""
    return pytest.raises(getattr(kernel_errors, name))


def sqlstate(exc: BaseException) -> str:
    """The PostgreSQL SQLSTATE of a driver error (``""`` when the driver reports none)."""
    for candidate in (getattr(exc, "orig", None), exc):
        if candidate is None:
            continue
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(candidate, attribute, None)
            if value:
                return str(value)
    return ""
