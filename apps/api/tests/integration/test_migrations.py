"""Alembic round trips and real-catalog schema proof (migration database only).

Everything here runs against ``TEST_MIGRATION_DATABASE_URL``, the disposable
database that may be destroyed. The business database is never downgraded:
:func:`run_alembic` refuses it and ``test_downgrade_refused_for_business_db``
proves the guard fires before a subprocess starts.

Round trip exercised: ``base -> baseline -> head -> baseline -> head -> base``.
Assertions read ``pg_catalog`` / ``information_schema`` and trigger real
constraint violations, so a revision that documents one thing and creates another
fails here.
"""

from __future__ import annotations

import pytest
from pg_migrations import AlembicError, AlembicGuardError, MigrationDatabase
from pg_testing import (
    BASELINE_REVISION,
    HEAD_TABLES,
    KERNEL_REVISION,
    KERNEL_TABLES,
    VERSION_TABLE,
    Catalog,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

pytestmark = [pytest.mark.anyio, pytest.mark.integration, pytest.mark.migrations]

SENTINEL = "migration_sentinel"

# Fixed UUIDs: readable in failures, unique because each case starts from scratch.
USER = "018f0000-0000-7000-8000-000000000001"
WORKSPACE = "018f0000-0000-7000-8000-000000000002"
WORKSPACE_B = "018f0000-0000-7000-8000-000000000003"
FOLDER_A = "018f0000-0000-7000-8000-000000000004"
OP = "018f0000-0000-7000-8000-000000000005"
MISSING_USER = "018f0000-0000-7000-8000-00000000d00d"
FREE_ENTITY = "018f0000-0000-7000-8000-0000000000ff"

GOOD_HASH = b"\x2a" * 32
BAD_HASH = b"\x2a" * 31


@pytest.fixture
def at_base(alembic_migrations: MigrationDatabase) -> MigrationDatabase:
    """Start each case from a clean slate (``downgrade base`` is idempotent)."""
    alembic_migrations.downgrade("base")
    return alembic_migrations


async def seed_owner(session_factory, *, with_event: bool = False, extra_workspace=False) -> None:
    """Commit the minimal rows the constraint cases need.

    Raw SQL on purpose: this module proves the *schema*, so it must not depend on
    the service layer.
    """
    async with session_factory() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, 'migrations@revnote.test')"),
            {"id": USER},
        )
        workspaces = [WORKSPACE] + ([WORKSPACE_B] if extra_workspace else [])
        for workspace in workspaces:
            await session.execute(
                text(
                    "INSERT INTO workspaces (id, owner_id, last_seq) "
                    "VALUES (:id, :owner, :last_seq)"
                ),
                {"id": workspace, "owner": USER, "last_seq": 1 if with_event else 0},
            )
        if with_event:
            # entity_id is a deliberate polymorphic reference: no FK, any uuid works.
            await session.execute(
                text(
                    "INSERT INTO change_events "
                    "(workspace_id, seq, entity_type, entity_id, op, rev, op_id) "
                    "VALUES (:ws, 1, 'folder', :entity, 'create', 1, :op)"
                ),
                {"ws": WORKSPACE, "entity": FREE_ENTITY, "op": OP},
            )
        await session.commit()


# --------------------------------------------------------------------------- #
# gating
# --------------------------------------------------------------------------- #


def test_business_and_migration_databases_are_different(
    postgres_targets, alembic_migrations: MigrationDatabase
) -> None:
    """The destructive round trip cannot share a database with the business tests."""
    assert postgres_targets.business_identity != postgres_targets.migration_identity
    assert alembic_migrations.business_url != alembic_migrations.url
    alembic_migrations.assert_isolated_from_business()


def test_revision_chain_has_exactly_one_head(
    alembic_heads: tuple[str, ...], head_revision: str
) -> None:
    """One head, the sync-kernel revision sitting on the baseline."""
    assert alembic_heads == (KERNEL_REVISION,)
    assert head_revision == KERNEL_REVISION


def test_downgrade_refused_for_business_db(alembic_cli, normal_database_url: str) -> None:
    """``downgrade`` against ``TEST_DATABASE_URL`` is blocked before any subprocess."""
    with pytest.raises(AlembicGuardError, match="only permitted"):
        alembic_cli.run(normal_database_url, "downgrade", "base")
    with pytest.raises(AlembicGuardError):
        alembic_cli.guard(normal_database_url)


def test_downgrade_permitted_on_migration_db(alembic_cli, migration_database_url: str) -> None:
    """The same call against the migration DSN passes the guard and runs."""
    alembic_cli.guard(migration_database_url)
    assert alembic_cli.run(migration_database_url, "current").returncode == 0


# --------------------------------------------------------------------------- #
# the round trip
# --------------------------------------------------------------------------- #


async def test_baseline_creates_only_the_version_table(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """``base`` -> baseline: the empty baseline creates no business table."""
    at_base.upgrade(BASELINE_REVISION)

    assert await migration_catalog.tables() == [VERSION_TABLE]
    assert await migration_catalog.version() == BASELINE_REVISION
    assert await migration_catalog.sequences() == []


async def test_head_creates_exactly_six_tables_plus_version(
    at_base: MigrationDatabase, migration_catalog: Catalog, head_revision: str
) -> None:
    """``upgrade head`` -> the six domain tables + ``alembic_version``, nothing else."""
    at_base.upgrade("head")

    assert await migration_catalog.tables() == list(HEAD_TABLES)
    assert await migration_catalog.version() == head_revision


async def test_downgrade_to_baseline_removes_all_business_tables(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """``head`` -> baseline drops every business table (schema reversible)."""
    at_base.upgrade("head")
    assert await migration_catalog.table_set() == {*KERNEL_TABLES, VERSION_TABLE}

    at_base.downgrade(BASELINE_REVISION)

    assert await migration_catalog.tables() == [VERSION_TABLE]
    assert await migration_catalog.version() == BASELINE_REVISION


async def test_full_round_trip_and_reapply(
    at_base: MigrationDatabase, migration_catalog: Catalog, head_revision: str
) -> None:
    """``base -> baseline -> head -> baseline -> head`` lands correctly each time."""
    at_base.upgrade(BASELINE_REVISION)
    assert await migration_catalog.version() == BASELINE_REVISION

    at_base.upgrade("head")
    head_tables = await migration_catalog.tables()
    assert head_tables == list(HEAD_TABLES)
    assert await migration_catalog.version() == head_revision

    at_base.downgrade(BASELINE_REVISION)
    assert await migration_catalog.tables() == [VERSION_TABLE]

    at_base.upgrade("head")
    assert await migration_catalog.tables() == head_tables


async def test_downgrade_to_base_leaves_an_empty_version_table(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """``head`` -> ``base``: business tables go, nothing is stamped any more.

    Alembic keeps ``alembic_version`` and deletes only its row, so the precise
    expectation is "table present, no revision" (asserted, not assumed).
    """
    at_base.upgrade("head")
    at_base.downgrade("base")

    assert await migration_catalog.tables() == [VERSION_TABLE]
    assert await migration_catalog.version() is None
    assert await migration_catalog.sequences() == []


async def test_repeated_upgrade_and_downgrade_are_no_ops(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """Re-running the same target must neither fail nor drift the schema."""
    at_base.upgrade("head")
    first = await migration_catalog.tables()
    at_base.upgrade("head")
    assert await migration_catalog.tables() == first

    at_base.downgrade("base")
    at_base.downgrade("base")
    assert await migration_catalog.version() is None


async def test_unrelated_sentinel_table_survives(
    at_base: MigrationDatabase, migration_session_factory, migration_catalog: Catalog
) -> None:
    """A downgrade drops only what the revision owns; unrelated tables are kept."""
    async with migration_session_factory() as session:
        await session.execute(text(f"CREATE TABLE {SENTINEL} (id int PRIMARY KEY, note text)"))
        await session.execute(text(f"INSERT INTO {SENTINEL} VALUES (1, 'keep me')"))
        await session.commit()

    at_base.upgrade("head")
    at_base.downgrade("base")
    assert await migration_catalog.table_set() == {VERSION_TABLE, SENTINEL}

    async with migration_session_factory() as session:
        row = (await session.execute(text(f"SELECT id, note FROM {SENTINEL}"))).one()
        assert (row.id, row.note) == (1, "keep me")
        await session.execute(text(f"DROP TABLE {SENTINEL}"))
        await session.commit()


async def test_alembic_check_reports_no_diff(
    at_base: MigrationDatabase, migration_catalog: Catalog, head_revision: str
) -> None:
    """The migrated schema and ``Base.metadata`` agree (autogenerate is diff-clean)."""
    at_base.upgrade("head")

    result = at_base.check()

    assert result.returncode == 0, result.output
    assert "No new upgrade operations detected" in result.output
    assert await migration_catalog.version() == head_revision


async def test_unknown_revision_target_fails_loudly(
    at_base: MigrationDatabase, alembic_cli, migration_database_url: str
) -> None:
    """A bogus revision id is an error, never a silent no-op."""
    with pytest.raises(AlembicError):
        alembic_cli.run(migration_database_url, "upgrade", "0099_does_not_exist")


# --------------------------------------------------------------------------- #
# catalog shape at head
# --------------------------------------------------------------------------- #


async def test_no_sequences_and_no_nextval_defaults(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """No SERIAL/IDENTITY/nextval: keys and counters are allocated in application code."""
    at_base.upgrade("head")

    assert await migration_catalog.sequences() == []
    assert await migration_catalog.nextval_defaults() == []
    for table in KERNEL_TABLES:
        identity = [c.name for c in await migration_catalog.columns(table) if c.is_identity]
        assert identity == [], f"{table} has identity columns: {identity}"


async def test_primary_keys_and_uniques_match_the_contract(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """PK/UNIQUE definitions are exactly the approved ones (single and composite)."""
    at_base.upgrade("head")

    expected = {
        "pk_users": ("users", "PRIMARY KEY (id)"),
        "uq_users_email": ("users", "UNIQUE (email)"),
        "pk_workspaces": ("workspaces", "PRIMARY KEY (id)"),
        "pk_folders": ("folders", "PRIMARY KEY (id)"),
        "uq_folders_workspace_id_id": ("folders", "UNIQUE (workspace_id, id)"),
        "pk_notes": ("notes", "PRIMARY KEY (id)"),
        "pk_change_events": ("change_events", "PRIMARY KEY (workspace_id, seq)"),
        "uq_change_events_workspace_id_op_id": (
            "change_events",
            "UNIQUE (workspace_id, op_id)",
        ),
        "pk_op_ledger": ("op_ledger", "PRIMARY KEY (workspace_id, op_id)"),
        "uq_op_ledger_workspace_id_result_seq": (
            "op_ledger",
            "UNIQUE (workspace_id, result_seq)",
        ),
    }
    for name, (table, definition) in expected.items():
        constraints = await migration_catalog.constraint_map(table)
        assert constraints[name].definition == definition, name
        assert constraints[name].kind in ("primary key", "unique"), name


async def test_foreign_keys_columns_and_on_delete(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """FK targets/columns and ``ON DELETE RESTRICT`` match the contract exactly."""
    at_base.upgrade("head")

    expected = {
        "fk_workspaces_owner_id_users": (
            "workspaces",
            "FOREIGN KEY (owner_id) REFERENCES users(id) ON DELETE RESTRICT",
        ),
        "fk_folders_workspace_id_workspaces": (
            "folders",
            "FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT",
        ),
        "fk_folders_workspace_id_parent_id_folders": (
            "folders",
            "FOREIGN KEY (workspace_id, parent_id) REFERENCES "
            "folders(workspace_id, id) ON DELETE RESTRICT",
        ),
        "fk_notes_workspace_id_workspaces": (
            "notes",
            "FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT",
        ),
        "fk_notes_workspace_id_folder_id_folders": (
            "notes",
            "FOREIGN KEY (workspace_id, folder_id) REFERENCES "
            "folders(workspace_id, id) ON DELETE RESTRICT",
        ),
        "fk_change_events_workspace_id_workspaces": (
            "change_events",
            "FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT",
        ),
        "fk_op_ledger_workspace_id_workspaces": (
            "op_ledger",
            "FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE RESTRICT",
        ),
        "fk_op_ledger_workspace_id_result_seq_change_events": (
            "op_ledger",
            "FOREIGN KEY (workspace_id, result_seq) REFERENCES "
            "change_events(workspace_id, seq) ON DELETE RESTRICT",
        ),
    }
    for name, (table, definition) in expected.items():
        constraint = (await migration_catalog.constraint_map(table))[name]
        assert constraint.kind == "foreign key", name
        assert constraint.definition == definition, name
        assert constraint.on_delete == "RESTRICT", name
        # The ledger FK stays immediate: it is the "result exists" dam.
        assert constraint.deferrable is False, name


async def test_only_object_to_event_fks_are_deferred(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """Exactly two deferrable FKs: ``NO ACTION DEFERRABLE INITIALLY DEFERRED``."""
    at_base.upgrade("head")

    found = [
        (table, constraint.name)
        for table in KERNEL_TABLES
        for constraint in await migration_catalog.constraints(table)
        if constraint.kind == "foreign key" and constraint.deferrable
    ]

    assert found == [
        ("folders", "fk_folders_workspace_id_updated_seq_change_events"),
        ("notes", "fk_notes_workspace_id_updated_seq_change_events"),
    ]
    for table, name in found:
        constraint = (await migration_catalog.constraint_map(table))[name]
        assert constraint.initially_deferred is True, name
        assert constraint.on_delete == "NO ACTION", name
        assert constraint.definition == (
            "FOREIGN KEY (workspace_id, updated_seq) "
            "REFERENCES change_events(workspace_id, seq) "
            "DEFERRABLE INITIALLY DEFERRED"
        ), name


async def test_every_check_constraint_is_named(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """All 16 contract CHECKs exist under their exact names, and nothing else.

    The set is exact per table - no missing CHECK, no extra unnamed CHECK, and no
    drift between the models, the revision and the live catalog. ``users`` carries
    ``ck_users_email_not_blank``, which rejects '' and space-only e-mails (plain
    ``btrim(email)`` trims spaces only, so tabs/newlines still pass - see the
    behavioural test for the exact reach of the guard).
    """
    at_base.upgrade("head")

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
        "op_ledger": {"ck_op_ledger_request_hash_len", "ck_op_ledger_result_seq_positive"},
    }
    total = 0
    for table, wanted in expected.items():
        found = {c.name for c in await migration_catalog.constraints(table) if c.kind == "check"}
        assert found == wanted, f"{table}: expected {sorted(wanted)}, found {sorted(found)}"
        total += len(wanted)
    assert total == 16


async def test_check_constraint_definitions_match(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """Each CHECK's expression is the approved one (read back from pg_constraint)."""
    at_base.upgrade("head")

    expected = {
        "users": {"ck_users_email_not_blank": "CHECK ((btrim(email) <> ''::text))"},
        "workspaces": {
            "ck_workspaces_last_seq_non_negative": "CHECK ((last_seq >= 0))",
            "ck_workspaces_min_retained_seq_min": "CHECK ((min_retained_seq >= 1))",
            "ck_workspaces_min_retained_seq_within_last_seq": (
                "CHECK (((min_retained_seq - 1) <= last_seq))"
            ),
        },
        "folders": {
            "ck_folders_name_not_blank": "CHECK ((btrim(name) <> ''::text))",
            "ck_folders_rev_positive": "CHECK ((rev > 0))",
            "ck_folders_updated_seq_positive": "CHECK ((updated_seq > 0))",
            "ck_folders_parent_id_not_self": ("CHECK (((parent_id IS NULL) OR (parent_id <> id)))"),
        },
        "notes": {
            "ck_notes_rev_positive": "CHECK ((rev > 0))",
            "ck_notes_updated_seq_positive": "CHECK ((updated_seq > 0))",
        },
        "change_events": {
            "ck_change_events_seq_positive": "CHECK ((seq > 0))",
            "ck_change_events_entity_type_known": (
                "CHECK ((entity_type = ANY (ARRAY['folder'::text, 'note'::text])))"
            ),
            "ck_change_events_op_known": (
                "CHECK ((op = ANY (ARRAY['create'::text, 'update'::text, "
                "'delete'::text, 'move'::text])))"
            ),
            "ck_change_events_rev_positive": "CHECK ((rev > 0))",
        },
        "op_ledger": {
            "ck_op_ledger_request_hash_len": "CHECK ((octet_length(request_hash) = 32))",
            "ck_op_ledger_result_seq_positive": "CHECK ((result_seq > 0))",
        },
    }
    for table, wanted in expected.items():
        constraints = await migration_catalog.constraint_map(table)
        for name, definition in wanted.items():
            assert constraints[name].definition == definition, name
            assert constraints[name].deferrable is False, name


async def test_index_set_is_exact(at_base: MigrationDatabase, migration_catalog: Catalog) -> None:
    """Cursor/tree indexes exist and no index duplicates the events PK."""
    at_base.upgrade("head")

    expected = {
        "users": {"pk_users", "uq_users_email"},
        "workspaces": {"pk_workspaces", "ix_workspaces_owner_id"},
        "folders": {
            "pk_folders",
            "uq_folders_workspace_id_id",
            "ix_folders_workspace_id_updated_seq",
            "ix_folders_workspace_id_parent_id",
        },
        "notes": {
            "pk_notes",
            "ix_notes_workspace_id_updated_seq",
            "ix_notes_workspace_id_folder_id",
        },
        # The composite PK is itself the cursor-scan index: nothing duplicated.
        "change_events": {"pk_change_events", "uq_change_events_workspace_id_op_id"},
        "op_ledger": {"pk_op_ledger", "uq_op_ledger_workspace_id_result_seq"},
    }
    for table, wanted in expected.items():
        found = await migration_catalog.index_names(table)
        assert found == wanted, f"{table}: expected {sorted(wanted)}, found {sorted(found)}"


async def test_column_types_and_defaults(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """``seq``/``updated_seq`` have no default, counters do, body/title are TEXT."""
    at_base.upgrade("head")

    for table in ("folders", "notes"):
        columns = await migration_catalog.column_map(table)
        assert columns["updated_seq"].data_type == "bigint"
        assert columns["updated_seq"].nullable is False
        assert columns["updated_seq"].default is None, f"{table}.updated_seq has a default"
        assert columns["rev"].default == "1"
        assert columns["deleted_at"].nullable is True
        assert columns["updated_at"].default is None, "the service sets updated_at"
        assert columns["updated_at"].nullable is False

    notes = await migration_catalog.column_map("notes")
    assert notes["body_md"].data_type == "text"
    assert notes["body_md"].nullable is False  # empty string legal, NULL is not
    assert notes["folder_id"].nullable is True

    events = await migration_catalog.column_map("change_events")
    assert events["seq"].default is None, "seq is allocated by the application"
    assert {c.data_type for c in events.values() if c.name.endswith("_id")} == {"uuid"}

    workspaces = await migration_catalog.column_map("workspaces")
    assert workspaces["last_seq"].default == "0"
    assert workspaces["min_retained_seq"].default == "1"

    ledger = await migration_catalog.column_map("op_ledger")
    assert ledger["request_hash"].data_type == "bytea"
    assert ledger["result_seq"].default is None


# --------------------------------------------------------------------------- #
# PostgreSQL itself rejects violations (behaviour, not text)
# --------------------------------------------------------------------------- #


async def test_check_constraints_reject_bad_rows(
    at_base: MigrationDatabase, migration_session_factory
) -> None:
    """Each declared CHECK is enforced by the server, not merely present.

    Covers all six tables, including the blank-e-mail guard on ``users``.
    """
    at_base.upgrade("head")
    await seed_owner(migration_session_factory)

    cases = [
        (
            "INSERT INTO folders (id, workspace_id, name, rev, updated_seq, updated_at) "
            "VALUES (:folder, :ws, '   ', 1, 1, now())",
            "ck_folders_name_not_blank",
        ),
        (
            "INSERT INTO folders (id, workspace_id, parent_id, name, rev, updated_seq, "
            "updated_at) VALUES (:folder, :ws, :folder, 'self', 1, 1, now())",
            "ck_folders_parent_id_not_self",
        ),
        (
            "INSERT INTO folders (id, workspace_id, name, rev, updated_seq, updated_at) "
            "VALUES (:folder, :ws, 'zero-seq', 1, 0, now())",
            "ck_folders_updated_seq_positive",
        ),
        (
            "INSERT INTO folders (id, workspace_id, name, rev, updated_seq, updated_at) "
            "VALUES (:folder, :ws, 'zero-rev', 0, 1, now())",
            "ck_folders_rev_positive",
        ),
        (
            "INSERT INTO notes (id, workspace_id, folder_id, title, body_md, rev, "
            "updated_seq, updated_at) VALUES (:entity, :ws, NULL, 't', 'b', 0, 1, now())",
            "ck_notes_rev_positive",
        ),
        (
            "INSERT INTO notes (id, workspace_id, folder_id, title, body_md, rev, "
            "updated_seq, updated_at) VALUES (:entity, :ws, NULL, 't', 'b', 1, 0, now())",
            "ck_notes_updated_seq_positive",
        ),
        (
            "INSERT INTO change_events (workspace_id, seq, entity_type, entity_id, op, rev, "
            "op_id) VALUES (:ws, 0, 'folder', :entity, 'create', 1, :op)",
            "ck_change_events_seq_positive",
        ),
        (
            "INSERT INTO change_events (workspace_id, seq, entity_type, entity_id, op, rev, "
            "op_id) VALUES (:ws, 1, 'attachment', :entity, 'create', 1, :op)",
            "ck_change_events_entity_type_known",
        ),
        (
            "INSERT INTO change_events (workspace_id, seq, entity_type, entity_id, op, rev, "
            "op_id) VALUES (:ws, 1, 'folder', :entity, 'purge', 1, :op)",
            "ck_change_events_op_known",
        ),
        (
            "INSERT INTO change_events (workspace_id, seq, entity_type, entity_id, op, rev, "
            "op_id) VALUES (:ws, 1, 'folder', :entity, 'create', 0, :op)",
            "ck_change_events_rev_positive",
        ),
        # users.email must not be blank. The CHECK is ``btrim(email) <> ''`` and
        # one-argument btrim trims spaces only, so '' and space-only are rejected.
        (
            "INSERT INTO users (id, email) VALUES (:entity, '')",
            "ck_users_email_not_blank",
        ),
        (
            "INSERT INTO users (id, email) VALUES (:entity, '   ')",
            "ck_users_email_not_blank",
        ),
    ]
    for index, (statement, expected) in enumerate(cases):
        params = {
            "ws": WORKSPACE,
            "folder": FOLDER_A,
            "entity": FREE_ENTITY,
            "op": f"018f0000-0000-7000-8000-000000002{index:03d}",
        }
        with pytest.raises(IntegrityError) as error:
            async with migration_session_factory() as session:
                await session.execute(text(statement), params)
                await session.commit()
        assert expected in str(error.value), f"{expected} not enforced: {error.value}"

    workspace_cases = [
        ("0", "0", "ck_workspaces_min_retained_seq_min"),
        ("0", "5", "ck_workspaces_min_retained_seq_within_last_seq"),
    ]
    for index, (last_seq, min_retained, expected) in enumerate(workspace_cases):
        with pytest.raises(IntegrityError) as error:
            async with migration_session_factory() as session:
                await session.execute(
                    text(
                        "INSERT INTO workspaces (id, owner_id, last_seq, min_retained_seq) "
                        "VALUES (:id, :owner, :last_seq, :min_retained)"
                    ),
                    {
                        "id": f"018f0000-0000-7000-8000-0000000030{index:02d}",
                        "owner": USER,
                        "last_seq": int(last_seq),
                        "min_retained": int(min_retained),
                    },
                )
                await session.commit()
        assert expected in str(error.value), f"{expected} not enforced: {error.value}"


async def test_unique_constraints_reject_duplicates(
    at_base: MigrationDatabase, migration_session_factory
) -> None:
    """The idempotency dams are real: duplicate ``(ws, op_id)``/``(ws, seq)`` die."""
    at_base.upgrade("head")
    await seed_owner(migration_session_factory, with_event=True)

    event_sql = (
        "INSERT INTO change_events (workspace_id, seq, entity_type, entity_id, op, rev, op_id) "
        "VALUES (:ws, :seq, 'folder', :entity, 'create', 1, :op)"
    )
    with pytest.raises(IntegrityError) as error:
        async with migration_session_factory() as session:
            await session.execute(
                text(event_sql), {"ws": WORKSPACE, "seq": 2, "entity": FREE_ENTITY, "op": OP}
            )
            await session.commit()
    assert "uq_change_events_workspace_id_op_id" in str(error.value)

    with pytest.raises(IntegrityError) as error:
        async with migration_session_factory() as session:
            await session.execute(
                text(event_sql),
                {
                    "ws": WORKSPACE,
                    "seq": 1,
                    "entity": FREE_ENTITY,
                    "op": "018f0000-0000-7000-8000-000000009001",
                },
            )
            await session.commit()
    assert "pk_change_events" in str(error.value)

    ledger_sql = (
        "INSERT INTO op_ledger (workspace_id, op_id, request_hash, result_seq) "
        "VALUES (:ws, :op, :hash, 1)"
    )
    async with migration_session_factory() as session:
        await session.execute(
            text(ledger_sql),
            {"ws": WORKSPACE, "op": "018f0000-0000-7000-8000-000000009002", "hash": GOOD_HASH},
        )
        await session.commit()

    with pytest.raises(IntegrityError) as error:
        async with migration_session_factory() as session:
            await session.execute(
                text(ledger_sql),
                {
                    "ws": WORKSPACE,
                    "op": "018f0000-0000-7000-8000-000000009003",
                    "hash": GOOD_HASH,
                },
            )
            await session.commit()
    assert "uq_op_ledger_workspace_id_result_seq" in str(error.value)


async def test_request_hash_length_is_enforced(
    at_base: MigrationDatabase, migration_session_factory
) -> None:
    """``octet_length(request_hash) = 32`` rejects 31/33 bytes and accepts 32."""
    at_base.upgrade("head")
    await seed_owner(migration_session_factory, with_event=True)

    statement = (
        "INSERT INTO op_ledger (workspace_id, op_id, request_hash, result_seq) "
        "VALUES (:ws, :op, :hash, 1)"
    )
    for index, digest in enumerate((BAD_HASH, b"\x2a" * 33), start=1):
        with pytest.raises(IntegrityError) as error:
            async with migration_session_factory() as session:
                await session.execute(
                    text(statement),
                    {
                        "ws": WORKSPACE,
                        "op": f"018f0000-0000-7000-8000-00000000910{index}",
                        "hash": digest,
                    },
                )
                await session.commit()
        assert "ck_op_ledger_request_hash_len" in str(error.value)

    async with migration_session_factory() as session:
        await session.execute(
            text(statement),
            {"ws": WORKSPACE, "op": "018f0000-0000-7000-8000-000000009199", "hash": GOOD_HASH},
        )
        await session.commit()


async def test_foreign_keys_enforce_workspace_isolation(
    at_base: MigrationDatabase, migration_session_factory
) -> None:
    """Cross-workspace references are impossible: the composite FKs reject them."""
    at_base.upgrade("head")
    await seed_owner(migration_session_factory, with_event=True, extra_workspace=True)

    cases = [
        (
            "INSERT INTO folders (id, workspace_id, parent_id, name, rev, updated_seq, "
            "updated_at) VALUES (:folder, :ws_b, :folder_a, 'cross', 1, 1, now())",
            "fk_folders_workspace_id_parent_id_folders",
        ),
        (
            "INSERT INTO notes (id, workspace_id, folder_id, title, body_md, rev, "
            "updated_seq, updated_at) VALUES (:entity, :ws_b, :folder_a, 't', 'b', 1, 1, now())",
            "fk_notes_workspace_id_folder_id_folders",
        ),
        (
            "INSERT INTO workspaces (id, owner_id) VALUES (:ghost_ws, :missing_user)",
            "fk_workspaces_owner_id_users",
        ),
        (
            "INSERT INTO change_events (workspace_id, seq, entity_type, entity_id, op, rev, "
            "op_id) VALUES (:ghost_ws, 1, 'folder', :entity, 'create', 1, :op)",
            "fk_change_events_workspace_id_workspaces",
        ),
        (
            "INSERT INTO op_ledger (workspace_id, op_id, request_hash, result_seq) "
            "VALUES (:ws, :op, :hash, 999)",
            "fk_op_ledger_workspace_id_result_seq_change_events",
        ),
    ]
    params = {
        "ws": WORKSPACE,
        "ws_b": WORKSPACE_B,
        "folder_a": FOLDER_A,
        "folder": "018f0000-0000-7000-8000-000000009201",
        "entity": FREE_ENTITY,
        "op": "018f0000-0000-7000-8000-000000009202",
        "hash": GOOD_HASH,
        "missing_user": MISSING_USER,
        "ghost_ws": "018f0000-0000-7000-8000-000000009203",
    }
    for statement, expected in cases:
        with pytest.raises(IntegrityError) as error:
            async with migration_session_factory() as session:
                await session.execute(text(statement), params)
                await session.commit()
        assert expected in str(error.value), f"{expected} not enforced: {error.value}"


async def test_restrict_blocks_deleting_an_owner(
    at_base: MigrationDatabase, migration_session_factory
) -> None:
    """``ON DELETE RESTRICT`` is real: an owner with a workspace cannot be deleted."""
    at_base.upgrade("head")
    await seed_owner(migration_session_factory, extra_workspace=True)

    with pytest.raises(IntegrityError) as error:
        async with migration_session_factory() as session:
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": USER})
            await session.commit()
    assert "fk_workspaces_owner_id_users" in str(error.value)

    async with migration_session_factory() as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM workspaces WHERE owner_id = :id"), {"id": USER}
            )
        ).scalar_one()
    assert remaining == 2


async def test_email_not_blank_guard_rejects_empty_and_space_only(
    at_base: MigrationDatabase, migration_session_factory
) -> None:
    """``ck_users_email_not_blank`` rejects '' and space-only, and stores values verbatim.

    The CHECK is ``btrim(email) <> ''``; one-argument ``btrim`` trims **spaces
    only**, so a tab/newline-only value still passes. That reach is pinned here
    (observed against PostgreSQL) instead of being assumed, and it matches the
    plan's "no e-mail normalisation, plain TEXT uniqueness" rule: the database
    stores exactly what the caller sent.
    """
    at_base.upgrade("head")

    async with migration_session_factory() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, 'Stored.As-Is@Revnote.Test')"),
            {"id": USER},
        )
        await session.commit()

    async with migration_session_factory() as session:
        stored = (
            await session.execute(text("SELECT email FROM users WHERE id = :id"), {"id": USER})
        ).scalar_one()
    assert stored == "Stored.As-Is@Revnote.Test", "no case folding or trimming may be applied"

    # Accepted by the CHECK: whitespace that btrim(' ') does not remove.
    async with migration_session_factory() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, E'\\t')"),
            {"id": "018f0000-0000-7000-8000-0000000000e1"},
        )
        await session.commit()

    # Uniqueness is plain TEXT equality: a case variant is a different row.
    async with migration_session_factory() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, 'stored.as-is@revnote.test')"),
            {"id": "018f0000-0000-7000-8000-0000000000e2"},
        )
        await session.commit()


# --------------------------------------------------------------------------- #
# the deferred FK is enforced by the database, at COMMIT
# --------------------------------------------------------------------------- #


async def test_deferred_fk_is_validated_at_commit(
    at_base: MigrationDatabase, migration_session_factory
) -> None:
    """A folder may name a not-yet-existing event seq; COMMIT then rejects it."""
    at_base.upgrade("head")
    await seed_owner(migration_session_factory)

    async with migration_session_factory() as session:
        # The event with seq = 1 does not exist. Because the FK is deferred this
        # INSERT is accepted inside the transaction (kernel order: object, event).
        await session.execute(
            text(
                "INSERT INTO folders (id, workspace_id, parent_id, name, rev, deleted_at, "
                "updated_seq, created_at, updated_at) "
                "VALUES (:id, :ws, NULL, 'deferred', 1, NULL, 1, now(), now())"
            ),
            {"id": FOLDER_A, "ws": WORKSPACE},
        )
        with pytest.raises(IntegrityError) as error:
            await session.commit()

    assert "fk_folders_workspace_id_updated_seq_change_events" in str(error.value)


async def test_deferred_fk_passes_once_the_event_exists(
    at_base: MigrationDatabase, migration_session_factory, migration_catalog: Catalog
) -> None:
    """Object-then-event in one transaction commits, which is the kernel's ordering."""
    at_base.upgrade("head")

    async with migration_session_factory() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, 'deferred@revnote.test')"),
            {"id": USER},
        )
        await session.execute(
            text("INSERT INTO workspaces (id, owner_id, last_seq) VALUES (:id, :owner, 1)"),
            {"id": WORKSPACE, "owner": USER},
        )
        await session.execute(
            text(
                "INSERT INTO folders (id, workspace_id, parent_id, name, rev, deleted_at, "
                "updated_seq, created_at, updated_at) "
                "VALUES (:id, :ws, NULL, 'deferred', 1, NULL, 1, now(), now())"
            ),
            {"id": FOLDER_A, "ws": WORKSPACE},
        )
        await session.execute(
            text(
                "INSERT INTO change_events (workspace_id, seq, entity_type, entity_id, op, "
                "rev, op_id) VALUES (:ws, 1, 'folder', :entity, 'create', 1, :op)"
            ),
            {"ws": WORKSPACE, "entity": FOLDER_A, "op": OP},
        )
        await session.commit()

    async with migration_session_factory() as session:
        folder_seq = (
            await session.execute(
                text("SELECT updated_seq FROM folders WHERE id = :id"), {"id": FOLDER_A}
            )
        ).scalar_one()
    assert folder_seq == 1
    assert await migration_catalog.sequences() == []


@pytest.fixture(scope="module", autouse=True)
def _reset_migration_database(alembic_migrations: MigrationDatabase):
    """Return the migration database to ``base`` when the module finishes.

    The round trip is destructive by design, so it must not leave the dedicated
    database half-migrated. A teardown failure is reported, never swallowed: the
    ``downgrade`` result is asserted instead of being wrapped in a suppress.
    """
    yield
    alembic_migrations.downgrade("base")  # raises AlembicError if it cannot run
    assert alembic_migrations.cli.run(alembic_migrations.url, "current").returncode == 0
