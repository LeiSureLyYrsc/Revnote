# Stack Blueprint — FastAPI + SQLAlchemy Async Sync Kernel

> **历史记录 / historical record.** 本文撰写时的 S3 后端为 MinIO。Phase 5 已迁移到
> **RustFS 1.0.4**（官方 GitHub release，固定版本与 SHA-256），原因是 MinIO 官方镜像已下架
> （registry 401 / Hub 404 / 下载站 410，实测见 `docs/phase5-validation.md`）。
> 本文正文保留当时原样，未做改写。


**Locked stack:** Python 3.12 · FastAPI · uv · SQLAlchemy 2.x async · PostgreSQL first (SQLite/MySQL reserved) · MinIO attachments.  
**Architecture rule:** sync **kernel** owns correctness; Joplin Server protocol lives only in `adapters/joplin/`.  
**Baseline compatibility:** Joplin client v3.7.x via adapter (see `joplin-protocol-notes.md`).

---

## 1. uv project layout

```
note-sync/
├── pyproject.toml              # uv; requires-python >=3.12
├── uv.lock
├── README.md
├── .env.example
├── alembic.ini
├── migrations/
│   ├── env.py                  # async Alembic
│   └── versions/
├── src/
│   └── notesync/
│       ├── __init__.py
│       ├── main.py             # FastAPI app factory
│       ├── config.py           # pydantic-settings
│       ├── db/
│       │   ├── __init__.py
│       │   ├── engine.py       # create_async_engine + sessionmaker
│       │   ├── base.py         # DeclarativeBase
│       │   ├── dialects.py     # PG/SQLite/MySQL type & FOR UPDATE helpers
│       │   └── session.py      # get_db Depends
│       ├── models/
│       │   ├── __init__.py
│       │   ├── user.py
│       │   ├── workspace.py
│       │   ├── note.py
│       │   ├── attachment.py
│       │   ├── change_event.py
│       │   ├── processed_operation.py
│       │   ├── tombstone.py
│       │   └── sync_lock.py    # optional; Joplin locks can stay adapter-local
│       ├── schemas/            # Pydantic request/response DTOs
│       │   ├── notes.py
│       │   ├── sync.py
│       │   └── attachments.py
│       ├── kernel/             # ★ sync correctness — no Joplin imports
│       │   ├── __init__.py
│       │   ├── rev.py          # conditional rev compare & bump
│       │   ├── seq.py          # workspace FOR UPDATE seq allocation
│       │   ├── outbox.py       # client op ingestion / idempotency
│       │   ├── merge.py        # 3-way merge (base/local/remote)
│       │   ├── tombstones.py
│       │   └── events.py       # append change_events after mutation
│       ├── services/
│       │   ├── notes.py
│       │   ├── attachments.py  # pending→ready state machine + MinIO
│       │   └── auth.py
│       ├── api/                # native open client API
│       │   ├── __init__.py
│       │   ├── deps.py
│       │   ├── v1/
│       │   │   ├── notes.py
│       │   │   ├── sync.py
│       │   │   ├── attachments.py
│       │   │   └── auth.py
│       │   └── router.py
│       ├── adapters/
│       │   └── joplin/         # ★ protocol only — maps to kernel
│       │       ├── __init__.py
│       │       ├── routes.py   # /api/items, /api/locks, delta
│       │       ├── serialize.py
│       │       ├── delta.py    # page compression → Joplin event shapes
│       │       └── locks.py
│       ├── storage/
│       │   ├── minio.py        # presign put/get; no distributed TX
│       │   └── paths.py
│       └── web/                # optional: serve SPA later
│           └── static.py
├── tests/
│   ├── conftest.py             # async PG fixture (testcontainers or local)
│   ├── kernel/
│   │   ├── test_rev.py
│   │   ├── test_seq_visibility.py
│   │   ├── test_idempotency.py
│   │   └── test_merge.py
│   ├── api/
│   └── adapters/joplin/
└── scripts/
    ├── seed_dev.py
    └── check_joplin_compat.py
```

`pyproject.toml` extras sketch: `asyncpg`, `aiosqlite` (dev/reserved), `aiomysql` (reserved), `sqlalchemy[asyncio]`, `alembic`, `boto3`/`aiobotocore` for MinIO, `python-multipart`, auth libs for OIDC/Passkey later.

---

## 2. Key SQLAlchemy models (fields only)

Dialect note: use `BigInteger` + app-allocated `seq` for change streams. Avoid relying on PG `SERIAL`/`IDENTITY` visibility across concurrent commits for cursor semantics (`dialects.py` may map differently per backend, but **allocation algorithm** stays in `kernel/seq.py`).

### User
- `id: UUID` PK  
- `email: str` unique  
- `password_hash: str | None`  # Basic auth; null if OIDC-only  
- `oidc_sub: str | None` unique  
- `webauthn_user_handle: bytes | None`  
- `created_at`, `updated_at`

### Workspace
- `id: UUID` PK  
- `owner_user_id: UUID` FK → users  
- `name: str`  
- `change_seq: BigInteger`  # last allocated seq; bumped under `SELECT … FOR UPDATE`  
- `created_at`, `updated_at`

### Note
- `id: UUID` PK  # our canonical id; Joplin `jop_id` stored separately when adapted  
- `workspace_id: UUID` FK  
- `parent_id: UUID | None`  # notebook / folder  
- `title: str`  
- `body_md: Text`  # Markdown + LaTeX source  
- `rev: BigInteger`  # optimistic concurrency; starts at 1  
- `deleted_at: DateTime | None`  # soft delete mirrors tombstone for queries  
- `external_joplin_id: str | None` indexed  # adapter mapping  
- `created_at`, `updated_at`, `user_updated_at`

### Tombstone
- `id: UUID` PK  
- `workspace_id: UUID`  
- `entity_type: str`  # note | attachment | folder  
- `entity_id: UUID`  
- `deleted_at: DateTime`  
- `change_seq: BigInteger`  # seq of the delete event  
- unique `(workspace_id, entity_type, entity_id)`

### ChangeEvent
- `id: UUID` PK  
- `workspace_id: UUID` indexed  
- `seq: BigInteger`  # strictly increasing **per workspace**; unique `(workspace_id, seq)`  
- `entity_type: str`  
- `entity_id: UUID`  
- `op: str`  # create | update | delete  
- `rev_after: BigInteger | None`  
- `payload_meta: JSON`  # title snippet, parent_id, content hash — not full body if large  
- `actor_user_id: UUID | None`  
- `created_at`  
- Index: `(workspace_id, seq)` for cursor scans

### ProcessedOperation (idempotency)
- `id: UUID` PK  
- `workspace_id: UUID`  
- `op_id: UUID`  # client-generated; unique `(workspace_id, op_id)`  
- `result_entity_id: UUID | None`  
- `result_rev: BigInteger | None`  
- `status: str`  # applied | conflict | rejected  
- `response_snapshot: JSON`  # replay identical HTTP outcome  
- `created_at`

### Attachment
- `id: UUID` PK  
- `workspace_id: UUID`  
- `note_id: UUID | None`  
- `content_hash: str`  # sha256; immutability key  
- `byte_size: BigInteger`  
- `mime: str`  
- `state: str`  # pending | ready | orphaned | deleted  
- `storage_key: str`  # MinIO object key (hash-addressed)  
- `rev: BigInteger`  
- `created_at`, `ready_at`, `deleted_at`  
- unique `(workspace_id, content_hash)` where state != deleted (partial index on PG)

### SyncLock (native optional; Joplin adapter may own its table)
- `id: str` PK  # `{type}_{clientType}_{clientId}`  
- `workspace_id: UUID`  
- `lock_type: str`  # sync | exclusive  
- `client_type: str`  
- `client_id: str`  
- `updated_at: DateTime`  # refresh heartbeat

---

## 3. Kernel algorithms (concrete)

### Conditional rev write
```
BEGIN;
  SELECT rev FROM notes WHERE id=:id FOR UPDATE;
  IF row.rev != client_base_rev → CONFLICT (return server note + remote rev);
  UPDATE notes SET …, rev = rev + 1;
  seq = allocate_seq(workspace_id);   -- see below
  INSERT change_events (…, seq, op='update', rev_after=new_rev);
  UPSERT processed_operations (op_id → snapshot);
COMMIT;
```

### Workspace seq allocation (avoids PG SERIAL visibility trap)
```
SELECT change_seq FROM workspaces WHERE id=:ws FOR UPDATE;
next = change_seq + 1;
UPDATE workspaces SET change_seq = next;
-- use `next` only inside this same transaction when inserting change_events
```
Readers of delta use `WHERE workspace_id=:ws AND seq > :cursor_seq ORDER BY seq`. Because allocation and event insert share one TX holding the workspace row lock, a committed event is never invisible to a cursor that already observed a higher seq from another session.

### Attachment state machine
1. Client requests upload → row `state=pending`, MinIO **presigned PUT** to hash key.  
2. Client confirms → server HEAD/object exists + hash matches → `state=ready`, emit change_event.  
3. Never mutate bytes in place: new hash = new object + new row (or new version row).  
4. Delete → tombstone + `state=deleted`; GC job removes orphan MinIO keys later.  
5. **No** 2PC across PG and MinIO: pending rows older than TTL → orphaned → cleanup.

### Adapter boundary
- `adapters/joplin/routes.py` may only call `kernel.*` and `services.*`.  
- Kernel never imports `adapters`.  
- Joplin delta compression / `resyncRequired` / path `root:/…:` stay in adapter.  
- Native API speaks `rev` + `op_id` + `cursor=seq`; Joplin API translates.

---

## 4. Three critical API endpoint shapes

### A) Native: conditional note write (kernel path)
```
PUT /v1/workspaces/{workspace_id}/notes/{note_id}
Headers:
  Authorization: Bearer … | Basic …
  Idempotency-Key: <op_id UUID>          # → processed_operations.op_id
Body:
  {
    "base_rev": 12,
    "title": "…",
    "body_md": "… $E=mc^2$ …",
    "parent_id": null
  }
Responses:
  200 { "note_id", "rev": 13, "updated_at", "op_id" }          # applied
  200 { … same as first … }                                    # idempotent replay
  409 {
    "code": "rev_conflict",
    "server": { "note_id", "rev", "title", "body_md", "parent_id" },
    "client_base_rev": 12
  }
```
Server runs 3-way merge **only when client sends optional `merge_base_rev` + intends auto-merge**; MVP default is **detect conflict → 409**, client merges.

### B) Native: delta / change cursor
```
GET /v1/workspaces/{workspace_id}/changes?cursor={seq}&limit=100
Response 200:
  {
    "items": [
      {
        "seq": 1042,
        "entity_type": "note",
        "entity_id": "…",
        "op": "update",
        "rev_after": 13,
        "meta": { "parent_id": "…", "title": "…" }
      }
    ],
    "cursor": 1042,          # last seq in page (resume token)
    "has_more": true
  }
Error 410:
  { "code": "resync_required", "reason": "cursor_unknown_or_expired" }
```

### C) Attachments: presign + confirm (MinIO state machine)
```
POST /v1/workspaces/{workspace_id}/attachments
Body: { "content_hash": "sha256:…", "byte_size": 12345, "mime": "image/png", "note_id": "…" }
Response 201:
  {
    "attachment_id": "…",
    "state": "pending",
    "upload": { "url": "https://minio/…", "method": "PUT", "headers": {…} },
    "expires_at": "…"
  }

POST /v1/workspaces/{workspace_id}/attachments/{attachment_id}/complete
Response 200: { "attachment_id", "state": "ready", "rev", "change_seq" }
Response 409: { "code": "hash_mismatch" | "object_missing" }
```

Joplin adapter maps these to `PUT /api/items…/content`, `GET …/delta`, `POST /api/locks` without changing kernel tables.

---

## 5. Multi-DB stance in this blueprint

- **v1 ship target: PostgreSQL only** (asyncpg).  
- SQLAlchemy models stay mostly dialect-agnostic; `dialects.py` isolates `FOR UPDATE`, partial indexes, JSON types.  
- SQLite/MySQL engines may boot for smoke tests, but **seq + FOR UPDATE + partial unique indexes** are certified on PG first.  
- Do not block MVP on multi-DB green CI for sync invariants.

---

## 6. Auth (MVP sketch, not blocking kernel)

- Basic Auth against custom login page (not browser-native popup alone).  
- OIDC authorization-code + sessions.  
- Passkey/WebAuthn as progressive enhancement.  
- Joplin clients typically use email/password session against Server — adapter exposes compatible session endpoints as needed.
