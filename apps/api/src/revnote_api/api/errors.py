"""The one error surface of ``/api/v1``: codes, statuses, retryability, envelope.

Three tables, one purpose: translate *anything* that can go wrong into exactly
one wire shape, ``{"error": {code, message, request_id, retryable, details}}``
(plan §11.1), without letting an internal detail escape.

1. :data:`ERROR_SPECS` - the closed code vocabulary from plan §10. A code exists
   here or it does not exist at all; handlers never invent one.
2. :data:`KERNEL_ERROR_CODES` - kernel exception class -> code. This is the only
   place the kernel vocabulary meets HTTP. ``EntityIdConflict`` and
   ``OpIdConflict`` deliberately collapse into one ``conflict`` code with one
   fixed message: a distinct response would tell a caller whether an id exists
   in *another* workspace (P2 #3, plan §9.2).
3. :func:`error_response` / :func:`kernel_error_response` - the builders. Both
   take the request id explicitly, so the envelope and the ``X-Request-ID``
   header can never disagree.

Messages are **fixed strings owned by this module**. ``str(exc)`` is never
forwarded: PostgreSQL's own text echoes row values (``Key (body_md)=(...)``),
kernel messages can name ids, and a Pydantic validation error can quote the
input. The original exception stays on ``__cause__`` for a local debugger.
"""

from __future__ import annotations

import logging
import types
from typing import Annotated, Any, Final, Union, get_args, get_origin

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from revnote_api.api.schemas.common import ErrorBody, ErrorEnvelope
from revnote_api.kernel.errors import (
    CommitOutcomeUnknown,
    CounterOverflow,
    EntityDeleted,
    EntityIdConflict,
    EntityNotFound,
    FolderCycle,
    FolderNotEmpty,
    InvalidCommand,
    InvalidParent,
    OpIdConflict,
    RevConflict,
    StorageFailure,
    SyncKernelError,
    WorkspaceNotFound,
    WriteBusy,
)
from revnote_api.storage.ports import StorageUnavailable

__all__ = [
    "CODE_STATUS",
    "ERROR_SPECS",
    "KERNEL_ERROR_CODES",
    "LOCATION_MARKERS",
    "MAX_PATH_SEGMENTS",
    "MAX_REASON_CHARS",
    "MAX_REPORTED_FIELDS",
    "REDACTED_SEGMENT",
    "ApiError",
    "ErrorSpec",
    "RequestIdHeader",
    "error_response",
    "get_request_id",
    "install_error_handlers",
    "is_stop_code",
    "kernel_error_response",
    "spec_for",
]

#: Header carrying the per-request correlation id, both directions.
RequestIdHeader: Final[str] = "X-Request-ID"


class ErrorSpec:
    """HTTP status, retryability and the fixed message for one error code."""

    __slots__ = ("code", "message", "retryable", "status")

    def __init__(self, status: int, code: str, message: str, *, retryable: bool = False) -> None:
        self.status = status
        self.code = code
        self.message = message
        self.retryable = retryable

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ErrorSpec(status={self.status}, code={self.code!r})"


#: The closed error vocabulary (plan §10). Every entry's message is a fixed
#: sentence: no ids, no user input, no server text. ``retryable`` is advisory and
#: only true where re-sending the *identical* request is safe.
ERROR_SPECS: Final[dict[str, ErrorSpec]] = {
    spec.code: spec
    for spec in (
        # -- request / validation -------------------------------------------- #
        ErrorSpec(400, "invalid_request", "The request does not match the API contract."),
        ErrorSpec(400, "cursor_ahead", "The cursor is ahead of this workspace."),
        ErrorSpec(413, "payload_too_large", "The request body exceeds the allowed size."),
        # -- authentication --------------------------------------------------- #
        ErrorSpec(401, "unauthenticated", "Authentication is required."),
        ErrorSpec(401, "invalid_credentials", "The e-mail or password is incorrect."),
        ErrorSpec(401, "invalid_refresh", "The refresh token is invalid or no longer usable."),
        ErrorSpec(401, "token_reused", "This session was revoked; sign in again."),
        ErrorSpec(403, "registration_closed", "Registration is not open on this deployment."),
        ErrorSpec(429, "rate_limited", "Too many requests.", retryable=True),
        # -- addressability (never leaks existence across workspaces) --------- #
        ErrorSpec(404, "not_found", "The requested resource does not exist."),
        # -- conflicts -------------------------------------------------------- #
        ErrorSpec(409, "conflict", "The request conflicts with recorded state."),
        ErrorSpec(409, "rev_conflict", "The object changed since the given revision."),
        ErrorSpec(409, "entity_deleted", "The object is deleted."),
        ErrorSpec(409, "folder_not_empty", "The folder still has active children."),
        ErrorSpec(409, "invalid_parent", "The target parent folder is not usable."),
        ErrorSpec(409, "folder_cycle", "The move would make a folder its own ancestor."),
        ErrorSpec(409, "object_too_large", "The object exceeds the allowed size."),
        ErrorSpec(409, "snapshot_too_large", "The workspace is too large for a full snapshot."),
        ErrorSpec(409, "counter_overflow", "A workspace counter reached its limit."),
        # -- attachments (Phase 5, additive) ---------------------------------- #
        # A second upload to an id that is already ``ready``. The object is
        # immutable, so re-sending is refused rather than silently accepted.
        ErrorSpec(409, "attachment_already_ready", "The attachment is already uploaded."),
        # The bytes the server read back did not hash to the declared digest.
        # Nothing enters ``ready`` on this path, so the client may retry.
        ErrorSpec(409, "attachment_hash_mismatch", "The uploaded bytes do not match the digest."),
        # The ``pending`` row outlived the grace window and the GC may remove it.
        ErrorSpec(409, "attachment_expired", "The upload was not completed in time."),
        # The declared size (or the actual stream) exceeded the configured cap.
        # Distinct from the middleware's ``payload_too_large`` so a client can
        # tell "your body was too big for this path" from "your attachment is
        # over the attachment limit".
        ErrorSpec(413, "attachment_too_large", "The attachment exceeds the allowed size."),
        # Never for ``image/svg+xml``: the allowlist is a security boundary (D9).
        ErrorSpec(400, "attachment_unsupported_type", "This file type is not accepted."),
        # -- cursors ---------------------------------------------------------- #
        ErrorSpec(410, "cursor_expired", "The cursor is older than the retained history."),
        # -- transient -------------------------------------------------------- #
        ErrorSpec(
            503,
            "write_busy",
            "The workspace is busy; retry the same request.",
            retryable=True,
        ),
        ErrorSpec(
            503,
            "outcome_unknown",
            "The write outcome is unknown; retry the same request to confirm it.",
            retryable=True,
        ),
        ErrorSpec(
            503,
            "auth_temporarily_unavailable",
            "Authentication is temporarily unavailable.",
            retryable=True,
        ),
        # The object store is not answering (or is not configured). Retryable
        # because the client's bytes are fine; nothing was committed.
        ErrorSpec(
            503,
            "attachment_storage_unavailable",
            "Attachment storage is temporarily unavailable.",
            retryable=True,
        ),
        # -- server ----------------------------------------------------------- #
        ErrorSpec(500, "storage_failure", "The write could not be stored.", retryable=True),
        ErrorSpec(500, "internal", "An unexpected error occurred."),
    )
}

#: ``code -> HTTP status`` (convenience view; :data:`ERROR_SPECS` is the truth).
CODE_STATUS: Final[dict[str, int]] = {code: spec.status for code, spec in ERROR_SPECS.items()}

#: Kernel exception -> wire code. The two id-conflict classes collapse on
#: purpose (P2 #3): distinguishing them would disclose whether an id exists in
#: another workspace, and ``op_id`` reuse across workspaces is legitimate.
KERNEL_ERROR_CODES: Final[dict[type[SyncKernelError], str]] = {
    InvalidCommand: "invalid_request",
    WorkspaceNotFound: "not_found",
    EntityNotFound: "not_found",
    EntityIdConflict: "conflict",
    OpIdConflict: "conflict",
    RevConflict: "rev_conflict",
    EntityDeleted: "entity_deleted",
    InvalidParent: "invalid_parent",
    FolderCycle: "folder_cycle",
    FolderNotEmpty: "folder_not_empty",
    CounterOverflow: "counter_overflow",
    WriteBusy: "write_busy",
    CommitOutcomeUnknown: "outcome_unknown",
    StorageFailure: "storage_failure",
}

#: Codes a push may keep going after (``conflict``/``rejected``), as opposed to
#: the ones that stop the batch. Kept next to the vocabulary so the two cannot
#: drift apart when the push lane lands.
_STOP_CODES: Final[frozenset[str]] = frozenset(
    {"write_busy", "outcome_unknown", "storage_failure", "internal"}
)


def spec_for(code: str) -> ErrorSpec:
    """The spec for ``code``; unknown codes degrade to ``internal``, never 500-raise."""
    return ERROR_SPECS.get(code, ERROR_SPECS["internal"])


def get_request_id(request: Request) -> str:
    """The id :class:`RequestIdMiddleware` stored on the request state.

    Middleware always runs before a handler, so the attribute exists in
    production. Two paths can still reach an exception handler without it: a
    direct handler call in a test, and a 500 raised by middleware that sits above
    ``RequestIdMiddleware``. Both fall back to a fresh server id - never an empty
    string, never a placeholder that a client could mistake for a real correlation
    id, and never an invented value that differs from the ``X-Request-ID`` header
    :func:`error_response` sets from the same argument.
    """
    request_id = getattr(request.state, "request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id
    # Imported lazily: ``errors`` and the middleware package must stay importable
    # independently (the body limiter imports ``error_response`` inside a method).
    from revnote_api.api.middleware.request_id import new_request_id

    request_id = new_request_id()
    request.state.request_id = request_id
    return request_id


def _envelope(
    code: str,
    request_id: str,
    *,
    details: dict[str, Any] | None = None,
    message: str | None = None,
    retryable: bool | None = None,
) -> ErrorEnvelope:
    spec = spec_for(code)
    return ErrorEnvelope(
        error=ErrorBody(
            code=spec.code,
            message=spec.message if message is None else message,
            request_id=request_id,
            retryable=spec.retryable if retryable is None else retryable,
            details={} if details is None else details,
        )
    )


def error_response(
    code: str,
    request_id: str,
    *,
    status: int | None = None,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build the ``JSONResponse`` for ``code`` with the fixed envelope.

    ``status`` defaults to the code's own status; callers pass it only when the
    same code legitimately appears under two statuses (push per-op results carry
    no status at all, so they do not use this function).
    """
    spec = spec_for(code)
    envelope = _envelope(code, request_id, details=details)
    response_headers = {RequestIdHeader: request_id}
    if spec.status == 401:
        # RFC 6750: a 401 must say which scheme is expected.
        response_headers["WWW-Authenticate"] = "Bearer"
    if envelope.error.retryable and spec.status == 503:
        response_headers["Retry-After"] = "1"
    if headers:
        response_headers.update(headers)
    return JSONResponse(
        status_code=spec.status if status is None else status,
        content=envelope.model_dump(mode="json"),
        headers=response_headers,
    )


def kernel_error_response(exc: SyncKernelError, request_id: str) -> JSONResponse:
    """Map a :class:`SyncKernelError` onto its wire code and envelope.

    Only the class decides the code. The exception's own message is dropped: it
    may name ids, and PostgreSQL text can carry note content. Extra
    machine-readable facts (a fresh revision, an overflowed counter) go into
    ``details`` under fixed keys.
    """
    code = KERNEL_ERROR_CODES.get(type(exc), "internal")
    return error_response(code, request_id, details=_kernel_details(exc, code))


def _kernel_details(exc: SyncKernelError, code: str) -> dict[str, Any]:
    """Facts a client can act on, chosen by class - never free-form text."""
    if isinstance(exc, RevConflict):
        return {"expected_rev": str(exc.expected), "current_rev": str(exc.current)}
    if isinstance(exc, CounterOverflow):
        return {"counter": exc.counter}
    if code in {"write_busy", "outcome_unknown"}:
        # The client's only correct move is to re-send the identical op.
        return {"retry_same_op": True}
    return {}


def is_stop_code(code: str) -> bool:
    """True when a push batch must stop after an op with this code (plan §10)."""
    return code in _STOP_CODES


class ApiError(Exception):
    """A deliberate protocol rejection raised by a handler or dependency.

    Carries only a code from :data:`ERROR_SPECS` plus optional machine-readable
    ``details`` - never a message: the sentence shown to the client belongs to the
    code, so two paths cannot describe the same failure differently. Handlers
    raise this instead of building a response, which keeps endpoint code free of
    status numbers.
    """

    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        self.code = spec_for(code).code
        self.details = {} if details is None else details
        super().__init__(self.code)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ApiError(code={self.code!r})"


LOGGER: Final = logging.getLogger("revnote_api.error")

#: Placeholder for a ``loc`` segment that is not a declared field name.
#:
#: The replacement is deliberately constant (never a prefix of the original, never
#: a hash of it): a client that sends a secret *as a JSON key* must not be able to
#: identify it in the response, not even partially or by correlation.
REDACTED_SEGMENT: Final[str] = "<field>"

#: Hard bounds on the reported validation detail. A batch-of-50 push produces one
#: error per bad field; without a cap a hostile body could inflate the envelope far
#: past the request budget it was refused for.
MAX_REPORTED_FIELDS: Final[int] = 20

#: Longest ``loc`` prefix reported. Real paths are ``("body", "ops", 0, "payload",
#: "title")``-shaped; anything deeper is an attacker-chosen nest.
MAX_PATH_SEGMENTS: Final[int] = 8

#: Longest declared field name accepted as a literal. Field names come from our own
#: models, so this only guards against a future model with an absurd name.
MAX_FIELD_NAME_CHARS: Final[int] = 64

#: Longest reason string reported (Pydantic's own type codes are far shorter).
MAX_REASON_CHARS: Final[int] = 64

#: Static location markers FastAPI itself puts at ``loc[0]``. They are framework
#: vocabulary, not client data, so they are echoed as-is - but only in first
#: position, and only from this closed set.
LOCATION_MARKERS: Final[frozenset[str]] = frozenset({"body", "query", "path", "header", "cookie"})


def _declared_names(annotation: Any, *, depth: int = 0) -> frozenset[str]:
    """Every field name reachable from ``annotation``, plus container/index markers.

    This is the allowlist for ``loc`` segments: a string in ``loc`` is only echoed
    when it is one of *our* declared field names. An unknown key from the request
    body (``extra_forbidden`` reports it verbatim in ``loc``) is therefore never
    reflected - the value a client sent as a JSON key can itself be a password,
    a token or a note body, and echoing it is a leak with a 400 attached.

    Recursion is depth-bounded and cycle-safe so a pathological model cannot make
    the error path expensive.
    """
    if depth > 6:
        return frozenset()

    names: set[str] = set()
    origin = get_origin(annotation)

    # Optional[X] / X | None / Union[...] - unwrap to the members.
    if origin is Union or origin is types.UnionType:
        for member in get_args(annotation):
            names |= _declared_names(member, depth=depth + 1)
        return frozenset(names)

    # Annotated[X, ...] - the metadata is irrelevant here.
    if origin is Annotated:
        args = get_args(annotation)
        if args:
            names |= _declared_names(args[0], depth=depth + 1)
        return frozenset(names)

    # list[X] / tuple[X, ...] / set[X] - element fields are reachable.
    if origin in (list, tuple, set, frozenset):
        for member in get_args(annotation):
            names |= _declared_names(member, depth=depth + 1)
        return frozenset(names)

    # dict[str, X]: keys are client-chosen, so they can never be echoed. Only the
    # *value* shape contributes names (a dict of models can still report fields).
    if origin is dict:
        args = get_args(annotation)
        for member in args[1:]:
            names |= _declared_names(member, depth=depth + 1)
        return frozenset(names)

    fields = getattr(annotation, "model_fields", None)
    if isinstance(fields, dict):
        for name, info in fields.items():
            names.add(name)
            alias = getattr(info, "alias", None)
            if isinstance(alias, str) and alias:
                names.add(alias)
            names |= _declared_names(getattr(info, "annotation", None), depth=depth + 1)
    return frozenset(names)


def _route_field_names(request: Request) -> frozenset[str]:
    """Declared parameter and body-field names of the route serving ``request``.

    Read from the matched ``APIRoute``, which Starlette stores on the scope before
    calling the endpoint - so this is available inside the exception handler too.
    A request that matched no route (or a non-API route) yields an empty set, and
    every string segment is then redacted. Failing closed is correct: a 400 with
    ``"<field>"`` is still useful, an echoed secret is not.
    """
    route = request.scope.get("route")
    dependant = getattr(route, "dependant", None)
    if dependant is None:
        return frozenset()

    names: set[str] = set()
    pending = [dependant]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:  # cycle-safe: a dependency can be reused
            continue
        seen.add(id(current))
        for param in (
            *getattr(current, "path_params", ()),
            *getattr(current, "query_params", ()),
            *getattr(current, "header_params", ()),
            *getattr(current, "cookie_params", ()),
            *getattr(current, "body_params", ()),
        ):
            name = getattr(param, "name", None)
            if isinstance(name, str) and name:
                names.add(name)
            alias = getattr(param, "alias", None)
            if isinstance(alias, str) and alias:
                names.add(alias)
            field_info = getattr(param, "field_info", None)
            names |= _declared_names(getattr(field_info, "annotation", None))
        pending.extend(getattr(current, "dependencies", ()))
    return frozenset(names)


def _sanitize_loc(parts: Any, allowed: frozenset[str]) -> list[str | int]:
    """The reportable form of one ``loc`` tuple.

    Position 0 may be a FastAPI location marker (``body``/``query``/...), which is
    framework vocabulary from :data:`LOCATION_MARKERS`, not client data. Every
    later string is kept only when it is a declared field name of the route that
    was called; anything else becomes :data:`REDACTED_SEGMENT`. Integers are
    positional (``ops[0]``) and carry no client content, so they are kept.
    """
    cleaned: list[str | int] = []
    for index, part in enumerate(tuple(parts)[:MAX_PATH_SEGMENTS]):
        if isinstance(part, str):
            is_marker = index == 0 and part in LOCATION_MARKERS
            is_declared = part in allowed and len(part) <= MAX_FIELD_NAME_CHARS
            cleaned.append(part if is_marker or is_declared else REDACTED_SEGMENT)
        elif isinstance(part, int) and not isinstance(part, bool):
            cleaned.append(part)
        else:
            # dict keys, bytes, enums, ... - no safe representation, no report.
            cleaned.append(REDACTED_SEGMENT)
    return cleaned


def _validation_fields(
    exc: RequestValidationError, request: Request | None = None
) -> list[dict[str, Any]]:
    """Allowed field paths + stable reasons - never the input or Pydantic's ctx.

    ``loc`` is the only part of a Pydantic error that can be reported, and even
    that is not safe as-is: with ``extra="forbid"`` Pydantic puts the *offending
    JSON key* in ``loc`` (``("body", "hunter2-my-password")``), so string segments
    are matched against the declared field names of the matched route and replaced
    with :data:`REDACTED_SEGMENT` when unknown. Count, path depth, field-name and
    reason lengths are bounded so one hostile body cannot inflate the envelope.

    Nothing else from the error is used: ``input`` and ``ctx`` quote the rejected
    value, and ``msg`` interpolates it (plan §11.1).
    """
    allowed = _route_field_names(request) if request is not None else frozenset()
    fields: list[dict[str, Any]] = []
    for error in exc.errors()[:MAX_REPORTED_FIELDS]:
        path = _sanitize_loc(error.get("loc", ()), allowed)
        reason = str(error.get("type", "invalid"))[:MAX_REASON_CHARS]
        fields.append({"path": path, "reason": reason})
    return fields


def install_error_handlers(app: FastAPI) -> None:
    """Register the handlers that make every failure use the fixed envelope.

    Registered types, in the order Starlette resolves them:

    * :class:`ApiError` - deliberate protocol rejections.
    * :class:`SyncKernelError` - the kernel vocabulary, mapped by class.
    * :class:`RequestValidationError` - 400 with allowed field paths only.
    * ``StarletteHTTPException`` - 404/405 keep their status, envelope included.
    * ``Exception`` - 500 ``internal``, with no stack, no message, no cause.

    The last one is the reason this function exists: FastAPI's default 500 would
    be plain text, and a naive ``logger.exception`` there could print SQL
    parameters (note bodies). Here only the request id and the route template are
    logged.
    """

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.code, get_request_id(request), details=exc.details)

    @app.exception_handler(SyncKernelError)
    async def _kernel_error(request: Request, exc: SyncKernelError) -> JSONResponse:
        return kernel_error_response(exc, get_request_id(request))

    @app.exception_handler(StorageUnavailable)
    async def _storage_unavailable(request: Request, exc: StorageUnavailable) -> JSONResponse:
        """An unreachable or unconfigured object store is a 503, never a 500.

        This handler exists because of *where* the failure is raised. The attachment
        routes build their store in a FastAPI **dependency**, and dependencies resolve
        before the route body - so a deployment with ``ATTACHMENTS_ENABLED=true`` and no
        ``OBJECT_STORAGE_*`` reached the catch-all ``Exception`` handler and answered
        ``500 internal``. That reads as "the server is broken" for a configuration that
        is merely incomplete, and it contradicted the route contract's own promise of
        ``attachment_storage_unavailable`` (503, retryable).

        Only this class is mapped: a programming error must still surface as ``internal``
        rather than hide behind a reassuring "retry later". ``retryable`` and the
        ``Retry-After`` header come from :data:`ERROR_SPECS` through ``error_response``.
        """
        del exc  # the message can name an endpoint or a bucket; the code carries it
        return error_response("attachment_storage_unavailable", get_request_id(request))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(
            "invalid_request",
            get_request_id(request),
            details={"fields": _validation_fields(exc, request)},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        request_id = get_request_id(request)
        if exc.status_code == 405:
            return error_response("invalid_request", request_id, status=405)
        if exc.status_code >= 500:
            # A framework-level 5xx keeps its number with an ``internal`` body: it
            # is a route-level decision, not a kernel outcome, and inventing a code
            # for it would widen the protocol vocabulary.
            return error_response("internal", request_id, status=exc.status_code)
        # 404 (unmatched path), 401/403 raised by a dependency before this commit's
        # vocabulary existed, etc.: keep the status, use the closest fixed body.
        code = "not_found" if exc.status_code == 404 else "invalid_request"
        return error_response(code, request_id, status=exc.status_code)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Route *template*, not the raw path: a path can carry an id or a token.
        route = request.scope.get("route")
        template = getattr(route, "path", "unmatched")
        LOGGER.error(
            "unhandled_exception request_id=%s route=%s error_class=%s",
            get_request_id(request),
            template,
            type(exc).__name__,
        )
        return error_response("internal", get_request_id(request))
