"""``storage/bootstrap.py``: bucket creation and prefix purge, without touching a store.

The two properties that matter are both about what these helpers may *not* do:
``purge_prefix`` must refuse an empty prefix (an empty prefix is the whole bucket), and
``ensure_bucket`` must be idempotent (a fixture reruns unconditionally). Everything else
here checks that a failure is reported rather than swallowed - these run from a
``finally`` and from an operator's shell, where silence is the worst outcome.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from revnote_api.storage import bootstrap
from revnote_api.storage.bootstrap import (
    BucketBootstrapError,
    ensure_bucket,
    purge_prefix,
)
from revnote_api.storage.ports import StorageUnavailable
from revnote_api.storage.settings import ObjectStorageSettings

BUCKET = "revnote-attachments"
OBJECT_PREFIX = "ws/00000000-0000-0000-0000-000000000001/att/"


class FakeListing:
    """The SDK's listing handle: an iterator of items with a ``close``."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = list(keys)
        self.closed = 0

    def __iter__(self):
        for key in self._keys:
            yield type("Item", (), {"object_name": key})()

    def close(self) -> None:
        self.closed += 1


class FakeDeleteError:
    def __init__(self, code: str = "AccessDenied") -> None:
        self.code = code


class FakeMinio:
    """Bucket + bulk-delete surface used by the bootstrap helpers."""

    def __init__(
        self,
        *,
        objects: dict[str, bytes] | None = None,
        buckets: set[str] | None = None,
        fail_delete_for: set[str] | None = None,
        exists_error: BaseException | None = None,
        make_error: BaseException | None = None,
        list_error: BaseException | None = None,
    ) -> None:
        self.objects = dict(objects or {})
        self.buckets = set(buckets if buckets is not None else {BUCKET})
        self.fail_delete_for = set(fail_delete_for or ())
        self.exists_error = exists_error
        self.make_error = make_error
        self.list_error = list_error
        self.created: list[str] = []
        self.listings: list[tuple[str, str | None, bool]] = []
        self.delete_batches: list[list[str]] = []
        self.last_listing: FakeListing | None = None

    def bucket_exists(self, bucket_name: str) -> bool:
        if self.exists_error is not None:
            raise self.exists_error
        return bucket_name in self.buckets

    def make_bucket(self, bucket_name: str) -> None:
        if self.make_error is not None:
            raise self.make_error
        self.created.append(bucket_name)
        self.buckets.add(bucket_name)

    def list_objects(self, bucket_name: str, prefix: str = "", recursive: bool = False):
        if self.list_error is not None:
            raise self.list_error
        self.listings.append((bucket_name, prefix, recursive))
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        self.last_listing = FakeListing(keys)
        return self.last_listing

    def remove_objects(self, bucket_name: str, delete_list):
        keys = [item._object_name for item in delete_list]
        self.delete_batches.append(keys)
        for key in keys:
            if key in self.fail_delete_for:
                yield FakeDeleteError()
                continue
            self.objects.pop(key, None)


class FakeDeleteObject:
    """Stand-in for ``minio.deleteobjects.DeleteObject`` (built inside the helper)."""

    def __init__(self, name: str) -> None:
        self._object_name = name


@pytest.fixture(autouse=True)
def _patch_delete_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the real ``DeleteObject`` so no SDK object is constructed.

    ``bootstrap`` imports it lazily from ``minio.deleteobjects`` precisely so this
    module can run without the SDK's delete-object model; the patch mirrors the one
    attribute the helper reads.
    """
    import minio.deleteobjects as deleteobjects

    monkeypatch.setattr(deleteobjects, "DeleteObject", FakeDeleteObject, raising=True)


def _settings() -> ObjectStorageSettings:
    return ObjectStorageSettings(
        _env_file=None,
        endpoint="http://127.0.0.1:9000",
        bucket=BUCKET,
        access_key="access-sentinel",
        secret_key="private-sentinel",
    )


class _Store:
    """The narrow shape ``bootstrap`` accepts: ``.client`` plus ``.settings``."""

    def __init__(self, client: FakeMinio, settings: ObjectStorageSettings) -> None:
        self.client = client
        self.settings = settings


# --------------------------------------------------------------------------- #
# import purity
# --------------------------------------------------------------------------- #


def test_the_module_has_no_import_time_side_effects() -> None:
    """No client, no bucket, no environment read: importing must do nothing.

    Checked structurally: the module's top level may import, define and assign only.
    A ``Minio(...)`` call or an ``os.environ`` read at module scope would turn a plain
    import into a connection attempt.
    """
    tree = ast.parse(Path(bootstrap.__file__).read_text(encoding="utf-8"))

    allowed = (
        ast.Import,
        ast.ImportFrom,
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Assign,
        ast.AnnAssign,
        ast.Expr,  # the docstring
        ast.If,  # ``if TYPE_CHECKING:``
    )
    for node in tree.body:
        assert isinstance(node, allowed), f"unexpected module-level {type(node).__name__}"


def test_no_http_surface_is_declared() -> None:
    """Ops-only by construction: nothing here can be mounted on a router.

    Asserted on *imports and decorators*, not on the file's prose. A substring scan
    would fail on a docstring that merely names the thing it is promising not to use -
    which is exactly how an assertion starts policing wording instead of behaviour.
    """
    tree = ast.parse(Path(bootstrap.__file__).read_text(encoding="utf-8"))

    imported: set[str] = set()
    decorated: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorated.update(ast.unparse(decorator) for decorator in node.decorator_list)

    assert not imported & {"fastapi", "starlette"}, f"HTTP framework leaked in: {imported}"
    assert not {d for d in decorated if "router" in d.lower()}, decorated


# --------------------------------------------------------------------------- #
# ensure_bucket
# --------------------------------------------------------------------------- #


def test_ensure_bucket_creates_a_missing_bucket() -> None:
    client = FakeMinio(buckets=set())
    store = _Store(client, _settings())

    assert ensure_bucket(store) == BUCKET  # type: ignore[arg-type]
    assert client.created == [BUCKET]


def test_ensure_bucket_is_idempotent() -> None:
    """A session fixture reruns unconditionally; a second call must be a no-op."""
    client = FakeMinio(buckets={BUCKET})
    store = _Store(client, _settings())

    ensure_bucket(store)  # type: ignore[arg-type]
    ensure_bucket(store)  # type: ignore[arg-type]

    assert client.created == []


def test_ensure_bucket_attaches_no_policy() -> None:
    """Created private: running a bootstrap step must not make anything readable."""
    client = FakeMinio(buckets=set())

    ensure_bucket(_Store(client, _settings()))  # type: ignore[arg-type]

    assert not hasattr(client, "set_bucket_policy"), (
        "the helper must not attach a policy; a deployment that needs one sets it itself"
    )


def test_ensure_bucket_reports_a_rejected_creation() -> None:
    client = FakeMinio(buckets=set(), make_error=ValueError("invalid bucket name"))
    store = _Store(client, _settings())

    with pytest.raises(BucketBootstrapError) as caught:
        ensure_bucket(store)  # type: ignore[arg-type]

    assert "OBJECT_STORAGE_BUCKET" in str(caught.value)
    assert "private-sentinel" not in str(caught.value)
    assert caught.value.__cause__ is not None


def test_ensure_bucket_propagates_a_store_outage_as_a_port_error() -> None:
    """Unreachable is a port failure, not a bootstrap one: an operator cares."""
    client = FakeMinio(exists_error=StorageUnavailable("down"))
    store = _Store(client, _settings())

    with pytest.raises(StorageUnavailable):
        ensure_bucket(store)  # type: ignore[arg-type]


def test_ensure_bucket_refuses_an_argument_it_cannot_use() -> None:
    with pytest.raises(TypeError, match="ObjectStorageSettings"):
        ensure_bucket(object())  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# purge_prefix
# --------------------------------------------------------------------------- #


def test_purge_prefix_removes_only_the_requested_prefix() -> None:
    """Prefix-scoped by contract: a neighbouring attachment must survive."""
    client = FakeMinio(
        objects={
            f"{OBJECT_PREFIX}one/nonce": b"a",
            f"{OBJECT_PREFIX}two/nonce": b"b",
            "ws/00000000-0000-0000-0000-000000000009/att/other/nonce": b"keep",
        }
    )

    removed = purge_prefix(_Store(client, _settings()), OBJECT_PREFIX)  # type: ignore[arg-type]

    assert removed == 2
    assert list(client.objects) == ["ws/00000000-0000-0000-0000-000000000009/att/other/nonce"]
    assert client.listings[0][1] == OBJECT_PREFIX


def test_purge_prefix_refuses_an_empty_prefix() -> None:
    """An empty prefix is "the whole bucket" - the one thing this must never do."""
    client = FakeMinio(objects={"keep": b"x"})

    with pytest.raises(ValueError, match="prefix"):
        purge_prefix(_Store(client, _settings()), "")  # type: ignore[arg-type]

    assert client.delete_batches == []
    assert client.listings == []


def test_purge_prefix_on_an_empty_prefix_is_a_no_op() -> None:
    """Idempotent, so a cleanup can rerun over a prefix it already emptied."""
    client = FakeMinio(objects={"elsewhere/key": b"x"})

    assert purge_prefix(_Store(client, _settings()), OBJECT_PREFIX) == 0  # type: ignore[arg-type]
    assert client.objects == {"elsewhere/key": b"x"}


def test_purge_prefix_batches_a_large_purge() -> None:
    """One oversized bulk request would make the entire purge all-or-nothing."""
    objects = {f"{OBJECT_PREFIX}{index}/nonce": b"x" for index in range(1050)}
    client = FakeMinio(objects=objects)

    removed = purge_prefix(
        _Store(client, _settings()),  # type: ignore[arg-type]
        OBJECT_PREFIX,
        batch_size=500,
    )

    assert removed == 1050
    assert [len(batch) for batch in client.delete_batches] == [500, 500, 50]


def test_purge_prefix_closes_the_listing() -> None:
    """An abandoned listing leaks its HTTP response; the helper closes it."""
    client = FakeMinio(objects={f"{OBJECT_PREFIX}a/nonce": b"x"})

    purge_prefix(_Store(client, _settings()), OBJECT_PREFIX)  # type: ignore[arg-type]

    assert client.last_listing is not None
    assert client.last_listing.closed == 1


def test_purge_prefix_reports_a_partial_failure_without_naming_the_object() -> None:
    """A per-object ``DeleteError`` echoes the key, which must not enter the message."""
    failing = f"{OBJECT_PREFIX}{'0' * 12}/nonce"
    client = FakeMinio(
        objects={failing: b"x", f"{OBJECT_PREFIX}b/nonce": b"y"},
        fail_delete_for={failing},
    )

    with pytest.raises(BucketBootstrapError) as caught:
        purge_prefix(_Store(client, _settings()), OBJECT_PREFIX)  # type: ignore[arg-type]

    message = str(caught.value)
    assert "could not delete" in message
    assert failing not in message
    assert "private-sentinel" not in message


def test_purge_prefix_propagates_a_store_outage_as_a_port_error() -> None:
    client = FakeMinio(list_error=StorageUnavailable("down"))

    with pytest.raises(StorageUnavailable):
        purge_prefix(_Store(client, _settings()), OBJECT_PREFIX)  # type: ignore[arg-type]


def test_purge_prefix_reports_an_unexpected_failure_with_a_fixed_message() -> None:
    """The SDK's own text can quote a bucket; only the phase is reported."""
    client = FakeMinio(list_error=RuntimeError(f"weird failure on {BUCKET}"))
    client.list_objects = lambda *a, **k: (_ for _ in ()).throw(RuntimeError(f"on {BUCKET}"))  # type: ignore[assignment]

    with pytest.raises(BucketBootstrapError) as caught:
        purge_prefix(_Store(client, _settings()), OBJECT_PREFIX)  # type: ignore[arg-type]

    assert caught.value.__cause__ is not None
    assert "private-sentinel" not in str(caught.value)


def test_purge_prefix_refuses_a_nonsensical_batch_size() -> None:
    client = FakeMinio(objects={f"{OBJECT_PREFIX}a/nonce": b"x"})

    with pytest.raises(ValueError, match="batch_size"):
        purge_prefix(_Store(client, _settings()), OBJECT_PREFIX, batch_size=0)  # type: ignore[arg-type]
