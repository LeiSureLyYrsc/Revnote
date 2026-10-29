"""Password hashing and opaque-token primitives (framework-free).

This module is the *only* place that knows how a password becomes a digest or how
a bearer token becomes a database key. It deliberately imports no FastAPI, no
Pydantic and no ``revnote_api.config``: ``services/`` is checked by
``test_kernel_boundaries`` and it may not reach into the HTTP or settings layer,
so anything it needs is passed in or declared as a structural protocol here.

Two separate concerns live side by side because they share one rule — *the raw
value never survives the call*:

**Password hashing (Argon2id)**

* Parameters are fixed by the plan (D7): 64 MiB, time cost 3, parallelism 4. They
  are baked into every digest, so a future change is handled by
  :meth:`PasswordHasher.check_needs_rehash` at login rather than by invalidating
  everyone's password.
* Input is **never trimmed or truncated**. The service boundary validates length
  (8-256 characters, UTF-8 <= 1024 bytes) and this module hashes exactly the
  string it is given — silently "fixing" a password would make it impossible to
  log in with the same keystrokes a user typed.
* Verification returns a boolean and swallows ``VerifyMismatchError``: a wrong
  password is an ordinary outcome, not an exception the caller has to translate.
  A malformed/foreign digest in the database is treated as "no match" rather than
  crashing a login.

**Opaque tokens**

* Access and refresh tokens are pure random bytes rendered as URL-safe text with
  at least 256 bits of entropy. They are *opaque*: nothing about them is signed or
  parseable, so revocation is immediate and a leaked database row cannot be
  replayed.
* Only ``sha256(raw)`` is stored. :func:`token_digest` is the single conversion
  point, which is what makes "we never persist a raw token" checkable by reading
  one function.
* Comparison is constant-time (:func:`digests_equal`) where a caller compares
  digests directly; normal verification is an indexed lookup by digest, which
  leaks nothing beyond "this digest exists".
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher as _Argon2PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from revnote_api.services.auth_rules import AuthError

__all__ = [
    "DIGEST_BYTES",
    "PASSWORD_HASHER",
    "TOKEN_BYTES",
    "digest_or_none",
    "digests_equal",
    "hash_needs_rehash",
    "hash_password",
    "new_token",
    "token_digest",
    "verify_password",
]

#: SHA-256 output length; ``auth_*_tokens.token_hash`` is a CHECKed 32-byte BYTEA.
DIGEST_BYTES = 32

#: Raw entropy per token: 32 bytes = 256 bits, the plan's minimum (D1).
TOKEN_BYTES = 32

#: The one hasher instance. Argon2's parameters are read from the digest at verify
#: time, so a single stateless object serves both old and new hashes.
#:
#: The costs are the locked D7 values. They are module constants rather than
#: settings because a per-deployment Argon2 cost would make "the same password
#: verifies" depend on which node answered; ``Settings`` still mirrors them for
#: documentation and ``.env.example``.
PASSWORD_HASHER = _Argon2PasswordHasher(
    time_cost=3,
    memory_cost=65_536,  # KiB == 64 MiB
    parallelism=4,
    hash_len=32,
    salt_len=16,
)


def hash_password(password: str) -> str:
    """Return the Argon2id digest for ``password``.

    The input is hashed **verbatim** — no ``strip()``, no truncation, no Unicode
    normalisation. Length policy is enforced by the caller (8-256 characters,
    UTF-8 <= 1024 bytes) so that the same rule produces the same rejection for
    registration and login.

    A password containing an unpaired surrogate (possible only via JSON
    ``\\ud800``-style escapes) is not representable as UTF-8 and Argon2 cannot hash
    it. It is rejected with ``invalid_request`` rather than allowed to raise
    ``UnicodeEncodeError`` into a 500; the policy check in
    :mod:`revnote_api.services.auth_rules` normally catches this first, and this
    guard exists so no call path can be missed.
    """
    try:
        password.encode("utf-8")
    except UnicodeEncodeError:
        raise AuthError(
            "invalid_request", {"field": "password", "reason": "invalid_encoding"}
        ) from None
    return PASSWORD_HASHER.hash(password)


def verify_password(digest: str, password: str) -> bool:
    """True when ``password`` matches ``digest``.

    A wrong password, a corrupted digest, or a digest produced by some other
    scheme all return ``False``: the login path must answer with one uniform
    ``invalid_credentials`` and must not branch on *why* it failed. Only a
    programming error (e.g. a non-string) propagates.

    An unencodable password also returns ``False``: it cannot be the stored
    credential, and a login attempt must not distinguish that from a typo.
    """
    try:
        return PASSWORD_HASHER.verify(digest, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    except UnicodeEncodeError:
        return False


def hash_needs_rehash(digest: str) -> bool:
    """True when ``digest`` was produced with different (older) parameters.

    Called after a successful login so an operator can raise the cost later
    without a migration: the next successful login rewrites the row.
    """
    return PASSWORD_HASHER.check_needs_rehash(digest)


def new_token() -> str:
    """Return a fresh opaque token: 256 bits of entropy, URL-safe text.

    ``secrets.token_urlsafe`` is used rather than ``uuid4()`` because a UUID
    carries only 122 random bits and announces its format.
    """
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_digest(raw_token: str) -> bytes:
    """Return the stored form of ``raw_token``: its SHA-256 digest.

    The token is encoded as UTF-8 before hashing so the same string always maps
    to the same 32 bytes regardless of platform. This is the *only* place a raw
    token is converted; nothing else in the code base may store the raw value.

    A string containing an unpaired surrogate is **not** valid UTF-8 (it can only
    arise from a client sending ``\\ud800``-style escapes, which JSON permits), so
    it cannot be a token this service ever issued. Such input is rejected with
    :class:`UnicodeEncodeError` — see :func:`digest_or_none`, which is what the
    auth paths call so the caller can map it to its own code (access vs refresh)
    without this low-level helper deciding protocol semantics.
    """
    return hashlib.sha256(raw_token.encode("utf-8")).digest()


def digest_or_none(raw_token: str) -> bytes | None:
    """``token_digest(raw_token)``, or ``None`` when it cannot be a token.

    Used by every lookup path: an unencodable token is simply "no such token", so
    an access token yields 401 ``unauthenticated`` and a refresh token yields 401
    ``invalid_refresh`` — decided by the caller, never by a 500 from ``.encode()``.
    """
    try:
        return token_digest(raw_token)
    except UnicodeEncodeError:
        return None


def digests_equal(left: bytes, right: bytes) -> bool:
    """Constant-time comparison for two digests.

    Used where a caller already holds both values (e.g. asserting a rotated token
    does not collide). Lookup-by-unique-index needs no comparison at all.
    """
    return hmac.compare_digest(left, right)
