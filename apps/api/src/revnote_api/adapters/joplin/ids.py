"""Joplin item keys: 32 lowercase hex characters, never a second identity.

A Joplin item is named by its ``id``, and the client validates the shape itself
(``/^[a-fA-F0-9]{32}$/``). Revnote's authoritative id is a UUID. The bridge
therefore derives the Joplin key of a native entity from the UUID — ``uuid.hex``
is exactly 32 hex characters — and only stores a row in ``joplin_id_map`` when
a *client* chose the key (a note or folder it created).

Resolution is therefore two-step and has no double authority:

1. a row in ``joplin_id_map`` wins;
2. otherwise the key is read as ``uuid.UUID(hex=…)`` and the entity is looked up
   by that UUID inside the caller's workspace.

The second step means a client could name an item after an existing native UUID
and address it. That client is already a workspace member and could read the
item through the native API, so it is not an escalation; it is documented in
``docs/phase6-joplin-bridge.md`` rather than silently possible.
"""

from __future__ import annotations

import re
import uuid

__all__ = ["JOP_KEY_RE", "is_jop_key", "jop_key_for_uuid", "uuid_from_jop_key"]

#: Exactly the shape the client's own ``unserialize`` enforces.
JOP_KEY_RE = re.compile(r"^[0-9a-f]{32}$")

_UUID_HEX_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def is_jop_key(value: object) -> bool:
    """True when ``value`` is a canonical (lowercase) Joplin item key."""
    return isinstance(value, str) and JOP_KEY_RE.fullmatch(value) is not None


def jop_key_for_uuid(entity_id: uuid.UUID) -> str:
    """The deterministic Joplin key of a native entity."""
    return entity_id.hex


def uuid_from_jop_key(value: str) -> uuid.UUID | None:
    """Read a key as a native UUID, or ``None`` when it cannot be one.

    Case-insensitive on input (a server may hand out uppercase ids) but the
    caller always sees the canonical UUID.
    """
    if not isinstance(value, str) or _UUID_HEX_RE.fullmatch(value) is None:
        return None
    try:
        return uuid.UUID(hex=value)
    except ValueError:  # pragma: no cover - the regex already guarantees the shape
        return None
