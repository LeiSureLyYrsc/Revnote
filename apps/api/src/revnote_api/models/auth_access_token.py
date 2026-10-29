"""``auth_access_tokens`` — short-lived opaque access tokens (D1).

Only a **hash** is stored: the service issues 256+ random bits, returns the raw
value exactly once and keeps ``sha256(raw)`` here. A database dump therefore
cannot be replayed as a credential, and neither can a log line (the model's
``__repr__`` never mentions the column).

The token is opaque, not a JWT: verification is one indexed lookup by
``token_hash``, so revocation is immediate — no signature can outlive the row.
``expires_at`` is the 600-second access TTL and is always within the session's
own deadline; the service is what keeps the two consistent because the invariant
spans two tables and cannot be a CHECK.

``ON DELETE RESTRICT`` on the session matches ``auth_sessions``: token history is
part of the audit trail and is only removed with the whole table.
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


class AuthAccessToken(Base):
    """One issued access token; a revoked session invalidates all of its rows."""

    __tablename__ = "auth_access_tokens"
    __table_args__ = (
        sa.PrimaryKeyConstraint("id", name="pk_auth_access_tokens"),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["auth_sessions.id"],
            name="fk_auth_access_tokens_session_id_auth_sessions",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("token_hash", name="uq_auth_access_tokens_token_hash"),
        sa.CheckConstraint(
            f"octet_length(token_hash) = {DIGEST_BYTES}",
            name="token_hash_len",
        ),
        sa.Index("ix_auth_access_tokens_session_id", "session_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    session_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    #: ``sha256`` of the opaque token; the raw value is never persisted.
    token_hash: Mapped[bytes] = mapped_column(postgresql.BYTEA(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
    #: created_at + access TTL (600s); never later than the session's expiry (D1).
    expires_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        # Deliberately silent about token_hash: repr is logged.
        return f"AuthAccessToken(id={self.id!r}, session_id={self.session_id!r})"
