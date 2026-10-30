"""The Joplin Server compatibility surface (mounted only when enabled).

Registered under ``/api`` — the paths a Joplin client builds itself:
``POST /api/sessions``, ``/api/items/root:/<name>:/content``,
``.../delta``, ``.../children``, ``/api/locks`` and ``/api/batch_items``.
The native contract keeps living under ``/api/v1`` and is untouched.

Translation rules enforced here:

* **the bridge is a parallel face** — every write goes through the kernel
  (``SyncWriteService``), so seq allocation, the ledger and the invariants are
  the ones Phase 2 proved;
* **Joplin is last-write-wins** — a client sends no ``base_rev``, so the bridge
  reads the current revision and uses it. A concurrent native writer can still
  win and produce ``409``; the client re-reads and retries, which is exactly
  what the kernel's CAS is for;
* **resources are attachments** — no second store, no presigning;
* **errors use the Joplin envelope** — see ``errors.py`` — and never echo a
  lower layer's message.

The compatibility routes are hidden from the native OpenAPI document on
purpose: that document is the v1 contract, and publishing a second, differently
shaped error model in it would invite a generated client to use it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from revnote_api.adapters.joplin import repository
from revnote_api.adapters.joplin.delta import RawEvent, compress_events
from revnote_api.adapters.joplin.errors import (
    JoplinProtocolError,
    joplin_response,
    translate_native,
)
from revnote_api.adapters.joplin.ids import is_jop_key, jop_key_for_uuid
from revnote_api.adapters.joplin.locks import (
    LOCK_EXCLUSIVE,
    LOCK_SYNC,
    acquire_lock,
    list_locks,
    parse_lock_id,
    release_lock,
    serialize_lock,
)
from revnote_api.adapters.joplin.resources import open_blob, resource_item, store_blob
from revnote_api.adapters.joplin.serialize import (
    TYPE_FOLDER,
    TYPE_NOTE,
    TYPE_RESOURCE,
    JoplinItem,
    JoplinItemError,
    parse_item,
    serialize_folder,
    serialize_note,
)
from revnote_api.adapters.joplin.session import (
    BridgeContext,
    create_session,
    resolve_session,
    revoke_session,
)
from revnote_api.api.dependencies import get_auth_service
from revnote_api.api.routes.attachments import build_store
from revnote_api.config import Settings
from revnote_api.db.session import get_sessionmaker
from revnote_api.kernel.commands import (
    CreateFolder,
    CreateNote,
    DeleteFolder,
    DeleteNote,
    MoveFolder,
    MoveNote,
    UpdateFolder,
    UpdateNote,
)
from revnote_api.kernel.errors import SyncKernelError
from revnote_api.kernel.ids import new_op_id
from revnote_api.services.attachments import (
    AttachmentError,
    AttachmentPolicy,
    AttachmentService,
)
from revnote_api.services.auth import AuthError
from revnote_api.services.sync_push_adapter import code_for_kernel_error
from revnote_api.services.sync_write import SyncWriteService

__all__ = ["install", "router"]

router = APIRouter(prefix="/api", tags=["joplin"], include_in_schema=False)

#: A Joplin item body is a note (bounded by the native 256 KiB body budget) plus
#: property lines; the same ceiling the push endpoint uses is generous.
_ITEM_BODY_MAX_BYTES = 262_144
_SESSION_BODY_MAX_BYTES = 8_192
_BATCH_BODY_MAX_BYTES = 1_048_576

_NO_STORE = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


# --------------------------------------------------------------------------- #
# app wiring
# --------------------------------------------------------------------------- #


def install(app: Any) -> None:
    """Register the Joplin error envelope on an enabled app."""
    app.add_exception_handler(JoplinProtocolError, _on_protocol_error)


async def _on_protocol_error(request: Request, exc: JoplinProtocolError) -> JSONResponse:
    del request
    return joplin_response(exc)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _factory(request: Request):
    return get_sessionmaker(_settings(request))


def _attachment_service(request: Request) -> AttachmentService:
    settings = _settings(request)
    return AttachmentService(
        get_sessionmaker(settings),
        build_store(settings),
        AttachmentPolicy.from_settings(settings),
    )


async def _json_body(request: Request, *, max_bytes: int) -> dict[str, Any]:
    raw = await request.body()
    if len(raw) > max_bytes:
        raise JoplinProtocolError("too_large")
    try:
        payload = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError) as exc:
        raise JoplinProtocolError("bad_request") from exc
    if not isinstance(payload, dict):
        raise JoplinProtocolError("bad_request")
    return payload


def _header_token(request: Request) -> str | None:
    token = request.headers.get("X-API-AUTH", "").strip()
    if token:
        return token
    authorization = request.headers.get("Authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip() or None
    return None


async def bridge_context(request: Request) -> BridgeContext:
    """Dependency: resolve ``X-API-AUTH`` (or a bearer token) to a context."""
    return await resolve_session(_factory(request), _header_token(request))


BridgeContextDep = Annotated[BridgeContext, Depends(bridge_context)]


def _millis(value: object) -> int | None:
    if value is None:
        return None
    timestamp = getattr(value, "timestamp", None)
    if timestamp is None:  # pragma: no cover - always a datetime
        return None
    return int(timestamp() * 1000)


def _item_json(
    *,
    jop_key: str,
    kind: str,
    parent_key: str,
    created_at: object,
    updated_at: object,
    change_type: int | None = None,
) -> dict[str, Any]:
    jop_type = {"note": TYPE_NOTE, "folder": TYPE_FOLDER, "attachment": TYPE_RESOURCE}[kind]
    payload: dict[str, Any] = {
        "id": jop_key,
        "name": f"{jop_key}.md",
        "jop_id": jop_key,
        "jop_type": jop_type,
        "jop_parent_id": parent_key,
        "created_time": _millis(created_at),
        "updated_time": _millis(updated_at),
        "jop_updated_time": _millis(updated_at),
    }
    if change_type is not None:
        payload["type"] = change_type
    return payload


def _name_from_raw(raw: str) -> str:
    """Strip the trailing ``:`` and any ``/*`` wildcard from a route path param.

    The route's own suffix (``/content``, ``/delta``, ``/children``) is *outside*
    the captured parameter, so it is not part of ``raw``.
    """
    body = raw.rstrip("/")
    if body.endswith(":"):
        body = body[:-1]
    if body.endswith("/*"):
        body = body[:-2]
    # Resource blobs are addressed as ".resource/<key>" and the prefix is part of
    # the name; a note/folder lives at the root, so only its final segment counts.
    marker = ".resource/"
    index = body.find(marker)
    if index >= 0:
        return body[index:]
    # A sync-target subpath ("Apps/Joplin/xxx.md") is accepted; items live at the
    # root of the workspace, so only the final segment names the item.
    return body.rsplit("/", 1)[-1]


def _require_item_name(name: str) -> None:
    if name.startswith(".resource/"):
        key = name[len(".resource/") :]
    elif name.endswith(".md"):
        key = name[: -len(".md")]
    else:
        raise JoplinProtocolError("bad_request")
    if not is_jop_key(key):
        raise JoplinProtocolError("bad_request")


async def _translate_call(call: Any) -> Any:
    """Run one service call and map its error vocabulary."""
    try:
        return await call
    except (AttachmentError, AuthError) as exc:
        raise translate_native(exc.code) from exc
    except SyncKernelError as exc:
        raise translate_native(code_for_kernel_error(exc)) from exc


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #


@router.post("/sessions")
async def post_session(request: Request) -> Response:
    """Log in with email/password and mint one bridge session token."""
    payload = await _json_body(request, max_bytes=_SESSION_BODY_MAX_BYTES)
    email = payload.get("email")
    password = payload.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        raise JoplinProtocolError("bad_request")

    platform = payload.get("platform")
    version = payload.get("version")
    client_name = (
        " ".join(str(part) for part in (platform, version) if isinstance(part, (str, int)))[:128]
        or None
    )

    token, user_id = await create_session(
        _factory(request),
        get_auth_service(request),
        email=email,
        password=password,
        ttl_s=int(_settings(request).joplin_session_ttl_s),
        client_name=client_name,
    )
    return JSONResponse(
        status_code=200,
        content={"id": token, "user_id": str(user_id)},
        headers=_NO_STORE,
    )


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, request: Request) -> Response:
    """Log out; idempotent and silent about unknown tokens."""
    await revoke_session(_factory(request), session_id)
    return Response(status_code=204, headers=_NO_STORE)


# --------------------------------------------------------------------------- #
# items: reads
# --------------------------------------------------------------------------- #


async def _load_item(request: Request, ctx: BridgeContext, name: str) -> tuple[str, uuid.UUID, Any]:
    """Resolve ``<key>.md`` to ``(kind, entity_id, row)`` or raise ``not_found``."""
    key = name[: -len(".md")] if name.endswith(".md") else name
    factory = _factory(request)
    async with factory() as session:
        resolved = await repository.resolve_mapping(session, ctx.workspace_id, key)
        if resolved is None:
            raise JoplinProtocolError("not_found")
        kind, entity_id = resolved
        if kind == "note":
            row = await repository.load_note(session, ctx.workspace_id, entity_id)
        elif kind == "folder":
            row = await repository.load_folder(session, ctx.workspace_id, entity_id)
        else:
            row = await repository.load_attachment(session, ctx.workspace_id, entity_id)
    if row is None or getattr(row, "deleted_at", None) is not None:
        raise JoplinProtocolError("not_found")
    return kind, entity_id, row


async def _parent_key(request: Request, ctx: BridgeContext, parent_id: uuid.UUID | None) -> str:
    if parent_id is None:
        return ""
    async with _factory(request)() as session:
        return await repository.mapping_key(session, ctx.workspace_id, "folder", parent_id)


@router.get("/items/root:/{raw:path}/content")
async def get_item_content(raw: str, request: Request, ctx: BridgeContextDep) -> Response:
    """Serialized item text for ``.md`` items; bytes for ``.resource/`` blobs."""
    name = _name_from_raw(raw)
    _require_item_name(name)

    if name.startswith(".resource/"):
        key = name[len(".resource/") :]
        factory = _factory(request)
        async with factory() as session:
            resolved = await repository.resolve_mapping(session, ctx.workspace_id, key)
        if resolved is None or resolved[0] != "attachment":
            raise JoplinProtocolError("not_found")
        view, chunks = await _translate_call(
            open_blob(_attachment_service(request), resolved[1], user_id=ctx.user_id)
        )
        headers = dict(_NO_STORE)
        headers["Content-Type"] = view.mime
        return StreamingResponse(chunks, media_type=view.mime, headers=headers)

    kind, _entity_id, row = await _load_item(request, ctx, name)
    key = name[: -len(".md")]

    if kind == "note":
        content = serialize_note(
            jop_key=key,
            title=row.title,
            body=row.body_md,
            parent_key=await _parent_key(request, ctx, row.folder_id),
            created_time=row.created_at,
            updated_time=row.updated_at,
        )
    elif kind == "folder":
        content = serialize_folder(
            jop_key=key,
            title=row.name,
            parent_key=await _parent_key(request, ctx, row.parent_id),
            created_time=row.created_at,
            updated_time=row.updated_at,
        )
    else:
        content = resource_item(
            attachment_id=row.id,
            jop_key=key,
            mime=row.mime,
            file_name=row.file_name,
            byte_size=row.byte_size,
            created_at=row.created_at,
            updated_at=row.ready_at or row.created_at,
        )
    return Response(content=content, media_type="text/plain; charset=utf-8", headers=_NO_STORE)


@router.get("/items/root:/{raw:path}/delta")
async def get_delta(raw: str, request: Request, ctx: BridgeContextDep) -> Response:
    """Compressed change feed with an opaque (decimal seq) cursor."""
    name = _name_from_raw(raw)
    if name not in ("", ".", "root"):
        _require_item_name(name)

    settings = _settings(request)
    cursor_raw = request.query_params.get("cursor")
    try:
        cursor = 0 if cursor_raw in (None, "") else int(cursor_raw)
    except (TypeError, ValueError) as exc:
        raise JoplinProtocolError("resync_required") from exc
    if cursor < 0:
        raise JoplinProtocolError("resync_required")

    raw_limit = request.query_params.get("limit")
    try:
        limit = int(raw_limit) if raw_limit else settings.joplin_delta_default_limit
    except (TypeError, ValueError) as exc:
        raise JoplinProtocolError("bad_request") from exc
    limit = max(1, min(limit, settings.joplin_delta_max_limit))

    factory = _factory(request)
    async with factory() as session:
        last_seq = (
            await session.execute(
                sa.text("SELECT last_seq FROM workspaces WHERE id = :w"),
                {"w": ctx.workspace_id},
            )
        ).scalar_one_or_none()
        if last_seq is None:
            raise JoplinProtocolError("not_found")
        if cursor > int(last_seq):
            raise JoplinProtocolError("resync_required")

        rows = (
            await session.execute(
                sa.text(
                    "SELECT seq, entity_type, entity_id, op, rev "
                    "  FROM change_events "
                    " WHERE workspace_id = :w AND seq > :cursor "
                    " ORDER BY seq LIMIT :limit"
                ),
                {"w": ctx.workspace_id, "cursor": cursor, "limit": limit + 1},
            )
        ).all()

        page = rows[:limit]
        has_more = len(rows) > limit
        next_cursor = int(page[-1][0]) if page else cursor

        events = [
            RawEvent(
                seq=int(row[0]),
                entity_type=str(row[1]),
                entity_id=row[2],
                op=str(row[3]),
                rev=int(row[4]),
            )
            for row in page
        ]
        changes = compress_events(events)

        wanted = [(change.entity_type, change.entity_id) for change in changes]
        metas = await repository.item_metas(session, ctx.workspace_id, wanted)

        items: list[dict[str, Any]] = []
        for change in changes:
            meta = metas.get((change.entity_type, change.entity_id))
            jop_key = meta.jop_key if meta is not None else jop_key_for_uuid(change.entity_id)
            parent_key = ""
            if meta is not None and meta.parent_id is not None:
                parent_key = await repository.mapping_key(
                    session, ctx.workspace_id, "folder", meta.parent_id
                )
            items.append(
                {
                    "id": jop_key,
                    "item_id": jop_key,
                    "item_name": f"{jop_key}.md",
                    "type": change.change_type,
                    "jop_type": TYPE_NOTE if change.entity_type == "note" else TYPE_FOLDER,
                    "jop_parent_id": parent_key,
                    "jop_updated_time": _millis(meta.updated_at) if meta else None,
                    "updated_time": _millis(meta.updated_at) if meta else None,
                }
            )

    return JSONResponse(
        status_code=200,
        content={"items": items, "cursor": str(next_cursor), "has_more": has_more},
        headers=_NO_STORE,
    )


@router.get("/items/root:/{raw:path}/children")
async def get_children(raw: str, request: Request, ctx: BridgeContextDep) -> Response:
    """List every item at the target root (the client uses this rarely)."""
    _name_from_raw(raw)  # shape check only; the workspace is flat

    factory = _factory(request)
    async with factory() as session:
        metas = await repository.list_items(session, ctx.workspace_id)

        items: list[dict[str, Any]] = []
        for meta in metas:
            parent_key = ""
            if meta.parent_id is not None:
                parent_key = await repository.mapping_key(
                    session, ctx.workspace_id, "folder", meta.parent_id
                )
            items.append(
                _item_json(
                    jop_key=meta.jop_key,
                    kind=meta.kind,
                    parent_key=parent_key,
                    created_at=meta.created_at,
                    updated_at=meta.updated_at,
                )
            )
    return JSONResponse(
        status_code=200, content={"items": items, "has_more": False}, headers=_NO_STORE
    )


@router.get("/items/root:/{raw:path}")
async def get_item(raw: str, request: Request, ctx: BridgeContextDep) -> Response:
    """Stat one item (the client's ``stat``; a 404 means "not on the server")."""
    name = _name_from_raw(raw)
    _require_item_name(name)
    if name.startswith(".resource/"):
        raise JoplinProtocolError("bad_request")

    kind, _entity_id, row = await _load_item(request, ctx, name)
    parent_id = row.parent_id if kind == "folder" else getattr(row, "folder_id", None)
    return JSONResponse(
        status_code=200,
        content=_item_json(
            jop_key=name[: -len(".md")],
            kind=kind,
            parent_key=await _parent_key(request, ctx, parent_id),
            created_at=row.created_at,
            updated_at=getattr(row, "updated_at", None) or row.created_at,
        ),
        headers=_NO_STORE,
    )


# --------------------------------------------------------------------------- #
# items: writes
# --------------------------------------------------------------------------- #


async def _resolve_parent(
    session: Any, workspace_id: uuid.UUID, parent_key: str
) -> uuid.UUID | None:
    if not parent_key:
        return None
    resolved = await repository.resolve_mapping(session, workspace_id, parent_key)
    if resolved is None or resolved[0] != "folder":
        raise JoplinProtocolError("rejected")
    return resolved[1]


async def _put_document(
    request: Request, ctx: BridgeContext, name: str, item: JoplinItem
) -> dict[str, Any]:
    """Create or update one note/folder item through the kernel."""
    key = item.id
    expected_kind = "note" if item.type_ == TYPE_NOTE else "folder"
    writer = SyncWriteService(_factory(request))
    factory = _factory(request)

    async with factory() as session:
        resolved = await repository.resolve_mapping(session, ctx.workspace_id, key)
        parent_uuid = await _resolve_parent(session, ctx.workspace_id, item.parent_id)
        row: Any = None
        if resolved is not None:
            kind, entity_id = resolved
            if kind != expected_kind:
                raise JoplinProtocolError("rejected")
            row = (
                await repository.load_note(session, ctx.workspace_id, entity_id)
                if kind == "note"
                else await repository.load_folder(session, ctx.workspace_id, entity_id)
            )
        else:
            entity_id = await repository.ensure_mapping(
                session, ctx.workspace_id, key, expected_kind
            )

    if row is not None and row.deleted_at is not None:
        raise JoplinProtocolError("rejected")

    if row is None:
        if item.type_ == TYPE_NOTE:
            command: Any = CreateNote(
                workspace_id=ctx.workspace_id,
                entity_id=entity_id,
                op_id=new_op_id(),
                title=item.title,
                body_md=item.body,
                folder_id=parent_uuid,
            )
        else:
            command = CreateFolder(
                workspace_id=ctx.workspace_id,
                entity_id=entity_id,
                op_id=new_op_id(),
                name=item.title,
                parent_id=parent_uuid,
            )
        await _translate_call(writer.execute(command))
    else:
        await _update_document(
            request=request,
            ctx=ctx,
            writer=writer,
            row=row,
            item=item,
            parent_uuid=parent_uuid,
            kind=expected_kind,
        )

    kind, _entity_id, fresh = await _load_item(request, ctx, name)
    parent_id = fresh.parent_id if kind == "folder" else getattr(fresh, "folder_id", None)
    return _item_json(
        jop_key=key,
        kind=kind,
        parent_key=await _parent_key(request, ctx, parent_id),
        created_at=fresh.created_at,
        updated_at=getattr(fresh, "updated_at", None) or fresh.created_at,
    )


async def _update_document(
    *,
    request: Request,
    ctx: BridgeContext,
    writer: SyncWriteService,
    row: Any,
    item: JoplinItem,
    parent_uuid: uuid.UUID | None,
    kind: str,
) -> None:
    """Apply the difference between the client's item and the stored row."""
    rev = int(row.rev)
    if kind == "note":
        if row.folder_id != parent_uuid:
            await _translate_call(
                writer.execute(
                    MoveNote(
                        workspace_id=ctx.workspace_id,
                        entity_id=row.id,
                        op_id=new_op_id(),
                        base_rev=rev,
                        folder_id=parent_uuid,
                    )
                )
            )
            rev += 1
        if item.title != row.title or item.body != row.body_md:
            await _translate_call(
                writer.execute(
                    UpdateNote(
                        workspace_id=ctx.workspace_id,
                        entity_id=row.id,
                        op_id=new_op_id(),
                        base_rev=rev,
                        title=item.title,
                        body_md=item.body,
                    )
                )
            )
        return

    if row.parent_id != parent_uuid:
        await _translate_call(
            writer.execute(
                MoveFolder(
                    workspace_id=ctx.workspace_id,
                    entity_id=row.id,
                    op_id=new_op_id(),
                    base_rev=rev,
                    parent_id=parent_uuid,
                )
            )
        )
        rev += 1
    if item.title != row.name:
        await _translate_call(
            writer.execute(
                UpdateFolder(
                    workspace_id=ctx.workspace_id,
                    entity_id=row.id,
                    op_id=new_op_id(),
                    base_rev=rev,
                    name=item.title,
                )
            )
        )


async def _put_resource_item(
    request: Request, ctx: BridgeContext, key: str, item: JoplinItem
) -> dict[str, Any]:
    """Declare a resource: create the pending attachment row behind it."""
    if not bool(getattr(_settings(request), "attachments_enabled", False)):
        raise JoplinProtocolError("storage_unavailable")
    factory = _factory(request)
    async with factory() as session:
        resolved = await repository.resolve_mapping(session, ctx.workspace_id, key)
        if resolved is not None:
            if resolved[0] != "attachment":
                raise JoplinProtocolError("rejected")
            row = await repository.load_attachment(session, ctx.workspace_id, resolved[1])
            if row is not None:
                return _item_json(
                    jop_key=key,
                    kind="attachment",
                    parent_key="",
                    created_at=row.created_at,
                    updated_at=row.ready_at or row.created_at,
                )

    mime = item.resource_mime
    if not mime:
        raise JoplinProtocolError("bad_request")
    pending = await _translate_call(
        _attachment_service(request).create(
            ctx.workspace_id,
            mime=mime,
            byte_size=item.resource_size,
            file_name=item.resource_name,
        )
    )
    async with factory() as session:
        await repository.record_mapping(session, ctx.workspace_id, key, "attachment", pending.id)
    return _item_json(
        jop_key=key,
        kind="attachment",
        parent_key="",
        created_at=pending.created_at,
        updated_at=pending.created_at,
    )


async def _put_item(request: Request, ctx: BridgeContext, name: str, body: bytes) -> dict[str, Any]:
    """Write one ``.md`` item (note, folder or resource metadata)."""
    try:
        text = body.decode("utf-8")
        item = parse_item(text)
    except (UnicodeDecodeError, JoplinItemError) as exc:
        raise JoplinProtocolError("bad_request") from exc

    expected = name[: -len(".md")] if name.endswith(".md") else name
    if item.id != expected:
        raise JoplinProtocolError("bad_request")

    if item.type_ == TYPE_RESOURCE:
        return await _put_resource_item(request, ctx, item.id, item)
    return await _put_document(request, ctx, name, item)


async def _put_blob(
    request: Request, ctx: BridgeContext, key: str, chunks: AsyncIterator[bytes]
) -> dict[str, Any]:
    """Store ``.resource/<key>`` bytes through the attachment state machine."""
    if not bool(getattr(_settings(request), "attachments_enabled", False)):
        raise JoplinProtocolError("storage_unavailable")
    factory = _factory(request)
    async with factory() as session:
        resolved = await repository.resolve_mapping(session, ctx.workspace_id, key)
        if resolved is None or resolved[0] != "attachment":
            raise JoplinProtocolError("not_found")
        row = await repository.load_attachment(session, ctx.workspace_id, resolved[1])
    if row is None:
        raise JoplinProtocolError("not_found")
    if row.state == "ready":
        return _item_json(
            jop_key=key,
            kind="attachment",
            parent_key="",
            created_at=row.created_at,
            updated_at=row.ready_at or row.created_at,
        )

    result = await _translate_call(
        store_blob(
            _attachment_service(request),
            ctx.workspace_id,
            row.id,
            chunks=chunks,
            declared_content_type=row.mime,
        )
    )
    return _item_json(
        jop_key=key,
        kind="attachment",
        parent_key="",
        created_at=row.created_at,
        updated_at=getattr(result, "ready_at", row.created_at),
    )


@router.put("/items/root:/{raw:path}/content")
async def put_item_content(raw: str, request: Request, ctx: BridgeContextDep) -> Response:
    """Create or replace one item (Joplin's ``put``)."""
    name = _name_from_raw(raw)
    _require_item_name(name)

    if name.startswith(".resource/"):
        payload = await _put_blob(request, ctx, name[len(".resource/") :], request.stream())
        return JSONResponse(status_code=200, content=payload, headers=_NO_STORE)

    body = await request.body()
    if len(body) > _ITEM_BODY_MAX_BYTES:
        raise JoplinProtocolError("too_large")
    return JSONResponse(
        status_code=200,
        content=await _put_item(request, ctx, name, body),
        headers=_NO_STORE,
    )


async def _delete_item(request: Request, ctx: BridgeContext, name: str) -> None:
    if name.startswith(".resource/"):
        # Phase 5 has no attachment deletion (D13): accept the client's view and
        # leave the bytes alone. Documented in docs/phase6-joplin-bridge.md.
        return
    kind, entity_id, row = await _load_item(request, ctx, name)
    if getattr(row, "deleted_at", None) is not None:
        return

    writer = SyncWriteService(_factory(request))
    if kind == "note":
        command: Any = DeleteNote(
            workspace_id=ctx.workspace_id,
            entity_id=entity_id,
            op_id=new_op_id(),
            base_rev=int(row.rev),
        )
    else:
        command = DeleteFolder(
            workspace_id=ctx.workspace_id,
            entity_id=entity_id,
            op_id=new_op_id(),
            base_rev=int(row.rev),
        )
    await _translate_call(writer.execute(command))


@router.delete("/items/root:/{raw:path}")
async def delete_item(raw: str, request: Request, ctx: BridgeContextDep) -> Response:
    """Soft-delete a note/folder; a resource is a documented no-op."""
    name = _name_from_raw(raw)
    _require_item_name(name)
    await _delete_item(request, ctx, name)
    return Response(status_code=204, headers=_NO_STORE)


# --------------------------------------------------------------------------- #
# batch (the client declares supportsMultiPut/supportsMultiDelete)
# --------------------------------------------------------------------------- #


@router.put("/batch_items")
async def put_batch_items(request: Request, ctx: BridgeContextDep) -> Response:
    """``{items: [{name, body}]}`` -> per-name result or per-name error."""
    payload = await _json_body(request, max_bytes=_BATCH_BODY_MAX_BYTES)
    entries = payload.get("items")
    if not isinstance(entries, list):
        raise JoplinProtocolError("bad_request")

    results: dict[str, Any] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise JoplinProtocolError("bad_request")
        name = entry.get("name")
        body = entry.get("body")
        if not isinstance(name, str) or not isinstance(body, str):
            raise JoplinProtocolError("bad_request")
        try:
            _require_item_name(name)
            results[name] = {"item": await _put_item(request, ctx, name, body.encode("utf-8"))}
        except JoplinProtocolError as error:
            results[name] = {
                "error": {
                    "error": error.wire_code,
                    "code": error.wire_code,
                    "httpCode": error.status,
                }
            }
    return JSONResponse(status_code=200, content={"items": results}, headers=_NO_STORE)


@router.delete("/batch_items")
async def delete_batch_items(request: Request, ctx: BridgeContextDep) -> Response:
    """``{items: [name, ...]}`` -> per-name acceptance."""
    payload = await _json_body(request, max_bytes=_BATCH_BODY_MAX_BYTES)
    entries = payload.get("items")
    if not isinstance(entries, list) or not all(isinstance(name, str) for name in entries):
        raise JoplinProtocolError("bad_request")

    results: dict[str, Any] = {}
    for name in entries:
        try:
            _require_item_name(name)
            await _delete_item(request, ctx, name)
            results[name] = {}
        except JoplinProtocolError as error:
            results[name] = {
                "error": {
                    "error": error.wire_code,
                    "code": error.wire_code,
                    "httpCode": error.status,
                }
            }
    return JSONResponse(status_code=200, content={"items": results}, headers=_NO_STORE)


# --------------------------------------------------------------------------- #
# locks
# --------------------------------------------------------------------------- #


@router.get("/locks")
async def get_locks(request: Request, ctx: BridgeContextDep) -> Response:
    """List live locks (camelCase, epoch ms — the client's ``Lock`` shape)."""
    rows = await _translate_call(
        list_locks(
            _factory(request),
            ctx.workspace_id,
            ttl_s=int(_settings(request).joplin_lock_ttl_s),
        )
    )
    return JSONResponse(
        status_code=200,
        content={"items": [serialize_lock(row) for row in rows], "has_more": False},
        headers=_NO_STORE,
    )


@router.post("/locks")
async def post_lock(request: Request, ctx: BridgeContextDep) -> Response:
    """Acquire ``sync`` or ``exclusive``; conflicts use the client's code names."""
    payload = await _json_body(request, max_bytes=_SESSION_BODY_MAX_BYTES)
    raw_type = payload.get("type")
    raw_client = payload.get("clientType")
    client_id = payload.get("clientId")
    if not isinstance(raw_type, int) or not isinstance(raw_client, int):
        raise JoplinProtocolError("bad_request")
    if not isinstance(client_id, str) or not client_id:
        raise JoplinProtocolError("bad_request")
    lock_type = LOCK_SYNC if raw_type == 1 else LOCK_EXCLUSIVE if raw_type == 2 else None
    if lock_type is None:
        raise JoplinProtocolError("bad_request")

    row = await _translate_call(
        acquire_lock(
            _factory(request),
            ctx.workspace_id,
            lock_type=lock_type,
            client_type=raw_client,
            client_id=client_id,
            ttl_s=int(_settings(request).joplin_lock_ttl_s),
        )
    )
    return JSONResponse(status_code=200, content=serialize_lock(row), headers=_NO_STORE)


@router.delete("/locks/{lock_id}")
async def delete_lock(lock_id: str, request: Request, ctx: BridgeContextDep) -> Response:
    """Release one lock (idempotent)."""
    lock_type, client_type, client_id = parse_lock_id(lock_id)
    await _translate_call(
        release_lock(
            _factory(request),
            ctx.workspace_id,
            lock_type=lock_type,
            client_type=client_type,
            client_id=client_id,
        )
    )
    return Response(status_code=204, headers=_NO_STORE)
