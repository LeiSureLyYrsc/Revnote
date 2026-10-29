"""Log redaction: the two log sinks that can see request data.

Why this module exists
----------------------

The application's own error path is already safe: the envelope carries fixed
sentences and :mod:`revnote_api.api.errors` never logs ``str(exc)``. Two *other*
writers can still leak, and neither is under the app's control:

1. **``uvicorn.access``** renders ``"%(method)s %(full_path)s HTTP/%(version)s"``.
   ``full_path`` is the request target **including the raw query string**, as
   received and before any routing. A client that sends
   ``GET /api/v1/...?access_token=...`` has that token written to stdout even
   though the route itself would answer with a safe 400/401. Query strings are on
   the forbidden list (plan §11.1), so an access line may only show method,
   route template and status.
2. **``uvicorn.error``** renders ``"Exception in ASGI application"`` with
   ``exc_info=exc``. A traceback renders ``str()`` of every exception in the
   chain, and a SQLAlchemy/asyncpg ``DBAPIError`` message quotes statement
   parameters - note bodies and password hashes among them. ``hide_parameters``
   covers the engine's own loggers, not a traceback's text. Starlette re-raises
   after sending the safe 500 *on purpose* ("allows servers to log the error"), so
   this sink is the one that must be neutralised.

Neither fix disables logging globally: the access logger still writes one line per
request through the same handler and formatter, and ``uvicorn.error`` still writes
every record that carries no traceback. Only the payload changes - no query, no
headers, no traceback, no chained causes.

What is preserved
-----------------

Operational usefulness is kept deliberately: method, route **template** (not the
concrete path, so ids do not appear), status, request id and the exception **class
name**. That is enough to correlate a 500 with the envelope the client received;
the stack itself is available in a local debugger, where secrets are not being
shipped to a log aggregator.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "ACCESS_LOGGER",
    "APP_EXCEPTION_LOGGERS",
    "ERROR_LOGGER",
    "AccessLogRedactionFilter",
    "ExceptionRedactionFilter",
    "RouteTemplateMiddleware",
    "install_log_redaction",
    "render_access_target",
]

#: Access sink: Stdout handler, ``propagate=False``, ``AccessFormatter``.
ACCESS_LOGGER: Final[str] = "uvicorn.access"

#: Server/exception sink. Starlette re-raises every unhandled error so this logger
#: can print it - with the full traceback, which is the leak.
ERROR_LOGGER: Final[str] = "uvicorn.error"

#: App loggers that call ``logger.exception``. A logging filter runs on the
#: *originating* logger, so each needs its own instance; a parent's filter is never
#: consulted for a child's record.
APP_EXCEPTION_LOGGERS: Final[tuple[str, ...]] = (
    "revnote_api.main",
    "revnote_api.db.engine",
)

#: Bounds on the fields re-rendered here. Values come from the protocol, but a
#: filter is the last line of defence and must not emit arbitrary length.
MAX_METHOD_CHARS: Final[int] = 16
MAX_TARGET_CHARS: Final[int] = 256
MAX_CLASS_CHARS: Final[int] = 64
MAX_REQUEST_ID_CHARS: Final[int] = 64

#: Printed when a value is unavailable; never empty, so a field cannot silently
#: vanish and make the line read like a different event.
UNKNOWN: Final[str] = "-"

#: The access record's argument shape: ``(client, method, target, version, status)``.
_ACCESS_ARG_COUNT: Final[int] = 5

#: Request target for the log line: route template when routing succeeded, else the
#: bare path (which never contains the query string). Set per request by
#: :class:`RouteTemplateMiddleware`.
_access_target: ContextVar[str | None] = ContextVar("revnote_access_target", default=None)

#: ``X-Request-ID`` of the request being served, for log/envelope correlation.
_access_request_id: ContextVar[str | None] = ContextVar("revnote_access_request_id", default=None)


def _bounded(value: Any, limit: int) -> str:
    """``value`` as a single-line, bounded string (control characters removed)."""
    text = value if isinstance(value, str) else str(value)
    # A newline inside a log field lets a client forge an extra record.
    text = text.replace("\r", "").replace("\n", "").replace("\x00", "")
    return text[:limit]


def _strip_query(target: str) -> str:
    """The part of a request target before ``?`` and ``#``."""
    return target.split("#", 1)[0].split("?", 1)[0]


def render_access_target(fallback: str | None = None) -> str:
    """The only target text an access line may contain.

    Prefers the route **template** captured while serving
    (``/api/v1/workspaces/{workspace_id}/sync/pull``): no ids, no query. Falls back
    to a bare path, and finally to :data:`UNKNOWN`. The query is stripped in every
    case, so even a caller passing a raw target cannot reintroduce it.
    """
    target = _access_target.get() or fallback
    if not target:
        return UNKNOWN
    return _bounded(_strip_query(target), MAX_TARGET_CHARS)


def _current_request_id() -> str:
    return _bounded(_access_request_id.get() or UNKNOWN, MAX_REQUEST_ID_CHARS)


class AccessLogRedactionFilter(logging.Filter):
    """Rewrite ``uvicorn.access`` records to ``client - "METHOD template req=ID" status``.

    Runs on the ``uvicorn.access`` logger, i.e. before the handler and before
    ``AccessFormatter`` unpacks the arguments. The replacement keeps the
    five-argument shape that formatter requires; only the target changes.

    A record whose shape is not the expected five-tuple is **dropped**: an access
    line that cannot be re-rendered safely is worth less than the guarantee that no
    query string is ever printed.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) != _ACCESS_ARG_COUNT:
            return False

        client, method, _target, version, status = args
        record.args = (
            _bounded(client, 64),
            _bounded(method, MAX_METHOD_CHARS).upper(),
            f"{render_access_target()} req={_current_request_id()}",
            _bounded(version, 16),
            status,
        )
        # ``record.msg`` stays uvicorn's own pattern: ``AccessFormatter`` depends on
        # both it and the argument order.
        record.__dict__["revnote_redacted"] = True
        return True


class ExceptionRedactionFilter(logging.Filter):
    """Replace an exception record with a fixed, class-only line.

    Dropping ``exc_info``/``stack_info`` is the point: the traceback renders
    ``str()`` of every exception in the chain, which is exactly where SQL
    parameters and rejected input appear. What survives is enough to correlate:
    request id, route template and the exception class name.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info is None and not record.stack_info:
            # Informational lines (startup, shutdown) carry no request data.
            return True

        error_class = UNKNOWN
        exc_info = record.exc_info
        if isinstance(exc_info, tuple):
            exc_type = exc_info[0]
            if isinstance(exc_type, type):
                error_class = _bounded(exc_type.__name__, MAX_CLASS_CHARS)

        record.msg = (
            f"asgi_exception request_id={_current_request_id()} "
            f"route={render_access_target()} error_class={error_class}"
        )
        record.args = ()
        # All three must go: ``exc_text`` is the cached formatted traceback and
        # ``Formatter.format`` prefers it over ``exc_info``.
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        record.__dict__["revnote_redacted"] = True
        return True


class RouteTemplateMiddleware:
    """Publish the route template and request id for the log filters.

    Pure ASGI, no extra task: the write path's cancellation semantics must not be
    reshaped by middleware. Two context variables are set when the request arrives
    (id, if ``RequestIdMiddleware`` ran outside this one, plus the bare path) and
    upgraded to the route template when the response starts, at which point
    ``scope["route"]`` exists.

    On the **exception** path the values are deliberately *not* reset: Starlette
    re-raises after sending the 500, and uvicorn logs that exception from the same
    per-request task, so this is the only way the redacted traceback line can still
    name the request. The context is per-task and dies with the request; the
    success path resets it explicitly so a keep-alive connection cannot attribute
    one request's template to the next.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        target_token = _access_target.set(scope.get("path"))
        request_id_token = _access_request_id.set(self._request_id(scope))
        published = False

        async def send_with_template(message: Message) -> None:
            nonlocal published
            if message["type"] == "http.response.start" and not published:
                published = True
                _access_target.set(self._template(scope))
                _access_request_id.set(self._request_id(scope))
            await send(message)

        try:
            await self.app(scope, receive, send_with_template)
        except BaseException:
            # Keep the best-known values for the server's exception log.
            _access_target.set(self._template(scope))
            _access_request_id.set(self._request_id(scope))
            raise
        else:
            _access_request_id.reset(request_id_token)
            _access_target.reset(target_token)

    @staticmethod
    def _template(scope: Scope) -> str | None:
        """Route template when routing succeeded, else the bare path."""
        template = getattr(scope.get("route"), "path", None)
        if isinstance(template, str) and template:
            return template
        path = scope.get("path")
        return path if isinstance(path, str) else None

    @staticmethod
    def _request_id(scope: Scope) -> str | None:
        state = scope.get("state") or {}
        request_id = state.get("request_id")
        return request_id if isinstance(request_id, str) and request_id else None


def _ensure_filter(logger: logging.Logger, redaction_filter: logging.Filter) -> None:
    """Attach ``redaction_filter`` once, however many apps get created.

    ``create_app`` runs per application (and per test); without this guard the same
    logger would accumulate one filter per app.
    """
    if not any(type(existing) is type(redaction_filter) for existing in logger.filters):
        logger.addFilter(redaction_filter)


def install_log_redaction() -> None:
    """Install both filters. Idempotent; safe to call from every ``create_app``.

    Scope is deliberately narrow - three named loggers, never the root logger and
    never ``logging.disable``. Every other library keeps its normal verbosity; the
    SQL engine already hides parameters (``db.engine``).
    """
    _ensure_filter(logging.getLogger(ACCESS_LOGGER), AccessLogRedactionFilter())
    _ensure_filter(logging.getLogger(ERROR_LOGGER), ExceptionRedactionFilter())
    for name in APP_EXCEPTION_LOGGERS:
        _ensure_filter(logging.getLogger(name), ExceptionRedactionFilter())
