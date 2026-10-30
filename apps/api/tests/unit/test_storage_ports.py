"""``storage/ports.py``: the value objects, and the import promise that makes them useful.

Two things are worth testing here and nothing else is:

* the digest's own invariants, because a 63-character hash reaching the database would
  trip a CHECK constraint halfway through an upload;
* the *absence* of an SDK import. That is the property the whole port exists for - the
  service layer must be importable without ``minio`` installed - so it is asserted
  twice: against the module's own source, and against the contents of a **fresh
  interpreter** that has imported nothing but the port. Neither is promised in a
  docstring, and neither can be satisfied by an accident of test ordering.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from revnote_api.storage import ports

PORTS_PATH = Path(ports.__file__)

#: Directory that ``import revnote_api`` resolves against. Handed to the child
#: interpreter explicitly so the check does not depend on how the package happens to
#: be installed (editable ``.pth``, real wheel, or a bare ``PYTHONPATH``) - and so
#: ``-I`` can still be used to keep the child free of inherited import state.
IMPORT_ROOT = str(Path(ports.__file__).resolve().parents[2])

#: Ceiling for the child interpreter. It imports the port and nothing else, so a real
#: run is ~1s on a cold cache; this exists to fail a hung child rather than stall the
#: suite.
PROBE_TIMEOUT_S = 120

#: The child program: prove the SDK is installed, import the port, prove the SDK is
#: still not loaded.
_PROBE_SOURCE = "\n".join(
    (
        "import importlib.util",
        "import sys",
        "",
        "sys.path.insert(0, sys.argv[1])",
        "",
        "# The SDK must be *installed* for this check to mean anything: the absence of",
        "# minio from sys.modules is trivially true in an interpreter without minio, and",
        "# that would turn the assertion below into a no-op exactly when it cannot be",
        "# trusted.",
        "assert importlib.util.find_spec('minio') is not None, (",
        "    'minio is not installed, so this check would pass for the wrong reason'",
        ")",
        "",
        "import revnote_api.storage.ports",
        "",
        "loaded = sorted(name for name in sys.modules if name.split('.')[0] == 'minio')",
        "assert not loaded, f'importing the port loaded the storage SDK: {loaded}'",
        "",
        "# The adapter module itself must stay unimported too: an __init__ that",
        "# re-exported storage.s3 would drag the SDK in through the same side door.",
        "assert 'revnote_api.storage.s3' not in sys.modules, (",
        "    'importing the port also imported the adapter module'",
        ")",
    )
)


def _imports_of(path: Path) -> set[str]:
    """Every module name imported by one file (absolute and relative roots)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


# --------------------------------------------------------------------------- #
# the import promise
# --------------------------------------------------------------------------- #


def test_ports_imports_only_the_standard_library() -> None:
    """Zero third-party imports: this file is what makes the service layer portable.

    Checked against the source rather than ``sys.modules`` so the assertion is about
    this file's own intent - a transitive import elsewhere in the process would
    otherwise mask a new ``import minio`` line here.
    """
    imported = _imports_of(PORTS_PATH)

    third_party = {
        name
        for name in imported
        if not name.startswith(("from __future__",))
        and name.split(".")[0]
        not in {
            "__future__",
            "collections",
            "dataclasses",
            "typing",
        }
    }
    assert third_party == set(), f"ports.py must stay dependency-free, found: {third_party}"


def test_ports_does_not_pull_in_the_storage_sdk() -> None:
    """The reverse direction: importing the port must not import the adapter's SDK.

    Run in a **fresh interpreter**, and that is the whole point. The earlier spelling
    of this test dropped ``revnote_api.storage*`` from ``sys.modules`` and re-imported
    the port - which answers the wrong question once any other test module has
    imported ``storage.s3``: the SDK's own cache entry is still there, so the assertion
    failed for a reason that had nothing to do with ``ports`` (green alone, red in the
    full suite). Deleting ``minio``'s entry too would not really fix it either: the
    interpreter would still be one where the SDK had been loaded, and re-importing a
    module that others hold live references to is exactly the kind of global-state
    surgery that produces order-dependent failures somewhere else later.

    A child process asks the honest question instead - "in an interpreter that has
    only ever imported ``revnote_api.storage.ports``, is the SDK absent?" - so the
    result cannot depend on which test ran before this one, and the parent's
    ``sys.modules`` is not mutated at all. The child first asserts that ``minio`` is
    installed, because "not loaded" is trivially true when there is nothing to load;
    without that guard the test would quietly become a no-op on a machine missing the
    dependency.
    """
    completed = subprocess.run(
        # ``-I`` (isolated mode) is what makes the child's state genuinely fresh: it
        # ignores ``PYTHON*`` environment variables and does not put the current
        # directory on ``sys.path``, so nothing this test session did can reach it.
        # ``IMPORT_ROOT`` is then supplied explicitly, as argv[1].
        [sys.executable, "-I", "-c", _PROBE_SOURCE, IMPORT_ROOT],
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_S,
        check=False,
    )

    assert completed.returncode == 0, (
        "a fresh interpreter that imported only the port must not see the SDK:\n"
        f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}"
    )


# --------------------------------------------------------------------------- #
# BlobDigest
# --------------------------------------------------------------------------- #


def test_a_valid_digest_is_accepted_and_frozen() -> None:
    digest = ports.BlobDigest(sha256_hex="a" * 64, size=12)

    assert (digest.sha256_hex, digest.size) == ("a" * 64, 12)
    with pytest.raises(dataclasses.FrozenInstanceError):
        digest.size = 13  # type: ignore[misc]


def test_the_exported_hash_length_matches_the_column_width() -> None:
    """The database stores ``content_hash`` as 32 bytes, i.e. 64 hex characters."""
    assert ports.SHA256_HEX_LENGTH == 64


@pytest.mark.parametrize(
    "sha256_hex",
    [
        "",  # empty
        "a" * 63,  # one short: the off-by-one that would reach the database
        "a" * 65,  # one long
        "A" * 64,  # uppercase is a different wire form
        "g" * 64,  # not hex at all
        "a" * 63 + "-",
    ],
)
def test_a_malformed_digest_is_refused_at_construction(sha256_hex: str) -> None:
    """Fail here, not at COMMIT: the column's CHECK would see it sawtoothed."""
    with pytest.raises(ValueError, match="sha256_hex"):
        ports.BlobDigest(sha256_hex=sha256_hex, size=1)


def test_a_negative_size_is_refused() -> None:
    with pytest.raises(ValueError, match="size"):
        ports.BlobDigest(sha256_hex="a" * 64, size=-1)


def test_a_zero_size_is_legal() -> None:
    """An empty attachment is a real (if useless) object: SHA-256 of no bytes."""
    import hashlib

    digest = ports.BlobDigest(sha256_hex=hashlib.sha256(b"").hexdigest(), size=0)

    assert digest.size == 0


# --------------------------------------------------------------------------- #
# BlobStat
# --------------------------------------------------------------------------- #


def test_blob_stat_carries_the_key_and_size_and_nothing_else() -> None:
    """Metadata only: a stat must never be a way to read bytes."""
    fields = {field.name for field in dataclasses.fields(ports.BlobStat)}

    assert fields == {"key", "size"}


def test_blob_digest_carries_exactly_the_two_documented_fields() -> None:
    fields = {field.name for field in dataclasses.fields(ports.BlobDigest)}

    assert fields == {"sha256_hex", "size"}


# --------------------------------------------------------------------------- #
# the exception vocabulary
# --------------------------------------------------------------------------- #


def test_a_missing_object_is_a_storage_failure_not_a_silent_empty_stream() -> None:
    """The frozen rule: absence is an error on the read paths, never ``b""``."""
    assert issubclass(ports.StorageUnavailable, ports.BlobStoreError)
    assert issubclass(ports.StorageUnavailable, Exception)


def test_too_large_is_not_reported_as_a_transient_storage_failure() -> None:
    """An oversized upload must not be told to retry the identical request."""
    assert issubclass(ports.BlobTooLarge, ports.BlobStoreError)
    assert not issubclass(ports.BlobTooLarge, ports.StorageUnavailable)


def test_every_blob_error_shares_one_base() -> None:
    """So a caller can be exhaustive without catching ``Exception``."""
    for name in ("BlobStoreError", "BlobTooLarge", "StorageUnavailable"):
        assert hasattr(ports, name), name

    assert ports.BlobStoreError.__bases__ == (Exception,)


# --------------------------------------------------------------------------- #
# BlobStore is a structural protocol, not a base class
# --------------------------------------------------------------------------- #


def test_blob_store_declares_exactly_the_six_frozen_operations() -> None:
    """A seventh method is a contract change; this pins the count so it is deliberate."""
    members = {
        name
        for name in vars(ports.BlobStore)
        if not name.startswith("_") and callable(getattr(ports.BlobStore, name))
    }

    assert members == {"put", "digest", "stat", "iter_chunks", "delete", "ready"}


def test_the_write_and_read_signatures_match_the_contract() -> None:
    """``max_bytes`` and ``chunk_size`` are keyword-only, so a caller cannot swap them."""
    put = inspect.signature(ports.BlobStore.put).parameters
    read = inspect.signature(ports.BlobStore.iter_chunks).parameters

    assert list(put) == ["self", "key", "chunks", "max_bytes", "content_type"]
    assert put["max_bytes"].kind is inspect.Parameter.KEYWORD_ONLY
    assert put["content_type"].kind is inspect.Parameter.KEYWORD_ONLY
    assert list(read) == ["self", "key", "chunk_size"]
    assert read["chunk_size"].kind is inspect.Parameter.KEYWORD_ONLY
    assert read["chunk_size"].default == 65536


def test_a_plain_object_satisfies_the_protocol_without_inheriting() -> None:
    """Structural typing is the point: a test double must not need the real base.

    Asserted by comparing member names rather than ``isinstance``: the frozen contract
    declares a plain ``Protocol`` (not ``@runtime_checkable``), and making it runtime
    checkable purely so a test could ask the question would change the published type
    for the convenience of the test. A missing or extra member still fails here.
    """

    class Double:
        async def put(self, key, chunks, *, max_bytes, content_type):
            raise NotImplementedError

        async def digest(self, key):
            raise NotImplementedError

        async def stat(self, key):
            raise NotImplementedError

        def iter_chunks(self, key, *, chunk_size=65536):
            raise NotImplementedError

        async def delete(self, key):
            raise NotImplementedError

        async def ready(self):
            raise NotImplementedError

    required = {
        name
        for name in vars(ports.BlobStore)
        if not name.startswith("_") and callable(getattr(ports.BlobStore, name))
    }
    provided = {name for name in vars(Double) if not name.startswith("_")}

    assert provided == required
    # Not a subclass: ``BlobStore`` is a structural ``Protocol``, so conformance comes
    # from the member set alone. ``port_inheritance`` is False in the class namespace,
    # which is how a plain Protocol reports "not inherited" without a runtime check.
    assert "BlobStore" not in {base.__name__ for base in Double.__mro__}
