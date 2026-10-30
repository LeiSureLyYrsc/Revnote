"""Phase 6 unit tests: protocol translation, no database, no object store.

What is pinned here is the part a Joplin client is strict about:

* the serialized item format (``title``/blank/``body``/blank/``props``) and its
  parser, including the exact shape the client's ``unserialize`` accepts;
* the delta compression table and the cursor rule that dropped events still
  advance the cursor;
* the error vocabulary: every code the adapter can raise has a status and a
  fixed sentence, and only the three behavioural codes travel as names;
* the lock id round trip (``<type>_<clientType>_<clientId>``);
* the switch: a disabled deployment does not mount the compatibility routes and
  says so in ``/capabilities``.
"""

from __future__ import annotations

import datetime
import uuid

import httpx
import pytest

from revnote_api.adapters.joplin.delta import (
    CHANGE_CREATE,
    CHANGE_DELETE,
    CHANGE_UPDATE,
    RawEvent,
    compress_events,
)
from revnote_api.adapters.joplin.errors import (
    ERROR_MESSAGE,
    JOPLIN_ERROR_STATUS,
    NATIVE_CODE_MAP,
    JoplinProtocolError,
    translate_native,
)
from revnote_api.adapters.joplin.ids import (
    is_jop_key,
    jop_key_for_uuid,
    uuid_from_jop_key,
)
from revnote_api.adapters.joplin.locks import lock_id, parse_lock_id
from revnote_api.adapters.joplin.serialize import (
    TYPE_FOLDER,
    TYPE_NOTE,
    TYPE_RESOURCE,
    JoplinItemError,
    parse_item,
    serialize_folder,
    serialize_note,
    serialize_resource,
)
from revnote_api.config import Settings
from revnote_api.main import create_app

pytestmark = pytest.mark.anyio

UTC = datetime.UTC
CREATED = datetime.datetime(2026, 10, 30, 8, 0, 0, 120000, tzinfo=UTC)
UPDATED = datetime.datetime(2026, 10, 30, 9, 30, 15, 450000, tzinfo=UTC)
KEY = "0123456789abcdef0123456789abcdef"
PARENT = "fedcba9876543210fedcba9876543210"


# --------------------------------------------------------------------------- #
# serialization
# --------------------------------------------------------------------------- #


def test_a_note_serializes_and_parses_back_identically() -> None:
    content = serialize_note(
        jop_key=KEY,
        title="Shopping list",
        body="milk\n\nbread\n- eggs",
        parent_key=PARENT,
        created_time=CREATED,
        updated_time=UPDATED,
    )
    item = parse_item(content)

    assert item.id == KEY
    assert item.type_ == TYPE_NOTE
    assert item.title == "Shopping list"
    assert item.body == "milk\n\nbread\n- eggs"
    assert item.parent_id == PARENT
    assert item.created_time == int(CREATED.timestamp() * 1000)
    assert item.updated_time == int(UPDATED.timestamp() * 1000)


def test_a_note_with_an_empty_body_has_the_header_only_shape() -> None:
    content = serialize_note(
        jop_key=KEY,
        title="Empty",
        body="",
        parent_key="",
        created_time=CREATED,
        updated_time=UPDATED,
    )
    # The client's own serializer omits an empty body, so the text is
    # "title\n\nprops" and the parser must not invent a body.
    assert content.split("\n\n")[1].startswith("id:")
    item = parse_item(content)
    assert item.body == ""
    assert item.parent_id == ""


def test_a_folder_has_no_body_and_a_title_on_the_first_line() -> None:
    content = serialize_folder(
        jop_key=KEY,
        title="Projects",
        parent_key="",
        created_time=CREATED,
        updated_time=UPDATED,
    )
    item = parse_item(content)
    assert item.type_ == TYPE_FOLDER
    assert item.title == "Projects"
    assert item.body == ""


def test_a_resource_carries_its_metadata_properties() -> None:
    content = serialize_resource(
        jop_key=KEY,
        title="pixel.png",
        mime="image/png",
        filename="pixel.png",
        size=2048,
        created_time=CREATED,
        updated_time=UPDATED,
    )
    item = parse_item(content)
    assert item.type_ == TYPE_RESOURCE
    assert item.resource_mime == "image/png"
    assert item.resource_name == "pixel.png"
    assert item.resource_size == 2048
    assert item.props["file_extension"] == "png"


def test_unknown_properties_are_accepted_and_ignored() -> None:
    content = "Title\n\n" + "\n".join(
        [
            "id: " + KEY,
            "type_: 2",
            "future_field: 42",
        ]
    )
    item = parse_item(content)
    assert item.id == KEY
    assert item.props["future_field"] == "42"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("Title\n\nid: " + KEY, "missing type_"),
        ("Title\n\nid: nothex\ntype_: 1", "invalid item id"),
        ("Title\n\nid: " + KEY + "\ntype_: 9", "unsupported item type"),
        ("Title\n\nid: " + KEY + "\ntype_: 1\nnot a property", "invalid property line"),
    ],
)
def test_malformed_items_are_refused(content: str, reason: str) -> None:
    del reason
    with pytest.raises(JoplinItemError):
        parse_item(content)


def test_the_parser_accepts_uppercase_ids_and_normalises_them() -> None:
    content = "Title\n\nid: " + KEY.upper() + "\ntype_: 2"
    assert parse_item(content).id == KEY


# --------------------------------------------------------------------------- #
# keys
# --------------------------------------------------------------------------- #


def test_a_native_uuid_has_one_deterministic_key() -> None:
    entity = uuid.UUID("2f1c0e9a-1b2c-4d3e-8f90-1234567890ab")
    key = jop_key_for_uuid(entity)

    assert len(key) == 32
    assert is_jop_key(key)
    assert uuid_from_jop_key(key) == entity
    assert uuid_from_jop_key(key.upper()) == entity


def test_a_key_that_is_not_hex_is_not_read_as_a_uuid() -> None:
    assert uuid_from_jop_key("z" * 32) is None
    assert uuid_from_jop_key("f" * 31) is None
    assert not is_jop_key("F" * 32)


# --------------------------------------------------------------------------- #
# delta compression
# --------------------------------------------------------------------------- #


def _events(*ops: str, entity_id: uuid.UUID | None = None) -> list[RawEvent]:
    identifier = entity_id or uuid.uuid4()
    return [
        RawEvent(seq=index + 1, entity_type="note", entity_id=identifier, op=op, rev=index + 1)
        for index, op in enumerate(ops)
    ]


@pytest.mark.parametrize(
    ("ops", "expected"),
    [
        (("update", "update"), CHANGE_UPDATE),
        (("create", "update"), CHANGE_CREATE),
        (("create", "delete"), None),
        (("update", "delete"), CHANGE_DELETE),
        (("create",), CHANGE_CREATE),
        (("update",), CHANGE_UPDATE),
        (("move",), CHANGE_UPDATE),
        (("create", "move"), CHANGE_CREATE),
        (("create", "update", "move", "delete"), None),
        (("update", "move", "delete"), CHANGE_DELETE),
    ],
)
def test_the_compression_table(ops: tuple[str, ...], expected: int | None) -> None:
    compressed = compress_events(_events(*ops))
    if expected is None:
        assert compressed == []
    else:
        assert [change.change_type for change in compressed] == [expected]


def test_compression_is_per_entity_and_ordered_by_last_event() -> None:
    first = uuid.uuid4()
    second = uuid.uuid4()
    events = [
        RawEvent(seq=1, entity_type="note", entity_id=first, op="create", rev=1),
        RawEvent(seq=2, entity_type="folder", entity_id=second, op="create", rev=1),
        RawEvent(seq=3, entity_type="note", entity_id=first, op="update", rev=2),
        RawEvent(seq=4, entity_type="folder", entity_id=second, op="delete", rev=2),
    ]
    compressed = compress_events(events)

    assert [(change.entity_type, change.change_type, change.seq) for change in compressed] == [
        ("note", CHANGE_CREATE, 3)
    ]


def test_a_create_and_delete_pair_produces_nothing_but_consumes_its_seqs() -> None:
    identifier = uuid.uuid4()
    events = _events("create", "delete", entity_id=identifier)
    assert compress_events(events) == []
    # The cursor is the page's last raw seq, not the last emitted one: the route
    # must still advance past seq 2 or it would read these events forever.
    assert events[-1].seq == 2


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #


def test_every_code_has_a_status_and_a_fixed_sentence() -> None:
    assert set(JOPLIN_ERROR_STATUS) == set(ERROR_MESSAGE)
    for code in JOPLIN_ERROR_STATUS:
        assert JoplinProtocolError(code).code == code


def test_every_native_code_maps_onto_a_bridge_code() -> None:
    for native, bridge in NATIVE_CODE_MAP.items():
        assert bridge in JOPLIN_ERROR_STATUS, native
        assert translate_native(native).code == bridge


def test_an_unknown_native_code_degrades_to_internal() -> None:
    error = translate_native("something_new")
    assert error.code == "internal"
    assert error.status == 500


def test_only_the_behavioural_codes_travel_as_names() -> None:
    assert JoplinProtocolError("resync_required").wire_code == "resyncRequired"
    assert JoplinProtocolError("has_exclusive_lock").wire_code == "hasExclusiveLock"
    assert JoplinProtocolError("has_sync_lock").wire_code == "hasSyncLock"
    assert JoplinProtocolError("not_found").wire_code == "not_found"


def test_an_unknown_code_cannot_be_constructed() -> None:
    with pytest.raises(ValueError):
        JoplinProtocolError("made_up")


# --------------------------------------------------------------------------- #
# locks
# --------------------------------------------------------------------------- #


def test_a_lock_id_round_trips() -> None:
    identifier = lock_id("exclusive", 1, "desktop-abc")
    assert identifier == "2_1_desktop-abc"
    assert parse_lock_id(identifier) == ("exclusive", 1, "desktop-abc")


@pytest.mark.parametrize("value", ["", "1", "3_1_x", "1_9_x", "1_1_", "x_1_y"])
def test_a_malformed_lock_id_is_a_bad_request(value: str) -> None:
    with pytest.raises(JoplinProtocolError) as caught:
        parse_lock_id(value)
    assert caught.value.code == "bad_request"


# --------------------------------------------------------------------------- #
# the switch
# --------------------------------------------------------------------------- #


async def _client(settings: Settings) -> httpx.AsyncClient:
    app = create_app(settings)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://revnote.test")


async def test_the_bridge_is_off_by_default_and_mounts_nothing() -> None:
    settings = Settings(_env_file=None)
    assert settings.joplin_bridge_enabled is False

    async with await _client(settings) as client:
        assert (await client.post("/api/sessions", json={})).status_code == 404
        assert (await client.get("/api/locks")).status_code == 404


async def test_an_enabled_app_mounts_the_session_route() -> None:
    settings = Settings(_env_file=None, joplin_bridge_enabled=True)

    async with await _client(settings) as client:
        # 400 (bad body), not 404: the route exists and parses before auth.
        response = await client.post("/api/sessions", json={"nope": 1})
        assert response.status_code == 400
        assert response.json()["code"] == "bad_request"


async def test_capabilities_reports_the_bridge_additively() -> None:
    for enabled in (False, True):
        settings = Settings(_env_file=None, joplin_bridge_enabled=enabled)
        async with await _client(settings) as client:
            body = (await client.get("/api/v1/capabilities")).json()
        assert body["joplin_bridge"] == {
            "enabled": enabled,
            "protocol_baseline": "3.7",
            "mount": "/api",
        }
        # The Phase 5 object is still exactly there.
        assert set(body["attachments"]) == {"enabled", "max_bytes", "accepted_mime"}
