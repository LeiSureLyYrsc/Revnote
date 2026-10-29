"""``/api/v1/auth/*`` and ``/api/v1/me`` — thin HTTP shells over ``AuthService``.

Every endpoint here does the same three things and nothing else: parse the body
(schema does the validation), call one service method, and turn its result into a
response model. All authentication logic — hashing, locking, rotation, reuse
detection — lives in :mod:`revnote_api.services.auth`; all error shapes come from
:func:`revnote_api.api.dependencies._translate` via ``ApiError``.

Contract notes worth stating once:

* **Token responses are uncacheable.** ``Cache-Control: no-store`` is attached to
  login and refresh: an intermediary must never hold a bearer token.
* **Registration does not log in** (D6). It returns 201 with the new user and
  workspace only; the client must call login.
* **Logout is 204 and idempotent** — logging out twice, or with a token that no
  longer resolves, is still success from the client's point of view.
* **Bad credentials are uniform.** Unknown address, legacy account without a
  password, disabled account and wrong password all produce the same 401 body.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from revnote_api.api.dependencies import (
    AuthContext,
    get_auth_context,
    get_auth_service,
)
from revnote_api.api.errors import ApiError
from revnote_api.api.schemas.auth import (
    LoginRequest,
    MeResponse,
    PublicUser,
    PublicWorkspace,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    TokenPair,
    WorkspaceMembership,
)
from revnote_api.services.auth import AuthService, LoginResult
from revnote_api.services.auth_rules import AuthError

__all__ = ["me_router", "router"]

#: ``/auth/*`` endpoints (register, login, refresh, logout).
router = APIRouter(prefix="/auth", tags=["auth"])

#: ``GET /me`` lives at the version root, not under ``/auth`` (spec §2).
me_router = APIRouter(tags=["auth"])

#: Headers on every success that contains a bearer token.
_NO_STORE = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
}

#: Same header on the ``/me`` response: it describes an authenticated principal.
_PRIVATE = {"Cache-Control": "no-store"}


def _as_api_error(error: AuthError) -> ApiError:
    """Translate a service rejection, keeping only scalar, safe details."""
    details = {
        key: value
        for key, value in (error.details or {}).items()
        if isinstance(value, (str, int, float, bool))
    }
    return ApiError(error.code, details=details)


def _token_pair(result: LoginResult) -> TokenPair:
    """Render the service result as the wire model (raw tokens appear only here)."""
    return TokenPair(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        token_type=result.token_type,
        expires_in=result.expires_in,
        session_id=result.session_id,
        session_expires_at=result.session_expires_at,
    )


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterResponse,
    summary="Create an account (does not log in)",
    responses={
        201: {"description": "User and default workspace created; no token issued."},
        403: {"description": "Registration is closed on this deployment."},
    },
)
async def register(
    payload: RegisterRequest,
    request: Request,
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> RegisterResponse:
    """Create user + default workspace + owner membership atomically (D6)."""
    try:
        result = await service.register(payload.email, payload.password)
    except AuthError as error:
        raise _as_api_error(error) from None

    response = RegisterResponse(
        user=PublicUser(
            id=result.user.id,
            email=result.user.email,
            created_at=result.user.created_at,
        ),
        workspace=WorkspaceMembership(id=result.workspace_id, role="owner"),
    )
    return response


@router.post(
    "/login",
    response_model=TokenPair,
    summary="Exchange credentials for a token pair",
    responses={
        200: {"description": "Access + rotating refresh token; response is uncacheable."},
        401: {"description": "Credentials rejected (uniform for every failure mode)."},
    },
)
async def login(
    payload: LoginRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
    response: Response,
) -> TokenPair:
    """Open a new session (device login and refresh family, D4)."""
    try:
        result = await service.login(
            payload.email, payload.password, device_name=payload.device_name
        )
    except AuthError as error:
        raise _as_api_error(error) from None

    response.headers.update(_NO_STORE)
    return _token_pair(result)


@router.post(
    "/refresh",
    response_model=TokenPair,
    summary="Rotate the refresh token",
    responses={
        200: {"description": "New pair; the presented refresh token is now spent."},
        401: {"description": "Invalid, expired, or already-used refresh token."},
    },
)
async def refresh(
    payload: RefreshRequest,
    service: Annotated[AuthService, Depends(get_auth_service)],
    response: Response,
) -> TokenPair:
    """Rotate, or revoke the family and report ``token_reused`` (R1, D3)."""
    try:
        result = await service.refresh(payload.refresh_token)
    except AuthError as error:
        raise _as_api_error(error) from None

    response.headers.update(_NO_STORE)
    return _token_pair(result)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current session",
    responses={204: {"description": "Session revoked (idempotent)."}},
)
async def logout(
    request: Request,
    context: Annotated[AuthContext, Depends(get_auth_context)],
    service: Annotated[AuthService, Depends(get_auth_service)],
) -> Response:
    """Revoke the session behind the presented access token.

    The raw token is taken from the header rather than re-derived: revocation is
    keyed on the access token row, and only the header holds it.
    """
    authorization = request.headers.get("authorization", "")
    _, _, raw_token = authorization.partition(" ")
    if raw_token:
        try:
            await service.logout(raw_token.strip())
        except AuthError as error:  # pragma: no cover - logout tolerates everything
            raise _as_api_error(error) from None

    del context  # authentication already happened; the session id is all we needed
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@me_router.get(
    "/me",
    response_model=MeResponse,
    summary="The caller and its workspaces",
    responses={200: {"description": "Authenticated principal and memberships."}},
)
async def me(
    context: Annotated[AuthContext, Depends(get_auth_context)],
    service: Annotated[AuthService, Depends(get_auth_service)],
    response: Response,
) -> MeResponse:
    """Return the caller's public identity and the workspaces it belongs to."""
    try:
        result = await service.me(context.user_id)
    except AuthError as error:
        raise _as_api_error(error) from None

    response.headers.update(_PRIVATE)
    return MeResponse(
        user=PublicUser(
            id=result.user.id,
            email=result.user.email,
            created_at=result.user.created_at,
        ),
        workspaces=[
            PublicWorkspace(id=workspace_id, role=role) for workspace_id, role in result.workspaces
        ],
    )
