"""ASGI middleware of the ``/api/v1`` surface.

Both middlewares are pure ASGI (not ``BaseHTTPMiddleware``) on purpose: they
must not introduce an extra task around the request, because this phase's write
path depends on ``asyncio.CancelledError`` propagating with its original
semantics (plan §9.3) and on disconnect observation being honest.

:mod:`revnote_api.api.middleware.request_id`
    Accept/validate/echo ``X-Request-ID``; exposes it on ``request.state``.
:mod:`revnote_api.api.middleware.request_limits`
    Bound request bodies by bytes actually received; own the push batch budget.

Order in the app: ``RequestIdMiddleware`` outermost, then ``BodyLimitMiddleware``,
so a 413 still carries the request id that the request-id layer assigned.
"""

from __future__ import annotations

from revnote_api.api.middleware.request_id import (
    REQUEST_ID_HEADER,
    RequestIdMiddleware,
    new_request_id,
    sanitize_request_id,
)
from revnote_api.api.middleware.request_limits import (
    AUTH_BODY_LIMIT_BYTES,
    DEFAULT_BODY_LIMIT_BYTES,
    PUSH_BODY_LIMIT_BYTES,
    BodyLimitMiddleware,
    PayloadTooLarge,
    RequestBudget,
    limit_for_path,
)

__all__ = [
    "AUTH_BODY_LIMIT_BYTES",
    "DEFAULT_BODY_LIMIT_BYTES",
    "PUSH_BODY_LIMIT_BYTES",
    "REQUEST_ID_HEADER",
    "BodyLimitMiddleware",
    "PayloadTooLarge",
    "RequestBudget",
    "RequestIdMiddleware",
    "limit_for_path",
    "new_request_id",
    "sanitize_request_id",
]
