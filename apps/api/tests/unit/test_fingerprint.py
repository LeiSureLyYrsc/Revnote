"""Unit tests for the canonical request fingerprint (D4) — no database.

The hash is the *only* thing that decides "same command, replay" versus "same
``op_id``, different command, conflict", so these tests pin every property the
plan requires: a schema version, fixed key order and separators, lowercase UUIDs,
explicit ``null``, lossless integers, byte-exact text, and a hard rejection of
values whose stringification would be ambiguous.

They also prove the two things a weak implementation usually gets wrong:
``op_id`` must not participate (it is the lookup key), and a changed ``base_rev``
must produce a different hash (that is a new command needing a new ``op_id``).
"""

from __future__ import annotations

import hashlib
import json
import uuid

import pytest

from revnote_api.kernel.commands import (
    CreateFolder,
    CreateNote,
    DeleteFolder,
    DeleteNote,
    MoveFolder,
    MoveNote,
    UpdateFolder,
    UpdateNote,
)
from revnote_api.kernel.errors import InvalidCommand
from revnote_api.kernel.fingerprint import (
    HASH_BYTES,
    HASH_SCHEMA_VERSION,
    canonical_encode,
    command_hash,
    command_payload,
    request_hash,
)

WS = uuid.UUID("018f0000-0000-7000-8000-000000000001")
ENTITY = uuid.UUID("018f0000-0000-7000-8000-000000000002")
OP = uuid.UUID("018f0000-0000-7000-8000-000000000003")
PARENT = uuid.UUID("018f0000-0000-7000-8000-000000000004")


def ids(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {"workspace_id": WS, "entity_id": ENTITY, "op_id": OP}
    base.update(overrides)
    return base


def folder_create(**overrides: object) -> CreateFolder:
    kwargs: dict[str, object] = {**ids(), "name": "Inbox"}
    kwargs.update(overrides)
    return CreateFolder(**kwargs)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# shape of the payload and of the hash
# --------------------------------------------------------------------------- #


def test_hash_is_32_raw_sha256_bytes() -> None:
    digest = command_hash(folder_create())

    assert isinstance(digest, bytes)
    assert len(digest) == HASH_BYTES == 32
    # It really is SHA-256 over the canonical text, not a truncated or re-encoded form.
    expected = hashlib.sha256(canonical_encode(command_payload(folder_create())).encode()).digest()
    assert digest == expected


def test_payload_carries_the_schema_version_and_envelope() -> None:
    payload = command_payload(folder_create())

    assert payload["hash_schema_version"] == HASH_SCHEMA_VERSION == 1
    assert payload["entity_type"] == "folder"
    assert payload["op"] == "create"
    assert payload["workspace_id"] == WS
    assert payload["entity_id"] == ENTITY
    assert payload["base_rev"] is None
    assert payload["name"] == "Inbox"
    assert payload["parent_id"] is None  # explicit null, never omitted


def test_canonical_encoding_is_sorted_compact_json() -> None:
    text = canonical_encode(command_payload(folder_create()))

    assert " " not in text  # compact separators, no insignificant whitespace
    assert ", " not in text and '": ' not in text
    keys = list(json.loads(text))
    assert keys == sorted(keys)  # sort_keys=True
    assert text.startswith('{"base_rev":null,"entity_id":"')


def test_canonical_encoding_uses_lowercase_hyphenated_uuids() -> None:
    text = canonical_encode(command_payload(folder_create()))

    assert str(WS) in text and str(ENTITY) in text
    # Standard form (lowercase, hyphenated, no braces) for the ids, while business
    # text keeps whatever case the caller used.
    assert '"workspace_id":"018f0000-0000-7000-8000-000000000001"' in text
    assert WS.hex.upper() not in text


def test_integers_are_lossless_decimal_beyond_float_precision() -> None:
    huge = 2**53 + 1
    payload = {
        "hash_schema_version": HASH_SCHEMA_VERSION,
        "entity_type": "note",
        "op": "delete",
        "workspace_id": WS,
        "entity_id": ENTITY,
        "base_rev": huge,
    }

    assert f'"base_rev":{huge}' in canonical_encode(payload)
    assert f'"base_rev":{huge}.0' not in canonical_encode(payload)


def test_hash_is_stable_across_processes_and_dict_order() -> None:
    payload = {
        "hash_schema_version": HASH_SCHEMA_VERSION,
        "entity_type": "folder",
        "op": "create",
        "workspace_id": WS,
        "entity_id": ENTITY,
        "base_rev": None,
        "parent_id": None,
        "name": "Inbox",
    }
    reordered = dict(reversed(list(payload.items())))

    assert canonical_encode(payload) == canonical_encode(reordered)
    assert request_hash(payload) == request_hash(reordered)


# --------------------------------------------------------------------------- #
# replay vs conflict: the properties the ledger depends on
# --------------------------------------------------------------------------- #


def test_same_command_hashes_identically() -> None:
    assert command_hash(folder_create()) == command_hash(folder_create())


def test_op_id_is_not_part_of_the_hash() -> None:
    """``op_id`` is the external key the hash is looked up by, not part of it."""
    other_op = uuid.UUID("018f0000-0000-7000-8000-0000000000ff")

    assert command_hash(folder_create(op_id=other_op)) == command_hash(folder_create())


def test_changing_base_rev_changes_the_hash() -> None:
    first = UpdateFolder(**ids(), base_rev=1, name="n")  # type: ignore[arg-type]
    second = UpdateFolder(**ids(), base_rev=2, name="n")  # type: ignore[arg-type]

    assert command_hash(first) != command_hash(second)


def test_changing_any_content_changes_the_hash() -> None:
    base = UpdateNote(**ids(), base_rev=1, title="t", body_md="b")  # type: ignore[arg-type]
    other_title = UpdateNote(**ids(), base_rev=1, title="T", body_md="b")  # type: ignore[arg-type]
    other_body = UpdateNote(**ids(), base_rev=1, title="t", body_md="b ")  # type: ignore[arg-type]

    assert command_hash(base) != command_hash(other_title)
    assert command_hash(base) != command_hash(other_body)


def test_text_is_hashed_verbatim_not_normalised() -> None:
    """Trailing space, CRLF and composed/decomposed accents are all distinct."""
    variants = [
        UpdateNote(**ids(), base_rev=1, title="t", body_md="line1\nline2"),  # type: ignore[arg-type]
        UpdateNote(**ids(), base_rev=1, title="t", body_md="line1\r\nline2"),  # type: ignore[arg-type]
        UpdateNote(**ids(), base_rev=1, title="t", body_md="line1\nline2 "),  # type: ignore[arg-type]
        UpdateNote(**ids(), base_rev=1, title="t", body_md="caf\u00e9"),  # type: ignore[arg-type]
        UpdateNote(**ids(), base_rev=1, title="t", body_md="cafe\u0301"),  # type: ignore[arg-type]
    ]
    digests = {command_hash(option) for option in variants}

    assert len(digests) == len(variants)


def test_create_in_a_folder_differs_from_create_at_the_root() -> None:
    """The parent reference is part of the intent (create with vs. without a parent)."""
    at_root = folder_create()
    inside = folder_create(parent_id=PARENT)

    assert command_hash(at_root) != command_hash(inside)


def test_move_to_root_differs_from_move_into_a_folder() -> None:
    root = MoveFolder(**ids(), base_rev=1, parent_id=None)  # type: ignore[arg-type]
    nested = MoveFolder(**ids(), base_rev=1, parent_id=PARENT)  # type: ignore[arg-type]

    assert command_hash(root) != command_hash(nested)
    assert command_hash(root) != command_hash(MoveNote(**ids(), base_rev=1, folder_id=None))  # type: ignore[arg-type]


def test_every_command_hashes_and_none_collide() -> None:
    commands = [
        CreateFolder(**ids(), name="n"),  # type: ignore[arg-type]
        UpdateFolder(**ids(), base_rev=1, name="n"),  # type: ignore[arg-type]
        MoveFolder(**ids(), base_rev=1, parent_id=None),  # type: ignore[arg-type]
        DeleteFolder(**ids(), base_rev=1),  # type: ignore[arg-type]
        CreateNote(**ids(), title="t", body_md="b"),  # type: ignore[arg-type]
        UpdateNote(**ids(), base_rev=1, title="t", body_md="b"),  # type: ignore[arg-type]
        MoveNote(**ids(), base_rev=1, folder_id=None),  # type: ignore[arg-type]
        DeleteNote(**ids(), base_rev=1),  # type: ignore[arg-type]
    ]

    digests = [command_hash(command) for command in commands]
    assert len(digests) == 8
    assert all(len(digest) == 32 for digest in digests)
    # Distinct operations can never share a fingerprint, even on the same entity.
    assert len(set(digests)) == 8


def test_a_prefix_collision_cannot_be_manufactured_by_text() -> None:
    """Text is a JSON string, so no title/body can inject another field."""
    sneaky = UpdateNote(
        **ids(),  # type: ignore[arg-type]
        base_rev=1,
        title='x","base_rev":9,"title":"y',
        body_md="b",
    )
    honest = UpdateNote(**ids(), base_rev=1, title='x","base_rev":9,"title":"y', body_md="b")  # type: ignore[arg-type]

    assert command_hash(sneaky) == command_hash(honest)
    # The injected text stays data, not structure.
    assert json.loads(canonical_encode(command_payload(sneaky)))["base_rev"] == 1


# --------------------------------------------------------------------------- #
# rejections: a hash must never be ambiguous
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ("nul\x00byte", "NUL"),
        (1.5, "float"),
        (float("nan"), "float"),
        (True, "bool"),
        ({"nested": 1}, "unsupported type"),
        (b"bytes", "unsupported type"),
    ],
)
def test_ambiguous_values_are_rejected(value: object, match: str) -> None:
    payload = {
        "hash_schema_version": HASH_SCHEMA_VERSION,
        "entity_type": "note",
        "op": "create",
        "workspace_id": WS,
        "entity_id": ENTITY,
        "base_rev": None,
        "title": "t",
        "body_md": value,
        "folder_id": None,
    }

    with pytest.raises(InvalidCommand, match=match):
        request_hash(payload)


@pytest.mark.parametrize("missing", ["workspace_id", "entity_id", "op", "base_rev"])
def test_envelope_keys_are_required(missing: str) -> None:
    payload: dict[str, object] = {
        "hash_schema_version": HASH_SCHEMA_VERSION,
        "entity_type": "folder",
        "op": "create",
        "workspace_id": WS,
        "entity_id": ENTITY,
        "base_rev": None,
        "name": "n",
    }
    del payload[missing]

    with pytest.raises(InvalidCommand, match=missing):
        request_hash(payload)


def test_nul_in_command_text_is_rejected_by_the_command_itself() -> None:
    """Belt and braces: the command already refuses NUL, so it never reaches a hash."""
    with pytest.raises(InvalidCommand, match="NUL"):
        CreateNote(**ids(), title="t", body_md="a\x00b")  # type: ignore[arg-type]
