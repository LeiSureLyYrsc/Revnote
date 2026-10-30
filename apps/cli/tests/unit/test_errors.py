"""The frozen exit-code table and the error payload."""

from __future__ import annotations

import pytest

from revnote_cli.errors import (
    EXIT_API,
    EXIT_AUTH,
    EXIT_CONFLICT,
    EXIT_NOT_FOUND,
    CliError,
    exit_code_for,
)

pytestmark = pytest.mark.cli


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("invalid_credentials", EXIT_AUTH),
        ("invalid_refresh", EXIT_AUTH),
        ("token_reused", EXIT_AUTH),
        ("unauthenticated", EXIT_AUTH),
        ("rev_conflict", EXIT_CONFLICT),
        ("conflict", EXIT_CONFLICT),
        ("entity_deleted", EXIT_CONFLICT),
        ("snapshot_too_large", EXIT_CONFLICT),
        ("not_found", EXIT_NOT_FOUND),
        ("invalid_request", EXIT_API),
        ("payload_too_large", EXIT_API),
        ("network_error", EXIT_API),
        (None, EXIT_API),
    ],
)
def test_exit_code_table_is_total(code: str | None, expected: int) -> None:
    assert exit_code_for(code) == expected


def test_error_json_carries_no_token_even_when_details_are_passed() -> None:
    error = CliError(
        "The object changed since the given revision.",
        code="rev_conflict",
        status=409,
        request_id="req-1",
        retryable=False,
    )
    payload = error.as_json()
    assert payload == {
        "code": "rev_conflict",
        "message": "The object changed since the given revision.",
        "status": 409,
        "request_id": "req-1",
        "retryable": False,
    }
    assert "token" not in str(payload).lower()
