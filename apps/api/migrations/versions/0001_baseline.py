"""empty baseline

Revision ID: 0001_baseline
Revises:

Scaffold baseline: establishes the Alembic revision chain for Revnote.

This revision deliberately creates **no tables**. Domain tables (workspace,
note, revision, change_event, attachment, outbox, ...) arrive in later changes
as their own revisions, and the schema is never produced by bulk metadata
creation from the ORM models.

``upgrade``/``downgrade`` are both no-ops, so they must succeed against any
reachable PostgreSQL database and can be exercised as a connectivity smoke test.
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: the baseline intentionally creates no objects."""
    pass


def downgrade() -> None:
    """No-op: nothing to undo for the baseline."""
    pass
