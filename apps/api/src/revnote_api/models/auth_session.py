"""``auth_sessions`` — one device login **and** one refresh-token family (D4).

A session is created by ``POST /auth/login`` and lives for a fixed absolute
lifetime (30 days by default). Refreshing a token never extends ``expires_at``:
rotation replaces tokens, not the session (D2).

Revocation is a timestamp, never a delete, for two reasons:

* the refresh-reuse rule (D3/R1) must be able to answer "was this family
  revoked?" after the fact;
* ``auth_access_tokens`` / ``auth_refresh_tokens`` reference the row with
  ``ON DELETE RESTRICT``, so history cannot silently disappear.

``revoke_reason`` is an application-owned vocabulary (``logout``,
``refresh_reuse``, ``disabled``, ...). No CHECK enforces it: the set grows with
the auth state machine, and a database constraint would turn a new reason into a
failed login rather than a rejected enum value.

Expiry is *not* a trigger. The auth service compares ``expires_at`` with the
current time on every request; the column exists so a revoke and an expiry look
the same to the reader without a background sweeper.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base


class AuthSession(Base):
    """A device login; the root of one rotating refresh-token family."""

    __tablename__ = "auth_sessions"
    __table_args__ = (
        sa.PrimaryKeyConstraint("id", name="pk_auth_sessions"),
        # RESTRICT: an account with login history is disabled, never deleted
        # into an orphaned token tree.
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_auth_sessions_user_id_users",
            ondelete="RESTRICT",
        ),
        # Every authenticated request starts from the session, so this is the
        # lookups' hot path ("list/verify sessions of this user").
        sa.Index("ix_auth_sessions_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    #: Free-text device label supplied at login; ``NULL`` when unknown.
    device_name: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
    #: Absolute deadline (created_at + 30d). Never extended by a refresh (D2).
    expires_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=False
    )
    #: ``NULL`` = active; a timestamp = revoked (logout, reuse detection, ...).
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=True
    )
    revoke_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"AuthSession(id={self.id!r}, user_id={self.user_id!r}, "
            f"revoked={self.revoked_at is not None!r})"
        )
