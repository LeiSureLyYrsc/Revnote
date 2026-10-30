"""``revnote_api.storage`` — the object-storage adapter, behind a pure port.

The package exists to keep one arrow honest: ``services -> storage.ports``, never
``services -> storage.s3``. ``ports`` holds the frozen vocabulary (two value objects, a
``Protocol`` and the exception types) and imports nothing third-party, so the service
layer can be imported and unit-tested without an object-store SDK in the picture.

``s3`` is the only module that imports the S3 SDK, and it is the only place that knows
a bucket, an endpoint or a credential exists. ``settings`` reads the connection
configuration and masks its credential values in ``repr``; ``bootstrap`` holds the
create-bucket and purge-prefix helpers, which are for operators and tests and are
never mounted on a router.

Importing this package is side-effect free: no client is built, no environment is
read and no connection is attempted. The names below are re-exported for convenience
only - importing ``revnote_api.storage`` does **not** pull in the SDK.
"""

from __future__ import annotations

from revnote_api.storage.ports import (
    SHA256_HEX_LENGTH,
    BlobDigest,
    BlobStat,
    BlobStore,
    BlobStoreError,
    BlobTooLarge,
    StorageUnavailable,
)

__all__ = [
    "SHA256_HEX_LENGTH",
    "BlobDigest",
    "BlobStat",
    "BlobStore",
    "BlobStoreError",
    "BlobTooLarge",
    "StorageUnavailable",
]
