# Phase 5 attachment API — protocol appendix

This document is an **appendix** to the locked Phase 3 contract
([`phase3-sync-api-spec.md`](phase3-sync-api-spec.md)). It is not a replacement, not a revision, and not a
new version of it.

## 1. Scope

Phase 5 **adds** attachment transfer to `/api/v1`. It removes nothing and changes nothing:

- no Phase 3 endpoint, request field, response field or error code is modified;
- the eight push commands, the six push statuses, `pull`/`snapshot`/`tree`/`note` semantics and the error
  envelope are exactly as the Phase 3 document specifies;
- one table is added (`attachments`), one migration (`0004_attachments`), five endpoints and six error
  codes.

Because every change is additive and strictly optional for a client, the protocol version stays
**`/api/v1`. There is no `/api/v2`.**

A client that knows nothing about attachments (the Phase 4 Web build) keeps working unchanged against a
Phase 5 server: it never calls the new endpoints. A client that does know about them degrades cleanly
against an older server (§9).

Everything below describes `apps/api/src/revnote_api/` as it exists: `api/routes/attachments.py`,
`api/schemas/attachments.py`, `services/attachments.py`, `models/attachment.py`, `storage/ports.py`,
`storage/s3.py`, `api/errors.py`, `config.py`, `api/middleware/request_limits.py`.

## 2. Endpoints

All paths are relative to `/api/v1` and are registered in `api/router.py`. Except `GET /capabilities`,
every endpoint requires `Authorization: Bearer <access>` and a caller who is a member of the workspace.

| Method | Path | Request | Success |
|---|---|---|---|
| `GET` | `/capabilities` | no body, **no token** | `200` always |
| `POST` | `/workspaces/{workspace_id}/attachments` | JSON `{mime, byte_size?, file_name?}` | `201` pending row + upload target |
| `PUT` | `/workspaces/{workspace_id}/attachments/{attachment_id}/content` | raw bytes | `200` verified digest |
| `GET` | `/attachments/{attachment_id}` | no body | `200` metadata |
| `GET` | `/attachments/{attachment_id}/content` | no body | `200` byte stream |

### 2.1 `GET /capabilities`

Unauthenticated, parameters-free, always `200`. See §9 for the full contract and the reason it is open.

```json
{
  "attachments": {
    "enabled": true,
    "max_bytes": 26214400,
    "accepted_mime": ["image/png", "image/jpeg", "image/webp", "image/gif", "application/pdf", "text/plain"]
  }
}
```

`enabled` is `attachments_enabled` (default `false`). `max_bytes` is `attachment_max_bytes`, default
`26214400` (25 MiB). `accepted_mime` is the normalised allowlist, or `ACCEPTED_MIME_DEFAULT` when the
configured list normalises to empty. Response header: `Cache-Control: no-store`.

### 2.2 `POST /workspaces/{workspace_id}/attachments`

Body is a closed model (`extra="forbid"`): an unknown key is a `400`, not a silently ignored field.

| Field | Type | Required | Notes |
|---|---|---|---|
| `mime` | string, 3–127 characters | yes | must be in the deployment allowlist (§8) |
| `byte_size` | integer ≥ 1, or absent | no | **advisory**; the authoritative size is what the store writes |
| `file_name` | string, ≤ 255 characters, or absent | no | display only; never part of a storage key, never logged |

`201` response:

```json
{
  "attachment_id": "…uuid…",
  "state": "pending",
  "upload": {
    "method": "PUT",
    "url": "/api/v1/workspaces/{workspace_id}/attachments/{attachment_id}/content"
  },
  "expires_at": "2026-01-01T00:00:00Z"
}
```

`upload.url` is a **relative path**, not a presigned URL. The client sends the bytes back through this
API with its own bearer token; there is no credential-bearing link into the object store, and therefore no
second authorisation model to keep in sync. `expires_at` is `created_at + attachment_pending_grace_s`,
computed by PostgreSQL.

This call touches no object: it inserts one `pending` row. An abandoned `POST` therefore costs one row for
the GC to collect, never a half-written object.

Failures, and why each exists:

| Status | Code | Condition |
|---|---|---|
| `400` | `invalid_request` | body does not match the contract: unknown key, wrong type, `mime` shorter than 3 or longer than 127, `file_name` longer than 255, or `byte_size` absent-or-≥1 violated |
| `400` | `attachment_unsupported_type` | `mime` rejected by the allowlist. `details.reason` is one of `mime_missing`, `mime_length`, `mime_shape`, `mime_svg`, `mime_not_accepted` |
| `401` | `unauthenticated` | missing or unusable token |
| `404` | `not_found` | workspace does not exist **or** the caller is not a member |
| `413` | `attachment_too_large` | declared `byte_size` exceeds `attachment_max_bytes`; `details.max_bytes` carries the limit |
| `503` | `attachment_storage_unavailable` | `attachments_enabled` is `false`; `details.reason` is `"disabled"` |
| `500` | `internal` | the per-request object-store settings could not be built (see the note below) |

Two entries deserve their reasons stated:

- **`invalid_request` vs `attachment_unsupported_type`** for bad MIME are different failures. A `mime` of
  the wrong *shape or length* is a contract error; a well-formed `mime` outside the allowlist is a policy
  refusal that the client can present as "this file type is not accepted".
- **`413` is `attachment_too_large`, not `payload_too_large`.** The generic `payload_too_large` means "this
  request body exceeded this path's budget"; `attachment_too_large` means "this attachment exceeds the
  deployment's attachment cap". Keeping them distinct lets a client react correctly, and the middleware and
  the store write are configured from the **same** `attachment_max_bytes`, so "allowed here, refused a layer
  deeper" is not expressible.

> **Note on an unconfigured object store (measured, not intended).** When `ATTACHMENTS_ENABLED=true` but
> `OBJECT_STORAGE_ENDPOINT`/`_BUCKET`/`_ACCESS_KEY`/`_SECRET_KEY` are missing, `ObjectStorageSettings()`
> raises, `build_store` re-raises it as `StorageUnavailable`, and that exception escapes the
> `get_attachment_service` dependency **before** the handler body runs. No error handler is registered for
> `BlobStoreError`, so the catch-all handler renders it as **`500 internal`** — not as the
> `503 attachment_storage_unavailable` the route module's own docstring describes. The same applies to
> every other attachment endpoint, including the two reads. The `503 … details.reason="disabled"` row above
> is real and was verified, but only for a deployment whose storage settings **are** present.

### 2.3 `PUT /workspaces/{workspace_id}/attachments/{attachment_id}/content`

The body is the raw byte stream: not multipart, not base64, not parsed, not decoded. It is re-chunked into
`UPLOAD_CHUNK_BYTES` = 65536-byte slices (`_read_request_chunks`) so that memory is O(chunk) regardless of
attachment size.

| Header | Required | Meaning |
|---|---|---|
| `Content-Type` | yes | must equal the `mime` declared at `POST`; parameters such as `; charset=utf-8` are ignored |
| `X-Content-SHA256` | no | the client's **claim** about the bytes: 64 hex characters, or base64 (padded or not) decoding to 32 bytes |
| `Content-Length` | no | a claim only; the cap is enforced against bytes actually received |
| `Content-Encoding` | — | only absent or `identity` is accepted; anything else is `413 payload_too_large` |

`200` response:

```json
{
  "attachment_id": "…uuid…",
  "state": "ready",
  "content_hash": "…64 lowercase hex…",
  "byte_size": 123456
}
```

`content_hash` is the digest the server computed by **re-reading the object**, not the value the client
declared.

**An empty body is refused, not stored.** After the write and the digest comparison, a verified size of
`0` is rejected as `400 invalid_request` (the object is discarded and the row stays `pending`, so the GC
owns cleanup). Without this check the CAS would try to write `byte_size = 0`, violating
`CHECK (byte_size > 0)`, and the client would see a `500` for its own mistake.

Failures:

| Status | Code | Condition |
|---|---|---|
| `400` | `attachment_unsupported_type` | `Content-Type` differs from the declared `mime`, is absent, or is `image/svg+xml`. `details.reason` is `content_type_missing`, `content_type_mismatch` or `mime_svg` |
| `400` | `invalid_request` | `X-Content-SHA256` is neither 64 hex characters nor base64 decoding to 32 bytes (`details.fields[0]` = `{"path": ["header","X-Content-SHA256"], "reason": "invalid"}`), **or** the body was zero bytes |
| `401` | `unauthenticated` | missing or unusable token |
| `404` | `not_found` | no such attachment in this workspace, the caller is not a member, or the row is `deleted` |
| `409` | `attachment_already_ready` | the row is already `ready`. Decided **before the first body byte is read** |
| `409` | `attachment_expired` | the `pending` row outlived `attachment_pending_grace_s` |
| `409` | `attachment_hash_mismatch` | the recomputed digest differs from the client's declared `X-Content-SHA256` |
| `413` | `payload_too_large` | the body-limit middleware refused it: declared length over the cap, running total over the cap, or a non-identity `Content-Encoding` |
| `413` | `attachment_too_large` | the **store write** aborted at the first byte past `attachment_max_bytes`; `details.max_bytes` carries the limit |
| `503` | `attachment_storage_unavailable` | the store is unreachable, the deployment has attachments disabled, or the object read back differed from the object written (`details.reason = "stored_bytes_changed"`) |

**Why the state is checked before the body.** `not_found`, `already_ready`, `attachment_expired` and a
mismatched `Content-Type` are all decided in phase 1, from one short read, before a single body byte is
consumed. A refused upload therefore costs no transfer, and a client never spends a 25 MiB upload to be
told `409`.

**Why the two `413`s differ.** `payload_too_large` comes from the ASGI middleware and means "this request
was too large for this path"; `attachment_too_large` comes from the service and means "this attachment is
over the cap". They share one configured number (`attachment_max_bytes`, passed to both the middleware and
`BlobStore.put`), so they can never disagree about which bodies are legal.

### 2.4 `GET /attachments/{attachment_id}`

The path is **flat** — no `workspace_id`. Authorization comes from the membership join inside the service,
not from the URL. `200` response:

```json
{
  "id": "…uuid…",
  "workspace_id": "…uuid…",
  "state": "ready",
  "mime": "image/png",
  "byte_size": 123456,
  "content_hash": "…64 lowercase hex…",
  "created_at": "2026-01-01T00:00:00Z",
  "ready_at": "2026-01-01T00:00:05Z",
  "file_name": "example.png"
}
```

`content_hash` travels as lowercase hex, never as `BYTEA`. `byte_size` is a JSON integer: a byte count is
not a sync counter and never approaches 2^53. `file_name` is the only field echoing user-supplied text and
the only optional one. Response header: `Cache-Control: no-store`.

Failures: `401 unauthenticated`; `404 not_found` for **unknown id, non-member, and every state other than
`ready`** (`pending` and `deleted` included); `500 internal` if the object-store settings cannot be built
(§2.2 note). This endpoint never touches the store, so it has no `503`.

### 2.5 `GET /attachments/{attachment_id}/content`

`200` with the stored bytes, `Content-Type` set from the row's `mime`. Three headers are contract:

| Header | Value | Why |
|---|---|---|
| `Cache-Control` | `no-store` | the bytes are authorised, workspace-private content; a shared cache retaining them would serve them to the next principal |
| `X-Content-Type-Options` | `nosniff` | the stored MIME is a claim made by the **uploader**; a browser must not re-interpret the payload (`text/plain` that is really HTML is the classic case) |
| `Content-Security-Policy` | `default-src 'none'; sandbox` | defence in depth: even if a MIME judgement is wrong, the content must not execute on this origin |

`Content-Length` is deliberately **not** set from the row. The row records what the store verified; if that
and the stream ever disagreed, setting the header would either truncate the body or hang the connection.
Framing is left to the store's own response.

The service calls `BlobStore.stat` **before** the response starts, outside any transaction, so "the row says
`ready` but the object is gone" is a clean `503` rather than a `200` that ends after zero bytes. A download
is relayed chunk by chunk (`iter_chunks`, 65536-byte chunks) and the iterator is closed in a `finally`, so an
abandoned download returns its connection instead of leaking one.

Failures: `401 unauthenticated`; `404 not_found` (same collapse as §2.4); `503
attachment_storage_unavailable` when the store is unreachable or the object is missing
(`details.reason = "object_missing"`); `500 internal` if the object-store settings cannot be built.

**No `Range` support.** There is no `206`, no resume, no part upload anywhere in the attachment code; a
download is always the full object in one `200`.

### 2.6 Error codes added by Phase 5

Six codes were **appended** to `ERROR_SPECS`; no existing code changed status, message or retryability.

| Code | Status | `retryable` | Meaning |
|---|---|---|---|
| `attachment_storage_unavailable` | `503` | **true** | store unreachable, or attachments disabled. A `503` with `retryable: true` also carries `Retry-After: 1`. An **unconfigured** store is not this code — it surfaces as `500 internal` (§2.2 note) |
| `attachment_already_ready` | `409` | false | the attachment is already uploaded; the object is immutable |
| `attachment_hash_mismatch` | `409` | false | the bytes read back do not hash to the client's declared digest |
| `attachment_expired` | `409` | false | the `pending` row outlived the grace window |
| `attachment_too_large` | `413` | false | the attachment exceeds `attachment_max_bytes` |
| `attachment_unsupported_type` | `400` | false | the media type is not accepted (`image/svg+xml` never is) |

The envelope is unchanged: `{"error": {code, message, request_id, retryable, details}}`. `message` is a
fixed sentence owned by `api/errors.py` — `str(exc)` is never forwarded. `details` carries fixed labels and
policy numbers only, never client input and never driver text (a driver message can quote row values,
including a file name).

## 3. State machine

```
POST                INSERT one row: state='pending', upload_nonce=fresh uuid7
                    created_at = now();  expires_at = created_at + grace (derived, not stored)

PUT  phase 1        short transaction: read (workspace_id, attachment_id)
                    missing/deleted -> not_found;  ready -> already_ready;  expired -> attachment_expired
                    Content-Type != declared mime -> attachment_unsupported_type

PUT  phase 2        NO TRANSACTION: stamp upload_started_at in its own short commit, then
                    stream the bytes into the store (SHA-256 and length computed while writing),
                    then re-read the object and recompute the digest, then compare.

PUT  phase 3        short transaction: one conditional UPDATE publishes 'ready'
                    (a verified size of 0 is refused as invalid_request before this point)
                    UPDATE ... WHERE id=? AND workspace_id=? AND upload_nonce=? AND state='pending'
                    1 row  -> 200 with the verified digest
                    0 rows -> delete this attempt's key, then re-read the row:
                              gone/deleted -> not_found,  otherwise -> already_ready

GET (metadata)      one statement joining workspace_members; state='ready' filtered in SQL
GET (content)       same statement, then stat, then stream — no session open while streaming

GC (operator)       DELETE only rows with state='pending' AND created_at < now() - grace.
                    'ready' is invisible to the scan by construction.
```

**Upload and verification happen outside any transaction, deliberately.** No code path may hold a
PostgreSQL connection or transaction while performing object-store I/O. A pooled connection is the scarcest
resource this service has: a 25 MiB upload trickling in over thirty seconds would pin one connection per
concurrent client, and a stalled store would pin them until the pool timed out. So every storage call is
sandwiched *between* short transactions rather than wrapped in one. The HTTP layer keeps the other half of
the rule: it never wraps a streaming response in a session either.

**Exactly one `ready` row.** Two clients can `PUT` the same `attachment_id` concurrently. The winner is
chosen by a single `UPDATE … WHERE state='pending'` under the row lock PostgreSQL takes for that update: the
second writer sees zero rows and knows it lost. There is no read-then-write window, and no row is locked
across storage I/O. That much is sound. What is **not** sound is the loser's cleanup: because both attempts
write the same object key (§4), a losing attempt that runs its cleanup after the winner published deletes
the winner's bytes. So "one `ready` row" holds while "one intact object" does not — see §10.

**Expiry is decided in SQL.** `created_at` comes from `now()`, so the deadline is evaluated by PostgreSQL
against the clock that produced it. A Python-side comparison would depend on clock skew between the
application and the database, and the GC — a separate process — would then disagree with the upload path
about the same row.

**`deleted` is a legal state that current code does not write.** The column's `CHECK` allows
`('pending','ready','deleted')` and `deleted_at`/`delete_reason` columns exist, but nothing in the service
sets `state='deleted'`: the GC issues `DELETE FROM attachments`, removing the row outright. The read paths
treat a `deleted` row as `not_found` and the upload path refuses to publish one, so the state is handled
correctly wherever it is encountered — it is simply not produced today. This document records that rather
than describing the unimplemented attachment-delete feature as a soft delete.

`upload_started_at` is stamped in a **separate** short commit before the stream begins, not folded into the
publish step, so an upload that never finished remains visible to an operator.

## 4. Storage layout

Key form:

```
ws/{workspace_id}/att/{attachment_id}/{upload_nonce}
```

- Exactly four path segments; every UUID in canonical lowercase form.
- **Immutable.** `storage_key` and `upload_nonce` are written once, at `create`, and never change.
  `UNIQUE(storage_key)` makes one key mean one row.
- **No request content contributes a byte.** No file name, no client-supplied string is concatenated, so a
  hostile `file_name` cannot influence where bytes are written.
- `storage_key` is internal: it is never serialised into any response (`AttachmentView.storage_key` is
  excluded).

**What the nonce does, precisely.** `upload_nonce` is minted per **row** at `create` time and never
changes. It is therefore part of the key's identity — the key an attempt wrote is fixed and unforgeable —
and it appears in the CAS predicate so the publish step re-checks that the row still carries the nonce the
attempt read. It does **not** distinguish one attempt from another: two concurrent `PUT`s to the same
`attachment_id` read the same nonce from the same row and therefore write the **same** key. The
winner/loser decision comes from `state='pending'` under the row lock, not from differing keys.

A consequence worth stating plainly, because the code does it: a losing attempt calls
`_discard_object(key)` → `BlobStore.delete(key)` on that shared key. If a losing attempt reaches its cleanup
after the winner's publish, it deletes the object the winning row points at. The cleanup exists so a failed
attempt leaves no object behind (which is what the `attachment_hash_mismatch` path needs); on the
concurrent-upload path it is not key-disjoint. This is a real, reproduced data-loss defect — see §10.

## 5. Digest rule

A `ready` row means "a SHA-256 of these exact bytes was measured on the server", and the rule is:

1. **While writing**, `BlobStore.put` updates a SHA-256 and a running byte count as each chunk arrives, and
   returns a `BlobDigest` describing **what reached the store**. `size` is the number of bytes the store
   accepted, not the number the client announced: a stream that ends early yields a short digest, not an
   error. The write aborts the instant the running total passes `max_bytes`
   (`BlobTooLarge` → `attachment_too_large`), so an oversized upload never becomes a partial object.
2. **After writing**, `BlobStore.digest` re-reads the object chunk by chunk and hashes it again. This is a
   **measurement**, not a statement.
3. **The two are compared**: `(verified.sha256_hex, verified.size)` must equal
   `(written.sha256_hex, written.size)`. A mismatch means the store handed back different bytes than it
   accepted. The client did nothing wrong, so this is a storage failure:
   `503 attachment_storage_unavailable`, `details.reason = "stored_bytes_changed"`, and the attempt's key is
   deleted first.
4. **The client's claim is compared separately**: if `X-Content-SHA256` was sent and differs from the
   recomputed digest, the failure code is **`attachment_hash_mismatch`** (`409`), the object is deleted
   first, and no `ready` row exists afterwards. The row stays `pending`, so a correct retry can still
   complete it.

**Never trusted**: ETag, `HEAD`/`stat_object`, `Content-Length`, or the client's own digest. An ETag is not
necessarily a content digest (multipart uploads, server-side encryption and replication all change it), and
even when it happens to be exact it is the store reporting on itself — which is exactly the claim the
re-read exists to check.

The price is honest: verification costs roughly one extra full read of every object. That is what "`ready`
means the digest was independently verified" costs.

The database enforces the same invariant rather than trusting application code:
`CHECK ((state = 'ready') = (content_hash IS NOT NULL))` — a `ready` row without a digest, or a `pending`
row with one, is impossible.

## 6. Not a kernel entity

An attachment is **not** a sync-kernel entity. Concretely:

- no `change_events` row is written, and no `op_ledger` row;
- no `seq` is allocated; `workspaces.last_seq` does not move because of an upload;
- it does not appear in `pull`, in `snapshot`, or in `tree`;
- the table has no `rev`, no `updated_seq`, no `note_id` and no `seq` column.

The only reference between a note and an attachment is Markdown in the note body — a relative URL such as
`![pixel](/api/v1/attachments/{id}/content)`. That keeps the Phase 3 protocol untouched: the note is an
ordinary note with ordinary text, uploaded through the ordinary push path, and the sync kernel never learns
that attachments exist.

Because there is no `note_id`, there is also no "unreferenced object" determination: the server cannot know
whether a `ready` attachment is still referenced by any note, which is one reason `ready` rows are never
collected automatically.

## 7. Authorization

Reads use **one** statement that both fetches the row and authorises it:

```sql
SELECT a.…
  FROM attachments AS a
  JOIN workspace_members AS m
    ON m.workspace_id = a.workspace_id
   AND m.user_id = :user_id
 WHERE a.id = :attachment_id
   AND a.state = 'ready'
```

That join **is** the authorisation check: a row the caller is not a member of simply does not come back.
Consequently:

- a non-member,
- a non-existent id,
- an id in a workspace the caller does not belong to, and
- a row that is `pending` or `deleted`

all produce the single answer `404 not_found`. Existence is not observable, so an id cannot be probed.

Writes use `require_workspace_member` on the path workspace, which likewise collapses "workspace does not
exist" and "not a member" into `not_found`; the service then re-checks `(workspace_id, attachment_id)`
together, so a caller who *is* a member of a different workspace still cannot touch this attachment.

`401` and `404` are never mixed: `401` means "sign in", `404` means "not yours".

## 8. MIME policy

Default allowlist (`ACCEPTED_MIME_DEFAULT`, also the default of `Settings.attachment_accepted_mime`):

```
image/png
image/jpeg
image/webp
image/gif
application/pdf
text/plain
```

`image/svg+xml` is rejected with **three independent locks**:

1. it is not in the allowlist;
2. `_require_supported_mime` checks it explicitly with its own reason label (`details.reason = "mime_svg"`),
   so the rule is visible in code rather than inferred from an editable configuration list;
3. `Settings` refuses to start if `ATTACHMENT_ACCEPTED_MIME` contains it — rejected **at startup**, not
   only per request.

The reason: an SVG is a document that can carry script, and these bytes would be served from our own
origin.

**Declared-MIME validation at `POST`** (`create`) normalises first (lowercase, drop everything after `;`),
then checks, in order: non-empty; UTF-8 length 3–127 (mirrors `ck_attachments_mime_len`); shape (contains
`/`, does not start or end with `/`); not `image/svg+xml`; and finally membership in the allowlist. Any
failure is `400 attachment_unsupported_type` with a fixed `details.reason`.

**Upload MIME validation at `PUT`** is a separate rule: the request's `Content-Type`, normalised the same
way, must **equal** the `mime` declared at `POST`. Parameters such as `; charset=utf-8` are ignored because
they describe an encoding of the body, not a different file type. `image/svg+xml` is refused with
`reason = "mime_svg"`; a missing header is `content_type_missing`; any other difference is
`content_type_mismatch`.

The stored MIME is the value the uploader declared and the server validated — which is precisely why
downloads set `X-Content-Type-Options: nosniff` and a sandboxing `Content-Security-Policy`.

## 9. Capability discovery

`GET /api/v1/capabilities` is the only discovery entry point. Its shape is fixed:

```
{attachments: {enabled, max_bytes, accepted_mime}}
```

- It is **unauthenticated** because a client needs the answer before it holds a token (the editor decides
  whether to render an attach button when the workspace screen mounts), and because the endpoint exposes no
  workspace data — only the deployment's own limits.
- It is **always `200`**, even when attachments are disabled: a disabled deployment reports
  `enabled: false` together with its real limits, so a client can say "this server does not accept
  attachments" instead of "unknown".
- It sets `Cache-Control: no-store`, so a client behind a cache cannot be held to an answer from before an
  operator flipped the switch.

**Degrading against an older server.** A newer client must treat "capability discovery is unavailable" as
"attachments are off":

- a server that predates Phase 5 answers **`404`** for `/api/v1/capabilities` (the route does not exist);
- the Web client (`apps/web/src/api/attachments.ts`) catches *any* failure of that call — `404`, network
  error, undecodable body — and returns the frozen `ATTACHMENTS_DISABLED_CAPABILITIES`
  (`enabled: false`, `maxBytes: 0`, `acceptedMime: []`). It never throws at the application shell;
- the decision to hide the entry point lives in `canUploadNow` (`apps/web/src/workspace/attachmentPorts.ts`),
  which reports the first applicable reason in order: `no_workspace` → `read_only` → `offline` →
  `disabled` → `no_storage`, each with its own message.

The consequence is a client that hides the attach button and explains why, rather than one that fails when
the user tries to upload.

**Default off.** `ATTACHMENTS_ENABLED` defaults to `false`: a deployment with no object storage configured
must not *look* like it supports attachments.

## 10. Limitations

Most of these are scope decisions. The concurrent-upload entry is not: it is a reproduced data-loss defect
that is still open, and it is listed here because a reader of this protocol needs to know it.

- **No presigned URLs.** Both upload and download are proxied by the API; the browser never talks to the
  object store. The cost is that the API carries the bandwidth and the hashing CPU. The benefit is that
  "a presigned `PUT` can overwrite", "bucket CORS", and "internal vs. public address divergence" are not
  failure modes of this protocol.
- **No dedupe.** Uploading the same bytes twice produces two objects and two rows.
- **No attachment deletion.** There is no delete endpoint and no delete UI. Once `ready`, an object is not
  removed by this service — not by the API and not by the GC.
- **No `Range`, no resume, no part upload.** Downloads are always the full object in one `200`; an upload
  must succeed in a single request.
- **The GC collects only expired `pending` rows.** `python -m revnote_api.tools.attachments_gc`
  (`--dry-run`, `--limit N`, `--grace-seconds S`) is an operator command, not a background scheduler; it
  deletes the row first and then the object, re-checking `state='pending' AND created_at < now() - grace`
  in the deleting statement. `ready` rows are invisible to its scan by construction, so a stored attachment
  is never collected. Output is counts only — never a key, a file name or an id — and the exit code is `0`
  when nothing failed, `1` when at least one row or object could not be handled.
- **Concurrent uploads share one key, and the loser can destroy the winner's bytes.** As described in §4,
  the nonce is per-row rather than per-attempt, so two simultaneous `PUT`s to the same `attachment_id` write
  the same object key, and the loser's cleanup deletes that key. With identical payloads the result is
  benign — which is why the existing concurrency check passed. With **differing** payloads the loser can
  remove the object the winning row names, leaving a `ready` row with a digest and no bytes: every later
  `GET …/content` answers `503` with `details.reason = "object_missing"`. This was reproduced against real
  PostgreSQL and a real S3-compatible store — see [`phase5-validation.md`](phase5-validation.md) §B1, where 11 of 12 rounds
  lost the bytes. A per-attempt nonce, or a cleanup scoped to the losing attempt's own bytes, is the fix;
  **neither is implemented here**, so concurrent uploads to one attachment must be treated as unsafe until
  it is.
- **No rate limiting suitable for public deployment.** `RATE_LIMIT_ENABLED` is a declared placeholder
  (default `false`) and nothing reads it; `429 rate_limited` exists in the error vocabulary but has no
  executing path. The release grade remains a controlled preview.
- **Attachments are not part of the sync stream.** No attachment events, no `pull`/`snapshot` membership,
  no `last_seq` movement; Markdown in the note body is the only reference (§6).
- **No offline attachment.** Binary content is not queued in IndexedDB; the upload entry point is disabled
  with an explicit reason when the client is offline.
