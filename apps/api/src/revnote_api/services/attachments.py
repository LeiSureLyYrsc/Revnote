"""The attachment state machine, framework-free and storage-agnostic.

Four operations, one deliberate rule that shapes all of them:

    **No object-storage I/O ever happens while a PostgreSQL connection is held.**

The rule is not cosmetic. A pooled connection is the scarcest resource this
service has; a 25 MiB upload that trickles in over thirty seconds would pin one
per concurrent client, and a stalled store would pin them until the pool timed
out. So every storage call is sandwiched *between* short transactions
(plan §6.4, interface §6):

``create``
    One ``INSERT`` of a ``pending`` row. Commits, closes the session, returns.

``store_content``
    1. **short transaction** - read the row for ``(workspace_id, attachment_id)``
       and reject early, before a single body byte is read: ``not_found`` when it
       is missing or ``deleted``, ``attachment_already_ready`` when it is ``ready``,
       ``attachment_expired`` when it outlived the grace window.
    2. **no transaction open** - mint this attempt's own nonce, stream the body into
       :meth:`BlobStore.put` under this attempt's own key, then re-read the object
       with :meth:`BlobStore.digest` and compare (D6: the store's ETag or ``HEAD`` is
       a claim; this is a measurement).
    3. **short transaction** - one conditional ``UPDATE`` that publishes ``ready``,
       this attempt's ``storage_key`` and the digest, but only if this attempt still
       owns the row (see :data:`CAS_STATEMENT`).
    4. A failed verification or a lost race **deletes this attempt's own key first**,
       so a loser leaves no object behind and no ``ready`` row.

``metadata`` / ``open_content``
    One statement that joins ``workspace_members``, so "missing", "not a member"
    and "not ready" are the same ``not_found`` (D20). ``open_content`` then hands
    back an async iterator from the store, again with no session open.

Why the CAS is one statement
----------------------------
Two clients can ``PUT`` the same ``attachment_id`` concurrently; the plan requires
exactly one winner, one object and one ``ready`` row.

**The key belongs to the attempt, not to the row.** ``store_content`` mints its own
nonce and writes ``ws/{ws}/att/{id}/{attempt_nonce}``, so two concurrent uploads are
writing two *different* objects. That is what makes the loser's cleanup safe:
:meth:`AttachmentService._discard_object` is only ever handed the key the attempt
itself wrote, so the winner's bytes cannot be deleted by the loser. (Deriving the key
from a per-*row* nonce - which is what this module did once - makes both attempts
write the *same* object, and then the loser's cleanup removes the winner's bytes: the
row ends up ``ready``, with a digest, and permanently unreadable.)

**The winner is decided by one statement.** ``UPDATE ... WHERE id = ... AND
workspace_id = ... AND state = 'pending'`` is evaluated by PostgreSQL under the row
lock the update takes, so of two concurrent attempts exactly one matches and the other
sees zero rows and knows it lost. The same statement publishes the verified digest
*and* the winning attempt's ``storage_key`` - the key cannot be derived from the row,
so a ``ready`` row has to be told which object it means. No read-then-write window
exists, and no row is ever locked across storage I/O.

Why expiry is computed in SQL
-----------------------------
``created_at`` comes from ``now()``, so expiry is decided by PostgreSQL against the
same clock that produced it. A Python-side comparison would make the answer depend
on clock skew between the application and the database - and the GC, which runs as
a separate process, would then disagree with the upload path about the same row.

What this module never does
---------------------------
* It imports no web framework, no validation library and no settings module: the
  policy arrives as :class:`AttachmentPolicy`, the session factory and the
  :class:`~revnote_api.storage.ports.BlobStore` are injected.
* It imports no storage adapter - only the types in
  :mod:`revnote_api.storage.ports`.
* It logs no file name and no content hash: both are user- or content-derived, and
  a log line is not a place to put either.
"""

from __future__ import annotations

import base64
import binascii
import datetime
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from revnote_api.kernel.ids import new_id
from revnote_api.models.attachment import CONTENT_HASH_BYTES, MIME_MAX_BYTES, MIME_MIN_BYTES
from revnote_api.storage.ports import BlobDigest, BlobStore, BlobTooLarge, StorageUnavailable

__all__ = [
    "ACCEPTED_MIME_DEFAULT",
    "ALREADY_READY",
    "CAS_STATEMENT",
    "EXPIRED",
    "HASH_MISMATCH",
    "NOT_FOUND",
    "STORAGE_UNAVAILABLE",
    "TOO_LARGE",
    "UNSUPPORTED_TYPE",
    "AttachmentError",
    "AttachmentPolicy",
    "AttachmentService",
    "AttachmentView",
    "GcCandidate",
    "GcReport",
    "PendingAttachment",
    "ReadyAttachment",
    "collect_expired",
    "gc_delete_statement",
    "gc_select_statement",
    "grace_interval_sql",
    "normalize_declared_sha256",
    "storage_key_for",
]

LOGGER: Final = logging.getLogger("revnote_api.attachments")

# --------------------------------------------------------------------------- #
# frozen error vocabulary (interface §1)
# --------------------------------------------------------------------------- #

#: The store is unreachable or the deployment has no storage: retry the same call.
STORAGE_UNAVAILABLE: Final[str] = "attachment_storage_unavailable"
#: Another attempt already published this attachment; the object is immutable.
ALREADY_READY: Final[str] = "attachment_already_ready"
#: The bytes on the server do not hash to what the client declared.
HASH_MISMATCH: Final[str] = "attachment_hash_mismatch"
#: The ``pending`` row outlived the grace window.
EXPIRED: Final[str] = "attachment_expired"
#: Over ``ATTACHMENT_MAX_BYTES`` - distinct from the middleware's 413.
TOO_LARGE: Final[str] = "attachment_too_large"
#: Not an accepted MIME (``image/svg+xml`` in particular is never accepted).
UNSUPPORTED_TYPE: Final[str] = "attachment_unsupported_type"
#: Missing, not a member, or not ``ready`` - one answer for all three (D20).
NOT_FOUND: Final[str] = "not_found"

#: Codes this module may put on the wire. ``not_found`` and ``internal`` are the
#: two pre-existing codes it is allowed to reuse: D20 requires the first, and an
#: unexpected database failure must not invent a new one. Anything else is folded
#: into ``internal`` so the protocol vocabulary cannot widen by accident - the same
#: guard :mod:`revnote_api.services.sync_read` uses.
_SAFE_CODES: Final[frozenset[str]] = frozenset(
    {
        ALREADY_READY,
        EXPIRED,
        HASH_MISMATCH,
        NOT_FOUND,
        STORAGE_UNAVAILABLE,
        TOO_LARGE,
        UNSUPPORTED_TYPE,
        # A zero-byte body is a contract violation rather than a storage or size
        # problem, and ``invalid_request`` is the existing protocol code for that
        # (400). Without it here the coercion below would silently turn the
        # rejection into ``internal`` - a 500 for what is a client mistake.
        "invalid_request",
        "internal",
    }
)

#: The allowlist a deployment gets before anyone configures one. Mirrors
#: ``Settings.attachment_accepted_mime``; ``image/svg+xml`` is deliberately absent
#: (D9): an SVG is a document that can carry script and we serve it from our own
#: origin.
ACCEPTED_MIME_DEFAULT: Final[tuple[str, ...]] = (
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "application/pdf",
    "text/plain",
)

#: Never accepted, whatever the configuration says. Checked separately from the
#: allowlist so the rule is visible even if an operator adds it back (the settings
#: validator refuses that too - this is the second lock on the same door).
_SVG_MIME: Final[str] = "image/svg+xml"


class AttachmentError(Exception):
    """A rejection in protocol terms: one safe code plus machine-readable facts.

    ``details`` is built here from fixed labels and policy numbers, never from
    client input and never from a driver message (which can quote row values -
    a file name among them).
    """

    __slots__ = ("code", "details")

    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        self.code = code if code in _SAFE_CODES else "internal"
        self.details: dict[str, Any] = dict(details or {})
        super().__init__(self.code)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AttachmentError(code={self.code!r})"


# --------------------------------------------------------------------------- #
# SQL
# --------------------------------------------------------------------------- #

#: The row a ``PUT`` is allowed to publish, as one atomic statement.
#:
#: ``state = 'pending'`` is the pre-state; that predicate *is* the winner selection.
#: Of two concurrent attempts on one row, PostgreSQL matches this ``WHERE`` under the
#: row lock the ``UPDATE`` takes, so exactly one of them updates a row and the other
#: gets zero rows back and knows it lost.
#:
#: ``workspace_id`` keeps a caller that names a foreign attachment from touching it at
#: all. ``storage_key`` is **written by the winner**: each attempt streams to its own
#: attempt-nonce key (see :meth:`AttachmentService.store_content`), and the row must
#: name the object that actually holds the bytes it just verified - the attempt's key
#: cannot be recovered from the row, so it has to be published with the digest. That
#: is also why the nonce is *not* a CAS predicate: it is per attempt, so no stored
#: copy of it exists to compare against, and ``state = 'pending'`` already rules out a
#: second winner. One row back = winner; zero rows = somebody else already published,
#: or the row is gone.
CAS_STATEMENT: Final[str] = """
UPDATE attachments
   SET state = 'ready',
       content_hash = :content_hash,
       byte_size = :byte_size,
       storage_key = :storage_key,
       ready_at = now()
 WHERE id = :attachment_id
   AND workspace_id = :workspace_id
   AND state = 'pending'
RETURNING ready_at
""".strip()


def grace_interval_sql(grace_s: int) -> str:
    """The grace window as a PostgreSQL interval expression.

    ``grace_s`` is range-checked before it is interpolated, so the statement text
    never carries anything a caller supplied. Building an interval here (rather
    than passing seconds and multiplying in SQL) keeps every statement that needs
    the window byte-identical, which is what lets the PDF-facing test assert on the
    statement text instead of on a row count.
    """
    seconds = int(grace_s)
    if seconds < 1:
        raise ValueError("grace must be at least one second")
    return f"make_interval(secs => {seconds:d})"


def gc_select_statement(grace_s: int, *, limit: int | None = None) -> str:
    """The GC scan: expired ``pending`` rows only, oldest first.

    ``ready`` and ``deleted`` rows are invisible to it by construction, so "the GC
    never deletes a ready attachment" is a property of this statement rather than
    of the loop that consumes it.
    """
    statement = f"""
SELECT id, workspace_id, storage_key
  FROM attachments
 WHERE state = 'pending'
   AND created_at < now() - {grace_interval_sql(grace_s)}
 ORDER BY created_at, id
"""
    if limit is not None:
        if int(limit) < 1:
            raise ValueError("limit must be positive")
        statement += f" LIMIT {int(limit):d}\n"
    return statement.strip()


def gc_delete_statement(grace_s: int) -> str:
    """The conditional delete that pairs with :func:`gc_select_statement`.

    Both predicates are re-checked in the statement that deletes, so a row that
    became ``ready`` between the scan and the delete is never collected.
    """
    return f"""
DELETE FROM attachments
 WHERE id = :attachment_id
   AND state = 'pending'
   AND created_at < now() - {grace_interval_sql(grace_s)}
RETURNING storage_key
""".strip()


# --------------------------------------------------------------------------- #
# value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AttachmentPolicy:
    """The three numbers this service needs, taken from settings by injection.

    Duck-typed on purpose (see :meth:`from_settings`): importing
    ``revnote_api.config`` here would make the service depend on the deployment
    layer, which the boundary tests forbid.
    """

    #: Hard per-attachment ceiling, enforced by the store write itself.
    max_bytes: int
    #: How long a ``pending`` row may live before the upload is refused and the GC
    #: may collect it.
    pending_grace_s: int
    #: Accepted MIME types, already normalised (lowercase, no parameters).
    accepted_mime: tuple[str, ...] = ACCEPTED_MIME_DEFAULT

    def __post_init__(self) -> None:
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if self.pending_grace_s < 1:
            raise ValueError("pending_grace_s must be positive")
        if not self.accepted_mime:
            raise ValueError("accepted_mime must not be empty")

    @classmethod
    def from_settings(cls, settings: object) -> AttachmentPolicy:
        """Build the policy from any object exposing the documented field names."""
        raw = getattr(settings, "attachment_accepted_mime", ACCEPTED_MIME_DEFAULT)
        accepted = tuple(
            dict.fromkeys(normalized for mime in raw if (normalized := _normalize_mime_text(mime)))
        )
        return cls(
            max_bytes=int(getattr(settings, "attachment_max_bytes")),  # noqa: B009
            pending_grace_s=int(getattr(settings, "attachment_pending_grace_s")),  # noqa: B009
            accepted_mime=accepted or ACCEPTED_MIME_DEFAULT,
        )


@dataclass(frozen=True, slots=True)
class PendingAttachment:
    """The result of ``create``: an id, a state and where to send the bytes."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    mime: str
    state: str
    created_at: datetime.datetime
    expires_at: datetime.datetime
    #: Relative upload path - never a presigned URL (D4).
    upload_path: str
    #: The object key. Internal: never published on the wire.
    storage_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ReadyAttachment:
    """The result of a verified upload."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    #: Lowercase hex SHA-256 of the stored bytes.
    content_hash: str = field(repr=False)
    byte_size: int
    ready_at: datetime.datetime

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"ReadyAttachment(id={self.id!r}, byte_size={self.byte_size!r}, "
            f"ready_at={self.ready_at!r})"
        )


@dataclass(frozen=True, slots=True)
class AttachmentView:
    """One ``ready`` attachment as the metadata endpoint describes it.

    ``content_hash`` and ``file_name`` are excluded from ``repr``: a repr ends up
    in a log or a traceback, and both values are content- or user-derived. The hash
    is still published on the wire - that is the point of it - but it is not
    something to spill into an unrelated log line.
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    state: str
    mime: str
    byte_size: int
    created_at: datetime.datetime
    content_hash: str = field(repr=False)
    file_name: str | None = field(default=None, repr=False)
    ready_at: datetime.datetime | None = None
    deleted_at: datetime.datetime | None = None
    #: The immutable object key. Internal; never serialised.
    storage_key: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class GcCandidate:
    """One expired ``pending`` row the GC may collect."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    storage_key: str = field(repr=False)


@dataclass
class GcReport:
    """Counts only. The GC never prints a key, a name or an id."""

    scanned: int = 0
    rows_deleted: int = 0
    objects_deleted: int = 0
    failed: int = 0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _normalize_mime_text(value: object) -> str | None:
    """Lowercase, parameter-free media type; ``None`` when it cannot be one."""
    if not isinstance(value, str):
        return None
    media_type = value.split(";", 1)[0].strip().lower()
    return media_type or None


def storage_key_for(
    workspace_id: uuid.UUID, attachment_id: uuid.UUID, attempt_nonce: uuid.UUID
) -> str:
    """The object key: ``ws/{ws}/att/{id}/{attempt_nonce}`` (D5, interface §4).

    Exactly four path segments, every UUID in canonical lowercase form. Nothing
    from the request (no file name, no client string) contributes a byte, so a
    hostile name cannot influence where an object is written.

    ``attempt_nonce`` is the nonce of **one upload attempt**, not of the row: two
    concurrent ``PUT``s of the same attachment therefore name two different objects,
    which is what lets a loser delete its own bytes without touching the winner's.
    An attempt's key never changes once chosen - the immutability D5 asks for - and
    the winning key is copied into the row at publish time, where it never changes
    again either.
    """
    return f"ws/{workspace_id!s}/att/{attachment_id!s}/{attempt_nonce!s}"


def normalize_declared_sha256(value: str | None) -> str | None:
    """Decode an optional ``X-Content-SHA256`` into lowercase hex.

    Accepts hex (64 characters) or base64 (padded or not) - the two spellings a
    browser client plausibly produces. Anything else raises :class:`ValueError`,
    which the HTTP layer renders as ``invalid_request``: a malformed digest is a
    contract error, not a storage problem, and guessing at it would let a client
    believe verification happened when it did not.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if len(candidate) == 2 * CONTENT_HASH_BYTES and all(
        char in "0123456789abcdefABCDEF" for char in candidate
    ):
        return candidate.lower()
    try:
        decoded = base64.b64decode(candidate + "=" * (-len(candidate) % 4), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("X-Content-SHA256 is not hex or base64") from exc
    if len(decoded) != CONTENT_HASH_BYTES:
        raise ValueError("X-Content-SHA256 must decode to 32 bytes")
    return decoded.hex()


def _row_hash(value: object) -> str:
    """Render a stored ``BYTEA`` digest as the lowercase hex the wire carries."""
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise AttachmentError("internal")
    return bytes(value).hex()


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #


class AttachmentService:
    """The attachment state machine over one session factory and one store.

    Stateless apart from the injected collaborators, so an instance is safe to
    share and cheap to build per request.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        store: BlobStore,
        policy: AttachmentPolicy,
        *,
        upload_path_prefix: str = "/api/v1/workspaces",
    ) -> None:
        self._factory = session_factory
        self._store = store
        self._policy = policy
        self._upload_path_prefix = upload_path_prefix.rstrip("/")

    @property
    def policy(self) -> AttachmentPolicy:
        """The effective policy (read by the capabilities endpoint)."""
        return self._policy

    # -- create ------------------------------------------------------------- #

    async def create(
        self,
        workspace_id: uuid.UUID,
        *,
        mime: str,
        byte_size: int | None = None,
        file_name: str | None = None,
    ) -> PendingAttachment:
        """Insert one ``pending`` row and describe where to upload.

        No object is touched: nothing exists in the store until a ``PUT`` streams
        real bytes, so an abandoned ``create`` costs one row the GC can collect.
        """
        accepted_mime = self._require_supported_mime(mime)
        if byte_size is not None:
            if byte_size <= 0:
                raise AttachmentError(TOO_LARGE, details={"reason": "declared_size_not_positive"})
            if byte_size > self._policy.max_bytes:
                raise AttachmentError(
                    TOO_LARGE,
                    details={"max_bytes": self._policy.max_bytes, "reason": "declared_size"},
                )

        attachment_id = new_id()
        upload_nonce = new_id()
        storage_key = storage_key_for(workspace_id, attachment_id, upload_nonce)
        grace = grace_interval_sql(self._policy.pending_grace_s)

        try:
            async with self._factory() as session:
                result = await session.execute(
                    sa.text(
                        "INSERT INTO attachments "
                        "(id, workspace_id, state, mime, byte_size, storage_key, "
                        " upload_nonce, file_name) "
                        "VALUES (:id, :workspace_id, 'pending', :mime, :byte_size, "
                        "        :storage_key, :upload_nonce, :file_name) "
                        f"RETURNING created_at, created_at + {grace} AS expires_at"
                    ),
                    {
                        "id": attachment_id,
                        "workspace_id": workspace_id,
                        "mime": accepted_mime,
                        "byte_size": byte_size,
                        "storage_key": storage_key,
                        "upload_nonce": upload_nonce,
                        "file_name": file_name,
                    },
                )
                row = result.first()
                await session.commit()
        except SQLAlchemyError as exc:
            raise AttachmentError("internal") from exc

        if row is None:  # pragma: no cover - INSERT ... RETURNING always yields a row
            raise AttachmentError("internal")

        return PendingAttachment(
            id=attachment_id,
            workspace_id=workspace_id,
            mime=accepted_mime,
            state="pending",
            created_at=row[0],
            expires_at=row[1],
            upload_path=self.upload_path(workspace_id, attachment_id),
            storage_key=storage_key,
        )

    def upload_path(self, workspace_id: uuid.UUID, attachment_id: uuid.UUID) -> str:
        """The **relative** upload path published as ``upload.url`` (D4).

        A presigned URL would hand the client a credential-bearing link to the
        object store and route the bytes around this service; a relative path keeps
        one authorisation model and one place that enforces the limits.
        """
        return f"{self._upload_path_prefix}/{workspace_id!s}/attachments/{attachment_id!s}/content"

    # -- upload ------------------------------------------------------------- #

    async def store_content(
        self,
        workspace_id: uuid.UUID,
        attachment_id: uuid.UUID,
        *,
        chunks: AsyncIterator[bytes],
        declared_content_type: str | None,
        declared_sha256: str | None = None,
    ) -> ReadyAttachment:
        """Stream, verify and publish one upload.

        The three phases are separated so that **no** storage call happens inside a
        transaction; see the module docstring. Every rejection that can be decided
        before the body is read (wrong state, expired row, wrong content type) is
        decided in phase 1, so a refused upload never costs a byte of transfer.
        """
        row = await self._read_upload_target(workspace_id, attachment_id)
        self._require_matching_content_type(row["mime"], declared_content_type)

        # The key belongs to this *attempt*, not to the row (interface §4, D5).
        # Minting it here is what makes two concurrent uploads write two different
        # objects: the CAS then decides which attempt's key the row ends up naming,
        # and a loser's cleanup can only ever delete its own bytes. Reading the
        # key off the row (as this code used to) made both attempts share one
        # object, so the loser deleted the winner's bytes and left a `ready` row
        # with a digest and no content.
        attempt_nonce = new_id()
        key = storage_key_for(workspace_id, attachment_id, attempt_nonce)
        await self._mark_upload_started(workspace_id, attachment_id)

        # ---- no session exists between these two lines and the CAS below ---- #
        written = await self._write_object(
            key, chunks, content_type=row["mime"], max_bytes=self._policy.max_bytes
        )
        verified = await self._read_back_digest(key)
        # -------------------------------------------------------------------- #

        if (verified.sha256_hex, verified.size) != (written.sha256_hex, written.size):
            # The store handed back different bytes than it accepted. The client did
            # nothing wrong, so this is a storage failure, not a hash mismatch.
            await self._discard_object(key)
            raise AttachmentError(STORAGE_UNAVAILABLE, details={"reason": "stored_bytes_changed"})

        if declared_sha256 is not None and declared_sha256 != verified.sha256_hex:
            # The client's own claim about the bytes is wrong: no ``ready`` row may
            # exist afterwards, and its object must not linger.
            await self._discard_object(key)
            raise AttachmentError(HASH_MISMATCH)

        if verified.size == 0:
            # An empty body is a client mistake, not a server fault. The table
            # enforces ``CHECK (byte_size > 0)``, so letting this reach the CAS would
            # violate a constraint and surface as a 500 ``internal`` - which reads as
            # "the server broke" for what is really "you sent nothing". Reported with
            # the existing ``invalid_request`` code rather than a new one: the six
            # attachment codes are a frozen vocabulary and an empty body is precisely
            # "the request does not match the API contract". Caught after the write so
            # the object is still discarded and the row stays ``pending`` (GC-eligible).
            await self._discard_object(key)
            raise AttachmentError("invalid_request")

        return await self._publish_ready(
            workspace_id=workspace_id,
            attachment_id=attachment_id,
            key=key,
            content_hash=verified.sha256_hex,
            byte_size=verified.size,
        )

    async def _read_upload_target(
        self, workspace_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> dict[str, Any]:
        """Phase 1: the row this upload may publish, or the reason it may not."""
        grace = grace_interval_sql(self._policy.pending_grace_s)
        try:
            async with self._factory() as session:
                row = (
                    await session.execute(
                        sa.text(
                            "SELECT id, workspace_id, state, mime, upload_nonce, "
                            "       storage_key, "
                            f"       ((created_at + {grace}) <= now()) AS expired "
                            "  FROM attachments "
                            " WHERE id = :attachment_id AND workspace_id = :workspace_id"
                        ),
                        {"attachment_id": attachment_id, "workspace_id": workspace_id},
                    )
                ).first()
        except SQLAlchemyError as exc:
            raise AttachmentError("internal") from exc

        if row is None or row[2] == "deleted":
            raise AttachmentError(NOT_FOUND)
        if row[2] == "ready":
            # Decided before a single body byte is read: an attachment is immutable.
            raise AttachmentError(ALREADY_READY)
        if bool(row[6]):
            raise AttachmentError(EXPIRED)
        return {
            "id": row[0],
            "workspace_id": row[1],
            "state": row[2],
            "mime": row[3],
            "upload_nonce": row[4],
            "storage_key": row[5],
        }

    async def _mark_upload_started(self, workspace_id: uuid.UUID, attachment_id: uuid.UUID) -> None:
        """Record that an attempt started, in its own short committed transaction.

        Deliberately not folded into the CAS: the point of the column is to show an
        operator an upload that *never finished*, which is invisible if the value is
        only written when the upload succeeds.
        """
        try:
            async with self._factory() as session:
                await session.execute(
                    sa.text(
                        "UPDATE attachments "
                        "   SET upload_started_at = coalesce(upload_started_at, now()) "
                        " WHERE id = :attachment_id AND workspace_id = :workspace_id "
                        "   AND state = 'pending'"
                    ),
                    {"attachment_id": attachment_id, "workspace_id": workspace_id},
                )
                await session.commit()
        except SQLAlchemyError as exc:
            raise AttachmentError("internal") from exc

    async def _write_object(
        self,
        key: str,
        chunks: AsyncIterator[bytes],
        *,
        content_type: str,
        max_bytes: int,
    ) -> BlobDigest:
        """Phase 2a: the store write, with **no** session open anywhere."""
        try:
            return await self._store.put(
                key, chunks, max_bytes=max_bytes, content_type=content_type
            )
        except BlobTooLarge as exc:
            await self._discard_object(key)
            raise AttachmentError(TOO_LARGE, details={"max_bytes": max_bytes}) from exc
        except StorageUnavailable as exc:
            await self._discard_object(key)
            raise AttachmentError(STORAGE_UNAVAILABLE) from exc

    async def _read_back_digest(self, key: str) -> BlobDigest:
        """Phase 2b: re-read the object and recompute its digest (D6)."""
        try:
            return await self._store.digest(key)
        except StorageUnavailable as exc:
            await self._discard_object(key)
            raise AttachmentError(STORAGE_UNAVAILABLE) from exc

    async def _publish_ready(
        self,
        *,
        workspace_id: uuid.UUID,
        attachment_id: uuid.UUID,
        key: str,
        content_hash: str,
        byte_size: int,
    ) -> ReadyAttachment:
        """Phase 3: the one statement that picks a single winner."""
        try:
            async with self._factory() as session:
                result = await session.execute(
                    sa.text(CAS_STATEMENT),
                    {
                        "content_hash": bytes.fromhex(content_hash),
                        "byte_size": byte_size,
                        "attachment_id": attachment_id,
                        "workspace_id": workspace_id,
                        # The winner's key, written by the CAS itself. Each attempt
                        # writes its own object, so the row must end up naming the
                        # object whose bytes actually won - not the one minted at
                        # ``create``, and not the loser's.
                        "storage_key": key,
                    },
                )
                # ``len(rows)`` rather than ``rowcount``: the latter is driver- and
                # statement-shape-dependent, and "exactly one row was published" is
                # the whole point of this statement.
                rows = result.all()
                await session.commit()
        except SQLAlchemyError as exc:
            await self._discard_object(key)
            raise AttachmentError("internal") from exc

        if len(rows) != 1:
            # Lost the race (or the row vanished): the object this attempt wrote is
            # nobody's, so it is removed before the client is told.
            await self._discard_object(key)
            raise await self._lost_race_error(workspace_id, attachment_id)

        return ReadyAttachment(
            id=attachment_id,
            workspace_id=workspace_id,
            content_hash=content_hash,
            byte_size=byte_size,
            ready_at=rows[0][0],
        )

    async def _lost_race_error(
        self, workspace_id: uuid.UUID, attachment_id: uuid.UUID
    ) -> AttachmentError:
        """Explain a zero-row CAS without ever guessing.

        The ordinary answer is ``attachment_already_ready``: another attempt won and
        the object is immutable. A row that disappeared (the GC collected it while
        this upload was in flight) is ``not_found``.
        """
        try:
            async with self._factory() as session:
                row = (
                    await session.execute(
                        sa.text(
                            "SELECT state FROM attachments "
                            " WHERE id = :attachment_id AND workspace_id = :workspace_id"
                        ),
                        {"attachment_id": attachment_id, "workspace_id": workspace_id},
                    )
                ).first()
        except SQLAlchemyError:
            return AttachmentError("internal")
        if row is None or row[0] == "deleted":
            return AttachmentError(NOT_FOUND)
        return AttachmentError(ALREADY_READY)

    async def _discard_object(self, key: str) -> None:
        """Best-effort removal of this attempt's own key.

        ``delete`` is idempotent by contract, and a failure here must not replace the
        error the caller is about to see: a leaked object is a housekeeping problem,
        while a masked ``attachment_hash_mismatch`` is a correctness one.
        """
        try:
            await self._store.delete(key)
        except StorageUnavailable:  # pragma: no cover - store down mid-cleanup
            LOGGER.warning("attachment_cleanup_failed reason=storage_unavailable")

    # -- reads -------------------------------------------------------------- #

    async def metadata(self, attachment_id: uuid.UUID, *, user_id: uuid.UUID) -> AttachmentView:
        """Describe one ``ready`` attachment the caller's membership covers."""
        return await self._read_view(attachment_id, user_id=user_id)

    async def open_content(
        self, attachment_id: uuid.UUID, *, user_id: uuid.UUID
    ) -> tuple[AttachmentView, AsyncIterator[bytes]]:
        """Authorise, prove the object exists, then hand back a chunk iterator.

        The ``stat`` probe happens **before** the response starts, so "the row says
        ready but the object is gone" is a clean ``503`` instead of a half-written
        200 - and it is again outside any transaction.
        """
        view = await self._read_view(attachment_id, user_id=user_id)
        try:
            stat = await self._store.stat(view.storage_key)
        except StorageUnavailable as exc:
            raise AttachmentError(STORAGE_UNAVAILABLE) from exc
        if stat is None:
            raise AttachmentError(STORAGE_UNAVAILABLE, details={"reason": "object_missing"})
        return view, self._store.iter_chunks(view.storage_key)

    async def _read_view(self, attachment_id: uuid.UUID, *, user_id: uuid.UUID) -> AttachmentView:
        """One statement joining membership, so non-membership is indistinguishable.

        The join *is* the authorisation check: a row the caller is not a member of
        simply does not come back, which is why a foreign attachment and a
        nonexistent one produce the same code (D20). ``state = 'ready'`` is filtered
        in SQL for the same reason - a pending row is not a resource yet.
        """
        try:
            async with self._factory() as session:
                row = (
                    await session.execute(
                        sa.text(
                            "SELECT a.id, a.workspace_id, a.state, a.mime, a.byte_size, "
                            "       a.content_hash, a.file_name, a.created_at, a.ready_at, "
                            "       a.deleted_at, a.storage_key "
                            "  FROM attachments AS a "
                            "  JOIN workspace_members AS m "
                            "    ON m.workspace_id = a.workspace_id "
                            "   AND m.user_id = :user_id "
                            " WHERE a.id = :attachment_id "
                            "   AND a.state = 'ready'"
                        ),
                        {"attachment_id": attachment_id, "user_id": user_id},
                    )
                ).first()
        except SQLAlchemyError as exc:
            raise AttachmentError("internal") from exc

        if row is None:
            raise AttachmentError(NOT_FOUND)

        return AttachmentView(
            id=row[0],
            workspace_id=row[1],
            state=row[2],
            mime=row[3],
            byte_size=int(row[4]),
            created_at=row[7],
            content_hash=_row_hash(row[5]),
            file_name=row[6],
            ready_at=row[8],
            deleted_at=row[9],
            storage_key=row[10],
        )

    # -- validation --------------------------------------------------------- #

    def _require_supported_mime(self, mime: object) -> str:
        """Normalise ``mime`` and refuse anything outside the allowlist.

        ``image/svg+xml`` has its own check and its own reason label even though the
        allowlist already excludes it: the rule is a security boundary (D9), and it
        must stay visible in this function rather than being inferred from a
        configuration list someone may edit.
        """
        media_type = _normalize_mime_text(mime)
        if media_type is None:
            raise AttachmentError(UNSUPPORTED_TYPE, details={"reason": "mime_missing"})
        size = len(media_type.encode("utf-8"))
        if size < MIME_MIN_BYTES or size > MIME_MAX_BYTES:
            raise AttachmentError(UNSUPPORTED_TYPE, details={"reason": "mime_length"})
        if "/" not in media_type or media_type.startswith("/") or media_type.endswith("/"):
            raise AttachmentError(UNSUPPORTED_TYPE, details={"reason": "mime_shape"})
        if media_type == _SVG_MIME:
            raise AttachmentError(UNSUPPORTED_TYPE, details={"reason": "mime_svg"})
        if media_type not in self._policy.accepted_mime:
            raise AttachmentError(UNSUPPORTED_TYPE, details={"reason": "mime_not_accepted"})
        return media_type

    @staticmethod
    def _require_matching_content_type(mime: str, declared: str | None) -> None:
        """The upload's ``Content-Type`` must be the MIME declared at ``create``.

        Parameters (``; charset=utf-8``) are ignored - they describe an encoding of
        the body, not a different file type - but the media type itself must match
        exactly. A mismatch, including an explicit ``image/svg+xml``, is
        ``attachment_unsupported_type``.
        """
        media_type = _normalize_mime_text(declared)
        if media_type == _SVG_MIME:
            raise AttachmentError(UNSUPPORTED_TYPE, details={"reason": "mime_svg"})
        if media_type is None:
            raise AttachmentError(UNSUPPORTED_TYPE, details={"reason": "content_type_missing"})
        if media_type != mime:
            raise AttachmentError(UNSUPPORTED_TYPE, details={"reason": "content_type_mismatch"})


# --------------------------------------------------------------------------- #
# garbage collection (used by ``python -m revnote_api.tools.attachments_gc``)
# --------------------------------------------------------------------------- #


async def collect_expired(
    session_factory: async_sessionmaker[AsyncSession],
    store: BlobStore,
    *,
    grace_s: int,
    dry_run: bool = False,
    limit: int | None = None,
    candidates: Sequence[GcCandidate] | None = None,
) -> GcReport:
    """Delete expired ``pending`` rows **and** their objects; ``ready`` never.

    Ordering is deliberate: the row goes first, under a statement that re-checks
    ``state = 'pending' AND created_at < now() - grace``. That is what makes a
    concurrent upload safe - if the collection wins, the uploader's CAS matches zero
    rows and it deletes its own key - and it means a crash between the two deletes
    leaves an orphan *object* (cheap, invisible) rather than a row pointing at
    nothing (confusing, user-visible).
    """
    select_sql = gc_select_statement(grace_s, limit=limit)
    delete_sql = gc_delete_statement(grace_s)
    report = GcReport()

    rows = candidates
    if rows is None:
        try:
            async with session_factory() as session:
                result = await session.execute(sa.text(select_sql))
                rows = [
                    GcCandidate(id=row[0], workspace_id=row[1], storage_key=row[2])
                    for row in result.all()
                ]
        except SQLAlchemyError:
            LOGGER.error("attachments_gc_failed stage=scan")
            report.failed += 1
            return report

    report.scanned = len(rows)
    for candidate in rows:
        if dry_run:
            report.rows_deleted += 1
            report.objects_deleted += 1
            continue
        if not await _delete_row(session_factory, delete_sql, candidate):
            continue
        report.rows_deleted += 1
        try:
            await store.delete(candidate.storage_key)
        except StorageUnavailable:
            LOGGER.warning("attachments_gc_failed stage=object_delete")
            report.failed += 1
            continue
        report.objects_deleted += 1
    return report


async def _delete_row(
    session_factory: async_sessionmaker[AsyncSession],
    statement: str,
    candidate: GcCandidate,
) -> bool:
    """Conditionally delete one row; ``True`` only if this call removed it."""
    try:
        async with session_factory() as session:
            result = await session.execute(sa.text(statement), {"attachment_id": candidate.id})
            removed = bool(result.all())
            await session.commit()
    except SQLAlchemyError:
        LOGGER.error("attachments_gc_failed stage=row_delete")
        return False
    return removed
