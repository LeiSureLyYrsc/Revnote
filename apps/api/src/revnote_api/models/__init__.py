"""Revnote domain models.

Explicit registration — importing this package is what puts the tables on
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
* Sensitive columns (``password_hash``, ``token_hash``) are excluded from every
  model ``__repr__``: repr ends up in logs and tracebacks.

Phase 2 declared the six sync-kernel tables; Phase 3 adds the authentication
schema (``auth_sessions``, ``auth_access_tokens``, ``auth_refresh_tokens``) and
the per-request authorisation row (``workspace_members``). ``users`` gained two
nullable columns rather than a second credentials table.

Services/tests import the entities from here::

    from revnote_api.models import Attachment, Folder, Note, User, Workspace, WorkspaceMember
"""

from __future__ import annotations

from revnote_api.db.base import Base
from revnote_api.models.attachment import ATTACHMENT_STATES, Attachment
from revnote_api.models.auth_access_token import AuthAccessToken
from revnote_api.models.auth_refresh_token import AuthRefreshToken
from revnote_api.models.auth_session import AuthSession
from revnote_api.models.change_event import ENTITY_TYPES, OPERATIONS, ChangeEvent
from revnote_api.models.folder import Folder
from revnote_api.models.note import Note
from revnote_api.models.op_ledger import REQUEST_HASH_BYTES, OpLedger
from revnote_api.models.user import User
from revnote_api.models.workspace import Workspace
from revnote_api.models.workspace_member import MEMBER_ROLES, WorkspaceMember

#: The exact table set this phase owns. Migrations and tests assert against it.
MODEL_TABLES: tuple[str, ...] = (
    "users",
    "workspaces",
    "folders",
    "notes",
    "change_events",
    "op_ledger",
    "auth_sessions",
    "auth_access_tokens",
    "auth_refresh_tokens",
    "workspace_members",
    "attachments",
)

__all__ = [
    "ATTACHMENT_STATES",
    "ENTITY_TYPES",
    "MEMBER_ROLES",
    "MODEL_TABLES",
    "OPERATIONS",
    "REQUEST_HASH_BYTES",
    "Attachment",
    "AuthAccessToken",
    "AuthRefreshToken",
    "AuthSession",
    "Base",
    "ChangeEvent",
    "Folder",
    "Note",
    "OpLedger",
    "User",
    "Workspace",
    "WorkspaceMember",
]
