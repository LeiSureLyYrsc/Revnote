"""The eight sync-kernel commands: one frozen dataclass per single-object write.

    folder:  CreateFolder  UpdateFolder  MoveFolder  DeleteFolder
    note:    CreateNote    UpdateNote    MoveNote    DeleteNote

All derive from :class:`Command`, so the service validates and dispatches on one type
while call sites stay readable::

    await service.execute(
        CreateFolder(workspace_id=ws, entity_id=new_entity_id(), op_id=new_op_id(),
                     name="Inbox")
    )

Envelope fields on every command: ``workspace_id``, ``entity_id`` and ``op_id`` are
mandatory UUIDs owned by the caller and reused across retries (D5); ``base_rev`` is
``None`` for ``create`` and a positive integer otherwise.

Each subclass declares only the fields its own operation uses, so an illegal mix
(content in a move, a parent in a rename, a payload on a delete) is a ``TypeError`` at
construction rather than a silent write. Validation here is purely structural and needs
no database: existence, cycles and emptiness are the service's job under the workspace
lock.

``repr`` must never print note content. Dataclasses generate a ``__repr__`` per class
that would dump ``body_md`` verbatim, so every subclass is declared ``repr=False`` (the
hand-written :meth:`Command.__repr__` is used) *and* the content fields carry
``repr=False`` as a second line of defence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, fields
from typing import Any

from revnote_api.kernel.errors import InvalidCommand

__all__ = [
    "ALL_OPS",
    "COMMAND_TYPES",
    "ENTITY_FOLDER",
    "ENTITY_NOTE",
    "ENTITY_TYPES",
    "MAX_BIGINT",
    "OP_CREATE",
    "OP_DELETE",
    "OP_MOVE",
    "OP_UPDATE",
    "Command",
    "CreateFolder",
    "CreateNote",
    "DeleteFolder",
    "DeleteNote",
    "MoveFolder",
    "MoveNote",
    "UpdateFolder",
    "UpdateNote",
    "command_for",
]

ENTITY_FOLDER = "folder"
ENTITY_NOTE = "note"
ENTITY_TYPES: tuple[str, ...] = (ENTITY_FOLDER, ENTITY_NOTE)

OP_CREATE = "create"
OP_UPDATE = "update"
OP_MOVE = "move"
OP_DELETE = "delete"
ALL_OPS: tuple[str, ...] = (OP_CREATE, OP_UPDATE, OP_MOVE, OP_DELETE)

#: PostgreSQL ``bigint`` upper bound, shared with the service's counter guards.
MAX_BIGINT: int = 2**63 - 1

#: Fields every command carries; everything else is operation-specific payload.
_ENVELOPE = frozenset({"workspace_id", "entity_id", "op_id", "entity_type", "op", "base_rev"})

#: Parent references, where ``None`` is meaningful (root folder / unfiled note).
_PARENT_FIELDS = frozenset({"parent_id", "folder_id"})


def _require_uuid(value: Any, name: str) -> None:
    if not isinstance(value, uuid.UUID):
        raise InvalidCommand(f"{name} must be a uuid.UUID, got {type(value).__name__}")


def _require_text(value: Any, name: str, *, blank_allowed: bool) -> None:
    if not isinstance(value, str):
        raise InvalidCommand(f"{name} must be a str, got {type(value).__name__}")
    if "\x00" in value:
        raise InvalidCommand(f"{name} must not contain NUL (U+0000)")
    if not blank_allowed and not value.strip():
        raise InvalidCommand(f"{name} must not be blank")


def _require_revision(value: Any) -> None:
    # bool subclasses int but is never a legal revision.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidCommand(f"base_rev must be an int, got {type(value).__name__}")
    if value < 1:
        raise InvalidCommand(f"base_rev must be >= 1, got {value}")
    if value > MAX_BIGINT:
        raise InvalidCommand(f"base_rev exceeds the BIGINT range (max {MAX_BIGINT})")


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class Command:
    """Base of the eight commands: identity, discriminators, ``base_rev``."""

    workspace_id: uuid.UUID
    entity_id: uuid.UUID
    op_id: uuid.UUID
    entity_type: str = ""
    op: str = ""
    base_rev: int | None = None

    def __post_init__(self) -> None:
        entity_type, op = self.entity_type, self.op
        if entity_type not in ENTITY_TYPES:
            raise InvalidCommand(f"unknown entity_type {entity_type!r}")
        if op not in ALL_OPS:
            raise InvalidCommand(f"unknown op {op!r}: expected one of {', '.join(ALL_OPS)}")

        for name in ("workspace_id", "entity_id", "op_id"):
            _require_uuid(getattr(self, name), name)

        if op == OP_CREATE:
            if self.base_rev is not None:
                raise InvalidCommand("create must not carry base_rev")
        elif self.base_rev is None:
            raise InvalidCommand(f"{op} requires base_rev (the revision being replaced)")
        else:
            _require_revision(self.base_rev)

        # Field-by-field over this command's own payload, so the rules cannot drift
        # from what the subclasses actually declare. ``None`` is a legal parent value
        # (root folder / unfiled note) but never a legal content value.
        for item in fields(self):
            if item.name in _ENVELOPE:
                continue
            value = getattr(self, item.name)
            if item.name in _PARENT_FIELDS:
                if value is not None:
                    _require_uuid(value, item.name)
                continue
            if value is None:
                raise InvalidCommand(f"{entity_type} {op} requires {item.name}, got None")
            if item.name == "name":
                _require_text(value, "name", blank_allowed=False)
            else:  # title / body_md: empty is legal, whitespace is content
                _require_text(value, item.name, blank_allowed=True)

        # A folder is never its own parent. For a note, ``folder_id == entity_id`` is
        # NOT a cycle: the two ids live in different tables, so a coincidentally equal
        # UUID is two unrelated rows and the folder lookup answers InvalidParent.
        if entity_type == ENTITY_FOLDER and getattr(self, "parent_id", None) == self.entity_id:
            raise InvalidCommand(
                f"folder {self.entity_id} cannot be its own parent (a self-parent is invalid)"
            )

    @property
    def parent_field(self) -> str:
        """Column holding the parent reference for this entity type."""
        return "parent_id" if self.entity_type == ENTITY_FOLDER else "folder_id"

    @property
    def target_parent_id(self) -> uuid.UUID | None:
        """Parent this command addresses; ``None`` means root folder / unfiled note."""
        return getattr(self, self.parent_field, None)

    def business_fields(self) -> dict[str, Any]:
        """Operation payload as ``{field: value}`` with explicit ``None``s.

        Feeds :func:`revnote_api.kernel.fingerprint.command_hash`; key order is
        irrelevant there because keys are sorted.
        """
        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name not in _ENVELOPE
        }

    def __repr__(self) -> str:
        """Identity and sizes only — never note text (logging rule, plan section 11)."""
        parts = [
            f"type={self.entity_type}",
            f"op={self.op}",
            f"workspace_id={self.workspace_id}",
            f"entity_id={self.entity_id}",
            f"op_id={self.op_id}",
            f"base_rev={self.base_rev!r}",
        ]
        for name, label in (
            ("name", "name_chars"),
            ("title", "title_chars"),
            ("body_md", "body_chars"),
        ):
            value = getattr(self, name, None)
            if value is not None:
                parts.append(f"{label}={len(value)}")
        if self.target_parent_id is not None:
            parts.append(f"{self.parent_field}={self.target_parent_id!r}")
        return f"{type(self).__name__}({', '.join(parts)})"


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class CreateFolder(Command):
    """Create a folder; ``parent_id=None`` places it at the root."""

    entity_type: str = field(default=ENTITY_FOLDER, init=False)
    op: str = field(default=OP_CREATE, init=False)
    name: str = field(repr=False)
    parent_id: uuid.UUID | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class UpdateFolder(Command):
    """Rename a folder. Never moves."""

    entity_type: str = field(default=ENTITY_FOLDER, init=False)
    op: str = field(default=OP_UPDATE, init=False)
    name: str = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class MoveFolder(Command):
    """Re-parent a folder; the subtree follows and emits no events (D3)."""

    entity_type: str = field(default=ENTITY_FOLDER, init=False)
    op: str = field(default=OP_MOVE, init=False)
    parent_id: uuid.UUID | None = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class DeleteFolder(Command):
    """Soft-delete a folder; refuses non-empty folders, never cascades."""

    entity_type: str = field(default=ENTITY_FOLDER, init=False)
    op: str = field(default=OP_DELETE, init=False)


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class CreateNote(Command):
    """Create a note; ``folder_id=None`` leaves it unfiled."""

    entity_type: str = field(default=ENTITY_NOTE, init=False)
    op: str = field(default=OP_CREATE, init=False)
    title: str = field(repr=False)
    body_md: str = field(repr=False)
    folder_id: uuid.UUID | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class UpdateNote(Command):
    """Replace a note's content. Never moves."""

    entity_type: str = field(default=ENTITY_NOTE, init=False)
    op: str = field(default=OP_UPDATE, init=False)
    title: str = field(repr=False)
    body_md: str = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class MoveNote(Command):
    """File a note elsewhere; ``folder_id=None`` unfiles it."""

    entity_type: str = field(default=ENTITY_NOTE, init=False)
    op: str = field(default=OP_MOVE, init=False)
    folder_id: uuid.UUID | None = field(repr=False)


@dataclass(frozen=True, slots=True, kw_only=True, repr=False)
class DeleteNote(Command):
    """Soft-delete a note: tombstone the row, keep it."""

    entity_type: str = field(default=ENTITY_NOTE, init=False)
    op: str = field(default=OP_DELETE, init=False)


#: All eight commands: (folder, note) x (create, update, move, delete).
COMMAND_TYPES: tuple[type[Command], ...] = (
    CreateFolder,
    UpdateFolder,
    MoveFolder,
    DeleteFolder,
    CreateNote,
    UpdateNote,
    MoveNote,
    DeleteNote,
)

_LOOKUP: dict[tuple[str, str], type[Command]] = {
    (cls.__dataclass_fields__["entity_type"].default, cls.__dataclass_fields__["op"].default): cls
    for cls in COMMAND_TYPES
}


def command_for(entity_type: str, op: str) -> type[Command]:
    """Return the command type for ``(entity_type, op)`` (protocol-layer lookup)."""
    try:
        return _LOOKUP[(entity_type, op)]
    except KeyError:
        raise InvalidCommand(f"no command for entity_type {entity_type!r} op {op!r}") from None
