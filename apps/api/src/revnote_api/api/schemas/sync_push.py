"""Wire schema of ``POST /workspaces/{workspace_id}/sync/push`` (spec §3, D9-D12).

One batch in, an ordered array of per-op results out. The schema is where a bad
batch is rejected **before any side effect** (plan §11.2): FastAPI parses and
validates the whole body into these models first, so a malformed op at index 7
never lets ops 0-6 run.

What this module guarantees
---------------------------
* **Exactly eight command shapes.** ``entity_type`` x ``op`` selects one payload
  model, so ``{"op": "delete", "payload": {"name": ...}}`` is a contract error
  rather than a silently ignored field. ``extra="forbid"`` closes the envelope
  *and* the payload.
* **The same structural rules the kernel enforces**, checked here so a bad op is a
  400 for the whole batch instead of a per-op ``rejected`` after earlier ops already
  committed: NUL-free ``name``/``title``/``body_md``, non-blank folder names, a
  folder that is not its own parent, ``base_rev`` present exactly on the mutating
  ops, and a ``base_rev`` bounded to 19 digits *before* any ``int()`` call.
* **``base_rev`` is a positive decimal string**, never a JSON number; ``"0"``,
  ``"007"``, signs, exponents and whitespace are refused. 19 digits is the BIGINT
  ceiling with no room for an unbounded integer conversion.
* **The workspace is not in the body.** It is the path parameter and is
  authoritative (spec §3); an op naming a workspace fails validation because the
  envelope forbids unknown fields.
* **No content in diagnostics.** Every model inherits :class:`_WireModel`, whose
  ``__repr__``/``__str__`` render *field names and types only* - pydantic's default
  ``str()`` is a per-field dump that would print ``body_md`` verbatim, and it is what
  a log line or an unhandled-exception formatter would pick up.

Counts and byte budgets are **not** enforced here: ``1..50`` ops and the 256 KiB
``body_md`` cap are configuration (``SYNC_PUSH_MAX_OPS`` / ``SYNC_NOTE_MAX_BYTES``)
read from settings by the route.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from revnote_api.api.schemas.common import BIGINT_MAX, ErrorBody

__all__ = [
    "ENTITY_FOLDER",
    "ENTITY_NOTE",
    "PAYLOAD_MODELS",
    "PushOp",
    "PushRequest",
    "PushResponse",
    "PushResultItem",
]

ENTITY_FOLDER: Final[str] = "folder"
ENTITY_NOTE: Final[str] = "note"

EntityType = Literal["folder", "note"]
OpName = Literal["create", "update", "move", "delete"]

#: ASCII digits only. ``str.isdigit()`` also accepts Arabic-Indic numerals, which
#: ``int()`` converts happily; that must never reach a revision comparison.
_DIGITS: Final[frozenset[str]] = frozenset("0123456789")

#: Longest legal ``base_rev`` text: 19 digits is the BIGINT ceiling.
MAX_REVISION_DIGITS: Final[int] = 19

#: Nothing rendered by :meth:`_WireModel.__repr__` may be longer than this. A field
#: name is code, not client data, so this is a paranoia bound, not a policy.
_MAX_NAME: Final[int] = 64


class _WireModel(BaseModel):
    """Strict, closed wire model whose diagnostics never carry values.

    ``extra="forbid"`` is the contract. The ``__repr__``/``__str__`` overrides are the
    leak guard: pydantic renders values by default, and both a log line and a
    ``ValidationError`` formatter reach for ``str()``. Nothing here may print a note
    title, a body, or a name a client chose, so only field *names* and their types
    survive. Inherited by every model below (including the request wrapper, whose
    ``str()`` would otherwise recurse into the ops).
    """

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        validate_default=True,
    )

    def __repr__(self) -> str:
        return self._safe_render()

    def __str__(self) -> str:
        return self._safe_render()

    def _safe_render(self) -> str:
        """``Type(field: Type, ...)`` - shape only, bounded, never a value."""
        parts = [
            f"{name[:_MAX_NAME]}: {type(getattr(self, name, None)).__name__}"
            for name in type(self).model_fields
        ]
        return f"{type(self).__name__}({', '.join(parts)})"


def _reject_nul(value: str, field: str) -> str:
    """Refuse U+0000: PostgreSQL cannot store it, and the kernel refuses it too."""
    if "\x00" in value:
        raise ValueError(f"{field}_contains_nul")
    return value


def _validate_positive_revision(value: str) -> str:
    """``base_rev``: ASCII digits, no leading zero, at most 19 digits.

    The length check comes **first** so an attacker cannot hand us a 4,000-digit
    string and force a quadratic ``int()`` conversion; only after the bound is a
    numeric comparison safe.
    """
    if not value or len(value) > MAX_REVISION_DIGITS:
        raise ValueError("revision_out_of_range")
    if any(char not in _DIGITS for char in value):
        raise ValueError("revision_required")
    if value[0] == "0":
        # ``"0"`` and ``"007"`` are not revisions: the kernel starts at 1, and a
        # leading zero would let two clients spell one revision two ways.
        raise ValueError("revision_required")
    if int(value) > BIGINT_MAX:
        raise ValueError("revision_out_of_range")
    return value


#: A revision on the wire: ``"1"`` ... up to ``BIGINT_MAX`` (19 digits max).
PositiveDecimalString = Annotated[
    str,
    Field(
        description=(
            "Revision being replaced, as a positive decimal string (no sign, no "
            "leading zero, no exponent); max 9223372036854775807."
        ),
        pattern=r"^[1-9][0-9]*$",
        max_length=MAX_REVISION_DIGITS,
        examples=["1", "7"],
    ),
    AfterValidator(_validate_positive_revision),
]


def _folder_name(value: str) -> str:
    """A folder name: not blank (the kernel refuses it) and NUL-free."""
    _reject_nul(value, "name")
    if not value.strip():
        raise ValueError("name_must_not_be_blank")
    return value


def _note_title(value: str) -> str:
    """A note title: empty is legal content, NUL is not."""
    return _reject_nul(value, "title")


def _note_body(value: str) -> str:
    """A note body: empty is legal content, NUL is not."""
    return _reject_nul(value, "body_md")


Name = Annotated[
    str,
    Field(description="Folder name; not blank, no NUL.", min_length=1, repr=False),
    AfterValidator(_folder_name),
]
Title = Annotated[
    str,
    Field(description="Note title; may be empty, no NUL.", repr=False),
    AfterValidator(_note_title),
]
BodyMarkdown = Annotated[
    str,
    Field(description="Note body in Markdown; no NUL.", repr=False),
    AfterValidator(_note_body),
]
_OptionalParent = Annotated[
    uuid.UUID | None,
    Field(description="Parent folder id, or null for the root / unfiled.", repr=False),
]


class FolderCreatePayload(_WireModel):
    """``folder/create``: optional parent, defaults to the root."""

    name: Name
    parent_id: _OptionalParent = None


class FolderUpdatePayload(_WireModel):
    """``folder/update``: rename only. A move is a separate op."""

    name: Name


class FolderMovePayload(_WireModel):
    """``folder/move``: ``parent_id`` is required and may be null (root)."""

    parent_id: _OptionalParent


class FolderDeletePayload(_WireModel):
    """``folder/delete``: empty. A delete never cascades."""


class NoteCreatePayload(_WireModel):
    """``note/create``: optional folder, defaults to unfiled."""

    title: Title
    body_md: BodyMarkdown
    folder_id: _OptionalParent = None


class NoteUpdatePayload(_WireModel):
    """``note/update``: replace content only. A move is a separate op."""

    title: Title
    body_md: BodyMarkdown


class NoteMovePayload(_WireModel):
    """``note/move``: ``folder_id`` is required and may be null (unfiled)."""

    folder_id: _OptionalParent


class NoteDeletePayload(_WireModel):
    """``note/delete``: empty. Tombstones the row, keeps it."""


#: ``(entity_type, op) -> payload model``. The one lookup the validator and the
#: adapter share, so the eight wire shapes cannot drift from the kernel's eight.
PAYLOAD_MODELS: Final[dict[tuple[str, str], type[_WireModel]]] = {
    (ENTITY_FOLDER, "create"): FolderCreatePayload,
    (ENTITY_FOLDER, "update"): FolderUpdatePayload,
    (ENTITY_FOLDER, "move"): FolderMovePayload,
    (ENTITY_FOLDER, "delete"): FolderDeletePayload,
    (ENTITY_NOTE, "create"): NoteCreatePayload,
    (ENTITY_NOTE, "update"): NoteUpdatePayload,
    (ENTITY_NOTE, "move"): NoteMovePayload,
    (ENTITY_NOTE, "delete"): NoteDeletePayload,
}

Payload = (
    FolderCreatePayload
    | FolderUpdatePayload
    | FolderMovePayload
    | FolderDeletePayload
    | NoteCreatePayload
    | NoteUpdatePayload
    | NoteMovePayload
    | NoteDeletePayload
)


class PushOp(_WireModel):
    """One operation of a push batch: identity envelope + the payload its op needs.

    ``op_id``/``entity_id`` are client-owned UUIDs stored canonically, so a duplicate
    spelled differently still collides when the route checks the batch (D12).
    """

    op_id: Annotated[
        uuid.UUID,
        Field(description="Client-owned idempotency key, reused verbatim on retry."),
    ]
    entity_type: EntityType
    op: OpName
    entity_id: Annotated[uuid.UUID, Field(description="Id of the object this op targets.")]
    base_rev: PositiveDecimalString | None = None
    payload: Annotated[Payload, Field(description="Op-specific fields.")]

    @model_validator(mode="before")
    @classmethod
    def _select_payload(cls, data: Any) -> Any:
        """Bind ``payload`` to the model its ``(entity_type, op)`` declares.

        A non-string/absent discriminator is left alone so ordinary field validation
        reports it: an earlier version called ``PAYLOAD_MODELS.get()`` directly, and
        an unhashable value (a list or a dict in ``entity_type``) raised
        ``TypeError`` - a 500 for input that deserves a 400.
        """
        if not isinstance(data, dict):
            return data
        entity_type = data.get("entity_type")
        op = data.get("op")
        if not isinstance(entity_type, str) or not isinstance(op, str):
            return data  # let the declared Literal fields produce the 400
        model = PAYLOAD_MODELS.get((entity_type, op))
        if model is None:
            # Unknown pair: keep the raw payload so the discriminator is reported.
            return data
        raw = data.get("payload", {})
        if isinstance(raw, dict):
            return {**data, "payload": model.model_validate(raw)}
        return data

    @model_validator(mode="after")
    def _base_rev_matches_the_op(self) -> PushOp:
        """``create`` carries no ``base_rev``; every other op requires a positive one."""
        if self.op == "create":
            if self.base_rev is not None:
                raise ValueError("create_must_not_carry_base_rev")
        elif self.base_rev is None:
            raise ValueError(f"{self.op}_requires_base_rev")
        return self

    @model_validator(mode="after")
    def _folder_is_not_its_own_parent(self) -> PushOp:
        """The kernel refuses a self-parent folder; the batch must fail before op 1."""
        if (
            self.entity_type == ENTITY_FOLDER
            and isinstance(self.payload, (FolderCreatePayload, FolderMovePayload))
            and self.payload.parent_id == self.entity_id
        ):
            raise ValueError("folder_cannot_be_its_own_parent")
        return self

    def payload_fields(self) -> dict[str, Any]:
        """The payload as a plain dict (adapter input; never logged or rendered)."""
        return dict(self.payload.model_dump())


class PushRequest(_WireModel):
    """``{"ops": [...]}`` - the whole batch, validated before anything runs."""

    ops: Annotated[
        list[PushOp],
        Field(
            description="Operations in execution order; each runs in its own transaction.",
            min_length=1,
            repr=False,
        ),
    ]


class PushResultItem(_WireModel):
    """One per-op outcome in the spec's own error shape (spec §3).

    A failure carries a full ``error`` object - the same
    ``{code, message, request_id, retryable, details}`` the envelope uses - rather
    than a bare code, so a client has one error shape to parse for the whole API.
    ``seq``/``rev`` are present only for the two success statuses.
    """

    op_id: Annotated[uuid.UUID, Field(description="Echo of the request's op_id.")]
    status: Annotated[
        Literal["applied", "replayed", "conflict", "rejected", "unknown", "not_attempted"],
        Field(description="Outcome of this op; see the push protocol table."),
    ]
    seq: Annotated[
        str | None,
        Field(default=None, description="Allocated cursor, decimal string; success only."),
    ] = None
    rev: Annotated[
        str | None,
        Field(default=None, description="Revision after this op; success only."),
    ] = None
    error: Annotated[
        ErrorBody | None,
        Field(
            default=None,
            description="Failure details in the shared envelope shape; success only omits it.",
        ),
    ] = None


class PushResponse(_WireModel):
    """``{"results": [...]}`` - always ``200`` when the batch was well formed.

    A per-op failure is *data*, not a transport error: the batch stops or continues
    by rule (D10) while the HTTP status stays 200. Only a malformed request
    (400/413) or an unauthenticated caller (401/404) uses the top-level envelope.
    """

    results: Annotated[
        list[PushResultItem],
        Field(description="One entry per submitted op, in request order."),
    ]
