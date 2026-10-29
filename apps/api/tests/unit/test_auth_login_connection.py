"""Login must not hold a pooled database connection while Argon2 runs.

``AuthService.login`` verifies the password with Argon2, which is CPU-bound and can
queue behind other logins in the hash limiter. If that work happened inside an open
session, every in-flight login would pin a pool connection for ~60 ms and more —
a login burst could exhaust the pool and take down the whole API.

These tests use a **fake session factory** (no PostgreSQL, no network) whose session
counts itself as "active" from ``__aenter__`` to ``__aexit__``. The verifier is
replaced with a callable that blocks on an ``asyncio.Event``, so the assertion is
made at the exact moment the real login would be holding the connection.

Also covered: the re-validation that closes the TOCTOU window — the account being
disabled or its password changing *while* hashing must not yield a session.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from revnote_api.security.passwords import hash_password
from revnote_api.services.auth import _DUMMY_DIGEST, AuthService, AuthSettings
from revnote_api.services.auth_rules import AuthError

#: Argon2 at the locked cost is too slow to repeat, but one real digest is needed
#: so ``hash_needs_rehash`` sees current parameters instead of an unparseable fake
#: (the *verifier* is still faked in every test below).
REAL_PASSWORD = "correct horse battery staple"
REAL_DIGEST = hash_password(REAL_PASSWORD)


class _Settings:
    """Minimal structural ``AuthSettings`` double (no pydantic, no config import)."""

    auth_registration_open = False
    auth_access_ttl_s = 600
    auth_session_ttl_s = 2_592_000
    auth_password_min_chars = 8
    auth_password_max_chars = 256
    auth_hash_max_concurrency = 2
    argon2_memory_kib = 65_536
    argon2_time_cost = 3
    argon2_parallelism = 4


class _Row:
    """A row-like tuple for the fake ``.first()`` results."""

    def __init__(self, *values: Any) -> None:
        self._values = values

    def __getitem__(self, index: int) -> Any:
        return self._values[index]


class _Result:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def first(self) -> _Row | None:
        return self._row


class _FakeSession:
    """Counts itself as active for exactly the lifetime of the ``async with``."""

    def __init__(self, tracker: _Tracker, reads: list[_Row | None]) -> None:
        self._tracker = tracker
        self._reads = reads
        self.added: list[Any] = []
        self.committed = 0

    async def __aenter__(self) -> _FakeSession:
        self._tracker.active += 1
        self._tracker.peak = max(self._tracker.peak, self._tracker.active)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._tracker.active -= 1

    async def execute(self, statement: object, *args: object, **kwargs: object) -> Any:
        # ``update(...)`` returns a result too; the fake never inspects SQL.
        if self._reads:
            return _Result(self._reads.pop(0))
        return _Result(None)

    async def scalar(self, statement: object, *args: object, **kwargs: object) -> Any:
        row = self._reads.pop(0) if self._reads else None
        return None if row is None else row[0]

    def add(self, entity: object) -> None:
        self.added.append(entity)

    def add_all(self, entities: list[Any]) -> None:
        self.added.extend(entities)

    async def flush(self) -> None:
        """No-op: the fake has no DDL, so ordering is asserted in the flush test."""

    async def commit(self) -> None:
        self.committed += 1

    @property
    def active_connections(self) -> int:
        return self._tracker.active


class _Tracker:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.sessions: list[_FakeSession] = []


def _factory(tracker: _Tracker, reads: list[_Row | None]):
    """Build a factory whose sessions consume ``reads`` **in sequence, shared**.

    The list is shared across sessions on purpose: phase 1 and phase 4 are two
    separate sessions that must each see the *next* row, not restart the script.
    """

    def make() -> _FakeSession:
        session = _FakeSession(tracker, reads)
        tracker.sessions.append(session)
        return session

    return make


# --------------------------------------------------------------------------- #
# no connection while Argon2 runs
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_no_connection_is_held_while_the_password_verifier_blocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The blocker: a login waiting inside Argon2 must hold **zero** connections."""
    tracker = _Tracker()
    digest = REAL_DIGEST
    reads: list[_Row | None] = [
        _Row(uuid.uuid4(), digest, None),  # phase 1 snapshot: id, hash, disabled
        _Row(digest, None),  # phase 4 locked re-read: hash, disabled
    ]

    service = AuthService(_factory(tracker, reads), _Settings())

    started = asyncio.Event()
    release = asyncio.Event()
    observed: dict[str, int] = {}

    async def blocking_verify(stored: str, candidate: str) -> bool:
        started.set()
        await release.wait()  # stands in for a slow / queued Argon2 call
        observed["active_while_hashing"] = tracker.active
        return True

    monkeypatch.setattr(AuthService, "_verify", staticmethod(blocking_verify))
    monkeypatch.setattr(AuthService, "_hash", staticmethod(lambda _pw: _async_value("unused")))

    task = asyncio.create_task(service.login("user@revnote.test", REAL_PASSWORD))
    await started.wait()

    # The verifier is mid-flight: nothing may be checked out.
    assert tracker.active == 0, "a pooled connection was held across Argon2"
    assert service is not None

    release.set()
    try:
        result = await asyncio.wait_for(task, timeout=5)
    except AuthError:  # pragma: no cover - a rejection still proves the timing
        result = None
    finally:
        service.close()

    assert observed["active_while_hashing"] == 0
    if result is not None:
        assert result.access_token and result.refresh_token


@pytest.mark.anyio
async def test_the_two_reads_are_separate_short_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read, hash, re-lock: three phases, never one long-lived session."""
    tracker = _Tracker()
    digest = REAL_DIGEST
    reads: list[_Row | None] = [
        _Row(uuid.uuid4(), digest, None),
        _Row(digest, None),
    ]
    service = AuthService(_factory(tracker, reads), _Settings())

    async def ok_verify(stored: str, candidate: str) -> bool:
        assert tracker.active == 0, "verifier ran with a session open"
        return True

    monkeypatch.setattr(AuthService, "_verify", staticmethod(ok_verify))

    try:
        await service.login("user@revnote.test", REAL_PASSWORD)
    except AuthError:
        pass
    finally:
        service.close()

    # Phase 1 read and phase 4 write are distinct sessions; phase 2/3 have none.
    assert len(tracker.sessions) == 2, [type(s).__name__ for s in tracker.sessions]
    # The first is a read-only snapshot (never commits); the second issues and commits.
    assert tracker.sessions[0].committed == 0, "the snapshot session must not commit"
    assert tracker.sessions[1].committed == 1, "the issuing transaction must commit"


# --------------------------------------------------------------------------- #
# re-validation closes the TOCTOU window
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_account_disabled_while_hashing_does_not_get_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disable that lands during Argon2 must win over the credentials we checked."""
    tracker = _Tracker()
    digest = REAL_DIGEST
    reads: list[_Row | None] = [
        _Row(uuid.uuid4(), digest, None),  # still enabled when hashing began
        _Row(digest, _now()),  # disabled by the time we lock
    ]
    service = AuthService(_factory(tracker, reads), _Settings())

    async def ok_verify(stored: str, candidate: str) -> bool:
        return True

    monkeypatch.setattr(AuthService, "_verify", staticmethod(ok_verify))

    try:
        with pytest.raises(AuthError) as error:
            await service.login("user@revnote.test", REAL_PASSWORD)
    finally:
        service.close()

    assert error.value.code == "invalid_credentials"
    # Nothing was issued: no session row reached the fake.
    assert all(session.committed == 0 for session in tracker.sessions)


@pytest.mark.anyio
async def test_password_changed_while_hashing_does_not_get_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A different digest after the lock means the verified credentials are stale."""
    tracker = _Tracker()
    old_digest = REAL_DIGEST
    reads: list[_Row | None] = [
        _Row(uuid.uuid4(), old_digest, None),
        _Row(hash_password("a-new-password"), None),  # changed mid-flight
    ]
    service = AuthService(_factory(tracker, reads), _Settings())

    async def ok_verify(stored: str, candidate: str) -> bool:
        return True

    monkeypatch.setattr(AuthService, "_verify", staticmethod(ok_verify))

    try:
        with pytest.raises(AuthError) as error:
            await service.login("user@revnote.test", REAL_PASSWORD)
    finally:
        service.close()

    assert error.value.code == "invalid_credentials"
    assert all(session.committed == 0 for session in tracker.sessions)


@pytest.mark.anyio
async def test_account_deleted_while_hashing_does_not_get_a_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _Tracker()
    digest = REAL_DIGEST
    reads: list[_Row | None] = [
        _Row(uuid.uuid4(), digest, None),
        None,  # the locked re-read finds nothing
    ]
    service = AuthService(_factory(tracker, reads), _Settings())

    monkeypatch.setattr(AuthService, "_verify", staticmethod(lambda s, c: _async_value(True)))

    try:
        with pytest.raises(AuthError) as error:
            await service.login("user@revnote.test", REAL_PASSWORD)
    finally:
        service.close()

    assert error.value.code == "invalid_credentials"


# --------------------------------------------------------------------------- #
# the uniform-work rule survives the restructure
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_an_unknown_address_still_does_one_dummy_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No early return before hashing: the timing oracle must stay closed."""
    tracker = _Tracker()
    reads: list[_Row | None] = [None]  # no such user
    service = AuthService(_factory(tracker, reads), _Settings())

    attempted: list[str] = []

    async def recording_verify(stored: str, candidate: str) -> bool:
        attempted.append(stored)
        return False

    monkeypatch.setattr(AuthService, "_verify", staticmethod(recording_verify))

    try:
        with pytest.raises(AuthError) as error:
            await service.login("nobody@revnote.test", REAL_PASSWORD)
    finally:
        service.close()

    assert error.value.code == "invalid_credentials"
    assert attempted == [_DUMMY_DIGEST], "the dummy digest was not used exactly once"


@pytest.mark.anyio
async def test_a_legacy_row_without_a_digest_still_does_one_dummy_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _Tracker()
    reads: list[_Row | None] = [_Row(uuid.uuid4(), None, None)]
    service = AuthService(_factory(tracker, reads), _Settings())

    attempted: list[str] = []

    async def recording_verify(stored: str, candidate: str) -> bool:
        attempted.append(stored)
        return False

    monkeypatch.setattr(AuthService, "_verify", staticmethod(recording_verify))

    try:
        with pytest.raises(AuthError):
            await service.login("legacy@revnote.test", REAL_PASSWORD)
    finally:
        service.close()

    assert attempted == [_DUMMY_DIGEST]


@pytest.mark.anyio
async def test_a_wrong_password_does_no_rehash_and_issues_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tracker = _Tracker()
    reads: list[_Row | None] = [_Row(uuid.uuid4(), REAL_DIGEST, None)]
    service = AuthService(_factory(tracker, reads), _Settings())

    monkeypatch.setattr(AuthService, "_verify", staticmethod(lambda s, c: _async_value(False)))

    try:
        with pytest.raises(AuthError):
            await service.login("user@revnote.test", REAL_PASSWORD)
    finally:
        service.close()

    # Only the phase-1 read happened; no second transaction was opened.
    assert len(tracker.sessions) == 1
    assert tracker.sessions[0].committed == 0


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def _async_value(value: Any) -> Any:
    return value


# Keep the protocol import meaningful for type checkers: the fake above must model
# it. If ``AuthSettings`` gains a required field, this assertion starts failing to
# import rather than silently drifting.
_ = (AuthSettings, hash_password, asynccontextmanager)
