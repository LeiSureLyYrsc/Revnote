"""Wire schemas of the attachment endpoints (interface §2).

Five shapes, one theme: nothing that could carry a hostile value onto the wire is
modelled loosely.

* :class:`CreateAttachmentRequest` is the ``POST`` body. ``mime`` is a plain
  string because the *service* owns the allowlist - a Pydantic ``Literal`` would
  duplicate that list in two places and turn a policy change into a code change.
  The length bounds here mirror the column CHECKs, so an over-long value is a
  ``400`` with a field path rather than a ``500`` from PostgreSQL.
* :class:`AttachmentResponse` is the metadata view. ``content_hash`` travels as
  lowercase hex (never ``BYTEA``), ``byte_size`` is a JSON integer (a byte count
  is not a sync counter and never approaches 2**53), and ``file_name`` is the
  user's own value echoed back - the only response that carries it.
* :class:`UploadTarget` is the ``upload`` object. ``url`` is a **relative path**
  (D4): there is no presigning anywhere in this protocol, so a client never holds
  a credential-bearing link to the object store.
* :class:`CapabilitiesResponse` is the discovery endpoint. It stays a plain nested
  model so a generated client and a hand-written ``fetch`` see the same frozen
  shape ``{attachments: {enabled, max_bytes, accepted_mime}}``.

Every model inherits ``extra="forbid"`` from :class:`_WireModel`, so a request
that adds an unknown key is a contract error rather than a silently ignored field.
``file_name`` is the one user-supplied string in any of these models; it is never
used to build a storage key and never reaches a log.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from revnote_api.api.schemas.common import UTCDateTime

__all__ = [
    "MAX_FILE_NAME_CHARS",
    "AttachmentResponse",
    "CapabilitiesResponse",
    "CapabilityAttachment",
    "CapabilityJoplinBridge",
    "CreateAttachmentRequest",
    "CreateAttachmentResponse",
    "UploadContentResponse",
    "UploadTarget",
]

#: An uploaded object's size in bytes. Not a ``DecimalString``: this is a byte
#: count, not a sync counter, and it can never exceed ``ATTACHMENT_MAX_BYTES``.
ByteSize = Annotated[int, Field(ge=1, description="Size of the stored object in bytes.")]

#: Lowercase hexadecimal SHA-256. The service renders it; a client only compares.
ContentHash = Annotated[
    str,
    Field(
        description="Lowercase hexadecimal SHA-256 of the stored bytes.",
        pattern=r"^[0-9a-f]{64}$",
    ),
]

#: The three states an attachment row can report. ``deleted`` is listed because the
#: column allows it, but a response is only ever produced for ``pending`` (create)
#: and ``ready`` (upload/metadata): a ``deleted`` row is a ``404``.
AttachmentState = Literal["pending", "ready", "deleted"]

#: Longest ``file_name`` accepted. Generous for a real name and bounded so the body
#: cannot be used as a storage channel. The column itself has no length CHECK: the
#: application-level rule is the one the client is told about.
MAX_FILE_NAME_CHARS: int = 255

#: Longest declared ``mime`` accepted; mirrors ``ck_attachments_mime_len``.
MAX_MIME_CHARS: int = 127


class _WireModel(BaseModel):
    """Strict, closed model: unknown fields are errors, nothing is coerced."""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=False,
        validate_default=True,
    )


class CreateAttachmentRequest(_WireModel):
    """``POST /workspaces/{workspace_id}/attachments`` body."""

    mime: Annotated[
        str,
        Field(
            min_length=3,
            max_length=MAX_MIME_CHARS,
            description="Declared media type; must be in the deployment's allowlist.",
            examples=["image/png"],
        ),
    ]
    #: Optional declared size. Advisory only: the authoritative size is what the
    #: store actually wrote, and the ceiling is enforced on the running total.
    byte_size: (
        Annotated[
            int,
            Field(
                ge=1,
                description="Declared size in bytes (advisory); must not exceed the cap.",
            ),
        ]
        | None
    ) = None
    #: Optional display name. Stored and echoed back, never logged, never part of a
    #: storage key.
    file_name: (
        Annotated[
            str,
            Field(
                max_length=MAX_FILE_NAME_CHARS,
                description="Original file name for display; never part of a storage key.",
            ),
        ]
        | None
    ) = None


class UploadTarget(_WireModel):
    """Where to send the bytes: a method and a **relative** path (D4)."""

    method: Literal["PUT"] = "PUT"
    url: Annotated[
        str,
        Field(
            description=(
                "Relative upload path "
                "(/api/v1/workspaces/{workspace_id}/attachments/{attachment_id}/content). "
                "No presigned URL: the API proxies the bytes."
            ),
        ),
    ]


class CreateAttachmentResponse(_WireModel):
    """``201`` answer to ``POST .../attachments``."""

    attachment_id: uuid.UUID
    state: Literal["pending"] = "pending"
    upload: UploadTarget
    expires_at: UTCDateTime


class UploadContentResponse(_WireModel):
    """``200`` answer to ``PUT .../content``: the verified object."""

    attachment_id: uuid.UUID
    state: Literal["ready"] = "ready"
    content_hash: ContentHash
    byte_size: ByteSize


class AttachmentResponse(_WireModel):
    """``200`` answer to ``GET /attachments/{id}``.

    ``file_name`` is the only field carrying user-supplied text, and the only one
    that is optional because the client may never have supplied a name.
    """

    id: uuid.UUID
    workspace_id: uuid.UUID
    state: AttachmentState
    mime: str
    byte_size: ByteSize
    content_hash: ContentHash
    created_at: UTCDateTime
    ready_at: UTCDateTime | None = None
    file_name: str | None = None


class CapabilityAttachment(_WireModel):
    """The ``attachments`` object of the capabilities response."""

    enabled: Annotated[
        bool,
        Field(description="Whether this deployment accepts attachment uploads."),
    ]
    max_bytes: Annotated[
        int,
        Field(ge=1, description="Largest accepted attachment, in bytes."),
    ]
    accepted_mime: Annotated[
        list[str],
        Field(description="Accepted media types; image/svg+xml is never included."),
    ]


class CapabilityJoplinBridge(_WireModel):
    """The ``joplin_bridge`` object of the capabilities response (Phase 6, additive).

    Purely informational: a Joplin client never calls this route. It exists so a
    Revnote client (or an operator's smoke test) can tell whether the
    compatibility surface is mounted on this deployment without probing a route
    that may legitimately be absent.
    """

    enabled: Annotated[
        bool,
        Field(description="Whether this deployment mounts the Joplin compatibility routes."),
    ]
    protocol_baseline: Annotated[
        str,
        Field(description="The Joplin Server protocol generation this bridge targets."),
    ]
    mount: Annotated[
        str,
        Field(description="Path prefix the compatibility routes are mounted under."),
    ] = "/api"


class CapabilitiesResponse(_WireModel):
    """``GET /capabilities`` - always ``200``, even when attachments are disabled.

    A client uses this to *hide* the entry point rather than to discover the gap on
    its first upload, and an older server that does not implement the route answers
    ``404``, so a newer client degrades to "no attachments" instead of failing.
    """

    attachments: CapabilityAttachment
    #: Phase 6, additive. An old client ignores an unknown object; a new one can
    #: tell "bridge off" from "bridge missing" without a second request.
    joplin_bridge: CapabilityJoplinBridge
