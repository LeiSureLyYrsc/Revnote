"""``folders`` — adjacency-tree folder inside one workspace.

Shape decisions that matter for the sync kernel:

* ``(workspace_id, id)`` is unique so the *composite* foreign keys
  ``parent_id`` / ``notes.folder_id`` can never point across workspaces.
* ``updated_seq`` is NOT NULL with **no default**: it is always the
  ``change_events.seq`` of the operation that last touched this row. It is a
  cursor, not a timestamp.
* ``updated_seq`` is enforced by a **deferrable initially deferred** FK to
  ``change_events(workspace_id, seq)`` — the object row is written first and the
  event row later in the same transaction, so validation must happen at COMMIT.
* ``parent_id`` uses ``RESTRICT``: no ORM/DB cascade. Deleting a non-empty folder
  is refused by the service (D6), never silently propagated.
* ``deleted_at`` + ``updated_seq`` form the tombstone (D7); rows are never
  physically deleted by the kernel.

No ``relationship()`` is declared anywhere in the models: cascades and cycles must
stay explicit SQL, and the service layer is the only writer.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base


class Folder(Base):
    """A folder in the per-workspace tree."""

    __tablename__ = "folders"
    __table_args__ = (
        sa.PrimaryKeyConstraint("id", name="pk_folders"),
        # Enforces the cross-workspace guarantee for parent_id / notes.folder_id.
        sa.UniqueConstraint("workspace_id", "id", name="uq_folders_workspace_id_id"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_folders_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "parent_id"],
            ["folders.workspace_id", "folders.id"],
            name="fk_folders_workspace_id_parent_id_folders",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_seq"],
            ["change_events.workspace_id", "change_events.seq"],
            name="fk_folders_workspace_id_updated_seq_change_events",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint("btrim(name) <> ''", name="name_not_blank"),
        sa.CheckConstraint("rev > 0", name="rev_positive"),
        sa.CheckConstraint("updated_seq > 0", name="updated_seq_positive"),
        sa.CheckConstraint("parent_id IS NULL OR parent_id <> id", name="parent_id_not_self"),
        # Cursor scan (sync) and child lookup / cycle check.
        sa.Index("ix_folders_workspace_id_updated_seq", "workspace_id", "updated_seq"),
        sa.Index("ix_folders_workspace_id_parent_id", "workspace_id", "parent_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    #: NULL = tree root. Ancestor cycles are checked by the service inside the
    #: workspace lock; the DB only guarantees the immediate self-loop is impossible.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    #: Optimistic-concurrency counter; 1 at creation, +1 per successful op.
    rev: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False, server_default=sa.text("1"))
    #: Tombstone marker (D7). NULL means active.
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=True
    )
    #: ``change_events.seq`` of the op that last touched this row (no default).
    updated_seq: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
    #: Set explicitly by the service on every write (never a DB trigger).
    updated_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"Folder(id={self.id!r}, workspace_id={self.workspace_id!r}, "
            f"rev={self.rev!r}, updated_seq={self.updated_seq!r})"
        )
