"""Helpers shared by the CLI test tiers (not a conftest: tests import it)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from revnote_cli.client import RevnoteClient
from revnote_cli.config import CredentialStore, StoredCredentials

API_BASE = "http://api.test"


def json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload, headers={"Content-Type": "application/json"})


def error_response(
    code: str,
    status: int,
    *,
    message: str = "The request does not match the API contract.",
    request_id: str = "req-test",
    retryable: bool = False,
    details: dict[str, Any] | None = None,
) -> httpx.Response:
    return json_response(
        {
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id,
                "retryable": retryable,
                "details": details or {},
            }
        },
        status,
    )


def make_client(
    tmp_path: Path,
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    access_token: str | None = "access-1",
    refresh_token: str | None = "refresh-1",
    api_base: str = API_BASE,
) -> RevnoteClient:
    """A client whose credentials live in ``tmp_path`` and whose HTTP is mocked."""
    store = CredentialStore(tmp_path / "config")
    credentials = StoredCredentials(
        api_base=api_base,
        access_token=access_token,
        refresh_token=refresh_token,
        workspace_id="00000000-0000-4000-8000-000000000001",
    )
    store.save(credentials)
    return RevnoteClient(credentials, store, transport=httpx.MockTransport(handler))


def capabilities(
    *,
    pagination: bool = True,
    page_max_objects: int = 1000,
) -> dict[str, Any]:
    return {
        "attachments": {
            "enabled": False,
            "max_bytes": 26214400,
            "accepted_mime": ["image/png"],
        },
        "joplin_bridge": {"enabled": False, "protocol_baseline": "3.7"},
        "snapshot_pagination": {
            "enabled": pagination,
            "page_max_objects": page_max_objects,
            "page_max_bytes": 4194304,
            "max_total_objects": 100000,
        },
    }


def note_object(index: int, *, rev: str = "1") -> dict[str, Any]:
    return {
        "entity_type": "note",
        "id": f"00000000-0000-4000-8000-{index:012d}",
        "workspace_id": "00000000-0000-4000-8000-000000000001",
        "rev": rev,
        "updated_seq": "1",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "deleted": False,
        "deleted_at": None,
        "title": f"Note {index}",
        "body_md": f"Body {index}",
        "folder_id": None,
    }
