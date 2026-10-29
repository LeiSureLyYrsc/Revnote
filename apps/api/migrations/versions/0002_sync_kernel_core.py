"""Phase 2 sync kernel core: six domain tables.

Revision ID: 0002_sync_kernel_core
Revises: 0001_baseline

Creates the sync kernel schema (plan §4) in dependency order:

``users`` - ``workspaces`` - ``folders`` - ``notes`` - ``change_events`` -
``op_ledger``, then a second ALTER pass adds the two **deferrable initially
deferred** ``updated_seq -> change_events(seq)`` foreign keys, because the object
row is written before the event that names its sequence.

``downgrade`` reverses exactly that: deferred FKs first, then tables
children-first, so the revision is reversible on a disposable database.

No seeds, no extensions, no RLS, no triggers, no sequences/identity columns and
never ``create_all()``. Names are wrapped in :meth:`alembic.operations.Operations.f`
so the literal string in this file is the final database name — identical to the
name the ORM metadata resolves to (``alembic check`` stays diff-clean).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_sync_kernel_core"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------ users
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
        sa.CheckConstraint("btrim(email) <> ''", name=op.f("ck_users_email_not_blank")),
    )

    # ------------------------------------------------------------- workspaces
    op.create_table(
        "workspaces",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "last_seq",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "min_retained_seq",
            sa.BigInteger(),
            server_default=sa.text("1"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_workspaces_owner_id_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("last_seq >= 0", name=op.f("ck_workspaces_last_seq_non_negative")),
        sa.CheckConstraint(
            "min_retained_seq >= 1", name=op.f("ck_workspaces_min_retained_seq_min")
        ),
        sa.CheckConstraint(
            "min_retained_seq - 1 <= last_seq",
            name=op.f("ck_workspaces_min_retained_seq_within_last_seq"),
        ),
    )
    op.create_index(op.f("ix_workspaces_owner_id"), "workspaces", ["owner_id"])

    # ---------------------------------------------------------------- folders
    op.create_table(
        "folders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("rev", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("deleted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_seq", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_folders")),
        sa.UniqueConstraint("workspace_id", "id", name=op.f("uq_folders_workspace_id_id")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_folders_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "parent_id"],
            ["folders.workspace_id", "folders.id"],
            name=op.f("fk_folders_workspace_id_parent_id_folders"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("btrim(name) <> ''", name=op.f("ck_folders_name_not_blank")),
        sa.CheckConstraint("rev > 0", name=op.f("ck_folders_rev_positive")),
        sa.CheckConstraint("updated_seq > 0", name=op.f("ck_folders_updated_seq_positive")),
        sa.CheckConstraint(
            "parent_id IS NULL OR parent_id <> id",
            name=op.f("ck_folders_parent_id_not_self"),
        ),
    )
    op.create_index(
        op.f("ix_folders_workspace_id_updated_seq"),
        "folders",
        ["workspace_id", "updated_seq"],
    )
    op.create_index(
        op.f("ix_folders_workspace_id_parent_id"),
        "folders",
        ["workspace_id", "parent_id"],
    )

    # ------------------------------------------------------------------ notes
    op.create_table(
        "notes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("folder_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_md", sa.Text(), nullable=False),
        sa.Column("rev", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column("deleted_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_seq", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notes")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_notes_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "folder_id"],
            ["folders.workspace_id", "folders.id"],
            name=op.f("fk_notes_workspace_id_folder_id_folders"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("rev > 0", name=op.f("ck_notes_rev_positive")),
        sa.CheckConstraint("updated_seq > 0", name=op.f("ck_notes_updated_seq_positive")),
    )
    op.create_index(
        op.f("ix_notes_workspace_id_updated_seq"),
        "notes",
        ["workspace_id", "updated_seq"],
    )
    op.create_index(op.f("ix_notes_workspace_id_folder_id"), "notes", ["workspace_id", "folder_id"])

    # ---------------------------------------------------------- change_events
    # The (workspace_id, seq) primary key is also the cursor-scan index, so no
    # extra index on those columns is created.
    op.create_table(
        "change_events",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("op", sa.Text(), nullable=False),
        sa.Column("rev", sa.BigInteger(), nullable=False),
        sa.Column("op_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("workspace_id", "seq", name=op.f("pk_change_events")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_change_events_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "op_id",
            name=op.f("uq_change_events_workspace_id_op_id"),
        ),
        sa.CheckConstraint("seq > 0", name=op.f("ck_change_events_seq_positive")),
        sa.CheckConstraint(
            "entity_type IN ('folder', 'note')",
            name=op.f("ck_change_events_entity_type_known"),
        ),
        sa.CheckConstraint(
            "op IN ('create', 'update', 'delete', 'move')",
            name=op.f("ck_change_events_op_known"),
        ),
        sa.CheckConstraint("rev > 0", name=op.f("ck_change_events_rev_positive")),
    )

    # -------------------------------------------------------------- op_ledger
    op.create_table(
        "op_ledger",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("op_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("request_hash", postgresql.BYTEA(), nullable=False),
        sa.Column("result_seq", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("workspace_id", "op_id", name=op.f("pk_op_ledger")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_op_ledger_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "result_seq"],
            ["change_events.workspace_id", "change_events.seq"],
            name=op.f("fk_op_ledger_workspace_id_result_seq_change_events"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "result_seq",
            name=op.f("uq_op_ledger_workspace_id_result_seq"),
        ),
        sa.CheckConstraint(
            "octet_length(request_hash) = 32",
            name=op.f("ck_op_ledger_request_hash_len"),
        ),
        sa.CheckConstraint("result_seq > 0", name=op.f("ck_op_ledger_result_seq_positive")),
    )

    # ------------------------------------------- deferred object -> event FKs
    # Added last: both tables must already exist, and the constraint must be
    # DEFERRABLE INITIALLY DEFERRED so the object row may be written before the
    # event that carries its seq (validated at COMMIT).
    op.create_foreign_key(
        op.f("fk_folders_workspace_id_updated_seq_change_events"),
        "folders",
        "change_events",
        ["workspace_id", "updated_seq"],
        ["workspace_id", "seq"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_foreign_key(
        op.f("fk_notes_workspace_id_updated_seq_change_events"),
        "notes",
        "change_events",
        ["workspace_id", "updated_seq"],
        ["workspace_id", "seq"],
        ondelete="NO ACTION",
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    # Reverse of upgrade: the deferred FKs disappear before their tables.
    op.drop_constraint(
        op.f("fk_notes_workspace_id_updated_seq_change_events"),
        "notes",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_folders_workspace_id_updated_seq_change_events"),
        "folders",
        type_="foreignkey",
    )

    op.drop_table("op_ledger")
    op.drop_table("notes")
    op.drop_table("folders")
    op.drop_table("change_events")
    op.drop_table("workspaces")
    op.drop_table("users")
