# Phase 6 — Joplin Server compatibility bridge

Status: **implemented** (`adapters/joplin`). The bridge is a parallel HTTP face
that lets a stock Joplin client treat Revnote as a Joplin Server sync target. It
is a compatibility layer, **not** the product's main path: the native
`/api/v1` contract is unchanged, and with the switch off the deployment behaves
exactly as it did in Phase 5.

Protocol baseline: Joplin **3.7.x** Server driver. The endpoint/shape reference
is [`docs/joplin-protocol-notes.md`](joplin-protocol-notes.md).

## 1. Switch and surface

| Setting | Default | Meaning |
| --- | --- | --- |
| `JOPLIN_BRIDGE_ENABLED` | `false` | Mounts `/api/sessions`, `/api/items/...`, `/api/locks`, `/api/batch_items`. Off means `404`, the same answer as a server that never implemented them. |
| `JOPLIN_PROTOCOL_BASELINE` | `3.7` | Reported by `/api/v1/capabilities`; informational. |
| `JOPLIN_SESSION_TTL_S` | `2592000` | Bridge session lifetime (30 days). |
| `JOPLIN_LOCK_TTL_S` | `10800` | A lock not refreshed for three hours is abandoned. |
| `JOPLIN_DELTA_DEFAULT_LIMIT` / `JOPLIN_DELTA_MAX_LIMIT` | `100` / `500` | Delta page size, clamped. |

`GET /api/v1/capabilities` gains one object (additive; old clients ignore it):

```json
{"attachments": {...},
 "joplin_bridge": {"enabled": true, "protocol_baseline": "3.7", "mount": "/api"}}
```

The compatibility routes are excluded from the native OpenAPI document on
purpose: that document is the v1 contract, and publishing a second error model
in it would invite generated clients to depend on the bridge.

## 2. Identity and sessions

* Login is `POST /api/sessions` with email/password. The password is verified by
  the **real** `AuthService` (same Argon2 path, same constant-time rejection).
  The native token pair that verification issues is revoked immediately, so a
  Joplin login never leaves a live native credential behind.
* The bridge then mints its own opaque token (32 random bytes, url-safe). Only
  its SHA-256 digest is stored, in `joplin_sessions`. The client sends it as
  `X-API-AUTH`; a bearer header is also accepted.
* The workspace is chosen **once, at login**: the user's earliest membership
  (tie-broken by workspace id), frozen on the session row. A Joplin account is
  one sync space; recomputing it per request could silently move a client.
* Wrong password, unknown account, expired row, revoked row and unknown token
  all produce the same `401 {"code": "unauthenticated"}` or
  `401 {"code": "invalid_credentials"}` — never "does this account exist".

## 3. Item keys and the mapping table

The native UUID is authoritative (D6). A Joplin key is 32 lowercase hex.

* For an entity Revnote created, the key is **derived**: `uuid.hex`. No row is
  needed, and the key is stable forever.
* For an entity a Joplin client created with its own id, `joplin_id_map` stores
  `(workspace_id, jop_key) → (entity_kind, entity_id)` plus the reverse unique
  constraint. The mapping row is written **before** the kernel command, so a
  retry after a lost response resolves the same entity instead of creating a
  duplicate; a mapping that never became an entity is reused by the next
  attempt.

Documented consequence: a client that invents a key equal to an existing native
UUID's hex can address that entity. It is already a workspace member and could
read the item through the native API, so this is not an escalation — it is
recorded so it is not a surprise.

## 4. Writes: Joplin is last-write-wins, the kernel still owns the invariants

A Joplin `PUT` carries no `base_rev`. The bridge therefore:

1. resolves the item (map row, else UUID hex);
2. reads the current row and its `rev`;
3. creates it (`CreateNote`/`CreateFolder`) or applies the difference — move
   first (parent changed), then update (title/body/name changed) — each through
   `SyncWriteService` with the revision it just read.

A concurrent native writer can still win between the read and the write; the
kernel answers `RevConflict`, the bridge maps it to `409`, and the client
re-reads and retries. That is the CAS doing its job, not a bridge bug.

An identical `PUT` writes nothing: no event, no revision bump. This matters
because Joplin re-uploads items it is unsure about.

Notes are stored in `notes.body_md`; folders in `folders`. The bridge never
writes either table directly. Deletes go through `DeleteNote`/`DeleteFolder`, so
a bridge delete is an ordinary tombstone with an ordinary `change_events` row.

## 5. Delta

`GET /api/items/root:/<path>:/delta?cursor=` reads `change_events` after the
cursor (a decimal `seq`), compresses the page (create/update/delete as in the
protocol notes), and returns `{items, cursor, has_more}`.

* The cursor is the page's **last raw seq**, including events compression
  dropped. Returning the last emitted seq would re-read a create+delete pair
  forever.
* A non-numeric, negative or ahead-of-`last_seq` cursor answers
  `400 {"code": "resyncRequired"}`; the client then restarts from zero.
* Compression is page-local, exactly like upstream: a note created on one page
  and updated on the next is `create` then `update`. Both pages are correct; the
  client re-fetches the content.
* `jopItem` is deliberately omitted, so the client fetches item content itself.
  That keeps the bridge's serialization the single source of truth.

## 6. Locks

`joplin_locks` holds `(workspace, lock_type, client_type, client_id)` rows.
Mutations take a transaction-scoped advisory lock per workspace, because the
first acquisition of a new client is an insert and there is no row to lock yet.

* `sync` acquires unless another client holds `exclusive`.
* `exclusive` acquires only when no other client holds anything.
* Re-acquiring your own lock refreshes `updated_at`.
* Rows older than `JOPLIN_LOCK_TTL_S` are deleted on the next acquire and
  invisible to `GET`.
* Lock traffic never touches `change_events`, `op_ledger` or
  `workspaces.last_seq`.

Responses use the client's shape: `{"id": "2_1_abc", "type": 2, "clientType": 1,
"clientId": "abc", "updatedTime": <epoch ms>}`.

## 7. Resources

A resource is **an attachment**, not a second store (D7):

1. `PUT <key>.md` with `type_: 3` creates a `pending` attachment through
   `AttachmentService.create` (MIME allowlist, size cap) and maps the key.
2. `PUT .resource/<key>` streams into `AttachmentService.store_content`, which
   writes under `ws/{workspace}/att/{attachment}/{nonce}`, **re-reads** the
   object and publishes `ready` only when the digest matches.
3. `GET .resource/<key>` authorizes through the same membership join as the
   native endpoint and streams the stored object.

Consequences, stated plainly:

* only the Phase 5 MIME allowlist is accepted (`png/jpeg/webp/gif/pdf/text`);
  anything else is `422`, which the client treats as "permanently rejected";
* `DELETE .resource/<key>` returns `204` and **keeps the object**: Phase 5 has
  no attachment deletion (D13), and pretending to delete would either break a
  note that still references it or report a state the database does not hold;
* resource discovery through delta is not provided — attachments are not sync
  entities. A client sees a resource when the note's item changes; the blob is
  fetched by path when the client asks for it.

## 8. Boundaries this phase did not move

* `kernel/**` and `services/**` do not import `adapters/**`; the adapter imports
  them, never the reverse. A unit test enforces the arrow.
* `change_events` has no new `entity_type`, no new CHECK and no bridge rows.
  Attachments, mappings, locks and sessions never advance `last_seq`.
* The native `/api/v1` routes, error envelope, response shapes and the Phase 5
  attachment contract are unchanged. The only native change is the additive
  `joplin_bridge` object in `/capabilities`.
* RustFS 1.0.4 and the `S3_*` adapter are unchanged; no second store, no
  presigning, no MinIO.

## 9. Known limitations

| Limitation | Why | Effect |
| --- | --- | --- |
| Resource deletion is a no-op | Phase 5 has no attachment deletion | The Joplin client removes its local copy; the object remains |
| No E2EE decryption | The bridge is not a crypto endpoint | E2EE items cross only if they can be parsed as plaintext; not promised |
| No `api/files/*` | The 3.7 client uses `api/items/*` | Older clients are not supported |
| No sharing / revisions | Out of scope (D1) | Single-user workspaces only |
| Bridge sees one workspace per account | A Joplin account is one sync space | Multi-workspace users sync their earliest workspace |
| Full Joplin GUI not in CI | D12 | Protocol is covered by HTTP contract tests; GUI is optional local evidence |

## 10. Verification

`apps/api/tests/api/test_joplin_bridge.py` (13 cases, real PostgreSQL) and
`test_joplin_resources.py` (4 cases, real PostgreSQL + real RustFS) are the
evidence. They run in the `api-attachments` CI job under `-m joplin` and are
guarded by `apps/api/scripts/check_test_reports.py --group joplin`. Actual run
results are in [`docs/phase6-validation.md`](phase6-validation.md).
