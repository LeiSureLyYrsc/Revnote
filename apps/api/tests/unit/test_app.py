"""HTTP tests for the scaffold: hello + liveness must work with no services.

The tests run the *real* lifespan (startup and shutdown), so they also prove
that booting and stopping the app never requires PostgreSQL or MinIO.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

from revnote_api import __version__
from revnote_api.config import Settings
from revnote_api.main import create_app

pytestmark = pytest.mark.anyio


async def test_root_hello(client: AsyncClient) -> None:
    response = await client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body["service"] == "revnote-api"
    assert "Hello from Revnote API" in body["message"]
    assert body["health"] == "/health"
    assert body["docs"] == "/docs"


async def test_health_is_json_ok(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "status": "ok",
        "service": "revnote-api",
        "version": __version__,
        "environment": "local",
    }


async def test_health_reports_configured_environment(make_client) -> None:
    settings = Settings(_env_file=None, environment="production")

    async with make_client(settings) as http:
        response = await http.get("/health")

    assert response.status_code == 200
    assert response.json()["environment"] == "production"


async def test_health_up_while_database_unreachable(make_client) -> None:
    """A dead DSN must not affect liveness (health never touches the DB)."""
    unreachable = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://nobody:nobody@127.0.0.1:1/nowhere",
    )

    async with make_client(unreachable) as http:
        response = await http.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


async def test_unknown_path_is_404(client: AsyncClient) -> None:
    assert (await client.get("/nope")).status_code == 404


async def test_openapi_exposes_only_scaffold_routes(client: AsyncClient) -> None:
    """Guardrail: the scaffold ships no business/auth/sync endpoints."""
    schema = (await client.get("/openapi.json")).json()

    assert set(schema["paths"]) == {"/", "/health"}
    assert schema["info"]["title"] == "Revnote API"


def test_create_app_is_side_effect_free(settings: Settings) -> None:
    """Factory returns an app and keeps the settings on app.state."""
    app = create_app(settings)

    assert app.state.settings is settings
    assert app.title == "Revnote API"
    assert app.version == __version__


async def test_lifespan_runs_twice_cleanly(settings: Settings) -> None:
    """Repeated startup/shutdown cycles (e.g. reload) must not raise."""
    app = create_app(settings)

    for _ in range(2):
        async with app.router.lifespan_context(app):
            pass
