"""``attachments`` metadata, migration-source and repr-safety contract.

Pure unit tests: no server, no connection, no ``create_all()``. They assert the
schema shape the service and the GC depend on (constraint names, NULLability, the
state invariant, the indexes) and that the Alembic revision emits exactly that DDL
under the same names - which is what keeps ``alembic check`` diff-clean.

The repr tests are not cosmetic: a model repr ends up in logs and tracebacks, and
``file_name`` is user content while ``content_hash`` is content-derived. Neither may
be reachable from it.
"""

from __future__ import annotations

import ast
import re
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from revnote_api.db.base import Base
from revnote_api.models import ATTACHMENT_STATES, MODEL_TABLES, Attachment

API_DIR = Path(__file__).resolve().parents[2]
MIGRATION_0004 = API_DIR / "migrations" / "versions" / "0004_attachments.py"

PG = postgresql.dialect()

#: The exact table set after Phase 5: the ten Phase 2/3 tables plus attachments.
EXPECTED_TABLES = (
    "users",
    "workspaces",
    "folders",
    "notes",
    "change_events",
    "op_ledger",
    "auth_sessions",
    "auth_access_tokens",
    "auth_refresh_tokens",
    "workspace_members",
    "attachments",
    # Phase 6: additive bridge tables (not kernel entities either).
    "joplin_id_map",
    "joplin_locks",
    "joplin_sessions",
)

TABLE = "attachments"


def _checks(table: str = TABLE) -> dict[str, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in Base.metadata.tables[table].constraints
        if isinstance(constraint, sa.CheckConstraint)
    }


def _fks(table: str = TABLE) -> dict[str, sa.ForeignKeyConstraint]:
    return {
        constraint.name: constraint
        for constraint in Base.metadata.tables[table].constraints
        if isinstance(constraint, sa.ForeignKeyConstraint)
    }


def _indexes(table: str = TABLE) -> set[tuple[str, tuple[str, ...]]]:
    return {
        (index.name, tuple(column.name for column in index.columns))
        for index in Base.metadata.tables[table].indexes
    }


def _called_names(source: str) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #
def test_attachments_is_registered_in_model_tables() -> None:
    assert "attachments" in MODEL_TABLES
    assert tuple(MODEL_TABLES) == EXPECTED_TABLES
    assert TABLE in Base.metadata.tables


def test_attachment_states_match_the_check() -> None:
    assert ATTACHMENT_STATES == ("pending", "ready", "deleted")
    assert "state IN ('pending', 'ready', 'deleted')" in _checks()["ck_attachments_state_known"]


def test_attachment_table_declares_no_kernel_columns() -> None:
    """D3/D7: no ``rev``, no ``seq``, no ``updated_seq``, no ``note_id``."""
    columns = {column.name for column in Base.metadata.tables[TABLE].columns}

    assert not columns & {"rev", "seq", "updated_seq", "note_id", "workspace_seq", "op_id"}


def test_attachment_columns_are_exactly_the_contract_set() -> None:
    assert [column.name for column in Base.metadata.tables[TABLE].columns] == [
        "id",
        "workspace_id",
        "state",
        "mime",
        "byte_size",
        "content_hash",
        "storage_key",
        "upload_nonce",
        "file_name",
        "created_at",
        "upload_started_at",
        "ready_at",
        "deleted_at",
        "delete_reason",
    ]


# --------------------------------------------------------------------------- #
# keys, fks, checks, indexes
# --------------------------------------------------------------------------- #
def test_primary_key_is_named_by_the_convention() -> None:
    table = Base.metadata.tables[TABLE]

    assert str(table.primary_key.name) == "pk_attachments"
    assert [column.name for column in table.primary_key.columns] == ["id"]
    assert isinstance(table.c["id"].type, postgresql.UUID)
    assert not table.c["id"].nullable


def test_workspace_fk_is_restrict_and_immediate() -> None:
    fk = _fks()["fk_attachments_workspace_id_workspaces"]

    assert fk.ondelete == "RESTRICT"
    assert fk.deferrable is None
    assert [column.name for column in fk.columns] == ["workspace_id"]
    assert [element.target_fullname for element in fk.elements] == ["workspaces.id"]
    assert isinstance(Base.metadata.tables[TABLE].c["workspace_id"].type, postgresql.UUID)


def test_storage_key_is_unique() -> None:
    uniques = {
        (constraint.name, tuple(column.name for column in constraint.columns))
        for constraint in Base.metadata.tables[TABLE].constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }

    assert uniques == {("uq_attachments_storage_key", ("storage_key",))}


def test_every_check_is_named_with_the_table_prefix() -> None:
    checks = _checks()

    assert set(checks) == {
        "ck_attachments_state_known",
        "ck_attachments_mime_len",
        "ck_attachments_byte_size_positive",
        "ck_attachments_content_hash_len",
        "ck_attachments_storage_key_len",
        "ck_attachments_ready_iff_hash",
    }
    for name in checks:
        assert name.startswith("ck_attachments_"), name


def test_check_expressions_are_pinned_verbatim() -> None:
    """A semantic drift in any of these would change what a row may be."""
    assert _checks() == {
        "ck_attachments_state_known": "state IN ('pending', 'ready', 'deleted')",
        "ck_attachments_mime_len": "octet_length(mime) BETWEEN 3 AND 127",
        "ck_attachments_byte_size_positive": "byte_size IS NULL OR byte_size > 0",
        "ck_attachments_content_hash_len": (
            "content_hash IS NULL OR octet_length(content_hash) = 32"
        ),
        "ck_attachments_storage_key_len": ("octet_length(storage_key) BETWEEN 8 AND 512"),
        "ck_attachments_ready_iff_hash": "(state = 'ready') = (content_hash IS NOT NULL)",
    }


def test_the_state_invariant_is_an_equivalence_not_an_implication() -> None:
    """``ready <=> digest``: a pending row with a digest is also rejected."""
    expression = _checks()["ck_attachments_ready_iff_hash"]

    assert expression.startswith("(state = 'ready') = (content_hash IS NOT NULL)")
    # ``=>`` would let a pending row keep a digest; ``=`` does not.
    assert "=>" not in expression


def test_indexes_are_the_two_the_service_scans_with() -> None:
    assert _indexes() == {
        ("ix_attachments_workspace_id_state", ("workspace_id", "state")),
        ("ix_attachments_workspace_id_created_at", ("workspace_id", "created_at")),
    }


def test_index_ddl_is_rendered_under_the_convention_names() -> None:
    rendered = {
        str(CreateIndex(index).compile(dialect=PG)).strip().splitlines()[0]
        for index in Base.metadata.tables[TABLE].indexes
    }

    assert rendered == {
        "CREATE INDEX ix_attachments_workspace_id_state ON attachments (workspace_id, state)",
        "CREATE INDEX ix_attachments_workspace_id_created_at "
        "ON attachments (workspace_id, created_at)",
    }


# --------------------------------------------------------------------------- #
# nullability and types
# --------------------------------------------------------------------------- #
def test_byte_size_and_content_hash_are_nullable_bigint_and_bytea() -> None:
    table = Base.metadata.tables[TABLE]

    assert table.c["byte_size"].nullable
    assert isinstance(table.c["byte_size"].type, sa.BigInteger)
    assert table.c["byte_size"].server_default is None

    assert table.c["content_hash"].nullable
    assert isinstance(table.c["content_hash"].type, postgresql.BYTEA)
    assert table.c["content_hash"].server_default is None


def test_required_text_columns_are_not_null() -> None:
    table = Base.metadata.tables[TABLE]

    for name in ("state", "mime", "storage_key"):
        assert not table.c[name].nullable, name
        assert isinstance(table.c[name].type, sa.Text), name


def test_upload_nonce_is_a_required_uuid() -> None:
    column = Base.metadata.tables[TABLE].c["upload_nonce"]

    assert not column.nullable
    assert isinstance(column.type, postgresql.UUID)
    assert column.server_default is None


def test_file_name_and_delete_reason_are_optional() -> None:
    table = Base.metadata.tables[TABLE]

    assert table.c["file_name"].nullable
    assert table.c["delete_reason"].nullable
    for name in ("file_name", "delete_reason"):
        assert table.c[name].server_default is None, name


def test_timestamps_are_timestamptz_with_only_created_at_defaulted() -> None:
    table = Base.metadata.tables[TABLE]

    for name in ("created_at", "upload_started_at", "ready_at", "deleted_at"):
        column = table.c[name]
        assert isinstance(column.type, postgresql.TIMESTAMP), name
        assert column.type.timezone is True, name

    assert not table.c["created_at"].nullable
    assert str(table.c["created_at"].server_default.arg) == "now()"
    # Every other timestamp is set by the service, never by a trigger or a default.
    for name in ("upload_started_at", "ready_at", "deleted_at"):
        assert table.c[name].nullable, name
        assert table.c[name].server_default is None, name


def test_no_sequence_or_identity_is_used() -> None:
    rendered = str(CreateTable(Base.metadata.tables[TABLE]).compile(dialect=PG)).upper()

    assert "SERIAL" not in rendered
    assert "IDENTITY" not in rendered
    assert "NEXTVAL" not in rendered


# --------------------------------------------------------------------------- #
# repr safety
# --------------------------------------------------------------------------- #
def test_repr_omits_the_file_name() -> None:
    sentinel = "private-quarterly-report.pdf"

    row = Attachment(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        state="ready",
        mime="application/pdf",
        byte_size=12,
        storage_key="ws/x/att/y/z",
        upload_nonce=uuid.uuid4(),
        file_name=sentinel,
    )

    assert sentinel not in repr(row)


def test_repr_omits_the_content_hash() -> None:
    digest = bytes(range(32))

    row = Attachment(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        state="ready",
        mime="application/pdf",
        byte_size=12,
        content_hash=digest,
        storage_key="ws/x/att/y/z",
        upload_nonce=uuid.uuid4(),
    )

    rendered = repr(row)
    assert digest.hex() not in rendered
    assert repr(digest) not in rendered


def test_repr_still_carries_the_diagnostic_fields() -> None:
    """A repr that says nothing is useless: ids, state and size stay."""
    workspace = uuid.uuid4()
    row = Attachment(
        id=uuid.uuid4(),
        workspace_id=workspace,
        state="pending",
        mime="image/png",
        storage_key="ws/x/att/y/z",
        upload_nonce=uuid.uuid4(),
    )

    rendered = repr(row)
    assert rendered.startswith("Attachment(")
    assert str(workspace) in rendered
    assert "'pending'" in rendered


def test_model_module_imports_no_web_or_service_layer() -> None:
    source = (API_DIR / "src" / "revnote_api" / "models" / "attachment.py").read_text(
        encoding="utf-8"
    )
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    for forbidden in (
        "fastapi",
        "starlette",
        "revnote_api.api",
        "revnote_api.services",
        "revnote_api.config",
        "revnote_api.db.engine",
        "revnote_api.db.session",
    ):
        assert forbidden not in imported, forbidden


# --------------------------------------------------------------------------- #
# migration source
# --------------------------------------------------------------------------- #
def test_migration_0004_is_the_head_on_top_of_0003() -> None:
    source = MIGRATION_0004.read_text(encoding="utf-8")

    assert 'revision: str = "0004_attachments"' in source
    assert 'down_revision: str | None = "0003_auth_sessions_members"' in source
    assert not _called_names(source) & {"create_all", "drop_all"}, "bulk metadata DDL forbidden"
    assert source.count("def upgrade") == 1
    assert source.count("def downgrade") == 1


def test_migration_0004_creates_only_the_attachments_table() -> None:
    source = MIGRATION_0004.read_text(encoding="utf-8")

    created = re.findall(r'op\.create_table\(\s*"(\w+)"', source)
    assert created == ["attachments"]
    # And it must not touch any of the ten existing tables.
    for existing in EXPECTED_TABLES[:10]:
        assert f'op.create_table(\n        "{existing}"' not in source, existing
        assert f'op.add_column(\n        "{existing}"' not in source, existing
        assert f'op.drop_table("{existing}")' not in source, existing
        assert f'op.drop_column("{existing}"' not in source, existing


def test_migration_0004_never_mentions_a_kernel_table_in_a_statement() -> None:
    """No ALTER, no UPDATE, no INSERT: this revision is purely additive DDL."""
    source = MIGRATION_0004.read_text(encoding="utf-8")
    calls = _called_names(source)

    assert not calls & {"add_column", "drop_column", "alter_column", "execute", "bulk_insert"}
    assert "INSERT INTO" not in source.upper()
    assert "UPDATE " not in source.upper()
    assert "DELETE FROM" not in source.upper()


def test_migration_0004_names_match_the_metadata_exactly() -> None:
    """Every name the ORM resolves to appears in the revision, and nothing else."""
    source = MIGRATION_0004.read_text(encoding="utf-8")

    metadata_names = {
        str(constraint.name)
        for constraint in Base.metadata.tables[TABLE].constraints
        if constraint.name is not None
    } | {str(index.name) for index in Base.metadata.tables[TABLE].indexes}
    revision_names = set(re.findall(r'op\.f\("([^"]+)"\)', source))

    assert metadata_names == revision_names


def test_migration_0004_check_sql_matches_the_metadata() -> None:
    source = MIGRATION_0004.read_text(encoding="utf-8")

    for constraint in Base.metadata.tables[TABLE].constraints:
        if isinstance(constraint, sa.CheckConstraint):
            assert str(constraint.sqltext) in source, str(constraint.sqltext)


def test_migration_0004_downgrade_drops_only_its_own_objects() -> None:
    source = MIGRATION_0004.read_text(encoding="utf-8")
    downgrade = source.split("def downgrade()", 1)[1]

    assert list(re.findall(r'op\.drop_table\("(\w+)"\)', downgrade)) == ["attachments"]
    dropped_indexes = re.findall(r'op\.drop_index\(\s*op\.f\("([^"]+)"\)', downgrade)
    assert sorted(dropped_indexes) == [
        "ix_attachments_workspace_id_created_at",
        "ix_attachments_workspace_id_state",
    ]
    # Nothing else is dropped: no constraint, no column, no other table.
    assert "op.drop_constraint" not in downgrade
    assert "op.drop_column" not in downgrade


def test_migration_0004_is_pure_ddl_without_data_statements() -> None:
    source = MIGRATION_0004.read_text(encoding="utf-8")

    assert "sa.text(" not in source.replace('server_default=sa.text("now()")', "")
