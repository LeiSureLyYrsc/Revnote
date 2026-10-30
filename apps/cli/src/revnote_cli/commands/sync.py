"""Sync commands: paged ``snapshot``, ``pull`` and minimal ``push``.

Push is deliberately small: a raw batch file for anything scripted, plus note
create/update convenience flags for the common case. A conflict is never retried,
merged or resolved by choosing a side; the process exits 4 and names
``rev_conflict``. Folder operations remain available through ``--ops-file`` - the
eight kernel shapes are the protocol, and the CLI does not invent a ninth.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from revnote_cli.client import RevnoteClient
from revnote_cli.commands import CommandResult, quote, resolve_workspace
from revnote_cli.errors import EXIT_API, EXIT_CONFLICT, EXIT_OK, EXIT_USAGE, CliError

__all__ = ["pull", "push", "snapshot"]


def snapshot(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    workspace_id = resolve_workspace(client, args.workspace)
    payload = client.snapshot(workspace_id, page_size=args.page_size)
    objects = payload.get("objects")
    objects = objects if isinstance(objects, list) else []
    lines = [
        f"as_of_seq={payload.get('as_of_seq')} "
        f"min_retained_seq={payload.get('min_retained_seq')} "
        f"objects={len(objects)} pages={payload.get('pages')} "
        f"paged={str(bool(payload.get('paged'))).lower()}"
    ]
    for value in objects:
        if not isinstance(value, dict):
            continue
        deleted = str(bool(value.get("deleted"))).lower()
        if value.get("entity_type") == "note":
            lines.append(
                f"note {value.get('id', '?')} rev={value.get('rev', '?')} "
                f"title={quote(value.get('title', ''))} "
                f"folder={value.get('folder_id') or '-'} deleted={deleted}"
            )
        else:
            lines.append(
                f"folder {value.get('id', '?')} rev={value.get('rev', '?')} "
                f"name={quote(value.get('name', ''))} "
                f"parent={value.get('parent_id') or '-'} deleted={deleted}"
            )
    return CommandResult(payload={"workspace_id": workspace_id, **payload}, lines=lines)


def pull(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    workspace_id = resolve_workspace(client, args.workspace)
    payload = client.pull(
        workspace_id,
        since=args.since,
        limit=args.limit,
        drain=not args.once,
    )
    events = payload.get("events")
    events = events if isinstance(events, list) else []
    objects = payload.get("objects")
    objects = objects if isinstance(objects, list) else []
    lines = [
        f"since={payload.get('since')} next_cursor={payload.get('next_cursor')} "
        f"last_seq={payload.get('last_seq')} has_more={str(bool(payload.get('has_more'))).lower()} "
        f"events={len(events)} objects={len(objects)} pages={payload.get('pages')}"
    ]
    for value in events:
        if not isinstance(value, dict):
            continue
        lines.append(
            f"event {value.get('seq', '?')} {value.get('op', '?')} "
            f"{value.get('entity_type', '?')} {value.get('entity_id', '?')} "
            f"rev={value.get('rev', '?')}"
        )
    return CommandResult(payload={"workspace_id": workspace_id, **payload}, lines=lines)


def push(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    workspace_id = resolve_workspace(client, args.workspace)
    ops, created_note_id = _collect_ops(args)
    payload = client.push(workspace_id, ops)
    results = payload.get("results")
    results = results if isinstance(results, list) else []

    exit_code = EXIT_OK
    error_code: str | None = None
    error_message: str | None = None
    lines: list[str] = []
    for value in results:
        if not isinstance(value, dict):
            continue
        status = value.get("status", "?")
        error = value.get("error") if isinstance(value.get("error"), dict) else {}
        code = error.get("code") if isinstance(error.get("code"), str) else None
        lines.append(
            f"op {value.get('op_id', '?')} status={status} "
            f"seq={value.get('seq') or '-'} rev={value.get('rev') or '-'} "
            f"code={code or '-'}"
        )
        if status == "conflict":
            if exit_code != EXIT_CONFLICT:
                exit_code = EXIT_CONFLICT
                error_code = code or "rev_conflict"
                error_message = (
                    error.get("message")
                    if isinstance(error.get("message"), str)
                    else "The object changed since the given revision."
                )
        elif status in {"rejected", "unknown", "not_attempted"} and exit_code == EXIT_OK:
            exit_code = EXIT_API
            error_code = code or status
            error_message = (
                error.get("message")
                if isinstance(error.get("message"), str)
                else f"The server reported {status} for an operation."
            )

    result_payload: dict[str, Any] = {"workspace_id": workspace_id, **payload}
    if created_note_id:
        # The push result does not echo the entity id, so the convenience command
        # reports the id it chose. A script needs it to read the note it just made.
        result_payload["created_note_id"] = created_note_id
        lines.append(f"created note {created_note_id}")

    return CommandResult(
        payload=result_payload,
        lines=lines or ["(no operations)"],
        exit_code=exit_code,
        error_code=error_code,
        error_message=error_message,
    )


def _collect_ops(args: argparse.Namespace) -> tuple[list[dict[str, Any]], str | None]:
    """Operations plus the note id a create command chose (``None`` otherwise)."""
    if args.ops_file:
        if getattr(args, "push_command", None) is not None:
            raise CliError(
                "--ops-file cannot be combined with a note subcommand.",
                code="usage",
                exit_code=EXIT_USAGE,
            )
        return _load_ops(Path(args.ops_file)), None

    if getattr(args, "push_command", None) != "note":
        raise CliError(
            "sync push needs --ops-file or a `note create|update` subcommand.",
            code="usage",
            exit_code=EXIT_USAGE,
        )

    if args.note_command == "create":
        entity_id = args.entity_id or str(uuid.uuid4())
        payload: dict[str, Any] = {
            "title": args.title,
            "body_md": _read_body(args),
        }
        if args.folder_id:
            payload["folder_id"] = args.folder_id
        return (
            [
                {
                    "op_id": str(uuid.uuid4()),
                    "entity_type": "note",
                    "op": "create",
                    "entity_id": entity_id,
                    "payload": payload,
                }
            ],
            entity_id,
        )

    payload = {"title": args.title, "body_md": _read_body(args)}
    return (
        [
            {
                "op_id": str(uuid.uuid4()),
                "entity_type": "note",
                "op": "update",
                "entity_id": args.note_id,
                "base_rev": args.base_rev,
                "payload": payload,
            }
        ],
        None,
    )


def _read_body(args: argparse.Namespace) -> str:
    if args.body_file and args.body_text is not None:
        raise CliError(
            "--body-file and --body-text are mutually exclusive.",
            code="usage",
            exit_code=EXIT_USAGE,
        )
    if args.body_file:
        try:
            return Path(args.body_file).read_text(encoding="utf-8")
        except OSError:
            raise CliError(
                f"Could not read --body-file {quote(args.body_file)}.",
                code="usage",
                exit_code=EXIT_USAGE,
            ) from None
    if args.body_text is not None:
        return args.body_text
    return ""


def _load_ops(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        raise CliError(
            f"Could not read --ops-file {quote(str(path))}.",
            code="usage",
            exit_code=EXIT_USAGE,
        ) from None
    except json.JSONDecodeError:
        raise CliError(
            "--ops-file is not valid JSON.", code="usage", exit_code=EXIT_USAGE
        ) from None

    if isinstance(document, dict):
        document = document.get("ops")
    if not isinstance(document, list) or not document:
        raise CliError(
            '--ops-file must be a non-empty array or {"ops": [...]}.',
            code="usage",
            exit_code=EXIT_USAGE,
        )
    ops: list[dict[str, Any]] = []
    for value in document:
        if not isinstance(value, dict):
            raise CliError(
                "--ops-file contains a non-object operation.",
                code="usage",
                exit_code=EXIT_USAGE,
            )
        ops.append(value)
    return ops
