"""Bridge sessions: password-verified, opaque, and independent of native tokens.

Login calls the real :class:`~revnote_api.services.auth.AuthService` — the same
Argon2 path, the same constant-time rejection, the same error vocabulary — and
then **revokes the pair it issued**. The bridge keeps only its own credential, a
random 32-byte token whose SHA-256 digest is what the database stores.

The workspace is resolved once, at login, and stored on the session row. A
Joplin account is a single sync space; recomputing "the user's workspace" on
every request could silently switch targets if the membership list changes.

Every failure is ``unauthenticated``: a wrong token, an expired row and a
revoked row are indistinguishable, exactly like the native surface.
"""

from __future__ import annotations

import datetime
import hashlib
import secrets
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from revnote_api.adapters.joplin.errors import JoplinProtocolError
from revnote_api.adapters.joplin.repository import default_workspace
from revnote_api.kernel.ids import new_id
from revnote_api.services.auth import AuthError, AuthService
from revnote_api.services.auth_rules import normalize_email

__all__ = [
    "SESSION_TOKEN_BYTES",
    "BridgeContext",
    "create_session",
    "hash_token",
    "resolve_session",
    "revoke_session",
]

#: 32 raw bytes -> 43 urlsafe characters. Long enough that guessing is hopeless,
#: short enough to travel in a header and a path.
SESSION_TOKEN_BYTES = 32

_UNUSABLE = "unauthenticated"


@dataclass(frozen=True, slots=True)
class BridgeContext:
    """The authenticated caller of one bridge request: ids only, never a token."""

    user_id: uuid.UUID
    workspace_id: uuid.UUID
    session_id: uuid.UUID


def hash_token(token: str) -> bytes:
    """The stored form of a bridge token."""
    return hashlib.sha256(token.encode("utf-8")).digest()


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


async def create_session(
    factory: async_sessionmaker[AsyncSession],
    auth_service: AuthService,
    *,
    email: str,
    password: str,
    ttl_s: int,
    client_name: str | None = None,
) -> tuple[str, uuid.UUID]:
    """Verify the password and mint one bridge token.

    Returns ``(token, user_id)``. The token is the only copy that ever exists;
    the caller must put it in the response and forget it.

    The native pair is revoked in a ``finally`` block: if revocation itself
    fails the login fails, because leaving a live ten-minute credential behind
    on every Joplin login is not an acceptable failure mode.
    """
    try:
        issued = await auth_service.login(normalize_email(email), password, device_name=client_name)
    except AuthError as error:
        raise JoplinProtocolError("invalid_credentials") from error

    try:
        # ``LoginResult`` carries tokens, not a user id; resolving the freshly
        # issued access token is the one supported way to read it, and it happens
        # before the revoke below so the token is still live.
        context = await auth_service.authenticate_access(issued.access_token)
        async with factory() as session:
            workspace_id = await default_workspace(session, context.user_id)
    finally:
        await auth_service.logout(issued.access_token)

    if workspace_id is None:  # pragma: no cover - registration always creates one
        raise JoplinProtocolError("internal")

    token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
    now = _utcnow()
    try:
        async with factory() as session:
            await session.execute(
                sa.text(
                    "INSERT INTO joplin_sessions "
                    "(id, user_id, workspace_id, token_hash, client_name, created_at, expires_at) "
                    "VALUES (:id, :user_id, :workspace_id, :token_hash, :client_name, "
                    "        :created_at, :expires_at)"
                ),
                {
                    "id": new_id(),
                    "user_id": context.user_id,
                    "workspace_id": workspace_id,
                    "token_hash": hash_token(token),
                    "client_name": client_name,
                    "created_at": now,
                    "expires_at": now + datetime.timedelta(seconds=ttl_s),
                },
            )
            await session.commit()
    except sa.exc.SQLAlchemyError as exc:  # pragma: no cover - storage failure
        raise JoplinProtocolError("storage_unavailable") from exc

    return token, context.user_id


async def resolve_session(
    factory: async_sessionmaker[AsyncSession], token: str | None
) -> BridgeContext:
    """Resolve ``X-API-AUTH`` to a live context, or raise ``unauthenticated``."""
    if not token or len(token) > 256:
        raise JoplinProtocolError(_UNUSABLE)

    try:
        async with factory() as session:
            row = (
                await session.execute(
                    sa.text(
                        "SELECT s.id, s.user_id, s.workspace_id "
                        "  FROM joplin_sessions AS s "
                        "  JOIN workspace_members AS m "
                        "    ON m.workspace_id = s.workspace_id AND m.user_id = s.user_id "
                        " WHERE s.token_hash = :token_hash "
                        "   AND s.revoked_at IS NULL "
                        "   AND s.expires_at > now()"
                    ),
                    {"token_hash": hash_token(token)},
                )
            ).first()
    except sa.exc.SQLAlchemyError as exc:  # pragma: no cover - storage failure
        raise JoplinProtocolError("storage_unavailable") from exc

    if row is None:
        raise JoplinProtocolError(_UNUSABLE)
    return BridgeContext(user_id=row[1], workspace_id=row[2], session_id=row[0])


async def revoke_session(factory: async_sessionmaker[AsyncSession], token: str | None) -> None:
    """Revoke one bridge token; idempotent, and silent about unknown tokens."""
    if not token:
        return
    try:
        async with factory() as session:
            await session.execute(
                sa.text(
                    "UPDATE joplin_sessions SET revoked_at = now() "
                    " WHERE token_hash = :token_hash AND revoked_at IS NULL"
                ),
                {"token_hash": hash_token(token)},
            )
            await session.commit()
    except sa.exc.SQLAlchemyError as exc:  # pragma: no cover - storage failure
        raise JoplinProtocolError("storage_unavailable") from exc
