"""Object-storage connection settings, with the credential value never rendered.

Why this is a separate settings class
------------------------------------
``config.Settings`` describes the *application*: budgets, protocol numbers, the
database DSN. This class describes one **connection to one bucket**, and mixing the
two would mean every test that wants a settings object also needs a bucket. More
importantly, D11 keeps every credential out of ``Settings``: its ``repr`` is logged
at boot, and its field list is asserted to contain no credential-shaped name. So
the credential pair lives here, in a class whose ``repr`` masks it, and ``Settings``
stays exactly as it was.

What "never rendered" covers
----------------------------
Every path a *print* takes: ``repr``, ``str``, an f-string, ``%s``, ``str.format``
and therefore ``logger.info("%s", settings)``. It deliberately does not cover
``model_dump``/``model_dump_json`` - pydantic's serialisation surface, which hands
back raw field values by contract - so this class is safe to print and must never be
fed to a structured-dump log line. The distinction is pinned by
``test_storage_settings.py``, which asserts both halves.

One config source
-----------------
``env_file`` reuses :data:`revnote_api.config.ENV_FILES`, i.e. the app-local
``apps/api/.env`` that the rest of the service already reads. A second, separately
resolved ``.env`` would be a second source of truth that silently disagrees with
the first as soon as somebody edits only one of them; the repository-root ``.env``
stays owned by compose and is passed in explicitly with ``uv run --env-file .env``.

Fail at construction, not at first upload
----------------------------------------
``endpoint``, ``bucket``, ``access_key`` and ``secret_key`` have **no defaults**, so
a deployment cannot start half-configured and discover it when a user uploads a
file. A deployment that serves no attachments never constructs this class at all
(``ATTACHMENTS_ENABLED=false``), which is why requiring all four costs nothing.
"""

from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import SplitResult, urlsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from revnote_api.config import ENV_FILES

__all__ = [
    "BUCKET_NAME_MAX_CHARS",
    "BUCKET_NAME_MIN_CHARS",
    "MASKED_VALUE",
    "MAX_TIMEOUT_S",
    "ObjectStorageSettings",
    "get_object_storage_settings",
]

#: Every env var this class reads is namespaced: ``endpoint`` -> ``OBJECT_STORAGE_ENDPOINT``.
#: A bare ``ENDPOINT`` or ``BUCKET`` in a shared process environment would be far too
#: easy to collide with something else the deployment sets.
ENV_PREFIX: str = "OBJECT_STORAGE_"

#: What a masked field renders as. A constant, so a reader can tell "masked" from
#: "unset" and no prefix of the real value can survive.
MASKED_VALUE: str = "***"

#: S3 bucket naming bounds (3-63 characters, RFC 1123-ish).
BUCKET_NAME_MIN_CHARS: int = 3
BUCKET_NAME_MAX_CHARS: int = 63

#: Ceiling for one socket timeout. Not a tuning knob: an unbounded read timeout is
#: the *absence* of a timeout, and one typed extra zero (``100`` for ``10``) would
#: turn every storage call into a hang. Ten minutes is already generous for one
#: connect/read against a 25 MiB object.
MAX_TIMEOUT_S: float = 600.0

#: Lowercase alphanumeric at both ends, only ``a-z 0-9 . -`` inside.
_BUCKET_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]*[a-z0-9]$")

#: Pairs an S3 implementation rejects: a double dot reads as an empty label, and the
#: mixed pairs read as a stray separator.
_BUCKET_NAME_FORBIDDEN_PAIRS: tuple[str, ...] = ("..", ".-", "-.")

#: Fields whose *value* must never be rendered. ``access_key`` is included even
#: though it is nominally an identifier: together with the endpoint and the bucket
#: it is enough to attempt a login, and this object is exactly what lands in a boot
#: log or a pasted traceback.
_MASKED_FIELDS: frozenset[str] = frozenset({"access_key", "secret_key"})


def _split_endpoint(value: str) -> SplitResult:
    """Parse an endpoint that may or may not carry a scheme.

    ``urlsplit('host:9000')`` reads ``host`` as a *scheme* and ``9000`` as a path,
    which would make the very common bare spelling look like a malformed URL. A
    scheme-less value is therefore parsed as a bare authority (``//host:9000``), so
    both spellings end up with the same ``netloc`` and the same "no path, no query"
    checks.
    """
    return urlsplit(value if "://" in value else f"//{value}")


class ObjectStorageSettings(BaseSettings):
    """How to reach the attachment bucket.

    ``OBJECT_STORAGE_ENDPOINT`` may be written with a scheme (``http://host:9000``)
    or bare (``host:9000``); the scheme is stripped before the value reaches the SDK,
    which wants host and port only and takes TLS as a separate flag. See
    :attr:`is_secure` for how the two spellings combine.
    """

    model_config = SettingsConfigDict(
        # Same app-local ``.env`` as ``config.Settings`` - never a second file.
        env_file=ENV_FILES,
        env_file_encoding="utf-8",
        env_prefix=ENV_PREFIX,
        case_sensitive=False,
        # The process environment is shared with compose, which also exports
        # ``MINIO_*`` and ``POSTGRES_*`` keys this class has no field for.
        extra="ignore",
        populate_by_name=True,
    )

    # -- address ------------------------------------------------------------ #
    endpoint: str = Field(description="OBJECT_STORAGE_ENDPOINT; the scheme is optional.")
    bucket: str = Field(
        description="OBJECT_STORAGE_BUCKET; a private bucket the API alone may reach."
    )

    # -- credentials -------------------------------------------------------- #
    #: Values masked by :meth:`__repr__`. ``repr=False`` removes them from a
    #: *pydantic-generated* repr as well, so the masking survives even if somebody
    #: deletes :meth:`__repr__`. It does **not** affect ``model_dump``/
    #: ``model_dump_json``, which return the raw values - this object is therefore
    #: never a candidate for a structured-dump log line, only for ``repr``/``str``
    #: (see :func:`revnote_api.storage.settings.__doc__` and the masking test matrix).
    access_key: str = Field(repr=False, description="OBJECT_STORAGE_ACCESS_KEY.")
    secret_key: str = Field(repr=False, description="OBJECT_STORAGE_SECRET_KEY.")

    # -- transport ---------------------------------------------------------- #
    #: TLS to the object store. Only consulted when ``endpoint`` carries no scheme;
    #: an explicit ``https://`` endpoint always wins (see :attr:`is_secure`).
    secure: bool = False

    #: Per-socket connect/read budget, in seconds. Deliberately **one** number: a
    #: connect timeout shorter than the read timeout would make a merely slow store
    #: look unreachable, and two knobs means one of them is always misconfigured.
    timeout_s: float = Field(default=10.0, gt=0.0, le=MAX_TIMEOUT_S)

    # -- validation --------------------------------------------------------- #

    @field_validator("endpoint")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        """Require a host, and nothing but a host (no path, query or fragment).

        The SDK builds every request URL from this value, so a path would silently
        become part of the bucket address. A trailing slash is accepted and removed
        because that is the spelling people copy out of a browser.
        """
        raw = value.strip().rstrip("/")
        if not raw:
            raise ValueError("OBJECT_STORAGE_ENDPOINT must not be empty")

        parts = _split_endpoint(raw)
        if parts.scheme and parts.scheme not in {"http", "https"}:
            raise ValueError(
                f"OBJECT_STORAGE_ENDPOINT scheme must be http or https, got {parts.scheme!r}"
            )
        if parts.query or parts.fragment:
            raise ValueError("OBJECT_STORAGE_ENDPOINT must not carry a query or a fragment")
        if parts.path:
            raise ValueError("OBJECT_STORAGE_ENDPOINT must not carry a path")
        if not parts.hostname:
            raise ValueError("OBJECT_STORAGE_ENDPOINT must include a host")
        return raw

    @field_validator("bucket")
    @classmethod
    def _validate_bucket(cls, value: str) -> str:
        """Enforce the S3 naming rules the bucket will be created under.

        Checked here rather than left to the SDK for two reasons: a bad name must
        fail at boot (not on the first upload), and the error has to name the *rule*
        - the SDK's message quotes the rejected name, which is fine for us but is
        exactly the kind of value a caller might forward into a response.
        """
        name = value.strip()
        if not name:
            raise ValueError("OBJECT_STORAGE_BUCKET must not be empty")
        if not BUCKET_NAME_MIN_CHARS <= len(name) <= BUCKET_NAME_MAX_CHARS:
            raise ValueError(
                f"OBJECT_STORAGE_BUCKET must be {BUCKET_NAME_MIN_CHARS}-"
                f"{BUCKET_NAME_MAX_CHARS} characters long"
            )
        if not _BUCKET_NAME_PATTERN.match(name):
            raise ValueError(
                "OBJECT_STORAGE_BUCKET must be lowercase letters, digits, dots and "
                "hyphens, and must start and end with a letter or a digit"
            )
        if any(pair in name for pair in _BUCKET_NAME_FORBIDDEN_PAIRS):
            raise ValueError(
                "OBJECT_STORAGE_BUCKET must not contain '..', '.-' or '-.' (the SDK "
                "rejects them, and they make an empty label look like a real one)"
            )
        return name

    # -- derived views ------------------------------------------------------ #

    @property
    def is_secure(self) -> bool:
        """Whether to speak TLS.

        An explicit ``https://`` endpoint wins over a false ``secure`` flag, because
        the more specific, harder-to-typo spelling is the one the operator meant. The
        flag then covers the bare ``host:port`` spelling, where there is nothing else
        to read the intent from. The combination never *downgrades*: no input makes
        an ``https`` endpoint connect in plaintext.
        """
        return _split_endpoint(self.endpoint).scheme == "https" or self.secure

    @property
    def host_port(self) -> str:
        """``host:port`` exactly as the SDK receives it - userinfo removed.

        Some deployments template a single URL that carries credentials; they are
        stripped here, so the SDK (and every log line derived from this object) only
        ever sees an address. The explicit ``access_key``/``secret_key`` fields are
        the only credentials this class uses.
        """
        return _split_endpoint(self.endpoint).netloc.rsplit("@", 1)[-1]

    @property
    def masked_endpoint(self) -> str:
        """The endpoint with any embedded credentials removed - safe to print."""
        scheme = _split_endpoint(self.endpoint).scheme
        return f"{scheme}://{self.host_port}" if scheme else self.host_port

    @property
    def diagnostics(self) -> str:
        """One line an operator can paste into an incident: address and bucket only.

        Never the credential values, and never the object layout. This is the summary
        the real-storage test support reports and what a failed ``ready()`` probe
        logs.
        """
        return f"endpoint={self.masked_endpoint} bucket={self.bucket} secure={self.is_secure}"

    # -- rendering ---------------------------------------------------------- #

    def __repr__(self) -> str:
        """Field list with every credential value masked.

        pydantic's generated ``repr`` prints ``access_key`` and ``secret_key``
        verbatim, and a settings object is exactly the thing people log at boot
        ("effective storage configuration"). Masking here is the same defence
        ``config.Settings`` applies to its DSN password: an accidental
        ``logger.info(settings)`` cannot leak a credential.

        ``endpoint`` is rendered through :attr:`masked_endpoint` for the same reason
        the DSN is rendered through ``hide_password``: a single templated URL that
        carries credentials is a common deployment shape, and it must not survive
        into a log line just because it arrived in a different field.

        Scope of the guarantee: **every** rendering an f-string, ``%s``, ``str()``,
        ``format()`` or a logging call can produce. It does not extend to
        ``model_dump``/``model_dump_json``, which are pydantic's serialisation surface
        and hand back the raw values by design - so this object is safe to *print* and
        unsafe to *dump*, and nothing in this package dumps it.
        """
        rendered = {name: getattr(self, name) for name in type(self).model_fields}
        rendered["endpoint"] = self.masked_endpoint
        rendered.update(dict.fromkeys(_MASKED_FIELDS, MASKED_VALUE))
        fields = ", ".join(f"{name}={rendered[name]!r}" for name in type(self).model_fields)
        return f"ObjectStorageSettings({fields})"

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_object_storage_settings() -> ObjectStorageSettings:
    """The process-wide storage settings singleton.

    Cached so the adapter is built from one consistent view per process, matching
    :func:`revnote_api.config.get_settings` so the two cannot disagree about which
    ``.env`` was read. Tests call ``cache_clear()`` after mutating the environment,
    exactly as they do for the application settings.
    """
    return ObjectStorageSettings()
