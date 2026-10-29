"""``notes`` — note rows inside one workspace / one folder slot.

``body_md`` is the persisted truth of the body (TipTap JSON is *not* the source of
truth) and is stored verbatim: no Markdown normalisation, no newline or Unicode
folding. ``title`` and ``body_md`` are NOT NULL but **empty strings are legal**.

Same cursor contract as ``folders``: ``updated_seq`` is NOT NULL, has no default,
is checked ``> 0`` and is tied to ``change_events(workspace_id, seq)`` through a
deferrable initially deferred FK (object first, event later, validated at COMMIT).
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base


class Note(Base):
    """A note in one workspace."""

    __tablename__ = "notes"
    __table_args__ = (
        sa.PrimaryKeyConstraint("id", name="pk_notes"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_notes_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        # Composite: a note can never reference a folder of another workspace.
        sa.ForeignKeyConstraint(
            ["workspace_id", "folder_id"],
            ["folders.workspace_id", "folders.id"],
            name="fk_notes_workspace_id_folder_id_folders",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_seq"],
            ["change_events.workspace_id", "change_events.seq"],
            name="fk_notes_workspace_id_updated_seq_change_events",
            ondelete="NO ACTION",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint("rev > 0", name="rev_positive"),
        sa.CheckConstraint("updated_seq > 0", name="updated_seq_positive"),
        sa.Index("ix_notes_workspace_id_updated_seq", "workspace_id", "updated_seq"),
        sa.Index("ix_notes_workspace_id_folder_id", "workspace_id", "folder_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    #: NULL = top level (no folder). Validated together with workspace_id.
    folder_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    title: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    #: Body truth, stored verbatim.
    body_md: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    rev: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False, server_default=sa.text("1"))
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=True
    )
    updated_seq: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"Note(id={self.id!r}, workspace_id={self.workspace_id!r}, "
            f"rev={self.rev!r}, updated_seq={self.updated_seq!r})"
        )
