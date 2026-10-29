"""``users`` — identity row plus the two Phase 3 authentication columns.

Phase 2 kept this table deliberately minimal (``id``/``email``/``created_at``) so
that ``workspaces.owner_id`` had a referential target. Phase 3 extends it **in
place** rather than creating a parallel credentials table:

* ``password_hash`` (nullable) holds the Argon2id digest. It is nullable on
  purpose: rows that existed before Phase 3 keep ``NULL`` and therefore cannot
  log in locally (D8). A nullable column also means no backfill has to invent a
  credential during the migration.
* ``disabled_at`` (nullable) is the account switch. ``NULL`` means enabled; a
  timestamp disables login without deleting the row (and without touching the
  workspaces it owns through ``ON DELETE RESTRICT``).

E-mail handling changes from "plain ``TEXT`` unique" to a **functional unique
index** over ``lower(btrim(email))`` (D8):

* the stored value is never rewritten — no trimming, no case folding, no trigger;
* two addresses that differ only in case or surrounding spaces therefore collide,
  which is exactly the "one account per human" rule;
* the old ``uq_users_email`` constraint disappears, so the only uniqueness the
  database enforces is the normalised one.

The CHECK is still a *blankness* guard only (``btrim(email) <> ''``): it does not
normalise, trim, fold case or validate syntax.

Only the digest is stored. :meth:`User.__repr__` deliberately names neither the
hash nor the address, so a stray log line cannot leak a credential or PII.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base


class User(Base):
    """A person that may own workspaces and log in."""

    __tablename__ = "users"
    __table_args__ = (
        # Explicit names: FKs and error messages can point at them, and
        # Postgres cannot pick a different name after a dump/restore.
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        # Short name -> convention expands it to ``ck_users_email_not_blank``.
        # Rejects '' and whitespace-only; no trimming is applied to the value.
        sa.CheckConstraint("btrim(email) <> ''", name="email_not_blank"),
        # Case/space-insensitive uniqueness (D8). Named ``uq_`` because it *is*
        # the uniqueness rule for this table; it is an Index, not a constraint,
        # because PostgreSQL cannot put an expression in UNIQUE (...).
        sa.Index("uq_users_email_normalized", sa.text("lower(btrim(email))"), unique=True),
    )

    #: Application-side UUIDv7 (D1): the database never generates UUIDs here.
    id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    email: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    #: Argon2id digest. ``NULL`` = legacy row that may not log in locally (D8).
    password_hash: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    #: ``NULL`` = enabled; a timestamp blocks login without deleting the row.
    disabled_at: Mapped[datetime.datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        # Never include email or password_hash: repr ends up in logs.
        return f"User(id={self.id!r}, disabled={self.disabled_at is not None!r})"
