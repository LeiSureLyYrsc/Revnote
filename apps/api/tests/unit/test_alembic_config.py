"""Alembic wiring tests — runnable with no database (config/metadata only).

These guard the *configuration* half of the migration story:

* ``alembic.ini`` paths resolve when invoked from the repository root
  (``alembic -c apps/api/alembic.ini ...``);
* the async ``env.py`` loads and derives the DSN from settings instead of the
  ini file, and owns no schema creation of its own;
* the shipped baseline is a single no-op revision with no tables.

Real ``upgrade``/``downgrade`` execution lives in
``tests/integration/test_pg_connectivity.py`` (gated by ``TEST_DATABASE_URL``).
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

API_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = API_DIR.parents[1]
ALEMBIC_INI = API_DIR / "alembic.ini"


def _raw_ini() -> configparser.RawConfigParser:
    """RawConfigParser: ``%(here)s`` is Alembic's token, not configparser's."""
    parser = configparser.RawConfigParser()
    parser.read(ALEMBIC_INI, encoding="utf-8")
    return parser


def test_alembic_ini_exists() -> None:
    assert ALEMBIC_INI.is_file()


def test_script_location_is_here_relative_and_exists() -> None:
    """`-c apps/api/alembic.ini` must resolve scripts next to the ini file."""
    script_location = _raw_ini().get("alembic", "script_location")

    assert script_location == "%(here)s/migrations"
    assert (API_DIR / "migrations" / "env.py").is_file()
    assert (API_DIR / "migrations" / "script.py.mako").is_file()
    assert (API_DIR / "migrations" / "versions").is_dir()


def test_prepend_sys_path_points_at_src() -> None:
    prepend = _raw_ini().get("alembic", "prepend_sys_path")

    assert prepend == "%(here)s/src"
    assert (REPO_ROOT / "apps/api/src/revnote_api/__init__.py").is_file()


def test_url_is_not_hardcoded_in_ini() -> None:
    """The DSN comes from Settings/env only — never from alembic.ini."""
    assert _raw_ini().get("alembic", "sqlalchemy.url").strip() == ""


def test_ini_is_reachable_from_repo_root_invocation() -> None:
    """Documented command form: run from the repo root with -c apps/api/..."""
    assert (REPO_ROOT / "apps/api/alembic.ini").is_file()
    assert (REPO_ROOT / "pyproject.toml").is_file()  # shared .venv lives here


def test_env_py_never_mass_creates_schema() -> None:
    """Schema is owned by revisions; bulk metadata creation must not appear."""
    env_source = (API_DIR / "migrations" / "env.py").read_text(encoding="utf-8")

    for forbidden in ("create_all", "drop_all", "CheckConstraint"):
        assert forbidden not in env_source


def test_env_py_is_async_and_uses_settings() -> None:
    env_source = (API_DIR / "migrations" / "env.py").read_text(encoding="utf-8")

    assert "async_engine_from_config" in env_source
    assert "run_sync" in env_source
    assert "get_settings" in env_source
    assert "target_metadata = Base.metadata" in env_source


def test_single_baseline_revision_is_empty() -> None:
    versions = sorted((API_DIR / "migrations" / "versions").glob("*.py"))

    assert [path.name for path in versions] == ["0001_baseline.py"]

    source = versions[0].read_text(encoding="utf-8")
    assert 'revision: str = "0001_baseline"' in source
    assert "down_revision: str | None = None" in source
    assert "op.create_table" not in source
    assert "op.execute" not in source


def test_scaffold_has_no_models_package() -> None:
    """Guardrail: no ORM models / CRUD surface in this scaffold."""
    assert not (API_DIR / "src" / "revnote_api" / "models").exists()
    assert not (API_DIR / "src" / "revnote_api" / "kernel").exists()
    assert not (API_DIR / "src" / "revnote_api" / "adapters").exists()


@pytest.mark.parametrize("path", ["migrations/env.py", "migrations/script.py.mako"])
def test_migration_assets_present(path: str) -> None:
    assert (API_DIR / path).is_file()
