"""Wire schema of ``GET .../tree`` and ``GET .../notes/{note_id}`` (spec §5).

Two read-only views, deliberately different in one field:

* :class:`TreeResponse` is **metadata only**. Folders carry their name and
  position; notes carry title, folder and revision - never ``body_md``. A tree
  read is how a client renders a navigator, so shipping bodies would multiply the
  payload by the size of the workspace for information the user is not looking at.
* :class:`NoteResponse` is one *active* note and **does** carry the body. A missing
  note and a tombstoned one are the same ``404 not_found`` (plan §12.2): telling
  them apart would let a client probe deleted ids.

Both reuse the counter/timestamp types of
:mod:`revnote_api.api.schemas.common`, so a sequence number is rendered exactly
once for the whole protocol. ``body_md`` is ``repr=False`` for the same reason as
in :mod:`revnote_api.api.schemas.sync_pull`: a repr reaches logs and tracebacks.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from revnote_api.api.schemas.common import DecimalString, UTCDateTime

__all__ = [
    "FolderMetaView",
    "NoteMetaView",
    "NoteResponse",
    "TreeResponse",
]


class _WireModel(BaseModel):
    """Strict, closed response model."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        validate_default=True,
    )


class FolderMetaView(_WireModel):
    """One active folder: name and position, no content."""

    entity_type: Literal["folder"] = "folder"
    id: uuid.UUID
    parent_id: uuid.UUID | None = None
    name: str
    rev: DecimalString
    updated_seq: DecimalString
    created_at: UTCDateTime
    updated_at: UTCDateTime


class NoteMetaView(_WireModel):
    """One active note's metadata. The body is intentionally absent."""

    entity_type: Literal["note"] = "note"
    id: uuid.UUID
    folder_id: uuid.UUID | None = None
    title: str
    rev: DecimalString
    updated_seq: DecimalString
    created_at: UTCDateTime
    updated_at: UTCDateTime


class TreeResponse(_WireModel):
    """Active folders and notes of one workspace, metadata only."""

    folders: list[FolderMetaView]
    notes: list[NoteMetaView]


class NoteResponse(_WireModel):
    """One active note, with its stored Markdown body and current revision."""

    entity_type: Literal["note"] = "note"
    id: uuid.UUID
    workspace_id: uuid.UUID
    folder_id: uuid.UUID | None = None
    title: str
    body_md: str = Field(repr=False)
    rev: DecimalString
    updated_seq: DecimalString
    created_at: UTCDateTime
    updated_at: UTCDateTime
