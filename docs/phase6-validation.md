# Phase 6 — actual verification record

This file records **only runs that actually happened**, on the machine and
commit named. A section marked 「未运行」 was not run; a green CI result is
recorded only after the run exists and is linked by id.

Phase: 6 (H + C). Plan: [`docs/plan-phase6.md`](plan-phase6.md) (D1–D16 locked
to the recommended defaults).

## 0. Environment and baseline

| Item | Value |
| --- | --- |
| Host | Windows (Murasame-PC), no Docker daemon used |
| Baseline HEAD before work | `c93f994` (`docs: record the final rustfs ci evidence and the conflict-scope fix`) |
| RustFS | `D:\tools\rustfs\rustfs.exe`, SHA-256 `3396617EC60905E72FB70FD99A0B2395E425EDF495EB6499EDBDE6A5FA97D899` — matches the Phase 5 record |
| Object store | RustFS 1.0.4 only. No MinIO, no second store, no downloaded binary |
| PostgreSQL | temporary instance outside the repository (`revnote_phase2_pg.py`), databases `revnote_phase2_test` / `revnote_phase2_migrations`, stopped in a `finally` step |
| Python / tooling | `uv run` in the repo venv; git `D:\Git\cmd\git.exe`; gh CLI |
| SubAgents | none used (standing rule D14) |
| Model | `deepseek-official/deepseek-flash#max`; `gpt-6.1-sol` never used |

## 1. H — Phase 5 leftovers: re-checked, no new fix needed

The plan allowed a minimal H only if a Phase 5 leftover still blocked the
resource/bridge proof. Both recorded items were re-checked in the tree:

| Phase 5 leftover | State found | Action |
| --- | --- | --- |
| E2E runner did not pass storage env to its children | `apps/api/scripts/run_web_e2e.py` already has `ObjectStorage.child_values` (`S3_*`, `ATTACHMENTS_ENABLED`) and `export_to`/`prepare_object_storage`, which fail loudly if a promised variable is unpublished. Landed in Phase 5's `1ecdf38`/`eee818a`. | none |
| Concurrent upload could lose bytes | `AttachmentService.store_content` mints an **attempt** nonce and writes `ws/{ws}/att/{id}/{attempt_nonce}`; the loser deletes only its own key. Present in the tree and covered by `test_two_concurrent_uploads_produce_one_winner_and_one_object`. | none |

Because the attachment path was usable, the resource cases were built directly on
it; no retry, skip or weakened assertion was added.

## 2. Local gates (actual runs)

All commands ran from the repository root on the baseline above. PostgreSQL and
RustFS were the real processes described in §0; nothing was faked.

| Command | Result |
| --- | --- |
| `uv run ruff check apps/api` | **All checks passed** |
| `uv run ruff format --check apps/api` | **154 files already formatted** |
| `uv run pytest apps/api/tests/unit -q` | **1230 passed** |
| `uv run pytest apps/api/tests/integration -q -m "integration and not migrations"` | **177 passed, 43 deselected** |
| `uv run pytest apps/api/tests/integration/kernel -q -m concurrency` | **48 passed, 116 deselected** |
| `uv run pytest apps/api/tests/integration/test_migrations.py -q` | **43 passed** (includes `alembic check` = no diff and the 0005 round trip) |
| `uv run pytest apps/api/tests/api -q` (real PG + real RustFS) | **114 passed** |
| `uv run pytest apps/api/tests/api -m attachments -q` | **24 passed** (Phase 5 group unaffected) |
| `uv run pytest apps/api/tests/api -m joplin -v --junitxml=api-joplin.xml` | **17 passed, 97 deselected** |
| `uv run python apps/api/scripts/check_test_reports.py --group joplin` | `api/joplin: passed=17 required=17; zero skips/failures/errors` |

Notes on what the 17 bridge cases prove (one case per claim of plan §3.2):

| Claim | Case |
| --- | --- |
| Email/password session; every misuse is `unauthenticated`; logout revokes | `test_session_login_is_email_password_and_misuse_is_unauthenticated` |
| Bridge write → native pull/snapshot sees an equivalent note | `test_a_note_written_through_the_bridge_is_readable_natively` |
| Folder/note parent mapping both ways | `test_a_folder_and_its_note_keep_the_parent_mapping` |
| Bridge delete is an ordinary kernel tombstone | `test_deleting_through_the_bridge_is_a_kernel_tombstone` |
| Native write → delta + item read, deterministic key | `test_a_native_note_appears_in_the_bridge_delta_and_by_name` |
| Repeated PUT updates, identical PUT is a no-op | `test_an_item_can_be_updated_repeatedly_through_the_bridge` |
| Delta compression + cursor chain | `test_delta_chains_without_gaps_and_compresses_a_create_then_updates` |
| Bad cursor → `resyncRequired` | `test_an_invalid_or_ahead_cursor_requires_a_resync` |
| create+delete is compressed away but still advances the cursor | `test_a_create_then_delete_pair_is_absent_from_the_delta` |
| SYNC coexists, EXCLUSIVE excludes, lock traffic never moves the cursor | `test_sync_locks_coexist_and_an_exclusive_lock_excludes_everyone` |
| Cross-account isolation (404, empty delta) | `test_another_account_cannot_address_this_workspace` |
| Batch put/delete per item | `test_batch_put_and_batch_delete_are_per_item` |
| Item metadata carries the times the client compares | `test_item_metadata_carries_the_times_the_client_compares` |
| Resource → `ready` attachment → byte-identical download, real RustFS | `test_a_resource_round_trips_into_the_attachment_state_machine` |
| Unsupported resource MIME → 422 (permanently rejected) | `test_an_unsupported_resource_type_is_permanently_rejected` |
| Resource delete is the documented no-op | `test_a_resource_delete_is_accepted_and_the_bytes_remain` |
| Bridge refuses resources when attachments are disabled | `test_a_resource_upload_is_refused_when_the_deployment_has_no_storage` |

## 3. What was **not** run locally

* **Web suite** (`pnpm lint/typecheck/test/build`, Playwright E2E): this phase
  changed no Web file. It is verified by the same-SHA CI `web` and `web-e2e`
  jobs, not by a local run, and this file says so rather than implying one.
* **Full Joplin GUI matrix**: per D12 it is not a CI gate and was not run. The
  protocol contract is covered by the 17 HTTP cases against the real server.
* **Docker image build**: no Docker on this host (forbidden locally); CI's
  `image` job remains the evidence.
* **Public deployment hardening**: out of scope (TLS, real rate limiting,
  backups).

## 4. CI

The workflow is unchanged in shape: the same five jobs. `api-attachments` now
also runs `-m joplin` and produces `api-joplin.xml`, guarded by
`check_test_reports.py --group joplin` (17 required case names).

Rounds 1 and 2 below are the **same SHA**, run back to back, on the
implementation commit before this evidence file was added.

| Round | SHA | Run | api | api-attachments (incl. `-m joplin`) | web | web-e2e | image |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `eb743f5` | [41024247226](https://github.com/LeiSureLyYrsc/Revnote/actions/runs/41024247226) | success | success | success | success | success |
| 2 (same SHA) | `eb743f5` | same run, attempt 2 | success | success | success | success | success |

Both rounds: `conclusion=success`, five jobs `success`, no skipped acceptance
case. The `api-attachments` job therefore produced both the 24-case Phase 5
report and the 17-case Joplin report on a real RustFS.

This evidence commit changes the tip; per the build rule the tip was then run
twice more on its own SHA. Those two runs (same run id, attempts 1 and 2) are
recorded with their links in
`.slim/deepwork/phase6-build/progress.md` (local, git-ignored), because a file
cannot contain the SHA of the commit that adds it.
