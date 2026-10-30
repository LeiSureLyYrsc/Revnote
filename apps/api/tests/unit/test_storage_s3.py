"""The S3 adapter against a fake boto3 client: chunked digest math, limits, error mapping.

Why a fake client and not a real store
-------------------------------------
Everything this file checks is *our* logic: how many bytes were hashed, when the
upload aborted, which exception a given SDK failure becomes. None of it needs a
server, and a fake makes the interesting cases (a stream one byte too long, an
``InvalidResponseError`` carrying a body, a call that never returns) expressible at
all. Real round trips are the real-storage suite's job, behind
``REQUIRE_OBJECT_STORAGE_TESTS``.

The fake is a real object with real methods, not a ``Mock``: the adapter has to keep
working when the SDK's exact attribute surface changes, and a mock would happily
accept a call shape the real SDK rejects. ``FakeS3Client`` therefore *asserts* the shape
of every call it receives.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from revnote_api.storage import s3 as s3_module
from revnote_api.storage.ports import BlobDigest, BlobTooLarge, StorageUnavailable
from revnote_api.storage.s3 import S3BlobStore, StorageClientError
from revnote_api.storage.settings import ObjectStorageSettings

pytestmark = pytest.mark.anyio

#: Sentinels that must not survive into an exception message.
ACCESS_SENTINEL = "AKIA_ACCESS_SENTINEL_0001"
SECRET_SENTINEL = "sk-private-sentinel-do-not-log-9f3a"
KEY_SENTINEL = "ws/00000000-0000-0000-0000-000000000001/att/000000000002/nonce"

BUCKET = "revnote-attachments"


# --------------------------------------------------------------------------- #
# the fake SDK client
# --------------------------------------------------------------------------- #


class FakeResponse:
    """``urllib3``-shaped streaming response with a length-bounded ``read``."""

    def __init__(self, data: bytes, *, declare_size: int | None = None) -> None:
        self._data = data
        self._position = 0
        #: Overrides what ``read`` will hand out, to model a short/long body.
        self._limit = len(data) if declare_size is None else declare_size
        self.closed = 0
        self.released = 0

    def read(self, size: int = -1) -> bytes:
        end = self._limit if size is None or size < 0 else min(self._position + size, self._limit)
        chunk = self._data[self._position : end]
        self._position = end
        return chunk

    def close(self) -> None:
        self.closed += 1

    def release_conn(self) -> None:
        self.released += 1


class FakeS3Client:
    """The subset of a boto3 S3 client the adapter uses, with call-shape assertions.

    ``objects`` is the store: ``{key: bytes}``. Every mutation goes through it, so a
    test can assert on *exactly* what the store would hold - which is what makes "the
    digest describes what was written" checkable instead of assumed.

    boto3 answers with plain dicts and takes keyword arguments, so the fake does the
    same. It is a real object with real methods rather than a ``Mock``: a mock would
    happily accept a call shape the real SDK rejects, which is the one thing this fake
    exists to prevent.
    """

    def __init__(
        self,
        *,
        objects: dict[str, bytes] | None = None,
        buckets: set[str] | None = None,
        put_error: BaseException | None = None,
        get_error: BaseException | None = None,
        stat_error: BaseException | None = None,
        remove_error: BaseException | None = None,
        exists_error: BaseException | None = None,
        hang_seconds: float = 0.0,
        read_size: int | None = None,
    ) -> None:
        self.objects: dict[str, bytes] = dict(objects or {})
        self.buckets: set[str] = set(buckets if buckets is not None else {BUCKET})
        self.put_error = put_error
        self.get_error = get_error
        self.stat_error = stat_error
        self.remove_error = remove_error
        self.exists_error = exists_error
        self.hang_seconds = hang_seconds
        self.read_size = read_size
        #: Every accepted upload, as ``(key, content_type, length, body)``.
        self.puts: list[tuple[str, str, int, bytes]] = []
        self.removals: list[str] = []
        self.reads: list[str] = []
        self.bucket_calls: list[str] = []

    def _maybe_hang(self) -> None:
        if self.hang_seconds:
            import time

            time.sleep(self.hang_seconds)

    # -- write -------------------------------------------------------------- #

    def put_object(self, *, Bucket, Key, Body, ContentLength, ContentType="", **kwargs):
        """Upload, asserting the exact shape the adapter promises to use."""
        assert Bucket == BUCKET, Bucket
        assert callable(getattr(Body, "read", None)), "the SDK requires a file-like object"
        assert isinstance(ContentLength, int) and ContentLength >= 0
        self._maybe_hang()
        if self.put_error is not None:
            raise self.put_error
        body = Body.read(ContentLength)
        # The adapter's contract with the SDK: the declared length is the real one.
        assert len(body) == ContentLength, f"declared {ContentLength}, supplied {len(body)}"
        self.objects[Key] = body
        self.puts.append((Key, ContentType, ContentLength, body))
        return {"ETag": '"fake"'}

    # -- read --------------------------------------------------------------- #

    def get_object(self, *, Bucket, Key, **kwargs):
        assert Bucket == BUCKET, Bucket
        self._maybe_hang()
        if self.get_error is not None:
            raise self.get_error
        if Key not in self.objects:
            raise _no_such_key()
        self.reads.append(Key)
        return {"Body": FakeResponse(self.objects[Key], declare_size=self.read_size)}

    def head_object(self, *, Bucket, Key, **kwargs):
        assert Bucket == BUCKET, Bucket
        self._maybe_hang()
        if self.stat_error is not None:
            raise self.stat_error
        if Key not in self.objects:
            raise _no_such_key()
        return {"ContentLength": len(self.objects[Key])}

    # -- delete ------------------------------------------------------------- #

    def delete_object(self, *, Bucket, Key, **kwargs):
        assert Bucket == BUCKET, Bucket
        if self.remove_error is not None:
            raise self.remove_error
        self.removals.append(Key)
        self.objects.pop(Key, None)
        return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    # -- bucket ------------------------------------------------------------- #

    def head_bucket(self, *, Bucket, **kwargs):
        self.bucket_calls.append(Bucket)
        if self.exists_error is not None:
            raise self.exists_error
        if Bucket not in self.buckets:
            raise _client_error("404", 404)
        return {"ResponseMetadata": {"HTTPStatusCode": 200}}


def _client_error(code: str, status: int = 500) -> ClientError:
    """A ``ClientError`` whose *message* carries every value that must not leak.

    The adapter must classify by ``response["Error"]["Code"]`` and the HTTP status,
    never by the text, so putting the sentinels in the message is the point: if the
    translator ever leaked ``str(exc)`` this fixture would catch it.
    """
    return ClientError(
        {
            "Error": {
                "Code": code,
                "Message": f"server message quoting {KEY_SENTINEL} for {ACCESS_SENTINEL}",
            },
            "ResponseMetadata": {
                "HTTPStatusCode": status,
                "RequestId": "request-id-should-not-appear",
                "HostId": "host-id-should-not-appear",
            },
        },
        "GetObject",
    )


def _s3_error(code: str, status: int = 500) -> ClientError:
    """Alias kept so the existing cases read the same: an S3-level failure."""
    return _client_error(code, status)


def _no_such_key() -> ClientError:
    return _client_error("NoSuchKey", 404)


def _rename_error() -> ClientError:
    """A rejected request a retry cannot fix (used by the configuration-code cases)."""
    return _client_error("InvalidAccessKeyId", 403)


# --------------------------------------------------------------------------- #
# fixtures and helpers
# --------------------------------------------------------------------------- #


@pytest.fixture
def settings() -> ObjectStorageSettings:
    """Connection settings with sentinel credentials, ignoring any local ``.env``."""
    return ObjectStorageSettings(
        _env_file=None,
        endpoint="http://127.0.0.1:9000",
        bucket=BUCKET,
        access_key=ACCESS_SENTINEL,
        secret_key=SECRET_SENTINEL,
        timeout_s=5.0,
    )


def _store(settings: ObjectStorageSettings, client: FakeS3Client, **kwargs: Any) -> S3BlobStore:
    return S3BlobStore(settings, client=client, **kwargs)


async def _chunks(data: bytes, size: int = 4096) -> AsyncIterator[bytes]:
    """An async iterator over ``data`` in ``size`` pieces (the caller's side)."""
    for start in range(0, len(data), size):
        yield data[start : start + size]


async def _collect(store: S3BlobStore, key: str, *, chunk_size: int = 1024) -> bytes:
    return b"".join([piece async for piece in store.iter_chunks(key, chunk_size=chunk_size)])


def _assert_no_leak(message: str) -> None:
    """Nothing the store knew may appear in a message a caller can log."""
    for sentinel in (SECRET_SENTINEL, ACCESS_SENTINEL, KEY_SENTINEL, "request-id-should"):
        assert sentinel not in message, f"{sentinel!r} leaked into: {message}"


# --------------------------------------------------------------------------- #
# put: streaming, digest math, limits
# --------------------------------------------------------------------------- #


async def test_put_hashes_and_counts_exactly_what_reaches_the_store(
    settings: ObjectStorageSettings,
) -> None:
    """The digest describes the stored bytes, not the announced size (D6)."""
    payload = bytes(range(256)) * 700  # 179_200 bytes, deliberately not a chunk multiple
    client = FakeS3Client()

    digest = await _store(settings, client).put(
        KEY_SENTINEL, _chunks(payload), max_bytes=10_000_000, content_type="application/pdf"
    )

    assert digest == BlobDigest(sha256_hex=hashlib.sha256(payload).hexdigest(), size=len(payload))
    assert await _store(settings, client).digest(KEY_SENTINEL) == digest
    assert client.objects[KEY_SENTINEL] == payload
    # The declared Content-Type travels with the object; the adapter never invents one.
    assert client.puts[0][1] == "application/pdf"


async def test_put_matches_hashlib_across_chunk_boundaries(
    settings: ObjectStorageSettings,
) -> None:
    """Same bytes, any chunking: the hash is over the stream, not over the chunking."""
    payload = b"the quick brown fox jumps over the lazy dog" * 2500
    client = FakeS3Client()
    store = _store(settings, client)

    for size in (1, 2, 3, 7, 64, 4096, len(payload)):
        digest = await store.put(
            KEY_SENTINEL, _chunks(payload, size), max_bytes=10_000_000, content_type="text/plain"
        )
        assert digest.sha256_hex == hashlib.sha256(payload).hexdigest(), f"chunk size {size}"
        assert digest.size == len(payload)


async def test_put_of_an_empty_stream_is_a_legal_empty_object(
    settings: ObjectStorageSettings,
) -> None:
    """An empty body must produce the empty-string hash, not an error or a skipped write."""
    client = FakeS3Client()

    digest = await _store(settings, client).put(
        KEY_SENTINEL, _chunks(b""), max_bytes=1024, content_type="text/plain"
    )

    assert digest == BlobDigest(sha256_hex=hashlib.sha256(b"").hexdigest(), size=0)
    assert client.objects[KEY_SENTINEL] == b""


async def test_put_skips_empty_chunks_instead_of_treating_them_as_eof(
    settings: ObjectStorageSettings,
) -> None:
    """An async iterator may legitimately yield ``b""``; stopping there would truncate."""
    payload = b"abcdefghij" * 100
    client = FakeS3Client()

    async def noisy() -> AsyncIterator[bytes]:
        for start in range(0, len(payload), 50):
            yield b""
            yield payload[start : start + 50]
            yield b""

    digest = await _store(settings, client).put(
        KEY_SENTINEL, noisy(), max_bytes=10_000, content_type="text/plain"
    )

    assert digest.size == len(payload)
    assert client.objects[KEY_SENTINEL] == payload


async def test_put_aborts_at_the_first_byte_past_the_limit(
    settings: ObjectStorageSettings,
) -> None:
    """``max_bytes`` is a running total, checked before the chunk is retained."""
    client = FakeS3Client()
    consumed = 0

    async def counted() -> AsyncIterator[bytes]:
        nonlocal consumed
        for _ in range(1000):
            consumed += 1
            yield b"x" * 1024

    with pytest.raises(BlobTooLarge):
        await _store(settings, client).put(
            KEY_SENTINEL, counted(), max_bytes=4096, content_type="text/plain"
        )

    # 4 chunks fit exactly; the fifth crosses the limit and stops the stream.
    assert consumed == 5
    # Nothing was uploaded, so there is no partial object for anyone to clean up.
    assert client.puts == []
    assert client.objects == {}


async def test_put_accepts_a_stream_exactly_at_the_limit(
    settings: ObjectStorageSettings,
) -> None:
    """The boundary must be inclusive, or a 25 MiB attachment of exactly 25 MiB fails."""
    client = FakeS3Client()
    payload = b"y" * 4096

    digest = await _store(settings, client).put(
        KEY_SENTINEL, _chunks(payload), max_bytes=4096, content_type="text/plain"
    )

    assert digest.size == 4096
    assert client.objects[KEY_SENTINEL] == payload


async def test_put_reports_a_short_stream_as_short_rather_than_failing(
    settings: ObjectStorageSettings,
) -> None:
    """The service compares this digest with what it meant to send; it must be honest."""
    client = FakeS3Client()
    payload = b"only this much"

    digest = await _store(settings, client).put(
        KEY_SENTINEL, _chunks(payload), max_bytes=1_000_000, content_type="text/plain"
    )

    assert digest.size == len(payload)
    assert digest.sha256_hex == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("max_bytes", [-1, -1000])
async def test_put_refuses_a_nonsensical_limit_before_touching_the_store(
    settings: ObjectStorageSettings, max_bytes: int
) -> None:
    client = FakeS3Client()

    with pytest.raises(ValueError, match="max_bytes"):
        await _store(settings, client).put(
            KEY_SENTINEL, _chunks(b"x"), max_bytes=max_bytes, content_type="text/plain"
        )
    assert client.puts == []


async def test_put_refuses_an_empty_key(settings: ObjectStorageSettings) -> None:
    with pytest.raises(ValueError, match="key"):
        await _store(settings, FakeS3Client()).put(
            "", _chunks(b"x"), max_bytes=10, content_type="text/plain"
        )


async def test_put_refuses_a_stream_of_something_other_than_bytes(
    settings: ObjectStorageSettings,
) -> None:
    """A str chunk would be encoded differently by every later reader."""

    async def text_chunks() -> AsyncIterator[bytes]:
        yield "not bytes"  # type: ignore[misc]

    with pytest.raises(TypeError):
        await _store(settings, FakeS3Client()).put(
            KEY_SENTINEL, text_chunks(), max_bytes=1024, content_type="text/plain"
        )


async def test_put_defaults_the_content_type_only_when_it_is_blank(
    settings: ObjectStorageSettings,
) -> None:
    """Never invent a MIME type from a filename: the caller's value is the contract."""
    client = FakeS3Client()

    await _store(settings, client).put(KEY_SENTINEL, _chunks(b"x"), max_bytes=10, content_type="")

    assert client.puts[0][1] == "application/octet-stream"


# --------------------------------------------------------------------------- #
# digest: the D6 re-read, not a stat
# --------------------------------------------------------------------------- #


async def test_digest_re_reads_the_object_rather_than_trusting_a_stat(
    settings: ObjectStorageSettings,
) -> None:
    """The truth check must consume the bytes; a stat-only implementation would not."""
    payload = b"recomputed-from-the-stored-bytes" * 400
    client = FakeS3Client(objects={KEY_SENTINEL: payload})

    digest = await _store(settings, client).digest(KEY_SENTINEL)

    assert client.reads == [KEY_SENTINEL], "digest must issue a real GET"
    assert digest.sha256_hex == hashlib.sha256(payload).hexdigest()
    assert digest.size == len(payload)


async def test_digest_notices_that_the_store_returned_different_bytes(
    settings: ObjectStorageSettings,
) -> None:
    """The whole point: a store that altered the object must produce a different hash."""
    client = FakeS3Client(objects={KEY_SENTINEL: b"tampered"})
    written = hashlib.sha256(b"original").hexdigest()

    digest = await _store(settings, client).digest(KEY_SENTINEL)

    assert digest.sha256_hex != written
    assert digest.size == len(b"tampered")


async def test_digest_releases_the_response_even_when_the_read_fails(
    settings: ObjectStorageSettings,
) -> None:
    """A leaked pooled connection is invisible until the pool runs dry under load."""
    response = FakeResponse(b"x" * 100)

    class ShortClient(FakeS3Client):
        def get_object(self, *, Bucket, Key, **kwargs):
            return {"Body": response}

    class Boom(response.__class__):
        def read(self, size: int = -1) -> bytes:
            raise OSError("socket died mid-stream")

    boom = Boom(b"x")

    class FailingReadClient(FakeS3Client):
        def get_object(self, *, Bucket, Key, **kwargs):
            return {"Body": boom}

    with pytest.raises(StorageUnavailable):
        await _store(settings, FailingReadClient()).digest(KEY_SENTINEL)

    # boto3 exposes close() only; there is no release_conn on a StreamingBody.
    assert boom.closed == 1


async def test_digest_of_an_absent_key_is_a_storage_failure(
    settings: ObjectStorageSettings,
) -> None:
    """A missing object is never "empty": the pair of digests would silently agree."""
    with pytest.raises(StorageUnavailable):
        await _store(settings, FakeS3Client()).digest(KEY_SENTINEL)


# --------------------------------------------------------------------------- #
# iter_chunks: bounded streaming, and absence is an error
# --------------------------------------------------------------------------- #


async def test_iter_chunks_yields_the_object_in_bounded_pieces(
    settings: ObjectStorageSettings,
) -> None:
    payload = bytes(range(256)) * 40
    store = _store(settings, FakeS3Client(objects={KEY_SENTINEL: payload}))

    pieces = [piece async for piece in store.iter_chunks(KEY_SENTINEL, chunk_size=100)]

    assert b"".join(pieces) == payload
    assert all(len(piece) <= 100 for piece in pieces), "a chunk exceeded the bound"
    assert len(pieces) > 1, "the object must not arrive as one buffered piece"


async def test_iter_chunks_of_a_missing_key_raises_instead_of_yielding_nothing(
    settings: ObjectStorageSettings,
) -> None:
    """A silent empty stream would be served as a 200 with zero bytes."""
    store = _store(settings, FakeS3Client())

    received: list[bytes] = []
    with pytest.raises(StorageUnavailable):
        async for piece in store.iter_chunks(KEY_SENTINEL):
            received.append(piece)

    assert received == []


async def test_iter_chunks_releases_the_response_when_the_consumer_goes_away(
    settings: ObjectStorageSettings,
) -> None:
    """A client disconnect closes the stream; the pooled socket must still return.

    Modelled the way the real transport works. Starlette iterates the body generator
    inside a task; when the client vanishes that task is cancelled, ``CancelledError``
    is thrown into the generator, the ``finally`` runs, and the response returns to the
    pool. Two things are asserted, because a leak would hide behind either one:

    * the cancellation propagates untouched - it is never rewritten as a storage
      failure, which would make a torn-down request look retryable;
    * the SDK response is released exactly once, including a close after the
      cancellation (which is how the framework deterministically finishes the
      iterator rather than leaving it to the garbage collector).
    """
    payload = b"z" * 10_000
    response = FakeResponse(payload)
    started = asyncio.Event()
    first_piece: list[bytes] = []

    class Client(FakeS3Client):
        def get_object(self, *, Bucket, Key, **kwargs):
            self.reads.append(Key)
            return {"Body": response}

    store = _store(settings, Client())
    stream = store.iter_chunks(KEY_SENTINEL, chunk_size=16)

    async def consume() -> None:
        async for piece in stream:
            first_piece.append(piece)
            started.set()
            await asyncio.sleep(3600)  # a client that stops reading, forever

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert first_piece, "the stream must have started before the disconnect"
    # Deterministic close, exactly as a framework would do it: no reliance on when the
    # garbage collector finalises an abandoned async generator.
    await stream.aclose()  # type: ignore[attr-defined]

    assert response.closed == 1


async def test_iter_chunks_releases_the_response_on_an_explicit_aclose(
    settings: ObjectStorageSettings,
) -> None:
    """A caller that closes the iterator itself gets the same guarantee."""
    response = FakeResponse(b"q" * 1000)

    class Client(FakeS3Client):
        def get_object(self, *, Bucket, Key, **kwargs):
            return {"Body": response}

    stream = _store(settings, Client()).iter_chunks(KEY_SENTINEL, chunk_size=16)
    await stream.__anext__()
    await stream.aclose()  # type: ignore[attr-defined]

    assert response.closed == 1


async def test_iter_chunks_refuses_a_nonsensical_chunk_size(
    settings: ObjectStorageSettings,
) -> None:
    store = _store(settings, FakeS3Client(objects={KEY_SENTINEL: b"x"}))

    with pytest.raises(ValueError, match="chunk_size"):
        store.iter_chunks(KEY_SENTINEL, chunk_size=0)


# --------------------------------------------------------------------------- #
# stat: the one call where "absent" is an answer
# --------------------------------------------------------------------------- #


async def test_stat_returns_none_for_an_absent_key(settings: ObjectStorageSettings) -> None:
    assert await _store(settings, FakeS3Client()).stat(KEY_SENTINEL) is None


async def test_stat_returns_the_size_of_a_present_key(settings: ObjectStorageSettings) -> None:
    store = _store(settings, FakeS3Client(objects={KEY_SENTINEL: b"12345"}))

    stat = await store.stat(KEY_SENTINEL)

    assert stat is not None
    assert (stat.key, stat.size) == (KEY_SENTINEL, 5)


async def test_a_missing_bucket_is_never_reported_as_a_missing_object(
    settings: ObjectStorageSettings,
) -> None:
    """Otherwise the service would re-upload into a bucket that does not exist."""
    store = _store(settings, FakeS3Client(stat_error=_s3_error("NoSuchBucket", 404)))

    with pytest.raises(StorageClientError):
        await store.stat(KEY_SENTINEL)


# --------------------------------------------------------------------------- #
# delete: idempotent
# --------------------------------------------------------------------------- #


async def test_delete_removes_the_object(settings: ObjectStorageSettings) -> None:
    client = FakeS3Client(objects={KEY_SENTINEL: b"x"})

    await _store(settings, client).delete(KEY_SENTINEL)

    assert KEY_SENTINEL not in client.objects
    assert client.removals == [KEY_SENTINEL]


async def test_delete_of_an_absent_key_succeeds(settings: ObjectStorageSettings) -> None:
    """Cleanup runs from ``finally``; "already gone" is the desired end state."""
    client = FakeS3Client()

    await _store(settings, client).delete(KEY_SENTINEL)
    await _store(settings, client).delete(KEY_SENTINEL)  # and again, for good measure

    assert client.removals == [KEY_SENTINEL, KEY_SENTINEL]


async def test_delete_still_raises_when_the_store_is_unreachable(
    settings: ObjectStorageSettings,
) -> None:
    """Idempotent is not the same as silent: a real outage must surface."""
    store = _store(settings, FakeS3Client(remove_error=_s3_error("InternalError", 500)))

    with pytest.raises(StorageUnavailable):
        await store.delete(KEY_SENTINEL)


# --------------------------------------------------------------------------- #
# error mapping: port vocabulary, no SDK leak
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_s3_error("InternalError", 500), StorageUnavailable),
        (_s3_error("SlowDown", 503), StorageUnavailable),
        (_s3_error("AccessDenied", 403), StorageClientError),
        (_s3_error("NoSuchBucket", 404), StorageClientError),
        (
            EndpointConnectionError(endpoint_url=f"http://127.0.0.1:9000/{KEY_SENTINEL}"),
            StorageUnavailable,
        ),
        (ConnectionResetError("connection reset by peer"), StorageUnavailable),
        (OSError("socket timeout"), StorageUnavailable),
    ],
)
async def test_every_sdk_failure_becomes_a_port_error(
    settings: ObjectStorageSettings,
    error: BaseException,
    expected: type[Exception],
) -> None:
    """A raw SDK exception must never escape the adapter."""
    store = _store(settings, FakeS3Client(get_error=error))

    with pytest.raises(expected) as caught:
        await store.digest(KEY_SENTINEL)

    assert not isinstance(caught.value, ClientError | EndpointConnectionError)
    _assert_no_leak(str(caught.value))
    # The original stays reachable for a local debugger, on the cause chain only.
    assert caught.value.__cause__ is error


async def test_a_missing_object_maps_to_storage_unavailable(
    settings: ObjectStorageSettings,
) -> None:
    """The absent-key case is still ``StorageUnavailable`` for the service layer."""
    store = _store(settings, FakeS3Client(get_error=_no_such_key()))

    with pytest.raises(StorageUnavailable) as caught:
        await store.digest(KEY_SENTINEL)

    _assert_no_leak(str(caught.value))


async def test_a_client_error_is_still_a_storage_unavailable(
    settings: ObjectStorageSettings,
) -> None:
    """One name for callers to catch; the narrower type is for probes and operators."""
    assert issubclass(StorageClientError, StorageUnavailable)

    store = _store(settings, FakeS3Client(put_error=_s3_error("AccessDenied", 403)))
    with pytest.raises(StorageUnavailable):
        await store.put(KEY_SENTINEL, _chunks(b"x"), max_bytes=10, content_type="text/plain")


async def test_put_maps_an_upload_failure_without_leaking_the_key(
    settings: ObjectStorageSettings,
) -> None:
    store = _store(settings, FakeS3Client(put_error=_s3_error("InternalError", 500)))

    with pytest.raises(StorageUnavailable) as caught:
        await store.put(
            KEY_SENTINEL, _chunks(b"payload"), max_bytes=10_000, content_type="text/plain"
        )

    _assert_no_leak(str(caught.value))
    assert "put" in str(caught.value)


async def test_a_transient_error_is_not_confused_with_a_configuration_one(
    settings: ObjectStorageSettings,
) -> None:
    """The 503 is retryable; the 403 is not, and the two must be distinguishable."""
    transient = _store(settings, FakeS3Client(stat_error=_s3_error("SlowDown", 503)))
    permanent = _store(settings, FakeS3Client(stat_error=_s3_error("SignatureDoesNotMatch", 403)))

    with pytest.raises(StorageUnavailable) as retryable:
        await transient.stat(KEY_SENTINEL)
    with pytest.raises(StorageClientError) as broken:
        await permanent.stat(KEY_SENTINEL)

    assert not isinstance(retryable.value, StorageClientError)
    assert isinstance(broken.value, StorageClientError)


# --------------------------------------------------------------------------- #
# timeouts
# --------------------------------------------------------------------------- #


async def test_a_hung_call_becomes_a_timeout_not_a_wedge(
    settings: ObjectStorageSettings,
) -> None:
    """Without the deadline a stuck socket would pin a thread and a task forever.

    The hang is 0.5 s against a 0.05 s deadline: long enough that a missing timeout
    would be unmistakable, short enough that the abandoned worker thread (see
    ``_call``) cannot slow the suite down. The elapsed-time assertion is the real
    check - "it eventually raised" would also pass for an implementation that waited
    out the whole hang.
    """
    client = FakeS3Client(hang_seconds=0.5)
    store = _store(settings, client, timeout_s=0.05)

    started = asyncio.get_running_loop().time()
    with pytest.raises(StorageUnavailable, match="timed out"):
        await store.stat(KEY_SENTINEL)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 0.4, f"the deadline did not bound the call (took {elapsed:.2f}s)"


async def test_the_timeout_must_be_positive(settings: ObjectStorageSettings) -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        S3BlobStore(settings, client=FakeS3Client(), timeout_s=0)  # type: ignore[arg-type]


async def test_the_timeout_defaults_from_the_settings(settings: ObjectStorageSettings) -> None:
    assert S3BlobStore(settings, client=FakeS3Client()).timeout_s == settings.timeout_s  # type: ignore[arg-type]


async def test_cancellation_is_never_rewritten_as_a_storage_failure(
    settings: ObjectStorageSettings,
) -> None:
    """A cancelled request must be torn down, not retried as if the store were down."""

    class Cancelling(FakeS3Client):
        def head_object(self, *, Bucket, Key, **kwargs):
            raise asyncio.CancelledError()

    store = _store(settings, Cancelling())

    with pytest.raises(asyncio.CancelledError):
        await store.stat(KEY_SENTINEL)


# --------------------------------------------------------------------------- #
# readiness
# --------------------------------------------------------------------------- #


async def test_ready_is_true_when_the_bucket_exists(settings: ObjectStorageSettings) -> None:
    assert await _store(settings, FakeS3Client(buckets={BUCKET})).ready() is True


async def test_ready_is_false_when_the_bucket_is_absent(settings: ObjectStorageSettings) -> None:
    """A readiness probe answers ``False``; it does not raise."""
    assert await _store(settings, FakeS3Client(buckets=set())).ready() is False


@pytest.mark.parametrize(
    "error",
    [
        _s3_error("AccessDenied", 403),
        _s3_error("InternalError", 500),
        ConnectionResetError("no route to host"),
    ],
)
async def test_ready_answers_false_for_every_failure(
    settings: ObjectStorageSettings, error: BaseException
) -> None:
    """A probe that throws cannot distinguish "not ready" from "probe broken"."""
    assert await _store(settings, FakeS3Client(exists_error=error)).ready() is False


# --------------------------------------------------------------------------- #
# the adapter's own surface
# --------------------------------------------------------------------------- #


def test_the_adapter_constructs_a_client_from_settings_without_credentials_in_repr(
    settings: ObjectStorageSettings,
) -> None:
    store = S3BlobStore(settings)

    rendered = repr(store)
    assert "127.0.0.1:9000" in rendered
    assert BUCKET in rendered
    _assert_no_leak(rendered)
    assert store.settings is settings


def test_the_client_property_is_the_injected_one(settings: ObjectStorageSettings) -> None:
    """The bootstrap helpers need it; a request path must not."""
    client = FakeS3Client()

    assert S3BlobStore(settings, client=client).client is client  # type: ignore[arg-type]


def test_the_sdk_is_reachable_from_this_module_only() -> None:
    """Layering: the adapter is the only module that touches the S3 SDK.

    Asserted by name against the module's own globals, so a future refactor that
    imports the SDK somewhere else fails here rather than in review.
    """
    imported = {
        name
        for name in vars(s3_module)
        if name in {"boto3", "Config", "ClientError", "BotoCoreError"}
    }

    assert imported == {"boto3", "Config", "ClientError", "BotoCoreError"}


async def test_chunk_size_is_the_size_the_contract_names(
    settings: ObjectStorageSettings,
) -> None:
    """64 KiB is a protocol number (D6), not a preference."""
    assert s3_module.CHUNK_SIZE == 65536
