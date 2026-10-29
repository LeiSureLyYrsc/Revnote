"""Framework-free authentication vocabulary: errors, e-mail and password policy.

Everything here is pure: no database, no FastAPI, no Pydantic, no settings
import. ``services/`` is checked by ``test_kernel_boundaries`` and may not import
the HTTP layer or ``revnote_api.config``, so the policies that both the service
and the tests need are stated here once and reused.

Why a separate module from :mod:`revnote_api.services.auth`:

* the *rules* (what counts as a valid e-mail/password, which codes exist) are
  stable and cheap to test, while the service is about locking and transactions;
* the API layer needs :class:`AuthError` to translate into ``ApiError`` without
  importing the service's SQLAlchemy machinery.

**E-mail normalisation matches the database exactly.** Revision ``0003`` makes
uniqueness a functional index on ``lower(btrim(email))``. ``btrim`` with one
argument strips **spaces only** (not tabs/newlines), and ``lower`` folds ASCII
case. :func:`normalize_email` performs precisely those two steps so the value the
service looks up is the value the index keys on — no more, no less.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "AUTH_ERROR_CODES",
    "EMAIL_MAX_CHARS",
    "PASSWORD_MAX_BYTES",
    "PASSWORD_MAX_CHARS",
    "PASSWORD_MIN_CHARS",
    "AuthError",
    "normalize_email",
    "validate_email",
    "validate_password",
]

#: Password policy (D7, locked): 8-256 **characters**, UTF-8 <= 1024 bytes.
PASSWORD_MIN_CHARS: Final[int] = 8
PASSWORD_MAX_CHARS: Final[int] = 256
PASSWORD_MAX_BYTES: Final[int] = 1_024

#: Practical ceiling for an e-mail address (RFC 5321 path limit). Keeps a hostile
#: 10 MB "address" out of the database and out of Argon2-adjacent code paths.
EMAIL_MAX_CHARS: Final[int] = 254

#: The closed set of codes this stage can raise. Each maps to exactly one HTTP
#: status in ``api/errors.py``; the service never chooses a status itself.
AUTH_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "unauthenticated",
        "invalid_credentials",
        "invalid_refresh",
        "token_reused",
        "registration_closed",
        "invalid_request",
        "not_found",
        "auth_temporarily_unavailable",
    }
)


class AuthError(Exception):
    """A rejected authentication attempt, described in protocol terms.

    Carries a **code from the closed vocabulary** plus optional machine-readable
    ``details`` — never a message, never the offending value. The API layer turns
    this into ``ApiError(code)`` and the fixed sentence comes from
    ``api/errors.py``, so two paths cannot describe the same failure differently
    and no user input or database text can reach a response.

    ``details`` is for facts a client can act on (``min_length``,
    ``password_too_long``); it must never contain the attempted password, a token,
    an e-mail address or a SQL fragment.

    Deliberately an **ordinary mutable exception**, not a frozen dataclass: Python
    assigns ``__traceback__`` on every raise, and a frozen/slots dataclass rejects
    that in the generated ``__setattr__`` with
    ``TypeError: super(type, obj): obj must be an instance or subtype of type``
    (verified) — the error would escape as a 500 instead of the intended 4xx.
    ``code`` and ``details`` are validated in ``__init__`` and never reassigned.

    ``Exception.args`` is deliberately empty: the message the client sees belongs to
    the code, so there is nothing here that could leak through ``str()`` of a
    standard-library wrapper (``repr(exc.args)``, a generic handler, a logger).
    """

    __slots__ = ("code", "details")

    def __init__(self, code: str, details: dict[str, Any] | None = None) -> None:
        # ``Exception.__init__`` is called with **no** arguments so ``args`` stays
        # empty; ``str()``/``repr()`` are overridden below regardless.
        super().__init__()
        if code not in AUTH_ERROR_CODES:  # pragma: no cover - programming error
            raise ValueError(f"unknown auth error code {code!r}")
        if details is not None and not isinstance(details, dict):
            raise TypeError("details must be a dict or None")
        self.code = code
        self.details = details

    def __str__(self) -> str:  # never render details: they travel on the object
        return self.code

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"AuthError(code={self.code!r})"


def normalize_email(email: str) -> str:
    """Apply exactly the database's uniqueness transformation.

    ``btrim(email)`` then ``lower(...)``: strip ASCII spaces from both ends, fold
    case. Tabs and newlines are **not** stripped, because one-argument ``btrim``
    does not strip them either — if this function were more aggressive, a row
    could exist that the service can never find again.

    Returns the lookup key; callers must not store it as the display address.
    """
    return email.strip(" ").lower()


def validate_email(email: str) -> str:
    """Return the normalised e-mail, or raise ``invalid_request``.

    Deliberately stricter than the database and deliberately simpler than RFC
    5322: one ``@``, a non-empty local part, a dotted domain, ASCII only, no
    whitespace or control characters inside. The point is to reject obvious
    garbage early with a clear code, not to decide which exotic addresses are
    theoretically legal — the database's blank guard and unique index remain the
    authority on what may be stored.

    Details never echo the address: a rejected value is exactly what an attacker
    wants reflected back, and logs would then hold it.
    """
    if not isinstance(email, str):  # pragma: no cover - pydantic pre-filters
        raise AuthError("invalid_request", {"field": "email"})

    normalized = normalize_email(email)
    if not normalized:
        raise AuthError("invalid_request", {"field": "email", "reason": "blank"})
    # A surrogate cannot be valid UTF-8, hence never an ASCII address. Checked
    # before any length/character work that could raise ``UnicodeEncodeError``.
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        raise AuthError(
            "invalid_request", {"field": "email", "reason": "invalid_encoding"}
        ) from None
    if len(normalized) > EMAIL_MAX_CHARS:
        raise AuthError("invalid_request", {"field": "email", "reason": "too_long"})
    if not normalized.isascii():
        raise AuthError("invalid_request", {"field": "email", "reason": "not_ascii"})
    if any(character.isspace() or ord(character) < 0x20 for character in normalized):
        raise AuthError("invalid_request", {"field": "email", "reason": "whitespace"})

    local, separator, domain = normalized.partition("@")
    if not separator or not local or not domain:
        raise AuthError("invalid_request", {"field": "email", "reason": "malformed"})
    if "@" in domain or "." not in domain:
        raise AuthError("invalid_request", {"field": "email", "reason": "malformed"})
    if domain.startswith(".") or domain.endswith(".") or ".." in domain:
        raise AuthError("invalid_request", {"field": "email", "reason": "malformed"})
    if domain.split(".")[-1] == "":
        raise AuthError("invalid_request", {"field": "email", "reason": "malformed"})
    return normalized


def validate_password(
    password: str,
    *,
    min_chars: int = PASSWORD_MIN_CHARS,
    max_chars: int = PASSWORD_MAX_CHARS,
) -> None:
    """Raise ``invalid_request`` unless the password satisfies the locked policy.

    Rules (D7): **8-256 characters**, UTF-8 **<= 1024 bytes**, and the value is
    hashed exactly as given — no trimming, no truncation, no Unicode
    normalisation. Trimming would make a password unreproducible; truncation would
    silently accept a *different* password than the one typed.

    ``min_chars``/``max_chars`` default to the locked constants and exist so the
    service can honour ``AUTH_PASSWORD_MIN_CHARS``/``..._MAX_CHARS`` without this
    module importing settings; the byte ceiling is **not** configurable, because a
    larger one would let a request force unbounded Argon2 input.

    Character and byte limits are separate on purpose: 256 emoji are few
    characters but many bytes, and an attacker should not be able to force a
    multi-megabyte Argon2 input by staying under the character count.
    """
    if not isinstance(password, str):  # pragma: no cover - pydantic pre-filters
        raise AuthError("invalid_request", {"field": "password"})

    length = len(password)
    if length < min_chars:
        raise AuthError(
            "invalid_request",
            {"field": "password", "reason": "too_short", "min_length": min_chars},
        )
    if length > max_chars:
        raise AuthError(
            "invalid_request",
            {"field": "password", "reason": "too_long", "max_length": max_chars},
        )

    # A lone surrogate (only reachable through JSON ``\ud800``-style escapes) cannot
    # be encoded as UTF-8, so it can neither be hashed nor stored. Checked *before*
    # the byte-length measurement, which would itself raise ``UnicodeEncodeError``.
    try:
        encoded = password.encode("utf-8")
    except UnicodeEncodeError:
        raise AuthError(
            "invalid_request", {"field": "password", "reason": "invalid_encoding"}
        ) from None
    if len(encoded) > PASSWORD_MAX_BYTES:
        raise AuthError(
            "invalid_request",
            {"field": "password", "reason": "too_many_bytes", "max_bytes": PASSWORD_MAX_BYTES},
        )
