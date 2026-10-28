"""ASGI application object for ``uvicorn revnote_api.app:app`` and ASGI servers.

Prefer the factory (``revnote_api.main:create_app``) when you need custom
settings; this module-level instance is what production servers import.
"""

from __future__ import annotations

from revnote_api.main import create_app

app = create_app()

__all__ = ["app"]
