"""The read side of the sync protocol: pull, snapshot, tree and one note.

Four operations, each with one deliberate consistency story:

``pull``
    **One** SQL statement. A single CTE anchors the workspace (``last_seq`` and
    ``min_retained_seq``), takes the ordered event page ``(since, since + 1, ...]``
    and joins the *current* folder/note rows for exactly the entities that page
    names, de-duplicated per ``(entity_type, id)``. Watermarks, events and objects
    therefore come from one snapshot by construction - there is no "read the
    cursor, then read the events" gap in which a concurrent push could hide ``N``
    while ``N+1`` became visible (plan §12.1). Events are never compacted: a note
    updated five times contributes five events plus **one** object at its true
    current ``rev``/``updated_seq``, which may lead the events in the page.

``snapshot``
    One ``REPEATABLE READ, READ ONLY`` transaction (plan §12.2, D15). Two preflight
    statements *inside that same transaction* first count the active objects and
    then bound their content bytes, so a workspace past either budget is refused
    before the rows are materialised. The data statement then reads every active
    folder/note once; the API layer measures the real serialized JSON and refuses
    the whole request when it does not fit. No pagination, no partial snapshot.

``tree`` / ``note``
    Single statements over active rows only. ``tree`` never selects ``body_md``
    (metadata only); ``note`` returns one active note inside the path workspace and
    answers the same ``not_found`` for "missing" and "tombstoned" (plan §12.2).

The byte budget, and what it actually guarantees
------------------------------------------------
The pull response is bounded at about 4 MiB. The SQL has to shorten the
**contiguous event prefix**, and it can only do that while reading, so it needs a
content size per entity. Sizes are measured with ``octet_length`` on the text
columns - the real logical size in UTF-8 bytes:

    octet_length(title) + octet_length(body_md)

``pg_column_size`` is deliberately **not** used: it reports the *physical stored*
size, which is the compressed size for a compressed value and the 18-byte pointer
for an out-of-line one. A highly compressible megabyte would then look nearly free,
be admitted to the page, and only be caught after materialisation - an error in the
dangerous direction.

What the guard is, and what it is not
    These sizes decide **how much data may enter the response**. They bound wire
    materialisation: the page that gets assembled and serialized cannot exceed the
    budget by more than one event/object pair, and a page that cannot fit one pair
    is refused outright (413) instead of skipping it.

    They are **not** a hard cap on PostgreSQL-side working memory. ``octet_length``
    on a ``text`` value can usually be answered from the varlena header without
    expanding the value, but that is an implementation optimisation, not a contract:
    a value stored out-of-line and compressed may be detoasted to be measured. A
    legacy row written before the HTTP size cap existed can therefore cost the
    backend real memory to measure, up to roughly its decompressed size. That is
    accepted: the read is bounded in *result* size and does not grow with the number
    of rows considered (the page is limited before sizing), so a pathological row
    costs one measurement rather than loading the entire event history. Write-only
    ``DB_STATEMENT_TIMEOUT_MS`` is not applied to these reads and is not a memory
    limit; deployment-level read/resource limits require separate configuration.

    An earlier revision tried ``octet_length(left(body_md, n))`` to bound the
    detoast. That was wrong: ``left``/``substr`` on pglz-compressed text does not
    guarantee partial decompression - it may detoast the whole value before slicing.
    The bound it advertised was not real, so it was removed rather than restated.

Titles and folder names are measured the same way; HTTP writes are bounded by the
raw JSON budget, but trusted legacy writers can exceed that. Wire cost is re-measured on
the serialized JSON by the HTTP layer (below), which is the only figure that includes
escaping.

JSON escaping can then still inflate a value (``"\\n"`` is 2 bytes, ``"\\u0001"``
is 6), which is why the HTTP layer re-measures the exact serialized bytes, shortens
the prefix further if needed, and recomputes the object set from the rows already
fetched rather than issuing a second query (plan §12.1, D14). Content is never
truncated: an event/object pair is always either returned whole or not at all.

Framework-free by contract: no FastAPI, no Pydantic, no settings import. Rows that
came from the database are returned as frozen dataclasses and the HTTP layer
renders them. Failures leave as :class:`SyncReadError`, which carries a code from
the protocol vocabulary plus machine-readable details - never the driver's text,
because PostgreSQL messages quote row values, note bodies included.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar, Final, Literal

import sqlalchemy as sa
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from revnote_api.kernel.commands import MAX_BIGINT

__all__ = [
    "ENVELOPE_RESERVE_BYTES",
    "EVENT_OVERHEAD_BYTES",
    "SET_SNAPSHOT_TRANSACTION",
    "EntityType",
    "FolderMeta",
    "NoteMeta",
    "Operation",
    "PullPage",
    "ReadEvent",
    "ReadFolder",
    "ReadNote",
    "ReadObject",
    "SnapshotView",
    "SyncReadError",
    "SyncReadService",
    "TreeView",
]

#: The two entity kinds the sync protocol knows (matches the schema's CHECK).
EntityType = Literal["folder", "note"]

#: The four recorded operations (matches the schema's CHECK).
Operation = Literal["create", "update", "delete", "move"]

ENTITY_TYPES: Final[frozenset[str]] = frozenset({"folder", "note"})
OPERATIONS: Final[frozenset[str]] = frozenset({"create", "update", "delete", "move"})

#: Approximate wire cost of one event object, used by the SQL prefix estimator so a
#: page does not spend the whole budget on events that are mostly JSON keys. An
#: estimate only; the API layer measures the real bytes.
EVENT_OVERHEAD_BYTES: Final[int] = 200

#: Bytes held back from the SQL budget for the response envelope
#: (``{"events":...,"objects":...,"next_cursor":...}``). Small and constant.
ENVELOPE_RESERVE_BYTES: Final[int] = 512

#: The isolation + read-only pin of a snapshot, issued as the first statement of
#: the transaction so PostgreSQL takes the snapshot under the right rules. A
#: ``REPEATABLE READ`` snapshot is acquired on the first *query* and held to the
#: end, so the preflight and the data statement cannot disagree (plan §12.2).
SET_SNAPSHOT_TRANSACTION: Final[str] = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"

#: Codes this module may put on the wire. An unknown code would widen the protocol
#: vocabulary by accident, so anything else is folded into ``internal``.
_SAFE_CODES: Final[frozenset[str]] = frozenset(
    {
        "cursor_ahead",
        "cursor_expired",
        "internal",
        "invalid_request",
        "not_found",
        "payload_too_large",
        "snapshot_too_large",
    }
)


class SyncReadError(Exception):
    """A read rejection in protocol terms: a safe code plus machine-readable facts.

    ``details`` is built here, never copied from a driver error, so it can only
    contain values this module chose (a sequence number, an action verb). The
    original exception stays on ``__cause__`` for a local debugger.
    """

    __slots__ = ("code", "details")

    def __init__(self, code: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code if code in _SAFE_CODES else "internal"
        self.details = dict(details or {})
        super().__init__(self.code)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SyncReadError(code={self.code!r})"


# --------------------------------------------------------------------------- #
# value objects
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ReadEvent:
    """One immutable ``change_events`` row, as the pull page returns it."""

    seq: int
    entity_type: EntityType
    entity_id: uuid.UUID
    op: Operation
    rev: int
    op_id: uuid.UUID
    created_at: datetime.datetime

    @property
    def key(self) -> tuple[str, uuid.UUID]:
        """The de-duplication key shared with :class:`ReadFolder`/:class:`ReadNote`."""
        return (self.entity_type, self.entity_id)


@dataclass(frozen=True, slots=True)
class ReadFolder:
    """The current state of one folder row (possibly a tombstone)."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    rev: int
    updated_seq: int
    deleted_at: datetime.datetime | None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    entity_type: ClassVar[str] = "folder"

    @property
    def key(self) -> tuple[str, uuid.UUID]:
        return (self.entity_type, self.id)


@dataclass(frozen=True, slots=True)
class ReadNote:
    """The current state of one note row (possibly a tombstone).

    ``body_md`` is excluded from ``repr``: a repr ends up in logs and tracebacks,
    and the note body is the one field the protocol forbids echoing (plan §11.1).
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    folder_id: uuid.UUID | None
    title: str
    body_md: str = field(repr=False)
    rev: int
    updated_seq: int
    deleted_at: datetime.datetime | None
    created_at: datetime.datetime
    updated_at: datetime.datetime

    entity_type: ClassVar[str] = "note"

    @property
    def key(self) -> tuple[str, uuid.UUID]:
        return (self.entity_type, self.id)


#: A current folder or note. Discriminated by ``entity_type`` at the wire layer.
ReadObject = ReadFolder | ReadNote


@dataclass(frozen=True, slots=True)
class FolderMeta:
    """Tree metadata of one active folder: name and position, no content."""

    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    rev: int
    updated_seq: int
    created_at: datetime.datetime
    updated_at: datetime.datetime

    entity_type: ClassVar[str] = "folder"


@dataclass(frozen=True, slots=True)
class NoteMeta:
    """Tree metadata of one active note: title and position, never ``body_md``."""

    id: uuid.UUID
    folder_id: uuid.UUID | None
    title: str
    rev: int
    updated_seq: int
    created_at: datetime.datetime
    updated_at: datetime.datetime

    entity_type: ClassVar[str] = "note"


@dataclass(frozen=True, slots=True)
class PullPage:
    """One consistent pull page.

    ``next_cursor`` is the last returned event's ``seq`` (``since`` when the page is
    empty, so an empty page never moves the cursor); ``has_more`` tells the client
    whether another page exists **in the same snapshot**.
    """

    events: tuple[ReadEvent, ...]
    objects: tuple[ReadObject, ...]
    next_cursor: int
    has_more: bool
    last_seq: int
    min_retained_seq: int
    since: int


@dataclass(frozen=True, slots=True)
class SnapshotView:
    """A complete, single-snapshot bootstrap of one workspace."""

    as_of_seq: int
    min_retained_seq: int
    objects: tuple[ReadObject, ...]


@dataclass(frozen=True, slots=True)
class TreeView:
    """Active folders and notes, metadata only."""

    folders: tuple[FolderMeta, ...]
    notes: tuple[NoteMeta, ...]


# --------------------------------------------------------------------------- #
# SQL fragments
# --------------------------------------------------------------------------- #


def _ts(column: str) -> str:
    """Render a ``timestamptz`` column as RFC 3339 UTC with microseconds.

    Done in SQL so the string inside ``jsonb`` is already the wire form: relying on
    ``to_json(timestamptz)`` would make the format depend on the session's
    ``DateStyle``/``TimeZone``. Python re-validates it into an aware ``datetime``,
    and the fixed serializer renders exactly the same text again.
    """
    return f"to_char({column} AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"


def _uuid_text(column: str) -> str:
    return f"CAST({column} AS TEXT)"


def _optional_uuid_text(column: str) -> str:
    return f"CASE WHEN {column} IS NULL THEN NULL ELSE CAST({column} AS TEXT) END"


def _optional_ts(column: str) -> str:
    return f"CASE WHEN {column} IS NULL THEN NULL ELSE {_ts(column)} END"


def _folder_json(alias: str) -> str:
    """``jsonb_build_object`` for one folder row (a folder has no body)."""
    return (
        "jsonb_build_object("
        "'entity_type', 'folder', "
        f"'id', {_uuid_text(f'{alias}.id')}, "
        f"'workspace_id', {_uuid_text(f'{alias}.workspace_id')}, "
        f"'parent_id', {_optional_uuid_text(f'{alias}.parent_id')}, "
        f"'name', {alias}.name, "
        f"'rev', {_uuid_text(f'{alias}.rev')}, "
        f"'updated_seq', {_uuid_text(f'{alias}.updated_seq')}, "
        f"'deleted_at', {_optional_ts(f'{alias}.deleted_at')}, "
        f"'created_at', {_ts(f'{alias}.created_at')}, "
        f"'updated_at', {_ts(f'{alias}.updated_at')}"
        ")"
    )


def _note_json(alias: str, *, tree: bool) -> str:
    """``jsonb_build_object`` for one note row; ``tree`` omits ``body_md``."""
    parts = [
        "'entity_type', 'note'",
        f"'id', {_uuid_text(f'{alias}.id')}",
        f"'workspace_id', {_uuid_text(f'{alias}.workspace_id')}",
        f"'folder_id', {_optional_uuid_text(f'{alias}.folder_id')}",
        f"'title', {alias}.title",
    ]
    if not tree:
        parts.append(f"'body_md', {alias}.body_md")
    parts.extend(
        [
            f"'rev', {_uuid_text(f'{alias}.rev')}",
            f"'updated_seq', {_uuid_text(f'{alias}.updated_seq')}",
            f"'deleted_at', {_optional_ts(f'{alias}.deleted_at')}",
            f"'created_at', {_ts(f'{alias}.created_at')}",
            f"'updated_at', {_ts(f'{alias}.updated_at')}",
        ]
    )
    return "jsonb_build_object(" + ", ".join(parts) + ")"


def _folder_size(alias: str) -> str:
    """Logical content byte length of one folder (its name, in UTF-8 bytes)."""
    return f"octet_length({alias}.name)"


def _note_size(alias: str) -> str:
    """Logical content byte length of one note: title plus body, in UTF-8 bytes.

    ``octet_length`` is the real logical size, which is what the response will carry
    once JSON-encoded (before escaping). ``pg_column_size`` is deliberately not used:
    it is the *physical stored* size, so it reports the compressed size for a
    compressed value and the 18-byte pointer for an out-of-line one - either way it
    can understate the data by orders of magnitude and admit a page that will not fit.
    """
    return f"octet_length({alias}.title) + octet_length({alias}.body_md)"


def pull_statement() -> str:
    """The one statement a pull executes.

    Shape, in order:

    ``anchor``      the workspace's ``last_seq``/``min_retained_seq`` (one row)
    ``page``        ``seq > since`` ordered, ``limit + 1`` rows (the +1 is the
                    same-snapshot ``has_more`` probe). The ``LIMIT`` is applied
                    *inside* the subquery, before the row numbering, so neither the
                    sort nor the window function runs over the whole remaining
                    history - only over the page it will return.
    ``referenced``  each page entity once, with its first row number and its
                    content size (see :func:`_note_size`)
    ``running``     cumulative event + object bytes, so a *contiguous* prefix can
                    be chosen instead of an arbitrary subset
    ``cutoff``      the last row number whose cumulative size fits the budget

    The final select returns exactly one row: the two watermarks, how many events
    the page really had (so "cut to nothing" is distinguishable from "no events"),
    the cutoff, and three JSON arrays (events, folders, notes). A missing workspace
    yields zero rows, which the caller reports as ``not_found``.
    """
    return f"""
WITH anchor AS (
    SELECT w.id AS workspace_id,
           w.last_seq AS last_seq,
           w.min_retained_seq AS min_retained_seq
    FROM workspaces AS w
    WHERE w.id = :workspace_id
),
page AS (
    SELECT q.seq AS seq,
           q.entity_type AS entity_type,
           q.entity_id AS entity_id,
           q.op AS op,
           q.rev AS rev,
           q.op_id AS op_id,
           q.created_at AS created_at,
           row_number() OVER (ORDER BY q.seq) AS rn
    FROM (
        SELECT e.seq AS seq,
               e.entity_type AS entity_type,
               e.entity_id AS entity_id,
               e.op AS op,
               e.rev AS rev,
               e.op_id AS op_id,
               e.created_at AS created_at
        FROM change_events AS e
        JOIN anchor AS a ON a.workspace_id = e.workspace_id
        WHERE e.seq > CAST(:since AS BIGINT)
        ORDER BY e.seq
        LIMIT CAST(:fetch_limit AS BIGINT)
    ) AS q
),
referenced AS (
    SELECT p.entity_type AS entity_type,
           p.entity_id AS entity_id,
           min(p.rn) AS first_rn,
           max(
               CASE WHEN p.entity_type = 'folder'
                    THEN {_folder_size("f")}
                    ELSE {_note_size("n")}
               END
           ) AS object_bytes
    FROM page AS p
    CROSS JOIN anchor AS a
    LEFT JOIN folders AS f
           ON p.entity_type = 'folder'
          AND f.workspace_id = a.workspace_id
          AND f.id = p.entity_id
    LEFT JOIN notes AS n
           ON p.entity_type = 'note'
          AND n.workspace_id = a.workspace_id
          AND n.id = p.entity_id
    GROUP BY p.entity_type, p.entity_id
),
running AS (
    SELECT p.rn AS rn,
           sum(
               CAST(:event_overhead AS BIGINT)
               + CASE WHEN r.first_rn = p.rn THEN coalesce(r.object_bytes, 0) ELSE 0 END
           ) OVER (
               ORDER BY p.rn
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
           ) AS cumulative_bytes
    FROM page AS p
    LEFT JOIN referenced AS r
           ON r.entity_type = p.entity_type
          AND r.entity_id = p.entity_id
),
cutoff AS (
    SELECT coalesce(max(rn), 0) AS last_rn
    FROM running
    WHERE cumulative_bytes <= CAST(:max_bytes AS BIGINT)
)
SELECT a.last_seq AS last_seq,
       a.min_retained_seq AS min_retained_seq,
       (SELECT count(*) FROM page) AS fetched_events,
       (SELECT last_rn FROM cutoff) AS cutoff_rn,
       coalesce(
           (SELECT jsonb_agg(
                       jsonb_build_object(
                           'seq', CAST(p.seq AS TEXT),
                           'entity_type', p.entity_type,
                           'entity_id', CAST(p.entity_id AS TEXT),
                           'op', p.op,
                           'rev', CAST(p.rev AS TEXT),
                           'op_id', CAST(p.op_id AS TEXT),
                           'created_at', {_ts("p.created_at")}
                       )
                       ORDER BY p.seq
                   )
            FROM page AS p
            WHERE p.rn <= (SELECT last_rn FROM cutoff)),
           '[]'::jsonb
       ) AS events,
       coalesce(
           (SELECT jsonb_agg({_folder_json("f")} ORDER BY f.id)
            FROM referenced AS r
            JOIN folders AS f
              ON r.entity_type = 'folder'
             AND f.workspace_id = a.workspace_id
             AND f.id = r.entity_id
            WHERE r.first_rn <= (SELECT last_rn FROM cutoff)),
           '[]'::jsonb
       ) AS folder_objects,
       coalesce(
           (SELECT jsonb_agg({_note_json("n", tree=False)} ORDER BY n.id)
            FROM referenced AS r
            JOIN notes AS n
              ON r.entity_type = 'note'
             AND n.workspace_id = a.workspace_id
             AND n.id = r.entity_id
            WHERE r.first_rn <= (SELECT last_rn FROM cutoff)),
           '[]'::jsonb
       ) AS note_objects
FROM anchor AS a
""".strip()


def snapshot_count_statement() -> str:
    """Cheap preflight: watermark plus the number of active objects.

    No content column is touched, so this cannot materialise a body even if a
    legacy workspace holds a 2 GiB note. Running it first also caps how many values
    the byte probe below can examine (``<= max_objects``).
    """
    return """
WITH anchor AS (
    SELECT w.id AS workspace_id,
           w.last_seq AS last_seq,
           w.min_retained_seq AS min_retained_seq
    FROM workspaces AS w
    WHERE w.id = :workspace_id
)
SELECT a.last_seq AS last_seq,
       a.min_retained_seq AS min_retained_seq,
       (SELECT count(*)
          FROM folders AS f
         WHERE f.workspace_id = a.workspace_id AND f.deleted_at IS NULL)
     + (SELECT count(*)
          FROM notes AS n
         WHERE n.workspace_id = a.workspace_id AND n.deleted_at IS NULL) AS object_count
FROM anchor AS a
""".strip()


def snapshot_bytes_statement() -> str:
    """Preflight: logical content bytes of the active objects.

    Exact, honest UTF-8 sizes (see :func:`_note_size`), so a workspace whose content
    cannot fit is refused before any body is materialised into the response. It is a
    guard on *result* size, not a cap on PostgreSQL's per-value working memory - a
    legacy out-of-line compressed body may cost a detoast to measure. The count
    preflight runs first so this statement only ever examines at most
    ``max_objects`` values.
    """
    return f"""
WITH anchor AS (
    SELECT w.id AS workspace_id
    FROM workspaces AS w
    WHERE w.id = :workspace_id
)
SELECT (SELECT coalesce(sum({_folder_size("f")}), 0)
          FROM folders AS f
         WHERE f.workspace_id = a.workspace_id AND f.deleted_at IS NULL)
     + (SELECT coalesce(sum({_note_size("n")}), 0)
          FROM notes AS n
         WHERE n.workspace_id = a.workspace_id AND n.deleted_at IS NULL) AS raw_bytes
FROM anchor AS a
""".strip()


def snapshot_statement() -> str:
    """Every active folder and note, one statement, no pagination (D15)."""
    return f"""
WITH anchor AS (
    SELECT w.id AS workspace_id,
           w.last_seq AS last_seq,
           w.min_retained_seq AS min_retained_seq
    FROM workspaces AS w
    WHERE w.id = :workspace_id
)
SELECT a.last_seq AS last_seq,
       a.min_retained_seq AS min_retained_seq,
       coalesce(
           (SELECT jsonb_agg({_folder_json("f")} ORDER BY f.id)
              FROM folders AS f
             WHERE f.workspace_id = a.workspace_id AND f.deleted_at IS NULL),
           '[]'::jsonb
       ) AS folders,
       coalesce(
           (SELECT jsonb_agg({_note_json("n", tree=False)} ORDER BY n.id)
              FROM notes AS n
             WHERE n.workspace_id = a.workspace_id AND n.deleted_at IS NULL),
           '[]'::jsonb
       ) AS notes
FROM anchor AS a
""".strip()


def tree_statement() -> str:
    """Active folders/notes metadata, no bodies (plan §12.2)."""
    return f"""
WITH anchor AS (
    SELECT w.id AS workspace_id
    FROM workspaces AS w
    WHERE w.id = :workspace_id
)
SELECT (SELECT count(*) FROM anchor) AS found,
       coalesce(
           (SELECT jsonb_agg({_folder_json("f")} ORDER BY f.created_at, f.id)
              FROM folders AS f
             WHERE f.workspace_id = :workspace_id AND f.deleted_at IS NULL),
           '[]'::jsonb
       ) AS folders,
       coalesce(
           (SELECT jsonb_agg({_note_json("n", tree=True)} ORDER BY n.created_at, n.id)
              FROM notes AS n
             WHERE n.workspace_id = :workspace_id AND n.deleted_at IS NULL),
           '[]'::jsonb
       ) AS notes
""".strip()


def note_statement() -> str:
    """One *active* note inside the workspace; a tombstone is simply absent."""
    return f"""
SELECT {_note_json("n", tree=False)} AS note
FROM notes AS n
WHERE n.workspace_id = :workspace_id
  AND n.id = :note_id
  AND n.deleted_at IS NULL
""".strip()


# --------------------------------------------------------------------------- #
# decoding
# --------------------------------------------------------------------------- #


def _json_value(value: Any) -> Any:
    """Decode a ``json``/``jsonb`` column that may arrive as text or as objects.

    SQLAlchemy's asyncpg dialect installs a decoder, but a caller may build the
    engine differently (or use a session double). Accepting both keeps the read
    path independent of that detail - and never guesses: anything undecodable is an
    ``internal`` error, not a silently empty page.
    """
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except (TypeError, ValueError) as exc:
            raise SyncReadError("internal") from exc
    return value


def _json_items(value: Any) -> tuple[Mapping[str, Any], ...]:
    decoded = _json_value(value)
    if decoded is None:
        return ()
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise SyncReadError("internal")
    return tuple(decoded)


def _json_object(value: Any) -> Mapping[str, Any]:
    decoded = _json_value(value)
    if not isinstance(decoded, dict):
        raise SyncReadError("internal")
    return decoded


def _timestamp(value: Any) -> datetime.datetime:
    if not isinstance(value, str):
        raise SyncReadError("internal")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError as exc:
        raise SyncReadError("internal") from exc
    if parsed.tzinfo is None:
        raise SyncReadError("internal")
    return parsed.astimezone(datetime.UTC)


def _optional_timestamp(value: Any) -> datetime.datetime | None:
    return None if value is None else _timestamp(value)


def _identifier(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise SyncReadError("internal") from exc


def _optional_identifier(value: Any) -> uuid.UUID | None:
    return None if value is None else _identifier(value)


def _counter(value: Any) -> int:
    """Decode a counter that may arrive as text, ``int`` or ``Decimal``.

    The wire form is always text (``DecimalString``), so decoding is one conversion
    here rather than a guess at each call site.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SyncReadError("internal") from exc
    if parsed < 0 or parsed > MAX_BIGINT:
        raise SyncReadError("internal")
    return parsed


def _total(value: Any) -> int:
    """Decode a byte total without the BIGINT ceiling (``snapshot_too_large``).

    ``sum(bigint)`` is ``numeric``, so the driver may hand back ``Decimal``. A total
    above the byte budget must produce ``snapshot_too_large``, not an ``internal``
    error, so this variant only requires a non-negative integer.
    """
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise SyncReadError("internal") from exc
    if parsed < 0:
        raise SyncReadError("internal")
    return parsed


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise SyncReadError("internal")
    return value


def _event_from_json(payload: Mapping[str, Any]) -> ReadEvent:
    entity_type = payload.get("entity_type")
    op = payload.get("op")
    if entity_type not in ENTITY_TYPES or op not in OPERATIONS:
        # The schema's CHECK constraints make this impossible; a violation means the
        # row was written by something other than the kernel, and it must not reach
        # the wire as an unknown enum value.
        raise SyncReadError("internal")
    return ReadEvent(
        seq=_counter(payload.get("seq")),
        entity_type=entity_type,  # type: ignore[arg-type]
        entity_id=_identifier(payload.get("entity_id")),
        op=op,  # type: ignore[arg-type]
        rev=_counter(payload.get("rev")),
        op_id=_identifier(payload.get("op_id")),
        created_at=_timestamp(payload.get("created_at")),
    )


def _object_from_json(payload: Mapping[str, Any]) -> ReadObject:
    entity_type = payload.get("entity_type")
    if entity_type == "folder":
        return ReadFolder(
            id=_identifier(payload.get("id")),
            workspace_id=_identifier(payload.get("workspace_id")),
            parent_id=_optional_identifier(payload.get("parent_id")),
            name=_text(payload.get("name")),
            rev=_counter(payload.get("rev")),
            updated_seq=_counter(payload.get("updated_seq")),
            deleted_at=_optional_timestamp(payload.get("deleted_at")),
            created_at=_timestamp(payload.get("created_at")),
            updated_at=_timestamp(payload.get("updated_at")),
        )
    if entity_type == "note":
        return ReadNote(
            id=_identifier(payload.get("id")),
            workspace_id=_identifier(payload.get("workspace_id")),
            folder_id=_optional_identifier(payload.get("folder_id")),
            title=_text(payload.get("title")),
            body_md=_text(payload.get("body_md")),
            rev=_counter(payload.get("rev")),
            updated_seq=_counter(payload.get("updated_seq")),
            deleted_at=_optional_timestamp(payload.get("deleted_at")),
            created_at=_timestamp(payload.get("created_at")),
            updated_at=_timestamp(payload.get("updated_at")),
        )
    raise SyncReadError("internal")


def _folder_meta_from_json(payload: Mapping[str, Any]) -> FolderMeta:
    return FolderMeta(
        id=_identifier(payload.get("id")),
        parent_id=_optional_identifier(payload.get("parent_id")),
        name=_text(payload.get("name")),
        rev=_counter(payload.get("rev")),
        updated_seq=_counter(payload.get("updated_seq")),
        created_at=_timestamp(payload.get("created_at")),
        updated_at=_timestamp(payload.get("updated_at")),
    )


def _note_meta_from_json(payload: Mapping[str, Any]) -> NoteMeta:
    return NoteMeta(
        id=_identifier(payload.get("id")),
        folder_id=_optional_identifier(payload.get("folder_id")),
        title=_text(payload.get("title")),
        rev=_counter(payload.get("rev")),
        updated_seq=_counter(payload.get("updated_seq")),
        created_at=_timestamp(payload.get("created_at")),
        updated_at=_timestamp(payload.get("updated_at")),
    )


# --------------------------------------------------------------------------- #
# service
# --------------------------------------------------------------------------- #


class SyncReadService:
    """Consistent reads for one workspace at a time.

    The service owns no engine and no transaction policy beyond the snapshot's
    ``REPEATABLE READ, READ ONLY`` pin: the factory hands out a fresh
    ``AsyncSession`` per call. Pull, tree and note are single statements, so the
    session's implicit transaction is closed without a commit - nothing here writes.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = session_factory

    # -- pull --------------------------------------------------------------- #

    async def pull(
        self,
        workspace_id: uuid.UUID,
        *,
        since: int,
        limit: int,
        max_bytes: int,
    ) -> PullPage:
        """Return one consistent page of events plus their current objects.

        ``since`` is **exclusive**; ``limit`` is the page bound (the statement asks
        for ``limit + 1`` rows to answer ``has_more`` from the same snapshot).
        ``max_bytes`` bounds the contiguous prefix the statement ships; the API
        layer applies the exact serialized-byte budget afterwards.
        """
        self._require_pull_arguments(workspace_id, since=since, limit=limit, max_bytes=max_bytes)
        budget = self._sql_budget(max_bytes)
        params = {
            "workspace_id": workspace_id,
            "since": since,
            "fetch_limit": limit + 1,
            "max_bytes": budget,
            "event_overhead": EVENT_OVERHEAD_BYTES,
        }

        try:
            async with self._factory() as session:
                row = await self._read_pull_row(session, params)
        except SQLAlchemyError as exc:
            raise SyncReadError("internal") from exc

        if row is None:
            raise SyncReadError("not_found")

        last_seq = _counter(row[0])
        min_retained_seq = _counter(row[1])
        fetched_events = _counter(row[2])
        cutoff_rn = _counter(row[3])

        # Both cursor verdicts come before any content is looked at, and both use
        # the watermark from *this* snapshot, so an empty page gets the same answer
        # as a full one (D13/D16).
        if since > last_seq:
            raise SyncReadError("cursor_ahead", details={"last_seq": str(last_seq)})
        if since < min_retained_seq - 1:
            raise SyncReadError(
                "cursor_expired",
                details={"min_retained_seq": str(min_retained_seq), "action": "resync"},
            )

        if fetched_events and cutoff_rn == 0:
            # Not one event *and its object* fits the budget. Skipping it would move
            # the cursor past unseen data, so the page is refused instead (plan
            # §12.1: an explicit 413, never a skipped event).
            raise SyncReadError("payload_too_large")

        events = tuple(_event_from_json(item) for item in _json_items(row[4]))
        objects = tuple(
            _object_from_json(item) for item in (*_json_items(row[5]), *_json_items(row[6]))
        )

        window = events[:limit]
        wanted = {event.key for event in window}
        # Recompute the page's object set from rows already fetched: when the
        # statement's prefix was one row longer than the page (the has_more probe),
        # that probe entity's object must not travel.
        selected_objects = tuple(obj for obj in objects if obj.key in wanted)

        return PullPage(
            events=window,
            objects=selected_objects,
            next_cursor=window[-1].seq if window else since,
            has_more=fetched_events > limit or cutoff_rn < fetched_events,
            last_seq=last_seq,
            min_retained_seq=min_retained_seq,
            since=since,
        )

    # -- snapshot ----------------------------------------------------------- #

    async def snapshot(
        self,
        workspace_id: uuid.UUID,
        *,
        max_objects: int,
        max_bytes: int,
    ) -> SnapshotView:
        """Read every active object in **one** ``REPEATABLE READ`` view.

        All four steps run on a single session, so they share one transaction and
        therefore one snapshot:

        1. :meth:`_begin_snapshot` - pin ``REPEATABLE READ, READ ONLY``,
        2. :meth:`_snapshot_counts` - watermark + object count (touches no content),
        3. :meth:`_snapshot_bytes` - bounded content-byte probe,
        4. :meth:`_read_snapshot` - the object rows themselves.

        The count is checked first because it caps how many values step 3 has to
        examine. Both limits refuse the whole request - there is no partial
        snapshot (D15). Each step is a separate method so a test can pause between
        them (the ``RR`` proof in lane E) without a production hook.
        """
        if not isinstance(workspace_id, uuid.UUID) or max_objects < 1 or max_bytes < 1:
            raise SyncReadError("invalid_request")

        try:
            async with self._factory() as session:
                try:
                    # All four steps are invoked through the class rather than ``self``
                    # (see the note below the object read): they are the documented
                    # test seams, and a caller that patches one with ``staticmethod``
                    # and later restores the plain function must not turn the next
                    # call into a bound method with a stray ``self`` argument.
                    await SyncReadService._begin_snapshot(session)

                    (
                        as_of_seq,
                        min_retained_seq,
                        object_count,
                    ) = await SyncReadService._snapshot_counts(session, workspace_id)
                    if object_count > max_objects:
                        raise SyncReadError("snapshot_too_large")

                    if await SyncReadService._snapshot_bytes(session, workspace_id) > max_bytes:
                        raise SyncReadError("snapshot_too_large")

                    objects = await SyncReadService._read_snapshot(session, workspace_id)
                    return SnapshotView(
                        as_of_seq=as_of_seq,
                        min_retained_seq=min_retained_seq,
                        objects=objects,
                    )
                finally:
                    # A read-only transaction has nothing to publish. Rollback is the
                    # honest verb and releases the connection immediately.
                    with contextlib.suppress(SQLAlchemyError):
                        await session.rollback()
        except SyncReadError:
            raise
        except SQLAlchemyError as exc:
            raise SyncReadError("internal") from exc

    # -- snapshot: one method per step (stable test seams) ------------------- #

    @staticmethod
    async def _begin_snapshot(session: AsyncSession) -> None:
        """Step 1: take the ``REPEATABLE READ, READ ONLY`` snapshot.

        A ``REPEATABLE READ`` snapshot is acquired by the first *query*, so this
        ``SET TRANSACTION`` (which is itself the first statement of the
        transaction) decides how everything after it is read. Nothing may run on
        the session before it.
        """
        await session.execute(sa.text(SET_SNAPSHOT_TRANSACTION))

    @staticmethod
    async def _snapshot_counts(
        session: AsyncSession, workspace_id: uuid.UUID
    ) -> tuple[int, int, int]:
        """Step 2: ``(last_seq, min_retained_seq, active object count)``.

        Deliberately reads no content column, so an oversized legacy body cannot be
        toasted just to be counted.
        """
        row = (
            await session.execute(
                sa.text(snapshot_count_statement()),
                {"workspace_id": workspace_id},
            )
        ).first()
        if row is None:
            raise SyncReadError("not_found")
        return _counter(row[0]), _counter(row[1]), _counter(row[2])

    @staticmethod
    async def _snapshot_bytes(session: AsyncSession, workspace_id: uuid.UUID) -> int:
        """Step 3: logical content bytes of the active objects (``octet_length``)."""
        row = (
            await session.execute(
                sa.text(snapshot_bytes_statement()),
                {"workspace_id": workspace_id},
            )
        ).first()
        if row is None:  # pragma: no cover - step 2 saw the workspace row
            raise SyncReadError("not_found")
        return _total(row[0])

    @staticmethod
    async def _read_snapshot(
        session: AsyncSession, workspace_id: uuid.UUID
    ) -> tuple[ReadObject, ...]:
        """Step 4: every active folder and note, decoded from one result row."""
        row = (
            await session.execute(
                sa.text(snapshot_statement()),
                {"workspace_id": workspace_id},
            )
        ).first()
        if row is None:  # pragma: no cover - step 2 saw the workspace row
            raise SyncReadError("not_found")
        return tuple(
            _object_from_json(item) for item in (*_json_items(row[2]), *_json_items(row[3]))
        )

    # -- pull: one method per statement (stable test seams) ------------------ #

    @staticmethod
    async def _read_pull_row(session: AsyncSession, params: Mapping[str, Any]) -> Any:
        """The **only** statement a pull executes; returns its single result row.

        Kept as its own method so a test can wrap exactly this call (and only this
        one) - e.g. to prove that one statement really is all a pull needs, or to
        pause a pull while a push commits.
        """
        return (await session.execute(sa.text(pull_statement()), params)).first()

    # -- tree --------------------------------------------------------------- #

    async def tree(self, workspace_id: uuid.UUID) -> TreeView:
        """Active folders and notes, metadata only."""
        try:
            async with self._factory() as session:
                row = (
                    await session.execute(sa.text(tree_statement()), {"workspace_id": workspace_id})
                ).first()
        except SQLAlchemyError as exc:
            raise SyncReadError("internal") from exc

        if row is None or _counter(row[0]) == 0:
            raise SyncReadError("not_found")

        return TreeView(
            folders=tuple(_folder_meta_from_json(item) for item in _json_items(row[1])),
            notes=tuple(_note_meta_from_json(item) for item in _json_items(row[2])),
        )

    # -- one note ----------------------------------------------------------- #

    async def note(self, workspace_id: uuid.UUID, note_id: uuid.UUID) -> ReadNote:
        """One active note with its body; missing, foreign or deleted is ``not_found``."""
        try:
            async with self._factory() as session:
                row = (
                    await session.execute(
                        sa.text(note_statement()),
                        {"workspace_id": workspace_id, "note_id": note_id},
                    )
                ).first()
        except SQLAlchemyError as exc:
            raise SyncReadError("internal") from exc

        if row is None or row[0] is None:
            raise SyncReadError("not_found")

        note = _object_from_json(_json_object(row[0]))
        if not isinstance(note, ReadNote):  # pragma: no cover - statement is note-only
            raise SyncReadError("internal")
        return note

    # -- internals ---------------------------------------------------------- #

    @staticmethod
    def _require_pull_arguments(
        workspace_id: uuid.UUID,
        *,
        since: int,
        limit: int,
        max_bytes: int,
    ) -> None:
        """Reject a call the protocol could not have produced, before any I/O.

        The route validates the wire form; this only keeps a direct caller from
        turning a negative cursor into a nonsensical or unbounded statement.
        """
        if not isinstance(workspace_id, uuid.UUID):
            raise SyncReadError("invalid_request")
        for value in (since, limit, max_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SyncReadError("invalid_request")
        if since > MAX_BIGINT or limit < 1 or max_bytes < 1:
            raise SyncReadError("invalid_request")

    @staticmethod
    def _sql_budget(max_bytes: int) -> int:
        """The byte budget handed to the statement, after the envelope reserve."""
        return max(1, max_bytes - ENVELOPE_RESERVE_BYTES)
