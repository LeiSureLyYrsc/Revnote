"""``change_events`` — the append-only per-workspace operation log.

One successful operation = exactly one event (D6). ``seq`` is allocated from
``workspaces.last_seq`` while that row is locked, therefore it is dense per
workspace and has **no** default, sequence or identity column.

``entity_id`` is a polymorphic reference (``entity_type`` in ``('folder','note')``)
and intentionally has **no** FK — the plan forbids faking a normal FK for it. The
composite PK ``(workspace_id, seq)`` doubles as the cursor-scan index, so no
duplicate index on the same columns is created.

There is no update or delete path in the kernel: rows are immutable.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base

ENTITY_TYPES: tuple[str, ...] = ("folder", "note")
OPERATIONS: tuple[str, ...] = ("create", "update", "delete", "move")


class ChangeEvent(Base):
    """An immutable, ordered record of one successful write."""

    __tablename__ = "change_events"
    __table_args__ = (
        sa.PrimaryKeyConstraint("workspace_id", "seq", name="pk_change_events"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_change_events_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        # Idempotency key: one op can never record two events.
        sa.UniqueConstraint("workspace_id", "op_id", name="uq_change_events_workspace_id_op_id"),
        sa.CheckConstraint("seq > 0", name="seq_positive"),
        sa.CheckConstraint("entity_type IN ('folder', 'note')", name="entity_type_known"),
        sa.CheckConstraint("op IN ('create', 'update', 'delete', 'move')", name="op_known"),
        sa.CheckConstraint("rev > 0", name="rev_positive"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    #: Dense per-workspace position, allocated under the workspace lock.
    seq: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    entity_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    #: Polymorphic target; deliberately not backed by a foreign key.
    entity_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    op: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    #: The entity ``rev`` this event produced.
    rev: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    #: Client-supplied idempotency id (part of the ledger key as well).
    op_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"ChangeEvent(workspace_id={self.workspace_id!r}, seq={self.seq!r}, "
            f"{self.entity_type}={self.entity_id!r}, op={self.op!r})"
        )
