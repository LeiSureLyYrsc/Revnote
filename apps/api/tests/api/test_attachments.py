"""The attachment endpoints over HTTP, against **real PostgreSQL and a real S3-compatible store**.

This module is the load-bearing evidence for the Phase 5 attachment claims (plan §7.1).
Nothing here is faked: the app is ``create_app(settings)``, the schema comes from the
real Alembic chain, the bearer token is a real one, and the bytes really travel through
the S3 adapter into a private bucket this run owns.

Why no double is acceptable at this layer
-----------------------------------------
Every claim below is a property of two real systems disagreeing correctly, and a fake
store can only restate what the code already believes:

* the digest is recomputed by **re-reading the object** (D6) - a fake would just hand
  back the number it was given;
* two concurrent ``PUT``s produce one ``200``, one ``409`` and **one** object - that is
  row locking and a CAS predicate, which no in-memory fake models;
* an attachment never touches the sync kernel - ``change_events`` and
  ``workspaces.last_seq`` are read back on a *separate* connection;
* the GC deletes expired ``pending`` rows and leaves ``ready`` alone - proven by listing
  the bucket and querying the table afterwards.

The environment contract is Lane B's (:mod:`pg_support.object_storage`):
``REQUIRE_OBJECT_STORAGE_TESTS=1`` plus ``S3_*``. When storage was promised
and is not usable, :func:`require_object_storage` raises and :func:`object_storage`
turns that into a **failure**, never a skip: a skipped attachment suite is
indistinguishable from a passing one in CI.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest

from pg_support.api_harness import ApiHarness, open_api
from pg_support.auth_support import Account
from pg_support.object_storage import (
    S3_ACCESS_KEY_ENV,
    S3_BUCKET_ENV,
    S3_ENDPOINT_ENV,
    S3_SECRET_KEY_ENV,
    ObjectStorageRequirementError,
    ObjectStorageTarget,
    describe,
    object_storage_is_required,
    purge_and_drop_bucket,
    require_object_storage,
)
from revnote_api.services.attachments import collect_expired, grace_interval_sql
from revnote_api.storage.bootstrap import ensure_bucket
from revnote_api.storage.s3 import S3BlobStore
from revnote_api.storage.settings import ObjectStorageSettings

pytestmark = [pytest.mark.anyio, pytest.mark.api, pytest.mark.attachments]

#: Version prefix of every business route.
API: Final[str] = "/api/v1"

#: A tiny but real PNG header plus payload; the bytes only have to be distinguishable.
PNG: Final[bytes] = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 4
PDF: Final[bytes] = b"%PDF-1.7\n" + b"x" * 2048


# --------------------------------------------------------------------------- #
# environment: real object storage, or a hard failure
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="session")
def object_storage() -> Iterator[ObjectStorageTarget]:
    """The run's private bucket, purged and dropped afterwards.

    ``require_object_storage`` refuses to guess: a promised-but-absent store is a
    ``pytest.fail`` here (never a skip), and the same call generates one bucket name
    per invocation - so the name is generated **once** and pinned into the environment
    the app reads, or ``build_store`` would target a bucket nobody created.
    """
    try:
        target = require_object_storage()
    except ObjectStorageRequirementError as exc:
        if object_storage_is_required():
            pytest.fail(str(exc), pytrace=False)
        pytest.skip(str(exc))

    store = _store_for(target)
    try:
        ensure_bucket(store)
    except Exception as exc:  # the store answered, but not usefully
        pytest.fail(
            f"object storage at {describe(target)} could not be prepared: {type(exc).__name__}",
            pytrace=False,
        )

    try:
        yield target
    finally:
        purge_and_drop_bucket(store)


@pytest.fixture(autouse=True)
def _storage_environment(
    object_storage: ObjectStorageTarget, monkeypatch: pytest.MonkeyPatch
) -> ObjectStorageTarget:
    """Point the app's own storage settings at this run's bucket.

    ``build_store`` constructs ``ObjectStorageSettings()`` from the environment, so the
    bucket has to arrive the same way production's would. Endpoint and credentials are
    expected to be set already (``require_object_storage`` demanded them); the bucket is
    per-run and therefore forced here.
    """
    monkeypatch.setenv(S3_BUCKET_ENV, object_storage.bucket)
    monkeypatch.setenv(S3_ENDPOINT_ENV, object_storage.endpoint)
    monkeypatch.setenv(S3_ACCESS_KEY_ENV, object_storage.access_key)
    monkeypatch.setenv(S3_SECRET_KEY_ENV, object_storage.secret_key)
    return object_storage


def _store_for(target: ObjectStorageTarget) -> S3BlobStore:
    """A real adapter bound to one target (the app builds its own from the env)."""
    return S3BlobStore(
        ObjectStorageSettings(
            _env_file=None,
            endpoint=target.endpoint,
            bucket=target.bucket,
            access_key=target.access_key,
            secret_key=target.secret_key,
            timeout_s=target.timeout_s,
        )
    )


@pytest.fixture
def store(object_storage: ObjectStorageTarget) -> S3BlobStore:
    """The adapter for the assertions that inspect objects directly."""
    return _store_for(object_storage)


def _objects_under(store: S3BlobStore, workspace_id: uuid.UUID) -> list[str]:
    """Every object key this workspace wrote (``ws/<ws>/...``), in listing order.

    Uses the paginator rather than one ``list_objects_v2`` call: a single page caps at
    1000 keys, so a truncated listing would make "exactly one object" assertions pass for
    the wrong reason on a busy store.
    """
    prefix = f"ws/{workspace_id}/"
    paginator = store.client.get_paginator("list_objects_v2")
    keys: list[str] = []
    for page in paginator.paginate(Bucket=store.settings.bucket, Prefix=prefix):
        for item in page.get("Contents", ()) or ():
            key = item.get("Key")
            if key:
                keys.append(key)
    return sorted(keys)


# --------------------------------------------------------------------------- #
# the app under test
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class AttachmentsHarness:
    """The API harness plus the calls this suite makes."""

    api: ApiHarness
    store: S3BlobStore

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.api.workspace_id

    # -- calls --------------------------------------------------------------- #
    async def create(
        self,
        *,
        mime: str = "image/png",
        byte_size: int | None = None,
        file_name: str | None = "diagram.png",
        workspace_id: uuid.UUID | None = None,
        token: str | None = None,
    ) -> httpx.Response:
        body: dict[str, Any] = {"mime": mime}
        if byte_size is not None:
            body["byte_size"] = byte_size
        if file_name is not None:
            body["file_name"] = file_name
        return await self.api.client.post(
            self.api.url(f"/workspaces/{workspace_id or self.workspace_id}/attachments"),
            json=body,
            headers=self.api.headers(token),
        )

    async def upload(
        self,
        attachment_id: uuid.UUID,
        payload: bytes,
        *,
        content_type: str = "image/png",
        declared_sha256: str | None = None,
        workspace_id: uuid.UUID | None = None,
        token: str | None = None,
    ) -> httpx.Response:
        """PUT the bytes as ``token``'s principal (default: the harness owner).

        ``token`` is not decoration: without it a "non-member" case would be sent as the
        owner and would exercise the owner's own authorisation, which is exactly the bug
        this parameter exists to rule out. The **path** workspace stays whatever the
        caller asked for, so ``upload(..., token=outsider)`` really is "an outsider
        uploading into this workspace".
        """
        request_headers = {**self.api.headers(token), "Content-Type": content_type}
        if declared_sha256 is not None:
            request_headers["X-Content-SHA256"] = declared_sha256
        return await self.api.client.put(
            self.api.url(
                f"/workspaces/{workspace_id or self.workspace_id}"
                f"/attachments/{attachment_id}/content"
            ),
            content=payload,
            headers=request_headers,
        )

    async def metadata(
        self, attachment_id: uuid.UUID, *, token: str | None = None
    ) -> httpx.Response:
        return await self.api.client.get(
            self.api.url(f"/attachments/{attachment_id}"),
            headers=self.api.headers(token),
        )

    async def content(
        self, attachment_id: uuid.UUID, *, token: str | None = None
    ) -> httpx.Response:
        return await self.api.client.get(
            self.api.url(f"/attachments/{attachment_id}/content"),
            headers=self.api.headers(token),
        )

    async def capabilities(self, *, token: str | None = None) -> httpx.Response:
        headers = {} if token is None else self.api.headers(token)
        return await self.api.client.get(self.api.url("/capabilities"), headers=headers)

    # -- helpers ------------------------------------------------------------- #
    async def upload_ready(self, payload: bytes = PNG, *, mime: str = "image/png") -> Any:
        """Create and upload in one step; returns ``(attachment_id, upload_json)``."""
        created = await self.create(mime=mime, byte_size=len(payload))
        assert created.status_code == 201, created.text
        attachment_id = uuid.UUID(created.json()["attachment_id"])
        uploaded = await self.upload(attachment_id, payload, content_type=mime)
        assert uploaded.status_code == 200, uploaded.text
        return attachment_id, uploaded.json()


@asynccontextmanager
async def open_attachments(
    *, overrides: dict[str, Any] | None = None, store: S3BlobStore
) -> AsyncIterator[AttachmentsHarness]:
    """A real app with attachments enabled, a seeded owner and a live token."""
    settings_overrides: dict[str, Any] = {"attachments_enabled": True}
    settings_overrides.update(overrides or {})
    async with open_api(overrides=settings_overrides) as api:
        yield AttachmentsHarness(api=api, store=store)


@pytest.fixture
async def app(store: S3BlobStore) -> AsyncIterator[AttachmentsHarness]:
    """The default harness: attachments enabled, the deployment cap in force."""
    async with open_attachments(store=store) as harness:
        yield harness


def error_code(response: httpx.Response) -> str:
    """The protocol code of a failed response (asserting it used the envelope)."""
    payload = response.json()
    assert set(payload) == {"error"}, payload
    return str(payload["error"]["code"])


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #
async def test_capabilities_reports_the_effective_policy(app: AttachmentsHarness) -> None:
    response = await app.capabilities()

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"attachments"}
    attachment_support = body["attachments"]
    assert attachment_support["enabled"] is True
    assert attachment_support["max_bytes"] == app.api.settings.attachment_max_bytes
    assert attachment_support["accepted_mime"] == app.api.settings.attachment_accepted_mime
    # No presigning anywhere: the allowlist names media types only.
    assert "image/svg+xml" not in attachment_support["accepted_mime"]
    assert response.headers.get("cache-control") == "no-store"


async def test_capabilities_is_reachable_without_a_token_and_always_200(
    store: S3BlobStore,
) -> None:
    """Feature detection must work before sign-in, and on a disabled deployment."""
    async with open_attachments(overrides={"attachments_enabled": False}, store=store) as app:
        anonymous = await app.capabilities()
        assert anonymous.status_code == 200, anonymous.text
        # Still the full object: "off" is information, not an error.
        assert anonymous.json()["attachments"]["enabled"] is False
        assert anonymous.json()["attachments"]["accepted_mime"]

        authenticated = await app.capabilities(token=app.api.token)
        assert authenticated.status_code == 200
        assert authenticated.json()["attachments"]["enabled"] is False


async def test_a_disabled_deployment_refuses_to_create_but_still_reads(
    store: S3BlobStore,
) -> None:
    """Reads keep working when the switch flips: existing data is not lost."""
    async with open_attachments(store=store) as enabled:
        attachment_id, _ = await enabled.upload_ready()

    async with open_attachments(overrides={"attachments_enabled": False}, store=store) as off:
        # A different workspace id, so only the *policy* differs, not the data.
        refused = await off.create()
        assert refused.status_code == 503, refused.text
        assert refused.json()["error"]["retryable"] is True
        # The metadata endpoint for the other workspace's row is a 404, not a 503:
        # authorisation still comes first.
        foreign = await off.metadata(attachment_id)
        assert foreign.status_code == 404


# --------------------------------------------------------------------------- #
# the happy path
# --------------------------------------------------------------------------- #
async def test_upload_then_download_is_byte_identical(app: AttachmentsHarness) -> None:
    created = await app.create(mime="image/png", byte_size=len(PNG), file_name="pixel.png")
    assert created.status_code == 201, created.text
    body = created.json()
    attachment_id = uuid.UUID(body["attachment_id"])

    assert body["state"] == "pending"
    assert body["upload"]["method"] == "PUT"
    # D4: a *relative* path, never a presigned URL.
    assert body["upload"]["url"] == (
        f"/api/v1/workspaces/{app.workspace_id}/attachments/{attachment_id}/content"
    )
    assert "://" not in body["upload"]["url"]
    assert body["expires_at"].endswith("Z")
    assert created.headers.get("cache-control") == "no-store"

    uploaded = await app.upload(attachment_id, PNG, content_type="image/png")
    assert uploaded.status_code == 200, uploaded.text
    ready = uploaded.json()
    assert ready["state"] == "ready"
    assert ready["content_hash"] == hashlib.sha256(PNG).hexdigest()
    assert ready["byte_size"] == len(PNG)

    metadata = await app.metadata(attachment_id)
    assert metadata.status_code == 200, metadata.text
    described = metadata.json()
    assert described["id"] == str(attachment_id)
    assert described["mime"] == "image/png"
    assert described["byte_size"] == len(PNG)
    assert described["content_hash"] == hashlib.sha256(PNG).hexdigest()
    assert described["file_name"] == "pixel.png"
    assert described["ready_at"] is not None
    assert metadata.headers.get("cache-control") == "no-store"

    downloaded = await app.content(attachment_id)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == PNG, "the stored bytes must come back unchanged"
    assert downloaded.headers["content-type"].startswith("image/png")
    assert downloaded.headers.get("cache-control") == "no-store"
    assert downloaded.headers.get("x-content-type-options") == "nosniff"


async def test_the_declared_digest_is_verified_against_the_stored_bytes(
    app: AttachmentsHarness,
) -> None:
    """``X-Content-SHA256`` is a claim; the server compares it with a re-read digest."""
    created = await app.create(mime="application/pdf", byte_size=len(PDF))
    attachment_id = uuid.UUID(created.json()["attachment_id"])

    accepted = await app.upload(
        attachment_id,
        PDF,
        content_type="application/pdf",
        declared_sha256=hashlib.sha256(PDF).hexdigest(),
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["content_hash"] == hashlib.sha256(PDF).hexdigest()


async def test_a_wrong_declared_digest_is_refused_and_leaves_no_ready_row(
    app: AttachmentsHarness,
) -> None:
    """The refusal must also remove the object: one attempt, no leftovers."""
    created = await app.create(mime="image/png", byte_size=len(PNG))
    attachment_id = uuid.UUID(created.json()["attachment_id"])

    rejected = await app.upload(
        attachment_id,
        PNG,
        content_type="image/png",
        declared_sha256="0" * 64,
    )

    assert rejected.status_code == 409, rejected.text
    assert error_code(rejected) == "attachment_hash_mismatch"
    # No ready row: the metadata endpoint cannot describe it.
    assert (await app.metadata(attachment_id)).status_code == 404
    # And no object survived the failed attempt.
    assert _objects_under(app.store, app.workspace_id) == []

    # The row is still pending, so a correct retry can complete it.
    retried = await app.upload(attachment_id, PNG, content_type="image/png")
    assert retried.status_code == 200, retried.text
    assert retried.json()["content_hash"] == hashlib.sha256(PNG).hexdigest()


async def test_a_second_upload_to_a_ready_attachment_is_refused_and_changes_nothing(
    app: AttachmentsHarness,
) -> None:
    """The object is immutable: the stored bytes and the digest stay the winner's."""
    attachment_id, first = await app.upload_ready(PNG)
    stored_before = _objects_under(app.store, app.workspace_id)
    assert len(stored_before) == 1

    second = await app.upload(attachment_id, PDF, content_type="image/png")

    assert second.status_code == 409, second.text
    assert error_code(second) == "attachment_already_ready"
    # Same digest, same object set: the second attempt wrote nothing that survived.
    metadata = await app.metadata(attachment_id)
    assert metadata.json()["content_hash"] == first["content_hash"]
    assert metadata.json()["byte_size"] == len(PNG)
    assert _objects_under(app.store, app.workspace_id) == stored_before
    assert (await app.content(attachment_id)).content == PNG


# --------------------------------------------------------------------------- #
# the single winner
# --------------------------------------------------------------------------- #
async def test_two_concurrent_uploads_produce_one_winner_and_one_object(
    app: AttachmentsHarness,
) -> None:
    """One ``200``, one ``409`` and exactly one object - never two.

    Both attempts stream through the real adapter to *different* keys (the key carries
    the upload nonce), so neither can corrupt the other's bytes; the winner is decided by
    the single conditional ``UPDATE``. The assertion on the object count is what proves
    the loser cleaned up after itself.
    """
    created = await app.create(mime="image/png", byte_size=len(PNG))
    attachment_id = uuid.UUID(created.json()["attachment_id"])

    first, second = await asyncio.gather(
        app.upload(attachment_id, PNG, content_type="image/png"),
        app.upload(attachment_id, PNG, content_type="image/png"),
    )

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 409], (first.text, second.text)
    loser = first if first.status_code == 409 else second
    assert error_code(loser) == "attachment_already_ready"

    keys = _objects_under(app.store, app.workspace_id)
    assert len(keys) == 1, f"expected exactly one object, found {keys}"
    # The surviving key is the winner's: it is the one the row names.
    metadata = await app.metadata(attachment_id)
    assert metadata.json()["content_hash"] == hashlib.sha256(PNG).hexdigest()
    assert (await app.content(attachment_id)).content == PNG


async def test_the_winner_of_a_concurrent_upload_keeps_its_own_bytes(
    app: AttachmentsHarness,
) -> None:
    """Two concurrent PUTs with **different** bytes: the winner's content survives.

    This is the case the identical-payload test above cannot see. When the object key
    was minted once per row, both attempts wrote the same key and the loser's cleanup
    deleted the *winner's* object - leaving a ``ready`` row with a digest and no bytes,
    permanently unreadable. Identical payloads hid it, because the two writes happened
    to be interchangeable.

    So the assertion is not merely "one object": it is "the row names a key whose bytes
    equal exactly one of the two payloads, and reading it back succeeds".
    """
    first_payload = PNG
    second_payload = PNG[: len(PNG) // 2] + b"different-tail" + PNG[len(PNG) // 2 :]

    created = await app.create(mime="image/png")
    attachment_id = uuid.UUID(created.json()["attachment_id"])

    first, second = await asyncio.gather(
        app.upload(attachment_id, first_payload, content_type="image/png"),
        app.upload(attachment_id, second_payload, content_type="image/png"),
    )

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 409], (first.text, second.text)

    # Exactly one object, and it is readable - not a digest without bytes.
    keys = _objects_under(app.store, app.workspace_id)
    assert len(keys) == 1, f"expected exactly one object, found {keys}"

    downloaded = await app.content(attachment_id)
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content in {first_payload, second_payload}, (
        "the surviving bytes must be one of the two attempts, verbatim"
    )
    # And the digest the row advertises must describe those bytes.
    metadata = await app.metadata(attachment_id)
    assert metadata.json()["content_hash"] == hashlib.sha256(downloaded.content).hexdigest()


async def test_replaying_the_same_upload_after_a_commit_is_still_one_row(
    app: AttachmentsHarness,
) -> None:
    """A lost response plus a client retry must not create a second ready row."""
    attachment_id, _ = await app.upload_ready(PNG)

    replay = await app.upload(attachment_id, PNG, content_type="image/png")

    assert replay.status_code == 409
    assert error_code(replay) == "attachment_already_ready"
    row = await app.api.db.conn()
    count = await row.fetchval(
        "SELECT count(*) FROM attachments WHERE workspace_id = $1 AND state = 'ready'",
        app.workspace_id,
    )
    assert count == 1


# --------------------------------------------------------------------------- #
# authorisation
# --------------------------------------------------------------------------- #
async def test_a_non_member_cannot_read_metadata_content_or_upload(
    app: AttachmentsHarness,
) -> None:
    """All three collapse to ``404 not_found``: existence is not observable (D20)."""
    attachment_id, _ = await app.upload_ready(PNG)
    other: Account = await app.api.app.seed_second_account()
    other_token = await app.api.login(other)

    for label, response in (
        ("metadata", await app.metadata(attachment_id, token=other_token)),
        ("content", await app.content(attachment_id, token=other_token)),
        (
            # Pointedly *not* the owner's token: the path workspace is the owner's, so a
            # caller who is not a member must be refused before any state is consulted.
            "upload",
            await app.upload(attachment_id, PNG, content_type="image/png", token=other_token),
        ),
    ):
        assert response.status_code == 404, (label, response.text)
        assert error_code(response) == "not_found", label

    # The same three calls, with a workspace id that does not exist - again as the outsider.
    unknown = uuid.uuid4()
    anonymous_create = await app.create(workspace_id=unknown, token=other_token)
    assert anonymous_create.status_code == 404
    assert error_code(anonymous_create) == "not_found"

    # The victim's attachment survived every refused call above: still ready, same bytes,
    # still exactly one object. A 404 that had side effects would not be a 404.
    assert (await app.metadata(attachment_id)).json()["state"] == "ready"
    assert (await app.content(attachment_id)).content == PNG
    assert len(_objects_under(app.store, app.workspace_id)) == 1

    # And a member of *another* workspace cannot reach this one's attachment. This is the
    # sharper of the two upload cases: the caller really is a member of ``other.workspace_id``,
    # so the membership gate passes and the 404 can only come from the service's own
    # ``(workspace_id, attachment_id)`` lookup. A path-scoped check alone would not catch it.
    crossed = await app.upload(
        attachment_id,
        PNG,
        content_type="image/png",
        workspace_id=other.workspace_id,
        token=other_token,
    )
    assert crossed.status_code == 404, crossed.text
    assert error_code(crossed) == "not_found"


async def test_every_attachment_endpoint_requires_a_token(app: AttachmentsHarness) -> None:
    attachment_id, _ = await app.upload_ready(PNG)

    for path, method in (
        (f"/workspaces/{app.workspace_id}/attachments", "post"),
        (f"/workspaces/{app.workspace_id}/attachments/{attachment_id}/content", "put"),
        (f"/attachments/{attachment_id}", "get"),
        (f"/attachments/{attachment_id}/content", "get"),
    ):
        response = await app.api.client.request(method, app.api.url(path))
        assert response.status_code == 401, (path, response.text)
        assert error_code(response) == "unauthenticated"


# --------------------------------------------------------------------------- #
# size and type limits
# --------------------------------------------------------------------------- #
async def test_a_declared_size_over_the_cap_is_attachment_too_large(
    store: S3BlobStore,
) -> None:
    """The *application* code, distinct from the middleware's ``payload_too_large``.

    A ``POST`` body is tiny (16 KiB budget), so this plane is reachable on every
    deployment: the client declares a size the cap refuses and learns which limit it hit.
    """
    async with open_attachments(overrides={"attachment_max_bytes": 1024}, store=store) as app:
        refused = await app.create(mime="image/png", byte_size=4096)

        assert refused.status_code == 413, refused.text
        assert error_code(refused) == "attachment_too_large"
        assert refused.json()["error"]["details"]["max_bytes"] == 1024
        # Nothing was created: no row, no object.
        assert _objects_under(app.store, app.workspace_id) == []


async def test_an_oversize_upload_body_is_refused_by_the_request_limiter(
    store: S3BlobStore,
) -> None:
    """The middleware budget is the configured cap, and its code is the generic one."""
    async with open_attachments(overrides={"attachment_max_bytes": 1024}, store=store) as app:
        created = await app.create(mime="image/png")
        attachment_id = uuid.UUID(created.json()["attachment_id"])

        refused = await app.upload(attachment_id, b"z" * 4096, content_type="image/png")

        assert refused.status_code == 413, refused.text
        assert error_code(refused) == "payload_too_large"
        # Still pending, and no object was written.
        assert (await app.metadata(attachment_id)).status_code == 404
        assert _objects_under(app.store, app.workspace_id) == []


async def test_an_svg_upload_is_refused(app: AttachmentsHarness) -> None:
    """D9: an SVG is a scriptable document, and it would be served from our origin."""
    refused = await app.create(mime="image/svg+xml")

    assert refused.status_code == 400, refused.text
    assert error_code(refused) == "attachment_unsupported_type"
    assert refused.json()["error"]["details"]["reason"] == "mime_svg"


async def test_a_content_type_that_disagrees_with_the_declared_mime_is_refused(
    app: AttachmentsHarness,
) -> None:
    created = await app.create(mime="image/png")
    attachment_id = uuid.UUID(created.json()["attachment_id"])

    refused = await app.upload(attachment_id, PDF, content_type="application/pdf")

    assert refused.status_code == 400, refused.text
    assert error_code(refused) == "attachment_unsupported_type"
    assert _objects_under(app.store, app.workspace_id) == []


async def test_an_empty_body_is_a_client_error_and_leaves_no_ready_row(
    app: AttachmentsHarness,
) -> None:
    """A zero-byte upload must be a 4xx, not a 500 from a violated CHECK.

    The table enforces ``CHECK (byte_size > 0)``, so publishing a zero-size object
    would raise a database constraint error - which the error surface reports as
    ``500 internal``. That reads as "the server broke" for what is really "you sent
    nothing", so the service refuses it first. The row must stay ``pending`` (the
    GC's responsibility) and the object must not linger.
    """
    created = await app.create(mime="image/png")
    attachment_id = uuid.UUID(created.json()["attachment_id"])

    refused = await app.upload(attachment_id, b"", content_type="image/png")

    assert refused.status_code == 400, refused.text
    assert error_code(refused) == "invalid_request"
    assert _objects_under(app.store, app.workspace_id) == []

    # Still ``pending``, so the ordinary grace-window GC owns the cleanup.
    metadata = await app.metadata(attachment_id)
    assert metadata.status_code == 404, "a pending attachment is not readable"


async def test_an_upload_to_an_expired_pending_row_is_refused(app: AttachmentsHarness) -> None:
    """The row outlived the grace window: refuse, and leave it for the GC."""
    created = await app.create(mime="image/png")
    attachment_id = uuid.UUID(created.json()["attachment_id"])

    connection = await app.api.db.conn()
    await connection.execute(
        "UPDATE attachments SET created_at = now() - interval '2 days' WHERE id = $1",
        attachment_id,
    )

    refused = await app.upload(attachment_id, PNG, content_type="image/png")
    assert refused.status_code == 409, refused.text
    assert error_code(refused) == "attachment_expired"
    assert _objects_under(app.store, app.workspace_id) == []


async def test_an_unknown_attachment_is_not_found_on_every_path(app: AttachmentsHarness) -> None:
    unknown = uuid.uuid4()

    assert (await app.metadata(unknown)).status_code == 404
    assert (await app.content(unknown)).status_code == 404
    upload = await app.upload(unknown, PNG, content_type="image/png")
    assert upload.status_code == 404
    assert error_code(upload) == "not_found"


# --------------------------------------------------------------------------- #
# an attachment is not a kernel entity
# --------------------------------------------------------------------------- #
async def test_an_attachment_never_moves_the_sync_cursor_or_the_event_log(
    app: AttachmentsHarness,
) -> None:
    """Plan §3.1/D3: no ``change_events`` row, no ``last_seq`` bump, no pull object."""
    before = await app.api.state()
    assert before.last_seq == 0

    attachment_id, _ = await app.upload_ready()

    after = await app.api.state()
    assert after.last_seq == before.last_seq, "an upload must not allocate a seq"
    assert after.event_seqs == before.event_seqs
    assert after.ledger_rows == before.ledger_rows

    # Pull and snapshot are byte-for-byte unchanged in shape and content.
    page = await app.api.pull(since=0)
    assert page.status_code == 200, page.text
    assert page.json()["events"] == []
    assert page.json()["objects"] == []
    assert page.json()["last_seq"] == "0"

    snapshot = await app.api.snapshot()
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["objects"] == []
    assert snapshot.json()["as_of_seq"] == "0"

    # The tree does not mention attachments either.
    tree = await app.api.tree()
    assert tree.status_code == 200
    assert tree.json() == {"folders": [], "notes": []}
    assert "attachment" not in tree.text.lower()

    # And the row exists exactly once, in state ready.
    connection = await app.api.db.conn()
    state = await connection.fetchval("SELECT state FROM attachments WHERE id = $1", attachment_id)
    assert state == "ready"


async def test_a_note_referencing_an_attachment_is_still_an_ordinary_note(
    app: AttachmentsHarness,
) -> None:
    """D7: the reference lives in Markdown as a relative URL - no new column, no event type."""
    from pg_support.api_harness import note_create

    attachment_id, _ = await app.upload_ready()
    body_md = f"![pixel](/api/v1/attachments/{attachment_id}/content)"

    pushed = await app.api.push([note_create(title="with image", body_md=body_md)])
    assert pushed.status_code == 200, pushed.text

    page = await app.api.pull(since=0)
    assert page.status_code == 200
    note = next(obj for obj in page.json()["objects"] if obj["entity_type"] == "note")
    assert note["body_md"] == body_md
    # The stored URL still resolves - the note is a plain note, the bytes are elsewhere.
    download = await app.content(attachment_id)
    assert download.status_code == 200
    assert download.content == PNG


# --------------------------------------------------------------------------- #
# garbage collection
# --------------------------------------------------------------------------- #
async def _expired_pending_ids(connection: Any, *, grace_s: int) -> set[uuid.UUID]:
    """Every id the GC's own scan would collect right now, across the whole database.

    ``collect_expired`` is a *global operator sweep* - it is not scoped to a workspace -
    so any count it returns describes the entire table. Several tests in this session
    leave expired ``pending`` rows behind on purpose, which makes a bare
    ``report.rows_deleted == 1`` a statement about test order rather than about the GC.

    Reading the eligible set with the service's **own** interval expression keeps the
    assertion exact instead of vague: the pre-count and the sweep cannot disagree about
    the window, so the expected count is deterministic even though the set is shared.
    """
    rows = await connection.fetch(
        "SELECT id FROM attachments"
        " WHERE state = 'pending'"
        f"   AND created_at < now() - {grace_interval_sql(grace_s)}"
    )
    return {row["id"] for row in rows}


async def test_gc_removes_expired_pending_rows_and_leaves_ready_ones(
    app: AttachmentsHarness,
) -> None:
    """The GC's whole contract, against the real table and the real bucket."""
    ready_id, _ = await app.upload_ready(PNG)

    abandoned = await app.create(mime="image/png")
    pending_id = uuid.UUID(abandoned.json()["attachment_id"])
    connection = await app.api.db.conn()
    await connection.execute(
        "UPDATE attachments SET created_at = now() - interval '2 days' WHERE id = $1",
        pending_id,
    )

    # A pending row inside the grace window must survive.
    fresh = await app.create(mime="image/png")
    fresh_id = uuid.UUID(fresh.json()["attachment_id"])

    grace_s = app.api.settings.attachment_pending_grace_s
    eligible = await _expired_pending_ids(connection, grace_s=grace_s)
    assert pending_id in eligible, "the row this test expired is eligible"
    assert ready_id not in eligible, "a ready row is never a candidate, however old"
    assert fresh_id not in eligible, "a row inside the grace window is not a candidate"

    report = await collect_expired(app.api.app.factory, app.store, grace_s=grace_s)

    assert report.failed == 0
    # The sweep collected exactly the eligible set the database reported - no row more
    # (which would mean it reached something it should not have) and no row fewer.
    assert report.scanned == len(eligible), "the sweep scans the whole database"
    assert report.rows_deleted == len(eligible)
    # Ordering is row first, then object: one successful object delete per removed row.
    assert report.objects_deleted == report.rows_deleted
    # Nothing expired survives the sweep - including other workspaces' leftovers.
    assert await _expired_pending_ids(connection, grace_s=grace_s) == set()

    remaining = await connection.fetch(
        "SELECT id, state FROM attachments WHERE workspace_id = $1 ORDER BY created_at",
        app.workspace_id,
    )
    states = {uuid.UUID(str(row["id"])): row["state"] for row in remaining}
    assert ready_id in states and states[ready_id] == "ready", "ready is never collected"
    assert fresh_id in states and states[fresh_id] == "pending"
    assert pending_id not in states, "the expired pending row is gone"

    # The ready attachment's bytes are untouched.
    assert (await app.content(ready_id)).content == PNG
    assert len(_objects_under(app.store, app.workspace_id)) == 1


async def test_gc_dry_run_reports_without_deleting(app: AttachmentsHarness) -> None:
    abandoned = await app.create(mime="image/png")
    pending_id = uuid.UUID(abandoned.json()["attachment_id"])
    connection = await app.api.db.conn()
    await connection.execute(
        "UPDATE attachments SET created_at = now() - interval '2 days' WHERE id = $1",
        pending_id,
    )

    grace_s = app.api.settings.attachment_pending_grace_s
    eligible = await _expired_pending_ids(connection, grace_s=grace_s)
    assert pending_id in eligible

    report = await collect_expired(app.api.app.factory, app.store, grace_s=grace_s, dry_run=True)

    # Same global-count reasoning as the sweep above: compare against the eligible set.
    assert report.scanned == len(eligible)
    assert report.rows_deleted == len(eligible)
    # A dry run deletes nothing, so the eligible set is byte-for-byte unchanged after it.
    assert await _expired_pending_ids(connection, grace_s=grace_s) == eligible
    still_there = await connection.fetchval(
        "SELECT count(*) FROM attachments WHERE id = $1", pending_id
    )
    assert still_there == 1, "a dry run must delete nothing"


# --------------------------------------------------------------------------- #
# the storage key is the one the contract names
# --------------------------------------------------------------------------- #
async def test_the_object_lives_under_the_contract_key(app: AttachmentsHarness) -> None:
    """``ws/{workspace}/att/{attachment}/{nonce}`` - four segments, no file name."""
    attachment_id, _ = await app.upload_ready(PNG)

    keys = _objects_under(app.store, app.workspace_id)
    assert len(keys) == 1
    key = keys[0]
    assert key.startswith(f"ws/{app.workspace_id}/att/{attachment_id}/")
    # The segments after the attachment id are the nonce: one UUID, nothing else.
    nonce = key.rsplit("/", 1)[1]
    assert str(uuid.UUID(nonce)) == nonce
    # A file name never contributes a byte to a key.
    assert "pixel" not in key and "diagram" not in key
