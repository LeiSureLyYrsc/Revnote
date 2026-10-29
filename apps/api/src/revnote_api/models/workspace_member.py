"""``workspace_members`` — the per-request authorisation row (D5/D20).

Phase 3 stops treating "owns a workspace" as an implicit right to use it. Every
workspace request resolves a membership row instead:

* the primary key is ``(workspace_id, user_id)``, so the hot check
  ("may this user act in this workspace?") is a single index probe and a user
  cannot be present twice;
* ``ix_workspace_members_user_id`` serves the other direction (``GET /me`` lists
  a user's workspaces);
* both foreign keys are ``ON DELETE RESTRICT``. Deleting a user or workspace
  while a membership points at it is a bug in the caller, and a cascade would
  silently widen access — a member row must never be created or removed by a
  side effect.

``role`` is constrained to ``'owner'`` for v1 (**D5**: no member management, no
sharing). The CHECK is written as ``role = 'owner'`` rather than an ``IN`` list on
purpose: widening it later has to be a deliberate migration, not an accident. The
``owner_id`` already on ``workspaces`` remains the kernel's ownership record; the
backfill in revision ``0003`` copies it here so both views agree.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base

#: Roles this phase allows. v1 is owner-only by decision (D5).
MEMBER_ROLES: tuple[str, ...] = ("owner",)


class WorkspaceMember(Base):
    """Membership of one user in one workspace; PK ``(workspace_id, user_id)``."""

    __tablename__ = "workspace_members"
    __table_args__ = (
        sa.PrimaryKeyConstraint("workspace_id", "user_id", name="pk_workspace_members"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_workspace_members_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_workspace_members_user_id_users",
            ondelete="RESTRICT",
        ),
        # Short name; the convention prefixes ``ck_workspace_members_``.
        sa.CheckConstraint("role = 'owner'", name="role_owner_only"),
        # Reverse direction of the PK (``GET /me`` lists a user's workspaces).
        sa.Index("ix_workspace_members_user_id", "user_id"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    #: v1: always ``'owner'``; the CHECK makes any other value impossible.
    role: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"WorkspaceMember(workspace_id={self.workspace_id!r}, "
            f"user_id={self.user_id!r}, role={self.role!r})"
        )
