"""Wire schema of ``GET /workspaces/{workspace_id}/sync/pull`` (spec §4, D13/D14).

The page is one consistent snapshot: ``events`` is the ordered, **complete**
event window ``(since, ...]`` (history is never compacted - no
``superseded_by_seq``), and ``objects`` holds the *current* state of every
entity that window names, de-duplicated by ``(entity_type, id)``. An object may
therefore be newer than the event that introduced it; its ``rev``/``updated_seq``
are the true current values, not the event's.

Counter fields are decimal strings (:data:`~revnote_api.api.schemas.common.DecimalString`)
because PostgreSQL's ``BIGINT`` exceeds JavaScript's safe integer range; UUIDs
are strings; timestamps are RFC 3339 UTC with a ``Z``. ``body_md`` is declared
with ``repr=False``: a pydantic model's default repr prints field values, and a
note body must never appear in a log line or a traceback (plan §11.1).

The two helpers at the bottom render a model to the **exact** UTF-8 bytes
``JSONResponse`` would send. The pull and snapshot budgets are byte budgets, so
"does it fit" must be answered on the serialized form - escaped newlines, quotes
and control characters included - not on a character count.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from revnote_api.api.schemas.common import DecimalString, UTCDateTime

__all__ = [
    "EntityType",
    "FolderObject",
    "NoteObject",
    "ObjectList",
    "OpName",
    "PullEvent",
    "PullResponse",
    "SnapshotResponse",
    "dump_body",
    "dump_size",
]

EntityType = Literal["folder", "note"]
OpName = Literal["create", "update", "delete", "move"]


class _WireModel(BaseModel):
    """Strict, closed wire model (a response model never receives input)."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        validate_default=True,
    )


class PullEvent(_WireModel):
    """One immutable event of the page: what changed, when, and at which revision."""

    seq: DecimalString
    entity_type: EntityType
    entity_id: uuid.UUID
    op: OpName
    rev: DecimalString
    op_id: uuid.UUID
    created_at: UTCDateTime


class FolderObject(_WireModel):
    """Current folder state, possibly a tombstone (``deleted``/``deleted_at``)."""

    entity_type: Literal["folder"] = "folder"
    id: uuid.UUID
    workspace_id: uuid.UUID
    parent_id: uuid.UUID | None = None
    name: str
    rev: DecimalString
    updated_seq: DecimalString
    deleted: bool
    deleted_at: UTCDateTime | None = None
    created_at: UTCDateTime
    updated_at: UTCDateTime


class NoteObject(_WireModel):
    """Current note state, possibly a tombstone, always with the stored body.

    ``body_md`` is the one response field the protocol allows to carry note text,
    and it is excluded from ``repr`` so no log line or traceback can echo it.
    """

    entity_type: Literal["note"] = "note"
    id: uuid.UUID
    workspace_id: uuid.UUID
    folder_id: uuid.UUID | None = None
    title: str
    body_md: str = Field(repr=False)
    rev: DecimalString
    updated_seq: DecimalString
    deleted: bool
    deleted_at: UTCDateTime | None = None
    created_at: UTCDateTime
    updated_at: UTCDateTime


#: One current object, discriminated by ``entity_type``.
ObjectList = Annotated[
    list[Annotated[FolderObject | NoteObject, Field(discriminator="entity_type")]],
    Field(description="Current folder/note rows for the events on this page."),
]


class PullResponse(_WireModel):
    """One consistent pull page plus the watermarks it was read at.

    ``next_cursor`` is the last returned event's ``seq`` and equals ``since`` for
    an empty page, so a client can store it unconditionally. ``has_more`` was
    decided inside the same snapshot, so following it can never skip a sequence.
    """

    events: list[PullEvent]
    objects: ObjectList
    next_cursor: DecimalString
    has_more: bool
    last_seq: DecimalString
    min_retained_seq: DecimalString


class SnapshotResponse(_WireModel):
    """A complete bootstrap: every active object at one ``REPEATABLE READ`` view.

    There is no cursor and no page: the client pulls ``since=as_of_seq`` next.
    Oversize is refused as a whole (409 ``snapshot_too_large``), so a response
    that exists is always complete for its ``as_of_seq``.
    """

    as_of_seq: DecimalString
    min_retained_seq: DecimalString
    objects: ObjectList


#: The separators ``JSONResponse`` uses: no whitespace at all.
_SEPARATORS: Final[tuple[str, str]] = (",", ":")


def _payload(model: BaseModel) -> Any:
    """The model as JSON-ready primitives (our ``UTCDateTime`` serializer applies)."""
    return model.model_dump(mode="json")


def dump_body(model: BaseModel) -> bytes:
    """The exact body bytes for ``model`` - the same encoder ``JSONResponse`` uses."""
    return json.dumps(
        _payload(model),
        ensure_ascii=False,
        allow_nan=False,
        indent=None,
        separators=_SEPARATORS,
    ).encode("utf-8")


def dump_size(model: BaseModel) -> int:
    """UTF-8 byte length of :func:`dump_body` (escapes and multi-byte text included)."""
    return len(dump_body(model))
