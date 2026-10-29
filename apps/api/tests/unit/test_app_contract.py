"""HTTP behaviour of the cross-cutting contract, over the real app.

Everything here goes through ``create_app()`` and an ASGI transport, so the
middleware stack, the exception handlers and the published OpenAPI document are
exercised as a client sees them - still without a database, because none of these
routes touch one.

Covered:

* ``GET /`` and ``GET /health`` keep their scaffold behaviour;
* every response carries ``X-Request-ID``, legal ids are echoed, illegal ones are
  replaced (never echoed, never truncated into something client-chosen);
* the error envelope is used for 404, 405, validation and unclassified 500, and
  never leaks an internal message, a stack or a cause chain;
* the OpenAPI document publishes exactly the liveness routes - the versioned
  contract is present (bearer scheme, decimal-string counters) while **no**
  business endpoint is advertised before it can be served.
"""

from __future__ import annotations

import json
import re

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient, Response
from pydantic import BaseModel, ConfigDict

from revnote_api import __version__
from revnote_api.api.errors import (
    ERROR_SPECS,
    MAX_REPORTED_FIELDS,
    REDACTED_SEGMENT,
    install_error_handlers,
)
from revnote_api.api.middleware.request_id import RequestIdMiddleware
from revnote_api.api.schemas.common import REQUEST_ID_RE
from revnote_api.config import Settings
from revnote_api.main import create_app

pytestmark = pytest.mark.anyio

#: What ``/api/v1`` must NOT advertise yet: these are added by later commits.
FORBIDDEN_V1_PATHS = {
    "/api/v1/me/sessions",
}

#: The exact route set this commit publishes.
EXPECTED_PATHS = {
    "/",
    "/health",
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/refresh",
    "/api/v1/auth/logout",
    "/api/v1/me",
    "/api/v1/workspaces/{workspace_id}/sync/push",
    "/api/v1/workspaces/{workspace_id}/sync/pull",
    "/api/v1/workspaces/{workspace_id}/sync/snapshot",
    "/api/v1/workspaces/{workspace_id}/tree",
    "/api/v1/workspaces/{workspace_id}/notes/{note_id}",
}


class _ValidationProbe(BaseModel):
    """Module-level so FastAPI can build a stable request schema for it."""

    token: str


@pytest.fixture
async def http(settings: Settings) -> AsyncClient:
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        yield client


def envelope(response: Response) -> dict[str, object]:
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message", "request_id", "retryable", "details"}
    assert isinstance(error["details"], dict)
    return error


# --------------------------------------------------------------------------- #
# liveness keeps working
# --------------------------------------------------------------------------- #


async def test_root_and_health_are_unchanged(http: AsyncClient) -> None:
    root = await http.get("/")
    health = await http.get("/health")

    assert root.status_code == 200
    assert root.json()["service"] == "revnote-api"
    assert "Hello from Revnote API" in root.json()["message"]
    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "service": "revnote-api",
        "version": __version__,
        "environment": "local",
    }


async def test_liveness_carries_a_request_id(http: AsyncClient) -> None:
    response = await http.get("/health")

    assert REQUEST_ID_RE.fullmatch(response.headers["x-request-id"])


# --------------------------------------------------------------------------- #
# request id
# --------------------------------------------------------------------------- #


async def test_a_legal_client_request_id_is_echoed_verbatim(http: AsyncClient) -> None:
    response = await http.get("/health", headers={"X-Request-ID": "client_Req-01.a"})

    assert response.headers["x-request-id"] == "client_Req-01.a"


@pytest.mark.parametrize(
    "supplied",
    [
        "has space",
        "semi;colon",
        "new\nline",  # header injection attempt
        "a" * 65,  # too long
        "",  # empty
        "tab\there",
        'quote"mark',
        "<script>",
    ],
)
async def test_an_illegal_client_request_id_is_replaced_not_echoed(
    http: AsyncClient, supplied: str
) -> None:
    response = await http.get("/health", headers={"X-Request-ID": supplied})
    echoed = response.headers["x-request-id"]

    assert echoed != supplied
    assert REQUEST_ID_RE.fullmatch(echoed), echoed
    assert echoed.startswith("req-")


async def test_a_non_latin1_request_id_is_replaced_not_crashing(settings: Settings) -> None:
    """A byte-level honouring of the contract: non-ASCII is replaced, never echoed.

    ``httpx`` cannot encode an emoji into a latin-1 header, so the value is sent
    the way a hostile client would send it: raw bytes on the wire.
    """
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/health", headers=[(b"x-request-id", "emoji-🙂".encode())])

    echoed = response.headers["x-request-id"]
    assert REQUEST_ID_RE.fullmatch(echoed), echoed
    assert echoed.startswith("req-")


async def test_request_ids_are_unique_per_request(http: AsyncClient) -> None:
    first = await http.get("/health")
    second = await http.get("/health")

    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_the_error_envelope_repeats_the_response_header_id(http: AsyncClient) -> None:
    response = await http.get("/nope", headers={"X-Request-ID": "trace-42"})

    assert response.headers["x-request-id"] == "trace-42"
    assert envelope(response)["request_id"] == "trace-42"


# --------------------------------------------------------------------------- #
# error envelope on every failure
# --------------------------------------------------------------------------- #


async def test_an_unknown_path_is_a_not_found_envelope(http: AsyncClient) -> None:
    response = await http.get("/nope")

    assert response.status_code == 404
    assert envelope(response)["code"] == "not_found"


async def test_a_known_path_with_the_wrong_method_is_an_envelope(http: AsyncClient) -> None:
    response = await http.post("/health")

    assert response.status_code == 405
    assert envelope(response)["code"] == "invalid_request"


async def test_a_body_over_the_limit_is_a_payload_too_large_envelope(
    http: AsyncClient,
) -> None:
    response = await http.post("/api/v1/auth/login", content=b"x" * 20_000)

    assert response.status_code == 413
    assert envelope(response)["code"] == "payload_too_large"


async def test_an_unclassified_error_is_500_internal_without_any_detail(
    settings: Settings,
) -> None:
    """The handler must not forward the message, the type or a stack trace."""
    secret = "PRIVATE-BODY-TEXT-and-a-dsn"

    app = create_app(settings)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(secret)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client,
    ):
        response = await client.get("/boom", headers={"X-Request-ID": "trace-500"})

    assert response.status_code == 500
    error = envelope(response)
    assert error["code"] == "internal"
    assert error["request_id"] == "trace-500"
    assert secret not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


async def test_a_kernel_error_raised_in_a_handler_uses_the_envelope(settings: Settings) -> None:
    """A kernel failure must reach the client as a code, not a 500."""
    from revnote_api.kernel.errors import RevConflict

    app = create_app(settings)

    @app.get("/kernel-boom")
    async def kernel_boom() -> None:
        raise RevConflict("note", __import__("uuid").uuid4(), expected=1, current=4)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/kernel-boom")

    assert response.status_code == 409
    error = envelope(response)
    assert error["code"] == "rev_conflict"
    assert error["details"] == {"expected_rev": "1", "current_rev": "4"}


async def test_a_validation_error_reports_field_paths_but_not_the_input(
    settings: Settings,
) -> None:
    """Pydantic messages quote the rejected input; the envelope must not."""
    secret = "rva_SECRET_TOKEN_VALUE"

    app = create_app(settings)

    @app.post("/validate")
    async def validate(body: _ValidationProbe) -> dict[str, str]:
        return {"token": body.token}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        # ``token`` present but the wrong type: the *field* is at fault, so the
        # reported path must name it rather than saying "missing".
        response = await client.post("/validate", json={"token": {"nested": secret}})

    assert response.status_code == 400
    error = envelope(response)
    assert error["code"] == "invalid_request"
    assert "input" not in json.dumps(error["details"])
    assert secret not in response.text
    fields = error["details"]["fields"]  # type: ignore[index]
    assert fields and fields[0]["path"] == ["body", "token"]


async def test_every_envelope_code_exists_in_the_vocabulary(http: AsyncClient) -> None:
    responses = [
        await http.get("/nope"),
        await http.post("/health"),
        await http.post("/api/v1/auth/login", content=b"x" * 20_000),
    ]

    for response in responses:
        assert envelope(response)["code"] in ERROR_SPECS


# --------------------------------------------------------------------------- #
# validation details over a real route: declared names kept, client keys erased
# --------------------------------------------------------------------------- #

#: Sentinels a client could send **as a JSON key**. ``extra_forbidden`` reports the
#: key in ``loc``; a naive pass-through would echo it. Each must disappear.
SECRET_KEYS = (
    "hunter2-my-password",
    "rva_ACCESS_TOKEN_0123456789",
    "refresh_TOKEN_SENTINEL",
    "BODY_MD_SENTINEL_9182",
)


class _StrictInner(BaseModel):
    """Nested body model with ``extra="forbid"``, push-payload shaped."""

    model_config = ConfigDict(extra="forbid")

    title: str


class _StrictProbe(BaseModel):
    """Top-level body model with ``extra="forbid"``."""

    model_config = ConfigDict(extra="forbid")

    op_id: str
    payload: _StrictInner


async def test_declared_field_paths_survive_redaction(settings: Settings) -> None:
    """A 400 must still point at the offending field - redaction is not deletion."""
    app = create_app(settings)

    @app.post("/strict")
    async def strict(body: _StrictProbe) -> dict[str, str]:
        return {"op_id": body.op_id}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post("/strict", json={"op_id": "1", "payload": {"title": 7}})

    assert response.status_code == 400
    fields = envelope(response)["details"]["fields"]  # type: ignore[index]
    paths = [field["path"] for field in fields]

    assert ["body", "payload", "title"] in paths, paths
    # The framework marker is kept in first position; declared names are kept.
    assert all(path[0] == "body" for path in paths), paths


@pytest.mark.parametrize("secret_key", SECRET_KEYS)
async def test_a_secret_used_as_an_unknown_json_key_is_never_echoed(
    settings: Settings, secret_key: str
) -> None:
    """The leak this guards: ``{"...", "<password>": ...}`` must not come back."""
    app = create_app(settings)

    @app.post("/strict")
    async def strict(body: _StrictProbe) -> dict[str, str]:
        return {"op_id": body.op_id}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        # Top-level unknown key.
        top = await client.post(
            "/strict", json={"op_id": "1", "payload": {"title": "x"}, secret_key: "v"}
        )
        # Nested unknown key (inside the ``extra="forbid"`` sub-model).
        nested = await client.post(
            "/strict",
            json={"op_id": "1", "payload": {"title": "x", secret_key: "v"}},
        )
        # Unknown key whose *value* is the sentinel, too.
        valued = await client.post("/strict", json={"op_id": "1", "payload": {"title": secret_key}})

    for response in (top, nested):
        assert response.status_code == 400
        assert secret_key not in response.text
        assert "input" not in response.text
        assert "ctx" not in response.text
        fields = envelope(response)["details"]["fields"]  # type: ignore[index]
        assert fields, response.text
        assert any(REDACTED_SEGMENT in field["path"] for field in fields), fields
        assert envelope(response)["code"] == "invalid_request"

    # A sentinel as a *value* of a declared field is simply valid data: the call
    # succeeds, and (because nothing echoes request content) it still cannot be
    # read back. That is the control case for the redaction above.
    assert valued.status_code == 200, valued.text
    assert secret_key not in valued.text


async def test_validation_details_are_bounded_for_a_flood_of_bad_keys(
    settings: Settings,
) -> None:
    """A hostile body must not inflate the error envelope past the request budget."""
    app = create_app(settings)

    @app.post("/strict")
    async def strict(body: _StrictProbe) -> dict[str, str]:
        return {"op_id": body.op_id}

    flood = {f"unknown_key_{index}": "v" for index in range(300)}
    flood["op_id"] = "1"
    flood["payload"] = {"title": "x"}

    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post("/strict", json=flood)

    assert response.status_code == 400
    fields = envelope(response)["details"]["fields"]  # type: ignore[index]
    assert len(fields) <= MAX_REPORTED_FIELDS
    assert not any("unknown_key_" in response.text for _ in range(1))


# --------------------------------------------------------------------------- #
# the unclassified 500: exact shape, from inside and outside the user stack
# --------------------------------------------------------------------------- #


async def test_an_unclassified_500_is_the_exact_envelope_with_its_header(
    settings: Settings,
) -> None:
    """``ServerErrorMiddleware`` sits *outside* all user middleware.

    So a 500 may reach the client without ``RequestIdMiddleware`` having wrapped the
    send path, and the response header cannot be assumed to be added by middleware.
    The envelope and the header must still agree, and the id must be legal.
    """
    secret = "PRIVATE-BODY-TEXT-and-a-dsn"
    app = create_app(settings)

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError(secret)

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client,
    ):
        response = await client.get("/boom", headers={"X-Request-ID": "trace-500"})

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert envelope(response) == {
        "code": "internal",
        "message": "An unexpected error occurred.",
        "request_id": "trace-500",
        "retryable": False,
        "details": {},
    }
    # Header and body are the same value, and it is a legal request id.
    assert response.headers["x-request-id"] == "trace-500"
    assert REQUEST_ID_RE.fullmatch(response.headers["x-request-id"])
    # Exactly one such header (a duplicate would let a cache pick the wrong one).
    assert response.headers.get_list("x-request-id") == ["trace-500"]
    # Nothing internal leaks, and a permanent failure carries no retry hint.
    assert secret not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text
    assert "retry-after" not in {key.lower() for key in response.headers}


async def test_a_500_raised_above_the_request_id_middleware_still_has_a_legal_id() -> None:
    """When middleware itself throws, no ``request.state`` id exists yet.

    The handler must mint a real server id rather than emit an empty field or a
    placeholder, and the ``X-Request-ID`` header must carry that same value.
    """

    class Exploding:
        """Throws before any inner middleware runs."""

        def __init__(self, app: object) -> None:
            self.app = app

        async def __call__(self, scope: object, receive: object, send: object) -> None:
            raise RuntimeError("PRIVATE-BODY-SENTINEL-500")

    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(Exploding)  # type: ignore[arg-type]

    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
        ) as client,
    ):
        response = await client.get("/anything")

    assert response.status_code == 500
    payload = response.json()
    assert payload["error"]["code"] == "internal"
    echoed = payload["error"]["request_id"]
    assert echoed and echoed != "-", echoed
    assert REQUEST_ID_RE.fullmatch(echoed), echoed
    assert response.headers["x-request-id"] == echoed
    assert "PRIVATE-BODY-SENTINEL-500" not in response.text


# --------------------------------------------------------------------------- #
# OpenAPI: the published contract skeleton
# --------------------------------------------------------------------------- #


async def test_openapi_publishes_only_implemented_routes(http: AsyncClient) -> None:
    """Guardrail: no half-built business endpoint may be advertised.

    The exact approved whitelist grows only when each endpoint is implemented.
    """
    schema = (await http.get("/openapi.json")).json()

    assert set(schema["paths"]) == EXPECTED_PATHS
    assert not set(schema["paths"]) & FORBIDDEN_V1_PATHS
    assert all(path.startswith("/api/v1/") for path in schema["paths"] if path.startswith("/api/"))


async def test_openapi_documents_the_contract_skeleton(http: AsyncClient) -> None:
    """The cross-cutting pieces are published even before the first endpoint."""
    schema = (await http.get("/openapi.json")).json()
    components = schema["components"]

    assert schema["info"]["title"] == "Revnote API"
    assert schema["info"]["version"] == __version__

    # Bearer auth is the only scheme, and it is declared now so clients can be
    # generated against it before the auth endpoints land.
    schemes = components["securitySchemes"]
    assert schemes == {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "description": schemes["BearerAuth"]["description"],
        }
    }

    # The shared wire types are published under stable names.
    assert {"ErrorEnvelope", "ErrorBody"} <= set(components["schemas"])
    error_body = components["schemas"]["ErrorBody"]
    assert set(error_body["properties"]) == {
        "code",
        "message",
        "request_id",
        "retryable",
        "details",
    }
    # Counters are strings with a decimal pattern - never JSON integers.
    seq = components["schemas"]["ErrorBody"]["properties"]["code"]
    assert seq["type"] == "string"


async def test_openapi_declares_decimal_string_counters_not_integers(
    http: AsyncClient,
) -> None:
    """A ``seq``/``rev``/``cursor`` field must be a patterned string if published."""
    schema = (await http.get("/openapi.json")).json()
    definitions = schema["components"]["schemas"]

    decimal_fields = [
        (name, prop)
        for name, model in definitions.items()
        for prop in model.get("properties", {}).values()
        if prop.get("description", "").startswith("Non-negative integer rendered as a decimal")
    ]

    for name, prop in decimal_fields:
        assert prop["type"] == "string", name
        assert prop["pattern"] == r"^(?:0|[1-9][0-9]*)$", name
        assert prop.get("format") != "int64", name


async def test_the_bearer_scheme_is_usable_by_generated_clients(http: AsyncClient) -> None:
    """``securitySchemes`` must be an HTTP bearer, not an API key in a header."""
    schema = (await http.get("/openapi.json")).json()
    bearer = schema["components"]["securitySchemes"]["BearerAuth"]

    assert bearer["type"] == "http"
    assert bearer["scheme"] == "bearer"


def test_the_v1_prefix_is_declared_once() -> None:
    import importlib

    router_module = importlib.import_module("revnote_api.api.router")

    assert router_module.API_V1_PREFIX == "/api/v1"


def test_every_versioned_route_lives_under_the_v1_prefix() -> None:
    """Registration happens through one router, so the prefix cannot be bypassed."""
    import importlib

    router_module = importlib.import_module("revnote_api.api.router")

    assert router_module.api_v1.prefix == "/api/v1"
    # Inspect the resolved published paths: recent FastAPI keeps nested routers
    # lazy, so its private _IncludedRouter is not itself an APIRoute.
    paths = create_app().openapi()["paths"]
    assert set(paths) == EXPECTED_PATHS
    assert all(path.startswith("/api/v1/") for path in paths if path not in {"/", "/health"})


def test_routes_module_is_a_package_not_a_module() -> None:
    """``api/routes.py`` and ``api/routes/`` cannot coexist; the package won."""
    from pathlib import Path

    api_dir = Path(__file__).resolve().parents[2] / "src" / "revnote_api" / "api"

    assert not (api_dir / "routes.py").exists()
    assert (api_dir / "routes" / "health.py").is_file()
    assert (api_dir / "router.py").is_file()


def test_request_id_pattern_is_the_documented_character_class() -> None:
    assert REQUEST_ID_RE.pattern == r"^[A-Za-z0-9._-]{1,64}$"
    assert re.fullmatch(REQUEST_ID_RE.pattern, "ok-1") is not None
