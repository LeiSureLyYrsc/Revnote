"""The object-storage port: the only storage vocabulary the service layer knows.

Why a port instead of the client
--------------------------------
``services/attachments.py`` has to move bytes somewhere, but it must not know
*where*. Everything it is allowed to touch lives in this module: two frozen value
objects, one ``Protocol`` and the exception vocabulary. There is **no third-party
import here**, not even a lazy one, so the service layer can be imported - and its
tests can run - without an object-store SDK being involved, and the D4 rule
("the API proxies the bytes, nobody presigns") is checkable by reading one import
list rather than auditing call sites.

Why the size and the hash travel together
----------------------------------------
:class:`BlobDigest` is what the D6 truth check compares: the digest computed while
streaming the bytes *in*, and the digest recomputed by re-reading them *out*. A
size alone cannot tell "the store truncated the object" apart from "we sent fewer
bytes than we meant to", and an ETag is not recomputable by a client that does not
know the store's chunking - which is why neither of them is the contract here.

Why ``iter_chunks`` raises instead of ending early
--------------------------------------------------
An async generator that yields nothing for an absent object is indistinguishable
from an empty object, so the HTTP layer would answer ``200`` with zero bytes for a
file the user believes is stored. Absence is therefore an error
(:class:`StorageUnavailable`) on the read paths, never an empty stream. ``stat`` is
the one call allowed to answer "absent", because asking *whether* a key exists is
its whole job.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final, Protocol

__all__ = [
    "SHA256_HEX_LENGTH",
    "BlobDigest",
    "BlobStat",
    "BlobStore",
    "BlobStoreError",
    "BlobTooLarge",
    "StorageUnavailable",
]

#: Length of a lowercase hexadecimal SHA-256, i.e. the only ``sha256_hex`` shape the
#: port promises. Exported so a caller can bounds-check a *declared* hash (the
#: upload's optional ``X-Content-SHA256``) against the same number the adapter
#: produces, instead of spelling 64 in two places.
SHA256_HEX_LENGTH: Final[int] = 64


class BlobStoreError(Exception):
    """Base of every failure this port raises.

    Subclasses exist so a caller can be exhaustive without catching ``Exception``:
    a ``except BlobStoreError`` that then inspects ``isinstance`` can never swallow
    a programming error such as a closed event loop or a bad argument. Catching
    this class *alone* and treating it as "storage is down" is wrong - it would
    also swallow :class:`BlobTooLarge`, which is a client problem.
    """


class StorageUnavailable(BlobStoreError):
    """The store is unreachable, timed out, or answered with a transient failure.

    The caller's only correct move is to retry the identical request: the service
    maps this to ``attachment_storage_unavailable`` (503, retryable). A key that is
    unexpectedly *missing* arrives as this error too, because "the bytes I just
    wrote are not there" is a storage problem; answering "empty file" instead would
    silently publish a zero-byte attachment.
    """


class BlobTooLarge(BlobStoreError):
    """The stream passed ``max_bytes``; nothing was kept.

    Deliberately **not** a :class:`StorageUnavailable`: an uploader that sends too
    much must not be told to retry the same request, and the deployment is
    perfectly healthy. ``put`` aborts the instant the running total crosses the
    limit, so an oversized upload costs one chunk of work - never a full transfer,
    and never a half-written object the database would have to reason about.
    """


@dataclass(frozen=True, slots=True)
class BlobDigest:
    """What was actually written: SHA-256 of the bytes, and how many there were.

    ``size`` is the number of bytes the store accepted, not the number the caller
    announced. A stream can end early or run long, and the database row must
    describe the object that exists (D6) - a declared size that disagreed with
    reality would make the two disagree forever.
    """

    #: Lowercase hexadecimal, :data:`SHA256_HEX_LENGTH` characters.
    sha256_hex: str
    size: int

    def __post_init__(self) -> None:
        """Refuse a digest that could not have come from SHA-256 of some bytes.

        Cheap and total: a 63-character hash reaching the database would violate the
        column's ``octet_length = 32`` CHECK sawtoothed across requests, so the
        adapter fails here instead of at COMMIT time.
        """
        if len(self.sha256_hex) != SHA256_HEX_LENGTH or any(
            char not in "0123456789abcdef" for char in self.sha256_hex
        ):
            raise ValueError("sha256_hex must be exactly 64 lowercase hex characters")
        if self.size < 0:
            raise ValueError("size must not be negative")


@dataclass(frozen=True, slots=True)
class BlobStat:
    """A stored object's key and byte size - metadata only, never its bytes."""

    key: str
    size: int


class BlobStore(Protocol):
    """The five operations the attachment service needs, and nothing else.

    Structural, not inherited: the adapter in :mod:`revnote_api.storage.s3` does
    not subclass this, it merely satisfies it, so a test double only has to spell
    the same methods. Every method is a coroutine (or an async iterator) because a
    store call is I/O that must never block the event loop; ``iter_chunks`` is a
    plain ``def`` returning an async iterator, which is how the interface can
    stream without buffering the object.
    """

    async def put(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        *,
        max_bytes: int,
        content_type: str,
    ) -> BlobDigest:
        """Store ``chunks`` under ``key``; return the digest of what was written.

        Two guarantees the caller relies on. ``max_bytes`` is enforced against the
        **running total** and the write is aborted the moment it is crossed
        (:class:`BlobTooLarge`), so an oversized upload is refused before it is
        finished rather than after. And the returned
        :class:`~revnote_api.storage.ports.BlobDigest` describes the bytes that
        reached the store, whatever the caller expected: a short stream yields a
        short ``size``, not an error.
        """
        ...

    async def digest(self, key: str) -> BlobDigest:
        """Re-read the object and recompute its digest (the D6 truth check).

        Implementations must read the bytes back and hash them. An ETag, a
        ``Content-Length`` or a cached stat are all cheaper and all useless here:
        they are the store's *claim* about the object, while the point of this call
        is an independent measurement of it.
        """
        ...

    async def stat(self, key: str) -> BlobStat | None:
        """Size of one object, or ``None`` when the key is absent.

        The only "absent is not an error" call. Everything that reads bytes raises
        instead (see :meth:`iter_chunks`), because there the caller cannot tell an
        absent object from an empty one.
        """
        ...

    def iter_chunks(self, key: str, *, chunk_size: int = 65536) -> AsyncIterator[bytes]:
        """Stream one object in bounded chunks; ``StorageUnavailable`` if absent.

        Memory is O(``chunk_size``), never O(object): the response is relayed to the
        client as it arrives, which is what keeps a 25 MiB download from becoming a
        25 MiB allocation per concurrent reader.
        """
        ...

    async def delete(self, key: str) -> None:
        """Remove ``key``. Idempotent: deleting an absent key is a success.

        Cleanup paths call this from a ``finally`` block and from a GC sweep, so it
        must be safe to repeat and must not turn "already gone" into an error that
        hides the failure that is actually being handled.
        """
        ...

    async def ready(self) -> bool:
        """Whether the store is usable right now - a probe, not an assertion.

        Answers ``False`` instead of raising, because a readiness probe that throws
        cannot distinguish "not ready yet" from "the probe itself is broken". The
        answer is deliberately shallow (the configured bucket is reachable): a deep
        probe would need write access on a health endpoint.
        """
        ...
