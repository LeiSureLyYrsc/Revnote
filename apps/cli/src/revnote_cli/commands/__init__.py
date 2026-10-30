"""Command handlers and the shared result shape.

Every handler returns a :class:`CommandResult`: the stable JSON payload, the
human-readable lines, and an exit code. Handlers never print and never exit; the
entry point owns both, which is what makes the same functions testable in-process
and usable behind the console script.

A push batch is the one command where "the request succeeded" and "the command
succeeded" differ: the server answers ``200`` with a per-op status. The handler
therefore returns exit 4 (any ``conflict``) or 5 (``rejected``/``unknown``/
``not_attempted``) while still rendering every result, and the entry point writes
a matching error object to stderr so a script sees both the detail and the code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from revnote_cli.client import RevnoteClient
from revnote_cli.errors import EXIT_OK, EXIT_USAGE, CliError

__all__ = [
    "CommandResult",
    "quote",
    "resolve_workspace",
]


@dataclass(slots=True)
class CommandResult:
    """What one command produced: one JSON document, text lines, and an exit code."""

    payload: dict[str, Any] = field(default_factory=dict)
    lines: list[str] = field(default_factory=list)
    exit_code: int = EXIT_OK
    error_code: str | None = None
    error_message: str | None = None


def quote(value: Any) -> str:
    """Render a user-supplied string on one line, escaped and bounded by quotes.

    Titles, folder names and file names may contain newlines or quotes; a text
    output that pasted them raw would be ambiguous to read and impossible to parse
    line by line. JSON escaping keeps every record on one line.
    """
    return json.dumps(value, ensure_ascii=False)


def resolve_workspace(client: RevnoteClient, explicit: str | None) -> str:
    """``--workspace`` > saved default > the only membership > usage error.

    The resolved id is persisted when it came from a single-membership ``/me``,
    so later invocations need no flag. ``me`` is only called when the id is not
    already known.
    """
    if explicit:
        return explicit
    saved = client.credentials.workspace_id
    if saved:
        return saved
    memberships = client.me().get("workspaces")
    if not isinstance(memberships, list):
        raise CliError(
            "The server returned an invalid membership list.",
            code="invalid_response",
            exit_code=EXIT_USAGE,
        )
    if len(memberships) == 1:
        membership = memberships[0]
        if isinstance(membership, dict) and isinstance(membership.get("id"), str):
            workspace_id = membership["id"]
            client.credentials.workspace_id = workspace_id
            client.store.save(client.credentials)
            return workspace_id
    raise CliError(
        "Several workspaces are available; pass --workspace <id>.",
        code="workspace_required",
        exit_code=EXIT_USAGE,
    )
