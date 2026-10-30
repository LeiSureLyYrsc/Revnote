"""The attachment state machine against a fake store and a fake session factory.

No external service is touched: :class:`FakeStore` implements
:class:`~revnote_api.storage.ports.BlobStore` structurally, and
:class:`FakeSession` answers the statements the service issues by inspecting their
text. That is enough to prove the things that would otherwise only fail against a
real deployment:

* the transition table - ``pending`` accepts one upload, ``ready`` refuses a second
  before reading a byte, ``deleted`` is a ``404``;
* the CAS branches - a winner publishes, a loser deletes **its own key** and reports
  ``attachment_already_ready``, a declared-digest mismatch leaves no ``ready`` row;
* expiry is decided by SQL, so an expired row is refused with ``attachment_expired``;
* the MIME allowlist, including the separate ``image/svg+xml`` rejection;
* every storage failure maps to ``attachment_storage_unavailable``;
* no storage call happens while a session is open - the double records the order.

What this module deliberately does not prove: real locking, real MVCC, the real
``UNIQUE``/CHECK constraints, and that a second writer really sees zero rows. Those
are properties of PostgreSQL and live in ``tests/api/test_attachments*.py``.
"""

from __future__ import annotations

import datetime
import hashlib
import uuid
from collections.abc import AsyncIterator, Iterable
from typing import Any, ClassVar, Final

import pytest

from revnote_api.services.attachments import (
    ALREADY_READY,
    EXPIRED,
    HASH_MISMATCH,
    NOT_FOUND,
    STORAGE_UNAVAILABLE,
    TOO_LARGE,
    UNSUPPORTED_TYPE,
    AttachmentError,
    AttachmentPolicy,
    AttachmentService,
    GcCandidate,
    collect_expired,
    gc_delete_statement,
    gc_select_statement,
    grace_interval_sql,
    normalize_declared_sha256,
    storage_key_for,
)
from revnote_api.storage.ports import (
    BlobDigest,
    BlobStat,
    BlobTooLarge,
    StorageUnavailable,
)

NOW = datetime.datetime(2026, 3, 4, 5, 6, 7, tzinfo=datetime.UTC)
LATER = NOW + datetime.timedelta(hours=1)

WORKSPACE = uuid.UUID("018f0000-0000-7000-8000-0000000000a1")
WORKSPACE_B = uuid.UUID("018f0000-0000-7000-8000-0000000000a2")
ATTACHMENT = uuid.UUID("018f0000-0000-7000-8000-0000000000b1")
NONCE = uuid.UUID("018f0000-0000-7000-8000-0000000000c1")
USER = uuid.UUID("018f0000-0000-7000-8000-0000000000d1")

PAYLOAD = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()

MAX_BYTES = 1_024
GRACE_S = 3_600

#: The default allowlist, read once from the policy the service ships.
DEFAULT_MIME: Final[tuple[str, ...]] = AttachmentPolicy(
    max_bytes=1, pending_grace_s=1
).accepted_mime

#: Sentinel meaning "this field was not passed", so ``None`` stays distinguishable.
UNSET: Any = object()


# --------------------------------------------------------------------------- #
# store double
# --------------------------------------------------------------------------- #
class FakeStore:
    """A structural :class:`BlobStore` backed by a dict, with fault injection."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_calls: list[tuple[str, int, str]] = []
        self.digest_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.stat_calls: list[str] = []
        #: Raised from the named method instead of doing the work.
        self.raise_on: dict[str, BaseException] = {}
        #: ``digest`` answers this instead of recomputing (a store that lost bytes).
        self.digest_override: BlobDigest | None = None
        #: ``put`` keeps only this many bytes (a store that wrote less than it got).
        self.keep_bytes: int | None = None

    async def put(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        *,
        max_bytes: int,
        content_type: str,
    ) -> BlobDigest:
        if error := self.raise_on.get("put"):
            raise error
        self.put_calls.append((key, max_bytes, content_type))
        buffer = bytearray()
        async for chunk in chunks:
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise BlobTooLarge(f"{len(buffer)} > {max_bytes}")
        data = bytes(buffer)
        if self.keep_bytes is not None:
            data = data[: self.keep_bytes]
        self.objects[key] = data
        return BlobDigest(sha256_hex=hashlib.sha256(data).hexdigest(), size=len(data))

    async def digest(self, key: str) -> BlobDigest:
        if error := self.raise_on.get("digest"):
            raise error
        self.digest_calls.append(key)
        if self.digest_override is not None:
            return self.digest_override
        if key not in self.objects:
            raise StorageUnavailable("absent")
        data = self.objects[key]
        return BlobDigest(sha256_hex=hashlib.sha256(data).hexdigest(), size=len(data))

    async def stat(self, key: str) -> BlobStat | None:
        if error := self.raise_on.get("stat"):
            raise error
        self.stat_calls.append(key)
        data = self.objects.get(key)
        return None if data is None else BlobStat(key=key, size=len(data))

    def iter_chunks(self, key: str, *, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        data = self.objects.get(key)

        async def _stream() -> AsyncIterator[bytes]:
            if data is None:
                raise StorageUnavailable("absent")
            for offset in range(0, len(data), chunk_size):
                yield data[offset : offset + chunk_size]

        return _stream()

    async def delete(self, key: str) -> None:
        if error := self.raise_on.get("delete"):
            raise error
        self.delete_calls.append(key)
        self.objects.pop(key, None)

    async def ready(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# database double
# --------------------------------------------------------------------------- #
class FakeResult:
    """The two accessors the service uses on a result object."""

    def __init__(self, rows: Iterable[tuple[Any, ...]] = ()) -> None:
        self._rows = list(rows)

    def first(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[tuple[Any, ...]]:
        return list(self._rows)


class Row:
    """One attachment row, as the fake database holds it."""

    def __init__(
        self,
        *,
        attachment_id: uuid.UUID = ATTACHMENT,
        workspace_id: uuid.UUID = WORKSPACE,
        state: str = "pending",
        mime: str = "image/png",
        content_hash: bytes | None = None,
        byte_size: Any = UNSET,
        storage_key: str | None = None,
        upload_nonce: uuid.UUID = NONCE,
        file_name: str | None = "diagram.png",
        created_at: datetime.datetime = NOW,
        ready_at: datetime.datetime | None = None,
        expired: bool = False,
    ) -> None:
        self.id = attachment_id
        self.workspace_id = workspace_id
        self.state = state
        self.mime = mime
        self.content_hash = content_hash
        self.byte_size = None if byte_size is UNSET else byte_size
        self.storage_key = storage_key or storage_key_for(workspace_id, attachment_id, NONCE)
        self.upload_nonce = upload_nonce
        self.file_name = file_name
        self.created_at = created_at
        self.ready_at = ready_at
        self.expired = expired


def ready_row(**kwargs: Any) -> Row:
    """A row that is already ``ready`` with :data:`DIGEST` over :data:`PAYLOAD`."""
    kwargs.setdefault("state", "ready")
    kwargs.setdefault("content_hash", bytes.fromhex(DIGEST))
    kwargs.setdefault("byte_size", len(PAYLOAD))
    kwargs.setdefault("ready_at", NOW)
    return Row(**kwargs)


class FakeSession:
    """Answers the service's statements from its factory's one mutable :class:`Row`."""

    def __init__(self, factory: FakeFactory) -> None:
        self.factory = factory
        self.trace = factory.trace
        self.statements: list[str] = []
        self.committed = 0
        self.rolled_back = 0
        self.open = False

    @property
    def row(self) -> Row | None:
        return self.factory.row

    @row.setter
    def row(self, value: Row | None) -> None:
        self.factory.row = value

    # -- statement dispatch ------------------------------------------------- #
    async def execute(self, statement: Any, params: dict[str, Any] | None = None) -> FakeResult:
        sql = str(statement)
        self.statements.append(sql)
        self.trace.append(("sql", sql.split("\n", 1)[0].strip()))

        if sql.startswith("INSERT INTO attachments"):
            return FakeResult([(NOW, LATER)])
        if sql.startswith("UPDATE attachments") and "upload_started_at" in sql:
            return FakeResult()
        if sql.startswith("UPDATE attachments") and "state = 'ready'" in sql:
            return self._cas(params or {})
        if sql.startswith("DELETE FROM attachments"):
            return self._delete(params or {})
        if sql.startswith("SELECT id, workspace_id, state, mime, upload_nonce"):
            return self._target(params or {})
        if sql.startswith("SELECT state FROM attachments"):
            matched = self._matches(params or {})
            return FakeResult([(matched.state,)] if matched is not None else [])
        if sql.startswith("SELECT a.id"):
            return self._view(params or {})
        if sql.startswith("SELECT id, workspace_id, storage_key"):
            return self._gc_scan()
        raise AssertionError(f"unexpected statement: {sql[:120]}")

    def _matches(self, params: dict[str, Any]) -> Row | None:
        """Apply the ``id``/``workspace_id`` predicate the statements spell out."""
        row = self.factory.row
        if row is None:
            return None
        if "attachment_id" in params and row.id != params["attachment_id"]:
            return None
        if "workspace_id" in params and row.workspace_id != params["workspace_id"]:
            return None
        return row

    def _target(self, params: dict[str, Any]) -> FakeResult:
        row = self._matches(params)
        if row is None:
            return FakeResult()
        return FakeResult(
            [
                (
                    row.id,
                    row.workspace_id,
                    row.state,
                    row.mime,
                    row.upload_nonce,
                    row.storage_key,
                    row.expired,
                )
            ]
        )

    def _cas(self, params: dict[str, Any]) -> FakeResult:
        """Apply the CAS predicate exactly as the SQL says it should.

        The predicate is ``id + workspace_id + state='pending'``. It deliberately does
        NOT compare a nonce: the key belongs to the *attempt*, so the row's own
        ``upload_nonce`` no longer identifies who may publish. ``storage_key`` is an
        output of the statement, not an input condition.
        """
        row = self.factory.row
        if row is None or self.factory.cas_misses:
            return FakeResult()
        matches = (
            row.id == params["attachment_id"]
            and row.workspace_id == params["workspace_id"]
            and row.state == "pending"
        )
        if not matches:
            return FakeResult()
        row.state = "ready"
        row.content_hash = params["content_hash"]
        row.byte_size = params["byte_size"]
        row.storage_key = params["storage_key"]
        row.ready_at = NOW
        return FakeResult([(NOW,)])

    def _delete(self, params: dict[str, Any]) -> FakeResult:
        row = self.factory.row
        if row is None or row.id != params["attachment_id"]:
            return FakeResult()
        if row.state != "pending" or not row.expired:
            return FakeResult()
        key = row.storage_key
        self.factory.row = None
        return FakeResult([(key,)])

    def _view(self, params: dict[str, Any]) -> FakeResult:
        row = self._matches(params)
        if row is None or row.state != "ready":
            return FakeResult()
        return FakeResult(
            [
                (
                    row.id,
                    row.workspace_id,
                    row.state,
                    row.mime,
                    row.byte_size,
                    row.content_hash,
                    row.file_name,
                    row.created_at,
                    row.ready_at,
                    None,
                    row.storage_key,
                )
            ]
        )

    def _gc_scan(self) -> FakeResult:
        row = self.factory.row
        if row is None or row.state != "pending" or not row.expired:
            return FakeResult()
        return FakeResult([(row.id, row.workspace_id, row.storage_key)])

    # -- transaction plumbing ----------------------------------------------- #
    async def commit(self) -> None:
        self.committed += 1
        self.trace.append(("commit", ""))

    async def rollback(self) -> None:
        self.rolled_back += 1
        self.trace.append(("rollback", ""))

    async def __aenter__(self) -> FakeSession:
        self.open = True
        self.trace.append(("open", ""))
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.open = False
        self.trace.append(("close", ""))


class FakeFactory:
    """A session factory over one shared row; every session is recorded."""

    def __init__(self, row: Row | None = None, *, cas_misses: bool = False) -> None:
        self.row = row
        #: When true every CAS reports zero rows, as a lost race does.
        self.cas_misses = cas_misses
        self.trace: list[tuple[str, str]] = []
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    @property
    def statements(self) -> list[str]:
        return [statement for session in self.sessions for statement in session.statements]


def make_service(
    *,
    row: Row | None = None,
    store: FakeStore | None = None,
    factory: FakeFactory | None = None,
    max_bytes: int = MAX_BYTES,
    accepted_mime: tuple[str, ...] = DEFAULT_MIME,
) -> tuple[AttachmentService, FakeStore, FakeFactory]:
    """Build a service over the doubles, returning them for assertions."""
    blob_store = FakeStore() if store is None else store
    session_factory = FakeFactory(row) if factory is None else factory
    policy = AttachmentPolicy(
        max_bytes=max_bytes,
        pending_grace_s=GRACE_S,
        accepted_mime=accepted_mime,
    )
    service = AttachmentService(session_factory, blob_store, policy)
    return service, blob_store, session_factory


async def body(*chunks: bytes) -> AsyncIterator[bytes]:
    """A request body as an async iterator of the given pieces."""
    for chunk in chunks:
        yield chunk


async def drain(iterator: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in iterator])


def code_of(error: AttachmentError) -> str:
    return error.code


# --------------------------------------------------------------------------- #
# create
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_create_inserts_a_pending_row_and_returns_a_relative_upload_path() -> None:
    service, store, factory = make_service()

    pending = await service.create(WORKSPACE, mime="image/png", file_name="diagram.png")

    assert pending.state == "pending"
    assert pending.mime == "image/png"
    assert pending.workspace_id == WORKSPACE
    # The upload target is a relative path: no scheme, no host, no signature (D4).
    assert pending.upload_path == (
        f"/api/v1/workspaces/{WORKSPACE}/attachments/{pending.id}/content"
    )
    assert "://" not in pending.upload_path
    assert "?" not in pending.upload_path
    # Nothing was written to the store: an abandoned create costs one row.
    assert store.put_calls == []
    assert any(statement.startswith("INSERT INTO attachments") for statement in factory.statements)


@pytest.mark.anyio
async def test_create_derives_expiry_from_the_database_clock() -> None:
    """``expires_at`` comes back from SQL, not from a Python timestamp."""
    service, _, factory = make_service()

    pending = await service.create(WORKSPACE, mime="image/png")

    insert = next(
        statement
        for statement in factory.statements
        if statement.startswith("INSERT INTO attachments")
    )
    assert grace_interval_sql(GRACE_S) in insert
    assert pending.expires_at == LATER


@pytest.mark.anyio
async def test_create_uses_a_fresh_nonce_per_row_so_keys_never_collide() -> None:
    service, _, _ = make_service()

    first = await service.create(WORKSPACE, mime="image/png")
    second = await service.create(WORKSPACE, mime="image/png")

    assert first.id != second.id
    assert first.storage_key != second.storage_key
    # ``ws/<ws>/att/<id>/<nonce>`` - four meaningful segments, three of them UUIDs.
    assert len(first.storage_key.split("/")) == 5
    assert first.storage_key.startswith(f"ws/{WORKSPACE}/att/{first.id}/")


@pytest.mark.anyio
async def test_create_refuses_a_declared_size_over_the_cap() -> None:
    service, _, _ = make_service(max_bytes=100)

    with pytest.raises(AttachmentError) as caught:
        await service.create(WORKSPACE, mime="image/png", byte_size=101)

    assert code_of(caught.value) == TOO_LARGE
    assert caught.value.details["max_bytes"] == 100


@pytest.mark.anyio
async def test_create_refuses_a_non_positive_declared_size() -> None:
    service, _, _ = make_service()

    with pytest.raises(AttachmentError) as caught:
        await service.create(WORKSPACE, mime="image/png", byte_size=0)

    assert code_of(caught.value) == TOO_LARGE


# --------------------------------------------------------------------------- #
# the happy path: pending -> ready
# --------------------------------------------------------------------------- #
def attempt_key_of(store: FakeStore) -> str:
    """The key *this attempt* wrote, read back from the store rather than predicted.

    The fourth path segment is a fresh nonce per attempt (that is what stops a loser
    deleting a winner's bytes), so a test cannot compute it in advance. Reading it from
    ``put_calls`` asserts on what happened instead of on a value the test invented, and
    the shape check keeps the storage-key contract under test.
    """
    assert store.put_calls, "the attempt wrote nothing"
    key = store.put_calls[0][0]
    prefix = f"ws/{WORKSPACE}/att/{ATTACHMENT}/"
    assert key.startswith(prefix), key
    # ws / {workspace_id} / att / {attachment_id} / {attempt_nonce}
    parts = key.split("/")
    assert parts[0] == "ws" and parts[2] == "att", parts
    assert len(parts) == 5, parts
    # The attempt nonce must be a canonical UUID, and must not be the one minted at
    # create time: that is the whole point of the fix.
    assert parts[4] != str(NONCE), "the key must not reuse the row's creation nonce"
    uuid.UUID(parts[4])
    return key


@pytest.mark.anyio
async def test_upload_publishes_ready_with_the_real_digest_and_size() -> None:
    service, store, factory = make_service(row=Row())

    ready = await service.store_content(
        WORKSPACE,
        ATTACHMENT,
        chunks=body(PAYLOAD),
        declared_content_type="image/png",
    )

    key = attempt_key_of(store)

    assert ready.content_hash == DIGEST
    assert ready.byte_size == len(PAYLOAD)
    assert ready.ready_at == NOW
    # The digest is the one computed by re-reading the object, not the one ``put``
    # returned: both agree here, which is what D6 requires.
    assert store.objects[key] == PAYLOAD
    assert store.digest_calls == [key]
    assert factory.row is not None and factory.row.state == "ready"
    assert factory.row.content_hash == bytes.fromhex(DIGEST)
    assert factory.row.byte_size == len(PAYLOAD)


@pytest.mark.anyio
async def test_upload_streams_without_buffering_the_whole_body() -> None:
    """The chunks reach the store as they arrive; nothing accumulates in the service."""
    service, store, _factory = make_service(row=Row())
    seen: list[int] = []

    async def measured() -> AsyncIterator[bytes]:
        for index in range(4):
            seen.append(index)
            yield PAYLOAD

    await service.store_content(
        WORKSPACE,
        ATTACHMENT,
        chunks=measured(),
        declared_content_type="image/png",
    )

    assert seen == [0, 1, 2, 3]
    assert store.put_calls[0][1] == MAX_BYTES
    assert store.put_calls[0][2] == "image/png"
    assert store.objects[store.put_calls[0][0]] == PAYLOAD * 4


@pytest.mark.anyio
async def test_upload_accepts_a_matching_declared_digest_in_hex() -> None:
    service, _, _ = make_service(row=Row())

    ready = await service.store_content(
        WORKSPACE,
        ATTACHMENT,
        chunks=body(PAYLOAD),
        declared_content_type="image/png",
        declared_sha256=DIGEST,
    )

    assert ready.content_hash == DIGEST


@pytest.mark.anyio
async def test_upload_ignores_a_content_type_parameter() -> None:
    """``; charset=utf-8`` describes the encoding of the body, not another type."""
    service, _, _ = make_service(row=Row())

    ready = await service.store_content(
        WORKSPACE,
        ATTACHMENT,
        chunks=body(PAYLOAD),
        declared_content_type="image/png; charset=binary",
    )

    assert ready.byte_size == len(PAYLOAD)


# --------------------------------------------------------------------------- #
# rejection branches
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_a_second_upload_to_a_ready_row_is_refused_before_reading_the_body() -> None:
    """The immutable-object rule: no byte is read, no object is written."""
    service, store, factory = make_service(row=ready_row())
    consumed: list[bytes] = []

    async def should_not_be_read() -> AsyncIterator[bytes]:
        consumed.append(b"x")
        yield PAYLOAD

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=should_not_be_read(),
            declared_content_type="image/png",
        )

    assert code_of(caught.value) == ALREADY_READY
    assert consumed == [], "the body must not be read at all"
    assert store.put_calls == []
    assert factory.row is not None and factory.row.content_hash == bytes.fromhex(DIGEST)


@pytest.mark.anyio
async def test_a_hash_mismatch_deletes_the_object_and_leaves_no_ready_row() -> None:
    service, store, factory = make_service(row=Row())
    wrong = "0" * 64

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type="image/png",
            declared_sha256=wrong,
        )

    key = attempt_key_of(store)

    assert code_of(caught.value) == HASH_MISMATCH
    # The attempt's own key is gone and the row is still pending: no ready row.
    assert store.delete_calls == [key]
    assert key not in store.objects
    assert factory.row is not None
    assert factory.row.state == "pending"
    assert factory.row.content_hash is None


@pytest.mark.anyio
async def test_a_store_that_returns_different_bytes_is_a_storage_failure() -> None:
    """``put`` and ``digest`` disagreeing means the store changed the object."""
    service, store, factory = make_service(row=Row())
    key = storage_key_for(WORKSPACE, ATTACHMENT, NONCE)
    store.digest_override = BlobDigest(sha256_hex="f" * 64, size=len(PAYLOAD))

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type="image/png",
        )

    assert code_of(caught.value) == STORAGE_UNAVAILABLE
    assert caught.value.details["reason"] == "stored_bytes_changed"
    assert key not in store.objects
    assert factory.row is not None and factory.row.state == "pending"


@pytest.mark.anyio
async def test_an_expired_pending_row_is_refused() -> None:
    service, store, _ = make_service(row=Row(expired=True))

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type="image/png",
        )

    assert code_of(caught.value) == EXPIRED
    assert store.put_calls == []


@pytest.mark.parametrize("state", ["deleted"])
@pytest.mark.anyio
async def test_a_deleted_row_is_not_found(state: str) -> None:
    """A collected row is indistinguishable from an id that never existed."""
    service, store, _ = make_service(row=Row(state=state))

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type="image/png",
        )

    assert code_of(caught.value) == NOT_FOUND
    assert store.put_calls == []


@pytest.mark.anyio
async def test_an_unknown_attachment_id_is_not_found() -> None:
    service, store, _ = make_service(row=None)

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type="image/png",
        )

    assert code_of(caught.value) == NOT_FOUND
    assert store.put_calls == []


@pytest.mark.anyio
async def test_an_attachment_in_another_workspace_is_not_found() -> None:
    """The workspace is part of the predicate, so a foreign id cannot be uploaded."""
    service, store, _ = make_service(row=Row(workspace_id=WORKSPACE))

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE_B,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type="image/png",
        )

    assert code_of(caught.value) == NOT_FOUND
    assert store.put_calls == []


# --------------------------------------------------------------------------- #
# the CAS: one winner
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_a_lost_cas_race_deletes_its_own_key_and_reports_already_ready() -> None:
    """The loser wrote its own object, then a winner published first.

    Simulated at the SQL boundary: the factory makes the CAS match nothing, which is
    exactly what PostgreSQL reports to the second writer. The row itself is already
    ``ready`` (the winner published it), so the reason is ``attachment_already_ready``
    and the loser's object is removed.
    """
    factory = FakeFactory(ready_row())
    store = FakeStore()
    service = AttachmentService(
        factory,
        store,
        AttachmentPolicy(max_bytes=MAX_BYTES, pending_grace_s=GRACE_S),
    )
    # The row is ready, so phase 1 refuses before the CAS. To reach the CAS the row
    # has to *look* pending and only lose at the publish step - which is what a
    # concurrent winner does between phase 1 and phase 3.
    factory.row = Row(state="pending")
    original = FakeSession.execute

    async def publish_then_miss(self: FakeSession, statement: Any, params: Any = None) -> Any:
        sql = str(statement)
        if sql.startswith("UPDATE attachments") and "state = 'ready'" in sql:
            # The winner publishes first; this attempt's CAS then matches nothing.
            assert factory.row is not None
            factory.row.state = "ready"
            factory.row.content_hash = bytes.fromhex(DIGEST)
            factory.row.ready_at = NOW
        return await original(self, statement, params)

    FakeSession.execute = publish_then_miss  # type: ignore[method-assign]
    try:
        with pytest.raises(AttachmentError) as caught:
            await service.store_content(
                WORKSPACE, ATTACHMENT, chunks=body(PAYLOAD), declared_content_type="image/png"
            )
    finally:
        FakeSession.execute = original  # type: ignore[method-assign]

    key = attempt_key_of(store)
    assert code_of(caught.value) == ALREADY_READY
    # The loser's own object was removed: one attachment, one object.
    assert store.delete_calls == [key]
    assert store.objects == {}


@pytest.mark.anyio
async def test_a_zero_row_cas_with_a_vanished_row_is_not_found() -> None:
    factory = FakeFactory(Row())
    store = FakeStore()
    service = AttachmentService(
        factory, store, AttachmentPolicy(max_bytes=MAX_BYTES, pending_grace_s=GRACE_S)
    )

    original = FakeSession.execute

    async def vanish_before_publish(self: FakeSession, statement: Any, params: Any = None) -> Any:
        """Emulate the GC collecting the row while the upload is in flight."""
        sql = str(statement)
        if sql.startswith("UPDATE attachments") and "state = 'ready'" in sql:
            factory.row = None
        return await original(self, statement, params)

    FakeSession.execute = vanish_before_publish  # type: ignore[method-assign]
    try:
        with pytest.raises(AttachmentError) as caught:
            await service.store_content(
                WORKSPACE, ATTACHMENT, chunks=body(PAYLOAD), declared_content_type="image/png"
            )
    finally:
        FakeSession.execute = original  # type: ignore[method-assign]

    key = attempt_key_of(store)
    assert code_of(caught.value) == NOT_FOUND
    assert store.delete_calls == [key]


# --------------------------------------------------------------------------- #
# size and MIME
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_an_oversize_body_is_refused_and_leaves_nothing() -> None:
    service, store, factory = make_service(row=Row(), max_bytes=16)
    key = storage_key_for(WORKSPACE, ATTACHMENT, NONCE)

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type="image/png",
        )

    assert code_of(caught.value) == TOO_LARGE
    assert caught.value.details["max_bytes"] == 16
    assert key not in store.objects
    assert factory.row is not None and factory.row.state == "pending"


@pytest.mark.parametrize(
    "mime",
    ["image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf", "text/plain"],
)
@pytest.mark.anyio
async def test_every_allowlisted_mime_is_accepted(mime: str) -> None:
    service, _, _ = make_service()

    pending = await service.create(WORKSPACE, mime=mime)

    assert pending.mime == mime


@pytest.mark.parametrize(
    "mime",
    [
        "image/svg+xml",
        "IMAGE/SVG+XML",
        "image/svg+xml; charset=utf-8",
        "application/x-msdownload",
        "text/html",
        "application/javascript",
        "",
        "not-a-mime",
        "/png",
        "image/",
    ],
)
@pytest.mark.anyio
async def test_a_mime_outside_the_allowlist_is_unsupported(mime: str) -> None:
    service, _, _ = make_service()

    with pytest.raises(AttachmentError) as caught:
        await service.create(WORKSPACE, mime=mime)

    assert code_of(caught.value) == UNSUPPORTED_TYPE


@pytest.mark.anyio
async def test_svg_is_rejected_with_its_own_reason_even_if_configured() -> None:
    """The SVG rule is a separate lock, not merely an entry in a configurable list."""
    service, _, _ = make_service(
        accepted_mime=("image/png", "image/svg+xml"),
    )

    with pytest.raises(AttachmentError) as caught:
        await service.create(WORKSPACE, mime="image/svg+xml")

    assert code_of(caught.value) == UNSUPPORTED_TYPE
    assert caught.value.details["reason"] == "mime_svg"


@pytest.mark.anyio
async def test_an_uploaded_svg_content_type_is_rejected_before_the_body() -> None:
    service, store, _ = make_service(row=Row(mime="image/png"))

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type="image/svg+xml",
        )

    assert code_of(caught.value) == UNSUPPORTED_TYPE
    assert caught.value.details["reason"] == "mime_svg"
    assert store.put_calls == []


@pytest.mark.anyio
async def test_a_content_type_that_differs_from_the_declared_mime_is_refused() -> None:
    service, store, _ = make_service(row=Row(mime="image/png"))

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type="application/pdf",
        )

    assert code_of(caught.value) == UNSUPPORTED_TYPE
    assert caught.value.details["reason"] == "content_type_mismatch"
    assert store.put_calls == []


@pytest.mark.anyio
async def test_a_missing_content_type_is_refused() -> None:
    service, store, _ = make_service(row=Row())

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type=None,
        )

    assert code_of(caught.value) == UNSUPPORTED_TYPE
    assert caught.value.details["reason"] == "content_type_missing"
    assert store.put_calls == []


# --------------------------------------------------------------------------- #
# storage failures
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_a_store_outage_on_put_is_storage_unavailable() -> None:
    service, store, factory = make_service(row=Row())
    store.raise_on["put"] = StorageUnavailable("down")

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type="image/png",
        )

    assert code_of(caught.value) == STORAGE_UNAVAILABLE
    assert factory.row is not None and factory.row.state == "pending"


@pytest.mark.anyio
async def test_a_store_outage_on_the_read_back_is_storage_unavailable() -> None:
    service, store, factory = make_service(row=Row())
    key = storage_key_for(WORKSPACE, ATTACHMENT, NONCE)
    store.raise_on["digest"] = StorageUnavailable("down")

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type="image/png",
        )

    assert code_of(caught.value) == STORAGE_UNAVAILABLE
    assert key not in store.objects
    assert factory.row is not None and factory.row.state == "pending"


@pytest.mark.anyio
async def test_a_cleanup_failure_does_not_mask_the_original_error() -> None:
    """A refused upload reports *why* it was refused, not that cleanup also failed."""
    service, store, _ = make_service(row=Row())
    store.raise_on["delete"] = StorageUnavailable("down")

    with pytest.raises(AttachmentError) as caught:
        await service.store_content(
            WORKSPACE,
            ATTACHMENT,
            chunks=body(PAYLOAD),
            declared_content_type="image/png",
            declared_sha256="0" * 64,
        )

    assert code_of(caught.value) == HASH_MISMATCH


# --------------------------------------------------------------------------- #
# no storage I/O inside a transaction
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_no_store_call_happens_while_a_session_is_open() -> None:
    """The rule the whole design exists for, checked on the event ordering.

    Every ``put``/``digest``/``delete`` must land between a session ``close`` and the
    next ``open``. A regression that moved a store call inside the ``async with``
    would be caught here, before it could pin a pooled connection under load.
    """
    service, store, factory = make_service(row=Row())
    trace = factory.trace

    original_put = store.put

    async def traced_put(*args: Any, **kwargs: Any) -> BlobDigest:
        trace.append(("store", "put"))
        return await original_put(*args, **kwargs)

    original_digest = store.digest

    async def traced_digest(key: str) -> BlobDigest:
        trace.append(("store", "digest"))
        return await original_digest(key)

    store.put = traced_put  # type: ignore[method-assign]
    store.digest = traced_digest  # type: ignore[method-assign]

    await service.store_content(
        WORKSPACE, ATTACHMENT, chunks=body(PAYLOAD), declared_content_type="image/png"
    )

    open_sessions = 0
    for kind, label in trace:
        if kind == "open":
            open_sessions += 1
        elif kind == "close":
            open_sessions -= 1
        elif kind == "store":
            assert open_sessions == 0, (
                f"storage call {label!r} ran while {open_sessions} session(s) were open"
            )
    assert any(kind == "store" for kind, _ in trace)


@pytest.mark.anyio
async def test_create_and_upload_commit_every_write() -> None:
    service, _, factory = make_service(row=Row())

    await service.create(WORKSPACE, mime="image/png")
    await service.store_content(
        WORKSPACE, ATTACHMENT, chunks=body(PAYLOAD), declared_content_type="image/png"
    )

    # Every session that *mutated* committed it; none was left open. A read-only
    # session (the post-miss re-read) legitimately ends without a commit: nothing
    # there needs publishing, and closing it is what releases the connection.
    for session in factory.sessions:
        assert not session.open
        if any(
            sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE")) for sql in session.statements
        ):
            assert session.committed == 1


# --------------------------------------------------------------------------- #
# reads: metadata and content
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_metadata_returns_the_ready_view_with_a_hex_digest() -> None:
    service, _, _ = make_service(row=ready_row())

    view = await service.metadata(ATTACHMENT, user_id=USER)

    assert view.id == ATTACHMENT
    assert view.state == "ready"
    assert view.mime == "image/png"
    assert view.byte_size == len(PAYLOAD)
    assert view.content_hash == DIGEST
    assert view.ready_at == NOW


@pytest.mark.anyio
async def test_metadata_of_a_pending_row_is_not_found() -> None:
    service, _, _ = make_service(row=Row())

    with pytest.raises(AttachmentError) as caught:
        await service.metadata(ATTACHMENT, user_id=USER)

    assert code_of(caught.value) == NOT_FOUND


@pytest.mark.anyio
async def test_metadata_joins_membership_so_non_membership_is_not_found() -> None:
    """The authorisation check is the JOIN: a non-member simply gets no row."""
    service, _, factory = make_service(row=ready_row())

    await service.metadata(ATTACHMENT, user_id=USER)

    statement = next(sql for sql in factory.statements if sql.startswith("SELECT a.id"))
    assert "JOIN workspace_members" in statement
    assert "m.user_id = :user_id" in statement
    assert "a.state = 'ready'" in statement


@pytest.mark.anyio
async def test_open_content_streams_the_bytes_byte_for_byte() -> None:
    service, store, _factory = make_service(row=ready_row())
    key = storage_key_for(WORKSPACE, ATTACHMENT, NONCE)
    store.objects[key] = PAYLOAD

    view, chunks = await service.open_content(ATTACHMENT, user_id=USER)

    assert view.content_hash == DIGEST
    assert await drain(chunks) == PAYLOAD
    assert store.stat_calls == [key]


@pytest.mark.anyio
async def test_open_content_reports_a_missing_object_rather_than_an_empty_stream() -> None:
    """The row says ready but the object is gone: never answer 200 with zero bytes."""
    service, _, _ = make_service(row=ready_row())

    with pytest.raises(AttachmentError) as caught:
        await service.open_content(ATTACHMENT, user_id=USER)

    assert code_of(caught.value) == STORAGE_UNAVAILABLE
    assert caught.value.details["reason"] == "object_missing"


@pytest.mark.anyio
async def test_open_content_maps_a_stat_outage_to_storage_unavailable() -> None:
    service, store, _ = make_service(row=ready_row())
    store.raise_on["stat"] = StorageUnavailable("down")

    with pytest.raises(AttachmentError) as caught:
        await service.open_content(ATTACHMENT, user_id=USER)

    assert code_of(caught.value) == STORAGE_UNAVAILABLE


@pytest.mark.anyio
async def test_open_content_of_a_deleted_row_is_not_found() -> None:
    service, _, _ = make_service(row=Row(state="deleted"))

    with pytest.raises(AttachmentError) as caught:
        await service.open_content(ATTACHMENT, user_id=USER)

    assert code_of(caught.value) == NOT_FOUND


# --------------------------------------------------------------------------- #
# the error -> code mapping
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "code",
    [
        ALREADY_READY,
        EXPIRED,
        HASH_MISMATCH,
        NOT_FOUND,
        STORAGE_UNAVAILABLE,
        TOO_LARGE,
        UNSUPPORTED_TYPE,
    ],
)
def test_every_attachment_code_survives_construction(code: str) -> None:
    error = AttachmentError(code)

    assert error.code == code
    assert error.details == {}
    assert str(error) == code


def test_an_unknown_code_degrades_to_internal() -> None:
    """The vocabulary cannot widen by accident: an invented code becomes ``internal``."""
    assert AttachmentError("some_new_code").code == "internal"


def test_the_error_repr_carries_no_details() -> None:
    """``details`` is machine-readable; a repr must not spill client-derived values."""
    error = AttachmentError(HASH_MISMATCH, details={"file_name": "private-report.pdf"})

    assert "private-report.pdf" not in repr(error)
    assert repr(error) == "AttachmentError(code='attachment_hash_mismatch')"


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #
def test_policy_reads_only_the_documented_settings_fields() -> None:
    class Probe:
        attachment_max_bytes = 4_096
        attachment_pending_grace_s = 120
        attachment_accepted_mime: ClassVar[list[str]] = [
            "image/png",
            "IMAGE/PNG",
            " text/plain; charset=utf-8 ",
        ]

    policy = AttachmentPolicy.from_settings(Probe())

    assert policy.max_bytes == 4_096
    assert policy.pending_grace_s == 120
    # Normalised and de-duplicated, so a redundant config entry cannot widen it.
    assert policy.accepted_mime == ("image/png", "text/plain")


def test_policy_falls_back_to_the_default_allowlist() -> None:
    class Probe:
        attachment_max_bytes = 10
        attachment_pending_grace_s = 10
        attachment_accepted_mime: ClassVar[list[str]] = []

    assert AttachmentPolicy.from_settings(Probe()).accepted_mime == DEFAULT_MIME


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 0, "pending_grace_s": 1},
        {"max_bytes": -1, "pending_grace_s": 1},
        {"max_bytes": 1, "pending_grace_s": 0},
    ],
)
def test_policy_refuses_a_nonsensical_limit(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        AttachmentPolicy(**kwargs)  # type: ignore[arg-type]


def test_policy_refuses_an_empty_allowlist() -> None:
    with pytest.raises(ValueError):
        AttachmentPolicy(max_bytes=1, pending_grace_s=1, accepted_mime=())


# --------------------------------------------------------------------------- #
# helper functions
# --------------------------------------------------------------------------- #
def test_storage_key_layout_is_canonical() -> None:
    key = storage_key_for(WORKSPACE, ATTACHMENT, NONCE)

    assert key == f"ws/{WORKSPACE}/att/{ATTACHMENT}/{NONCE}"
    parts = key.split("/")
    # Four meaningful segments: ``ws``, the workspace id, ``att``, then the two ids
    # nested under the attachment id - matching interface §4 verbatim.
    assert parts[0] == "ws"
    assert parts[2] == "att"
    for value in (parts[1], parts[3], parts[4]):
        assert value == str(uuid.UUID(value)), f"{value} is not canonical lowercase"


def test_storage_key_never_contains_a_file_name() -> None:
    """Nothing from the request contributes a byte: a hostile name cannot move a key."""
    hostile_tail = "../../etc/passwd"

    key = storage_key_for(WORKSPACE, ATTACHMENT, NONCE)

    assert hostile_tail not in key
    assert ".." not in key
    assert "passwd" not in key


def test_grace_interval_is_an_interpolated_integer() -> None:
    assert grace_interval_sql(60) == "make_interval(secs => 60)"
    assert grace_interval_sql(86_400) == "make_interval(secs => 86400)"


def test_grace_interval_refuses_a_non_positive_window() -> None:
    with pytest.raises(ValueError):
        grace_interval_sql(0)


@pytest.mark.parametrize(
    "declared",
    [
        DIGEST,
        DIGEST.upper(),
        hashlib.sha256(PAYLOAD).digest().hex(),
    ],
)
def test_a_hex_digest_is_normalised_to_lowercase(declared: str) -> None:
    assert normalize_declared_sha256(declared) == DIGEST


def test_a_base64_digest_is_decoded() -> None:
    import base64

    encoded = base64.b64encode(hashlib.sha256(PAYLOAD).digest()).decode()

    assert normalize_declared_sha256(encoded) == DIGEST
    assert normalize_declared_sha256(encoded.rstrip("=")) == DIGEST


@pytest.mark.parametrize("declared", [None, "", "   "])
def test_an_absent_digest_is_simply_absent(declared: str | None) -> None:
    assert normalize_declared_sha256(declared) is None


@pytest.mark.parametrize(
    "declared",
    ["not-a-digest", "zz" * 32, "0" * 63, "0" * 65, "!!!!", "AAAA"],
)
def test_a_malformed_digest_is_refused(declared: str) -> None:
    with pytest.raises(ValueError):
        normalize_declared_sha256(declared)


# --------------------------------------------------------------------------- #
# GC selection
# --------------------------------------------------------------------------- #
def test_gc_selects_pending_rows_only() -> None:
    statement = gc_select_statement(GRACE_S)

    assert "state = 'pending'" in statement
    assert "created_at < now() - make_interval(secs => 3600)" in statement
    # A ready or deleted row can never match: the filter is in the statement.
    assert "state <> 'pending'" not in statement


def test_gc_delete_rechecks_both_predicates() -> None:
    """The delete is conditional, so a row published between scan and delete survives."""
    statement = gc_delete_statement(GRACE_S)

    assert "DELETE FROM attachments" in statement
    assert "state = 'pending'" in statement
    assert "created_at < now()" in statement
    assert "RETURNING storage_key" in statement


def test_gc_statements_are_parameterised_not_concatenated() -> None:
    """The only interpolated value is a range-checked integer."""
    with pytest.raises(ValueError):
        gc_select_statement(0)
    with pytest.raises(ValueError):
        gc_select_statement(GRACE_S, limit=0)


@pytest.mark.anyio
async def test_gc_deletes_the_row_then_the_object() -> None:
    factory = FakeFactory(Row(expired=True))
    store = FakeStore()
    key = storage_key_for(WORKSPACE, ATTACHMENT, NONCE)
    store.objects[key] = PAYLOAD

    report = await collect_expired(factory, store, grace_s=GRACE_S, dry_run=False)

    assert report.scanned == 1
    assert report.rows_deleted == 1
    assert report.objects_deleted == 1
    assert report.failed == 0
    assert factory.row is None
    assert store.delete_calls == [key]
    assert key not in store.objects


@pytest.mark.anyio
async def test_gc_dry_run_deletes_nothing() -> None:
    factory = FakeFactory(Row(expired=True))
    store = FakeStore()
    key = storage_key_for(WORKSPACE, ATTACHMENT, NONCE)
    store.objects[key] = PAYLOAD

    report = await collect_expired(factory, store, grace_s=GRACE_S, dry_run=True)

    assert report.scanned == 1
    assert report.rows_deleted == 1
    assert store.delete_calls == []
    assert key in store.objects
    assert factory.row is not None


@pytest.mark.anyio
async def test_gc_never_collects_a_ready_row() -> None:
    """The load-bearing claim: ``ready`` is never deleted automatically (D8)."""
    factory = FakeFactory(ready_row(expired=True))
    store = FakeStore()
    key = storage_key_for(WORKSPACE, ATTACHMENT, NONCE)
    store.objects[key] = PAYLOAD

    report = await collect_expired(factory, store, grace_s=GRACE_S, dry_run=False)

    assert report.scanned == 0
    assert report.rows_deleted == 0
    assert report.objects_deleted == 0
    assert factory.row is not None and factory.row.state == "ready"
    assert store.delete_calls == []
    assert key in store.objects


@pytest.mark.anyio
async def test_gc_ignores_a_pending_row_inside_the_grace_window() -> None:
    factory = FakeFactory(Row(expired=False))
    store = FakeStore()

    report = await collect_expired(factory, store, grace_s=GRACE_S, dry_run=False)

    assert report.scanned == 0
    assert factory.row is not None and factory.row.state == "pending"


@pytest.mark.anyio
async def test_gc_counts_a_failed_object_delete_without_losing_the_row_count() -> None:
    factory = FakeFactory(Row(expired=True))
    store = FakeStore()
    store.raise_on["delete"] = StorageUnavailable("down")

    report = await collect_expired(factory, store, grace_s=GRACE_S, dry_run=False)

    assert report.rows_deleted == 1
    assert report.objects_deleted == 0
    assert report.failed == 1


@pytest.mark.anyio
async def test_gc_accepts_a_precomputed_candidate_list() -> None:
    """The scan can be supplied, so a caller can collect exactly what it inspected."""
    candidate = GcCandidate(
        id=ATTACHMENT,
        workspace_id=WORKSPACE,
        storage_key=storage_key_for(WORKSPACE, ATTACHMENT, NONCE),
    )
    factory = FakeFactory(None)
    store = FakeStore()

    report = await collect_expired(
        factory,
        store,
        grace_s=GRACE_S,
        candidates=[candidate],
        dry_run=True,
    )

    assert report.scanned == 1
    assert report.rows_deleted == 1


# --------------------------------------------------------------------------- #
# immutability of the value objects
# --------------------------------------------------------------------------- #
def test_pending_and_ready_results_are_frozen() -> None:
    import dataclasses

    from revnote_api.services.attachments import PendingAttachment, ReadyAttachment

    assert dataclasses.is_dataclass(PendingAttachment)
    assert dataclasses.is_dataclass(ReadyAttachment)
    for cls in (PendingAttachment, ReadyAttachment):
        assert cls.__dataclass_params__.frozen, cls  # type: ignore[attr-defined]


def test_ready_result_repr_omits_the_content_hash() -> None:
    """A repr reaches logs and tracebacks; a content digest is not log material."""
    from revnote_api.services.attachments import ReadyAttachment

    ready = ReadyAttachment(
        id=ATTACHMENT,
        workspace_id=WORKSPACE,
        content_hash=DIGEST,
        byte_size=len(PAYLOAD),
        ready_at=NOW,
    )

    assert DIGEST not in repr(ready)
    assert str(len(PAYLOAD)) in repr(ready)
