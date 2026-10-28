"""Session factory and session scope helpers.

Transaction rules enforced here (contract for later features):

* **No implicit commit.** :func:`session_scope` commits only when the caller
  exits successfully, and a session obtained via :func:`get_session` is
  read-oriented: FastAPI dependencies never commit on your behalf.
* **Rollback on exception.** Any error inside a scope rolls the transaction
  back before the error propagates.
* Sessions are *closed* but not committed when a dependency-provided session
  goes out of scope (``expire_on_commit=False`` keeps loaded attributes usable).

No business queries live here — this is plumbing only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from revnote_api.config import Settings, get_settings
from revnote_api.db.engine import get_engine

_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_sessionmaker_url: str | None = None


def get_sessionmaker(
    settings: Settings | None = None,
) -> async_sessionmaker[AsyncSession]:
    """Return a cached ``async_sessionmaker`` bound to the configured engine."""
    global _sessionmaker, _sessionmaker_url

    settings = settings or get_settings()
    url = settings.database_url

    if _sessionmaker is None or _sessionmaker_url != url:
        _sessionmaker = async_sessionmaker(
            bind=get_engine(settings),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
        _sessionmaker_url = url

    return _sessionmaker


@asynccontextmanager
async def session_scope(
    settings: Settings | None = None,
) -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, rollback on any exception.

    Usage::

        async with session_scope() as session:
            session.add(obj)      # commits when the block exits cleanly
    """
    session = get_sessionmaker(settings)()
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session.

    The session is **not** committed here; callers that mutate state must use
    :func:`session_scope` (or commit explicitly). A raised error rolls the
    transaction back before propagating.
    """
    session = get_sessionmaker()()
    try:
        yield session
    except GeneratorExit:
        # Consumer stopped early (dependency teardown / generator close): there
        # is nothing to commit and no error to report. ``close()`` in the
        # ``finally`` block releases the connection and discards the open
        # transaction, so no explicit rollback is required here.
        raise
    except BaseException:
        # Includes asyncio.CancelledError: an aborted request must not leave a
        # half-open transaction behind.
        await session.rollback()
        raise
    finally:
        await session.close()


def reset_session_state() -> None:
    """Drop the cached sessionmaker (used when settings/engine change in tests)."""
    global _sessionmaker, _sessionmaker_url

    _sessionmaker = None
    _sessionmaker_url = None


__all__ = [
    "get_session",
    "get_sessionmaker",
    "reset_session_state",
    "session_scope",
]
