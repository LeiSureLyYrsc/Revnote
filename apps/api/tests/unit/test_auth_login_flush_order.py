"""Regression: login must persist ``auth_sessions`` before its token rows.

The models deliberately declare **no** ``relationship()`` (a Phase 2 rule), so
SQLAlchemy's unit of work has no dependency edge between ``auth_sessions`` and
``auth_access_tokens``/``auth_refresh_tokens`` and is free to emit the token
INSERTs first. PostgreSQL then rejects them with SQLSTATE ``23503`` on
``fk_auth_access_tokens_session_id_auth_sessions``, which surfaced as
``auth_temporarily_unavailable`` (503) on **every successful login** — the first
real-PG auth run caught it, and no unit test had, because the unit tier never
executes the DDL.

This test pins the ordering at the seam the database would enforce, using a fake
session that records the flush/commit chronology. It needs no server, so the
guard runs in the ordinary unit gate, and it fails if the explicit
``await session.flush()`` between ``session.add(auth_session)`` and the token rows
is ever removed.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

import pytest

from revnote_api.security.passwords import hash_password
from revnote_api.services.auth import AuthService

PASSWORD = "correct horse battery staple"
#: One real digest (a single Argon2 call) so ``check_needs_rehash`` sees current
#: parameters; the verifier itself is stubbed in these tests.
DIGEST = hash_password(PASSWORD)


class _Settings:
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
    def __init__(self, *values: Any) -> None:
        self._values = values

    def __getitem__(self, index: int) -> Any:
        return self._values[index]


class _Result:
    def __init__(self, row: _Row | None) -> None:
        self._row = row

    def first(self) -> _Row | None:
        return self._row


class _OrderedSession:
    """Records the interleaving of ``add``/``flush``/``commit`` by object type."""

    def __init__(self, reads: list[_Row | None], log: list[tuple[str, str]]) -> None:
        self._reads = reads
        self._log = log
        self._pending: list[tuple[str, Any]] = []

    async def __aenter__(self) -> _OrderedSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def execute(self, statement: object, *args: object, **kwargs: object) -> Any:
        if self._reads:
            return _Result(self._reads.pop(0))
        return _Result(None)

    async def scalar(self, statement: object, *args: object, **kwargs: object) -> Any:
        row = self._reads.pop(0) if self._reads else None
        return None if row is None else row[0]

    def add(self, entity: Any) -> None:
        self._pending.append((type(entity).__name__, entity))

    def add_all(self, entities: list[Any]) -> None:
        for entity in entities:
            self.add(entity)

    async def flush(self) -> None:
        for name, _ in self._pending:
            self._log.append(("flush", name))
        self._pending.clear()

    async def commit(self) -> None:
        # Anything still pending is flushed implicitly *by commit*, in insertion
        # order — which is exactly the ordering hazard this test guards.
        for name, _ in self._pending:
            self._log.append(("implicit", name))
        self._pending.clear()
        self._log.append(("commit", "-"))


def _factory(reads: list[_Row | None], log: list[tuple[str, str]]):
    def make() -> _OrderedSession:
        return _OrderedSession(reads, log)

    return make


@pytest.mark.anyio
async def test_login_flushes_the_session_row_before_its_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The FK target must hit the database before the rows that reference it."""
    log: list[tuple[str, str]] = []
    reads: list[_Row | None] = [
        _Row(uuid.uuid4(), DIGEST, None),  # phase 1 snapshot
        _Row(DIGEST, None),  # phase 4 locked re-read
    ]
    service = AuthService(_factory(reads, log), _Settings())

    async def ok_verify(digest: str, candidate: str) -> bool:
        return True

    monkeypatch.setattr(AuthService, "_verify", staticmethod(ok_verify))

    try:
        await service.login("user@revnote.test", PASSWORD)
    finally:
        service.close()

    flushes = [name for event, name in log if event == "flush"]
    assert flushes == ["AuthSession"], (
        f"auth_sessions must be flushed on its own before the token rows; got {log}"
    )

    # And no token row may be written implicitly by COMMIT *ahead* of the session.
    assert log.index(("flush", "AuthSession")) < log.index(("commit", "-"))
    implicit = [name for event, name in log if event == "implicit"]
    assert implicit == ["AuthAccessToken", "AuthRefreshToken"], implicit


@pytest.mark.anyio
async def test_login_persists_exactly_one_session_two_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sanity on the row set: one session, one access token, one refresh token."""
    log: list[tuple[str, str]] = []
    reads: list[_Row | None] = [
        _Row(uuid.uuid4(), DIGEST, None),
        _Row(DIGEST, None),
    ]
    service = AuthService(_factory(reads, log), _Settings())

    monkeypatch.setattr(AuthService, "_verify", staticmethod(lambda d, c: _async_value(True)))

    try:
        result = await service.login("user@revnote.test", PASSWORD)
    finally:
        service.close()

    assert isinstance(result.session_id, uuid.UUID)
    written = [name for event, name in log if event in {"flush", "implicit"}]
    assert written == ["AuthSession", "AuthAccessToken", "AuthRefreshToken"]
    # The issued pair's deadlines never outlive the session (D2).
    assert isinstance(result.session_expires_at, datetime.datetime)
    assert result.session_expires_at.tzinfo is not None
    # The raw tokens are present exactly once, on the result, never in the log.
    assert result.access_token and result.refresh_token
    assert "access_token" not in str(written)


@pytest.mark.anyio
async def test_refresh_does_not_need_a_session_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``refresh`` inserts tokens for an **already persisted** session, so no flush.

    Pinned deliberately: if someone "symmetrically" adds a flush there it is
    harmless, but if the login fix is later generalised into a shared helper this
    test documents which path genuinely needs it and why the FK is satisfied.
    """
    from revnote_api.services.auth import token_digest

    log: list[tuple[str, str]] = []
    session_id = uuid.uuid4()
    token_id = uuid.uuid4()
    expires = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=1)
    stored_refresh = "refresh-token-for-this-test"

    reads: list[_Row | None] = [
        _Row(token_id, session_id),  # locate the token
        _Row(expires, None, uuid.uuid4()),  # locked session: expiry, revoked, user
        _Row(None, expires),  # locked token: used_at, expiry
        _Row(None),  # disabled_at (NULL)
    ]
    service = AuthService(_factory(reads, log), _Settings())

    try:
        result = await service.refresh(stored_refresh)
    finally:
        service.close()

    assert result.session_id == session_id
    # No explicit session flush on this path; the tokens are flushed by commit.
    assert [name for event, name in log if event == "flush"] == []
    implicit = [name for event, name in log if event == "implicit"]
    assert implicit == ["AuthAccessToken", "AuthRefreshToken"]
    # The digest we looked up really is the sha256 of the presented token.
    assert len(token_digest(stored_refresh)) == 32


async def _async_value(value: Any) -> Any:
    return value
