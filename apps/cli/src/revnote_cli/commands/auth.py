"""``login``, ``logout``, ``refresh`` and ``me``."""

from __future__ import annotations

import argparse
from typing import Any

from revnote_cli.client import RevnoteClient
from revnote_cli.commands import CommandResult, quote

__all__ = ["login", "logout", "me", "refresh"]


def _memberships(payload: dict[str, Any]) -> list[dict[str, Any]]:
    workspaces = payload.get("workspaces")
    if not isinstance(workspaces, list):
        return []
    return [item for item in workspaces if isinstance(item, dict)]


def login(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    """Persist a token pair, then record the workspace when there is exactly one."""
    token_pair = client.login(
        args.email,
        args.password,
        device_name=args.device_name,
    )
    identity = client.me()
    workspaces = _memberships(identity)
    if not client.credentials.workspace_id and len(workspaces) == 1:
        workspace_id = workspaces[0].get("id")
        if isinstance(workspace_id, str):
            client.credentials.workspace_id = workspace_id
            client.store.save(client.credentials)

    user: dict[str, Any] = {}
    raw_user = identity.get("user")
    if isinstance(raw_user, dict):
        user = raw_user
    email = user.get("email") if isinstance(user.get("email"), str) else args.email
    user_id = user.get("id") if isinstance(user.get("id"), str) else "(unknown)"
    lines = [f"logged in as {quote(email)} (user {user_id})"]
    for workspace in workspaces:
        lines.append(f"workspace {workspace.get('id', '?')} role={workspace.get('role', '?')}")
    if not workspaces:
        lines.append("workspace (none)")

    return CommandResult(
        payload={
            "user": identity.get("user"),
            "workspaces": identity.get("workspaces"),
            "workspace_id": client.credentials.workspace_id,
            "expires_in": token_pair.get("expires_in"),
            "session_id": token_pair.get("session_id"),
            "session_expires_at": token_pair.get("session_expires_at"),
        },
        lines=lines,
    )


def logout(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    del args
    revoked = client.logout()
    return CommandResult(
        payload={"logged_out": True, "session_revoked": revoked},
        lines=["logged out" if revoked else "no saved session"],
    )


def refresh(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    del args
    payload = client.refresh()
    return CommandResult(
        payload={
            "expires_in": payload.get("expires_in"),
            "session_id": payload.get("session_id"),
            "session_expires_at": payload.get("session_expires_at"),
        },
        lines=[f"token refreshed; expires_in={payload.get('expires_in', '?')}"],
    )


def me(client: RevnoteClient, args: argparse.Namespace) -> CommandResult:
    del args
    payload = client.me()
    user: dict[str, Any] = {}
    raw_user = payload.get("user")
    if isinstance(raw_user, dict):
        user = raw_user
    lines = [f"user {user.get('id', '?')} {quote(user.get('email', ''))}"]
    for workspace in _memberships(payload):
        lines.append(f"workspace {workspace.get('id', '?')} role={workspace.get('role', '?')}")
    return CommandResult(payload=payload, lines=lines or ["user (none)"])
