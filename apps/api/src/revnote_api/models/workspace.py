"""``workspaces`` — the sync scope and the only sequence allocator.

Every note/folder mutation happens inside exactly one workspace, and ``last_seq``
is handed out while the workspace row is locked ``FOR UPDATE`` (Phase 2 step 2).
``last_seq`` is a *per-workspace counter*, never a global ``change_seq`` and
never a clock.

``min_retained_seq`` is a placeholder for a future retention/compaction window:
Phase 2 seeds it to ``1`` and **never advances it** (D7). It exists so the
invariant "nothing below the floor is still referenced" has a home later, and so
that the floor can never overtake the allocated sequence.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base


class Workspace(Base):
    """A sync scope owned by one user."""

    __tablename__ = "workspaces"
    __table_args__ = (
        sa.PrimaryKeyConstraint("id", name="pk_workspaces"),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name="fk_workspaces_owner_id_users",
            ondelete="RESTRICT",
        ),
        # Short names; the convention prefixes ``ck_workspaces_``.
        sa.CheckConstraint("last_seq >= 0", name="last_seq_non_negative"),
        sa.CheckConstraint("min_retained_seq >= 1", name="min_retained_seq_min"),
        sa.CheckConstraint(
            "min_retained_seq - 1 <= last_seq",
            name="min_retained_seq_within_last_seq",
        ),
        sa.Index("ix_workspaces_owner_id", "owner_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    #: Highest allocated ``change_events.seq`` for this workspace (0 = empty).
    last_seq: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, server_default=sa.text("0")
    )
    #: Retention floor placeholder; default 1 and never advanced in Phase 2.
    min_retained_seq: Mapped[int] = mapped_column(
        sa.BigInteger(), nullable=False, server_default=sa.text("1")
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"Workspace(id={self.id!r}, last_seq={self.last_seq!r})"
