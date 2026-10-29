"""Deterministic request fingerprints (D4).

The ledger stores a 32-byte SHA-256 of the **canonical JSON** encoding of a
command. Same ``op_id`` + same hash ⇒ replay (return the first result). Same
``op_id`` + different hash ⇒ :class:`revnote_api.kernel.errors.OpIdConflict`.

Canonical form (§5), versioned by :data:`HASH_SCHEMA_VERSION`:

* the payload is a flat mapping that always carries ``hash_schema_version``,
  ``entity_type``, ``op``, ``workspace_id``, ``entity_id``, ``base_rev`` plus
  every business field of the operation (explicit ``null`` when absent —
  defaults are expanded first, so "no parent" and "parent omitted" are one form);
* encoded with ``json.dumps(sort_keys=True, separators=(",", ":"))`` and UTF-8,
  i.e. fixed key ordering, fixed separators, no insignificant whitespace;
* UUIDs are the standard lowercase hyphenated strings;
* integers are lossless decimal (never float);
* text is verbatim: no trimming, no newline conversion, no Unicode normalisation;
* NUL, ``float``/``NaN``/``Infinity``, ``bool``, and any unknown type are
  **rejected** rather than stringified — a hash that a different command could
  also produce is worthless;
* ``op_id`` is deliberately not hashed: it is the external key the hash is looked
  up by. Changing ``base_rev`` therefore makes a *new* command that needs a new
  ``op_id`` (§5).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from typing import Any

from revnote_api.kernel.commands import Command
from revnote_api.kernel.errors import InvalidCommand

__all__ = [
    "HASH_BYTES",
    "HASH_SCHEMA_VERSION",
    "canonical_encode",
    "command_hash",
    "command_payload",
    "command_request_hash",
    "request_hash",
]

#: Bumped whenever the canonical encoding changes. It is part of the hashed bytes,
#: so two schema versions can never collide.
HASH_SCHEMA_VERSION = 1

#: Length of the SHA-256 digest enforced by ``op_ledger.request_hash``.
HASH_BYTES = 32

#: Always-present envelope keys. Every command contributes them, so the payload
#: shape never depends on which optional fields happen to be set.
_ENVELOPE_KEYS: tuple[str, ...] = (
    "hash_schema_version",
    "entity_type",
    "op",
    "workspace_id",
    "entity_id",
    "base_rev",
)


def _encode_scalar(key: str, value: Any) -> Any:
    """JSON-safe scalar for ``value``, or a rejection explaining why there is none."""
    if value is None or isinstance(value, str):
        if isinstance(value, str) and "\x00" in value:
            raise InvalidCommand(f"{key} must not contain NUL (U+0000)")
        return value
    if isinstance(value, uuid.UUID):
        return str(value)  # canonical lowercase hyphenated form
    if isinstance(value, bool):
        raise InvalidCommand(f"{key} must not be a bool: commands carry no flags")
    if isinstance(value, int):
        return value  # json.dumps renders bigint-range ints losslessly
    if isinstance(value, float):
        raise InvalidCommand(
            f"{key} must not be a float (NaN/Infinity included): use decimal int or str"
        )
    raise InvalidCommand(
        f"{key} has unsupported type {type(value).__name__}: "
        "command payloads are UUID/int/str/None only"
    )


def canonical_encode(payload: Mapping[str, Any]) -> str:
    """The canonical JSON text of ``payload`` — the exact bytes that get hashed."""
    for key in _ENVELOPE_KEYS:
        if key not in payload:
            raise InvalidCommand(f"canonical encoding requires {key!r}")

    encoded: dict[str, Any] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not key or "\x00" in key:
            raise InvalidCommand(f"payload key {key!r} is not a clean identifier")
        encoded[key] = _encode_scalar(key, value)

    return json.dumps(encoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_hash(payload: Mapping[str, Any]) -> bytes:
    """SHA-256 of :func:`canonical_encode` — 32 raw bytes for ``op_ledger``."""
    return hashlib.sha256(canonical_encode(payload).encode("utf-8")).digest()


def command_payload(command: Command) -> dict[str, Any]:
    """The exact mapping that identifies ``command``'s intent."""
    payload: dict[str, Any] = {
        "hash_schema_version": HASH_SCHEMA_VERSION,
        "entity_type": command.entity_type,
        "op": command.op,
        "workspace_id": command.workspace_id,
        "entity_id": command.entity_id,
        "base_rev": command.base_rev,
    }
    payload.update(command.business_fields())
    return payload


def command_request_hash(command: Command) -> bytes:
    """Fingerprint of ``command`` (``op_id`` excluded — it is the lookup key)."""
    return request_hash(command_payload(command))


#: Public spelling: ``command_hash(cmd) -> bytes``.
command_hash = command_request_hash
