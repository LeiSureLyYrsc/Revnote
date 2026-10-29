"""The single registration entry of the HTTP surface.

Everything mounted on the app is listed here, and nothing else may call
``include_router``. That makes the published OpenAPI document a deliberate,
reviewable list instead of a side effect of import order, and gives later lanes
one file to add a line to instead of racing to edit ``main.py``.

Mounted today::

    GET /                       liveness hello   (unversioned, no auth)
    GET /health                 liveness probe   (unversioned, no auth)

Versioned business routes are added by the commits that implement them, each
under :data:`API_V1_PREFIX`. In ``commit 1`` the contract skeleton (error
envelope, request id, limits) is in place but **no** ``/api/v1`` endpoint is
published, so the OpenAPI document cannot advertise a route that cannot be
served. The versioned ``APIRouter`` is created here anyway so the prefix, tags
and description live with the rest of the contract rather than in ``main.py``.
"""

from __future__ import annotations

from fastapi import APIRouter

from revnote_api.api.routes.health import liveness_router

__all__ = ["API_V1_PREFIX", "api_v1", "router"]

#: All business routes live under this prefix (plan §7.1). Liveness stays bare.
API_V1_PREFIX = "/api/v1"

#: The versioned router. Empty in this commit; endpoint modules get included into
#: it (``api_v1.include_router(auth.router)``) as their commits land.
api_v1 = APIRouter(prefix=API_V1_PREFIX)

#: What the app mounts. Liveness first (it must answer even if a later route's
#: import is broken), then the versioned group.
router = APIRouter()
router.include_router(liveness_router)
router.include_router(api_v1)
