"""SQL used by the bridge's item routes.

Everything here reads or writes the *native* tables through plain SQL, in the
same style as the services: explicit columns, explicit parameters, no ORM
identity map. The one bridge-owned table touched here is ``joplin_id_map``.

Why a separate module: the routes stay a translation layer (parse the protocol,
call a command, render a response), and the statements are testable against a
real PostgreSQL without booting an app.

Writes to notes and folders never happen here — they go through
:class:`~revnote_api.services.sync_write.SyncWriteService`, because the kernel
is the only writer (seq allocation, ledger, events, invariants). This module
only decides *which* command to send.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from revnote_api.adapters.joplin.ids import jop_key_for_uuid, uuid_from_jop_key
from revnote_api.kernel.ids import new_id

__all__ = [
    "AttachmentRow",
    "FolderRow",
    "ItemMeta",
    "NoteRow",
    "default_workspace",
    "ensure_mapping",
    "item_metas",
    "list_items",
    "load_attachment",
    "load_folder",
    "load_note",
    "mapping_key",
    "record_mapping",
    "resolve_mapping",
]


@dataclass(frozen=True, slots=True)
class NoteRow:
    id: uuid.UUID
    folder_id: uuid.UUID | None
    title: str
    body_md: str
    rev: int
    deleted_at: datetime.datetime | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class FolderRow:
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    rev: int
    deleted_at: datetime.datetime | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class AttachmentRow:
    id: uuid.UUID
    state: str
    mime: str
    byte_size: int | None
    file_name: str | None
    created_at: datetime.datetime
    ready_at: datetime.datetime | None


@dataclass(frozen=True, slots=True)
class ItemMeta:
    """What a stat/delta entry needs about one entity."""

    entity_id: uuid.UUID
    kind: str
    jop_key: str
    title: str
    parent_id: uuid.UUID | None
    created_at: datetime.datetime
    updated_at: datetime.datetime


# --------------------------------------------------------------------------- #
# mapping
# --------------------------------------------------------------------------- #


async def default_workspace(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID | None:
    """The workspace a Joplin account is bound to: its earliest membership.

    A Joplin account synchronises into exactly one space. Picking the earliest
    membership (tie-broken by id) is deterministic, and the choice is frozen on
    the session row at login so it cannot change under a running client.
    """
    row = (
        await session.execute(
            sa.text(
                "SELECT workspace_id FROM workspace_members "
                " WHERE user_id = :user_id "
                " ORDER BY created_at, workspace_id LIMIT 1"
            ),
            {"user_id": user_id},
        )
    ).first()
    return None if row is None else row[0]


async def resolve_mapping(
    session: AsyncSession, workspace_id: uuid.UUID, jop_key: str
) -> tuple[str, uuid.UUID] | None:
    """``jop_key`` -> ``(kind, entity_id)``, from the map table or the UUID shape."""
    row = (
        await session.execute(
            sa.text(
                "SELECT entity_kind, entity_id FROM joplin_id_map "
                " WHERE workspace_id = :workspace_id AND jop_key = :jop_key"
            ),
            {"workspace_id": workspace_id, "jop_key": jop_key},
        )
    ).first()
    if row is not None:
        return str(row[0]), row[1]

    # No row: a native entity is addressed by its own uuid.hex. The entity is
    # looked up by the caller, so a key that names nothing simply resolves to
    # nothing.
    candidate = uuid_from_jop_key(jop_key)
    if candidate is None:
        return None
    # The kind is discovered by the caller's load; return a sentinel by trying
    # notes, then folders, then attachments.
    for kind, statement in (
        ("note", "SELECT 1 FROM notes WHERE workspace_id = :w AND id = :id"),
        ("folder", "SELECT 1 FROM folders WHERE workspace_id = :w AND id = :id"),
        ("attachment", "SELECT 1 FROM attachments WHERE workspace_id = :w AND id = :id"),
    ):
        found = (
            await session.execute(sa.text(statement), {"w": workspace_id, "id": candidate})
        ).first()
        if found is not None:
            return kind, candidate
    return None


async def ensure_mapping(
    session: AsyncSession, workspace_id: uuid.UUID, jop_key: str, entity_kind: str
) -> uuid.UUID:
    """Return the entity id for ``jop_key``, creating the mapping if needed.

    Written **before** the entity is created, so a retry after a lost response
    resolves the same row instead of creating a duplicate. A mapping that never
    became an entity is reused by the next attempt with the same id, which is
    the self-healing case rather than a leak.
    """
    existing = (
        await session.execute(
            sa.text(
                "SELECT entity_id FROM joplin_id_map "
                " WHERE workspace_id = :workspace_id AND jop_key = :jop_key"
            ),
            {"workspace_id": workspace_id, "jop_key": jop_key},
        )
    ).first()
    if existing is not None:
        return existing[0]

    entity_id = new_id()
    await session.execute(
        sa.text(
            "INSERT INTO joplin_id_map (id, workspace_id, jop_key, entity_kind, entity_id) "
            "VALUES (:id, :workspace_id, :jop_key, :entity_kind, :entity_id) "
            "ON CONFLICT (workspace_id, jop_key) DO NOTHING"
        ),
        {
            "id": new_id(),
            "workspace_id": workspace_id,
            "jop_key": jop_key,
            "entity_kind": entity_kind,
            "entity_id": entity_id,
        },
    )
    winner = (
        await session.execute(
            sa.text(
                "SELECT entity_id FROM joplin_id_map "
                " WHERE workspace_id = :workspace_id AND jop_key = :jop_key"
            ),
            {"workspace_id": workspace_id, "jop_key": jop_key},
        )
    ).first()
    await session.commit()
    if winner is None:  # pragma: no cover - the INSERT above guarantees a row
        raise RuntimeError("mapping insert vanished")
    return winner[0]


async def record_mapping(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    jop_key: str,
    entity_kind: str,
    entity_id: uuid.UUID,
) -> None:
    """Bind an already-created entity to a key (used by the resource path)."""
    await session.execute(
        sa.text(
            "INSERT INTO joplin_id_map (id, workspace_id, jop_key, entity_kind, entity_id) "
            "VALUES (:id, :workspace_id, :jop_key, :entity_kind, :entity_id) "
            "ON CONFLICT (workspace_id, jop_key) DO NOTHING"
        ),
        {
            "id": new_id(),
            "workspace_id": workspace_id,
            "jop_key": jop_key,
            "entity_kind": entity_kind,
            "entity_id": entity_id,
        },
    )
    await session.commit()


async def mapping_key(
    session: AsyncSession, workspace_id: uuid.UUID, kind: str, entity_id: uuid.UUID
) -> str:
    """The Joplin key of an entity: its map row, else ``uuid.hex``."""
    row = (
        await session.execute(
            sa.text(
                "SELECT jop_key FROM joplin_id_map "
                " WHERE workspace_id = :workspace_id AND entity_kind = :entity_kind "
                "   AND entity_id = :entity_id"
            ),
            {"workspace_id": workspace_id, "entity_kind": kind, "entity_id": entity_id},
        )
    ).first()
    return jop_key_for_uuid(entity_id) if row is None else str(row[0])


# --------------------------------------------------------------------------- #
# entities
# --------------------------------------------------------------------------- #


async def load_note(
    session: AsyncSession, workspace_id: uuid.UUID, note_id: uuid.UUID
) -> NoteRow | None:
    row = (
        await session.execute(
            sa.text(
                "SELECT id, folder_id, title, body_md, rev, deleted_at, created_at, updated_at "
                "  FROM notes WHERE workspace_id = :w AND id = :id"
            ),
            {"w": workspace_id, "id": note_id},
        )
    ).first()
    if row is None:
        return None
    return NoteRow(
        id=row[0],
        folder_id=row[1],
        title=row[2],
        body_md=row[3],
        rev=int(row[4]),
        deleted_at=row[5],
        created_at=row[6],
        updated_at=row[7],
    )


async def load_folder(
    session: AsyncSession, workspace_id: uuid.UUID, folder_id: uuid.UUID
) -> FolderRow | None:
    row = (
        await session.execute(
            sa.text(
                "SELECT id, parent_id, name, rev, deleted_at, created_at, updated_at "
                "  FROM folders WHERE workspace_id = :w AND id = :id"
            ),
            {"w": workspace_id, "id": folder_id},
        )
    ).first()
    if row is None:
        return None
    return FolderRow(
        id=row[0],
        parent_id=row[1],
        name=row[2],
        rev=int(row[3]),
        deleted_at=row[4],
        created_at=row[5],
        updated_at=row[6],
    )


async def load_attachment(
    session: AsyncSession, workspace_id: uuid.UUID, attachment_id: uuid.UUID
) -> AttachmentRow | None:
    row = (
        await session.execute(
            sa.text(
                "SELECT id, state, mime, byte_size, file_name, created_at, ready_at "
                "  FROM attachments WHERE workspace_id = :w AND id = :id"
            ),
            {"w": workspace_id, "id": attachment_id},
        )
    ).first()
    if row is None:
        return None
    return AttachmentRow(
        id=row[0],
        state=row[1],
        mime=row[2],
        byte_size=row[3],
        file_name=row[4],
        created_at=row[5],
        ready_at=row[6],
    )


async def item_metas(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    wanted: list[tuple[str, uuid.UUID]],
) -> dict[tuple[str, uuid.UUID], ItemMeta]:
    """Bulk-load the metadata of the entities a delta page mentions."""
    note_ids = [entity for kind, entity in wanted if kind == "note"]
    folder_ids = [entity for kind, entity in wanted if kind == "folder"]
    found: dict[tuple[str, uuid.UUID], ItemMeta] = {}

    if note_ids:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT n.id, n.title, n.folder_id, n.created_at, n.updated_at, "
                    "       COALESCE(m.jop_key, '') "
                    "  FROM notes AS n "
                    "  LEFT JOIN joplin_id_map AS m "
                    "    ON m.workspace_id = n.workspace_id AND m.entity_kind = 'note' "
                    "   AND m.entity_id = n.id "
                    " WHERE n.workspace_id = :w AND n.id = ANY(:ids)"
                ),
                {"w": workspace_id, "ids": note_ids},
            )
        ).all()
        for row in rows:
            found[("note", row[0])] = ItemMeta(
                entity_id=row[0],
                kind="note",
                jop_key=str(row[5]) or jop_key_for_uuid(row[0]),
                title=row[1],
                parent_id=row[2],
                created_at=row[3],
                updated_at=row[4],
            )

    if folder_ids:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT f.id, f.name, f.parent_id, f.created_at, f.updated_at, "
                    "       COALESCE(m.jop_key, '') "
                    "  FROM folders AS f "
                    "  LEFT JOIN joplin_id_map AS m "
                    "    ON m.workspace_id = f.workspace_id AND m.entity_kind = 'folder' "
                    "   AND m.entity_id = f.id "
                    " WHERE f.workspace_id = :w AND f.id = ANY(:ids)"
                ),
                {"w": workspace_id, "ids": folder_ids},
            )
        ).all()
        for row in rows:
            found[("folder", row[0])] = ItemMeta(
                entity_id=row[0],
                kind="folder",
                jop_key=str(row[5]) or jop_key_for_uuid(row[0]),
                title=row[1],
                parent_id=row[2],
                created_at=row[3],
                updated_at=row[4],
            )
    return found


async def list_items(session: AsyncSession, workspace_id: uuid.UUID) -> list[ItemMeta]:
    """Every active note and folder in the workspace (the ``/children`` view)."""
    rows = (
        await session.execute(
            sa.text(
                "SELECT 'note' AS kind, n.id, n.title, n.folder_id AS parent_id, "
                "       n.created_at, n.updated_at, COALESCE(m.jop_key, '') "
                "  FROM notes AS n "
                "  LEFT JOIN joplin_id_map AS m "
                "    ON m.workspace_id = n.workspace_id AND m.entity_kind = 'note' "
                "   AND m.entity_id = n.id "
                " WHERE n.workspace_id = :w AND n.deleted_at IS NULL "
                " UNION ALL "
                "SELECT 'folder' AS kind, f.id, f.name, f.parent_id, f.created_at, f.updated_at, "
                "       COALESCE(m.jop_key, '') "
                "  FROM folders AS f "
                "  LEFT JOIN joplin_id_map AS m "
                "    ON m.workspace_id = f.workspace_id AND m.entity_kind = 'folder' "
                "   AND m.entity_id = f.id "
                " WHERE f.workspace_id = :w AND f.deleted_at IS NULL "
                " ORDER BY 5, 2"
            ),
            {"w": workspace_id},
        )
    ).all()
    return [
        ItemMeta(
            entity_id=row[1],
            kind=str(row[0]),
            jop_key=str(row[6]) or jop_key_for_uuid(row[1]),
            title=row[2],
            parent_id=row[3],
            created_at=row[4],
            updated_at=row[5],
        )
        for row in rows
    ]
