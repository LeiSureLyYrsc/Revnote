"""Auth/API fixture foundation for ``tests/api``: one app, real PG, real Alembic schema.

This module is the single owner of "build an app and seed an account". Everything
else in the suite (``api_harness``, the transport cases) builds on top of it rather
than assembling a second app, because the details that matter here are exactly the
ones that silently drift: which routers are mounted, whether the schema exists, how
big the pool is, and whether a secret can reach a log.

Rules baked in
--------------

* **Real PostgreSQL, never skip.** The DSN comes from ``TEST_DATABASE_URL``, the same
  contract the kernel tests use. A missing or malformed URL is a hard ``pytest.fail``:
  a skipped auth suite is indistinguishable from a passing one in CI, and SQLite
  cannot represent this schema.
* **The schema is migrated, not created.** ``business_schema_at_head`` runs the real
  Alembic CLI once per session (:mod:`pg_support.pg_env`). ``create_all()`` would
  prove only that the ORM can build *a* schema - it skips the migration chain, its
  ordering, its backfills and its constraint names, which is what the suite asserts.
  A fixture that merely pointed at whatever tables happened to exist would pass on a
  developer's machine and fail on a clean CI database.
* **Production wiring only.** The app is exactly ``create_app(settings)``: no private
  ``/api/v1`` sub-router, no second registration surface. ``api/router.py`` mounts
  every group, so mounting one again would register a duplicate route and quietly
  exercise a different handler than production serves. ``build_app`` is kept as a
  named seam for the transport tier, but it delegates.
* **One app per test, hermetic engine state.** The fixture disposes engines and
  clears the sessionmaker cache on both sides of the test, so a previous test's pool,
  transaction or settings can never influence the next one, and no connection bound
  to a finished event loop is ever reused.
* **Prehashed credentials.** Argon2 at the locked cost (64 MiB, t3, p4) costs
  ~50-100 ms per call, so :func:`seed_account` inserts an *already hashed* password
  using the production hasher. The locked parameters are never lowered.
* **Failures are inspectable.** ``ASGITransport`` is built with
  ``raise_app_exceptions=False`` so an unhandled exception arrives as the real 500
  envelope (with its fixed body and ``X-Request-ID``) instead of a traceback in the
  middle of a test. That is what a client sees, and it is what the leak tests need to
  examine.

Public API (stable - other lanes may rely on these names and signatures)
----------------------------------------------------------------------
Fixtures:  ``auth_app``, ``open_registration_app``, ``business_schema_at_head``
Builders:  ``build_app(settings)``, ``build_auth_app(settings)`` (alias),
           ``require_database_url()``, ``make_settings(...)``
Seeders:   ``seed_account(...)``, ``seed_user(...)``, ``seed_workspace_for(...)``,
           ``seed_member(...)``
Records:   ``Account``, ``SeededUser``, ``AuthTestApp``
Constants: ``DEFAULT_PASSWORD``, ``TEST_DATABASE_URL_ENV``, ``API_V1``,
           ``NO_WORKSPACE``, ``OWNER``
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import pytest
from httpx import ASGITransport, AsyncClient, Response
from sqlalchemy import pool, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from pg_support.pg_env import (
    PgTargets,
    SchemaBootstrapError,
    bootstrap_business_schema,
    require_targets,
)
from revnote_api.config import Settings
from revnote_api.db.engine import dispose_engines
from revnote_api.db.session import get_sessionmaker, reset_session_state
from revnote_api.main import create_app
from revnote_api.security.passwords import hash_password
from revnote_api.services.auth import AuthService

__all__ = [
    "API_V1",
    "DEFAULT_PASSWORD",
    "NO_WORKSPACE",
    "OWNER",
    "TEST_DATABASE_URL_ENV",
    "Account",
    "AuthTestApp",
    "SeededUser",
    "auth_app",
    "build_app",
    "build_auth_app",
    "business_schema_at_head",
    "make_settings",
    "open_registration_app",
    "pg_targets",
    "require_database_url",
    "seed_account",
    "seed_member",
    "seed_user",
    "seed_workspace_for",
]

#: Business database: the one that carries the migrated schema.
TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"

#: Version prefix every business route lives under (``api.router.API_V1_PREFIX``).
API_V1 = "/api/v1"

#: A password that satisfies the policy and is reused across tests. One digest
#: covers the common case, because hashing is the expensive part of a test.
DEFAULT_PASSWORD = "correct horse battery staple"

#: ``role`` values a membership may carry in v1 (mirrors the schema CHECK).
OWNER = "owner"

SessionFactory = async_sessionmaker[AsyncSession]

#: Pool sizing for the app under test. A concurrency case holds several requests at
#: once and each op takes one connection for the membership re-check and another for
#: its transaction, so the pool must not be the scaffold's small default: a
#: ``pool_timeout`` under contention would look exactly like a deadlock.
APP_POOL_SIZE = 10
APP_MAX_OVERFLOW = 20


# --------------------------------------------------------------------------- #
# environment gate
# --------------------------------------------------------------------------- #
def require_database_url() -> str:
    """Return the validated business ``TEST_DATABASE_URL`` or fail the run.

    Delegates to :mod:`pg_support.pg_env` so this package and the kernel suite share
    one definition of "a usable DSN", then translates its error into a pytest failure
    with no traceback (the message is the whole diagnostic).
    """
    try:
        return require_targets().database_url
    except RuntimeError as exc:
        pytest.fail(str(exc), pytrace=False)


def _fail_on_schema_error(exc: Exception) -> None:
    """Turn a bootstrap failure into a test failure, keeping Alembic's output."""
    pytest.fail(str(exc), pytrace=False)


# --------------------------------------------------------------------------- #
# records returned by the seeders
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SeededUser:
    """A user row with no workspace attached (used for non-member cases)."""

    user_id: uuid.UUID
    email: str
    password: str

    def __repr__(self) -> str:  # never render the password
        return f"SeededUser(user_id={self.user_id!r}, email={self.email!r})"


#: Sentinel for "this account has no workspace" (all-zero UUID, never generated).
NO_WORKSPACE = uuid.UUID(int=0)


@dataclass(frozen=True, slots=True)
class Account(SeededUser):
    """A user plus its own workspace, where it is the owner (v1 default)."""

    workspace_id: uuid.UUID = NO_WORKSPACE
    role: str = OWNER

    def __repr__(self) -> str:  # never render the password
        return (
            f"Account(user_id={self.user_id!r}, email={self.email!r}, "
            f"workspace_id={self.workspace_id!r}, role={self.role!r})"
        )


# --------------------------------------------------------------------------- #
# the app bundle
# --------------------------------------------------------------------------- #
@dataclass
class AuthTestApp:
    """One app, its client, its settings and the handles to seed/assert.

    ``app`` is exposed because a transport test needs the ASGI callable itself
    (E runs it under real uvicorn); everything else here is convenience.
    """

    app: object
    client: AsyncClient
    settings: Settings
    #: Factory over a dedicated ``NullPool`` engine owned by this fixture.
    factory: SessionFactory
    #: The app-scoped service, for direct service-level tests.
    service: AuthService
    #: Allocates *additional* independent factories (HTTP concurrency tests).
    new_factory: Callable[[], SessionFactory]
    _engines: list[AsyncEngine] = field(default_factory=list, repr=False)

    # -- request helpers --------------------------------------------------- #
    @staticmethod
    def bearer(token: str) -> dict[str, str]:
        """``Authorization: Bearer <token>`` header for ``httpx`` calls."""
        return {"Authorization": f"Bearer {token}"}

    async def login(
        self,
        email: str,
        password: str = DEFAULT_PASSWORD,
        *,
        device_name: str | None = None,
    ) -> Response:
        """POST ``/api/v1/auth/login``; returns the raw response for assertions."""
        payload: dict[str, str] = {"email": email, "password": password}
        if device_name is not None:
            payload["device_name"] = device_name
        return await self.client.post(f"{API_V1}/auth/login", json=payload)

    async def login_token(
        self,
        account: SeededUser | str,
        password: str | None = None,
        *,
        device_name: str | None = None,
    ) -> str:
        """Log in and return the access token, failing the test on a non-200.

        ``account`` may be a seeded record (its own password is used) or a bare
        e-mail (``password`` then defaults to :data:`DEFAULT_PASSWORD`).
        """
        email = account.email if isinstance(account, SeededUser) else account
        secret = password or (account.password if isinstance(account, SeededUser) else None)
        response = await self.login(
            email, DEFAULT_PASSWORD if secret is None else secret, device_name=device_name
        )
        assert response.status_code == 200, response.text
        return str(response.json()["access_token"])

    async def login_pair(
        self,
        account: SeededUser | str,
        password: str | None = None,
        *,
        device_name: str | None = None,
    ) -> dict[str, object]:
        """Log in and return the whole token-pair body (access *and* refresh)."""
        email = account.email if isinstance(account, SeededUser) else account
        secret = password or (account.password if isinstance(account, SeededUser) else None)
        response = await self.login(
            email, DEFAULT_PASSWORD if secret is None else secret, device_name=device_name
        )
        assert response.status_code == 200, response.text
        return dict(response.json())


# --------------------------------------------------------------------------- #
# app construction
# --------------------------------------------------------------------------- #
def build_app(settings: Settings):
    """The production application: ``create_app`` and nothing else.

    ``api/router.py`` is the single registration surface and mounts every versioned
    group, so this helper deliberately adds no router. Re-mounting one here - even on
    a private sub-router - would register a duplicate path and test a handler
    production never reaches; FastAPI would also warn about duplicate operation ids.
    """
    return create_app(settings)


#: Backwards-compatible name: the old helper mounted the auth routers itself.
#: Keeping the alias means no caller has to change, while there is only one builder.
build_auth_app = build_app


def make_settings(database_url: str, **overrides: object) -> Settings:
    """Settings for one test app: explicit DSN, no ``.env``, concurrency-sized pool.

    ``_env_file=None`` keeps a developer's local ``.env`` out of the run. The pool is
    widened here rather than per test so an HTTP concurrency case cannot be
    serialised by the scaffold's small default and look like a lock bug.
    """
    options: dict[str, object] = {
        "db_pool_size": APP_POOL_SIZE,
        "db_max_overflow": APP_MAX_OVERFLOW,
    }
    options.update(overrides)
    return Settings(_env_file=None, database_url=database_url, **options)  # type: ignore[arg-type]


def _new_engine(url: str) -> AsyncEngine:
    """A dedicated ``NullPool`` engine for seeding and direct assertions.

    ``NullPool`` gives each checkout a real connection and never recycles one, which
    is what makes the concurrency cases honest: two factories cannot accidentally
    share a pooled connection. ``hide_parameters`` is mandatory here - a seeded
    password hash and a note body are bound parameters, and this engine is the one a
    failing assertion is most likely to dump.
    """
    return create_async_engine(
        url,
        poolclass=pool.NullPool,
        hide_parameters=True,
        future=True,
    )


async def _variant(*, registration_open: bool) -> AsyncIterator[AuthTestApp]:
    """Build one app for a given registration policy (shared by both fixtures).

    Reset/teardown order matters:

    1. clear any previous sessionmaker/engine state, so this test starts clean;
    2. build the app and the service (same DSN-keyed factory the app will use);
    3. run the real lifespan, which is what a client would experience;
    4. on the way out, close the limiter, dispose every engine this fixture made,
       then reset the global caches again so the next test cannot inherit them.
    """
    url = require_database_url()

    await dispose_engines()
    reset_session_state()

    settings = make_settings(url, auth_registration_open=registration_open)
    app = build_app(settings)

    # Built eagerly (rather than lazily on first request) so a test may call the
    # service directly without depending on a request having happened first.
    service = AuthService(get_sessionmaker(settings), settings)
    app.state.auth_service = service

    engines: list[AsyncEngine] = []

    def new_factory() -> SessionFactory:
        engine = _new_engine(url)
        engines.append(engine)
        return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

    seeding_factory = new_factory()
    # ``raise_app_exceptions=False``: an unhandled error must arrive as the real 500
    # envelope (fixed body + X-Request-ID) that production would send. With the
    # default ``True`` httpx re-raises it instead, so the very tests that check the
    # error surface and the leak guards would never see a response.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with (
            app.router.lifespan_context(app),
            AsyncClient(transport=transport, base_url="http://api.test", timeout=60.0) as client,
        ):
            yield AuthTestApp(
                app=app,
                client=client,
                settings=settings,
                factory=seeding_factory,
                service=service,
                new_factory=new_factory,
                _engines=engines,
            )
    finally:
        service.close()
        for engine in engines:
            await engine.dispose()
        await dispose_engines()
        reset_session_state()


# --------------------------------------------------------------------------- #
# session-scoped schema and target fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def pg_targets() -> PgTargets:
    """Both validated DSNs, proven to address different databases.

    Session-scoped and synchronous on purpose: it must not capture a running event
    loop, because nothing here needs one and a session-scoped async fixture would bind
    its connection to whichever loop happened to be first.
    """
    try:
        return require_targets()
    except RuntimeError as exc:
        _fail_on_schema_error(exc)
        raise  # pragma: no cover - pytest.fail always raises


@pytest.fixture(scope="session")
def business_schema_at_head(pg_targets: PgTargets) -> str:
    """Run the real Alembic chain once for the whole session; return the revision.

    This is the fixture the whole API suite hangs off. Without it a run either fails
    on a missing table or, worse, passes against whatever schema a previous session
    left behind. It is a **sync** fixture that shells out to the CLI, so the migration
    runs off the event loop (``env.py`` calls ``asyncio.run`` itself) and no test loop
    is involved.
    """
    try:
        return bootstrap_business_schema(pg_targets.database_url)
    except SchemaBootstrapError as exc:
        _fail_on_schema_error(exc)
        raise  # pragma: no cover - pytest.fail always raises


@pytest.fixture
async def auth_app(
    business_schema_at_head: str,
) -> AsyncIterator[AuthTestApp]:
    """App with ``AUTH_REGISTRATION_OPEN=false`` (the deployment default, D6)."""
    del business_schema_at_head  # dependency: the schema must be migrated first
    async for bundle in _variant(registration_open=False):
        yield bundle


@pytest.fixture
async def open_registration_app(
    business_schema_at_head: str,
) -> AsyncIterator[AuthTestApp]:
    """App with ``AUTH_REGISTRATION_OPEN=true`` (registration tests only)."""
    del business_schema_at_head
    async for bundle in _variant(registration_open=True):
        yield bundle


# --------------------------------------------------------------------------- #
# seeding
# --------------------------------------------------------------------------- #
async def seed_user(
    factory: SessionFactory,
    *,
    email: str | None = None,
    password: str = DEFAULT_PASSWORD,
    password_hash: str | bool | None = True,
    disabled: bool = False,
) -> SeededUser:
    """Insert one ``users`` row and nothing else.

    Use this for "a user who is *not* a member of that workspace": no workspace is
    created, so a later membership check has nothing to find.

    ``password_hash``: ``True`` (default) hashes ``password`` with the production
    hasher, ``False``/``None`` stores ``NULL`` (a pre-Phase-3 account, D8), and a
    ``str`` is inserted verbatim (for rehash tests).
    """
    user_id = uuid.uuid4()
    address = email or f"user-{user_id.hex}@revnote.test"

    if password_hash is True:
        digest: str | None = hash_password(password)
    elif password_hash is False:
        digest = None
    else:
        digest = password_hash

    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, email, password_hash, disabled_at) VALUES "
                "(:id, :email, :digest, CASE WHEN :disabled THEN now() ELSE NULL END)"
            ),
            {"id": user_id, "email": address, "digest": digest, "disabled": disabled},
        )
        await session.commit()

    return SeededUser(user_id=user_id, email=address, password=password)


async def seed_member(
    factory: SessionFactory,
    *,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    role: str = OWNER,
) -> None:
    """Insert one ``workspace_members`` row.

    The pair must not already exist (composite PK) and both sides must exist
    (RESTRICT FKs) - the database says so, and letting it say so keeps the fixture
    honest about the schema.
    """
    async with factory() as session:
        await session.execute(
            text(
                "INSERT INTO workspace_members (workspace_id, user_id, role) "
                "VALUES (:ws, :user, :role)"
            ),
            {"ws": workspace_id, "user": user_id, "role": role},
        )
        await session.commit()


async def seed_workspace_for(
    factory: SessionFactory,
    user_id: uuid.UUID,
    *,
    role: str = OWNER,
    with_membership: bool = True,
    name: str | None = None,
) -> uuid.UUID:
    """Create a workspace owned by ``user_id`` and (by default) its membership.

    ``with_membership=False`` produces the deliberately inconsistent state a legacy
    workspace without a member row would have - useful for asserting that
    authorisation relies on ``workspace_members`` and not on ``owner_id``.
    """
    workspace_id = uuid.uuid4()
    del name  # the schema has no workspace name column; kept for call-site symmetry
    async with factory() as session:
        await session.execute(
            text("INSERT INTO workspaces (id, owner_id, last_seq) VALUES (:id, :owner, 0)"),
            {"id": workspace_id, "owner": user_id},
        )
        if with_membership:
            await session.execute(
                text(
                    "INSERT INTO workspace_members (workspace_id, user_id, role) "
                    "VALUES (:ws, :user, :role)"
                ),
                {"ws": workspace_id, "user": user_id, "role": role},
            )
        await session.commit()
    return workspace_id


async def seed_account(
    factory: SessionFactory,
    *,
    email: str | None = None,
    password: str = DEFAULT_PASSWORD,
    password_hash: str | bool | None = True,
    disabled: bool = False,
    create_workspace: bool = True,
) -> Account:
    """Insert a user plus its own owner workspace: the common test starting point.

    Convenience composition of :func:`seed_user` and :func:`seed_workspace_for`.
    With ``create_workspace=False`` the returned ``workspace_id`` is all-zeroes -
    use :func:`seed_user` instead when no workspace should exist at all.
    """
    user = await seed_user(
        factory,
        email=email,
        password=password,
        password_hash=password_hash,
        disabled=disabled,
    )
    if not create_workspace:
        return Account(
            user_id=user.user_id,
            email=user.email,
            password=user.password,
            workspace_id=NO_WORKSPACE,
            role=OWNER,
        )

    workspace_id = await seed_workspace_for(factory, user.user_id)
    return Account(
        user_id=user.user_id,
        email=user.email,
        password=user.password,
        workspace_id=workspace_id,
        role=OWNER,
    )
