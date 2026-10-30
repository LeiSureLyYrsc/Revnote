"""CLI test support: shared path setup and small HTTP fixtures.

The real-PostgreSQL tier reuses ``apps/api/tests/pg_support`` rather than
duplicating an app builder, a schema bootstrap or an account seeder. That import
needs the API test directory on ``sys.path`` before collection, which is why it
happens here and not inside a test module.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

#: ``apps/cli/tests`` -> repository root -> ``apps/api/tests``.
API_TESTS = HERE.parents[2] / "apps" / "api" / "tests"
if str(API_TESTS) not in sys.path:
    sys.path.insert(0, str(API_TESTS))

# Re-exported fixtures (imported for pytest's collection, used by the integration
# tier): the real Alembic bootstrap and the validated pair of disposable DSNs.
from pg_support.auth_support import business_schema_at_head, pg_targets  # noqa: E402, F401


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """asyncio only: the API under test is asyncpg-backed."""
    return "asyncio"
