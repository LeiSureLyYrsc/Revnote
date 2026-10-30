"""Phase 6: the Joplin bridge's three additive tables.

Revision ID: 0005_joplin_bridge
Revises: 0004_attachments

What this revision owns (plan D5):

* ``joplin_id_map`` — a Joplin item key bound to one native UUID, plus the
  ``(workspace_id, entity_kind, entity_id)`` reverse direction;
* ``joplin_locks`` — sync/exclusive locks, isolated from ``change_events``;
* ``joplin_sessions`` — the bridge's opaque session credentials (digest only).

Deliberate non-actions, asserted by the migration tests:

* the eleven existing tables are **not** touched: no column, constraint,
  index or ``change_events`` CHECK is rewritten, and no row is inserted;
* nothing here is a kernel entity. A lock or a mapping write never moves
  ``workspaces.last_seq`` and never appears in a pull page.

``downgrade`` drops exactly the three tables and their indexes. They reference
``workspaces``/``users`` but nothing references them, so they are dropped
first and in any order.

Names are wrapped in :meth:`alembic.operations.Operations.f` so the literal
string is the final database name and matches the ORM metadata
(``alembic check`` stays diff-clean).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_joplin_bridge"
down_revision: str | None = "0004_attachments"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "joplin_id_map",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        # 32 lowercase hex characters; the client enforces the same shape.
        sa.Column("jop_key", sa.Text(), nullable=False),
        sa.Column("entity_kind", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_joplin_id_map")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_joplin_id_map_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "jop_key",
            name=op.f("uq_joplin_id_map_workspace_id_jop_key"),
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "entity_kind",
            "entity_id",
            name=op.f("uq_joplin_id_map_workspace_id_entity_kind_entity_id"),
        ),
        sa.CheckConstraint(
            "entity_kind IN ('folder', 'note', 'attachment')",
            name=op.f("ck_joplin_id_map_entity_kind_known"),
        ),
        sa.CheckConstraint(
            "octet_length(jop_key) BETWEEN 32 AND 32",
            name=op.f("ck_joplin_id_map_jop_key_len"),
        ),
        sa.CheckConstraint(
            "jop_key ~ '^[0-9a-f]{32}$'",
            name=op.f("ck_joplin_id_map_jop_key_hex"),
        ),
    )

    op.create_table(
        "joplin_locks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lock_type", sa.Text(), nullable=False),
        sa.Column("client_type", sa.BigInteger(), nullable=False),
        sa.Column("client_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_joplin_locks")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_joplin_locks_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "lock_type",
            "client_type",
            "client_id",
            name=op.f("uq_joplin_locks_workspace_id_lock_type_client_type_client_id"),
        ),
        sa.CheckConstraint(
            "lock_type IN ('sync', 'exclusive')",
            name=op.f("ck_joplin_locks_lock_type_known"),
        ),
        sa.CheckConstraint(
            "client_type IN (1, 2, 3)",
            name=op.f("ck_joplin_locks_client_type_known"),
        ),
        sa.CheckConstraint(
            "octet_length(client_id) BETWEEN 1 AND 128",
            name=op.f("ck_joplin_locks_client_id_len"),
        ),
    )
    op.create_index(
        op.f("ix_joplin_locks_workspace_id_updated_at"),
        "joplin_locks",
        ["workspace_id", "updated_at"],
        unique=False,
    )

    op.create_table(
        "joplin_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        # SHA-256 digest only; the token itself never reaches the database.
        sa.Column("token_hash", postgresql.BYTEA(), nullable=False),
        sa.Column("client_name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_joplin_sessions")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_joplin_sessions_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_joplin_sessions_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("token_hash", name=op.f("uq_joplin_sessions_token_hash")),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32",
            name=op.f("ck_joplin_sessions_token_hash_len"),
        ),
    )
    op.create_index(
        op.f("ix_joplin_sessions_expires_at"),
        "joplin_sessions",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_joplin_sessions_user_id"),
        "joplin_sessions",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_joplin_sessions_user_id"), table_name="joplin_sessions")
    op.drop_index(op.f("ix_joplin_sessions_expires_at"), table_name="joplin_sessions")
    op.drop_table("joplin_sessions")
    op.drop_index(op.f("ix_joplin_locks_workspace_id_updated_at"), table_name="joplin_locks")
    op.drop_table("joplin_locks")
    op.drop_table("joplin_id_map")
