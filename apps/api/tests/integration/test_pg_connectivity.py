"""Real PostgreSQL connectivity for the *business* database.

Upgrade/downgrade coverage used to live here; it belongs to
:mod:`test_migrations`, which owns the disposable migration database. What stays
is genuinely about connecting, plus the schema promise the harness must deliver:

* the engine talks to the database named by ``TEST_DATABASE_URL``;
* ``NullPool`` opens a fresh server connection per checkout, sessions roll back,
  and an uncommitted write is invisible to a second connection;
* the business schema came from the real Alembic chain (single head stamped,
  exactly the ten contract tables, no sequences);
* nothing here downgrades, and the guard that makes that impossible is proven.
"""

from __future__ import annotations

import uuid

import pytest
from pg_migrations import AlembicGuardError
from pg_testing import (
    DOMAIN_TABLES,
    HEAD_TABLES,
    KERNEL_TABLES,
    TEST_DATABASE_URL_ENV,
    TEST_MIGRATION_DATABASE_URL_ENV,
    VERSION_TABLE,
    Catalog,
)
from sqlalchemy import text
from sqlalchemy.engine import make_url

pytestmark = [pytest.mark.anyio, pytest.mark.integration]


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #


async def test_dsn_addresses_the_business_database(
    normal_database_url: str, catalog: Catalog, postgres_targets
) -> None:
    """The engine connects to the database named by ``TEST_DATABASE_URL``."""
    assert await catalog.current_database() == make_url(normal_database_url).database
    assert postgres_targets.business_identity != postgres_targets.migration_identity


async def test_select_1_and_server_version(pg_engine) -> None:
    """``SELECT 1`` and ``version()`` succeed against the real server."""
    async with pg_engine.connect() as connection:
        assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
        version = (await connection.execute(text("SELECT version()"))).scalar_one()
    assert "PostgreSQL" in version


async def test_null_pool_opens_a_new_connection_per_checkout(pg_engine) -> None:
    """``NullPool`` never recycles a connection, so tasks cannot accidentally share one.

    The backend pid is the server's own identity for a connection, so comparing pids
    is evidence rather than a claim about the pool implementation.
    """
    async with pg_engine.connect() as first:
        pid_one = (await first.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    async with pg_engine.connect() as second:
        pid_two = (await second.execute(text("SELECT pg_backend_pid()"))).scalar_one()
    assert pid_one != pid_two


async def test_session_round_trip_and_explicit_rollback(session_factory) -> None:
    """Sessions work, and an explicit rollback leaves no state behind."""
    async with session_factory() as session:
        assert (await session.execute(text("SELECT 42"))).scalar_one() == 42

    async with session_factory() as session:
        await session.execute(text("SELECT 1"))
        await session.rollback()
        assert (await session.execute(text("SELECT 2"))).scalar_one() == 2


async def test_uncommitted_write_is_invisible_to_another_connection(
    session_factory, new_session_factory
) -> None:
    """A write becomes visible only after COMMIT (real isolation, two connections)."""
    user_id = uuid.uuid4()
    statement = text("SELECT count(*) FROM users WHERE id = :id")

    async with session_factory() as writer:
        await writer.execute(
            text("INSERT INTO users (id, email) VALUES (:id, :email)"),
            {"id": user_id, "email": f"uncommitted-{user_id.hex}@revnote.test"},
        )

        async with new_session_factory()() as reader:
            assert (await reader.execute(statement, {"id": user_id})).scalar_one() == 0

        await writer.commit()

        async with new_session_factory()() as reader:
            assert (await reader.execute(statement, {"id": user_id})).scalar_one() == 1

    async with session_factory() as cleanup:  # this module asserts schema, not rows
        await cleanup.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        await cleanup.commit()


# --------------------------------------------------------------------------- #
# schema delivered by the harness
# --------------------------------------------------------------------------- #


async def test_business_database_is_stamped_with_the_single_head(
    catalog: Catalog, head_revision: str, alembic_heads: tuple[str, ...]
) -> None:
    """Exactly one revision is stamped, and it is the single head."""
    assert alembic_heads == (head_revision,)
    assert await catalog.version() == head_revision


async def test_exactly_the_contract_tables_exist(catalog: Catalog) -> None:
    """The business schema is the ten domain tables + ``alembic_version``."""
    assert await catalog.tables() == list(HEAD_TABLES)
    assert await catalog.table_set() == {*DOMAIN_TABLES, VERSION_TABLE}


async def test_no_sequences_so_counters_are_application_allocated(catalog: Catalog) -> None:
    """No SERIAL/IDENTITY/nextval: the kernel allocates ``seq``/``rev`` itself."""
    assert await catalog.sequences() == []
    assert await catalog.nextval_defaults() == []


async def test_every_kernel_table_has_a_primary_key(catalog: Catalog) -> None:
    """Each of the six kernel tables is protected by a real primary key."""
    for table in KERNEL_TABLES:
        primary = [c.name for c in await catalog.constraints(table) if c.kind == "primary key"]
        assert primary, f"{table} has no primary key"


async def test_every_auth_table_has_a_primary_key(catalog: Catalog) -> None:
    """Phase 3 tables are keyed too; membership is a composite key."""
    for table in DOMAIN_TABLES:
        primary = [c.name for c in await catalog.constraints(table) if c.kind == "primary key"]
        assert primary, f"{table} has no primary key"

    members = (await catalog.constraint_map("workspace_members"))["pk_workspace_members"]
    assert members.definition == "PRIMARY KEY (workspace_id, user_id)"


# --------------------------------------------------------------------------- #
# the business database is never downgraded
# --------------------------------------------------------------------------- #


def test_downgrade_is_refused_for_the_business_database(
    alembic_cli, normal_database_url: str
) -> None:
    """The guard rejects the business DSN before any subprocess can start."""
    with pytest.raises(AlembicGuardError, match="only permitted"):
        alembic_cli.run(normal_database_url, "downgrade", "base")


async def test_schema_is_still_at_head_after_connectivity(
    catalog: Catalog, head_revision: str
) -> None:
    """Nothing in this module mutated the schema: still head, still ten tables."""
    assert await catalog.version() == head_revision
    assert await catalog.table_set() == {*DOMAIN_TABLES, VERSION_TABLE}


def test_environment_variable_names_are_the_contract_ones() -> None:
    """The two DSNs are distinct, explicitly named variables (no silent fallback)."""
    assert (TEST_DATABASE_URL_ENV, TEST_MIGRATION_DATABASE_URL_ENV) == (
        "TEST_DATABASE_URL",
        "TEST_MIGRATION_DATABASE_URL",
    )
