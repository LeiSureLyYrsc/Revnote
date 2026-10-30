"""Exit codes and the one exception type the CLI raises.

The table is part of the published contract (``docs/phase8-reference-cli.md``) and
is frozen here so a script can branch on a number instead of parsing prose:

=====  ====================================================================
0      success
1      unexpected internal error
2      usage / argument error (argparse also exits 2)
3      authentication failure (bad credentials, expired session, no login)
4      conflict (``rev_conflict``, ``entity_deleted``, other 409s, per-op
       push ``conflict``)
5      API or network failure (any other non-2xx, transport error)
6      not found (a missing note or workspace the caller may not see)
=====  ====================================================================

Server error codes map deterministically onto these values, so
``revnote sync push`` fails with 4 for a conflict exactly like a bare 409 does.
Nothing here ever prints a token: messages are the server's fixed text or a
CLI-authored sentence naming only a code and a request id.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "EXIT_API",
    "EXIT_AUTH",
    "EXIT_CONFLICT",
    "EXIT_INTERNAL",
    "EXIT_NOT_FOUND",
    "EXIT_OK",
    "EXIT_USAGE",
    "CliError",
    "exit_code_for",
]

EXIT_OK: Final[int] = 0
EXIT_INTERNAL: Final[int] = 1
EXIT_USAGE: Final[int] = 2
EXIT_AUTH: Final[int] = 3
EXIT_CONFLICT: Final[int] = 4
EXIT_API: Final[int] = 5
EXIT_NOT_FOUND: Final[int] = 6

#: Authentication-shaped codes: credentials, refresh family, missing token.
_AUTH_CODES: Final[frozenset[str]] = frozenset(
    {
        "invalid_credentials",
        "invalid_refresh",
        "token_reused",
        "unauthenticated",
        "registration_closed",
        "auth_temporarily_unavailable",
    }
)

#: Codes the server publishes with status 409, plus the per-op push status.
_CONFLICT_CODES: Final[frozenset[str]] = frozenset(
    {
        "conflict",
        "rev_conflict",
        "entity_deleted",
        "folder_not_empty",
        "invalid_parent",
        "folder_cycle",
        "object_too_large",
        "snapshot_too_large",
        "snapshot_total_too_large",
        "attachment_already_ready",
        "attachment_hash_mismatch",
        "attachment_expired",
    }
)


def exit_code_for(code: str | None) -> int:
    """Map one server error code onto the frozen exit table."""
    if code in _AUTH_CODES:
        return EXIT_AUTH
    if code in _CONFLICT_CODES:
        return EXIT_CONFLICT
    if code == "not_found":
        return EXIT_NOT_FOUND
    return EXIT_API


class CliError(Exception):
    """A failure that ends the command with a documented exit code.

    ``code`` is the server's stable error code when there is one (``rev_conflict``),
    or a CLI-authored one for local failures (``not_logged_in``, ``usage``,
    ``network_error``, ``invalid_response``). ``details`` is echoed only when the
    caller asked for JSON and only carries scalar values the server already
    published; no request body, note text or credential is ever attached.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        exit_code: int | None = None,
        status: int | None = None,
        request_id: str | None = None,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code_for(code) if exit_code is None else exit_code
        self.status = status
        self.request_id = request_id
        self.retryable = retryable
        self.details = details or {}

    def as_json(self) -> dict[str, Any]:
        """The stable error object printed by ``--json`` (no secrets, no bodies)."""
        payload: dict[str, Any] = {"code": self.code or "cli_error", "message": self.message}
        if self.status is not None:
            payload["status"] = self.status
        if self.request_id is not None:
            payload["request_id"] = self.request_id
        if self.retryable is not None:
            payload["retryable"] = self.retryable
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:
        return self.message
