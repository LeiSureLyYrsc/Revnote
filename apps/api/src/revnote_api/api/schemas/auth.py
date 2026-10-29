"""Wire models for ``/api/v1/auth/*`` and the shared auth envelope pieces.

Shape follows ``docs/phase3-sync-api-spec.md`` §2 exactly (the root spec is
authoritative; there is no historical upsert/extra field here):

============================  ==========================================
``POST /auth/register``       201 ``user`` + ``workspace``, **no** token
``POST /auth/login``          200 token pair, ``Cache-Control: no-store``
``POST /auth/refresh``        200 token pair, ``Cache-Control: no-store``
``POST /auth/logout``         204, nothing
``GET  /me``                  200 ``user`` + ``workspaces``
============================  ==========================================

Two rules are baked into these models rather than checked later:

* **Secrets are ``repr=False``.** A token field is the one value in this API whose
  leak is unrecoverable, and Pydantic models end up in tracebacks and logs. The
  field is still serialised into the success response (that is its purpose) but it
  is never rendered by ``repr``/``str`` of the model.
* **The request models forbid unknown fields.** A typo in ``password`` (say
  ``pasword``) must be a 400, not a silently ignored field that turns into a
  confusing "invalid credentials".

Timestamps reuse :data:`~revnote_api.api.schemas.common.UTCDateTime` so every
timestamp in the API has one rendering. Nothing here declares ``body_md`` or any
note content.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from revnote_api.api.schemas.common import UTCDateTime

__all__ = [
    "LoginRequest",
    "MeResponse",
    "PublicUser",
    "PublicWorkspace",
    "RefreshRequest",
    "RegisterRequest",
    "RegisterResponse",
    "TokenPair",
    "WorkspaceMembership",
]


class _AuthModel(BaseModel):
    """Strict, closed base: unknown request fields are a contract error."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,  # never mutate a password or e-mail silently
        validate_default=True,
    )


# --------------------------------------------------------------------------- #
# requests
# --------------------------------------------------------------------------- #
class RegisterRequest(_AuthModel):
    """``POST /auth/register`` body.

    Length limits mirror the locked policy so an oversized body is rejected by
    validation (cheap) before any Argon2 work happens. The *service* still enforces
    both character and byte limits — this is the first gate, not the only one.
    """

    email: Annotated[
        str,
        Field(
            min_length=1,
            max_length=254,
            description="ASCII e-mail address; normalised as lower(btrim(email)) for uniqueness.",
            examples=["person@example.com"],
        ),
    ]
    password: Annotated[
        str,
        Field(
            repr=False,
            min_length=8,
            max_length=256,
            description="8-256 characters, UTF-8 <= 1024 bytes, never trimmed or truncated.",
        ),
    ]


class LoginRequest(_AuthModel):
    """``POST /auth/login`` body."""

    email: Annotated[
        str,
        Field(min_length=1, max_length=254, description="Registered e-mail address."),
    ]
    #: ``min_length=1`` only: a login must not reveal the password policy, and an
    #: old account may predate it. Length is not re-validated against 8-256 here
    #: because a wrong password of any length is just ``invalid_credentials``.
    #: ``repr=False`` matters most *here*: a login body is the one request whose
    #: password is a real, valid credential worth stealing from a log line.
    password: Annotated[
        str,
        Field(repr=False, min_length=1, max_length=256, description="Account password."),
    ]
    device_name: Annotated[
        str | None,
        Field(
            default=None,
            max_length=128,
            description="Optional free-text device label stored on the session.",
        ),
    ]


class RefreshRequest(_AuthModel):
    """``POST /auth/refresh`` body: only the refresh token."""

    refresh_token: Annotated[
        str,
        Field(repr=False, min_length=1, max_length=512, description="Rotating refresh token."),
    ]


# --------------------------------------------------------------------------- #
# responses
# --------------------------------------------------------------------------- #
class PublicUser(_AuthModel):
    """What a client may know about an account: id, address, creation time."""

    id: uuid.UUID
    email: Annotated[str, Field(description="The address exactly as registered.")]
    created_at: UTCDateTime


class PublicWorkspace(_AuthModel):
    """A workspace the caller belongs to, with its role in that workspace."""

    id: uuid.UUID
    role: Annotated[
        str,
        Field(description="Membership role; v1 only ever reports 'owner'.", examples=["owner"]),
    ]


class WorkspaceMembership(_AuthModel):
    """Registration's workspace payload: the new workspace plus the owner role."""

    id: uuid.UUID
    role: Annotated[str, Field(description="Always 'owner' for a freshly created workspace.")]


class TokenPair(_AuthModel):
    """The success body of login/refresh.

    ``access_token`` and ``refresh_token`` are ``repr=False``: this model is
    returned to the client once, and must never be rendered into a log line. The
    response also carries ``Cache-Control: no-store`` (set by the route).
    """

    access_token: Annotated[
        str,
        Field(repr=False, description="Opaque access token; send as 'Authorization: Bearer ...'."),
    ]
    refresh_token: Annotated[
        str,
        Field(repr=False, description="Opaque rotating refresh token; store securely."),
    ]
    token_type: Annotated[str, Field(default="Bearer", description="Always 'Bearer'.")]
    expires_in: Annotated[
        int,
        Field(description="Access-token lifetime in seconds (AUTH_ACCESS_TTL_S)."),
    ]
    session_id: Annotated[uuid.UUID, Field(description="Session (refresh family) identifier.")]
    session_expires_at: Annotated[
        UTCDateTime,
        Field(description="Absolute session deadline; refreshing never extends it."),
    ]


class RegisterResponse(_AuthModel):
    """201 body of registration: identity created, **not** logged in (D6)."""

    user: PublicUser
    workspace: WorkspaceMembership


class MeResponse(_AuthModel):
    """``GET /me``: the caller plus every workspace it may act in."""

    user: PublicUser
    workspaces: Annotated[
        list[PublicWorkspace],
        Field(description="Workspaces this user is a member of, stable order."),
    ]


def error_details(value: Any) -> dict[str, Any]:
    """Narrow an arbitrary ``details`` mapping to a plain dict for the envelope.

    Kept tiny and local: the auth service produces ``dict[str, Any] | None`` and
    the envelope requires a dict, never ``None``.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    return {}
