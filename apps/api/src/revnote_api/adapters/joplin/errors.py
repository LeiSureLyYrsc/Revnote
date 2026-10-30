"""Joplin's error envelope, and the explicit mapping from native codes.

A Joplin client parses ``{"error": "<message>", "code": <string|number>}`` on a
non-2xx response and puts ``code`` on the thrown error object. Two code strings
carry behaviour:

* ``resyncRequired`` — discard the delta cursor and sync from scratch;
* ``hasExclusiveLock`` / ``hasSyncLock`` — the lock handler waits instead of
  aborting.

Everything else the client treats as a numeric HTTP status (404 is "gone", 409
and 422 are "permanently rejected", 5xx is retried with backoff). That is why
:data:`STATUS_BY_CODE` is a *table* rather than an ``if`` ladder: a new native
code must be assigned a Joplin status deliberately, and the unit test asserts
that every code the adapter can raise has an entry.

The native envelope (``revnote_api.api.errors``) is untouched: it is what the
native ``/api/v1`` surface keeps returning, and the bridge is a parallel face.
"""

from __future__ import annotations

from typing import Final

from fastapi.responses import JSONResponse

__all__ = [
    "ERROR_MESSAGE",
    "JOPLIN_ERROR_STATUS",
    "JoplinProtocolError",
    "joplin_response",
    "translate_native",
]

#: The fixed sentences a Joplin client may display. No ids, no user input, no
#: server text: the client logs the body verbatim.
ERROR_MESSAGE: Final[dict[str, str]] = {
    "bad_request": "The request does not match the Joplin Server contract.",
    "unauthenticated": "Invalid or missing session.",
    "invalid_credentials": "The e-mail or password is incorrect.",
    "not_found": "Not found.",
    "conflict": "The item conflicts with the recorded state.",
    "rev_conflict": "The item changed since it was read.",
    "rejected": "The sync target permanently refused this item.",
    "too_large": "The request body is too large.",
    "unsupported_type": "This resource type is not accepted by this deployment.",
    "storage_unavailable": "Storage is temporarily unavailable.",
    "internal": "An unexpected error occurred.",
    "resync_required": "Delta cursor is invalid and the complete data should be resynced.",
    "has_exclusive_lock": "An exclusive sync lock is held by another client.",
    "has_sync_lock": "A sync lock is held by another client.",
    "bridge_disabled": "The Joplin compatibility bridge is not enabled.",
}

#: Native/adaptor code -> HTTP status the client understands.
JOPLIN_ERROR_STATUS: Final[dict[str, int]] = {
    "bad_request": 400,
    "unauthenticated": 401,
    "invalid_credentials": 401,
    "not_found": 404,
    "conflict": 409,
    "rev_conflict": 409,
    "rejected": 409,
    "has_exclusive_lock": 409,
    "has_sync_lock": 409,
    "too_large": 413,
    "unsupported_type": 422,
    "storage_unavailable": 503,
    "internal": 500,
    "resync_required": 400,
    "bridge_disabled": 404,
}

#: Code strings the client compares by name. Everything else is numeric-only.
_NAMED_CODES: Final[dict[str, str]] = {
    "resync_required": "resyncRequired",
    "has_exclusive_lock": "hasExclusiveLock",
    "has_sync_lock": "hasSyncLock",
}

#: Native application codes -> bridge codes. Every branch is deliberate.
NATIVE_CODE_MAP: Final[dict[str, str]] = {
    "invalid_request": "bad_request",
    "unauthenticated": "unauthenticated",
    "invalid_credentials": "invalid_credentials",
    "not_found": "not_found",
    "conflict": "conflict",
    "rev_conflict": "rev_conflict",
    "entity_deleted": "rejected",
    "folder_not_empty": "rejected",
    "invalid_parent": "rejected",
    "folder_cycle": "rejected",
    "payload_too_large": "too_large",
    "attachment_too_large": "too_large",
    "attachment_unsupported_type": "unsupported_type",
    "attachment_already_ready": "conflict",
    "attachment_expired": "rejected",
    "attachment_hash_mismatch": "rejected",
    "attachment_storage_unavailable": "storage_unavailable",
    "write_busy": "storage_unavailable",
    "outcome_unknown": "storage_unavailable",
    "storage_failure": "storage_unavailable",
    "internal": "internal",
}


class JoplinProtocolError(Exception):
    """A failure that must reach the client as a Joplin error object.

    The message is never taken from the exception of a lower layer: a database
    message can contain a value and a value can be a note body. Only the fixed
    sentences in :data:`ERROR_MESSAGE` are rendered.
    """

    __slots__ = ("code", "status")

    def __init__(self, code: str, *, status: int | None = None) -> None:
        if code not in JOPLIN_ERROR_STATUS:
            raise ValueError(f"unknown Joplin error code {code!r}")
        self.code = code
        self.status = JOPLIN_ERROR_STATUS[code] if status is None else status
        super().__init__(code)

    @property
    def wire_code(self) -> str:
        """The ``code`` field: a named string only for the three special cases."""
        return _NAMED_CODES.get(self.code, self.code)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"JoplinProtocolError(code={self.code!r}, status={self.status!r})"


def translate_native(code: str) -> JoplinProtocolError:
    """Map a native error code onto the bridge's vocabulary."""
    return JoplinProtocolError(NATIVE_CODE_MAP.get(code, "internal"))


def joplin_response(error: JoplinProtocolError) -> JSONResponse:
    """Render one error as the client expects it."""
    return JSONResponse(
        status_code=error.status,
        content={"error": ERROR_MESSAGE[error.code], "code": error.wire_code},
    )
