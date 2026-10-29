"""Route packages of the HTTP surface.

One module per endpoint group: ``health`` (unversioned liveness), and later
``auth``, ``sync_push``, ``sync_pull``, ``read`` under ``/api/v1``. Registration
happens in exactly one place - :mod:`revnote_api.api.router` - so the mounted set
is readable in a single file and the OpenAPI document is a deliberate list rather
than a side effect of import order.
"""

from __future__ import annotations

from revnote_api.api.routes.health import liveness_router

__all__ = ["liveness_router"]
