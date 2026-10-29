# Phase 4 browser acceptance inventory

There are no placeholder, skipped or empty browser tests. The
required labels are frozen in `scripts/check-test-reports.ts` (`E2E_LABELS`). A
full report must contain all labels; adding a title alone is not acceptance.

The five `*.spec.ts` files now define the real acceptance scenarios; collection
and static checks are not proof that those scenarios passed. Record actual
runner results separately. `fixtures.ts` performs UI login/editing, inspects real
IndexedDB, and obtains only counts/hashes from PostgreSQL through runner IPC.

Runner-only IPC lives on a separate ephemeral loopback port, protected by a
random in-memory control key available only to the Node test process. It is not
proxied through Vite, mounted in the API, or exposed to browser application code.
Its bounded actions stop/restart only the owned API, lower the snapshot object
cap on a restarted test process, inspect PG counts/hashes, and prepare a retained
event floor in the one-time database. It accepts no SQL or note bodies. Production
routes, migrations and business code are unchanged. API restarts run sequentially.

Lost-response cases call `route.fetch()` first, verify committed PG event/ledger
rows via another connection, then discard/delay the real response. Cursor tests
request actual one-event API pages (including leading objects), and abort a real
browser IDB page transaction through a test-installed wrapper. Corrupt-cursor and
bootstrap preparation changes only browser-local metadata; all user edits and
manual resolution actions still use the rendered UI. Authentication and request
payloads are never logged or serialized into the restricted reports.

### Fault-injection invariants

Five fixture rules keep a deliberately damaged environment from being
misreported as a product defect:

1. **API restoration is unconditional.** `api_restart_recovery` and
   `resync_preservation` stop the runner-owned API. Everything after the stop —
   including preparation and assertions — runs inside `try`, and `finally` always
   restarts it (`ensureOwnedApi` / `restartOwnedApi`). `restartOwnedApi` also
   clears the lowered `snapshotLimit`, so a capped process cannot leak into the
   next case. A stopped API stays stoppable only inside the `try`.
2. **The API guard fails the leaking case, not the suite.** The auto
   `apiGuard` fixture probes the owned API through the same proxy after every
   case. If a case exits with the process down it restarts it and fails *that*
   case with an explicit message, instead of letting every later
   `POST /auth/register` fail with "registration succeeded" and look like a
   product bug. The probe uses only `/api/health`, the single route the proxy
   rewrites; it never becomes a production debug endpoint.
3. **`workspaces` is a keyless store.** `createObjectStore('workspaces')` has no
   keyPath, so `put(value)` throws `DataError` and aborts the transaction. The
   cursor/bootstrap helpers therefore read `getAllKeys()` and call
   `put(state, key)` with that exact key, mirroring `partitionKey` in
   `src/storage/repository.ts`. They also refuse to write when no row matches the
   workspace selected in the UI, rather than fabricating a key.
4. **A move assertion must move something.** `moveLocal` returning a receipt for
   an unchanged parent is approved no-op semantics, and such a call legitimately
   emits no operation. `workspace_flow` therefore builds each target at the root
   (creating a folder also *selects* it, so roots require an explicit
   `全部笔记` click) and asserts the resulting `parentId` really changed before
   the eight-operation set is compared. Otherwise the missing `folder:move`
   would look like a product defect instead of a test that moved a folder into
   the folder it was already in.
5. **Login and workspace readiness are separate facts.** `enterLogin` proves only
   that the credential exchange succeeded: the header selects this account's
   workspace and the login form is gone. It deliberately does *not* assert that
   `新建笔记` is enabled, because `NotesSidebar` gates creation on
   `bootstrapped`, and `resync_preservation` intentionally leaves the workspace
   unbootstrappable by restarting the API with `snapshotLimit: 1` (a real
   `snapshot_too_large` can never bootstrap). Disabling creation there is
   approved behaviour, not a bug. Cases that need an editable workspace call
   `expectWorkspaceReady`, and `createNote`/`createFolder` wait for it
   themselves, so removing the coupled assertion cannot introduce a bootstrap
   race.

`single_writer_takeover` keeps the writer and follower as two tabs of the same
`BrowserContext`, because only that shares the origin-wide Web Lock and
IndexedDB. The writer dies through `Page.crash` (a real renderer crash, not a
graceful close), which leaves its in-flight command durable but uncertain.
`Page.crash` never resolves, so it is sent un-awaited and the actual `crash`
event is awaited; the page is then closed and the takeover waits until
`navigator.locks.query()` shows no `revnote-writer-v1` holder. Only then does the
follower click `接管写入`. A renderer crash emits no `pageerror`, so the runtime
guard is unaffected, and the assertion that the crashed command produces exactly
one event/ledger row (and no invented replacement) is unchanged.

| Label | Real browser / API / PostgreSQL responsibility | Spec |
| --- | --- | --- |
| `workspace_flow` | Proxy login, me, snapshot and all eight operation kinds | `workspace.spec.ts` |
| `draft_reload` | Local reload and a fresh context reading server content | `workspace.spec.ts` |
| `api_restart_recovery` | API stopped, Vite reachable, edit/reload, restart and drain | `durability.spec.ts` |
| `offline_recovery` | Loaded page offline edit and online queue drain | `durability.spec.ts` |
| `lost_response_replay` | Fetch real committed response before dropping it; reload; same command; one event/ledger | `durability.spec.ts` |
| `ack_preserves_later_edit` | Delay A acknowledgement, edit B, acknowledge A without discarding B | `durability.spec.ts` |
| `manual_conflict` | Independent clients, CAS conflict, retained versions and explicit resolution | `conflicts.spec.ts` |
| `remote_delete_recovery` | Remote delete with local draft; recovery and new-ID save only | `conflicts.spec.ts` |
| `single_writer_takeover` | Same-origin tabs, read-only restrictions, crash takeover and late results | `durability.spec.ts` |
| `refresh_rotation` | Remembered refresh restores one session without concurrent reuse | `sessions.spec.ts` |
| `refresh_uncertainty` | Real refresh processed, response dropped, explicit reauthentication | `sessions.spec.ts` |
| `session_identity_isolation` | Another account cannot view or transmit the first account's queue | `sessions.spec.ts` |
| `resync_preservation` | Snapshot limits, 410 and abnormal cursor retain local state | `reconciliation.spec.ts` |
| `cursor_integrity` | Leading objects, replay and page transaction failure cannot skip events | `reconciliation.spec.ts` |
| `markdown_preservation` | Extended syntax, CRLF, trailing whitespace, HTML/LaTeX/unknown blocks unchanged | `workspace.spec.ts` |
| `mobile_layout` | 390px panels, keyboard operation, read-only state, no runtime errors | `workspace.spec.ts` |

Run through `apps/api/scripts/run_web_e2e.py`; never attach to an existing API or
Vite process. The PostgreSQL controller owns the external one-time database.
The runner owns API, Vite and browser process groups, and always cleans up.
Windows additionally uses a kill-on-close kernel Job Object, retaining ownership
of browser descendants even if pnpm has already exited. The explicit DSN must use
`postgresql+asyncpg`, a loopback host and a fresh empty database named
`revnote_web_e2e` or `revnote_web_e2e_<id>`, distinct from both Python test targets.
Reusing a database containing a prior E2E run is rejected: create another one-time
database externally. Applicable env files (`./.env*` plus `apps/api/.env*` and
`apps/web/.env*`) are rejected without reading them; API/Alembic explicitly
disable implicit env-file settings.

The browser child process never receives `DATABASE_URL` or
`WEB_E2E_DATABASE_URL`; only the runner and the owned API do. A failure prints at
most 25 lines of the form
`Browser failure: e2e/<file>.spec.ts:<line> labels=<fixed labels> case=<sha256>`
— always `source:line`, never a message, stack, request body or credential. The
same restricted output is printed when the browser exits zero but the report
guard rejects the evidence.

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
`WEB_E2E_RUN_ID`, `WEB_TEST_REPORT_SCOPE`, `API_PROXY_TARGET` and test-only API
settings to its children. `DATABASE_URL`/`WEB_E2E_DATABASE_URL` reach only the
runner and the owned API process, never the browser test process. Callers must
not use any of those variables to attach to unrelated services.
`WEB_TEST_REPORT_SCOPE` is informational: reporters derive full/focused from
actual command selection, not that environment value.

`pnpm test:web` writes `reports/full/vitest.{json,xml}`. Arguments selecting a
subset write `reports/focused/` instead. Browser reports follow the same rule.
`pnpm check:web-reports` accepts full evidence only. To inspect a focused run use
`--scope focused --runner vitest` (or `playwright`) and optionally
`--require workspace_flow,markdown_preservation,manual_conflict,mobile_layout`.
Focused evidence never replaces the full report. Reporters overwrite an initial
incomplete report at run start, reject retries/skips/todos/errors, and persist
only hashed case IDs, fixed labels, counts and `source:line`. No assertion
messages, note content, credentials, stack traces, attachments or browser
recordings are saved.

Locating metadata is deliberately narrow. `source` must match a test-owned
filename (`e2e/<name>.spec.ts` or `tests/{unit,components}/**/*.test.ts[x]`);
anything else — a script, `src/`, a nested directory, a `..` segment, a URL or a
relative path — is dropped rather than sanitized. `line` must be an integer that
exists in that file. Playwright's `TestCase.location.line` is not trusted on its
own: it can report an index that is not a line of the on-disk file, so the
reporter reads the exact `test('<title>'` declaration from disk and uses the
runner index only as a fallback. A case therefore either carries a verifiable
`source:line` or no location at all. `line` is present exactly when `source` is,
and `validateReport` rejects unknown keys so a message, stack or attachment
field cannot be appended to a report and pass the gate.

CI job-local full gates use `check:reports --unit` and `check:reports --e2e`.
The root `check:web-reports` without arguments requires both full reports.

Vitest uses jsdom and a new fake-indexeddb factory per case; this is not evidence
for browser durability or Web Locks. Only real Chromium acceptance proves those.
