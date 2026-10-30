"""``joplin_id_map`` — the bridge's mapping between a Joplin item key and a UUID.

The native UUID stays authoritative (D6): this table only records a *second
name* for an entity, never a second identity. Rows exist in exactly one case —
a Joplin client created (or first named) an item with its own 32-hex key — so a
native entity that was never touched through the bridge needs no row at all:
its Joplin key is simply ``uuid.hex`` (32 lowercase hex characters, which is
the shape Joplin's own ``unserialize`` enforces).

``entity_kind`` is deliberately three values rather than the kernel's two: an
attachment is not a sync entity, but a ``.resource/<key>`` blob still needs a
name that maps back to a row in ``attachments``.

Nothing here touches ``change_events``: writing a mapping is bookkeeping, not a
workspace operation, and it must never advance ``workspaces.last_seq``.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base

#: Entity kinds a Joplin key may name.
JOPLIN_ENTITY_KINDS: tuple[str, ...] = ("folder", "note", "attachment")

#: A Joplin item id is 32 hex characters (validated by the client itself).
JOP_KEY_MIN_BYTES = 32
JOP_KEY_MAX_BYTES = 32


class JoplinIdMap(Base):
    """One Joplin item key bound to one native entity."""

    __tablename__ = "joplin_id_map"
    __table_args__ = (
        sa.PrimaryKeyConstraint("id", name="pk_joplin_id_map"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_joplin_id_map_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        # A key names one entity, and one entity has one key: both directions.
        sa.UniqueConstraint(
            "workspace_id", "jop_key", name="uq_joplin_id_map_workspace_id_jop_key"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "entity_kind",
            "entity_id",
            name="uq_joplin_id_map_workspace_id_entity_kind_entity_id",
        ),
        sa.CheckConstraint(
            "entity_kind IN ('folder', 'note', 'attachment')",
            name="entity_kind_known",
        ),
        # Lowercase hex only: the server compares keys case-sensitively, and two
        # spellings of one id would defeat the uniqueness constraint above.
        sa.CheckConstraint(
            f"octet_length(jop_key) BETWEEN {JOP_KEY_MIN_BYTES} AND {JOP_KEY_MAX_BYTES}",
            name="jop_key_len",
        ),
        sa.CheckConstraint("jop_key ~ '^[0-9a-f]{32}$'", name="jop_key_hex"),
    )

    id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    jop_key: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    entity_kind: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"JoplinIdMap(workspace_id={self.workspace_id!r}, "
            f"entity_kind={self.entity_kind!r}, entity_id={self.entity_id!r})"
        )


__all__ = ["JOPLIN_ENTITY_KINDS", "JOP_KEY_MAX_BYTES", "JOP_KEY_MIN_BYTES", "JoplinIdMap"]
