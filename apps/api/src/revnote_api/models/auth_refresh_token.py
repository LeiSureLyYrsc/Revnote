"""``auth_refresh_tokens`` — rotating refresh tokens with reuse detection (D3/R1).

Rotation is modelled as a chain, not a mutation:

* ``parent_id`` points at the token that was exchanged for this one, so an
  entire family is walkable from any member;
* the **used** row is kept with ``used_at`` set instead of being deleted. That
  row is the reuse tripwire: presenting a token whose ``used_at`` is already set
  means someone replayed an old credential;
* reuse revokes the whole session (= family, D4) and only then answers
  ``401 token_reused``. Storing history is what makes "no grace window" (D3)
  enforceable rather than aspirational.

Like access tokens, only ``sha256(raw)`` is stored (32 bytes, CHECK-enforced),
and ``__repr__`` never prints it. ``expires_at`` never exceeds the session's
deadline, and a refresh never extends it (D2).
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base

#: Length of the stored SHA-256 digest in bytes.
DIGEST_BYTES = 32


class AuthRefreshToken(Base):
    """One refresh token in a session's rotation chain."""

    __tablename__ = "auth_refresh_tokens"
    __table_args__ = (
        sa.PrimaryKeyConstraint("id", name="pk_auth_refresh_tokens"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["auth_sessions.id"],
            name="fk_auth_refresh_tokens_session_id_auth_sessions",
            ondelete="RESTRICT",
        ),
        # Self-reference: NULL for the first token of a session, then the token
        # it rotated. RESTRICT because history is never rewritten.
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["auth_refresh_tokens.id"],
            name="fk_auth_refresh_tokens_parent_id_auth_refresh_tokens",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("token_hash", name="uq_auth_refresh_tokens_token_hash"),
        sa.CheckConstraint(
            f"octet_length(token_hash) = {DIGEST_BYTES}",
            name="token_hash_len",
        ),
        sa.Index("ix_auth_refresh_tokens_session_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    #: ``sha256`` of the opaque refresh token; the raw value is never persisted.
    token_hash: Mapped[bytes] = mapped_column(postgresql.BYTEA(), nullable=False)
    #: Token this one rotated from; ``NULL`` for the login-issued token.
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        postgresql.UUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
    #: Deadline: capped by the session's absolute expiry, never extended (D2).
    expires_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=False
    )
    #: Set exactly when the token is exchanged. A second exchange is a reuse (R1).
    used_at: Mapped[datetime.datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        # Deliberately silent about token_hash: repr is logged.
        return (
            f"AuthRefreshToken(id={self.id!r}, session_id={self.session_id!r}, "
            f"used={self.used_at is not None!r})"
        )
