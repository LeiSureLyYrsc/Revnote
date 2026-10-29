"""Concurrency proofs over HTTP with **independent PostgreSQL connections** (spec §3-5).

Why these cases cannot live in the ASGI-only tier
-------------------------------------------------
Each one is a claim about what several *committed* transactions may reveal, so each
needs at least two request paths in flight, and some need a third connection holding
a real lock. Every wait is on observable server state (``pg_locks``, committed
rows, a response arriving), never on a guessed sleep:

* ``test_http_push_dense_seq`` - many concurrent HTTP pushes allocate a dense
  ``seq``: no gap, no duplicate, one ledger row per op.
* ``test_http_pull_during_push`` - a pull taken while pushes are committing returns
  a contiguous prefix, and a chain from its cursor still sees every event once.
* ``test_pull_cannot_observe_n_plus_one_first`` - while a write holding ``seq = N``
  is uncommitted and a later push is *waiting* for the workspace lock, a pull sees
  only the committed prefix; it can never see ``N+1`` before ``N``.
* ``test_snapshot_is_one_rr_view`` - a snapshot paused after its waterline read,
  with a push committing in between, still reports one view: the same
  ``as_of_seq``, no object from the later write, and a ``pull since=as_of_seq``
  that yields it.
* ``test_concurrent_refresh_same_token_has_one_winner`` - two HTTP refreshes of
  the **same** refresh token: exactly one rotates, the other is ``token_reused``,
  and the family revocation is committed (a fresh connection reads it) so the
  winner's access *and* refresh tokens are dead (R1).
* ``test_concurrent_registration_of_one_address_creates_one_account`` - two
  concurrent registrations of case/space variants of one address: exactly one
  account, one 201 and one 400 on ``email`` (the functional unique index, not the
  pre-check, is the authority).

The snapshot case pauses between two statements of a real ``REPEATABLE READ``
transaction through ``AsyncSession.execute`` (the same kind of seam the kernel's
durability test uses): the statements, the transaction and the isolation level are
all real - only the delay is injected, and the gate is asserted to have fired, so
the test cannot pass vacuously if the SQL shape changes.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from pg_support.api_harness import (
    backend_pid,
    folder_create,
    note_create,
    open_api,
    parse_decimal,
    wait_until_blocked_by,
)
from pg_support.auth_support import DEFAULT_PASSWORD
from revnote_api.services.sync_read import SyncReadService

pytestmark = [pytest.mark.anyio, pytest.mark.api, pytest.mark.api_concurrency]


def _statuses(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["status"] for item in response.json()["results"]]


def _seqs(body: dict[str, Any]) -> list[int]:
    return [parse_decimal(event["seq"]) for event in body["events"]]


# --------------------------------------------------------------------------- #
# dense sequence over HTTP
# --------------------------------------------------------------------------- #
async def test_http_push_dense_seq() -> None:
    """Twelve concurrent batches over HTTP still allocate ``1..N`` exactly once.

    Every request runs on its own task, so each op takes its own pooled connection
    and its own transaction; the only serialisation is the kernel's workspace row
    lock. A gap would mean a cursor that never becomes visible; a duplicate would
    mean two objects share one event position - both are checked against committed
    rows, not against the responses.
    """
    async with open_api() as api:
        producers = 6
        per_producer = 3

        async def produce(index: int) -> Any:
            ops = [
                note_create(title=f"p{index}-{step}", body_md=f"body {index}/{step}")
                for step in range(per_producer)
            ]
            return await api.push(ops)

        responses = await asyncio.gather(*(produce(index) for index in range(producers)))

        allocated: list[int] = []
        for response in responses:
            assert response.status_code == 200, response.text
            results = response.json()["results"]
            assert [item["status"] for item in results] == ["applied"] * per_producer
            allocated.extend(parse_decimal(item["seq"]) for item in results)

        total = producers * per_producer
        assert sorted(allocated) == list(range(1, total + 1)), "seq must be dense and unique"
        assert len(set(allocated)) == total, "two ops shared a sequence number"

        state = await api.state()
        assert state.last_seq == total
        assert state.event_seqs == tuple(range(1, total + 1))
        assert state.ledger_rows == total
        assert state.notes == total

        # The cursor is also what pull reports, from the same committed state.
        page = await api.pull(since=0, limit=total)
        body = page.json()
        assert _seqs(body) == list(range(1, total + 1))
        assert body["last_seq"] == str(total)


# --------------------------------------------------------------------------- #
# pull while pushes commit
# --------------------------------------------------------------------------- #
async def test_http_pull_during_push() -> None:
    """A pull concurrent with commits returns a contiguous prefix, never a gap.

    The puller loops until every producer has finished; each page is validated
    against its own ``last_seq`` *and* against the chain rule, so a page that
    skipped an event - or that advertised a watermark ahead of its events - fails
    immediately rather than at the end.
    """
    async with open_api() as api:
        producers, per_producer = 3, 5
        finished = asyncio.Event()
        observed: list[tuple[int, tuple[int, ...]]] = []

        async def produce(index: int) -> Any:
            response = await api.push(
                [note_create(title=f"p{index}-{step}") for step in range(per_producer)]
            )
            return response

        async def pull_until_done() -> None:
            """Chain pages at full speed until the producers are done and caught up."""
            since = 0
            while True:
                page = await api.pull(since=since, limit=4)
                assert page.status_code == 200, page.text
                body = page.json()
                seqs = _seqs(body)
                last_seq = parse_decimal(body["last_seq"])
                assert seqs == list(range(since + 1, since + 1 + len(seqs))), (
                    f"a page skipped an event: since={since} seqs={seqs}"
                )
                assert all(seq <= last_seq for seq in seqs), "an event ahead of its watermark"
                observed.append((last_seq, tuple(seqs)))

                # The cursor is always the last shipped event (or the current one for
                # an empty page), so the chain can never go backwards or stall.
                since = parse_decimal(body["next_cursor"])
                if finished.is_set() and since >= last_seq:
                    return
                # A poll interval, not a guessed ordering: the loop only re-reads a
                # cursor that has already been committed a moment ago.
                await asyncio.sleep(0.01)

        responses: list[Any] = []
        producer_tasks = [asyncio.create_task(produce(index)) for index in range(producers)]
        puller = asyncio.create_task(pull_until_done())
        try:
            responses = await asyncio.gather(*producer_tasks)
            finished.set()
            await asyncio.wait_for(puller, timeout=60)
        finally:
            for task in (*producer_tasks, puller):
                task.cancel()

        for response in responses:
            assert response.status_code == 200, response.text
            assert _statuses(response) == ["applied"] * per_producer

        total = producers * per_producer
        state = await api.state()
        assert state.event_seqs == tuple(range(1, total + 1)), state

        # Every observed watermark was a committed count, and never decreased.
        watermarks = [last for last, _ in observed]
        assert watermarks == sorted(watermarks), "a pull reported a shrinking watermark"
        assert observed, "the puller never observed anything"
        assert all(last <= total for last in watermarks)


# --------------------------------------------------------------------------- #
# no N+1 before N
# --------------------------------------------------------------------------- #
async def test_pull_cannot_observe_n_plus_one_first() -> None:
    """A waiting push cannot publish ``N+1`` while ``N`` is still uncommitted.

    The state is built with SQL on purpose: reproducing a *real* in-flight kernel
    transaction needs either a production hook or a lock that the kernel takes later
    than the desired point. Here one connection opens a transaction that advances the
    workspace cursor to ``N``, writes event ``N`` **and** the folder row whose
    deferred ``updated_seq -> change_events`` foreign key points at it, all without
    committing - exactly the state the kernel is in between steps 7 and 10. It keeps
    the workspace row locked, so a real HTTP push arriving now cannot allocate any
    sequence at all.

    During that window pull must show only ``1..N-1``: the uncommitted event is
    invisible, and no later event can exist because the cursor is held. Then the
    staged transaction is rolled back, which proves nothing else consumed ``N``, and
    the waiting push takes ``N`` itself - so the write order stays dense.
    """
    async with open_api(
        # A long lock budget so the waiting push stays parked while the pull runs;
        # the point of the case is visibility, not the timeout path.
        overrides={"db_lock_timeout_ms": 20_000, "db_statement_timeout_ms": 30_000}
    ) as api:
        for _ in range(2):
            assert _statuses(await api.push([folder_create(name="seed")])) == ["applied"]

        connection = await api.db.conn()
        held_seq = 3
        held_folder = uuid.uuid4()
        await connection.execute("BEGIN")
        try:
            await connection.fetchrow(
                "SELECT id FROM workspaces WHERE id = $1 FOR UPDATE", api.workspace_id
            )
            await connection.execute(
                "UPDATE workspaces SET last_seq = $2 WHERE id = $1",
                api.workspace_id,
                held_seq,
            )
            await connection.execute(
                "INSERT INTO change_events"
                " (workspace_id, seq, entity_type, entity_id, op, rev, op_id)"
                " VALUES ($1, $2, 'folder', $3, 'create', 1, $4)",
                api.workspace_id,
                held_seq,
                held_folder,
                uuid.uuid4(),
            )
            # The object whose deferred FK names the staged event: both halves exist
            # inside this transaction, so a COMMIT would be valid - and a ROLLBACK
            # discards both. Without this row the staged state could never commit,
            # which is not what the kernel's in-flight window looks like.
            await connection.execute(
                "INSERT INTO folders"
                " (id, workspace_id, parent_id, name, rev, updated_seq, updated_at)"
                " VALUES ($1, $2, NULL, 'staged', 1, $3, now())",
                held_folder,
                api.workspace_id,
                held_seq,
            )

            blocked = asyncio.create_task(api.push([note_create(title="waiting")]))
            # The push now waits on the workspace row we hold: a real server fact.
            await wait_until_blocked_by(connection, await backend_pid(connection))

            visible = await api.pull(since=0, limit=100)
            assert visible.status_code == 200, visible.text
            body = visible.json()
            assert _seqs(body) == [1, 2], "an uncommitted N must be invisible"
            assert body["last_seq"] == "2"
            assert str(held_folder) not in visible.text, "no shadow of the staged object"
            assert "staged" not in visible.text

            assert not blocked.done(), "the waiting push must not have completed"
        finally:
            await connection.execute("ROLLBACK")

        response = await blocked
        assert _statuses(response) == ["applied"]
        # Rolled back, so the push took the freed position instead of N+1.
        assert response.json()["results"][0]["seq"] == str(held_seq)

        state = await api.state()
        assert state.event_seqs == (1, 2, 3), state
        assert state.dense
        entities = {
            str(row["entity_id"])
            for row in await (await api.db.conn()).fetch(
                "SELECT entity_id FROM change_events WHERE workspace_id = $1",
                api.workspace_id,
            )
        }
        assert str(held_folder) not in entities, "the rolled-back object left no event"
        assert state.folders == 2, "only the two seed folders remain"


# --------------------------------------------------------------------------- #
# snapshot: one REPEATABLE READ view
# --------------------------------------------------------------------------- #
async def test_snapshot_is_one_rr_view() -> None:
    """A snapshot paused mid-transaction still reports the view it started with.

    The app's snapshot service issues three statements in one ``REPEATABLE READ,
    READ ONLY`` transaction: pin the level, preflight the size, then read the
    objects. This test pauses **before the third** - after the waterline has been
    read, inside the open transaction - lets a real push commit, then lets the
    snapshot continue.

    A handler that read the waterline and the objects in two transactions (or that
    re-read ``last_seq`` afterwards) would return the *new* cursor with the *old*
    objects, or the new object with an old cursor. Both are impossible if the view
    is one transaction, which is exactly what is asserted: ``as_of_seq`` is the
    pre-push cursor, the pushed note is absent, and ``pull since=as_of_seq``
    returns the note that was committed "during" the snapshot.
    """
    async with open_api() as api:
        seeded = await api.push([note_create(title="before", body_md="old")])
        assert _statuses(seeded) == ["applied"]
        before = await api.state()

        gate = asyncio.Event()
        entered = asyncio.Event()
        real_read = SyncReadService._read_snapshot

        async def paused_read(session: AsyncSession, workspace_id: uuid.UUID) -> tuple[Any, ...]:
            # The snapshot service's own step-4 seam: the RR transaction is open and
            # the waterline has already been read, so this is the exact moment a
            # second connection's commit can slip in.
            entered.set()
            await gate.wait()
            return await real_read(session, workspace_id)

        SyncReadService._read_snapshot = staticmethod(paused_read)  # type: ignore[method-assign]
        snapshot_task = asyncio.create_task(api.snapshot())
        try:
            await asyncio.wait_for(entered.wait(), timeout=30)

            # The snapshot transaction is open and paused; a real write commits now.
            pushed = await api.push([note_create(title="during", body_md="new")])
            assert _statuses(pushed) == ["applied"]
            during = await api.state()
            assert during.last_seq == before.last_seq + 1

            gate.set()
            response = await asyncio.wait_for(snapshot_task, timeout=30)
        finally:
            gate.set()
            if not snapshot_task.done():
                snapshot_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await snapshot_task
            SyncReadService._read_snapshot = real_read  # type: ignore[method-assign]

        assert entered.is_set(), "the pause never fired: the statement shape changed"
        assert response.status_code == 200, response.text
        body = response.json()

        # One view: the cursor and the objects both belong to the pre-push state.
        assert body["as_of_seq"] == str(before.last_seq), "the watermark moved mid-snapshot"
        titles = [obj["title"] for obj in body["objects"] if obj["entity_type"] == "note"]
        assert titles == ["before"]
        assert "during" not in {obj.get("title") for obj in body["objects"]}

        # Continuing from that cursor yields exactly the write that happened after.
        page = await api.pull(since=body["as_of_seq"])
        assert page.status_code == 200, page.text
        assert _seqs(page.json()) == [before.last_seq + 1]
        assert page.json()["objects"][0]["title"] == "during"


# --------------------------------------------------------------------------- #
# refresh: one winner under a real concurrent replay (R1)
# --------------------------------------------------------------------------- #
async def test_concurrent_refresh_same_token_has_one_winner() -> None:
    """Two HTTP refreshes of one token: one rotates, the other is ``token_reused``.

    Both requests run on their own task and their own pooled connection, so the
    only thing that decides the outcome is the service's lock order (session, then
    token) plus the ``used_at`` re-read *under* that lock. A handler that trusted a
    pre-lock read would rotate twice and never notice the replay.

    The revocation must also be **durable before the loser is answered**: the
    check reads the session row on the probe's separate connection (never the app's
    pool), and then the winner's own credentials are presented again. Both are
    refused, so "the session is gone" is a fact about committed rows rather than an
    inference from one error body.
    """
    async with open_api() as api:
        login = await api.client.post(
            api.url("/auth/login"),
            json={"email": api.account.email, "password": api.account.password},
        )
        assert login.status_code == 200, login.text
        first = login.json()

        responses = await asyncio.gather(
            *(
                api.client.post(
                    api.url("/auth/refresh"), json={"refresh_token": first["refresh_token"]}
                )
                for _ in range(2)
            )
        )
        statuses = sorted(response.status_code for response in responses)
        assert statuses == [200, 401], [response.text for response in responses]

        winner_response = next(r for r in responses if r.status_code == 200)
        loser = next(r for r in responses if r.status_code == 401)
        winner = winner_response.json()
        assert loser.json()["error"]["code"] == "token_reused"
        assert winner["refresh_token"] != first["refresh_token"], "the winner rotated"
        assert winner["session_id"] == first["session_id"], "same family"

        # Committed, read from a connection that never served a request.
        connection = await api.db.conn()
        session_row = await connection.fetchrow(
            "SELECT revoked_at, revoke_reason FROM auth_sessions WHERE id = $1",
            uuid.UUID(first["session_id"]),
        )
        assert session_row is not None
        assert session_row["revoked_at"] is not None, "the reuse revocation must be durable"
        assert session_row["revoke_reason"] == "refresh_reuse"

        # The winner's access token is dead immediately, and so is its refresh token.
        me = await api.client.get(api.url("/me"), headers=api.headers(winner["access_token"]))
        assert me.status_code == 401
        assert me.json()["error"]["code"] == "unauthenticated"

        again = await api.client.post(
            api.url("/auth/refresh"), json={"refresh_token": winner["refresh_token"]}
        )
        assert again.status_code == 401
        assert again.json()["error"]["code"] in {"invalid_refresh", "token_reused"}


# --------------------------------------------------------------------------- #
# registration: one account for one normalised address
# --------------------------------------------------------------------------- #
async def test_concurrent_registration_of_one_address_creates_one_account() -> None:
    """Two concurrent registrations of case/space variants: exactly one 201.

    The service's pre-check is only a convenience; under concurrency both requests
    can pass it, and the functional unique index ``lower(btrim(email))`` is what
    decides. The loser must therefore be a clean ``400`` on the ``email`` field
    (the ``IntegrityError`` translation), never a 500/503 - and the database must
    hold exactly one account and one owner membership for that address.

    The address is derived from a fresh UUID: the suite never truncates, so a fixed
    address would collide with its own earlier run instead of with this race.
    """
    async with open_api(overrides={"auth_registration_open": True}) as api:
        address = f"race-{uuid.uuid4().hex}@revnote.test"
        local, _, domain = address.partition("@")
        # The same address, spelled with the noise normalisation is meant to absorb.
        noisy = f"  {local.upper()}@{domain.upper()} "

        responses = await asyncio.gather(
            *(
                api.client.post(
                    api.url("/auth/register"),
                    json={"email": candidate, "password": DEFAULT_PASSWORD},
                )
                for candidate in (address, noisy)
            )
        )
        statuses = sorted(response.status_code for response in responses)
        assert statuses == [201, 400], [response.text for response in responses]

        rejected = next(r for r in responses if r.status_code == 400)
        assert rejected.json()["error"]["code"] == "invalid_request"
        assert rejected.json()["error"]["details"]["field"] == "email"

        created = next(r for r in responses if r.status_code == 201).json()
        assert created["user"]["email"] == address, "a new account is stored normalised"

        connection = await api.db.conn()
        accounts = await connection.fetchval(
            "SELECT count(*) FROM users WHERE lower(btrim(email)) = $1", address
        )
        assert accounts == 1, "the unique index must admit exactly one account"

        owners = await connection.fetchval(
            "SELECT count(*) FROM workspace_members WHERE workspace_id = $1 AND role = 'owner'",
            uuid.UUID(created["workspace"]["id"]),
        )
        assert owners == 1, "the one account got exactly one workspace membership"
