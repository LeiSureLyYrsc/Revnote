"""``.resource/<key>`` blobs on top of the Phase 5 attachment service.

There is no second store and no second key scheme (D7): a resource is an
``attachments`` row, written by the same streaming upload path that enforces the
MIME allowlist, the byte cap and the re-read digest, and stored under the same
``ws/{workspace}/att/{id}/{nonce}`` key.

The only new thing is the *name*: a Joplin client addresses a resource blob as
``.resource/<32-hex key>``. ``joplin_id_map`` maps that key to the attachment
UUID, so the bridge never needs to embed a Joplin id in a storage key.

Deletion is deliberately a no-op. Phase 5 has no attachment deletion (D13), and
pretending otherwise would either delete bytes a note still references or report
a state the database does not hold. ``DELETE`` therefore answers ``204`` and the
object stays; the limitation is recorded in ``docs/phase6-joplin-bridge.md``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

from revnote_api.adapters.joplin.ids import jop_key_for_uuid
from revnote_api.adapters.joplin.serialize import serialize_resource
from revnote_api.services.attachments import AttachmentService, AttachmentView

__all__ = ["open_blob", "resource_item", "store_blob"]


def resource_item(
    *,
    attachment_id: uuid.UUID,
    jop_key: str,
    mime: str,
    file_name: str | None,
    byte_size: int | None,
    created_at: object,
    updated_at: object,
) -> str:
    """Serialized resource metadata item for a stored (or pending) attachment."""
    name = file_name or f"{jop_key}{_extension_for(mime)}"
    return serialize_resource(
        jop_key=jop_key,
        title=name,
        mime=mime,
        filename=name,
        size=int(byte_size or 0),
        created_time=created_at,  # type: ignore[arg-type]
        updated_time=updated_at,  # type: ignore[arg-type]
    )


def _extension_for(mime: str) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "application/pdf": ".pdf",
        "text/plain": ".txt",
    }.get(mime, "")


async def store_blob(
    service: AttachmentService,
    workspace_id: uuid.UUID,
    attachment_id: uuid.UUID,
    *,
    chunks: AsyncIterator[bytes],
    declared_content_type: str,
) -> object:
    """Stream one blob into the attachment state machine.

    ``declared_content_type`` is the MIME the *metadata item* declared, not the
    client's ``application/octet-stream``: the Phase 5 service compares the
    upload's content type against the row, and a Joplin client legitimately
    sends the generic type for a blob.
    """
    return await service.store_content(
        workspace_id,
        attachment_id,
        chunks=chunks,
        declared_content_type=declared_content_type,
    )


async def open_blob(
    service: AttachmentService, attachment_id: uuid.UUID, *, user_id: uuid.UUID
) -> tuple[AttachmentView, AsyncIterator[bytes]]:
    """Authorise and open one stored blob for the caller."""
    return await service.open_content(attachment_id, user_id=user_id)


def blob_item_name(jop_key: str) -> str:
    """The delta/list name of a resource blob (filtered out by the client)."""
    return f".resource/{jop_key}"


def key_of(attachment_id: uuid.UUID) -> str:
    """Deterministic key of a native attachment."""
    return jop_key_for_uuid(attachment_id)
