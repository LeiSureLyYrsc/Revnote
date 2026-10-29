"""Alembic round trips and real-catalog schema proof (migration database only).

Everything here runs against ``TEST_MIGRATION_DATABASE_URL``, the disposable
database that may be destroyed. The business database is never downgraded:
:func:`run_alembic` refuses it and ``test_downgrade_refused_for_business_db``
proves the guard fires before a subprocess starts.

Round trip exercised: ``base -> baseline -> 0002 -> head -> 0002 -> head ->
base``. Assertions read ``pg_catalog`` / ``information_schema`` and trigger real
constraint violations, so a revision that documents one thing and creates another
fails here. Phase 3 adds data-survival coverage: notes/events seeded at 0002 must
still be intact after 0003, and legacy ``users`` rows must come out with
``password_hash IS NULL``.
"""

from __future__ import annotations

import uuid

import pytest
from pg_migrations import AlembicError, AlembicGuardError, MigrationDatabase
from pg_testing import (
    AUTH_REVISION,
    AUTH_TABLES,
    BASELINE_REVISION,
    DOMAIN_TABLES,
    HEAD_REVISION,
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

SESSION = "018f0000-0000-7000-8000-0000000000a1"
ACCESS_TOKEN = "018f0000-0000-7000-8000-0000000000b1"
REFRESH_TOKEN = "018f0000-0000-7000-8000-0000000000c1"

GOOD_HASH = b"\x2a" * 32
BAD_HASH = b"\x2a" * 31
GOOD_DIGEST = b"\x5c" * 32
BAD_DIGEST = b"\x5c" * 33


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
    """One head: the auth revision sits on the sync-kernel revision."""
    assert alembic_heads == (AUTH_REVISION,)
    assert head_revision == AUTH_REVISION == HEAD_REVISION


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


async def test_head_creates_exactly_the_domain_tables_plus_version(
    at_base: MigrationDatabase, migration_catalog: Catalog, head_revision: str
) -> None:
    """``upgrade head`` -> ten domain tables + ``alembic_version``, nothing else."""
    at_base.upgrade("head")

    assert await migration_catalog.tables() == list(HEAD_TABLES)
    assert await migration_catalog.version() == head_revision
    assert await migration_catalog.table_set() == {*DOMAIN_TABLES, VERSION_TABLE}


async def test_kernel_revision_still_creates_only_the_six_kernel_tables(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """``upgrade 0002`` -> exactly the Phase 2 six, so 0003 is the only auth DDL."""
    at_base.upgrade(KERNEL_REVISION)

    assert await migration_catalog.tables() == sorted((*KERNEL_TABLES, VERSION_TABLE))
    assert await migration_catalog.version() == KERNEL_REVISION
    assert not await migration_catalog.table_set() & set(AUTH_TABLES)


async def test_downgrade_to_baseline_removes_all_business_tables(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """``head`` -> baseline drops every business table (schema reversible)."""
    at_base.upgrade("head")
    assert await migration_catalog.table_set() == {*DOMAIN_TABLES, VERSION_TABLE}

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


async def test_auth_revision_downgrade_restores_the_kernel_schema(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """``head -> 0002`` is a clean step back: auth gone, kernel and data intact."""
    at_base.upgrade("head")
    at_base.downgrade(KERNEL_REVISION)

    assert await migration_catalog.version() == KERNEL_REVISION
    assert await migration_catalog.table_set() == {*KERNEL_TABLES, VERSION_TABLE}
    # The users extension is undone, including the functional index.
    columns = await migration_catalog.column_map("users")
    assert "password_hash" not in columns and "disabled_at" not in columns
    assert "uq_users_email" in await migration_catalog.index_names("users")
    assert "uq_users_email_normalized" not in await migration_catalog.index_names("users")


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
    """PK/UNIQUE definitions are exactly the approved ones (single and composite).

    ``users`` no longer has ``uq_users_email``: Phase 3 replaces it with the
    functional index ``uq_users_email_normalized`` (asserted with the index set).
    """
    at_base.upgrade("head")

    expected = {
        "pk_users": ("users", "PRIMARY KEY (id)"),
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
        "pk_auth_sessions": ("auth_sessions", "PRIMARY KEY (id)"),
        "pk_auth_access_tokens": ("auth_access_tokens", "PRIMARY KEY (id)"),
        "uq_auth_access_tokens_token_hash": (
            "auth_access_tokens",
            "UNIQUE (token_hash)",
        ),
        "pk_auth_refresh_tokens": ("auth_refresh_tokens", "PRIMARY KEY (id)"),
        "uq_auth_refresh_tokens_token_hash": (
            "auth_refresh_tokens",
            "UNIQUE (token_hash)",
        ),
        "pk_workspace_members": (
            "workspace_members",
            "PRIMARY KEY (workspace_id, user_id)",
        ),
    }
    for name, (table, definition) in expected.items():
        constraints = await migration_catalog.constraint_map(table)
        assert constraints[name].definition == definition, name
        assert constraints[name].kind in ("primary key", "unique"), name

    # The legacy constraint is gone; uniqueness now lives in the expression index.
    assert "uq_users_email" not in await migration_catalog.constraint_map("users")


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
    """All 19 contract CHECKs exist under their exact names, and nothing else.

    The set is exact per table - no missing CHECK, no extra unnamed CHECK, and no
    drift between the models, the revisions and the live catalog. ``users``
    carries ``ck_users_email_not_blank``, which rejects '' and space-only e-mails
    (plain ``btrim(email)`` trims spaces only, so tabs/newlines still pass - see
    the behavioural test for the exact reach of the guard).
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
        "auth_sessions": set(),
        "auth_access_tokens": {"ck_auth_access_tokens_token_hash_len"},
        "auth_refresh_tokens": {"ck_auth_refresh_tokens_token_hash_len"},
        "workspace_members": {"ck_workspace_members_role_owner_only"},
    }
    total = 0
    for table, wanted in expected.items():
        found = {c.name for c in await migration_catalog.constraints(table) if c.kind == "check"}
        assert found == wanted, f"{table}: expected {sorted(wanted)}, found {sorted(found)}"
        total += len(wanted)
    assert total == 19


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
    """Cursor/tree/auth indexes exist and no index duplicates the events PK."""
    at_base.upgrade("head")

    expected = {
        # Uniqueness moved into a functional index when uq_users_email was dropped.
        "users": {"pk_users", "uq_users_email_normalized"},
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
        "auth_sessions": {"pk_auth_sessions", "ix_auth_sessions_user_id"},
        "auth_access_tokens": {
            "pk_auth_access_tokens",
            "uq_auth_access_tokens_token_hash",
            "ix_auth_access_tokens_session_id",
        },
        "auth_refresh_tokens": {
            "pk_auth_refresh_tokens",
            "uq_auth_refresh_tokens_token_hash",
            "ix_auth_refresh_tokens_session_id",
        },
        # The (workspace_id, user_id) PK covers the forward lookup.
        "workspace_members": {"pk_workspace_members", "ix_workspace_members_user_id"},
    }
    for table, wanted in expected.items():
        found = await migration_catalog.index_names(table)
        assert found == wanted, f"{table}: expected {sorted(wanted)}, found {sorted(found)}"


async def test_normalised_email_index_is_a_unique_expression_index(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """D8: ``lower(btrim(email))`` is the uniqueness key, and it really is an index."""
    at_base.upgrade("head")

    indexes = {index.name: index for index in await migration_catalog.indexes("users")}
    index = indexes["uq_users_email_normalized"]

    assert index.unique is True
    assert index.primary is False
    # pg_get_indexdef renders schema-qualified, so the schema name is not pinned;
    # the expression, the uniqueness and the index name are.
    assert index.definition.startswith("CREATE UNIQUE INDEX uq_users_email_normalized ON ")
    assert index.definition.endswith(" USING btree (lower(btrim(email)))")


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

    Covers the kernel tables (Phase 2) and the Phase 3 additions: the token-digest
    length checks and the owner-only role check.
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

    # Phase 3: 32-byte digests only, and v1 accepts no role but ``owner``.
    session_sql = (
        "INSERT INTO auth_sessions (id, user_id, expires_at) "
        "VALUES (:session, :owner, now() + interval '30 days')"
    )
    async with migration_session_factory() as writer:
        await writer.execute(text(session_sql), {"session": SESSION, "owner": USER})
        await writer.commit()

    auth_cases = [
        (
            "INSERT INTO auth_access_tokens (id, session_id, token_hash, expires_at) "
            "VALUES (:token, :session, :digest, now() + interval '10 minutes')",
            "ck_auth_access_tokens_token_hash_len",
        ),
        (
            "INSERT INTO auth_refresh_tokens (id, session_id, token_hash, expires_at) "
            "VALUES (:token, :session, :digest, now() + interval '30 days')",
            "ck_auth_refresh_tokens_token_hash_len",
        ),
        (
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            "VALUES (:ws, :owner, 'editor')",
            "ck_workspace_members_role_owner_only",
        ),
    ]
    for statement, expected in auth_cases:
        with pytest.raises(IntegrityError) as error:
            async with migration_session_factory() as session:
                await session.execute(
                    text(statement),
                    {
                        "token": ACCESS_TOKEN,
                        "session": SESSION,
                        "digest": BAD_DIGEST,
                        "ws": WORKSPACE,
                        "owner": USER,
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
    """``ON DELETE RESTRICT`` is real: an account still referenced cannot be deleted.

    After 0003 a user is referenced by ``workspaces.owner_id`` **and** by the
    backfilled ``workspace_members`` row; both are RESTRICT, so the delete fails
    whichever constraint PostgreSQL reports first. The data-survival assertions
    are the actual contract.
    """
    at_base.upgrade("head")
    await seed_owner(migration_session_factory, extra_workspace=True)
    async with migration_session_factory() as session:
        await session.execute(
            text(
                "INSERT INTO workspace_members (workspace_id, user_id, role) "
                "VALUES (:ws, :user, 'owner')"
            ),
            {"ws": WORKSPACE, "user": USER},
        )
        await session.commit()

    with pytest.raises(IntegrityError) as error:
        async with migration_session_factory() as session:
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": USER})
            await session.commit()
    message = str(error.value)
    assert "fk_workspaces_owner_id_users" in message or (
        "fk_workspace_members_user_id_users" in message
    ), message

    async with migration_session_factory() as session:
        remaining = (
            await session.execute(
                text("SELECT count(*) FROM workspaces WHERE owner_id = :id"), {"id": USER}
            )
        ).scalar_one()
        members = (
            await session.execute(
                text("SELECT count(*) FROM workspace_members WHERE user_id = :id"), {"id": USER}
            )
        ).scalar_one()
    assert remaining == 2
    assert members == 1


async def test_email_check_stores_values_verbatim_but_normalises_uniqueness(
    at_base: MigrationDatabase, migration_session_factory
) -> None:
    """``ck_users_email_not_blank`` gates blanks; uniqueness is the normalised key.

    Two separate promises are pinned here (D8):

    * the **stored value is never rewritten** — the database returns exactly what
      the caller sent (no trigger, no ``citext``, no application-level trim);
    * **uniqueness is case/space insensitive** through
      ``uq_users_email_normalized`` — ``lower(btrim(email))`` collides, so one
      person cannot register twice with a differently-cased address.

    The CHECK is ``btrim(email) <> ''``; one-argument ``btrim`` trims **spaces
    only**, so a tab-only value still passes it. That reach is pinned here
    (observed against PostgreSQL) instead of being assumed.
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

    # A case variant is the *same* account now: the functional index rejects it.
    for index, variant in enumerate(("stored.as-is@revnote.test", "  STORED.AS-IS@revnote.TEST ")):
        with pytest.raises(IntegrityError) as error:
            async with migration_session_factory() as session:
                await session.execute(
                    text("INSERT INTO users (id, email) VALUES (:id, :email)"),
                    {
                        "id": f"018f0000-0000-7000-8000-0000000000e{index + 2}",
                        "email": variant,
                    },
                )
                await session.commit()
        assert "uq_users_email_normalized" in str(error.value), variant

    # A genuinely different address is still accepted.
    async with migration_session_factory() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, 'other@revnote.test')"),
            {"id": "018f0000-0000-7000-8000-0000000000ef"},
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


# --------------------------------------------------------------------------- #
# Phase 3: 0002 -> 0003 data survival, backfill and refusal
# --------------------------------------------------------------------------- #

KERNEL_SEED = (
    "INSERT INTO users (id, email) VALUES (:user, 'legacy@revnote.test')",
    "INSERT INTO workspaces (id, owner_id, last_seq) VALUES (:ws, :user, 1)",
    (
        "INSERT INTO folders (id, workspace_id, parent_id, name, rev, deleted_at, "
        "updated_seq, created_at, updated_at) "
        "VALUES (:folder, :ws, NULL, 'Legacy folder', 1, NULL, 1, now(), now())"
    ),
    (
        "INSERT INTO notes (id, workspace_id, folder_id, title, body_md, rev, deleted_at, "
        "updated_seq, created_at, updated_at) "
        "VALUES (:note, :ws, :folder, 'Legacy note', '# body survives', 1, NULL, 1, "
        "now(), now())"
    ),
    (
        "INSERT INTO change_events (workspace_id, seq, entity_type, entity_id, op, rev, op_id) "
        "VALUES (:ws, 1, 'note', :note, 'create', 1, :op)"
    ),
    (
        "INSERT INTO op_ledger (workspace_id, op_id, request_hash, result_seq) "
        "VALUES (:ws, :op, :hash, 1)"
    ),
)


async def seed_kernel_rows(session_factory, *, workspace: str = WORKSPACE) -> None:
    """Seed one complete kernel history at whatever revision is current.

    Written as raw SQL with only Phase 2 columns, so the same helper works at
    ``0002`` (before the auth columns exist) and at ``head``.
    """
    params = {
        "user": USER,
        "ws": workspace,
        "folder": FOLDER_A,
        "note": FREE_ENTITY,
        "op": OP,
        "hash": GOOD_HASH,
    }
    async with session_factory() as session:
        for statement in KERNEL_SEED:
            await session.execute(text(statement), params)
        await session.commit()


async def test_upgrade_0003_preserves_existing_notes_and_events(
    at_base: MigrationDatabase, migration_session_factory, migration_catalog: Catalog
) -> None:
    """Plan §9.2: 0003 must not rewrite folders/notes/change_events/op_ledger."""
    at_base.upgrade(KERNEL_REVISION)
    await seed_kernel_rows(migration_session_factory)

    at_base.upgrade("head")

    async with migration_session_factory() as session:
        note = (
            await session.execute(
                text(
                    "SELECT title, body_md, rev, updated_seq, folder_id FROM notes WHERE id = :id"
                ),
                {"id": FREE_ENTITY},
            )
        ).one()
        event_count = (
            await session.execute(text("SELECT count(*) FROM change_events"))
        ).scalar_one()
        ledger = (
            await session.execute(
                text("SELECT result_seq, request_hash FROM op_ledger WHERE op_id = :op"),
                {"op": OP},
            )
        ).one()
        last_seq = (
            await session.execute(
                text("SELECT last_seq FROM workspaces WHERE id = :id"), {"id": WORKSPACE}
            )
        ).scalar_one()

    assert (note.title, note.body_md) == ("Legacy note", "# body survives")
    assert (note.rev, note.updated_seq) == (1, 1)
    assert note.folder_id == uuid.UUID(FOLDER_A)
    assert event_count == 1
    assert ledger.result_seq == 1 and bytes(ledger.request_hash) == GOOD_HASH
    assert last_seq == 1, "the cursor must not be advanced or reset by the migration"


async def test_upgrade_0003_backfills_one_owner_membership_per_workspace(
    at_base: MigrationDatabase, migration_session_factory, migration_catalog: Catalog
) -> None:
    """D5: ``workspaces.owner_id`` becomes a real membership row."""
    at_base.upgrade(KERNEL_REVISION)
    await seed_kernel_rows(migration_session_factory)

    at_base.upgrade("head")

    async with migration_session_factory() as session:
        rows = (
            await session.execute(text("SELECT workspace_id, user_id, role FROM workspace_members"))
        ).all()

    assert [(str(row.workspace_id), str(row.user_id), row.role) for row in rows] == [
        (WORKSPACE, USER, "owner")
    ]


async def test_upgrade_0003_keeps_the_legacy_password_hash_null(
    at_base: MigrationDatabase, migration_session_factory, migration_catalog: Catalog
) -> None:
    """D8: a pre-Phase-3 account has no local credential and cannot be invented."""
    at_base.upgrade(KERNEL_REVISION)
    await seed_kernel_rows(migration_session_factory)

    at_base.upgrade("head")

    async with migration_session_factory() as session:
        row = (
            await session.execute(
                text("SELECT password_hash, disabled_at, email FROM users WHERE id = :id"),
                {"id": USER},
            )
        ).one()

    assert row.password_hash is None
    assert row.disabled_at is None
    assert row.email == "legacy@revnote.test", "the address must stay exactly as stored"


async def test_upgrade_0003_refuses_to_merge_normalised_email_collisions(
    at_base: MigrationDatabase, migration_session_factory, migration_catalog: Catalog
) -> None:
    """A collision is a human decision: the upgrade aborts and leaves 0002 intact."""
    at_base.upgrade(KERNEL_REVISION)
    async with migration_session_factory() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, 'Same.Person@revnote.test')"),
            {"id": USER},
        )
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, '  same.person@REVNOTE.test ')"),
            {"id": MISSING_USER},
        )
        await session.commit()

    with pytest.raises(AlembicError, match="collision") as error:
        at_base.upgrade("head")
    # The fixed message is all we assert: it must not name the address, an id or
    # a count. ``AlembicError`` embeds stdout/stderr in its text; it has no
    # ``.stdout`` attribute of its own.
    assert "revnote.test" not in str(error.value)
    assert "same.person" not in str(error.value)

    # Nothing was applied: still 0002, no auth tables, both accounts preserved.
    assert await migration_catalog.version() == KERNEL_REVISION
    assert not await migration_catalog.table_set() & set(AUTH_TABLES)
    async with migration_session_factory() as session:
        remaining = (await session.execute(text("SELECT count(*) FROM users"))).scalar_one()
    assert remaining == 2, "a failed upgrade must not delete an account"


async def test_downgrade_0003_preserves_kernel_data_and_email_uniqueness(
    at_base: MigrationDatabase, migration_session_factory, migration_catalog: Catalog
) -> None:
    """``head -> 0002`` restores the Phase 2 schema without touching user content."""
    at_base.upgrade("head")
    await seed_kernel_rows(migration_session_factory)

    at_base.downgrade(KERNEL_REVISION)

    async with migration_session_factory() as session:
        note = (
            await session.execute(
                text("SELECT title, body_md FROM notes WHERE id = :id"), {"id": FREE_ENTITY}
            )
        ).one()
        event_count = (
            await session.execute(text("SELECT count(*) FROM change_events"))
        ).scalar_one()
        user = (
            await session.execute(text("SELECT email FROM users WHERE id = :id"), {"id": USER})
        ).one()

    assert (note.title, note.body_md) == ("Legacy note", "# body survives")
    assert event_count == 1
    assert user.email == "legacy@revnote.test"

    # The old constraint is back and enforced again.
    with pytest.raises(IntegrityError) as error:
        async with migration_session_factory() as session:
            await session.execute(
                text("INSERT INTO users (id, email) VALUES (:id, 'legacy@revnote.test')"),
                {"id": "018f0000-0000-7000-8000-0000000000f1"},
            )
            await session.commit()
    assert "uq_users_email" in str(error.value)


async def test_token_digests_are_unique_and_rotation_parent_is_optional(
    at_base: MigrationDatabase, migration_session_factory
) -> None:
    """Same digest twice is a storage bug; a first refresh token has no parent."""
    at_base.upgrade("head")
    async with migration_session_factory() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, 'tokens@revnote.test')"),
            {"id": USER},
        )
        await session.execute(
            text(
                "INSERT INTO auth_sessions (id, user_id, expires_at) "
                "VALUES (:session, :user, now() + interval '30 days')"
            ),
            {"session": SESSION, "user": USER},
        )
        await session.execute(
            text(
                "INSERT INTO auth_refresh_tokens (id, session_id, token_hash, expires_at) "
                "VALUES (:token, :session, :digest, now() + interval '30 days')"
            ),
            {"token": REFRESH_TOKEN, "session": SESSION, "digest": GOOD_DIGEST},
        )
        await session.commit()

    with pytest.raises(IntegrityError) as error:
        async with migration_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO auth_refresh_tokens (id, session_id, token_hash, parent_id, "
                    "expires_at) VALUES (:token, :session, :digest, :parent, "
                    "now() + interval '30 days')"
                ),
                {
                    "token": "018f0000-0000-7000-8000-0000000000c2",
                    "session": SESSION,
                    "digest": GOOD_DIGEST,
                    "parent": REFRESH_TOKEN,
                },
            )
            await session.commit()
    assert "uq_auth_refresh_tokens_token_hash" in str(error.value)

    # The parent FK is real, and the chain accepts a genuine rotation.
    with pytest.raises(IntegrityError) as error:
        async with migration_session_factory() as session:
            await session.execute(
                text(
                    "INSERT INTO auth_refresh_tokens (id, session_id, token_hash, parent_id, "
                    "expires_at) VALUES (:token, :session, :digest, :parent, "
                    "now() + interval '30 days')"
                ),
                {
                    "token": "018f0000-0000-7000-8000-0000000000c3",
                    "session": SESSION,
                    "digest": b"\x77" * 32,
                    "parent": "018f0000-0000-7000-8000-0000000000dd",
                },
            )
            await session.commit()
    assert "fk_auth_refresh_tokens_parent_id_auth_refresh_tokens" in str(error.value)


async def test_workspace_members_rejects_duplicate_and_second_role(
    at_base: MigrationDatabase, migration_session_factory
) -> None:
    """The PK makes membership unique; the CHECK keeps v1 owner-only (D5)."""
    at_base.upgrade("head")
    await seed_owner(migration_session_factory)

    insert = (
        "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES (:ws, :user, :role)"
    )
    async with migration_session_factory() as session:
        await session.execute(text(insert), {"ws": WORKSPACE, "user": USER, "role": "owner"})
        await session.commit()

    with pytest.raises(IntegrityError) as error:
        async with migration_session_factory() as session:
            await session.execute(text(insert), {"ws": WORKSPACE, "user": USER, "role": "owner"})
            await session.commit()
    assert "pk_workspace_members" in str(error.value)

    # A *distinct* existing user, so the only thing left to reject is the role:
    # an unknown user would fail the FK first and prove nothing about the CHECK.
    # A dedicated id keeps ``MISSING_USER`` meaning what its name says elsewhere.
    second_user = "018f0000-0000-7000-8000-0000000000d2"
    async with migration_session_factory() as session:
        await session.execute(
            text("INSERT INTO users (id, email) VALUES (:id, 'member2@revnote.test')"),
            {"id": second_user},
        )
        await session.commit()

    with pytest.raises(IntegrityError) as error:
        async with migration_session_factory() as session:
            await session.execute(
                text(insert),
                {"ws": WORKSPACE, "user": second_user, "role": "editor"},
            )
            await session.commit()
    assert "ck_workspace_members_role_owner_only" in str(error.value)


async def test_auth_columns_and_nullability_at_head(
    at_base: MigrationDatabase, migration_catalog: Catalog
) -> None:
    """The Phase 3 columns exist with the exact types and NULLability promised."""
    at_base.upgrade("head")

    users = await migration_catalog.column_map("users")
    assert users["password_hash"].data_type == "text"
    assert users["password_hash"].nullable is True
    assert users["disabled_at"].data_type == "timestamp with time zone"
    assert users["disabled_at"].nullable is True

    sessions = await migration_catalog.column_map("auth_sessions")
    assert sessions["expires_at"].nullable is False
    assert sessions["expires_at"].default is None
    assert sessions["revoked_at"].nullable is True
    assert sessions["revoke_reason"].data_type == "text"

    refresh = await migration_catalog.column_map("auth_refresh_tokens")
    assert refresh["parent_id"].nullable is True
    assert refresh["used_at"].nullable is True
    assert refresh["token_hash"].data_type == "bytea"

    members = await migration_catalog.column_map("workspace_members")
    assert members["role"].nullable is False
    assert members["role"].data_type == "text"


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
