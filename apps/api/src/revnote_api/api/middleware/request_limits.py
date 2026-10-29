"""Request-body limits, counted from bytes actually received (plan §11.2, D9).

``Content-Length`` is a *claim*. A client can omit it (chunked), understate it,
or send ``Content-Encoding: gzip`` and let 40 KiB of compressed data expand into
megabytes. So this middleware

* refuses any ``Content-Encoding`` other than identity (no compressed request
  bodies in v1 - that removes the whole decompression-bomb surface),
* rejects early when the declared length already exceeds the limit, and
* **counts the bytes actually handed to the app**, aborting the moment the
  running total passes the limit.

Limits are per path prefix, because the payload classes genuinely differ:

===============  =========  ===============================================
class            budget     rationale
===============  =========  ===============================================
``/api/v1/auth`` 16 KiB     two short strings; nothing legitimate is larger
``sync/push``    2 MiB      <=50 ops of <=256 KiB body, in practice far less
default          1 MiB      anything else that grows a body later
===============  =========  ===============================================

A rejection is the ordinary error envelope with code ``payload_too_large``
(413), so a client parses one error shape everywhere.

This module also owns the **batch budget**: a monotonic deadline, created when a
push request starts, that the push loop checks *between* ops (plan §5 P2: never
mid-statement). Injecting it as a value - rather than reading the clock inside
the kernel - keeps the kernel free of configuration and makes the deadline
testable without sleeping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from revnote_api.api.middleware.request_id import new_request_id

__all__ = [
    "AUTH_BODY_LIMIT_BYTES",
    "DEFAULT_BODY_LIMIT_BYTES",
    "PUSH_BODY_LIMIT_BYTES",
    "BodyLimitMiddleware",
    "PayloadTooLarge",
    "RequestBudget",
]

#: Two short credentials: 16 KiB is already generous.
AUTH_BODY_LIMIT_BYTES: Final[int] = 16 * 1024

#: A push batch: plan D9 caps the raw JSON at 2 MiB.
PUSH_BODY_LIMIT_BYTES: Final[int] = 2 * 1024 * 1024

#: Anything else that later grows a body (pull has none today).
DEFAULT_BODY_LIMIT_BYTES: Final[int] = 1 * 1024 * 1024

#: Path prefixes that select a limit, with the class of payload they carry.
#: Longest prefix wins, so ordering is explicit.
_LIMIT_BY_PREFIX: Final[tuple[tuple[str, str], ...]] = (
    ("/api/v1/auth", "auth"),
    ("/api/v1/workspaces/", "push"),
)


class PayloadTooLarge(Exception):
    """Internal signal: the running body size passed the path's limit."""


def limit_for_path(path: str, *, push_limit: int = PUSH_BODY_LIMIT_BYTES) -> int:
    """The body budget for ``path`` (longest matching prefix, else the default).

    ``push_limit`` is the configured ``SYNC_PUSH_MAX_BYTES``; passing it in keeps
    the module free of a settings import and keeps the defaults testable.
    """
    best_length = -1
    best_kind = "default"
    for prefix, kind in _LIMIT_BY_PREFIX:
        if path.startswith(prefix) and len(prefix) > best_length:
            best_length, best_kind = len(prefix), kind
    if best_kind == "auth":
        return AUTH_BODY_LIMIT_BYTES
    if best_kind == "push":
        return push_limit
    return DEFAULT_BODY_LIMIT_BYTES


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _declared_length(scope: Scope) -> int | None:
    raw = _header(scope, b"content-length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return -1  # malformed: treat as "unknown but suspicious" -> reject
    return value


class BodyLimitMiddleware:
    """Enforce per-path request-body budgets on bytes really received."""

    def __init__(self, app: ASGIApp, *, push_limit: int | None = None) -> None:
        self.app = app
        #: ``SYNC_PUSH_MAX_BYTES`` when configured; the constant is the default so
        #: the middleware stays usable on its own (tests, embedded apps).
        self._push_limit = PUSH_BODY_LIMIT_BYTES if push_limit is None else int(push_limit)

    def _limit_for(self, path: str) -> int:
        return limit_for_path(path, push_limit=self._push_limit)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "DELETE", "OPTIONS"}:
            # No request body is expected on these methods; skip the wrapper so
            # their receive path stays exactly the server's.
            await self.app(scope, receive, send)
            return

        limit = self._limit_for(scope.get("path", ""))
        encoding = _header(scope, b"content-encoding")
        if encoding is not None and encoding.strip().lower() not in {"", "identity"}:
            await self._reject(scope, receive, send)
            return

        declared = _declared_length(scope)
        if declared is not None and (declared < 0 or declared > limit):
            await self._reject(scope, receive, send)
            return

        received = 0
        response_started = False

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise PayloadTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, bounded_receive, tracking_send)
        except PayloadTooLarge:
            # A handler that already began responding cannot be answered twice:
            # the server closes the connection instead. Before that point the
            # client gets the ordinary envelope.
            if not response_started:
                await self._reject(scope, receive, send)

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        from revnote_api.api.errors import error_response

        state = scope.get("state") or {}
        request_id = state.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            # Only reachable when this middleware runs without RequestIdMiddleware.
            request_id = new_request_id()
        response = error_response("payload_too_large", request_id)
        # Drain nothing: an oversized body is never read to the end.
        await response(scope, receive, send)


@dataclass(frozen=True, slots=True)
class RequestBudget:
    """A monotonic deadline for one push batch (plan §5 P2 #1, D17).

    Created from the configured millisecond budget when the request starts. The
    push loop asks :attr:`expired` **between** ops only: a statement that has
    begun is allowed to finish (or to hit the statement timeout), because
    interrupting it mid-write is precisely what would create an unknown outcome.
    """

    deadline: float
    budget_ms: int

    @classmethod
    def start(cls, budget_ms: int, *, now: float | None = None) -> RequestBudget:
        """Begin a budget of ``budget_ms`` milliseconds from now (monotonic)."""
        current = time.monotonic() if now is None else now
        return cls(deadline=current + (budget_ms / 1000.0), budget_ms=int(budget_ms))

    def remaining_ms(self, *, now: float | None = None) -> float:
        """Milliseconds left (may be negative once the budget is spent)."""
        current = time.monotonic() if now is None else now
        return (self.deadline - current) * 1000.0

    def expired(self, *, now: float | None = None) -> bool:
        """True once no further op may be started."""
        return self.remaining_ms(now=now) <= 0.0
