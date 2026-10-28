"""Async SQLAlchemy plumbing (engine, session factory, declarative base).

Nothing in this subpackage opens a database connection on import: engines are
created lazily so the service boots without PostgreSQL running.
"""

from __future__ import annotations

from revnote_api.db.base import Base
from revnote_api.db.engine import dispose_engines, get_engine
from revnote_api.db.session import get_sessionmaker, session_scope

__all__ = ["Base", "dispose_engines", "get_engine", "get_sessionmaker", "session_scope"]
