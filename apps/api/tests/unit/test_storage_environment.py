"""The real-storage environment contract: mandatory-when-promised, and never a skip.

These tests are the reason ``pg_support/object_storage.py`` can be trusted to gate the
attachment suite. The contract it encodes is short and severe:

* ``REQUIRE_OBJECT_STORAGE_TESTS=1`` and a missing variable is a **failure**;
* **skip is never an acceptable outcome** - a skipped storage test is indistinguishable
  from a passing one in a report, so the module that decides "storage is unavailable"
  raises instead;
* diagnostics print ``host:port`` and the bucket, never a credential or a key;
* cleanup deletes objects *then* the bucket, in that order, because S3 refuses to delete
  a non-empty bucket.

The tests run without a store: every decision here is about the environment, so a
``monkeypatch`` is the correct instrument. The real round trips live behind the
contract, not in this file.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from pg_support import object_storage
from pg_support.object_storage import (
    BUCKET_NAME_PREFIX,
    DEFAULT_TIMEOUT_S,
    ObjectStorageRequirementError,
    describe,
    object_storage_is_required,
    purge_and_drop_bucket,
    require_object_storage,
    unique_bucket_name,
)

#: Sentinel credentials: they must not survive into any printable form.
ACCESS_SENTINEL = "AKIA_ACCESS_SENTINEL_0001"
SECRET_SENTINEL = "sk-private-sentinel-do-not-log-9f3a"

#: Every variable this module reads, so a test can clear all of them.
ENV_VARS = (
    "REQUIRE_OBJECT_STORAGE_TESTS",
    "OBJECT_STORAGE_ENDPOINT",
    "OBJECT_STORAGE_ACCESS_KEY",
    "OBJECT_STORAGE_SECRET_KEY",
    "OBJECT_STORAGE_TIMEOUT_S",
    "CI",
    "GITHUB_ACTIONS",
)

#: What a legal S3 bucket looks like (the same rules the settings validator applies).
BUCKET_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Start every case from an empty contract, so nothing inherits from the machine."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _configure(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    """Set the four mandatory variables, with optional overrides."""
    values = {
        "OBJECT_STORAGE_ENDPOINT": "http://127.0.0.1:9000",
        "OBJECT_STORAGE_ACCESS_KEY": ACCESS_SENTINEL,
        "OBJECT_STORAGE_SECRET_KEY": SECRET_SENTINEL,
    }
    values.update(overrides)
    for name, value in values.items():
        monkeypatch.setenv(name, value)


# --------------------------------------------------------------------------- #
# the module states its own contract
# --------------------------------------------------------------------------- #


def test_the_docstring_names_this_module_as_the_only_decider() -> None:
    """Stated in prose *and* pinned here, so the sentence cannot quietly disappear."""
    docstring = object_storage.__doc__ or ""

    assert "only place allowed to decide" in docstring
    assert "skip is never an acceptable" in docstring


def test_the_module_never_calls_pytest_skip() -> None:
    """Structural, not textual: no ``pytest.skip``/``skip`` call may exist in it."""
    tree = ast.parse(Path(object_storage.__file__).read_text(encoding="utf-8"))

    skipped: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            rendered = ast.unparse(node.func)
            if rendered.endswith("skip") or rendered.endswith("skipif"):
                skipped.append(rendered)

    assert skipped == [], f"this module must never skip; found: {skipped}"


def test_the_module_does_not_import_pytest() -> None:
    """Importable outside a test run, so an operator script can reuse the contract."""
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(Path(object_storage.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(ast.parse(Path(object_storage.__file__).read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert "pytest" not in imported


# --------------------------------------------------------------------------- #
# REQUIRE_OBJECT_STORAGE_TESTS gating
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_a_truthy_requirement_is_honoured(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REQUIRE_OBJECT_STORAGE_TESTS", value)

    assert object_storage_is_required() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_a_falsy_requirement_is_honoured(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("REQUIRE_OBJECT_STORAGE_TESTS", value)

    assert object_storage_is_required() is False


@pytest.mark.parametrize("ci_var", ["CI", "GITHUB_ACTIONS"])
def test_a_ci_run_implies_the_requirement(monkeypatch: pytest.MonkeyPatch, ci_var: str) -> None:
    """A green CI run must never be the result of nobody configuring storage."""
    monkeypatch.setenv(ci_var, "true")

    assert object_storage_is_required() is True


def test_an_explicit_zero_wins_over_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    """The explicit switch is the contract; CI only supplies a default for it."""
    monkeypatch.setenv("CI", "true")
    monkeypatch.setenv("REQUIRE_OBJECT_STORAGE_TESTS", "0")

    assert object_storage_is_required() is False


def test_an_unconfigured_environment_is_not_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing set at all: a developer without a store is not making a promise."""
    for name in ("REQUIRE_OBJECT_STORAGE_TESTS", "CI", "GITHUB_ACTIONS"):
        monkeypatch.delenv(name, raising=False)

    assert object_storage_is_required() is False


# --------------------------------------------------------------------------- #
# a promised store that is absent is a hard failure
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "missing",
    [
        "OBJECT_STORAGE_ENDPOINT",
        "OBJECT_STORAGE_ACCESS_KEY",
        "OBJECT_STORAGE_SECRET_KEY",
    ],
)
def test_a_missing_variable_raises_and_names_itself(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """``pytest.fail`` is the caller's job; this module raises, and never skips."""
    _configure(monkeypatch)
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(ObjectStorageRequirementError) as caught:
        require_object_storage()

    message = str(caught.value)
    assert missing in message
    assert "never skips" in message


def test_a_blank_value_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Whitespace is what an unset shell variable expands to; treating it as set lies."""
    _configure(monkeypatch, OBJECT_STORAGE_ENDPOINT="   ")

    with pytest.raises(ObjectStorageRequirementError, match="OBJECT_STORAGE_ENDPOINT"):
        require_object_storage()


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1:9000/path", "ftp://store:9000", "http://store:9000/?q=1"],
)
def test_a_malformed_endpoint_raises_rather_than_proceeding(
    monkeypatch: pytest.MonkeyPatch, endpoint: str
) -> None:
    """Failing here, with a message about the setting, beats failing inside the SDK."""
    _configure(monkeypatch, OBJECT_STORAGE_ENDPOINT=endpoint)

    with pytest.raises(ObjectStorageRequirementError, match="not usable"):
        require_object_storage()


def test_a_non_numeric_timeout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, OBJECT_STORAGE_TIMEOUT_S="soon")

    with pytest.raises(ObjectStorageRequirementError, match="OBJECT_STORAGE_TIMEOUT_S"):
        require_object_storage()


# --------------------------------------------------------------------------- #
# a configured environment becomes a validated target
# --------------------------------------------------------------------------- #


def test_a_complete_environment_yields_a_target(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch)

    target = require_object_storage()

    assert target.endpoint == "http://127.0.0.1:9000"
    assert target.access_key == ACCESS_SENTINEL
    assert target.secret_key == SECRET_SENTINEL
    assert target.timeout_s == DEFAULT_TIMEOUT_S
    assert BUCKET_NAME_PATTERN.match(target.bucket), target.bucket


def test_the_timeout_override_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure(monkeypatch, OBJECT_STORAGE_TIMEOUT_S="2.5")

    assert require_object_storage().timeout_s == 2.5


def test_a_caller_supplied_bucket_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A session fixture that already created a bucket must not get a second name."""
    _configure(monkeypatch)

    target = require_object_storage(bucket="revnote-test-adopted")

    assert target.bucket == "revnote-test-adopted"


def test_the_configured_bucket_is_deliberately_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-run bucket is created instead, so a real bucket can never be emptied."""
    _configure(monkeypatch, OBJECT_STORAGE_BUCKET="a-production-bucket-name")

    target = require_object_storage()

    assert target.bucket != "a-production-bucket-name"
    assert target.bucket.startswith(BUCKET_NAME_PREFIX)


# --------------------------------------------------------------------------- #
# per-run isolation
# --------------------------------------------------------------------------- #


def test_unique_bucket_names_are_legal_and_distinct() -> None:
    """Two CI jobs starting in the same second must not collide."""
    names = {unique_bucket_name() for _ in range(200)}

    assert len(names) == 200
    for name in names:
        assert BUCKET_NAME_PATTERN.match(name), name
        assert len(name) <= 63, name
        assert name.startswith(BUCKET_NAME_PREFIX), name


def test_the_bucket_name_is_stable_only_within_one_call() -> None:
    """It is a *generator*, not a cached property: every call means a new run."""
    assert unique_bucket_name() != unique_bucket_name()


@pytest.mark.parametrize(
    "tag",
    ["Test_File.py", "a b c", "UPPER", "x" * 200, "", "---", "emoji-\N{ROCKET}"],
)
def test_a_tag_is_sanitised_into_a_legal_bucket_name(tag: str) -> None:
    """A test module path is the natural tag, and it is full of illegal characters."""
    name = unique_bucket_name(tag=tag)

    assert BUCKET_NAME_PATTERN.match(name), name
    assert len(name) <= 63, name
    assert name.endswith(name.split("-")[-1])


def test_a_long_tag_still_produces_a_legal_name() -> None:
    name = unique_bucket_name(tag="very-long-test-module-name-attachment-concurrency")

    assert len(name) <= 63
    assert BUCKET_NAME_PATTERN.match(name)


# --------------------------------------------------------------------------- #
# diagnostics: address and bucket, never a credential
# --------------------------------------------------------------------------- #


def test_the_target_repr_masks_both_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing assertion prints its argument, and repr is what an ``assert`` shows."""
    _configure(monkeypatch)
    target = require_object_storage()

    for rendered in (repr(target), str(target), target.describe(), describe(target)):
        assert SECRET_SENTINEL not in rendered
        assert ACCESS_SENTINEL not in rendered
        assert "127.0.0.1:9000" in rendered
        assert target.bucket in rendered


def test_diagnostics_render_host_and_port_for_a_scheme_less_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``host:9000`` parses ``host`` as a scheme; the printer must not print nothing."""
    _configure(monkeypatch, OBJECT_STORAGE_ENDPOINT="store.internal:9000")

    assert "store.internal:9000" in describe(require_object_storage())


def test_diagnostics_never_raise_on_a_broken_endpoint() -> None:
    """It runs while another failure is unwinding; it must not add a second one."""

    class Broken:
        endpoint = ":///not-an-endpoint"
        bucket = "b"

    assert describe(Broken()) == "endpoint=<unparsed endpoint> bucket=b"


def test_describe_accepts_settings_and_a_store_shaped_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every one of these ends up in a message, so all shapes must be printable."""
    from revnote_api.storage.settings import ObjectStorageSettings

    settings = ObjectStorageSettings(
        _env_file=None,
        endpoint="http://127.0.0.1:9000",
        bucket="revnote-test-x",
        access_key=ACCESS_SENTINEL,
        secret_key=SECRET_SENTINEL,
    )

    class StoreShaped:
        def __init__(self) -> None:
            self.settings = settings

    for subject in (settings, StoreShaped()):
        rendered = describe(subject)
        assert SECRET_SENTINEL not in rendered
        assert ACCESS_SENTINEL not in rendered
        assert "revnote-test-x" in rendered


# --------------------------------------------------------------------------- #
# cleanup: objects first, bucket second
# --------------------------------------------------------------------------- #


class FakeListing:
    def __init__(self, keys: list[str]) -> None:
        self._keys = list(keys)
        self.closed = 0

    def __iter__(self):
        for key in self._keys:
            yield type("Item", (), {"object_name": key})()

    def close(self) -> None:
        self.closed += 1


class FakeCleanupClient:
    """Records the order of the two steps, which is the contract."""

    def __init__(
        self,
        keys: list[str],
        *,
        remove_error: BaseException | None = None,
        bucket_error: BaseException | None = None,
    ) -> None:
        self.keys = list(keys)
        self.remove_error = remove_error
        self.bucket_error = bucket_error
        self.calls: list[str] = []
        self.listing: FakeListing | None = None

    def list_objects(self, bucket_name: str, prefix: str = "", recursive: bool = False):
        self.calls.append("list")
        self.listing = FakeListing(self.keys)
        return self.listing

    def remove_objects(self, bucket_name: str, delete_list):
        self.calls.append("remove_objects")
        if self.remove_error is not None:
            raise self.remove_error
        for _ in delete_list:
            yield

    def remove_bucket(self, bucket_name: str) -> None:
        self.calls.append("remove_bucket")
        if self.bucket_error is not None:
            raise self.bucket_error


class _Store:
    def __init__(self, client: FakeCleanupClient, settings: object) -> None:
        self.client = client
        self.settings = settings


@pytest.fixture(autouse=True)
def _patch_delete_object(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``DeleteObject`` so the fake client receives a plain string carrier."""
    import minio.deleteobjects as deleteobjects

    monkeypatch.setattr(deleteobjects, "DeleteObject", _FakeDeleteObject, raising=True)


class _FakeDeleteObject:
    def __init__(self, name: str) -> None:
        self.name = name


def _settings() -> object:
    from revnote_api.storage.settings import ObjectStorageSettings

    return ObjectStorageSettings(
        _env_file=None,
        endpoint="http://127.0.0.1:9000",
        bucket="revnote-test-cleanup",
        access_key=ACCESS_SENTINEL,
        secret_key=SECRET_SENTINEL,
    )


def test_cleanup_deletes_the_objects_before_dropping_the_bucket() -> None:
    """S3 refuses to delete a non-empty bucket, so the order *is* the contract."""
    client = FakeCleanupClient(["a/1", "a/2"])

    purge_and_drop_bucket(_Store(client, _settings()))

    assert client.calls == ["list", "remove_objects", "remove_bucket"]
    assert client.listing is not None
    assert client.listing.closed == 1


def test_cleanup_drops_an_already_empty_bucket() -> None:
    client = FakeCleanupClient([])

    purge_and_drop_bucket(_Store(client, _settings()))

    assert client.calls == ["list", "remove_bucket"]


def test_cleanup_still_tries_the_bucket_when_emptying_failed() -> None:
    """One failure must not skip the other step, or a bucket leaks forever."""
    client = FakeCleanupClient(["a/1"], remove_error=RuntimeError("store said no"))

    with pytest.raises(RuntimeError, match="could not empty the bucket") as caught:
        purge_and_drop_bucket(_Store(client, _settings()))

    assert client.calls == ["list", "remove_objects", "remove_bucket"]
    assert SECRET_SENTINEL not in str(caught.value)


def test_cleanup_reports_a_bucket_that_could_not_be_dropped() -> None:
    client = FakeCleanupClient(["a/1"], bucket_error=RuntimeError("bucket not empty"))

    with pytest.raises(RuntimeError, match="could not drop the bucket") as caught:
        purge_and_drop_bucket(_Store(client, _settings()))

    message = str(caught.value)
    assert "revnote-test-cleanup" in message
    assert SECRET_SENTINEL not in message
    assert ACCESS_SENTINEL not in message


def test_cleanup_reports_both_failures_together() -> None:
    """A partial report would hide the second problem behind the first one."""
    client = FakeCleanupClient(
        ["a/1"],
        remove_error=RuntimeError("no"),
        bucket_error=RuntimeError("also no"),
    )

    with pytest.raises(RuntimeError) as caught:
        purge_and_drop_bucket(_Store(client, _settings()))

    message = str(caught.value)
    assert "could not empty the bucket" in message
    assert "could not drop the bucket" in message


def test_cleanup_never_prints_an_object_key() -> None:
    """A key names an attachment; a cleanup message must not."""
    key = "ws/00000000-0000-0000-0000-000000000001/att/00000000-0000-0000-0000-000000000002/nonce"
    client = FakeCleanupClient([key], remove_error=RuntimeError(f"failed on {key}"))

    with pytest.raises(RuntimeError) as caught:
        purge_and_drop_bucket(_Store(client, _settings()))

    assert key not in str(caught.value)
