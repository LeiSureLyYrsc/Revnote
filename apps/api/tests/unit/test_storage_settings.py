"""``ObjectStorageSettings``: masking, validation, and the one-source rule.

Why these assertions and not more
---------------------------------
The interesting failure of a settings class is not "does the default parse". It is
**does a credential survive into a place people look**: a boot log, a traceback, a
``pytest`` assertion message. So most of this file is about rendering, and the
validation cases exist because a bad endpoint or bucket must fail at construction
rather than on the first upload.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from revnote_api import config as revnote_api_config
from revnote_api.storage import settings as storage_settings
from revnote_api.storage.settings import (
    MASKED_VALUE,
    ObjectStorageSettings,
    get_object_storage_settings,
)

#: Sentinels: strings that must never appear in a rendered settings object.
ACCESS_SENTINEL = "AKIA_ACCESS_SENTINEL_0001"
SECRET_SENTINEL = "sk-private-sentinel-do-not-log-9f3a"
EMBEDDED_SENTINEL = "userinfo-private-sentinel"


def _settings(**overrides: object) -> ObjectStorageSettings:
    """Settings built explicitly, ignoring any local ``.env`` on the machine."""
    base: dict[str, object] = {
        "endpoint": "http://127.0.0.1:9000",
        "bucket": "revnote-attachments",
        "access_key": ACCESS_SENTINEL,
        "secret_key": SECRET_SENTINEL,
    }
    base.update(overrides)
    return ObjectStorageSettings(_env_file=None, **base)


# --------------------------------------------------------------------------- #
# the credential never renders
# --------------------------------------------------------------------------- #


def test_repr_masks_both_credential_values() -> None:
    """``repr`` is what ends up in a log; the values must not survive it."""
    rendered = repr(_settings())

    assert ACCESS_SENTINEL not in rendered
    assert SECRET_SENTINEL not in rendered
    # Masking must not gut the line: the non-credential fields stay readable, or the
    # log is useless for the thing it exists for (which address was configured).
    assert "127.0.0.1:9000" in rendered
    assert "revnote-attachments" in rendered
    assert MASKED_VALUE in rendered


def test_str_is_the_masked_repr() -> None:
    """Most logging paths use ``%s``, and pydantic's default ``__str__`` is not ours."""
    settings = _settings()

    assert str(settings) == repr(settings)
    assert SECRET_SENTINEL not in str(settings)


def test_the_logging_idiom_does_not_leak_a_credential() -> None:
    """The actual call the service would make, not just ``repr``."""
    record = logging.LogRecord(
        name="revnote_api.storage",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="object storage configured: %s",
        args=(_settings(),),
        exc_info=None,
    )

    message = record.getMessage()
    assert SECRET_SENTINEL not in message
    assert ACCESS_SENTINEL not in message
    assert "127.0.0.1:9000" in message


def test_credentials_arriving_inside_the_endpoint_are_not_rendered() -> None:
    """A single templated URL carrying credentials is a real deployment shape.

    The masked endpoint is the same defence ``Settings.masked_database_url`` applies
    to a DSN password: it must not matter *which* field the credential arrived in.
    """
    settings = _settings(endpoint=f"http://revnote:{EMBEDDED_SENTINEL}@127.0.0.1:9000")

    assert EMBEDDED_SENTINEL not in repr(settings)
    assert EMBEDDED_SENTINEL not in str(settings)
    assert EMBEDDED_SENTINEL not in settings.diagnostics
    assert settings.host_port == "127.0.0.1:9000"


def test_the_field_metadata_also_hides_the_credentials() -> None:
    """``repr=False`` is the second belt: a pydantic-generated repr omits them."""
    fields = ObjectStorageSettings.model_fields

    assert fields["access_key"].repr is False
    assert fields["secret_key"].repr is False


def test_every_print_path_is_masked() -> None:
    """Not just ``repr``: every rendering a log call can reach through.

    ``repr``/``str`` are asserted above, but the call that actually leaks in practice
    is ``logger.info(f"{settings}")`` or ``"{}".format(settings)``, and a masking that
    only covers one of them is a masking somebody will route around by accident.
    """
    settings = _settings()

    for rendered in (
        f"{settings}",
        "{}".format(settings),  # noqa: UP032 - the point is this exact spelling
        "%s" % (settings,),  # noqa: UP031 - likewise
        settings.__repr__(),
        str(settings),
        repr(settings),
        settings.diagnostics,
    ):
        assert ACCESS_SENTINEL not in rendered
        assert SECRET_SENTINEL not in rendered


def test_a_structured_dump_is_explicitly_outside_the_guarantee() -> None:
    """The boundary is pinned, so it cannot drift into a false sense of safety.

    ``model_dump``/``model_dump_json`` are pydantic's serialisation surface and return
    raw values by design; ``repr=False`` does not reach them. That is exactly why this
    object must only ever be *printed*, never dumped into a log record - and asserting
    the limitation here means the module docstring's promise stays honest. If somebody
    later teaches this class to mask its dump too, this test fails and the docs get
    updated rather than quietly disagreeing with the code.
    """
    settings = _settings()

    dumped = settings.model_dump_json()

    assert SECRET_SENTINEL in dumped, "model_dump is expected to be raw; see the docstring"
    # Nothing in the package dumps it - the printable forms are the safe ones.
    assert SECRET_SENTINEL not in repr(settings)


def test_diagnostics_name_the_address_and_bucket_only() -> None:
    """What an operator pastes into an incident, so it must be safe by construction."""
    diagnostics = _settings(endpoint="https://minio.internal:9000").diagnostics

    assert "minio.internal:9000" in diagnostics
    assert "revnote-attachments" in diagnostics
    assert ACCESS_SENTINEL not in diagnostics
    assert SECRET_SENTINEL not in diagnostics


# --------------------------------------------------------------------------- #
# endpoint handling
# --------------------------------------------------------------------------- #


def test_a_trailing_slash_is_accepted_and_removed() -> None:
    """The spelling people copy out of a browser must not fail validation."""
    assert _settings(endpoint="http://127.0.0.1:9000/").endpoint == "http://127.0.0.1:9000"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:9000/bucket-path",
        "http://127.0.0.1:9000/?x=1",
        "http://127.0.0.1:9000/#frag",
        "ftp://127.0.0.1:9000",
        "",
        "   ",
    ],
)
def test_a_malformed_endpoint_is_refused(endpoint: str) -> None:
    """A path or query would silently become part of the bucket address."""
    with pytest.raises(ValidationError):
        _settings(endpoint=endpoint)


def test_a_bare_host_port_endpoint_is_accepted() -> None:
    """``MINIO_ENDPOINT``-style spellings (no scheme) are common and must work."""
    settings = _settings(endpoint="minio:9000")

    assert settings.host_port == "minio:9000"
    assert settings.is_secure is False


def test_an_https_endpoint_forces_tls_even_when_the_flag_is_false() -> None:
    """The harder-to-typo spelling wins: no input downgrades an ``https`` endpoint."""
    assert _settings(endpoint="https://minio.internal:9000", secure=False).is_secure is True


def test_the_secure_flag_applies_to_a_scheme_less_endpoint() -> None:
    """With nothing else to read the intent from, the flag decides."""
    assert _settings(endpoint="minio.internal:9000", secure=True).is_secure is True


def test_secure_is_off_by_default() -> None:
    """The local compose service speaks plaintext; a default of ``True`` would break it."""
    assert _settings().is_secure is False


# --------------------------------------------------------------------------- #
# bucket validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bucket", ["ab", "", "   ", "-leading", "trailing-", ".leading"])
def test_a_bucket_name_outside_the_s3_rules_is_refused(bucket: str) -> None:
    with pytest.raises(ValidationError):
        _settings(bucket=bucket)


@pytest.mark.parametrize(
    "bucket",
    ["UPPERCASE", "under_score", "has..dots", "mixed.-pair", "colonish:name"],
)
def test_a_bucket_name_with_illegal_characters_is_refused(bucket: str) -> None:
    """The SDK would reject these too, but only on the first upload, with a message
    that quotes the name back."""
    with pytest.raises(ValidationError):
        _settings(bucket=bucket)


def test_a_bucket_name_longer_than_the_s3_limit_is_refused() -> None:
    with pytest.raises(ValidationError):
        _settings(bucket="a" * 64)


@pytest.mark.parametrize(
    "bucket",
    ["abc", "revnote-attachments", "revnote-test-abc-0123456789", "a" * 63, "b1.c2-d3"],
)
def test_the_full_legal_bucket_range_is_accepted(bucket: str) -> None:
    """Including the 3- and 63-character boundaries, so the check is not off by one."""
    assert _settings(bucket=bucket).bucket == bucket


# --------------------------------------------------------------------------- #
# timeout validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("timeout_s", [0, 0.0, -1, -0.5])
def test_a_non_positive_timeout_is_refused(timeout_s: float) -> None:
    """A zero timeout is not "no timeout" - it is "always fail"."""
    with pytest.raises(ValidationError):
        _settings(timeout_s=timeout_s)


def test_an_absurd_timeout_is_refused() -> None:
    """An unbounded read timeout is the absence of a timeout, one typo away."""
    with pytest.raises(ValidationError):
        _settings(timeout_s=storage_settings.MAX_TIMEOUT_S + 1)


def test_the_default_timeout_is_ten_seconds() -> None:
    assert _settings().timeout_s == 10.0


# --------------------------------------------------------------------------- #
# one config source, unknown env tolerated
# --------------------------------------------------------------------------- #


def test_the_settings_reuse_the_app_local_env_file() -> None:
    """A second, separately resolved ``.env`` would be a second source of truth."""
    assert ObjectStorageSettings.model_config["env_file"] == revnote_api_config.ENV_FILES
    assert str(revnote_api_config.REPO_ROOT / ".env") not in revnote_api_config.ENV_FILES
    for candidate in ObjectStorageSettings.model_config["env_file"]:
        assert Path(candidate).is_absolute()


def test_unknown_environment_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process environment is shared with compose (``MINIO_*``, ``POSTGRES_*``)."""
    monkeypatch.setenv("MINIO_ROOT_USER", "revnote_minio_dev")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "irrelevant-here")
    monkeypatch.setenv("SOMETHING_ELSE", "1")

    assert _settings().bucket == "revnote-attachments"


def test_the_values_are_read_from_the_namespaced_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every field is ``OBJECT_STORAGE_``-prefixed, so nothing else can collide."""
    monkeypatch.setenv("OBJECT_STORAGE_ENDPOINT", "http://store.internal:9000")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "env-bucket")
    monkeypatch.setenv("OBJECT_STORAGE_ACCESS_KEY", "env-access")
    monkeypatch.setenv("OBJECT_STORAGE_SECRET_KEY", "env-private")
    monkeypatch.setenv("OBJECT_STORAGE_SECURE", "true")
    monkeypatch.setenv("OBJECT_STORAGE_TIMEOUT_S", "3.5")

    settings = ObjectStorageSettings(_env_file=None)

    assert settings.endpoint == "http://store.internal:9000"
    assert settings.bucket == "env-bucket"
    assert settings.access_key == "env-access"
    assert settings.secret_key == "env-private"
    assert settings.secure is True
    assert settings.timeout_s == 3.5


def test_a_missing_required_value_is_refused_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No defaults: a half-configured deployment must not start and fail on upload."""
    for name in (
        "OBJECT_STORAGE_ENDPOINT",
        "OBJECT_STORAGE_BUCKET",
        "OBJECT_STORAGE_ACCESS_KEY",
        "OBJECT_STORAGE_SECRET_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ValidationError):
        ObjectStorageSettings(_env_file=None)


def test_the_settings_singleton_is_cached_and_clearable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Matches ``config.get_settings``: one view per process, and re-readable.

    The cache is what makes "the adapter was built from the settings the process
    validated at boot" true; ``cache_clear`` is what lets a test change the environment
    without leaking a stale object into the next case.
    """
    monkeypatch.setenv("OBJECT_STORAGE_ENDPOINT", "http://127.0.0.1:9000")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "singleton-bucket")
    monkeypatch.setenv("OBJECT_STORAGE_ACCESS_KEY", "access")
    monkeypatch.setenv("OBJECT_STORAGE_SECRET_KEY", SECRET_SENTINEL)
    get_object_storage_settings.cache_clear()
    try:
        first = get_object_storage_settings()

        assert get_object_storage_settings() is first
        assert first.bucket == "singleton-bucket"
        assert SECRET_SENTINEL not in repr(first)

        monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "changed-bucket")
        assert get_object_storage_settings() is first  # cached, not re-read

        get_object_storage_settings.cache_clear()
        assert get_object_storage_settings().bucket == "changed-bucket"
    finally:
        get_object_storage_settings.cache_clear()
