"""Focused metadata tests for the Phase 2 sync kernel schema.

These are **pure unit tests**: no server, no connection, no ``create_all()``. They
assert the contract that ``docs/plan-phase2.md`` §4 fixes for the six tables —
column presence/NULLability/defaults, primary keys, composite foreign keys with
their ``ON DELETE`` behaviour, deferrability, named checks and indexes — and that
the same DDL is what the Alembic revision emits.

Why assertions on metadata instead of a live database: the DDL text is produced
from exactly this metadata (``alembic upgrade --sql``), so a metadata regression
is a schema regression. The real PostgreSQL round trip is exercised separately in
``tests/integration/test_migrations.py``.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from revnote_api.db.base import NAMING_CONVENTION, Base
from revnote_api.models import MODEL_TABLES, ChangeEvent, Folder, Note, OpLedger, User, Workspace

API_DIR = Path(__file__).resolve().parents[2]
MIGRATION_0002 = API_DIR / "migrations" / "versions" / "0002_sync_kernel_core.py"
MIGRATION_0001 = API_DIR / "migrations" / "versions" / "0001_baseline.py"

PG = postgresql.dialect()

#: The exact table set of this phase (plan §4): six domain tables, nothing else.
EXPECTED_TABLES = (
    "users",
    "workspaces",
    "folders",
    "notes",
    "change_events",
    "op_ledger",
)


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
def test_metadata_declares_exactly_the_six_kernel_tables() -> None:
    assert tuple(sorted(Base.metadata.tables)) == tuple(sorted(EXPECTED_TABLES))
    assert tuple(MODEL_TABLES) == EXPECTED_TABLES


def test_out_of_scope_tables_are_absent() -> None:
    """Phase 2 forbids extra business tables (plan §1): none may be declared."""
    forbidden = {
        "attachments",
        "change_seq",
        "outbox",
        "revisions",
        "shared_links",
        "sync_sessions",
        "tombstones",
        "user_workspaces",
        "workspace_members",
    }

    assert not set(Base.metadata.tables) & forbidden


def test_mappers_configure_without_mapper_errors() -> None:
    """No half-declared relationships / duplicate table names behind the scenes."""
    sa.orm.configure_mappers()


def test_only_one_declarative_base_is_in_use() -> None:
    """Every model sits on the shared Base.metadata — no second metadata."""
    for model in (User, Workspace, Folder, Note, ChangeEvent, OpLedger):
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
    """The full named CHECK inventory (plan §4) — no missing, extra or unnamed CHECK.

    A composite-PK table has no separate PK-constraint CHECK entry, so this table
    is also the guard that no CHECK is silently added by a naming convention.
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
    }

    for table_name, checks in expected.items():
        assert _checks(table_name) == checks, table_name


# --------------------------------------------------------------------------- #
# users / workspaces
# --------------------------------------------------------------------------- #
def test_users_columns_pk_and_unique_email() -> None:
    table = Base.metadata.tables["users"]

    assert [c.name for c in table.columns] == ["id", "email", "created_at"]
    assert _col("users", "id").primary_key
    assert not _col("users", "id").nullable
    assert isinstance(_col("users", "id").type, postgresql.UUID)
    assert not _col("users", "email").nullable
    assert isinstance(_col("users", "email").type, sa.Text)

    assert {c.name for c in table.constraints if isinstance(c, sa.UniqueConstraint)} == {
        "uq_users_email"
    }
    # Exactly one CHECK: the non-blank guard. No auth columns, no normalisation,
    # no syntax/format validation (the plan requires a non-empty e-mail only).
    assert _checks("users") == {"ck_users_email_not_blank": "btrim(email) <> ''"}
    assert not _fks("users")
    assert not table.indexes


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
    }


# --------------------------------------------------------------------------- #
# migration source (metadata must agree with what the revision emits)
# --------------------------------------------------------------------------- #
def test_migration_0002_declares_every_table_and_constraint_name() -> None:
    source = MIGRATION_0002.read_text(encoding="utf-8")

    assert 'revision: str = "0002_sync_kernel_core"' in source
    assert 'down_revision: str | None = "0001_baseline"' in source
    assert not _called_names(source) & {"create_all", "drop_all"}, "bulk metadata DDL is forbidden"

    for table in EXPECTED_TABLES:
        assert f'op.create_table(\n        "{table}"' in source, table

    # Every name the ORM metadata resolves to must appear verbatim in the
    # revision (wrapped in op.f(), i.e. already final) — and the revision must
    # not invent constraint names the metadata does not know about.
    metadata_names = {
        str(constraint.name)
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if constraint.name is not None
    } | {str(index.name) for table in Base.metadata.tables.values() for index in table.indexes}
    revision_names = set(re.findall(r'op\.f\("([^"]+)"\)', source))

    assert metadata_names == revision_names


def test_migration_0002_constraint_sql_matches_metadata() -> None:
    """Rendered DDL of revision and metadata agree, CHECK text included."""
    source = MIGRATION_0002.read_text(encoding="utf-8")

    for table in Base.metadata.tables.values():
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
    """``alembic check`` must see the six tables, by explicit import (plan §3)."""
    source = (API_DIR / "migrations" / "env.py").read_text(encoding="utf-8")

    assert "import revnote_api.models" in source
    assert "target_metadata = Base.metadata" in source
    assert source.index("import revnote_api.models") < source.index(
        "target_metadata = Base.metadata"
    )
    assert not _called_names(source) & {"create_all", "drop_all"}
