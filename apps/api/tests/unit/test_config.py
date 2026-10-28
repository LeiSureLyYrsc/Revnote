"""Configuration tests: defaults, extra-env tolerance, PostgreSQL-only guard."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from revnote_api import config as revnote_api_config
from revnote_api.config import ASYNC_PG_DRIVER, DEFAULT_DATABASE_URL, Settings, get_settings


def test_defaults_allow_local_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """No environment at all must still produce a usable settings object."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(_env_file=None)

    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.database_url.startswith(f"{ASYNC_PG_DRIVER}://")
    assert settings.environment == "local"
    assert settings.db_pool_size >= 1


def test_ignores_unrelated_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compose/.env keys this service does not use must not raise."""
    monkeypatch.setenv("POSTGRES_USER", "revnote")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "minio-secret")
    monkeypatch.setenv("SOMETHING_ELSE_ENTIRELY", "1")

    settings = Settings(_env_file=None)

    assert settings.database_url == DEFAULT_DATABASE_URL


def test_database_url_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+asyncpg://user:pw@db.internal:5432/revnote_test"
    monkeypatch.setenv("DATABASE_URL", url)

    settings = Settings(_env_file=None)

    assert settings.database_url == url
    assert "pw" not in settings.masked_database_url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:pw@localhost:5432/revnote",  # sync-ish psycopg URL
        "postgresql+psycopg://user:pw@localhost:5432/revnote",
        "postgresql+psycopg2://user:pw@localhost:5432/revnote",
        "sqlite+aiosqlite:///./revnote.db",
        "mysql+aiomysql://user:pw@localhost/revnote",
    ],
)
def test_rejects_non_asyncpg_drivers(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """v1 is PostgreSQL/asyncpg only; everything else fails fast."""
    monkeypatch.setenv("DATABASE_URL", url)

    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None)

    assert "asyncpg" in str(excinfo.value)


def test_rejects_url_without_database_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pw@localhost:5432")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_env_files_are_absolute_and_cwd_independent() -> None:
    """`.env` lookup must not depend on the working directory."""
    package_dir = Path(revnote_api_config.__file__).resolve().parent
    api_dir = package_dir.parents[1]
    repo_root = package_dir.parents[3]

    assert api_dir == revnote_api_config.API_DIR
    assert repo_root == revnote_api_config.REPO_ROOT
    assert (api_dir / "alembic.ini").is_file()

    for candidate in revnote_api_config.ENV_FILES:
        assert Path(candidate).is_absolute()


def test_only_app_local_env_file_is_implicit() -> None:
    """Single config source: the repo-root `.env` is never read implicitly.

    The root `.env` belongs to compose / the root README and is passed in
    explicitly with `uv run --env-file .env ...`, so it cannot silently drift
    into a second source of truth for this service.
    """
    files = revnote_api_config.ENV_FILES

    assert files == (str(revnote_api_config.API_DIR / ".env"),)
    assert str(revnote_api_config.REPO_ROOT / ".env") not in files


def test_app_env_is_primary_and_defaults_to_local(monkeypatch: pytest.MonkeyPatch) -> None:
    """APP_ENV is the contract name; unset falls back to `local`."""
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)

    assert Settings(_env_file=None).environment == "local"

    monkeypatch.setenv("APP_ENV", "production")
    assert Settings(_env_file=None).environment == "production"


def test_legacy_environment_is_still_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    """ENVIRONMENT remains a compat alias (older deployments keep working)."""
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "test")

    assert Settings(_env_file=None).environment == "test"


def test_app_env_wins_over_legacy_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both set -> APP_ENV takes precedence."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ENVIRONMENT", "test")

    assert Settings(_env_file=None).environment == "production"


def test_rejects_unknown_app_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "staging")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_get_settings_is_cached_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """`get_settings()` is process-wide; cache_clear() re-reads the env."""
    get_settings.cache_clear()
    first = get_settings()
    assert get_settings() is first

    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings() is not first
        assert get_settings().environment == "test"
    finally:
        get_settings.cache_clear()


def test_masked_url_hides_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:sup3rs3cret@db:5432/revnote")
    settings = Settings(_env_file=None)

    assert "sup3rs3cret" not in settings.masked_database_url
    assert "db:5432/revnote" in settings.masked_database_url
