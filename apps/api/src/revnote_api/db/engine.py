"""Async engine factory.

The engine is created **lazily** and cached, so importing this module (or
booting the app) never requires PostgreSQL to be reachable.

Lifecycle: every engine produced by :func:`get_engine` is remembered in a small
registry. :func:`dispose_engines` (called by the FastAPI lifespan on shutdown)
walks that registry and disposes **all** of them, so switching the DSN at
runtime can never orphan a pooled engine.
"""

from __future__ import annotations

import logging

from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from revnote_api.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: The engine for the currently selected DSN (``None`` until first use).
_engine: AsyncEngine | None = None
_engine_url: str | None = None

#: Every engine this module has created and not yet disposed. Guarded by
#: :func:`dispose_engines`, which drains it.
_tracked: dict[int, AsyncEngine] = {}


def create_engine_for_url(url: str, *, echo: bool = False) -> AsyncEngine:
    """Build a fresh ``AsyncEngine`` for ``url`` (no connection is opened yet).

    Used by tests that need an explicitly configured, throwaway engine; the
    caller owns it and must ``await engine.dispose()`` itself.
    """
    return create_async_engine(
        url,
        echo=echo,
        # Even with ``echo`` off, SQLAlchemy can emit statement logs through the
        # ``sqlalchemy.engine`` logger. Bound parameters of this service include
        # note bodies and password hashes, so they are never rendered.
        hide_parameters=True,
        pool_pre_ping=True,
        future=True,
    )


def _build_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        echo=settings.db_echo,
        # ``hide_parameters`` applies to ``echo=True`` as well: enabling SQL echo
        # for debugging must not print note bodies (plan §11.1 / §14.1).
        hide_parameters=True,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_seconds,
        pool_recycle=settings.db_pool_recycle_seconds,
        pool_pre_ping=True,
        future=True,
    )


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """Return the engine for ``settings.database_url``, creating it on first use.

    If the DSN changes at runtime a new engine is built for the new DSN and the
    previous one stays in the registry, so :func:`dispose_engines` can still
    close its pooled connections (no leak, no silent drop).
    """
    global _engine, _engine_url

    settings = settings or get_settings()
    url = settings.database_url

    if _engine is not None and _engine_url == url:
        return _engine

    if _engine is not None and _engine_url is not None:
        logger.debug(
            "DSN changed: switching engine (%s -> %s); the old engine stays tracked until dispose",
            make_url(_engine_url).render_as_string(hide_password=True),
            settings.masked_database_url,
        )

    logger.debug("creating async engine for %s", settings.masked_database_url)
    engine = _build_engine(settings)
    # Register *before* rebinding so an exception here cannot leak the engine.
    _tracked[id(engine)] = engine
    _engine, _engine_url = engine, url

    return engine


async def dispose_engines() -> None:
    """Dispose every engine created by :func:`get_engine` and clear the registry.

    Called from the FastAPI lifespan on shutdown. Safe when no engine was ever
    created, safe to call more than once, and disposal errors on one engine do
    not prevent the others from being disposed.
    """
    global _engine, _engine_url

    engines = list(_tracked.values())
    _tracked.clear()
    _engine, _engine_url = None, None

    if not engines:
        return

    for engine in engines:
        try:
            logger.debug("disposing async engine")
            await engine.dispose()
        except Exception:  # pragma: no cover - shutdown must never abort
            logger.exception("failed to dispose an async engine during shutdown")
