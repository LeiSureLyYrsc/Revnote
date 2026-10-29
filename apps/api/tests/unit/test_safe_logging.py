"""Log-redaction tests: the access line and the server exception line.

Both sinks are driven with a **real logging setup** - the same logger names,
formatters and handlers uvicorn installs - so the assertions are about what a
terminal or log shipper would actually receive, not about a helper's return value.

The two leaks under test are the ones the application envelope cannot cover:

* the access line renders the raw target, query string included, *before* any
  routing (``GET /x?access_token=...``);
* the server's exception line renders a traceback, whose ``str()`` includes chained
  causes such as a ``DBAPIError`` message quoting statement parameters.

Neither test needs a database: the "SQL" case is an exception raised with the
parameter text in its message, which is exactly what asyncpg produces.
"""

from __future__ import annotations

import logging
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from revnote_api.api.middleware.safe_logging import (
    ACCESS_LOGGER,
    ERROR_LOGGER,
    AccessLogRedactionFilter,
    ExceptionRedactionFilter,
    RouteTemplateMiddleware,
    install_log_redaction,
    render_access_target,
)
from revnote_api.config import Settings
from revnote_api.main import create_app

pytestmark = pytest.mark.anyio

#: Sentinels: what a leak would look like in the log stream.
QUERY_TOKEN = "access_token=rva_LEAK_ME_0123456789"
HEADER_TOKEN = "Bearer rva_HEADER_LEAK_ME"
BODY_SECRET = "BODY_MD_SENTINEL_should_never_be_logged"
SQL_PARAM = "hunter2-PASSWORD-IN-SQL-PARAMS"

#: uvicorn's own access format (uvicorn.config.LOGGING_CONFIG "access").
ACCESS_FORMAT = '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s'


@pytest.fixture
def access_capture() -> tuple[logging.Logger, logging.Handler]:
    """A real handler + uvicorn's access formatter on the access logger."""
    logger = logging.getLogger(ACCESS_LOGGER)
    handler = logging.StreamHandler()
    handler.setFormatter(_uvicorn_access_formatter())
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    install_log_redaction()
    try:
        yield logger, handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        # Drop the filters this test installed, so other tests see a clean logger.
        logger.filters.clear()


def _uvicorn_access_formatter() -> logging.Formatter:
    """uvicorn's ``AccessFormatter``, which is the piece that unpacks ``args``."""
    from uvicorn.logging import AccessFormatter

    return AccessFormatter(fmt=ACCESS_FORMAT, use_colors=False)


class _ListHandler(logging.Handler):
    """Collect formatted output (``Formatter.format``, not just ``getMessage``).

    Using ``format`` matters: it is what applies ``exc_info``/``exc_text`` and the
    formatter's own field substitution.
    """

    def __init__(self, formatter: logging.Formatter | None = None) -> None:
        super().__init__()
        if formatter is not None:
            self.setFormatter(formatter)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def _install_capture(logger_name: str, handler: logging.Handler) -> None:
    """Attach ``handler`` to ``logger_name`` and install the production filters."""
    logger = logging.getLogger(logger_name)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    install_log_redaction()


def _remove_capture(logger_name: str, handler: logging.Handler, level: int) -> None:
    logger = logging.getLogger(logger_name)
    logger.removeHandler(handler)
    logger.setLevel(level)
    logger.filters.clear()


@pytest.fixture
def access_lines() -> _ListHandler:
    """Uvicorn-shaped access capture that renders exactly like production."""
    handler = _ListHandler(_uvicorn_access_formatter())
    logger = logging.getLogger(ACCESS_LOGGER)
    previous_level = logger.level
    _install_capture(ACCESS_LOGGER, handler)
    try:
        yield handler
    finally:
        _remove_capture(ACCESS_LOGGER, handler, previous_level)


@pytest.fixture
def error_lines() -> _ListHandler:
    handler = _ListHandler(logging.Formatter("%(levelname)s %(message)s"))
    logger = logging.getLogger(ERROR_LOGGER)
    previous_level = logger.level
    _install_capture(ERROR_LOGGER, handler)
    try:
        yield handler
    finally:
        _remove_capture(ERROR_LOGGER, handler, previous_level)


# --------------------------------------------------------------------------- #
# access line: method + route template + status, never the query
# --------------------------------------------------------------------------- #


def test_the_access_filter_removes_the_query_string() -> None:
    """The narrow assertion: whatever else changes, the query must not survive."""
    record = _access_record(
        client="127.0.0.1:5000",
        method="GET",
        target=f"/api/v1/workspaces/w/sync/pull?since=0&{QUERY_TOKEN}",
        version="1.1",
        status=200,
    )

    AccessLogRedactionFilter().filter(record)

    rendered = _format_access(record)
    assert QUERY_TOKEN not in rendered
    assert "since=0" not in rendered
    assert "access_token" not in rendered


def test_a_client_cannot_forge_a_log_line_through_the_query() -> None:
    """CRLF in a query string is log injection; the filter must flatten it."""
    record = _access_record(
        client="127.0.0.1:5000",
        method="GET",
        target="/health?x=1%0d%0aFORGED%20ADMIN%20LOGIN",
        version="1.1",
        status=200,
    )

    AccessLogRedactionFilter().filter(record)

    rendered = _format_access(record)
    assert len(rendered.splitlines()) == 1
    assert "FORGED" not in rendered or "?" not in rendered


def test_the_access_line_still_names_method_and_status() -> None:
    """Redaction must not gut the line: operators need the request shape."""
    record = _access_record(
        client="10.0.0.7:1234",
        method="post",
        target="/api/v1/auth/login?redirect=/nope",
        version="1.1",
        status=401,
    )

    AccessLogRedactionFilter().filter(record)

    rendered = _format_access(record)
    assert "POST" in rendered
    assert "401" in rendered
    assert "10.0.0.7:1234" in rendered
    assert "redirect" not in rendered


def test_the_target_uses_the_templates_context_when_published() -> None:
    """A real request publishes the matched template, so ids never appear."""
    from revnote_api.api.middleware.safe_logging import _access_target

    token = _access_target.set("/api/v1/workspaces/{workspace_id}/notes/{note_id}")
    try:
        assert render_access_target() == "/api/v1/workspaces/{workspace_id}/notes/{note_id}"
    finally:
        _access_target.reset(token)


def test_a_record_with_an_unexpected_shape_is_dropped_not_printed() -> None:
    """A line that cannot be re-rendered safely must not reach the handler."""
    record = logging.LogRecord(
        name=ACCESS_LOGGER,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s" %s',
        args=("only", "three"),
        exc_info=None,
    )

    assert AccessLogRedactionFilter().filter(record) is False


async def test_a_real_request_logs_no_query_but_keeps_the_template(
    settings: Settings, access_lines: _ListHandler
) -> None:
    """End to end: the concrete path and its query never reach the log.

    The transport is an ASGI client, so *uvicorn itself* is not in the loop; the
    access call the protocol makes is reproduced from the same scope values
    (``uvicorn.protocols.utils.get_path_with_query_string``) at response start,
    which is where the real server does it.
    """
    workspace = uuid.uuid4()
    app = create_app(settings)

    @app.get("/logging-probe/workspaces/{workspace_id}/sync/pull")
    async def pull(workspace_id: uuid.UUID, since: str = "0") -> dict[str, str]:
        return {"since": since}

    scope = _http_scope(
        f"/logging-probe/workspaces/{workspace}/sync/pull",
        query=b"since=0&access_token=rva_LEAK_ME_0123456789",
    )
    response = await _call_with_access_log(app, scope, headers={"X-Request-ID": "trace-access-1"})

    assert response["status"] == 200
    stream = "\n".join(access_lines.lines)
    assert stream, "the access logger must still emit one line"
    assert QUERY_TOKEN not in stream
    assert "access_token" not in stream
    # The concrete id is absent; the template is present.
    assert str(workspace) not in stream
    assert "/logging-probe/workspaces/{workspace_id}/sync/pull" in stream
    assert "GET" in stream
    assert "200" in stream
    # Correlation with the client-visible envelope/header.
    assert "trace-access-1" in stream


async def test_an_unknown_path_still_logs_without_the_query(
    settings: Settings, access_lines: _ListHandler
) -> None:
    """No route matched -> no template; the bare path is the safe fallback."""
    app = create_app(settings)
    scope = _http_scope("/nope", query=b"access_token=rva_LEAK_ME_0123456789")

    response = await _call_with_access_log(app, scope)

    assert response["status"] == 404
    stream = "\n".join(access_lines.lines)
    assert QUERY_TOKEN not in stream
    assert "/nope" in stream


async def test_an_authorization_header_never_appears(
    settings: Settings, access_lines: _ListHandler
) -> None:
    """Headers are never part of the access line, at any log level."""
    app = create_app(settings)

    @app.get("/api/v1/me")
    async def me() -> dict[str, bool]:
        return {"ok": True}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        await client.get("/api/v1/me", headers={"Authorization": HEADER_TOKEN})

    assert HEADER_TOKEN not in "\n".join(access_lines.lines)


# --------------------------------------------------------------------------- #
# exception line: class name only, no traceback, no chained SQL parameters
# --------------------------------------------------------------------------- #


def test_the_exception_filter_drops_the_traceback_but_keeps_the_class() -> None:
    try:
        raise RuntimeError(BODY_SECRET)
    except RuntimeError:
        record = _exception_record("uvicorn.error", "Exception in ASGI application")

    ExceptionRedactionFilter().filter(record)

    assert record.exc_info is None
    assert record.exc_text is None
    assert record.stack_info is None
    assert "RuntimeError" in record.getMessage()


def test_a_chained_sql_error_does_not_leak_parameters() -> None:
    """The realistic shape: a DBAPIError quoting statement parameters."""
    try:
        try:
            raise ValueError(f"Key (body_md)=({SQL_PARAM})")
        except ValueError as cause:
            raise RuntimeError("write failed") from cause
    except RuntimeError:
        record = _exception_record("uvicorn.error", "Exception in ASGI application")

    ExceptionRedactionFilter().filter(record)

    rendered = logging.Formatter("%(message)s").format(record)
    assert SQL_PARAM not in rendered
    assert "body_md" not in rendered
    assert "RuntimeError" in rendered
    assert "Traceback" not in rendered


def test_a_record_without_a_traceback_passes_through_untouched() -> None:
    """Startup/stop lines keep their text: dropping them would hide operations."""
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Application startup complete.",
        args=(),
        exc_info=None,
    )

    assert ExceptionRedactionFilter().filter(record) is True
    assert record.getMessage() == "Application startup complete."


def test_the_exception_line_carries_the_request_id_when_available() -> None:
    """Correlation is the reason to keep the line at all."""
    from revnote_api.api.middleware.safe_logging import _access_request_id

    token = _access_request_id.set("trace-500-abc")
    try:
        try:
            raise RuntimeError(BODY_SECRET)
        except RuntimeError:
            record = _exception_record("uvicorn.error", "Exception in ASGI application")
        ExceptionRedactionFilter().filter(record)
    finally:
        _access_request_id.reset(token)

    line = logging.Formatter("%(message)s").format(record)
    assert "request_id=trace-500-abc" in line
    assert BODY_SECRET not in line


async def test_a_500_never_writes_the_traceback_to_the_error_logger(
    settings: Settings, error_lines: _ListHandler
) -> None:
    """End to end through a real app and the real logger name.

    Starlette re-raises after sending the safe envelope, so uvicorn's sink is the
    one that would print the stack. The protocol's handler is reproduced from
    ``h11_impl``::

        except BaseException as exc:
            self.logger.error("Exception in ASGI application\\n", exc_info=exc)

    This asserts the stack is gone while the envelope the client saw stays intact.
    """
    app = create_app(settings)

    @app.get("/api/v1/boom")
    async def boom() -> None:
        raise RuntimeError(f"failed with {BODY_SECRET}")

    scope = _http_scope("/api/v1/boom")
    scope["headers"] = [(b"host", b"testserver"), (b"x-request-id", b"trace-boom")]
    captured: dict[str, object] = {}

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            captured["status"] = int(message["status"])  # type: ignore[arg-type]
            captured["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            captured["body"] = captured.get("body", b"") + message.get("body", b"")  # type: ignore[operator]

    try:
        await app(scope, receive, send)
    except RuntimeError as exc:
        # What the server does: one error line with the traceback attached.
        logging.getLogger(ERROR_LOGGER).error("Exception in ASGI application\n", exc_info=exc)
    else:  # pragma: no cover - Starlette always re-raises
        raise AssertionError("the 500 must be re-raised for the server to log")

    assert captured["status"] == 500
    assert b'"code":"internal"' in captured["body"]  # type: ignore[operator]
    assert BODY_SECRET.encode() not in captured["body"]  # type: ignore[operator]

    stream = "\n".join(error_lines.lines)
    assert BODY_SECRET not in stream
    assert "Traceback" not in stream
    assert "RuntimeError" in stream
    # Correlation: the redacted line still names the request.
    assert "trace-boom" in stream
    assert "/api/v1/boom" in stream


async def test_the_sql_alchemy_logger_is_untouched_by_the_filters(
    settings: Settings,
) -> None:
    """Narrow scope: only the three named loggers are filtered.

    The SQL engine already hides parameters, and silencing ``sqlalchemy`` globally
    would remove legitimate diagnostics for every other deployment concern.
    """
    create_app(settings)

    sqlalchemy_logger = logging.getLogger("sqlalchemy.engine.Engine")
    assert not any(isinstance(f, ExceptionRedactionFilter) for f in sqlalchemy_logger.filters)
    assert not any(isinstance(f, AccessLogRedactionFilter) for f in sqlalchemy_logger.filters)
    assert logging.getLogger().filters == []


def test_installing_twice_does_not_duplicate_filters() -> None:
    """``create_app`` runs per app (and per test): duplicate filters would stack."""
    install_log_redaction()
    install_log_redaction()

    access_filters = [
        f for f in logging.getLogger(ACCESS_LOGGER).filters if type(f) is AccessLogRedactionFilter
    ]
    error_filters = [
        f for f in logging.getLogger(ERROR_LOGGER).filters if type(f) is ExceptionRedactionFilter
    ]

    assert len(access_filters) <= 1
    assert len(error_filters) <= 1
    logging.getLogger(ACCESS_LOGGER).filters.clear()
    logging.getLogger(ERROR_LOGGER).filters.clear()


async def test_the_route_template_middleware_does_not_change_the_response(
    settings: Settings,
) -> None:
    """It is an observer: status, body and envelope must be identical."""
    app = create_app(settings)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client,
    ):
        health = await client.get("/health")
        missing = await client.get("/nope", headers={"X-Request-ID": "trace-missing"})

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert missing.status_code == 404
    assert missing.json()["error"]["request_id"] == "trace-missing"
    assert missing.headers["x-request-id"] == "trace-missing"


def test_the_route_template_middleware_is_pure_asgi() -> None:
    """No ``BaseHTTPMiddleware``: it would add a task and reshape cancellation."""
    import inspect

    assert inspect.iscoroutinefunction(RouteTemplateMiddleware.__call__)
    assert not any(
        base.__name__ == "BaseHTTPMiddleware" for base in RouteTemplateMiddleware.__mro__
    )


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _access_record(
    *,
    client: str,
    method: str,
    target: str,
    version: str,
    status: int,
) -> logging.LogRecord:
    """An ``uvicorn.access`` record exactly as the protocol builds it."""
    return logging.LogRecord(
        name=ACCESS_LOGGER,
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(client, method, target, version, status),
        exc_info=None,
    )


def _exception_record(name: str, message: str) -> logging.LogRecord:
    """A record carrying the active exception, like ``logger.error(msg, exc_info=exc)``."""
    import sys

    return logging.LogRecord(
        name=name,
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=sys.exc_info(),
    )


def _format_access(record: logging.LogRecord) -> str:
    return _uvicorn_access_formatter().format(record)


def _http_scope(path: str, *, query: bytes = b"", method: str = "GET") -> dict[str, object]:
    """A minimal HTTP ASGI scope, shaped like uvicorn's."""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": query,
        "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 51234),
        "server": ("testserver", 80),
        "state": {},
    }


async def _call_with_access_log(
    app: object, scope: dict[str, object], *, headers: dict[str, str] | None = None
) -> dict[str, object]:
    """Drive one request and emit the access line the way uvicorn's protocol does.

    ``ASGITransport`` skips the server, so this reproduces the *exact* call from
    ``uvicorn.protocols.http.h11_impl`` at ``http.response.start``::

        self.access_logger.info('%s - "%s %s HTTP/%s" %d',
            get_client_addr(self.scope), self.scope["method"],
            get_path_with_query_string(self.scope),
            self.scope["http_version"], status)

    That is what makes the assertion meaningful - the unredacted target really does
    contain the query, so the filter is what removes it.
    """
    from uvicorn.protocols.utils import get_client_addr, get_path_with_query_string

    if headers:
        scope["headers"] = [
            *scope["headers"],  # type: ignore[misc]
            *[(key.lower().encode(), value.encode()) for key, value in headers.items()],
        ]

    scope["state"] = {}
    body = b""
    status_holder: dict[str, object] = {}

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if message["type"] == "http.response.start":
            status = int(message["status"])  # type: ignore[arg-type]
            status_holder["status"] = status
            logging.getLogger(ACCESS_LOGGER).info(
                '%s - "%s %s HTTP/%s" %d',
                get_client_addr(scope),
                scope["method"],
                get_path_with_query_string(scope),  # type: ignore[arg-type]
                scope["http_version"],
                status,
            )
        elif message["type"] == "http.response.body":
            status_holder.setdefault("body", b"")
            status_holder["body"] = status_holder["body"] + message.get("body", b"")  # type: ignore[operator]

    await app(scope, receive, send)  # type: ignore[operator]
    return status_holder
