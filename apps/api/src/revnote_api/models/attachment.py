"""``attachments`` — uploaded bytes that belong to a workspace, not to a note.

An attachment is **not** a kernel entity (D3): it has no ``rev``, no
``updated_seq``, no ``seq``, no ``change_events`` row and no ``note_id`` column.
The only reference to an attachment lives inside note Markdown as a relative URL
(D7), which keeps the sync protocol untouched: pull/snapshot never mention this
table and ``workspaces.last_seq`` never moves because of an upload.

State machine (plan §6.4)::

    pending --(upload verified)--> ready
    pending --(GC past the grace window)--> deleted
    ready   --(never automatic)--> deleted

``content_hash`` is the SHA-256 of the bytes **as verified by re-reading the
object**, so ``ready`` is defined as "a digest exists": the CHECK
``(state = 'ready') = (content_hash IS NOT NULL)`` makes a ``ready`` row without a
digest (or a ``pending`` row with one) impossible, not merely discouraged.

``storage_key`` is immutable (D5) and unique: ``ws/{workspace}/att/{id}/{nonce}``,
always four path segments, UUIDs canonical lowercase. ``upload_nonce`` is written
once at creation and is the CAS predicate that lets exactly one concurrent upload
win (see :mod:`revnote_api.services.attachments`).

``file_name`` is user content. It is deliberately absent from ``__repr__`` (a repr
reaches logs and tracebacks) and it is never used to build a storage key or a
path, so a hostile name cannot influence where bytes are written.

``expires_at`` is **derived**, not stored: a ``pending`` row expires at
``created_at + ATTACHMENT_PENDING_GRACE_S``. Keeping one source of truth means
the GC predicate and the upload deadline cannot drift apart.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base

#: The three legal states, in lifecycle order. Mirrors the ``state`` CHECK.
ATTACHMENT_STATES: tuple[str, ...] = ("pending", "ready", "deleted")

#: SHA-256 digest length stored in ``content_hash`` (``BYTEA``, 32 octets).
CONTENT_HASH_BYTES = 32

#: Bounds on the two text columns that appear in a URL or a storage key.
MIME_MIN_BYTES = 3
MIME_MAX_BYTES = 127
STORAGE_KEY_MIN_BYTES = 8
STORAGE_KEY_MAX_BYTES = 512


class Attachment(Base):
    """One uploaded object and its state; never part of the sync stream."""

    __tablename__ = "attachments"
    __table_args__ = (
        sa.PrimaryKeyConstraint("id", name="pk_attachments"),
        # RESTRICT: a workspace with attachments cannot be deleted out from under
        # them, and nothing here cascades (the model package declares no cascade).
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_attachments_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        # The object key is the identity of the bytes: one key, one row (D5).
        sa.UniqueConstraint("storage_key", name="uq_attachments_storage_key"),
        # Short names; the convention prefixes ``ck_attachments_``.
        sa.CheckConstraint(
            "state IN ('pending', 'ready', 'deleted')",
            name="state_known",
        ),
        sa.CheckConstraint(
            f"octet_length(mime) BETWEEN {MIME_MIN_BYTES} AND {MIME_MAX_BYTES}",
            name="mime_len",
        ),
        # NULL = the client did not declare a size. 0 is never a legal size.
        sa.CheckConstraint("byte_size IS NULL OR byte_size > 0", name="byte_size_positive"),
        sa.CheckConstraint(
            f"content_hash IS NULL OR octet_length(content_hash) = {CONTENT_HASH_BYTES}",
            name="content_hash_len",
        ),
        sa.CheckConstraint(
            f"octet_length(storage_key) BETWEEN {STORAGE_KEY_MIN_BYTES} "
            f"AND {STORAGE_KEY_MAX_BYTES}",
            name="storage_key_len",
        ),
        # The state machine's single invariant: ready <=> a verified digest exists.
        sa.CheckConstraint(
            "(state = 'ready') = (content_hash IS NOT NULL)",
            name="ready_iff_hash",
        ),
        # GC scan ("expired pending rows of this workspace") and the per-workspace
        # state listing.
        sa.Index("ix_attachments_workspace_id_state", "workspace_id", "state"),
        sa.Index("ix_attachments_workspace_id_created_at", "workspace_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    workspace_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    #: ``pending`` / ``ready`` / ``deleted`` (CHECK-enforced).
    state: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    #: Declared MIME; the upload's ``Content-Type`` must equal it.
    mime: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    #: Bytes actually stored; NULL until the upload has been verified.
    byte_size: Mapped[int | None] = mapped_column(sa.BigInteger(), nullable=True)
    #: SHA-256 of the stored bytes (32 octets). NULL unless ``state = 'ready'``.
    content_hash: Mapped[bytes | None] = mapped_column(postgresql.BYTEA(), nullable=True)
    #: Immutable object key: ``ws/{workspace_id}/att/{id}/{upload_nonce}``.
    storage_key: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    #: Per-row upload nonce: the CAS predicate that picks one winning upload.
    upload_nonce: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    #: User content. Never logged, never in a repr, never part of a key.
    file_name: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    #: Creation time; ``created_at + grace`` is when a ``pending`` row expires.
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )
    #: When the first upload attempt started (NULL until a PUT was accepted).
    upload_started_at: Mapped[datetime.datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=True
    )
    #: When the bytes were verified and the row became ``ready``.
    ready_at: Mapped[datetime.datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime.datetime | None] = mapped_column(
        postgresql.TIMESTAMP(timezone=True), nullable=True
    )
    #: Application-owned vocabulary (``expired``, ``orphaned``, ...). No CHECK: a
    #: new reason must not turn a GC run into a failed constraint.
    delete_reason: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        # ``file_name`` and ``content_hash`` are deliberately absent: a repr ends
        # up in logs and tracebacks, and both are user- or content-derived.
        return (
            f"Attachment(id={self.id!r}, workspace_id={self.workspace_id!r}, "
            f"state={self.state!r}, byte_size={self.byte_size!r})"
        )
