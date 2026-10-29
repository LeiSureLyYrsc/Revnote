"""HTTP-level auth behaviour against real PostgreSQL.

Every case goes through the real ASGI app, the real middleware stack, the real
error envelope and the real database — no mocks of the service, no SQLite, no
skips. What this module deliberately does *not* re-prove: Argon2 parameters and
e-mail/password policy details (``tests/unit/test_auth_security.py``), and
password/token leak sentinels at the model level (unit).

Coverage map to the contract (``docs/phase3-sync-api-spec.md`` §2):

* register: closed by default, and when open creates user + workspace + owner
  membership without logging in;
* login: uniform 401 for every failure mode, including a legacy ``NULL`` hash and a
  disabled account; success returns a token pair with ``Cache-Control: no-store``;
* refresh: rotates once, and a replayed token revokes the family, killing the
  winner's access token immediately (R1);
* logout: 204 and idempotent, and the access token stops working;
* /me: requires a token, reports the caller and its workspaces;
* workspace access: unknown workspace and non-member are indistinguishable (404).
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from pg_support.auth_support import (
    API_V1,
    DEFAULT_PASSWORD,
    AuthTestApp,
    seed_account,
    seed_member,
    seed_user,
    seed_workspace_for,
)
from revnote_api.db.session import get_sessionmaker
from revnote_api.services.auth import assert_workspace_member
from revnote_api.services.auth_rules import AuthError

pytestmark = [pytest.mark.anyio, pytest.mark.api]


def unique_email(prefix: str) -> str:
    """A per-run unique address.

    The API suite runs against a database that is *not* truncated between runs (the
    kernel tests follow the same convention, isolating by fresh UUIDs). Fixed
    addresses would make the second run of the gate fail on
    ``uq_users_email_normalized`` — so every test that actually inserts an account
    derives its address from a fresh UUID.
    """
    return f"{prefix}-{uuid.uuid4().hex}@revnote.test"


# --------------------------------------------------------------------------- #
# register
# --------------------------------------------------------------------------- #
async def test_register_is_closed_by_default(auth_app: AuthTestApp) -> None:
    """D6: a fresh deployment has no self-service signup."""
    assert auth_app.settings.auth_registration_open is False

    response = await auth_app.client.post(
        f"{API_V1}/auth/register",
        json={"email": unique_email("new"), "password": DEFAULT_PASSWORD},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "registration_closed"


async def test_register_is_not_available_on_the_open_app_while_closed(
    auth_app: AuthTestApp,
) -> None:
    """The closed app must stay closed: the two fixtures are fully independent."""
    closed_email = unique_email("still-closed")
    create = await auth_app.client.post(
        f"{API_V1}/auth/register",
        json={"email": closed_email, "password": DEFAULT_PASSWORD},
    )

    assert create.status_code == 403
    async with auth_app.factory() as session:
        found = (
            await session.execute(
                text("SELECT count(*) FROM users WHERE email = :email"), {"email": closed_email}
            )
        ).scalar_one()
    assert found == 0, "a refused registration must not have written a row"


async def test_register_creates_user_workspace_and_membership_without_logging_in(
    open_registration_app: AuthTestApp,
) -> None:
    """D6: user + default workspace + owner member, atomically, no token."""
    app = open_registration_app
    # A per-run address, spelled with the case/space noise this test exists to prove
    # is normalised. ``unique_email`` keeps the second local run working: the suite
    # writes committed rows and never truncates, so a fixed address would collide with
    # its own earlier run on ``uq_users_email_normalized``.
    normalised = unique_email("newcomer")
    local, _, domain = normalised.partition("@")
    raw_email = f"  {local.upper()}@{domain.title()}  "

    response = await app.client.post(
        f"{API_V1}/auth/register",
        json={"email": raw_email, "password": DEFAULT_PASSWORD},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    # Plan §9.1: a *new* registration is stored normalised (legacy rows are not).
    assert body["user"]["email"] == normalised
    assert body["workspace"]["role"] == "owner"
    assert "access_token" not in body and "refresh_token" not in body

    async with app.factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT w.id, w.owner_id, m.user_id, m.role "
                    "FROM workspaces w JOIN workspace_members m ON m.workspace_id = w.id "
                    "WHERE w.id = :id"
                ),
                {"id": body["workspace"]["id"]},
            )
        ).one()
        stored_email = (
            await session.execute(
                text("SELECT email FROM users WHERE id = :id"), {"id": body["user"]["id"]}
            )
        ).scalar_one()

    assert str(row.user_id) == body["user"]["id"]
    assert row.role == "owner"
    assert stored_email == normalised

    # And the new account can log in with the normalised form of that address.
    logged_in = await app.login(normalised)
    assert logged_in.status_code == 200, logged_in.text


async def test_register_rejects_a_duplicate_address_after_normalisation(
    open_registration_app: AuthTestApp,
) -> None:
    """``lower(btrim(email))`` is the uniqueness key, so case/space variants collide."""
    app = open_registration_app
    # Unique per run (the suite never truncates), same address in both requests.
    address = unique_email("dupe")
    first = await app.client.post(
        f"{API_V1}/auth/register",
        json={"email": address, "password": DEFAULT_PASSWORD},
    )
    assert first.status_code == 201

    # The same address, spelled with case and padding noise: the normalised index
    # must treat it as one account.
    local, _, domain = address.partition("@")
    noisy = f"  {local.upper()}@{domain.title()} "
    second = await app.client.post(
        f"{API_V1}/auth/register",
        json={"email": noisy, "password": DEFAULT_PASSWORD},
    )

    assert second.status_code == 400
    assert second.json()["error"]["details"]["field"] == "email"


async def test_register_rejects_a_short_password(open_registration_app: AuthTestApp) -> None:
    app = open_registration_app

    response = await app.client.post(
        f"{API_V1}/auth/register",
        json={"email": unique_email("short"), "password": "1234567"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


# --------------------------------------------------------------------------- #
# login
# --------------------------------------------------------------------------- #
async def test_login_returns_a_token_pair_that_is_uncacheable(auth_app: AuthTestApp) -> None:
    account = await seed_account(auth_app.factory)

    response = await auth_app.login(account.email)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == auth_app.settings.auth_access_ttl_s
    assert body["access_token"] and body["refresh_token"]
    assert body["access_token"] != body["refresh_token"]
    assert body["session_id"]
    assert body["session_expires_at"]
    assert response.headers["cache-control"] == "no-store"


async def test_login_accepts_the_normalised_address_form(auth_app: AuthTestApp) -> None:
    seeded = await seed_account(auth_app.factory, email=unique_email("case"))

    # Same address, different case and surrounding spaces (btrim strips spaces).
    noisy = f"  {seeded.email.upper()}  "
    response = await auth_app.login(noisy)

    assert response.status_code == 200, response.text
    assert response.json()["session_id"]


async def test_login_wrong_password_is_invalid_credentials(auth_app: AuthTestApp) -> None:
    account = await seed_account(auth_app.factory)

    response = await auth_app.login(account.email, "definitely-wrong")

    assert response.status_code == 401
    body = response.json()["error"]
    assert body["code"] == "invalid_credentials"
    assert body["retryable"] is False
    assert body["details"] == {}


async def test_login_unknown_address_is_the_same_401(auth_app: AuthTestApp) -> None:
    """No enumeration: an unknown address answers exactly like a wrong password."""
    response = await auth_app.login("nobody@revnote.test")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert response.json()["error"]["details"] == {}
    assert response.headers["www-authenticate"] == "Bearer"


async def test_login_legacy_account_without_a_password_cannot_log_in(
    auth_app: AuthTestApp,
) -> None:
    """D8: a pre-Phase-3 row keeps ``password_hash = NULL`` and is not locally usable."""
    account = await seed_account(auth_app.factory, password_hash=False)

    response = await auth_app.login(account.email, DEFAULT_PASSWORD)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_disabled_account_cannot_log_in(auth_app: AuthTestApp) -> None:
    account = await seed_account(auth_app.factory, disabled=True)

    response = await auth_app.login(account.email)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_login_records_the_device_name_on_the_session(auth_app: AuthTestApp) -> None:
    account = await seed_account(auth_app.factory)

    response = await auth_app.login(account.email, device_name="laptop")
    assert response.status_code == 200

    async with auth_app.factory() as session:
        stored = (
            await session.execute(
                text("SELECT device_name FROM auth_sessions WHERE id = :id"),
                {"id": response.json()["session_id"]},
            )
        ).scalar_one()
    assert stored == "laptop"


async def test_login_stores_only_a_digest_and_never_the_raw_token(
    auth_app: AuthTestApp,
) -> None:
    account = await seed_account(auth_app.factory)

    response = await auth_app.login(account.email)
    access, refresh = response.json()["access_token"], response.json()["refresh_token"]

    async with auth_app.factory() as session:
        stored = (
            await session.execute(text("SELECT token_hash FROM auth_access_tokens"))
        ).scalars()
        for digest in stored:
            raw = bytes(digest)
            assert raw != access.encode("utf-8")
            assert raw not in (access + refresh).encode("utf-8")


# --------------------------------------------------------------------------- #
# /me and workspace authorisation
# --------------------------------------------------------------------------- #
async def test_me_requires_a_token(auth_app: AuthTestApp) -> None:
    response = await auth_app.client.get(f"{API_V1}/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
    assert response.headers["www-authenticate"] == "Bearer"


async def test_me_returns_the_caller_and_its_workspaces(auth_app: AuthTestApp) -> None:
    account = await seed_account(auth_app.factory)
    token = await auth_app.login_token(account)

    response = await auth_app.client.get(f"{API_V1}/me", headers=auth_app.bearer(token))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["id"] == str(account.user_id)
    assert body["user"]["email"] == account.email
    assert body["workspaces"] == [{"id": str(account.workspace_id), "role": "owner"}]


async def test_me_rejects_a_revoked_session_access_token(auth_app: AuthTestApp) -> None:
    """Revocation is immediate: no cached authorisation survives logout."""
    account = await seed_account(auth_app.factory)
    token = await auth_app.login_token(account)

    assert (
        await auth_app.client.post(f"{API_V1}/auth/logout", headers=auth_app.bearer(token))
    ).status_code == 204

    response = await auth_app.client.get(f"{API_V1}/me", headers=auth_app.bearer(token))
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_me_rejects_a_garbage_token(auth_app: AuthTestApp) -> None:
    response = await auth_app.client.get(
        f"{API_V1}/me", headers=auth_app.bearer("not-a-real-token")
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


# --------------------------------------------------------------------------- #
# secrets never reach a response body (plan §3: no password/token echo)
# --------------------------------------------------------------------------- #
async def test_a_rejected_login_body_never_echoes_the_password(
    auth_app: AuthTestApp,
) -> None:
    """D's validation handler must not reflect the submitted secret back."""
    sentinel = "SENTINELPW" + "x" * 300  # over the 256-char limit

    response = await auth_app.client.post(
        f"{API_V1}/auth/login", json={"email": "a@example.com", "password": sentinel}
    )

    assert response.status_code == 400
    assert "SENTINELPW" not in response.text
    assert len(response.text) < 1_000, "no giant input echoed"


async def test_a_wrong_password_response_never_echoes_it(auth_app: AuthTestApp) -> None:
    account = await seed_account(auth_app.factory)
    sentinel = "wrong-SENTINEL-password"

    response = await auth_app.login(account.email, sentinel)

    assert response.status_code == 401
    assert "SENTINEL" not in response.text


async def test_a_bad_refresh_token_is_not_echoed(auth_app: AuthTestApp) -> None:
    response = await auth_app.client.post(
        f"{API_V1}/auth/refresh", json={"refresh_token": "REFRESH-SENTINEL-VALUE"}
    )

    assert response.status_code == 401
    assert "REFRESH-SENTINEL-VALUE" not in response.text


async def test_an_invalid_access_token_is_not_echoed(auth_app: AuthTestApp) -> None:
    response = await auth_app.client.get(
        f"{API_V1}/me", headers=auth_app.bearer("ACCESS-SENTINEL-VALUE")
    )

    assert response.status_code == 401
    assert "ACCESS-SENTINEL-VALUE" not in response.text


async def test_a_surrogate_password_is_a_clean_400(auth_app: AuthTestApp) -> None:
    """``\ud800`` escapes are legal JSON but not UTF-8: a 400, never a 500."""
    raw = b'{"email":"a@b.co","password":"\\ud800\\ud800\\ud800\\ud800\\ud800"}'

    response = await auth_app.client.post(
        f"{API_V1}/auth/login", content=raw, headers={"content-type": "application/json"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_a_surrogate_refresh_token_is_invalid_refresh(auth_app: AuthTestApp) -> None:
    raw = b'{"refresh_token":"\\ud800"}'

    response = await auth_app.client.post(
        f"{API_V1}/auth/refresh", content=raw, headers={"content-type": "application/json"}
    )

    # 400 (schema) or 401 (lookup) are both acceptable; a 500 is not.
    assert response.status_code in (400, 401)
    assert "internal" not in response.text


# --------------------------------------------------------------------------- #
# refresh: rotation and R1 reuse detection
# --------------------------------------------------------------------------- #
async def test_refresh_rotates_the_pair_once(auth_app: AuthTestApp) -> None:
    account = await seed_account(auth_app.factory)
    first = await auth_app.login_pair(account)

    response = await auth_app.client.post(
        f"{API_V1}/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )

    assert response.status_code == 200, response.text
    second = response.json()
    assert second["refresh_token"] != first["refresh_token"]
    assert second["access_token"] != first["access_token"]
    assert second["session_id"] == first["session_id"], "same family"
    assert response.headers["cache-control"] == "no-store"


async def test_refresh_reuse_revokes_the_family_and_reports_token_reused(
    auth_app: AuthTestApp,
) -> None:
    """R1: replaying a spent token kills the session, including the winner's token."""
    account = await seed_account(auth_app.factory)
    first = await auth_app.login_pair(account)

    rotated = await auth_app.client.post(
        f"{API_V1}/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert rotated.status_code == 200
    winner = rotated.json()

    replayed = await auth_app.client.post(
        f"{API_V1}/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )

    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] == "token_reused"

    # The winner's access token is dead too: the session, not just the token, is gone.
    assert (
        await auth_app.client.get(f"{API_V1}/me", headers=auth_app.bearer(winner["access_token"]))
    ).status_code == 401
    # And the newly issued refresh token is equally unusable.
    assert (
        await auth_app.client.post(
            f"{API_V1}/auth/refresh", json={"refresh_token": winner["refresh_token"]}
        )
    ).status_code == 401


async def test_refresh_is_refused_once_the_account_is_disabled(auth_app: AuthTestApp) -> None:
    """D8: disabling an account must stop *rotation* too, not only access.

    Without this check a disabled account could keep minting fresh pairs forever
    while every access token was rejected — the ban would never actually bite.
    """
    account = await seed_account(auth_app.factory)
    pair = await auth_app.login_pair(account)

    async with auth_app.factory() as session:
        await session.execute(
            text("UPDATE users SET disabled_at = now() WHERE id = :id"),
            {"id": account.user_id},
        )
        await session.commit()

    response = await auth_app.client.post(
        f"{API_V1}/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_refresh"

    # And the account is still unusable afterwards: the token was not consumed.
    assert (
        await auth_app.client.post(
            f"{API_V1}/auth/refresh", json={"refresh_token": pair["refresh_token"]}
        )
    ).status_code == 401


async def test_refresh_reuse_on_a_disabled_account_still_revokes_the_family(
    auth_app: AuthTestApp,
) -> None:
    """The disable check must not create a hole in R1 for spent tokens."""
    account = await seed_account(auth_app.factory)
    first = await auth_app.login_pair(account)
    rotated = await auth_app.client.post(
        f"{API_V1}/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )
    assert rotated.status_code == 200

    async with auth_app.factory() as session:
        await session.execute(
            text("UPDATE users SET disabled_at = now() WHERE id = :id"),
            {"id": account.user_id},
        )
        await session.commit()

    replayed = await auth_app.client.post(
        f"{API_V1}/auth/refresh", json={"refresh_token": first["refresh_token"]}
    )

    # A disabled account learns nothing extra: still a 401, never 500.
    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] in {"invalid_refresh", "token_reused"}


async def test_refresh_with_an_unknown_token_is_invalid_refresh(
    auth_app: AuthTestApp,
) -> None:
    response = await auth_app.client.post(
        f"{API_V1}/auth/refresh", json={"refresh_token": "nothing-like-a-token"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_refresh"


async def test_refresh_rejects_an_access_token(auth_app: AuthTestApp) -> None:
    """The two tables are separate: an access token is not a refresh token."""
    account = await seed_account(auth_app.factory)
    token = await auth_app.login_token(account)

    response = await auth_app.client.post(f"{API_V1}/auth/refresh", json={"refresh_token": token})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_refresh"


# --------------------------------------------------------------------------- #
# logout
# --------------------------------------------------------------------------- #
async def test_logout_is_204_and_idempotent(auth_app: AuthTestApp) -> None:
    account = await seed_account(auth_app.factory)
    token = await auth_app.login_token(account)

    first = await auth_app.client.post(f"{API_V1}/auth/logout", headers=auth_app.bearer(token))
    second = await auth_app.client.post(f"{API_V1}/auth/logout", headers=auth_app.bearer(token))

    assert first.status_code == 204
    assert second.status_code in (204, 401), "already-revoked is tolerated or refused"


async def test_logout_requires_a_token(auth_app: AuthTestApp) -> None:
    response = await auth_app.client.post(f"{API_V1}/auth/logout")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_logout_does_not_revoke_another_session(auth_app: AuthTestApp) -> None:
    """One device logging out must not sign the other device out."""
    account = await seed_account(auth_app.factory)
    first = await auth_app.login_pair(account, device_name="phone")
    second = await auth_app.login_pair(account, device_name="tablet")
    assert first["session_id"] != second["session_id"]

    assert (
        await auth_app.client.post(
            f"{API_V1}/auth/logout", headers=auth_app.bearer(first["access_token"])
        )
    ).status_code == 204

    assert (
        await auth_app.client.get(f"{API_V1}/me", headers=auth_app.bearer(first["access_token"]))
    ).status_code == 401
    assert (
        await auth_app.client.get(f"{API_V1}/me", headers=auth_app.bearer(second["access_token"]))
    ).status_code == 200


# --------------------------------------------------------------------------- #
# workspace authorisation (the contract later push/pull lanes depend on)
# --------------------------------------------------------------------------- #
async def test_workspace_access_denies_non_members_with_not_found(
    auth_app: AuthTestApp,
) -> None:
    """D20: a non-member and a missing workspace are indistinguishable → 404.

    Service-level on purpose: this is the exact call ``require_workspace_member``
    and push's per-op recheck make, and it is the seam E's HTTP cases assert
    through a real route.
    """
    account = await seed_account(auth_app.factory)
    outsider = await seed_user(auth_app.factory)
    stranger_workspace = await seed_workspace_for(auth_app.factory, outsider.user_id)
    factory = get_sessionmaker(auth_app.settings)

    # Own workspace: allowed.
    await assert_workspace_member(factory, account.user_id, account.workspace_id)

    # Someone else's workspace: not found, not forbidden.
    with pytest.raises(AuthError) as not_member:
        await assert_workspace_member(factory, account.user_id, stranger_workspace)
    assert not_member.value.code == "not_found"

    # A workspace that does not exist: the identical code.
    with pytest.raises(AuthError) as missing:
        await assert_workspace_member(factory, account.user_id, uuid.uuid4())
    assert missing.value.code == not_member.value.code == "not_found"


async def test_membership_is_what_authorises_not_owner_id(auth_app: AuthTestApp) -> None:
    """A workspace whose owner has no member row is unusable — D5, not a bug.

    The schema's ``owner_id`` is the sync kernel's ownership record; access is
    decided by ``workspace_members``. This pins that the two are not
    interchangeable, which is why registration and the 0003 backfill write both.
    """
    user = await seed_user(auth_app.factory)
    orphan = await seed_workspace_for(auth_app.factory, user.user_id, with_membership=False)

    with pytest.raises(AuthError) as error:
        await assert_workspace_member(get_sessionmaker(auth_app.settings), user.user_id, orphan)
    assert error.value.code == "not_found"


async def test_a_membership_added_later_immediately_authorises(
    auth_app: AuthTestApp,
) -> None:
    """The per-op recheck must observe a membership granted mid-session (D5)."""
    owner = await seed_account(auth_app.factory)
    collaborator = await seed_user(auth_app.factory)
    factory = get_sessionmaker(auth_app.settings)

    with pytest.raises(AuthError):
        await assert_workspace_member(factory, collaborator.user_id, owner.workspace_id)

    await seed_member(
        auth_app.factory,
        user_id=collaborator.user_id,
        workspace_id=owner.workspace_id,
    )

    # No caching: the very next call sees the new row.
    await assert_workspace_member(factory, collaborator.user_id, owner.workspace_id)
