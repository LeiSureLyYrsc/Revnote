"""HTTP routes for the scaffold.

Deliberately tiny: a hello route and a liveness route.

``GET /health`` reports **process liveness only** — it never opens a database
connection, so it keeps returning ``200`` while PostgreSQL / MinIO are down.
Deep dependency checks (DB ping, object-storage ping) belong to a future
``/health/ready`` and must not be mixed into this endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from revnote_api._version import __version__
from revnote_api.config import Settings
from revnote_api.schemas import HealthResponse, RootResponse

router = APIRouter()


@router.get("/", response_model=RootResponse, response_model_exclude_none=True)
async def read_root() -> RootResponse:
    """Hello endpoint — no auth, no database."""
    return RootResponse(
        service="revnote-api",
        message="Hello from Revnote API - scaffold only, no business endpoints yet.",
    )


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    """Liveness probe: confirms the process serves HTTP. No I/O performed."""
    settings: Settings = request.app.state.settings
    return HealthResponse(
        status="ok",
        service="revnote-api",
        version=__version__,
        environment=settings.environment,
    )
