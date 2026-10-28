"""Uvicorn entrypoint: ``uv run revnote-api`` (or ``python -m revnote_api``).

Uses the import string so ``--reload`` works. Nothing here connects to a
database at import time.
"""

from __future__ import annotations

import uvicorn

from revnote_api.config import get_settings


def run() -> None:
    """Start the API with settings from the environment."""
    settings = get_settings()
    uvicorn.run(
        "revnote_api.main:create_app",
        factory=True,
        host=settings.server_host,
        port=settings.server_port,
        reload=settings.server_reload,
        log_level="info",
    )


if __name__ == "__main__":
    run()
