"""PostgreSQL targets and schema bootstrap shared by every API test.

Why this is a module and not a conftest
---------------------------------------
Two rules force the split:

* **Test modules must never import a conftest.** A conftest is collected by path, so
  importing one either duplicates its fixtures or creates a second, silently
  different copy of the same names.
* **``tests/api`` and ``tests/integration`` must agree on the environment contract.**
  The kernel suite already proves the schema against ``TEST_DATABASE_URL``; this
  package has to speak the same two-variable contract rather than invent a third.

What it guarantees
------------------
* **Real Alembic, never ``create_all``.** The business database is brought to ``head``
  by the actual CLI (``python -m alembic -c apps/api/alembic.ini upgrade head``) in a
  subprocess. ``Base.metadata.create_all()`` would prove only that the ORM can build
  *a* schema: it skips the migration chain, its ordering, its backfills and its
  constraint names - which is precisely what the auth and sync suites depend on.
* **Two distinct databases.** ``TEST_DATABASE_URL`` (business: migrated, written to)
  and ``TEST_MIGRATION_DATABASE_URL`` (destructive round trips) are both required and
  proven to address different databases. Pointing both at one database silently
  breaks the guarantee that a downgrade test can never destroy the business schema.
* **Migration runs off the event loop.** ``migrations/env.py`` calls
  ``asyncio.run()`` itself, so it cannot execute inside a live test loop; a
  subprocess also keeps the migration connections away from the test engines. That is
  what lets the schema fixture be **session-scoped and synchronous** - no event loop
  is captured, so no later test can inherit a connection bound to a dead loop.
* **No skip.** A missing or malformed DSN is a hard failure. A skipped API suite is
  indistinguishable from a passing one in CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from revnote_api.config import ASYNC_PG_DRIVER

__all__ = [
    "TEST_DATABASE_URL_ENV",
    "TEST_MIGRATION_DATABASE_URL_ENV",
    "PgTargets",
    "alembic_current_revision",
    "bootstrap_business_schema",
    "require_targets",
]

#: The business database: migrated to ``head`` once per session, then written to.
TEST_DATABASE_URL_ENV = "TEST_DATABASE_URL"

#: The migration database: destructive ``base``/``head`` round trips only.
TEST_MIGRATION_DATABASE_URL_ENV = "TEST_MIGRATION_DATABASE_URL"

# apps/api/tests/pg_support/pg_env.py -> pg_support, tests, api
API_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = API_DIR.parents[1]

#: Passed to the CLI relative to the repo root, which is the documented invocation.
ALEMBIC_INI = str((API_DIR / "alembic.ini").relative_to(REPO_ROOT))


class SchemaBootstrapError(RuntimeError):
    """A real Alembic command failed, or left the database at the wrong revision."""


def _identity(database_url: str) -> tuple[str | None, int | None, str | None]:
    """``(host, port, database)``: what makes two DSNs the *same* target."""
    url = make_url(database_url)
    return url.host, url.port, url.database


def _require(env_name: str) -> str:
    """A validated ``postgresql+asyncpg`` DSN, or a hard failure naming the variable."""
    raw = os.environ.get(env_name, "").strip()
    if not raw:
        raise RuntimeError(
            f"{env_name} is not set. The API suite runs against real PostgreSQL and "
            "never skips; deselect apps/api/tests/api explicitly if you have no server."
        )
    try:
        url = make_url(raw)
    except ArgumentError as exc:
        raise RuntimeError(f"{env_name} is not a valid SQLAlchemy URL: {exc}") from exc
    if url.drivername != ASYNC_PG_DRIVER:
        raise RuntimeError(f"{env_name} must use {ASYNC_PG_DRIVER!r}, got {url.drivername!r}")
    if not url.database:
        raise RuntimeError(f"{env_name} must include a database name")
    return raw


@dataclass(frozen=True, slots=True)
class PgTargets:
    """The two disposable databases, validated and proven distinct."""

    database_url: str
    migration_database_url: str

    def masked(self) -> str:
        """Both DSNs with passwords hidden - the only form safe to print."""
        business = make_url(self.database_url).render_as_string(hide_password=True)
        migration = make_url(self.migration_database_url).render_as_string(hide_password=True)
        return f"business={business} migration={migration}"


def require_targets() -> PgTargets:
    """Read, validate and de-collide both DSNs (raises ``RuntimeError``).

    Raises rather than calling ``pytest.fail`` so this module stays importable
    outside a test run; the fixtures translate the failure.
    """
    business = _require(TEST_DATABASE_URL_ENV)
    migration = _require(TEST_MIGRATION_DATABASE_URL_ENV)
    if _identity(business) == _identity(migration):
        raise RuntimeError(
            f"{TEST_DATABASE_URL_ENV} and {TEST_MIGRATION_DATABASE_URL_ENV} address the "
            f"same database ({make_url(business).render_as_string(hide_password=True)}). "
            "The migration round trips are destructive and must never touch the "
            "business schema."
        )
    return PgTargets(database_url=business, migration_database_url=migration)


def _run_alembic(database_url: str, *args: str, timeout: float = 300.0) -> str:
    """Run the real Alembic CLI from the repo root; return its combined output.

    ``DATABASE_URL`` is set for this child process only: ``migrations/env.py`` reads
    it through ``Settings``, so neither ``alembic.ini`` nor this module's environment
    ever holds a DSN.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", ALEMBIC_INI, *args],
        cwd=str(REPO_ROOT),
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    output = f"{completed.stdout}{completed.stderr}"
    if completed.returncode != 0:
        raise SchemaBootstrapError(
            f"alembic {' '.join(args)} exited {completed.returncode}\n--- output ---\n{output}"
        )
    return output


def alembic_current_revision(database_url: str) -> str:
    """The revision stamped in ``alembic_version``, read through the real CLI.

    Parsed from ``alembic current`` rather than queried directly: the point is to
    prove the *migration tool* agrees with the database, which is the same check CI
    runs as its own step.
    """
    output = _run_alembic(database_url, "current")
    for line in output.splitlines():
        stripped = line.strip()
        # ``current`` prints ``<revision> (head)``; warnings and INFO lines do not
        # start with a revision-shaped token.
        token = stripped.split(" ", 1)[0]
        if token and token[0].isdigit() and all(char.isalnum() or char == "_" for char in token):
            return token
    raise SchemaBootstrapError(f"could not read a revision from `alembic current`:\n{output}")


def bootstrap_business_schema(database_url: str) -> str:
    """Bring the business database to ``head`` with the real chain; return the revision.

    Idempotent by construction: ``upgrade head`` on an already-migrated database is a
    no-op, so a session-scoped caller can run it unconditionally. It deliberately does
    **not** downgrade first - the API suite writes rows that a round trip would
    destroy, and a failed run should be reproducible rather than silently rebuilt.
    """
    _run_alembic(database_url, "upgrade", "head")
    revision = alembic_current_revision(database_url)
    if not revision:
        raise SchemaBootstrapError(
            f"{database_url.split('@')[-1]} has no stamped revision after `upgrade head`"
        )
    return revision
