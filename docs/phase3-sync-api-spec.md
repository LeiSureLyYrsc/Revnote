# Revnote Phase 3 HTTP API v1（Build终稿）

以[计划](plan-phase3.md)§2和Build prompt锁定表为准；本规范替代历史草案的JWT、分页快照、upsert、superseded_by_seq方案。

## 1. 通用

业务前缀/api/v1；健康/与/health保留。JSON；UUID字符串，seq/rev/cursor/last_seq为十进制字符串，最大9223372036854775807；UTC RFC3339时间。Authorization: Bearer opaque-access；合法X-Request-ID（ASCII [A-Za-z0-9._-]，1–64）原样回传，非法替换。错误固定如下，禁止input/ctx/原始异常/SQL/秘密回显：

```json
{"error":{"code":"not_found","message":"Resource not found.","request_id":"request-id","retryable":false,"details":{}}}
```

密码、token/hash、Authorization、正文、原始query禁止出现在repr/日志/错误；成功token与授权正文读取响应除外。

## 2. 认证

| 方法/路径 | 输入 | 成功 |
|---|---|---|
| POST /auth/register | email,password | 201 user+workspace，不登录 |
| POST /auth/login | email,password,可选device_name | 200 token pair |
| POST /auth/refresh | refresh_token | 200新token pair |
| POST /auth/logout | Bearer access | 204撤销当前session |
| GET /me | Bearer access | 200 user+workspaces |

公开user含id/email/created_at；workspace至少id/role。token pair含access_token/refresh_token/token_type="Bearer"/expires_in=600，可含session_id/session_expires_at；Cache-Control:no-store。随机token至少256bit，仅存SHA-256。session即family，绝对30d，不续期。refresh先锁session再锁token，active→used+新pair原子提交；used复用→提交session撤销后401 token_reused，全部新旧token失效，无宽限。

注册默认关闭403 registration_closed；开放则原子建user/workspace/owner。ASCII邮箱lower(btrim(email))唯一；密码8–256字符、UTF-8≤1024bytes，不trim/截断。Argon2id 65536KiB/time3/parallelism4，登录check_needs_rehash，线程内受限并发运行。旧hash=NULL不可登录。不存在/disabled/hash=NULL/密码错统一401 invalid_credentials，dummy verify防明显时序侧信道。无效access401，workspace缺失/非成员统一404。

## 3. Push

POST /workspaces/{workspace_id}/sync/push。1–50ops，原始JSON≤2MiB，HTTP body_md≤256KiB UTF-8。schema与重复规范化op_id整批预校验，非法400零执行。路径workspace权威，不允许op另带workspace。

```json
{"ops":[{"op_id":"01900000-0000-7000-8000-000000000001","entity_type":"note","op":"create","entity_id":"01900000-0000-7000-8000-000000000002","payload":{"title":"Example","body_md":"Text","folder_id":null}}]}
```

| entity_type/op | payload | base_rev |
|---|---|---|
| folder/create | name,parent_id缺省null | 无/null |
| folder/update | name | 正十进制字符串 |
| folder/move | parent_id必须提供可null | 同上 |
| folder/delete | {} | 同上 |
| note/create | title,body_md,folder_id缺省null | 无/null |
| note/update | title,body_md | 同上 |
| note/move | folder_id必须提供可null | 同上 |
| note/delete | {} | 同上 |

禁止未知字段、upsert。按数组顺序，每op重查member、独立事务。execute_with_outcome决定applied/replayed，HTTP不预查ledger。响应200 {results:[...]}，每项op_id/status，成功seq/rev及安全结果元数据；失败error使用通用词表。

六状态：applied首次提交；replayed相同op_id+payload首次结果；conflict版本/删除/ID冲突继续；rejected业务拒绝继续，write_busy/storage_failure则停止；unknown/outcome_unknown停止；后缀not_attempted。30s预算仅op间检查。EntityIdConflict与OpIdConflict统一conflict固定消息空details；跨workspace同op_id合法。57014/55P03确认失败write_busy；COMMIT确认丢失unknown。取消上抛，不shield COMMIT，可疑连接invalidate；断开不意味着回滚，客户端同op_id+payload重试。

## 4. Pull

GET /workspaces/{workspace_id}/sync/pull?since=0&limit=100。since排他，默认0，ASCII十进制BIGINT；limit默认100、1–500。响应events（完整连续页，含seq/entity_type/entity_id/op/rev/op_id/created_at）、objects（页内按entity_type+id去重当前对象）、next_cursor/has_more/last_seq/min_retained_seq。对象当前版本可领先事件，以真实rev/updated_seq返回。无事件next_cursor=since。

一条SQL同快照读取水位、事件、对象，不压缩事件历史。约4MiB预算内缩短连续事件前缀；单事件+对象放不下明确413 payload_too_large，不跳过事件。floor数据库默认1；since<min_retained_seq-1→410 cursor_expired（details带min_retained_seq/action=resync）；since>last_seq→400 cursor_ahead。

对象含entity_type/id/workspace_id、parent_id或folder_id、name或title/body_md、rev/updated_seq、deleted_at/created_at/updated_at，可附deleted布尔。删除事件保留，对象可为tombstone。

## 5. Snapshot/只读

GET /workspaces/{workspace_id}/sync/snapshot：一个REPEATABLE READ READ ONLY事务，返回as_of_seq/min_retained_seq/objects（全部活跃folders/notes含正文），无分页、拒绝cursor/limit。默认10,000对象/16MiB完整JSON；同事务先count/原文bytes预检避免超大物化，再按JSON真实字节检测，超限整次409 snapshot_too_large，绝不半成功。随后pull since=as_of_seq。

GET /workspaces/{workspace_id}/tree：活跃folders/notes元数据，无正文。GET /workspaces/{workspace_id}/notes/{note_id}：指定workspace活跃笔记含正文/rev，缺失/已删404。无REST写包装/成员管理/session列表额外范围。

## 6. 错误码

| code | HTTP或push status | retryable |
|---|---|---|
| invalid_request / cursor_ahead | 400 | false |
| unauthenticated / invalid_credentials / token_reused | 401 | false |
| registration_closed | 403 | false |
| not_found | 404 | false |
| conflict / snapshot_too_large | 409（push conflict） | false |
| cursor_expired | 410 | false |
| payload_too_large | 413 | false |
| rev_conflict / entity_deleted | push conflict | false |
| invalid_parent / folder_cycle / folder_not_empty / counter_overflow | push rejected | false |
| write_busy | 503 / push rejected停止 | true |
| outcome_unknown | 503 / push unknown停止 | true，同op_id |
| storage_failure | 500 / push rejected停止 | true |
| internal | 500 | false |

## 7. 配置/验收

AUTH_REGISTRATION_OPEN=false、AUTH_PASSWORD_MIN_CHARS=8、AUTH_PASSWORD_MAX_CHARS=256、AUTH_ACCESS_TTL_S=600、AUTH_SESSION_TTL_S=2592000、ARGON2_MEMORY_COST=65536、ARGON2_TIME_COST=3、ARGON2_PARALLELISM=4、AUTH_HASH_MAX_CONCURRENCY=2；SYNC_PUSH_MAX_OPS=50、SYNC_PUSH_MAX_BYTES=2097152、SYNC_NOTE_MAX_BYTES=262144、SYNC_PUSH_BUDGET_MS=30000、SYNC_PULL_DEFAULT_LIMIT=100、SYNC_PULL_MAX_LIMIT=500、SYNC_PULL_MAX_BYTES=4194304、SYNC_SNAPSHOT_MAX_OBJECTS=10000、SYNC_SNAPSHOT_MAX_BYTES=16777216；DB_LOCK_TIMEOUT_MS=5000、DB_STATEMENT_TIMEOUT_MS=10000；CORS_ALLOW_ORIGINS=[]、RATE_LIMIT_ENABLED=false（仅占位）。配置名称与.env.example保持同步。

具体schema与认证状态机见计划§9–10，所有权§13，验证§14。真实PG/多连接/uvicorn socket承重测试，0skip，秘密哨兵，保留旧内核责任。六提交、本地全绿、一次push、gh run watch --exit-status等CI绿；不得本机build镜像、触碰Clash或改前端业务。
