"""The S3/MinIO adapter: the only module in the service that knows a store exists.

Design notes that matter
------------------------
**Everything runs off the event loop.** The MinIO SDK is synchronous and
``urllib3``-backed, so every call goes through :meth:`S3BlobStore._call`, which
wraps it in :func:`asyncio.to_thread` under an explicit ``asyncio.wait_for``
deadline. Without the deadline a hung socket would pin a thread from the default
executor *and* the awaiting task forever, and enough of those would wedge the
process - the timeout is what turns "the store is having a bad day" into a
``StorageUnavailable`` the caller can retry.

**The upload never holds the object in memory.** ``put`` pulls from the caller's
async iterator 64 KiB at a time, hashing and counting as it goes, and spools those
bytes into a ``SpooledTemporaryFile`` that stays in RAM only for small objects and
rolls over to disk past a fixed threshold. The SDK then streams *out of the spool*
with an exact length, which is the one shape it can upload without either buffering
the whole object itself or guessing a multipart part size. Memory is therefore
O(chunk) plus a constant; the object lands on disk before it lands in the store,
and a 25 MiB attachment cannot become a 25 MiB allocation per concurrent uploader.

**Two hashes, one truth.** ``put`` measures what it wrote; ``digest`` re-reads the
object and measures it again (D6). Neither an ETag nor a ``Content-Length`` is
consulted: both are the store's *claim* about the object, and the whole point is an
independent measurement. ``put``'s digest is the first opinion; only the pair is
evidence.

**No SDK exception escapes unshaped.** ``minio`` raises ``S3Error`` whose message
embeds the object key, the bucket, the request id and whatever the server echoed -
``InvalidResponseError`` even embeds a response *body*. All of it is translated to
``StorageUnavailable`` (or its ``StorageClientError`` subclass for faults a retry
cannot fix) carrying a fixed phrase plus structured classification (the S3 code, the
HTTP status). The original stays on ``__cause__``, which only a local debugger sees.

**Nothing here logs.** Not a key, not a MIME value, not a byte count, not a
credential. A store call knows all four, and every one of them is user data or an
authentication factor. The call sites that *may* log - the readiness probe, the GC
script - log an outcome, not the arguments.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import tempfile
from collections.abc import AsyncIterator, Callable
from typing import Any, Final

from minio import Minio
from minio.error import InvalidResponseError, MinioException, S3Error, ServerError

from revnote_api.storage.ports import (
    BlobDigest,
    BlobStat,
    BlobStoreError,
    BlobTooLarge,
    StorageUnavailable,
)
from revnote_api.storage.settings import ObjectStorageSettings

__all__ = [
    "CHUNK_SIZE",
    "SPOOL_MAX_BYTES",
    "S3BlobStore",
    "StorageClientError",
]

#: Streaming chunk size, in bytes. 64 KiB is the size the plan fixes (D6): large
#: enough that per-read overhead vanishes on a 25 MiB object, small enough that the
#: per-reader buffer is a rounding error in memory accounting.
CHUNK_SIZE: Final[int] = 65536

#: Bytes a spooled upload may keep in RAM before rolling over to a temp file. Eight
#: chunks (512 KiB) covers the small attachments - a pasted screenshot, a PDF page -
#: without a syscall per upload, while keeping the resident cost of a large one flat.
SPOOL_MAX_BYTES: Final[int] = CHUNK_SIZE * 8

#: S3 codes that mean "this key is not there". Only these become
#: :meth:`S3BlobStore.stat` returning ``None``; everything else still raises.
_MISSING_OBJECT_CODES: Final[frozenset[str]] = frozenset(
    {"NoSuchKey", "NoSuchObject", "NoSuchVersion", "ResourceNotFound"}
)

#: Codes that mean "the store answered, but this deployment is wrong": the bucket is
#: absent, or the credentials were rejected. Deliberately **not** folded into
#: "the key is missing" - a botched bucket must never look like a lost file - and not
#: treated as retryable, because a retry cannot fix a configuration mistake.
_CONFIGURATION_CODES: Final[frozenset[str]] = frozenset(
    {
        "AccessDenied",
        "InvalidAccessKeyId",
        "NoSuchBucket",
        "SignatureDoesNotMatch",
        "XMinioInvalidObjectName",
    }
)

#: Exceptions that mean the transport failed rather than the store answering.
_TRANSPORT_ERRORS: Final[tuple[type[BaseException], ...]] = (TimeoutError, ConnectionError, OSError)


class StorageClientError(StorageUnavailable):
    """The store rejected a request in a way a retry cannot fix.

    A *subclass* of :class:`~revnote_api.storage.ports.StorageUnavailable` on
    purpose. The frozen port promises that storage failures are
    ``StorageUnavailable``, and the service maps that to a retryable 503; making this
    a sibling would force every caller to know a second name, and the one caller that
    forgot would turn a missing bucket into an opaque ``internal``. It stays a
    distinct type so a probe (:meth:`S3BlobStore.ready`) and an operator can tell
    "misconfigured" from "unreachable", while the request path needs no extra
    knowledge. Retrying such a request is harmless - it keeps failing until somebody
    fixes the deployment, and the logs say which deployment.
    """


class _MissingObject(StorageUnavailable):
    """The key is not there - still a ``StorageUnavailable`` for callers.

    A private subclass so ``stat`` (answer ``None``) and ``delete`` (succeed) can
    catch the narrow case, while the service layer keeps seeing the port's own
    vocabulary. Internal on purpose: no other lane should branch on it.
    """


class S3BlobStore:
    """A :class:`~revnote_api.storage.ports.BlobStore` over the official SDK.

    One instance per process is fine: the SDK's ``urllib3`` pool is thread-safe and
    every call here is stateless, so two concurrent uploads cannot observe each
    other.
    """

    def __init__(
        self,
        settings: ObjectStorageSettings,
        *,
        client: Minio | None = None,
        timeout_s: float | None = None,
    ) -> None:
        """Build an adapter; ``client`` exists so a unit test can inject a fake.

        ``timeout_s`` defaults to the settings value and bounds one complete SDK
        call. It is a *per-call* deadline, not a total for the whole object: a
        streaming read issues one call per chunk, which is why each read can afford
        the same budget a whole small call would get.
        """
        self._settings = settings
        self._timeout_s = float(timeout_s if timeout_s is not None else settings.timeout_s)
        if self._timeout_s <= 0:
            raise ValueError("timeout_s must be positive")
        self._client = client if client is not None else _build_client(settings)

    # -- introspection ------------------------------------------------------ #

    @property
    def settings(self) -> ObjectStorageSettings:
        """The connection this adapter was built from (repring it is safe)."""
        return self._settings

    @property
    def client(self) -> Minio:
        """The SDK client this adapter drives.

        Exposed for the ops helpers in :mod:`revnote_api.storage.bootstrap`, which
        need bucket administration and bulk deletes the port deliberately does not
        offer. It is **not** a seam for request paths: everything the service may do
        goes through the port, and reaching for this from a route would be the exact
        layering break the port exists to prevent.
        """
        return self._client

    @property
    def timeout_s(self) -> float:
        """Per-call deadline in seconds."""
        return self._timeout_s

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        """Address and bucket only: the credentials are never rendered."""
        return f"S3BlobStore({self._settings.diagnostics})"

    # -- write -------------------------------------------------------------- #

    async def put(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        *,
        max_bytes: int,
        content_type: str,
    ) -> BlobDigest:
        """Store one object from a byte stream; return the digest of *what landed*.

        Three properties the service depends on:

        * ``max_bytes`` is checked against the **accumulated** size before the chunk
          is retained, so the abort happens at the first byte past the limit
          (:class:`~revnote_api.storage.ports.BlobTooLarge`) and an oversized upload
          never reaches the store - no partial object is created, so there is nothing
          for the database or the GC to reason about;
        * the SHA-256 and the size describe the bytes actually spooled, i.e. the exact
          bytes handed to the SDK, so a stream that ends early produces a short digest
          instead of an error. The caller compares that against what it meant to send;
        * the digest is computed while writing, so the bytes are never read twice.

        Empty chunks are skipped rather than treated as end-of-stream: an async
        iterator may legitimately yield ``b""`` between reads, and stopping there
        would truncate the object at a point the client never chose.
        """
        if max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        if not key:
            raise ValueError("key must not be empty")

        hasher = hashlib.sha256()
        total = 0
        # ``mode="w+b"`` because the SDK calls ``read()``; the spool starts in RAM and
        # rolls over to a real temp file past SPOOL_MAX_BYTES, so resident memory does
        # not grow with the object.
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES, mode="w+b") as spool:
            async for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("chunks must yield bytes")
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    # Refuse before the offending bytes are retained anywhere, and
                    # before the SDK has been touched.
                    raise BlobTooLarge(f"object exceeds the {max_bytes}-byte limit")
                hasher.update(chunk)
                spool.write(chunk)

            digest = BlobDigest(sha256_hex=hasher.hexdigest(), size=total)
            spool.seek(0)
            await self._call(
                "put",
                self._client.put_object,
                self._settings.bucket,
                key,
                spool,
                total,
                content_type=content_type or "application/octet-stream",
            )
        return digest

    # -- read --------------------------------------------------------------- #

    async def digest(self, key: str) -> BlobDigest:
        """Re-read the object and recompute SHA-256 and size (the D6 truth check).

        Every stored byte is pulled back through the same chunk size used to write it
        and hashed again, so the result measures the object rather than describing
        it. ``stat_object``/``etag`` are deliberately not used: an ETag is not
        necessarily a content digest (multipart, SSE, replication all change it), and
        even an exact one is the store reporting on itself - which is exactly the
        claim this call exists to check.
        """
        hasher = hashlib.sha256()
        total = 0
        response = await self._call(
            "digest",
            self._client.get_object,
            self._settings.bucket,
            key,
        )
        try:
            while True:
                piece = await self._call("digest", response.read, CHUNK_SIZE)
                if not piece:
                    break
                if not isinstance(piece, bytes):  # pragma: no cover - urllib3 contract
                    raise StorageUnavailable("object storage returned a non-bytes read")
                hasher.update(piece)
                total += len(piece)
        finally:
            await _release(response)
        return BlobDigest(sha256_hex=hasher.hexdigest(), size=total)

    async def stat(self, key: str) -> BlobStat | None:
        """Size of one object; ``None`` when the key is genuinely absent.

        The one place "absent" is an answer rather than an error, because the question
        *is* whether the key exists. Every other S3 failure still raises - a missing
        bucket included, since reporting it as "the object is gone" would make the
        service cheerfully re-upload into a bucket that does not exist.
        """
        try:
            result = await self._call(
                "stat",
                self._client.stat_object,
                self._settings.bucket,
                key,
            )
        except _MissingObject:
            return None
        return BlobStat(key=key, size=int(getattr(result, "size", 0)))

    def iter_chunks(self, key: str, *, chunk_size: int = CHUNK_SIZE) -> AsyncIterator[bytes]:
        """Stream one object in bounded chunks; ``StorageUnavailable`` if absent.

        The absent-key case **raises** rather than yielding nothing: an async
        generator that ends immediately is indistinguishable from an empty object, and
        the HTTP layer would answer ``200`` with zero bytes for a file the user
        believes is stored. The object is therefore opened *before* the first byte is
        yielded (see :meth:`_iter_chunks`).

        Backpressure is the async generator's own: each ``response.read`` runs on a
        worker thread only when the consumer asks for the next chunk, so a slow client
        stalls the store read instead of filling a buffer.
        """
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        return self._iter_chunks(key, chunk_size)

    async def _iter_chunks(self, key: str, chunk_size: int) -> AsyncIterator[bytes]:
        """The generator behind :meth:`iter_chunks` (see its docstring).

        Shaped as an async generator so cancellation is honest: when the client
        disconnects Starlette stops iterating, ``GeneratorExit`` runs the ``finally``,
        and the SDK response is released back to the pool. Buffering the object into a
        queue first would leak the connection - and the pooled socket - on exactly the
        failure path that is most common in production.
        """
        response = await self._call(
            "read",
            self._client.get_object,
            self._settings.bucket,
            key,
        )
        try:
            while True:
                piece = await self._call("read", response.read, chunk_size)
                if not piece:
                    return
                if not isinstance(piece, bytes):  # pragma: no cover - urllib3 contract
                    raise StorageUnavailable("object storage returned a non-bytes read")
                yield piece
        finally:
            await _release(response)

    # -- delete ------------------------------------------------------------- #

    async def delete(self, key: str) -> None:
        """Remove ``key``; deleting an absent key succeeds.

        Cleanup runs from ``finally`` blocks and from the GC sweep, so it must be safe
        to repeat: "already gone" is the desired end state, not a failure that would
        mask the error actually being handled.
        """
        try:
            await self._call(
                "delete",
                self._client.remove_object,
                self._settings.bucket,
                key,
            )
        except _MissingObject:
            # S3 DELETE is specified as idempotent and answers 204 for an absent key,
            # but a proxy or a bucket policy can still surface NoSuchKey. Same end
            # state, so the same outcome.
            return

    # -- readiness ---------------------------------------------------------- #

    async def ready(self) -> bool:
        """Whether the configured bucket is reachable right now.

        Answers ``False`` instead of raising: a readiness probe that throws cannot
        distinguish "not ready yet" from "the probe itself is broken". The probe is
        deliberately shallow (``bucket_exists``) - a deep probe would need write access
        on a health endpoint, which is a much worse thing to expose than a slightly
        weaker check.
        """
        try:
            return bool(
                await self._call(
                    "ready",
                    self._client.bucket_exists,
                    self._settings.bucket,
                )
            )
        except BlobStoreError:
            return False

    # -- internals ---------------------------------------------------------- #

    async def _call(
        self,
        operation: str,
        function: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """Run one blocking SDK call on a thread under an explicit deadline.

        The single funnel every adapter call goes through, which is what makes the
        timeout claim true: there is no second path to the SDK that could forget it.
        Every exception is translated here (see :meth:`_translate`), so no raw
        ``minio`` exception - whose message embeds the object key, the bucket, the
        request id and, in one case, a response body - ever leaves this module.
        """
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(function, *args, **kwargs),
                timeout=self._timeout_s,
            )
        except TimeoutError as exc:
            # ``wait_for`` cancels the awaiting task; the worker thread finishes on its
            # own. That is the documented limit of off-loop calls, and it is why the
            # SDK's own socket timeout is configured to the same number: the thread is
            # bounded too, not merely abandoned.
            raise StorageUnavailable(f"object storage timed out during {operation}") from exc
        except asyncio.CancelledError:
            # A live cancellation is not a storage verdict: propagate untouched, so a
            # request can be torn down instead of being retried as a store failure.
            raise
        except (KeyboardInterrupt, SystemExit):
            # Process-level signals are never reinterpreted as a storage outcome.
            raise
        except BlobStoreError:
            raise
        except BaseException as exc:
            raise self._translate(operation, exc) from exc

    def _translate(self, operation: str, exc: BaseException) -> BlobStoreError:
        """Map one SDK/transport exception onto the port vocabulary.

        The classification reads the S3 *code* and the HTTP status - structured values
        - never the message text, because that text carries the object key and whatever
        the server chose to echo. The returned message is a fixed phrase plus that
        classification, so it is stable, greppable and free of user data.

        ``_MISSING_OBJECT_CODES`` is tested before ``_CONFIGURATION_CODES`` on purpose
        and ``NoSuchBucket`` appears in the latter only: a lost bucket is a deployment
        fault, and letting it collapse into "key absent" is how a masked outage becomes
        a silent re-upload.
        """
        if isinstance(exc, _MissingObject):
            return exc
        if isinstance(exc, S3Error):
            code = exc.code or "unknown"
            status = getattr(getattr(exc, "response", None), "status", None)
            if code in _MISSING_OBJECT_CODES:
                return _MissingObject(f"object is absent during {operation} ({code})")
            if code in _CONFIGURATION_CODES:
                return StorageClientError(
                    f"object storage rejected {operation} ({code}, http {status})"
                )
            return StorageUnavailable(
                f"object storage failed during {operation} ({code}, http {status})"
            )
        if isinstance(exc, ServerError):
            return StorageUnavailable(
                f"object storage failed during {operation} (http {exc.status_code})"
            )
        if isinstance(exc, InvalidResponseError):
            # Its message embeds the response *body*; only the fact is kept.
            return StorageUnavailable(f"object storage answered {operation} with a non-S3 body")
        if isinstance(exc, MinioException):
            return StorageUnavailable(f"object storage failed during {operation} (sdk)")
        if isinstance(exc, _TRANSPORT_ERRORS):
            return StorageUnavailable(f"object storage was unreachable during {operation}")
        if isinstance(exc, ValueError):
            # The SDK validates keys and bucket names with ``ValueError``; a rejected
            # argument is our bug or a malformed key, never a transient condition.
            return StorageClientError(f"object storage rejected {operation} (invalid argument)")
        return StorageUnavailable(f"object storage failed during {operation} (unexpected)")


async def _release(response: Any) -> None:
    """Close and release an SDK response without letting the cleanup raise.

    ``release_conn`` returns the socket to the pool; if it fails the connection is
    already unusable and the exception would replace whatever the ``finally`` is
    unwinding - including the real reason the read stopped. Swallowed on purpose.
    """
    closer = getattr(response, "close", None)
    if callable(closer):
        with contextlib.suppress(Exception):
            closer()
    releaser = getattr(response, "release_conn", None)
    if callable(releaser):
        with contextlib.suppress(Exception):
            releaser()


def _build_client(settings: ObjectStorageSettings) -> Minio:
    """Construct the SDK client from settings.

    ``secure`` comes from :attr:`ObjectStorageSettings.is_secure`, which reconciles an
    ``https://`` endpoint with the separate flag. The SDK is given the bare
    ``host:port`` because it prepends the scheme itself.

    The SDK's own ``urllib3`` pool is left at its defaults deliberately: it already
    retries idempotent 5xx responses, and giving it a second, differently-tuned retry
    policy here would multiply the two - so the visible retry budget would no longer
    be the one the timeout describes.
    """
    return Minio(
        settings.host_port,
        access_key=settings.access_key,
        secret_key=settings.secret_key,
        secure=settings.is_secure,
    )
