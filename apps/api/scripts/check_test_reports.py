"""Guard the exact seven Phase 3 API report groups without printing failure bodies.

By default every CI group is required. Repeat --group for focused verification;
that does not stand in for the default full group gate. --full-report optionally
checks an additional aggregate report, never instead of the seven group reports.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Exact extraction of the Phase 3 ci.yml acceptance inventory. Do not replace
# these names with total counts: a larger unrelated group cannot prove a case ran.
REQUIRED: dict[str, set[str]] = {
    "api-unit.xml": set(),
    "api-integration.xml": {
        "test_create_folder_writes_rev1_and_one_event",
        "test_note_can_share_a_uuid_with_the_folder_it_lives_in",
        "test_statement_timeout_real_57014",
    },
    "api-concurrency.xml": {
        "test_eight_producers_allocate_a_dense_sequence",
        "test_same_op_from_many_connections_writes_once",
        "test_cas_race_has_exactly_one_winner",
        "test_ledger_backstop_returning_false_replays_the_recorded_result",
        "test_three_connection_visibility_then_commit",
        "test_three_connection_visibility_then_rollback_reuses_the_seq",
        "test_lost_commit_ack_is_durable_and_retry_replays",
        "test_lock_timeout_metrics",
        "test_cancel_before_commit_rolls_back_and_propagates",
        "test_cancel_during_commit_is_cancelled_and_discards_the_connection",
    },
    "api-migrations.xml": {
        "test_full_round_trip_and_reapply",
        "test_alembic_check_reports_no_diff",
    },
    "api-http.xml": {
        "test_refresh_rotates_the_pair_once",
        "test_workspace_access_denies_non_members_with_not_found",
        "test_http_push_six_statuses_are_wire_contract",
        "test_http_push_per_op_failure_is_an_error_object_not_a_code",
        "test_http_push_per_op_membership_loss_is_not_found",
        "test_http_push_duplicate_op_id_executes_nothing",
        "test_http_push_caps_reject_the_whole_batch",
        "test_http_push_lock_timeout_stops_the_batch",
        "test_http_push_unknown_is_retried_by_the_same_op_id",
        "test_http_push_prebuild_catches_what_the_schema_cannot",
        "test_http_push_structural_batch_errors_reject_everything",
        "test_http_push_unhashable_discriminator_is_a_400",
        "test_http_push_repr_never_leaks_note_content",
        "test_id_conflict_origin_hidden",
        "test_secrets_never_echo",
        "test_pull_since_is_exclusive_and_cursor_chains",
        "test_pull_cursor_bounds_and_floor",
        "test_pull_objects_are_deduplicated_and_lead_their_events",
        "test_snapshot_has_no_partial_success",
        "test_snapshot_bootstraps_from_one_watermark",
        "test_tree_and_note_reads_hide_content_and_deletions",
        "test_a_legacy_oversize_body_is_refused_by_both_reads",
    },
    "api-http-concurrency.xml": {
        "test_http_push_dense_seq",
        "test_http_pull_during_push",
        "test_pull_cannot_observe_n_plus_one_first",
        "test_snapshot_is_one_rr_view",
        "test_concurrent_refresh_same_token_has_one_winner",
        "test_concurrent_registration_of_one_address_creates_one_account",
    },
    "api-transport.xml": {
        "test_socket_disconnect_stops_batch",
        "test_commit_survives_a_lost_response",
        "test_the_probe_serves_http_on_a_real_socket",
    },
}
GROUPS = {filename.removeprefix("api-").removesuffix(".xml"): filename for filename in REQUIRED}


class ReportError(RuntimeError):
    """An absent or unsuccessful report; diagnostics contain counts only."""


def check_report(path: Path, required: set[str]) -> tuple[int, int]:
    """Parse real elements; reject suite-level failures and inconsistent counters too."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        raise ReportError("Report is missing or malformed.") from None
    if root.tag not in {"testsuites", "testsuite"}:
        raise ReportError("Report has no JUnit suite root.")
    cases = list(root.iter("testcase"))
    missing = required - {case.attrib.get("name", "") for case in cases}
    rejected = sum(1 for item in root.iter() if item.tag in {"skipped", "failure", "error"})
    for suite in root.iter():
        if suite.tag not in {"testsuite", "testsuites"}:
            continue
        for name in ("skipped", "failures", "errors", "disabled"):
            raw = suite.attrib.get(name, "0")
            if not raw.isdecimal() or int(raw) != 0:
                rejected += 1
        count = suite.attrib.get("tests")
        if count is not None and (
            not count.isdecimal() or int(count) != len(list(suite.iter("testcase")))
        ):
            rejected += 1
    if not cases or rejected or missing:
        raise ReportError(
            f"Acceptance incomplete: cases={len(cases)} rejected={rejected} missing={len(missing)}."
        )
    return len(cases), len(required)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, default=REPO_ROOT)
    parser.add_argument("--group", action="append", choices=tuple(GROUPS))
    parser.add_argument("--full-report", type=Path, help="Additional full pytest JUnit report")
    args = parser.parse_args(argv)
    selected = tuple(dict.fromkeys(args.group or GROUPS))
    failed = False
    for group in selected:
        try:
            count, names = check_report(args.report_dir / GROUPS[group], REQUIRED[GROUPS[group]])
            print(f"api/{group}: passed={count} required={names}; zero skips/failures/errors")
        except ReportError as exc:
            print(f"api/{group}: {exc}", file=sys.stderr)
            failed = True
    if args.full_report:
        try:
            required = set().union(*REQUIRED.values())
            count, names = check_report(args.full_report, required)
            print(f"api/full: passed={count} required={names}; zero skips/failures/errors")
        except ReportError as exc:
            print(f"api/full: {exc}", file=sys.stderr)
            failed = True
    if args.group:
        print("Focused group evidence only; the default seven-group gate is still required.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
