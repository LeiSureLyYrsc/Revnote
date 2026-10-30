"""The attachment endpoints and the capability probe (interface §2).

Two routers in one module, because they are one feature and one import line in
:mod:`revnote_api.api.router`:

``capabilities_router``
    ``GET /capabilities`` - unauthenticated, always ``200``. A client needs it
    *before* it has a token (the editor decides whether to render an attach button
    when the workspace screen mounts), and it exposes no workspace data, only the
    deployment's own limits. Adding a bearer requirement here would make the one
    endpoint whose purpose is feature detection the one endpoint a signed-out
    client cannot reach.

``router``
    The four workspace/attachment operations. Every one of them is a thin shell
    over :class:`~revnote_api.services.attachments.AttachmentService`; this module
    only translates an :class:`~revnote_api.services.attachments.AttachmentError`
    into the envelope and renders rows.

Streaming, in both directions and for the same reason
-----------------------------------------------------
Upload reads the request stream in :data:`UPLOAD_CHUNK_BYTES` slices and hands the
async iterator straight to ``BlobStore.put``; download hands ``iter_chunks`` to a
:class:`~starlette.responses.StreamingResponse`. Neither path ever holds a whole
object in memory, so a 25 MiB attachment costs O(64 KiB) per request whether it is
being written or read. The service owns the "no storage call inside a transaction"
rule; this module owns the second half of it: it never wraps a streaming response
in a session either.

Every response that carries an attachment - or its metadata - sets
``Cache-Control: no-store``. These are authorised, workspace-private objects; a
shared cache retaining one would serve it to the next principal. Downloads
additionally set ``X-Content-Type-Options: nosniff``: the stored MIME is a claim
the *uploader* made, and a browser must not be allowed to re-interpret the bytes as
something else (``text/plain`` that is really HTML is the classic case).
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import StreamingResponse

from revnote_api.api.dependencies import AuthContext, get_auth_context, require_workspace_member
from revnote_api.api.errors import ApiError
from revnote_api.api.schemas.attachments import (
    AttachmentResponse,
    CapabilitiesResponse,
    CapabilityAttachment,
    CapabilityJoplinBridge,
    CreateAttachmentRequest,
    CreateAttachmentResponse,
    UploadContentResponse,
    UploadTarget,
)
from revnote_api.config import Settings
from revnote_api.db.session import get_sessionmaker
from revnote_api.services.attachments import (
    ACCEPTED_MIME_DEFAULT,
    AttachmentError,
    AttachmentPolicy,
    AttachmentService,
    AttachmentView,
    ReadyAttachment,
    normalize_declared_sha256,
)
from revnote_api.storage.ports import BlobStore, StorageUnavailable

__all__ = [
    "UPLOAD_CHUNK_BYTES",
    "AttachmentServiceDep",
    "build_store",
    "capabilities_router",
    "get_attachment_service",
    "router",
]

#: Size of the slices the upload endpoint reads out of the request stream. Matches
#: ``BlobStore.iter_chunks``' default so both directons move the same amount of
#: data per await; memory stays O(chunk), never O(object).
UPLOAD_CHUNK_BYTES: Final[int] = 65536

#: Header carrying the client's own claim about the bytes (optional).
CONTENT_SHA256_HEADER: Final[str] = "X-Content-SHA256"


def _translate(error: AttachmentError) -> ApiError:
    """Carry the service's safe code and scalar details onto the wire."""
    details = {
        key: value
        for key, value in error.details.items()
        if isinstance(value, (str, int, float, bool))
    }
    return ApiError(error.code, details=details)


def build_store(settings: Settings) -> BlobStore:
    """The object store for this request, built from the deployment's configuration.

    Three things matter here, and all of them are about *when* an adapter is built:

    * the storage adapter is imported **inside** the function, so this module - and
      therefore ``create_app`` - imports without the object-store SDK being present.
      A deployment that never enables attachments never loads it.
    * storage has its own settings class (:class:`ObjectStorageSettings`, whose
      ``repr`` masks the credential pair) rather than fields on ``Settings``: the
      interface (D11) keeps every credential-shaped name out of the application
      settings, and a connection description is not application policy.
    * a deployment with no storage configured fails as
      ``attachment_storage_unavailable`` (503, retryable) on the request that needs
      it, never as a ``500`` and never at import time. Constructing
      ``ObjectStorageSettings`` raises a validation error when the endpoint, bucket or
      credential pair is absent - which is exactly that case.

    ``settings`` is accepted (and deliberately unused beyond that) so the call site
    reads uniformly with every other per-request collaborator and a test can override
    this one function to inject a double.
    """
    del settings  # the adapter reads its own, credential-bearing settings class
    from revnote_api.storage.s3 import S3BlobStore
    from revnote_api.storage.settings import ObjectStorageSettings

    try:
        storage_settings = ObjectStorageSettings()
    except Exception as exc:  # a missing endpoint/bucket/credential pair
        raise StorageUnavailable("object storage is not configured") from exc
    return S3BlobStore(storage_settings)


def get_attachment_service(request: Request) -> AttachmentService:
    """The attachment service for this request, built from the app's settings.

    A FastAPI dependency (not a module global) so tests can override exactly this
    callable with ``app.dependency_overrides`` and no second construction path can
    exist. Building it per request costs nothing: the service holds three
    references and no per-call state.
    """
    settings: Settings = request.app.state.settings
    policy = AttachmentPolicy.from_settings(settings)
    return AttachmentService(get_sessionmaker(settings), build_store(settings), policy)


#: The annotated, injectable form of :func:`get_attachment_service`.
AttachmentServiceDep = Annotated[AttachmentService, Depends(get_attachment_service)]


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #

capabilities_router = APIRouter(tags=["attachments"])


@capabilities_router.get(
    "/capabilities",
    response_model=CapabilitiesResponse,
    summary="Deployment feature detection (always 200)",
    responses={200: {"description": "Attachment support, size cap and MIME allowlist."}},
)
async def capabilities(request: Request) -> Response:
    """Report the effective attachment policy.

    Always ``200``, and always the full object: a disabled deployment reports
    ``enabled: false`` with its limits still present, so a client can show "this
    server does not accept attachments" rather than "unknown". A cache must not
    retain it past a deploy that flips the switch, hence ``no-store``.
    """
    settings: Settings = request.app.state.settings
    policy = AttachmentPolicy.from_settings(settings)
    payload = CapabilitiesResponse(
        attachments=CapabilityAttachment(
            enabled=bool(getattr(settings, "attachments_enabled", False)),
            max_bytes=policy.max_bytes,
            accepted_mime=list(policy.accepted_mime) or list(ACCEPTED_MIME_DEFAULT),
        ),
        joplin_bridge=CapabilityJoplinBridge(
            enabled=bool(getattr(settings, "joplin_bridge_enabled", False)),
            protocol_baseline=str(getattr(settings, "joplin_protocol_baseline", "3.7")),
        ),
    )
    return Response(
        content=payload.model_dump_json(),
        media_type="application/json",
        headers=_NO_STORE,
    )


# --------------------------------------------------------------------------- #
# attachments
# --------------------------------------------------------------------------- #

router = APIRouter(tags=["attachments"])


def _attachment_model(view: AttachmentView) -> AttachmentResponse:
    return AttachmentResponse(
        id=view.id,
        workspace_id=view.workspace_id,
        state=view.state,
        mime=view.mime,
        byte_size=view.byte_size,
        content_hash=view.content_hash,
        created_at=view.created_at,
        ready_at=view.ready_at,
        file_name=view.file_name,
    )


def _require_enabled(settings: Settings) -> None:
    """Refuse attachment *writes* on a deployment with the feature switched off.

    ``GET /capabilities`` is how a client learns this, but a client that ignores it
    must not be able to create rows either. Reads stay allowed: data written while
    the switch was on is still the user's data, and hiding it after a flip would
    look like data loss.
    """
    if not bool(getattr(settings, "attachments_enabled", False)):
        raise ApiError("attachment_storage_unavailable", details={"reason": "disabled"})


@router.post(
    "/workspaces/{workspace_id}/attachments",
    response_model=CreateAttachmentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a pending attachment and get its upload target",
    responses={
        201: {"description": "A pending row plus where to PUT the bytes."},
        400: {"description": "MIME not accepted, or a declared size out of range."},
        404: {"description": "No such workspace, or the caller is not a member."},
        413: {"description": "The declared size exceeds the attachment cap."},
        503: {"description": "Object storage is unavailable, or attachments are disabled."},
    },
)
async def create_attachment(
    workspace_id: uuid.UUID,
    body: CreateAttachmentRequest,
    request: Request,
    context: Annotated[AuthContext, Depends(require_workspace_member)],
    service: AttachmentServiceDep,
) -> Response:
    """Insert the ``pending`` row; nothing is stored until the ``PUT``."""
    del context  # authorisation side effect only; the workspace id is the scope
    settings: Settings = request.app.state.settings
    _require_enabled(settings)

    try:
        pending = await service.create(
            workspace_id,
            mime=body.mime,
            byte_size=body.byte_size,
            file_name=body.file_name,
        )
    except AttachmentError as error:
        raise _translate(error) from None

    payload = CreateAttachmentResponse(
        attachment_id=pending.id,
        upload=UploadTarget(url=pending.upload_path),
        expires_at=pending.expires_at,
    )
    return Response(
        content=payload.model_dump_json(),
        status_code=status.HTTP_201_CREATED,
        media_type="application/json",
        headers=_NO_STORE,
    )


#: ``no-store`` on every attachment response: authorised, workspace-private content
#: must never be retained by a cache another principal could read.
_NO_STORE: Final[dict[str, str]] = {"Cache-Control": "no-store"}


async def _read_request_chunks(stream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Re-chunk the request body into :data:`UPLOAD_CHUNK_BYTES` slices.

    The ASGI server's own chunking is not a contract - it may hand over 1 byte or
    the whole body - so this normalises it. It is a pass-through otherwise: no
    buffering of the whole body, no accumulation, and an exception raised by the
    consumer (``BlobStore.put`` aborting on the size cap) propagates immediately.
    """
    pending = bytearray()
    async for piece in stream:
        if not piece:
            continue
        pending.extend(piece)
        while len(pending) >= UPLOAD_CHUNK_BYTES:
            yield bytes(pending[:UPLOAD_CHUNK_BYTES])
            del pending[:UPLOAD_CHUNK_BYTES]
    if pending:
        yield bytes(pending)


def _request_body(request: Request) -> AsyncIterator[bytes]:
    """The raw request stream as an async iterator (no parsing, no decoding)."""
    return _read_request_chunks(request.stream())


@router.put(
    "/workspaces/{workspace_id}/attachments/{attachment_id}/content",
    response_model=UploadContentResponse,
    summary="Stream the bytes of a pending attachment and publish it as ready",
    responses={
        200: {"description": "The bytes were stored and verified; the digest is returned."},
        400: {"description": "Content-Type does not match the declared MIME."},
        404: {"description": "No such attachment in this workspace, or it is deleted."},
        409: {"description": "Already uploaded, expired, or the declared digest was wrong."},
        413: {"description": "The body exceeds the attachment cap."},
        503: {"description": "Object storage is unavailable."},
    },
)
async def upload_content(
    workspace_id: uuid.UUID,
    attachment_id: uuid.UUID,
    request: Request,
    context: Annotated[AuthContext, Depends(require_workspace_member)],
    service: AttachmentServiceDep,
) -> Response:
    """Stream the raw body into the store, then verify and publish it.

    The body is never parsed, never decoded and never buffered whole. ``Content-Type``
    is passed to the service as a *claim* to be compared against the declared MIME -
    it is not used to pick a codec, because the bytes are stored verbatim.
    """
    del context

    settings: Settings = request.app.state.settings
    _require_enabled(settings)

    try:
        declared_sha256 = normalize_declared_sha256(request.headers.get(CONTENT_SHA256_HEADER))
    except ValueError:
        raise ApiError(
            "invalid_request",
            details={"fields": [{"path": ["header", CONTENT_SHA256_HEADER], "reason": "invalid"}]},
        ) from None

    try:
        ready: ReadyAttachment = await service.store_content(
            workspace_id,
            attachment_id,
            chunks=_request_body(request),
            declared_content_type=request.headers.get("content-type"),
            declared_sha256=declared_sha256,
        )
    except AttachmentError as error:
        raise _translate(error) from None

    payload = UploadContentResponse(
        attachment_id=ready.id,
        content_hash=ready.content_hash,
        byte_size=ready.byte_size,
    )
    return Response(
        content=payload.model_dump_json(),
        media_type="application/json",
        headers=_NO_STORE,
    )


@router.get(
    "/attachments/{attachment_id}",
    response_model=AttachmentResponse,
    summary="Metadata of one ready attachment",
    responses={
        200: {"description": "State, MIME, size and digest; never the bytes."},
        404: {"description": "Unknown id, not a member, or not ready."},
    },
)
async def attachment_metadata(
    attachment_id: uuid.UUID,
    context: Annotated[AuthContext, Depends(get_auth_context)],
    service: AttachmentServiceDep,
) -> Response:
    """Describe one attachment. Membership comes from the service's join, not a path."""
    try:
        view = await service.metadata(attachment_id, user_id=context.user_id)
    except AttachmentError as error:
        raise _translate(error) from None

    return Response(
        content=_attachment_model(view).model_dump_json(),
        media_type="application/json",
        headers=_NO_STORE,
    )


@router.get(
    "/attachments/{attachment_id}/content",
    summary="Stream the bytes of one ready attachment",
    response_class=StreamingResponse,
    responses={
        200: {"description": "The stored bytes, with the stored MIME and nosniff."},
        404: {"description": "Unknown id, not a member, or not ready."},
        503: {"description": "Object storage is unavailable."},
    },
)
async def attachment_content(
    attachment_id: uuid.UUID,
    context: Annotated[AuthContext, Depends(get_auth_context)],
    service: AttachmentServiceDep,
) -> Response:
    """Relay the object to the client chunk by chunk, with no caching and no sniffing.

    Two headers are contract, not decoration:

    * ``Cache-Control: no-store`` - the bytes are workspace-private;
    * ``X-Content-Type-Options: nosniff`` - the stored MIME was *declared by the
      uploader*. A browser that re-interprets the payload (HTML labelled
      ``text/plain``, a ``.pdf`` that is really a script) would be executing
      attacker-supplied content on our origin.

    ``Content-Length`` is deliberately **not** set from the row: the row records
    what the store verified, and a mismatch between it and the stream would either
    truncate the body or hang the connection. The store's own framing is correct.
    """
    try:
        view, chunks = await service.open_content(attachment_id, user_id=context.user_id)
    except AttachmentError as error:
        raise _translate(error) from None

    headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }
    return StreamingResponse(
        _closing_stream(chunks),
        media_type=view.mime,
        headers=headers,
    )


async def _closing_stream(chunks: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    """Relay ``chunks`` and close the iterator even when the client disconnects.

    ``aclose`` on the store's iterator is what returns the underlying connection;
    skipping it on a cancelled download would leak one per abandoned request.
    ``contextlib.suppress`` on the close is intentional: a store that fails while
    closing must not turn a completed download into an error.
    """
    try:
        async for chunk in chunks:
            yield chunk
    finally:
        close: Callable[[], Awaitable[object]] | None = getattr(chunks, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()
