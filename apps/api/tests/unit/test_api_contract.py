"""Wire-contract tests: the error envelope, its mapping and its fixed vocabulary.

Everything here is pure: the envelope models, the code table and the kernel->code
mapping are all importable without a server, and every mapping question can be
answered without one. The HTTP behaviour of the handlers is covered in
``test_app_contract.py``.

The point of these tests is the part that is easy to lose in review: that an
error response can never carry leaked content, an invented code, or a status that
disagrees with the code table.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, ValidationError

from revnote_api.api import errors as api_errors
from revnote_api.api.schemas.common import (
    BIGINT_MAX,
    CURSOR_ZERO,
    DecimalString,
    ErrorBody,
    ErrorEnvelope,
    render_decimal,
    render_utc,
)
from revnote_api.kernel import errors as kernel_errors

# --------------------------------------------------------------------------- #
# decimal-string counters
# --------------------------------------------------------------------------- #


class _Holder(BaseModel):
    """Minimal model exposing one ``DecimalString`` field, for validator tests."""

    model_config = ConfigDict(extra="forbid")

    seq: DecimalString


def _parse(value: object) -> str:
    return _Holder(seq=value).seq  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["0", "1", "42", "9223372036854775807"])
def test_canonical_decimals_are_accepted(value: str) -> None:
    assert _parse(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "00",  # leading zero
        "007",
        "-1",  # sign
        "+1",
        "1e3",  # exponent
        "1.0",  # fraction
        " 1",  # whitespace
        "1 ",
        "",  # empty
        "9223372036854775808",  # BIGINT_MAX + 1
        "١٢",  # non-ASCII digits
    ],
)
def test_non_canonical_decimals_are_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        _parse(value)


@pytest.mark.parametrize("value", [0, 1, 2**63 - 1, True, None])
def test_a_json_number_is_never_coerced_into_a_counter(value: object) -> None:
    """``"42"`` is the contract; ``42`` is a client bug, not a synonym."""
    with pytest.raises(ValidationError):
        _parse(value)


def test_cursor_zero_is_the_legal_floor() -> None:
    assert CURSOR_ZERO == "0"
    assert _parse(CURSOR_ZERO) == "0"


def test_render_decimal_is_plain_and_never_padded() -> None:
    assert render_decimal(0) == "0"
    assert render_decimal(7) == "7"
    assert render_decimal(BIGINT_MAX) == "9223372036854775807"


# --------------------------------------------------------------------------- #
# timestamps
# --------------------------------------------------------------------------- #


def test_utc_timestamp_renders_with_a_z_suffix() -> None:
    stamp = datetime.datetime(2026, 10, 29, 3, 16, 0, tzinfo=datetime.UTC)

    assert render_utc(stamp) == "2026-10-29T03:16:00Z"


def test_utc_timestamp_keeps_microseconds_when_present() -> None:
    stamp = datetime.datetime(2026, 10, 29, 3, 16, 0, 123456, tzinfo=datetime.UTC)

    assert render_utc(stamp) == "2026-10-29T03:16:00.123456Z"


def test_a_non_utc_aware_timestamp_is_converted_not_relabelled() -> None:
    offset = datetime.timezone(datetime.timedelta(hours=8))
    stamp = datetime.datetime(2026, 10, 29, 11, 16, 0, tzinfo=offset)

    assert render_utc(stamp) == "2026-10-29T03:16:00Z"


def test_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        render_utc(datetime.datetime(2026, 10, 29, 3, 16, 0))


# --------------------------------------------------------------------------- #
# the error envelope
# --------------------------------------------------------------------------- #


def test_envelope_has_exactly_the_five_documented_fields() -> None:
    assert set(ErrorBody.model_fields) == {
        "code",
        "message",
        "request_id",
        "retryable",
        "details",
    }
    assert set(ErrorEnvelope.model_fields) == {"error"}


def test_envelope_details_default_to_an_empty_object() -> None:
    body = ErrorBody(code="not_found", message="x", request_id="req-1", retryable=False)

    assert body.details == {}
    assert body.retryable is False


def test_envelope_requires_retryable_explicitly() -> None:
    """The flag is never defaulted: a builder must state whether a retry is safe."""
    with pytest.raises(ValidationError):
        ErrorBody(code="not_found", message="x", request_id="req-1")  # type: ignore[call-arg]


def test_envelope_round_trips_as_json_with_the_fixed_shape() -> None:
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code="conflict",
            message="The request conflicts with recorded state.",
            request_id="req-abc",
            retryable=False,
            details={"expected_rev": "2"},
        )
    )

    assert envelope.model_dump(mode="json") == {
        "error": {
            "code": "conflict",
            "message": "The request conflicts with recorded state.",
            "request_id": "req-abc",
            "retryable": False,
            "details": {"expected_rev": "2"},
        }
    }


@pytest.mark.parametrize("request_id", ["", "has space", "emoji-🙂", "a" * 65, "semi;colon"])
def test_envelope_refuses_an_illegal_request_id(request_id: str) -> None:
    with pytest.raises(ValidationError):
        ErrorBody(code="not_found", message="x", request_id=request_id)


def test_envelope_refuses_an_unknown_field() -> None:
    """The shape is closed: a stray key cannot ride along on an error body."""
    with pytest.raises(ValidationError):
        ErrorBody(
            code="not_found",
            message="x",
            request_id="req-1",
            extra_field="nope",  # type: ignore[call-arg]
        )


def test_envelope_refuses_a_non_snake_case_code() -> None:
    with pytest.raises(ValidationError):
        ErrorBody(code="NotFound", message="x", request_id="req-1")


def test_error_envelope_repr_is_safe_to_log() -> None:
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code="internal",
            message="An unexpected error occurred.",
            request_id="r-1",
            retryable=False,
        )
    )

    assert "An unexpected error occurred." in repr(envelope)
    assert "password" not in repr(envelope)


# --------------------------------------------------------------------------- #
# the code table
# --------------------------------------------------------------------------- #


def test_every_code_maps_to_one_status_and_one_message() -> None:
    specs = api_errors.ERROR_SPECS

    assert specs, "the vocabulary must not be empty"
    for code, spec in specs.items():
        assert spec.code == code, "the table key must match the spec's own code"
        assert isinstance(spec.status, int) and 400 <= spec.status <= 599
        assert spec.message and spec.message.endswith(".")
        # No interpolation: a fixed sentence cannot leak an id or a body.
        assert "{" not in spec.message
        assert "%" not in spec.message


def test_the_documented_vocabulary_is_present_exactly() -> None:
    """Plan §10's closed list - adding a code must be a deliberate edit here."""
    assert set(api_errors.ERROR_SPECS) == {
        "invalid_request",
        "unauthenticated",
        "invalid_credentials",
        "registration_closed",
        "token_reused",
        "invalid_refresh",
        "not_found",
        "conflict",
        "rev_conflict",
        "entity_deleted",
        "folder_not_empty",
        "invalid_parent",
        "folder_cycle",
        "cursor_expired",
        "cursor_ahead",
        "payload_too_large",
        "object_too_large",
        "snapshot_too_large",
        "write_busy",
        "outcome_unknown",
        "auth_temporarily_unavailable",
        "storage_failure",
        "counter_overflow",
        "rate_limited",
        "internal",
        # -- Phase 5 attachments (additive: new codes only, none changed) ----- #
        "attachment_storage_unavailable",
        "attachment_already_ready",
        "attachment_hash_mismatch",
        "attachment_expired",
        "attachment_too_large",
        "attachment_unsupported_type",
    }


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("invalid_request", 400),
        ("cursor_ahead", 400),
        ("payload_too_large", 413),
        ("unauthenticated", 401),
        ("invalid_credentials", 401),
        ("token_reused", 401),
        ("invalid_refresh", 401),
        ("registration_closed", 403),
        ("not_found", 404),
        ("conflict", 409),
        ("rev_conflict", 409),
        ("entity_deleted", 409),
        ("folder_not_empty", 409),
        ("invalid_parent", 409),
        ("folder_cycle", 409),
        ("object_too_large", 409),
        ("snapshot_too_large", 409),
        ("counter_overflow", 409),
        ("cursor_expired", 410),
        ("rate_limited", 429),
        ("write_busy", 503),
        ("outcome_unknown", 503),
        ("auth_temporarily_unavailable", 503),
        ("storage_failure", 500),
        ("internal", 500),
        # -- Phase 5 attachments (additive) ------------------------------------ #
        ("attachment_unsupported_type", 400),
        ("attachment_already_ready", 409),
        ("attachment_hash_mismatch", 409),
        ("attachment_expired", 409),
        ("attachment_too_large", 413),
        ("attachment_storage_unavailable", 503),
    ],
)
def test_status_codes_match_the_protocol_table(code: str, status: int) -> None:
    assert api_errors.CODE_STATUS[code] == status


def test_only_transient_codes_are_retryable() -> None:
    retryable = {code for code, spec in api_errors.ERROR_SPECS.items() if spec.retryable}

    assert retryable == {
        "rate_limited",
        "write_busy",
        "outcome_unknown",
        "auth_temporarily_unavailable",
        "storage_failure",
        # Phase 5: the store was unreachable and nothing was committed, so
        # re-sending the identical upload is safe.
        "attachment_storage_unavailable",
    }


def test_an_unknown_code_degrades_to_internal_instead_of_raising() -> None:
    assert api_errors.spec_for("no_such_code").code == "internal"


def test_stop_codes_are_the_ones_that_end_a_push_batch() -> None:
    assert {code for code in api_errors.ERROR_SPECS if api_errors.is_stop_code(code)} == {
        "write_busy",
        "outcome_unknown",
        "storage_failure",
        "internal",
    }


# --------------------------------------------------------------------------- #
# kernel -> code mapping
# --------------------------------------------------------------------------- #


def test_every_kernel_error_has_a_wire_code() -> None:
    kernel_classes = {
        value
        for value in vars(kernel_errors).values()
        if isinstance(value, type)
        and issubclass(value, kernel_errors.SyncKernelError)
        and value is not kernel_errors.SyncKernelError
    }

    assert kernel_classes == set(api_errors.KERNEL_ERROR_CODES), (
        "a new kernel error without a mapping would silently become 500 internal"
    )
    for mapped in api_errors.KERNEL_ERROR_CODES.values():
        assert mapped in api_errors.ERROR_SPECS


def test_the_two_id_conflicts_share_one_code_and_one_message() -> None:
    """P2 #3: the response must not disclose *where* an id already exists."""
    workspace = uuid.uuid4()
    entity = uuid.uuid4()

    same_ws = kernel_errors.EntityIdConflict("note", entity)
    other_ws = kernel_errors.OpIdConflict(workspace, uuid.uuid4())

    first = api_errors.kernel_error_response(same_ws, "req-1")
    second = api_errors.kernel_error_response(other_ws, "req-1")

    assert first.status_code == second.status_code == 409
    assert first.body == second.body


def test_kernel_messages_never_reach_the_response_body() -> None:
    """A kernel message can name ids; the envelope carries the fixed sentence."""
    secret_id = uuid.uuid4()
    exc = kernel_errors.EntityNotFound("note", secret_id)

    response = api_errors.kernel_error_response(exc, "req-1")
    body = response.body.decode("utf-8")

    assert str(secret_id) not in body
    assert body.count("The requested resource does not exist.") == 1


def test_a_rev_conflict_reports_both_revisions_as_decimal_strings() -> None:
    exc = kernel_errors.RevConflict("note", uuid.uuid4(), expected=2, current=7)

    response = api_errors.kernel_error_response(exc, "req-1")
    payload = __import__("json").loads(response.body)

    assert response.status_code == 409
    assert payload["error"]["code"] == "rev_conflict"
    assert payload["error"]["details"] == {"expected_rev": "2", "current_rev": "7"}


def test_a_counter_overflow_names_the_counter_but_not_the_value() -> None:
    exc = kernel_errors.CounterOverflow("seq")

    payload = __import__("json").loads(api_errors.kernel_error_response(exc, "req-1").body)

    assert payload["error"]["code"] == "counter_overflow"
    assert payload["error"]["details"] == {"counter": "seq"}


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (kernel_errors.WriteBusy("55P03"), "write_busy"),
        (kernel_errors.CommitOutcomeUnknown(uuid.uuid4()), "outcome_unknown"),
    ],
)
def test_transient_kernel_errors_tell_the_client_to_resend_the_same_op(
    exc: kernel_errors.SyncKernelError, code: str
) -> None:
    response = api_errors.kernel_error_response(exc, "req-1")
    payload = __import__("json").loads(response.body)

    assert payload["error"]["code"] == code
    assert payload["error"]["retryable"] is True
    assert payload["error"]["details"] == {"retry_same_op": True}
    # A 503 that says "retry" must also say when.
    assert response.headers["retry-after"] == "1"


def test_a_401_carries_the_bearer_challenge() -> None:
    response = api_errors.error_response("unauthenticated", "req-1")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_only_a_503_gets_a_retry_after_header() -> None:
    """A retry hint on a 4xx or a 500 would be untrue; only transients get one."""
    transient = api_errors.error_response("write_busy", "req-1")
    permanent = api_errors.error_response("conflict", "req-1")
    storage = api_errors.error_response("storage_failure", "req-1")

    assert transient.headers["retry-after"] == "1"
    assert "retry-after" not in permanent.headers
    assert "retry-after" not in storage.headers


def test_every_error_response_echoes_the_request_id_header() -> None:
    response = api_errors.error_response("internal", "req-abc")

    assert response.headers["x-request-id"] == "req-abc"
    assert __import__("json").loads(response.body)["error"]["request_id"] == "req-abc"


# --------------------------------------------------------------------------- #
# the deliberate-rejection exception
# --------------------------------------------------------------------------- #


def test_api_error_coerces_an_unknown_code_to_internal() -> None:
    error = api_errors.ApiError("totally_invented")

    assert error.code == "internal"
    assert error.details == {}


def test_api_error_keeps_machine_readable_details() -> None:
    error = api_errors.ApiError("payload_too_large", details={"limit_bytes": 2048})

    assert error.code == "payload_too_large"
    assert error.details == {"limit_bytes": 2048}


def test_api_error_repr_carries_no_message_or_input() -> None:
    error = api_errors.ApiError("invalid_request")

    assert repr(error) == "ApiError(code='invalid_request')"
    assert str(error) == "invalid_request"


# --------------------------------------------------------------------------- #
# validation details: a client-chosen key is never echoed
# --------------------------------------------------------------------------- #

#: Values a client might send *as a JSON key*. Pydantic quotes such a key verbatim
#: in ``loc`` for ``extra_forbidden``, so a naive ``loc`` pass-through is a leak.
SECRET_KEYS = [
    "hunter2-my-password",
    "rva_ACCESS_TOKEN_0123456789",
    "refresh-token-sentinel",
    "BODY_MD_SENTINEL_9182",
    'quoted "body" text',
    "semi;colon\nnewline",
    "🙂 emoji key",
    "x" * 200,
]


class _StrictInner(BaseModel):
    """Nested model with ``extra="forbid"``, like a push op payload."""

    model_config = ConfigDict(extra="forbid")

    title: str


class _StrictProbe(BaseModel):
    """Top-level model with ``extra="forbid"``, like a request body."""

    model_config = ConfigDict(extra="forbid")

    email: str
    nested: _StrictInner


def test_an_unknown_json_key_is_replaced_by_a_constant_placeholder() -> None:
    """The *reporting* path must never reflect a key the client invented."""
    exc = RequestValidationError(
        [
            {
                "type": "extra_forbidden",
                "loc": ("body", "hunter2-my-password"),
                "msg": "Extra inputs are not permitted",
                "input": "hunter2-my-password",
            }
        ]
    )

    fields = api_errors._validation_fields(exc)

    assert fields == [{"path": ["body", api_errors.REDACTED_SEGMENT], "reason": "extra_forbidden"}]
    assert "hunter2-my-password" not in str(fields)


@pytest.mark.parametrize("secret_key", SECRET_KEYS)
def test_no_secret_used_as_a_json_key_can_be_read_back(secret_key: str) -> None:
    """Password/token/body sentinels used as keys must vanish, not truncate."""
    exc = RequestValidationError(
        [{"type": "extra_forbidden", "loc": ("body", secret_key), "input": secret_key}]
    )

    fields = api_errors._validation_fields(exc)

    assert fields[0]["path"] == ["body", api_errors.REDACTED_SEGMENT]
    rendered = str(fields)
    assert secret_key not in rendered
    # The placeholder must not be a prefix/derivative of the original either.
    assert secret_key[:4] not in rendered or secret_key[:4] == "body"


def test_declared_field_names_need_a_matched_route_to_be_reported() -> None:
    """Without route context the sanitiser fails closed: everything is redacted.

    ``_validation_fields(exc)`` has no ``request``, so it cannot know which names
    are declared - and an unknown string in ``loc`` might be a client key. Refusing
    to echo *any* string in that situation is the safe default; the app-level test
    proves declared paths survive when the real route is available.
    """
    exc = RequestValidationError(
        [
            {
                "type": "string_type",
                "loc": ("body", "nested", "title"),
                "msg": "Input should be a valid string",
                "input": 1,
            }
        ]
    )

    fields = api_errors._validation_fields(exc)

    # The framework marker survives; the unverified names do not.
    assert fields[0]["path"] == ["body", api_errors.REDACTED_SEGMENT, api_errors.REDACTED_SEGMENT]
    assert fields[0]["reason"] == "string_type"


def test_only_the_five_fastapi_markers_pass_in_first_position() -> None:
    """``loc[0]`` is framework vocabulary; anything else there is client data."""
    markers = api_errors.LOCATION_MARKERS

    assert markers == {"body", "query", "path", "header", "cookie"}
    exc = RequestValidationError(
        [{"type": "extra_forbidden", "loc": ("payload", "email"), "input": 1}]
    )
    path = api_errors._validation_fields(exc)[0]["path"]
    # ``payload`` is neither a marker nor a declared field, and ``email`` is
    # unverifiable without route context: both are placeholders, no echo.
    assert path == [api_errors.REDACTED_SEGMENT, api_errors.REDACTED_SEGMENT]
    assert "payload" not in path
    assert "email" not in path


def test_a_deeply_nested_loc_is_bounded() -> None:
    """An attacker-chosen nest must not become an arbitrarily deep path."""
    exc = RequestValidationError(
        [
            {
                "type": "extra_forbidden",
                "loc": ("body", "a", "b", "c", "d", "e", "f", "g", "h", "i", "j"),
                "input": 1,
            }
        ]
    )

    path = api_errors._validation_fields(exc)[0]["path"]

    assert len(path) == api_errors.MAX_PATH_SEGMENTS


def test_the_number_of_reported_fields_is_bounded() -> None:
    exc = RequestValidationError(
        [
            {"type": "string_type", "loc": ("body", f"f{index}"), "input": index}
            for index in range(500)
        ]
    )

    fields = api_errors._validation_fields(exc)

    assert len(fields) == api_errors.MAX_REPORTED_FIELDS
    assert len(fields) < 500


def test_a_non_string_non_int_loc_segment_is_redacted() -> None:
    """Dict keys of a ``dict``-typed body are client data; no representation."""
    exc = RequestValidationError(
        [
            {
                "type": "int_parsing",
                "loc": ("body", (1, "tuplekey")),
                "input": "not-an-int",
            }
        ]
    )

    assert api_errors._validation_fields(exc)[0]["path"] == [
        "body",
        api_errors.REDACTED_SEGMENT,
    ]


def test_a_reason_is_bounded_even_if_pydantic_reports_something_long() -> None:
    exc = RequestValidationError([{"type": "x" * 500, "loc": ("body", "email"), "input": "v"}])

    reason = api_errors._validation_fields(exc)[0]["reason"]

    assert len(reason) == api_errors.MAX_REASON_CHARS


def test_validation_details_never_include_input_or_ctx() -> None:
    """The two fields that quote the rejected value are structurally absent."""
    exc = RequestValidationError(
        [
            {
                "type": "string_type",
                "loc": ("body", "email"),
                "msg": "Input should be a valid string",
                "input": {"password": "hunter2"},
                "ctx": {"error": ValueError("hunter2")},
            }
        ]
    )

    rendered = str(api_errors._validation_fields(exc))

    assert "input" not in rendered
    assert "ctx" not in rendered
    assert "hunter2" not in rendered
    assert set(api_errors._validation_fields(exc)[0]) == {"path", "reason"}
