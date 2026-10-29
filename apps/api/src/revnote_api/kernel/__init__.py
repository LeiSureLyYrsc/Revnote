"""``kernel`` — the sync kernel's vocabulary: commands, errors, hashes, ids, results.

Layering (§3): this package imports **nothing** from FastAPI/HTTP/adapters and
opens no database connection. It holds only pure data and pure functions;
``services`` is the single place that writes, and the only public entry point is
:class:`revnote_api.services.sync_write.SyncWriteService`.
"""

from __future__ import annotations

from revnote_api.kernel.commands import (
    ALL_OPS,
    COMMAND_TYPES,
    ENTITY_FOLDER,
    ENTITY_NOTE,
    ENTITY_TYPES,
    MAX_BIGINT,
    OP_CREATE,
    OP_DELETE,
    OP_MOVE,
    OP_UPDATE,
    Command,
    CreateFolder,
    CreateNote,
    DeleteFolder,
    DeleteNote,
    MoveFolder,
    MoveNote,
    UpdateFolder,
    UpdateNote,
    command_for,
)
from revnote_api.kernel.errors import (
    CommitOutcomeUnknown,
    CounterOverflow,
    EntityDeleted,
    EntityIdConflict,
    EntityNotFound,
    FolderCycle,
    FolderNotEmpty,
    InvalidCommand,
    InvalidParent,
    OpIdConflict,
    RevConflict,
    StorageFailure,
    SyncKernelError,
    WorkspaceNotFound,
    WriteBusy,
)
from revnote_api.kernel.fingerprint import (
    HASH_BYTES,
    HASH_SCHEMA_VERSION,
    canonical_encode,
    command_hash,
    command_payload,
    request_hash,
)
from revnote_api.kernel.ids import new_entity_id, new_id, new_op_id, uuid7
from revnote_api.kernel.results import ExecutedWrite, WriteResult

__all__ = [
    "ALL_OPS",
    "COMMAND_TYPES",
    "ENTITY_FOLDER",
    "ENTITY_NOTE",
    "ENTITY_TYPES",
    "HASH_BYTES",
    "HASH_SCHEMA_VERSION",
    "MAX_BIGINT",
    "OP_CREATE",
    "OP_DELETE",
    "OP_MOVE",
    "OP_UPDATE",
    "Command",
    "CommitOutcomeUnknown",
    "CounterOverflow",
    "CreateFolder",
    "CreateNote",
    "DeleteFolder",
    "DeleteNote",
    "EntityDeleted",
    "EntityIdConflict",
    "EntityNotFound",
    "ExecutedWrite",
    "FolderCycle",
    "FolderNotEmpty",
    "InvalidCommand",
    "InvalidParent",
    "MoveFolder",
    "MoveNote",
    "OpIdConflict",
    "RevConflict",
    "StorageFailure",
    "SyncKernelError",
    "UpdateFolder",
    "UpdateNote",
    "WorkspaceNotFound",
    "WriteBusy",
    "WriteResult",
    "canonical_encode",
    "command_for",
    "command_hash",
    "command_payload",
    "new_entity_id",
    "new_id",
    "new_op_id",
    "request_hash",
    "uuid7",
]
