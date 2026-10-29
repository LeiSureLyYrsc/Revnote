"""``GET .../tree`` and ``GET .../notes/{note_id}`` - the read-only views.

Both are single statements in :mod:`revnote_api.services.sync_read`; this module
parses the path, authorises the workspace, and renders the rows. Nothing here can
write: there is no REST write wrapper in v1 (D19), and these endpoints exist so a
client can render a navigator and open one note without pulling a whole page.

Two contract points are enforced by what the service *asks for*, not by a filter
added afterwards:

* ``tree`` reads active rows only and never selects ``body_md``;
* ``note`` reads one active note inside the path workspace, and a row that is
  missing **or** tombstoned produces the same ``404 not_found`` - a client must not
  be able to distinguish "never existed" from "deleted" (plan §12.2).

Both responses carry ``Cache-Control: no-store``. A note body is an authorised
response, but it is still workspace-private content: no shared cache may retain it.

The service itself is injected through
:data:`~revnote_api.api.routes.sync_pull.ReadServiceDep`, the single dependency the
pull/snapshot endpoints define, so a whole test app overrides one callable and no
second copy of the factory exists.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from revnote_api.api.dependencies import AuthContext, require_workspace_member
from revnote_api.api.errors import ApiError
from revnote_api.api.routes.sync_pull import ReadServiceDep
from revnote_api.api.schemas.common import render_decimal
from revnote_api.api.schemas.read import (
    FolderMetaView,
    NoteMetaView,
    NoteResponse,
    TreeResponse,
)
from revnote_api.services.sync_read import SyncReadError, TreeView

__all__ = ["router"]

router = APIRouter(tags=["read"])


def _translate(error: SyncReadError) -> ApiError:
    """Carry the service's safe code and scalar details onto the wire."""
    details = {
        key: value
        for key, value in error.details.items()
        if isinstance(value, (str, int, float, bool))
    }
    return ApiError(error.code, details=details)


def _tree_model(view: TreeView) -> TreeResponse:
    return TreeResponse(
        folders=[
            FolderMetaView(
                id=folder.id,
                parent_id=folder.parent_id,
                name=folder.name,
                rev=render_decimal(folder.rev),
                updated_seq=render_decimal(folder.updated_seq),
                created_at=folder.created_at,
                updated_at=folder.updated_at,
            )
            for folder in view.folders
        ],
        notes=[
            NoteMetaView(
                id=note.id,
                folder_id=note.folder_id,
                title=note.title,
                rev=render_decimal(note.rev),
                updated_seq=render_decimal(note.updated_seq),
                created_at=note.created_at,
                updated_at=note.updated_at,
            )
            for note in view.notes
        ],
    )


@router.get(
    "/workspaces/{workspace_id}/tree",
    response_model=TreeResponse,
    summary="Active folders and note metadata (no bodies)",
    responses={200: {"description": "Names, positions and revisions only."}},
)
async def tree(
    workspace_id: uuid.UUID,
    context: Annotated[AuthContext, Depends(require_workspace_member)],
    service: ReadServiceDep,
    response: Response,
) -> TreeResponse:
    """Metadata of every active folder and note in the workspace."""
    del context  # authorisation side effect only

    try:
        view = await service.tree(workspace_id)
    except SyncReadError as error:
        raise _translate(error) from None

    response.headers["Cache-Control"] = "no-store"
    return _tree_model(view)


@router.get(
    "/workspaces/{workspace_id}/notes/{note_id}",
    response_model=NoteResponse,
    summary="One active note with its body",
    responses={404: {"description": "No such active note in this workspace."}},
)
async def note(
    workspace_id: uuid.UUID,
    note_id: uuid.UUID,
    context: Annotated[AuthContext, Depends(require_workspace_member)],
    service: ReadServiceDep,
    response: Response,
) -> NoteResponse:
    """Read one note; a tombstone and a missing id are the same ``not_found``."""
    del context

    try:
        found = await service.note(workspace_id, note_id)
    except SyncReadError as error:
        raise _translate(error) from None

    # An authorised note body is a legitimate response, but it must never be held
    # by a cache that another principal could read.
    response.headers["Cache-Control"] = "no-store"
    return NoteResponse(
        id=found.id,
        workspace_id=found.workspace_id,
        folder_id=found.folder_id,
        title=found.title,
        body_md=found.body_md,
        rev=render_decimal(found.rev),
        updated_seq=render_decimal(found.updated_seq),
        created_at=found.created_at,
        updated_at=found.updated_at,
    )
