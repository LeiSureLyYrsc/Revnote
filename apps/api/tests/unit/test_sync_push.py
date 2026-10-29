"""Focus tests for the push endpoint: schema contract, adapter loop, route glue.

Three layers, one file, because the interesting failures are *seams*: a schema that
accepts a ninth shape, an adapter that keeps going after ``WriteBusy``, a route that
writes before rejecting a duplicate ``op_id``, or a payload field leaking into a
``repr``/log. Each layer gets its own section; the route section drives the real
FastAPI app with a stubbed kernel writer, so the HTTP behaviour under test is the
shipped one.

What is deliberately **not** here: PostgreSQL. Lock/statement timeouts and the
applied-versus-replayed distinction against a real server live in
``tests/integration/kernel``; these tests prove ordering, stop rules, mapping and
request rejection, which need no database.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

import revnote_api.api.schemas.sync_push as schema_module
import revnote_api.services.sync_push_adapter as adapter_module
from revnote_api.api.routes import sync_push as route_module
from revnote_api.api.schemas.sync_push import (
    PAYLOAD_MODELS,
    FolderCreatePayload,
    FolderDeletePayload,
    FolderMovePayload,
    FolderUpdatePayload,
    NoteCreatePayload,
    NoteDeletePayload,
    NoteMovePayload,
    NoteUpdatePayload,
    PushOp,
    PushRequest,
    PushResponse,
    PushResultItem,
)
from revnote_api.config import Settings
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
from revnote_api.kernel.errors import (
    CommitOutcomeUnknown,
    EntityIdConflict,
    FolderNotEmpty,
    OpIdConflict,
    RevConflict,
    StorageFailure,
    WriteBusy,
)
from revnote_api.kernel.results import ExecutedWrite, WriteResult
from revnote_api.services.sync_push_adapter import (
    BUSY_CODE,
    CONFLICT_CODE,
    STATUS_APPLIED,
    STATUS_CONFLICT,
    STATUS_NOT_ATTEMPTED,
    STATUS_REJECTED,
    STATUS_REPLAYED,
    STATUS_UNKNOWN,
    OpResult,
    PushBatch,
    PushOpSpec,
    PushOutcome,
    PushService,
    build_command,
    code_for_kernel_error,
    prepare_batch,
)

# --------------------------------------------------------------------------- #
# wire helpers
# --------------------------------------------------------------------------- #

WS = uuid.UUID("01900000-0000-7000-8000-0000000000aa")


def an_op(**overrides: Any) -> dict[str, Any]:
    """A valid ``note/create`` op as raw JSON; override any envelope field."""
    base: dict[str, Any] = {
        "op_id": str(uuid.uuid4()),
        "entity_type": "note",
        "op": "create",
        "entity_id": str(uuid.uuid4()),
        "payload": {"title": "t", "body_md": "b"},
    }
    base.update(overrides)
    return base


def a_spec(**overrides: Any) -> PushOpSpec:
    """The framework-free equivalent of :func:`an_op`, for adapter-level tests."""
    base: dict[str, Any] = {
        "op_id": uuid.uuid4(),
        "entity_type": "folder",
        "op": "create",
        "entity_id": uuid.uuid4(),
        "base_rev": None,
        "payload": {"name": "Inbox"},
    }
    base.update(overrides)
    return PushOpSpec(**base)


def a_batch(*specs: PushOpSpec, **overrides: Any) -> PushBatch:
    """A batch with its commands already built, exactly as the route does it."""
    resolved = specs or (a_spec(),)
    built = prepare_batch(WS, resolved)
    assert built is not None, "the fixture builds valid specs only"
    base: dict[str, Any] = {
        "workspace_id": WS,
        "ops": resolved,
        "commands": built,
        "lock_timeout_ms": 5_000,
        "statement_timeout_ms": 10_000,
        "budget_ms": 30_000,
    }
    base.update(overrides)
    return PushBatch(**base)


def a_result(spec: PushOpSpec, *, seq: int = 1, rev: int = 1) -> WriteResult:
    return WriteResult(
        workspace_id=WS,
        op_id=spec.op_id,
        entity_type=spec.entity_type,
        entity_id=spec.entity_id,
        op=spec.op,
        seq=seq,
        rev=rev,
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )


class FakeWriter:
    """Stands in for ``SyncWriteService``: replays a scripted outcome per op.

    Counts calls and records the budgets it was handed, which is what proves the
    adapter passes the injected timeouts through instead of the module defaults.
    """

    def __init__(self, script: dict[uuid.UUID, Any] | None = None) -> None:
        self.script = script or {}
        self.calls: list[Any] = []
        self.budgets: list[tuple[Any, Any]] = []

    async def execute_with_outcome(
        self,
        command: Any,
        *,
        lock_timeout_ms: int | None = None,
        statement_timeout_ms: int | None = None,
    ) -> ExecutedWrite:
        self.calls.append(command)
        self.budgets.append((lock_timeout_ms, statement_timeout_ms))
        outcome = self.script.get(command.op_id, ("applied", 1, 1))
        if isinstance(outcome, BaseException):
            raise outcome
        disposition, seq, rev = outcome
        return ExecutedWrite(
            result=a_result(
                a_spec(op_id=command.op_id, entity_id=command.entity_id), seq=seq, rev=rev
            ),
            disposition=disposition,
        )


async def _no_member(workspace: uuid.UUID, user_id: uuid.UUID) -> None:
    return None


# --------------------------------------------------------------------------- #
# schema: exactly eight shapes, closed everywhere
# --------------------------------------------------------------------------- #


def test_the_schema_declares_exactly_the_eight_kernel_shapes() -> None:
    """Two entity types x four ops; no ninth shape and no missing pair."""
    assert set(PAYLOAD_MODELS) == {
        ("folder", "create"),
        ("folder", "update"),
        ("folder", "move"),
        ("folder", "delete"),
        ("note", "create"),
        ("note", "update"),
        ("note", "move"),
        ("note", "delete"),
    }
    assert set(PAYLOAD_MODELS.values()) == {
        FolderCreatePayload,
        FolderUpdatePayload,
        FolderMovePayload,
        FolderDeletePayload,
        NoteCreatePayload,
        NoteUpdatePayload,
        NoteMovePayload,
        NoteDeletePayload,
    }


def test_the_schema_and_the_kernel_declare_the_same_eight_commands() -> None:
    """A shape the wire accepts must exist in the kernel, and vice versa."""
    from revnote_api.kernel.commands import COMMAND_TYPES

    kernel_pairs = {
        (cls.__dataclass_fields__["entity_type"].default, cls.__dataclass_fields__["op"].default)
        for cls in COMMAND_TYPES
    }
    assert set(PAYLOAD_MODELS) == kernel_pairs


@pytest.mark.parametrize(
    ("entity_type", "op", "payload"),
    [
        ("folder", "create", {"name": "x"}),
        ("folder", "update", {"name": "x"}),
        ("folder", "move", {"parent_id": None}),
        ("folder", "delete", {}),
        ("note", "create", {"title": "t", "body_md": "b"}),
        ("note", "update", {"title": "t", "body_md": "b"}),
        ("note", "move", {"folder_id": None}),
        ("note", "delete", {}),
    ],
)
def test_every_declared_shape_validates(entity_type: str, op: str, payload: dict[str, Any]) -> None:
    envelope: dict[str, Any] = {
        "op_id": str(uuid.uuid4()),
        "entity_type": entity_type,
        "op": op,
        "entity_id": str(uuid.uuid4()),
        "payload": payload,
    }
    if op != "create":
        envelope["base_rev"] = "1"

    parsed = PushOp.model_validate(envelope)

    assert (parsed.entity_type, parsed.op) == (entity_type, op)
    assert type(parsed.payload) is PAYLOAD_MODELS[(entity_type, op)]


@pytest.mark.parametrize(
    ("entity_type", "op", "payload"),
    [
        ("folder", "delete", {"name": "x"}),
        ("folder", "update", {"parent_id": None}),
        ("note", "delete", {"title": "t"}),
        ("note", "move", {"title": "t", "folder_id": None}),
        ("note", "create", {"title": "t", "body_md": "b", "folder": None}),
    ],
)
def test_a_payload_field_of_another_op_is_refused(
    entity_type: str, op: str, payload: dict[str, Any]
) -> None:
    """``extra="forbid"`` per shape: no op silently ignores a field it cannot use."""
    envelope: dict[str, Any] = {
        "op_id": str(uuid.uuid4()),
        "entity_type": entity_type,
        "op": op,
        "entity_id": str(uuid.uuid4()),
        "payload": payload,
    }
    if op != "create":
        envelope["base_rev"] = "1"

    with pytest.raises(ValidationError) as caught:
        PushOp.model_validate(envelope)

    assert any(error["type"] == "extra_forbidden" for error in caught.value.errors())


def test_the_body_cannot_declare_a_workspace() -> None:
    """The path is authoritative: an op naming a workspace is a contract error."""
    with pytest.raises(ValidationError) as caught:
        PushOp.model_validate({**an_op(), "workspace_id": str(uuid.uuid4())})

    assert any(error["type"] == "extra_forbidden" for error in caught.value.errors())


@pytest.mark.parametrize("base_rev", [2, 2.0, True, None])
@pytest.mark.parametrize("op", ["update", "move", "delete"])
def test_base_rev_must_be_a_decimal_string(op: str, base_rev: Any) -> None:
    """A JSON number, a bool or a missing value is refused for every mutating op."""
    payload = {"title": "t", "body_md": "b"} if op == "update" else {}
    body = {
        "op_id": str(uuid.uuid4()),
        "entity_type": "note" if op != "move" else "folder",
        "op": op,
        "entity_id": str(uuid.uuid4()),
        "base_rev": base_rev,
        "payload": payload if op != "move" else {"parent_id": None},
    }

    with pytest.raises(ValidationError):
        PushOp.model_validate(body)


@pytest.mark.parametrize("base_rev", ["0", "007", "-1", "1e3", " 1", "1.0", ""])
def test_a_non_canonical_revision_is_refused(base_rev: str) -> None:
    body = {
        "op_id": str(uuid.uuid4()),
        "entity_type": "folder",
        "op": "update",
        "entity_id": str(uuid.uuid4()),
        "base_rev": base_rev,
        "payload": {"name": "x"},
    }

    with pytest.raises(ValidationError):
        PushOp.model_validate(body)


def test_a_create_must_not_carry_a_revision() -> None:
    """The kernel refuses it; the wire says so first so the error names the shape."""
    with pytest.raises(ValidationError, match="create_must_not_carry_base_rev"):
        PushOp.model_validate({**an_op(), "base_rev": "1"})


def test_an_empty_batch_is_refused_by_the_schema() -> None:
    with pytest.raises(ValidationError):
        PushRequest.model_validate({"ops": []})


def test_a_batch_with_an_unknown_envelope_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        PushRequest.model_validate({"ops": [an_op()], "workspace_id": str(uuid.uuid4())})


def test_a_push_op_repr_never_prints_the_body() -> None:
    """The schema's own repr is a leak surface: title and body must not appear."""
    secret = "SECRET-BODY-TEXT"
    op = PushOp.model_validate(an_op(payload={"title": "SECRET-TITLE", "body_md": secret}))

    rendered = repr(op) + str(op)

    assert secret not in rendered
    assert "SECRET-TITLE" not in rendered
    assert op.op_id is not None


# --------------------------------------------------------------------------- #
# adapter: mapping, ordering, stop rules
# --------------------------------------------------------------------------- #


def test_kernel_errors_map_to_the_documented_codes() -> None:
    ws, entity, op_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    cases: list[tuple[BaseException, str]] = [
        (EntityIdConflict("note", entity), CONFLICT_CODE),
        (OpIdConflict(ws, op_id), CONFLICT_CODE),
        (RevConflict("note", entity, expected=1, current=2), "rev_conflict"),
        (WriteBusy("busy"), BUSY_CODE),
        (CommitOutcomeUnknown(op_id), "outcome_unknown"),
        (StorageFailure("broken"), "storage_failure"),
        (FolderNotEmpty(entity), "folder_not_empty"),
    ]

    for exc, code in cases:
        assert code_for_kernel_error(exc) == code, type(exc).__name__


def test_the_two_id_conflict_classes_are_indistinguishable() -> None:
    """P2 #3: the response must not reveal whether an id exists elsewhere."""
    entity, op_id = uuid.uuid4(), uuid.uuid4()
    entity_conflict = code_for_kernel_error(EntityIdConflict("note", entity))
    op_conflict = code_for_kernel_error(OpIdConflict(uuid.uuid4(), op_id))

    assert entity_conflict == op_conflict == CONFLICT_CODE


def test_build_command_uses_the_path_workspace_even_if_the_payload_lies() -> None:
    spec = a_spec(
        payload={"name": "x", "workspace_id": str(uuid.uuid4()), "parent_id": None},
        entity_type="folder",
        op="create",
    )

    command = build_command(WS, spec)

    assert isinstance(command, CreateFolder)
    assert command.workspace_id == WS


def test_build_command_maps_each_shape_to_its_kernel_class() -> None:
    pairs = {
        ("folder", "create"): (CreateFolder, {"name": "n"}),
        ("folder", "update"): (UpdateFolder, {"name": "n"}),
        ("folder", "move"): (MoveFolder, {"parent_id": None}),
        ("folder", "delete"): (DeleteFolder, {}),
        ("note", "create"): (CreateNote, {"title": "t", "body_md": "b"}),
        ("note", "update"): (UpdateNote, {"title": "t", "body_md": "b"}),
        ("note", "move"): (MoveNote, {"folder_id": None}),
        ("note", "delete"): (DeleteNote, {}),
    }

    for (entity_type, op), (expected, payload) in pairs.items():
        spec = a_spec(
            entity_type=entity_type,
            op=op,
            base_rev=None if op == "create" else 3,
            payload=payload,
        )
        command = build_command(WS, spec)
        assert type(command) is expected, (entity_type, op, type(command))
        assert command.op_id == spec.op_id
        assert command.base_rev == (None if op == "create" else 3)


def test_an_unbuildable_payload_becomes_a_rejected_result_not_an_exception() -> None:
    """A malformed op inside a well-formed batch is data, so the batch can continue."""
    spec = a_spec(op="delete", base_rev=1, payload={"name": "surprise"})

    result = build_command(WS, spec)

    assert isinstance(result, OpResult)
    assert (result.status, result.code) == (STATUS_REJECTED, "invalid_request")


@pytest.mark.anyio
async def test_ops_run_in_array_order_with_one_call_each() -> None:
    specs = tuple(a_spec() for _ in range(4))
    writer = FakeWriter()
    service = PushService(writer)  # type: ignore[arg-type]

    outcome = await service.run(a_batch(*specs), user_id=uuid.uuid4(), membership=_no_member)

    assert [call.op_id for call in writer.calls] == [spec.op_id for spec in specs]
    assert [result.status for result in outcome.results] == [STATUS_APPLIED] * 4


@pytest.mark.anyio
async def test_the_injected_budgets_are_passed_to_the_kernel() -> None:
    writer = FakeWriter()
    service = PushService(writer)  # type: ignore[arg-type]

    await service.run(
        a_batch(a_spec(), lock_timeout_ms=250, statement_timeout_ms=900),
        user_id=uuid.uuid4(),
        membership=_no_member,
    )

    assert writer.budgets == [(250, 900)]


@pytest.mark.anyio
async def test_a_replay_is_reported_as_replayed_with_its_original_coordinates() -> None:
    spec = a_spec()
    writer = FakeWriter({spec.op_id: ("replayed", 4, 2)})
    service = PushService(writer)  # type: ignore[arg-type]

    outcome = await service.run(a_batch(spec), user_id=uuid.uuid4(), membership=_no_member)

    assert outcome.results[0].status == STATUS_REPLAYED
    assert (outcome.results[0].seq, outcome.results[0].rev) == (4, 2)
    assert outcome.results[0].code is None


@pytest.mark.anyio
async def test_a_conflict_continues_the_batch() -> None:
    first, second = a_spec(), a_spec()
    writer = FakeWriter(
        {first.op_id: RevConflict("folder", first.entity_id, expected=1, current=2)}
    )
    service = PushService(writer)  # type: ignore[arg-type]

    outcome = await service.run(a_batch(first, second), user_id=uuid.uuid4(), membership=_no_member)

    assert [result.status for result in outcome.results] == [STATUS_CONFLICT, STATUS_APPLIED]
    assert outcome.results[0].code == "rev_conflict"
    assert len(writer.calls) == 2


@pytest.mark.anyio
async def test_a_business_rejection_continues_the_batch() -> None:
    first, second = a_spec(), a_spec()
    writer = FakeWriter({first.op_id: FolderNotEmpty(first.entity_id)})
    service = PushService(writer)  # type: ignore[arg-type]

    outcome = await service.run(a_batch(first, second), user_id=uuid.uuid4(), membership=_no_member)

    assert [result.status for result in outcome.results] == [STATUS_REJECTED, STATUS_APPLIED]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (WriteBusy("busy"), STATUS_REJECTED, BUSY_CODE),
        (CommitOutcomeUnknown(uuid.uuid4()), STATUS_UNKNOWN, "outcome_unknown"),
        (StorageFailure("boom"), STATUS_REJECTED, "storage_failure"),
    ],
)
async def test_a_transient_failure_stops_and_marks_the_rest_not_attempted(
    error: BaseException, status: str, code: str
) -> None:
    """D10: stop on busy/unknown/storage; everything after is ``not_attempted``."""
    first, second, third = a_spec(), a_spec(), a_spec()
    writer = FakeWriter({first.op_id: error})
    service = PushService(writer)  # type: ignore[arg-type]

    outcome = await service.run(
        a_batch(first, second, third), user_id=uuid.uuid4(), membership=_no_member
    )

    assert [result.status for result in outcome.results] == [
        status,
        STATUS_NOT_ATTEMPTED,
        STATUS_NOT_ATTEMPTED,
    ]
    # The rest were never sent to the kernel: "not attempted" is literal.
    assert [call.op_id for call in writer.calls] == [first.op_id]
    assert all(result.code == code for result in outcome.results)
    assert outcome.stopped


@pytest.mark.anyio
async def test_a_stop_on_the_last_op_leaves_no_padding() -> None:
    only = a_spec()
    writer = FakeWriter({only.op_id: WriteBusy("busy")})
    service = PushService(writer)  # type: ignore[arg-type]

    outcome = await service.run(a_batch(only), user_id=uuid.uuid4(), membership=_no_member)

    assert len(outcome.results) == 1
    assert outcome.stopped is False


@pytest.mark.anyio
async def test_one_result_per_submitted_op_always() -> None:
    specs = tuple(a_spec() for _ in range(3))
    writer = FakeWriter({specs[1].op_id: StorageFailure("boom")})
    service = PushService(writer)  # type: ignore[arg-type]

    outcome = await service.run(a_batch(*specs), user_id=uuid.uuid4(), membership=_no_member)

    assert len(outcome.results) == len(specs)
    assert [result.op_id for result in outcome.results] == [spec.op_id for spec in specs]


@pytest.mark.anyio
async def test_membership_is_rechecked_before_every_op() -> None:
    """D5: a revoked member must be refused at the next op, not at the next request."""
    specs = tuple(a_spec() for _ in range(3))
    writer = FakeWriter()
    service = PushService(writer)  # type: ignore[arg-type]
    checked: list[uuid.UUID] = []

    async def membership(workspace: uuid.UUID, user_id: uuid.UUID) -> None:
        checked.append(user_id)
        assert workspace == WS

    await service.run(a_batch(*specs), user_id=uuid.uuid4(), membership=membership)

    assert len(checked) == 3


@pytest.mark.anyio
async def test_a_revoked_member_stops_before_the_next_op() -> None:
    """The auth error is not converted: the route turns it into the uniform 404."""
    specs = tuple(a_spec() for _ in range(3))
    writer = FakeWriter()
    service = PushService(writer)  # type: ignore[arg-type]
    calls = {"n": 0}

    class FakeAuthError(Exception):
        pass

    async def membership(workspace: uuid.UUID, user_id: uuid.UUID) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise FakeAuthError

    with pytest.raises(FakeAuthError):
        await service.run(a_batch(*specs), user_id=uuid.uuid4(), membership=membership)

    # Only the first op ran; the second was refused at its membership check.
    assert len(writer.calls) == 1


@pytest.mark.anyio
async def test_the_budget_is_checked_between_ops_only() -> None:
    """A clock that advances past the deadline stops the batch *before* the next op."""
    specs = tuple(a_spec() for _ in range(3))
    writer = FakeWriter()
    # Call 1 sets the deadline; the check before op 2 still fits, the one before op 3
    # does not - so exactly two ops run.
    ticks = iter([0.0, 0.0, 31.0])
    service = PushService(writer, clock=lambda: next(ticks, 31.0))  # type: ignore[arg-type]

    outcome = await service.run(
        a_batch(*specs, budget_ms=30_000), user_id=uuid.uuid4(), membership=_no_member
    )

    # Two ops completed inside their allowed window, the third is unattempted.
    assert len(writer.calls) == 2
    assert outcome.results[-1].status == STATUS_NOT_ATTEMPTED
    assert outcome.results[-1].code == BUSY_CODE
    assert outcome.stopped


@pytest.mark.anyio
async def test_an_op_that_started_is_never_cut_short_by_the_budget() -> None:
    """D17: the budget is between-ops only. A running op is ended by its own timeouts.

    The clock jumps far past the deadline *during* the first op; that op still
    completes and reports its real result, because abandoning it mid-flight would
    turn a definite outcome into an unknown one.
    """
    first, second = a_spec(), a_spec()
    writer = FakeWriter()
    state = {"now": 0.0, "calls": 0}

    def clock() -> float:
        state["calls"] += 1
        if state["calls"] == 1:
            return 0.0
        # Every later reading is past the 30 s budget.
        state["now"] = 999.0
        return state["now"]

    service = PushService(writer, clock=clock)  # type: ignore[arg-type]

    outcome = await service.run(
        a_batch(first, second, budget_ms=30_000), user_id=uuid.uuid4(), membership=_no_member
    )

    assert outcome.results[0].status == STATUS_APPLIED
    assert len(writer.calls) == 1
    assert outcome.results[1].status == STATUS_NOT_ATTEMPTED


@pytest.mark.anyio
async def test_a_disconnected_client_stops_the_batch_without_fabricating_work() -> None:
    """The abort probe stands in for ``request.is_disconnected`` between ops."""
    specs = tuple(a_spec() for _ in range(3))
    writer = FakeWriter()
    service = PushService(writer)  # type: ignore[arg-type]
    answered = {"count": 0}

    async def probe() -> bool:
        answered["count"] += 1
        return True  # the client is already gone at the first between-ops check

    outcome = await service.run(
        a_batch(*specs), user_id=uuid.uuid4(), membership=_no_member, should_abort=probe
    )

    assert outcome.results[0].status == STATUS_APPLIED
    assert [result.status for result in outcome.results[1:]] == [
        STATUS_NOT_ATTEMPTED,
        STATUS_NOT_ATTEMPTED,
    ]
    assert len(writer.calls) == 1, "no op after the disconnect may reach the kernel"


@pytest.mark.anyio
async def test_cancellation_propagates_through_the_adapter() -> None:
    """No catching, no wrapping: a cancelled task must not be turned into a result."""
    spec = a_spec()

    class CancellingWriter(FakeWriter):
        async def execute_with_outcome(self, command: Any, **kwargs: Any) -> Any:
            raise asyncio.CancelledError

    service = PushService(CancellingWriter())  # type: ignore[arg-type]

    with pytest.raises(asyncio.CancelledError):
        await service.run(a_batch(spec), user_id=uuid.uuid4(), membership=_no_member)


@pytest.mark.anyio
async def test_a_not_attempted_result_cannot_carry_coordinates() -> None:
    """A skipped op must never look like a committed position."""
    result = OpResult(op_id=uuid.uuid4(), status=STATUS_NOT_ATTEMPTED, code=BUSY_CODE)

    assert result.seq is None and result.rev is None
    assert result.succeeded is False


def test_result_invariants_are_enforced_at_construction() -> None:
    with pytest.raises(ValueError, match="requires a failure code"):
        OpResult(op_id=uuid.uuid4(), status=STATUS_REJECTED)
    with pytest.raises(ValueError, match="must not carry a failure code"):
        OpResult(op_id=uuid.uuid4(), status=STATUS_APPLIED, code=CONFLICT_CODE)
    with pytest.raises(ValueError, match="unknown push status"):
        OpResult(op_id=uuid.uuid4(), status="success")


def test_an_outcome_must_not_be_empty() -> None:
    with pytest.raises(ValueError):
        PushOutcome(results=())


def test_adapter_repr_never_prints_note_content() -> None:
    spec = a_spec(
        entity_type="note",
        op="create",
        payload={"title": "SECRET-TITLE", "body_md": "SECRET-BODY", "folder_id": None},
    )
    batch = a_batch(spec)
    result = OpResult(op_id=spec.op_id, status=STATUS_APPLIED, seq=1, rev=1)

    rendered = repr(spec) + repr(batch) + repr(result) + repr(PushOutcome(results=(result,)))

    assert "SECRET-BODY" not in rendered
    assert "SECRET-TITLE" not in rendered
    # Only field *names* may appear (payload_keys), never a value.
    assert "'title'" in rendered or "title" in rendered  # the key name is code
    assert "SECRET" not in rendered


def test_the_adapter_imports_no_http_framework() -> None:
    """Boundary rule: ``services/`` renders nothing and speaks no protocol."""
    source = inspect.getsource(adapter_module)

    for forbidden in ("fastapi", "starlette", "pydantic", "revnote_api.api"):
        assert f"import {forbidden}" not in source
        assert f"from {forbidden}" not in source


def test_the_service_is_stateless() -> None:
    """One instance must be safe for concurrent batches: no per-call attributes."""
    service = PushService(FakeWriter())  # type: ignore[arg-type]

    assert set(vars(service)) == {"_writer", "_clock"}


# --------------------------------------------------------------------------- #
# route: pre-flight rejection, rendering, registration
# --------------------------------------------------------------------------- #


def _app(
    monkeypatch: pytest.MonkeyPatch,
    writer: FakeWriter,
    *,
    stub_membership: bool = True,
) -> FastAPI:
    """A minimal app mounting the real route with only the DB seams stubbed.

    Deliberately keeps the **real** :class:`PushService` and the real route body, so
    the ordering, stop rules and - most importantly - the ``is_disconnected``
    between-ops check under test are the shipped ones. Only these are replaced: the
    kernel writer (no PostgreSQL in the unit tier) and, unless
    ``stub_membership=False``, the per-op probe (no auth database). The disconnect
    poll is never touched.
    """
    from revnote_api.api.dependencies import (
        AuthContext,
        get_auth_context,
        require_workspace_member,
    )
    from revnote_api.api.errors import install_error_handlers
    from revnote_api.api.middleware.request_id import RequestIdMiddleware

    app = FastAPI()
    app.state.settings = Settings(_env_file=None)
    install_error_handlers(app)
    app.include_router(route_module.router, prefix="/api/v1")
    # The shipped app carries this middleware; without it ``get_request_id`` falls
    # back to a fresh id and the X-Request-ID correlation could not be asserted.
    app.add_middleware(RequestIdMiddleware)

    context = AuthContext(user_id=uuid.uuid4(), session_id=uuid.uuid4())

    async def fake_context() -> Any:
        return context

    async def fake_member() -> Any:
        return context

    app.dependency_overrides[get_auth_context] = fake_context
    app.dependency_overrides[require_workspace_member] = fake_member

    monkeypatch.setattr(route_module, "get_sessionmaker", lambda _settings: object())
    monkeypatch.setattr(route_module, "SyncWriteService", lambda _factory: writer)
    if stub_membership:
        # The per-op probe is the auth service; stub it so no database is needed.
        monkeypatch.setattr(route_module, "_assert_member", _always_member)
    return app


async def _always_member(*_args: Any, **_kwargs: Any) -> None:
    return None


@pytest.mark.anyio
async def test_the_route_rejects_a_duplicate_op_id_before_any_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D12: a repeated op_id rejects the whole batch with zero execution."""
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    op = an_op()
    duplicate = dict(op)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/v1/workspaces/{WS}/sync/push", json={"ops": [op, duplicate]}
        )

    assert response.status_code == 400
    assert writer.calls == [], "nothing may execute when the batch is rejected"


@pytest.mark.anyio
async def test_duplicate_detection_uses_normalised_uuids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two spellings of one UUID are still one op_id."""
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    shared = uuid.UUID("01900000-0000-7000-8000-0000000000bb")
    upper = str(shared).upper()
    op_a = an_op(op_id=str(shared))
    op_b = an_op(op_id=upper)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/v1/workspaces/{WS}/sync/push", json={"ops": [op_a, op_b]}
        )

    assert response.status_code == 400
    assert writer.calls == []


@pytest.mark.anyio
async def test_the_route_rejects_too_many_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    ops = [an_op() for _ in range(app.state.settings.sync_push_max_ops + 1)]

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(f"/api/v1/workspaces/{WS}/sync/push", json={"ops": ops})

    assert response.status_code == 400
    assert response.json()["error"]["details"]["max_ops"] == app.state.settings.sync_push_max_ops
    assert writer.calls == []


@pytest.mark.anyio
async def test_the_route_measures_the_body_cap_in_utf8_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CJK body is 3 bytes per character: the limit is bytes, not characters."""
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    limit = app.state.settings.sync_note_max_bytes
    # Characters well under the limit, bytes over it.
    body = "汉" * (limit // 3 + 1)
    assert len(body) < limit

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/v1/workspaces/{WS}/sync/push",
            json={"ops": [an_op(payload={"title": "t", "body_md": body})]},
        )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert response.json()["error"]["details"]["op_index"] == 0
    assert writer.calls == []


@pytest.mark.anyio
async def test_the_route_renders_successes_as_decimal_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = FakeWriter()
    app = _app(monkeypatch, writer)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(f"/api/v1/workspaces/{WS}/sync/push", json={"ops": [an_op()]})

    assert response.status_code == 200
    item = response.json()["results"][0]
    assert item["status"] == "applied"
    assert (item["seq"], item["rev"]) == ("1", "1")
    assert item["error"] is None, "a successful op carries no error object"


@pytest.mark.anyio
async def test_a_malformed_op_is_a_400_without_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = FakeWriter()
    app = _app(monkeypatch, writer)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/v1/workspaces/{WS}/sync/push",
            json={"ops": [an_op(payload={"title": "t"})]},  # body_md missing
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert writer.calls == []


@pytest.mark.anyio
async def test_the_route_never_echoes_a_secret_from_a_rejected_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile JSON key must not be reflected in the validation details."""
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    secret = "hunter2-super-secret"

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/v1/workspaces/{WS}/sync/push",
            json={"ops": [an_op(payload={"title": "t", "body_md": "b", secret: "x"})]},
        )

    assert response.status_code == 400
    assert secret not in response.text


@pytest.mark.anyio
async def test_a_stop_renders_not_attempted_for_the_remainder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    first, second = an_op(), an_op()
    writer.script[uuid.UUID(first["op_id"])] = WriteBusy("busy")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/v1/workspaces/{WS}/sync/push", json={"ops": [first, second]}
        )

    assert response.status_code == 200, "a per-op failure is data, not a transport error"
    results = response.json()["results"]
    assert results[0]["status"] == "rejected"
    assert results[0]["error"]["code"] == "write_busy"
    assert results[1]["status"] == "not_attempted"
    assert results[1]["error"]["code"] == "write_busy"


def test_the_route_module_declares_the_documented_path_and_method() -> None:
    """The endpoint's own contract: one POST at the spec's path, no trailing variants.

    Registration in ``api/router.py`` is the single registration entry's job (another
    lane's file), so this test owns what this module publishes:

        api_v1.include_router(sync_push.sync_push_router)
    """
    declared = {
        (route.path, method)
        for route in route_module.router.routes
        for method in getattr(route, "methods", ())
    }

    assert ("/workspaces/{workspace_id}/sync/push", "POST") in declared
    # One endpoint only: no REST write wrapper, no extra verb (D19).
    assert len(route_module.router.routes) == 1


def test_the_route_module_exports_the_alias_the_registration_entry_needs() -> None:
    """``sync_push_router`` must be the very same router, so mounting it cannot drift.

    Registration itself is one line in :mod:`revnote_api.api.router` (owned by the
    router lane)::

        from revnote_api.api.routes.sync_push import sync_push_router
        api_v1.include_router(sync_push_router)

    This module is verified here; the mount is verified by that lane's contract test
    and by the API tier once the line exists.
    """
    assert route_module.sync_push_router is route_module.router


def test_the_schema_module_declares_no_message_or_secret_field() -> None:
    """Rendering belongs to ``api/errors``; a wire model must not invent wording."""
    source = inspect.getsource(schema_module)

    assert "message:" not in source
    # No model *field* named like a credential (the docstring may mention them).
    for forbidden in ("password:", "token:", "authorization:", "access_token"):
        assert forbidden not in source.lower()


def test_the_route_module_does_not_import_the_ledger_or_http_error_helpers() -> None:
    """No ledger pre-lookup, no hand-built error responses: the envelope is shared."""
    source = inspect.getsource(route_module)

    assert "op_ledger" not in source
    assert "find_ledger" not in source
    assert "error_response(" not in source


# --------------------------------------------------------------------------- #
# disconnect: the next op must not start once the peer is gone
# --------------------------------------------------------------------------- #
#
# ``request.is_disconnected`` reads the ASGI ``receive`` channel, so the faithful
# unit-level proof is to drive the app with a channel that answers ``http.disconnect``
# at a chosen moment. That is what uvicorn does when the client closes the socket;
# a real TCP close is the transport tier's job (E), and this tier proves the *route's
# reaction* without needing a socket.


class _DisconnectAfter:
    """An ASGI ``receive`` that serves the request body, then reports a disconnect.

    Counts what the app actually asks for, so a test can tell "the route polled the
    peer between ops" from "the route never looked".
    """

    def __init__(self, body: bytes, *, disconnect_after_calls: int) -> None:
        self._body = body
        self._sent_body = False
        self._seen = 0
        self._disconnect_at = disconnect_after_calls
        self.disconnects_served = 0

    async def __call__(self) -> dict[str, Any]:
        if not self._sent_body:
            self._sent_body = True
            return {"type": "http.request", "body": self._body, "more_body": False}

        self._seen += 1
        if self._seen >= self._disconnect_at:
            self.disconnects_served += 1
            return {"type": "http.disconnect"}
        # The peer is still there: nothing to report yet.
        return {"type": "http.request", "body": b"", "more_body": True}


async def _post_through_channel(
    app: FastAPI,
    path: str,
    body: bytes,
    receive: _DisconnectAfter,
) -> list[dict[str, Any]]:
    """Send one POST straight through the ASGI interface with a custom ``receive``."""
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


@pytest.mark.anyio
async def test_a_disconnect_between_ops_stops_the_next_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mandatory guarantee: after a peer disconnect, no further op may start.

    Three ops are submitted and the peer reports a disconnect as soon as the route
    first polls. The kernel writer must have been called exactly once, and the two
    remaining ops must be ``not_attempted`` - never silently applied, never claimed
    as rolled back.
    """
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    ops = [an_op() for _ in range(3)]
    body = json.dumps({"ops": ops}).encode()

    # One body read, then disconnect on the first poll (before op #2).
    receive = _DisconnectAfter(body, disconnect_after_calls=1)
    sent = await _post_through_channel(app, f"/api/v1/workspaces/{WS}/sync/push", body, receive)

    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 200, "the batch's shape was fine; this is a per-op outcome"
    payload = json.loads(next(m for m in sent if m["type"] == "http.response.body")["body"])

    assert receive.disconnects_served >= 1, "the route never polled for a disconnect"
    assert len(writer.calls) == 1, (
        f"a disconnect between ops must stop the batch, but {len(writer.calls)} ops ran"
    )
    statuses = [item["status"] for item in payload["results"]]
    assert statuses == ["applied", "not_attempted", "not_attempted"]


@pytest.mark.anyio
async def test_a_connected_client_runs_every_op(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control case: with no disconnect, the same batch runs all three ops.

    Without this, the stop above could be an artefact of the harness rather than the
    disconnect check.
    """
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    ops = [an_op() for _ in range(3)]
    body = json.dumps({"ops": ops}).encode()

    # Never reports a disconnect (the counter is unreachable).
    receive = _DisconnectAfter(body, disconnect_after_calls=10_000)
    sent = await _post_through_channel(app, f"/api/v1/workspaces/{WS}/sync/push", body, receive)

    payload = json.loads(next(m for m in sent if m["type"] == "http.response.body")["body"])
    assert [item["status"] for item in payload["results"]] == ["applied"] * 3
    assert len(writer.calls) == 3


@pytest.mark.anyio
async def test_the_first_op_always_runs_even_if_the_peer_is_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check is *between* ops. Op 1 is not gated, so a client that closed is still
    never half-applied by a pre-check that fires before anything was attempted.

    Reporting ``not_attempted`` for op 1 would be a lie of a different kind: the
    request had already been accepted and the first write is what the client's
    ``op_id`` guards. This test pins that deliberate asymmetry.
    """
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    ops = [an_op() for _ in range(2)]
    body = json.dumps({"ops": ops}).encode()

    receive = _DisconnectAfter(body, disconnect_after_calls=1)
    sent = await _post_through_channel(app, f"/api/v1/workspaces/{WS}/sync/push", body, receive)

    payload = json.loads(next(m for m in sent if m["type"] == "http.response.body")["body"])
    assert payload["results"][0]["status"] == "applied"
    assert payload["results"][1]["status"] == "not_attempted"


def test_the_route_polls_is_disconnected_between_ops() -> None:
    """Source guard: the poll must be wired to the request, and only there."""
    source = inspect.getsource(route_module)

    assert "request.is_disconnected" in source
    assert "should_abort=" in source


# --------------------------------------------------------------------------- #
# the spec's error object per failed op (not a bare code)
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_a_failed_op_carries_the_full_error_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec §3: ``error`` has code/message/request_id/retryable/details, not just a code."""
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    op = an_op()
    writer.script[uuid.UUID(op["op_id"])] = RevConflict(
        "note", uuid.UUID(op["entity_id"]), expected=1, current=2
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/v1/workspaces/{WS}/sync/push",
            json={"ops": [op]},
            headers={"X-Request-ID": "req-abc.123"},
        )

    assert response.status_code == 200
    error = response.json()["results"][0]["error"]
    assert set(error) == {"code", "message", "request_id", "retryable", "details"}
    assert error["code"] == "rev_conflict"
    assert isinstance(error["message"], str) and error["message"]
    assert error["request_id"] == "req-abc.123", "the op error echoes the request's id"
    assert error["retryable"] is False
    assert error["details"] == {}


@pytest.mark.anyio
async def test_an_id_conflict_error_is_structurally_identical_for_both_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 #3: the two conflict classes must be indistinguishable on the wire."""
    entity, op_id = uuid.uuid4(), uuid.uuid4()
    rendered: list[dict[str, Any]] = []

    for error in (EntityIdConflict("note", entity), OpIdConflict(WS, op_id)):
        writer = FakeWriter()
        app = _app(monkeypatch, writer)
        op = an_op()
        writer.script[uuid.UUID(op["op_id"])] = error
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                f"/api/v1/workspaces/{WS}/sync/push",
                json={"ops": [op]},
                headers={"X-Request-ID": "same-id"},
            )
        rendered.append(response.json()["results"][0]["error"])

    assert rendered[0] == rendered[1], "the origin of an id conflict must not be observable"
    assert rendered[0]["code"] == "conflict"
    assert rendered[0]["details"] == {}


@pytest.mark.anyio
async def test_a_busy_op_error_is_marked_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error object must tell the client it may resend the identical op."""
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    op = an_op()
    writer.script[uuid.UUID(op["op_id"])] = WriteBusy("busy")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(f"/api/v1/workspaces/{WS}/sync/push", json={"ops": [op]})

    error = response.json()["results"][0]["error"]
    assert (error["code"], error["retryable"]) == ("write_busy", True)


# --------------------------------------------------------------------------- #
# a revoked member is a uniform 404, never a 500
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_a_membership_revocation_between_ops_is_a_404_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D5/D20: the per-op recheck raises ``AuthError``; the route must render 404.

    Without the translation an uncaught service exception becomes ``internal``/500,
    which both leaks that something failed deep in the stack and tells the client the
    wrong thing: the correct answer is the same uniform "not found" as a missing
    workspace.
    """
    from revnote_api.services.auth_rules import AuthError

    writer = FakeWriter()
    app = _app(monkeypatch, writer, stub_membership=False)
    calls = {"n": 0}

    async def revoke_on_second(*_args: Any, **_kwargs: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise AuthError("not_found")

    # Patch the *service* call, so the route's own ``except AuthError`` translation
    # is what the test exercises (patching ``_assert_member`` would bypass it).
    monkeypatch.setattr(route_module, "assert_workspace_member", revoke_on_second)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/v1/workspaces/{WS}/sync/push", json={"ops": [an_op(), an_op()]}
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert len(writer.calls) == 1, "the op after the revocation must not run"


# --------------------------------------------------------------------------- #
# structural pre-validation: zero execution for a batch the kernel would refuse
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_a_batch_the_kernel_would_refuse_executes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If any op cannot become a command, the request is a 400 with zero execution.

    The wire schema rejects almost every structural problem, so this drives the last
    line of defence directly: ``prepare_batch`` reporting an unusable op must abort
    the whole batch *before* the first write, never after ops 0..n-1 committed.
    """
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    monkeypatch.setattr(route_module, "prepare_batch", lambda *_args, **_kwargs: None)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/v1/workspaces/{WS}/sync/push", json={"ops": [an_op(), an_op()]}
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert writer.calls == [], "no op may execute when the batch cannot be built"


@pytest.mark.anyio
async def test_the_kernel_structural_rules_are_enforced_before_any_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NUL text, a blank name and a folder self-parent are 400s, with zero calls.

    These are the kernel's own preconditions, mirrored in the schema precisely so a
    batch containing one is refused as a whole instead of being discovered as a
    per-op ``rejected`` after earlier ops already committed.
    """
    cases = [
        an_op(payload={"title": "a\x00b", "body_md": "b"}),
        an_op(payload={"title": "t", "body_md": "a\x00b"}),
        an_op(entity_type="folder", op="create", payload={"name": "a\x00b"}),
        an_op(entity_type="folder", op="create", payload={"name": "   "}),
    ]
    self_parent_id = str(uuid.uuid4())
    cases.append(
        an_op(
            entity_type="folder",
            op="create",
            entity_id=self_parent_id,
            payload={"name": "n", "parent_id": self_parent_id},
        )
    )

    for case in cases:
        writer = FakeWriter()
        app = _app(monkeypatch, writer)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.post(
                f"/api/v1/workspaces/{WS}/sync/push", json={"ops": [an_op(), case]}
            )
        assert response.status_code == 400, case["payload"]
        assert writer.calls == [], f"a structurally invalid batch wrote: {case['payload']}"


@pytest.mark.anyio
async def test_a_lone_surrogate_in_a_body_is_a_400_not_a_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON can carry ``"\\ud800"``; it must fail the size check as a 400.

    ``str.encode("utf-8")`` raises on a lone surrogate, which would surface as an
    unhandled 500 from the body-cap check. The batch is refused instead.
    """
    writer = FakeWriter()
    app = _app(monkeypatch, writer)
    op = an_op(payload={"title": "t", "body_md": "x"})
    raw = json.dumps({"ops": [op]}).replace('"body_md": "x"', '"body_md": "\\ud800"')

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(
            f"/api/v1/workspaces/{WS}/sync/push",
            content=raw.encode("utf-8", "surrogatepass"),
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert writer.calls == []


def test_prepare_batch_reports_failure_without_any_command() -> None:
    """``prepare_batch`` returns ``None`` when any op cannot become a command."""
    good = a_spec()
    # ``DeleteFolder`` takes no ``name``: the kernel raises ``TypeError``.
    unusable = a_spec(op="delete", base_rev=1, payload={"name": "surprise"})

    assert prepare_batch(WS, [good]) is not None
    assert prepare_batch(WS, [good, unusable]) is None
    assert prepare_batch(WS, [unusable, good]) is None


def test_prepare_batch_performs_no_database_access() -> None:
    """It is pure construction: the structural gate must not open a connection."""
    source = inspect.getsource(adapter_module)

    prepare = source.split("def prepare_batch", 1)[1].split("\ndef ", 1)[0]
    for forbidden in ("session", "engine", "execute(", "find_ledger"):
        assert forbidden not in prepare, forbidden


def test_prepare_batch_builds_one_command_per_op_in_order() -> None:
    specs = (
        a_spec(),
        a_spec(entity_type="note", op="create", payload={"title": "t", "body_md": "b"}),
    )

    built = prepare_batch(WS, specs)

    assert built is not None
    assert [command.op_id for command in built] == [spec.op_id for spec in specs]
    assert all(command.workspace_id == WS for command in built)


# --------------------------------------------------------------------------- #
# diagnostics: no content in any nested repr/str (the pydantic str() pitfall)
# --------------------------------------------------------------------------- #


def _every_wire_model() -> list[Any]:
    """One instance of every model in the module, all carrying the same sentinels."""
    secret = "SECRET_SENTINEL_TEXT"
    note_create = PushOp.model_validate(
        an_op(payload={"title": secret, "body_md": secret, "folder_id": None})
    )
    folder_create = PushOp.model_validate(
        an_op(entity_type="folder", op="create", payload={"name": secret, "parent_id": None})
    )
    instances: list[Any] = [
        note_create,
        folder_create,
        note_create.payload,
        folder_create.payload,
        PushRequest.model_validate({"ops": [an_op(payload={"title": secret, "body_md": secret})]}),
        PushResponse(
            results=[
                PushResultItem(
                    op_id=uuid.uuid4(),
                    status=STATUS_APPLIED,
                    seq="1",
                    rev="1",
                )
            ]
        ),
        PushResultItem(
            op_id=uuid.uuid4(),
            status=STATUS_REJECTED,
            error=_conflict_error(),
        ),
    ]
    instances.extend(model.model_validate(_minimal_for(model)) for model in PAYLOAD_MODELS.values())
    return instances


def _conflict_error() -> Any:
    from revnote_api.api.schemas.common import ErrorBody

    return ErrorBody(
        code=CONFLICT_CODE,
        message="conflict",
        request_id="req-1",
        retryable=False,
        details={},
    )


def _minimal_for(model: type[Any]) -> dict[str, Any]:
    """The smallest payload body a given payload model accepts."""
    name = model.__name__
    if "Move" in name:
        return {"folder_id": None} if name.startswith("Note") else {"parent_id": None}
    if name.startswith("Folder"):
        return {"name": "x"} if ("Create" in name or "Update" in name) else {}
    return {"title": "t", "body_md": "b"} if ("Create" in name or "Update" in name) else {}


@pytest.mark.parametrize("renderer", ["__repr__", "__str__"])
def test_no_wire_model_renders_content(renderer: str) -> None:
    """Pydantic's default ``str()`` is a value dump; every model must override it.

    This is the leak that matters most: an unhandled exception, a log line or a
    debugger prints ``str(model)``, and pydantic's own implementation walks every
    field - ``body_md`` verbatim. Proven across **all** models, nested and nested
    inside a request, with a distinctive sentinel in every content field.
    """
    for instance in _every_wire_model():
        rendered = getattr(instance, renderer)()
        assert "SECRET_SENTINEL_TEXT" not in rendered, f"{type(instance).__name__} leaks"
        # The field *names* may appear (they are code, not user content).
        assert type(instance).__name__ in rendered


def test_the_request_wrapper_does_not_recurse_into_op_values() -> None:
    """``PushRequest.__str__`` would otherwise inherit pydantic's recursive dump."""
    secret = "SECRET_SENTINEL_TEXT"
    request = PushRequest.model_validate(
        {"ops": [an_op(payload={"title": secret, "body_md": secret})]}
    )

    for rendered in (repr(request), str(request)):
        assert secret not in rendered
        # And it names the element type, not the element's fields.
        assert "PushOp" not in rendered or "list" in rendered


# --------------------------------------------------------------------------- #
# the batch budget is a real bound, measured on the clock
# --------------------------------------------------------------------------- #


@pytest.mark.anyio
async def test_the_budget_uses_the_real_monotonic_clock() -> None:
    """With the default clock, a batch whose ops outrun the budget stops early.

    Proves the production clock is wired (not a stub that always returns 0): one
    millisecond of budget cannot cover three ops that each touch a slow writer.
    """
    specs = tuple(a_spec() for _ in range(3))

    class SlowWriter(FakeWriter):
        async def execute_with_outcome(self, command: Any, **kwargs: Any) -> Any:
            await asyncio.sleep(0.02)  # > the 1 ms budget
            return await super().execute_with_outcome(command, **kwargs)

    writer = SlowWriter()
    service = PushService(writer)  # type: ignore[arg-type]

    outcome = await service.run(
        a_batch(*specs, budget_ms=1), user_id=uuid.uuid4(), membership=_no_member
    )

    assert len(writer.calls) == 1, "one op ran; the budget then refused the second"
    assert [result.status for result in outcome.results[1:]] == [
        STATUS_NOT_ATTEMPTED,
        STATUS_NOT_ATTEMPTED,
    ]
    assert outcome.results[1].code == BUSY_CODE
