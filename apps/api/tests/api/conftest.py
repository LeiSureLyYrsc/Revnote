"""Fixtures for the HTTP API suite (``tests/api``).

The machinery lives in :mod:`pg_support.auth_support` so other lanes can import it
without importing this ``conftest`` - the same rule ``tests/integration`` follows,
and the reason the support module must stay free of ``conftest`` imports.

What is provided here, and where it actually comes from:

======================================  ==========================================
``business_schema_at_head``             session-scoped; runs the **real Alembic CLI**
                                        once (``upgrade head``) and returns the
                                        revision. Every PG fixture depends on it, so
                                        no test can run against an empty or stale
                                        database.
``pg_targets``                          session-scoped pair of validated DSNs,
                                        proven to address two different databases.
``auth_app``                            one app per test, registration closed (D6).
``open_registration_app``               same, registration open (registration cases).
======================================  ==========================================

Both app fixtures also import ``pg_support.api_harness`` helpers on demand; this
module only re-exports, so adding a fixture here would make it invisible to tests
that import the support module directly.

Extension point for later lanes: add lane-specific helpers to ``pg_support/`` and
import them here, rather than defining a second app builder or a second schema
bootstrap - two of either is how a suite ends up testing something production does
not serve.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from pg_support.auth_support import (
    API_V1,
    DEFAULT_PASSWORD,
    TEST_DATABASE_URL_ENV,
    Account,
    AuthTestApp,
    SeededUser,
    auth_app,
    build_app,
    build_auth_app,
    business_schema_at_head,
    make_settings,
    open_registration_app,
    pg_targets,
    require_database_url,
    seed_account,
    seed_member,
    seed_user,
    seed_workspace_for,
)

__all__ = [
    "API_V1",
    "DEFAULT_PASSWORD",
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


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """asyncio only: asyncpg has no trio story."""
    return "asyncio"


@pytest.fixture(autouse=True)
def _api_schema(business_schema_at_head: str) -> str:
    """Ensure the real Alembic chain ran before any test in this package connects.

    Autouse because every module here touches the database. The ``auth_app`` fixtures
    depend on it explicitly as well; this makes that dependency unmissable for the
    modules (push, pull, concurrency, transport) that build their app from
    ``pg_support.api_harness`` instead of taking the bundle. Without it one forgotten
    module fails with an opaque ``UndefinedTable`` - or worse, passes against whatever
    schema a previous session left in the database.
    """
    return business_schema_at_head


@pytest.fixture(autouse=True)
def _reset_engine_caches() -> AsyncIterator[None]:
    """Clear the DSN-keyed engine/sessionmaker caches around every test.

    The harnesses do this themselves, but a test that builds an app directly (or fails
    before the harness body runs) would otherwise leave a pooled engine bound to a
    finished event loop, which surfaces as a timeout in some later, unrelated test.
    """
    from revnote_api.db.session import reset_session_state

    reset_session_state()
    yield
    reset_session_state()
