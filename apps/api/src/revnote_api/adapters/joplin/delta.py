"""``change_events`` -> Joplin delta: compression and cursor rules.

The kernel records one event per successful operation and never rewrites them.
Joplin's delta protocol is a *compressed* view of the same facts, so this module
is a pure function over a page of events; it never writes and never touches
``workspaces.last_seq``.

Compression (the table is Joplin's own, and the unit test is table-driven):

===============  =========  ============================================
sequence         result     why
===============  =========  ============================================
update - update  update     one notification is enough
create - update  create     the client only needs the latest version
create - delete  (nothing)  the client never knew the item
update - delete  delete     the item must disappear locally
===============  =========  ============================================

A ``move`` is a create-or-update on the wire: the client re-fetches the item
and sees its new ``parent_id``. It is *not* a separate Joplin notion.

The cursor is the raw last seq of the page, including events that compression
dropped. Returning the last *emitted* seq instead would re-read the same
dropped events forever.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal

__all__ = [
    "CHANGE_CREATE",
    "CHANGE_DELETE",
    "CHANGE_UPDATE",
    "DeltaChange",
    "RawEvent",
    "compress_events",
]

#: Joplin's ``ChangeType`` values.
CHANGE_CREATE: Final[int] = 1
CHANGE_UPDATE: Final[int] = 2
CHANGE_DELETE: Final[int] = 3

_ChangeKind = Literal["note", "folder"]


@dataclass(frozen=True, slots=True)
class RawEvent:
    """One ``change_events`` row, as read by the delta query."""

    seq: int
    entity_type: str
    entity_id: uuid.UUID
    op: str
    rev: int


@dataclass(frozen=True, slots=True)
class DeltaChange:
    """One compressed delta entry.

    ``seq`` is the last event the entry consumed: it is used for ordering and
    for the ``updated_time`` fallback, never as the response cursor.
    """

    seq: int
    entity_type: str
    entity_id: uuid.UUID
    change_type: int

    @property
    def is_deleted(self) -> bool:
        return self.change_type == CHANGE_DELETE


def _compress_one(ops: list[str]) -> int | None:
    """Return the Joplin change type for one entity's op sequence, or ``None``."""
    first = ops[0]
    deleted = "delete" in ops
    if deleted:
        # create -> delete means the client never saw it; anything else is a
        # real deletion (an update or a move was observed before it).
        return None if first == "create" else CHANGE_DELETE
    if first == "create":
        return CHANGE_CREATE
    return CHANGE_UPDATE


def compress_events(events: list[RawEvent]) -> list[DeltaChange]:
    """Compress one ascending page of events into Joplin delta entries.

    Order is preserved by the last seq each entity consumed, so a create that
    precedes a delete on the same page stays in front of it.
    """
    order: list[tuple[str, uuid.UUID]] = []
    grouped: dict[tuple[str, uuid.UUID], list[RawEvent]] = {}

    for event in events:
        key = (event.entity_type, event.entity_id)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(event)

    compressed: list[DeltaChange] = []
    for key in order:
        group = grouped[key]
        change_type = _compress_one([event.op for event in group])
        if change_type is None:
            continue
        compressed.append(
            DeltaChange(
                seq=group[-1].seq,
                entity_type=key[0],
                entity_id=key[1],
                change_type=change_type,
            )
        )

    compressed.sort(key=lambda change: change.seq)
    return compressed
