"""``sync_push`` - the ordered, independently transactional push batch (D9-D12, D17).

Framework-free on purpose: ``services/`` is scanned for FastAPI/Pydantic/api imports
(``test_kernel_boundaries``), and the HTTP layer must render results, not decide
policy. So this module speaks three plain types and nothing else:

* :class:`PushOpSpec` in - the *already validated* shape of one op (the route's
  Pydantic schema is converted to this before the service is called, so a request
  reaching here has no unknown fields and no bad ``base_rev``);
* :class:`OpResult` out - one of six statuses plus a code from the shared kernel
  vocabulary, never a message;
* :class:`PushOutcome` out - the ordered results, one entry per input op.

What the loop guarantees
------------------------
* **Order.** Ops run in array order; nothing is reordered, batched or parallelised.
* **One transaction per op.** Each op is a separate :class:`SyncWriteService` call,
  which owns its own session and commits (or rolls back) on its own. There is no
  outer transaction to abort, so op 3 failing cannot undo ops 0-2.
* **Continue / stop (D10).** ``conflict`` and ``rejected`` are per-op outcomes and
  the batch continues - except for the transient rejections (``write_busy``,
  ``storage_failure``), which stop it along with ``unknown``. Everything after a
  stop is ``not_attempted``. A stop is never retried here: retrying is the
  client's decision, and it must reuse the same ``op_id`` and payload.
* **No ledger pre-lookup.** The applied/replayed distinction comes from
  :meth:`SyncWriteService.execute_with_outcome`, never from a query this layer
  makes, so a replay costs exactly what the kernel charges for one.
* **Budget between ops only.** The 30 s batch budget (D17) is checked *before*
  each op starts. An op that is already running is never abandoned by the clock:
  its own bounded statement/lock timeouts are what end it, and cutting it short
  would turn a definite result into an unknown one for no gain.
* **Cancellation is never caught.** If the request task is cancelled (a real
  socket disconnect, server shutdown), ``CancelledError`` propagates through this
  module untouched and the remaining ops are simply never attempted. The reply is
  gone, so reporting anything about them would be fiction; ``not_attempted`` is
  only ever filled in for an *observed* stop, which is a result the client can
  still read.

What this module deliberately does not do
-----------------------------------------
It does not check membership itself, does not open the session, does not render
status strings for the wire, and does not know about HTTP status codes. The route
supplies the membership re-check, renders the results, and owns the disconnect
loop.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from revnote_api.kernel.commands import Command, command_for
from revnote_api.kernel.errors import (
    CommitOutcomeUnknown,
    CounterOverflow,
    EntityDeleted,
    EntityIdConflict,
    EntityNotFound,
    FolderCycle,
    FolderNotEmpty,
    InvalidCommand,
    InvalidParent,
    OpIdConflict,
    RevConflict,
    StorageFailure,
    SyncKernelError,
    WorkspaceNotFound,
    WriteBusy,
)
from revnote_api.kernel.results import ExecutedWrite
from revnote_api.services.sync_write import SyncWriteService

__all__ = [
    "ABORT_CODE",
    "BUSY_CODE",
    "CONFLICT_CODE",
    "MEMBERSHIP_CODE",
    "OP_STATUSES",
    "STATUS_APPLIED",
    "STATUS_CONFLICT",
    "STATUS_NOT_ATTEMPTED",
    "STATUS_REJECTED",
    "STATUS_REPLAYED",
    "STATUS_UNKNOWN",
    "STORAGE_CODE",
    "TRANSIENT_CODES",
    "UNKNOWN_CODE",
    "AbortCheck",
    "OpResult",
    "PushBatch",
    "PushOpSpec",
    "PushOutcome",
    "PushService",
    "PushStatus",
    "build_command",
    "code_for_kernel_error",
    "result_from_executed",
]

# -- statuses (D11) ---------------------------------------------------------- #

STATUS_APPLIED = "applied"
STATUS_REPLAYED = "replayed"
STATUS_CONFLICT = "conflict"
STATUS_REJECTED = "rejected"
STATUS_UNKNOWN = "unknown"
STATUS_NOT_ATTEMPTED = "not_attempted"

#: The closed set as a type. Defined here (framework-free) so the adapter and the
#: wire schema share one vocabulary instead of two lists that could drift.
PushStatus = Literal["applied", "replayed", "conflict", "rejected", "unknown", "not_attempted"]

#: The same set at runtime, in the protocol's own order.
OP_STATUSES: tuple[PushStatus, ...] = (
    STATUS_APPLIED,
    STATUS_REPLAYED,
    STATUS_CONFLICT,
    STATUS_REJECTED,
    STATUS_UNKNOWN,
    STATUS_NOT_ATTEMPTED,
)

# -- codes this layer itself can produce ------------------------------------ #

#: Nothing was written; retry the same op.
BUSY_CODE = "write_busy"
#: The outcome is unknowable; retry the same op to confirm it.
UNKNOWN_CODE = "outcome_unknown"
#: Storage refused the write; retryable, but most likely a fault.
STORAGE_CODE = "storage_failure"
#: The one code for *both* id-conflict classes, so a caller cannot tell whether an
#: id exists in another workspace (P2 #3).
CONFLICT_CODE = "conflict"
#: Membership is re-checked per op; a workspace can disappear between two ops.
MEMBERSHIP_CODE = "not_found"
#: The reason recorded for ops skipped because the client went away. Deliberately a
#: stop-class code: the batch did not complete, and nothing is claimed about those
#: ops beyond "the server never sent them to the kernel".
ABORT_CODE = "write_busy"

#: Codes after which the batch stops (D10). ``internal`` is not a kernel code; it is
#: included because an unexpected failure must not be followed by more writes.
TRANSIENT_CODES: frozenset[str] = frozenset({BUSY_CODE, UNKNOWN_CODE, STORAGE_CODE, "internal"})

#: Kernel error class -> wire code, by **exact** type. Exact-type lookup (not
#: ``isinstance``): a future subclass of ``StorageFailure`` must not silently inherit
#: its retry semantics. Anything unrecognised becomes ``internal``, which stops.
_KERNEL_CODES: dict[type[SyncKernelError], str] = {
    EntityIdConflict: CONFLICT_CODE,
    OpIdConflict: CONFLICT_CODE,
    RevConflict: "rev_conflict",
    EntityDeleted: "entity_deleted",
    EntityNotFound: "not_found",
    InvalidParent: "invalid_parent",
    FolderCycle: "folder_cycle",
    FolderNotEmpty: "folder_not_empty",
    CounterOverflow: "counter_overflow",
    WriteBusy: BUSY_CODE,
    CommitOutcomeUnknown: UNKNOWN_CODE,
    StorageFailure: STORAGE_CODE,
    WorkspaceNotFound: MEMBERSHIP_CODE,
    InvalidCommand: "invalid_request",
}

#: Failure codes that report ``conflict`` rather than ``rejected`` (D11).
_CONFLICT_CODES: frozenset[str] = frozenset({CONFLICT_CODE, "rev_conflict", "entity_deleted"})

#: Failure codes that report ``unknown`` rather than ``rejected``: the write may
#: have been applied, so the client must confirm with the same ``op_id``.
_UNKNOWN_CODES: frozenset[str] = frozenset({UNKNOWN_CODE})

#: Polled between ops; ``True`` means "stop, the caller cannot receive a reply".
AbortCheck = Callable[[], Awaitable[bool]]


def code_for_kernel_error(exc: SyncKernelError) -> str:
    """The wire code for a kernel error, by exact class (``internal`` otherwise)."""
    return _KERNEL_CODES.get(type(exc), "internal")


@dataclass(frozen=True, slots=True)
class PushOpSpec:
    """One validated operation, decoupled from the wire schema.

    ``payload`` holds the op's business fields exactly as the kernel command names
    them (``name``, ``parent_id``, ``title``, ``body_md``, ``folder_id``). It is
    never rendered: those fields can hold note content.
    """

    op_id: uuid.UUID
    entity_type: str
    op: str
    entity_id: uuid.UUID
    base_rev: int | None
    payload: Mapping[str, Any]

    def __repr__(self) -> str:
        """Identity only - a payload dict may contain the note body."""
        return (
            f"PushOpSpec(entity_type={self.entity_type}, op={self.op}, "
            f"op_id={self.op_id}, entity_id={self.entity_id}, "
            f"base_rev={self.base_rev!r}, payload_keys={sorted(self.payload)})"
        )


@dataclass(frozen=True, slots=True)
class OpResult:
    """One op's outcome: a status, an optional code, and the kernel's coordinates.

    ``seq``/``rev`` are kernel integers here; the route renders them as decimal
    strings. They are set only for ``applied``/``replayed`` so a client can never
    mistake a failure for a committed position.
    """

    op_id: uuid.UUID
    status: PushStatus
    code: str | None = None
    seq: int | None = None
    rev: int | None = None

    def __post_init__(self) -> None:
        if self.status not in OP_STATUSES:
            raise ValueError(f"unknown push status {self.status!r}")
        if self.status in (STATUS_APPLIED, STATUS_REPLAYED):
            if self.code is not None:
                raise ValueError("a successful op must not carry a failure code")
            return
        if self.code is None:
            raise ValueError(f"status {self.status!r} requires a failure code")

    @property
    def succeeded(self) -> bool:
        """True only for the two committed statuses."""
        return self.status in (STATUS_APPLIED, STATUS_REPLAYED)

    @property
    def stops_batch(self) -> bool:
        """True when this result ends the batch (D10): a transient code or a stop."""
        if self.status == STATUS_NOT_ATTEMPTED:
            return True
        return self.code in TRANSIENT_CODES

    def __repr__(self) -> str:
        return (
            f"OpResult(status={self.status}, code={self.code!r}, op_id={self.op_id}, "
            f"seq={self.seq!r}, rev={self.rev!r})"
        )


@dataclass(frozen=True, slots=True)
class PushOutcome:
    """The ordered results of one batch, always one per submitted op."""

    results: tuple[OpResult, ...]

    def __post_init__(self) -> None:
        if not self.results:
            raise ValueError("a push outcome always has at least one result")

    @property
    def stopped(self) -> bool:
        """True when the batch ended early rather than running every op."""
        return self.results[-1].status == STATUS_NOT_ATTEMPTED

    def __repr__(self) -> str:
        return f"PushOutcome(results={list(self.results)!r})"


@dataclass(frozen=True, slots=True)
class PushBatch:
    """Everything one batch call needs that is not derived from the request body.

    Passing these explicitly keeps :class:`PushService` stateless: no per-request
    field lives on the service, so one instance can serve concurrent requests, and
    the budgets a route injects stay visible at the call site.

    ``commands`` carries the pre-built kernel commands from :func:`prepare_batch`,
    positionally aligned with ``ops``. They are built **before** the first op runs, so
    a structurally invalid op anywhere in the batch rejects the whole request instead
    of being discovered halfway through.
    """

    workspace_id: uuid.UUID
    ops: Sequence[PushOpSpec]
    commands: Sequence[Command]
    lock_timeout_ms: int
    statement_timeout_ms: int
    budget_ms: int

    def __post_init__(self) -> None:
        if len(self.ops) != len(self.commands):
            raise ValueError("every op needs exactly one pre-built command")

    def __repr__(self) -> str:
        return (
            f"PushBatch(workspace_id={self.workspace_id}, ops={len(self.ops)}, "
            f"lock_timeout_ms={self.lock_timeout_ms}, "
            f"statement_timeout_ms={self.statement_timeout_ms}, "
            f"budget_ms={self.budget_ms})"
        )


#: ``async (workspace_id, user_id) -> None``, raising ``AuthError`` when the user may
#: not act in the workspace. Supplied by the route so this layer never imports auth.
MembershipCheck = Callable[[uuid.UUID, uuid.UUID], Awaitable[None]]

#: ``now`` indirection so the budget is testable without sleeping.
Clock = Callable[[], float]


class PushService:
    """Run one push batch: sequential ops, one transaction each, stop rules applied.

    Stateless by construction - the constructor takes collaborators, never a
    request - so one instance may serve concurrent batches and no attribute can
    leak between them (the same rule the kernel service follows).
    """

    def __init__(self, writer: SyncWriteService, *, clock: Clock = time.monotonic) -> None:
        self._writer = writer
        self._clock = clock

    async def run(
        self,
        batch: PushBatch,
        *,
        user_id: uuid.UUID,
        membership: MembershipCheck,
        should_abort: AbortCheck | None = None,
    ) -> PushOutcome:
        """Execute ``batch`` in order and return one result per submitted op.

        ``membership`` is awaited **before every op** (D5) on its own short-lived
        check, so a user removed mid-batch is refused at the next op instead of
        writing through a stale decision. An ``AuthError`` from it is *not* caught
        here: losing access is an authorisation outcome for the whole request (the
        route renders the uniform 404), not a per-op status.

        ``should_abort`` is polled between ops only, like the budget. It exists for
        one reason: Starlette/uvicorn do not cancel a handler when the client closes
        the socket, so the route supplies ``request.is_disconnected``. A ``True``
        answer stops the batch with the remaining ops ``not_attempted`` and no
        pretence of a rollback - they were never started.

        The commands come from ``batch.commands`` (built by :func:`prepare_batch`),
        so nothing here can reject a single op structurally: the batch is already
        known to be buildable.

        Cancellation propagates: a cancelled request has no reply to put results into.
        """
        deadline = self._clock() + batch.budget_ms / 1000.0
        results: list[OpResult] = []
        ops = list(batch.ops)
        commands = list(batch.commands)

        for index, spec in enumerate(ops):
            if index > 0:
                # Between-ops checks only, never mid-op (D17): an op already running
                # is ended by its own bounded timeouts, not by this clock.
                if should_abort is not None and await should_abort():
                    results.extend(self._not_attempted(ops[index:], ABORT_CODE))
                    break
                if self._clock() >= deadline:
                    # Budget spent: stop cleanly. "Busy" is the honest code - nothing
                    # was written and the client may resend the same ops.
                    results.extend(self._not_attempted(ops[index:], BUSY_CODE))
                    break

            await membership(batch.workspace_id, user_id)
            result = await self._run_one(batch, spec, commands[index])
            results.append(result)

            if result.stops_batch:
                results.extend(self._not_attempted(ops[index + 1 :], result.code))
                break

        return PushOutcome(results=tuple(results))

    # ------------------------------------------------------------------ #
    # one op
    # ------------------------------------------------------------------ #
    async def _run_one(self, batch: PushBatch, spec: PushOpSpec, command: Command) -> OpResult:
        """Map one kernel call onto one result; never lets a kernel error escape."""
        try:
            executed = await self._writer.execute_with_outcome(
                command,
                lock_timeout_ms=batch.lock_timeout_ms,
                statement_timeout_ms=batch.statement_timeout_ms,
            )
        except SyncKernelError as exc:
            # The class decides the code; the message is dropped (it can name ids).
            code = code_for_kernel_error(exc)
            return OpResult(op_id=spec.op_id, status=_status_for_code(code), code=code)

        return result_from_executed(executed, spec.op_id)

    @staticmethod
    def _not_attempted(remaining: Sequence[PushOpSpec], code: str | None) -> list[OpResult]:
        """Every op after a stop, marked with the reason the batch ended."""
        reason = code if code is not None else "internal"
        return [
            OpResult(op_id=spec.op_id, status=STATUS_NOT_ATTEMPTED, code=reason)
            for spec in remaining
        ]


def _status_for_code(code: str) -> PushStatus:
    """The status a failure code reports (D11).

    Contract (§11.2): ``WriteBusy`` and ``StorageFailure`` report ``rejected``,
    ``CommitOutcomeUnknown`` reports ``unknown``, and conflicts keep their own
    status - so a client can tell "fix and resend" from "retry the same op".
    """
    if code in _CONFLICT_CODES:
        return STATUS_CONFLICT
    if code in _UNKNOWN_CODES:
        return STATUS_UNKNOWN
    return STATUS_REJECTED


def result_from_executed(executed: ExecutedWrite, op_id: uuid.UUID) -> OpResult:
    """A successful kernel outcome -> ``applied``/``replayed`` plus coordinates."""
    status = STATUS_APPLIED if executed.disposition == "applied" else STATUS_REPLAYED
    result = executed.result
    return OpResult(op_id=op_id, status=status, seq=result.seq, rev=result.rev)


def build_command(workspace_id: uuid.UUID, spec: PushOpSpec) -> Command | OpResult:
    """Turn one validated spec into its kernel command, or a ``rejected`` result.

    The route already validated the payload's *shape*; this is the structural
    conversion the kernel needs (``base_rev`` int, ``UUID`` ids, no unknown keys).
    A payload that still cannot build a command is reported as ``rejected`` /
    ``invalid_request`` rather than raising: a malformed op inside a well-formed
    batch is a per-op outcome, and the batch must keep going.

    Callers that must not partially apply a batch pre-build **every** command first
    (see :func:`prepare_batch`); this function alone makes no such promise.

    The workspace from the **path** is the only source of ``workspace_id``, which is
    what makes the path authoritative over anything a body could claim (spec §3).
    """
    payload = dict(spec.payload)
    payload.pop("workspace_id", None)
    payload.pop("entity_type", None)
    payload.pop("op", None)
    payload.pop("entity_id", None)
    payload.pop("op_id", None)

    try:
        command_type = command_for(spec.entity_type, spec.op)
        return command_type(
            workspace_id=workspace_id,
            entity_id=spec.entity_id,
            op_id=spec.op_id,
            base_rev=None if spec.base_rev is None else int(spec.base_rev),
            **payload,
        )
    except (SyncKernelError, TypeError, ValueError):
        # ``TypeError`` = an unexpected payload key; ``ValueError``/``InvalidCommand``
        # = a field the kernel refuses. Neither message is forwarded: it can quote
        # the note body.
        return OpResult(op_id=spec.op_id, status=STATUS_REJECTED, code="invalid_request")


def prepare_batch(
    workspace_id: uuid.UUID, specs: Sequence[PushOpSpec]
) -> tuple[Command, ...] | None:
    """Build **every** command up front, or report that the batch cannot run.

    This is the "no partial application" guarantee (plan §11.2). The wire schema
    rejects almost every structural problem already, but the kernel is the final
    authority on what a legal command is (NUL text, blank names, a bad revision
    conversion). If that check happened inside the execution loop, an op at index 7
    could fail *after* indices 0-6 committed - a partially applied batch with no way
    for the client to tell which half landed.

    So the whole batch is converted first. ``None`` means "at least one op is
    unusable": the caller must reject the request without writing anything. This
    performs **no database access**; it is pure construction.

    Because the loop later reuses these exact commands, ``int(base_rev)`` runs once
    here rather than per attempt, and the values the schema bounded (<= 19 digits)
    are the values converted.
    """
    built: list[Command] = []
    for spec in specs:
        command = build_command(workspace_id, spec)
        if isinstance(command, OpResult):
            return None
        built.append(command)
    return tuple(built)
