"""The CLI tier always runs at the migrated schema; there is no skip path.

``business_schema_at_head`` is registered by the parent ``tests/conftest.py``
(imported from ``pg_support``), so this fixture only makes every integration case
depend on it.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _schema_at_head(business_schema_at_head: str) -> str:
    """Run the real Alembic chain once before any CLI integration case connects."""
    return business_schema_at_head
