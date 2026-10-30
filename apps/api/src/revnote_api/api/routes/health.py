"""Liveness routes: ``GET /`` and ``GET /health``.

Unchanged behaviour from the scaffold, now living in a package so its siblings
(``auth``, ``sync_push``, ``sync_pull``, ``read``) can be added without fighting
the old ``api/routes.py`` module name.

``GET /health`` reports **process liveness only** - it never opens a database
connection, so it keeps returning ``200`` while PostgreSQL / the object store are down.
Deep dependency checks belong to a future ``/health/ready`` and must not be mixed
into this endpoint. Neither route is versioned: probes must not need to know the
API version.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from revnote_api._version import __version__
from revnote_api.config import Settings
from revnote_api.schemas import HealthResponse, RootResponse

__all__ = ["liveness_router", "router"]

router = APIRouter()


@router.get("/", response_model=RootResponse, response_model_exclude_none=True)
async def read_root() -> RootResponse:
    """Hello endpoint - no auth, no database."""
    return RootResponse(
        service="revnote-api",
        message="Hello from Revnote API.",
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


#: Descriptive alias used by :mod:`revnote_api.api.router` (kept so the module can
#: be imported by name without implying a versioned mount).
liveness_router = router
