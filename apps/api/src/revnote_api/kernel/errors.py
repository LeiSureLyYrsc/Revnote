"""Contract errors of the sync kernel.

Every failure the kernel can produce is one of the classes below. They are pure
Python contract types: **no HTTP status codes, no transport concerns**. A later
phase maps them to a protocol; adapters must never invent new meanings.

Two of them are *retryable with the same command* (same ``op_id``/``entity_id``):

* :class:`WriteBusy` — the workspace row lock could not be obtained within the
  bounded ``lock_timeout``. Nothing was written; re-sending the identical
  command is safe.
* :class:`CommitOutcomeUnknown` — the ``COMMIT`` acknowledgement was lost. The
  write may or may not be applied; re-sending the identical command returns the
  first result if it committed (and applies it exactly once if it did not).
  This is *not* a rollback and must never be reported as one.

Everything else is a deterministic rejection: the caller must change something
(new ``op_id``, fresh ``base_rev``, different entity, ...) before retrying.
"""

from __future__ import annotations

import uuid

__all__ = [
    "CommitOutcomeUnknown",
    "CounterOverflow",
    "EntityDeleted",
    "EntityIdConflict",
    "EntityNotFound",
    "FolderCycle",
    "FolderNotEmpty",
    "InvalidCommand",
    "InvalidParent",
    "OpIdConflict",
    "RevConflict",
    "StorageFailure",
    "SyncKernelError",
    "WorkspaceNotFound",
    "WriteBusy",
]


class SyncKernelError(Exception):
    """Base class for every error the sync kernel raises."""


class InvalidCommand(SyncKernelError):
    """The command is structurally invalid (bad ids, blank name, ``base_rev`` < 1, NUL, ...).

    Raised *before* a transaction is opened, so it never touches the database.
    """


class WorkspaceNotFound(SyncKernelError):
    """The workspace id in the command does not exist."""

    def __init__(self, workspace_id: uuid.UUID | None = None) -> None:
        self.workspace_id = workspace_id
        detail = "workspace" if workspace_id is None else f"workspace {workspace_id}"
        super().__init__(f"{detail} does not exist")


class EntityNotFound(SyncKernelError):
    """The addressed folder/note does not exist in this workspace."""

    def __init__(self, entity_type: str, entity_id: uuid.UUID) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} {entity_id} does not exist in this workspace")


class EntityIdConflict(SyncKernelError):
    """A create reused an entity id that already exists.

    Ids are never recycled, not even after a soft delete, so a create can never
    resurrect or overwrite an existing row.
    """

    def __init__(self, entity_type: str, entity_id: uuid.UUID) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} {entity_id} already exists; entity ids are never reused")


class OpIdConflict(SyncKernelError):
    """This ``op_id`` was already used in this workspace with a different request.

    One ``op_id`` means one command, forever: retrying a mutated payload is a
    different command and therefore needs a new ``op_id``.
    """

    def __init__(
        self, workspace_id: uuid.UUID | None = None, op_id: uuid.UUID | None = None
    ) -> None:
        self.workspace_id = workspace_id
        self.op_id = op_id
        super().__init__(
            f"op_id {op_id} in workspace {workspace_id} is already recorded for a different request"
        )


class RevConflict(SyncKernelError):
    """``base_rev`` does not match the object's current ``rev`` (lost update)."""

    def __init__(
        self,
        entity_type: str,
        entity_id: uuid.UUID,
        *,
        expected: int,
        current: int,
    ) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        self.expected = expected
        self.current = current
        super().__init__(
            f"{entity_type} {entity_id} is at rev {current}, command expected base_rev {expected}"
        )


class EntityDeleted(SyncKernelError):
    """The object is soft-deleted; a *new* operation on it is rejected.

    Replaying the original delete command still returns that delete's first
    result — that path is served from the ledger, not from the live row.
    """

    def __init__(self, entity_type: str, entity_id: uuid.UUID) -> None:
        self.entity_type = entity_type
        self.entity_id = entity_id
        super().__init__(f"{entity_type} {entity_id} is deleted")


class InvalidParent(SyncKernelError):
    """The target parent folder does not exist, is deleted, or is outside the workspace."""

    def __init__(self, parent_id: uuid.UUID | None = None, *, reason: str = "") -> None:
        self.parent_id = parent_id
        detail = f"invalid parent folder {parent_id}" if parent_id is not None else "invalid parent"
        super().__init__(f"{detail}: {reason}" if reason else detail)


class FolderCycle(SyncKernelError):
    """The move would make a folder its own ancestor (self-parent or indirect cycle)."""

    def __init__(
        self, folder_id: uuid.UUID | None = None, parent_id: uuid.UUID | None = None
    ) -> None:
        self.folder_id = folder_id
        self.parent_id = parent_id
        super().__init__(f"moving folder {folder_id} under {parent_id} would create a cycle")


class FolderNotEmpty(SyncKernelError):
    """The folder still has active children; deletion never cascades."""

    def __init__(self, folder_id: uuid.UUID | None = None) -> None:
        self.folder_id = folder_id
        super().__init__(f"folder {folder_id} still has active children; delete or move them first")


class CounterOverflow(SyncKernelError):
    """``seq`` or ``rev`` would leave the BIGINT range; nothing was written."""

    def __init__(self, counter: str = "seq", limit: int = 2**63 - 1) -> None:
        self.counter = counter
        self.limit = limit
        super().__init__(f"{counter} would exceed the BIGINT range (limit {limit})")


class WriteBusy(SyncKernelError):
    """The workspace row stayed locked past the bounded ``lock_timeout``.

    Nothing was written; retry the same command (same ``op_id``/``entity_id``).
    """

    def __init__(self, reason: str = "") -> None:
        detail = f": {reason}" if reason else ""
        super().__init__(
            f"the workspace is busy with another write{detail}; retry the same command"
        )


class StorageFailure(SyncKernelError):
    """A storage-level failure or a stored-state inconsistency; nothing was committed.

    Also covers "the database rejected a write we believed was valid" and
    orphan ledger/event rows. It is never used for a lost commit
    acknowledgement — that is :class:`CommitOutcomeUnknown`.
    """


class CommitOutcomeUnknown(SyncKernelError):
    """The transaction was committed client-side but the acknowledgement was lost.

    The write may or may not be applied. Retry the *identical* command (same
    ``op_id``): the ledger makes it exactly-once, and a replay returns the first
    result. Never report this as "rolled back".
    """

    def __init__(self, op_id: uuid.UUID | None = None) -> None:
        self.op_id = op_id
        super().__init__(
            f"commit acknowledgement lost for op {op_id}; outcome unknown — "
            "retry the same op_id to confirm the first result"
        )
