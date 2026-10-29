"""Shared wire types of the ``/api/v1`` contract.

Three things every endpoint group needs, and nothing else:

* **Counters are decimal strings.** ``seq``, ``rev``, ``cursor`` and
  ``min_retained_seq`` travel as *strings* (``"42"``), never JSON numbers. The
  kernel keeps them as ``int`` (``D2``); the rendering boundary is here so a
  JavaScript client cannot silently lose precision past 2**53, and so a client
  sending ``2`` instead of ``"2"`` is a contract error rather than a guess.
  ``"0"`` is legal (the ``since`` floor), ``"007"``, ``"-1"``, ``"1e3"``,
  ``" 1"`` and ``"1"``-with-any-other-noise are not (plan §7.1).
* **Timestamps are RFC 3339 in UTC**, rendered with a ``Z`` suffix. The kernel
  hands out timezone-aware ``datetime`` objects; the wire form is fixed so two
  clients cannot disagree about what "now" looked like.
* **The error envelope has one shape.** Every rejected request - validation,
  auth, kernel, unclassified - is
  ``{"error": {code, message, request_id, retryable, details}}`` (plan §11.1).
  ``details`` is always an object (never null) so a client can index into it
  unconditionally; ``request_id`` is always present and always the value echoed
  in ``X-Request-ID``.

Secrets never reach these models: no password, token or note body is declared
here, and no model in this module carries a ``body_md`` field.
"""

from __future__ import annotations

import datetime
import re
from typing import Annotated, Any, Final

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, PlainSerializer

__all__ = [
    "BIGINT_MAX",
    "CURSOR_ZERO",
    "DecimalString",
    "ErrorBody",
    "ErrorEnvelope",
    "RequestId",
    "Retryable",
    "UTCDateTime",
    "render_decimal",
    "render_utc",
]

#: PostgreSQL ``BIGINT`` upper bound; counters are never allowed past it.
BIGINT_MAX: Final[int] = 2**63 - 1

#: The only legal "beginning of time" cursor (``since`` defaults to this).
CURSOR_ZERO: Final[str] = "0"

#: Canonical non-negative decimal: ``0`` or a digit string with no leading zero.
_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(r"^(?:0|[1-9][0-9]*)$")

#: ``X-Request-ID`` character set and length (plan §11.1: ASCII, 1-64, no
#: control characters). The pattern is anchored, so no partial match is possible.
REQUEST_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def render_decimal(value: int) -> str:
    """Render a kernel counter as its canonical wire string."""
    return str(int(value))


def _validate_decimal(value: str) -> str:
    """Accept only canonical decimal strings inside ``0..BIGINT_MAX``."""
    if not isinstance(value, str):  # pragma: no cover - pydantic pre-filters
        raise ValueError("decimal_string_required")
    if not _DECIMAL_RE.fullmatch(value):
        raise ValueError("decimal_string_required")
    if len(value) > 19 or int(value) > BIGINT_MAX:
        raise ValueError("decimal_string_out_of_range")
    return value


#: A counter on the wire: ``"0"``, ``"1"``, ``"42"`` ... up to ``BIGINT_MAX``.
#:
#: Validated by pattern (not by coercion), serialised verbatim. A JSON number
#: fails: ``TypeError`` at the boundary, never a silent ``int -> str``.
DecimalString = Annotated[
    str,
    Field(
        description=(
            "Non-negative integer rendered as a decimal string (no sign, no leading "
            "zero, no exponent); max 9223372036854775807."
        ),
        examples=["0", "1", "42"],
        pattern=r"^(?:0|[1-9][0-9]*)$",
    ),
    AfterValidator(_validate_decimal),
]

#: A per-request correlation id: ASCII ``[A-Za-z0-9._-]``, 1-64 characters.
RequestId = Annotated[
    str,
    Field(
        description="Correlation id echoed in X-Request-ID; ASCII [A-Za-z0-9._-], 1-64 chars.",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9._-]{1,64}$",
    ),
]


def render_utc(value: datetime.datetime) -> str:
    """Render a timestamp as RFC 3339 UTC with a ``Z`` suffix.

    Naive values are rejected: this service only ever stores ``timestamptz``, so
    a naive value would mean the caller guessed the zone. Microseconds are kept
    when present (PostgreSQL's resolution), otherwise the form is
    ``2026-10-29T03:16:00Z``.
    """
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware (UTC)")
    utc = value.astimezone(datetime.UTC)
    if utc.microsecond:
        return utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


#: A timestamp on the wire: RFC 3339, UTC, ``Z`` suffix.
UTCDateTime = Annotated[
    datetime.datetime,
    Field(description="RFC 3339 UTC timestamp, e.g. 2026-10-29T03:16:00Z."),
    PlainSerializer(render_utc, return_type=str, when_used="json"),
]

#: Whether the client may retry the *same* request. Advisory, never a promise.
Retryable = Annotated[
    bool,
    Field(description="True only when re-sending the identical request is safe."),
]


class _ContractModel(BaseModel):
    """Base for wire models: strict, closed, and never leaking unknowns."""

    model_config = ConfigDict(
        # Unknown fields are a contract error, not something to ignore: a typo in
        # ``base_rev`` must not silently write at the wrong revision.
        extra="forbid",
        # Strip nothing and coerce nothing: the wire form is the parsed form.
        str_strip_whitespace=False,
        validate_default=True,
    )


class ErrorBody(_ContractModel):
    """The fixed error object (plan §11.1).

    ``message`` is a stable, human-readable sentence chosen by the server - it
    is **never** ``str(exception)``: PostgreSQL messages carry row values (note
    bodies, e-mail addresses) and kernel messages can name ids. ``details`` is
    for machine-readable extras (allowed field paths, ``min_retained_seq``, ...)
    and is always an object.
    """

    code: Annotated[
        str,
        Field(
            description="Stable machine-readable error code (see the protocol error table).",
            pattern=r"^[a-z][a-z0-9_]{1,63}$",
            examples=["invalid_request", "rev_conflict", "write_busy"],
        ),
    ]
    message: Annotated[
        str,
        Field(description="Short human-readable explanation; never contains secrets."),
    ]
    request_id: RequestId
    retryable: Retryable
    details: Annotated[
        dict[str, Any],
        Field(
            default_factory=dict,
            description="Machine-readable extras; always an object, possibly empty.",
        ),
    ]


class ErrorEnvelope(_ContractModel):
    """``{"error": {...}}`` - the only error body this API returns."""

    error: ErrorBody
