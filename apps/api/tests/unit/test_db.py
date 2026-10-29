"""Database plumbing tests — lazy engine creation and session transaction rules.

These run without a server: SQLAlchemy engines/sessions are inert objects until
a connection is actually requested.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from revnote_api.config import Settings
from revnote_api.db import engine as engine_module
from revnote_api.db import session as session_module
from revnote_api.db.base import Base
from revnote_api.db.engine import create_engine_for_url, dispose_engines, get_engine
from revnote_api.db.session import get_sessionmaker, reset_session_state, session_scope
from revnote_api.models import MODEL_TABLES


@pytest.fixture(autouse=True)
def _clean_db_state():
    """Keep the module-level engine/sessionmaker caches isolated per test.

    Sync on purpose: async autouse fixtures need an async test to drive them,
    and several tests here are plain sync.
    """

    def _reset() -> None:
        asyncio.run(dispose_engines())
        reset_session_state()

    _reset()
    yield
    _reset()


def test_base_metadata_declares_the_six_kernel_tables() -> None:
    """Phase 2: the six domain tables are declared, and nothing else.

    Schema is still owned by Alembic (``create_all`` is never called), so this
    asserts *declared metadata*, not a live database.
    """
    assert tuple(sorted(Base.metadata.tables)) == tuple(sorted(MODEL_TABLES))
    # Pinned literally too: MODEL_TABLES must not silently drift.
    assert tuple(sorted(MODEL_TABLES)) == (
        "change_events",
        "folders",
        "notes",
        "op_ledger",
        "users",
        "workspaces",
    )


def test_naming_convention_is_configured() -> None:
    convention = Base.metadata.naming_convention

    assert convention["pk"] == "pk_%(table_name)s"
    assert convention["fk"] == "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"
    # Regression: `column_0_list_name` is not a SQLAlchemy token (it raised
    # KeyError for every composite index/unique constraint); `column_0_N_name` is.
    assert convention["ix"] == "ix_%(table_name)s_%(column_0_N_name)s"
    assert convention["uq"] == "uq_%(table_name)s_%(column_0_N_name)s"
    assert convention["ck"] == "ck_%(table_name)s_%(constraint_name)s"


def test_engine_is_created_lazily(settings: Settings) -> None:
    """get_engine() builds an AsyncEngine without connecting to the server."""
    engine = get_engine(settings)

    assert isinstance(engine, AsyncEngine)
    assert engine.url.drivername == "postgresql+asyncpg"
    assert engine.dialect.driver == "asyncpg"
    assert engine.pool.checkedout() == 0  # nothing checked out => no connection
    assert get_engine(settings) is engine  # cached


def test_engine_is_rebuilt_when_dsn_changes(settings: Settings) -> None:
    first = get_engine(settings)
    other = settings.model_copy(
        update={"database_url": "postgresql+asyncpg://u:p@127.0.0.1:1/other"}
    )

    assert get_engine(other) is not first


@pytest.mark.anyio
async def test_dsn_switch_keeps_old_engine_tracked_and_disposed(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: switching the DSN must not orphan the previous engine.

    The old engine used to be referenced only by a global that got overwritten,
    so :func:`dispose_engines` could never close its pool. It now stays in the
    registry until shutdown, where *every* tracked engine is disposed.
    """
    disposed: list[int] = []
    original = AsyncEngine.dispose

    async def spy(self: AsyncEngine) -> None:
        disposed.append(id(self))
        await original(self)

    monkeypatch.setattr(AsyncEngine, "dispose", spy)

    first = get_engine(settings)
    other = settings.model_copy(
        update={"database_url": "postgresql+asyncpg://u:p@127.0.0.1:1/other"}
    )
    second = get_engine(other)
    assert second is not first

    # Both engines are still reachable from the registry.
    assert set(engine_module._tracked.values()) == {first, second}

    await dispose_engines()

    assert sorted(disposed) == sorted([id(first), id(second)])
    assert engine_module._tracked == {}
    assert engine_module._engine is None


@pytest.mark.anyio
async def test_dispose_engines_drains_registry(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    disposed: list[int] = []
    original = AsyncEngine.dispose

    async def spy(self: AsyncEngine) -> None:
        disposed.append(id(self))
        await original(self)

    monkeypatch.setattr(AsyncEngine, "dispose", spy)

    engine = get_engine(settings)
    assert engine_module._tracked != {}

    await dispose_engines()

    assert disposed == [id(engine)]
    assert engine_module._tracked == {}
    # A later call must still be a no-op rather than an error.
    await dispose_engines()
    assert disposed == [id(engine)]


@pytest.mark.anyio
async def test_dispose_engines_survives_one_failing_engine(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad engine must not prevent the others from being disposed."""
    attempts: list[int] = []

    async def failing(self: AsyncEngine) -> None:
        attempts.append(id(self))
        raise RuntimeError("pool already gone")

    monkeypatch.setattr(AsyncEngine, "dispose", failing)

    first = get_engine(settings)
    other = settings.model_copy(
        update={"database_url": "postgresql+asyncpg://u:p@127.0.0.1:1/other"}
    )
    second = get_engine(other)

    await dispose_engines()  # must not raise

    assert sorted(attempts) == sorted([id(first), id(second)])
    assert engine_module._tracked == {}


@pytest.mark.anyio
async def test_revisiting_dsn_keeps_every_created_engine(settings: Settings) -> None:
    first = get_engine(settings)
    other = settings.model_copy(
        update={"database_url": "postgresql+asyncpg://u:p@127.0.0.1:1/other"}
    )
    second = get_engine(other)
    third = get_engine(settings)
    assert set(engine_module._tracked.values()) == {first, second, third}
    await dispose_engines()
    assert engine_module._tracked == {}


def test_create_engine_for_url_uses_asyncpg() -> None:
    engine = create_engine_for_url("postgresql+asyncpg://u:p@127.0.0.1:1/x")

    assert engine.dialect.name == "postgresql"
    assert engine.dialect.driver == "asyncpg"


def test_engines_hide_bound_parameters(settings: Settings) -> None:
    """SQL echo/logging must never render a note body or a password hash.

    ``hide_parameters`` is set on every engine this module builds, including the
    one used with ``DB_ECHO=true``: statement logging is a debugging aid, not a
    licence to print user content (plan §11.1).
    """
    configured = get_engine(settings)
    throwaway = create_engine_for_url("postgresql+asyncpg://u:p@127.0.0.1:1/x")

    # ``hide_parameters`` lives on the sync engine that ``AsyncEngine`` wraps.
    assert configured.sync_engine.hide_parameters is True
    assert throwaway.sync_engine.hide_parameters is True


@pytest.mark.anyio
async def test_dispose_engines_is_safe_when_never_created() -> None:
    await dispose_engines()  # must not raise
    await dispose_engines()


@pytest.mark.anyio
async def test_sessionmaker_is_cached_and_configured(settings: Settings) -> None:
    factory = get_sessionmaker(settings)

    assert get_sessionmaker(settings) is factory
    assert factory.class_ is AsyncSession
    assert factory.kw["expire_on_commit"] is False  # no implicit commit semantics
    assert factory.kw["autoflush"] is False

    session = factory()
    try:
        assert session.sync_session.expire_on_commit is False
    finally:
        await session.close()


@pytest.mark.anyio
async def test_session_scope_rolls_back_on_error_and_never_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exceptions roll the transaction back; commit is never reached."""
    calls: list[str] = []

    async def fake_commit(self: AsyncSession) -> None:  # pragma: no cover - must not run
        calls.append("commit")

    async def fake_rollback(self: AsyncSession) -> None:
        calls.append("rollback")

    monkeypatch.setattr(AsyncSession, "commit", fake_commit)
    monkeypatch.setattr(AsyncSession, "rollback", fake_rollback)

    with pytest.raises(RuntimeError, match="boom"):
        async with session_scope() as session:
            assert isinstance(session, AsyncSession)
            raise RuntimeError("boom")

    assert calls == ["rollback"]


@pytest.mark.anyio
async def test_session_scope_commits_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean exit is the only path that commits."""
    calls: list[str] = []

    async def fake_commit(self: AsyncSession) -> None:
        calls.append("commit")

    async def fake_rollback(self: AsyncSession) -> None:  # pragma: no cover - happy path
        calls.append("rollback")

    monkeypatch.setattr(AsyncSession, "commit", fake_commit)
    monkeypatch.setattr(AsyncSession, "rollback", fake_rollback)

    async with session_scope():
        pass

    assert calls == ["commit"]


@pytest.mark.anyio
async def test_get_session_dependency_never_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    """The FastAPI dependency never commits on the caller's behalf."""
    calls: list[str] = []

    async def fake_commit(self: AsyncSession) -> None:  # pragma: no cover - must not run
        calls.append("commit")

    monkeypatch.setattr(AsyncSession, "commit", fake_commit)

    agen = session_module.get_session()
    session = await agen.__anext__()
    try:
        assert isinstance(session, AsyncSession)
    finally:
        await agen.aclose()

    assert calls == []


@pytest.mark.anyio
async def test_get_session_dependency_rolls_back_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An error raised by the endpoint rolls the transaction back."""
    calls: list[str] = []

    async def fake_rollback(self: AsyncSession) -> None:
        calls.append("rollback")

    monkeypatch.setattr(AsyncSession, "rollback", fake_rollback)

    agen = session_module.get_session()
    await agen.__anext__()

    with pytest.raises(RuntimeError, match="boom"):
        await agen.athrow(RuntimeError("boom"))

    assert calls == ["rollback"]


def test_module_level_caches_exist() -> None:
    """Regression guard: caches live on the modules (single process-wide engine)."""
    assert hasattr(engine_module, "_engine")
    assert hasattr(session_module, "_sessionmaker")
