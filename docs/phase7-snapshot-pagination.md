# Phase 7：Snapshot 分页 bootstrap

> 状态：已实现（Phase 7 Build）。本文件描述加法协议与一致性边界；Phase 3 的无参 snapshot 契约保持不变，修订说明见 `docs/phase3-sync-api-spec.md` 顶部与 §5。

## 1. 目标

Phase 3 的 `GET /workspaces/{id}/sync/snapshot` 是单次 `REPEATABLE READ, READ ONLY` 全量读：默认上限 10,000 对象 / 16 MiB，超限整次 `409 snapshot_too_large`。大工作区因此无法 bootstrap。

Phase 7 增加**带游标的分页读取**：客户端在同一逻辑水位 `as_of_seq` 下拉完所有活跃对象，再 `pull since=as_of_seq` 追上增量。无 `limit`/`cursor` 的请求仍走 Phase 3 原路径，响应形状与错误行为不变。

## 2. 协议

### 2.1 模式选择

| 请求 | 模式 | 行为 |
|---|---|---|
| `GET .../sync/snapshot` | 遗留全量 | 与 Phase 3 完全一致；超限 `409 snapshot_too_large` |
| `GET .../sync/snapshot?limit=N` | 分页首页 | 钉住 `as_of_seq`，返回一页 |
| `GET .../sync/snapshot?limit=N&cursor=C` | 分页续页 | 按游标继续同一水位 |
| `GET .../sync/snapshot?cursor=C` | 分页续页 | `limit` 缺省为部署的单页对象上限 |

`offset`、`page`、`since` 仍是 `400 invalid_request`（`details.fields[].reason=not_supported`），不会被静默忽略。`limit` 必须为 1 到 `SYNC_SNAPSHOT_PAGE_MAX_OBJECTS`；超过是 `400 invalid_request`。

### 2.2 分页响应

```json
{
  "as_of_seq": "123",
  "min_retained_seq": "1",
  "objects": [ ... FolderObject / NoteObject ... ],
  "next_cursor": "eyJ...signature",
  "has_more": true
}
```

- `as_of_seq` 由首页钉住；续页必须返回同一 `as_of_seq`。`min_retained_seq` 是**当前**保留下限，bootstrap 期间可能前进；若钉住的水位已落后于它，续页返回 `410 cursor_expired`。
- `next_cursor` 是**不透明、签名**的字符串；客户端不得解析。`has_more=true` 时必非空，`has_more=false` 时为 `null`。显式传入空字符串游标按 `400 invalid_cursor` 处理，不会悄悄回到首页。
- 空页只能作为终止页；**禁止空页 + `has_more=true`**。
- 每页对象按稳定全序 `(folder, id)`、然后 `(note, id)` 排列；对象不会跨页重复。
- 单页对象数上限 `SYNC_SNAPSHOT_PAGE_MAX_OBJECTS`（默认 1,000），序列化字节上限 `SYNC_SNAPSHOT_PAGE_MAX_BYTES`（默认 4 MiB）。
- 整个 bootstrap 的对象数硬顶 `SYNC_SNAPSHOT_MAX_TOTAL_OBJECTS`（默认 100,000）。首页在读取对象前计数，超顶整次 `409 snapshot_total_too_large`，不静默截断。

### 2.3 错误码（加法）

| code | HTTP | 含义 |
|---|---|---|
| `invalid_cursor` | 400 | 游标格式/签名错误、来自其他 workspace、或不是本部署签发 |
| `cursor_ahead` | 400 | 游标内嵌水位高于当前 `last_seq` |
| `cursor_expired` | 410 | 游标水位低于保留下限；`details.action=resync` |
| `snapshot_page_too_large` | 413 | 单个对象 + JSON 信封装不进一页；不跳过该对象 |
| `snapshot_total_too_large` | 409 | 超过跨页对象硬顶 |
| `snapshot_too_large` | 409 | 仅无参遗留全量路径：超过 10,000 对象 / 16 MiB |

`snapshot_page_too_large` 与 `snapshot_total_too_large` 分开，避免一个 code 同时表示“单页装不下”和“整个 bootstrap 太大”。`snapshot_too_large` 不再用于分页路径。

## 3. 一致性模型

### 3.1 首页

1. 在 `REPEATABLE READ, READ ONLY` 事务中读取 `last_seq` / `min_retained_seq` / 活跃对象数。
2. 检查 `SYNC_SNAPSHOT_MAX_TOTAL_OBJECTS`。
3. 将 `as_of_seq` 钉为首页视图的 `last_seq`。
4. 按稳定序读取 `limit+1` 行（`+1` 是 `has_more` 探针），按连续字节前缀裁剪，返回第一页。
5. 回滚只读事务（没有东西可提交）。

### 3.2 续页

每个续页各自开一个 `REPEATABLE READ, READ ONLY` 事务，读取当前水位并校验游标，然后只返回满足以下条件的行：

```sql
deleted_at IS NULL
AND updated_seq <= as_of_seq
AND (kind, id) > (cursor.kind, cursor.id)
```

不跨 HTTP 请求保持同一个数据库事务（D4/D13）。服务端没有游标租约表，因此本阶段**没有新迁移**。

### 3.3 水位隔离的准确含义

- 首页之后**新建**的对象 `updated_seq > as_of_seq`，不会出现在任何已钉页；它们由后续 `pull since=as_of_seq` 交付。
- 首页之后**更新/删除**的对象也不会进入后续页；它们的更新/删除事件由 pull 交付。已在前页交付的对象若之后被更新，pull 会再次交付新状态。
- 因此 `snapshot pages + pull since=as_of_seq` 是完整 bootstrap。分页快照本身**不做行级时间旅行**：第 2 页返回的是当前行内容，只保证“当前状态生效于钉住水位之前且仍活跃”。这是无状态游标、不跨请求长事务的必然边界，已在测试与文档中明确。
- 首页 `as_of_seq` 与后续 `pull since=as_of_seq` 之间没有空洞：所有 `seq > as_of_seq` 的事件都在 pull 中。

### 3.4 游标

游标 payload（规范 JSON）包含：

```json
{"v":1,"w":"<workspace uuid>","a":"<as_of_seq>","t":"folder|note","i":"<last object uuid>"}
```

编码为 `base64url(payload) + "." + base64url(HMAC-SHA256)`。签名 key 由部署 DSN 经域分隔 SHA-256 派生：

```text
sha256("revnote.sync.snapshot.cursor.v1\0" + database_url)
```

- 同一部署的所有 worker 得到同一 key，游标可跨进程续页；换 DSN 后旧游标自然失效并返回 `invalid_cursor`，客户端重新 bootstrap。
- 游标绑定 workspace；把 A 的游标用于 B 是 `invalid_cursor`。
- 游标不是授权令牌：每个请求仍要 Bearer token + workspace membership，SQL 也始终按 `workspace_id` 限定。
- 游标内容与签名 key 都不写入日志、错误、progress 或提交信息。

## 4. Capabilities（加法）

`GET /api/v1/capabilities` 新增：

```json
{
  "snapshot_pagination": {
    "enabled": true,
    "page_max_objects": 1000,
    "page_max_bytes": 4194304,
    "max_total_objects": 100000
  }
}
```

旧客户端忽略未知字段；新客户端据此决定是否走分页。未声明该字段的旧服务端仍按遗留单次 snapshot 工作。

## 5. Web 客户端

`apps/web/src/api/sync.ts` 的 transport 在首次 bootstrap 时探测一次 capabilities：

- `snapshot_pagination.enabled=true`：以 `page_max_objects`（客户端再封顶 1,000）循环请求 `limit`/`cursor`，校验每页 `as_of_seq` 一致、对象活跃且 `updated_seq <= as_of_seq`、无跨页重复，最后合并为一个 `SnapshotResponse` 交给既有 `activateSnapshot`。最多 1,000 页，超出即失败而不是无限循环。
- capability 缺失 / `enabled=false` / 旧服务端 404：回退到遗留单次 snapshot。
- 每次分页请求都重新取 token 并检查写租约；账号切换不会跨身份复用分页结果。
- 合并后的对象集在一个原子 IndexedDB 事务里激活；失败保留本地状态，不产生半成品 bootstrap。

## 6. 明确不做

- 不跨请求长持数据库事务，不做 `pg_export_snapshot`。
- 不新增快照租约表 / 迁移 0006。
- 不做行级历史内容时间旅行；并发更新由 pull 补齐。
- 不把附件放进 `change_events` / snapshot / pull。
- 不改变 push/pull/tree/note 语义；不改变 Joplin 适配器。
- 不做自动合并、参考 CLI、公网加固、MinIO。

## 7. 相关文件

- 服务：`apps/api/src/revnote_api/services/sync_read.py`
- 路由：`apps/api/src/revnote_api/api/routes/sync_pull.py`
- 响应模型：`apps/api/src/revnote_api/api/schemas/sync_pull.py`
- 配置：`apps/api/src/revnote_api/config.py`、`.env.example`
- Capabilities：`apps/api/src/revnote_api/api/schemas/attachments.py`、`api/routes/attachments.py`
- Web：`apps/web/src/api/sync.ts`、`apps/web/src/api/contracts.ts`
- 真 PG 证据：`apps/api/tests/api/test_sync_reads.py`（Phase 7 分页用例）
- 浏览器证据：`apps/web/e2e/pagination.spec.ts`
