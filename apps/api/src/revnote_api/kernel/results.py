"""Immutable write results returned by the sync kernel.

A result is a value object, never an ORM entity: the caller gets exactly the
coordinates a successful operation produced and nothing it can accidentally
mutate (D2 keeps ``seq``/``rev`` as kernel integers; a future protocol layer
renders them as decimal strings).

Replays return an **identical** :class:`WriteResult`, rebuilt from the recorded
event — not from the live row, which may have moved on (§5). Any "was this a
replay?" diagnostics live outside this type.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

__all__ = ["WriteResult"]


@dataclass(frozen=True, slots=True)
class WriteResult:
    """The outcome of one accepted command (new write or replayed first result).

    ``workspace_id``/``op_id`` echo the command; ``entity_type``/``entity_id``
    say which object the event describes; ``op`` is the recorded operation;
    ``seq`` is the workspace cursor the event occupies; ``rev`` is the object's
    revision after this operation; ``created_at`` is the event's own timestamp.
    """

    workspace_id: uuid.UUID
    op_id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    op: str
    seq: int
    rev: int
    created_at: datetime

    def __repr__(self) -> str:
        # Timestamps only to microsecond precision, no content: safe to log (§11).
        return (
            f"WriteResult(entity_type={self.entity_type}, op={self.op}, "
            f"workspace_id={self.workspace_id}, entity_id={self.entity_id}, "
            f"op_id={self.op_id}, seq={self.seq}, rev={self.rev}, "
            f"created_at={self.created_at.isoformat()})"
        )
