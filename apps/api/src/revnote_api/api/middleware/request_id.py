"""``X-Request-ID`` propagation (plan §11.1, D21).

One rule: **every** response carries a request id, and it is the same value that
appears in the error envelope and in the log record for that request.

* A client-supplied ``X-Request-ID`` is accepted **only** when it is ASCII
  ``[A-Za-z0-9._-]`` and 1-64 characters long. Anything else (control
  characters, newlines, unicode, empty, 10 KB of junk) is *replaced* by a
  server-generated id - never echoed, never truncated into something that looks
  client-chosen. Echoing arbitrary bytes back into a header would be a response-
  splitting and log-injection primitive.
* The id is stored on ``request.state.request_id`` so handlers, exception
  handlers and the error builders all read one value instead of re-parsing a
  header.
* Middleware is pure ASGI (not ``BaseHTTPMiddleware``): the response header is
  added in the ``http.response.start`` message, which keeps the request's
  cancellation and disconnect semantics untouched - important here, because this
  phase's write path must never have its cancellation reshaped by middleware.
"""

from __future__ import annotations

import secrets
from typing import Any, Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from revnote_api.api.schemas.common import REQUEST_ID_RE

__all__ = ["REQUEST_ID_HEADER", "RequestIdMiddleware", "new_request_id", "sanitize_request_id"]

#: Header name, lower-cased for ASGI header lookups.
REQUEST_ID_HEADER: Final[str] = "x-request-id"


def new_request_id() -> str:
    """A fresh server id: ``req-`` plus 24 hex characters (28 ASCII bytes)."""
    return f"req-{secrets.token_hex(12)}"


def sanitize_request_id(candidate: str | None) -> str:
    """Return the client id when it is legal, otherwise a fresh server id."""
    if candidate is not None and REQUEST_ID_RE.fullmatch(candidate):
        return candidate
    return new_request_id()


def _header(scope: Scope, name: bytes) -> str | None:
    """First value of ``name`` in the ASGI header list (latin-1 decoded)."""
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


class RequestIdMiddleware:
    """Assign, validate and echo the per-request correlation id."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = sanitize_request_id(_header(scope, REQUEST_ID_HEADER.encode("ascii")))
        # ``Request.state`` is backed by this dict, so a handler sees the id
        # without importing anything from this module.
        state: dict[str, Any] = scope.setdefault("state", {})
        state["request_id"] = request_id

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(key.lower() == REQUEST_ID_HEADER.encode("ascii") for key, _ in headers):
                    headers.append((REQUEST_ID_HEADER.encode("ascii"), request_id.encode("ascii")))
                    message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)
