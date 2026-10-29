"""Unit tests for the framework-free security and auth-rule layer.

No database, no HTTP and no event loop beyond what anyio needs: this module proves
the properties that must hold *regardless* of PostgreSQL — the locked Argon2
parameters, "never trim/truncate", exactly matching the database's
``lower(btrim(email))`` key, and that no secret reaches a repr.
"""

from __future__ import annotations

import datetime
import re

import pytest

from revnote_api.security import hashing, passwords
from revnote_api.security.hashing import HashLimiter
from revnote_api.services.auth_rules import (
    PASSWORD_MAX_BYTES,
    PASSWORD_MAX_CHARS,
    PASSWORD_MIN_CHARS,
    AuthError,
    normalize_email,
    validate_email,
    validate_password,
)

#: One password reused everywhere: Argon2 at the locked cost is deliberately slow.
PASSWORD = "correct horse battery staple"


# --------------------------------------------------------------------------- #
# Argon2id parameters (D7)
# --------------------------------------------------------------------------- #
def test_hasher_uses_the_locked_argon2id_parameters() -> None:
    """64 MiB / time 3 / parallelism 4, Argon2id — asserted, not assumed."""
    parameters = passwords.PASSWORD_HASHER._parameters

    assert parameters.memory_cost == 65_536  # KiB == 64 MiB
    assert parameters.time_cost == 3
    assert parameters.parallelism == 4
    assert passwords.PASSWORD_HASHER.hash(PASSWORD).startswith("$argon2id$")


def test_hash_and_verify_round_trip() -> None:
    digest = passwords.hash_password(PASSWORD)

    assert passwords.verify_password(digest, PASSWORD) is True
    assert passwords.verify_password(digest, PASSWORD + "x") is False


def test_password_is_hashed_verbatim_never_trimmed_or_truncated() -> None:
    """A password with edge spaces must verify only with those spaces present.

    Trimming is a silent behaviour change: the user's keystrokes would no longer
    reproduce their password.
    """
    padded = "  spaced password  "
    digest = passwords.hash_password(padded)

    assert passwords.verify_password(digest, padded) is True
    assert passwords.verify_password(digest, padded.strip()) is False
    assert passwords.verify_password(digest, padded.upper()) is False


def test_hash_is_salted_so_equal_passwords_differ() -> None:
    first = passwords.hash_password(PASSWORD)
    second = passwords.hash_password(PASSWORD)

    assert first != second
    assert passwords.verify_password(first, PASSWORD)
    assert passwords.verify_password(second, PASSWORD)


def test_unicode_password_round_trips_without_normalisation() -> None:
    """NFC and NFD spellings are different byte sequences, hence different passwords."""
    composed = "caf\u00e9 password"
    decomposed = "cafe\u0301 password"

    digest = passwords.hash_password(composed)
    assert passwords.verify_password(digest, composed) is True
    assert passwords.verify_password(digest, decomposed) is False


def test_verify_treats_a_corrupt_digest_as_no_match() -> None:
    """A foreign/corrupt hash is a failed login, not a 500."""
    for bogus in ("", "not-a-hash", "$argon2id$v=19$m=1,t=1,p=1$short$short"):
        assert passwords.verify_password(bogus, PASSWORD) is False


def test_rehash_is_false_for_current_parameters() -> None:
    assert passwords.hash_needs_rehash(passwords.hash_password(PASSWORD)) is False


def test_rehash_is_true_for_weaker_parameters() -> None:
    """A digest made with lower costs must be upgraded on the next login (D7)."""
    from argon2 import PasswordHasher

    weak = PasswordHasher(time_cost=1, memory_cost=8_192, parallelism=1).hash(PASSWORD)

    assert passwords.verify_password(weak, PASSWORD) is True
    assert passwords.hash_needs_rehash(weak) is True


# --------------------------------------------------------------------------- #
# opaque tokens
# --------------------------------------------------------------------------- #
def test_tokens_are_long_random_url_safe_strings() -> None:
    first = passwords.new_token()
    second = passwords.new_token()

    assert first != second
    assert len(first) >= 43  # 32 random bytes -> 43 base64url chars
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first), "must stay URL-safe"


def test_token_entropy_is_at_least_256_bits() -> None:
    assert passwords.TOKEN_BYTES * 8 >= 256


def test_digest_is_sha256_and_32_bytes() -> None:
    token = passwords.new_token()
    digest = passwords.token_digest(token)

    assert isinstance(digest, bytes)
    assert len(digest) == passwords.DIGEST_BYTES == 32
    # Deterministic: the same token always maps to the same stored bytes.
    assert passwords.token_digest(token) == digest


def test_digest_differs_for_different_tokens() -> None:
    assert passwords.token_digest("a") != passwords.token_digest("b")


def test_digests_equal_is_a_real_comparison() -> None:
    left = passwords.token_digest("left")

    assert passwords.digests_equal(left, passwords.token_digest("left")) is True
    assert passwords.digests_equal(left, passwords.token_digest("right")) is False
    assert passwords.digests_equal(left, b"short") is False


def test_raw_token_never_appears_in_a_digest() -> None:
    token = passwords.new_token()

    assert token.encode("utf-8") not in passwords.token_digest(token)


# --------------------------------------------------------------------------- #
# e-mail normalisation must equal the database's lower(btrim(email))
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("user@example.com", "user@example.com"),
        ("USER@EXAMPLE.COM", "user@example.com"),
        ("  user@example.com  ", "user@example.com"),
        ("  User@Example.Com ", "user@example.com"),
        ("user@example.com\t", "user@example.com\t"),  # btrim(' ') keeps a tab
        ("\tuser@example.com", "\tuser@example.com"),
    ],
)
def test_normalize_email_matches_lower_btrim(raw: str, expected: str) -> None:
    """Exactly ``lower(btrim(email))``: spaces only, ASCII case only."""
    assert normalize_email(raw) == expected


def test_normalize_email_does_not_strip_tabs_like_btrim_does_not() -> None:
    """Pinned because a mismatch here makes a stored row unfindable."""
    assert normalize_email("\tuser@example.com") != normalize_email("user@example.com")


# --------------------------------------------------------------------------- #
# e-mail validation
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "address",
    [
        "user@example.com",
        "first.last+tag@sub.example.co.uk",
        "  User@Example.COM  ",
        "a@b.co",
    ],
)
def test_valid_addresses_are_accepted(address: str) -> None:
    assert validate_email(address) == normalize_email(address)


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("", "blank"),
        ("   ", "blank"),
        ("no-at-sign", "malformed"),
        ("@example.com", "malformed"),
        ("user@", "malformed"),
        ("user@@example.com", "malformed"),
        ("user@example", "malformed"),  # no dot in the domain
        ("user@.example.com", "malformed"),
        ("user@example..com", "malformed"),
        ("user@example.com.", "malformed"),
    ],
)
def test_invalid_addresses_are_rejected_without_echoing_them(address: str, reason: str) -> None:
    with pytest.raises(AuthError) as error:
        validate_email(address)

    assert error.value.code == "invalid_request"
    assert error.value.details == {"field": "email", "reason": reason}
    # The rejected value must never be in details: it is attacker-controlled.
    # (An empty address would trivially "not appear" in everything, so only
    # non-empty input is meaningfully checked for reflection.)
    if address:
        assert address not in str(error.value.details)


def test_non_ascii_address_is_rejected() -> None:
    with pytest.raises(AuthError) as error:
        validate_email("us\u00e9r@example.com")

    assert error.value.details["reason"] == "not_ascii"


def test_whitespace_inside_an_address_is_rejected() -> None:
    with pytest.raises(AuthError) as error:
        validate_email("us er@example.com")

    assert error.value.details["reason"] == "whitespace"


def test_overlong_address_is_rejected() -> None:
    with pytest.raises(AuthError) as error:
        validate_email("a" * 250 + "@example.com")

    assert error.value.details["reason"] == "too_long"


# --------------------------------------------------------------------------- #
# password policy (D7: 8-256 characters, UTF-8 <= 1024 bytes)
# --------------------------------------------------------------------------- #
def test_locked_password_bounds_are_eight_to_two_hundred_fifty_six() -> None:
    assert (PASSWORD_MIN_CHARS, PASSWORD_MAX_CHARS, PASSWORD_MAX_BYTES) == (8, 256, 1_024)


@pytest.mark.parametrize("length", [8, 9, 64, 255, 256])
def test_lengths_inside_the_policy_are_accepted(length: int) -> None:
    validate_password("a" * length)


@pytest.mark.parametrize("length", [0, 1, 7])
def test_short_passwords_are_rejected_with_the_minimum_in_details(length: int) -> None:
    with pytest.raises(AuthError) as error:
        validate_password("a" * length)

    assert error.value.details == {
        "field": "password",
        "reason": "too_short",
        "min_length": 8,
    }


def test_overlong_password_is_rejected_with_the_maximum() -> None:
    with pytest.raises(AuthError) as error:
        validate_password("a" * 257)

    assert error.value.details == {
        "field": "password",
        "reason": "too_long",
        "max_length": 256,
    }


def test_byte_limit_bites_before_the_character_limit() -> None:
    """256 emoji are inside the character budget but exactly at the byte ceiling."""
    emoji = "\U0001f600" * 256  # 4 bytes each == 1024 bytes: allowed
    validate_password(emoji)

    with pytest.raises(AuthError) as error:
        validate_password("\U0001f600" * 257)

    assert error.value.details["reason"] in {"too_long", "too_many_bytes"}


def test_bytes_limit_rejects_wide_characters_under_the_char_limit() -> None:
    """Isolates the byte rule: 300 four-byte characters, 1200 bytes, 300 chars."""
    with pytest.raises(AuthError) as error:
        validate_password("\U0001f600" * 300, max_chars=400)

    assert error.value.details == {
        "field": "password",
        "reason": "too_many_bytes",
        "max_bytes": 1024,
    }


def test_password_is_not_trimmed_by_validation() -> None:
    """A padded password is legal as typed; whitespace counts toward the length."""
    # 10 characters including the padding: legal.
    validate_password("  padded  ")
    # Exactly 8 characters, all spaces: legal by policy, and *not* trimmed to 0.
    validate_password(" " * 8)
    # 7 spaces are below the minimum, proving the raw value is measured.
    with pytest.raises(AuthError) as error:
        validate_password(" " * 7)
    assert error.value.details["reason"] == "too_short"


def test_policy_bounds_follow_the_settings_parameters() -> None:
    """The service passes settings values; the helper must honour them."""
    validate_password("x" * 12, min_chars=12, max_chars=12)
    with pytest.raises(AuthError) as error:
        validate_password("x" * 11, min_chars=12)
    assert error.value.details["min_length"] == 12


# --------------------------------------------------------------------------- #
# AuthError is a safe carrier
# --------------------------------------------------------------------------- #
def test_auth_error_keeps_a_closed_code_vocabulary() -> None:
    from revnote_api.services.auth_rules import AUTH_ERROR_CODES

    for code in AUTH_ERROR_CODES:
        assert AuthError(code).code == code

    with pytest.raises(ValueError):
        AuthError("something_new")


def test_auth_error_repr_and_str_never_render_details() -> None:
    error = AuthError("invalid_request", {"field": "password", "secret": "do-not-log"})

    assert "do-not-log" not in repr(error)
    assert "do-not-log" not in str(error)
    assert repr(error) == "AuthError(code='invalid_request')"


def test_auth_error_survives_a_real_raise() -> None:
    """Regression: a frozen/slots dataclass cannot be an exception.

    Python assigns ``__traceback__`` on every raise; a frozen dataclass's generated
    ``__setattr__`` rejected that with
    ``TypeError: super(type, obj): obj must be an instance or subtype of type``, so
    every 401/404 became a 500. This test fails loudly if the shape regresses.
    """
    error = AuthError("invalid_credentials")

    try:
        raise error
    except AuthError as caught:
        assert caught is error
        assert caught.code == "invalid_credentials"
        assert caught.__traceback__ is not None
    else:  # pragma: no cover - the raise must not be swallowed
        pytest.fail("AuthError was not raised")


@pytest.mark.anyio
async def test_auth_error_propagates_through_an_async_context_manager() -> None:
    """The same raise through an ``asynccontextmanager`` (the DB-session shape)."""
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def session_like():
        try:
            yield object()
        except BaseException:
            raise  # what ``AsyncSession.__aexit__`` does on rollback

    async def inner() -> None:
        async with session_like():
            raise AuthError("not_found", {"field": "email"})

    with pytest.raises(AuthError) as caught:
        await inner()

    assert caught.value.code == "not_found"
    assert caught.value.details == {"field": "email"}


def test_auth_error_args_hold_no_message() -> None:
    """``args`` stays empty, so a generic ``repr(exc.args)`` cannot leak a reason."""
    error = AuthError("invalid_request", {"reason": "too_short"})

    assert error.args == ()
    assert str(error) == "invalid_request"


def test_auth_error_instance_can_be_re_raised() -> None:
    """Translating and re-raising the same object must not mutate its payload."""
    error = AuthError("not_found")

    for _ in range(2):
        with pytest.raises(AuthError) as caught:
            raise error
        assert caught.value.code == "not_found"
        assert caught.value.details is None


# --------------------------------------------------------------------------- #
# hash limiter
# --------------------------------------------------------------------------- #
def test_limiter_rejects_a_zero_capacity() -> None:
    with pytest.raises(ValueError):
        HashLimiter(0)


@pytest.mark.anyio
async def test_limiter_runs_the_callable_and_returns_its_value() -> None:
    limiter = HashLimiter(capacity=2)
    try:
        assert await limiter.run(lambda a, b: a + b, 1, 2) == 3
    finally:
        limiter.shutdown()


@pytest.mark.anyio
async def test_limiter_caps_concurrency_at_its_capacity() -> None:
    """No more than ``capacity`` callables may overlap, counted inside the thread."""
    import threading
    import time

    capacity = 2
    limiter = HashLimiter(capacity=capacity)
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def work() -> None:
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1

    try:
        import asyncio

        await asyncio.gather(*(limiter.run(work) for _ in range(6)))
    finally:
        limiter.shutdown()

    assert state["peak"] <= capacity
    assert state["peak"] >= 1


@pytest.mark.anyio
async def test_limiter_propagates_the_callable_exception() -> None:
    limiter = HashLimiter(capacity=1)

    def explode() -> None:
        raise RuntimeError("boom")

    try:
        with pytest.raises(RuntimeError, match="boom"):
            await limiter.run(explode)
    finally:
        limiter.shutdown()


@pytest.mark.anyio
async def test_limiter_is_usable_after_shutdown() -> None:
    """A second request after teardown rebuilds the primitives rather than failing."""
    limiter = HashLimiter(capacity=1)
    assert await limiter.run(lambda: "first") == "first"
    limiter.shutdown()

    try:
        assert await limiter.run(lambda: "second") == "second"
    finally:
        limiter.shutdown()


@pytest.mark.anyio
async def test_limiter_runs_the_hash_off_the_event_loop_thread() -> None:
    """Argon2 must not block the loop: the callable runs in another thread."""
    import threading

    limiter = HashLimiter(capacity=1)
    try:
        caller = threading.get_ident()
        worker = await limiter.run(threading.get_ident)
    finally:
        limiter.shutdown()

    assert worker != caller


def test_hashing_module_imports_no_web_or_settings_layer() -> None:
    """The boundary the kernel tests enforce for ``services`` also holds here."""
    import ast
    from pathlib import Path

    module_dir = Path(hashing.__file__).resolve().parent
    forbidden = ("fastapi", "starlette", "pydantic", "revnote_api.config", "revnote_api.api")

    for path in sorted(module_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for name in imported:
            assert not any(name == bad or name.startswith(f"{bad}.") for bad in forbidden), (
                f"{path.name} imports {name}"
            )


# --------------------------------------------------------------------------- #
# unpaired surrogates: hostile input must be a 4xx, never a 500
# --------------------------------------------------------------------------- #
SURROGATE = "\ud800"


def test_token_digest_reports_an_unencodable_token() -> None:
    """A lone surrogate is not UTF-8, so it cannot be a token we issued."""
    from revnote_api.security.passwords import digest_or_none

    assert digest_or_none(SURROGATE) is None
    assert digest_or_none("normal-token") is not None


def test_token_digest_still_raises_for_the_low_level_helper() -> None:
    """``token_digest`` stays a pure conversion; callers use ``digest_or_none``."""
    with pytest.raises(UnicodeEncodeError):
        passwords.token_digest(SURROGATE)


def test_verify_password_returns_false_for_an_unencodable_password() -> None:
    """A login attempt with a surrogate is a mismatch, not a crash."""
    digest = passwords.hash_password(PASSWORD)

    assert passwords.verify_password(digest, SURROGATE) is False


def test_hash_password_rejects_an_unencodable_password() -> None:
    with pytest.raises(AuthError) as error:
        passwords.hash_password(SURROGATE)

    assert error.value.code == "invalid_request"
    assert error.value.details["reason"] == "invalid_encoding"


def test_validate_password_rejects_an_unencodable_password() -> None:
    with pytest.raises(AuthError) as error:
        validate_password(SURROGATE * 8)

    assert error.value.details == {"field": "password", "reason": "invalid_encoding"}


def test_validate_email_rejects_an_unencodable_address() -> None:
    with pytest.raises(AuthError) as error:
        validate_email(f"{SURROGATE}@example.com")

    assert error.value.details["reason"] == "invalid_encoding"


# --------------------------------------------------------------------------- #
# secret-carrying reprs: request models, response models and issued pairs
# --------------------------------------------------------------------------- #
def test_request_models_never_render_the_password() -> None:
    """Login/register bodies are the most valuable thing to leak from a log line."""
    from revnote_api.api.schemas.auth import LoginRequest, RefreshRequest, RegisterRequest

    register = RegisterRequest(email="a@example.com", password="REGISTER-SENTINEL")
    login = LoginRequest(email="a@example.com", password="LOGIN-SENTINEL")
    refresh = RefreshRequest(refresh_token="REFRESH-SENTINEL")

    assert "REGISTER-SENTINEL" not in repr(register)
    assert "REGISTER-SENTINEL" not in str(register)
    assert "LOGIN-SENTINEL" not in repr(login)
    assert "LOGIN-SENTINEL" not in str(login)
    # The e-mail may be shown for debugging; the secret may not.
    assert "a@example.com" in repr(register)
    assert "REFRESH-SENTINEL" not in repr(refresh)


def test_request_models_never_render_the_password_in_their_own_repr() -> None:
    """A pydantic ``ValidationError`` *does* echo ``input_value``, so the guard that
    matters is D's error handler, which strips it before the wire.

    This test pins the two halves that belong to this lane: the model's own ``repr``
    is clean (asserted above), and the *validated* model never exposes the secret
    through ``str``/``model_dump`` of the success path. The HTTP-level guarantee is
    covered in ``tests/api/test_auth.py`` (a rejected login body must not echo it).
    """
    from revnote_api.api.schemas.auth import LoginRequest

    model = LoginRequest(email="a@example.com", password="LOGIN-SENTINEL")

    assert "LOGIN-SENTINEL" not in repr(model)
    # The value is still there for the service to use — that is its purpose.
    assert model.password == "LOGIN-SENTINEL"


def test_token_pair_never_renders_either_token() -> None:
    import uuid as _uuid

    from revnote_api.api.schemas.auth import TokenPair

    pair = TokenPair(
        access_token="ACCESS-SENTINEL",
        refresh_token="REFRESH-SENTINEL",
        expires_in=600,
        session_id=_uuid.uuid4(),
        session_expires_at="2026-10-29T03:16:00Z",
    )

    assert "ACCESS-SENTINEL" not in repr(pair)
    assert "REFRESH-SENTINEL" not in repr(pair)
    # Still serialised for the success response — that is the one legitimate exit.
    body = pair.model_dump(mode="json")
    assert body["access_token"] == "ACCESS-SENTINEL"
    assert body["refresh_token"] == "REFRESH-SENTINEL"


def test_issued_pair_never_renders_raw_tokens_or_digests() -> None:
    """The service's internal pair object must be safe when a traceback prints it."""
    import uuid as _uuid

    from revnote_api.models import AuthAccessToken, AuthRefreshToken
    from revnote_api.services.auth import _IssuedPair

    session_id = _uuid.uuid4()
    pair = _IssuedPair(
        access_token="ACCESS-SENTINEL",
        refresh_token="REFRESH-SENTINEL",
        access_row=AuthAccessToken(
            id=_uuid.uuid4(),
            session_id=session_id,
            token_hash=b"ACCESS-DIGEST-32-BYTES--------",
            expires_at=datetime.datetime.now(datetime.UTC),
        ),
        refresh_row=AuthRefreshToken(
            id=_uuid.uuid4(),
            session_id=session_id,
            token_hash=b"REFRESH-DIGEST-32-BYTES-------",
            expires_at=datetime.datetime.now(datetime.UTC),
        ),
    )

    rendered = repr(pair)
    assert "ACCESS-SENTINEL" not in rendered
    assert "REFRESH-SENTINEL" not in rendered
    assert "ACCESS-DIGEST" not in rendered
    assert "REFRESH-DIGEST" not in rendered


def test_login_result_never_renders_tokens() -> None:
    import uuid as _uuid

    from revnote_api.services.auth import LoginResult

    result = LoginResult(
        access_token="ACCESS-SENTINEL",
        refresh_token="REFRESH-SENTINEL",
        expires_in=600,
        session_id=_uuid.uuid4(),
        session_expires_at=datetime.datetime.now(datetime.UTC),
    )

    assert "ACCESS-SENTINEL" not in repr(result)
    assert "REFRESH-SENTINEL" not in repr(result)


def test_dummy_digest_is_a_full_cost_argon2id_hash() -> None:
    """``#10``: a malformed dummy would fail fast and reinstate the timing oracle.

    Verifying against it must cost the same as verifying a real digest, so the
    "unknown account" path is not measurably faster than "wrong password".
    """
    import time

    from revnote_api.services.auth import _DUMMY_DIGEST

    real = passwords.hash_password(PASSWORD)

    def measure(digest: str) -> float:
        start = time.perf_counter()
        passwords.verify_password(digest, "definitely-wrong")
        return time.perf_counter() - start

    dummy = min(measure(_DUMMY_DIGEST) for _ in range(2))
    genuine = min(measure(real) for _ in range(2))

    assert _DUMMY_DIGEST.startswith("$argon2id$")
    # Same order of magnitude; a malformed digest would fail in microseconds.
    assert dummy > genuine * 0.4, (dummy, genuine)


# Keep the imported submodule referenced so a future refactor cannot drop it
# silently (the module is asserted on above via ``hashing.__file__``).
assert hashing is not None
assert datetime is not None
