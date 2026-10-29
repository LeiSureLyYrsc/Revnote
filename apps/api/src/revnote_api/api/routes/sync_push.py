"""``POST /workspaces/{workspace_id}/sync/push`` - the only write endpoint (D19).

Thin by design. Everything with policy in it lives elsewhere:

* shape, structural rules and the eight payload models -> ``api/schemas/sync_push``;
* ordering, per-op transactions, stop rules, applied/replayed -> ``services/sync_push_adapter``;
* the error envelope, codes and fixed messages -> ``api/errors``.

What remains here is exactly the protocol glue that *must* know about HTTP: read the
body once, reject the whole batch before any side effect, supply the per-op membership
probe on a short-lived connection, notice a client that has gone away, and render.

Order of operations (nothing writes before every check has passed)
-----------------------------------------------------------------
1. **Authenticate.** ``get_auth_context`` resolves the bearer token to
   :class:`~revnote_api.api.dependencies.AuthContext` (401 on failure).
2. **Authorise.** ``require_workspace_member`` turns "not a member" and "no such
   workspace" into one uniform 404 (D20); the response must not reveal which.
3. **Bound the batch.** ``1..SYNC_PUSH_MAX_OPS`` ops (D9) and every ``body_md`` at
   most ``SYNC_NOTE_MAX_BYTES`` UTF-8 bytes (D9 - HTTP layer only, the kernel CHECK
   is untouched).
4. **Reject a duplicate ``op_id``** anywhere in the batch (D12): 400 and *zero*
   execution, including for ops that precede the duplicate.
5. **Build every kernel command up front** (``prepare_batch``). An op the kernel
   would refuse structurally rejects the *whole* request with 400 and zero
   execution, so a bad op at index 7 can never leave indices 0-6 committed.
6. Run the ops in order, each in its own transaction.

Disconnects
-----------
Starlette/uvicorn do **not** cancel a handler when the client closes the socket, so a
vanished client cannot be inferred from cancellation alone. Between ops this route
supplies ``request.is_disconnected`` to the adapter: a gone peer stops the batch with
the remaining ops ``not_attempted``, and nothing is "rolled back" because nothing was
started. A real ``CancelledError`` (shutdown, task cancellation) is deliberately not
caught: it propagates, and the in-flight op's transaction is resolved by the kernel.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path, Request

from revnote_api.api.dependencies import (
    AuthContext,
    get_auth_context,
    require_workspace_member,
)
from revnote_api.api.errors import (
    ApiError,
    ErrorBody,
    get_request_id,
    spec_for,
)
from revnote_api.api.schemas.sync_push import (
    PushRequest,
    PushResponse,
    PushResultItem,
)
from revnote_api.config import Settings
from revnote_api.db.session import get_sessionmaker
from revnote_api.services.auth import assert_workspace_member
from revnote_api.services.auth_rules import AuthError
from revnote_api.services.sync_push_adapter import (
    PushBatch,
    PushOpSpec,
    PushOutcome,
    PushService,
    prepare_batch,
)
from revnote_api.services.sync_write import SyncWriteService

__all__ = ["router", "sync_push_router"]

router = APIRouter()

#: Tag used by the OpenAPI document for this group.
TAG: Final[str] = "sync"

#: Statuses whose result carries an error object (spec §3).
_FAILURE_STATUSES: Final[frozenset[str]] = frozenset(
    {"conflict", "rejected", "unknown", "not_attempted"}
)


def _utf8_len(value: str) -> int:
    """Byte length as the wire carries it, tolerating undecodable text.

    ``str.encode("utf-8")`` raises ``UnicodeEncodeError`` on a lone surrogate, which
    JSON can legally carry (``"\\ud800"``). That is client input, so it must become a
    400 rather than a 500: the character is unrepresentable on the wire *and* in
    PostgreSQL, so the batch is refused here.
    """
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ApiError("invalid_request") from None


def _specs(body: PushRequest) -> tuple[PushOpSpec, ...]:
    """Convert validated wire ops into framework-free specs.

    ``base_rev`` stays a string on the wire and becomes an ``int`` only at the
    kernel boundary. The payload passes through unchanged: the schema already proved
    it has exactly the fields its op declares.
    """
    return tuple(
        PushOpSpec(
            op_id=item.op_id,
            entity_type=item.entity_type,
            op=item.op,
            entity_id=item.entity_id,
            base_rev=None if item.base_rev is None else int(item.base_rev),
            payload=item.payload_fields(),
        )
        for item in body.ops
    )


def _check_bounds(settings: Settings, body: PushRequest) -> None:
    """Enforce the batch/bodies budgets before anything executes (D9).

    Both numbers come from ``app.state.settings`` rather than the schema, so a
    deployment's own limits stay reachable. A violation rejects the *whole* request:
    partially accepting a batch would leave the client unable to tell which ops ran.
    """
    if len(body.ops) > settings.sync_push_max_ops:
        raise ApiError("invalid_request", details={"max_ops": settings.sync_push_max_ops})

    for index, op in enumerate(body.ops):
        body_md = getattr(op.payload, "body_md", None)
        if body_md is not None and _utf8_len(body_md) > settings.sync_note_max_bytes:
            # 413 rather than 400: well formed, just too large. The failing op is
            # identified by index, never by content.
            raise ApiError(
                "payload_too_large",
                details={
                    "max_body_bytes": settings.sync_note_max_bytes,
                    "op_index": index,
                },
            )


def _reject_duplicate_op_ids(body: PushRequest) -> None:
    """D12: a repeated ``op_id`` anywhere in the batch rejects all of it.

    Checked after UUID normalisation (Pydantic parsed every ``op_id`` into a
    ``UUID``), so two different spellings of one value collide here rather than at
    the ledger. No details: naming the offending value would echo a client
    identifier for no benefit.
    """
    seen: set[uuid.UUID] = set()
    for op in body.ops:
        if op.op_id in seen:
            raise ApiError("invalid_request")
        seen.add(op.op_id)


def _error_body(code: str, request_id: str) -> ErrorBody:
    """The protocol's one error object for a failed op.

    Built from :func:`api.errors.spec_for` so ``code``, ``message`` and ``retryable``
    come from the single vocabulary table - a push result can never word a failure
    differently from the top-level envelope. ``details`` is empty on purpose for the
    id-conflict collapse (``EntityIdConflict``/``OpIdConflict`` share one code with no
    extras, so a caller cannot tell whether an id exists in another workspace, P2 #3).
    """
    spec = spec_for(code)
    return ErrorBody(
        code=spec.code,
        message=spec.message,
        request_id=request_id,
        retryable=spec.retryable,
        details={},
    )


def _render(outcome: PushOutcome, request_id: str) -> PushResponse:
    """Render results, giving every failure the protocol's one error shape.

    Spec §3: a failed op carries ``error`` - the same
    ``{code, message, request_id, retryable, details}`` object as the top-level
    envelope - not a bare code. ``message``/``retryable`` come from the shared specs,
    ``details`` stays empty, and ``request_id`` is the request's own id so a client
    can correlate a per-op failure with its logs.
    """
    items: list[PushResultItem] = []
    for result in outcome.results:
        items.append(
            PushResultItem(
                op_id=result.op_id,
                status=result.status,
                seq=None if result.seq is None else str(result.seq),
                rev=None if result.rev is None else str(result.rev),
                error=None if result.code is None else _error_body(result.code, request_id),
            )
        )
    return PushResponse(results=items)


@router.post(
    "/workspaces/{workspace_id}/sync/push",
    response_model=PushResponse,
    responses={
        400: {"description": "Malformed batch (shape, limits, duplicate op_id)."},
        401: {"description": "Missing or invalid access token."},
        404: {"description": "Workspace not found or not a member (identical)."},
        413: {"description": "A note body exceeds the per-object limit."},
        503: {"description": "Authentication is temporarily unavailable."},
    },
    tags=[TAG],
    summary="Apply an ordered batch of single-object operations",
)
async def push(
    request: Request,
    body: PushRequest,
    workspace_id: Annotated[uuid.UUID, Path(description="Workspace the batch applies to.")],
    context: Annotated[AuthContext, Depends(get_auth_context)],
    _member: Annotated[AuthContext, Depends(require_workspace_member)],
) -> PushResponse:
    """Apply ``body.ops`` in order, one independent transaction per op.

    The workspace comes from the **path** and never from an op (spec §3): the body
    cannot redirect a write to another workspace, and an op that tries to name one
    fails schema validation because unknown fields are forbidden.
    """
    settings: Settings = request.app.state.settings
    _check_bounds(settings, body)
    _reject_duplicate_op_ids(body)

    specs = _specs(body)
    commands = prepare_batch(workspace_id, specs)
    if commands is None:
        # The kernel would refuse at least one op structurally. Rejecting the whole
        # batch is the only honest answer: the alternative is a partial write whose
        # extent the client cannot see.
        raise ApiError("invalid_request")

    factory = get_sessionmaker(settings)
    service = PushService(SyncWriteService(factory))

    async def membership(workspace: uuid.UUID, user_id: uuid.UUID) -> None:
        """Per-op re-check on its own short-lived connection (D5).

        Deliberately not sharing the writer's connection: the check must not hold a
        session across the kernel's transaction, and it must observe a revocation
        that happened mid-batch.
        """
        await _assert_member(factory, workspace, user_id)

    outcome = await service.run(
        PushBatch(
            workspace_id=workspace_id,
            ops=specs,
            commands=commands,
            lock_timeout_ms=settings.db_lock_timeout_ms,
            statement_timeout_ms=settings.db_statement_timeout_ms,
            budget_ms=settings.sync_push_budget_ms,
        ),
        user_id=context.user_id,
        membership=membership,
        should_abort=request.is_disconnected,
    )
    return _render(outcome, get_request_id(request))


#: Descriptive alias so :mod:`revnote_api.api.router` can mount this group by name.
sync_push_router = router


async def _assert_member(factory: Any, workspace_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """The per-op membership probe, behind one named seam.

    ``assert_workspace_member`` is the frozen contract from ``services/auth``:
    ``async (factory, user_id, workspace_id) -> None``, raising ``AuthError``. The
    error is translated here to the uniform ``not_found`` (D20): a revoked member and
    a workspace that never existed must be indistinguishable, and an uncaught
    ``AuthError`` would surface as a 500 instead.
    """
    try:
        await assert_workspace_member(factory, user_id=user_id, workspace_id=workspace_id)
    except AuthError as error:
        raise ApiError(error.code) from None
