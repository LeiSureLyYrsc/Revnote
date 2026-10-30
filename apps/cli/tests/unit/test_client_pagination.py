"""Pagination, auth retry and error mapping of the HTTP client.

Everything here runs on ``httpx.MockTransport``: the client's own behaviour is
what is under test, not the server. The real-protocol cases live in
``tests/integration`` and talk to a migrated PostgreSQL through uvicorn.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from revnote_cli.client import RevnoteClient
from revnote_cli.config import StoredCredentials
from revnote_cli.errors import EXIT_API, EXIT_AUTH, EXIT_CONFLICT, EXIT_USAGE, CliError
from revnote_cli_support import (
    capabilities,
    error_response,
    json_response,
    make_client,
    note_object,
)

pytestmark = pytest.mark.cli

WORKSPACE = "00000000-0000-4000-8000-000000000001"


def _page(
    objects: list[dict[str, Any]],
    *,
    as_of: str = "9",
    min_retained: str = "9",
    next_cursor: str | None = None,
) -> dict[str, Any]:
    return {
        "as_of_seq": as_of,
        "min_retained_seq": min_retained,
        "objects": objects,
        "next_cursor": next_cursor,
        "has_more": next_cursor is not None,
    }


def test_snapshot_consumes_every_page_and_pins_the_watermark(tmp_path: Path) -> None:
    pages = {
        None: _page([note_object(1), note_object(2)], min_retained="8", next_cursor="c1"),
        "c1": _page([note_object(3), note_object(4)], min_retained="8", next_cursor="c2"),
        "c2": _page([note_object(5)], min_retained="9"),
    }
    seen_limits: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/capabilities":
            return json_response(capabilities(page_max_objects=2))
        assert request.url.path == f"/api/v1/workspaces/{WORKSPACE}/sync/snapshot"
        seen_limits.append(request.url.params.get("limit"))
        cursor = request.url.params.get("cursor")
        return json_response(pages[cursor])

    client = make_client(tmp_path, handler)
    try:
        result = client.snapshot(WORKSPACE)
    finally:
        client.close()

    assert result["paged"] is True
    assert result["pages"] == 3
    assert result["as_of_seq"] == "9"
    assert result["min_retained_seq"] == "9"
    assert [obj["id"] for obj in result["objects"]] == [
        note_object(index)["id"] for index in range(1, 6)
    ]
    # The capability's page maximum bounds the requested limit.
    assert seen_limits == ["2", "2", "2"]


def test_snapshot_rejects_a_moving_watermark(tmp_path: Path) -> None:
    pages = {
        None: _page([note_object(1)], next_cursor="c1"),
        "c1": _page([note_object(2)], as_of="10"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/capabilities":
            return json_response(capabilities())
        cursor = request.url.params.get("cursor")
        return json_response(pages[cursor])

    client = make_client(tmp_path, handler)
    try:
        with pytest.raises(CliError) as raised:
            client.snapshot(WORKSPACE)
    finally:
        client.close()
    assert raised.value.code == "invalid_response"
    assert raised.value.exit_code == EXIT_API


def test_snapshot_rejects_a_duplicate_object_across_pages(tmp_path: Path) -> None:
    pages = {
        None: _page([note_object(1)], next_cursor="c1"),
        "c1": _page([note_object(1)]),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/capabilities":
            return json_response(capabilities())
        return json_response(pages[request.url.params.get("cursor")])

    client = make_client(tmp_path, handler)
    try:
        with pytest.raises(CliError) as raised:
            client.snapshot(WORKSPACE)
    finally:
        client.close()
    assert raised.value.code == "invalid_response"


def test_snapshot_rejects_has_more_without_a_cursor(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/capabilities":
            return json_response(capabilities())
        return json_response(_page([note_object(1)], next_cursor=None) | {"has_more": True})

    client = make_client(tmp_path, handler)
    try:
        with pytest.raises(CliError) as raised:
            client.snapshot(WORKSPACE)
    finally:
        client.close()
    assert raised.value.code == "invalid_response"


def test_snapshot_uses_the_legacy_read_when_the_capability_is_absent(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/v1/capabilities":
            return json_response(capabilities(pagination=False))
        return json_response(
            {
                "as_of_seq": "5",
                "min_retained_seq": "1",
                "objects": [note_object(1)],
            }
        )

    client = make_client(tmp_path, handler)
    try:
        result = client.snapshot(WORKSPACE)
    finally:
        client.close()

    assert result["paged"] is False
    assert result["pages"] == 1
    assert result["objects"][0]["id"] == note_object(1)["id"]
    assert seen == [
        "/api/v1/capabilities",
        f"/api/v1/workspaces/{WORKSPACE}/sync/snapshot",
    ]


def test_snapshot_falls_back_when_the_capability_endpoint_is_missing(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/v1/capabilities":
            return error_response("not_found", 404)
        return json_response(
            {"as_of_seq": "5", "min_retained_seq": "1", "objects": [note_object(1)]}
        )

    client = make_client(tmp_path, handler)
    try:
        result = client.snapshot(WORKSPACE)
    finally:
        client.close()

    assert result["paged"] is False
    assert seen == [
        "/api/v1/capabilities",
        f"/api/v1/workspaces/{WORKSPACE}/sync/snapshot",
    ]


def test_explicit_page_size_over_the_server_maximum_is_a_usage_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(capabilities(page_max_objects=10))

    client = make_client(tmp_path, handler)
    try:
        with pytest.raises(CliError) as raised:
            client.snapshot(WORKSPACE, page_size=11)
    finally:
        client.close()
    assert raised.value.exit_code == EXIT_USAGE


def test_pull_chains_cursors_and_keeps_the_latest_object(tmp_path: Path) -> None:
    pages = {
        "0": {
            "events": [
                {
                    "seq": "1",
                    "entity_type": "note",
                    "entity_id": note_object(1)["id"],
                    "op": "create",
                    "rev": "1",
                    "op_id": "00000000-0000-4000-8000-0000000000a1",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
            "objects": [note_object(1)],
            "next_cursor": "1",
            "has_more": True,
            "last_seq": "3",
            "min_retained_seq": "1",
        },
        "1": {
            "events": [
                {
                    "seq": "2",
                    "entity_type": "note",
                    "entity_id": note_object(1)["id"],
                    "op": "update",
                    "rev": "2",
                    "op_id": "00000000-0000-4000-8000-0000000000a2",
                    "created_at": "2026-01-01T00:00:01Z",
                },
                {
                    "seq": "3",
                    "entity_type": "note",
                    "entity_id": note_object(2)["id"],
                    "op": "create",
                    "rev": "1",
                    "op_id": "00000000-0000-4000-8000-0000000000a3",
                    "created_at": "2026-01-01T00:00:02Z",
                },
            ],
            "objects": [note_object(1, rev="2"), note_object(2)],
            "next_cursor": "3",
            "has_more": False,
            "last_seq": "3",
            "min_retained_seq": "1",
        },
    }
    during: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        during.append(request.url.params.get("since", ""))
        return json_response(pages[request.url.params["since"]])

    client = make_client(tmp_path, handler)
    try:
        result = client.pull(WORKSPACE, since="0")
    finally:
        client.close()

    assert during == ["0", "1"]
    assert [event["seq"] for event in result["events"]] == ["1", "2", "3"]
    assert len(result["objects"]) == 2
    assert result["objects"][0]["rev"] == "2"
    assert result["next_cursor"] == "3"
    assert result["pages"] == 2


def test_pull_once_returns_only_the_first_page(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return json_response(
            {
                "events": [],
                "objects": [],
                "next_cursor": "7",
                "has_more": True,
                "last_seq": "9",
                "min_retained_seq": "1",
            }
        )

    client = make_client(tmp_path, handler)
    try:
        result = client.pull(WORKSPACE, since="6", drain=False)
    finally:
        client.close()
    assert calls == 1
    assert result["has_more"] is True
    assert result["next_cursor"] == "7"


def test_401_refreshes_once_and_persists_the_new_pair(tmp_path: Path) -> None:
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/refresh":
            assert request.content
            calls.append("refresh")
            return json_response(
                {
                    "access_token": "access-2",
                    "refresh_token": "refresh-2",
                    "token_type": "bearer",
                    "expires_in": 900,
                    "session_id": "session-1",
                    "session_expires_at": "2026-01-02T00:00:00Z",
                }
            )
        calls.append(request.headers.get("authorization"))
        if request.headers.get("authorization") == "Bearer access-2":
            return json_response({"user": {"id": "u1", "email": "a@b.c"}, "workspaces": []})
        return error_response("unauthenticated", 401, message="Authentication is required.")

    client = make_client(tmp_path, handler)
    try:
        payload = client.me()
    finally:
        client.close()

    assert payload["user"]["id"] == "u1"
    assert calls == ["Bearer access-1", "refresh", "Bearer access-2"]
    assert client.credentials.access_token == "access-2"
    assert client.store.load().access_token == "access-2"  # type: ignore[union-attr]


def test_push_conflict_is_reported_as_a_result_not_raised(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {
                "results": [
                    {
                        "op_id": "00000000-0000-4000-8000-0000000000b1",
                        "status": "conflict",
                        "seq": None,
                        "rev": None,
                        "error": {
                            "code": "rev_conflict",
                            "message": "The object changed since the given revision.",
                            "request_id": "req-1",
                            "retryable": False,
                            "details": {},
                        },
                    }
                ]
            }
        )

    client = make_client(tmp_path, handler)
    try:
        payload = client.push(WORKSPACE, [])
    finally:
        client.close()
    assert payload["results"][0]["status"] == "conflict"


def test_error_envelope_maps_to_a_documented_exit_code(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("rev_conflict", 409, request_id="req-9")

    client = make_client(tmp_path, handler)
    try:
        with pytest.raises(CliError) as raised:
            client.me()
    finally:
        client.close()
    assert raised.value.exit_code == EXIT_CONFLICT
    assert raised.value.request_id == "req-9"
    assert raised.value.code == "rev_conflict"


def test_a_transport_failure_is_exit_five(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    client = make_client(tmp_path, handler)
    try:
        with pytest.raises(CliError) as raised:
            client.me()
    finally:
        client.close()
    assert raised.value.exit_code == EXIT_API
    assert raised.value.code == "network_error"


def test_missing_access_token_is_an_auth_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - not reached
        return json_response({})

    client: RevnoteClient = make_client(tmp_path, handler, access_token=None, refresh_token=None)
    try:
        with pytest.raises(CliError) as raised:
            client.me()
    finally:
        client.close()
    assert raised.value.exit_code == EXIT_AUTH
    assert raised.value.code == "not_logged_in"
    assert isinstance(client.credentials, StoredCredentials)
