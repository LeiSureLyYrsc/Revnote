"""Phase 5 attachments: one new table, additive only.

Revision ID: 0004_attachments
Revises: 0003_auth_sessions_members

What this revision owns (plan §6.1, interface §7):

* ``attachments`` — ``id``, ``workspace_id`` (RESTRICT FK), ``state``
  (``pending``/``ready``/``deleted``), ``mime``, ``byte_size``, ``content_hash``,
  ``storage_key`` (UNIQUE), ``upload_nonce`` plus the five lifecycle timestamps and
  ``file_name``/``delete_reason``;
* the two indexes the service actually scans with:
  ``(workspace_id, state)`` and ``(workspace_id, created_at)``.

Deliberate non-actions, all asserted by the migration tests:

* the ten existing tables are **not** touched — no column, constraint, index or
  ``change_events``/``op_ledger`` CHECK is rewritten;
* no row is inserted anywhere, no seed, no backfill;
* no ``rev``/``note_id``/``seq`` column: an attachment is not a kernel entity
  (D3), so this table can never appear in a pull page or a snapshot.

``downgrade`` drops exactly this table and its indexes. Nothing else is dropped,
and the table's own rows are the only data lost — which is why the table is
created last and referenced by nothing.

Names are wrapped in :meth:`alembic.operations.Operations.f`, so the literal string
in this file is the final database name and matches what the ORM metadata resolves
to (``alembic check`` stays diff-clean).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_attachments"
down_revision: str | None = "0003_auth_sessions_members"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        # ``TEXT`` + CHECK rather than a PostgreSQL ENUM: adding a state must not
        # require ``ALTER TYPE`` on a live table.
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("mime", sa.Text(), nullable=False),
        # NULL until the upload has been verified; 0 is never legal.
        sa.Column("byte_size", sa.BigInteger(), nullable=True),
        sa.Column("content_hash", postgresql.BYTEA(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("upload_nonce", postgresql.UUID(as_uuid=True), nullable=False),
        # User content: never logged, never in a repr, never part of a key.
        sa.Column("file_name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("upload_started_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("ready_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delete_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_attachments")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_attachments_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        # One row per object key: the key is the identity of the bytes (D5).
        sa.UniqueConstraint("storage_key", name=op.f("uq_attachments_storage_key")),
        sa.CheckConstraint(
            "state IN ('pending', 'ready', 'deleted')",
            name=op.f("ck_attachments_state_known"),
        ),
        sa.CheckConstraint(
            "octet_length(mime) BETWEEN 3 AND 127",
            name=op.f("ck_attachments_mime_len"),
        ),
        sa.CheckConstraint(
            "byte_size IS NULL OR byte_size > 0",
            name=op.f("ck_attachments_byte_size_positive"),
        ),
        sa.CheckConstraint(
            "content_hash IS NULL OR octet_length(content_hash) = 32",
            name=op.f("ck_attachments_content_hash_len"),
        ),
        sa.CheckConstraint(
            "octet_length(storage_key) BETWEEN 8 AND 512",
            name=op.f("ck_attachments_storage_key_len"),
        ),
        # The state machine's single invariant: ready <=> a verified digest exists.
        sa.CheckConstraint(
            "(state = 'ready') = (content_hash IS NOT NULL)",
            name=op.f("ck_attachments_ready_iff_hash"),
        ),
    )
    # GC scan ("expired pending rows of this workspace") and state listing.
    op.create_index(
        op.f("ix_attachments_workspace_id_state"),
        "attachments",
        ["workspace_id", "state"],
    )
    op.create_index(
        op.f("ix_attachments_workspace_id_created_at"),
        "attachments",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    # Only what this revision created, newest index first. A table drop takes its
    # indexes and constraints with it, but dropping them explicitly keeps the
    # upgrade/downgrade pair exactly symmetric and readable.
    op.drop_index(op.f("ix_attachments_workspace_id_created_at"), table_name="attachments")
    op.drop_index(op.f("ix_attachments_workspace_id_state"), table_name="attachments")
    op.drop_table("attachments")
