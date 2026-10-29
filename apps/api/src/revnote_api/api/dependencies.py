"""Request-scoped authentication and authorisation dependencies.

This is the **only** place that knows both halves of authentication: it imports
FastAPI (for ``Depends``/``Security``/``HTTPBearer``), ``ApiError`` and the
service layer, and it translates :class:`~revnote_api.services.auth_rules.AuthError`
into protocol errors. The service itself stays framework-free.

Two caches matter and both live on the app, never on this module:

* the :class:`~revnote_api.services.auth.AuthService` is built once per application
  from ``request.app.state.settings``, so its :class:`HashLimiter` — and therefore
  the Argon2 concurrency cap — is per-app. A module-level singleton would be shared
  across every app in the process and bound to the first event loop.
* the ``async_sessionmaker`` comes from
  :func:`revnote_api.db.session.get_sessionmaker`, which is already keyed by DSN.

Security shape:

* ``HTTPBearer(auto_error=False)`` plus an explicit check, so a missing header is
  our fixed 401 envelope rather than FastAPI's default body.
* ``scheme_name="BearerAuth"`` reuses the scheme ``main.create_app`` declares, so
  OpenAPI shows one scheme and every protected route advertises it automatically.
* A missing/invalid token is ``unauthenticated``; a non-member or unknown workspace
  is ``not_found``. The two are never mixed: 401 means "sign in", 404 means "not
  yours", and neither discloses whether a workspace exists (D20).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Path, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from revnote_api.api.errors import ApiError
from revnote_api.db.session import get_sessionmaker
from revnote_api.services.auth import AuthService, SessionContext, assert_workspace_member
from revnote_api.services.auth_rules import AuthError

__all__ = [
    "AuthContext",
    "assert_workspace_member",
    "bearer_scheme",
    "get_auth_context",
    "get_auth_service",
    "require_workspace_member",
]

#: One bearer scheme for the whole app. ``auto_error=False`` is deliberate: the
#: 401 body must be our fixed envelope, and a malformed scheme ("Basic ...") must
#: be indistinguishable from a missing token.
bearer_scheme = HTTPBearer(
    scheme_name="BearerAuth",
    description=(
        "Opaque access token issued by POST /api/v1/auth/login. "
        "Send it as 'Authorization: Bearer <token>'."
    ),
    auto_error=False,
)


@dataclass(frozen=True, slots=True)
class AuthContext:
    """The authenticated caller of one request.

    Frozen and minimal by design: two ids, no token, no ORM object, no settings.
    Handler code can compare it, pass it to a service and put it in a log line
    without any risk of carrying a credential. ``session_id`` is present because
    logout revokes *this* session and push re-checking is per session.
    """

    user_id: uuid.UUID
    session_id: uuid.UUID

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AuthContext(user_id={self.user_id!r}, session_id={self.session_id!r})"


def _translate(error: AuthError) -> ApiError:
    """Map a service-level :class:`AuthError` onto the protocol envelope.

    Only the code and a *sanitised* details dict cross over. The service never puts
    user data in ``details``, but this function is the last gate before a response,
    so it also refuses anything non-serialisable rather than letting it through.
    """
    details = {
        key: value
        for key, value in (error.details or {}).items()
        if isinstance(value, (str, int, float, bool))
    }
    return ApiError(error.code, details=details)


def get_auth_service(request: Request) -> AuthService:
    """Return the app-scoped :class:`AuthService`, building it on first use.

    Cached on ``app.state`` (not in a module global) so two apps in one test
    process get independent hash limiters, and so the service is recreated with the
    app rather than outliving it.
    """
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        settings = request.app.state.settings
        factory = get_sessionmaker(settings)
        service = AuthService(factory, settings)
        request.app.state.auth_service = service
    return service


async def get_auth_context(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AuthContext:
    """Resolve ``Authorization: Bearer`` to an :class:`AuthContext`.

    Any failure — missing header, unknown digest, expired token, expired session,
    revoked session, disabled account — becomes ``unauthenticated``. The client
    learns only "sign in again" (D20); which check failed stays server-side.
    """
    if credentials is None or not credentials.credentials:
        raise ApiError("unauthenticated")

    service = get_auth_service(request)
    try:
        resolved: SessionContext = await service.authenticate_access(credentials.credentials)
    except AuthError as error:
        raise _translate(error) from None

    return AuthContext(user_id=resolved.user_id, session_id=resolved.session_id)


async def require_workspace_member(
    workspace_id: Annotated[uuid.UUID, Path(description="Workspace to act in.")],
    context: Annotated[AuthContext, Depends(get_auth_context)],
    request: Request,
) -> AuthContext:
    """Authorise the path workspace for the authenticated caller.

    A workspace that does not exist and one the caller is not a member of produce
    the **same** ``not_found``: existence is not observable (D20). Returns the same
    :class:`AuthContext` so a handler can depend on either this or
    :func:`get_auth_context` without unpacking anything.

    The membership check uses its own short-lived session (inside
    :func:`assert_workspace_member`), so a handler that then opens the kernel
    transaction never holds two connections.
    """
    try:
        await assert_workspace_member(
            get_sessionmaker(request.app.state.settings),
            context.user_id,
            workspace_id,
        )
    except AuthError as error:
        raise _translate(error) from None

    return context


#: Convenience alias for the type used in route signatures.
AuthContextDep = Annotated[AuthContext, Depends(get_auth_context)]
