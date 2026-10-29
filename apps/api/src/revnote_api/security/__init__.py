"""Security primitives for authentication: password hashing and opaque tokens.

Two submodules, both framework-free so ``services/`` may import them:

:mod:`revnote_api.security.passwords`
    Argon2id hashing/verification, digest conversion for opaque tokens, and the
    single ``PASSWORD_HASHER`` whose costs are the locked D7 values.

:mod:`revnote_api.security.hashing`
    :class:`HashLimiter`, which runs the CPU-bound hashing callables in worker
    threads under a per-application concurrency cap.

Nothing here imports FastAPI, Pydantic or ``revnote_api.config``.
"""

from __future__ import annotations

from revnote_api.security.hashing import HashLimiter
from revnote_api.security.passwords import (
    DIGEST_BYTES,
    PASSWORD_HASHER,
    TOKEN_BYTES,
    digests_equal,
    hash_needs_rehash,
    hash_password,
    new_token,
    token_digest,
    verify_password,
)

__all__ = [
    "DIGEST_BYTES",
    "PASSWORD_HASHER",
    "TOKEN_BYTES",
    "HashLimiter",
    "digests_equal",
    "hash_needs_rehash",
    "hash_password",
    "new_token",
    "token_digest",
    "verify_password",
]
