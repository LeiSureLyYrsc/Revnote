"""``joplin_locks`` — the bridge's sync locks, isolated from the kernel.

Joplin's lock protocol has two kinds (D9):

``sync``
    Shared. Several clients may hold it at once; it is what tells a client that
    *someone* is syncing, so an exclusive acquisition must wait.
``exclusive``
    Single holder. Acquiring it fails while any other client holds any lock.

A lock is **not** a workspace operation: it is never written to
``change_events``, never moves ``workspaces.last_seq`` and never appears in a
pull page. Expiry is a property of ``updated_at`` plus the configured TTL, so a
crashed client cannot wedge the workspace forever (the row is filtered and
reclaimed on the next acquire).

The identity is ``(workspace_id, lock_type, client_type, client_id)`` because
that is exactly what the client puts in the release path
(``<lockType>_<clientType>_<clientId>``). ``id`` exists only because every
table in this schema has a primary key; the wire id is derived.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base

#: Joplin ``LockType``: 1 = sync, 2 = exclusive.
JOPLIN_LOCK_TYPES: tuple[str, ...] = ("sync", "exclusive")

#: Joplin ``LockClientType``: 1 = desktop, 2 = mobile, 3 = cli.
JOPLIN_CLIENT_TYPES: tuple[int, ...] = (1, 2, 3)

#: Bound on a client-supplied id (it travels in a path on release).
CLIENT_ID_MAX_BYTES = 128


class JoplinLock(Base):
    """One held lock; rows expire by ``updated_at`` and are never events."""

    __tablename__ = "joplin_locks"
    __table_args__ = (
        sa.PrimaryKeyConstraint("id", name="pk_joplin_locks"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_joplin_locks_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        # One client holds at most one lock of each kind.
        sa.UniqueConstraint(
            "workspace_id",
            "lock_type",
            "client_type",
            "client_id",
            name="uq_joplin_locks_workspace_id_lock_type_client_type_client_id",
        ),
        sa.CheckConstraint("lock_type IN ('sync', 'exclusive')", name="lock_type_known"),
        sa.CheckConstraint("client_type IN (1, 2, 3)", name="client_type_known"),
        sa.CheckConstraint(
            f"octet_length(client_id) BETWEEN 1 AND {CLIENT_ID_MAX_BYTES}",
            name="client_id_len",
        ),
        # The scan the service makes on every acquire: "who holds what here?".
        sa.Index("ix_joplin_locks_workspace_id_updated_at", "workspace_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    lock_type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    client_type: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    client_id: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"JoplinLock(workspace_id={self.workspace_id!r}, "
            f"lock_type={self.lock_type!r}, client_type={self.client_type!r})"
        )


__all__ = [
    "CLIENT_ID_MAX_BYTES",
    "JOPLIN_CLIENT_TYPES",
    "JOPLIN_LOCK_TYPES",
    "JoplinLock",
]
