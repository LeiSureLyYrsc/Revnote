"""End-to-end command behaviour with a mocked transport: exit codes and output."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx
import pytest

from revnote_cli.__main__ import main
from revnote_cli_support import (
    capabilities,
    error_response,
    json_response,
    make_client,
    note_object,
)

pytestmark = pytest.mark.cli

WORKSPACE = "00000000-0000-4000-8000-000000000001"
NOTE_ID = note_object(1)["id"]


def test_login_prints_no_token_and_saves_the_single_workspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/login":
            return json_response(
                {
                    "access_token": "ACCESS-TOKEN-SENTINEL",
                    "refresh_token": "REFRESH-TOKEN-SENTINEL",
                    "token_type": "bearer",
                    "expires_in": 900,
                    "session_id": "00000000-0000-4000-8000-0000000000c1",
                    "session_expires_at": "2026-01-02T00:00:00Z",
                }
            )
        if request.url.path == "/api/v1/me":
            return json_response(
                {
                    "user": {"id": "u1", "email": "a@b.c", "created_at": "2026-01-01T00:00:00Z"},
                    "workspaces": [{"id": WORKSPACE, "role": "owner"}],
                }
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    client = make_client(tmp_path, handler, access_token=None, refresh_token=None)
    try:
        code = main(
            ["login", "--email", "a@b.c", "--password", "correct horse"],
            client=client,
        )
    finally:
        client.close()

    captured = capsys.readouterr()
    assert code == 0
    assert "a@b.c" in captured.out
    assert "ACCESS-TOKEN-SENTINEL" not in captured.out
    assert "ACCESS-TOKEN-SENTINEL" not in captured.err
    assert "REFRESH-TOKEN-SENTINEL" not in captured.out
    assert client.credentials.workspace_id == WORKSPACE
    saved = json.loads((tmp_path / "config" / "config.json").read_text(encoding="utf-8"))
    assert saved["access_token"] == "ACCESS-TOKEN-SENTINEL"


def test_me_without_a_token_exits_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - not reached
        return json_response({})

    client = make_client(tmp_path, handler, access_token=None, refresh_token=None)
    try:
        code = main(["me"], client=client)
    finally:
        client.close()

    captured = capsys.readouterr()
    assert code == 3
    assert "not_logged_in" in captured.err


def test_snapshot_json_is_one_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/capabilities":
            return json_response(capabilities(page_max_objects=10))
        return json_response(
            {
                "as_of_seq": "7",
                "min_retained_seq": "1",
                "objects": [note_object(1)],
                "next_cursor": None,
                "has_more": False,
            }
        )

    client = make_client(tmp_path, handler)
    try:
        code = main(["--json", "sync", "snapshot"], client=client)
    finally:
        client.close()

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)  # exactly one JSON document
    assert payload["as_of_seq"] == "7"
    assert payload["pages"] == 1
    assert payload["objects"][0]["id"] == NOTE_ID


def test_push_conflict_exits_four_and_names_the_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/workspaces/{WORKSPACE}/sync/push"
        return json_response(
            {
                "results": [
                    {
                        "op_id": "00000000-0000-4000-8000-0000000000d1",
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
        code = main(
            [
                "sync",
                "push",
                "note",
                "update",
                NOTE_ID,
                "--base-rev",
                "2",
                "--title",
                "changed",
            ],
            client=client,
        )
    finally:
        client.close()

    captured = capsys.readouterr()
    assert code == 4
    assert "status=conflict" in captured.out
    assert "rev_conflict" in captured.err

    def json_handler(request: httpx.Request) -> httpx.Response:
        return json_response(
            {
                "results": [
                    {
                        "op_id": "00000000-0000-4000-8000-0000000000d2",
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

    json_client = make_client(tmp_path, json_handler)
    try:
        json_code = main(
            ["--json", "sync", "push", "--ops-file", _ops_file(tmp_path)], client=json_client
        )
    finally:
        json_client.close()
    json_captured = capsys.readouterr()
    assert json_code == 4
    assert json.loads(json_captured.out)["results"][0]["status"] == "conflict"
    assert json.loads(json_captured.err)["error"]["code"] == "rev_conflict"


def test_push_without_a_source_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - not reached
        return json_response({})

    client = make_client(tmp_path, handler)
    try:
        code = main(["sync", "push"], client=client)
    finally:
        client.close()
    captured = capsys.readouterr()
    assert code == 2
    assert "usage" in captured.err


def test_attachment_upload_streams_the_bytes_and_the_digest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "note.txt"
    source.write_bytes(b"hello attachment")
    digest = hashlib.sha256(b"hello attachment").hexdigest()
    seen: dict[str, object] = {}
    attachment_id = "00000000-0000-4000-8000-0000000000e1"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert request.url.path == f"/api/v1/workspaces/{WORKSPACE}/attachments"
            upload_url = f"/api/v1/workspaces/{WORKSPACE}/attachments/{attachment_id}/content"
            return json_response(
                {
                    "attachment_id": attachment_id,
                    "state": "pending",
                    "upload": {"method": "PUT", "url": upload_url},
                    "expires_at": "2026-01-01T01:00:00Z",
                },
                201,
            )
        assert request.method == "PUT"
        seen["body"] = request.content
        seen["digest"] = request.headers.get("x-content-sha256")
        return json_response(
            {
                "attachment_id": attachment_id,
                "state": "ready",
                "content_hash": digest,
                "byte_size": len(b"hello attachment"),
            }
        )

    client = make_client(tmp_path, handler)
    try:
        code = main(
            ["attachment", "upload", str(source), "--workspace", WORKSPACE],
            client=client,
        )
    finally:
        client.close()

    captured = capsys.readouterr()
    assert code == 0
    assert seen == {"body": b"hello attachment", "digest": digest}
    assert attachment_id in captured.out


def test_attachment_download_writes_the_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    content = b"\x89PNG\r\n\x1a\nstreamed"
    attachment_id = "00000000-0000-4000-8000-0000000000e2"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v1/attachments/{attachment_id}/content"
        return httpx.Response(200, content=content, headers={"Content-Type": "image/png"})

    destination = tmp_path / "downloads" / "image.png"
    client = make_client(tmp_path, handler)
    try:
        code = main(
            ["attachment", "download", attachment_id, "--output", str(destination)],
            client=client,
        )
    finally:
        client.close()

    captured = capsys.readouterr()
    assert code == 0
    assert destination.read_bytes() == content
    assert f"bytes={len(content)}" in captured.out


def test_api_error_in_json_mode_is_an_error_object_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("not_found", 404, request_id="req-404")

    client = make_client(tmp_path, handler)
    try:
        code = main(["--json", "note", "get", NOTE_ID, "--workspace", WORKSPACE], client=client)
    finally:
        client.close()

    captured = capsys.readouterr()
    assert code == 6
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == "not_found"
    assert error["request_id"] == "req-404"


def _ops_file(tmp_path: Path) -> str:
    path = tmp_path / "ops.json"
    path.write_text(
        json.dumps(
            {
                "ops": [
                    {
                        "op_id": "00000000-0000-4000-8000-0000000000f1",
                        "entity_type": "note",
                        "op": "update",
                        "entity_id": NOTE_ID,
                        "base_rev": "2",
                        "payload": {"title": "t", "body_md": ""},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return str(path)
