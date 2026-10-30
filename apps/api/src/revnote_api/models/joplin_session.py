"""``joplin_sessions`` — a Joplin-shaped session on top of a Revnote account.

Why this table exists rather than reusing ``auth_access_tokens``: a Joplin
client receives one ``session.id`` at login and sends it as ``X-API-AUTH`` for
weeks. A Revnote access token lives ten minutes and its refresh token rotates,
so neither is a usable bridge credential. The bridge therefore issues its own
opaque token, stores only its SHA-256 digest (never the token itself) and fixes
the workspace for the lifetime of the session — a Joplin account is one sync
space, and silently switching workspaces between requests would be data loss.

Login still verifies the password through the real :class:`AuthService`; the
short-lived Revnote pair it issues is revoked immediately, so no extra live
credential is left behind.

Revocation is a row update, not a delete: ``DELETE /api/sessions/<id>`` must be
idempotent and an audit trail of "this token existed" is worth more than the
row.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base
from revnote_api.models.attachment import CONTENT_HASH_BYTES


class JoplinSession(Base):
    """One opaque bridge credential bound to a user and a workspace."""

    __tablename__ = "joplin_sessions"
    __table_args__ = (
        sa.PrimaryKeyConstraint("id", name="pk_joplin_sessions"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_joplin_sessions_user_id_users",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_joplin_sessions_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        # The digest *is* the credential: one row per token.
        sa.UniqueConstraint("token_hash", name="uq_joplin_sessions_token_hash"),
        sa.CheckConstraint(
            f"octet_length(token_hash) = {CONTENT_HASH_BYTES}",
            name="token_hash_len",
        ),
        # "Still usable?" is one indexed predicate.
        sa.Index("ix_joplin_sessions_expires_at", "expires_at"),
        sa.Index("ix_joplin_sessions_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    #: SHA-256 of the opaque token; the token itself is never stored.
    token_hash: Mapped[bytes] = mapped_column(postgresql.BYTEA(), nullable=False)
    #: Client-supplied platform label, bounded. Never used for authorisation.
    client_name: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime.datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        # The digest is credential material: absent from the repr on purpose.
        return (
            f"JoplinSession(id={self.id!r}, user_id={self.user_id!r}, "
            f"workspace_id={self.workspace_id!r})"
        )


__all__ = ["JoplinSession"]
