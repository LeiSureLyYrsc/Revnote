"""Revnote domain models (Phase 2 sync kernel core).

Explicit registration — importing this package is what puts the six tables on
``Base.metadata``; nothing may depend on "some module happened to be imported".

Design rules for these models:

* PostgreSQL-only types: ``UUID``, ``BIGINT``, ``TIMESTAMPTZ``, ``BYTEA``.
* No ``SERIAL`` / IDENTITY / ``nextval`` anywhere: every primary key value and
  every ``seq``/``rev`` is allocated by the application/service.
* **No ``relationship()`` and no ORM cascade.** Cross-table behaviour is
  expressed with explicit named constraints (composite FKs, RESTRICT / deferred
  NO ACTION) so it is visible in the DDL and in migration diffs.
* ``create_all()`` is never called; Alembic revisions own the schema, and this
  metadata exists to make those revisions and ``alembic check`` agree.
* Only the six Phase 2 tables are declared: no revisions, tombstones, outbox,
  attachments or auth tables.

Services/tests import the entities from here::

    from revnote_api.models import ChangeEvent, Folder, Note, OpLedger, User, Workspace
"""

from __future__ import annotations

from revnote_api.db.base import Base
from revnote_api.models.change_event import ENTITY_TYPES, OPERATIONS, ChangeEvent
from revnote_api.models.folder import Folder
from revnote_api.models.note import Note
from revnote_api.models.op_ledger import REQUEST_HASH_BYTES, OpLedger
from revnote_api.models.user import User
from revnote_api.models.workspace import Workspace

#: The exact table set this phase owns. Migrations and tests assert against it.
MODEL_TABLES: tuple[str, ...] = (
    "users",
    "workspaces",
    "folders",
    "notes",
    "change_events",
    "op_ledger",
)

__all__ = [
    "ENTITY_TYPES",
    "MODEL_TABLES",
    "OPERATIONS",
    "REQUEST_HASH_BYTES",
    "Base",
    "ChangeEvent",
    "Folder",
    "Note",
    "OpLedger",
    "User",
    "Workspace",
]
