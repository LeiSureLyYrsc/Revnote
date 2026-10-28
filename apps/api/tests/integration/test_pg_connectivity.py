"""PostgreSQL connectivity / Alembic baseline integration tests (opt-in).

Gating contract:

* ``TEST_DATABASE_URL`` **unset** -> skipped. The scaffold must stay runnable on
  a machine with no PostgreSQL at all.
* ``TEST_DATABASE_URL`` **set**   -> the tests *must* connect. A refused
  connection, bad credentials or a wrong driver is a **hard failure**, never a
  skip: once a database is promised, silence is worse than red.

Run with an explicit URL, e.g.::

    $env:TEST_DATABASE_URL = "postgresql+asyncpg://revnote:revnote@127.0.0.1:5432/revnote_test"
    uv run pytest apps/api/tests/integration -v

The Alembic tests execute the real, empty baseline ``upgrade``/``downgrade``
against that database (no tables are created — the baseline is a no-op chain
entry, and ``Base.metadata.create_all()`` is never used).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker

from revnote_api.config import ASYNC_PG_DRIVER, get_settings
from revnote_api.db.engine import create_engine_for_url

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

API_DIR = Path(__file__).resolve().parents[2]
ALEMBIC_INI = API_DIR / "alembic.ini"


def _require_test_database_url(test_database_url: str | None) -> str:
    """Skip only when unset; any other problem must surface as a failure."""
    if test_database_url is None:
        pytest.skip("TEST_DATABASE_URL is not set; skipping PostgreSQL integration test")

    url = make_url(test_database_url)
    if url.drivername != ASYNC_PG_DRIVER:
        pytest.fail(f"TEST_DATABASE_URL must use {ASYNC_PG_DRIVER!r}, got {url.drivername!r}")
    return test_database_url


@pytest.fixture
def alembic_config(monkeypatch: pytest.MonkeyPatch, test_database_url: str | None) -> Config:
    """Alembic Config pointed at TEST_DATABASE_URL (script paths via %(here)s)."""
    url = _require_test_database_url(test_database_url)
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()

    return Config(str(ALEMBIC_INI), ini_section="alembic")


async def test_select_1_against_postgres(test_database_url: str | None) -> None:
    """Open a real connection and run ``SELECT 1`` through the async engine."""
    url = _require_test_database_url(test_database_url)

    engine = create_engine_for_url(url)
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
            version = (await connection.execute(text("SELECT version()"))).scalar_one()
            assert "PostgreSQL" in version
    finally:
        await engine.dispose()


async def test_session_round_trip_and_rollback(test_database_url: str | None) -> None:
    """Sessions work against the real server; rollback leaves no state behind."""
    url = _require_test_database_url(test_database_url)

    engine = create_engine_for_url(url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    try:
        async with factory() as session:
            assert (await session.execute(text("SELECT 42"))).scalar_one() == 42

        async with factory() as session:
            await session.execute(text("SELECT 1"))
            await session.rollback()
            assert (await session.execute(text("SELECT 2"))).scalar_one() == 2
    finally:
        await engine.dispose()


async def test_empty_baseline_upgrade_and_downgrade(alembic_config: Config) -> None:
    """The real empty baseline must upgrade *and* downgrade cleanly."""

    def _upgrade() -> None:
        command.upgrade(alembic_config, "head")

    def _downgrade() -> None:
        command.downgrade(alembic_config, "base")

    # Alembic's async env.py owns its event loop via asyncio.run().
    await asyncio.to_thread(_upgrade)
    await asyncio.to_thread(_downgrade)


async def test_baseline_creates_no_business_tables(
    alembic_config: Config, test_database_url: str | None
) -> None:
    """After upgrade the schema is still empty apart from alembic_version."""
    url = _require_test_database_url(test_database_url)

    def _upgrade() -> None:
        command.upgrade(alembic_config, "head")

    await asyncio.to_thread(_upgrade)

    engine = create_engine_for_url(url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = current_schema() ORDER BY tablename"
                )
            )
            tables = result.scalars().all()
    finally:
        await engine.dispose()

    assert tables == ["alembic_version"]
