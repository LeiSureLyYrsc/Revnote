"""Fixtures for the Phase 2 PostgreSQL integration tests.

Two **different** disposable databases, with the split enforced here rather than
left to convention:

``TEST_DATABASE_URL``
    Business database: the six-table schema produced by the real Alembic chain
    (never ``create_all()``), and the only database the connectivity/kernel tests
    write to. ``downgrade`` is refused against it.

``TEST_MIGRATION_DATABASE_URL``
    Migration database: ``base``/``baseline``/``head`` round trips and catalog
    inspection. It must address a different database or the suite fails.

Gating: ``REQUIRE_POSTGRES_TESTS`` truthy or a CI environment requires **both**
URLs (missing/malformed is a hard failure). Locally with no URL at all the package
skips; a URL that is set is never turned into a skip.

Every async fixture is function scoped over a ``NullPool`` engine, so each test
gets its own connection on its own event loop.

Fixture interface
-----------------
``pg_engine`` / ``session_factory`` / ``seed_workspace``
    The business-database contract used by the kernel tests.
``new_session_factory()``
    Factory for additional independent session factories (concurrency).
``catalog`` / ``migration_catalog`` / ``migration_engine``
    Catalog readers and the migration engine.
``migration_session_factory``
    Sessions on the migration database (constraint-behaviour cases).
``alembic_migrations`` / ``alembic_cli`` / ``alembic_heads`` / ``head_revision``
    Real Alembic CLI and revision chain.
``postgres_targets`` / ``normal_database_url`` / ``migration_database_url`` /
``test_database_url``
    The validated DSNs.

``seed_workspace`` contract (what the kernel tests may rely on)::

    user, workspace = await seed_workspace()
    await seed_workspace(last_seq=7)
    await seed_workspace(new_session_factory())

It returns a real 2-tuple of detached ORM rows, so ``user.id``, ``workspace.id``
and ``workspace.owner_id`` are readable. Every call inserts a **new** user +
workspace (fresh UUIDs, unique e-mail), which is why cases are isolated by
workspace and never need a ``TRUNCATE`` between them.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pg_migrations import (
    AlembicCli,
    AlembicError,
    AlembicGuardError,
    AlembicResult,
    MigrationDatabase,
    ensure_downgrade_allowed,
    run_alembic,
)
from pg_testing import (
    CI_ENV_VARS,
    FALSY_VALUES,
    HEAD_TABLES,
    KERNEL_TABLES,
    REQUIRE_POSTGRES_TESTS_ENV,
    TEST_DATABASE_URL_ENV,
    TEST_MIGRATION_DATABASE_URL_ENV,
    VERSION_TABLE,
    Catalog,
    build_engine,
    build_session_factory,
)
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from revnote_api.config import ASYNC_PG_DRIVER

__all__ = [
    "HEAD_TABLES",
    "KERNEL_TABLES",
    "VERSION_TABLE",
    "AlembicCli",
    "AlembicError",
    "AlembicGuardError",
    "AlembicResult",
    "MigrationDatabase",
    "PostgresTargets",
    "ensure_downgrade_allowed",
    "postgres_tests_are_required",
    "require_database_url",
    "run_alembic",
]

SessionFactory = async_sessionmaker[AsyncSession]
SessionFactoryFactory = Callable[[], SessionFactory]
SeedWorkspace = Callable[..., Awaitable[tuple[object, object]]]

# apps/api/tests/integration/conftest.py -> integration, tests, api
API_DIR = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# DSN gating
# --------------------------------------------------------------------------- #


def postgres_tests_are_required() -> bool:
    """True when this run promised a real PostgreSQL server."""
    configured = os.environ.get(REQUIRE_POSTGRES_TESTS_ENV, "").strip()
    if configured:
        return configured.lower() not in FALSY_VALUES
    return any(
        os.environ.get(name, "").strip().lower() not in FALSY_VALUES
        for name in CI_ENV_VARS
        if name in os.environ
    )


def require_database_url(env_name: str) -> str:
    """Return a validated asyncpg DSN; skip only when nothing was promised."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        message = (
            f"{env_name} is not set. The Phase 2 PostgreSQL suite needs two disposable "
            f"databases: {TEST_DATABASE_URL_ENV} (business) and "
            f"{TEST_MIGRATION_DATABASE_URL_ENV} (migration round trips)."
        )
        if postgres_tests_are_required():
            pytest.fail(message, pytrace=False)
        pytest.skip(message)

    try:
        url = make_url(raw)
    except Exception as exc:
        pytest.fail(f"{env_name} is not a valid SQLAlchemy URL: {exc}", pytrace=False)
    if url.drivername != ASYNC_PG_DRIVER:
        pytest.fail(
            f"{env_name} must use {ASYNC_PG_DRIVER!r}, got {url.drivername!r}", pytrace=False
        )
    if not url.database:
        pytest.fail(f"{env_name} must include a database name", pytrace=False)
    return raw


@dataclass(frozen=True, slots=True)
class PostgresTargets:
    """The validated pair of disposable databases for this session."""

    database_url: str
    migration_database_url: str

    @staticmethod
    def _identity(database_url: str) -> tuple[str | None, int | None, str | None]:
        url = make_url(database_url)
        return url.host, url.port, url.database

    @property
    def business_identity(self) -> tuple[str | None, int | None, str | None]:
        return self._identity(self.database_url)

    @property
    def migration_identity(self) -> tuple[str | None, int | None, str | None]:
        return self._identity(self.migration_database_url)


@pytest.fixture(scope="session")
def postgres_targets() -> PostgresTargets:
    """Both DSNs validated, and proven to address two *different* databases."""
    targets = PostgresTargets(
        database_url=require_database_url(TEST_DATABASE_URL_ENV),
        migration_database_url=require_database_url(TEST_MIGRATION_DATABASE_URL_ENV),
    )
    if targets.business_identity == targets.migration_identity:
        pytest.fail(
            "TEST_DATABASE_URL and TEST_MIGRATION_DATABASE_URL point at the same database "
            f"({make_url(targets.database_url).render_as_string(hide_password=True)}); the "
            "migration round trip runs destructive DDL and must not touch the business one.",
            pytrace=False,
        )
    return targets


@pytest.fixture(scope="session", autouse=True)
def _postgres_gate(postgres_targets: PostgresTargets) -> PostgresTargets:
    """Gate the package before any test body runs (skip locally, fail in CI)."""
    return postgres_targets


@pytest.fixture(scope="session")
def normal_database_url(postgres_targets: PostgresTargets) -> str:
    """Business DSN."""
    return postgres_targets.database_url


@pytest.fixture(scope="session")
def migration_database_url(postgres_targets: PostgresTargets) -> str:
    """Migration DSN."""
    return postgres_targets.migration_database_url


@pytest.fixture
def test_database_url(postgres_targets: PostgresTargets) -> str:
    """Strict business DSN; overrides the lenient fixture from the root conftest."""
    return postgres_targets.database_url


# --------------------------------------------------------------------------- #
# Alembic
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def alembic_cli() -> AlembicCli:
    """``run``/``guard`` around the real Alembic CLI."""
    return AlembicCli()


@pytest.fixture(scope="session")
def alembic_heads() -> tuple[str, ...]:
    """Heads declared by the script directory (no database involved)."""
    config = Config(str(API_DIR / "alembic.ini"), ini_section="alembic")
    return tuple(ScriptDirectory.from_config(config).get_heads())


@pytest.fixture(scope="session")
def head_revision(alembic_heads: tuple[str, ...]) -> str:
    """The single head; a forked chain is a hard failure."""
    if len(alembic_heads) != 1:
        pytest.fail(f"expected exactly one Alembic head, found {alembic_heads}", pytrace=False)
    return alembic_heads[0]


@pytest.fixture(scope="session")
def alembic_migrations(
    migration_database_url: str, normal_database_url: str, alembic_cli: AlembicCli
) -> MigrationDatabase:
    """Migration runner bound to the migration DSN only."""
    return MigrationDatabase(
        url=migration_database_url, business_url=normal_database_url, cli=alembic_cli
    )


@pytest.fixture(scope="session")
def business_schema_at_head(normal_database_url: str) -> AlembicResult:
    """Apply the real chain to the business database once per session.

    The only way business schema is created: no ``create_all()``, no SQLite, no
    fixture emitting DDL. A failure here is a hard error.
    """
    return run_alembic(normal_database_url, "upgrade", "head")


# --------------------------------------------------------------------------- #
# engines / sessions
# --------------------------------------------------------------------------- #


@pytest.fixture
async def pg_engine(
    business_schema_at_head: AlembicResult, normal_database_url: str
) -> AsyncIterator[AsyncEngine]:
    """Function-scoped ``NullPool`` engine on the business database."""
    del business_schema_at_head  # dependency: the schema must exist first
    engine = build_engine(normal_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def session_factory(pg_engine: AsyncEngine) -> SessionFactory:
    """``async_sessionmaker`` on ``pg_engine`` (``expire_on_commit=False``)."""
    return build_session_factory(pg_engine)


@pytest.fixture
async def new_session_factory(
    business_schema_at_head: AlembicResult, normal_database_url: str
) -> AsyncIterator[SessionFactoryFactory]:
    """Factory for *extra* independent session factories (concurrency tests).

    Each call opens a new ``NullPool`` engine, so concurrent tasks never share a
    connection; everything created is disposed at teardown.
    """
    del business_schema_at_head
    engines: list[AsyncEngine] = []

    def _create() -> SessionFactory:
        engine = build_engine(normal_database_url)
        engines.append(engine)
        return build_session_factory(engine)

    try:
        yield _create
    finally:
        for engine in engines:
            await engine.dispose()


@pytest.fixture
async def migration_engine(migration_database_url: str) -> AsyncIterator[AsyncEngine]:
    """Function-scoped ``NullPool`` engine on the migration database."""
    engine = build_engine(migration_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def migration_session_factory(migration_engine: AsyncEngine) -> SessionFactory:
    """Sessions on the migration database (constraint-behaviour cases)."""
    return build_session_factory(migration_engine)


@pytest.fixture
def catalog(pg_engine: AsyncEngine) -> Catalog:
    """Catalog reader over the business database."""
    return Catalog(engine=pg_engine)


@pytest.fixture
def migration_catalog(migration_engine: AsyncEngine) -> Catalog:
    """Catalog reader over the migration database."""
    return Catalog(engine=migration_engine)


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #


@pytest.fixture
def seed_workspace(session_factory: SessionFactory) -> SeedWorkspace:
    """Async callable inserting one new user + workspace and committing.

    See the module docstring for the exact contract; ``last_seq`` pins the
    workspace counter and an explicit factory seeds through another connection.
    """
    from revnote_api.models import User, Workspace

    async def seed(
        factory: SessionFactory | None = None,
        *,
        last_seq: int | None = None,
    ) -> tuple[User, Workspace]:
        maker = session_factory if factory is None else factory
        user_id, workspace_id = uuid.uuid4(), uuid.uuid4()
        user = User(id=user_id, email=f"seed-{workspace_id.hex}@revnote.test")
        workspace = Workspace(
            id=workspace_id,
            owner_id=user_id,
            last_seq=0 if last_seq is None else last_seq,
        )
        async with maker() as session:
            session.add(user)
            await session.flush()  # owner first: workspaces.owner_id is NOT NULL
            session.add(workspace)
            await session.commit()
        return user, workspace

    return seed
