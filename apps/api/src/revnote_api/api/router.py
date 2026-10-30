"""The single registration entry of the HTTP surface.

Everything mounted on the app is listed here, and nothing else may call
``include_router``. That makes the published OpenAPI document a deliberate,
reviewable list instead of a side effect of import order, and gives later lanes
one file to add a line to instead of racing to edit ``main.py``.

Mounted today::

    GET /                       liveness hello   (unversioned, no auth)
    GET /health                 liveness probe   (unversioned, no auth)

Versioned business routes are registered here as each implementation lands:
authentication, ordered push, consistent pull/snapshot, and read-only tree/note.
The prefix and registration order live here rather than in endpoint modules.
"""

from __future__ import annotations

from fastapi import APIRouter

from revnote_api.api.routes.attachments import capabilities_router
from revnote_api.api.routes.attachments import router as attachments_router
from revnote_api.api.routes.auth import me_router
from revnote_api.api.routes.auth import router as auth_router
from revnote_api.api.routes.health import liveness_router
from revnote_api.api.routes.read import router as read_router
from revnote_api.api.routes.sync_pull import router as sync_pull_router
from revnote_api.api.routes.sync_push import sync_push_router

__all__ = ["API_V1_PREFIX", "api_v1", "build_router", "router"]

#: All business routes live under this prefix (plan §7.1). Liveness stays bare.
API_V1_PREFIX = "/api/v1"

#: The single versioned registration surface.
api_v1 = APIRouter(prefix=API_V1_PREFIX)
api_v1.include_router(auth_router)
api_v1.include_router(me_router)
api_v1.include_router(sync_push_router)
api_v1.include_router(sync_pull_router)
api_v1.include_router(read_router)
api_v1.include_router(attachments_router)
api_v1.include_router(capabilities_router)

#: What the app mounts. Liveness first (it must answer even if a later route's
#: import is broken), then the versioned group.
router = APIRouter()
router.include_router(liveness_router)
router.include_router(api_v1)


def build_router(settings: object | None = None) -> APIRouter:
    """Build the mounted surface for one app, honouring the bridge switch (D10).

    ``router`` above is the default (native-only) instance, kept for tests and
    for anything that mounts it directly; ``create_app`` calls this function so
    two apps with different settings can coexist in one process.

    The Joplin compatibility routes are imported **only** when the switch is on:
    a deployment that never enables the bridge never loads the adapter, and a
    disabled deployment answers ``404`` on ``/api/sessions`` exactly like a
    server that does not implement the protocol.
    """
    root = APIRouter()
    root.include_router(liveness_router)
    root.include_router(api_v1)
    if settings is not None and bool(getattr(settings, "joplin_bridge_enabled", False)):
        from revnote_api.adapters.joplin.routes import router as joplin_router

        root.include_router(joplin_router)
    return root
