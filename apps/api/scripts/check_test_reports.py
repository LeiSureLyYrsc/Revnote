"""Guard the exact ten API report groups without printing failure bodies.

Eight are produced by the `api` job (the Phase 3 groups plus the Phase 8 reference
CLI), one is the Phase 5 real-PostgreSQL-plus-real-store group, and one is the
Phase 6 Joplin bridge group (also real PostgreSQL, plus real storage for its
resource cases). By default every one of the ten is required. Repeat --group for
focused verification; that does not stand in for the default full group gate.
--full-report optionally checks an additional aggregate report, never instead of
the ten group reports.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

# Exact extraction of the CI acceptance inventory. Do not replace these names
# with total counts: a larger unrelated group cannot prove a case ran.
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
        "test_snapshot_pagination_bootstraps_a_workspace_larger_than_one_page",
        "test_snapshot_pages_exclude_writes_after_the_first_page",
        "test_snapshot_pagination_cursor_verdicts_and_page_limits",
        "test_snapshot_pagination_refuses_the_total_cap_and_a_single_oversize_page",
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
    # Phase 8: the reference CLI. One case per load-bearing claim: session
    # storage, a complete paged bootstrap, the incremental cursor, push round
    # trips, an honest conflict exit, capability discovery, and the unauthenticated
    # refusal. The unit cases in the same report are extra evidence, not a
    # substitute for any name here.
    "api-cli.xml": {
        "test_cli_login_me_tree_logout_keep_tokens_in_the_config_dir",
        "test_cli_paged_snapshot_collects_every_object_and_pins_the_watermark",
        "test_cli_pull_returns_the_increment_after_the_snapshot",
        "test_cli_push_note_create_and_update_round_trip",
        "test_cli_push_conflict_exits_four_and_leaves_the_note_untouched",
        "test_cli_capabilities_are_available_without_a_session",
        "test_cli_me_without_a_session_exits_three",
    },
    # Phase 5: real PostgreSQL + real object storage, one case per load-bearing
    # claim of plan §7.1. A fake store cannot satisfy these: the digest is
    # recomputed by re-reading the object, the single winner is decided by a row
    # lock plus a CAS predicate, and the GC case lists the real bucket.
    "api-attachments.xml": {
        # §2 the whole happy path, bytes verified byte-identical.
        "test_upload_then_download_is_byte_identical",
        # D6: a re-read digest, not an ETag or a client claim.
        "test_the_declared_digest_is_verified_against_the_stored_bytes",
        "test_a_wrong_declared_digest_is_refused_and_leaves_no_ready_row",
        # D5: the object is immutable once ready.
        "test_a_second_upload_to_a_ready_attachment_is_refused_and_changes_nothing",
        # D5: one winner, one object, for two concurrent PUTs.
        "test_two_concurrent_uploads_produce_one_winner_and_one_object",
        # A lost response plus a client retry is still one ready row.
        "test_replaying_the_same_upload_after_a_commit_is_still_one_row",
        # D20: metadata, content and upload all collapse to 404 for a non-member.
        "test_a_non_member_cannot_read_metadata_content_or_upload",
        # 413 and 400 on the two refusal planes.
        "test_a_declared_size_over_the_cap_is_attachment_too_large",
        "test_an_svg_upload_is_refused",
        # D3: an attachment never touches the sync kernel.
        "test_an_attachment_never_moves_the_sync_cursor_or_the_event_log",
        # D8: the GC collects expired pending rows and leaves ready ones.
        "test_gc_removes_expired_pending_rows_and_leaves_ready_ones",
        # interface §4: the immutable four-segment storage key.
        "test_the_object_lives_under_the_contract_key",
    },
    # Phase 6: the Joplin compatibility bridge, real PostgreSQL (the resource
    # cases also use the real store in the api-attachments job). One case per
    # claim of plan §3.2; a group that loses a case must fail here, not shrink.
    "api-joplin.xml": {
        # session + the 401 collapse
        "test_session_login_is_email_password_and_misuse_is_unauthenticated",
        # bridge -> native, with the mapping and the kernel command
        "test_a_note_written_through_the_bridge_is_readable_natively",
        "test_a_folder_and_its_note_keep_the_parent_mapping",
        "test_deleting_through_the_bridge_is_a_kernel_tombstone",
        # native -> bridge, deterministic key + serialization
        "test_a_native_note_appears_in_the_bridge_delta_and_by_name",
        "test_an_item_can_be_updated_repeatedly_through_the_bridge",
        # delta compression, cursors and resync
        "test_delta_chains_without_gaps_and_compresses_a_create_then_updates",
        "test_an_invalid_or_ahead_cursor_requires_a_resync",
        "test_a_create_then_delete_pair_is_absent_from_the_delta",
        # locks
        "test_sync_locks_coexist_and_an_exclusive_lock_excludes_everyone",
        # authorisation and batch
        "test_another_account_cannot_address_this_workspace",
        "test_batch_put_and_batch_delete_are_per_item",
        "test_item_metadata_carries_the_times_the_client_compares",
        # resources on the Phase 5 state machine + real store
        "test_a_resource_round_trips_into_the_attachment_state_machine",
        "test_an_unsupported_resource_type_is_permanently_rejected",
        "test_a_resource_delete_is_accepted_and_the_bytes_remain",
        "test_a_resource_upload_is_refused_when_the_deployment_has_no_storage",
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
        print("Focused group evidence only; the default ten-group gate is still required.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
