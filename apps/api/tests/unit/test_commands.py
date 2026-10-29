"""Unit tests for the eight sync-kernel commands (no database, no I/O).

The commands are the kernel's only input vocabulary, so these tests pin the
structure rules of §5/§6 step 1: mandatory caller-owned ids, ``base_rev`` exactly
when it applies, no content in a move, no parent in a content update, and no NUL
in text. Business rules that need the database (existence, cycles, non-empty
folders) are the service's job and are covered by the PG integration suite.
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any

import pytest

from revnote_api.kernel.commands import (
    ALL_OPS,
    COMMAND_TYPES,
    ENTITY_FOLDER,
    ENTITY_NOTE,
    MAX_BIGINT,
    Command,
    CreateFolder,
    CreateNote,
    DeleteFolder,
    DeleteNote,
    MoveFolder,
    MoveNote,
    UpdateFolder,
    UpdateNote,
    command_for,
)
from revnote_api.kernel.errors import InvalidCommand
from revnote_api.kernel.ids import new_entity_id, new_id, new_op_id, uuid7

WS = uuid.UUID("018f0000-0000-7000-8000-000000000001")
ENTITY = uuid.UUID("018f0000-0000-7000-8000-000000000002")
OP = uuid.UUID("018f0000-0000-7000-8000-000000000003")
PARENT = uuid.UUID("018f0000-0000-7000-8000-000000000004")

#: Envelope fields every command carries (not operation payload).
_ENVELOPE_FIELDS = frozenset(
    {"workspace_id", "entity_id", "op_id", "entity_type", "op", "base_rev"}
)


def ids(**overrides: Any) -> dict[str, Any]:
    """Fresh identity triple, so no test can accidentally share an id."""
    base = {"workspace_id": WS, "entity_id": ENTITY, "op_id": OP}
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# ids helper (D1/D5)
# --------------------------------------------------------------------------- #


def test_uuid7_is_a_stdlib_uuid_version_7() -> None:
    value = uuid7()

    assert isinstance(value, uuid.UUID)
    assert value.version == 7


def test_ids_are_unique_and_carry_a_monotonic_time_prefix() -> None:
    generated = [new_id() for _ in range(64)]

    assert len(set(generated)) == len(generated)
    assert {v.variant for v in generated} == {uuid.RFC_4122}
    # UUIDv7's first 48 bits are a millisecond timestamp: non-decreasing across a
    # burst. This is *ordering*, never the sync cursor (D1).
    prefixes = [int.from_bytes(v.bytes[:6], "big") for v in generated]
    assert prefixes == sorted(prefixes)


def test_entity_and_op_helpers_are_uuid7() -> None:
    for value in (new_entity_id(), new_op_id()):
        assert isinstance(value, uuid.UUID)
        assert value.version == 7


# --------------------------------------------------------------------------- #
# the eight commands
# --------------------------------------------------------------------------- #


def test_all_eight_commands_exist_and_are_commands() -> None:
    assert len(COMMAND_TYPES) == 8
    assert {cls.__name__ for cls in COMMAND_TYPES} == {
        "CreateFolder",
        "UpdateFolder",
        "MoveFolder",
        "DeleteFolder",
        "CreateNote",
        "UpdateNote",
        "MoveNote",
        "DeleteNote",
    }
    for cls in COMMAND_TYPES:
        assert issubclass(cls, Command)
        assert dataclasses.is_dataclass(cls)


def test_command_matrix_matches_the_plan() -> None:
    """Every (entity_type, op) pair maps to exactly one command type."""
    expected = {
        (ENTITY_FOLDER, "create"): CreateFolder,
        (ENTITY_FOLDER, "update"): UpdateFolder,
        (ENTITY_FOLDER, "move"): MoveFolder,
        (ENTITY_FOLDER, "delete"): DeleteFolder,
        (ENTITY_NOTE, "create"): CreateNote,
        (ENTITY_NOTE, "update"): UpdateNote,
        (ENTITY_NOTE, "move"): MoveNote,
        (ENTITY_NOTE, "delete"): DeleteNote,
    }
    for key, cls in expected.items():
        assert command_for(*key) is cls


def test_command_for_rejects_unknown_pairs() -> None:
    with pytest.raises(InvalidCommand, match="no command"):
        command_for(ENTITY_NOTE, "rename")


def test_folder_create_carries_name_and_no_base_rev() -> None:
    command = CreateFolder(**ids(), name="Inbox")

    assert (command.entity_type, command.op) == (ENTITY_FOLDER, "create")
    assert command.base_rev is None
    assert command.name == "Inbox"
    assert command.business_fields() == {"name": "Inbox", "parent_id": None}


def test_folder_create_accepts_a_parent() -> None:
    command = CreateFolder(**ids(), name="Child", parent_id=PARENT)

    assert command.parent_id == PARENT
    assert command.target_parent_id == PARENT


def test_note_create_requires_title_and_allows_empty_body() -> None:
    command = CreateNote(**ids(), title="", body_md="")

    assert command.title == ""
    assert command.body_md == ""
    assert command.business_fields() == {"title": "", "body_md": "", "folder_id": None}


def test_note_create_accepts_a_folder() -> None:
    command = CreateNote(**ids(), title="t", body_md="b", folder_id=PARENT)

    assert command.folder_id == PARENT
    assert command.target_parent_id == PARENT


def test_update_commands_require_base_rev() -> None:
    folder = UpdateFolder(**ids(), base_rev=4, name="Renamed")
    note = UpdateNote(**ids(), base_rev=2, title="t", body_md="body")

    assert folder.base_rev == 4
    assert folder.business_fields() == {"name": "Renamed"}
    assert note.base_rev == 2
    assert note.business_fields() == {"title": "t", "body_md": "body"}


def test_move_commands_carry_only_the_parent() -> None:
    folder = MoveFolder(**ids(), base_rev=1, parent_id=PARENT)
    note = MoveNote(**ids(), base_rev=1, folder_id=None)

    assert folder.business_fields() == {"parent_id": PARENT}
    assert note.business_fields() == {"folder_id": None}
    assert note.target_parent_id is None  # explicitly unfiled


def test_delete_commands_carry_no_business_field() -> None:
    folder = DeleteFolder(**ids(), base_rev=3)
    note = DeleteNote(**ids(), base_rev=1)

    assert folder.business_fields() == {}
    assert note.business_fields() == {}
    assert folder.parent_field == "parent_id"
    assert note.parent_field == "folder_id"


# --------------------------------------------------------------------------- #
# structural rejections (§5/§6 step 1)
# --------------------------------------------------------------------------- #


def test_commands_are_frozen_and_value_comparable() -> None:
    first = CreateFolder(**ids(), name="Inbox")
    same = CreateFolder(**ids(), name="Inbox")

    assert first == same
    assert hash(first) == hash(same)
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.name = "other"  # type: ignore[misc]


def test_command_equality_detects_a_different_payload() -> None:
    assert CreateFolder(**ids(), name="a") != CreateFolder(**ids(), name="b")


@pytest.mark.parametrize("cls", [CreateFolder, UpdateFolder, MoveFolder, DeleteFolder])
@pytest.mark.parametrize("field", ["workspace_id", "entity_id", "op_id"])
def test_folder_commands_require_uuid_identifiers(cls: type[Command], field: str) -> None:
    kwargs: dict[str, Any] = {
        "workspace_id": WS,
        "entity_id": ENTITY,
        "op_id": OP,
        "name": "x",
        "base_rev": 1,
        "parent_id": None,
    }
    kwargs[field] = str(kwargs[field])

    with pytest.raises(InvalidCommand, match=field):
        cls(**{k: v for k, v in kwargs.items() if k in _accepted(cls)})


def _accepted(cls: type[Command]) -> set[str]:
    return {f.name for f in dataclasses.fields(cls) if f.init}


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"name": "   "}, "must not be blank"),
        ({"name": ""}, "must not be blank"),
        ({"name": "\t\n"}, "must not be blank"),
        ({"name": "a\x00b"}, "NUL"),
        ({"name": 5}, "must be a str"),
        ({"name": None}, "requires name"),
        ({"name": "ok", "base_rev": 2}, "must not carry base_rev"),
    ],
)
def test_invalid_creates_are_rejected(kwargs: dict[str, Any], match: str) -> None:
    with pytest.raises(InvalidCommand, match=match):
        CreateFolder(**ids(), **kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": "leak"},
        {"body_md": "leak"},
        {"folder_id": PARENT},
    ],
)
def test_folder_commands_reject_note_fields(kwargs: dict[str, Any]) -> None:
    """A folder command has no note field at all — Python rejects it outright."""
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        CreateFolder(**ids(), name="ok", **kwargs)


@pytest.mark.parametrize("base_rev", [0, -1, 0.5, True, "2"])
def test_base_rev_must_be_a_positive_int(base_rev: Any) -> None:
    with pytest.raises(InvalidCommand, match="base_rev"):
        DeleteNote(**ids(), base_rev=base_rev)


def test_base_rev_above_bigint_is_rejected() -> None:
    with pytest.raises(InvalidCommand, match="BIGINT"):
        DeleteNote(**ids(), base_rev=MAX_BIGINT + 1)


def test_update_without_base_rev_is_rejected() -> None:
    """``base_rev`` is inherited with a ``None`` default, so it is policed here."""
    with pytest.raises(InvalidCommand, match="requires base_rev"):
        UpdateFolder(**ids(), name="x")
    with pytest.raises(InvalidCommand, match="requires base_rev"):
        DeleteNote(**ids())


def test_content_update_never_carries_a_parent() -> None:
    """A rename has no parent field, so an implicit move is not expressible."""
    for field_name in ("parent_id", "folder_id"):
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            UpdateFolder(**ids(), base_rev=1, name="x", **{field_name: PARENT})
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        UpdateNote(**ids(), base_rev=1, title="t", body_md="b", folder_id=PARENT)


def test_note_update_requires_both_content_fields() -> None:
    with pytest.raises(TypeError, match="body_md"):
        UpdateNote(**ids(), base_rev=1, title="t")  # type: ignore[call-arg]
    with pytest.raises(TypeError, match="title"):
        UpdateNote(**ids(), base_rev=1, body_md="b")  # type: ignore[call-arg]


@pytest.mark.parametrize("value", [None, 5, b"bytes", ["list"]])
def test_note_content_must_be_text(value: Any) -> None:
    with pytest.raises(InvalidCommand, match=r"title|body_md"):
        CreateNote(**ids(), title=value, body_md="b")


def test_empty_note_content_is_legal() -> None:
    command = CreateNote(**ids(), title="", body_md="")

    assert command.title == "" and command.body_md == ""


def test_move_never_carries_content() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        MoveFolder(**ids(), base_rev=1, parent_id=None, name="sneaky")
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        MoveNote(**ids(), base_rev=1, folder_id=None, body_md="body")


def test_move_requires_an_explicit_parent_argument() -> None:
    """``None`` is a meaningful target (root/unfiled), so it must be passed."""
    with pytest.raises(TypeError):
        MoveFolder(**ids(), base_rev=1)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        MoveNote(**ids(), base_rev=1)  # type: ignore[call-arg]


def test_delete_rejects_business_fields() -> None:
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        DeleteFolder(**ids(), base_rev=1, name="x")
    with pytest.raises(TypeError, match="unexpected keyword argument"):
        DeleteNote(**ids(), base_rev=1, title="x")


def test_self_parent_is_rejected_before_the_database() -> None:
    with pytest.raises(InvalidCommand, match="own parent"):
        MoveFolder(workspace_id=WS, entity_id=ENTITY, op_id=OP, base_rev=1, parent_id=ENTITY)
    with pytest.raises(InvalidCommand, match="own parent"):
        CreateFolder(workspace_id=WS, entity_id=ENTITY, op_id=OP, name="x", parent_id=ENTITY)


def test_note_folder_id_equal_to_its_own_id_is_not_a_cycle() -> None:
    """A note and a folder with the same UUID are two unrelated rows, not a loop.

    ``folder_id`` references ``folders`` while ``entity_id`` is the note's own key, so
    equal ids are a coincidence, never ancestry. The command must accept it and let the
    service answer ``InvalidParent`` when no such folder exists.
    """
    created = CreateNote(
        workspace_id=WS, entity_id=ENTITY, op_id=OP, title="t", body_md="b", folder_id=ENTITY
    )
    moved = MoveNote(workspace_id=WS, entity_id=ENTITY, op_id=OP, base_rev=1, folder_id=ENTITY)

    assert created.folder_id == created.entity_id
    assert moved.target_parent_id == ENTITY


def test_unknown_op_or_entity_type_is_rejected() -> None:
    with pytest.raises(InvalidCommand, match="unknown op"):
        Command(**ids(), entity_type=ENTITY_FOLDER, op="rename", base_rev=1)
    with pytest.raises(InvalidCommand, match="unknown entity_type"):
        Command(**ids(), entity_type="workspace", op="create")


def test_ops_are_all_exposed() -> None:
    assert set(ALL_OPS) == {"create", "update", "move", "delete"}
    assert len(COMMAND_TYPES) == 8
    assert {cls.__dataclass_fields__["entity_type"].default for cls in COMMAND_TYPES} == {
        ENTITY_FOLDER,
        ENTITY_NOTE,
    }


def test_repr_hides_note_body_content() -> None:
    """Diagnostics must never leak content into logs (§11)."""
    secret = "TOP-SECRET-NOTE-BODY"
    command = UpdateNote(**ids(), base_rev=1, title="t", body_md=secret)

    assert secret not in repr(command)
    assert f"body_chars={len(secret)}" in repr(command)


def test_every_payload_field_is_excluded_from_generated_repr() -> None:
    """Belt and braces: the dataclass ``__repr__`` must be off for fields *and* classes.

    The hand-written ``Command.__repr__`` is what runs, but a future subclass that
    forgets ``repr=False`` would silently get the generated one, which dumps
    ``body_md`` verbatim. Both switches are therefore asserted structurally.
    """
    for cls in COMMAND_TYPES:
        assert cls.__dataclass_params__.repr is False, cls.__name__
        payload = [f.name for f in dataclasses.fields(cls) if f.name not in _ENVELOPE_FIELDS]
        assert all(not f.repr for f in dataclasses.fields(cls) if f.name in payload), cls.__name__


def test_str_and_format_also_go_through_the_safe_repr() -> None:
    secret = "SECRET-BODY"
    command = CreateNote(**ids(), title="t", body_md=secret)

    # f-string, str(), logging's %-style formatting and format() all use __repr__.
    rendered = [f"{command}", str(command), f"{command!r}", format(command)]
    assert all(secret not in text for text in rendered)
    assert all("body_chars=" in text for text in rendered)
