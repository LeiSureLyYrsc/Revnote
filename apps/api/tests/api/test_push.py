"""``POST .../sync/push`` over HTTP against real PostgreSQL (spec 搂3, D9-D12, D17).

What this module proves, and why each case needs the real server
----------------------------------------------------------------
A push result is a claim about *committed* rows, so every assertion here is
cross-checked on a separate connection (:class:`~pg_support.api_harness.DatabaseProbe`)
rather than taken from the response body:

* ``test_http_push_six_statuses_are_wire_contract``: the statuses reachable from
  ordinary input are produced by real kernel decisions, in one batch, in order.
* ``test_http_push_duplicate_op_id_executes_nothing``: D12 is about the *whole*
  batch, and only the database can show that the ops before the duplicate did not
  run.
* ``test_http_push_caps_reject_the_whole_batch`` / ``..._raw_json_budget...``: the
  1-50 op, 256 KiB body and 2 MiB raw-JSON budgets each refuse everything, and the
  boundaries are accepted.
* ``test_http_push_lock_timeout_stops_the_batch``: a real ``55P03`` from a held
  workspace lock -> ``rejected``/``write_busy``, the rest ``not_attempted``, and the
  same ops still apply afterwards.
* ``test_http_push_unknown_is_retried_by_the_same_op_id``: a lost ``COMMIT``
  acknowledgement is not a rollback. The seam is ``AsyncSession.commit`` - the
  transaction, the rows and the commit are real; only the ack is dropped, exactly as
  the committed kernel test does it. The retry with the same ``op_id`` reports
  ``replayed`` and the object exists once.
* ``test_id_conflict_origin_hidden``: a folder id already owned by *another*
  workspace and an id taken by the same workspace produce the same status, code and
  error body (P2 #3).

Not re-proven here: the kernel's own invariants (dense seq under concurrency, CAS,
ledger race) live in ``tests/integration/kernel``; ``test_http_concurrency.py``
covers the multi-connection version of the dense-sequence and visibility rules over
this transport.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pg_support.api_harness import (
    folder_create,
    folder_delete,
    folder_move,
    note_create,
    note_delete,
    note_update,
    open_api,
)

pytestmark = [pytest.mark.anyio, pytest.mark.api]


def _results(response: Any) -> list[dict[str, Any]]:
    assert response.status_code == 200, response.text
    return list(response.json()["results"])


def _statuses(response: Any) -> list[str]:
    return [item["status"] for item in _results(response)]


#: The exact keys of one error object (spec §1: one shape everywhere).
ERROR_KEYS = {"code", "message", "request_id", "retryable", "details"}


def _assert_error_object(response: Any, item: dict[str, Any]) -> dict[str, Any]:
    """A failed op carries the full error object, never a bare code.

    This is the wire guarantee a client parses against: the same
    ``{code, message, request_id, retryable, details}`` as the top-level envelope, so
    one decoder handles every failure. ``request_id`` must equal the response's own
    ``X-Request-ID``, which is what makes a per-op failure correlatable.
    """
    assert "code" not in item, f"the result still carries a bare code: {item}"
    error = item.get("error")
    assert isinstance(error, dict), f"a failed op must carry an error object, got {error!r}"
    assert set(error) == ERROR_KEYS, f"error keys {sorted(error)} != {sorted(ERROR_KEYS)}"
    assert error["request_id"] == response.headers["x-request-id"], (
        "the per-op request_id must be the request's own"
    )
    assert isinstance(error["details"], dict)
    assert error["message"], "the fixed message must be present"
    assert isinstance(error["retryable"], bool)
    return error


# --------------------------------------------------------------------------- #
# statuses
# --------------------------------------------------------------------------- #
async def test_http_push_six_statuses_are_wire_contract() -> None:
    """Each status reachable from ordinary input, produced by a real decision.

    ``unknown`` and ``not_attempted`` need an injected failure and have their own
    cases; here a create, its replay, a stale update and a folder cycle show the
    four that a well-formed batch can reach, in request order, with later ops still
    applying after a conflict.
    """
    seen: set[str] = set()
    async with open_api() as api:
        folder_id, note_id = uuid.uuid4(), uuid.uuid4()
        create_folder = folder_create(folder_id, name="Docs")
        create_note = note_create(note_id, title="One", body_md="first")

        first = await api.push([create_folder, create_note])
        assert _statuses(first) == ["applied", "applied"]
        results = _results(first)
        assert results[0]["seq"] == "1" and results[0]["rev"] == "1"
        assert results[1]["seq"] == "2" and results[1]["rev"] == "1"
        assert results[0]["error"] is None
        seen |= set(_statuses(first))

        # Replay the exact same folder op: same op_id, same payload.
        replay = await api.push([create_folder])
        assert _statuses(replay) == ["replayed"]
        assert _results(replay)[0]["seq"] == "1", "a replay returns the recorded position"
        seen |= set(_statuses(replay))

        # Stale base_rev -> conflict, and the following op still runs.
        mixed = await api.push([note_update(note_id, base_rev=9), folder_create(name="After")])
        assert _statuses(mixed) == ["conflict", "applied"]
        conflict = _results(mixed)[0]
        assert conflict["seq"] is None and conflict["rev"] is None
        assert conflict["error"]["code"] == "rev_conflict"
        assert conflict["error"]["retryable"] is False
        seen |= set(_statuses(mixed))

        # An indirect cycle -> rejected by the kernel, and the batch continues.
        # (A direct self-parent is already a 400 at the schema layer, so the only way
        # to reach this kernel rule is A under B, then B under A.)
        parent = folder_create(name="Parent").copy()
        child = folder_create(name="Child", parent_id=uuid.UUID(parent["entity_id"]))
        assert _statuses(await api.push([parent, child])) == ["applied", "applied"]
        cycle = folder_move(
            uuid.UUID(parent["entity_id"]), base_rev=1, parent_id=uuid.UUID(child["entity_id"])
        )
        final = await api.push([cycle, folder_create(name="Still running")])
        assert _statuses(final) == ["rejected", "applied"]
        assert _results(final)[0]["error"]["code"] == "folder_cycle"
        seen |= set(_statuses(final))

        state = await api.state()
        assert state.dense, state
        assert state.ledger_rows == len(state.event_seqs)

        assert seen == {"applied", "replayed", "conflict", "rejected"}
        assert seen <= {
            "applied",
            "replayed",
            "conflict",
            "rejected",
            "unknown",
            "not_attempted",
        }


async def test_http_push_replay_and_conflicts_continue_the_batch() -> None:
    """A replay and a tombstone conflict keep their place; later ops still apply."""
    async with open_api() as api:
        note_id = uuid.uuid4()
        create = note_create(note_id, title="Draft", body_md="v1")
        assert _statuses(await api.push([create])) == ["applied"]
        assert _statuses(await api.push([note_update(note_id, base_rev=1, body_md="v2")])) == [
            "applied"
        ]
        delete = note_delete(note_id, base_rev=2)
        assert _statuses(await api.push([delete])) == ["applied"]

        batch = await api.push(
            [
                delete,  # same op_id + payload: the recorded result
                note_delete(note_id, base_rev=2),  # a new op_id: the row is gone
                note_create(title="Survivor", body_md="ok"),
            ]
        )

        assert _statuses(batch) == ["replayed", "conflict", "applied"]
        assert _results(batch)[1]["error"]["code"] == "entity_deleted"
        assert (await api.state()).dense


# --------------------------------------------------------------------------- #
# duplicate op_id
# --------------------------------------------------------------------------- #
async def test_http_push_duplicate_op_id_executes_nothing() -> None:
    """D12: a repeated ``op_id`` rejects the batch and **no** op runs.

    The duplicate sits at the *end*, so a server that validated lazily would have
    committed the earlier ops before noticing. Asserting on committed rows is the
    only way to prove "zero execution" rather than "zero reported".
    """
    async with open_api() as api:
        shared = str(uuid.uuid4())
        first = note_create(title="First", body_md="a")
        second = note_create(title="Second", body_md="b")
        first["op_id"] = shared
        second["op_id"] = shared

        response = await api.push([first, second])

        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_request"

        state = await api.state()
        assert state.last_seq == 0
        assert state.event_seqs == ()
        assert state.folders == 0 and state.notes == 0
        assert state.ledger_rows == 0


async def test_http_push_duplicate_op_id_is_normalised_before_comparison() -> None:
    """Two spellings of one UUID are one ``op_id`` (both parsed to ``UUID``)."""
    async with open_api() as api:
        value = uuid.uuid4()
        upper, lower = str(value).upper(), str(value)
        assert upper != lower, "the spellings must differ at the byte level"

        first = note_create(title="A", body_md="a")
        second = note_create(title="B", body_md="b")
        first["op_id"], second["op_id"] = upper, lower

        response = await api.push([first, second])

        assert response.status_code == 400, response.text
        assert (await api.state()).last_seq == 0


# --------------------------------------------------------------------------- #
# caps
# --------------------------------------------------------------------------- #
async def test_http_push_caps_reject_the_whole_batch() -> None:
    """Op count and note-body bytes each reject everything; the boundaries apply."""
    async with open_api() as api:
        over = [note_create() for _ in range(51)]
        response = await api.push(over)
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_request"

        empty = await api.push([])
        assert empty.status_code == 400, empty.text

        oversized = [note_create(title="big", body_md="x" * (256 * 1024 + 1))]
        response = await api.push(oversized)
        assert response.status_code == 413, response.text
        assert response.json()["error"]["code"] == "payload_too_large"
        # The failing op is identified by index; the body itself is never echoed.
        assert response.json()["error"]["details"]["op_index"] == 0
        assert "xxxx" not in response.text

        state = await api.state()
        assert state.last_seq == 0 and state.ledger_rows == 0

        # Exactly at the per-object cap is accepted (it is a byte limit, not "<").
        boundary = note_create(title="edge", body_md="y" * (256 * 1024))
        assert _statuses(await api.push([boundary])) == ["applied"]
        assert (await api.state()).dense

        # Exactly 50 ops is accepted.
        fifty = [note_create() for _ in range(50)]
        assert _statuses(await api.push(fifty)) == ["applied"] * 50
        assert (await api.state()).dense


async def test_http_push_raw_json_budget_is_two_mib() -> None:
    """``SYNC_PUSH_MAX_BYTES`` is measured on the raw body, before validation."""
    async with open_api() as api:
        # Every op is individually legal; the *batch* exceeds 2 MiB.
        ops = [note_create(title=f"n{index}", body_md="z" * (250 * 1024)) for index in range(9)]
        response = await api.push(ops)

        assert response.status_code == 413, response.text
        assert response.json()["error"]["code"] == "payload_too_large"
        state = await api.state()
        assert state.last_seq == 0 and state.notes == 0


# --------------------------------------------------------------------------- #
# transient stop: write_busy (real 55P03)
# --------------------------------------------------------------------------- #
async def test_http_push_lock_timeout_stops_the_batch() -> None:
    """A held workspace lock -> real ``55P03`` -> ``write_busy`` and a clean stop.

    The lock is the kernel's own (``workspaces`` ``FOR UPDATE``) held by another
    connection, and ``DB_LOCK_TIMEOUT_MS`` is injected small, so the server really
    raises. Everything after the busy op is ``not_attempted`` with the same reason,
    nothing of it is committed, and the identical ops apply once the lock is gone.
    """
    async with open_api(overrides={"db_lock_timeout_ms": 150}) as api:
        connection = await api.db.conn()
        first = note_create(title="blocked", body_md="body")
        rest = [note_create(title="later", body_md="body"), folder_create(name="later")]

        started = time.monotonic()
        await connection.execute("BEGIN")
        try:
            await connection.fetchrow(
                "SELECT id FROM workspaces WHERE id = $1 FOR UPDATE", api.workspace_id
            )
            response = await api.push([first, *rest])
            elapsed = time.monotonic() - started
        finally:
            await connection.execute("ROLLBACK")

        # The injected 150 ms budget reached the server: nowhere near the 5 s default.
        assert elapsed < 2.0, f"the lock wait was not bounded: {elapsed:.2f}s"

        assert response.status_code == 200, response.text
        results = _results(response)
        assert [item["status"] for item in results] == [
            "rejected",
            "not_attempted",
            "not_attempted",
        ]
        assert results[0]["error"]["code"] == "write_busy"
        assert results[0]["error"]["retryable"] is True
        # The stop reason is named on every trailing op, never a bare status.
        assert results[1]["error"]["code"] == results[2]["error"]["code"] == "write_busy"

        state = await api.state()
        assert state.last_seq == 0, "a busy batch must not allocate a cursor"
        assert state.event_seqs == () and state.ledger_rows == 0

        # Same ops, same op_ids: they apply now (the failure wrote nothing).
        assert _statuses(await api.push([first, *rest])) == ["applied", "applied", "applied"]
        assert (await api.state()).dense


# --------------------------------------------------------------------------- #
# transient stop: unknown outcome
# --------------------------------------------------------------------------- #
async def test_http_push_unknown_is_retried_by_the_same_op_id() -> None:
    """A lost ``COMMIT`` ack reports ``unknown``, and the retry replays it.

    The seam is ``AsyncSession.commit``: the transaction, the rows and the
    ``COMMIT`` are real; only the acknowledgement is dropped (the same seam the
    phase 2 durability test uses). That is the one state in which "the outcome is
    unknowable" is true rather than simulated.

    The client's contract is "retry the identical op": the retry must answer
    ``replayed`` with the *first* position, and the object must exist exactly once.
    """
    async with open_api() as api:
        ops = [
            note_create(title="first", body_md="a"),
            note_create(title="second", body_md="b"),
            note_create(title="third", body_md="c"),
            note_create(title="fourth", body_md="d"),
        ]

        real_commit = AsyncSession.commit
        commits = 0

        async def commit_then_lose_ack(session: AsyncSession) -> None:
            nonlocal commits
            await real_commit(session)
            commits += 1
            if commits == 3:
                raise ConnectionResetError("acknowledgement lost")

        AsyncSession.commit = commit_then_lose_ack  # type: ignore[method-assign]
        try:
            response = await api.push(ops)
        finally:
            AsyncSession.commit = real_commit  # type: ignore[method-assign]

        # Two confirmed ops and one uncertain-but-durable op, then a stopped suffix.
        assert response.status_code == 200, response.text
        results = _results(response)
        assert [item["status"] for item in results] == [
            "applied",
            "applied",
            "unknown",
            "not_attempted",
        ]
        assert results[2]["error"]["code"] == "outcome_unknown"
        assert results[2]["error"]["retryable"] is True
        assert results[3]["error"]["code"] == "outcome_unknown"

        # The write is durable even though the caller never learned its position.
        state = await api.state()
        assert state.event_seqs == (1, 2, 3), state
        assert state.notes == 3
        assert state.ledger_rows == 3

        # Retry the identical batch: every durable prefix op replays, suffix applies.
        retried = await api.push(ops)
        assert _statuses(retried) == ["replayed", "replayed", "replayed", "applied"]
        assert _results(retried)[0]["seq"] == "1"

        state = await api.state()
        assert state.event_seqs == (1, 2, 3, 4)
        assert state.notes == 4
        assert state.ledger_rows == 4


# --------------------------------------------------------------------------- #
# id conflict origin hidden
# --------------------------------------------------------------------------- #
async def test_id_conflict_origin_hidden() -> None:
    """Every id-collision origin answers the same shape (P2 #3).

    Three genuinely different causes:

    * a folder id already owned by **another workspace** (``EntityIdConflict``),
    * a folder id already owned by **this** workspace (``EntityIdConflict``),
    * an ``op_id`` reused with a **different payload** (``OpIdConflict``).

    The first would disclose that a foreign row exists, the second would say "that
    id is yours", the third would say "that op id is taken" - so all three must be
    indistinguishable on the wire: same status, code, fixed message, empty details
    and no position. Internal logs may still tell them apart.
    """
    async with open_api() as api:
        other = await api.app.seed_second_account()
        other_token = await api.login(other)

        foreign_folder = uuid.uuid4()
        foreign_seed = await api.push(
            [folder_create(foreign_folder, name="theirs")],
            workspace_id=other.workspace_id,
            token=other_token,
        )
        assert _statuses(foreign_seed) == ["applied"]

        own_folder = uuid.uuid4()
        assert _statuses(await api.push([folder_create(own_folder, name="mine")])) == ["applied"]

        # An op_id already recorded here, re-sent with a different payload.
        reused_op = folder_create(name="first shape")
        assert _statuses(await api.push([reused_op])) == ["applied"]
        reused = folder_create(name="different shape")
        reused["op_id"] = reused_op["op_id"]

        foreign_id = await api.push([folder_create(foreign_folder, name="mine")])
        own_id = await api.push([folder_create(own_folder, name="again")])
        op_id = await api.push([reused])

        bodies = [_results(response)[0] for response in (foreign_id, own_id, op_id)]
        for body in bodies:
            assert body["status"] == "conflict"
            assert body["error"]["code"] == "conflict"
            assert body["error"]["retryable"] is False
            assert body["error"]["details"] == {}
            assert body["seq"] is None and body["rev"] is None

        # Identical apart from the echoed op_id and the request id.
        shapes = {
            (body["error"]["code"], body["error"]["message"], json.dumps(body["error"]["details"]))
            for body in bodies
        }
        assert len(shapes) == 1, shapes

        # No foreign id, no workspace hint, no driver text in any of them.
        for response in (foreign_id, own_id, op_id):
            assert str(foreign_folder) not in response.text
            assert str(other.workspace_id) not in response.text
            assert "duplicate key" not in response.text.lower()

        # Still one folder each: nothing was written by the failed attempts.
        assert (await api.state()).folders == 2
        assert (await api.state(other.workspace_id)).folders == 1
        assert (await api.state()).dense

        # A same-op_id create in two *different* workspaces is legitimate: the key
        # is per workspace, which is why the conflict above was not about op_id.
        shared_op = str(uuid.uuid4())
        mine = folder_create(name="mine-2")
        theirs = folder_create(name="theirs-2")
        mine["op_id"] = theirs["op_id"] = shared_op
        assert _statuses(await api.push([mine])) == ["applied"]
        assert _statuses(
            await api.push([theirs], workspace_id=other.workspace_id, token=other_token)
        ) == ["applied"]


# --------------------------------------------------------------------------- #
# structure: malformed ops never apply
# --------------------------------------------------------------------------- #
async def test_http_push_per_op_failure_is_an_error_object_not_a_code() -> None:
    """Every failure status carries the shared error object (spec §1/§3).

    A client must be able to decode one shape for the whole API, so a per-op failure
    is ``{code, message, request_id, retryable, details}`` - not a bare ``code``
    string. The ``request_id`` is the request's own ``X-Request-ID``, which is what
    lets a caller correlate the failing op with its server-side diagnostics.

    Three different failure classes are checked, because each is rendered by a
    different code path (kernel error -> conflict, kernel error -> rejected, and a
    per-op ``not_found``).
    """
    async with open_api() as api:
        note_id = uuid.uuid4()
        assert _statuses(await api.push([note_create(note_id, title="v1", body_md="x")])) == [
            "applied"
        ]

        # 1. stale revision -> conflict
        stale = await api.push([note_update(note_id, base_rev=9, body_md="stale")])
        # 2. unknown entity -> rejected (not_found is a per-op code)
        unknown_entity = await api.push(
            [
                {
                    "op_id": str(uuid.uuid4()),
                    "entity_type": "note",
                    "op": "update",
                    "entity_id": str(uuid.uuid4()),
                    "base_rev": "1",
                    "payload": {"title": "t", "body_md": "x"},
                }
            ]
        )
        # 3. a business rejection that continues the batch
        cycle_parent = uuid.uuid4()
        cycle_child = uuid.uuid4()
        assert _statuses(
            await api.push(
                [
                    folder_create(cycle_parent, name="parent"),
                    folder_create(cycle_child, name="child", parent_id=cycle_parent),
                ]
            )
        ) == ["applied", "applied"]
        cycle = await api.push([folder_move(cycle_parent, base_rev=1, parent_id=cycle_child)])

        for response in (stale, unknown_entity, cycle):
            item = _results(response)[0]
            assert item["status"] in {"conflict", "rejected"}
            assert item["seq"] is None and item["rev"] is None
            error = _assert_error_object(response, item)
            assert error["retryable"] is False

        # A successful result carries no error at all - and therefore no code field.
        success = await api.push([folder_create(name="fine")])
        applied = _results(success)[0]
        assert applied["status"] == "applied"
        assert applied["error"] is None
        assert "code" not in applied


async def test_http_push_per_op_membership_loss_is_not_found() -> None:
    """An ``AuthError`` from the per-op membership probe is a uniform 404.

    D5 re-checks membership before **every** op, so the row can disappear mid-batch.
    The translation must happen at the probe seam: an uncaught
    :class:`~revnote_api.services.auth_rules.AuthError` would surface as a 500, and a
    distinct code would disclose that the workspace *exists* while the caller is not
    a member (D20 wants one ``not_found`` for both).

    The removal happens between op 1 and op 2, which is exactly the window the
    per-op re-check exists for: op 1 committed, op 2 is then refused.
    """
    async with open_api() as api:
        first = folder_create(name="before removal")
        second = folder_create(name="after removal")

        connection = await api.db.conn()
        await connection.execute(
            "DELETE FROM workspace_members WHERE workspace_id = $1 AND user_id = $2",
            api.workspace_id,
            api.user_id,
        )

        response = await api.push([first, second])

        assert response.status_code == 404, response.text
        error = response.json()["error"]
        assert error["code"] == "not_found"
        assert set(error) == ERROR_KEYS

        # Nothing ran: the probe refused before the first op.
        state = await api.state()
        assert state.last_seq == 0
        assert state.folders == 0

        # Restoring membership makes the very same batch work, so the 404 really was
        # the membership row and not something about the ops.
        await connection.execute(
            "INSERT INTO workspace_members (workspace_id, user_id, role) VALUES ($1, $2, 'owner')",
            api.workspace_id,
            api.user_id,
        )
        assert _statuses(await api.push([first, second])) == ["applied", "applied"]
        assert (await api.state()).dense


async def test_http_push_repr_never_leaks_note_content() -> None:
    """The wire models' ``repr``/``str`` show shapes, never values (plan §11.1).

    Pydantic renders field values by default, and both a log line and a
    ``ValidationError`` formatter reach for ``str()``. A note body in a repr is
    therefore a leak with no attacker required: one ``logger.debug(request)`` writes
    user content to disk. The models render ``Type(field: Type, ...)`` instead, and
    the nesting is the risk: the wrapper holds the ops, which hold the payload.
    """
    secret_body = "repr-sentinel-body-a91f"
    secret_title = "repr-sentinel-title-b72e"

    async with open_api() as api:
        note_id = uuid.uuid4()
        op = note_create(note_id, title=secret_title, body_md=secret_body)

        from revnote_api.api.schemas.sync_push import PushRequest

        request = PushRequest.model_validate({"ops": [op]})
        rendered = [
            repr(request),
            str(request),
            repr(request.ops[0]),
            str(request.ops[0]),
            repr(request.ops[0].payload),
            str(request.ops[0].payload),
            repr(request.ops),
        ]
        for text in rendered:
            assert secret_body not in text, f"a body reached a repr: {text[:120]}"
            assert secret_title not in text, f"a title reached a repr: {text[:120]}"

        # The shape is still useful for debugging: field names and types survive.
        assert "ops" in repr(request)
        assert "body_md" in repr(request.ops[0].payload)

        # The push itself works, so this is the real request model and not a copy.
        assert _statuses(await api.push([op])) == ["applied"]
        stored = await api.db.note_row(api.workspace_id, note_id)
        assert stored["body_md"] == secret_body


async def test_http_push_structural_batch_errors_reject_everything() -> None:
    """NUL text and a self-parent folder reject the **whole** batch (plan §11.2).

    Both are refused before any op runs, and the earlier op in the array must not
    commit - that is the batch rule, whichever layer enforces it.

    Which layer enforces it is worth stating, because the two are different
    guarantees: the wire schema refuses both (a NUL fails the text validator, a
    self-parent is a ``model_validator``), and the *kernel* would also refuse them if
    they ever reached it. A test that only drove the HTTP endpoint would therefore
    pass even if ``prepare_batch`` were deleted, which is why
    ``test_http_push_prebuild_catches_what_the_schema_cannot`` pins the residual class
    the schema cannot see.
    """
    async with open_api() as api:
        # A NUL in a note body: PostgreSQL cannot store it, the kernel refuses it.
        bad_body = note_create(body_md="before\x00after")
        response = await api.push([folder_create(name="would have applied"), bad_body])
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_request"
        state = await api.state()
        assert state.last_seq == 0 and state.folders == 0, "the earlier op must not run"

        # A folder that is its own parent.
        parent = folder_create(name="self")
        parent["payload"]["parent_id"] = parent["entity_id"]
        response = await api.push([note_create(title="would have applied"), parent])
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_request"
        state = await api.state()
        assert state.last_seq == 0 and state.notes == 0

        # A NUL in a folder name is refused the same way.
        bad_name = folder_create(name="a\x00b")
        response = await api.push([bad_name])
        assert response.status_code == 400, response.text
        assert (await api.state()).folders == 0


async def test_http_push_unhashable_discriminator_is_a_400() -> None:
    """A non-string ``entity_type``/``op`` is a contract error, never a 500.

    The payload model is selected by ``PAYLOAD_MODELS[(entity_type, op)]``, and a
    list or object is unhashable: calling ``.get()`` with it raises ``TypeError``
    inside validation. A 500 for client input is both wrong and a hint that the
    request reached an unguarded path, so the selector must leave a non-string
    discriminator alone and let the declared ``Literal`` fields produce the 400.
    """
    valid = note_create(title="t", body_md="x")

    async with open_api() as api:
        for label, broken in (
            ("entity_type=[]", {**valid, "entity_type": []}),
            ("entity_type={}", {**valid, "entity_type": {"a": 1}}),
            ("op=[]", {**valid, "op": []}),
            ("op={}", {**valid, "op": {"a": 1}}),
            ("entity_type=null", {**valid, "entity_type": None}),
        ):
            response = await api.push([broken])
            assert response.status_code == 400, (label, response.status_code, response.text)
            assert response.json()["error"]["code"] == "invalid_request", label

        # And an unhashable discriminator in the *second* op still rejects the batch.
        response = await api.push([folder_create(name="early"), {**valid, "entity_type": []}])
        assert response.status_code == 400, response.text
        assert (await api.state()).last_seq == 0


async def test_http_push_prebuild_catches_what_the_schema_cannot() -> None:
    """``prepare_batch`` rejects the whole batch, not just per-op (plan §11.2).

    The wire schema already catches most structural problems (a NUL, a self-parent
    folder), and a test that stops there would not prove the *batch* rule at all: it
    would only prove that FastAPI validates the body. The kernel is the final
    authority on a few remaining shapes, and those are the ones that must not leak
    into the execution loop - an op at index 7 may not fail after 0-6 committed.

    This case drives the seam directly, so it fails if ``prepare_batch`` starts
    returning commands for a spec the kernel refuses.
    """
    from revnote_api.services.sync_push_adapter import (
        PushOpSpec,
        build_command,
        prepare_batch,
    )

    async with open_api() as api:
        good = PushOpSpec(
            op_id=uuid.uuid4(),
            entity_type="note",
            op="create",
            entity_id=uuid.uuid4(),
            base_rev=None,
            payload={"title": "t", "body_md": "fine"},
        )
        # A payload key the kernel command cannot accept: the schema would have
        # stripped it, so this is exactly the residual class ``prepare_batch`` owns.
        unusable = PushOpSpec(
            op_id=uuid.uuid4(),
            entity_type="note",
            op="create",
            entity_id=uuid.uuid4(),
            base_rev=None,
            payload={"title": "t", "body_md": "x", "not_a_field": 1},
        )

        assert isinstance(build_command(api.workspace_id, unusable), object)
        assert build_command(api.workspace_id, unusable).status == "rejected"  # type: ignore[union-attr]
        assert prepare_batch(api.workspace_id, [good]) is not None
        assert prepare_batch(api.workspace_id, [good, unusable]) is None, (
            "one unusable op must reject the whole batch"
        )

        # And the route honours that: the same pair over HTTP writes nothing.
        payload = note_create(title="t", body_md="fine")
        payload["payload"]["not_a_field"] = 1
        response = await api.push([folder_create(name="early"), payload])
        assert response.status_code == 400, response.text
        assert (await api.state()).last_seq == 0


async def test_http_push_unknown_payload_field_applies_nothing() -> None:
    """``extra="forbid"`` at the payload level: the whole batch is a 400."""
    async with open_api() as api:
        broken = note_create(title="t", body_md="b")
        broken["payload"]["body"] = "extra"  # type: ignore[index]

        response = await api.push([folder_create(name="early"), broken])

        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_request"
        state = await api.state()
        assert state.last_seq == 0 and state.folders == 0


async def test_http_push_path_workspace_is_authoritative() -> None:
    """An op cannot name a workspace; a foreign path is a uniform 404."""
    async with open_api() as api:
        other = await api.app.seed_second_account()

        smuggled = note_create(title="t", body_md="b")
        smuggled["workspace_id"] = str(other.workspace_id)
        response = await api.push([smuggled])
        assert response.status_code == 400, response.text

        denied = await api.push(
            [note_create(title="t", body_md="b")], workspace_id=other.workspace_id
        )
        assert denied.status_code == 404
        assert denied.json()["error"]["code"] == "not_found"

        assert (await api.state()).last_seq == 0
        assert (await api.state(other.workspace_id)).last_seq == 0


# --------------------------------------------------------------------------- #
# secrets are never echoed
# --------------------------------------------------------------------------- #
async def test_secrets_never_echo(caplog: pytest.LogCaptureFixture) -> None:
    """A secret in *any* position of a push never appears in output (plan 搂11.1).

    Four distinct escape routes, all real requests:

    1. **A note body in a rejected payload** - the 400 reason mechanism reports a
       field *path*, never the value, so the body cannot come back even though the
       schema had to parse it.
    2. **A secret used as an unknown JSON key** - ``extra="forbid"`` puts the
       offending key into Pydantic's ``loc``, which is exactly the leak the
       sanitizer exists for: it must be replaced by a constant placeholder.
    3. **The bearer token on an unauthorised request** - 401 responses describe the
       contract, never the credential.
    4. **The raw query string** - ``since`` with a sentinel value in it must not be
       quoted back by the 400.

    Logs are checked too: the write path logs a category and timings only, so a
    captured record may contain neither the body nor the token.
    """
    body_secret = "body-sentinel-9f2c1d"
    key_secret = "key-sentinel-4a7b3e"
    query_secret = "query-sentinel-6c8d2f"

    caplog.set_level("DEBUG")
    async with open_api() as api:
        # -- 1: a rejected payload that contains the body ------------------- #
        broken = note_create(title="t", body_md=body_secret)
        broken["payload"]["unexpected"] = 1  # type: ignore[index]
        response = await api.push([broken])
        assert response.status_code == 400, response.text
        assert body_secret not in response.text
        # The validator may name the *declared* path it rejected; it may never quote
        # the value it read while doing so.
        assert "ops" in response.text
        assert response.json()["error"]["details"]["fields"]
        for entry in response.json()["error"]["details"]["fields"]:
            assert "input" not in entry and "ctx" not in entry

        # -- 2: a secret *as a JSON key* ------------------------------------ #
        smeared = note_create(title="t", body_md=body_secret)
        smeared["payload"][key_secret] = "value"  # type: ignore[index]
        response = await api.push([smeared])
        assert response.status_code == 400, response.text
        assert key_secret not in response.text, "an unknown key must never be reflected"
        assert "<field>" in response.text, "the unknown key must be replaced, not dropped"
        assert body_secret not in response.text

        # An unknown *top-level* key is the same leak with a different path.
        smuggled = {"ops": [note_create(title="t", body_md="ok")], key_secret: "v"}
        response = await api.push_raw(smuggled)
        assert response.status_code == 400, response.text
        assert key_secret not in response.text
        assert "<field>" in response.text

        # -- 3: an invalid token -------------------------------------------- #
        response = await api.push([note_create()], token="token-sentinel-7e1a55")
        assert response.status_code == 401, response.text
        assert "token-sentinel" not in response.text

        # -- 4: the raw query string ---------------------------------------- #
        response = await api.pull(params={"since": query_secret})
        assert response.status_code == 400, response.text
        assert query_secret not in response.text

        # A *valid* query the server must still reject (cursor ahead) must not echo
        # the value either - the number travels as a stable detail, never the raw one.
        ahead = await api.pull(since=999)
        assert ahead.status_code == 400, ahead.text
        assert ahead.json()["error"]["code"] == "cursor_ahead"

        # -- and the note that *was* created stays readable to its owner ---- #
        note_id = uuid.uuid4()
        created = await api.push([note_create(note_id, title="kept", body_md=body_secret)])
        assert created.status_code == 200, created.text
        owner_view = await api.note(note_id)
        assert owner_view.status_code == 200
        assert owner_view.json()["body_md"] == body_secret, "an authorised read is allowed"

    # -- logs --------------------------------------------------------------- #
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert body_secret not in text, "a note body reached a log record"
    assert "token-sentinel" not in text


# --------------------------------------------------------------------------- #
# deletion is a tombstone, not a row removal
# --------------------------------------------------------------------------- #
async def test_http_push_delete_keeps_the_row_and_the_event() -> None:
    """A delete tombstones in place: the row stays and keeps its content."""
    async with open_api() as api:
        folder_id, note_id, spare_id, empty_id = (
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
            uuid.uuid4(),
        )
        assert _statuses(await api.push([folder_create(folder_id, name="f")])) == ["applied"]
        created = await api.push(
            [
                note_create(note_id, title="t", body_md="keep", folder_id=folder_id),
                note_create(spare_id, title="s", body_md="spare", folder_id=folder_id),
            ]
        )
        assert _statuses(created) == ["applied", "applied"]

        # A folder with a live note cannot be deleted.
        blocked = await api.push([folder_delete(folder_id, base_rev=1)])
        assert _statuses(blocked) == ["rejected"]
        assert _results(blocked)[0]["error"]["code"] == "folder_not_empty"

        assert _statuses(await api.push([note_delete(note_id, base_rev=1)])) == ["applied"]

        row = await api.db.note_row(api.workspace_id, note_id)
        assert row["deleted_at"] is not None
        assert row["body_md"] == "keep", "a tombstone keeps the last content"

        # One live note left: still not empty.
        assert _statuses(await api.push([folder_delete(folder_id, base_rev=1)])) == ["rejected"]
        assert _statuses(await api.push([note_delete(spare_id, base_rev=1)])) == ["applied"]
        assert _statuses(await api.push([folder_delete(folder_id, base_rev=1)])) == ["applied"]

        # An empty folder deletes normally.
        assert _statuses(await api.push([folder_create(empty_id, name="empty")])) == ["applied"]
        assert _statuses(await api.push([folder_delete(empty_id, base_rev=1)])) == ["applied"]

        state = await api.state()
        assert state.dense, state
        assert state.folders == 2, "a tombstone is still a row"
        assert state.notes == 2
