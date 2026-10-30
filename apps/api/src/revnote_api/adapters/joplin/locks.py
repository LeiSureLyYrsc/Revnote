"""Joplin sync locks: shared ``sync`` and single-holder ``exclusive``.

The rules are the client's ``LockHandler`` plus Joplin Server's ``LockModel``:

* acquiring ``sync`` fails only when another client holds an ``exclusive`` lock;
* acquiring ``exclusive`` fails when *any* other client holds any lock;
* re-acquiring your own lock refreshes ``updated_at`` (that is how a long sync
  keeps its lock alive);
* a lock older than the TTL is abandoned and may be reclaimed.

Concurrency is serialised per workspace with a transaction-scoped advisory
lock. Row locks alone cannot do it: the first acquisition of a *new* client is
an insert, and there is no row to lock yet.

Nothing here writes ``change_events`` or moves ``workspaces.last_seq``: a lock
is coordination state, not workspace data.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from revnote_api.adapters.joplin.errors import JoplinProtocolError
from revnote_api.kernel.ids import new_id

__all__ = [
    "LOCK_EXCLUSIVE",
    "LOCK_SYNC",
    "LOCK_TYPES",
    "LockRow",
    "acquire_lock",
    "list_locks",
    "lock_id",
    "parse_lock_id",
    "release_lock",
    "serialize_lock",
]

LOCK_SYNC = "sync"
LOCK_EXCLUSIVE = "exclusive"

#: Wire ids the client uses: ``1`` = sync, ``2`` = exclusive.
LOCK_TYPE_TO_WIRE: dict[str, int] = {LOCK_SYNC: 1, LOCK_EXCLUSIVE: 2}
WIRE_TO_LOCK_TYPE: dict[int, str] = {value: key for key, value in LOCK_TYPE_TO_WIRE.items()}
LOCK_TYPES = tuple(LOCK_TYPE_TO_WIRE)


@dataclass(frozen=True, slots=True)
class LockRow:
    id: uuid.UUID
    lock_type: str
    client_type: int
    client_id: str
    created_at: datetime.datetime
    updated_at: datetime.datetime

    @property
    def wire_id(self) -> str:
        return lock_id(self.lock_type, self.client_type, self.client_id)


def lock_id(lock_type: str, client_type: int, client_id: str) -> str:
    """``<type>_<clientType>_<clientId>`` — the id in the release path."""
    return f"{LOCK_TYPE_TO_WIRE[lock_type]}_{int(client_type)}_{client_id}"


def parse_lock_id(value: str) -> tuple[str, int, str]:
    """Inverse of :func:`lock_id`; refuses anything the client cannot produce."""
    parts = value.split("_", 2)
    if len(parts) != 3:
        raise JoplinProtocolError("bad_request")
    raw_type, raw_client, client_id = parts
    if not raw_type.isdigit() or not raw_client.isdigit():
        raise JoplinProtocolError("bad_request")
    lock_type = WIRE_TO_LOCK_TYPE.get(int(raw_type))
    client_type = int(raw_client)
    if lock_type is None or client_type not in (1, 2, 3) or not client_id:
        raise JoplinProtocolError("bad_request")
    if len(client_id.encode("utf-8")) > 128:
        raise JoplinProtocolError("bad_request")
    return lock_type, client_type, client_id


async def _lock_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Serialise lock mutations of one workspace for this transaction."""
    await session.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": str(workspace_id)},
    )


async def _live_rows(session: AsyncSession, workspace_id: uuid.UUID, ttl_s: int) -> list[LockRow]:
    rows = (
        await session.execute(
            sa.text(
                "SELECT id, lock_type, client_type, client_id, created_at, updated_at "
                "  FROM joplin_locks "
                " WHERE workspace_id = :w "
                "   AND updated_at > now() - make_interval(secs => :ttl) "
                " ORDER BY created_at, id"
            ),
            {"w": workspace_id, "ttl": int(ttl_s)},
        )
    ).all()
    return [
        LockRow(
            id=row[0],
            lock_type=str(row[1]),
            client_type=int(row[2]),
            client_id=str(row[3]),
            created_at=row[4],
            updated_at=row[5],
        )
        for row in rows
    ]


async def list_locks(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    *,
    ttl_s: int,
) -> list[LockRow]:
    """Active locks only; expired rows are invisible and reclaimable."""
    async with factory() as session:
        return await _live_rows(session, workspace_id, ttl_s)


async def acquire_lock(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    *,
    lock_type: str,
    client_type: int,
    client_id: str,
    ttl_s: int,
) -> LockRow:
    """Acquire or refresh one lock, or raise a ``has*Lock`` conflict."""
    if lock_type not in LOCK_TYPES:
        raise JoplinProtocolError("bad_request")
    if client_type not in (1, 2, 3) or not client_id:
        raise JoplinProtocolError("bad_request")
    if len(client_id.encode("utf-8")) > 128:
        raise JoplinProtocolError("bad_request")

    try:
        async with factory() as session:
            await _lock_workspace(session, workspace_id)
            # Reclaim first: an expired row must not block a live client, and
            # deleting here keeps the "live" query cheap without a sweeper.
            await session.execute(
                sa.text(
                    "DELETE FROM joplin_locks "
                    " WHERE workspace_id = :w "
                    "   AND updated_at <= now() - make_interval(secs => :ttl)"
                ),
                {"w": workspace_id, "ttl": int(ttl_s)},
            )
            locks = await _live_rows(session, workspace_id, ttl_s)

            for lock in locks:
                if lock.client_type == client_type and lock.client_id == client_id:
                    continue
                if lock_type == LOCK_SYNC and lock.lock_type == LOCK_EXCLUSIVE:
                    raise JoplinProtocolError("has_exclusive_lock")
                if lock_type == LOCK_EXCLUSIVE:
                    raise JoplinProtocolError(
                        "has_sync_lock" if lock.lock_type == LOCK_SYNC else "has_exclusive_lock"
                    )

            now = datetime.datetime.now(datetime.UTC)
            row = (
                await session.execute(
                    sa.text(
                        "INSERT INTO joplin_locks "
                        "(id, workspace_id, lock_type, client_type, client_id, "
                        " created_at, updated_at) "
                        "VALUES (:id, :w, :lock_type, :client_type, :client_id, :now, :now) "
                        "ON CONFLICT (workspace_id, lock_type, client_type, client_id) "
                        "DO UPDATE SET updated_at = :now "
                        "RETURNING id, created_at, updated_at"
                    ),
                    {
                        "id": new_id(),
                        "w": workspace_id,
                        "lock_type": lock_type,
                        "client_type": client_type,
                        "client_id": client_id,
                        "now": now,
                    },
                )
            ).first()
            await session.commit()
    except sa.exc.SQLAlchemyError as exc:  # pragma: no cover - storage failure
        raise JoplinProtocolError("storage_unavailable") from exc

    if row is None:  # pragma: no cover - RETURNING always yields a row
        raise JoplinProtocolError("internal")
    return LockRow(
        id=row[0],
        lock_type=lock_type,
        client_type=client_type,
        client_id=client_id,
        created_at=row[1],
        updated_at=row[2],
    )


async def release_lock(
    factory: async_sessionmaker[AsyncSession],
    workspace_id: uuid.UUID,
    *,
    lock_type: str,
    client_type: int,
    client_id: str,
) -> None:
    """Release one lock; missing is not an error (idempotent delete)."""
    try:
        async with factory() as session:
            await _lock_workspace(session, workspace_id)
            await session.execute(
                sa.text(
                    "DELETE FROM joplin_locks "
                    " WHERE workspace_id = :w AND lock_type = :lock_type "
                    "   AND client_type = :client_type AND client_id = :client_id"
                ),
                {
                    "w": workspace_id,
                    "lock_type": lock_type,
                    "client_type": client_type,
                    "client_id": client_id,
                },
            )
            await session.commit()
    except sa.exc.SQLAlchemyError as exc:  # pragma: no cover - storage failure
        raise JoplinProtocolError("storage_unavailable") from exc


def serialize_lock(lock: LockRow) -> dict[str, object]:
    """The client's ``Lock`` shape (camelCase, epoch milliseconds)."""
    return {
        "id": lock.wire_id,
        "type": LOCK_TYPE_TO_WIRE[lock.lock_type],
        "clientType": lock.client_type,
        "clientId": lock.client_id,
        "updatedTime": int(lock.updated_at.timestamp() * 1000),
    }
