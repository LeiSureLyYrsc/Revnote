"""Shared pytest fixtures for the Revnote API scaffold.

* The **unit** tests never touch a network service: ``create_app()`` boots with
  defaults, so ``GET /`` and ``GET /health`` pass with PostgreSQL / the object store absent.
* ``TEST_DATABASE_URL`` gates the PostgreSQL integration tests:
  - unset -> skipped (no local server required);
  - set   -> they **must** connect; a connection error is a hard failure and is
    never turned into a skip.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from revnote_api.config import Settings, get_settings
from revnote_api.main import create_app

TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"

ClientFactory = Callable[[Settings], "AsyncIterator[AsyncClient]"]


@asynccontextmanager
async def app_client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """Build an app, run its lifespan, and expose an in-process HTTP client."""
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http,
    ):
        yield http


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """Run async tests on asyncio only (asyncpg has no trio story)."""
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_settings_cache() -> AsyncIterator[None]:
    """Reset the settings singleton around every test (no cross-test leakage)."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def test_database_url() -> str | None:
    """Raw value of ``TEST_DATABASE_URL`` (``None`` when unset or blank)."""
    value = os.environ.get(TEST_DATABASE_URL_ENV, "").strip()
    return value or None


@pytest.fixture
def settings() -> Settings:
    """Settings built from an explicit, empty env — ignores any local ``.env``.

    ``_env_file=None`` keeps unit results deterministic on machines that do
    have a compose ``.env`` present.
    """
    return Settings(_env_file=None)


@pytest.fixture
def make_client() -> ClientFactory:
    """Factory for clients bound to *custom* settings (lifespan included)."""
    return app_client


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    """AsyncClient bound to a fresh app, with the real lifespan running."""
    async with app_client(settings) as http:
        yield http
