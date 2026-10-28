"""Application settings (pydantic-settings).

Scaffold rules baked into this module:

* **``APP_ENV`` is the contract name** for the runtime environment. The legacy
  ``ENVIRONMENT`` spelling is still accepted as a fallback alias so existing
  deployments keep working, but ``APP_ENV`` wins when both are present. When
  neither is set the value defaults to ``local``.
* **Unknown environment variables are ignored** (``extra="ignore"``). The repo
  root ``.env`` is shared with docker-compose and carries ``POSTGRES_*`` /
  ``MINIO_*`` keys this service must tolerate.
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

from pydantic import AliasChoices, Field, field_validator
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

    @property
    def masked_database_url(self) -> str:
        """DSN with the password hidden — safe to log."""
        return make_url(self.database_url).render_as_string(hide_password=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton.

    Cached so that repeated ``get_settings()`` calls (app factory, Alembic env)
    see identical values. Tests may call ``get_settings.cache_clear()`` after
    mutating the environment.
    """
    return Settings()
