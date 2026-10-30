"""Output rendering: one JSON document on stdout, or plain text lines.

``--json`` is a **single JSON document per invocation**, never JSON Lines. That
choice is frozen in the contract because both are defensible and only one can be
the documented one: a single object keeps ``jq '.objects | length'`` working and
cannot be confused with a stream of pages. Sequence numbers and revisions stay
decimal *strings* everywhere (the server's own wire rule), so a value above
2**53 never loses precision in a shell pipeline.

Failures go to stderr: JSON mode prints one ``{"error": {...}}`` object, text
mode prints ``revnote: <code>: <message>`` plus a request id when present. Tokens
are never part of any payload.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

__all__ = ["emit_error", "emit_json", "emit_text"]


def emit_json(payload: Any, *, stream: TextIO | None = None) -> None:
    """Write one JSON document with a trailing newline (UTF-8, unescaped)."""
    target = stream or sys.stdout
    target.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    target.flush()


def emit_text(lines: list[str], *, stream: TextIO | None = None) -> None:
    """Write text lines; an empty list writes nothing."""
    target = stream or sys.stdout
    if lines:
        target.write("\n".join(lines) + "\n")
    target.flush()


def emit_error(error: Any, *, json_mode: bool, stream: TextIO | None = None) -> None:
    """Render a :class:`revnote_cli.errors.CliError` on stderr."""
    target = stream or sys.stderr
    if json_mode:
        payload = {"error": error.as_json()}
        target.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    else:
        parts = [f"revnote: {error.code or 'error'}: {error.message}"]
        if error.request_id:
            parts.append(f"(request_id={error.request_id})")
        target.write(" ".join(parts) + "\n")
    target.flush()
