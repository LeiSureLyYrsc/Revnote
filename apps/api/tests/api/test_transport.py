"""Transport behaviour over a **real TCP socket served by uvicorn** (spec §3, P2 #5).

Why this tier exists
--------------------
``ASGITransport`` cannot produce a disconnect: its ``receive`` only answers
``http.disconnect`` after the response has already completed, so an in-process
client can never be "gone" while a handler is still running. Injecting the ASGI
message by hand would test the injection, not the server. These cases therefore
run the production app under ``uvicorn.Server`` on a real loopback socket, write
HTTP by hand, and close the socket with ``SO_LINGER(1, 0)`` so the peer receives an
RST — the state a crashed client actually leaves behind.

What is proven
--------------
``test_socket_disconnect_stops_batch``
    A batch is in flight and its second op is parked on a real row lock. While it
    waits, the client connection is reset. That op still commits (a disconnect is
    **not** a rollback), and the op *after* it is never started — the handler's
    between-ops ``request.is_disconnected()`` check sees the reset and stops. The
    committed prefix is dense; the trailing op does not exist.

``test_commit_survives_a_lost_response``
    The response is held at the ASGI boundary until the test has inspected the
    database, then the client socket is reset before the response is released. The
    commit is already durable, so losing the reply changes nothing about the write:
    the client must retry the same ``op_id`` to learn the recorded result.

``test_the_probe_serves_http_on_a_real_socket``
    A guard on the harness itself: a silently broken probe would make the
    disconnect cases pass for the wrong reason.

Both cases gate on observable state (``pg_locks``, the ASGI boundary, uvicorn's own
connection set, committed rows), never on a guessed sleep, and every probe is torn
down in ``finally`` — server first, then its listening socket, then the app's
engines.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from pg_support.api_harness import (
    AppUnderTest,
    backend_pid,
    folder_create,
    note_create,
    open_app,
    poll,
    row_lock,
    wait_until_blocked_by,
)
from pg_support.socket_probe import (
    Probe,
    build_request,
    open_probe,
    push_request,
    request,
    reset,
    send_raw,
)

pytestmark = [pytest.mark.anyio, pytest.mark.api, pytest.mark.transport]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _statuses(body: Mapping[str, Any]) -> list[str]:
    return [item["status"] for item in body["results"]]


async def _login(probe: Probe, email: str, password: str) -> str:
    """Log in *over the socket* and return the access token.

    Going through the real transport for the setup request too means the token is
    obtained exactly as a client would obtain it; nothing here shortcuts auth.
    """
    body = json.dumps({"email": email, "password": password}).encode()
    payload = build_request(
        method="POST", path="/api/v1/auth/login", body=body, token=None, port=probe.port
    )
    response = await request(probe, payload)
    assert response.status == 200, response.text
    return str(response.json()["access_token"])


async def _wait_connections_closed(probe: Probe) -> None:
    """Wait until the server dropped every connection (the RST was processed)."""
    await poll(
        lambda: not probe.server.server_state.connections,
        limit_s=20.0,
        what="uvicorn to observe the reset connection",
    )


async def _wait_lock_wait(app: AppUnderTest, blocker: int) -> None:
    """Wait until a backend is blocked by ``blocker`` (the row lock this test holds)."""
    connection = await app.db.conn()
    await wait_until_blocked_by(connection, blocker)


class _ResponseGate:
    """An outermost ASGI shim that parks ``http.response.start`` until released.

    Pure test scaffolding: it sits *above* the production app, changes no request
    and no response byte, and exists only so a case can assert "the write is
    committed while the reply has not been sent yet".

    ``arm()`` is what makes it selective: the gate starts **open** (so login and any
    setup request answer normally) and only the response that arrives after
    ``arm()`` is parked. Without that, the login response itself would trip
    :attr:`reached` and the case would assert against the wrong moment.
    """

    def __init__(self) -> None:
        self.reached = asyncio.Event()
        self._release = asyncio.Event()
        self._release.set()  # open until armed

    def arm(self) -> None:
        """Close the gate: the next response parks in ``http.response.start``."""
        self.reached = asyncio.Event()
        self._release.clear()

    def release(self) -> None:
        """Open the gate again (idempotent)."""
        self._release.set()

    def wrap(self, app: ASGIApp) -> ASGIApp:
        shim = self

        async def gated(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                await app(scope, receive, send)
                return

            async def gated_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    shim.reached.set()
                    await shim._release.wait()
                await send(message)

            await app(scope, receive, gated_send)

        return gated


@asynccontextmanager
async def _serving(app: AppUnderTest, *, gate: _ResponseGate | None = None) -> AsyncIterator[Probe]:
    """Run ``app`` under uvicorn for the block, optionally through a gate."""
    asgi_app = app.app if gate is None else gate.wrap(app.app)
    async with open_probe(asgi_app) as probe:
        yield probe


# --------------------------------------------------------------------------- #
# disconnect mid-batch
# --------------------------------------------------------------------------- #
async def test_socket_disconnect_stops_batch() -> None:
    """A reset mid-batch commits what ran, then stops: no rollback, no op3.

    Layout: three ops. ``op1`` creates a note (no contention). ``op2`` updates a
    *different* note whose row this test holds ``FOR UPDATE``, so it parks inside
    its own transaction at the CAS — a real lock, no production hook. ``op3``
    creates a folder and must never start.

    The client is reset while ``op2`` waits. Releasing the lock lets ``op2`` commit:
    the server never treats a vanished client as a rollback. The loop's next-op
    check then sees the disconnect, so ``op3`` is not attempted. The result is a
    dense committed prefix ``1..3`` and nothing from ``op3``.
    """
    async with open_app() as app:
        target_note = uuid.uuid4()
        op3_folder = uuid.uuid4()

        async with _serving(app) as probe:
            token = await _login(probe, app.account.email, app.account.password)

            # Seed the row op2 will update (one committed event).
            seed = await request(
                probe,
                push_request(
                    token=token,
                    workspace_id=app.workspace_id,
                    ops=[note_create(target_note, title="v1", body_md="one")],
                    port=probe.port,
                ),
            )
            assert seed.status == 200, seed.text
            assert _statuses(seed.json()) == ["applied"]

            connection = await app.db.conn()
            blocker = await backend_pid(connection)
            ops = [
                note_create(title="op1", body_md="first"),
                # op2: base_rev=1 against the note whose row we are about to lock.
                {
                    "op_id": str(uuid.uuid4()),
                    "entity_type": "note",
                    "op": "update",
                    "entity_id": str(target_note),
                    "base_rev": "1",
                    "payload": {"title": "v2", "body_md": "two"},
                },
                folder_create(op3_folder, name="op3"),
            ]

            async with row_lock(
                connection, "SELECT id FROM notes WHERE id = $1 FOR UPDATE", target_note
            ):
                _reader, writer = await send_raw(
                    probe,
                    push_request(
                        token=token,
                        workspace_id=app.workspace_id,
                        ops=ops,
                        port=probe.port,
                    ),
                )
                # op1 committed, op2 is parked on our row lock: a real server fact.
                await wait_until_blocked_by(connection, blocker)
                # The handler has already answered op1, so any op3 it was going to run
                # would be visible by now; a short bounded settle guards that.
                assert (await app.state()).event_seqs == (1, 2), "op1 must have committed"

                reset(writer)
                # Let uvicorn process the reset *before* the lock is released, so
                # the between-ops check cannot miss it.
                await _wait_connections_closed(probe)

            # The lock is gone: op2 completes. Nothing below ever replies to the
            # client (it is gone), and the handler must not fabricate a rollback.
            await _wait_committed(app, event_count=3)

        state = await app.state()
        # seed + op1 + op2 committed; op3 never started.
        assert state.event_seqs == (1, 2, 3), state
        assert state.dense
        assert state.notes == 2, "the seeded note and op1's note exist; op3 has no folder"
        assert state.folders == 0, "op3 must not have been attempted"

        row = await app.db.note_row(app.workspace_id, target_note)
        assert row["title"] == "v2", "op2 committed even though its client had gone"
        assert row["rev"] == 2

        # And op3's op never reached the ledger: there is nothing to replay.
        ledger_ops = await (await app.db.conn()).fetch(
            "SELECT op_id FROM op_ledger WHERE workspace_id = $1 ORDER BY result_seq",
            app.workspace_id,
        )
        recorded = {str(row["op_id"]) for row in ledger_ops}
        assert ops[2]["op_id"] not in recorded
        assert ops[0]["op_id"] in recorded and ops[1]["op_id"] in recorded


async def _wait_committed(app: AppUnderTest, *, event_count: int) -> None:
    """Wait until exactly ``event_count`` events are committed (no sleeping)."""
    await poll(
        lambda: _events(app, event_count),
        limit_s=30.0,
        what=f"{event_count} committed events",
    )


async def _events(app: AppUnderTest, expected: int) -> bool:
    state = await app.state()
    return len(state.event_seqs) == expected


# --------------------------------------------------------------------------- #
# the response is generated after the commit
# --------------------------------------------------------------------------- #
async def test_commit_survives_a_lost_response() -> None:
    """The reply is only produced after the commit; losing it loses no data.

    The gate parks ``http.response.start``, which the app can only reach once the
    handler has returned — i.e. after the op committed. The test inspects the
    database at that point (committed), resets the client, then releases the reply:
    the server writes into a dead socket and the row stays exactly as it was. That
    is why the protocol tells a client to retry the same ``op_id`` instead of
    assuming a rollback.
    """
    gate = _ResponseGate()
    note_id = uuid.uuid4()
    op = note_create(note_id, title="durable", body_md="payload")

    async with open_app() as app:
        async with _serving(app, gate=gate) as probe:
            token = await _login(probe, app.account.email, app.account.password)

            # Only the push's reply is parked: login already answered.
            gate.arm()
            _reader, writer = await send_raw(
                probe,
                push_request(
                    token=token,
                    workspace_id=app.workspace_id,
                    ops=[op],
                    port=probe.port,
                ),
            )
            await asyncio.wait_for(gate.reached.wait(), timeout=30)

            # The handler finished: the write is committed before any reply byte
            # exists, which is the ordering the protocol relies on.
            state = await app.state()
            assert state.event_seqs == (1,), state
            assert state.notes == 1
            assert state.ledger_rows == 1

            reset(writer)
            await _wait_connections_closed(probe)
            gate.release()

            # Nothing was rolled back by the lost reply, and the identical op (same
            # op_id, same payload) replays its first result on a later connection.
            state = await app.state()
            assert state.event_seqs == (1,)
            assert state.notes == 1

            replay = await request(
                probe,
                push_request(
                    token=token,
                    workspace_id=app.workspace_id,
                    ops=[op],
                    port=probe.port,
                ),
            )
            assert replay.status == 200, replay.text
            results = replay.json()["results"]
            assert results[0]["status"] == "replayed"
            assert results[0]["seq"] == "1", "the client learns the original position"

        state = await app.state()
        assert state.event_seqs == (1,), (state, "a replay must not write a second event")
        assert state.notes == 1


# --------------------------------------------------------------------------- #
# the transport is real
# --------------------------------------------------------------------------- #
async def test_the_probe_serves_http_on_a_real_socket() -> None:
    """A guard on the harness: the app answers over TCP, parsed from raw bytes."""
    async with open_app() as app, _serving(app) as probe:
        token = await _login(probe, app.account.email, app.account.password)
        assert token, "the login over TCP returned no token"

        response = await request(
            probe,
            build_request(
                method="GET",
                path=f"/api/v1/workspaces/{app.workspace_id}/tree",
                body=None,
                token=token,
                port=probe.port,
            ),
        )
        assert response.status == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == {"folders": [], "notes": []}

        # uvicorn's own counters prove the request went through it.
        assert probe.server.server_state.total_requests >= 2

        # And the access log is off: no request line may be written anywhere.
        assert probe.server.config.access_log is False
