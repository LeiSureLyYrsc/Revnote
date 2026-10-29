"""``users`` — base identity row (no auth fields in Phase 2).

Deliberately minimal: this table only exists so ``workspaces.owner_id`` has a
referential target. There is **no** registration, credential, session or
authorisation column, and no e-mail case folding or trimming — the plan keeps
plain PostgreSQL ``TEXT`` unique semantics.

The single CHECK here is a *blankness* guard only (``btrim(email) <> ''``): the
plan requires a non-empty e-mail, so whitespace-only values are rejected at the
database. It does not normalise, trim, fold case or validate syntax.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base


class User(Base):
    """A person that may own workspaces."""

    __tablename__ = "users"
    __table_args__ = (
        # Explicit names: FKs and error messages can point at them, and
        # Postgres cannot pick a different name after a dump/restore.
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        # Short name -> convention expands it to ``ck_users_email_not_blank``.
        # Rejects '' and whitespace-only; no trimming is applied to the value.
        sa.CheckConstraint("btrim(email) <> ''", name="email_not_blank"),
    )

    #: Application-side UUIDv7 (D1): the database never generates UUIDs here.
    id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    email: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"User(id={self.id!r})"
