"""Application-side identifier helpers (D1).

UUIDv7 is generated in the application, never by the database (there is no
``gen_random_uuid()``/``uuid_generate_v7()`` default anywhere in the schema).
``uuid-utils`` is used through its ``compat`` shim so callers always receive a
**standard library** :class:`uuid.UUID`, which is what SQLAlchemy/asyncpg and
the rest of the code base expect.

UUIDv7 is time-ordered but it is **not** the sync cursor: ordering of changes
comes exclusively from ``workspaces.last_seq`` (D1/D2).
"""

from __future__ import annotations

import uuid

from uuid_utils.compat import uuid7 as _uuid7

__all__ = ["new_entity_id", "new_id", "new_op_id", "uuid7"]


def uuid7() -> uuid.UUID:
    """Return a fresh UUIDv7 as a stdlib :class:`uuid.UUID`."""
    return _uuid7()


#: Unified helper spelling: one call, one new time-ordered id.
new_id = uuid7


def new_entity_id() -> uuid.UUID:
    """Identifier for a new folder/note (created once, then reused by retries, D5)."""
    return _uuid7()


def new_op_id() -> uuid.UUID:
    """Identifier for one command attempt.

    Generate it once per *logical* command and keep it across retries: the
    ``(workspace_id, op_id)`` ledger is what makes a retry idempotent (D5).
    """
    return _uuid7()
