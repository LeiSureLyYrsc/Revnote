"""Revnote API — scaffold package (no business logic yet).

This package only contains the runnable skeleton later features build on:
application factory, settings, async SQLAlchemy plumbing and liveness routes.

Import surface::

    from revnote_api import create_app, get_settings
"""

from __future__ import annotations

from revnote_api._version import __version__
from revnote_api.config import Settings, get_settings
from revnote_api.main import create_app

__all__ = ["Settings", "__version__", "create_app", "get_settings"]
