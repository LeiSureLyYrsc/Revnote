"""HTTP harness for the phase 3 API suite: one real app, one real database.

Why this module exists
----------------------
``tests/api`` needs four things a plain fixture does not give it:

1. **The production app.** ``pg_support.auth_support.build_app`` is
   ``create_app(settings)``: every versioned group is mounted by ``api/router.py``,
   the single registration surface. This harness therefore never mounts a router of
   its own - a second mounting would register duplicate paths and exercise a handler
   production does not reach.
2. **One seeded account, one workspace, one real bearer token.** Seeding reuses
   :mod:`pg_support.auth_support` (single owner of that logic) so the Argon2 cost is
   paid once per test, at the production parameters.
3. **Direct database access to prove what HTTP claims.** :meth:`DatabaseProbe.state`
   reads ``workspaces.last_seq``, ``change_events``, ``folders``/``notes`` and
   ``op_ledger`` on a *separate* connection, so "nothing was executed" is a fact
   about committed rows rather than an inference from a response body.
4. **Deterministic concurrency scaffolding.** :func:`workspace_lock` holds the
   kernel's own row lock, :func:`row_lock` holds one object row (to block a *later*
   step of an in-flight op), and :func:`poll` waits for observable server state
   (``pg_locks``, committed rows) instead of sleeping a guessed duration.

Two entry points:

``open_api()``
    app + ASGI client + token, for the ``api`` and ``api_concurrency`` tiers.
``open_app()``
    app + seeded account, **no client and no lifespan**, so a transport test can run
    the app under a real uvicorn server (:mod:`pg_support.socket_probe`) exactly
    once. Starting both a lifespan and a server on one app would dispose engines
    from under the other.

Both are used *inside* pytest fixtures that already requested
``business_schema_at_head``; calling one directly from a test means the schema was
not migrated, so the first query fails loudly rather than silently reading an empty
database.

Everything here runs against real PostgreSQL. A missing ``TEST_DATABASE_URL`` is a
hard failure, never a skip: a skipped API suite is indistinguishable from a passing
one in CI.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import asyncpg
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from pg_support.auth_support import (
    Account,
    build_app,
    require_database_url,
    seed_account,
)
from pg_support.auth_support import (
    make_settings as make_settings_shared,
)
from revnote_api.config import Settings
from revnote_api.db.engine import dispose_engines

__all__ = [
    "API_PREFIX",
    "ApiHarness",
    "AppUnderTest",
    "DatabaseProbe",
    "WorkspaceState",
    "asyncpg_dsn",
    "backend_pid",
    "build_api_app",
    "count_waiting_backends",
    "folder_create",
    "folder_delete",
    "folder_move",
    "folder_update",
    "note_create",
    "note_delete",
    "note_move",
    "note_update",
    "op",
    "open_api",
    "open_app",
    "parse_decimal",
    "poll",
    "row_lock",
    "wait_until_blocked_by",
    "workspace_lock",
]

#: Business prefix of every versioned route (spec §1).
API_PREFIX = "/api/v1"

_ASYNC_PG_PREFIX = re.compile(r"^postgresql\+asyncpg://")


def asyncpg_dsn(database_url: str) -> str:
    """``postgresql+asyncpg://…`` -> the plain DSN ``asyncpg`` itself accepts."""
    return _ASYNC_PG_PREFIX.sub("postgresql://", database_url, count=1)


def build_api_app(settings: Settings) -> FastAPI:
    """The production application - ``create_app`` and nothing else.

    Delegates to :func:`pg_support.auth_support.build_app`, the single app builder, so
    this harness and the auth fixtures cannot drift. ``api/router.py`` is the only
    registration surface and mounts every versioned group (auth, push, pull, read), so
    nothing here may mount a router: a second mounting registers duplicate paths and
    silently exercises a handler production does not reach.
    """
    return build_app(settings)


def make_settings(database_url: str, overrides: Mapping[str, Any] | None = None) -> Settings:
    """Settings for one test app: explicit DSN, no ``.env``, optional overrides.

    Delegates to :func:`pg_support.auth_support.make_settings` so both entrances use
    the same pool sizing - a harness with a smaller pool than the fixture would make
    an HTTP concurrency case look like a lock problem.
    """
    options: dict[str, object] = dict(overrides or {})
    return make_settings_shared(database_url, **options)


# --------------------------------------------------------------------------- #
# database probe
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class WorkspaceState:
    """Committed rows of one workspace, read on a separate connection."""

    last_seq: int
    event_seqs: tuple[int, ...]
    folders: int
    notes: int
    ledger_rows: int

    @property
    def dense(self) -> bool:
        """True when the events form exactly ``1..last_seq`` (no gap, no repeat)."""
        return self.event_seqs == tuple(range(1, self.last_seq + 1))


def parse_decimal(value: Any) -> int:
    """Read a decimal-string counter (spec §1) without caring about JSON type."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    raise AssertionError(f"not a decimal string or integer: {value!r}")


class DatabaseProbe:
    """A dedicated ``asyncpg`` connection: the ground truth for HTTP assertions."""

    def __init__(self, database_url: str) -> None:
        self._dsn = asyncpg_dsn(database_url)
        self._conn: asyncpg.Connection | None = None

    async def conn(self) -> asyncpg.Connection:
        """The probe's own connection (separate from every request's)."""
        if self._conn is None:
            self._conn = await asyncpg.connect(dsn=self._dsn)
        return self._conn

    async def state(self, workspace_id: uuid.UUID) -> WorkspaceState:
        connection = await self.conn()
        last_seq = await connection.fetchval(
            "SELECT last_seq FROM workspaces WHERE id = $1", workspace_id
        )
        if last_seq is None:
            pytest.fail(f"workspace {workspace_id} does not exist", pytrace=False)
        events = await connection.fetch(
            "SELECT seq FROM change_events WHERE workspace_id = $1 ORDER BY seq", workspace_id
        )
        folders = await connection.fetchval(
            "SELECT count(*) FROM folders WHERE workspace_id = $1", workspace_id
        )
        notes = await connection.fetchval(
            "SELECT count(*) FROM notes WHERE workspace_id = $1", workspace_id
        )
        ledger = await connection.fetchval(
            "SELECT count(*) FROM op_ledger WHERE workspace_id = $1", workspace_id
        )
        return WorkspaceState(
            last_seq=int(last_seq),
            event_seqs=tuple(int(row["seq"]) for row in events),
            folders=int(folders),
            notes=int(notes),
            ledger_rows=int(ledger),
        )

    async def note_row(self, workspace_id: uuid.UUID, note_id: uuid.UUID) -> Mapping[str, Any]:
        connection = await self.conn()
        row = await connection.fetchrow(
            "SELECT rev, title, body_md, deleted_at FROM notes WHERE workspace_id = $1 AND id = $2",
            workspace_id,
            note_id,
        )
        if row is None:
            pytest.fail(f"note {note_id} does not exist", pytrace=False)
        return dict(row)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None


# --------------------------------------------------------------------------- #
# app under test
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class AppUnderTest:
    """The app, its settings, one seeded owner account, and a database probe.

    ``lifespan_started`` records whether :func:`open_app` (``False``) or
    :func:`open_api` (``True``) built this instance: only the ASGI variant owns the
    lifespan, so a transport test never has two shutdowns racing to dispose the
    same engines.
    """

    app: FastAPI
    settings: Settings
    account: Account
    database_url: str
    factory: async_sessionmaker[AsyncSession]
    db: DatabaseProbe
    lifespan_started: bool = False

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.account.workspace_id

    @property
    def user_id(self) -> uuid.UUID:
        return self.account.user_id

    def url(self, path: str) -> str:
        return f"{API_PREFIX}{path}"

    def headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def state(self, workspace_id: uuid.UUID | None = None) -> WorkspaceState:
        return await self.db.state(workspace_id or self.workspace_id)

    async def seed_second_account(self) -> Account:
        """A second, fully independent account with its own workspace (isolation)."""
        return await seed_account(self.factory)

    async def close(self) -> None:
        await self.db.close()


@dataclass(slots=True)
class ApiHarness:
    """An :class:`AppUnderTest` plus an in-process client and a live access token.

    Composition rather than inheritance: the app bundle is also used *without* a
    client by the transport tier, and a subclass whose ``client`` is optional would
    make every method's assumptions harder to read.
    """

    app: AppUnderTest
    client: httpx.AsyncClient
    token: str

    # -- delegation ---------------------------------------------------------- #
    @property
    def workspace_id(self) -> uuid.UUID:
        return self.app.workspace_id

    @property
    def user_id(self) -> uuid.UUID:
        return self.app.user_id

    @property
    def settings(self) -> Settings:
        return self.app.settings

    @property
    def account(self) -> Account:
        return self.app.account

    @property
    def db(self) -> DatabaseProbe:
        return self.app.db

    def url(self, path: str) -> str:
        return f"{API_PREFIX}{path}"

    async def state(self, workspace_id: uuid.UUID | None = None) -> WorkspaceState:
        return await self.app.state(workspace_id)

    # -- HTTP ---------------------------------------------------------------- #
    def headers(self, token: str | None = None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token or self.token}"}

    async def push(
        self,
        ops: Sequence[Mapping[str, Any]],
        *,
        workspace_id: uuid.UUID | None = None,
        token: str | None = None,
    ) -> httpx.Response:
        """POST one batch; the caller asserts on status and per-op results."""
        return await self.client.post(
            self.url(f"/workspaces/{workspace_id or self.workspace_id}/sync/push"),
            json={"ops": list(ops)},
            headers=self.headers(token),
        )

    async def push_raw(
        self,
        body: Mapping[str, Any],
        *,
        workspace_id: uuid.UUID | None = None,
        token: str | None = None,
    ) -> httpx.Response:
        """POST a hand-built body (for shapes the schema would not produce)."""
        return await self.client.post(
            self.url(f"/workspaces/{workspace_id or self.workspace_id}/sync/push"),
            json=body,
            headers=self.headers(token),
        )

    async def pull(
        self,
        *,
        since: str | int = "0",
        limit: int | None = None,
        workspace_id: uuid.UUID | None = None,
        token: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        query: dict[str, Any] = {"since": str(since)}
        if limit is not None:
            query["limit"] = limit
        query.update(params or {})
        return await self.client.get(
            self.url(f"/workspaces/{workspace_id or self.workspace_id}/sync/pull"),
            params=query,
            headers=self.headers(token),
        )

    async def snapshot(
        self,
        *,
        workspace_id: uuid.UUID | None = None,
        token: str | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        return await self.client.get(
            self.url(f"/workspaces/{workspace_id or self.workspace_id}/sync/snapshot"),
            params=dict(params or {}),
            headers=self.headers(token),
        )

    async def tree(
        self, *, workspace_id: uuid.UUID | None = None, token: str | None = None
    ) -> httpx.Response:
        return await self.client.get(
            self.url(f"/workspaces/{workspace_id or self.workspace_id}/tree"),
            headers=self.headers(token),
        )

    async def note(
        self,
        note_id: uuid.UUID,
        *,
        workspace_id: uuid.UUID | None = None,
        token: str | None = None,
    ) -> httpx.Response:
        return await self.client.get(
            self.url(f"/workspaces/{workspace_id or self.workspace_id}/notes/{note_id}"),
            headers=self.headers(token),
        )

    async def login(self, account: Account | None = None, *, password: str | None = None) -> str:
        """Log in over HTTP and return the *access* token (never logged)."""
        target = account or self.app.account
        response = await self.client.post(
            self.url("/auth/login"),
            json={"email": target.email, "password": password or target.password},
        )
        assert response.status_code == 200, response.text
        return response.json()["access_token"]


# --------------------------------------------------------------------------- #
# op builders - the wire shape of one push op (spec §3)
# --------------------------------------------------------------------------- #
def folder_create(
    entity_id: uuid.UUID | None = None, *, name: str = "Folder", parent_id: uuid.UUID | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": name}
    if parent_id is not None:
        payload["parent_id"] = str(parent_id)
    return op("folder", "create", entity_id, payload)


def note_create(
    entity_id: uuid.UUID | None = None,
    *,
    title: str = "Note",
    body_md: str = "Body",
    folder_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": title, "body_md": body_md}
    if folder_id is not None:
        payload["folder_id"] = str(folder_id)
    return op("note", "create", entity_id, payload)


def note_update(
    entity_id: uuid.UUID, base_rev: int, *, title: str = "Note", body_md: str = "Body"
) -> dict[str, Any]:
    body = op("note", "update", entity_id, {"title": title, "body_md": body_md})
    body["base_rev"] = str(base_rev)
    return body


def note_move(entity_id: uuid.UUID, base_rev: int, folder_id: uuid.UUID | None) -> dict[str, Any]:
    body = op(
        "note", "move", entity_id, {"folder_id": None if folder_id is None else str(folder_id)}
    )
    body["base_rev"] = str(base_rev)
    return body


def folder_move(entity_id: uuid.UUID, base_rev: int, parent_id: uuid.UUID | None) -> dict[str, Any]:
    body = op(
        "folder", "move", entity_id, {"parent_id": None if parent_id is None else str(parent_id)}
    )
    body["base_rev"] = str(base_rev)
    return body


def folder_update(entity_id: uuid.UUID, base_rev: int, *, name: str) -> dict[str, Any]:
    body = op("folder", "update", entity_id, {"name": name})
    body["base_rev"] = str(base_rev)
    return body


def note_delete(entity_id: uuid.UUID, base_rev: int) -> dict[str, Any]:
    body = op("note", "delete", entity_id, {})
    body["base_rev"] = str(base_rev)
    return body


def folder_delete(entity_id: uuid.UUID, base_rev: int) -> dict[str, Any]:
    body = op("folder", "delete", entity_id, {})
    body["base_rev"] = str(base_rev)
    return body


def op(
    entity_type: str, name: str, entity_id: uuid.UUID | None, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """One wire op; ``op_id`` is fresh unless the caller overwrites it."""
    return {
        "op_id": str(uuid.uuid4()),
        "entity_type": entity_type,
        "op": name,
        "entity_id": str(entity_id or uuid.uuid4()),
        "payload": dict(payload),
    }


# --------------------------------------------------------------------------- #
# determinism helpers
# --------------------------------------------------------------------------- #
async def poll(
    predicate: Callable[[], bool | Awaitable[bool]],
    *,
    limit_s: float = 20.0,
    interval: float = 0.02,
    what: str = "condition",
) -> None:
    """Wait until ``predicate`` reports true, or fail naming what never happened.

    Used instead of ``sleep`` so a slow machine only waits longer, and a broken
    invariant fails quickly with a message that says which state was never reached.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while True:
        outcome = predicate()
        if isinstance(outcome, Awaitable):
            outcome = await outcome
        if outcome:
            return
        if loop.time() >= deadline:
            pytest.fail(f"timed out after {limit_s:.1f}s waiting for {what}", pytrace=False)
        await asyncio.sleep(interval)


@asynccontextmanager
async def workspace_lock(
    connection: asyncpg.Connection, workspace_id: uuid.UUID
) -> AsyncIterator[None]:
    """Hold the kernel's own workspace row lock in an uncommitted transaction.

    The kernel takes ``SELECT ... FOR UPDATE`` on ``workspaces`` first, so holding
    it here blocks the *next* write at exactly the step a concurrent push would
    reach - without any production hook or monkeypatched step.
    """
    await connection.execute("BEGIN")
    try:
        await connection.fetchrow(
            "SELECT id FROM workspaces WHERE id = $1 FOR UPDATE", workspace_id
        )
        yield
    finally:
        await connection.execute("ROLLBACK")


@asynccontextmanager
async def row_lock(
    connection: asyncpg.Connection, statement: str, *args: Any
) -> AsyncIterator[None]:
    """Hold a lock on one row (``SELECT ... FOR UPDATE``) until the block exits.

    Used to block one step of an in-flight op while an earlier op proceeds: the
    workspace row itself is uncontended, so this is the only way to make "this op is
    mid-transaction" observable without guessing timings.
    """
    await connection.execute("BEGIN")
    try:
        await connection.fetchrow(statement, *args)
        yield
    finally:
        await connection.execute("ROLLBACK")


async def count_waiting_backends(
    connection: asyncpg.Connection, *, blocked_by: int | None = None
) -> int:
    """Number of *other* backends currently waiting for a lock.

    ``pg_blocking_pids`` is the only reliable view here: a row lock held by
    ``SELECT ... FOR UPDATE`` shows up in ``pg_locks`` as a **transactionid** entry
    (with no ``relation``), so a relation-joined query would report nothing while the
    write is visibly stuck. With ``blocked_by`` the count is narrowed to the sessions
    that one specific backend is blocking, which is what a test needs to say "this
    push is parked on the row *I* hold".
    """
    if blocked_by is None:
        return int(
            await connection.fetchval(
                "SELECT count(*) FROM pg_stat_activity"
                " WHERE datname = current_database()"
                " AND cardinality(pg_blocking_pids(pid)) > 0"
            )
        )
    return int(
        await connection.fetchval(
            "SELECT count(*) FROM pg_stat_activity"
            " WHERE datname = current_database()"
            " AND $1 = any(pg_blocking_pids(pid))",
            blocked_by,
        )
    )


async def backend_pid(connection: asyncpg.Connection) -> int:
    """This connection's own backend pid (the blocker a case waits on)."""
    return int(await connection.fetchval("SELECT pg_backend_pid()"))


async def wait_until_blocked_by(
    connection: asyncpg.Connection, blocker: int, *, limit_s: float = 20.0
) -> None:
    """Wait until some backend is blocked **by ``blocker``** (a server-side fact)."""
    await poll(
        lambda: count_waiting_backends(connection, blocked_by=blocker),
        limit_s=limit_s,
        what=f"a backend waiting on a lock held by pid {blocker}",
    )


# --------------------------------------------------------------------------- #
# construction
# --------------------------------------------------------------------------- #
async def _build(
    overrides: Mapping[str, Any] | None,
) -> tuple[AppUnderTest, Any]:
    """Shared body of both entry points: settings, app, engine, seeded account."""
    database_url = require_database_url()
    settings = make_settings_shared(database_url, **dict(overrides or {}))
    app = build_api_app(settings)
    # ``NullPool`` + ``hide_parameters``: one real connection per checkout (so a
    # concurrency case cannot share a pooled one) and no bound-parameter dump, which
    # matters because a failing assertion here is the most likely place to print a
    # seeded password hash or note body.
    engine = create_async_engine(
        database_url, poolclass=pool.NullPool, hide_parameters=True, future=True
    )
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    account = await seed_account(factory)
    under_test = AppUnderTest(
        app=app,
        settings=settings,
        account=account,
        database_url=database_url,
        factory=factory,
        db=DatabaseProbe(database_url),
    )
    return under_test, engine


@asynccontextmanager
async def open_app(
    *, overrides: Mapping[str, Any] | None = None, schema: bool = False
) -> AsyncIterator[AppUnderTest]:
    """App + seeded account, **without** a client or a lifespan.

    The caller owns the lifecycle: a transport test serves this app with
    :func:`pg_support.socket_probe.open_app_probe`, which runs the real lifespan once.

    ``schema=True`` bootstraps the migrated schema first. It defaults to ``False``
    because the pytest fixtures already requested ``business_schema_at_head`` (a
    session-scoped fixture this plain context manager cannot see); passing ``True``
    is for a script or a case that calls the harness outside a fixture.
    """
    if schema:
        _require_schema()
    await dispose_engines()
    _reset_engine_state()
    under_test, engine = await _build(overrides)
    try:
        yield under_test
    finally:
        _close_app_service(under_test.app)
        await under_test.close()
        await engine.dispose()
        await dispose_engines()
        _reset_engine_state()


def _require_schema() -> None:
    """Run the real Alembic chain for a harness used outside the pytest fixtures."""
    from pg_support.pg_env import SchemaBootstrapError, bootstrap_business_schema

    try:
        bootstrap_business_schema(require_database_url())
    except SchemaBootstrapError as exc:  # pragma: no cover - configuration error
        pytest.fail(str(exc), pytrace=False)


@asynccontextmanager
async def open_api(
    *,
    overrides: Mapping[str, Any] | None = None,
    login: bool = True,
    schema: bool = False,
) -> AsyncIterator[ApiHarness]:
    """Fresh app + ASGI client + seeded owner + access token, on real PostgreSQL.

    One app per test: ``app.state`` carries the settings, the error handlers and the
    app-scoped ``AuthService`` (and therefore its Argon2 concurrency limiter). The
    DSN-keyed engine caches are cleared on both sides, so no pool or cached
    sessionmaker survives into the next test.

    ``schema=True`` bootstraps the migrated schema first; see :func:`open_app` for why
    it is off by default.
    """
    if schema:
        _require_schema()
    await dispose_engines()
    _reset_engine_state()
    under_test, engine = await _build(overrides)
    # ``raise_app_exceptions=False``: an unhandled error must arrive as the real 500
    # envelope (fixed body + X-Request-ID), exactly as production would answer. With
    # the default ``True`` httpx re-raises it inside the test instead, so the error
    # surface and the leak guards would never see a response at all.
    transport = httpx.ASGITransport(app=under_test.app, raise_app_exceptions=False)
    service = _build_auth_service(under_test)
    under_test.app.state.auth_service = service
    try:
        async with (
            under_test.app.router.lifespan_context(under_test.app),
            httpx.AsyncClient(
                transport=transport, base_url="http://api.test", timeout=60.0
            ) as client,
        ):
            harness = ApiHarness(app=under_test, client=client, token="")
            if login:
                harness.token = await harness.login()
            yield harness
    finally:
        service.close()
        await under_test.close()
        await engine.dispose()
        await dispose_engines()
        _reset_engine_state()


def _build_auth_service(under_test: AppUnderTest) -> Any:
    """The app-scoped ``AuthService``, built eagerly so its limiter can be closed.

    The dependency builds it lazily on first request; constructing it here means the
    fixture owns the object that holds worker threads, and teardown can shut them
    down instead of leaking one pool per test. It shares the app's DSN-keyed
    sessionmaker, so the app and the fixture use the same factory.
    """
    from revnote_api.db.session import get_sessionmaker
    from revnote_api.services.auth import AuthService

    return AuthService(get_sessionmaker(under_test.settings), under_test.settings)


def _close_app_service(app: FastAPI) -> None:
    """Shut down the app-scoped auth limiter, if a request built one."""
    service = getattr(app.state, "auth_service", None)
    close = getattr(service, "close", None)
    if callable(close):
        close()


def _reset_engine_state() -> None:
    """Drop the process-global engine/sessionmaker caches (see ``tests/conftest``).

    ``db.session.get_sessionmaker`` caches by DSN, and ``db.engine`` keeps every
    engine it made for the lifespan to dispose. A test app must not inherit the
    previous test's pool, so the caches are cleared on both sides of a harness.
    """
    from revnote_api.db.session import reset_session_state

    reset_session_state()
