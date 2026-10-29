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

import ast
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


def _called_names(source: str) -> set[str]:
    """Attribute names actually called in a module (so docstrings never match)."""
    return {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _imported_modules(path: Path) -> set[str]:
    """First-party modules a file imports (``import a.b`` / ``from a.b import x``)."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return {module for module in modules if module.startswith("revnote_api")}


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
    called = _called_names(env_source)

    assert not called & {"create_all", "drop_all"}
    assert "CheckConstraint" not in env_source


def test_env_py_imports_models_for_target_metadata() -> None:
    """Phase 2: ``env.py`` explicitly imports the model package (plan §3)."""
    env_source = (API_DIR / "migrations" / "env.py").read_text(encoding="utf-8")

    assert "import revnote_api.models" in env_source
    assert env_source.index("import revnote_api.models") < env_source.index(
        "target_metadata = Base.metadata"
    )


def test_env_py_is_async_and_uses_settings() -> None:
    env_source = (API_DIR / "migrations" / "env.py").read_text(encoding="utf-8")

    assert "async_engine_from_config" in env_source
    assert "run_sync" in env_source
    assert "get_settings" in env_source
    assert "target_metadata = Base.metadata" in env_source


def test_revision_chain_is_baseline_then_kernel_core() -> None:
    """One head: 0001_baseline (empty) -> 0002_sync_kernel_core (six tables)."""
    versions = sorted((API_DIR / "migrations" / "versions").glob("*.py"))

    assert [path.name for path in versions] == [
        "0001_baseline.py",
        "0002_sync_kernel_core.py",
    ]

    baseline = versions[0].read_text(encoding="utf-8")
    assert 'revision: str = "0001_baseline"' in baseline
    assert "down_revision: str | None = None" in baseline
    assert "op.create_table" not in baseline
    assert "op.execute" not in baseline

    kernel = versions[1].read_text(encoding="utf-8")
    assert 'revision: str = "0002_sync_kernel_core"' in kernel
    assert 'down_revision: str | None = "0001_baseline"' in kernel
    assert kernel.count("def upgrade") == 1
    assert kernel.count("def downgrade") == 1


def test_models_package_exists_and_declares_only_the_kernel_tables() -> None:
    """Phase 2 flips the old "no models" guardrail: models exist, kernel/adapter don't."""
    models_dir = API_DIR / "src" / "revnote_api" / "models"

    assert (models_dir / "__init__.py").is_file()
    assert sorted(path.name for path in models_dir.glob("*.py")) == [
        "__init__.py",
        "change_event.py",
        "folder.py",
        "note.py",
        "op_ledger.py",
        "user.py",
        "workspace.py",
    ]

    imported = _imported_modules(models_dir / "__init__.py")
    assert imported == {
        "revnote_api.db.base",
        "revnote_api.models.change_event",
        "revnote_api.models.folder",
        "revnote_api.models.note",
        "revnote_api.models.op_ledger",
        "revnote_api.models.user",
        "revnote_api.models.workspace",
    }


def test_models_package_is_layered_below_services_and_transport() -> None:
    """Models are pure schema: no FastAPI/HTTP/engine/session/service imports.

    This replaces the scaffold's "no models package" guardrail with the Phase 2
    rule from plan §3 (models must stay importable without a server or web stack),
    which stays true no matter what the kernel/service layer grows into.
    """
    models_dir = API_DIR / "src" / "revnote_api" / "models"
    forbidden = {
        "fastapi",
        "starlette",
        "httpx",
        "uvicorn",
        "revnote_api.api",
        "revnote_api.app",
        "revnote_api.main",
        "revnote_api.services",
        "revnote_api.kernel",
        "revnote_api.adapters",
        "revnote_api.db.engine",
        "revnote_api.db.session",
        "revnote_api.config",
    }

    for path in sorted(models_dir.glob("*.py")):
        imported = _imported_modules(path)
        assert not imported & forbidden, f"{path.name} imports {imported & forbidden}"


def test_models_never_open_connections_or_create_schema() -> None:
    models_dir = API_DIR / "src" / "revnote_api" / "models"

    for path in sorted(models_dir.glob("*.py")):
        called = _called_names(path.read_text(encoding="utf-8"))
        assert not called & {"create_all", "drop_all", "execute", "commit"}, path.name


@pytest.mark.parametrize("path", ["migrations/env.py", "migrations/script.py.mako"])
def test_migration_assets_present(path: str) -> None:
    assert (API_DIR / path).is_file()
