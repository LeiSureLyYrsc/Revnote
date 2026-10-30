"""The real-object-storage environment contract for the attachment suite.

**This module is the only place allowed to decide that real storage is unavailable.**
Nothing else - not a fixture, not a test module, not a CI script - may make that call.
The reason is the same one that produced ``pg_env``: a skip is indistinguishable from a
pass in a report, so a suite that quietly skips is a suite that proves nothing. Here the
decision is therefore *explicit and loud*: when ``REQUIRE_OBJECT_STORAGE_TESTS=1`` a
missing endpoint or bucket is a hard failure, and **skip is never an acceptable
outcome** of this module. Deselect the real-storage tests explicitly if you have no
store; do not let them evaporate.

The contract (identical in shape to ``REQUIRE_POSTGRES_TESTS``)
--------------------------------------------------------------
``REQUIRE_OBJECT_STORAGE_TESTS``
    ``1`` (truthy) => the four variables below are mandatory. Absent or malformed is a
    :class:`ObjectStorageRequirementError`, which the fixtures turn into ``pytest.fail``.
``OBJECT_STORAGE_ENDPOINT``
    ``http://127.0.0.1:9000`` or bare ``host:9000``.
``OBJECT_STORAGE_ACCESS_KEY`` / ``OBJECT_STORAGE_SECRET_KEY``
    Credentials for the store the test run owns. Local development reuses the compose
    root pair; CI uses a bucket-scoped user.
``OBJECT_STORAGE_BUCKET``
    Optional here. The suite always creates its **own** bucket (see
    :func:`unique_bucket_name`) so a test run can never touch a real bucket that merely
    happens to share the configured name - which is why this variable is *not* read.

Per-run isolation
-----------------
:func:`unique_bucket_name` produces a name that is legal for S3 and unique to this run,
so two concurrent CI jobs cannot see each other's objects. Cleanup is
:func:`purge_and_drop_bucket` in a ``finally``, deleting objects first and the bucket
second: S3 refuses to delete a non-empty bucket, so the order is the contract, not a
convenience.

Diagnostics
-----------
:func:`describe` renders ``host:port`` and the bucket name - never the credential
values, and never an object key. It is what a failed case should print, so a red CI run
says *which store* was involved without publishing an access key.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Final

__all__ = [
    "CI_ENV_VARS",
    "FALSY_VALUES",
    "OBJECT_STORAGE_ACCESS_KEY_ENV",
    "OBJECT_STORAGE_BUCKET_ENV",
    "OBJECT_STORAGE_ENDPOINT_ENV",
    "OBJECT_STORAGE_SECRET_KEY_ENV",
    "OBJECT_STORAGE_TIMEOUT_S_ENV",
    "REQUIRE_OBJECT_STORAGE_TESTS_ENV",
    "ObjectStorageRequirementError",
    "ObjectStorageTarget",
    "describe",
    "object_storage_is_required",
    "purge_and_drop_bucket",
    "require_object_storage",
    "unique_bucket_name",
]

#: Set to ``1`` to promise that a real store is present. Same contract as
#: ``REQUIRE_POSTGRES_TESTS``: absent means "a developer without storage", present means
#: "a missing store is a failure".
REQUIRE_OBJECT_STORAGE_TESTS_ENV: Final[str] = "REQUIRE_OBJECT_STORAGE_TESTS"

OBJECT_STORAGE_ENDPOINT_ENV: Final[str] = "OBJECT_STORAGE_ENDPOINT"
OBJECT_STORAGE_ACCESS_KEY_ENV: Final[str] = "OBJECT_STORAGE_ACCESS_KEY"
OBJECT_STORAGE_SECRET_KEY_ENV: Final[str] = "OBJECT_STORAGE_SECRET_KEY"
#: Not read by this module. Listed so the reader can see the deliberate omission:
#: the suite creates a per-run bucket instead of borrowing the configured one.
OBJECT_STORAGE_BUCKET_ENV: Final[str] = "OBJECT_STORAGE_BUCKET"
#: Optional override for the per-call deadline; defaults to the settings default.
OBJECT_STORAGE_TIMEOUT_S_ENV: Final[str] = "OBJECT_STORAGE_TIMEOUT_S"

#: A CI run must never silently pass because nothing was configured. Mirrors
#: ``pg_testing.CI_ENV_VARS`` so the two suites agree on what "this is CI" means.
CI_ENV_VARS: Final[tuple[str, ...]] = ("GITHUB_ACTIONS", "CI")

#: Values that mean "not set" (the same set ``pg_testing`` uses).
FALSY_VALUES: Final[frozenset[str]] = frozenset({"", "0", "false", "no", "off"})

#: Prefix of every bucket this module hands out. A recognisable prefix is what makes a
#: leftover bucket traceable to a test run, and what a cleanup job can sweep by.
BUCKET_NAME_PREFIX: Final[str] = "revnote-test"

#: Default per-call deadline when the environment does not set one.
DEFAULT_TIMEOUT_S: Final[float] = 10.0


class ObjectStorageRequirementError(RuntimeError):
    """Real object storage was promised and is not usable.

    A ``RuntimeError`` rather than a ``pytest`` failure so this module stays importable
    (and callable) outside a test run - an operator script can reuse
    :func:`require_object_storage` to fail fast with the same message. The fixtures
    translate it into ``pytest.fail``; nothing translates it into a skip.
    """


@dataclass(frozen=True, slots=True)
class ObjectStorageTarget:
    """A validated store to run against, plus the bucket this run owns.

    ``secret_key`` and ``access_key`` are ``repr=False``: this object gets printed next
    to a failing assertion, and a dataclass repr would put the credentials in the CI log
    the moment somebody adds ``assert target`` with a message. Use :meth:`describe` for
    anything that may be shown.
    """

    endpoint: str
    access_key: str
    secret_key: str
    bucket: str
    timeout_s: float = DEFAULT_TIMEOUT_S

    def describe(self) -> str:
        """``host:port`` and the run's bucket - the only printable form."""
        return f"endpoint={_host_port(self.endpoint)} bucket={self.bucket}"

    def __repr__(self) -> str:
        """Address and bucket only; the credentials are never rendered."""
        return f"ObjectStorageTarget({self.describe()}, timeout_s={self.timeout_s})"

    __str__ = __repr__


# --------------------------------------------------------------------------- #
# environment
# --------------------------------------------------------------------------- #


def object_storage_is_required() -> bool:
    """True when this run promised a real object store.

    Explicit ``REQUIRE_OBJECT_STORAGE_TESTS`` wins; otherwise a CI environment implies
    it, because a green CI run must never be the result of nobody configuring storage.
    """
    configured = os.environ.get(REQUIRE_OBJECT_STORAGE_TESTS_ENV, "").strip()
    if configured:
        return configured.lower() not in FALSY_VALUES
    return any(
        os.environ.get(name, "").strip().lower() not in FALSY_VALUES
        for name in CI_ENV_VARS
        if name in os.environ
    )


def _required(env_name: str) -> str:
    """Read one mandatory variable, or raise naming it."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        raise ObjectStorageRequirementError(
            f"{env_name} is not set. The attachment suite runs against real object "
            f"storage and never skips; deselect the real-storage tests explicitly if you "
            f"have no store. (Set {REQUIRE_OBJECT_STORAGE_TESTS_ENV}=1 only when one is "
            f"actually available.)"
        )
    return raw


def require_object_storage(*, bucket: str | None = None) -> ObjectStorageTarget:
    """Read, validate and de-collide the environment (raises, never skips).

    ``bucket`` overrides the generated per-run name; it exists so a caller that already
    created a bucket (a session fixture) can rebuild the target around it rather than
    generating a second name and silently testing somewhere else.

    The endpoint is validated through :class:`ObjectStorageSettings` so a malformed
    address fails *here*, with a message about the setting, rather than inside the SDK
    on the first upload. The credentials are only checked for presence: whether they
    actually work is a fact only the store can supply, and
    :func:`require_object_storage` refusing to guess is what keeps a wrong-credential
    run a real failure instead of a skip.
    """
    # Imported lazily: this module must stay importable with no SDK installed, so the
    # unit tier can exercise the environment contract on a machine without minio.
    from revnote_api.storage.settings import ObjectStorageSettings

    endpoint = _required(OBJECT_STORAGE_ENDPOINT_ENV)
    access_key = _required(OBJECT_STORAGE_ACCESS_KEY_ENV)
    secret_key = _required(OBJECT_STORAGE_SECRET_KEY_ENV)

    raw_timeout = os.environ.get(OBJECT_STORAGE_TIMEOUT_S_ENV, "").strip()
    try:
        timeout_s = float(raw_timeout) if raw_timeout else DEFAULT_TIMEOUT_S
    except ValueError as exc:
        raise ObjectStorageRequirementError(
            f"{OBJECT_STORAGE_TIMEOUT_S_ENV} must be a number of seconds, got a non-numeric value"
        ) from exc

    try:
        # Validation only: the bucket here is a placeholder, because the real name is
        # generated per run. A dummy legal name keeps the settings validator focused on
        # the endpoint and the credentials.
        settings = ObjectStorageSettings(
            _env_file=None,
            endpoint=endpoint,
            bucket=bucket or unique_bucket_name(),
            access_key=access_key,
            secret_key=secret_key,
            timeout_s=timeout_s,
        )
    except Exception as exc:
        raise ObjectStorageRequirementError(
            "the object-storage environment is not usable: "
            f"{OBJECT_STORAGE_ENDPOINT_ENV} or {OBJECT_STORAGE_TIMEOUT_S_ENV} is invalid"
        ) from exc

    return ObjectStorageTarget(
        endpoint=settings.endpoint,
        access_key=settings.access_key,
        secret_key=settings.secret_key,
        bucket=settings.bucket,
        timeout_s=settings.timeout_s,
    )


def unique_bucket_name(*, tag: str = "") -> str:
    """A legal, per-run bucket name: ``revnote-test-<tag>-<32 hex>``.

    Uniqueness comes from ``uuid4`` rather than a counter or a timestamp: two CI jobs
    starting in the same second must not collide, and a name that *looks* unique is not
    the same as one that is. ``tag`` is sanitised to the S3 character set (lowercase
    alphanumerics and hyphens) and truncated, so a test file name can be passed in
    without risking an illegal bucket.

    The result always satisfies the settings validator: lowercase, 3-63 characters,
    starting and ending with an alphanumeric.
    """
    suffix = uuid.uuid4().hex  # 32 chars, no hyphens
    cleaned = "".join(
        char if char.isalnum() and char.isascii() else "-" for char in tag.lower()
    ).strip("-")
    # 63 - len("revnote-test-") - len(suffix) - 2 separators leaves this much room.
    cleaned = cleaned[:20].strip("-")
    stem = f"{BUCKET_NAME_PREFIX}-{cleaned}" if cleaned else BUCKET_NAME_PREFIX
    return f"{stem}-{suffix}"[:63].rstrip("-")


# --------------------------------------------------------------------------- #
# diagnostics
# --------------------------------------------------------------------------- #


def describe(target_or_settings: object) -> str:
    """A printable one-liner: ``host:port`` and the bucket, never the credentials.

    Accepts an :class:`ObjectStorageTarget`, an
    :class:`~revnote_api.storage.settings.ObjectStorageSettings` or an
    :class:`~revnote_api.storage.s3.S3BlobStore` (through its ``settings``), because
    every one of those ends up in a failing assertion's message.
    """
    inner = getattr(target_or_settings, "settings", target_or_settings)
    owned = getattr(inner, "describe", None)
    if callable(owned):
        return str(owned())
    endpoint = getattr(inner, "endpoint", "")
    bucket = getattr(inner, "bucket", "")
    return f"endpoint={_host_port(str(endpoint))} bucket={bucket}"


def _host_port(endpoint: str) -> str:
    """``host:port`` from an endpoint with or without a scheme, credentials stripped.

    Deliberately not ``urllib.parse.urlsplit(...).netloc`` alone: a bare
    ``host:9000`` parses ``host`` as a *scheme*, so the scheme-less spelling has to be
    handled explicitly or the diagnostics would print an empty address.
    """
    from revnote_api.storage.settings import ObjectStorageSettings

    try:
        return ObjectStorageSettings(
            _env_file=None,
            endpoint=endpoint,
            bucket=unique_bucket_name(),
            access_key="diagnostics",
            secret_key="diagnostics",
        ).host_port
    except Exception:
        return "<unparsed endpoint>"


# --------------------------------------------------------------------------- #
# cleanup
# --------------------------------------------------------------------------- #


def purge_and_drop_bucket(store: object, bucket: str | None = None) -> None:
    """Delete every object in the bucket, then the bucket itself.

    Written for a ``finally``: it is called after a test has already failed, so it must
    report rather than raise past the original error - yet a silent failure would leave
    a bucket charging rent forever. The compromise is a ``RuntimeError`` only when the
    *whole* cleanup failed, and it is raised **after** attempting both steps, so one
    failure does not skip the other.

    Order matters and is the contract: S3 refuses to delete a non-empty bucket, so the
    objects go first. ``purge_prefix`` cannot express this (it requires a non-empty
    prefix, precisely so it can never empty a bucket by accident), which is why the
    bucket-wide delete lives here, in test support, behind a name that says what it
    does.
    """
    from minio import Minio
    from minio.deleteobjects import DeleteObject

    settings = getattr(store, "settings", store)
    client = getattr(store, "client", None)
    if client is None:
        client = Minio(
            settings.host_port,
            access_key=settings.access_key,
            secret_key=settings.secret_key,
            secure=settings.is_secure,
        )
    name = bucket or settings.bucket
    problems: list[str] = []

    try:
        listing = client.list_objects(name, recursive=True)
        try:
            keys = [item.object_name for item in listing if item.object_name]
        finally:
            closer = getattr(listing, "close", None)
            if callable(closer):
                closer()
        if keys:
            for _ in client.remove_objects(name, [DeleteObject(key) for key in keys]):
                # Drain: the deletions happen as this generator is consumed.
                pass
    except Exception as exc:
        problems.append(f"could not empty the bucket ({type(exc).__name__})")

    try:
        client.remove_bucket(name)
    except Exception as exc:
        problems.append(f"could not drop the bucket ({type(exc).__name__})")

    if problems:
        # Only the phase names and exception *classes*: an SDK message can quote a
        # bucket or a key, and this runs while another failure is unwinding.
        raise RuntimeError(
            f"object-storage cleanup was incomplete for {describe(settings)}: "
            + "; ".join(problems)
        )
