"""Pull, snapshot and read-only endpoints over HTTP against real PostgreSQL (spec §4-5).

The consistency proofs that need several connections at once live in
``test_http_concurrency.py``. What this module pins down is the *wire contract* of
the bootstrap reads, checked against committed rows on a separate connection:

* ``test_pull_since_is_exclusive_and_cursor_chains``: a page is a complete,
  contiguous window; ``since`` is exclusive; ``next_cursor`` always moves forward
  and chaining pages loses nothing and repeats nothing.
* ``test_pull_objects_are_deduplicated_and_lead_their_events``: one object per
  entity per page, at its **real** current ``rev``/``updated_seq`` (which may be
  later than the event that introduced it), and a delete keeps its event while the
  object becomes a tombstone.
* ``test_pull_cursor_bounds_and_floor``: ``since > last_seq`` is ``400
  cursor_ahead``; a cursor below the retention floor is ``410 cursor_expired`` with
  the floor and ``action=resync``; the boundary value is accepted.
* ``test_pull_limit_is_bounded_by_configuration``: ``limit`` over the ceiling is a
  400 rather than a silently clamped page.
* ``test_snapshot_has_no_partial_success``: the object cap and the *serialized*
  byte cap each refuse the whole snapshot - no ``objects`` array, no truncation,
  even when the stored bytes fit but JSON escaping does not (D15).
* ``test_snapshot_bootstraps_from_one_watermark``: ``as_of_seq`` is the workspace
  cursor at that view, and ``pull since=as_of_seq`` continues exactly there.
* ``test_a_legacy_oversize_body_is_refused_by_both_reads``: a body above the HTTP
  push cap (a row written before that cap existed) is 409 for the snapshot and 413
  for a pull, never a truncated page.
* ``test_tree_and_note_reads_hide_content_and_deletions``: the tree carries no
  ``body_md``, a deleted note is the same 404 as a missing one, and a note in
  another workspace is 404.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from pg_support.api_harness import (
    folder_create,
    note_create,
    note_delete,
    note_update,
    open_api,
    parse_decimal,
)

pytestmark = [pytest.mark.anyio, pytest.mark.api]


def _pull_body(response: Any) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    return dict(response.json())


def _seq(event: dict[str, Any]) -> int:
    return parse_decimal(event["seq"])


# --------------------------------------------------------------------------- #
# pull: cursors
# --------------------------------------------------------------------------- #
async def test_pull_since_is_exclusive_and_cursor_chains() -> None:
    """Every page is contiguous, ``since`` is exclusive, and chaining loses nothing."""
    async with open_api() as api:
        total = 7
        for index in range(total):
            response = await api.push([note_create(title=f"n{index}", body_md=f"body {index}")])
            assert response.status_code == 200, response.text

        last_seq = (await api.state()).last_seq
        assert last_seq == total

        collected: list[int] = []
        seen_objects: list[str] = []
        since = 0
        for _ in range(total + 1):
            page = _pull_body(await api.pull(since=since, limit=2))
            events = page["events"]
            seqs = [_seq(event) for event in events]

            # A page is a contiguous window that starts strictly after ``since``.
            assert seqs == list(range(since + 1, since + 1 + len(seqs))), (
                f"page since={since} is not contiguous: {seqs}"
            )
            assert all(_seq(event) > since for event in events), "since is exclusive"

            collected.extend(seqs)
            seen_objects.extend(str(obj["id"]) for obj in page["objects"])
            assert page["last_seq"] == str(last_seq)
            assert parse_decimal(page["min_retained_seq"]) >= 1

            if not page["has_more"]:
                # The terminal page's cursor is the last event it shipped.
                assert page["next_cursor"] == (str(seqs[-1]) if seqs else str(since))
                break
            assert page["next_cursor"] == str(seqs[-1]), "the cursor is the last shipped event"
            since = seqs[-1]
        else:  # pragma: no cover - the loop above always breaks
            pytest.fail("paging did not terminate")

        assert collected == list(range(1, total + 1)), "the chain must see every event once"
        assert len(seen_objects) == len(set(seen_objects)), "objects are deduplicated per page"

        # A cursor at the head answers an empty page whose cursor is itself.
        head = _pull_body(await api.pull(since=last_seq))
        assert head["events"] == [] and head["objects"] == []
        assert head["next_cursor"] == str(last_seq)
        assert head["has_more"] is False


async def test_pull_objects_are_deduplicated_and_lead_their_events() -> None:
    """One object per entity at its real current revision; deletes keep events."""
    async with open_api() as api:
        note_id = uuid.uuid4()
        assert (
            await api.push([note_create(note_id, title="v1", body_md="one")])
        ).status_code == 200
        assert (
            await api.push([note_update(note_id, base_rev=1, title="v2", body_md="two")])
        ).status_code == 200

        page = _pull_body(await api.pull(since=0, limit=100))

        assert [_seq(event) for event in page["events"]] == [1, 2], "history is never compacted"
        notes = [obj for obj in page["objects"] if obj["entity_type"] == "note"]
        assert len(notes) == 1, "the same entity appears once per page"
        note = notes[0]
        assert note["id"] == str(note_id)
        # The object leads its events: rev 2 belongs to seq 2, the second event.
        assert parse_decimal(note["rev"]) == 2
        assert parse_decimal(note["updated_seq"]) == 2
        assert note["body_md"] == "two"
        assert note["deleted"] is False

        # Delete: a new event, and the object becomes a tombstone that keeps its body.
        assert (await api.push([note_delete(note_id, base_rev=2)])).status_code == 200

        page = _pull_body(await api.pull(since=2, limit=100))
        assert [_seq(event) for event in page["events"]] == [3]
        assert page["events"][0]["op"] == "delete"
        notes = [obj for obj in page["objects"] if obj["entity_type"] == "note"]
        assert len(notes) == 1
        assert notes[0]["deleted"] is True
        assert notes[0]["deleted_at"] is not None
        assert notes[0]["body_md"] == "two", "a tombstone keeps the last content"


async def test_pull_cursor_bounds_and_floor() -> None:
    """``cursor_ahead`` is 400; below the floor is 410 with the floor and resync."""
    async with open_api() as api:
        for _ in range(4):
            assert (await api.push([note_create()])).status_code == 200

        ahead = await api.pull(since=99)
        assert ahead.status_code == 400, ahead.text
        assert ahead.json()["error"]["code"] == "cursor_ahead"
        assert ahead.json()["error"]["details"]["last_seq"] == "4"

        # Malformed cursors are contract errors, never coerced.
        for bad in ("-1", "007", "1e3", " 1", "abc", ""):
            broken = await api.pull(params={"since": bad})
            assert broken.status_code == 400, (bad, broken.text)
            assert broken.json()["error"]["code"] == "invalid_request"

        # Advance the retention floor (the schema allows it: floor - 1 <= last_seq).
        connection = await api.db.conn()
        await connection.execute(
            "UPDATE workspaces SET min_retained_seq = 3 WHERE id = $1", api.workspace_id
        )
        try:
            expired = await api.pull(since=1)
            assert expired.status_code == 410, expired.text
            error = expired.json()["error"]
            assert error["code"] == "cursor_expired"
            assert error["details"]["min_retained_seq"] == "3"
            assert error["details"]["action"] == "resync"

            # ``since == floor - 1`` is the boundary: still readable.
            boundary = _pull_body(await api.pull(since=2))
            assert [_seq(event) for event in boundary["events"]] == [3, 4]
            assert boundary["min_retained_seq"] == "3"
        finally:
            await connection.execute(
                "UPDATE workspaces SET min_retained_seq = 1 WHERE id = $1", api.workspace_id
            )


async def test_pull_limit_is_bounded_by_configuration() -> None:
    """``limit`` above the ceiling is a 400, never a silently clamped page."""
    async with open_api(overrides={"sync_pull_default_limit": 2, "sync_pull_max_limit": 3}) as api:
        for _ in range(4):
            assert (await api.push([note_create()])).status_code == 200

        too_large = await api.pull(since=0, limit=4)
        assert too_large.status_code == 400, too_large.text
        assert too_large.json()["error"]["code"] == "invalid_request"

        accepted = _pull_body(await api.pull(since=0, limit=3))
        assert len(accepted["events"]) == 3
        assert accepted["has_more"] is True
        assert accepted["next_cursor"] == "3"

        zero = await api.pull(since=0, limit=0)
        assert zero.status_code == 400, zero.text


# --------------------------------------------------------------------------- #
# snapshot
# --------------------------------------------------------------------------- #
async def test_snapshot_has_no_partial_success() -> None:
    """Both caps refuse the entire snapshot: no ``objects``, no truncation (D15).

    The byte case is the interesting one: the *stored* bytes fit the budget, but
    the JSON-escaped form does not (a control character is one byte in PostgreSQL
    and six on the wire as ``\\u0001``). A handler that measured the Python string,
    or that serialized first and capped afterwards, would answer a truncated
    ``objects`` array - which a client cannot distinguish from a smaller workspace.
    """
    async with open_api(
        overrides={
            # 3 objects, and a small byte budget: the object case trips the count,
            # the escaped-body case trips the *serialized* size.
            "sync_snapshot_max_objects": 3,
            "sync_snapshot_max_bytes": 2_000,
        }
    ) as api:
        # -- object cap ----------------------------------------------------- #
        for _ in range(4):
            assert (await api.push([note_create(title="x", body_md="short")])).status_code == 200

        over_objects = await api.snapshot()
        assert over_objects.status_code == 409, over_objects.text
        assert over_objects.json()["error"]["code"] == "snapshot_too_large"
        assert "objects" not in over_objects.text, "a refused snapshot has no object list"

        # Drop one note so the count fits again but the bytes are plain ASCII...
        state = await api.state()
        assert state.notes == 4

    async with open_api(
        overrides={"sync_snapshot_max_objects": 10, "sync_snapshot_max_bytes": 2_000}
    ) as api:
        # ... and a control character inflates 1 stored byte into 6 wire bytes.
        escaped_body = "\x01" * 1_000
        create = await api.push([note_create(title="t", body_md=escaped_body)])
        assert create.status_code == 200, create.text

        refused = await api.snapshot()
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "snapshot_too_large"
        assert "objects" not in refused.text

        # The same workspace is fully readable by pull: refusing a snapshot is not
        # a data-loss report, it is a budget report.
        page = _pull_body(await api.pull(since=0))
        assert page["events"]
        assert page["objects"][0]["body_md"] == escaped_body

    async with open_api(overrides={"sync_snapshot_max_objects": 2}) as api:
        for _ in range(3):
            assert (await api.push([folder_create(name="f")])).status_code == 200
        counted = await api.snapshot()
        assert counted.status_code == 409
        assert counted.json()["error"]["code"] == "snapshot_too_large"


async def test_snapshot_bootstraps_from_one_watermark() -> None:
    """``as_of_seq`` is the cursor to continue from, and every active object is there."""
    async with open_api() as api:
        folder_id, note_id = uuid.uuid4(), uuid.uuid4()
        assert (await api.push([folder_create(folder_id, name="Docs")])).status_code == 200
        assert (
            await api.push(
                [note_create(note_id, title="Note", body_md="hello", folder_id=folder_id)]
            )
        ).status_code == 200
        deleted_id = uuid.uuid4()
        created = await api.push([note_create(deleted_id, title="gone", body_md="x")])
        assert created.status_code == 200
        assert (await api.push([note_delete(deleted_id, base_rev=1)])).status_code == 200

        response = await api.snapshot()
        assert response.status_code == 200, response.text
        body = response.json()

        state = await api.state()
        assert body["as_of_seq"] == str(state.last_seq)
        assert body["min_retained_seq"] == "1"

        by_id = {obj["id"]: obj for obj in body["objects"]}
        assert set(by_id) == {str(folder_id), str(note_id)}, "deleted objects are excluded"
        assert by_id[str(note_id)]["body_md"] == "hello"
        assert by_id[str(note_id)]["folder_id"] == str(folder_id)
        assert by_id[str(folder_id)]["name"] == "Docs"

        # Continuing with pull at that watermark yields exactly the later changes.
        later = await api.push([note_update(note_id, base_rev=1, title="Note", body_md="bye")])
        assert later.status_code == 200, later.text
        page = _pull_body(await api.pull(since=body["as_of_seq"]))
        assert [_seq(event) for event in page["events"]] == [state.last_seq + 1]
        assert page["objects"][0]["body_md"] == "bye"

        # Pagination parameters are not part of a snapshot: asking for page 2 is a
        # contract error, not page 1 again.
        for params in ({"limit": 10}, {"cursor": "1"}, {"offset": 0}):
            rejected = await api.snapshot(params=params)
            assert rejected.status_code == 400, (params, rejected.text)
            assert rejected.json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------- #
# tree / note
# --------------------------------------------------------------------------- #
async def test_tree_and_note_reads_hide_content_and_deletions() -> None:
    """The tree has no bodies; a deleted or foreign note is the same 404."""
    async with open_api() as api:
        other = await api.app.seed_second_account()
        folder_id, note_id = uuid.uuid4(), uuid.uuid4()
        secret_body = "unique body marker 8f3a"

        assert (await api.push([folder_create(folder_id, name="Docs")])).status_code == 200
        assert (
            await api.push(
                [note_create(note_id, title="Visible", body_md=secret_body, folder_id=folder_id)]
            )
        ).status_code == 200

        tree = await api.tree()
        assert tree.status_code == 200, tree.text
        body = tree.json()
        assert [item["id"] for item in body["folders"]] == [str(folder_id)]
        assert [item["id"] for item in body["notes"]] == [str(note_id)]
        assert body["notes"][0]["title"] == "Visible"
        assert secret_body not in tree.text, "the tree must not carry note bodies"
        assert "body_md" not in tree.text

        note = await api.note(note_id)
        assert note.status_code == 200, note.text
        assert note.json()["body_md"] == secret_body
        assert note.json()["rev"] == "1"
        assert note.headers.get("cache-control") == "no-store"

        # A note in another workspace: 404, regardless of membership.
        foreign = await api.note(note_id, workspace_id=other.workspace_id)
        assert foreign.status_code == 404
        assert foreign.json()["error"]["code"] == "not_found"

        # After a delete the same read is 404, indistinguishable from unknown.
        assert (await api.push([note_delete(note_id, base_rev=1)])).status_code == 200
        deleted = await api.note(note_id)
        assert deleted.status_code == 404
        assert deleted.json()["error"]["code"] == "not_found"
        unknown = await api.note(uuid.uuid4())
        assert unknown.status_code == 404
        # Same code and message: a client cannot tell "deleted" from "never existed".
        assert unknown.json()["error"]["code"] == deleted.json()["error"]["code"]
        assert unknown.json()["error"]["message"] == deleted.json()["error"]["message"]

        # The tree drops the deleted note but the folder remains.
        tree = await api.tree()
        assert tree.status_code == 200
        assert tree.json()["notes"] == []
        assert [item["id"] for item in tree.json()["folders"]] == [str(folder_id)]


async def test_reads_require_membership_and_a_token() -> None:
    """Every read path answers 401 without a token and 404 for a foreign workspace."""
    async with open_api() as api:
        other = await api.app.seed_second_account()
        anonymous = await api.client.get(api.url(f"/workspaces/{api.workspace_id}/tree"))
        assert anonymous.status_code == 401
        assert anonymous.json()["error"]["code"] == "unauthenticated"

        for path in (
            f"/workspaces/{other.workspace_id}/tree",
            f"/workspaces/{other.workspace_id}/sync/pull",
            f"/workspaces/{other.workspace_id}/sync/snapshot",
            f"/workspaces/{uuid.uuid4()}/tree",
        ):
            response = await api.client.get(api.url(path), headers=api.headers())
            assert response.status_code == 404, (path, response.text)
            assert response.json()["error"]["code"] == "not_found"


async def test_pull_response_never_contains_a_workspace_member_list() -> None:
    """Pull describes objects and events only: no auth or membership surface."""
    async with open_api() as api:
        assert (await api.push([note_create()])).status_code == 200
        response = await api.pull(since=0)
        body = response.json()
        assert set(body) == {
            "events",
            "objects",
            "next_cursor",
            "has_more",
            "last_seq",
            "min_retained_seq",
        }
        assert "token" not in response.text.lower()


# --------------------------------------------------------------------------- #
# a row written before the HTTP push cap existed
# --------------------------------------------------------------------------- #
async def test_a_legacy_oversize_body_is_refused_by_both_reads() -> None:
    """A 20 MiB body is 409 for the snapshot and 413 for a pull - never truncated.

    ``SYNC_NOTE_MAX_BYTES`` (256 KiB) and the 2 MiB push-body budget bound what a
    *client* may write, so this state cannot be produced through ``/sync/push``. It
    is what a row written before those caps existed looks like, and the read side
    has to survive it: the note is created normally, then only its ``body_md`` is
    replaced by direct SQL - ``rev`` and ``updated_seq`` keep naming the original
    event, so the deferred object FK still holds and the row stays a legal object.

    Both reads must refuse **before** materialising anything: the snapshot's
    ``octet_length`` preflight trips the 16 MiB budget (409), and pull's prefix
    estimator cannot fit the first event plus its object into 4 MiB (413). The
    assertions on the response bodies are about absence: a page that shipped a
    shortened body, or an ``objects`` array with the note in it, would be the
    silent data loss both guards exist to prevent.
    """
    huge = 20 * 1024 * 1024  # above both the 2 MiB push cap and the 16 MiB snapshot cap
    async with open_api() as api:
        note_id = uuid.uuid4()
        assert (await api.push([note_create(note_id, title="legacy")])).status_code == 200

        connection = await api.db.conn()
        # Written server-side: 20 MiB of one repeated byte is compressible, which is
        # exactly the case a physical-size check would have understated.
        await connection.execute(
            "UPDATE notes SET body_md = repeat('a', CAST($2 AS INTEGER))"
            " WHERE workspace_id = $1 AND id = $3",
            api.workspace_id,
            huge,
            note_id,
        )

        # The row itself is intact: nothing in the refusal path truncated it.
        stored = await connection.fetchrow(
            "SELECT rev, updated_seq, octet_length(body_md) AS size"
            " FROM notes WHERE workspace_id = $1 AND id = $2",
            api.workspace_id,
            note_id,
        )
        assert stored is not None
        assert stored["size"] == huge
        assert stored["rev"] == 1 and stored["updated_seq"] == 1, (
            "the original event still names it"
        )

        snapshot = await api.snapshot()
        assert snapshot.status_code == 409, snapshot.text
        assert snapshot.json()["error"]["code"] == "snapshot_too_large"
        assert "objects" not in snapshot.text, "a refused snapshot has no object list"
        assert len(snapshot.content) < 4_096, "the refusal must not carry the body"

        page = await api.pull(since=0)
        assert page.status_code == 413, page.text
        assert page.json()["error"]["code"] == "payload_too_large"
        assert "objects" not in page.text, "no object may describe an event the client did not get"
        assert len(page.content) < 4_096, "the refusal must not carry the body"

        # Committed rows are unchanged by two refused reads (this is a read tier).
        state = await api.state()
        assert state.last_seq == 1 and state.event_seqs == (1,)
