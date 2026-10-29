"""Alembic migration environment (async SQLAlchemy + PostgreSQL/asyncpg).

Configuration comes from :class:`revnote_api.config.Settings` — the same
settings the API uses — so ``DATABASE_URL`` / ``.env`` is the single source of
truth and the URL is never duplicated in ``alembic.ini``.

Always invoke Alembic from the **repository root** (paths in ``alembic.ini``
are repo-root relative)::

    uv run alembic -c apps/api/alembic.ini current
    uv run alembic -c apps/api/alembic.ini upgrade head
    uv run alembic -c apps/api/alembic.ini downgrade base

Schema is owned by the revisions. This module imports the model package **only**
so ``Base.metadata`` is populated for autogenerate/``alembic check`` comparison
and so constraint names resolve identically in ``op.*`` calls; it never creates
schema from the ORM (no ``create_all`` anywhere).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# Explicit registration: the six Phase 2 tables must be on Base.metadata
# regardless of which modules the rest of the process happened to import.
# (Also read below for ``TARGET_TABLES``, so this is a real use, not a side-effect.)
import revnote_api.models
from revnote_api.config import get_settings
from revnote_api.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

#: Tables this phase owns, asserted against by the migration tests.
TARGET_TABLES: frozenset[str] = frozenset(revnote_api.models.MODEL_TABLES)

# Target metadata for autogenerate. Populated by the model package above; the
# migration revisions, not the ORM, are what actually create the schema.
target_metadata = Base.metadata

settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a DBAPI connection (``--sql`` mode)."""
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations through an async engine (asyncpg)."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(_do_run_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
