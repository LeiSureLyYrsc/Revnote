"""FastAPI application factory.

Contract:

* ``create_app()`` returns a fully configured app without touching PostgreSQL,
  an object store or any other external service — import and startup stay side-effect
  free (the engine is lazy, see :mod:`revnote_api.db.engine`).
* The lifespan disposes the async engine on shutdown so ``uvicorn`` exits
  cleanly without dangling pooled connections.
* Mounted routes come from exactly one place, :mod:`revnote_api.api.router`:
  ``GET /`` and ``GET /health`` today, versioned ``/api/v1`` groups as they land.
* Every response carries ``X-Request-ID``; every failure - including a 404 on an
  unknown path and an unclassified 500 - uses the fixed error envelope
  (:mod:`revnote_api.api.errors`).
* Request bodies are bounded by bytes actually received
  (:mod:`revnote_api.api.middleware.request_limits`).
* ``app.state.settings`` carries the configuration: handlers read it from the
  request instead of a process-global, so two apps with different settings can
  coexist in one test process.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi

from revnote_api._version import __version__
from revnote_api.api.errors import install_error_handlers
from revnote_api.api.middleware.request_id import RequestIdMiddleware
from revnote_api.api.middleware.request_limits import BodyLimitMiddleware
from revnote_api.api.middleware.safe_logging import (
    RouteTemplateMiddleware,
    install_log_redaction,
)
from revnote_api.api.router import build_router
from revnote_api.config import Settings, get_settings
from revnote_api.db.engine import dispose_engines
from revnote_api.db.session import reset_session_state

logger = logging.getLogger(__name__)

#: Name of the one security scheme (``Authorization: Bearer <access token>``).
BEARER_SCHEME_NAME = "BearerAuth"

#: Headers a browser client may send / read. Kept narrow on purpose: CORS is a
#: browser concession, never an authorization boundary, and credentials are off.
_CORS_ALLOW_HEADERS = ("Authorization", "Content-Type", "X-Request-ID")
_CORS_EXPOSE_HEADERS = ("X-Request-ID", "Retry-After")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
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
        auth_service = getattr(app.state, "auth_service", None)
        if auth_service is not None:
            auth_service.close()
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
        summary="Revnote sync service — v1 contract (auth + native sync)",
        lifespan=lifespan,
        # Endpoints opt into bearer auth with ``dependencies=[Security(...)]``; the
        # scheme itself is declared once here so clients can be generated now.
        swagger_ui_parameters={"persistAuthorization": True},
    )
    app.state.settings = settings

    # Before any request can be logged: the uvicorn access line and the server's
    # exception line are the two sinks that would otherwise print a raw query
    # string and a SQL-bearing traceback. Narrow filters, no global disable.
    install_log_redaction()

    install_error_handlers(app)
    app.include_router(build_router(settings))
    if settings.joplin_bridge_enabled:
        # The bridge has its own envelope ({"error", "code"}); registering its
        # handler here keeps every Joplin-shaped concern inside the adapter.
        from revnote_api.adapters.joplin.routes import install as install_joplin_bridge

        install_joplin_bridge(app)

    # Order matters. Starlette runs middleware in reverse registration order for
    # the request, so the *last* added is outermost: request id must be outermost
    # so a 413 from the body limiter still carries an id.
    # ``allow_credentials`` stays False: tokens travel in the Authorization
    # header, never in a cookie, so there is nothing for the browser to attach.
    if settings.cors_allow_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allow_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=list(_CORS_ALLOW_HEADERS),
            expose_headers=list(_CORS_EXPOSE_HEADERS),
            max_age=600,
        )
    app.add_middleware(
        BodyLimitMiddleware,
        # The push budget is configuration (``SYNC_PUSH_MAX_BYTES``, D9), not a
        # constant: the middleware default exists only for standalone use.
        push_limit=settings.sync_push_max_bytes,
        # Phase 5: the upload path gets its own configured budget. Passing the
        # same number the storage write enforces keeps "allowed by the limiter,
        # refused by the store" impossible.
        attachment_limit=settings.attachment_max_bytes,
    )
    app.add_middleware(RequestIdMiddleware)
    # Outermost: it reads ``scope["state"]["request_id"]`` (populated by the
    # middleware it wraps) and the matched route, then publishes both for the log
    # filters. Must be added last to sit outside everything it observes.
    app.add_middleware(RouteTemplateMiddleware)

    # FastAPI treats ``openapi`` as a method; replacing it is the documented way
    # to extend the generated document. ``cast`` keeps the checker honest about
    # the fact that this is a deliberate attribute override, not an accident.
    cast("Any", app).openapi = _contract_openapi(app)
    return app


def _contract_openapi(app: FastAPI) -> Callable[[], dict[str, Any]]:
    """OpenAPI generator that publishes the cross-cutting contract pieces.

    Route-level schemas appear on their own; the shared envelope and the bearer
    scheme do not (no endpoint references them yet), so they are added here. Doing
    it explicitly - rather than leaving unused models in a hidden corner - means a
    generated client gets the error shape and the auth scheme from day one, which
    is the whole point of publishing a contract before the endpoints exist.
    """
    from revnote_api.api.schemas.common import ErrorBody, ErrorEnvelope

    def custom() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
        )
        components = schema.setdefault("components", {})
        definitions = components.setdefault("schemas", {})
        for model in (ErrorBody, ErrorEnvelope):
            definitions.setdefault(model.__name__, model.model_json_schema())
        components.setdefault("securitySchemes", {})[BEARER_SCHEME_NAME] = {
            "type": "http",
            "scheme": "bearer",
            "description": (
                "Opaque access token issued by POST /api/v1/auth/login. "
                "Send it as 'Authorization: Bearer <token>'. Access tokens are "
                "short-lived (AUTH_ACCESS_TTL_S); refresh with the rotating "
                "refresh token instead of reusing an expired one. Revocation is "
                "immediate: every request re-checks the token, its session and "
                "the workspace membership."
            ),
        }
        app.openapi_schema = schema
        return schema

    return custom


__all__ = ["BEARER_SCHEME_NAME", "create_app", "lifespan"]
