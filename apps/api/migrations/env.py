"""Alembic migration environment (async SQLAlchemy + PostgreSQL/asyncpg).

Configuration comes from :class:`revnote_api.config.Settings` — the same
settings the API uses — so ``DATABASE_URL`` / ``.env`` is the single source of
truth and the URL is never duplicated in ``alembic.ini``.

Always invoke Alembic from the **repository root** (paths in ``alembic.ini``
are repo-root relative)::

    uv run alembic -c apps/api/alembic.ini current
    uv run alembic -c apps/api/alembic.ini upgrade head
    uv run alembic -c apps/api/alembic.ini downgrade base

The scaffold ships one empty baseline revision: it creates **no** business
tables and this module never mass-creates schema from ORM metadata.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from revnote_api.config import get_settings
from revnote_api.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Target metadata for future autogenerate. It is intentionally **empty** today
# (no models are declared in the scaffold); autogenerate must produce nothing.
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
