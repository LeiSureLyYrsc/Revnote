"""Authentication service: registration, login, rotating sessions, membership.

Framework-free by contract. ``test_kernel_boundaries`` asserts that every module
under ``services/`` imports no FastAPI, no Pydantic, no ``revnote_api.api`` and no
``revnote_api.config`` — so settings arrive as a **structural protocol** (only the
fields this module reads are declared) and failures leave as
:class:`~revnote_api.services.auth_rules.AuthError`. The HTTP layer translates.

State machine (``docs/plan-phase3.md`` §10, decisions D1-D4):

``register``
    Refused with ``registration_closed`` unless enabled. When enabled it creates
    user + default workspace + owner membership **atomically** and returns them
    without a token: registering is not logging in.

``login``
    Creates one ``auth_sessions`` row (device login *and* refresh family, D4) with
    an **absolute** ``expires_at`` (30 days) plus one access and one refresh token.
    Only ``sha256(raw)`` is stored. Every failure mode — unknown address,
    ``password_hash IS NULL`` (a pre-Phase-3 row), disabled account, wrong password
    — answers the same ``invalid_credentials`` and performs the same Argon2 work, so
    timing does not distinguish "no such account" from "wrong password".

``refresh`` (R1, D3)
    Locks the **session first**, then the token, and re-reads ``used_at``. An unused
    token becomes used and a fresh pair is issued in the same transaction. A token
    that was already used means the credential was replayed: the whole session
    (family) is revoked, the revocation is **committed**, and only then does the
    caller see ``token_reused``. There is no grace window; the winning client's
    access token stops working immediately because every request re-checks the
    session.

``logout``
    Takes the same session lock, revokes, and returns nothing. Idempotent: logging
    out twice is not an error.

``authenticate_access``
    Resolves an opaque access token to its session, honouring token expiry, session
    expiry, revocation and account disabling. Used by the request dependency.

Guarantees this module never breaks:

* a raw token or password is never stored, logged or put in an exception;
* ``expires_at`` of a token never exceeds its session's ``expires_at`` (D2);
* nothing here commits on a caller's behalf except where the plan requires the
  revocation to be durable before the error is raised (R1, logout).
"""

from __future__ import annotations

import contextlib
import datetime
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from revnote_api.kernel.ids import new_id
from revnote_api.models import (
    AuthAccessToken,
    AuthRefreshToken,
    AuthSession,
    User,
    Workspace,
)
from revnote_api.models.workspace_member import WorkspaceMember
from revnote_api.security.hashing import HashLimiter
from revnote_api.security.passwords import (
    digest_or_none,
    hash_needs_rehash,
    hash_password,
    new_token,
    token_digest,
    verify_password,
)
from revnote_api.services.auth_rules import (
    AuthError,
    normalize_email,
    validate_email,
    validate_password,
)

__all__ = [
    "AuthService",
    "AuthSettings",
    "LoginResult",
    "MeResult",
    "RegistrationResult",
    "SessionContext",
    "assert_workspace_member",
    "load_auth_service",
]

#: The session is the refresh family (D4) and its lifetime is absolute (D2).
DEFAULT_ACCESS_TTL_S = 600
DEFAULT_SESSION_TTL_S = 2_592_000  # 30 days

#: A digest guaranteed not to match any password, used to equalise the work done
#: for a *nonexistent* account. Verifying against it costs the same as a real
#: verify, which is the whole point; the value itself is meaningless.
_DUMMY_DIGEST = (
    "$argon2id$v=19$m=65536,t=3,p=4$"
    "c2VudGluZWwtc2FsdC0xNg$"
    "c2VudGluZWwtZGlnZXN0LXNlbnRpbmVsLTMyYnl0ZXM"
)

#: Default workspace name created at registration. Not user content, so a constant
#: is right: it must not vary with locale or input.
DEFAULT_WORKSPACE_NAME = "My Workspace"


class AuthSettings(Protocol):
    """The settings fields this service reads.

    Declared structurally so ``services/`` never imports ``revnote_api.config``
    (a boundary the tests enforce). ``Settings`` satisfies this protocol, and so
    does any test double that provides the same attributes.
    """

    auth_registration_open: bool
    auth_access_ttl_s: int
    auth_session_ttl_s: int
    auth_password_min_chars: int
    auth_password_max_chars: int
    auth_hash_max_concurrency: int
    #: Argon2 costs as configured. The service does **not** apply them (they are
    #: locked, D7); it compares them to the real hasher and refuses to start on a
    #: mismatch, so an operator's override is never silently ignored.
    argon2_memory_kib: int
    argon2_time_cost: int
    argon2_parallelism: int


@dataclass(frozen=True, slots=True)
class SessionContext:
    """What a validated access token proves. Ids only — never the token itself."""

    user_id: uuid.UUID
    session_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class LoginResult:
    """A freshly issued token pair plus what the client needs to store it.

    ``access_token`` / ``refresh_token`` are the **only** place raw token text
    exists in this process. The repr below is fixed so a stray log line cannot
    leak them.
    """

    access_token: str
    refresh_token: str
    expires_in: int
    session_id: uuid.UUID
    session_expires_at: datetime.datetime
    token_type: str = "Bearer"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"LoginResult(session_id={self.session_id!r}, "
            f"expires_in={self.expires_in!r}, token_type={self.token_type!r})"
        )


@dataclass(frozen=True, slots=True)
class _PublicUser:
    """Detached snapshot of a user row, safe to hand to the API layer."""

    id: uuid.UUID
    email: str
    created_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    user: _PublicUser
    workspace_id: uuid.UUID
    workspace_created_at: datetime.datetime


@dataclass(frozen=True, slots=True)
class MeResult:
    user: _PublicUser
    workspaces: tuple[tuple[uuid.UUID, str], ...]


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _verify_argon2_settings_match_hasher(settings: AuthSettings) -> None:
    """Refuse to run if the configured Argon2 costs differ from the real hasher.

    ``Settings`` exposes ``ARGON2_MEMORY_COST`` / ``ARGON2_TIME_COST`` /
    ``ARGON2_PARALLELISM`` (documented in ``.env.example`` and the plan §7), but the
    hasher's costs are **locked** module constants (D7: 64 MiB / t=3 / p=4) — that is
    deliberate, because a per-deployment cost would make "this password verifies"
    depend on which node answered.

    The dangerous outcome would be silently ignoring an operator who lowered the
    values and then believing the deployment is cheaper (or weaker) than it is.
    Since this module may not import ``revnote_api.config`` (service boundary), the
    check compares the values it is actually handed: if a caller passes a settings
    object whose numbers disagree with the hasher, startup fails with a clear
    ``ValueError`` instead of pretending.

    ``getattr`` with defaults keeps the protocol small: a settings double that does
    not model these fields is treated as "not configured", not as a mismatch.
    """
    from revnote_api.security.passwords import PASSWORD_HASHER

    parameters = PASSWORD_HASHER._parameters
    configured = (
        ("argon2_memory_kib", parameters.memory_cost),
        ("argon2_time_cost", parameters.time_cost),
        ("argon2_parallelism", parameters.parallelism),
    )
    for attribute, actual in configured:
        requested = getattr(settings, attribute, actual)
        if requested != actual:
            raise ValueError(
                f"{attribute.upper()}={requested} contradicts the locked Argon2 "
                f"parameters (actual {actual}). The D7 costs are fixed; remove the "
                "override rather than assuming it takes effect."
            )


class AuthService:
    """Registration, login, token rotation and membership checks.

    One instance per application (built by the API dependency and cached on
    ``app.state``) so the :class:`HashLimiter` — and therefore the concurrency cap
    on Argon2 — is scoped to that app rather than to the process.
    """

    __slots__ = ("_factory", "_limiter", "_settings")

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        settings: AuthSettings,
    ) -> None:
        _verify_argon2_settings_match_hasher(settings)
        self._factory = session_factory
        self._settings = settings
        self._limiter = HashLimiter(capacity=settings.auth_hash_max_concurrency)

    # -- password work, always off the event loop -------------------------- #

    async def _hash(self, password: str) -> str:
        return await self._limiter.run(hash_password, password)

    async def _verify(self, digest: str, password: str) -> bool:
        return await self._limiter.run(verify_password, digest, password)

    def _validate_password(self, password: str) -> None:
        """Apply the policy from settings, bounded by the locked constants."""
        validate_password(
            password,
            min_chars=self._settings.auth_password_min_chars,
            max_chars=self._settings.auth_password_max_chars,
        )

    def close(self) -> None:
        """Release the hash limiter's worker threads.

        Called by the app fixture on teardown. Safe to call more than once — a
        later :meth:`_hash` after ``close`` simply rebuilds the primitives, so a
        long-lived process never ends up with a dead limiter after a shutdown was
        requested by something else.
        """
        self._limiter.shutdown()

    # -- registration ------------------------------------------------------ #

    async def register(self, email: str, password: str) -> RegistrationResult:
        """Create user + default workspace + owner membership atomically (D6).

        No token is issued: registration never logs in.

        A duplicate address is reported as ``invalid_request`` on the ``email``
        field. Note what this does **not** hide: while registration is open, a
        caller can always discover whether an address is already registered by
        attempting to register it — that is inherent to open signup, not a flaw in
        the response. The default deployment ships with registration **closed**
        (``AUTH_REGISTRATION_OPEN=false``, D6), which is what keeps account
        identity private; the classification below simply avoids a 500 and keeps
        the response shape consistent with other validation failures.

        The address is stored **normalised** (``lower(btrim(email))``, plan §9.1
        "new registrations: ASCII e-mail, normalised"). Legacy rows keep whatever
        they held — the migration never rewrites them — so this asymmetry is
        deliberate: new data is clean, old data is untouched.
        """
        if not self._settings.auth_registration_open:
            raise AuthError("registration_closed")

        normalized = validate_email(email)
        self._validate_password(password)
        digest = await self._hash(password)

        now = _utcnow()
        # UUIDv7, like every other new row in this code base (kernel.ids).
        user_id = new_id()
        workspace_id = new_id()

        try:
            async with self._factory() as session:
                # Uniqueness is the functional index; this pre-check only produces a
                # clean error instead of an IntegrityError for the common case. The
                # index remains the authority under concurrency.
                existing = await session.scalar(
                    select(User.id).where(_normalized_email_expression() == normalized)
                )
                if existing is not None:
                    raise AuthError("invalid_request", {"field": "email", "reason": "taken"})

                user = User(
                    id=user_id,
                    email=normalized,
                    password_hash=digest,
                    disabled_at=None,
                    created_at=now,
                )
                workspace = Workspace(
                    id=workspace_id,
                    owner_id=user_id,
                    last_seq=0,
                    min_retained_seq=1,
                    created_at=now,
                )
                membership = WorkspaceMember(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    role="owner",
                    created_at=now,
                )
                session.add(user)
                await session.flush()
                session.add(workspace)
                await session.flush()
                session.add(membership)
                await session.commit()
        except AuthError:
            raise
        except IntegrityError as exc:
            # A concurrent registration for the same normalised address slipped past
            # the pre-check above. The unique index is the real authority, and a
            # *duplicate account* is a client error (400), not a server failure:
            # answering 503 would invite a pointless retry that can never succeed.
            # Only the e-mail index is translated — any other integrity failure is
            # genuinely unexpected and stays a storage error.
            if _is_duplicate_email(exc):
                raise AuthError("invalid_request", {"field": "email", "reason": "taken"}) from None
            raise _storage_error() from exc
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
            raise _storage_error() from exc

        return RegistrationResult(
            user=_PublicUser(id=user_id, email=normalized, created_at=now),
            workspace_id=workspace_id,
            workspace_created_at=now,
        )

    # -- login ------------------------------------------------------------- #

    async def login(
        self,
        email: str,
        password: str,
        *,
        device_name: str | None = None,
    ) -> LoginResult:
        """Authenticate and open a new session with a fresh token pair (D2/D4).

        **No database connection is held while Argon2 runs.** The verification (and
        any rehash) is CPU-bound and can wait in the hash limiter's queue behind
        other logins; keeping a pooled connection checked out for that window would
        let a login flood exhaust the pool and take the whole API down with it. The
        sequence is therefore:

        1. a **short read** that copies the three values needed (id, digest,
           disabled flag) and closes the session immediately;
        2. the Argon2 verification with **no session open**;
        3. a rehash digest, if the stored one is outdated, also with no session open;
        4. a **new short transaction** that locks the user row and re-validates the
           account before issuing anything.

        Step 4 is what closes the obvious TOCTOU: the password could be changed or
        the account disabled while Argon2 was running. The locked row must still show
        the *same* digest (otherwise the credentials we verified are stale) and no
        ``disabled_at``. A rehash is written in that same locked transaction, so a
        stale hash can never overwrite a newer password.

        Every rejection is ``invalid_credentials``, and exactly one Argon2
        verification happens in every case — including an unknown address or a
        legacy row with no digest, which verify against :data:`_DUMMY_DIGEST` — so
        response time does not disclose which case occurred.
        """
        normalized = normalize_email(email)

        # -- phase 1: short read, then close the session ---------------------- #
        try:
            async with self._factory() as session:
                snapshot = (
                    await session.execute(
                        select(User.id, User.password_hash, User.disabled_at).where(
                            _normalized_email_expression() == normalized
                        )
                    )
                ).first()
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
            raise _storage_error() from exc

        user_id = snapshot[0] if snapshot is not None else None
        stored_digest = snapshot[1] if snapshot is not None else None
        disabled_at = snapshot[2] if snapshot is not None else None
        digest = stored_digest or None

        # -- phase 2: Argon2, with no connection held ------------------------- #
        # One verification, always: the dummy digest is a real Argon2id hash, so
        # "unknown account" costs the same as "wrong password".
        verified = await self._verify(digest or _DUMMY_DIGEST, password)

        if snapshot is None or digest is None or not verified or disabled_at is not None:
            raise AuthError("invalid_credentials")

        # -- phase 3: optional rehash digest, still with no connection -------- #
        replacement: str | None = None
        if hash_needs_rehash(digest):
            # Computed here, applied only after the locked re-validation below.
            with contextlib.suppress(Exception):  # pragma: no cover - already hashed once
                replacement = await self._hash(password)

        # -- phase 4: lock, re-validate, then issue --------------------------- #
        try:
            async with self._factory() as session:
                locked = (
                    await session.execute(
                        select(User.password_hash, User.disabled_at)
                        .where(User.id == user_id)
                        .with_for_update()
                    )
                ).first()
                if locked is None:  # account deleted while we hashed
                    raise AuthError("invalid_credentials")
                current_digest, current_disabled_at = locked[0], locked[1]
                # The credentials we verified must still be the stored ones. A
                # different digest means the password changed mid-flight; writing a
                # rehash now would clobber the new password with the old one.
                if current_disabled_at is not None or (current_digest or None) != digest:
                    raise AuthError("invalid_credentials")

                if replacement is not None:
                    await session.execute(
                        update(User).where(User.id == user_id).values(password_hash=replacement)
                    )

                now = _utcnow()
                session_expires_at = now + datetime.timedelta(
                    seconds=self._settings.auth_session_ttl_s
                )
                auth_session = AuthSession(
                    id=new_id(),
                    user_id=user_id,
                    device_name=device_name,
                    created_at=now,
                    expires_at=session_expires_at,
                    revoked_at=None,
                    revoke_reason=None,
                )
                session.add(auth_session)
                # Explicit flush: the models declare no ``relationship()`` (a
                # deliberate Phase 2 rule), so SQLAlchemy's unit of work has no
                # dependency edge between ``auth_sessions`` and the two token tables
                # and may emit the token INSERTs first. PostgreSQL then rejects them
                # with ``23503`` on ``fk_auth_access_tokens_session_id_auth_sessions``,
                # which surfaced as ``auth_temporarily_unavailable`` (503) on every
                # successful login. Flushing the session row first makes the order
                # explicit instead of relying on mapper sorting.
                await session.flush()
                pair = self._issue_pair(auth_session.id, now, session_expires_at)
                session.add_all(
                    [
                        pair.access_row,
                        pair.refresh_row,
                    ]
                )
                await session.commit()
        except AuthError:
            raise
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
            raise _storage_error() from exc

        return LoginResult(
            access_token=pair.access_token,
            refresh_token=pair.refresh_token,
            expires_in=self._settings.auth_access_ttl_s,
            session_id=auth_session.id,
            session_expires_at=session_expires_at,
        )

    # -- refresh ----------------------------------------------------------- #

    async def refresh(self, refresh_token: str) -> LoginResult:
        """Rotate a refresh token, or detect reuse (R1, D3).

        Lock order is **session, then token**, always — the same order as
        :meth:`logout` — so two concurrent refreshes cannot deadlock and the reuse
        check happens under the lock that a concurrent revoke also takes.

        The locking reads are **column selects**, not ORM entity selects, on
        purpose: an entity select would return the instance already in the identity
        map (``expire_on_commit=False``) and ``token_row.used_at`` could be the value
        read *before* the lock, letting two concurrent refreshes both see "unused"
        and both rotate. Reading the columns re-executes against the locked row, so
        the second caller sees ``used_at`` set and triggers R1.
        """
        digest = digest_or_none(refresh_token)
        if digest is None:
            # Not representable as UTF-8, so it cannot be a token we issued.
            raise AuthError("invalid_refresh")
        revoked_for_reuse = False
        issued: LoginResult | None = None

        try:
            async with self._factory() as session:
                located = (
                    await session.execute(
                        select(AuthRefreshToken.id, AuthRefreshToken.session_id).where(
                            AuthRefreshToken.token_hash == digest
                        )
                    )
                ).first()
                if located is None:
                    raise AuthError("invalid_refresh")

                token_id, session_id = located[0], located[1]

                # 1. lock the session (family) first. Column reads: no identity map.
                session_row = (
                    await session.execute(
                        select(AuthSession.expires_at, AuthSession.revoked_at, AuthSession.user_id)
                        .where(AuthSession.id == session_id)
                        .with_for_update()
                    )
                ).first()
                if session_row is None:  # pragma: no cover - FK makes this impossible
                    raise AuthError("invalid_refresh")

                # 2. then lock the token and read its *current* used_at/expiry.
                token_row = (
                    await session.execute(
                        select(AuthRefreshToken.used_at, AuthRefreshToken.expires_at)
                        .where(AuthRefreshToken.id == token_id)
                        .with_for_update()
                    )
                ).first()
                if token_row is None:  # pragma: no cover - it existed a moment ago
                    raise AuthError("invalid_refresh")

                # 3. the account must still be usable (D8). Without this a disabled
                #    account could keep minting sessions forever while every access
                #    token was rejected — the refresh path would silently outlive the
                #    ban. Read under the session lock, so a concurrent disable is
                #    either fully before or fully after this decision.
                disabled_at = await session.scalar(
                    select(User.disabled_at).where(User.id == session_row[2])
                )
                if disabled_at is not None:
                    raise AuthError("invalid_refresh")

                # ``now`` is taken **after** both locks: the waits above can take
                # seconds under contention, and a stale ``now`` could rotate a token
                # that expired while we waited, or stamp ``used_at`` in the past and
                # hand out a pair whose deadline had already passed.
                now = _utcnow()

                used_at, token_expires_at = token_row[0], token_row[1]
                session_expires_at, session_revoked_at = session_row[0], session_row[1]

                if session_revoked_at is not None or session_expires_at <= now:
                    raise AuthError("invalid_refresh")
                if token_expires_at <= now:
                    raise AuthError("invalid_refresh")

                if used_at is not None:
                    # Reuse: revoke the whole family and COMMIT before answering.
                    # The winner's access token is dead as soon as this commits,
                    # because authentication re-checks the session row.
                    #
                    # ``revoked_at IS NULL`` in the WHERE is belt-and-braces: the
                    # check above already refused a revoked session, so this can only
                    # match an active one. Keeping the predicate means a future
                    # reordering cannot silently overwrite an earlier
                    # ``revoke_reason`` (e.g. ``logout``) with ``refresh_reuse``.
                    await session.execute(
                        update(AuthSession)
                        .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
                        .values(revoked_at=now, revoke_reason="refresh_reuse")
                    )
                    await session.commit()
                    revoked_for_reuse = True
                else:
                    await session.execute(
                        update(AuthRefreshToken)
                        .where(AuthRefreshToken.id == token_id)
                        .values(used_at=now)
                    )
                    pair = self._issue_pair(
                        session_id,
                        now,
                        session_expires_at,
                        parent_id=token_id,
                    )
                    session.add_all([pair.access_row, pair.refresh_row])
                    await session.commit()

                    issued = LoginResult(
                        access_token=pair.access_token,
                        refresh_token=pair.refresh_token,
                        expires_in=self._settings.auth_access_ttl_s,
                        session_id=session_id,
                        session_expires_at=session_expires_at,
                    )
        except AuthError:
            raise
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
            raise _storage_error() from exc

        if issued is not None:
            return issued
        if revoked_for_reuse:
            # Outside the transaction on purpose: the revocation is already durable,
            # and the caller must not be told "reused" about a failed rollback.
            raise AuthError("token_reused")

        raise AuthError("invalid_refresh")  # pragma: no cover - defensive

    # -- logout ------------------------------------------------------------ #

    async def logout(self, access_token: str) -> None:
        """Revoke the session behind ``access_token`` (idempotent, D3 lock order).

        An unknown, expired or already-revoked token is not an error: the caller
        asked for the session to be gone, and it is. Column reads and a targeted
        ``UPDATE`` keep this independent of the ORM identity map (see
        :meth:`refresh` for why that matters).
        """
        digest = digest_or_none(access_token)
        if digest is None:
            # Unknown by construction; logout is idempotent, so this is "already gone".
            return
        now = _utcnow()

        try:
            async with self._factory() as session:
                access_row = (
                    await session.execute(
                        select(AuthAccessToken.id, AuthAccessToken.session_id).where(
                            AuthAccessToken.token_hash == digest
                        )
                    )
                ).first()
                if access_row is None:
                    return

                session_id = access_row[1]

                # Same ordering as refresh: session lock first, then read state.
                session_row = (
                    await session.execute(
                        select(AuthSession.revoked_at)
                        .where(AuthSession.id == session_id)
                        .with_for_update()
                    )
                ).first()
                if session_row is None:  # pragma: no cover - FK guarantees it
                    return
                if session_row[0] is None:
                    await session.execute(
                        update(AuthSession)
                        .where(AuthSession.id == session_id)
                        .values(revoked_at=now, revoke_reason="logout")
                    )
                    await session.commit()
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
            raise _storage_error() from exc

    # -- request authentication --------------------------------------------- #

    async def authenticate_access(self, access_token: str) -> SessionContext:
        """Resolve an opaque access token to its session, or raise.

        Checks, in order: the token exists, the token has not expired, the session
        has not expired, the session was not revoked, the user is not disabled. Any
        failure is ``unauthenticated`` — the client only learns that it must sign in
        again (D20), never which check failed.
        """
        digest = digest_or_none(access_token)
        if digest is None:
            raise AuthError("unauthenticated")
        now = _utcnow()

        try:
            async with self._factory() as session:
                row = (
                    await session.execute(
                        select(
                            AuthAccessToken.session_id,
                            AuthAccessToken.expires_at,
                            AuthSession.expires_at,
                            AuthSession.revoked_at,
                            AuthSession.user_id,
                            User.disabled_at,
                        )
                        .join(AuthSession, AuthSession.id == AuthAccessToken.session_id)
                        .join(User, User.id == AuthSession.user_id)
                        .where(AuthAccessToken.token_hash == digest)
                    )
                ).first()
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
            raise _storage_error() from exc

        if row is None:
            raise AuthError("unauthenticated")

        token_expires_at, session_expires_at, revoked_at, user_id, disabled_at = (
            row[1],
            row[2],
            row[3],
            row[4],
            row[5],
        )
        if (
            token_expires_at <= now
            or session_expires_at <= now
            or revoked_at is not None
            or disabled_at is not None
        ):
            raise AuthError("unauthenticated")

        return SessionContext(user_id=user_id, session_id=row[0])

    # -- /me --------------------------------------------------------------- #

    async def me(self, user_id: uuid.UUID) -> MeResult:
        """Return the public user plus the workspaces it is a member of."""
        try:
            async with self._factory() as session:
                # ``select`` rather than ``session.get``: row-shaped reads keep this
                # module's style uniform with the rest of ``services/`` and never
                # leave an ORM instance in an identity map after the session closes.
                user_row = (
                    await session.execute(
                        select(User.id, User.email, User.created_at).where(User.id == user_id)
                    )
                ).first()
                if user_row is None:  # pragma: no cover - the token implies it exists
                    raise AuthError("not_found")
                rows = (
                    await session.execute(
                        select(WorkspaceMember.workspace_id, WorkspaceMember.role)
                        .where(WorkspaceMember.user_id == user_id)
                        .order_by(WorkspaceMember.created_at, WorkspaceMember.workspace_id)
                    )
                ).all()
        except AuthError:
            raise
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
            raise _storage_error() from exc

        return MeResult(
            user=_PublicUser(id=user_row[0], email=user_row[1], created_at=user_row[2]),
            workspaces=tuple((row[0], row[1]) for row in rows),
        )

    # -- internals ---------------------------------------------------------- #

    def _issue_pair(
        self,
        session_id: uuid.UUID,
        now: datetime.datetime,
        session_expires_at: datetime.datetime,
        *,
        parent_id: uuid.UUID | None = None,
    ) -> _IssuedPair:
        """Build one access + one refresh row, capped by the session deadline (D2).

        The deadline is computed as ``min(now + ttl, session_expires_at)`` so a
        token can never outlive its session even if the TTL exceeds the remaining
        session life — the invariant the schema cannot express.
        """
        access_raw = new_token()
        refresh_raw = new_token()

        access_expires_at = min(
            now + datetime.timedelta(seconds=self._settings.auth_access_ttl_s),
            session_expires_at,
        )
        refresh_expires_at = min(
            now + datetime.timedelta(seconds=self._settings.auth_session_ttl_s),
            session_expires_at,
        )

        return _IssuedPair(
            access_token=access_raw,
            refresh_token=refresh_raw,
            access_row=AuthAccessToken(
                id=new_id(),
                session_id=session_id,
                token_hash=token_digest(access_raw),
                created_at=now,
                expires_at=access_expires_at,
            ),
            refresh_row=AuthRefreshToken(
                id=new_id(),
                session_id=session_id,
                token_hash=token_digest(refresh_raw),
                parent_id=parent_id,
                created_at=now,
                expires_at=refresh_expires_at,
                used_at=None,
            ),
        )


@dataclass(frozen=True, slots=True)
class _IssuedPair:
    """One access + one refresh row and the raw text that must be returned once.

    ``repr=False`` on both token fields is load-bearing: this object exists only
    between :meth:`AuthService._issue_pair` and the response, and a dataclass's
    generated ``__repr__`` would otherwise print two live bearer credentials into
    any traceback that happens to include it. The rows are excluded too — they hold
    ``token_hash``.
    """

    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    access_row: AuthAccessToken = field(repr=False)
    refresh_row: AuthRefreshToken = field(repr=False)


def _normalized_email_expression() -> Any:
    """The SQL form of the uniqueness key: ``lower(btrim(email))``.

    Imported lazily inside the function so this module's import graph stays free of
    SQLAlchemy expression construction at import time; the value must match
    revision 0003's functional index exactly.
    """
    from sqlalchemy import func

    return func.lower(func.btrim(User.email))


def _is_duplicate_email(exc: IntegrityError) -> bool:
    """True only for the ``users`` normalised-e-mail unique index.

    Checked by **constraint name** where the driver exposes it, and by SQLSTATE
    ``23505`` plus the index name in the message otherwise. Being narrow matters:
    treating every ``IntegrityError`` as "address taken" would report a foreign-key
    or check failure as a client error and hide a real bug.
    """
    original = getattr(exc, "orig", None)
    constraint = getattr(original, "constraint_name", None)
    if constraint is not None:
        return str(constraint) == "uq_users_email_normalized"
    return "uq_users_email_normalized" in str(exc)


def _storage_error() -> AuthError:
    """Translate any database failure into a safe, non-leaking rejection.

    The driver's message is never included: PostgreSQL text can carry row values
    (an e-mail address, a digest prefix) and this exception reaches logs. The
    original exception is attached as ``__cause__`` for a debugger, and the code is
    retryable so a client may re-send.
    """
    return AuthError("auth_temporarily_unavailable")


async def _guarded(operation: Any) -> Any:
    """Run ``operation()``, mapping *any* database-layer failure to a safe code.

    ``SQLAlchemyError`` covers statement and pool errors, but a refused connection
    surfaces as the driver's own ``OSError``/``ConnectionError`` subclass (asyncpg
    raises before SQLAlchemy can wrap it) — and an unwrapped one would become a 500
    ``internal``, telling the client "you are broken" when the truth is "try again".
    """
    try:
        return await operation()
    except AuthError:
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _storage_error() from exc


async def assert_workspace_member(
    factory: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
) -> None:
    """Confirm ``user_id`` is a member of ``workspace_id``, else raise ``not_found``.

    This is the **per-operation** recheck used by push (D5): membership is verified
    again before every op, not once per request, so a membership removed mid-batch
    takes effect at the next op.

    A missing workspace and a non-membership are deliberately indistinguishable
    (``not_found`` for both, D20). The session is short-lived and closed *before*
    this function returns, so the caller can open the kernel transaction without
    ever holding two connections.
    """
    try:
        async with factory() as session:
            member = await session.scalar(
                select(WorkspaceMember.workspace_id).where(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.user_id == user_id,
                )
            )
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        raise _storage_error() from exc

    if member is None:
        raise AuthError("not_found")


def load_auth_service(
    factory: async_sessionmaker[AsyncSession],
    settings: AuthSettings,
) -> AuthService:
    """Build the app-scoped service. Kept a function so the API layer can cache it."""
    return AuthService(factory, settings)
