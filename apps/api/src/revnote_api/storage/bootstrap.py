"""Ops/test-only helpers: create the bucket, empty a prefix. Never served over HTTP.

Why this is a module and not a route
------------------------------------
``ensure_bucket`` creates a bucket and ``purge_prefix`` destroys objects in bulk.
Both are exactly the operations a remote caller must never reach: one changes the
deployment's topology, the other is an unbounded delete. Keeping them here - no
FastAPI import, no router, nothing that could be mounted by accident - means the only
way to run them is to import this module from a script, a test, or a shell, which is
the audience they are written for.

Importable without side effects
-------------------------------
Nothing runs at import: no client is constructed, no environment is read, no bucket is
touched. A module-level ``Minio(...)`` would make merely importing the package attempt
a connection and pull credentials into a module global, which is both surprising and a
needless failure mode for the unit suite.

Idempotent by construction
--------------------------
``ensure_bucket`` on an existing bucket is a no-op and ``purge_prefix`` over an empty
prefix removes nothing, so both are safe to call unconditionally from a fixture or a
repeatable operator command. Neither swallows a real failure: a bucket that cannot be
created, or an object that cannot be deleted, raises.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Final

from revnote_api.storage.ports import BlobStoreError

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from revnote_api.storage.s3 import S3BlobStore
    from revnote_api.storage.settings import ObjectStorageSettings

__all__ = [
    "PURGE_BATCH_SIZE",
    "BucketBootstrapError",
    "ensure_bucket",
    "probe_store",
    "purge_prefix",
]

#: Objects removed per ``remove_objects`` call. The S3 bulk-delete API accepts up to
#: 1000 keys; staying well under it keeps one oversized request from turning a large
#: purge into a single all-or-nothing failure.
PURGE_BATCH_SIZE: Final[int] = 500


class BucketBootstrapError(RuntimeError):
    """The bucket could not be created, or an object could not be deleted.

    A plain ``RuntimeError`` subclass rather than a port exception: this is an operator
    signal ("the deployment is not set up"), not something a request path should catch
    and translate into a client-visible code.
    """


def ensure_bucket(store_or_settings: S3BlobStore | ObjectStorageSettings) -> str:
    """Create the configured bucket if absent; return its name.

    Synchronous on purpose. Its only callers are a one-shot operator command and a
    session-scoped test fixture, both of which run *outside* any request's event loop -
    and a bootstrap step that can be driven from a plain ``python -m`` invocation is
    worth more than an ``async`` spelling nobody awaits.

    The bucket is created **private**: no policy is attached here, so nothing becomes
    anonymously readable by running this. A deployment that needs a different policy
    sets it deliberately, outside this module.

    Raises :class:`BucketBootstrapError` when the store rejects the operation, and
    :class:`~revnote_api.storage.ports.StorageUnavailable` (a ``BlobStoreError``) when
    it cannot be reached: an operator cares about the difference, and neither is
    swallowed.
    """
    client, settings = _resolve(store_or_settings)
    try:
        if not client.bucket_exists(settings.bucket):
            client.make_bucket(settings.bucket)
    except BlobStoreError:
        raise
    except Exception as exc:
        # ``from exc`` keeps the S3 code reachable for a debugger while the message
        # names only the *setting* and the address - never a credential or a key.
        raise BucketBootstrapError(
            "could not create the configured OBJECT_STORAGE_BUCKET "
            f"(endpoint={settings.masked_endpoint})"
        ) from exc
    return settings.bucket


def probe_store(store_or_settings: S3BlobStore | ObjectStorageSettings) -> bool:
    """Whether the store answers at all - **independent of any bucket**.

    ``BlobStore.ready()`` is ``bucket_exists``: it answers "is *my* bucket there",
    which is the right question for a serving process and the wrong one for a
    bootstrap waiter, because the bucket this run needs is created *after* the
    store becomes reachable. Polling ``ready()`` there waits for something only
    the next step can produce (this deadlocked the E2E runner's owned mode for a
    full deadline against a perfectly healthy server).

    ``list_buckets`` is the smallest call that proves reachability, credentials
    and SDK wiring together, and it needs no bucket to exist. A store that refuses
    ``list_buckets`` but would accept a scoped ``put`` is not a deployment this
    project supports, so treating it as "not ready" is correct rather than lossy.

    Answers ``False`` instead of raising so a bounded retry loop can poll it.
    """
    client, _settings = _resolve(store_or_settings)
    try:
        list(client.list_buckets())
    except Exception:
        # Deliberately broad: "not reachable yet" spans transport errors, auth
        # rejections during startup and SDK mapping quirks. The caller's deadline
        # is what decides, and it re-raises its own fixed message.
        return False
    return True


def purge_prefix(
    store_or_settings: S3BlobStore | ObjectStorageSettings,
    prefix: str,
    *,
    batch_size: int = PURGE_BATCH_SIZE,
) -> int:
    """Delete every object under ``prefix``; return how many were removed.

    The test-support and operator reset primitive. Deliberately **prefix-scoped rather
    than bucket-wide**: the real callers are a per-run test bucket and one
    ``ws/{id}/att/{id}/`` tree, and a helper that can empty a whole bucket is one
    careless argument away from deleting production data.

    ``prefix`` must be non-empty. That is not politeness: the SDK reads ``""`` as "the
    entire bucket", so an empty prefix would silently be the operation this function
    exists to avoid offering.

    Raises :class:`~revnote_api.storage.ports.StorageUnavailable` when the store cannot
    be reached, and :class:`BucketBootstrapError` when any object could not be deleted -
    a partial purge is reported as a failure, never as success.
    """
    if not prefix:
        raise ValueError("prefix must not be empty; an empty prefix would empty the bucket")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")

    client, settings = _resolve(store_or_settings)
    deleted = 0
    try:
        for batch in _batched(_iter_keys(client, settings.bucket, prefix), batch_size):
            removed, failed = _remove_batch(client, settings.bucket, batch)
            if failed:
                # A per-object DeleteError echoes the object name, which is precisely
                # the value that must not enter an exception message. Only the count is
                # reported; the details stay in the SDK for a local debugger.
                raise BucketBootstrapError(
                    f"could not delete {failed} of {len(batch)} object(s) under the "
                    f"requested prefix (endpoint={settings.masked_endpoint})"
                )
            deleted += removed
    except BlobStoreError:
        raise
    except BucketBootstrapError:
        raise
    except Exception as exc:
        raise BucketBootstrapError(
            "could not purge the requested prefix "
            f"(endpoint={settings.masked_endpoint}, bucket={settings.bucket})"
        ) from exc
    return deleted


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #


def _resolve(
    store_or_settings: S3BlobStore | ObjectStorageSettings,
) -> tuple[Any, ObjectStorageSettings]:
    """Split the one argument into ``(client, settings)``.

    Two input shapes are accepted, both structurally rather than by exact class:

    * an :class:`~revnote_api.storage.settings.ObjectStorageSettings`, from which an
      adapter (and therefore a client) is built - the ops command's case, where the
      bucket may not exist yet so there is nothing else to hand in;
    * anything exposing ``client`` and ``settings`` - an ``S3BlobStore``, and also a
      test double, because requiring the concrete class here would force every
      bootstrap test to construct a real SDK client just to assert on bucket logic.

    ``AttributeError`` is not raised for a wrong argument: a missing attribute is
    exactly what makes this informative, so the check is explicit and names the two
    shapes the function accepts.
    """
    from revnote_api.storage.s3 import S3BlobStore
    from revnote_api.storage.settings import ObjectStorageSettings

    if isinstance(store_or_settings, ObjectStorageSettings):
        settings = store_or_settings
        return S3BlobStore(settings).client, settings

    client = getattr(store_or_settings, "client", None)
    settings = getattr(store_or_settings, "settings", None)
    if client is None or not isinstance(settings, ObjectStorageSettings):
        raise TypeError(
            "expected ObjectStorageSettings or an S3BlobStore-like object exposing "
            f"client and settings, got {type(store_or_settings).__name__}"
        )
    return client, settings


def _iter_keys(client: Any, bucket: str, prefix: str) -> Iterator[str]:
    """Every key under ``prefix``, as a flat iterator.

    ``recursive=True`` because object keys have no hierarchy: a non-recursive listing
    would filter by a synthetic ``/`` delimiter and silently miss the deeper segments
    of a ``ws/{id}/att/{id}/{nonce}`` key. The listing handle is closed in a
    ``finally`` so an abandoned purge does not leak its HTTP response.
    """
    listing = client.list_objects(bucket, prefix=prefix, recursive=True)
    try:
        for item in listing:
            key = getattr(item, "object_name", None)
            if isinstance(key, str) and key:
                yield key
    finally:
        closer = getattr(listing, "close", None)
        if callable(closer):
            closer()


def _batched(keys: Iterator[str], size: int) -> Iterator[list[str]]:
    """Group a flat iterator into lists of at most ``size`` items."""
    batch: list[str] = []
    for key in keys:
        batch.append(key)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def _remove_batch(client: Any, bucket: str, keys: list[str]) -> tuple[int, int]:
    """Delete one batch; return ``(removed, failed)`` counts.

    The SDK's ``remove_objects`` is a generator that yields one error per object it
    could not delete, so it must be drained to completion - the deletions happen as it
    is consumed, and abandoning it early leaves objects in place while reporting
    success.
    """
    from minio.deleteobjects import DeleteObject

    failed = sum(1 for _ in client.remove_objects(bucket, [DeleteObject(k) for k in keys]))
    return len(keys) - failed, failed
