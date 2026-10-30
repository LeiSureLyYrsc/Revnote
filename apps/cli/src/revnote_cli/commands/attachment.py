"""Minimal attachment commands: ``upload``, ``download`` and ``info``.

These are the Phase 5 proxy endpoints, not presigned URLs and not a second storage
client: the CLI creates a pending row, PUTs the bytes to the relative target the
server returned, and lets the server hash and publish the object. Downloads stream
the proxy response to a file. The API's own attachment suite remains the storage
proof; these commands are the second-consumer check of the public surface.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from revnote_cli.client import RevnoteClient, guess_mime
from revnote_cli.commands import CommandResult, quote, resolve_workspace
from revnote_cli.errors import EXIT_USAGE, CliError

__all__ = ["download", "info", "upload"]


def upload(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    workspace_id = resolve_workspace(client, args.workspace)
    source = Path(args.path)
    if not source.is_file():
        raise CliError(f"No such file: {quote(args.path)}.", code="usage", exit_code=EXIT_USAGE)
    byte_size = source.stat().st_size
    if byte_size < 1:
        raise CliError("An empty file cannot be uploaded.", code="usage", exit_code=EXIT_USAGE)
    mime = args.mime or guess_mime(source)
    created = client.attachment_create(
        workspace_id,
        mime=mime,
        byte_size=byte_size,
        file_name=args.name or source.name,
    )
    attachment_id = str(created["attachment_id"])
    uploaded = client.attachment_upload(
        workspace_id,
        attachment_id,
        source,
        mime=mime,
    )
    return CommandResult(
        payload={
            "attachment_id": attachment_id,
            "state": uploaded.get("state"),
            "byte_size": uploaded.get("byte_size"),
            "content_hash": uploaded.get("content_hash"),
            "mime": mime,
            "file_name": args.name or source.name,
        },
        lines=[
            f"attachment {attachment_id} state={uploaded.get('state', '?')} "
            f"byte_size={uploaded.get('byte_size', '?')} "
            f"content_hash={uploaded.get('content_hash', '?')}"
        ],
    )


def download(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    destination = Path(args.output)
    if destination.exists() and destination.is_dir():
        raise CliError(
            f"--output is a directory: {quote(args.output)}.",
            code="usage",
            exit_code=EXIT_USAGE,
        )
    written = client.attachment_download(args.attachment_id, destination)
    return CommandResult(
        payload={
            "attachment_id": args.attachment_id,
            "path": str(destination),
            "byte_size": written,
        },
        lines=[f"downloaded {args.attachment_id} bytes={written} path={quote(str(destination))}"],
    )


def info(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    payload = client.attachment_info(args.attachment_id)
    lines = [
        f"attachment {payload.get('id', '?')} state={payload.get('state', '?')} "
        f"mime={payload.get('mime', '?')} byte_size={payload.get('byte_size', '?')} "
        f"content_hash={payload.get('content_hash', '?')} "
        f"file_name={quote(payload.get('file_name', ''))}"
    ]
    return CommandResult(payload=payload, lines=lines)
