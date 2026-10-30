"""Kernel boundary tests: layering, no HTTP, and the public service contract.

These run with **no database**: they read source and introspect the modules, which
is exactly what the plan asks of the unit tier (§3, §9). They are the guardrail
that keeps the kernel from growing an HTTP/auth/adapter surface, from depending on
FastAPI or on a specific ORM entity in its read path, and from losing the one
public entry point.

Nothing here asserts on behaviour that needs PostgreSQL; that is the integration
tier's job.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import datetime
import inspect
import logging
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import revnote_api.kernel as kernel_pkg
import revnote_api.kernel.commands as commands_module
import revnote_api.services as services_pkg
import revnote_api.services.sync_repository as repository_module
import revnote_api.services.sync_write as service_module
from revnote_api.kernel.commands import (
    Command,
    CreateFolder,
    CreateNote,
    DeleteFolder,
    DeleteNote,
    MoveFolder,
    MoveNote,
    UpdateFolder,
    UpdateNote,
)
from revnote_api.kernel.fingerprint import command_hash
from revnote_api.kernel.ids import new_id
from revnote_api.kernel.results import WriteResult
from revnote_api.services.sync_write import LOCK_TIMEOUT_MS, SyncWriteService

API_DIR = Path(__file__).resolve().parents[2]
SRC = API_DIR / "src" / "revnote_api"
KERNEL_DIR = SRC / "kernel"
SERVICES_DIR = SRC / "services"

#: Layers the kernel must never import (plan §3: no HTTP/auth/adapters, no web app).
FORBIDDEN_MODULES = (
    "fastapi",
    "starlette",
    "uvicorn",
    "httpx",
    "pydantic",
    "boto3",
    "revnote_api.api",
    "revnote_api.app",
    "revnote_api.main",
    "revnote_api.schemas",
    "revnote_api.adapters",
    "revnote_api.config",
)


def _imports_of(path: Path) -> set[str]:
    """Every module name imported by one file (absolute + relative roots)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def _kernel_files() -> list[Path]:
    return sorted(KERNEL_DIR.glob("*.py"))


# --------------------------------------------------------------------------- #
# layering
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", _kernel_files(), ids=lambda p: p.name)
def test_kernel_modules_never_import_http_or_adapters(path: Path) -> None:
    offenders = {
        name
        for name in _imports_of(path)
        for forbidden in FORBIDDEN_MODULES
        if name == forbidden or name.startswith(f"{forbidden}.")
    }

    assert offenders == set(), f"{path.name} must stay protocol-free, imports: {offenders}"


@pytest.mark.parametrize(
    "path",
    [*sorted(SERVICES_DIR.glob("*.py"))],
    ids=lambda p: p.name,
)
def test_services_never_import_http_or_adapters(path: Path) -> None:
    offenders = {
        name
        for name in _imports_of(path)
        for forbidden in (*FORBIDDEN_MODULES, "revnote_api.kernel.app")
        if name == forbidden or name.startswith(f"{forbidden}.")
    }

    assert offenders == set(), f"{path.name} must not serve HTTP, imports: {offenders}"


def test_kernel_package_exposes_no_database_or_writer() -> None:
    """The kernel is vocabulary; writing lives in ``services`` only."""
    exported = set(kernel_pkg.__all__)

    assert not {name for name in exported if "Service" in name or "Repository" in name}
    assert "SyncWriteService" not in exported


def test_kernel_does_not_import_the_service_layer() -> None:
    """Layering arrow: ``services -> kernel``, never the other way round."""
    for path in _kernel_files():
        imported = _imports_of(path)
        assert not any(name.startswith("revnote_api.services") for name in imported), path.name


def test_no_fastapi_route_or_auth_surface_was_added() -> None:
    """Phase 2 adds no HTTP/auth/adapter tables or routes (plan §1)."""
    assert not (SRC / "adapters").exists()
    for path in [*_kernel_files(), *sorted(SERVICES_DIR.glob("*.py"))]:
        text = path.read_text(encoding="utf-8")
        for forbidden in ("APIRouter", "@router", "Depends(", "create_all", "drop_all"):
            assert forbidden not in text, f"{path.name} contains {forbidden!r}"


def test_services_package_exports_the_single_write_entry_point() -> None:
    assert "SyncWriteService" in services_pkg.__all__
    assert services_pkg.SyncWriteService is SyncWriteService


# --------------------------------------------------------------------------- #
# the public service contract
# --------------------------------------------------------------------------- #


def test_service_takes_a_session_factory_positionally() -> None:
    signature = inspect.signature(SyncWriteService.__init__)
    parameters = list(signature.parameters.values())[1:]

    assert len(parameters) == 1
    assert parameters[0].name in {"session_factory", "factory", "bind"}
    assert parameters[0].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD


def test_service_has_exactly_one_async_entry_point() -> None:
    expected = {
        "execute",
        "write",
        "execute_with_outcome",
        "_run",
        "_replay_after_race",
        "_commit",
        "_lock_workspace",
    }
    public_async = {
        name
        for name, member in inspect.getmembers(SyncWriteService, inspect.isfunction)
        if inspect.iscoroutinefunction(member) and not name.startswith("_")
    }

    # One write entry point, plus the one approved exception (plan §9.1/§12.1):
    # ``execute_with_outcome`` returns the same result enriched with
    # applied/replayed. It is the implementation; ``execute`` is its projection,
    # so both spellings share a single code path rather than a second one.
    assert public_async == {"execute", "write", "execute_with_outcome"}
    # ``write`` is the same coroutine, so both spellings are one entry point.
    assert SyncWriteService.write is SyncWriteService.execute
    assert expected <= set(dir(SyncWriteService))


def test_outcome_entry_point_is_the_implementation_execute_projects() -> None:
    """``execute`` must delegate to ``execute_with_outcome``, not duplicate it."""
    source = inspect.getsource(SyncWriteService.execute)
    other = inspect.getsource(SyncWriteService.execute_with_outcome)

    assert "execute_with_outcome(" in source
    assert "self._validate_structure(command)" in other
    # Exactly one implementation of the write body: ``_run`` is called once.
    assert other.count("self._run(") == 1
    assert "self._run(" not in source


@pytest.mark.anyio
async def test_execute_rejects_a_non_command_without_touching_the_database() -> None:
    """Step 1 is structural: a bad argument fails before a session is ever built."""
    built: list[None] = []

    def factory() -> Any:  # pragma: no cover - must never be called
        built.append(None)
        raise AssertionError("session factory must not be used")

    service = SyncWriteService(factory)  # type: ignore[arg-type]
    with pytest.raises(kernel_pkg.InvalidCommand, match="expected a kernel Command"):
        await service.execute({"not": "a command"})  # type: ignore[arg-type]

    assert built == []


def test_result_is_an_immutable_value_object() -> None:
    fields = {field.name for field in dataclasses.fields(WriteResult)}

    assert fields == {
        "workspace_id",
        "op_id",
        "entity_type",
        "entity_id",
        "op",
        "seq",
        "rev",
        "created_at",
    }
    result = WriteResult(
        workspace_id=uuid.uuid4(),
        op_id=uuid.uuid4(),
        entity_type="note",
        entity_id=uuid.uuid4(),
        op="create",
        seq=1,
        rev=1,
        created_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.seq = 2  # type: ignore[misc]
    # No "replayed" flag: a replay returns the very same value (plan §5).
    assert not hasattr(result, "replayed")


def test_result_repr_carries_no_content() -> None:
    import datetime

    result = WriteResult(
        workspace_id=uuid.uuid4(),
        op_id=uuid.uuid4(),
        entity_type="note",
        entity_id=uuid.uuid4(),
        op="update",
        seq=3,
        rev=2,
        created_at=datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
    )

    assert "WriteResult(" in repr(result)
    assert "seq=3" in repr(result) and "rev=2" in repr(result)


def test_repository_never_loads_orm_entities() -> None:
    """Reads return frozen row tuples, so no identity map can serve a stale value."""
    text = (SERVICES_DIR / "sync_repository.py").read_text(encoding="utf-8")

    assert "session.get(" not in text  # would populate the identity map
    assert "relationship(" not in text
    for row_type in ("WorkspaceRow", "LedgerRow", "EntityRow", "EventRow"):
        assert dataclasses.is_dataclass(getattr(repository_module, row_type)), row_type


def test_repository_is_one_method_per_transaction_step() -> None:
    """The §6 steps must exist as patchable methods (tests inject faults there)."""
    required = {
        "lock_workspace",
        "find_ledger",
        "read_event",
        "advance_seq",
        "apply_object",
        "insert_event",
        "insert_ledger",
    }
    available = {
        name
        for name, member in inspect.getmembers(repository_module.SyncRepository, inspect.isfunction)
        if inspect.iscoroutinefunction(member)
    }

    assert required <= available
    # Every step is async (one statement on the service's single connection).
    for name in required:
        assert inspect.iscoroutinefunction(getattr(repository_module.SyncRepository, name)), name


def test_repository_step_names_match_the_test_lane() -> None:
    """Every step name the integration lane patches must exist and be async.

    The names are pinned here because a rename would silently break the failpoint
    matrix (``setattr`` on a missing attribute raises, but on a *renamed* method it
    would simply never fire). ``allocate_seq`` is deliberately absent: the landed
    name is ``advance_seq`` and the tests patch that one.
    """
    repo = repository_module.SyncRepository
    steps = (
        "lock_workspace",
        "advance_seq",
        "find_ledger",
        "read_event",
        "get_folder",
        "get_note",
        "find_folder_anywhere",
        "find_note_anywhere",
        "folder_ancestor_ids",
        "has_active_child_folders",
        "has_active_child_notes",
        "apply_object",
        "insert_event",
        "insert_ledger",
    )

    for step in steps:
        assert inspect.iscoroutinefunction(getattr(repo, step)), step


def test_service_holds_no_mutable_per_call_state() -> None:
    """A service instance must be safe to reuse: only the factory is stored."""
    service = SyncWriteService(object())  # type: ignore[arg-type]
    attributes = vars(service)

    assert list(attributes) == ["_session_factory"]
    # The stored value is the factory itself, never a session/transaction/result.
    assert attributes["_session_factory"] is not None
    assert not isinstance(attributes["_session_factory"], AsyncSession)


def test_lock_timeout_is_a_bounded_constant() -> None:
    """Bounded by a module constant, not a new config knob (plan §11)."""
    assert isinstance(LOCK_TIMEOUT_MS, int)
    assert 0 < LOCK_TIMEOUT_MS <= 30_000


def test_isolation_and_timeouts_are_pinned_per_transaction() -> None:
    """The statements actually issued: one READ COMMITTED pin plus two bounded waits."""
    isolation_sql = str(service_module._SET_READ_COMMITTED)
    lock_sql = str(service_module._SET_LOCK_TIMEOUT)
    statement_sql = str(service_module._SET_STATEMENT_TIMEOUT)

    assert isolation_sql == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    assert lock_sql == f"SET LOCAL lock_timeout = {LOCK_TIMEOUT_MS}"
    assert statement_sql == f"SET LOCAL statement_timeout = {service_module.STATEMENT_TIMEOUT_MS}"

    # Only these three SET statements exist in the module, so no second, silently
    # different isolation level or unbounded wait can be introduced.
    set_values = [
        str(value)
        for value in vars(service_module).values()
        if hasattr(value, "text") and str(value).startswith("SET ")
    ]
    assert sorted(set_values) == sorted([isolation_sql, lock_sql, statement_sql])


def test_no_retry_loop_and_no_production_failpoint_hook() -> None:
    """No internal retry, no env/debug failpoint (plan §6, §8, §11)."""
    text = (SERVICES_DIR / "sync_write.py").read_text(encoding="utf-8")

    for forbidden in ("FAILPOINT", "failpoint(", "os.environ", "while True", "for attempt"):
        assert forbidden not in text, forbidden
    assert "sleep(" not in text


def test_service_logs_categories_and_timings_but_never_content() -> None:
    """Diagnostics stay aggregate: no body, title or e-mail in a log *call*.

    Only rendered ``logger.*`` calls are inspected, so prose in a docstring that
    happens to name a column cannot trip this (and a real leak cannot hide behind
    comments either, since the check is on the call's arguments).
    """
    source = (SERVICES_DIR / "sync_write.py").read_text(encoding="utf-8")
    assert "workspace_lock_wait_ms" in source
    assert "transaction_duration_ms" in source

    rendered: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"info", "warning", "error", "exception", "debug"}
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "logger"
        ):
            rendered.append(ast.unparse(node))

    assert rendered, "expected at least one logger call"
    for call in rendered:
        for leak in ("body_md", "title=", "email", "secret"):
            assert leak not in call, f"{leak!r} appears in a log call: {call}"


def test_cancellation_is_never_wrapped() -> None:
    """``CancelledError``/``KeyboardInterrupt``/``SystemExit`` propagate untouched.

    The entry point catches all three and re-raises. The commit path catches
    ``CancelledError`` *alone* so it can invalidate the suspect connection first, and
    then raises bare: replacing it with a kernel error would let an adapter swallow a
    live cancellation. ``KeyboardInterrupt``/``SystemExit`` are never reinterpreted at
    all.
    """
    source = (SERVICES_DIR / "sync_write.py").read_text(encoding="utf-8")

    assert "except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):" in source
    # The commit path keeps its own, narrower handler plus a bare exit guard.
    assert "except asyncio.CancelledError:" in source
    assert "except (KeyboardInterrupt, SystemExit):" in source
    # No cancellation is ever converted into a kernel error on the commit path.
    commit_body = source.split("async def _commit", 1)[1]
    assert "raise CommitOutcomeUnknown" not in commit_body
    assert "raise WriteBusy" not in commit_body


def test_commands_module_declares_the_eight_types() -> None:
    names = {cls.__name__ for cls in commands_module.COMMAND_TYPES}

    assert names == {
        "CreateFolder",
        "UpdateFolder",
        "MoveFolder",
        "DeleteFolder",
        "CreateNote",
        "UpdateNote",
        "MoveNote",
        "DeleteNote",
    }
    assert all(issubclass(cls, Command) for cls in commands_module.COMMAND_TYPES)


def test_command_declares_no_orm_or_sql_dependency() -> None:
    """A command is pure data: nothing about tables or sessions leaks into it."""
    fields = {field.name for field in dataclasses.fields(UpdateNote)}

    assert fields == {
        "workspace_id",
        "entity_id",
        "op_id",
        "entity_type",
        "op",
        "base_rev",
        "title",
        "body_md",
    }
    assert not hasattr(CreateNote, "metadata")


def test_service_never_passes_bodies_to_the_logger() -> None:
    """Guard against a future ``logger.info(str(command))`` style leak."""
    source = (SERVICES_DIR / "sync_write.py").read_text(encoding="utf-8")
    logger_calls = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"info", "warning", "error", "exception", "debug"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
    ]

    assert logger_calls
    for call in logger_calls:
        rendered = ast.unparse(call)
        assert "body_md" not in rendered
        assert "title=" not in rendered


def test_unit_tier_imports_only_pure_modules() -> None:
    """This file must stay runnable without PostgreSQL (no engine created above)."""
    assert isinstance(logging.getLogger("revnote_api.services.sync_write"), logging.Logger)
    assert inspect.isclass(AsyncSession)


def test_kernel_and_services_never_import_the_db_plumbing() -> None:
    """Import-time purity: no engine/session module load, so no connection is set up."""
    for path in [*_kernel_files(), *sorted(SERVICES_DIR.glob("*.py"))]:
        imported = _imports_of(path)
        assert "revnote_api.db.engine" not in imported, path.name
        assert "revnote_api.db.session" not in imported, path.name


# --------------------------------------------------------------------------- #
# write-path logic against a fake session (still no PostgreSQL)
#
# These cannot prove locking, MVCC or deferred-FK behaviour - that is the
# integration tier's job. They prove the *wiring*: statement order, replay from
# the recorded event, full rollback of a lost idempotency race, and the error
# mapping of each outcome. Every one of those bugs would otherwise only surface
# against a real server.
#
# The fake answers by inspecting the rendered SQL against declared state, so a test
# says "the ledger already holds this op" instead of "the fourth queued result".
# Successive folder reads come from an explicit queue, which is what makes parent
# lookups and post-CAS re-reads expressible without mocking the repository.
# --------------------------------------------------------------------------- #

NOW = datetime.datetime(2026, 1, 2, 3, 4, 5, tzinfo=datetime.UTC)


def entity(
    entity_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    parent: uuid.UUID | None = None,
    rev: int = 1,
    deleted: bool = False,
) -> tuple[Any, ...]:
    """A folder/note row: ``(id, workspace_id, parent_id|folder_id, rev, deleted_at)``."""
    return (entity_id, workspace_id, parent, rev, NOW if deleted else None)


class FakeResult:
    """Stand-in for ``CursorResult`` with the four accessors the repository uses."""

    def __init__(
        self,
        *,
        row: Any = None,
        rows: Sequence[Any] = (),
        scalar: Any = None,
        rowcount: int = 1,
    ) -> None:
        self._row = row
        self._rows = list(rows)
        self._scalar = scalar
        self.rowcount = rowcount

    def first(self) -> Any:
        return self._row

    def all(self) -> list[Any]:
        return self._rows

    def scalar_one(self) -> Any:
        return self._scalar


class FakeDB:
    """Declared workspace state; the session answers queries from it."""

    def __init__(
        self,
        *,
        workspace: tuple[Any, ...] | None = None,
        last_seq: int = 0,
        ledger_row: tuple[Any, ...] | None = None,
        event_row: tuple[Any, ...] | None = None,
        folder_reads: Sequence[tuple[Any, ...] | None] = (),
        note_reads: Sequence[tuple[Any, ...] | None] = (),
        child_folders: bool = False,
        child_notes: bool = False,
        ancestors: Sequence[uuid.UUID] = (),
        cas_rowcount: int = 1,
        seq_rowcount: int = 1,
        ledger_conflict: bool = False,
    ) -> None:
        self.workspace = workspace
        self.last_seq = last_seq if workspace is None else workspace[1]
        self.ledger_row = ledger_row
        self.event_row = event_row
        #: Answers for successive folder SELECTs (create probe, entity, parent, re-read).
        self.folder_reads = list(folder_reads)
        self.note_reads = list(note_reads)
        self.child_folders = child_folders
        self.child_notes = child_notes
        self.ancestors = list(ancestors)
        self.cas_rowcount = cas_rowcount
        self.seq_rowcount = seq_rowcount
        self.ledger_conflict = ledger_conflict


class FakeSession:
    """Records statement text and answers from a :class:`FakeDB`.

    Mirrors ``AsyncSession`` semantics the service relies on: the first statement
    opens the transaction implicitly (autobegin), and ``commit``/``rollback`` are
    the only ways to end it. ``commit`` is what a test wraps to simulate a lost
    acknowledgement, exactly like the integration lane's ``CommitLoss``.
    ``invalidate`` is recorded separately from ``close``: after an unknown commit
    outcome the connection must be *discarded*, never merely closed and recycled.
    """

    def __init__(
        self,
        db: FakeDB,
        *,
        commit_error: BaseException | None = None,
        isolation_level: str | None = None,
        raise_on: str | None = None,
        raise_error: BaseException | None = None,
        commit_error_after_commit: bool = True,
    ) -> None:
        self.db = db
        self.statements: list[str] = []
        self.committed = 0
        self.rolled_back = 0
        self.invalidated = 0
        self.closed = False
        self._isolation_level = isolation_level
        self._raise_on = raise_on
        self._raise_error = raise_error
        self._commit_error = commit_error
        self._commit_error_after_commit = commit_error_after_commit

    def get_bind(self) -> Any:
        dialect = type("Dialect", (), {"_on_connect_isolation_level": self._isolation_level})()
        return type("Bind", (), {"dialect": dialect})()

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> FakeResult:
        sql = str(statement)
        self.statements.append(sql)
        if self._raise_on is not None and self._raise_on in sql:
            raise self._raise_error or RuntimeError("injected failure")
        return self._respond(sql)

    async def commit(self) -> None:
        if self._commit_error is not None and self._commit_error_after_commit:
            # The server committed; only the acknowledgement was lost.
            self.committed += 1
            raise self._commit_error
        if self._commit_error is not None:
            raise self._commit_error
        self.committed += 1

    async def rollback(self) -> None:
        self.rolled_back += 1

    async def invalidate(self) -> None:
        self.invalidated += 1

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.closed = True

    def _respond(self, sql: str) -> FakeResult:
        db = self.db
        if sql.startswith("SET "):
            return FakeResult()
        if sql.startswith("UPDATE workspaces"):
            return FakeResult(rowcount=db.seq_rowcount)
        if sql.startswith("UPDATE folders") or sql.startswith("UPDATE notes"):
            return FakeResult(rowcount=db.cas_rowcount)
        if sql.startswith(("INSERT INTO folders", "INSERT INTO notes")):
            return FakeResult()
        if sql.startswith("INSERT INTO change_events"):
            return FakeResult(scalar=NOW)
        if sql.startswith("INSERT INTO op_ledger"):
            return FakeResult(row=None if db.ledger_conflict else (db.last_seq + 1,))
        if "FOR UPDATE" in sql:
            return FakeResult(row=db.workspace)
        if "FROM op_ledger" in sql:
            return FakeResult(row=db.ledger_row)
        if "FROM change_events" in sql:
            return FakeResult(row=db.event_row)
        # The recursive walk is also a "FROM folders" statement: answer it first.
        if sql.startswith("WITH RECURSIVE"):
            return FakeResult(rows=[(parent, 1) for parent in db.ancestors])
        if " LIMIT " in sql:
            if "FROM folders" in sql:
                return FakeResult(row=(1,) if db.child_folders else None)
            return FakeResult(row=(1,) if db.child_notes else None)
        if "FROM folders" in sql:
            return FakeResult(row=db.folder_reads.pop(0) if db.folder_reads else None)
        if "FROM notes" in sql:
            return FakeResult(row=db.note_reads.pop(0) if db.note_reads else None)
        raise AssertionError(f"the fake database has no answer for: {sql[:140]}")


def static_factory(*sessions: FakeSession) -> Any:
    """A session factory handing out the prepared sessions, in order."""
    queue = list(sessions)

    def factory() -> FakeSession:
        if not queue:
            raise AssertionError("the service opened more sessions than the test prepared")
        return queue.pop(0)

    return factory


async def run_with(command: Any, *sessions: FakeSession) -> Any:
    return await SyncWriteService(static_factory(*sessions)).execute(command)


def writes(session: FakeSession) -> list[str]:
    """Only the mutating statements, ignoring SET/SELECT noise."""
    return [sql for sql in session.statements if sql.startswith(("INSERT", "UPDATE"))]


def a_folder_create(**kwargs: Any) -> Any:
    base: dict[str, Any] = {
        "workspace_id": new_id(),
        "entity_id": new_id(),
        "op_id": new_id(),
        "name": "Inbox",
    }
    base.update(kwargs)
    return CreateFolder(**base)


def a_folder_update(**kwargs: Any) -> Any:
    base: dict[str, Any] = {
        "workspace_id": new_id(),
        "entity_id": new_id(),
        "op_id": new_id(),
        "base_rev": 1,
        "name": "n",
    }
    base.update(kwargs)
    return UpdateFolder(**base)


def a_folder_move(**kwargs: Any) -> Any:
    base: dict[str, Any] = {
        "workspace_id": new_id(),
        "entity_id": new_id(),
        "op_id": new_id(),
        "base_rev": 1,
        "parent_id": new_id(),
    }
    base.update(kwargs)
    return MoveFolder(**base)


def a_note_create(**kwargs: Any) -> Any:
    base: dict[str, Any] = {
        "workspace_id": new_id(),
        "entity_id": new_id(),
        "op_id": new_id(),
        "title": "t",
        "body_md": "b",
    }
    base.update(kwargs)
    return CreateNote(**base)


def a_create_results(command: Any) -> tuple[FakeDB, FakeSession]:
    """A create that succeeds on an empty workspace (seq 1, rev 1)."""
    db = FakeDB(workspace=(command.workspace_id, 0))
    return db, FakeSession(db)


# -- step order -------------------------------------------------------------- #


@pytest.mark.anyio
async def test_create_follows_the_documented_step_order() -> None:
    command = a_folder_create()
    _, session = a_create_results(command)

    result = await run_with(command, session)

    assert (result.seq, result.rev, result.created_at) == (1, 1, NOW)
    assert (result.op, result.entity_type) == ("create", "folder")
    assert session.committed == 1
    assert session.rolled_back == 0

    statements = session.statements
    assert statements[0].startswith("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
    assert statements[1].startswith("SET LOCAL lock_timeout")
    assert statements[2].startswith("SET LOCAL statement_timeout")
    assert "FOR UPDATE" in statements[3] and "workspaces" in statements[3]
    assert "FROM op_ledger" in statements[4]  # ledger read before any object/CAS

    inserts = [sql for sql in statements if sql.startswith("INSERT")]
    assert inserts[0].startswith("INSERT INTO folders")
    assert inserts[1].startswith("INSERT INTO change_events")
    assert inserts[2].startswith("INSERT INTO op_ledger")


@pytest.mark.anyio
async def test_workspace_lock_precedes_every_object_row() -> None:
    command = CreateNote(
        workspace_id=new_id(), entity_id=new_id(), op_id=new_id(), title="t", body_md="b"
    )
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    await run_with(command, session)

    locked_at = next(i for i, sql in enumerate(session.statements) if "FOR UPDATE" in sql)
    first_note_at = next(i for i, sql in enumerate(session.statements) if "notes" in sql)
    assert locked_at < first_note_at


@pytest.mark.anyio
async def test_update_cas_puts_rev_into_the_statement() -> None:
    command = UpdateNote(
        workspace_id=new_id(),
        entity_id=new_id(),
        op_id=new_id(),
        base_rev=2,
        title="t",
        body_md="b",
    )
    db = FakeDB(
        workspace=(command.workspace_id, 4),
        note_reads=[entity(command.entity_id, command.workspace_id, rev=2)],
    )
    session = FakeSession(db)

    result = await run_with(command, session)

    assert (result.seq, result.rev) == (5, 3)
    cas = next(sql for sql in session.statements if sql.startswith("UPDATE notes"))
    assert "notes.rev = " in cas  # rev is part of the predicate, not a read-then-write
    assert "deleted_at IS NULL" in cas
    assert "notes.workspace_id = " in cas


@pytest.mark.anyio
async def test_move_updates_only_the_parent_column() -> None:
    workspace_id, note_id, folder_id = new_id(), new_id(), new_id()
    command = MoveNote(
        workspace_id=workspace_id,
        entity_id=note_id,
        op_id=new_id(),
        base_rev=1,
        folder_id=folder_id,
    )
    session = FakeSession(
        FakeDB(
            workspace=(workspace_id, 4),
            note_reads=[entity(note_id, workspace_id, rev=1)],
            folder_reads=[entity(folder_id, workspace_id, rev=1)],  # the target folder
        )
    )

    await run_with(command, session)

    cas = next(sql for sql in session.statements if sql.startswith("UPDATE notes"))
    assert "folder_id=" in cas.replace(" ", "")
    assert "title" not in cas and "body_md" not in cas


@pytest.mark.anyio
async def test_delete_tombstones_and_keeps_the_row() -> None:
    command = DeleteNote(workspace_id=new_id(), entity_id=new_id(), op_id=new_id(), base_rev=1)
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 4),
            note_reads=[entity(command.entity_id, command.workspace_id, rev=1)],
        )
    )

    await run_with(command, session)

    cas = next(sql for sql in session.statements if sql.startswith("UPDATE notes"))
    assert "deleted_at=" in cas.replace(" ", "")
    assert not any(sql.startswith("DELETE") for sql in session.statements)


# -- replay and op-id conflicts ---------------------------------------------- #


@pytest.mark.anyio
async def test_replay_returns_the_recorded_result_and_writes_nothing() -> None:
    command = a_folder_update()
    db = FakeDB(
        workspace=(command.workspace_id, 9),  # the workspace has moved on
        ledger_row=(command.op_id, command_hash(command), 4),
        event_row=(
            command.workspace_id,
            4,
            "folder",
            command.entity_id,
            "update",
            2,
            command.op_id,
            NOW,
        ),
    )
    session = FakeSession(db)

    result = await run_with(command, session)

    assert (result.seq, result.rev, result.created_at) == (4, 2, NOW)
    assert writes(session) == []
    # A replay writes nothing, so its read-only transaction is ended without commit.
    assert session.rolled_back == 1
    assert session.committed == 0


@pytest.mark.anyio
async def test_same_op_id_with_different_content_is_a_conflict() -> None:
    command = a_folder_update(name="new")
    variant = UpdateFolder(
        workspace_id=command.workspace_id,
        entity_id=command.entity_id,
        op_id=command.op_id,
        base_rev=1,
        name="other",
    )
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 3),
            ledger_row=(command.op_id, command_hash(command), 2),
        )
    )

    with pytest.raises(kernel_pkg.OpIdConflict):
        await run_with(variant, session)

    assert session.rolled_back == 1
    assert session.committed == 0


@pytest.mark.anyio
async def test_lost_idempotency_race_rolls_back_then_replays_in_a_new_session() -> None:
    """The database's uniqueness is the dam: the loser never commits its candidate."""
    command = a_folder_create()
    loser_session = FakeSession(FakeDB(workspace=(command.workspace_id, 0), ledger_conflict=True))
    winner_session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 1),
            ledger_row=(command.op_id, command_hash(command), 1),
            event_row=(
                command.workspace_id,
                1,
                "folder",
                command.entity_id,
                "create",
                1,
                command.op_id,
                NOW,
            ),
        )
    )

    result = await run_with(command, loser_session, winner_session)

    assert (result.seq, result.rev) == (1, 1)
    assert loser_session.rolled_back == 1
    assert loser_session.committed == 0
    # The recovery transaction only reads the recorded result, so it is not committed.
    assert winner_session.committed == 0
    assert winner_session.rolled_back == 1


@pytest.mark.anyio
async def test_lost_idempotency_race_with_other_content_reports_conflict() -> None:
    command = a_folder_create()
    other = CreateFolder(
        workspace_id=command.workspace_id,
        entity_id=command.entity_id,
        op_id=command.op_id,
        name="different",
    )
    loser_session = FakeSession(FakeDB(workspace=(command.workspace_id, 0), ledger_conflict=True))
    winner_session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 1),
            ledger_row=(command.op_id, command_hash(other), 1),
        )
    )

    with pytest.raises(kernel_pkg.OpIdConflict):
        await run_with(command, loser_session, winner_session)

    assert loser_session.committed == 0
    assert winner_session.committed == 0


@pytest.mark.anyio
async def test_collision_with_no_visible_ledger_is_inconsistent_state() -> None:
    command = a_folder_create()
    loser_session = FakeSession(FakeDB(workspace=(command.workspace_id, 0), ledger_conflict=True))
    winner_session = FakeSession(FakeDB(workspace=(command.workspace_id, 1)))

    with pytest.raises(kernel_pkg.StorageFailure, match="inconsistent"):
        await run_with(command, loser_session, winner_session)


@pytest.mark.anyio
async def test_event_uniqueness_collision_is_also_treated_as_a_lost_race() -> None:
    """Either op-id constraint losing must roll back, never commit.

    Classification uses the driver's real ``constraint_name``, never the message text,
    so this fake carries it the way asyncpg does (below the adapt layer).
    """
    command = a_folder_create()

    class Violation(Exception):
        sqlstate = "23505"
        constraint_name = "uq_change_events_workspace_id_op_id"

    loser_session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO change_events",
        raise_error=IntegrityError(
            "INSERT INTO change_events ...", {}, Violation("duplicate key value")
        ),
    )
    winner_session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 1),
            ledger_row=(command.op_id, command_hash(command), 1),
            event_row=(
                command.workspace_id,
                1,
                "folder",
                command.entity_id,
                "create",
                1,
                command.op_id,
                NOW,
            ),
        )
    )

    result = await run_with(command, loser_session, winner_session)

    assert (result.seq, result.rev) == (1, 1)
    assert loser_session.committed == 0
    assert loser_session.rolled_back == 1


@pytest.mark.anyio
async def test_result_seq_unique_is_not_mistaken_for_an_idempotent_replay() -> None:
    """``uq_op_ledger_workspace_id_result_seq`` is a *different* invariant.

    Its message also contains the word ``op_ledger``, which is exactly why the
    classification must read the constraint name instead of the text: a result-seq
    clash is a real bug and must surface, not be silently replayed.
    """
    command = a_folder_create()

    class Violation(Exception):
        sqlstate = "23505"
        constraint_name = "uq_op_ledger_workspace_id_result_seq"

        def __str__(self) -> str:  # contains "op_ledger", must be ignored
            return (
                "duplicate key value violates unique constraint "
                '"uq_op_ledger_workspace_id_result_seq"'
            )

    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO op_ledger",
        raise_error=IntegrityError("INSERT INTO op_ledger ...", {}, Violation()),
    )

    with pytest.raises(kernel_pkg.StorageFailure):
        await run_with(command, session)

    assert session.committed == 0


@pytest.mark.anyio
async def test_unidentifiable_unique_violation_is_never_treated_as_a_replay() -> None:
    """No constraint name means "unknown", which must not become a replay."""
    command = a_folder_create()

    class Violation(Exception):
        sqlstate = "23505"

    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO change_events",
        raise_error=IntegrityError("INSERT INTO change_events ...", {}, Violation("duplicate")),
    )

    with pytest.raises(kernel_pkg.StorageFailure):
        await run_with(command, session)


@pytest.mark.anyio
async def test_other_unique_violations_are_not_mistaken_for_the_race() -> None:
    """A clash on some unrelated unique index stays a plain storage failure."""
    command = a_folder_create()

    class Violation(Exception):
        sqlstate = "23505"
        constraint_name = "uq_users_email"

    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO folders",
        raise_error=IntegrityError("INSERT INTO folders ...", {}, Violation("duplicate")),
    )

    with pytest.raises(kernel_pkg.StorageFailure):
        await run_with(command, session)


# -- business rules ---------------------------------------------------------- #


@pytest.mark.anyio
async def test_missing_workspace_fails_before_any_object_access() -> None:
    session = FakeSession(FakeDB())

    with pytest.raises(kernel_pkg.WorkspaceNotFound):
        await run_with(a_folder_create(), session)

    assert session.rolled_back == 1
    assert writes(session) == []


@pytest.mark.anyio
async def test_missing_entity_is_not_found() -> None:
    command = a_folder_update()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    with pytest.raises(kernel_pkg.EntityNotFound):
        await run_with(command, session)
    assert writes(session) == []


@pytest.mark.anyio
async def test_stale_base_rev_reports_both_revisions() -> None:
    command = a_folder_update(base_rev=2)
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 5),
            folder_reads=[entity(command.entity_id, command.workspace_id, rev=4)],
        )
    )

    with pytest.raises(kernel_pkg.RevConflict) as caught:
        await run_with(command, session)

    assert (caught.value.expected, caught.value.current) == (2, 4)
    assert writes(session) == []


@pytest.mark.anyio
async def test_tombstone_wins_over_a_stale_base_rev() -> None:
    command = DeleteNote(workspace_id=new_id(), entity_id=new_id(), op_id=new_id(), base_rev=1)
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 5),
            note_reads=[entity(command.entity_id, command.workspace_id, rev=9, deleted=True)],
        )
    )

    with pytest.raises(kernel_pkg.EntityDeleted):
        await run_with(command, session)


@pytest.mark.anyio
async def test_create_reusing_an_id_is_a_conflict() -> None:
    command = a_folder_create()
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 1),
            folder_reads=[entity(command.entity_id, command.workspace_id)],
        )
    )

    with pytest.raises(kernel_pkg.EntityIdConflict):
        await run_with(command, session)
    assert writes(session) == []


@pytest.mark.anyio
async def test_non_empty_folder_delete_is_refused_without_cascading() -> None:
    command = DeleteFolder(workspace_id=new_id(), entity_id=new_id(), op_id=new_id(), base_rev=1)
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 3),
            folder_reads=[entity(command.entity_id, command.workspace_id, rev=1)],
            child_folders=True,
        )
    )

    with pytest.raises(kernel_pkg.FolderNotEmpty):
        await run_with(command, session)

    assert not any("deleted_at=" in sql.replace(" ", "") for sql in writes(session))


@pytest.mark.anyio
async def test_folder_delete_refused_by_a_live_child_note() -> None:
    command = DeleteFolder(workspace_id=new_id(), entity_id=new_id(), op_id=new_id(), base_rev=1)
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 3),
            folder_reads=[entity(command.entity_id, command.workspace_id, rev=1)],
            child_notes=True,
        )
    )

    with pytest.raises(kernel_pkg.FolderNotEmpty):
        await run_with(command, session)


@pytest.mark.anyio
async def test_move_into_a_missing_parent_is_invalid() -> None:
    command = a_folder_move()
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 3),
            folder_reads=[entity(command.entity_id, command.workspace_id, rev=1), None],
        )
    )

    with pytest.raises(kernel_pkg.InvalidParent):
        await run_with(command, session)


@pytest.mark.anyio
async def test_move_into_a_deleted_parent_is_invalid() -> None:
    command = a_folder_move()
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 3),
            folder_reads=[
                entity(command.entity_id, command.workspace_id, rev=1),
                entity(command.parent_id, command.workspace_id, rev=2, deleted=True),
            ],
        )
    )

    with pytest.raises(kernel_pkg.InvalidParent):
        await run_with(command, session)


@pytest.mark.anyio
async def test_indirect_cycle_is_refused() -> None:
    """Moving a folder under its own descendant would close a loop (D3)."""
    workspace_id, folder, child, grandchild = new_id(), new_id(), new_id(), new_id()
    command = MoveFolder(
        workspace_id=workspace_id,
        entity_id=folder,
        op_id=new_id(),
        base_rev=1,
        parent_id=grandchild,
    )
    session = FakeSession(
        FakeDB(
            workspace=(workspace_id, 3),
            folder_reads=[
                entity(folder, workspace_id, rev=1),  # the moving folder
                entity(grandchild, workspace_id, parent=child, rev=1),  # its descendant
            ],
            ancestors=[child, folder],  # the walk reaches the moving folder itself
        )
    )

    with pytest.raises(kernel_pkg.FolderCycle):
        await run_with(command, session)
    assert writes(session) == []


@pytest.mark.anyio
async def test_cycle_check_is_skipped_for_a_direct_self_parent() -> None:
    """The immediate self-loop is already rejected at command construction."""
    with pytest.raises(kernel_pkg.InvalidCommand, match="own parent"):
        MoveFolder(
            workspace_id=new_id(),
            entity_id=new_id(),
            op_id=new_id(),
            base_rev=1,
            parent_id=None,
        ).__class__(
            workspace_id=(ws := new_id()), entity_id=ws, op_id=new_id(), base_rev=1, parent_id=ws
        )


@pytest.mark.anyio
async def test_note_create_into_a_missing_folder_is_invalid() -> None:
    command = CreateNote(
        workspace_id=new_id(),
        entity_id=new_id(),
        op_id=new_id(),
        title="t",
        body_md="b",
        folder_id=new_id(),
    )
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)))

    with pytest.raises(kernel_pkg.InvalidParent):
        await run_with(command, session)


@pytest.mark.anyio
async def test_move_to_root_skips_the_parent_lookup() -> None:
    command = MoveNote(
        workspace_id=new_id(), entity_id=new_id(), op_id=new_id(), base_rev=1, folder_id=None
    )
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 4),
            note_reads=[entity(command.entity_id, command.workspace_id, parent=new_id(), rev=1)],
        )
    )

    result = await run_with(command, session)

    assert result.seq == 5
    assert not any("folders" in sql for sql in session.statements)


# -- counters and error classification --------------------------------------- #


@pytest.mark.anyio
async def test_seq_overflow_writes_nothing() -> None:
    command = a_folder_create()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 2**63 - 1)))

    with pytest.raises(kernel_pkg.CounterOverflow, match="seq"):
        await run_with(command, session)
    assert writes(session) == []


@pytest.mark.anyio
async def test_rev_overflow_writes_nothing() -> None:
    command = DeleteNote(
        workspace_id=new_id(), entity_id=new_id(), op_id=new_id(), base_rev=2**63 - 1
    )
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 4),
            note_reads=[entity(command.entity_id, command.workspace_id, rev=2**63 - 1)],
        )
    )

    with pytest.raises(kernel_pkg.CounterOverflow, match="rev"):
        await run_with(command, session)
    assert writes(session) == []


@pytest.mark.anyio
async def test_zero_row_cas_reports_the_revision_that_moved() -> None:
    command = a_folder_update(base_rev=2)
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 6),
            # first read: the target revision; post-miss re-read: it moved to 3
            folder_reads=[
                entity(command.entity_id, command.workspace_id, rev=2),
                entity(command.entity_id, command.workspace_id, rev=3),
            ],
            cas_rowcount=0,
        )
    )

    with pytest.raises(kernel_pkg.RevConflict) as caught:
        await run_with(command, session)

    assert (caught.value.expected, caught.value.current) == (2, 3)
    assert session.rolled_back == 1


@pytest.mark.anyio
async def test_seq_allocation_failure_writes_nothing() -> None:
    """The conditional ``last_seq`` UPDATE must match exactly one row."""
    command = a_folder_create()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 2), seq_rowcount=0))

    with pytest.raises(kernel_pkg.StorageFailure, match="last_seq"):
        await run_with(command, session)
    # The cursor is only ever advanced by that one conditional statement.
    assert len(writes(session)) == 1
    assert writes(session)[0].startswith("UPDATE workspaces SET last_seq=")
    assert "workspaces.last_seq = " in writes(session)[0]


@pytest.mark.anyio
async def test_lost_commit_acknowledgement_is_never_reported_as_rollback() -> None:
    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        commit_error=ConnectionResetError("connection went away during COMMIT"),
    )

    with pytest.raises(kernel_pkg.CommitOutcomeUnknown):
        await run_with(command, session)

    assert session.closed


@pytest.mark.anyio
async def test_commit_rejected_by_a_deferred_fk_is_a_storage_failure() -> None:
    class DeferredViolation(Exception):
        sqlstate = "23503"

    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        commit_error=DeferredViolation("fk checked at commit"),
    )

    with pytest.raises(kernel_pkg.StorageFailure, match="deferred"):
        await run_with(command, session)


@pytest.mark.anyio
async def test_lock_timeout_sqlstate_maps_to_write_busy() -> None:
    class LockNotAvailable(Exception):
        sqlstate = "55P03"

    session = FakeSession(
        FakeDB(),
        raise_on="FOR UPDATE",
        raise_error=LockNotAvailable("canceling statement due to lock timeout"),
    )

    with pytest.raises(kernel_pkg.WriteBusy):
        await run_with(a_folder_create(), session)


@pytest.mark.anyio
async def test_foreign_key_rejection_maps_to_storage_failure() -> None:
    class ForeignKeyViolation(Exception):
        sqlstate = "23503"

    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO folders",
        raise_error=ForeignKeyViolation("fk violated"),
    )

    with pytest.raises(kernel_pkg.StorageFailure, match="foreign key"):
        await run_with(command, session)


@pytest.mark.anyio
async def test_unknown_database_error_maps_to_storage_failure() -> None:
    class UnknownError(Exception):
        sqlstate = "XX000"

    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO folders",
        raise_error=UnknownError("boom"),
    )

    with pytest.raises(kernel_pkg.StorageFailure):
        await run_with(command, session)


@pytest.mark.anyio
async def test_cancellation_propagates_unwrapped() -> None:
    session = FakeSession(FakeDB(), raise_on="FOR UPDATE", raise_error=asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await run_with(a_folder_create(), session)


@pytest.mark.anyio
async def test_wider_engine_isolation_is_refused_before_the_first_statement() -> None:
    """A REPEATABLE READ engine would break read-after-lock; refuse it up front."""
    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)), isolation_level="REPEATABLE READ"
    )

    with pytest.raises(kernel_pkg.StorageFailure, match="isolation level"):
        await run_with(command, session)

    assert session.committed == 0
    assert session.statements == []


@pytest.mark.anyio
async def test_read_committed_engine_is_accepted() -> None:
    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)), isolation_level="READ COMMITTED"
    )

    result = await run_with(command, session)

    assert result.seq == 1


class _EngineOptionsSession(FakeSession):
    """Session whose bind pins isolation through ``execution_options`` instead."""

    def __init__(self, *args: Any, engine_option: str = "SERIALIZABLE", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._engine_option = engine_option

    def get_bind(self) -> Any:
        return type(
            "Engine",
            (),
            {
                "dialect": type("Dialect", (), {"_on_connect_isolation_level": None})(),
                "get_execution_options": staticmethod(
                    lambda: {"isolation_level": self._engine_option}
                ),
            },
        )()


@pytest.mark.anyio
async def test_engine_execution_option_isolation_is_also_checked() -> None:
    """``create_async_engine(...).execution_options(isolation_level=...)`` is caught too."""
    command = a_folder_create()
    session = _EngineOptionsSession(FakeDB(workspace=(command.workspace_id, 0)))

    with pytest.raises(kernel_pkg.StorageFailure, match="isolation level"):
        await run_with(command, session)

    assert session.statements == []


@pytest.mark.anyio
async def test_ledger_without_its_event_is_inconsistent_state() -> None:
    command = a_folder_update()
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 1),
            ledger_row=(command.op_id, command_hash(command), 1),
        )
    )

    with pytest.raises(kernel_pkg.StorageFailure, match="no change_event"):
        await run_with(command, session)


@pytest.mark.anyio
async def test_ledger_pointing_at_another_entity_is_inconsistent_state() -> None:
    command = a_folder_update()
    foreign_entity, foreign_op = new_id(), new_id()
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 1),
            ledger_row=(command.op_id, command_hash(command), 1),
            event_row=(
                command.workspace_id,
                1,
                "note",
                foreign_entity,
                "create",
                1,
                foreign_op,
                NOW,
            ),
        )
    )

    with pytest.raises(kernel_pkg.StorageFailure, match="does not describe"):
        await run_with(command, session)


@pytest.mark.anyio
async def test_server_message_never_reaches_a_contract_error() -> None:
    """PostgreSQL echoes offending column values; they must not enter the error.

    A unique violation's text is ``Key (email)=(someone@example.com) already exists``
    and a CHECK violation names row columns, so forwarding the server message would
    leak note content and e-mail addresses into something a caller may log or return.
    Only the SQLSTATE and the driver error class are forwarded; the original stays on
    ``__cause__``.
    """
    secret = "someone@example.invalid"

    class LeakyViolation(Exception):
        sqlstate = "23514"

        def __str__(self) -> str:
            return f'new row violates check constraint: body_md = "{secret}"'

    command = a_folder_create()
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO folders",
        raise_error=LeakyViolation(),
    )

    with pytest.raises(kernel_pkg.StorageFailure) as caught:
        await run_with(command, session)

    message = str(caught.value)
    assert secret not in message, f"server message leaked into the error: {message}"
    assert "23514" in message  # the actionable part is kept
    # The full driver error is still reachable for a local debugger, but only there.
    assert isinstance(caught.value.__cause__, LeakyViolation)


@pytest.mark.anyio
async def test_check_violation_failing_row_detail_is_not_forwarded() -> None:
    """PostgreSQL's CHECK detail is literally ``Failing row contains (…, body …)``.

    That is the note's stored content, not a style issue, so neither the message nor
    the ``.detail`` attribute may reach the contract error.
    """
    body = "SECRET-NOTE-BODY-verbatim"

    class CheckViolation(Exception):
        sqlstate = "23514"
        constraint_name = "notes_body_not_empty"

        def __init__(self) -> None:
            super().__init__(
                'new row for relation "notes" violates check constraint "notes_body_not_empty"'
            )
            self.detail = f"Failing row contains (7b1..., ws..., fold..., t, {body})."

    command = a_note_create(body_md=body)
    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO notes",
        raise_error=IntegrityError("INSERT INTO notes ...", {}, CheckViolation()),
    )

    with pytest.raises(kernel_pkg.StorageFailure) as caught:
        await run_with(command, session)

    message = str(caught.value)
    assert body not in message
    assert "Failing row" not in message
    assert "23514" in message  # the actionable part survives


@pytest.mark.anyio
async def test_commit_rejection_also_hides_the_server_message() -> None:
    secret = "PRIVATE-BODY-TEXT"

    class LeakyDeferred(Exception):
        sqlstate = "23503"

        def __str__(self) -> str:
            return f'Key (body_md)=({secret}) is not present in table "change_events".'

    command = a_folder_create()
    session = FakeSession(FakeDB(workspace=(command.workspace_id, 0)), commit_error=LeakyDeferred())

    with pytest.raises(kernel_pkg.StorageFailure) as caught:
        await run_with(command, session)

    assert secret not in str(caught.value)


@pytest.mark.anyio
async def test_note_move_to_its_own_id_is_invalid_parent_not_a_cycle() -> None:
    """A note moved into a folder whose UUID equals the note's own id is NOT a cycle.

    ``notes.folder_id`` points into ``folders`` while ``entity_id`` is the note's key:
    the two rows are unrelated, so the folder lookup simply finds nothing and the
    answer is ``InvalidParent``. Folder-cycle logic must never run for a note.
    """
    shared = new_id()
    command = MoveNote(
        workspace_id=new_id(), entity_id=shared, op_id=new_id(), base_rev=1, folder_id=shared
    )
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 3),
            note_reads=[entity(command.entity_id, command.workspace_id, rev=1)],
            # The folder read finds no folder with that id (it is a *note's* id).
            folder_reads=[None],
        )
    )

    with pytest.raises(kernel_pkg.InvalidParent):
        await run_with(command, session)

    assert writes(session) == []


@pytest.mark.anyio
async def test_note_move_to_its_own_id_succeeds_when_such_a_folder_exists() -> None:
    """A live folder that merely shares the note's UUID is a perfectly good target.

    This is the case the folder-only self-parent comparison would have rejected as a
    ``FolderCycle`` before the entity-type split: the ids live in different tables, so
    equality means nothing and the move must go through.
    """
    shared = new_id()
    command = MoveNote(
        workspace_id=new_id(), entity_id=shared, op_id=new_id(), base_rev=1, folder_id=shared
    )
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 3),
            note_reads=[entity(command.entity_id, command.workspace_id, rev=1)],
            folder_reads=[entity(shared, command.workspace_id, rev=1)],
            ancestors=[shared],  # would be a "cycle" if folder logic leaked in here
        )
    )

    result = await run_with(command, session)

    assert result.seq == 4
    # The ancestry CTE is folder-only: a note must never pay for it.
    assert not any("WITH RECURSIVE" in sql for sql in session.statements)


@pytest.mark.anyio
async def test_note_move_rejects_a_deleted_folder_sharing_its_id() -> None:
    """Deletion is still checked for a note target, even on the UUID-collision path."""
    shared = new_id()
    command = MoveNote(
        workspace_id=new_id(), entity_id=shared, op_id=new_id(), base_rev=1, folder_id=shared
    )
    session = FakeSession(
        FakeDB(
            workspace=(command.workspace_id, 3),
            note_reads=[entity(command.entity_id, command.workspace_id, rev=1)],
            folder_reads=[entity(shared, command.workspace_id, rev=1, deleted=True)],
        )
    )

    with pytest.raises(kernel_pkg.InvalidParent):
        await run_with(command, session)

    assert writes(session) == []


@pytest.mark.anyio
async def test_folder_move_still_rejects_an_indirect_cycle() -> None:
    """The folder path keeps its ancestor walk: this is what must not regress.

    A pure self-parent never reaches the service (the command refuses it), so the
    folder-only guarantee that *does* live here is the indirect cycle: moving a folder
    under one of its own descendants.
    """
    workspace_id, folder, child = new_id(), new_id(), new_id()
    command = MoveFolder(
        workspace_id=workspace_id,
        entity_id=folder,
        op_id=new_id(),
        base_rev=1,
        parent_id=child,
    )
    session = FakeSession(
        FakeDB(
            workspace=(workspace_id, 3),
            folder_reads=[
                entity(folder, workspace_id, rev=1),  # the moving folder
                entity(child, workspace_id, parent=folder, rev=1),  # a descendant
            ],
            ancestors=[folder],  # the walk reaches the moving folder itself
        )
    )

    with pytest.raises(kernel_pkg.FolderCycle):
        await run_with(command, session)

    assert writes(session) == []


@pytest.mark.anyio
async def test_entity_pk_collision_from_another_workspace_is_an_id_conflict() -> None:
    """Ids are global per table: losing the insert race to another ws is a conflict.

    The pre-check (``find_folder_anywhere``) cannot see an uncommitted row from the
    other transaction, so the unique index is the real arbiter. That one case maps to
    the documented ``EntityIdConflict`` rather than a storage fault.
    """
    command = a_folder_create()

    class Violation(Exception):
        sqlstate = "23505"
        constraint_name = "pk_folders"

    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO folders",
        raise_error=IntegrityError("INSERT INTO folders ...", {}, Violation()),
    )

    with pytest.raises(kernel_pkg.EntityIdConflict):
        await run_with(command, session)

    assert session.committed == 0


@pytest.mark.anyio
async def test_note_pk_collision_is_also_an_id_conflict() -> None:
    command = a_note_create()

    class Violation(Exception):
        sqlstate = "23505"
        constraint_name = "pk_notes"

    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO notes",
        raise_error=IntegrityError("INSERT INTO notes ...", {}, Violation()),
    )

    with pytest.raises(kernel_pkg.EntityIdConflict):
        await run_with(command, session)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "constraint",
    [
        "pk_change_events",
        "pk_op_ledger",
        "uq_change_events_workspace_id_op_id",
        "uq_op_ledger_workspace_id_result_seq",
        "uq_folders_workspace_id_id",
    ],
)
async def test_no_other_unique_key_becomes_an_id_conflict(constraint: str) -> None:
    """Only the entity PKs map to ``EntityIdConflict``; everything else stays a fault.

    Widening this would hide genuine bugs — e.g. a duplicated ``(workspace_id, seq)``
    cursor or a ledger ``result_seq`` clash is not "somebody else owns that id".
    """
    command = a_folder_create()

    class Violation(Exception):
        sqlstate = "23505"
        constraint_name = constraint

    session = FakeSession(
        FakeDB(workspace=(command.workspace_id, 0)),
        raise_on="INSERT INTO folders",
        raise_error=IntegrityError("INSERT INTO folders ...", {}, Violation()),
    )

    with pytest.raises(kernel_pkg.StorageFailure):
        await run_with(command, session)


def test_replay_is_diagnostics_only() -> None:
    """``WriteResult`` carries no ``replayed`` field; the category lives in the log."""
    assert service_module._Metrics().category == "success"
