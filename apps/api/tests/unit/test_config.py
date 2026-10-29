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


# --------------------------------------------------------------------------- #
# Phase 3 settings: locked defaults
# --------------------------------------------------------------------------- #


def test_phase3_defaults_match_the_locked_decision_table() -> None:
    """Every budget below is a protocol number: a silent drift is a bug."""
    settings = Settings(_env_file=None)

    # Auth (D1, D2, D6) and the locked password override: 8-256, not 15.
    assert settings.auth_access_ttl_s == 600
    assert settings.auth_session_ttl_s == 30 * 24 * 60 * 60
    assert settings.auth_registration_open is False
    assert settings.auth_password_min_chars == 8
    assert settings.auth_password_max_chars == 256
    # Argon2id cost recorded in the hash, so a change stays migratable.
    assert settings.argon2_memory_kib == 65_536
    assert settings.argon2_time_cost == 3
    assert settings.argon2_parallelism == 4
    assert settings.auth_hash_max_concurrency == 2

    # Push (D9): 1-50 ops, 2 MiB raw JSON, 256 KiB note body, 30 s batch budget.
    assert settings.sync_push_max_ops == 50
    assert settings.sync_push_max_bytes == 2 * 1024 * 1024
    assert settings.sync_note_max_bytes == 256 * 1024
    assert settings.sync_push_budget_ms == 30_000

    # Pull (D14) and snapshot (D15).
    assert settings.sync_pull_default_limit == 100
    assert settings.sync_pull_max_limit == 500
    assert settings.sync_pull_max_bytes == 4 * 1024 * 1024
    assert settings.sync_snapshot_max_objects == 10_000
    assert settings.sync_snapshot_max_bytes == 16 * 1024 * 1024

    # Timeouts (D17) and CORS.
    assert settings.db_lock_timeout_ms == 5_000
    assert settings.db_statement_timeout_ms == 10_000
    assert settings.cors_allow_origins == []


def test_lock_timeout_leaves_room_for_a_configured_statement_timeout() -> None:
    """The service constant and the config default must not contradict each other."""
    from revnote_api.services.sync_write import LOCK_TIMEOUT_MS

    settings = Settings(_env_file=None)

    assert settings.db_lock_timeout_ms == LOCK_TIMEOUT_MS


def test_the_password_minimum_is_eight_not_fifteen() -> None:
    """The locked override: plan D7's 15 was superseded by the build prompt."""
    settings = Settings(_env_file=None)

    assert settings.auth_password_min_chars == 8
    assert settings.auth_password_min_chars < 15


def test_a_password_policy_below_the_hard_floor_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_PASSWORD_MIN_CHARS", "4")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_an_inverted_password_range_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_PASSWORD_MIN_CHARS", "64")
    monkeypatch.setenv("AUTH_PASSWORD_MAX_CHARS", "32")

    with pytest.raises(ValidationError, match="MAX_CHARS"):
        Settings(_env_file=None)


def test_a_pull_default_above_its_ceiling_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SYNC_PULL_DEFAULT_LIMIT", "600")

    with pytest.raises(ValidationError, match="SYNC_PULL_DEFAULT_LIMIT"):
        Settings(_env_file=None)


def test_a_lock_timeout_above_the_statement_timeout_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lock wait longer than the statement budget could never be reported."""
    monkeypatch.setenv("DB_LOCK_TIMEOUT_MS", "20000")

    with pytest.raises(ValidationError, match="DB_LOCK_TIMEOUT_MS"):
        Settings(_env_file=None)


def test_phase3_settings_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYNC_PUSH_MAX_OPS", "10")
    monkeypatch.setenv("AUTH_REGISTRATION_OPEN", "true")
    monkeypatch.setenv("SYNC_PULL_DEFAULT_LIMIT", "25")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", '["https://app.example.test"]')

    settings = Settings(_env_file=None)

    assert settings.sync_push_max_ops == 10
    assert settings.auth_registration_open is True
    assert settings.sync_pull_default_limit == 25
    assert settings.cors_allow_origins == ["https://app.example.test"]


def test_argon2_memory_uses_the_protocol_env_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plan/spec spell it ``ARGON2_MEMORY_COST``; a verbatim copy must work."""
    monkeypatch.setenv("ARGON2_MEMORY_COST", "32768")

    settings = Settings(_env_file=None)

    assert settings.argon2_memory_kib == 32_768


def test_argon2_memory_also_accepts_the_field_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The alias must not break the auto-derived name (``ARGON2_MEMORY_KIB``)."""
    monkeypatch.delenv("ARGON2_MEMORY_COST", raising=False)
    monkeypatch.setenv("ARGON2_MEMORY_KIB", "16384")

    assert Settings(_env_file=None).argon2_memory_kib == 16_384


def test_rate_limiting_is_declared_but_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Placeholder only: no limiter exists, so the documented default is ``false``."""
    monkeypatch.delenv("RATE_LIMIT_ENABLED", raising=False)

    assert Settings(_env_file=None).rate_limit_enabled is False


def test_registration_is_closed_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """D6: a fresh deployment has no self-service signup."""
    monkeypatch.delenv("AUTH_REGISTRATION_OPEN", raising=False)

    assert Settings(_env_file=None).auth_registration_open is False


def test_no_auth_secret_is_declared() -> None:
    """Opaque tokens make a signing secret unnecessary; adding one invites misuse."""
    declared = set(Settings.model_fields)

    assert not {name for name in declared if "secret" in name}
    assert "auth_secret" not in declared


def test_phase3_settings_repr_shows_no_credential_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Settings`` is often logged at boot; the DSN password must not be in it.

    Field *names* like ``auth_password_max_chars`` are policy, not credentials;
    what must never appear is a secret *value*.
    """
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:sup3rs3cret@db:5432/revnote")

    rendered = repr(Settings(_env_file=None))

    assert "sup3rs3cret" not in rendered
    # No field carries a password/secret/token value at all.
    credential_fields = {
        name for name in Settings.model_fields if name.endswith(("_secret", "_password", "_token"))
    }
    assert credential_fields == set()
