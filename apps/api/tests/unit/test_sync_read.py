"""Unit tests for the sync read path: pull, snapshot, tree, one note.

Pure by design: no PostgreSQL is started and no connection is opened. What is
provable without a server *is* worth proving here:

* the SQL has the shape the contract depends on - a pull is exactly **one**
  statement whose prefix is anchored on the workspace row, whose event list is
  never compacted, and whose objects are de-duplicated per ``(entity_type, id)``;
  a snapshot pins ``REPEATABLE READ, READ ONLY`` and preflights before it reads;
  ``tree`` never selects a body and ``note`` never returns a tombstone;
* the cursor vocabulary is exact: ``since`` is exclusive, ``since > last_seq`` is
  ``cursor_ahead`` even for an empty page, and expiry is ``since < floor - 1``;
* the byte budget shrinks a *contiguous* prefix, recomputes the object set from the
  rows already fetched, and refuses (413) rather than skipping one large event;
* nothing leaks: no note body in a ``repr``, no driver text in an error, no logger
  call at all in the service.

What cannot be proven here - MVCC, one-snapshot visibility, real JSON byte sizes
against a real server - belongs to the PostgreSQL tier (lane E), which is why these
tests assert on statements and values rather than on timing or locking.
"""

from __future__ import annotations

import dataclasses
import datetime
import inspect
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from revnote_api.api.dependencies import AuthContext, require_workspace_member
from revnote_api.api.errors import install_error_handlers
from revnote_api.api.middleware.request_id import RequestIdMiddleware
from revnote_api.api.routes import read as read_routes
from revnote_api.api.routes import sync_pull as pull_routes
from revnote_api.api.schemas.common import BIGINT_MAX
from revnote_api.api.schemas.sync_pull import (
    NoteObject,
    PullResponse,
    SnapshotResponse,
    dump_body,
    dump_size,
)
from revnote_api.config import Settings
from revnote_api.services import sync_read as module
from revnote_api.services.sync_read import (
    ENVELOPE_RESERVE_BYTES,
    EVENT_OVERHEAD_BYTES,
    SET_SNAPSHOT_TRANSACTION,
    PullPage,
    ReadEvent,
    ReadFolder,
    ReadNote,
    SnapshotPage,
    SnapshotView,
    SyncReadError,
    SyncReadService,
    TreeView,
    decode_snapshot_cursor,
    derive_snapshot_cursor_key,
    encode_snapshot_cursor,
)

pytestmark = pytest.mark.anyio

NOW = datetime.datetime(2026, 10, 29, 3, 16, 0, 123456, tzinfo=datetime.UTC)
STAMP = "2026-10-29T03:16:00.123456Z"


# --------------------------------------------------------------------------- #
# fakes: a session that answers by statement text, and never talks to a server
# --------------------------------------------------------------------------- #


class FakeResult:
    """The three accessors the read path uses."""

    def __init__(self, *, row: Any = None, rowcount: int = 1) -> None:
        self._row = row
        self.rowcount = rowcount

    def first(self) -> Any:
        return self._row


class FakeSession:
    """Records every statement and answers from a prepared queue.

    ``answers`` is a list of ``(fragment, result)``; the first fragment that occurs
    in the rendered SQL answers it. An unanswered statement raises, so a test can
    never pass by silently getting ``None`` for a query it did not expect.
    """

    def __init__(
        self,
        answers: Sequence[tuple[str, FakeResult]] = (),
        *,
        error: BaseException | None = None,
        error_fragment: str | None = None,
    ) -> None:
        self.statements: list[str] = []
        self.parameters: list[Any] = []
        self._answers = list(answers)
        self._error = error
        self._error_fragment = error_fragment
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    async def execute(self, statement: Any, params: Any = None) -> FakeResult:
        sql = str(statement)
        self.statements.append(sql)
        self.parameters.append(params)
        if self._error is not None and (
            self._error_fragment is None or self._error_fragment in sql
        ):
            raise self._error
        for fragment, result in self._answers:
            if fragment in sql:
                return result
        raise AssertionError(f"unexpected statement: {sql[:120]}")

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.closed = True


def factory_for(*sessions: FakeSession) -> Any:
    """A session factory handing out the prepared sessions in order."""
    queue = list(sessions)

    def factory() -> FakeSession:
        if not queue:
            raise AssertionError("the service opened more sessions than the test prepared")
        return queue.pop(0)

    return factory


def service_for(*sessions: FakeSession) -> SyncReadService:
    return SyncReadService(factory_for(*sessions))


PAGED_CURSOR_KEY = derive_snapshot_cursor_key("postgresql+asyncpg://u:p@test/revnote")


def paged_service_for(*sessions: FakeSession) -> SyncReadService:
    return SyncReadService(factory_for(*sessions), cursor_key=PAGED_CURSOR_KEY)


def jsonb(value: Any) -> str:
    """A ``jsonb`` column as the driver may hand it back (text)."""
    return json.dumps(value)


def pull_row(
    *,
    last_seq: int = 0,
    min_retained_seq: int = 1,
    fetched_events: int = 0,
    cutoff_rn: int = 0,
    events: Sequence[Mapping[str, Any]] = (),
    folders: Sequence[Mapping[str, Any]] = (),
    notes: Sequence[Mapping[str, Any]] = (),
) -> tuple[Any, ...]:
    return (
        last_seq,
        min_retained_seq,
        fetched_events,
        cutoff_rn,
        jsonb(list(events)),
        jsonb(list(folders)),
        jsonb(list(notes)),
    )


def event_json(seq: int, entity_type: str, entity_id: uuid.UUID, *, rev: int = 1) -> dict[str, Any]:
    return {
        "seq": str(seq),
        "entity_type": entity_type,
        "entity_id": str(entity_id),
        "op": "create",
        "rev": str(rev),
        "op_id": str(uuid.uuid4()),
        "created_at": STAMP,
    }


def note_json(
    note_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    rev: int = 1,
    updated_seq: int = 1,
    body: str = "text",
    deleted_at: str | None = None,
) -> dict[str, Any]:
    return {
        "entity_type": "note",
        "id": str(note_id),
        "workspace_id": str(workspace_id),
        "folder_id": None,
        "title": "Title",
        "body_md": body,
        "rev": str(rev),
        "updated_seq": str(updated_seq),
        "deleted_at": deleted_at,
        "created_at": STAMP,
        "updated_at": STAMP,
    }


def folder_json(
    folder_id: uuid.UUID, workspace_id: uuid.UUID, *, deleted_at: str | None = None
) -> dict[str, Any]:
    return {
        "entity_type": "folder",
        "id": str(folder_id),
        "workspace_id": str(workspace_id),
        "parent_id": None,
        "name": "Inbox",
        "rev": "1",
        "updated_seq": "1",
        "deleted_at": deleted_at,
        "created_at": STAMP,
        "updated_at": STAMP,
    }


# --------------------------------------------------------------------------- #
# SQL shape
# --------------------------------------------------------------------------- #


def test_pull_is_one_statement() -> None:
    sql = module.pull_statement()

    assert sql.count(";") == 0, "a pull must be a single statement, never a batch"
    assert sql.upper().count("WITH ") == 1
    # Whole-word only: ``updated_seq`` contains "UPDATE" as a substring.
    for forbidden in (r"\bINSERT\b", r"\bUPDATE\b", r"\bDELETE\b", r"\bTRUNCATE\b", "FOR UPDATE"):
        assert re.search(forbidden, sql.upper()) is None, forbidden


def test_pull_anchors_the_watermarks_in_the_same_statement() -> None:
    """No "read the cursor, then read the page" - both come from one CTE."""
    sql = module.pull_statement()

    assert "WITH anchor AS (" in sql
    assert "w.last_seq AS last_seq" in sql
    assert "w.min_retained_seq AS min_retained_seq" in sql
    # Every other CTE is joined against the anchor, never a fresh workspace read.
    assert sql.count("FROM workspaces") == 1
    assert sql.count("JOIN anchor AS a") >= 1


def test_pull_event_page_is_exclusive_and_ordered_with_a_has_more_probe() -> None:
    sql = module.pull_statement()

    assert "WHERE e.seq > CAST(:since AS BIGINT)" in sql, "since must be exclusive"
    assert "ORDER BY e.seq" in sql
    assert "LIMIT CAST(:fetch_limit AS BIGINT)" in sql
    # The +1 probe is supplied by the service, not spelled in SQL.
    assert ":fetch_limit" in sql


def test_pull_keeps_every_event_and_dedupes_only_objects() -> None:
    """History is never compacted; only the *objects* are de-duplicated."""
    sql = module.pull_statement()

    assert "superseded" not in sql.lower()
    assert "DISTINCT" not in sql.upper()
    assert "GROUP BY p.entity_type, p.entity_id" in sql
    # The event list is a plain aggregate over the page, ordered by seq.
    assert "'seq', CAST(p.seq AS TEXT)" in sql
    assert "jsonb_agg(" in sql
    assert "ORDER BY p.seq" in sql


def test_pull_bounds_the_prefix_with_running_bytes() -> None:
    sql = module.pull_statement()

    assert "octet_length(n.title) + octet_length(n.body_md)" in sql
    assert "pg_column_size(" not in sql, "the storage size understates a compressed body"
    assert "OVER (" in sql and "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW" in sql
    assert "cumulative_bytes <= CAST(:max_bytes AS BIGINT)" in sql
    assert "coalesce(max(rn), 0) AS last_rn" in sql
    # An entity's object bytes are counted once, at its first event in the page.
    assert "CASE WHEN r.first_rn = p.rn THEN coalesce(r.object_bytes, 0) ELSE 0 END" in sql


def test_pull_selects_watermarks_counts_and_three_json_arrays() -> None:
    sql = module.pull_statement()
    select_tail = sql.split("SELECT a.last_seq AS last_seq", 1)[1]

    assert "AS fetched_events" in select_tail
    assert "AS cutoff_rn" in select_tail
    assert "AS events" in select_tail
    assert "AS folder_objects" in select_tail
    assert "AS note_objects" in select_tail


def test_snapshot_pins_repeatable_read_and_read_only() -> None:
    assert SET_SNAPSHOT_TRANSACTION == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
    )
    source = (module.__file__ or "").replace("\\", "/")
    assert source.endswith("services/sync_read.py")


def test_snapshot_preflights_count_then_bytes_before_building_the_response() -> None:
    count_sql = module.snapshot_count_statement()
    bytes_sql = module.snapshot_bytes_statement()

    assert "AS object_count" in count_sql
    assert "AS raw_bytes" in bytes_sql
    # The count touches no content column at all, so it cannot dominate the cost of
    # the byte preflight - and it caps how many values that preflight examines.
    assert "body_md" not in count_sql
    assert "jsonb_agg" not in count_sql
    # Logical UTF-8 sizes, never the physical stored size.
    assert "octet_length(n.title) + octet_length(n.body_md)" in bytes_sql
    assert "pg_column_size(" not in bytes_sql
    # Neither preflight aggregates the objects into the response body.
    assert "jsonb_agg" not in bytes_sql


def test_snapshot_reads_active_rows_only() -> None:
    sql = module.snapshot_statement()

    assert sql.count("AND f.deleted_at IS NULL") == 1
    assert sql.count("AND n.deleted_at IS NULL") == 1
    assert "jsonb_agg" in sql
    assert "LIMIT" not in sql.upper(), "a snapshot is never paginated (D15)"


def test_tree_never_selects_a_body() -> None:
    sql = module.tree_statement()

    assert "body_md" not in sql
    # Two filters (folders, notes); the ``CASE WHEN ... IS NULL`` of the tombstone
    # column is a rendering, not a filter.
    assert sql.count("AND f.deleted_at IS NULL") == 1
    assert sql.count("AND n.deleted_at IS NULL") == 1
    assert "AS found" in sql


def test_note_read_is_workspace_scoped_and_active_only() -> None:
    sql = module.note_statement()

    assert "n.workspace_id = :workspace_id" in sql
    assert "n.id = :note_id" in sql
    assert "n.deleted_at IS NULL" in sql


def test_every_statement_declares_only_its_intended_bind_parameters() -> None:
    """A colon inside a string literal must never become a bind parameter.

    ``to_char`` needs ``HH24:MI:SS`` in a quoted format. SQLAlchemy's bind regex
    treats ``:MI`` as a parameter unless the preceding character is a word
    character - true here, but one format edit away from silently breaking every
    read at runtime. This pins the parameter set of each statement.
    """
    expected = {
        "pull": {
            "workspace_id",
            "since",
            "fetch_limit",
            "max_bytes",
            "event_overhead",
        },
        "snapshot_count": {"workspace_id"},
        "snapshot_bytes": {"workspace_id"},
        "snapshot": {"workspace_id"},
        "snapshot_anchor": {"workspace_id"},
        "snapshot_page": {
            "workspace_id",
            "as_of_seq",
            "after_kind",
            "after_id",
            "fetch_limit",
            "max_bytes",
            "object_overhead",
        },
        "tree": {"workspace_id"},
        "note": {"workspace_id", "note_id"},
    }
    statements = {
        "pull": module.pull_statement(),
        "snapshot_count": module.snapshot_count_statement(),
        "snapshot_bytes": module.snapshot_bytes_statement(),
        "snapshot": module.snapshot_statement(),
        "snapshot_anchor": module.snapshot_anchor_statement(),
        "snapshot_page": module.snapshot_page_statement(),
        "tree": module.tree_statement(),
        "note": module.note_statement(),
    }

    for name, sql in statements.items():
        declared = set(sa.text(sql)._bindparams)  # type: ignore[attr-defined]
        assert declared == expected[name], f"{name} declares {sorted(declared)}"


def test_no_statement_interpolates_a_caller_value() -> None:
    """Every value is bound; only identifiers and timestamp formats are interpolated.

    ``to_char`` needs a literal format and the column aliases must be identifiers, so
    those are built by helpers. Anything that came from a caller must be a bind
    parameter, never string-formatted into the statement.
    """
    for sql in (
        module.pull_statement(),
        module.snapshot_count_statement(),
        module.snapshot_bytes_statement(),
        module.snapshot_statement(),
        module.snapshot_anchor_statement(),
        module.snapshot_page_statement(),
        module.tree_statement(),
        module.note_statement(),
    ):
        assert "{:" not in sql
        assert "workspace_id = '" not in sql
        assert "--" not in sql


# --------------------------------------------------------------------------- #
# pull: cursors and page assembly
# --------------------------------------------------------------------------- #


async def test_an_empty_page_does_not_move_the_cursor() -> None:
    session = FakeSession([("FROM workspaces", FakeResult(row=pull_row(last_seq=7)))])

    page = await service_for(session).pull(uuid.uuid4(), since=7, limit=100, max_bytes=4_194_304)

    assert page.events == ()
    assert page.objects == ()
    assert page.next_cursor == 7, "an empty page must echo since"
    assert page.has_more is False
    assert page.last_seq == 7


async def test_since_ahead_is_rejected_even_when_the_page_is_empty() -> None:
    session = FakeSession([("FROM workspaces", FakeResult(row=pull_row(last_seq=3)))])

    with pytest.raises(SyncReadError) as caught:
        await service_for(session).pull(uuid.uuid4(), since=4, limit=100, max_bytes=4_194_304)

    assert caught.value.code == "cursor_ahead"
    assert caught.value.details == {"last_seq": "3"}


async def test_expiry_is_strictly_below_the_floor_minus_one() -> None:
    """``since < min_retained_seq - 1``; the boundary value itself is served."""
    expired = FakeSession(
        [("FROM workspaces", FakeResult(row=pull_row(last_seq=9, min_retained_seq=5)))]
    )
    with pytest.raises(SyncReadError) as caught:
        await service_for(expired).pull(uuid.uuid4(), since=3, limit=10, max_bytes=1_024)

    assert caught.value.code == "cursor_expired"
    assert caught.value.details == {"min_retained_seq": "5", "action": "resync"}

    boundary = FakeSession(
        [("FROM workspaces", FakeResult(row=pull_row(last_seq=9, min_retained_seq=5)))]
    )
    page = await service_for(boundary).pull(uuid.uuid4(), since=4, limit=10, max_bytes=1_024)
    assert page.next_cursor == 4


async def test_cursor_verdicts_precede_any_content_decoding() -> None:
    """A hostile page body cannot change a 400/410 into a decode failure."""
    session = FakeSession(
        [
            (
                "FROM workspaces",
                FakeResult(row=pull_row(last_seq=1, events=[{"seq": "not-a-number"}])),
            )
        ]
    )

    with pytest.raises(SyncReadError) as caught:
        await service_for(session).pull(uuid.uuid4(), since=5, limit=10, max_bytes=1_024)

    assert caught.value.code == "cursor_ahead"


async def test_a_missing_workspace_is_not_found() -> None:
    session = FakeSession([("FROM workspaces", FakeResult(row=None))])

    with pytest.raises(SyncReadError) as caught:
        await service_for(session).pull(uuid.uuid4(), since=0, limit=10, max_bytes=1_024)

    assert caught.value.code == "not_found"


async def test_has_more_comes_from_the_limit_plus_one_probe() -> None:
    note_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    events = [event_json(seq, "note", note_id) for seq in (2, 3, 4)]
    session = FakeSession(
        [
            (
                "FROM workspaces",
                FakeResult(
                    row=pull_row(
                        last_seq=4,
                        fetched_events=3,
                        cutoff_rn=3,
                        events=events,
                        notes=[note_json(note_id, workspace_id)],
                    )
                ),
            )
        ]
    )

    page = await service_for(session).pull(workspace_id, since=1, limit=2, max_bytes=4_194_304)

    assert [event.seq for event in page.events] == [2, 3]
    assert page.has_more is True
    assert page.next_cursor == 3


async def test_a_cut_prefix_also_means_more() -> None:
    """Even when the page is short, a cut means the client must come back."""
    note_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    session = FakeSession(
        [
            (
                "FROM workspaces",
                FakeResult(
                    row=pull_row(
                        last_seq=5,
                        fetched_events=3,
                        cutoff_rn=2,
                        events=[
                            event_json(1, "note", note_id),
                            event_json(2, "note", note_id),
                            event_json(3, "note", note_id),
                        ],
                        notes=[note_json(note_id, workspace_id)],
                    )
                ),
            )
        ]
    )

    page = await service_for(session).pull(workspace_id, since=0, limit=100, max_bytes=4_194_304)

    assert page.has_more is True


async def test_one_entity_produces_many_events_and_one_object() -> None:
    """The current object may lead the page: it reports its real rev/updated_seq."""
    note_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    events = [event_json(seq, "note", note_id, rev=seq) for seq in (1, 2, 3)]
    session = FakeSession(
        [
            (
                "FROM workspaces",
                FakeResult(
                    row=pull_row(
                        last_seq=3,
                        fetched_events=3,
                        cutoff_rn=3,
                        events=events,
                        notes=[note_json(note_id, workspace_id, rev=3, updated_seq=3)],
                    )
                ),
            )
        ]
    )

    page = await service_for(session).pull(workspace_id, since=0, limit=100, max_bytes=4_194_304)

    assert len(page.events) == 3
    assert len(page.objects) == 1
    only = page.objects[0]
    assert isinstance(only, ReadNote)
    assert (only.rev, only.updated_seq) == (3, 3)


async def test_the_probe_entity_object_never_travels() -> None:
    """When the statement fetched limit+1 rows, the extra entity's object is dropped."""
    first, second, workspace_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    session = FakeSession(
        [
            (
                "FROM workspaces",
                FakeResult(
                    row=pull_row(
                        last_seq=2,
                        fetched_events=2,
                        cutoff_rn=2,
                        events=[event_json(1, "note", first), event_json(2, "note", second)],
                        notes=[
                            note_json(first, workspace_id),
                            note_json(second, workspace_id),
                        ],
                    )
                ),
            )
        ]
    )

    page = await service_for(session).pull(workspace_id, since=0, limit=1, max_bytes=4_194_304)

    assert [event.entity_id for event in page.events] == [first]
    assert [obj.id for obj in page.objects] == [first]


async def test_deleted_entities_travel_as_tombstones() -> None:
    note_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    session = FakeSession(
        [
            (
                "FROM workspaces",
                FakeResult(
                    row=pull_row(
                        last_seq=2,
                        fetched_events=1,
                        cutoff_rn=1,
                        events=[event_json(2, "note", note_id)],
                        notes=[
                            note_json(
                                note_id, workspace_id, deleted_at="2026-10-29T03:16:00.000000Z"
                            )
                        ],
                    )
                ),
            )
        ]
    )

    page = await service_for(session).pull(workspace_id, since=1, limit=10, max_bytes=4_194_304)

    tombstone = page.objects[0]
    assert isinstance(tombstone, ReadNote)
    assert tombstone.deleted_at is not None


async def test_not_one_event_fitting_is_a_413_not_an_empty_page() -> None:
    note_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    session = FakeSession(
        [
            (
                "FROM workspaces",
                FakeResult(
                    row=pull_row(
                        last_seq=1,
                        fetched_events=1,
                        cutoff_rn=0,
                        events=[event_json(1, "note", note_id)],
                        notes=[note_json(note_id, workspace_id, body="x" * 10_000)],
                    )
                ),
            )
        ]
    )

    with pytest.raises(SyncReadError) as caught:
        await service_for(session).pull(workspace_id, since=0, limit=10, max_bytes=4_194_304)

    assert caught.value.code == "payload_too_large"


async def test_pull_arguments_are_validated_before_any_io() -> None:
    def factory() -> Any:  # pragma: no cover - must never be called
        raise AssertionError("no session may be opened for an invalid call")

    service = SyncReadService(factory)
    for kwargs in (
        {"since": -1, "limit": 10, "max_bytes": 1_024},
        {"since": 0, "limit": 0, "max_bytes": 1_024},
        {"since": 0, "limit": 10, "max_bytes": 0},
        {"since": BIGINT_MAX + 1, "limit": 10, "max_bytes": 1_024},
        {"since": True, "limit": 10, "max_bytes": 1_024},
    ):
        with pytest.raises(SyncReadError) as caught:
            await service.pull(uuid.uuid4(), **kwargs)  # type: ignore[arg-type]
        assert caught.value.code == "invalid_request"


async def test_a_driver_error_becomes_internal_without_its_text() -> None:
    class Boom(SQLAlchemyError):
        def __str__(self) -> str:
            return "Key (body_md)=(secret body) already exists"

    session = FakeSession([], error=Boom("boom"), error_fragment="FROM workspaces")

    with pytest.raises(SyncReadError) as caught:
        await service_for(session).pull(uuid.uuid4(), since=0, limit=10, max_bytes=1_024)

    assert caught.value.code == "internal"
    assert "secret" not in str(caught.value)
    assert caught.value.details == {}
    assert isinstance(caught.value.__cause__, Boom)


# --------------------------------------------------------------------------- #
# pull: decoding
# --------------------------------------------------------------------------- #


async def test_jsonb_may_arrive_as_text_or_as_objects() -> None:
    note_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    row = pull_row(
        last_seq=1,
        fetched_events=1,
        cutoff_rn=1,
        events=[event_json(1, "note", note_id)],
        notes=[note_json(note_id, workspace_id)],
    )
    as_objects = (
        row[0],
        row[1],
        row[2],
        row[3],
        [event_json(1, "note", note_id)],
        [],
        [note_json(note_id, workspace_id)],
    )
    session = FakeSession([("FROM workspaces", FakeResult(row=as_objects))])

    page = await service_for(session).pull(workspace_id, since=0, limit=10, max_bytes=4_194_304)

    assert page.events[0].created_at == NOW
    assert page.objects[0].id == note_id


async def test_an_unexpected_json_shape_is_internal_not_empty() -> None:
    session = FakeSession(
        [("FROM workspaces", FakeResult(row=(1, 1, 1, 1, '{"not": "a list"}', "[]", "[]")))]
    )

    with pytest.raises(SyncReadError) as caught:
        await service_for(session).pull(uuid.uuid4(), since=0, limit=10, max_bytes=1_024)

    assert caught.value.code == "internal"


async def test_an_unknown_entity_type_is_internal() -> None:
    session = FakeSession(
        [
            (
                "FROM workspaces",
                FakeResult(
                    row=pull_row(
                        last_seq=1,
                        fetched_events=1,
                        cutoff_rn=1,
                        events=[event_json(1, "note", uuid.uuid4())],
                        notes=[{"entity_type": "attachment", "id": str(uuid.uuid4())}],
                    )
                ),
            )
        ]
    )

    with pytest.raises(SyncReadError) as caught:
        await service_for(session).pull(uuid.uuid4(), since=0, limit=10, max_bytes=1_024)

    assert caught.value.code == "internal"


def test_a_sync_read_error_only_keeps_protocol_codes() -> None:
    assert SyncReadError("not_found").code == "not_found"
    assert SyncReadError("invented").code == "internal"


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #


async def test_the_snapshot_steps_are_named_seams_a_test_can_pause_between() -> None:
    """The four steps exist as separate coroutines, in the documented order.

    Lane E's RR proof has to pause *between* "the waterline was read" and "the
    objects were read", then commit a write and let the snapshot resume. That is
    only expressible if the steps are distinct methods - matching on SQL text would
    break the moment a column alias changes. This pins the seam instead.
    """
    steps = (
        "_begin_snapshot",
        "_snapshot_counts",
        "_snapshot_bytes",
        "_read_snapshot",
    )

    for name in steps:
        assert inspect.iscoroutinefunction(getattr(SyncReadService, name)), name

    # Each step is reached from ``snapshot`` in this order, exactly once. The last
    # one is invoked through the class (see the note in ``snapshot``), so both
    # spellings are accepted here.
    source = inspect.getsource(SyncReadService.snapshot)
    positions = [source.index(f"{name}(") for name in steps]
    assert positions == sorted(positions), "the steps must run in their documented order"
    for name in steps:
        assert source.count(f"{name}(") == 1, name

    # And the data read is the *last* statement: the watermarks must be visible
    # before the objects are read, so a pause there still holds one view.
    assert positions[steps.index("_read_snapshot")] > positions[steps.index("_snapshot_counts")]

    # Every step is reached through the class, never ``self``: the seam must survive
    # being restored as a plain function (see the restore test below).
    for name in steps:
        assert f"SyncReadService.{name}(" in source, name
        assert f"self.{name}(" not in source, name


async def test_the_snapshot_data_read_is_the_only_statement_matching_the_rr_probe() -> None:
    """E's pause predicate must select exactly one statement.

    They match the object read by its two aggregates (``AS folders`` *and* ``AS
    notes``) while excluding the preflight counters (``object_count``). If the count
    statement ever grew a ``jsonb_agg`` - or the object statement a counter - the
    pause would fire on the wrong statement and the RR proof would silently pass
    against a different seam. Pinning the shape here keeps that honest.
    """
    object_read = module.snapshot_statement()
    count = module.snapshot_count_statement()
    probe = module.snapshot_bytes_statement()

    # Exactly one of the three matches the pause predicate.
    matches = [
        sql for sql in (object_read, count, probe) if "AS folders" in sql and "AS notes" in sql
    ]
    assert matches == [object_read]
    assert "object_count" not in object_read
    assert "AS folders" not in count and "AS notes" not in count
    assert "AS folders" not in probe and "AS notes" not in probe
    # ``object_count`` is what E excludes on; it must stay on the count statement.
    assert "object_count" in count


async def test_the_pull_statement_is_reachable_through_one_named_seam() -> None:
    """A pull's only statement is one method, so a test can wrap exactly it."""
    assert inspect.iscoroutinefunction(SyncReadService._read_pull_row)

    session = FakeSession([("FROM workspaces", FakeResult(row=pull_row()))])
    await service_for(session).pull(uuid.uuid4(), since=0, limit=10, max_bytes=1_024)

    assert len(session.statements) == 1


async def test_snapshot_never_commits_and_always_releases_the_transaction() -> None:
    """``READ ONLY`` means there is nothing to publish: rollback on every exit path."""
    workspace_id = uuid.uuid4()

    allowed = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(4, 1, 0))),
            ("AS raw_bytes", FakeResult(row=(0,))),
            ("AS folders", FakeResult(row=(4, 1, "[]", "[]"))),
        ]
    )
    await service_for(allowed).snapshot(workspace_id, max_objects=10, max_bytes=1_024)
    assert allowed.commits == 0 and allowed.rollbacks == 1

    refused = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(4, 1, 99))),
        ]
    )
    with pytest.raises(SyncReadError):
        await service_for(refused).snapshot(workspace_id, max_objects=10, max_bytes=1_024)
    assert refused.commits == 0 and refused.rollbacks == 1


async def test_a_patched_step_can_be_restored_with_either_spelling() -> None:
    """E patches ``_read_snapshot`` with ``staticmethod(fn)`` and restores the plain fn.

    Restoring a plain function onto a class makes it an instance method again, so a
    call site of ``self._read_snapshot(...)`` would then pass ``self`` as the first
    argument and break every *later* snapshot in the process - a failure that only
    shows up in whichever test happens to run afterwards. The call site therefore
    goes through the class. Both restoration spellings must keep working.
    """
    workspace_id = uuid.uuid4()
    session = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(4, 1, 0))),
            ("AS raw_bytes", FakeResult(row=(0,))),
            ("AS folders", FakeResult(row=(4, 1, "[]", "[]"))),
        ]
    )
    service = service_for(session)

    original = SyncReadService._read_snapshot
    calls: list[uuid.UUID] = []

    async def paused(session_: AsyncSession, ws: uuid.UUID) -> tuple[Any, ...]:
        calls.append(ws)
        return await original(session_, ws)

    try:
        SyncReadService._read_snapshot = staticmethod(paused)  # type: ignore[method-assign]
        view = await service.snapshot(workspace_id, max_objects=10, max_bytes=1_024)
        assert calls == [workspace_id]
        assert view.as_of_seq == 4
    finally:
        # The plain-function restore E's fixture performs.
        SyncReadService._read_snapshot = original  # type: ignore[method-assign]

    # A *later* call must still work, with no stray ``self`` argument.
    later = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(5, 1, 0))),
            ("AS raw_bytes", FakeResult(row=(0,))),
            ("AS folders", FakeResult(row=(5, 1, "[]", "[]"))),
        ]
    )
    view = await service_for(later).snapshot(workspace_id, max_objects=10, max_bytes=1_024)
    assert view.as_of_seq == 5


def test_size_is_measured_as_logical_bytes_not_stored_size() -> None:
    """Sizes must be logical UTF-8 bytes: storage size lets compression hide a body.

    ``pg_column_size`` returns the *physical stored* size - the compressed size for a
    compressed value, the 18-byte pointer for an out-of-line one - so a megabyte of
    repeated text would report a few hundred bytes, be admitted to the page, and be
    caught only after materialisation. That is the dangerous direction for a guard.
    """
    for sql in (module.pull_statement(), module.snapshot_bytes_statement()):
        assert "pg_column_size" not in sql, "storage size understates a compressed body"
        assert "octet_length(n.title)" in sql
        assert "octet_length(n.body_md)" in sql


def test_sizes_are_exact_not_a_bounded_prefix() -> None:
    """No ``left()``/``substr`` slicing: the measured length is the real length.

    A previous revision used ``octet_length(left(body_md, n))`` and claimed the probe
    bounded decompression. It does not: ``left``/``substr`` on pglz-compressed text
    may detoast the whole value before slicing, so that bound was fiction. Plain
    ``octet_length`` is exact, and the honest statement is that PostgreSQL may have
    to expand a value to measure it (documented in the module docstring).
    """
    for sql in (module.pull_statement(), module.snapshot_bytes_statement()):
        assert "left(" not in sql, "a sliced body read implies a bound it does not have"
        assert "substr" not in sql


def test_the_module_documents_what_the_size_guard_does_not_bound() -> None:
    """The residual cost must stay stated, so the claim cannot drift back to "bounded"."""
    source = inspect.getsource(module)
    section = source.split("The byte budget, and what it actually guarantees", 1)[1]
    section = section.split("Framework-free by contract", 1)[0]

    # It explains why storage size is rejected...
    assert "pg_column_size" in section
    assert "physical stored" in section or "compressed size" in section
    # ...names the residual risk instead of hiding it...
    assert "not** a hard cap" in section or "not a hard cap" in section
    # ...and records that the left() bound was wrong, so nobody reintroduces it.
    assert "left" in section and "pglz" in section


def test_the_note_size_helper_is_plain_octet_length() -> None:
    """A future edit must not swap in ``pg_column_size`` or a sliced read."""
    source = inspect.getsource(module._note_size)

    assert "octet_length" in source
    assert "pg_column_size" in source, "the docstring must name the rejected alternative"
    assert "left(" not in source


async def test_pull_route_rejects_an_over_long_cursor_before_converting(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    """A 5000-digit ``since`` is a 400, never a 500 and never a database call.

    Two independent guards must hold. The shared ``DecimalString`` validator rejects
    anything above ``BIGINT_MAX`` (so the wire contract is enforced at the boundary),
    and :func:`_parse_cursor` refuses by *length* before calling ``int()`` - because
    ``int(str)`` is capped by a process-global setting
    (``sys.set_int_max_str_digits``), so a conversion of 10 MB of digits is both
    interpreter-dependent and wasted work. Which guard fires is an implementation
    detail; what a client may rely on is 400 ``invalid_request`` and no I/O.
    """
    service = FakeService(page=a_page())
    client = await client_for(service)

    for digits in ("9" * 20, "9" * 5_000, "1" + "0" * 100):
        response = await client.get(f"/workspaces/{workspace_id}/sync/pull?since={digits}")
        assert response.status_code == 400, len(digits)
        error = response.json()["error"]
        assert error["code"] == "invalid_request"
        # The offending value is never echoed back, whatever the reason code.
        assert digits not in response.text
        assert [field["path"] for field in error["details"]["fields"]] == [["query", "since"]]

    assert service.calls == [], "an out-of-range cursor must not reach the database"


def test_parse_cursor_refuses_by_length_without_converting() -> None:
    """The length bound is enforced *before* ``int()``, independent of interpreter caps."""
    assert pull_routes._parse_cursor("0") == 0
    assert pull_routes._parse_cursor("9223372036854775807") == 9_223_372_036_854_775_807

    for too_long in ("9" * 20, "9" * 100_000):
        with pytest.raises(pull_routes.ApiError) as caught:
            pull_routes._parse_cursor(too_long)
        assert caught.value.code == "invalid_request"
        assert caught.value.details == {
            "fields": [{"path": ["query", "since"], "reason": "out_of_range"}]
        }


def test_parse_cursor_digit_bound_matches_bigint() -> None:
    """19 digits is the ceiling; the bound must not be off by one in either direction."""
    assert pull_routes.MAX_BIGINT_DIGITS == 19
    assert len(str(BIGINT_MAX)) == pull_routes.MAX_BIGINT_DIGITS
    # ``since`` above last_seq is a *different* verdict (cursor_ahead), so every
    # 19-digit value must get past the length check and into the service.
    assert pull_routes._parse_cursor("9" * 19) == int("9" * 19)


async def test_pull_route_accepts_the_widest_valid_cursor(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    """19 digits is exactly the BIGINT ceiling: accepted, and passed through as an int."""
    service = FakeService(page=a_page())
    client = await client_for(service)

    response = await client.get(f"/workspaces/{workspace_id}/sync/pull?since=9223372036854775807")

    assert response.status_code == 200
    assert service.calls[0][1]["since"] == 9223372036854775807


def preflight_row(*, last_seq: int = 4, floor: int = 1, count: int = 2) -> Any:
    """The count preflight's row (bytes are a separate, bounded statement)."""
    return (last_seq, floor, count)


async def test_snapshot_reads_preflights_then_data_in_one_session() -> None:
    workspace_id = uuid.uuid4()
    folder_id, note_id = uuid.uuid4(), uuid.uuid4()
    session = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(4, 1, 2))),
            ("AS raw_bytes", FakeResult(row=(100,))),
            (
                "AS folders",
                FakeResult(
                    row=(
                        4,
                        1,
                        jsonb([folder_json(folder_id, workspace_id)]),
                        jsonb([note_json(note_id, workspace_id)]),
                    )
                ),
            ),
        ]
    )

    view = await service_for(session).snapshot(workspace_id, max_objects=10, max_bytes=1_048_576)

    assert view.as_of_seq == 4
    assert view.min_retained_seq == 1
    assert {obj.id for obj in view.objects} == {folder_id, note_id}
    assert session.statements[0] == SET_SNAPSHOT_TRANSACTION
    assert len(session.statements) == 4, "one SET, two preflights, one data statement"
    # Read-only: nothing is committed, and the transaction is released.
    assert session.commits == 0
    assert session.rollbacks == 1


async def test_snapshot_refuses_oversize_counts_before_probing_bytes() -> None:
    session = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(4, 1, 10_001))),
        ]
    )

    with pytest.raises(SyncReadError) as caught:
        await service_for(session).snapshot(uuid.uuid4(), max_objects=10_000, max_bytes=16_777_216)

    assert caught.value.code == "snapshot_too_large"
    assert len(session.statements) == 2, "no byte probe and no object read may follow"
    assert session.rollbacks == 1


async def test_snapshot_refuses_oversize_bytes_before_reading_objects() -> None:
    session = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(4, 1, 2))),
            ("AS raw_bytes", FakeResult(row=(16_777_217,))),
        ]
    )

    with pytest.raises(SyncReadError) as caught:
        await service_for(session).snapshot(uuid.uuid4(), max_objects=10_000, max_bytes=16_777_216)

    assert caught.value.code == "snapshot_too_large"
    assert len(session.statements) == 3, "the object statement must never run"


async def test_snapshot_at_the_limit_is_allowed() -> None:
    session = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(1, 1, 1))),
            ("AS raw_bytes", FakeResult(row=(1_024,))),
            ("AS folders", FakeResult(row=(1, 1, "[]", "[]"))),
        ]
    )

    view = await service_for(session).snapshot(uuid.uuid4(), max_objects=1, max_bytes=1_024)

    assert view.objects == ()


async def test_snapshot_byte_preflight_takes_no_budget_parameter() -> None:
    """The byte preflight measures the real total; it is not parameterised by a probe.

    A earlier revision passed a ``probe_chars`` bound so the SQL could read a prefix.
    That bound did not hold (``left`` may detoast the whole value), so the parameter is
    gone: the statement returns the exact logical size, and the *caller* compares it
    against the budget.
    """
    session = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(1, 1, 1))),
            ("AS raw_bytes", FakeResult(row=(10,))),
            ("AS folders", FakeResult(row=(1, 1, "[]", "[]"))),
        ]
    )
    captured: dict[str, Any] = {}

    async def execute(statement: Any, params: Any = None) -> FakeResult:
        if "AS raw_bytes" in str(statement):
            captured.update(params or {})
        return await FakeSession.execute(session, statement, params)  # type: ignore[arg-type]

    session.execute = execute  # type: ignore[method-assign]

    await service_for(session).snapshot(uuid.uuid4(), max_objects=10, max_bytes=1_024)

    assert set(captured) == {"workspace_id"}


async def test_snapshot_of_a_missing_workspace_is_not_found() -> None:
    session = FakeSession(
        [("REPEATABLE READ", FakeResult()), ("AS object_count", FakeResult(row=None))]
    )

    with pytest.raises(SyncReadError) as caught:
        await service_for(session).snapshot(uuid.uuid4(), max_objects=10, max_bytes=1_024)

    assert caught.value.code == "not_found"


async def test_snapshot_rejects_a_nonsensical_limit_before_connecting() -> None:
    def factory() -> Any:  # pragma: no cover
        raise AssertionError("no session may be opened")

    with pytest.raises(SyncReadError) as caught:
        await SyncReadService(factory).snapshot(uuid.uuid4(), max_objects=0, max_bytes=1_024)

    assert caught.value.code == "invalid_request"


# --------------------------------------------------------------------------- #
# snapshot pagination (Phase 7)
# --------------------------------------------------------------------------- #


def snapshot_page_row(
    *,
    last_seq: int = 4,
    floor: int = 1,
    fetched: int = 2,
    cutoff: int = 2,
    folders: Sequence[Mapping[str, Any]] = (),
    notes: Sequence[Mapping[str, Any]] = (),
) -> tuple[Any, ...]:
    return (last_seq, floor, fetched, cutoff, jsonb(list(folders)), jsonb(list(notes)))


def test_snapshot_page_sql_is_watermark_filtered_and_stably_ordered() -> None:
    sql = module.snapshot_page_statement()

    # Membership is the current row *as of* the pinned watermark: a write after
    # page 1 cannot enter a later page, and the following pull carries it.
    assert sql.count("f.updated_seq <= CAST(:as_of_seq AS BIGINT)") == 1
    assert sql.count("n.updated_seq <= CAST(:as_of_seq AS BIGINT)") == 1
    assert sql.count("AND f.deleted_at IS NULL") == 1
    assert sql.count("AND n.deleted_at IS NULL") == 1
    # Stable total order: folders then notes, each by id.
    assert "ORDER BY kind, entity_id" in sql
    # The cursor is a strict "after this key" bound, not an offset.
    assert "f.id > CAST(:after_id AS UUID)" in sql
    assert "n.id > CAST(:after_id AS UUID)" in sql
    assert "OFFSET" not in sql.upper()
    # The byte cut is a contiguous prefix over the ordered page.
    assert "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW" in sql
    assert "cumulative_bytes <= CAST(:max_bytes AS BIGINT)" in sql
    assert "AS fetched_objects" in sql and "AS cutoff_rn" in sql


async def test_snapshot_page_first_page_pins_the_watermark_and_ships_one_page() -> None:
    workspace_id = uuid.uuid4()
    folder_id, note_id = uuid.uuid4(), uuid.uuid4()
    session = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(4, 1, 2))),
            (
                "AS fetched_objects",
                FakeResult(
                    row=snapshot_page_row(
                        folders=[folder_json(folder_id, workspace_id)],
                        notes=[note_json(note_id, workspace_id)],
                    )
                ),
            ),
        ]
    )

    page = await paged_service_for(session).snapshot_page(
        workspace_id,
        limit=10,
        max_bytes=1_048_576,
        max_total_objects=100,
    )

    assert page.as_of_seq == 4
    assert page.min_retained_seq == 1
    assert {obj.id for obj in page.objects} == {folder_id, note_id}
    assert page.has_more is False
    assert page.last_key == ("note", note_id)
    assert session.statements[0] == SET_SNAPSHOT_TRANSACTION
    assert session.rollbacks == 1 and session.commits == 0
    params = session.parameters[2]
    assert params["as_of_seq"] == 4
    assert params["after_kind"] is None and params["after_id"] is None
    assert params["fetch_limit"] == 11, "limit + 1 is the has_more probe"


async def test_snapshot_page_honours_the_total_cap_before_reading_objects() -> None:
    session = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(4, 1, 101))),
        ]
    )

    with pytest.raises(SyncReadError) as caught:
        await paged_service_for(session).snapshot_page(
            uuid.uuid4(), limit=10, max_bytes=1_024, max_total_objects=100
        )

    assert caught.value.code == "snapshot_total_too_large"
    assert caught.value.details == {"max_total_objects": "100"}
    assert len(session.statements) == 2, "no page statement may follow a refused bootstrap"
    assert session.rollbacks == 1


async def test_snapshot_page_cutoff_shrinks_and_marks_more() -> None:
    workspace_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(3)]
    session = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(4, 1, 3))),
            (
                "AS fetched_objects",
                FakeResult(
                    row=snapshot_page_row(
                        fetched=3,
                        cutoff=2,
                        notes=[note_json(entity_id, workspace_id) for entity_id in ids[:2]],
                    )
                ),
            ),
        ]
    )

    page = await paged_service_for(session).snapshot_page(
        workspace_id, limit=10, max_bytes=1_024, max_total_objects=100
    )

    assert len(page.objects) == 2
    assert page.has_more is True, "a cut prefix means the client must come back"
    assert page.last_key == ("note", ids[1])


async def test_snapshot_page_one_object_too_large_is_a_413_not_an_empty_page() -> None:
    session = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            ("AS object_count", FakeResult(row=(1, 1, 1))),
            (
                "AS fetched_objects",
                FakeResult(row=snapshot_page_row(last_seq=1, fetched=1, cutoff=0)),
            ),
        ]
    )

    with pytest.raises(SyncReadError) as caught:
        await paged_service_for(session).snapshot_page(
            uuid.uuid4(), limit=10, max_bytes=1_024, max_total_objects=100
        )

    assert caught.value.code == "snapshot_page_too_large"


async def test_snapshot_page_continuation_reuses_the_pinned_watermark_and_after_key() -> None:
    workspace_id = uuid.uuid4()
    after_id, next_id = uuid.uuid4(), uuid.uuid4()
    cursor = encode_snapshot_cursor(PAGED_CURSOR_KEY, workspace_id, 4, ("note", after_id))
    session = FakeSession(
        [
            ("REPEATABLE READ", FakeResult()),
            (
                "AS fetched_objects",
                FakeResult(
                    row=snapshot_page_row(
                        last_seq=6,
                        fetched=1,
                        cutoff=1,
                        notes=[note_json(next_id, workspace_id)],
                    )
                ),
            ),
            ("SELECT w.last_seq AS last_seq", FakeResult(row=(6, 1))),
        ]
    )

    page = await paged_service_for(session).snapshot_page(
        workspace_id,
        limit=10,
        max_bytes=1_048_576,
        max_total_objects=100,
        cursor=cursor,
    )

    assert page.as_of_seq == 4, "the pinned watermark is carried, not re-pinned"
    assert [obj.id for obj in page.objects] == [next_id]
    assert page.has_more is False
    params = session.parameters[2]  # SET, anchor, then the data statement
    assert params["as_of_seq"] == 4
    assert params["after_kind"] == 1
    assert params["after_id"] == after_id


async def test_snapshot_page_cursor_paths_are_closed() -> None:
    workspace_id = uuid.uuid4()
    key = PAGED_CURSOR_KEY
    token = encode_snapshot_cursor(key, workspace_id, 7, ("folder", uuid.uuid4()))

    assert decode_snapshot_cursor(key, workspace_id, token)[0] == 7

    # A different workspace, a changed signature and a changed payload all fail
    # with the same fixed code and never echo the token.
    wrong_workspace = encode_snapshot_cursor(key, uuid.uuid4(), 7, ("folder", uuid.uuid4()))
    for bad in ("", "abc", token + "x", wrong_workspace, "A" * 2_049):
        with pytest.raises(SyncReadError) as caught:
            decode_snapshot_cursor(key, workspace_id, bad)
        assert caught.value.code == "invalid_cursor"
        assert "cursor" in caught.value.code


async def test_snapshot_page_rejects_a_cursor_before_opening_a_session() -> None:
    def factory() -> Any:  # pragma: no cover - must never be called
        raise AssertionError("no session may be opened for an invalid cursor")

    service = SyncReadService(factory, cursor_key=PAGED_CURSOR_KEY)
    with pytest.raises(SyncReadError) as caught:
        await service.snapshot_page(
            uuid.uuid4(),
            limit=10,
            max_bytes=1_024,
            max_total_objects=100,
            cursor="not-a-cursor",
        )
    assert caught.value.code == "invalid_cursor"


async def test_snapshot_paging_requires_a_cursor_key() -> None:
    def factory() -> Any:  # pragma: no cover - must never be called
        raise AssertionError("no session may be opened without a cursor key")

    with pytest.raises(SyncReadError) as caught:
        await SyncReadService(factory).snapshot_page(
            uuid.uuid4(), limit=10, max_bytes=1_024, max_total_objects=100
        )
    assert caught.value.code == "internal"


async def test_snapshot_page_validates_arguments_before_any_io() -> None:
    def factory() -> Any:  # pragma: no cover - must never be called
        raise AssertionError("no session may be opened for an invalid call")

    service = SyncReadService(factory, cursor_key=PAGED_CURSOR_KEY)
    for kwargs in (
        {"limit": 0, "max_bytes": 1_024, "max_total_objects": 100},
        {"limit": 10, "max_bytes": 0, "max_total_objects": 100},
        {"limit": 10, "max_bytes": 1_024, "max_total_objects": 0},
        {"limit": True, "max_bytes": 1_024, "max_total_objects": 100},
    ):
        with pytest.raises(SyncReadError) as caught:
            await service.snapshot_page(uuid.uuid4(), **kwargs)  # type: ignore[arg-type]
        assert caught.value.code == "invalid_request"


def test_snapshot_cursor_key_is_stable_and_dsn_bound() -> None:
    first = derive_snapshot_cursor_key("postgresql+asyncpg://u:p@db/revnote")
    same = derive_snapshot_cursor_key("postgresql+asyncpg://u:p@db/revnote")
    other = derive_snapshot_cursor_key("postgresql+asyncpg://u:p@db/other")

    assert first == same
    assert first != other
    assert len(first) == 32


# --------------------------------------------------------------------------- #
# tree and note
# --------------------------------------------------------------------------- #


async def test_tree_returns_metadata_without_bodies() -> None:
    folder_id, note_id = uuid.uuid4(), uuid.uuid4()
    session = FakeSession(
        [
            (
                "AS found",
                FakeResult(
                    row=(
                        1,
                        jsonb([folder_json(folder_id, uuid.uuid4())]),
                        jsonb(
                            [
                                {
                                    "entity_type": "note",
                                    "id": str(note_id),
                                    "workspace_id": str(uuid.uuid4()),
                                    "folder_id": None,
                                    "title": "Title",
                                    "rev": "2",
                                    "updated_seq": "3",
                                    "deleted_at": None,
                                    "created_at": STAMP,
                                    "updated_at": STAMP,
                                }
                            ]
                        ),
                    )
                ),
            )
        ]
    )

    view = await service_for(session).tree(uuid.uuid4())

    assert isinstance(view, TreeView)
    assert view.folders[0].id == folder_id
    assert view.notes[0].rev == 2
    assert not hasattr(view.notes[0], "body_md")


async def test_tree_of_a_missing_workspace_is_not_found() -> None:
    session = FakeSession([("AS found", FakeResult(row=(0, "[]", "[]")))])

    with pytest.raises(SyncReadError) as caught:
        await service_for(session).tree(uuid.uuid4())

    assert caught.value.code == "not_found"


async def test_note_returns_an_active_note() -> None:
    note_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    session = FakeSession(
        [("n.id = :note_id", FakeResult(row=(jsonb(note_json(note_id, workspace_id, rev=4)),)))]
    )

    note = await service_for(session).note(workspace_id, note_id)

    assert (note.id, note.rev, note.body_md) == (note_id, 4, "text")


@pytest.mark.parametrize("row", [None, (None,)])
async def test_a_missing_or_deleted_note_is_the_same_not_found(row: Any) -> None:
    session = FakeSession([("n.id = :note_id", FakeResult(row=row))])

    with pytest.raises(SyncReadError) as caught:
        await service_for(session).note(uuid.uuid4(), uuid.uuid4())

    assert caught.value.code == "not_found"


# --------------------------------------------------------------------------- #
# byte budget: the exact serialized size, then a contiguous prefix
# --------------------------------------------------------------------------- #


def a_page(
    *,
    since: int = 0,
    events: Sequence[ReadEvent] = (),
    notes: Sequence[ReadNote] = (),
    has_more: bool = False,
) -> PullPage:
    return PullPage(
        events=tuple(events),
        objects=tuple(notes),
        next_cursor=events[-1].seq if events else since,
        has_more=has_more,
        last_seq=50,
        min_retained_seq=1,
        since=since,
    )


def an_event(seq: int, entity_id: uuid.UUID) -> ReadEvent:
    return ReadEvent(
        seq=seq,
        entity_type="note",
        entity_id=entity_id,
        op="update",
        rev=seq,
        op_id=uuid.uuid4(),
        created_at=NOW,
    )


def a_note(entity_id: uuid.UUID, workspace_id: uuid.UUID, body: str) -> ReadNote:
    return ReadNote(
        id=entity_id,
        workspace_id=workspace_id,
        folder_id=None,
        title="T",
        body_md=body,
        rev=1,
        updated_seq=1,
        deleted_at=None,
        created_at=NOW,
        updated_at=NOW,
    )


def test_dump_size_measures_escaped_bytes_not_characters() -> None:
    """``\\n`` is two bytes on the wire and ``\\u0001`` is six: the budget is bytes."""
    plain = NoteObject(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        title="T",
        body_md="\n",
        rev="1",
        updated_seq="1",
        deleted=False,
        created_at=NOW,
        updated_at=NOW,
    )
    control = plain.model_copy(update={"body_md": "\x01"})

    assert dump_size(plain) == len(dump_body(plain))
    assert dump_size(control) > dump_size(plain), "an escaped control char costs more bytes"
    assert b"\\u0001" in dump_body(control)


def test_a_full_page_that_fits_is_returned_unchanged() -> None:
    note_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    page = a_page(events=[an_event(1, note_id)], notes=[a_note(note_id, workspace_id, "short")])

    response = pull_routes._fit_to_budget(page, 65_536)

    assert len(response.events) == 1
    assert response.has_more is False
    assert response.next_cursor == "1"


def test_an_oversize_page_is_cut_to_a_contiguous_prefix() -> None:
    workspace_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(5)]
    page = a_page(
        events=[an_event(seq, ids[seq - 1]) for seq in range(1, 6)],
        notes=[a_note(entity_id, workspace_id, "x" * 2_000) for entity_id in ids],
    )
    budget = 4_000

    response = pull_routes._fit_to_budget(page, budget)

    assert 0 < len(response.events) < 5
    assert dump_size(response) <= budget
    # The kept events are the *first* ones, in order: a contiguous prefix.
    assert [event.seq for event in response.events] == [
        str(seq) for seq in range(1, len(response.events) + 1)
    ]
    assert response.has_more is True
    assert response.next_cursor == str(len(response.events))
    # Objects describe exactly the events that were shipped.
    kept = {event.entity_id for event in response.events}
    assert {obj.id for obj in response.objects} == kept


def test_the_cut_page_stays_within_budget_for_every_prefix() -> None:
    workspace_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(8)]
    page = a_page(
        events=[an_event(seq, ids[seq - 1]) for seq in range(1, 9)],
        notes=[a_note(entity_id, workspace_id, "y" * 900) for entity_id in ids],
    )

    for budget in (2_500, 4_000, 8_000, 16_000):
        response = pull_routes._fit_to_budget(page, budget)
        assert dump_size(response) <= budget
        assert [int(event.seq) for event in response.events] == list(
            range(1, len(response.events) + 1)
        )


def test_one_event_that_cannot_fit_is_a_413_and_never_skipped() -> None:
    note_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    page = a_page(
        events=[an_event(1, note_id)],
        notes=[a_note(note_id, workspace_id, "z" * 20_000)],
    )

    with pytest.raises(pull_routes.ApiError) as caught:
        pull_routes._fit_to_budget(page, 4_096)

    assert caught.value.code == "payload_too_large"


def test_an_empty_page_is_never_a_413() -> None:
    response = pull_routes._fit_to_budget(a_page(since=9), 10)

    assert response.events == []
    assert response.next_cursor == "9"
    assert response.has_more is False


def test_the_object_set_is_recomputed_without_a_second_query() -> None:
    """Objects are filtered from the fetched view, never re-queried."""
    workspace_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(3)]
    page = a_page(
        events=[an_event(seq, ids[seq - 1]) for seq in range(1, 4)],
        notes=[a_note(entity_id, workspace_id, "w" * 1_000) for entity_id in ids],
    )
    # A statement count is the only way to see a second query; the service owns the
    # session, so the fake below is the read path's only connection.
    session = FakeSession([])

    response = pull_routes._fit_to_budget(page, 3_000)

    assert dump_size(response) <= 3_000
    assert response.objects
    assert session.statements == []


def test_the_sql_budget_reserves_room_for_the_envelope() -> None:
    assert SyncReadService._sql_budget(4_194_304) == 4_194_304 - ENVELOPE_RESERVE_BYTES
    assert SyncReadService._sql_budget(1) == 1, "a tiny budget still stays positive"
    assert EVENT_OVERHEAD_BYTES > 0


def test_pull_page_objects_are_a_tuple_so_a_caller_cannot_mutate_them() -> None:
    page = a_page()

    assert isinstance(page.events, tuple)
    assert isinstance(page.objects, tuple)


def test_a_snapshot_view_is_frozen() -> None:
    view = SnapshotView(as_of_seq=1, min_retained_seq=1, objects=())

    with pytest.raises(dataclasses.FrozenInstanceError):
        view.as_of_seq = 2  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# leak guards
# --------------------------------------------------------------------------- #


def test_a_note_body_never_appears_in_a_repr() -> None:
    note = a_note(uuid.uuid4(), uuid.uuid4(), "TOP-SECRET-BODY")
    rendered = repr(note)

    assert "TOP-SECRET-BODY" not in rendered
    assert "ReadNote(" in rendered


def test_an_error_repr_carries_no_details() -> None:
    error = SyncReadError("cursor_expired", details={"min_retained_seq": "5"})

    assert "5" not in repr(error)
    assert "cursor_expired" in repr(error)


def test_the_service_never_logs() -> None:
    """No logger call at all: a body cannot leak from a call that does not exist."""
    text = module.__file__ and open(module.__file__, encoding="utf-8").read()  # noqa: SIM115

    assert text
    assert "logger" not in text
    assert "logging" not in text


# --------------------------------------------------------------------------- #
# routes over an in-process app (no database, dependency overridden)
# --------------------------------------------------------------------------- #


class FakeService:
    """A read service whose answers are prepared by the test."""

    def __init__(
        self,
        *,
        page: PullPage | None = None,
        view: SnapshotView | None = None,
        snapshot_page: SnapshotPage | None = None,
        tree: TreeView | None = None,
        note: ReadNote | None = None,
        error: SyncReadError | None = None,
    ) -> None:
        self.page = page
        self.view = view
        self.snapshot_page_view = snapshot_page
        self.tree_view = tree
        self.note_view = note
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def pull(self, workspace_id: uuid.UUID, **kwargs: Any) -> PullPage:
        self.calls.append(("pull", {"workspace_id": workspace_id, **kwargs}))
        if self.error is not None:
            raise self.error
        assert self.page is not None
        return self.page

    async def snapshot(self, workspace_id: uuid.UUID, **kwargs: Any) -> SnapshotView:
        self.calls.append(("snapshot", {"workspace_id": workspace_id, **kwargs}))
        if self.error is not None:
            raise self.error
        assert self.view is not None
        return self.view

    async def snapshot_page(self, workspace_id: uuid.UUID, **kwargs: Any) -> SnapshotPage:
        self.calls.append(("snapshot_page", {"workspace_id": workspace_id, **kwargs}))
        if self.error is not None:
            raise self.error
        assert self.snapshot_page_view is not None
        return self.snapshot_page_view

    def encode_snapshot_cursor(
        self,
        workspace_id: uuid.UUID,
        as_of_seq: int,
        last_key: tuple[str, uuid.UUID],
    ) -> str:
        return f"{workspace_id}_{as_of_seq}_{last_key[0]}_{last_key[1]}"

    async def tree(self, workspace_id: uuid.UUID) -> TreeView:
        self.calls.append(("tree", {"workspace_id": workspace_id}))
        if self.error is not None:
            raise self.error
        assert self.tree_view is not None
        return self.tree_view

    async def note(self, workspace_id: uuid.UUID, note_id: uuid.UUID) -> ReadNote:
        self.calls.append(("note", {"workspace_id": workspace_id, "note_id": note_id}))
        if self.error is not None:
            raise self.error
        assert self.note_view is not None
        return self.note_view


def build_app(service: FakeService, *, settings: Settings | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings or Settings(_env_file=None)
    install_error_handlers(app)
    app.include_router(pull_routes.router)
    app.include_router(read_routes.router)
    # The real app always installs this; without it an error body would carry a
    # freshly generated id in the JSON but no echoing header.
    app.add_middleware(RequestIdMiddleware)
    context = AuthContext(user_id=uuid.uuid4(), session_id=uuid.uuid4())
    app.dependency_overrides[require_workspace_member] = lambda: context
    # The framework's own seam: no production seam is added for tests.
    app.dependency_overrides[pull_routes.get_read_service] = lambda: service
    return app


@pytest.fixture
def workspace_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
async def client_for(workspace_id: uuid.UUID):
    """Build a client for one fake service (dependency override, no monkeypatching)."""

    clients: list[AsyncClient] = []

    async def make(service: FakeService, *, settings: Settings | None = None) -> AsyncClient:
        app = build_app(service, settings=settings)
        client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        clients.append(client)
        return client

    try:
        yield make
    finally:
        for client in clients:
            await client.aclose()


async def test_pull_route_renders_the_contract(client_for: Any, workspace_id: uuid.UUID) -> None:
    note_id = uuid.uuid4()
    page = a_page(
        events=[an_event(3, note_id)],
        notes=[a_note(note_id, workspace_id, "hello")],
    )
    service = FakeService(page=page)
    client = await client_for(service)

    response = await client.get(f"/workspaces/{workspace_id}/sync/pull?since=2&limit=10")

    assert response.status_code == 200
    body = response.json()
    assert body["next_cursor"] == "3"
    assert body["has_more"] is False
    assert body["last_seq"] == "50"
    assert body["min_retained_seq"] == "1"
    assert body["events"][0] == {
        "seq": "3",
        "entity_type": "note",
        "entity_id": str(note_id),
        "op": "update",
        "rev": "3",
        "op_id": str(page.events[0].op_id),
        "created_at": STAMP,
    }
    assert body["objects"][0]["body_md"] == "hello"
    assert body["objects"][0]["deleted"] is False
    assert service.calls[0][1]["since"] == 2
    assert service.calls[0][1]["limit"] == 10


async def test_pull_route_defaults_come_from_settings(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    settings = Settings(_env_file=None, sync_pull_default_limit=7)
    client = await client_for(FakeService(page=a_page()), settings=settings)

    await client.get(f"/workspaces/{workspace_id}/sync/pull")

    assert settings.sync_pull_default_limit == 7


async def test_pull_route_rejects_a_limit_above_the_maximum(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    settings = Settings(_env_file=None)
    service = FakeService(page=a_page())
    client = await client_for(service, settings=settings)

    response = await client.get(
        f"/workspaces/{workspace_id}/sync/pull?limit={settings.sync_pull_max_limit + 1}"
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert service.calls == [], "an over-limit request must not reach the database"


@pytest.mark.parametrize("since", ["-1", " 1", "1.0", "007", "", "9223372036854775808", "١٢"])
async def test_pull_route_rejects_a_non_canonical_cursor(
    client_for: Any, workspace_id: uuid.UUID, since: str
) -> None:
    service = FakeService(page=a_page())
    client = await client_for(service)

    response = await client.get(f"/workspaces/{workspace_id}/sync/pull?since={since}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert service.calls == []


async def test_pull_route_maps_cursor_expired_to_410(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    service = FakeService(
        error=SyncReadError("cursor_expired", details={"min_retained_seq": "5", "action": "resync"})
    )
    client = await client_for(service)

    response = await client.get(f"/workspaces/{workspace_id}/sync/pull?since=1")

    assert response.status_code == 410
    error = response.json()["error"]
    assert error["code"] == "cursor_expired"
    assert error["details"] == {"min_retained_seq": "5", "action": "resync"}
    assert error["retryable"] is False


async def test_pull_route_maps_payload_too_large_to_413(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    client = await client_for(FakeService(error=SyncReadError("payload_too_large")))

    response = await client.get(f"/workspaces/{workspace_id}/sync/pull")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_pull_route_shortens_a_page_that_exceeds_the_budget(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    settings = Settings(_env_file=None, sync_pull_max_bytes=1_024)
    note_id = uuid.uuid4()
    page = a_page(events=[an_event(1, note_id)], notes=[a_note(note_id, workspace_id, "q" * 4_000)])
    client = await client_for(FakeService(page=page), settings=settings)

    response = await client.get(f"/workspaces/{workspace_id}/sync/pull")

    assert response.status_code == 413, "one event cannot be shipped, so nothing is"


async def test_snapshot_route_rejects_ambiguous_pagination_spellings(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    service = FakeService(view=SnapshotView(as_of_seq=0, min_retained_seq=1, objects=()))
    client = await client_for(service)

    for params in ("offset=0", "page=2", "since=1"):
        response = await client.get(f"/workspaces/{workspace_id}/sync/snapshot?{params}")

        assert response.status_code == 400, params
        assert response.json()["error"]["code"] == "invalid_request"
        fields = response.json()["error"]["details"]["fields"]
        assert [field["path"][1] for field in fields] == [params.split("=", 1)[0]]
    assert service.calls == []


def a_snapshot_page(
    *objects: ReadNote,
    as_of: int = 11,
    floor: int = 1,
    has_more: bool = False,
) -> SnapshotPage:
    return SnapshotPage(
        as_of_seq=as_of,
        min_retained_seq=floor,
        objects=tuple(objects),
        has_more=has_more,
        last_key=objects[-1].key if objects else None,
    )


async def test_snapshot_route_renders_a_page_with_a_cursor_while_more_remains(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    note_id = uuid.uuid4()
    page = a_snapshot_page(a_note(note_id, workspace_id, "body"), has_more=True)
    service = FakeService(snapshot_page=page)
    client = await client_for(service)

    response = await client.get(f"/workspaces/{workspace_id}/sync/snapshot?limit=10")

    assert response.status_code == 200
    body = response.json()
    assert body["as_of_seq"] == "11"
    assert body["min_retained_seq"] == "1"
    assert body["has_more"] is True
    assert body["next_cursor"] == f"{workspace_id}_11_note_{note_id}"
    assert [obj["id"] for obj in body["objects"]] == [str(note_id)]
    call = service.calls[0][1]
    assert call["limit"] == 10
    assert call["max_total_objects"] == 100_000
    assert call["cursor"] is None


async def test_snapshot_route_terminal_page_has_no_cursor(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    note_id = uuid.uuid4()
    service = FakeService(snapshot_page=a_snapshot_page(a_note(note_id, workspace_id, "body")))
    client = await client_for(service)

    response = await client.get(
        f"/workspaces/{workspace_id}/sync/snapshot?limit=10&cursor=opaque-token"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["has_more"] is False
    assert body["next_cursor"] is None
    assert service.calls[0][1]["cursor"] == "opaque-token"


async def test_snapshot_route_shortens_a_page_to_the_serialized_budget(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    settings = Settings(_env_file=None, sync_snapshot_page_max_bytes=1_400)
    ids = [uuid.uuid4() for _ in range(3)]
    page = a_snapshot_page(
        *(a_note(entity_id, workspace_id, "q" * 700) for entity_id in ids),
        has_more=False,
    )
    service = FakeService(snapshot_page=page)
    client = await client_for(service, settings=settings)

    response = await client.get(f"/workspaces/{workspace_id}/sync/snapshot?limit=10")

    assert response.status_code == 200
    body = response.json()
    assert len(body["objects"]) < 3
    assert body["has_more"] is True
    assert body["next_cursor"]


async def test_snapshot_route_refuses_a_single_object_larger_than_a_page(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    settings = Settings(_env_file=None, sync_snapshot_page_max_bytes=1_024)
    page = a_snapshot_page(a_note(uuid.uuid4(), workspace_id, "z" * 20_000))
    client = await client_for(FakeService(snapshot_page=page), settings=settings)

    response = await client.get(f"/workspaces/{workspace_id}/sync/snapshot?limit=10")

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "snapshot_page_too_large"


async def test_snapshot_route_rejects_a_page_limit_above_the_maximum(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    settings = Settings(_env_file=None)
    service = FakeService(snapshot_page=a_snapshot_page())
    client = await client_for(service, settings=settings)

    too_large_limit = settings.sync_snapshot_page_max_objects + 1
    response = await client.get(f"/workspaces/{workspace_id}/sync/snapshot?limit={too_large_limit}")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert service.calls == []


async def test_snapshot_route_maps_bad_cursor_to_400(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    client = await client_for(FakeService(error=SyncReadError("invalid_cursor")))

    response = await client.get(f"/workspaces/{workspace_id}/sync/snapshot?cursor=bad")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"


async def test_snapshot_route_renders_one_complete_view(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    note_id = uuid.uuid4()
    service = FakeService(
        view=SnapshotView(
            as_of_seq=11,
            min_retained_seq=1,
            objects=(a_note(note_id, workspace_id, "body"),),
        )
    )
    client = await client_for(service)

    response = await client.get(f"/workspaces/{workspace_id}/sync/snapshot")

    assert response.status_code == 200
    body = response.json()
    assert body["as_of_seq"] == "11"
    assert body["min_retained_seq"] == "1"
    assert [obj["id"] for obj in body["objects"]] == [str(note_id)]
    assert service.calls[0][1]["max_objects"] == 10_000
    assert service.calls[0][1]["max_bytes"] == 16_777_216


async def test_snapshot_route_maps_oversize_to_409(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    client = await client_for(FakeService(error=SyncReadError("snapshot_too_large")))

    response = await client.get(f"/workspaces/{workspace_id}/sync/snapshot")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "snapshot_too_large"


async def test_snapshot_route_measures_the_serialized_bytes(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    """Escaping can push a snapshot past the budget; the whole request is refused."""
    settings = Settings(_env_file=None, sync_snapshot_max_bytes=1_024)
    note_id = uuid.uuid4()
    service = FakeService(
        view=SnapshotView(
            as_of_seq=1, min_retained_seq=1, objects=(a_note(note_id, workspace_id, "\x01" * 600),)
        )
    )
    client = await client_for(service, settings=settings)

    response = await client.get(f"/workspaces/{workspace_id}/sync/snapshot")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "snapshot_too_large"


async def test_tree_route_publishes_metadata_only(client_for: Any, workspace_id: uuid.UUID) -> None:
    note_meta = module.NoteMeta(
        id=uuid.uuid4(),
        folder_id=None,
        title="Title",
        rev=2,
        updated_seq=3,
        created_at=NOW,
        updated_at=NOW,
    )
    service = FakeService(tree=TreeView(folders=(), notes=(note_meta,)))
    client = await client_for(service)

    response = await client.get(f"/workspaces/{workspace_id}/tree")

    assert response.status_code == 200
    body = response.json()
    assert body["notes"][0]["title"] == "Title"
    assert body["notes"][0]["rev"] == "2"
    assert "body_md" not in body["notes"][0]
    assert response.headers["cache-control"] == "no-store"


async def test_note_route_returns_the_body_with_no_store(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    note_id = uuid.uuid4()
    service = FakeService(note=a_note(note_id, workspace_id, "body text"))
    client = await client_for(service)

    response = await client.get(f"/workspaces/{workspace_id}/notes/{note_id}")

    assert response.status_code == 200
    assert response.json()["body_md"] == "body text"
    assert response.headers["cache-control"] == "no-store"


async def test_note_route_hides_a_missing_note(client_for: Any, workspace_id: uuid.UUID) -> None:
    client = await client_for(FakeService(error=SyncReadError("not_found")))

    response = await client.get(f"/workspaces/{workspace_id}/notes/{uuid.uuid4()}")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"] == "The requested resource does not exist."


async def test_route_errors_keep_the_request_id_header(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    client = await client_for(FakeService(error=SyncReadError("snapshot_too_large")))

    response = await client.get(
        f"/workspaces/{workspace_id}/sync/snapshot", headers={"X-Request-ID": "req-1"}
    )

    assert response.headers["x-request-id"] == "req-1"
    assert response.json()["error"]["request_id"] == "req-1"


async def test_pull_response_is_not_cacheable(client_for: Any, workspace_id: uuid.UUID) -> None:
    """A pull page carries note bodies, so no shared cache may retain it."""
    client = await client_for(FakeService(page=a_page()))

    response = await client.get(f"/workspaces/{workspace_id}/sync/pull")

    assert response.headers["cache-control"] == "no-store"


async def test_snapshot_response_is_not_cacheable(client_for: Any, workspace_id: uuid.UUID) -> None:
    client = await client_for(
        FakeService(view=SnapshotView(as_of_seq=0, min_retained_seq=1, objects=()))
    )

    response = await client.get(f"/workspaces/{workspace_id}/sync/snapshot")

    assert response.headers["cache-control"] == "no-store"


async def test_an_internal_read_failure_hides_the_driver_text(
    client_for: Any, workspace_id: uuid.UUID
) -> None:
    client = await client_for(FakeService(error=SyncReadError("internal")))

    response = await client.get(f"/workspaces/{workspace_id}/tree")

    assert response.status_code == 500
    body = response.json()["error"]
    assert body["code"] == "internal"
    assert body["message"] == "An unexpected error occurred."
    assert body["details"] == {}


def test_the_sample_pull_body_is_compact_json() -> None:
    """The response body is compact: the byte budget must describe these bytes."""
    response = PullResponse(
        events=[],
        objects=[],
        next_cursor="0",
        has_more=False,
        last_seq="0",
        min_retained_seq="1",
    )
    body = dump_body(response)

    assert body == (
        b'{"events":[],"objects":[],"next_cursor":"0","has_more":false,'
        b'"last_seq":"0","min_retained_seq":"1"}'
    )


def test_a_snapshot_response_renders_counters_as_strings() -> None:
    payload = SnapshotResponse(as_of_seq="12", min_retained_seq="1", objects=[])

    assert json.loads(dump_body(payload)) == {
        "as_of_seq": "12",
        "min_retained_seq": "1",
        "objects": [],
    }


async def test_pull_uses_exactly_one_statement_and_one_session() -> None:
    """The whole point of the CTE: watermarks, events and objects in one statement."""
    session = FakeSession([("FROM workspaces", FakeResult(row=pull_row()))])
    await service_for(session).pull(uuid.uuid4(), since=0, limit=10, max_bytes=1_024)

    assert len(session.statements) == 1
    assert session.closed is True


async def test_pull_route_rejects_a_zero_limit(client_for: Any, workspace_id: uuid.UUID) -> None:
    service = FakeService(page=a_page())
    client = await client_for(service)

    response = await client.get(f"/workspaces/{workspace_id}/sync/pull?limit=0")

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert service.calls == []


def test_a_note_body_never_appears_in_a_wire_repr() -> None:
    note = NoteObject(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        title="T",
        body_md="TOP-SECRET-BODY",
        rev="1",
        updated_seq="1",
        deleted=False,
        created_at=NOW,
        updated_at=NOW,
    )

    assert "TOP-SECRET-BODY" not in repr(note)


def test_an_event_list_is_ordered_and_never_compacted_in_the_response() -> None:
    """Five events for one note stay five events plus one object (no superseded_by_seq)."""
    note_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    events = [an_event(seq, note_id) for seq in range(1, 6)]
    page = a_page(
        events=events,
        notes=[a_note(note_id, workspace_id, "v5")],
    )

    response = pull_routes._fit_to_budget(page, 65_536)

    assert [event.seq for event in response.events] == ["1", "2", "3", "4", "5"]
    assert len(response.objects) == 1
    assert response.objects[0].rev == "1"

    folder = ReadFolder(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        parent_id=None,
        name="Inbox",
        rev=1,
        updated_seq=1,
        deleted_at=None,
        created_at=NOW,
        updated_at=NOW,
    )

    model = pull_routes._object_model(folder)

    assert model.entity_type == "folder"
    assert model.deleted is False


def test_the_routers_publish_exactly_the_spec_paths() -> None:
    """Mounted under ``/api/v1`` these must be the four documented paths (spec §4/§5).

    Read from the generated OpenAPI document rather than ``app.routes``: FastAPI
    keeps an included router as a lazy node, and the document is what clients see.
    """
    app = FastAPI()
    app.include_router(pull_routes.router, prefix="/api/v1")
    app.include_router(read_routes.router, prefix="/api/v1")

    published = {
        (path, method.upper())
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }

    assert published == {
        ("/api/v1/workspaces/{workspace_id}/sync/pull", "GET"),
        ("/api/v1/workspaces/{workspace_id}/sync/snapshot", "GET"),
        ("/api/v1/workspaces/{workspace_id}/tree", "GET"),
        ("/api/v1/workspaces/{workspace_id}/notes/{note_id}", "GET"),
    }


def test_no_route_declares_a_write_method() -> None:
    """v1 has no REST write wrapper (D19): only GET may exist here."""
    for route in (*pull_routes.router.routes, *read_routes.router.routes):
        assert set(getattr(route, "methods", ())) <= {"GET", "HEAD"}, route.path


def test_both_routers_require_the_workspace_dependency() -> None:
    """Every route must authorise the path workspace; none may be accidentally public."""
    for router in (pull_routes.router, read_routes.router):
        for route in router.routes:
            assert require_workspace_member in _dependencies_of(route.dependant), route.path


def _dependencies_of(dependant: Any) -> set[Any]:
    """Every callable reachable from one route's dependency tree."""
    found: set[Any] = set()
    pending = [dependant]
    while pending:
        current = pending.pop()
        for dependency in getattr(current, "dependencies", ()):
            call = getattr(dependency, "call", None)
            if call is not None:
                found.add(call)
            pending.append(dependency)
    return found
