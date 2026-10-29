"""Real Alembic runs against the *migration* database, always in a subprocess.

Two reasons the CLI is shelled out instead of imported:

* ``migrations/env.py`` calls ``asyncio.run()`` itself, so it cannot run inside a
  live test event loop;
* the child process keeps the migration database's connections completely
  separate from the test engines.

:func:`run_alembic` refuses ``downgrade`` against anything but
``TEST_MIGRATION_DATABASE_URL``, so the business database can never be rolled
back by a test.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from pg_testing import TEST_DATABASE_URL_ENV, TEST_MIGRATION_DATABASE_URL_ENV
from sqlalchemy.engine import make_url

INTEGRATION_DIR = Path(__file__).resolve().parent
API_DIR = INTEGRATION_DIR.parents[1]
REPO_ROOT = API_DIR.parents[1]
ALEMBIC_INI_ARG = str((API_DIR / "alembic.ini").relative_to(REPO_ROOT))


class AlembicGuardError(RuntimeError):
    """A destructive command was about to hit the wrong database."""


class AlembicError(RuntimeError):
    """A real Alembic command exited non-zero."""


@dataclass(frozen=True, slots=True)
class AlembicResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def command(self) -> str:
        return " ".join(self.args)

    @property
    def output(self) -> str:
        return f"{self.stdout}{self.stderr}"


def masked(database_url: str) -> str:
    """DSN with the password hidden, safe for failure messages."""
    return make_url(database_url).render_as_string(hide_password=True)


def _identity(database_url: str) -> tuple[str | None, int | None, str | None]:
    url = make_url(database_url)
    return url.host, url.port, url.database


def ensure_downgrade_allowed(database_url: str) -> None:
    """Permit ``downgrade`` only against ``TEST_MIGRATION_DATABASE_URL``."""
    configured = os.environ.get(TEST_MIGRATION_DATABASE_URL_ENV, "").strip() or None
    if configured is None or _identity(configured) != _identity(database_url):
        raise AlembicGuardError(
            f"alembic downgrade is only permitted on {TEST_MIGRATION_DATABASE_URL_ENV}; "
            f"refusing to downgrade {masked(database_url)}"
        )


def run_alembic(
    database_url: str,
    *args: str,
    check: bool = True,
    timeout: float = 180.0,
) -> AlembicResult:
    """Run ``alembic`` from the repo root with ``DATABASE_URL`` set for that call.

    ``env.py`` reads the DSN through :class:`revnote_api.config.Settings`, so
    ``alembic.ini`` never carries one.
    """
    if "downgrade" in args:
        ensure_downgrade_allowed(database_url)

    env = {**os.environ, "DATABASE_URL": database_url}
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", ALEMBIC_INI_ARG, *args],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    result = AlembicResult(
        args=tuple(args),
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )
    if check and result.returncode != 0:
        raise AlembicError(
            f"alembic {result.command} exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return result


class AlembicCli:
    """Subprocess front end handed to tests as a fixture."""

    def run(
        self, database_url: str, *args: str, check: bool = True, timeout: float = 180.0
    ) -> AlembicResult:
        return run_alembic(database_url, *args, check=check, timeout=timeout)

    def guard(self, database_url: str) -> None:
        ensure_downgrade_allowed(database_url)


@dataclass(frozen=True, slots=True)
class MigrationDatabase:
    """The migration database, reachable only through the real Alembic CLI."""

    url: str
    business_url: str
    cli: AlembicCli

    def upgrade(self, target: str = "head") -> AlembicResult:
        return self.cli.run(self.url, "upgrade", target)

    def downgrade(self, target: str = "base") -> AlembicResult:
        return self.cli.run(self.url, "downgrade", target)

    def check(self) -> AlembicResult:
        """``alembic check``: ORM metadata and the live schema must agree."""
        return self.cli.run(self.url, "check")

    def assert_isolated_from_business(self) -> None:
        if self.url == self.business_url:
            raise AlembicGuardError(
                f"{TEST_MIGRATION_DATABASE_URL_ENV} must not equal {TEST_DATABASE_URL_ENV}"
            )
