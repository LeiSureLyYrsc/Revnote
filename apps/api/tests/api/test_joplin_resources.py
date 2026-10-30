"""Phase 6: Joplin ``.resource`` blobs on the Phase 5 attachments + real RustFS.

The load-bearing claim is that the bridge does **not** invent storage: a
``.resource/<key>`` upload enters the same state machine, gets the same object
key, the same re-read SHA-256 and the same immutable ``ready`` row as a native
upload; a second client reads the identical bytes back through the bridge.

The store is real (RustFS in CI). ``REQUIRE_OBJECT_STORAGE_TESTS=1`` plus the
``S3_*`` variables is the contract; a promised-but-absent store fails, never
skips.

Known, documented limitation: Phase 5 has no attachment deletion, so a
``DELETE .resource/<key>`` is accepted and the bytes stay. The last case pins
that instead of pretending otherwise.
"""

from __future__ import annotations

import hashlib
import os
import uuid

import asyncpg
import pytest

from pg_support.api_harness import asyncpg_dsn, open_api
from pg_support.object_storage import (
    ObjectStorageTarget,
    purge_and_drop_bucket,
    require_object_storage,
)
from revnote_api.storage.bootstrap import ensure_bucket
from revnote_api.storage.s3 import S3BlobStore
from revnote_api.storage.settings import ObjectStorageSettings

pytestmark = [pytest.mark.anyio, pytest.mark.joplin]

BRIDGE = "/api"
KEY = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 3


@pytest.fixture(scope="session")
def bridge_bucket() -> ObjectStorageTarget:
    """One real bucket for this run; the app builds its store from the same env."""
    target = require_object_storage()
    # ``AttachmentService`` inside the bridge builds ``build_store(settings)`` from
    # the process environment, so the bucket the fixture created must be the one
    # the environment names - otherwise the test would write somewhere nobody
    # created and fail on the first PUT with a misleading 503.
    os.environ["S3_BUCKET"] = target.bucket
    store = S3BlobStore(ObjectStorageSettings())
    ensure_bucket(store)
    yield target
    purge_and_drop_bucket(store, target.bucket)


def resource_text(*, key: str, mime: str, filename: str, size: int) -> str:
    return "\n\n".join(
        [
            filename,
            "\n".join(
                [
                    f"id: {key}",
                    "parent_id: ",
                    f"mime: {mime}",
                    f"filename: {filename}",
                    f"size: {size}",
                    f"file_extension: {filename.rsplit('.', 1)[-1]}",
                    "created_time: 2026-10-30T08:00:00.000Z",
                    "updated_time: 2026-10-30T08:00:00.000Z",
                    "type_: 3",
                ]
            ),
        ]
    )


async def bridge_login(harness) -> str:
    response = await harness.client.post(
        f"{BRIDGE}/sessions",
        json={"email": harness.account.email, "password": harness.account.password},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


def auth(token: str) -> dict[str, str]:
    return {"X-API-AUTH": token}


async def put_metadata(harness, token: str, key: str, mime: str, filename: str, size: int):
    return await harness.client.put(
        f"{BRIDGE}/items/root:/{key}.md:/content",
        content=resource_text(key=key, mime=mime, filename=filename, size=size).encode("utf-8"),
        headers={**auth(token), "Content-Type": "application/octet-stream"},
    )


async def put_blob(harness, token: str, key: str, payload: bytes):
    return await harness.client.put(
        f"{BRIDGE}/items/root:/.resource/{key}:/content",
        content=payload,
        headers={**auth(token), "Content-Type": "application/octet-stream"},
    )


async def test_a_resource_round_trips_into_the_attachment_state_machine(
    bridge_bucket: ObjectStorageTarget,
) -> None:
    async with open_api(
        overrides={"joplin_bridge_enabled": True, "attachments_enabled": True}
    ) as harness:
        token = await bridge_login(harness)

        metadata = await put_metadata(harness, token, KEY, "image/png", "pixel.png", len(PNG))
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["jop_type"] == 3

        uploaded = await put_blob(harness, token, KEY, PNG)
        assert uploaded.status_code == 200, uploaded.text

        downloaded = await harness.client.get(
            f"{BRIDGE}/items/root:/.resource/{KEY}:/content", headers=auth(token)
        )
        assert downloaded.status_code == 200
        assert downloaded.content == PNG
        assert downloaded.headers["content-type"] == "image/png"
        assert downloaded.headers["cache-control"] == "no-store"

        # The bytes are a real ``ready`` attachment row, with the stored digest.
        connection = await asyncpg.connect(asyncpg_dsn(harness.app.database_url))
        try:
            row = await connection.fetchrow(
                "SELECT a.state, a.byte_size, a.mime, a.content_hash, a.storage_key "
                "  FROM attachments AS a WHERE a.workspace_id = $1",
                harness.workspace_id,
            )
        finally:
            await connection.close()
        assert row is not None
        assert row["state"] == "ready"
        assert row["byte_size"] == len(PNG)
        assert row["mime"] == "image/png"
        assert bytes(row["content_hash"]).hex() == hashlib.sha256(PNG).hexdigest()
        assert row["storage_key"].startswith(f"ws/{harness.workspace_id}/att/")
        assert row["storage_key"].count("/") == 4

        # The metadata item reads back as a type-3 item the client can parse.
        item = await harness.client.get(
            f"{BRIDGE}/items/root:/{KEY}.md:/content", headers=auth(token)
        )
        assert item.status_code == 200
        assert "type_: 3" in item.text
        assert "mime: image/png" in item.text
        assert f"size: {len(PNG)}" in item.text


async def test_an_unsupported_resource_type_is_permanently_rejected(
    bridge_bucket: ObjectStorageTarget,
) -> None:
    async with open_api(
        overrides={"joplin_bridge_enabled": True, "attachments_enabled": True}
    ) as harness:
        token = await bridge_login(harness)
        response = await put_metadata(harness, token, KEY, "application/zip", "archive.zip", 1024)
        # 422 is "rejectedByTarget" for the client: retrying would never succeed.
        assert response.status_code == 422
        assert response.json()["code"] == "unsupported_type"


async def test_a_resource_delete_is_accepted_and_the_bytes_remain(
    bridge_bucket: ObjectStorageTarget,
) -> None:
    async with open_api(
        overrides={"joplin_bridge_enabled": True, "attachments_enabled": True}
    ) as harness:
        token = await bridge_login(harness)
        assert (
            await put_metadata(harness, token, KEY, "text/plain", "notes.txt", 5)
        ).status_code == 200
        assert (await put_blob(harness, token, KEY, b"hello")).status_code == 200

        deleted = await harness.client.delete(
            f"{BRIDGE}/items/root:/.resource/{KEY}:", headers=auth(token)
        )
        assert deleted.status_code == 204

        # Phase 5 has no attachment deletion (D13). The client sees its delete
        # accepted; the object is intentionally still there.
        still_there = await harness.client.get(
            f"{BRIDGE}/items/root:/.resource/{KEY}:/content", headers=auth(token)
        )
        assert still_there.status_code == 200
        assert still_there.content == b"hello"


async def test_a_resource_upload_is_refused_when_the_deployment_has_no_storage(
    bridge_bucket: ObjectStorageTarget,
) -> None:
    async with open_api(
        overrides={"joplin_bridge_enabled": True, "attachments_enabled": False}
    ) as harness:
        token = await bridge_login(harness)
        response = await put_metadata(harness, token, uuid.uuid4().hex, "image/png", "x.png", 1)
        assert response.status_code == 503
        assert response.json()["code"] == "storage_unavailable"
