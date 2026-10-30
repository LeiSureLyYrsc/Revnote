"""Application settings (pydantic-settings).

Scaffold rules baked into this module:

* **``APP_ENV`` is the contract name** for the runtime environment. The legacy
  ``ENVIRONMENT`` spelling is still accepted as a fallback alias so existing
  deployments keep working, but ``APP_ENV`` wins when both are present. When
  neither is set the value defaults to ``local``.
* **Unknown environment variables are ignored** (``extra="ignore"``). The repo
  root ``.env`` is shared with docker-compose and carries ``POSTGRES_*`` /
  ``RUSTFS_*`` keys this service must tolerate.
* **PostgreSQL via asyncpg is the only supported backend.** Any other driver
  (``sqlite``, ``postgresql+psycopg``, bare ``postgresql://``) is rejected at
  validation time, so a misconfigured deployment fails fast at boot.
* **Every field has a local-dev friendly default**, therefore ``create_app()``
  boots with no ``.env`` and no running database. ``GET /`` and ``GET /health``
  are pure process liveness and never touch the database.
* **One config source.** Only the app-local ``apps/api/.env`` is read
  implicitly; the repository-root ``.env`` stays owned by compose / the root
  docs and is passed in explicitly (``uv run --env-file .env ...``), so there is
  no second, silently-diverging copy of the root configuration.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

#: The single driver accepted by this service (PostgreSQL only, async).
ASYNC_PG_DRIVER = "postgresql+asyncpg"

#: Local-development default DSN. Matches ``docker-compose`` postgres defaults
#: so ``docker compose up`` + ``uv run uvicorn ...`` works out of the box.
DEFAULT_DATABASE_URL = "postgresql+asyncpg://revnote:revnote@127.0.0.1:5432/revnote"

#: Env var names for the runtime environment, highest precedence first.
#: ``APP_ENV`` is the documented contract; ``ENVIRONMENT`` is a compat alias.
APP_ENV_VARS: tuple[str, ...] = ("APP_ENV", "ENVIRONMENT")

# apps/api/src/revnote_api/config.py -> parents: 0=pkg 1=src 2=api 3=apps 4=root
_PACKAGE_DIR = Path(__file__).resolve().parent
API_DIR = _PACKAGE_DIR.parents[1]
REPO_ROOT = _PACKAGE_DIR.parents[3]

#: App-local ``.env``, resolved to an absolute path so behaviour does not depend
#: on the current working directory (repo root and ``apps/api`` both work).
#: The repository-root ``.env`` is intentionally NOT read here — it is passed
#: explicitly via ``uv run --env-file .env ...`` so it keeps a single owner.
ENV_FILES: tuple[str, ...] = (str(API_DIR / ".env"),)


class Settings(BaseSettings):
    """Runtime configuration read from the environment / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=ENV_FILES,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # Keep keyword construction (``Settings(environment="test")``) working
        # even though the field declares an env-var alias.
        populate_by_name=True,
    )

    # -- app ---------------------------------------------------------------- #
    environment: Literal["local", "test", "production"] = Field(
        default="local",
        validation_alias=AliasChoices(*APP_ENV_VARS),
        description="Runtime environment; APP_ENV is primary, ENVIRONMENT is a legacy alias.",
    )

    # -- database ----------------------------------------------------------- #
    database_url: str = DEFAULT_DATABASE_URL
    db_echo: bool = False
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)
    db_pool_timeout_seconds: float = Field(default=30.0, gt=0)
    db_pool_recycle_seconds: int = Field(default=1800, gt=0)

    #: ``SET LOCAL lock_timeout`` for every kernel write transaction. On expiry
    #: PostgreSQL raises ``55P03`` and the service reports ``WriteBusy`` with
    #: nothing written (plan D17). Kept below the statement timeout so "the row
    #: was busy" is distinguishable from "the statement was too slow".
    db_lock_timeout_ms: int = Field(default=5_000, gt=0, le=60_000)

    #: ``SET LOCAL statement_timeout`` for every kernel write transaction.
    #: ``57014`` also maps to ``WriteBusy`` when it is a confirmed failure;
    #: a lost commit acknowledgement stays ``outcome_unknown``.
    db_statement_timeout_ms: int = Field(default=10_000, gt=0, le=300_000)

    # -- auth (Phase 3) ----------------------------------------------------- #
    #: Opaque access token lifetime (D1). Short by design: it bounds how long a
    #: token that is *no longer accepted* could still be presented. It is **not** a
    #: revocation grace period - every request re-checks the token, its session and
    #: the workspace membership, so revoking a session invalidates its access tokens
    #: immediately rather than at expiry.
    auth_access_ttl_s: int = Field(default=600, gt=0)
    #: Absolute session lifetime (30 days). Refreshing never extends it.
    auth_session_ttl_s: int = Field(default=2_592_000, gt=0)
    #: Closed by default: a fresh deployment has no self-service signup (D6).
    auth_registration_open: bool = False
    #: Password length policy. Locked to **8-256 characters** (D7 override), and
    #: UTF-8 additionally capped at 1024 bytes (never trimmed or truncated). The
    #: bounds are clamped here, not merely documented: a deployment must not be able
    #: to widen the policy past the locked ceiling, because the byte cap alone would
    #: then allow a request to force unbounded Argon2 input.
    auth_password_min_chars: int = Field(default=8, ge=8, le=256)
    auth_password_max_chars: int = Field(default=256, ge=8, le=256)
    #: Argon2id cost parameters. These are **informational only**: the hasher's
    #: costs are locked module constants in :mod:`revnote_api.security.passwords`
    #: (D7: 64 MiB / t=3 / p=4), and ``AuthService`` refuses to start if these
    #: values disagree with it. They are declared (and documented in §7) so the
    #: protocol numbers are visible in configuration, not so they can be tuned.
    #: ``ARGON2_MEMORY_COST`` is the protocol name; the field reads in KiB because
    #: that is the unit Argon2 itself takes. Both spellings are accepted so an
    #: operator copying the plan verbatim works.
    argon2_memory_kib: int = Field(
        default=65_536,
        ge=8_192,
        validation_alias=AliasChoices("ARGON2_MEMORY_COST", "ARGON2_MEMORY_KIB"),
    )
    argon2_time_cost: int = Field(default=3, ge=1)
    argon2_parallelism: int = Field(default=4, ge=1)
    #: Hashing is CPU-bound: cap concurrent hashes so a login burst cannot
    #: starve the event loop. A resource guard, not a brute-force defence.
    auth_hash_max_concurrency: int = Field(default=2, ge=1)

    # -- sync (Phase 3) ----------------------------------------------------- #
    #: A push carries 1-50 ops (D9).
    sync_push_max_ops: int = Field(default=50, ge=1, le=50)
    #: Raw JSON body budget for a push request (D9: 2 MiB).
    sync_push_max_bytes: int = Field(default=2_097_152, ge=1_024)
    #: HTTP-layer note body budget (D9). The kernel's own CHECK is untouched.
    sync_note_max_bytes: int = Field(default=262_144, ge=1)
    #: Wall-clock budget for one push batch, checked **between** ops only (D17).
    sync_push_budget_ms: int = Field(default=30_000, ge=1)
    #: Pull page size: default and ceiling (D14).
    sync_pull_default_limit: int = Field(default=100, ge=1)
    sync_pull_max_limit: int = Field(default=500, ge=1)
    #: Approximate pull response budget (D14: ~4 MiB).
    sync_pull_max_bytes: int = Field(default=4_194_304, ge=1_024)
    #: Legacy full-bootstrap limits: one ``REPEATABLE READ`` view, no pagination
    #: (D15). Phase 7 keeps this path byte-compatible; paged reads use the three
    #: limits below instead.
    sync_snapshot_max_objects: int = Field(default=10_000, ge=1)
    sync_snapshot_max_bytes: int = Field(default=16_777_216, ge=1_024)
    #: Paged bootstrap (Phase 7). One page may hold at most this many objects and
    #: this many serialized bytes; the whole bootstrap is refused when the pinned
    #: watermark holds more than ``sync_snapshot_max_total_objects`` objects.
    sync_snapshot_page_max_objects: int = Field(default=1_000, ge=1)
    sync_snapshot_page_max_bytes: int = Field(default=4_194_304, ge=1_024)
    sync_snapshot_max_total_objects: int = Field(default=100_000, ge=1)

    # -- attachments (Phase 5) ---------------------------------------------- #
    #: Master switch (D12). Closed by default: a deployment with no object
    #: storage configured must not *look* like it supports attachments.
    #: ``GET /api/v1/capabilities`` reports the effective value so a client can
    #: hide the entry point instead of discovering the gap on first upload.
    attachments_enabled: bool = False
    #: Hard ceiling on one attachment, enforced twice: as the HTTP body budget
    #: for the upload path (middleware) and by the storage write itself, which
    #: aborts the moment the running total passes it (25 MiB).
    attachment_max_bytes: int = Field(default=26_214_400, ge=1_024)
    #: How long a ``pending`` row may exist before the GC script may delete it.
    #: ``ready`` rows are never collected automatically (D8).
    attachment_pending_grace_s: int = Field(default=86_400, ge=1)
    #: MIME allowlist (D9). Deliberately narrow, and ``image/svg+xml`` is
    #: excluded on purpose: an SVG is a document that can carry script, and it
    #: would be served from our own origin.
    attachment_accepted_mime: list[str] = Field(
        default_factory=lambda: [
            "image/png",
            "image/jpeg",
            "image/webp",
            "image/gif",
            "application/pdf",
            "text/plain",
        ]
    )

    # -- Joplin bridge (Phase 6) -------------------------------------------- #
    #: Master switch (D10). Closed by default: a deployment that has not opted
    #: into Joplin compatibility must not *look* like a Joplin Server. When it is
    #: false the compatibility routes are never registered and ``/capabilities``
    #: reports ``enabled: false``.
    joplin_bridge_enabled: bool = False
    #: Declared protocol baseline (D9). Informational: the HTTP contract in
    #: ``docs/joplin-protocol-notes.md`` is what the tests pin.
    joplin_protocol_baseline: str = "3.7"
    #: Lifetime of one bridge session token. A Joplin client keeps its
    #: ``session.id`` for weeks, so this is long by design; revocation is a row
    #: update and is checked on every request.
    joplin_session_ttl_s: int = Field(default=2_592_000, ge=300)
    #: A lock whose ``updated_at`` is older than this is treated as abandoned
    #: and may be reclaimed (Joplin Server's lock TTL is three hours).
    joplin_lock_ttl_s: int = Field(default=10_800, ge=60)
    #: Delta page size: default and ceiling. The ``limit`` parameter is advisory
    #: in Joplin's protocol (compression may shrink a page), but it is still
    #: bounded so one request cannot ask for the whole event log.
    joplin_delta_default_limit: int = Field(default=100, ge=1, le=500)
    joplin_delta_max_limit: int = Field(default=500, ge=1, le=500)

    # -- HTTP cross-cutting ------------------------------------------------- #
    #: Explicit origins only; empty means no CORS headers at all. CORS is not
    #: an authorization mechanism - credentials are never allowed.
    cors_allow_origins: list[str] = Field(default_factory=list)
    #: Reserved switch. Nothing enforces it in this phase: a real limiter needs
    #: shared state (Redis) that v1 deliberately does not have. Declared here so
    #: the name in the plan/spec is not invented later by someone else.
    rate_limit_enabled: bool = False

    # -- dev server (used by ``uv run revnote-api``) ------------------------ #
    server_host: str = "127.0.0.1"
    server_port: int = Field(default=8000, ge=1, le=65_535)
    server_reload: bool = False

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        """Accept ``postgresql+asyncpg`` URLs only, with a database name."""
        raw = value.strip()
        if not raw:
            raise ValueError("DATABASE_URL must not be empty")

        try:
            url = make_url(raw)
        except ArgumentError as exc:  # malformed URL
            raise ValueError(f"DATABASE_URL is not a valid SQLAlchemy URL: {exc}") from exc

        if url.drivername != ASYNC_PG_DRIVER:
            raise ValueError(
                f"unsupported database driver {url.drivername!r}: "
                f"Revnote v1 only supports {ASYNC_PG_DRIVER!r} "
                "(note: a bare 'postgresql://' URL must be written as "
                f"'{ASYNC_PG_DRIVER}://')"
            )
        if not url.database:
            raise ValueError("DATABASE_URL must include a database name")
        return raw

    @model_validator(mode="after")
    def _validate_cross_field_invariants(self) -> Settings:
        """Refuse configurations whose numbers would contradict each other."""
        if self.auth_password_max_chars < self.auth_password_min_chars:
            raise ValueError(
                "AUTH_PASSWORD_MAX_CHARS must be greater than or equal to AUTH_PASSWORD_MIN_CHARS"
            )
        if self.sync_pull_default_limit > self.sync_pull_max_limit:
            raise ValueError("SYNC_PULL_DEFAULT_LIMIT must not exceed SYNC_PULL_MAX_LIMIT")
        # Page limits are deliberately independent of the legacy full-snapshot
        # limits: a deployment may lower the legacy cap (fault injection included)
        # while keeping paged bootstrap usable.
        if self.joplin_delta_default_limit > self.joplin_delta_max_limit:
            raise ValueError("JOPLIN_DELTA_DEFAULT_LIMIT must not exceed JOPLIN_DELTA_MAX_LIMIT")
        if self.db_lock_timeout_ms > self.db_statement_timeout_ms:
            raise ValueError("DB_LOCK_TIMEOUT_MS must not exceed DB_STATEMENT_TIMEOUT_MS")
        # SVG is refused here as well as in the service: the allowlist is a
        # security boundary, not a preference, and configuring it back in is
        # exactly the change nobody would notice in review (D9).
        if "image/svg+xml" in {mime.strip().lower() for mime in self.attachment_accepted_mime}:
            raise ValueError("ATTACHMENT_ACCEPTED_MIME must not include image/svg+xml")
        return self

    @property
    def masked_database_url(self) -> str:
        """DSN with the password hidden — safe to log."""
        return make_url(self.database_url).render_as_string(hide_password=True)

    def __repr__(self) -> str:
        """Field list with the DSN password masked.

        pydantic's generated ``repr`` prints ``database_url`` verbatim, i.e. with
        the password, and a settings object is exactly the thing people log at
        boot ("effective configuration"). Masking here means an accidental
        ``logger.info(settings)`` cannot leak a credential.
        """
        fields = ", ".join(
            f"{name}={self.masked_database_url!r}"
            if name == "database_url"
            else f"{name}={getattr(self, name)!r}"
            for name in type(self).model_fields
        )
        return f"Settings({fields})"

    __str__ = __repr__


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that repeated ``get_settings()`` calls (app factory, Alembic env)
    see identical values. Tests may call ``get_settings.cache_clear()`` after
    mutating the environment.
    """
    return Settings()
