"""Phase 3 auth schema: users extension, sessions/tokens, owner membership.

Revision ID: 0003_auth_sessions_members
Revises: 0002_sync_kernel_core

What this revision owns (plan §9):

* ``users`` gains nullable ``password_hash`` (Argon2id digest) and
  ``disabled_at``. Pre-existing rows keep ``password_hash = NULL`` and therefore
  cannot log in locally (D8). **No credential is backfilled and no row is
  rewritten.**
* The plain ``UNIQUE (email)`` constraint becomes a functional unique index over
  ``lower(btrim(email))`` (D8). The stored value is never normalised — only the
  uniqueness key is.
* ``auth_sessions`` (device login **and** refresh family, D4) with an absolute
  ``expires_at``;
* ``auth_access_tokens`` / ``auth_refresh_tokens`` storing only 32-byte SHA-256
  digests, with the refresh ``parent_id`` chain that makes reuse detection (R1)
  possible;
* ``workspace_members`` with PK ``(workspace_id, user_id)`` and a ``role``
  CHECK that v1 can only satisfy with ``'owner'`` (D5), backfilled from
  ``workspaces.owner_id``.

Deliberate non-actions: ``folders``/``notes``/``change_events``/``op_ledger`` are
read but never written, no cursor or ``seq`` is touched, and no account is
merged. If legacy addresses collide once normalised, the upgrade **stops** with an
explicit exception instead of silently choosing a survivor — silently merging two
identities is a security bug, not a migration convenience.

``downgrade`` is the exact reverse: drop the four new tables (children before
parents), drop the functional index, restore ``UNIQUE (email)``, then drop the two
columns. The membership backfill is derived data, so dropping the table loses
nothing that ``workspaces.owner_id`` does not still hold.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_auth_sessions_members"
down_revision: str | None = "0002_sync_kernel_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Name of the functional uniqueness index that replaces ``uq_users_email``.
NORMALIZED_EMAIL_INDEX = "uq_users_email_normalized"


def _refuse_collisions(normalized: bool) -> None:
    """Abort the migration if legacy e-mails collide under the new/old key.

    ``normalized=True`` is the upgrade direction: two addresses are the same
    account when ``lower(btrim(email))`` matches. ``normalized=False`` is the
    downgrade direction, where the restored constraint is plain ``UNIQUE
    (email)``. Either way a collision is a *human* decision (which account
    survives?), never something a migration may pick automatically.

    The message is **fixed** and carries no address, id or count: a migration
    error ends up in CI logs and terminal scrollback, and which addresses a
    deployment holds is exactly the kind of data that must not leak there. The
    presence of a collision is enough to act on — the operator can find the
    duplicates with their own query.
    """
    # Two literal blocks rather than an f-string: the key is a constant either
    # way, and keeping SQL fully static means nothing is ever interpolated into
    # statement text by this revision.
    if normalized:
        sql = sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM users
                    GROUP BY lower(btrim(email))
                    HAVING count(*) > 1
                ) THEN
                    RAISE EXCEPTION
                        'users.email collision after normalisation; '
                        'refusing to merge accounts, resolve the duplicates manually';
                END IF;
            END
            $$;
            """
        )
    else:
        sql = sa.text(
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM users
                    GROUP BY email
                    HAVING count(*) > 1
                ) THEN
                    RAISE EXCEPTION
                        'users.email collision under plain uniqueness; '
                        'refusing to merge accounts, resolve the duplicates manually';
                END IF;
            END
            $$;
            """
        )
    op.execute(sql)


def upgrade() -> None:
    # Fail before *any* DDL, so a legacy collision leaves 0002 completely intact
    # and the operator gets one clear message instead of a half-migrated schema.
    _refuse_collisions(normalized=True)

    # ------------------------------------------------------------------ users
    # Nullable on purpose: legacy rows stay without a credential (D8) and no
    # default may invent one.
    op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=True))
    op.add_column(
        "users",
        sa.Column("disabled_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
    )

    op.drop_constraint(op.f("uq_users_email"), "users", type_="unique")
    op.create_index(
        NORMALIZED_EMAIL_INDEX,
        "users",
        [sa.text("lower(btrim(email))")],
        unique=True,
    )

    # --------------------------------------------------------- auth_sessions
    op.create_table(
        "auth_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("device_name", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # Absolute deadline; a refresh never extends it (D2).
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_sessions")),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_sessions_user_id_users"),
            ondelete="RESTRICT",
        ),
    )
    op.create_index(op.f("ix_auth_sessions_user_id"), "auth_sessions", ["user_id"])

    # -------------------------------------------------- auth_access_tokens
    op.create_table(
        "auth_access_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", postgresql.BYTEA(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_access_tokens")),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["auth_sessions.id"],
            name=op.f("fk_auth_access_tokens_session_id_auth_sessions"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_access_tokens_token_hash")),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32",
            name=op.f("ck_auth_access_tokens_token_hash_len"),
        ),
    )
    op.create_index(op.f("ix_auth_access_tokens_session_id"), "auth_access_tokens", ["session_id"])

    # ------------------------------------------------- auth_refresh_tokens
    op.create_table(
        "auth_refresh_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", postgresql.BYTEA(), nullable=False),
        sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("used_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_refresh_tokens")),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["auth_sessions.id"],
            name=op.f("fk_auth_refresh_tokens_session_id_auth_sessions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["auth_refresh_tokens.id"],
            name=op.f("fk_auth_refresh_tokens_parent_id_auth_refresh_tokens"),
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_refresh_tokens_token_hash")),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32",
            name=op.f("ck_auth_refresh_tokens_token_hash_len"),
        ),
    )
    op.create_index(
        op.f("ix_auth_refresh_tokens_session_id"), "auth_refresh_tokens", ["session_id"]
    )

    # ------------------------------------------------------ workspace_members
    op.create_table(
        "workspace_members",
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("workspace_id", "user_id", name=op.f("pk_workspace_members")),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_workspace_members_workspace_id_workspaces"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_workspace_members_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("role = 'owner'", name=op.f("ck_workspace_members_role_owner_only")),
    )
    op.create_index(op.f("ix_workspace_members_user_id"), "workspace_members", ["user_id"])

    # Backfill: every existing workspace gets exactly one owner membership.
    # Read-only with respect to workspaces; the pairs are unique because each
    # workspace row is, so this cannot conflict with the composite PK.
    op.execute(
        sa.text(
            """
            INSERT INTO workspace_members (workspace_id, user_id, role)
            SELECT id, owner_id, 'owner'
            FROM workspaces
            """
        )
    )


def downgrade() -> None:
    # Defence in depth, and *first*, so a refusal leaves the schema completely
    # intact (no half-downgraded table set).
    #
    # This check is expected never to fire while 0003's index is in place: plain
    # ``email`` equality implies ``lower(btrim(email))`` equality, so any pair the
    # restored constraint would reject is already rejected by the stronger
    # functional index. It exists for the one case the index cannot cover — an
    # operator who dropped or disabled the index by hand — and would otherwise
    # discover the problem halfway through the downgrade.
    _refuse_collisions(normalized=False)

    # Children first: tokens reference sessions, members reference both sides.
    op.drop_table("workspace_members")
    op.drop_table("auth_refresh_tokens")
    op.drop_table("auth_access_tokens")
    op.drop_table("auth_sessions")

    op.drop_index(NORMALIZED_EMAIL_INDEX, table_name="users")
    op.create_unique_constraint(op.f("uq_users_email"), "users", ["email"])

    op.drop_column("users", "disabled_at")
    op.drop_column("users", "password_hash")
