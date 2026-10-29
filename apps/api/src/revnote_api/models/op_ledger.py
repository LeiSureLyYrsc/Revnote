"""``op_ledger`` — the idempotency record for successfully applied operations.

Rules encoded here (D4/D5):

* ``(workspace_id, op_id)`` is the PK **and** non-deferrable, so a duplicate op is
  rejected by the database itself, not by a read-then-write check.
* ``request_hash`` is the SHA-256 of the canonical command JSON: exactly 32 bytes.
* ``result_seq`` is NOT NULL and immediately (non-deferrable) checked against
  ``change_events(workspace_id, seq)``: a ledger row can never claim a sequence
  that does not exist. Replay rebuilds the result from that immutable event.
* The ledger stores **only successful** outcomes — no pending/status column, no
  failure ledger, no current-entity snapshot.
"""

from __future__ import annotations

import datetime
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from revnote_api.db.base import Base

#: ``request_hash`` length in bytes (SHA-256 digest).
REQUEST_HASH_BYTES = 32


class OpLedger(Base):
    """Maps ``(workspace_id, op_id)`` to the event that first applied it."""

    __tablename__ = "op_ledger"
    __table_args__ = (
        # Composite PK; deliberately NOT deferrable — uniqueness is the dam.
        sa.PrimaryKeyConstraint("workspace_id", "op_id", name="pk_op_ledger"),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name="fk_op_ledger_workspace_id_workspaces",
            ondelete="RESTRICT",
        ),
        # Immediate FK: the referenced event must already exist when the ledger
        # row is inserted (unlike the deferred object -> event FK).
        sa.ForeignKeyConstraint(
            ["workspace_id", "result_seq"],
            ["change_events.workspace_id", "change_events.seq"],
            name="fk_op_ledger_workspace_id_result_seq_change_events",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id", "result_seq", name="uq_op_ledger_workspace_id_result_seq"
        ),
        sa.CheckConstraint(
            f"octet_length(request_hash) = {REQUEST_HASH_BYTES}",
            name="request_hash_len",
        ),
        sa.CheckConstraint("result_seq > 0", name="result_seq_positive"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    op_id: Mapped[uuid.UUID] = mapped_column(postgresql.UUID(as_uuid=True), nullable=False)
    #: SHA-256 of the canonical command JSON (32 bytes).
    request_hash: Mapped[bytes] = mapped_column(postgresql.BYTEA(), nullable=False)
    #: Sequence of the event that stored this operation's result.
    result_seq: Mapped[int] = mapped_column(sa.BigInteger(), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        postgresql.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return (
            f"OpLedger(workspace_id={self.workspace_id!r}, op_id={self.op_id!r}, "
            f"result_seq={self.result_seq!r})"
        )
