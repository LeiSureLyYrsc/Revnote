"""FastAPI application factory.

Scaffold contract:

* ``create_app()`` returns a fully configured app without touching PostgreSQL,
  MinIO or any other external service — import and startup stay side-effect
  free (the engine is lazy, see :mod:`revnote_api.db.engine`).
* The lifespan disposes the async engine on shutdown so ``uvicorn`` exits
  cleanly without dangling pooled connections.
* Only liveness routes are mounted: ``GET /`` and ``GET /health``.
  No CRUD, sync, auth or Joplin endpoints exist in this scaffold.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from revnote_api._version import __version__
from revnote_api.api import router
from revnote_api.config import Settings, get_settings
from revnote_api.db.engine import dispose_engines
from revnote_api.db.session import reset_session_state

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup/shutdown hooks — no external connections are opened here."""
    settings: Settings = app.state.settings
    logger.info(
        "starting revnote-api %s (env=%s, db=%s)",
        __version__,
        settings.environment,
        settings.masked_database_url,
    )
    try:
        yield
    finally:
        # Best-effort: dispose pooled DB connections even if shutdown is noisy.
        reset_session_state()
        try:
            await dispose_engines()
        except Exception:  # pragma: no cover - shutdown must never raise
            logger.exception("failed to dispose async engine on shutdown")
        logger.info("stopped revnote-api")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application (factory used by uvicorn/tests)."""
    settings = settings or get_settings()

    app = FastAPI(
        title="Revnote API",
        version=__version__,
        summary="Revnote sync service — scaffold (liveness routes only)",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.include_router(router)
    return app


__all__ = ["create_app", "lifespan"]
