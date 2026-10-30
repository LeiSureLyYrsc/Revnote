"""Fixture-free helpers shared by the Phase 2 PostgreSQL integration tests.

Kept out of ``conftest`` so test modules never import ``conftest``: this module is
plain Python (constants, engine builders, a catalog reader) and is importable from
both a conftest and a test file.

:class:`Catalog` answers every schema question from ``pg_catalog`` /
``information_schema`` so the tests assert on the *real* database rather than on
migration source text.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NamedTuple

from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# --------------------------------------------------------------------------- #
# environment contract
# --------------------------------------------------------------------------- #

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"  # business DB: schema + writes
TEST_MIGRATION_DATABASE_URL_ENV = "TEST_MIGRATION_DATABASE_URL"  # destructive DDL
REQUIRE_POSTGRES_TESTS_ENV = "REQUIRE_POSTGRES_TESTS"  # truthy => a URL is mandatory
CI_ENV_VARS = ("GITHUB_ACTIONS", "CI")  # a CI run must never silently skip
FALSY_VALUES = frozenset({"", "0", "false", "no", "off"})

# --------------------------------------------------------------------------- #
# the schema contract
# --------------------------------------------------------------------------- #

KERNEL_TABLES = ("users", "workspaces", "folders", "notes", "change_events", "op_ledger")
#: Phase 3 auth tables plus the per-request authorisation row (plan §9).
AUTH_TABLES = ("auth_sessions", "auth_access_tokens", "auth_refresh_tokens", "workspace_members")
#: Phase 5 attachment table: not a kernel entity, so never in the sync stream.
ATTACHMENT_TABLES = ("attachments",)
#: Every domain table the current head owns — eleven of them.
DOMAIN_TABLES = (*KERNEL_TABLES, *AUTH_TABLES, *ATTACHMENT_TABLES)
VERSION_TABLE = "alembic_version"
HEAD_TABLES = tuple(sorted((*DOMAIN_TABLES, VERSION_TABLE)))

BASELINE_REVISION = "0001_baseline"
KERNEL_REVISION = "0002_sync_kernel_core"
AUTH_REVISION = "0003_auth_sessions_members"
ATTACHMENT_REVISION = "0004_attachments"
HEAD_REVISION = ATTACHMENT_REVISION

# --------------------------------------------------------------------------- #
# engines / sessions
# --------------------------------------------------------------------------- #

SessionFactory = async_sessionmaker[AsyncSession]


def build_engine(database_url: str) -> AsyncEngine:
    """New ``NullPool`` engine: one real connection per checkout, never recycled."""
    return create_async_engine(database_url, poolclass=pool.NullPool, future=True)


def build_session_factory(engine: AsyncEngine) -> SessionFactory:
    """Session factory with explicit commits and no implicit flush."""
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


# --------------------------------------------------------------------------- #
# catalog reader
# --------------------------------------------------------------------------- #

_KIND_BY_CONTYPE = {"p": "primary key", "f": "foreign key", "u": "unique", "c": "check"}
_ON_DELETE_BY_CODE = {
    "r": "RESTRICT",
    "a": "NO ACTION",
    "c": "CASCADE",
    "n": "SET NULL",
    "d": "SET DEFAULT",
}


def as_text(value: Any) -> str:
    """Normalise a catalog value: asyncpg returns ``"char"`` columns as ``bytes``."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    return "" if value is None else str(value)


class Column(NamedTuple):
    name: str
    data_type: str
    nullable: bool
    default: str | None
    is_identity: bool


class Constraint(NamedTuple):
    name: str
    kind: str
    definition: str
    deferrable: bool
    initially_deferred: bool
    on_delete: str | None


class Index(NamedTuple):
    name: str
    definition: str
    unique: bool
    primary: bool


@dataclass(slots=True)
class Catalog:
    """Read-only catalog access; each call opens and closes its own connection."""

    engine: AsyncEngine

    async def _all(self, sql: str, **params: Any) -> list[Any]:
        async with self.engine.connect() as connection:
            return list((await connection.execute(text(sql), params)).all())

    async def _scalar(self, sql: str, **params: Any) -> Any:
        async with self.engine.connect() as connection:
            return await connection.scalar(text(sql), params)

    async def tables(self) -> list[str]:
        """Tables in the current schema, sorted (``alembic_version`` included)."""
        rows = await self._all(
            "SELECT tablename AS name FROM pg_tables "
            "WHERE schemaname = current_schema() ORDER BY tablename"
        )
        return [row.name for row in rows]

    async def table_set(self) -> set[str]:
        return set(await self.tables())

    async def version(self) -> str | None:
        """The single stamped revision, or ``None`` when absent/empty.

        ``downgrade base`` deletes the version *row* but keeps the (empty) table,
        so "no revision stamped" and "no table" are both handled.
        """
        if not await self._scalar("SELECT to_regclass(:name) IS NOT NULL", name=VERSION_TABLE):
            return None
        rows = await self._all(f"SELECT version_num FROM {VERSION_TABLE}")  # fixed name
        assert len(rows) <= 1, f"{VERSION_TABLE} must hold one row, got {rows}"
        return rows[0].version_num if rows else None

    async def current_database(self) -> str:
        return str(await self._scalar("SELECT current_database()"))

    async def columns(self, table: str) -> list[Column]:
        """Columns in declaration order."""
        rows = await self._all(
            "SELECT column_name, data_type, is_nullable, column_default, is_identity "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table "
            "ORDER BY ordinal_position",
            table=table,
        )
        return [
            Column(
                name=row.column_name,
                data_type=row.data_type,
                nullable=row.is_nullable == "YES",
                default=row.column_default,
                is_identity=as_text(row.is_identity) == "YES",
            )
            for row in rows
        ]

    async def column_map(self, table: str) -> dict[str, Column]:
        return {column.name: column for column in await self.columns(table)}

    async def constraints(self, table: str) -> list[Constraint]:
        rows = await self._all(
            "SELECT c.conname, c.contype, pg_get_constraintdef(c.oid) AS definition, "
            "c.condeferrable, c.condeferred, c.confdeltype "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() AND t.relname = :table "
            "ORDER BY c.conname",
            table=table,
        )
        result = []
        for row in rows:
            contype = as_text(row.contype)
            result.append(
                Constraint(
                    name=row.conname,
                    kind=_KIND_BY_CONTYPE.get(contype, contype),
                    definition=row.definition,
                    deferrable=bool(row.condeferrable),
                    initially_deferred=bool(row.condeferred),
                    on_delete=(
                        _ON_DELETE_BY_CODE.get(as_text(row.confdeltype)) if contype == "f" else None
                    ),
                )
            )
        return result

    async def constraint_map(self, table: str) -> dict[str, Constraint]:
        return {constraint.name: constraint for constraint in await self.constraints(table)}

    async def indexes(self, table: str) -> list[Index]:
        """Indexes sorted by name (the primary-key index included)."""
        rows = await self._all(
            "SELECT i.relname, pg_get_indexdef(ix.indexrelid) AS definition, "
            "ix.indisunique, ix.indisprimary "
            "FROM pg_index ix "
            "JOIN pg_class i ON i.oid = ix.indexrelid "
            "JOIN pg_class t ON t.oid = ix.indrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() AND t.relname = :table "
            "ORDER BY i.relname",
            table=table,
        )
        return [
            Index(
                name=row.relname,
                definition=row.definition,
                unique=bool(row.indisunique),
                primary=bool(row.indisprimary),
            )
            for row in rows
        ]

    async def index_names(self, table: str) -> set[str]:
        return {index.name for index in await self.indexes(table)}

    async def sequences(self) -> list[str]:
        """Sequences in the current schema (must stay empty: no SERIAL/IDENTITY)."""
        rows = await self._all(
            "SELECT c.relname AS name FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'S' AND n.nspname = current_schema() ORDER BY c.relname"
        )
        return [row.name for row in rows]

    async def nextval_defaults(self) -> list[str]:
        rows = await self._all(
            "SELECT table_name || '.' || column_name AS location "
            "FROM information_schema.columns WHERE table_schema = current_schema() "
            "AND column_default LIKE '%nextval%' ORDER BY 1"
        )
        return [row.location for row in rows]
