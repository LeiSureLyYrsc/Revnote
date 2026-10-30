"""Credential and endpoint configuration for the reference CLI.

Where the token lives
---------------------
A single JSON file inside the user's configuration directory:

* ``REVNOTE_CONFIG_DIR`` when set (tests point it at ``D:\\tmp\\…``),
* ``%APPDATA%\\revnote`` on Windows,
* ``$XDG_CONFIG_HOME/revnote`` or ``~/.config/revnote`` elsewhere.

The file is created with mode ``0o600`` where the platform honours it, is written
atomically (temporary file + rename), and must **never** live inside a repository.
The default location is always a user configuration directory; a test or script
that points ``REVNOTE_CONFIG_DIR`` at a temporary directory owns the cleanup.
Nothing in this module logs a token.

The dataclass implements ``__repr__`` itself so an accidental ``repr`` of the
credentials prints field names and whether a token is present, never its value.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

__all__ = [
    "API_BASE_ENV",
    "CONFIG_DIR_ENV",
    "CONFIG_FILE_NAME",
    "DEFAULT_API_BASE",
    "CredentialStore",
    "StoredCredentials",
    "default_api_base",
    "resolve_config_dir",
]

CONFIG_DIR_ENV: Final[str] = "REVNOTE_CONFIG_DIR"
API_BASE_ENV: Final[str] = "REVNOTE_API_BASE"
CONFIG_FILE_NAME: Final[str] = "config.json"
DEFAULT_API_BASE: Final[str] = "http://127.0.0.1:8000"


def default_api_base() -> str:
    """``REVNOTE_API_BASE`` or the local development default."""
    return os.environ.get(API_BASE_ENV, "").strip() or DEFAULT_API_BASE


def resolve_config_dir(explicit: str | None = None) -> Path:
    """Directory that holds ``config.json``; precedence flag, env, platform default."""
    if explicit:
        return Path(explicit).expanduser()
    configured = os.environ.get(CONFIG_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "").strip()
        if appdata:
            return Path(appdata) / "revnote"
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    if xdg:
        return Path(xdg) / "revnote"
    return Path.home() / ".config" / "revnote"


@dataclass(slots=True)
class StoredCredentials:
    """Everything persisted between invocations. Tokens never appear in ``repr``."""

    api_base: str = DEFAULT_API_BASE
    access_token: str | None = field(default=None, repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    token_type: str = "bearer"
    expires_in: int | None = None
    obtained_at: str | None = None
    session_id: str | None = None
    session_expires_at: str | None = None
    workspace_id: str | None = None
    user_id: str | None = None

    def expires_at(self) -> datetime | None:
        """Absolute expiry of the access token, when both fields are parseable."""
        if self.expires_in is None or not self.obtained_at:
            return None
        try:
            obtained = datetime.fromisoformat(self.obtained_at)
        except ValueError:
            return None
        if obtained.tzinfo is None:
            obtained = obtained.replace(tzinfo=UTC)
        return obtained + timedelta(seconds=int(self.expires_in))

    def is_expiring(self, *, skew_seconds: int = 30) -> bool:
        """True when the token is absent, unparseable, or about to expire."""
        expiry = self.expires_at()
        if expiry is None:
            return True
        return expiry <= datetime.now(UTC) + timedelta(seconds=skew_seconds)

    def to_wire(self) -> dict[str, Any]:
        """The persisted JSON object (keys are stable and documented)."""
        return {
            "api_base": self.api_base,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "obtained_at": self.obtained_at,
            "session_id": self.session_id,
            "session_expires_at": self.session_expires_at,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
        }

    @classmethod
    def from_wire(cls, payload: dict[str, Any]) -> StoredCredentials:
        """Read a saved object, ignoring keys a newer CLI may have added."""
        return cls(
            api_base=str(payload.get("api_base") or DEFAULT_API_BASE),
            access_token=_text(payload.get("access_token")),
            refresh_token=_text(payload.get("refresh_token")),
            token_type=str(payload.get("token_type") or "bearer"),
            expires_in=_integer(payload.get("expires_in")),
            obtained_at=_text(payload.get("obtained_at")),
            session_id=_text(payload.get("session_id")),
            session_expires_at=_text(payload.get("session_expires_at")),
            workspace_id=_text(payload.get("workspace_id")),
            user_id=_text(payload.get("user_id")),
        )

    def __repr__(self) -> str:
        """Field list with presence flags instead of token values."""
        return (
            "StoredCredentials("
            f"api_base={self.api_base!r}, "
            f"access_token={'present' if self.access_token else 'absent'}, "
            f"refresh_token={'present' if self.refresh_token else 'absent'}, "
            f"session_id={self.session_id!r}, "
            f"workspace_id={self.workspace_id!r})"
        )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


class CredentialStore:
    """Load, save and delete ``config.json`` in one directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.path = directory / CONFIG_FILE_NAME

    def load(self) -> StoredCredentials | None:
        """The saved credentials, or ``None`` when the file is absent/corrupt.

        A corrupt file is not an error the user must see on every command: the
        caller treats it as "not logged in" and ``login`` overwrites it.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        return StoredCredentials.from_wire(payload)

    def save(self, credentials: StoredCredentials) -> None:
        """Atomically replace the file with the given credentials."""
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        data = json.dumps(credentials.to_wire(), indent=2) + "\n"
        temporary.write_text(data, encoding="utf-8")
        _restrict(temporary)
        os.replace(temporary, self.path)

    def clear(self) -> bool:
        """Delete the file; ``True`` when something was removed."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        return True


def _restrict(path: Path) -> None:
    """Best-effort owner-only permissions; Windows ACLs are left to the user profile."""
    if os.name == "posix":
        with suppress(OSError):
            path.chmod(0o600)
