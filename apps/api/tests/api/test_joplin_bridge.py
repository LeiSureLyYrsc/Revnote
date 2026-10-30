"""Phase 6: the Joplin compatibility bridge over **real PostgreSQL**.

Each case is one load-bearing claim from the plan (§3.2) and runs against the
production app (``create_app``) and the real kernel; nothing here builds a
second router or a fake service. The only double is the object store in the
sibling resource module, and that one is a real RustFS in CI.

The two directions are proven separately because they fail differently:

* bridge -> native: a Joplin item must become a note the native pull can read
  (mapping, kernel command, serialization);
* native -> bridge: a change event must become a Joplin delta entry whose name
  and content a client can follow (deterministic key, compression, cursor).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from pg_support.api_harness import note_create, open_api

pytestmark = [pytest.mark.anyio, pytest.mark.joplin]

BRIDGE = "/api"
KEY = "0123456789abcdef0123456789abcdef"
FOLDER_KEY = "fedcba9876543210fedcba9876543210"


async def bridge_login(
    harness: Any, account: Any | None = None, *, password: str | None = None
) -> str:
    target = account or harness.account
    response = await harness.client.post(
        f"{BRIDGE}/sessions",
        json={"email": target.email, "password": password or target.password},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user_id"] == str(target.user_id)
    return body["id"]


def auth(token: str) -> dict[str, str]:
    return {"X-API-AUTH": token}


def note_text(*, key: str, title: str, body: str, parent: str = "") -> str:
    """A literal Joplin note item, exactly as the client's serializer emits it."""
    return "\n\n".join(
        [
            title,
            body,
            "\n".join(
                [
                    f"id: {key}",
                    f"parent_id: {parent}",
                    "created_time: 2026-10-30T08:00:00.000Z",
                    "updated_time: 2026-10-30T09:30:15.450Z",
                    "markup_language: 1",
                    "type_: 1",
                ]
            ),
        ]
    )


def folder_text(*, key: str, title: str, parent: str = "") -> str:
    return "\n\n".join(
        [
            title,
            "\n".join(
                [
                    f"id: {key}",
                    f"parent_id: {parent}",
                    "created_time: 2026-10-30T08:00:00.000Z",
                    "updated_time: 2026-10-30T09:30:15.450Z",
                    "type_: 2",
                ]
            ),
        ]
    )


async def put_item(harness: Any, token: str, name: str, content: str) -> Any:
    return await harness.client.put(
        f"{BRIDGE}/items/root:/{name}:/content",
        content=content.encode("utf-8"),
        headers={**auth(token), "Content-Type": "application/octet-stream"},
    )


async def get_item(harness: Any, token: str, name: str) -> Any:
    return await harness.client.get(f"{BRIDGE}/items/root:/{name}:/content", headers=auth(token))


async def delta(harness: Any, token: str, *, cursor: str | None = None, limit: int | None = None):
    params: dict[str, str] = {}
    if cursor is not None:
        params["cursor"] = cursor
    if limit is not None:
        params["limit"] = str(limit)
    return await harness.client.get(
        f"{BRIDGE}/items/root:/:/delta", params=params, headers=auth(token)
    )


# --------------------------------------------------------------------------- #
# session
# --------------------------------------------------------------------------- #


@pytest.mark.api
async def test_session_login_is_email_password_and_misuse_is_unauthenticated() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        token = await bridge_login(harness)
        assert token and token != harness.token  # a bridge credential, not the native one

        wrong = await harness.client.post(
            f"{BRIDGE}/sessions",
            json={"email": harness.account.email, "password": "not the password"},
        )
        assert wrong.status_code == 401
        assert wrong.json()["code"] == "invalid_credentials"

        unknown = await harness.client.post(
            f"{BRIDGE}/sessions",
            json={"email": f"nobody-{uuid.uuid4().hex}@example.test", "password": "whatever"},
        )
        assert unknown.status_code == 401
        # Same shape and same message: the response does not disclose existence.
        assert unknown.json() == wrong.json()

        assert (await harness.client.get(f"{BRIDGE}/locks")).status_code == 401
        assert (
            await harness.client.get(f"{BRIDGE}/locks", headers=auth("not-a-session"))
        ).status_code == 401

        logged_out = await harness.client.delete(f"{BRIDGE}/sessions/{token}")
        assert logged_out.status_code == 204
        assert (await harness.client.get(f"{BRIDGE}/locks", headers=auth(token))).status_code == 401


# --------------------------------------------------------------------------- #
# bridge -> native
# --------------------------------------------------------------------------- #


@pytest.mark.api
async def test_a_note_written_through_the_bridge_is_readable_natively() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        token = await bridge_login(harness)

        response = await put_item(
            harness,
            token,
            f"{KEY}.md",
            note_text(key=KEY, title="From Joplin", body="line one\n\nline two"),
        )
        assert response.status_code == 200, response.text
        assert response.json()["jop_type"] == 1

        state = await harness.state()
        assert state.last_seq == 1
        assert state.notes == 1

        page = await harness.pull(since="0")
        assert page.status_code == 200
        body = page.json()
        notes = [item for item in body["objects"] if item["entity_type"] == "note"]
        assert len(notes) == 1
        assert notes[0]["title"] == "From Joplin"
        assert notes[0]["body_md"] == "line one\n\nline two"
        # The native id is authoritative and is not the Joplin key.
        assert notes[0]["id"] != KEY

        read_back = await get_item(harness, token, f"{KEY}.md")
        assert read_back.status_code == 200
        text = read_back.text
        assert text.startswith("From Joplin\n\n")
        assert "line one\n\nline two" in text
        assert f"id: {KEY}" in text


@pytest.mark.api
async def test_a_folder_and_its_note_keep_the_parent_mapping() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        token = await bridge_login(harness)

        folder = await put_item(
            harness, token, f"{FOLDER_KEY}.md", folder_text(key=FOLDER_KEY, title="Inbox")
        )
        assert folder.status_code == 200
        assert folder.json()["jop_type"] == 2

        note = await put_item(
            harness,
            token,
            f"{KEY}.md",
            note_text(key=KEY, title="Child", body="inside", parent=FOLDER_KEY),
        )
        assert note.status_code == 200
        assert note.json()["jop_parent_id"] == FOLDER_KEY

        tree = await harness.tree()
        assert tree.status_code == 200
        folders = tree.json()["folders"]
        notes = tree.json()["notes"]
        assert [folder["name"] for folder in folders] == ["Inbox"]
        assert notes[0]["folder_id"] == folders[0]["id"]

        content = await get_item(harness, token, f"{KEY}.md")
        assert f"parent_id: {FOLDER_KEY}" in content.text


@pytest.mark.api
async def test_deleting_through_the_bridge_is_a_kernel_tombstone() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        token = await bridge_login(harness)
        assert (
            await put_item(
                harness, token, f"{KEY}.md", note_text(key=KEY, title="Doomed", body="x")
            )
        ).status_code == 200

        response = await harness.client.delete(
            f"{BRIDGE}/items/root:/{KEY}.md:", headers=auth(token)
        )
        assert response.status_code == 204

        state = await harness.state()
        assert state.last_seq == 2
        page = await harness.pull(since="1")
        assert [event["op"] for event in page.json()["events"]] == ["delete"]

        assert (await get_item(harness, token, f"{KEY}.md")).status_code == 404
        assert (
            await harness.client.get(f"{BRIDGE}/items/root:/{KEY}.md:", headers=auth(token))
        ).status_code == 404


# --------------------------------------------------------------------------- #
# native -> bridge
# --------------------------------------------------------------------------- #


@pytest.mark.api
async def test_a_native_note_appears_in_the_bridge_delta_and_by_name() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        note_id = uuid.uuid4()
        pushed = await harness.push([note_create(note_id, title="Native", body_md="from native")])
        assert pushed.status_code == 200, pushed.text
        token = await bridge_login(harness)

        page = await delta(harness, token)
        assert page.status_code == 200
        body = page.json()
        assert body["has_more"] is False
        assert body["cursor"] == "1"

        entries = body["items"]
        assert len(entries) == 1
        assert entries[0]["type"] == 1
        assert entries[0]["jop_type"] == 1
        # A native entity's Joplin key is its UUID hex, deterministically.
        assert entries[0]["item_name"] == f"{note_id.hex}.md"

        content = await get_item(harness, token, entries[0]["item_name"])
        assert content.status_code == 200
        assert content.text.startswith("Native\n\n")
        assert "from native" in content.text
        assert f"id: {note_id.hex}" in content.text


@pytest.mark.api
async def test_an_item_can_be_updated_repeatedly_through_the_bridge() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        token = await bridge_login(harness)
        name = f"{KEY}.md"
        assert (
            await put_item(harness, token, name, note_text(key=KEY, title="v1", body="one"))
        ).status_code == 200

        # An identical PUT is a no-op: no new event, no new revision.
        assert (
            await put_item(harness, token, name, note_text(key=KEY, title="v1", body="one"))
        ).status_code == 200
        assert (await harness.state()).last_seq == 1

        assert (
            await put_item(harness, token, name, note_text(key=KEY, title="v2", body="two"))
        ).status_code == 200
        state = await harness.state()
        assert state.last_seq == 2

        page = await harness.pull(since="0")
        note = next(item for item in page.json()["objects"] if item["entity_type"] == "note")
        assert (note["title"], note["body_md"]) == ("v2", "two")


# --------------------------------------------------------------------------- #
# delta
# --------------------------------------------------------------------------- #


@pytest.mark.api
async def test_delta_chains_without_gaps_and_compresses_a_create_then_updates() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        token = await bridge_login(harness)
        name = f"{KEY}.md"
        for version in ("one", "two", "three"):
            assert (
                await put_item(
                    harness,
                    token,
                    name,
                    note_text(key=KEY, title="Same note", body=version),
                )
            ).status_code == 200

        single = await delta(harness, token, limit=100)
        assert single.status_code == 200
        body = single.json()
        assert [entry["item_name"] for entry in body["items"]] == [name]
        assert [entry["type"] for entry in body["items"]] == [1]  # create wins
        assert body["cursor"] == "3"

        # Page-local compression: three pages of one event each are three deltas
        # (create, update, update) with a cursor that never goes backwards.
        cursor = "0"
        seen: list[int] = []
        for _ in range(10):
            page = await delta(harness, token, cursor=cursor, limit=1)
            assert page.status_code == 200
            payload = page.json()
            assert int(payload["cursor"]) >= int(cursor)
            cursor = payload["cursor"]
            seen.extend(entry["type"] for entry in payload["items"])
            if not payload["has_more"]:
                break
        assert seen == [1, 2, 2]
        assert cursor == "3"


@pytest.mark.api
async def test_an_invalid_or_ahead_cursor_requires_a_resync() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        token = await bridge_login(harness)

        for cursor in ("not-a-number", "-1", "999999"):
            response = await delta(harness, token, cursor=cursor)
            assert response.status_code == 400, cursor
            assert response.json()["code"] == "resyncRequired"

        first = await delta(harness, token, cursor="0")
        assert first.status_code == 200
        assert first.json() == {"items": [], "cursor": "0", "has_more": False}


@pytest.mark.api
async def test_a_create_then_delete_pair_is_absent_from_the_delta() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        token = await bridge_login(harness)
        name = f"{KEY}.md"
        assert (
            await put_item(harness, token, name, note_text(key=KEY, title="Gone", body="x"))
        ).status_code == 200
        assert (
            await harness.client.delete(f"{BRIDGE}/items/root:/{name}:", headers=auth(token))
        ).status_code == 204

        page = await delta(harness, token)
        assert page.json()["items"] == []
        # The cursor still advanced past both raw events.
        assert page.json()["cursor"] == "2"


# --------------------------------------------------------------------------- #
# locks
# --------------------------------------------------------------------------- #


async def acquire(harness: Any, token: str, *, lock_type: int, client_type: int, client_id: str):
    return await harness.client.post(
        f"{BRIDGE}/locks",
        json={"type": lock_type, "clientType": client_type, "clientId": client_id},
        headers=auth(token),
    )


@pytest.mark.api
async def test_sync_locks_coexist_and_an_exclusive_lock_excludes_everyone() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        token = await bridge_login(harness)

        assert (
            await acquire(harness, token, lock_type=1, client_type=1, client_id="desk")
        ).status_code == 200
        assert (
            await acquire(harness, token, lock_type=1, client_type=2, client_id="mob")
        ).status_code == 200

        blocked = await acquire(harness, token, lock_type=2, client_type=3, client_id="cli")
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "hasSyncLock"

        for client_type, client_id in ((1, "desk"), (2, "mob")):
            released = await harness.client.delete(
                f"{BRIDGE}/locks/1_{client_type}_{client_id}", headers=auth(token)
            )
            assert released.status_code == 204

        exclusive = await acquire(harness, token, lock_type=2, client_type=1, client_id="desk")
        assert exclusive.status_code == 200
        # The client's Lock shape is camelCase with epoch milliseconds.
        assert exclusive.json()["type"] == 2
        assert exclusive.json()["clientType"] == 1
        assert exclusive.json()["clientId"] == "desk"
        assert isinstance(exclusive.json()["updatedTime"], int)

        blocked_sync = await acquire(harness, token, lock_type=1, client_type=2, client_id="mob")
        assert blocked_sync.status_code == 409
        assert blocked_sync.json()["code"] == "hasExclusiveLock"

        listed = await harness.client.get(f"{BRIDGE}/locks", headers=auth(token))
        assert listed.status_code == 200
        assert listed.json()["has_more"] is False
        assert [(item["type"], item["clientId"]) for item in listed.json()["items"]] == [
            (2, "desk")
        ]

        # Lock traffic never moves the sync cursor or writes an event.
        state = await harness.state()
        assert (state.last_seq, state.event_seqs) == (0, ())

        assert (
            await harness.client.delete(f"{BRIDGE}/locks/2_1_desk", headers=auth(token))
        ).status_code == 204
        empty = await harness.client.get(f"{BRIDGE}/locks", headers=auth(token))
        assert empty.json()["items"] == []


# --------------------------------------------------------------------------- #
# isolation and batch
# --------------------------------------------------------------------------- #


@pytest.mark.api
async def test_another_account_cannot_address_this_workspace() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        token = await bridge_login(harness)
        assert (
            await put_item(
                harness, token, f"{KEY}.md", note_text(key=KEY, title="Private", body="secret")
            )
        ).status_code == 200

        second = await harness.app.seed_second_account()
        other = await bridge_login(harness, second)
        assert (await get_item(harness, other, f"{KEY}.md")).status_code == 404
        assert (
            await harness.client.get(f"{BRIDGE}/items/root:/{KEY}.md:", headers=auth(other))
        ).status_code == 404
        assert (await delta(harness, other)).json()["items"] == []


@pytest.mark.api
async def test_batch_put_and_batch_delete_are_per_item() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        token = await bridge_login(harness)
        other = uuid.uuid4().hex

        response = await harness.client.put(
            f"{BRIDGE}/batch_items",
            json={
                "items": [
                    {"name": f"{KEY}.md", "body": note_text(key=KEY, title="A", body="a")},
                    {"name": f"{other}.md", "body": folder_text(key=other, title="B")},
                ]
            },
            headers=auth(token),
        )
        assert response.status_code == 200
        results = response.json()["items"]
        assert set(results) == {f"{KEY}.md", f"{other}.md"}
        assert results[f"{KEY}.md"]["item"]["jop_type"] == 1
        assert results[f"{other}.md"]["item"]["jop_type"] == 2

        deleted = await harness.client.request(
            "DELETE",
            f"{BRIDGE}/batch_items",
            json={"items": [f"{KEY}.md", f"{other}.md"]},
            headers=auth(token),
        )
        assert deleted.status_code == 200
        assert deleted.json()["items"] == {f"{KEY}.md": {}, f"{other}.md": {}}
        assert (await harness.state()).last_seq == 4


@pytest.mark.api
async def test_item_metadata_carries_the_times_the_client_compares() -> None:
    async with open_api(overrides={"joplin_bridge_enabled": True}) as harness:
        token = await bridge_login(harness)
        await put_item(harness, token, f"{KEY}.md", note_text(key=KEY, title="Timed", body="x"))

        stat = await harness.client.get(f"{BRIDGE}/items/root:/{KEY}.md:", headers=auth(token))
        assert stat.status_code == 200
        body = stat.json()
        assert set(body) >= {
            "id",
            "name",
            "jop_id",
            "jop_type",
            "jop_parent_id",
            "jop_updated_time",
            "updated_time",
            "created_time",
        }
        assert body["id"] == KEY
        assert body["name"] == f"{KEY}.md"
        assert body["updated_time"] == body["jop_updated_time"]
        assert isinstance(body["created_time"], int) and body["created_time"] > 0
        assert isinstance(body["updated_time"], int) and body["updated_time"] > 0
        assert body["updated_time"] >= body["created_time"]
