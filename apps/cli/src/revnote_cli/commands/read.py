"""Read-only commands: ``tree``, ``note get`` and ``capabilities``."""

from __future__ import annotations

import argparse
from typing import Any

from revnote_cli.client import RevnoteClient
from revnote_cli.commands import CommandResult, quote, resolve_workspace

__all__ = ["capabilities", "note_get", "tree"]


def _objects(payload: dict[str, Any], field: str) -> list[dict[str, Any]]:
    values = payload.get(field)
    if not isinstance(values, list):
        return []
    return [value for value in values if isinstance(value, dict)]


def tree(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    workspace_id = resolve_workspace(client, args.workspace)
    payload = client.tree(workspace_id)
    lines: list[str] = []
    for folder in _objects(payload, "folders"):
        lines.append(
            f"folder {folder.get('id', '?')} name={quote(folder.get('name', ''))} "
            f"rev={folder.get('rev', '?')} parent={folder.get('parent_id') or '-'}"
        )
    for note in _objects(payload, "notes"):
        lines.append(
            f"note {note.get('id', '?')} title={quote(note.get('title', ''))} "
            f"rev={note.get('rev', '?')} folder={note.get('folder_id') or '-'}"
        )
    return CommandResult(
        payload={"workspace_id": workspace_id, **payload},
        lines=lines or ["(empty tree)"],
    )


def note_get(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    workspace_id = resolve_workspace(client, args.workspace)
    payload = client.note(workspace_id, args.note_id)
    header = (
        f"note {payload.get('id', '?')} rev={payload.get('rev', '?')} "
        f"title={quote(payload.get('title', ''))}"
    )
    body = payload.get("body_md")
    lines = [header, "", body if isinstance(body, str) else ""]
    return CommandResult(payload={"workspace_id": workspace_id, **payload}, lines=lines)


def capabilities(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    del args
    payload = client.capabilities()
    attachments = payload.get("attachments")
    attachments = attachments if isinstance(attachments, dict) else {}
    bridge = payload.get("joplin_bridge")
    bridge = bridge if isinstance(bridge, dict) else {}
    pagination = payload.get("snapshot_pagination")
    pagination = pagination if isinstance(pagination, dict) else {}
    lines = [
        f"attachments_enabled={str(bool(attachments.get('enabled'))).lower()} "
        f"max_bytes={attachments.get('max_bytes', '?')}",
        f"joplin_bridge_enabled={str(bool(bridge.get('enabled'))).lower()} "
        f"protocol_baseline={bridge.get('protocol_baseline', '?')}",
        f"snapshot_pagination_enabled={str(bool(pagination.get('enabled'))).lower()} "
        f"page_max_objects={pagination.get('page_max_objects', '?')} "
        f"page_max_bytes={pagination.get('page_max_bytes', '?')} "
        f"max_total_objects={pagination.get('max_total_objects', '?')}",
    ]
    return CommandResult(payload=payload, lines=lines)
