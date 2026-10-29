# Phase 4 browser acceptance inventory

There are intentionally no placeholder, skipped or empty browser tests. The
required labels are frozen in `scripts/check-test-reports.ts` (`E2E_LABELS`). A
full report must contain all labels; adding a title alone is not acceptance.

| Label | Real browser / API / PostgreSQL responsibility |
| --- | --- |
| `workspace_flow` | Proxy login, me, snapshot and all eight operation kinds |
| `draft_reload` | Local reload and a fresh context reading server content |
| `api_restart_recovery` | API stopped, Vite reachable, edit/reload, restart and drain |
| `offline_recovery` | Loaded page offline edit and online queue drain |
| `lost_response_replay` | Fetch real committed response before dropping it; reload; same command; one event/ledger |
| `ack_preserves_later_edit` | Delay A acknowledgement, edit B, acknowledge A without discarding B |
| `manual_conflict` | Independent clients, CAS conflict, retained versions and explicit resolution |
| `remote_delete_recovery` | Remote delete with local draft; recovery and new-ID save only |
| `single_writer_takeover` | Same-origin tabs, read-only restrictions, crash takeover and late results |
| `refresh_rotation` | Remembered refresh restores one session without concurrent reuse |
| `refresh_uncertainty` | Real refresh processed, response dropped, explicit reauthentication |
| `session_identity_isolation` | Another account cannot view or transmit the first account's queue |
| `resync_preservation` | Snapshot limits, 410 and abnormal cursor retain local state |
| `cursor_integrity` | Leading objects, replay and page transaction failure cannot skip events |
| `markdown_preservation` | Extended syntax, CRLF, trailing whitespace, HTML/LaTeX/unknown blocks unchanged |
| `mobile_layout` | 390px panels, keyboard operation, read-only state, no runtime errors |

Run through `apps/api/scripts/run_web_e2e.py`; never attach to an existing API or
Vite process. The PostgreSQL controller owns the external one-time database.
The runner owns API, Vite and browser process groups, and always cleans up.
Windows additionally uses a kill-on-close kernel Job Object, retaining ownership
of browser descendants even if pnpm has already exited. The explicit DSN must use
`postgresql+asyncpg`, a loopback host and a fresh empty database named
`revnote_web_e2e` or `revnote_web_e2e_<id>`, distinct from both Python test targets.
Reusing a database containing a prior E2E run is rejected: create another one-time
database externally. Applicable root `.env` files are rejected without reading
them; API/Alembic explicitly disable implicit env-file settings.

Manifest wiring owned by the orchestrator:

- Web `test`: `tsx e2e/run-unit-tests.ts`
- Root `test:web`: `pnpm --filter @revnote/web test`
- Root `test:e2e`: `uv run python apps/api/scripts/run_web_e2e.py`
- Web `check:reports`: `tsx scripts/check-test-reports.ts`
- Root `check:web-reports`: `pnpm --filter @revnote/web check:reports`

The unit wrapper accepts file selectors and `-t`/`--testNamePattern` only. Browser
forwarding accepts file selectors and `--grep`, `--grep-invert`, `--timeout` only.
Neither stable entry point permits custom reporters, config overrides, retries,
list-only runs or recording/debug modes to masquerade as acceptance evidence.

Public runner environment / CLI contract:

- `WEB_E2E_DATABASE_URL`: mandatory explicit one-time PostgreSQL DSN (or pass
  `--database-url`; never pass credentials on a logged command line).
- `REQUIRE_POSTGRES_TESTS=1`: caller release gate; runner always sets `1` for
  owned children even if its inherited environment omitted the variable.
- Optional runner `--api-port` / `--web-port`: default `0` selects available
  loopback ports; an explicitly occupied port fails rather than being reused.
- Full run: `pnpm test:e2e`.
- Focused run: `pnpm test:e2e -- --grep "workspace_flow|markdown_preservation|manual_conflict|mobile_layout"`.
  Repeated leading `--` separators from pnpm forwarding are accepted.

The runner privately supplies `WEB_E2E_BASE_URL`, `WEB_E2E_API_URL`,
`WEB_E2E_RUN_ID`, `WEB_TEST_REPORT_SCOPE`, `API_PROXY_TARGET`, `DATABASE_URL` and
test-only API settings to its children. Callers must not use those variables to
attach to unrelated services. `WEB_TEST_REPORT_SCOPE` is informational: reporters
derive full/focused from actual command selection, not that environment value.

`pnpm test:web` writes `reports/full/vitest.{json,xml}`. Arguments selecting a
subset write `reports/focused/` instead. Browser reports follow the same rule.
`pnpm check:web-reports` accepts full evidence only. To inspect a focused run use
`--scope focused --runner vitest` (or `playwright`) and optionally
`--require workspace_flow,markdown_preservation,manual_conflict,mobile_layout`.
Focused evidence never replaces the full report. Reporters overwrite an initial
incomplete report at run start, reject retries/skips/todos/errors, and persist
only hashed case IDs, fixed labels and counts. No assertion messages, note
content, credentials, stack traces, attachments or browser recordings are saved.

CI job-local full gates use `check:reports --unit` and `check:reports --e2e`.
The root `check:web-reports` without arguments requires both full reports.

Vitest uses jsdom and a new fake-indexeddb factory per case; this is not evidence
for browser durability or Web Locks. Only real Chromium acceptance proves those.
