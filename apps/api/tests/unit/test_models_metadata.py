"""Focused metadata tests for the Revnote schema (Phase 2 kernel + Phase 3 auth).

These are **pure unit tests**: no server, no connection, no ``create_all()``. They
assert the contract that ``docs/plan-phase2.md`` §4 fixes for the six kernel
tables and ``docs/plan-phase3.md`` §9 for the auth/membership tables — column
presence/NULLability/defaults, primary keys, composite foreign keys with their
``ON DELETE`` behaviour, deferrability, named checks and indexes — and that the
same DDL is what the Alembic revisions emit.

Why assertions on metadata instead of a live database: the DDL text is produced
from exactly this metadata (``alembic upgrade --sql``), so a metadata regression
is a schema regression. The real PostgreSQL round trip is exercised separately in
``tests/integration/test_migrations.py``.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from revnote_api.db.base import NAMING_CONVENTION, Base
from revnote_api.models import (
    MEMBER_ROLES,
    MODEL_TABLES,
    AuthAccessToken,
    AuthRefreshToken,
    AuthSession,
    ChangeEvent,
    Folder,
    Note,
    OpLedger,
    User,
    Workspace,
    WorkspaceMember,
)

API_DIR = Path(__file__).resolve().parents[2]
MIGRATION_0002 = API_DIR / "migrations" / "versions" / "0002_sync_kernel_core.py"
MIGRATION_0003 = API_DIR / "migrations" / "versions" / "0003_auth_sessions_members.py"
MIGRATION_0001 = API_DIR / "migrations" / "versions" / "0001_baseline.py"

PG = postgresql.dialect()

#: Phase 2 sync-kernel tables (unchanged by Phase 3).
KERNEL_TABLES = (
    "users",
    "workspaces",
    "folders",
    "notes",
    "change_events",
    "op_ledger",
)

#: Phase 3 auth tables + the per-request authorisation row.
AUTH_TABLES = (
    "auth_sessions",
    "auth_access_tokens",
    "auth_refresh_tokens",
    "workspace_members",
)

#: Phase 5 attachment table. Not a kernel entity: no ``rev``/``updated_seq``/``seq``.
ATTACHMENT_TABLES = ("attachments",)

#: The exact table set of the project: eleven domain tables, nothing else.
EXPECTED_TABLES = (*KERNEL_TABLES, *AUTH_TABLES, *ATTACHMENT_TABLES)


def _called_names(source: str) -> set[str]:
    """Names actually *called* in a migration module (docstrings excluded)."""
    tree = ast.parse(source)
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def ddl(table: str) -> str:
    """Rendered CREATE TABLE for ``table`` as PostgreSQL would receive it."""
    return str(CreateTable(Base.metadata.tables[table]).compile(dialect=PG))


def _col(table: str, name: str) -> sa.Column:
    return Base.metadata.tables[table].c[name]


def _checks(table: str) -> dict[str, str]:
    return {
        c.name: str(c.sqltext)
        for c in Base.metadata.tables[table].constraints
        if isinstance(c, sa.CheckConstraint)
    }


def _fks(table: str) -> dict[str, sa.ForeignKeyConstraint]:
    return {
        c.name: c
        for c in Base.metadata.tables[table].constraints
        if isinstance(c, sa.ForeignKeyConstraint)
    }


def _indexes(table: str) -> set[tuple[str, tuple[str, ...]]]:
    return {
        (ix.name, tuple(c.name for c in ix.columns)) for ix in Base.metadata.tables[table].indexes
    }


def index_sql(table: str, name: str) -> str:
    """Rendered ``CREATE INDEX`` for one declared index (expression indexes included)."""
    for index in Base.metadata.tables[table].indexes:
        if index.name == name:
            return str(CreateIndex(index).compile(dialect=PG)).strip()
    raise AssertionError(f"{table} has no index {name!r}")


def btrim_index_ddl() -> str:
    """The Phase 3 normalised-e-mail uniqueness index, as PostgreSQL receives it."""
    return index_sql("users", "uq_users_email_normalized")


# --------------------------------------------------------------------------- #
# naming convention (the base.py regression this phase had to fix)
# --------------------------------------------------------------------------- #
def test_naming_convention_uses_valid_sqlalchemy_tokens() -> None:
    """``column_0_list_name`` is not a token; ``column_0_N_name`` is (plan §4)."""
    assert NAMING_CONVENTION["ix"] == "ix_%(table_name)s_%(column_0_N_name)s"
    assert NAMING_CONVENTION["uq"] == "uq_%(table_name)s_%(column_0_N_name)s"
    assert NAMING_CONVENTION["ck"] == "ck_%(table_name)s_%(constraint_name)s"
    assert NAMING_CONVENTION["fk"] == (
        "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    )
    assert NAMING_CONVENTION["pk"] == "pk_%(table_name)s"
    assert "column_0_list_name" not in str(NAMING_CONVENTION)


def test_convention_expands_multicolumn_names() -> None:
    """A composite index/unique constraint must render ``col_a_col_b``, not crash."""
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
    table = sa.Table(
        "probe",
        metadata,
        sa.Column("a", sa.Integer, nullable=False),
        sa.Column("b", sa.Integer, nullable=False),
        sa.UniqueConstraint("a", "b"),
        sa.Index(None, "a", "b"),
        sa.CheckConstraint("b > 0", name="b_positive"),
    )

    assert {c.name for c in table.constraints} == {
        "ck_probe_b_positive",
        "pk_probe",
        "uq_probe_a_b",
    }
    assert {ix.name for ix in table.indexes} == {"ix_probe_a_b"}


def test_unnamed_check_constraint_is_rejected_by_convention() -> None:
    """``ck`` uses ``%(constraint_name)s``, so every CHECK must be named explicitly."""
    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)
    with pytest.raises(sa.exc.InvalidRequestError, match="explicitly named"):
        table = sa.Table(
            "probe",
            metadata,
            sa.Column("a", sa.Integer),
            sa.CheckConstraint("a > 0"),
        )
        _ = {c.name for c in table.constraints}


# --------------------------------------------------------------------------- #
# table set / registration
# --------------------------------------------------------------------------- #
def test_metadata_declares_exactly_the_contract_tables() -> None:
    assert tuple(sorted(Base.metadata.tables)) == tuple(sorted(EXPECTED_TABLES))
    assert tuple(MODEL_TABLES) == EXPECTED_TABLES


def test_out_of_scope_tables_are_absent() -> None:
    """Tables the plan excludes must not be declared (plan §1 and §9)."""
    forbidden = {
        "change_seq",
        "outbox",
        "revisions",
        "shared_links",
        "sync_sessions",
        "tombstones",
        "user_workspaces",
        "auth_devices",
        "auth_tokens",
        "workspace_invites",
    }

    assert not set(Base.metadata.tables) & forbidden


def test_mappers_configure_without_mapper_errors() -> None:
    """No half-declared relationships / duplicate table names behind the scenes."""
    sa.orm.configure_mappers()


def test_only_one_declarative_base_is_in_use() -> None:
    """Every model sits on the shared Base.metadata — no second metadata."""
    for model in (
        User,
        Workspace,
        Folder,
        Note,
        ChangeEvent,
        OpLedger,
        AuthSession,
        AuthAccessToken,
        AuthRefreshToken,
        WorkspaceMember,
    ):
        assert model.__table__.metadata is Base.metadata


def test_models_are_registered_by_importing_the_package() -> None:
    """Explicit export surface for services/tests (no accidental import order)."""
    for model, table in (
        (User, "users"),
        (Workspace, "workspaces"),
        (Folder, "folders"),
        (Note, "notes"),
        (ChangeEvent, "change_events"),
        (OpLedger, "op_ledger"),
        (AuthSession, "auth_sessions"),
        (AuthAccessToken, "auth_access_tokens"),
        (AuthRefreshToken, "auth_refresh_tokens"),
        (WorkspaceMember, "workspace_members"),
    ):
        assert model.__tablename__ == table
        assert model.__table__ is Base.metadata.tables[table]


def test_no_relationship_or_cascade_is_declared() -> None:
    """Cross-table behaviour must stay in explicit SQL constraints, not ORM cascades."""
    for model, table in (
        (User, "users"),
        (Workspace, "workspaces"),
        (Folder, "folders"),
        (Note, "notes"),
        (ChangeEvent, "change_events"),
        (OpLedger, "op_ledger"),
        (AuthSession, "auth_sessions"),
        (AuthAccessToken, "auth_access_tokens"),
        (AuthRefreshToken, "auth_refresh_tokens"),
        (WorkspaceMember, "workspace_members"),
    ):
        assert list(sa.inspect(model).relationships) == [], table

    # Nor may the model sources reference relationship()/cascade= at all
    # (AST-based, so docstrings cannot satisfy or trip the check).
    models_dir = API_DIR / "src" / "revnote_api" / "models"
    for path in sorted(models_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        calls = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        kwargs = {
            keyword.arg
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for keyword in node.keywords
        }

        assert "relationship" not in names | calls, path.name
        assert "backref" not in names | calls, path.name
        assert "cascade" not in kwargs, path.name


def test_all_checks_pk_uq_fk_indexes_are_explicitly_named() -> None:
    """Every constraint carries a name, and CHECK names already include the prefix."""
    for table in Base.metadata.tables.values():
        for constraint in table.constraints:
            assert constraint.name, f"unnamed {type(constraint).__name__} on {table.name}"
            if isinstance(constraint, sa.CheckConstraint):
                assert constraint.name.startswith(f"ck_{table.name}_"), constraint.name
        for index in table.indexes:
            assert index.name, f"unnamed index on {table.name}"


def test_exact_check_set_per_table() -> None:
    """The full named CHECK inventory — no missing, extra or unnamed CHECK.

    Kernel tables keep the Phase 2 set (plan §4); the Phase 3 tables add their own
    (plan §9). A composite-PK table has no separate PK-constraint CHECK entry, so
    this table is also the guard that no CHECK is silently added by a naming
    convention.
    """
    expected = {
        "users": {"ck_users_email_not_blank"},
        "workspaces": {
            "ck_workspaces_last_seq_non_negative",
            "ck_workspaces_min_retained_seq_min",
            "ck_workspaces_min_retained_seq_within_last_seq",
        },
        "folders": {
            "ck_folders_name_not_blank",
            "ck_folders_rev_positive",
            "ck_folders_updated_seq_positive",
            "ck_folders_parent_id_not_self",
        },
        "notes": {"ck_notes_rev_positive", "ck_notes_updated_seq_positive"},
        "change_events": {
            "ck_change_events_seq_positive",
            "ck_change_events_entity_type_known",
            "ck_change_events_op_known",
            "ck_change_events_rev_positive",
        },
        "op_ledger": {
            "ck_op_ledger_request_hash_len",
            "ck_op_ledger_result_seq_positive",
        },
        "auth_sessions": set(),
        "auth_access_tokens": {"ck_auth_access_tokens_token_hash_len"},
        "auth_refresh_tokens": {"ck_auth_refresh_tokens_token_hash_len"},
        "workspace_members": {"ck_workspace_members_role_owner_only"},
        "attachments": {
            "ck_attachments_state_known",
            "ck_attachments_mime_len",
            "ck_attachments_byte_size_positive",
            "ck_attachments_content_hash_len",
            "ck_attachments_storage_key_len",
            "ck_attachments_ready_iff_hash",
        },
    }

    assert set(Base.metadata.tables) == set(expected)
    for table_name, expected_checks in expected.items():
        assert set(_checks(table_name)) == expected_checks, table_name


def test_check_sql_text_is_pinned() -> None:
    """Every CHECK expression is asserted verbatim, so a semantic drift fails."""
    expected = {
        "users": {"ck_users_email_not_blank": "btrim(email) <> ''"},
        "workspaces": {
            "ck_workspaces_last_seq_non_negative": "last_seq >= 0",
            "ck_workspaces_min_retained_seq_min": "min_retained_seq >= 1",
            "ck_workspaces_min_retained_seq_within_last_seq": ("min_retained_seq - 1 <= last_seq"),
        },
        "folders": {
            "ck_folders_name_not_blank": "btrim(name) <> ''",
            "ck_folders_rev_positive": "rev > 0",
            "ck_folders_updated_seq_positive": "updated_seq > 0",
            "ck_folders_parent_id_not_self": "parent_id IS NULL OR parent_id <> id",
        },
        "notes": {
            "ck_notes_rev_positive": "rev > 0",
            "ck_notes_updated_seq_positive": "updated_seq > 0",
        },
        "change_events": {
            "ck_change_events_seq_positive": "seq > 0",
            "ck_change_events_entity_type_known": "entity_type IN ('folder', 'note')",
            "ck_change_events_op_known": ("op IN ('create', 'update', 'delete', 'move')"),
            "ck_change_events_rev_positive": "rev > 0",
        },
        "op_ledger": {
            "ck_op_ledger_request_hash_len": "octet_length(request_hash) = 32",
            "ck_op_ledger_result_seq_positive": "result_seq > 0",
        },
        "auth_sessions": {},
        "auth_access_tokens": {
            "ck_auth_access_tokens_token_hash_len": "octet_length(token_hash) = 32",
        },
        "auth_refresh_tokens": {
            "ck_auth_refresh_tokens_token_hash_len": "octet_length(token_hash) = 32",
        },
        "workspace_members": {
            "ck_workspace_members_role_owner_only": "role = 'owner'",
        },
        "attachments": {
            "ck_attachments_state_known": "state IN ('pending', 'ready', 'deleted')",
            "ck_attachments_mime_len": "octet_length(mime) BETWEEN 3 AND 127",
            "ck_attachments_byte_size_positive": "byte_size IS NULL OR byte_size > 0",
            "ck_attachments_content_hash_len": (
                "content_hash IS NULL OR octet_length(content_hash) = 32"
            ),
            "ck_attachments_storage_key_len": "octet_length(storage_key) BETWEEN 8 AND 512",
            "ck_attachments_ready_iff_hash": "(state = 'ready') = (content_hash IS NOT NULL)",
        },
    }

    for table_name, checks in expected.items():
        assert _checks(table_name) == checks, table_name


# --------------------------------------------------------------------------- #
# users / workspaces
# --------------------------------------------------------------------------- #
def test_users_columns_pk_and_normalised_email_uniqueness() -> None:
    """Phase 3: two extra nullable columns, and uniqueness moves to an expression."""
    table = Base.metadata.tables["users"]

    assert [c.name for c in table.columns] == [
        "id",
        "email",
        "password_hash",
        "disabled_at",
        "created_at",
    ]
    assert _col("users", "id").primary_key
    assert not _col("users", "id").nullable
    assert isinstance(_col("users", "id").type, postgresql.UUID)
    assert not _col("users", "email").nullable
    assert isinstance(_col("users", "email").type, sa.Text)

    # D8: the old plain constraint is gone; uniqueness is lower(btrim(email)).
    assert {c.name for c in table.constraints if isinstance(c, sa.UniqueConstraint)} == set()
    assert btrim_index_ddl() == (
        "CREATE UNIQUE INDEX uq_users_email_normalized ON users (lower(btrim(email)))"
    )

    # Both added columns are nullable: a legacy row keeps no credential (D8) and
    # no account is disabled by default.
    password_hash = _col("users", "password_hash")
    disabled_at = _col("users", "disabled_at")
    assert password_hash.nullable and isinstance(password_hash.type, sa.Text)
    assert password_hash.server_default is None
    assert disabled_at.nullable
    assert isinstance(disabled_at.type, postgresql.TIMESTAMP)
    assert disabled_at.type.timezone is True
    assert disabled_at.server_default is None

    # Exactly one CHECK: the non-blank guard. No normalisation in the stored
    # value, no syntax/format validation (the plan requires a non-empty e-mail).
    assert _checks("users") == {"ck_users_email_not_blank": "btrim(email) <> ''"}
    assert not _fks("users")


def test_users_email_is_unique_without_citext_or_triggers() -> None:
    """Case/space folding lives in the index expression, never in the column."""
    email = _col("users", "email")

    assert email.server_default is None
    assert not getattr(email.type, "collation", None)
    # No citext extension and no normalising trigger: nothing to keep in sync.
    assert not any(
        isinstance(constraint, sa.CheckConstraint) and "lower(" in str(constraint.sqltext)
        for constraint in Base.metadata.tables["users"].constraints
    )


def test_users_email_check_rejects_blank_without_mutating_the_value() -> None:
    """``btrim(email) <> ''`` gates whitespace-only, it never trims/stores btrim()."""
    check = _checks("users")["ck_users_email_not_blank"]

    assert "btrim(" in check
    assert "lower(" not in check and "citext" not in check.lower()
    # The column itself stays plain TEXT with no computed/normalised default.
    email = _col("users", "email")
    assert email.server_default is None
    assert not getattr(email.type, "collation", None)


def test_workspaces_columns_defaults_and_checks() -> None:
    table = Base.metadata.tables["workspaces"]

    assert [c.name for c in table.columns] == [
        "id",
        "owner_id",
        "last_seq",
        "min_retained_seq",
        "created_at",
    ]
    assert str(table.primary_key.name) == "pk_workspaces"
    assert not _col("workspaces", "last_seq").nullable
    assert isinstance(_col("workspaces", "last_seq").type, sa.BigInteger)
    # last_seq is a per-workspace counter seeded at 0, never a global change_seq.
    assert str(_col("workspaces", "last_seq").server_default.arg) == "0"
    # min_retained_seq is a floor placeholder: DEFAULT 1 and not advanced in Phase 2.
    assert not _col("workspaces", "min_retained_seq").nullable
    assert str(_col("workspaces", "min_retained_seq").server_default.arg) == "1"

    checks = _checks("workspaces")
    assert checks == {
        "ck_workspaces_last_seq_non_negative": "last_seq >= 0",
        "ck_workspaces_min_retained_seq_min": "min_retained_seq >= 1",
        "ck_workspaces_min_retained_seq_within_last_seq": "min_retained_seq - 1 <= last_seq",
    }

    fk = _fks("workspaces")["fk_workspaces_owner_id_users"]
    assert fk.ondelete == "RESTRICT"
    assert [c.name for c in fk.columns] == ["owner_id"]
    assert [e.target_fullname for e in fk.elements] == ["users.id"]


def test_workspaces_owner_id_is_indexed() -> None:
    assert _indexes("workspaces") == {("ix_workspaces_owner_id", ("owner_id",))}


def test_timestamps_are_timestamptz_with_current_timestamp_default() -> None:
    for table in EXPECTED_TABLES:
        created_at = _col(table, "created_at")
        assert isinstance(created_at.type, postgresql.TIMESTAMP), table
        assert created_at.type.timezone is True, table
        assert not created_at.nullable, table
        assert str(created_at.server_default.arg) == "now()", table


# --------------------------------------------------------------------------- #
# folders / notes (the sync-bearing entity tables)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("table_name", ["folders", "notes"])
def test_entity_tables_require_updated_seq_without_default(table_name: str) -> None:
    """``updated_seq`` is a cursor allocated by the service: NOT NULL, no default."""
    updated_seq = _col(table_name, "updated_seq")

    assert not updated_seq.nullable
    assert isinstance(updated_seq.type, sa.BigInteger)
    assert updated_seq.server_default is None
    assert updated_seq.default is None
    assert "ck_" + table_name + "_updated_seq_positive" in _checks(table_name)


@pytest.mark.parametrize("table_name", ["folders", "notes"])
def test_entity_tables_rev_defaults_to_one_and_is_positive(table_name: str) -> None:
    rev = _col(table_name, "rev")

    assert not rev.nullable
    assert isinstance(rev.type, sa.BigInteger)
    assert str(rev.server_default.arg) == "1"
    assert _checks(table_name)["ck_" + table_name + "_rev_positive"] == "rev > 0"


@pytest.mark.parametrize("table_name", ["folders", "notes"])
def test_entity_tables_carry_tombstone_column(table_name: str) -> None:
    deleted_at = _col(table_name, "deleted_at")

    assert deleted_at.nullable
    assert isinstance(deleted_at.type, postgresql.TIMESTAMP)
    assert deleted_at.type.timezone is True
    # updated_at is set explicitly by the service, never by a DB default/trigger.
    assert not _col(table_name, "updated_at").nullable
    assert _col(table_name, "updated_at").server_default is None


@pytest.mark.parametrize("table_name", ["folders", "notes"])
def test_updated_seq_to_event_fk_is_deferred_no_action(table_name: str) -> None:
    """Object row first, event row later: FK must be deferred, initially deferred."""
    fk = _fks(table_name)[f"fk_{table_name}_workspace_id_updated_seq_change_events"]

    assert fk.deferrable is True
    assert fk.initially == "DEFERRED"
    assert fk.ondelete == "NO ACTION"
    assert [c.name for c in fk.columns] == ["workspace_id", "updated_seq"]
    assert [e.target_fullname for e in fk.elements] == [
        "change_events.workspace_id",
        "change_events.seq",
    ]
    assert "DEFERRABLE INITIALLY DEFERRED" in ddl(table_name)


def test_only_the_two_updated_seq_fks_are_deferrable() -> None:
    """Exactly two deferred FKs exist; every other FK is checked immediately.

    ``op_ledger.result_seq`` and the ``workspace_id`` FKs must stay immediate —
    only the object -> event reference needs COMMIT-time validation.
    """
    deferrable = {
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
        and (constraint.deferrable or constraint.initially)
    }

    assert deferrable == {
        "fk_folders_workspace_id_updated_seq_change_events",
        "fk_notes_workspace_id_updated_seq_change_events",
    }


@pytest.mark.parametrize("table_name", ["folders", "notes"])
def test_entity_tables_use_restrict_for_workspace_fk(table_name: str) -> None:
    fk = _fks(table_name)[f"fk_{table_name}_workspace_id_workspaces"]

    assert fk.ondelete == "RESTRICT"
    assert [e.target_fullname for e in fk.elements] == ["workspaces.id"]


@pytest.mark.parametrize("table_name", ["folders", "notes"])
def test_entity_tables_index_workspace_cursor_scan(table_name: str) -> None:
    assert (
        f"ix_{table_name}_workspace_id_updated_seq",
        ("workspace_id", "updated_seq"),
    ) in _indexes(table_name)


def test_folders_columns_and_tree_constraints() -> None:
    table = Base.metadata.tables["folders"]

    assert [c.name for c in table.columns] == [
        "id",
        "workspace_id",
        "parent_id",
        "name",
        "rev",
        "deleted_at",
        "updated_seq",
        "created_at",
        "updated_at",
    ]
    assert _col("folders", "parent_id").nullable
    assert not _col("folders", "name").nullable
    assert isinstance(_col("folders", "name").type, sa.Text)

    # (workspace_id, id) unique enables the composite self-referencing parent FK.
    assert {
        (c.name, tuple(col.name for col in c.columns))
        for c in table.constraints
        if isinstance(c, sa.UniqueConstraint)
    } == {("uq_folders_workspace_id_id", ("workspace_id", "id"))}

    parent_fk = _fks("folders")["fk_folders_workspace_id_parent_id_folders"]
    assert [c.name for c in parent_fk.columns] == ["workspace_id", "parent_id"]
    assert [e.target_fullname for e in parent_fk.elements] == [
        "folders.workspace_id",
        "folders.id",
    ]
    assert parent_fk.ondelete == "RESTRICT"  # no implicit subtree cascade (D6)
    assert parent_fk.deferrable is None

    checks = _checks("folders")
    assert checks["ck_folders_name_not_blank"] == "btrim(name) <> ''"
    assert checks["ck_folders_parent_id_not_self"] == "parent_id IS NULL OR parent_id <> id"

    assert _indexes("folders") == {
        ("ix_folders_workspace_id_updated_seq", ("workspace_id", "updated_seq")),
        ("ix_folders_workspace_id_parent_id", ("workspace_id", "parent_id")),
    }


def test_notes_columns_and_folder_fk() -> None:
    table = Base.metadata.tables["notes"]

    assert [c.name for c in table.columns] == [
        "id",
        "workspace_id",
        "folder_id",
        "title",
        "body_md",
        "rev",
        "deleted_at",
        "updated_seq",
        "created_at",
        "updated_at",
    ]
    # Empty strings are legal: NOT NULL only, no blank CHECK on title/body_md.
    for column in ("title", "body_md"):
        assert not _col("notes", column).nullable
        assert isinstance(_col("notes", column).type, sa.Text)
    assert not _checks("notes") or set(_checks("notes")) == {
        "ck_notes_rev_positive",
        "ck_notes_updated_seq_positive",
    }

    folder_fk = _fks("notes")["fk_notes_workspace_id_folder_id_folders"]
    assert [c.name for c in folder_fk.columns] == ["workspace_id", "folder_id"]
    assert [e.target_fullname for e in folder_fk.elements] == [
        "folders.workspace_id",
        "folders.id",
    ]
    assert folder_fk.ondelete == "RESTRICT"
    assert _col("notes", "folder_id").nullable

    assert _indexes("notes") == {
        ("ix_notes_workspace_id_updated_seq", ("workspace_id", "updated_seq")),
        ("ix_notes_workspace_id_folder_id", ("workspace_id", "folder_id")),
    }


# --------------------------------------------------------------------------- #
# change_events / op_ledger
# --------------------------------------------------------------------------- #
def test_change_events_composite_pk_and_columns() -> None:
    table = Base.metadata.tables["change_events"]

    assert [c.name for c in table.columns] == [
        "workspace_id",
        "seq",
        "entity_type",
        "entity_id",
        "op",
        "rev",
        "op_id",
        "created_at",
    ]
    assert str(table.primary_key.name) == "pk_change_events"
    assert [c.name for c in table.primary_key.columns] == ["workspace_id", "seq"]
    # seq has no default: it is allocated from workspaces.last_seq under FOR UPDATE.
    assert _col("change_events", "seq").server_default is None
    assert isinstance(_col("change_events", "seq").type, sa.BigInteger)
    assert isinstance(_col("change_events", "entity_id").type, postgresql.UUID)
    assert isinstance(_col("change_events", "op_id").type, postgresql.UUID)


def test_change_events_checks_and_op_id_uniqueness() -> None:
    checks = _checks("change_events")

    assert checks["ck_change_events_entity_type_known"] == ("entity_type IN ('folder', 'note')")
    assert checks["ck_change_events_op_known"] == "op IN ('create', 'update', 'delete', 'move')"
    assert checks["ck_change_events_seq_positive"] == "seq > 0"
    assert checks["ck_change_events_rev_positive"] == "rev > 0"

    uniques = {
        (c.name, tuple(col.name for col in c.columns))
        for c in Base.metadata.tables["change_events"].constraints
        if isinstance(c, sa.UniqueConstraint)
    }
    assert uniques == {("uq_change_events_workspace_id_op_id", ("workspace_id", "op_id"))}


def test_change_events_multicolumn_pk_is_the_cursor_index() -> None:
    """The PK index already covers (workspace_id, seq); no duplicate index (plan §4)."""
    table = Base.metadata.tables["change_events"]

    assert table.indexes == set()
    pk_columns = tuple(c.name for c in table.primary_key.columns)
    for index in table.indexes:  # pragma: no cover - asserted empty above
        assert tuple(c.name for c in index.columns) != pk_columns


def test_change_events_entity_id_is_polymorphic_without_fk() -> None:
    """``entity_id`` is polymorphic; the plan forbids faking a normal FK for it."""
    targets = {
        element.target_fullname for fk in _fks("change_events").values() for element in fk.elements
    }

    assert targets == {"workspaces.id"}


def test_op_ledger_composite_pk_is_not_deferrable() -> None:
    table = Base.metadata.tables["op_ledger"]

    assert [c.name for c in table.columns] == [
        "workspace_id",
        "op_id",
        "request_hash",
        "result_seq",
        "created_at",
    ]
    assert str(table.primary_key.name) == "pk_op_ledger"
    assert [c.name for c in table.primary_key.columns] == ["workspace_id", "op_id"]
    assert table.primary_key.deferrable is None
    assert table.primary_key.initially is None


def test_op_ledger_result_seq_fk_is_immediate_restrict() -> None:
    """``result_seq`` is NOT NULL and checked immediately, unlike updated_seq."""
    fk = _fks("op_ledger")["fk_op_ledger_workspace_id_result_seq_change_events"]

    assert not _col("op_ledger", "result_seq").nullable
    assert fk.deferrable is None
    assert fk.initially is None
    assert fk.ondelete == "RESTRICT"
    assert [c.name for c in fk.columns] == ["workspace_id", "result_seq"]
    assert [e.target_fullname for e in fk.elements] == [
        "change_events.workspace_id",
        "change_events.seq",
    ]


def test_op_ledger_request_hash_is_32_byte_bytea() -> None:
    column = _col("op_ledger", "request_hash")

    assert isinstance(column.type, postgresql.BYTEA)
    assert not column.nullable
    assert _checks("op_ledger")["ck_op_ledger_request_hash_len"] == (
        "octet_length(request_hash) = 32"
    )


def test_op_ledger_has_no_pending_state_and_unique_result_seq() -> None:
    table = Base.metadata.tables["op_ledger"]

    # Only successful ops are recorded: no status/pending/snapshot columns.
    assert {c.name for c in table.columns} == {
        "workspace_id",
        "op_id",
        "request_hash",
        "result_seq",
        "created_at",
    }
    uniques = {
        (c.name, tuple(col.name for col in c.columns))
        for c in table.constraints
        if isinstance(c, sa.UniqueConstraint)
    }
    assert uniques == {("uq_op_ledger_workspace_id_result_seq", ("workspace_id", "result_seq"))}


# --------------------------------------------------------------------------- #
# Phase 3: auth_sessions / auth_access_tokens / auth_refresh_tokens
# --------------------------------------------------------------------------- #
def test_auth_sessions_columns_and_absolute_expiry() -> None:
    """A session is a device login *and* a refresh family (D4); expiry is absolute."""
    table = Base.metadata.tables["auth_sessions"]

    assert [c.name for c in table.columns] == [
        "id",
        "user_id",
        "device_name",
        "created_at",
        "expires_at",
        "revoked_at",
        "revoke_reason",
    ]
    assert str(table.primary_key.name) == "pk_auth_sessions"
    assert [c.name for c in table.primary_key.columns] == ["id"]
    assert isinstance(_col("auth_sessions", "id").type, postgresql.UUID)

    # expires_at is NOT NULL and has no default: the service computes it from the
    # session TTL so the value is testable and never a clock read inside SQL.
    expires_at = _col("auth_sessions", "expires_at")
    assert not expires_at.nullable
    assert expires_at.server_default is None
    assert isinstance(expires_at.type, postgresql.TIMESTAMP)
    assert expires_at.type.timezone is True

    # Optional metadata: no CHECK on device_name, no default, and revocation is a
    # timestamp pair — never a boolean, so "when" survives.
    assert _col("auth_sessions", "device_name").nullable
    assert _col("auth_sessions", "device_name").server_default is None
    for name in ("revoked_at", "revoke_reason"):
        assert _col("auth_sessions", name).nullable, name
        assert _col("auth_sessions", name).server_default is None, name

    assert not _checks("auth_sessions")


def test_auth_sessions_user_fk_restricts_and_is_indexed() -> None:
    """``RESTRICT`` keeps token history attached; the FK column carries the index."""
    fk = _fks("auth_sessions")["fk_auth_sessions_user_id_users"]

    assert fk.ondelete == "RESTRICT"
    assert fk.deferrable is None
    assert [c.name for c in fk.columns] == ["user_id"]
    assert [e.target_fullname for e in fk.elements] == ["users.id"]
    assert _indexes("auth_sessions") == {("ix_auth_sessions_user_id", ("user_id",))}


@pytest.mark.parametrize(
    "table_name, fk_name",
    [
        ("auth_access_tokens", "fk_auth_access_tokens_session_id_auth_sessions"),
        ("auth_refresh_tokens", "fk_auth_refresh_tokens_session_id_auth_sessions"),
    ],
)
def test_token_tables_bind_to_a_session_and_index_it(table_name: str, fk_name: str) -> None:
    fk = _fks(table_name)[fk_name]

    assert fk.ondelete == "RESTRICT"
    assert fk.deferrable is None
    assert [c.name for c in fk.columns] == ["session_id"]
    assert [e.target_fullname for e in fk.elements] == ["auth_sessions.id"]
    assert (f"ix_{table_name}_session_id", ("session_id",)) in _indexes(table_name)


@pytest.mark.parametrize("table_name", ["auth_access_tokens", "auth_refresh_tokens"])
def test_token_hashes_are_unique_32_byte_digests(table_name: str) -> None:
    """Only ``sha256(raw)`` is stored: 32-byte BYTEA, UNIQUE, CHECK-enforced."""
    table = Base.metadata.tables[table_name]

    token_hash = _col(table_name, "token_hash")
    assert isinstance(token_hash.type, postgresql.BYTEA)
    assert not token_hash.nullable
    assert token_hash.server_default is None
    assert _checks(table_name)[f"ck_{table_name}_token_hash_len"] == (
        "octet_length(token_hash) = 32"
    )
    assert {
        (c.name, tuple(column.name for column in c.columns))
        for c in table.constraints
        if isinstance(c, sa.UniqueConstraint)
    } == {(f"uq_{table_name}_token_hash", ("token_hash",))}

    # No raw-token column may ever exist: the secret is returned once, never stored.
    assert {c.name for c in table.columns} & {"token", "raw_token", "secret"} == set()


def test_auth_access_token_columns() -> None:
    assert [c.name for c in Base.metadata.tables["auth_access_tokens"].columns] == [
        "id",
        "session_id",
        "token_hash",
        "created_at",
        "expires_at",
    ]
    assert isinstance(_col("auth_access_tokens", "session_id").type, postgresql.UUID)
    expires_at = _col("auth_access_tokens", "expires_at")
    assert not expires_at.nullable
    assert expires_at.server_default is None
    # No revocation column: a revoked session invalidates its access tokens, and
    # the 600s TTL is the only other bound (D1).
    assert "revoked_at" not in Base.metadata.tables["auth_access_tokens"].c


def test_auth_refresh_token_rotation_chain() -> None:
    """``parent_id`` is the rotation chain; the used row is kept for R1."""
    table = Base.metadata.tables["auth_refresh_tokens"]

    assert [c.name for c in table.columns] == [
        "id",
        "session_id",
        "token_hash",
        "parent_id",
        "created_at",
        "expires_at",
        "used_at",
    ]
    parent = _col("auth_refresh_tokens", "parent_id")
    assert parent.nullable  # the login-issued token has no parent
    assert isinstance(parent.type, postgresql.UUID)

    fk = _fks("auth_refresh_tokens")["fk_auth_refresh_tokens_parent_id_auth_refresh_tokens"]
    assert fk.ondelete == "RESTRICT"
    assert [e.target_fullname for e in fk.elements] == ["auth_refresh_tokens.id"]

    # used_at NULL = still redeemable; a timestamp = already exchanged (reuse => R1).
    used_at = _col("auth_refresh_tokens", "used_at")
    assert used_at.nullable
    assert used_at.server_default is None
    assert isinstance(used_at.type, postgresql.TIMESTAMP)
    assert used_at.type.timezone is True
    # No boolean/status column may shadow the timestamp.
    assert {"used", "revoked", "status"} & {c.name for c in table.columns} == set()


# --------------------------------------------------------------------------- #
# Phase 3: workspace_members
# --------------------------------------------------------------------------- #
def test_workspace_members_composite_pk_and_columns() -> None:
    """PK ``(workspace_id, user_id)`` is the per-request authorisation probe (D5)."""
    table = Base.metadata.tables["workspace_members"]

    assert [c.name for c in table.columns] == [
        "workspace_id",
        "user_id",
        "role",
        "created_at",
    ]
    assert str(table.primary_key.name) == "pk_workspace_members"
    assert [c.name for c in table.primary_key.columns] == ["workspace_id", "user_id"]
    assert not table.primary_key.deferrable
    assert isinstance(_col("workspace_members", "workspace_id").type, postgresql.UUID)
    assert isinstance(_col("workspace_members", "user_id").type, postgresql.UUID)
    assert not _col("workspace_members", "role").nullable


def test_workspace_members_fks_are_restrict_and_user_side_indexed() -> None:
    fks = _fks("workspace_members")

    assert set(fks) == {
        "fk_workspace_members_workspace_id_workspaces",
        "fk_workspace_members_user_id_users",
    }
    for name, target in (
        ("fk_workspace_members_workspace_id_workspaces", "workspaces.id"),
        ("fk_workspace_members_user_id_users", "users.id"),
    ):
        assert fks[name].ondelete == "RESTRICT", name
        assert fks[name].deferrable is None, name
        assert [e.target_fullname for e in fks[name].elements] == [target], name

    # The PK covers (workspace_id, user_id); only the reverse direction needs an index.
    assert _indexes("workspace_members") == {("ix_workspace_members_user_id", ("user_id",))}


def test_workspace_members_role_is_owner_only_for_v1() -> None:
    """D5: v1 has no member management, so the database accepts ``'owner'`` only."""
    assert MEMBER_ROLES == ("owner",)
    assert _checks("workspace_members") == {
        "ck_workspace_members_role_owner_only": "role = 'owner'"
    }


# --------------------------------------------------------------------------- #
# Phase 3: sensitive values never reach a repr
# --------------------------------------------------------------------------- #
def test_secret_columns_are_absent_from_every_repr() -> None:
    """repr is logged: a password hash or token digest must not be reachable from it."""
    password_hash = "argon2id$sentinel-password-hash"
    token_digest = b"sentinel-token-digest-32-bytes.."
    assert len(token_digest) == 32

    user = User(id=uuid.uuid4(), email="repr-sentinel@revnote.test", password_hash=password_hash)
    assert password_hash not in repr(user)
    assert user.email not in repr(user)

    for model, kwargs in (
        (AuthAccessToken, {"token_hash": token_digest}),
        (AuthRefreshToken, {"token_hash": token_digest}),
    ):
        row = model(id=uuid.uuid4(), session_id=uuid.uuid4(), **kwargs)
        assert repr(token_digest) not in repr(row)
        assert token_digest.hex() not in repr(row)


# --------------------------------------------------------------------------- #
# PostgreSQL-only / no-sequence discipline
# --------------------------------------------------------------------------- #
def test_no_serial_identity_or_nextval_anywhere() -> None:
    """Sync counters come from the workspace row lock, never from a sequence."""
    for name, table in Base.metadata.tables.items():
        rendered = ddl(name).upper()
        assert "SERIAL" not in rendered, name
        assert "IDENTITY" not in rendered, name
        assert "NEXTVAL" not in rendered, name
        for column in table.columns:
            # A plain Integer/SmallInteger PK would make SQLAlchemy emit
            # SERIAL/IDENTITY on PostgreSQL; every numeric column here is BIGINT.
            leaf_type = column.type
            assert not isinstance(leaf_type, (sa.SmallInteger, sa.Integer)) or isinstance(
                leaf_type, sa.BigInteger
            ), f"{name}.{column.name}"
            default = column.server_default
            assert default is None or "sequence" not in str(default.arg).lower(), (
                f"{name}.{column.name}"
            )


def test_integer_columns_are_bigint() -> None:
    for name, table in Base.metadata.tables.items():
        for column in table.columns:
            if column.name in {"rev", "seq", "last_seq", "min_retained_seq", "updated_seq"} or (
                column.name == "result_seq"
            ):
                assert isinstance(column.type, sa.BigInteger), f"{name}.{column.name}"


def test_ddl_uses_postgresql_types() -> None:
    notes = ddl("notes")
    assert "UUID" in notes
    assert "BIGINT" in notes
    assert "TIMESTAMP WITH TIME ZONE" in notes
    assert "BYTEA" in ddl("op_ledger")


def test_index_ddl_is_rendered_for_every_declared_index() -> None:
    rendered = {
        str(CreateIndex(index).compile(dialect=PG)).strip().splitlines()[0]
        for table in Base.metadata.tables.values()
        for index in table.indexes
    }

    assert rendered == {
        "CREATE INDEX ix_workspaces_owner_id ON workspaces (owner_id)",
        "CREATE INDEX ix_folders_workspace_id_updated_seq ON folders (workspace_id, updated_seq)",
        "CREATE INDEX ix_folders_workspace_id_parent_id ON folders (workspace_id, parent_id)",
        "CREATE INDEX ix_notes_workspace_id_updated_seq ON notes (workspace_id, updated_seq)",
        "CREATE INDEX ix_notes_workspace_id_folder_id ON notes (workspace_id, folder_id)",
        "CREATE UNIQUE INDEX uq_users_email_normalized ON users (lower(btrim(email)))",
        "CREATE INDEX ix_auth_sessions_user_id ON auth_sessions (user_id)",
        "CREATE INDEX ix_auth_access_tokens_session_id ON auth_access_tokens (session_id)",
        "CREATE INDEX ix_auth_refresh_tokens_session_id ON auth_refresh_tokens (session_id)",
        "CREATE INDEX ix_workspace_members_user_id ON workspace_members (user_id)",
        "CREATE INDEX ix_attachments_workspace_id_state ON attachments (workspace_id, state)",
        "CREATE INDEX ix_attachments_workspace_id_created_at ON attachments "
        "(workspace_id, created_at)",
    }


# --------------------------------------------------------------------------- #
# migration source (metadata must agree with what the revisions emit)
# --------------------------------------------------------------------------- #
def test_migration_0002_declares_every_kernel_table_and_constraint_name() -> None:
    """0002 creates exactly the kernel tables and the names metadata implies.

    Phase 3 adds a second revision, so this check is scoped to what ``0002``
    owns: six ``create_table`` calls and every kernel constraint/index name —
    and it still forbids bulk metadata DDL.
    """
    source = MIGRATION_0002.read_text(encoding="utf-8")

    assert 'revision: str = "0002_sync_kernel_core"' in source
    assert 'down_revision: str | None = "0001_baseline"' in source
    assert not _called_names(source) & {"create_all", "drop_all"}, "bulk metadata DDL is forbidden"

    for table in KERNEL_TABLES:
        assert f'op.create_table(\n        "{table}"' in source, table

    # Every name the ORM metadata resolves to for these six tables must appear
    # verbatim in the revision (wrapped in op.f(), i.e. already final) — and the
    # revision must not invent names the metadata does not know about.
    # ``users`` is the one table 0003 later rewrites, so the name asserted here is
    # the *0002-era* one (plain ``uq_users_email``), not today's functional index.
    metadata_names = {
        str(constraint.name)
        for table_name in KERNEL_TABLES
        for constraint in Base.metadata.tables[table_name].constraints
        if constraint.name is not None
    } | {
        str(index.name)
        for table_name in KERNEL_TABLES
        for index in Base.metadata.tables[table_name].indexes
    }
    metadata_names.discard("uq_users_email_normalized")
    metadata_names.add("uq_users_email")
    revision_names = set(re.findall(r'op\.f\("([^"]+)"\)', source))

    assert metadata_names == revision_names


def test_migration_0002_constraint_sql_matches_metadata() -> None:
    """Rendered DDL of revision and metadata agree, CHECK text included."""
    source = MIGRATION_0002.read_text(encoding="utf-8")

    for table_name in KERNEL_TABLES:
        table = Base.metadata.tables[table_name]
        assert f'"{table.name}"' in source
        for constraint in table.constraints:
            if isinstance(constraint, sa.CheckConstraint):
                sqltext = str(constraint.sqltext)
                assert sqltext in source, f"{table.name}: {sqltext}"


def test_migration_0002_alters_in_both_deferred_fks() -> None:
    """The deferred FK is added by ALTER after both tables exist (plan §7)."""
    source = MIGRATION_0002.read_text(encoding="utf-8")
    deferrable_fks = re.findall(r'op\.create_foreign_key\(\s*op\.f\("([^"]+)"\)', source)

    assert sorted(deferrable_fks) == [
        "fk_folders_workspace_id_updated_seq_change_events",
        "fk_notes_workspace_id_updated_seq_change_events",
    ]
    assert source.count("deferrable=True") == 2
    assert source.count('initially="DEFERRED"') == 2
    assert 'ondelete="NO ACTION"' in source


def test_migration_0002_downgrade_drops_fks_before_tables() -> None:
    source = MIGRATION_0002.read_text(encoding="utf-8")
    downgrade = source.split("def downgrade()", 1)[1]

    first_drop_table = downgrade.index("op.drop_table")
    assert downgrade.index("op.drop_constraint") < first_drop_table
    assert list(re.findall(r'op\.drop_table\("(\w+)"\)', downgrade)) == [
        "op_ledger",
        "notes",
        "folders",
        "change_events",
        "workspaces",
        "users",
    ]


def test_baseline_revision_is_untouched_by_this_phase() -> None:
    """0001 stays a no-op baseline: same revision id and no DDL (plan §7)."""
    source = MIGRATION_0001.read_text(encoding="utf-8")

    assert 'revision: str = "0001_baseline"' in source
    assert "down_revision: str | None = None" in source
    assert (
        _called_names(source)
        & {
            "create_table",
            "drop_table",
            "execute",
            "create_index",
            "add_column",
        }
        == set()
    )
    assert "op." not in source


def test_env_py_imports_models_before_using_metadata() -> None:
    """``alembic check`` must see the model package, by explicit import (plan §3)."""
    source = (API_DIR / "migrations" / "env.py").read_text(encoding="utf-8")

    assert "import revnote_api.models" in source
    assert "target_metadata = Base.metadata" in source
    assert source.index("import revnote_api.models") < source.index(
        "target_metadata = Base.metadata"
    )
    assert not _called_names(source) & {"create_all", "drop_all"}


# --------------------------------------------------------------------------- #
# Phase 3 revision 0003 (source-level contract)
# --------------------------------------------------------------------------- #
def test_migration_0003_follows_0002_and_creates_the_auth_tables() -> None:
    source = MIGRATION_0003.read_text(encoding="utf-8")

    assert 'revision: str = "0003_auth_sessions_members"' in source
    assert 'down_revision: str | None = "0002_sync_kernel_core"' in source
    assert not _called_names(source) & {"create_all", "drop_all"}, "bulk metadata DDL is forbidden"

    for table in AUTH_TABLES:
        assert f'op.create_table(\n        "{table}"' in source, table
    # The kernel tables are not recreated by this revision.
    for table in KERNEL_TABLES:
        assert f'op.create_table(\n        "{table}"' not in source, table


def test_migration_0003_names_match_metadata() -> None:
    """Every name the ORM resolves to for the Phase 3 tables appears in 0003."""
    source = MIGRATION_0003.read_text(encoding="utf-8")
    metadata_names = {
        str(constraint.name)
        for table_name in AUTH_TABLES
        for constraint in Base.metadata.tables[table_name].constraints
        if constraint.name is not None
    } | {
        str(index.name)
        for table_name in AUTH_TABLES
        for index in Base.metadata.tables[table_name].indexes
    }
    revision_names = set(re.findall(r'op\.f\("([^"]+)"\)', source))

    # The revision also touches users: the dropped legacy constraint and the
    # functional index that replaces it.
    assert metadata_names | {"uq_users_email"} == revision_names


def test_migration_0003_check_sql_matches_metadata() -> None:
    source = MIGRATION_0003.read_text(encoding="utf-8")

    for table_name in AUTH_TABLES:
        for constraint in Base.metadata.tables[table_name].constraints:
            if isinstance(constraint, sa.CheckConstraint):
                assert str(constraint.sqltext) in source, f"{table_name}: {constraint.sqltext}"


def test_migration_0003_extends_users_without_backfilling_credentials() -> None:
    """The two columns are added nullable; legacy rows must keep ``NULL`` (D8)."""
    source = MIGRATION_0003.read_text(encoding="utf-8")

    assert 'op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=True))' in (
        source
    )
    assert '"password_hash"' in source and "nullable=True" in source
    assert '"disabled_at"' in source
    # No UPDATE of users anywhere: a migration must never invent or rewrite a hash.
    assert "UPDATE users" not in source.upper().replace('"', "")


def test_migration_0003_swaps_the_email_constraint_for_the_functional_index() -> None:
    source = MIGRATION_0003.read_text(encoding="utf-8")
    upgrade = source.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]

    drop = upgrade.index('op.drop_constraint(op.f("uq_users_email")')
    # The *usage* of the constant inside upgrade(), not its module-level definition.
    create = upgrade.index("op.create_index(\n        NORMALIZED_EMAIL_INDEX")
    assert drop < create, "the legacy constraint is dropped before the index is created"
    assert 'sa.text("lower(btrim(email))")' in upgrade
    assert "unique=True" in upgrade


def _migration_sql(source: str) -> str:
    """Every SQL string literal the revision executes (docstrings/comments excluded)."""
    blocks: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "text" or not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            blocks.append(argument.value)
    return "\n".join(blocks)


def test_migration_0003_refuses_to_merge_legacy_email_collisions() -> None:
    """A normalisation collision aborts the migration; accounts are never merged."""
    source = MIGRATION_0003.read_text(encoding="utf-8")

    assert "RAISE EXCEPTION" in source
    assert "HAVING count(*) > 1" in source
    # Checked before any DDL in upgrade: nothing has changed if it fires.
    upgrade = source.split("def upgrade()", 1)[1]
    assert upgrade.index("_refuse_collisions") < upgrade.index("op.add_column")
    # And before the old constraint is restored on the way down.
    downgrade = source.split("def downgrade()", 1)[1]
    assert downgrade.index("_refuse_collisions") < downgrade.index("op.create_unique_constraint")

    # The guard is a read-only probe: no account is deleted, updated or merged.
    sql = _migration_sql(source).upper()
    assert "DELETE FROM USERS" not in sql
    assert "UPDATE USERS" not in sql
    assert "DROP TABLE USERS" not in sql
    assert "GROUP BY EMAIL" in sql or "GROUP BY LOWER(BTRIM(EMAIL))" in sql


def test_migration_0003_collision_message_carries_no_account_data() -> None:
    """A migration error is logged: it must not print an address, id or count."""
    source = MIGRATION_0003.read_text(encoding="utf-8")
    sql = _migration_sql(source)

    assert "RAISE EXCEPTION" in sql
    # No placeholder arguments: the message is a fixed string.
    raise_clauses = [line.strip() for line in sql.splitlines() if "RAISE EXCEPTION" in line.upper()]
    assert raise_clauses
    for clause in raise_clauses:
        assert "%" not in clause, clause
    # ``USING`` attaches dynamic detail to a RAISE; no such clause may exist.
    assert " USING " not in sql.upper(), "no dynamic detail may be attached to the error"


def test_migration_0003_backfills_owner_membership() -> None:
    """Every existing workspace gets one owner membership, derived from owner_id."""
    source = MIGRATION_0003.read_text(encoding="utf-8")

    assert "INSERT INTO workspace_members" in source
    assert "SELECT id, owner_id, 'owner'" in source
    assert "FROM workspaces" in source


def test_migration_0003_downgrade_is_the_exact_reverse() -> None:
    source = MIGRATION_0003.read_text(encoding="utf-8")
    downgrade = source.split("def downgrade()", 1)[1]

    # The refusal guard runs before any DDL, so a refusal leaves the schema whole.
    assert downgrade.index("_refuse_collisions") < downgrade.index("op.drop_table")

    # Children before parents: tokens/members reference sessions/users.
    assert list(re.findall(r'op\.drop_table\("(\w+)"\)', downgrade)) == [
        "workspace_members",
        "auth_refresh_tokens",
        "auth_access_tokens",
        "auth_sessions",
    ]
    assert downgrade.index("op.drop_index") < downgrade.index("op.create_unique_constraint")
    assert list(re.findall(r'op\.drop_column\("users", "(\w+)"\)', downgrade)) == [
        "disabled_at",
        "password_hash",
    ]
    # Symmetry guard: the same two columns are added in upgrade.
    assert list(re.findall(r'op\.add_column\(\s*"users",\s*sa\.Column\("(\w+)"', source)) == [
        "password_hash",
        "disabled_at",
    ]
