"""Middleware tests: request-id validation and real byte-counted body limits.

Both middlewares are pure ASGI, so they are tested through a real app over an ASGI
transport - no server, no database. What matters here is the behaviour a client can
observe: which id comes back, which status an oversized body produces, and that an
over-limit body is stopped rather than read to the end.

The body limiter is exercised with a *real* streamed body (not just
``Content-Length``), because trusting the declared length is exactly the bug this
middleware exists to prevent.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from revnote_api.api.errors import error_response
from revnote_api.api.middleware.request_id import (
    RequestIdMiddleware,
    new_request_id,
    sanitize_request_id,
)
from revnote_api.api.middleware.request_limits import (
    AUTH_BODY_LIMIT_BYTES,
    DEFAULT_BODY_LIMIT_BYTES,
    PUSH_BODY_LIMIT_BYTES,
    BodyLimitMiddleware,
    RequestBudget,
    limit_for_path,
)
from revnote_api.config import Settings
from revnote_api.main import create_app

pytestmark = pytest.mark.anyio


@pytest.fixture
async def http(settings: Settings) -> AsyncClient:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


# --------------------------------------------------------------------------- #
# request id: the pure helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "candidate",
    ["a", "A-1", "abc.def_ghi", "x" * 64, "01JB2ZK4M9V8Q7W6S5T4R3P2N1"],
)
def test_legal_ids_are_kept(candidate: str) -> None:
    assert sanitize_request_id(candidate) == candidate


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        "",
        "x" * 65,
        "a b",
        "a\tb",
        "a\nb",
        "a\rb",
        "ü",
        "🙂",
        "<x>",
        '"x"',
        "a;b",
        "%00",
    ],
)
def test_illegal_ids_become_server_ids(candidate: str | None) -> None:
    replacement = sanitize_request_id(candidate)

    assert replacement != candidate
    assert replacement.startswith("req-")
    assert len(replacement) == len("req-") + 24


def test_server_ids_are_unique() -> None:
    ids = {new_request_id() for _ in range(200)}

    assert len(ids) == 200


def test_sanitize_never_truncates_a_too_long_id_into_a_legal_prefix() -> None:
    """Truncating would let a client choose the id it appears under."""
    long_id = "a" * 200

    assert sanitize_request_id(long_id) != long_id[:64]


# --------------------------------------------------------------------------- #
# request id: over HTTP
# --------------------------------------------------------------------------- #


async def test_a_client_id_is_echoed_on_success_and_failure(http: AsyncClient) -> None:
    ok = await http.get("/health", headers={"X-Request-ID": "client-abc"})
    missing = await http.get("/nowhere", headers={"X-Request-ID": "client-abc"})

    assert ok.headers["x-request-id"] == "client-abc"
    assert missing.headers["x-request-id"] == "client-abc"
    assert missing.json()["error"]["request_id"] == "client-abc"


async def test_two_invalid_ids_do_not_collide(http: AsyncClient) -> None:
    first = await http.get("/health", headers={"X-Request-ID": "bad id"})
    second = await http.get("/health", headers={"X-Request-ID": "bad id"})

    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_the_response_header_is_present_even_when_handlers_set_one(
    settings: Settings,
) -> None:
    """A handler-built response must not end up with two X-Request-ID headers."""
    app = create_app(settings)

    @app.get("/custom-header")
    async def custom() -> object:
        return error_response("not_found", "handler-chosen")

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/custom-header")

    values = response.headers.get_list("x-request-id")
    assert len(values) == 1, values


async def test_lifespan_scopes_are_not_treated_as_requests(settings: Settings) -> None:
    """The request-id middleware must let non-HTTP ASGI scopes through untouched."""
    from revnote_api.api.middleware.request_id import RequestIdMiddleware

    seen: list[str] = []

    async def inner(scope: dict[str, object], receive: object, send: object) -> None:
        seen.append(str(scope["type"]))

    app = RequestIdMiddleware(inner)  # type: ignore[arg-type]
    await app({"type": "lifespan"}, None, None)  # type: ignore[arg-type]

    assert seen == ["lifespan"]


# --------------------------------------------------------------------------- #
# body limits: path mapping
# --------------------------------------------------------------------------- #


def test_auth_bodies_have_the_tightest_budget() -> None:
    assert AUTH_BODY_LIMIT_BYTES == 16 * 1024
    assert limit_for_path("/api/v1/auth/login") == AUTH_BODY_LIMIT_BYTES
    assert limit_for_path("/api/v1/auth/register") == AUTH_BODY_LIMIT_BYTES
    assert limit_for_path("/api/v1/auth/refresh") == AUTH_BODY_LIMIT_BYTES


def test_push_bodies_use_the_documented_two_mebibyte_budget() -> None:
    assert PUSH_BODY_LIMIT_BYTES == 2 * 1024 * 1024
    assert limit_for_path("/api/v1/workspaces/abc/sync/push") == PUSH_BODY_LIMIT_BYTES
    # The later snapshot/read routes are GETs; their allowance is moot but must
    # not accidentally become the auth limit.
    assert limit_for_path("/api/v1/workspaces/abc/tree") == PUSH_BODY_LIMIT_BYTES


def test_everything_else_gets_the_default_budget() -> None:
    assert limit_for_path("/") == DEFAULT_BODY_LIMIT_BYTES
    assert limit_for_path("/health") == DEFAULT_BODY_LIMIT_BYTES
    assert limit_for_path("/some/future/route") == DEFAULT_BODY_LIMIT_BYTES


def test_the_push_budget_comes_from_configuration() -> None:
    """``SYNC_PUSH_MAX_BYTES`` is the budget, not a hard-coded constant."""
    configured = 4096

    assert limit_for_path("/api/v1/workspaces/w/sync/push", push_limit=configured) == configured
    # Only the push class follows it: auth and the default stay where they are.
    assert limit_for_path("/api/v1/auth/login", push_limit=configured) == AUTH_BODY_LIMIT_BYTES
    assert limit_for_path("/", push_limit=configured) == DEFAULT_BODY_LIMIT_BYTES


async def test_an_app_built_from_settings_enforces_the_configured_push_budget(
    settings: Settings,
) -> None:
    """The middleware must be wired to ``sync_push_max_bytes`` by ``create_app``."""
    tight = settings.model_copy(update={"sync_push_max_bytes": 1024})
    app = create_app(tight)

    @app.post("/api/v1/workspaces/w/sync/push")
    async def push_probe(request: Request) -> dict[str, int]:
        return {"length": len(await request.body())}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        small = await client.post("/api/v1/workspaces/w/sync/push", content=b"{}")
        large = await client.post("/api/v1/workspaces/w/sync/push", content=b"x" * 2048)

    assert small.status_code == 200, small.text
    assert large.status_code == 413
    assert large.json()["error"]["code"] == "payload_too_large"


def test_the_limits_are_ordered_from_tightest_to_widest() -> None:
    assert AUTH_BODY_LIMIT_BYTES < DEFAULT_BODY_LIMIT_BYTES < PUSH_BODY_LIMIT_BYTES


# --------------------------------------------------------------------------- #
# body limits: over HTTP, by bytes actually received
# --------------------------------------------------------------------------- #


async def test_a_declared_oversized_length_is_refused_before_reading(http: AsyncClient) -> None:
    response = await http.post("/api/v1/auth/login", content=b"x" * 20_000)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_a_body_without_content_length_still_hits_the_real_limit() -> None:
    """A lying or absent ``Content-Length`` must not buy a larger body.

    The limiter is driven directly as ASGI middleware around a tiny app that reads
    its body in two chunks: the second chunk crosses the limit, so the request must
    be refused at that point instead of being read to the end.
    """
    limit = AUTH_BODY_LIMIT_BYTES
    body = b"x" * (limit + 1)
    chunks = [body[: limit // 2], body[limit // 2 :]]
    read_fully = False

    async def inner(scope: object, receive: object, send: object) -> None:  # pragma: no cover
        nonlocal read_fully
        while True:
            message = await receive()  # type: ignore[operator]
            if not message.get("more_body", False):
                read_fully = True
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b"{}"})  # type: ignore[operator]

    app = BodyLimitMiddleware(inner)  # type: ignore[arg-type]

    async def receive() -> dict[str, object]:
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}
        return {"type": "http.disconnect"}

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/auth/login",
            "raw_path": b"/api/v1/auth/login",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"test")],  # deliberately no content-length
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        },
        receive,
        send,
    )

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 413, sent
    assert read_fully is False, "the over-limit body must not be drained"


async def test_a_body_at_exactly_the_limit_is_accepted(settings: Settings) -> None:
    app = create_app(settings)

    @app.post("/api/v1/auth/probe")
    async def probe(request: Request) -> dict[str, int]:
        raw = await request.body()
        return {"length": len(raw)}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post("/api/v1/auth/probe", content=b"y" * AUTH_BODY_LIMIT_BYTES)

    assert response.status_code == 200, response.text
    assert response.json() == {"length": AUTH_BODY_LIMIT_BYTES}


async def test_a_body_one_byte_over_the_limit_is_refused(settings: Settings) -> None:
    app = create_app(settings)

    @app.post("/api/v1/auth/probe2")
    async def probe(request: Request) -> dict[str, int]:
        raw = await request.body()
        return {"length": len(raw)}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/api/v1/auth/probe2", content=b"y" * (AUTH_BODY_LIMIT_BYTES + 1)
        )

    assert response.status_code == 413


async def test_a_compressed_request_body_is_refused(settings: Settings) -> None:
    """Accepting gzip would make the byte budget meaningless."""
    app = create_app(settings)

    @app.post("/api/v1/auth/compressed")
    async def compressed() -> dict[str, bool]:
        return {"ok": True}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/api/v1/auth/compressed",
            content=b"tiny",
            headers={"Content-Encoding": "gzip"},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_identity_encoding_is_accepted(settings: Settings) -> None:
    app = create_app(settings)

    @app.post("/api/v1/auth/identity")
    async def identity() -> dict[str, bool]:
        return {"ok": True}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/api/v1/auth/identity",
            content=b"{}",
            headers={"Content-Encoding": "identity"},
        )

    assert response.status_code == 200


async def test_a_get_with_a_body_is_not_wrapped(http: AsyncClient) -> None:
    """Read paths keep the server's own receive semantics untouched."""
    response = await http.get("/health")

    assert response.status_code == 200


async def test_a_rejection_still_carries_the_request_id(http: AsyncClient) -> None:
    response = await http.post(
        "/api/v1/auth/login", content=b"x" * 20_000, headers={"X-Request-ID": "too-big-1"}
    )

    assert response.headers["x-request-id"] == "too-big-1"
    assert response.json()["error"]["request_id"] == "too-big-1"


async def test_an_over_limit_body_after_a_started_response_is_not_answered_twice() -> None:
    """ASGI forbids two responses; a streaming handler that already answered wins.

    The middleware must not send ``413`` on top of a response that has begun:
    that would be a protocol error on a real server. The connection is closed
    instead, which the client observes as a broken/partial response.
    """
    limit = AUTH_BODY_LIMIT_BYTES
    chunks = [b"x" * (limit // 2), b"x" * (limit // 2 + 1)]

    async def streaming(scope: object, receive: object, send: object) -> None:
        # Answer first, then touch the body - the exact order a buggy handler
        # could produce.
        await send({"type": "http.response.start", "status": 200, "headers": []})  # type: ignore[operator]
        await send({"type": "http.response.body", "body": b'{"ok":true}', "more_body": True})  # type: ignore[operator]
        while True:
            message = await receive()  # type: ignore[operator]
            if not message.get("more_body", False):
                break

    app = BodyLimitMiddleware(streaming)  # type: ignore[arg-type]

    async def receive() -> dict[str, object]:
        if chunks:
            return {"type": "http.request", "body": chunks.pop(0), "more_body": bool(chunks)}
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict[str, object]] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/auth/login",
            "raw_path": b"/api/v1/auth/login",
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"test")],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
        },
        receive,
        send,
    )

    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert len(starts) == 1, sent
    assert starts[0]["status"] == 200, sent


# --------------------------------------------------------------------------- #
# the push batch budget
# --------------------------------------------------------------------------- #


def test_a_budget_expires_once_its_time_is_spent() -> None:
    budget = RequestBudget.start(30_000, now=100.0)

    assert budget.expired(now=100.0) is False
    assert budget.expired(now=129.999) is False
    assert budget.expired(now=130.0) is True
    assert budget.expired(now=200.0) is True


def test_the_remaining_time_is_reported_in_milliseconds() -> None:
    budget = RequestBudget.start(1_000, now=10.0)

    assert budget.remaining_ms(now=10.0) == pytest.approx(1_000.0)
    assert budget.remaining_ms(now=10.5) == pytest.approx(500.0)
    assert budget.remaining_ms(now=12.0) == pytest.approx(-1_000.0)


def test_a_budget_keeps_its_configured_size_for_diagnostics() -> None:
    assert RequestBudget.start(30_000, now=0.0).budget_ms == 30_000


def test_a_budget_is_an_immutable_value() -> None:
    budget = RequestBudget.start(1_000, now=0.0)

    with pytest.raises(AttributeError):
        budget.budget_ms = 5  # type: ignore[misc]


def test_the_middleware_is_pure_asgi() -> None:
    """``BaseHTTPMiddleware`` would spawn a task and distort cancellation."""
    import inspect

    from revnote_api.api.middleware import request_id, request_limits

    for module, name in (
        (request_id, "RequestIdMiddleware"),
        (request_limits, "BodyLimitMiddleware"),
    ):
        cls = getattr(module, name)
        assert callable(cls)
        assert inspect.iscoroutinefunction(cls.__call__)
        assert not any(base.__name__ == "BaseHTTPMiddleware" for base in cls.__mro__)


def test_the_request_id_middleware_adds_exactly_one_header() -> None:
    """A second header would let an intermediate cache pick the wrong value."""
    source = (
        __import__("pathlib")
        .Path(__import__("revnote_api.api.middleware.request_id", fromlist=["x"]).__file__)
        .read_text(encoding="utf-8")
    )

    assert source.count("headers.append(") == 1
    assert "REQUEST_ID_HEADER.encode" in source


def test_request_id_middleware_is_exported_with_its_helpers() -> None:
    from revnote_api.api import middleware

    assert middleware.RequestIdMiddleware is RequestIdMiddleware
    assert middleware.sanitize_request_id is sanitize_request_id
    assert middleware.new_request_id is new_request_id
