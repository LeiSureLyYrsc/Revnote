# Revnote Phase 3 实施计划：认证与原生同步 HTTP API v1（终稿）

> **状态：Build 终稿（已确认进入 Build，锁定决策执行版）。**
> 本文件恢复自本仓库先前 Build 提交 `bf8440d` 的最终计划记录，并写入本阶段**锁定决策**覆盖；未落库历史底稿不再作为规范依赖。
> 密码长度已覆盖为 **8–256**；快照已锁定为**不分页**；内核例外 `execute_with_outcome()` 已批准；其余按锁定表 D1–D22 执行。内核例外之外，Phase 2 内核不变量不得削弱。

## 1. 结论摘要

1. 保留 Phase 2 的事务、不变量和原 `execute()` 返回契约；HTTP 层不直接修改笔记表。
2. 采用 **不透明 access token（TTL 600s）+ 轮换 refresh token（会话绝对寿命 30 天）**，数据库只存 token 哈希。
3. 会话兼作设备登录实例与 refresh family；新增成员关系表，每次工作区请求鉴权，push 每个 op 前重新检查。
4. push 顺序处理、每 op 独立事务；业务冲突继续，暂时性故障或结果未知停止，剩余标 `not_attempted`。
5. pull 用**单条**查询读取水位、事件页和当前对象；保留全部事件、独立去重当前对象。
6. 沿用 `min_retained_seq=1`；过期条件是 **`since < min_retained_seq - 1`**。
7. snapshot 使用**单个** `REPEATABLE READ` 只读事务；本阶段**不分页**；默认上限 **10,000 对象 / 16 MiB**；超限整次 **409 `snapshot_too_large`**，不返回半快照。
8. 完成五项 P2 修正、真实 PG API/HTTP 并发测试、日志脱敏及 CI no-skip 验收。
9. 本轮**只**做 Phase 3 实施：`docs/plan-phase3.md` 已落盘；认证 + push/pull/snapshot/只读 + 横切 + Phase 2 P2 + PG 验收。

---

## 2. 锁定决策（不得再议）

| 项 | 锁定值 |
|---|---|
| **密码** | Argon2id；长度 **8–256** 字符（UTF-8 ≤1024 字节）；**覆盖**计划原 D7 的 15 下限。配置 `AUTH_PASSWORD_MIN_CHARS=8`、`AUTH_PASSWORD_MAX_CHARS=256` |
| **Snapshot** | 单一 `REPEATABLE READ` 只读事务，**不分页**；默认上限 **10,000 对象 / 16 MiB**；超限整次 **409 `snapshot_too_large`**，不返回半快照 |
| **内核例外** | 新增 `execute_with_outcome()`；**保留**现有 `execute()` 返回契约不变。**禁止**改动：payload 指纹/hash、seq 分配、CAS、删除语义、ledger 规则、一 op 一对象 |
| D1 | opaque access，SHA-256 查表，TTL **600s** |
| D2 | session 绝对寿命 **30d**；refresh 不延长会话 |
| D3 | refresh 复用 **R1**：吊销整个 family/session，提交撤销后 401 `token_reused` |
| D4 | `auth_sessions` = 设备登录 + token family |
| D5 | 建 `workspace_members`；v1 仅 owner；每请求校验，push **每 op 前**重查 |
| D6 | `AUTH_REGISTRATION_OPEN=false` 默认；开启时原子建 user+默认 workspace+owner member，不自动登录 |
| D7 | 密码覆盖为 8–256 字符、UTF-8 ≤1024 字节；Argon2id 64 MiB / time=3 / parallelism=4；不 trim/截断，登录 check_needs_rehash |
| D8 | ASCII 邮箱；`lower(btrim(email))` 唯一；旧账号 `password_hash=NULL` 不可本地登录 |
| D9 | push **1–50 op**，原始 JSON **≤2 MiB**；HTTP `body_md` **≤256 KiB** |
| D10 | 数组顺序；每 op **独立事务**；conflict/rejected **继续**；Busy / unknown / StorageFailure **停止**，后缀 `not_attempted` |
| D11 | 六状态：`applied/replayed/conflict/rejected/unknown/not_attempted` |
| D12 | 批内重复 `op_id` → 整批 **400**，**一个都不执行** |
| D13 | pull `since` **排他**下界，默认 `"0"`；非法/超 BIGINT → 400；`since > last_seq` → 400 `cursor_ahead` |
| D14 | pull：事件**全量保留** + 独立去重 `objects`；默认 limit 100、上限 500；响应预算约 4 MiB |
| D15 | Snapshot：单 RR 只读、不分页；10,000 对象/16 MiB，超限整次 409 snapshot_too_large（归档上方已锁定 Snapshot 决策） |
| D16 | `min_retained_seq` **保持默认 1**（不改为 0）；过期条件 `since < min_retained_seq - 1` → 410 |
| D17 | `lock_timeout` **5s**、`statement_timeout` **10s** → 确认失败映射 **WriteBusy**；push 批次预算 30s，仅在 op 之间检查 |
| D18 | **不**对 COMMIT 做 `CancelledError` shield；取消向上传播；疑似提交中连接 invalidate；客户端按 unknown + 同 `op_id` 重试 |
| D19 | **无** REST 写包装；写只走 push |
| D20 | 无效 access → 401；非成员或不存在 workspace → 统一 **404** |
| D21 | 错误信封固定；合法 `X-Request-ID` 原样回传，非法则**替换**为服务端 ID（ASCII `[A-Za-z0-9._-]`，1–64） |
| D22 | 见上「内核例外」 |

其余计划细节（迁移 0003 表结构、线格式、错误码表、refresh 状态机、文件路径）以本文件与 `docs/phase3-sync-api-spec.md` 为准；两者冲突时，以本文件的锁定决策（第 2 节）为最高优先级。

线格式与端点的落地参照顺序：

1. 本文件第 2 节锁定决策（最高优先级）
2. 本文件其余章节
3. `docs/phase3-sync-api-spec.md`（端点/字段级草案，用于线格式细节）
4. 用户附件 `phase3-build-prompt.md` / `phase3-build-conventions.md`；冲突以 Build prompt 锁定表为准。

---

## 3. 硬约束

- **只用 PostgreSQL**；禁止 SQLite 替代；**禁止 skip**（skip≠通过）；`REQUIRE_POSTGRES_TESTS=1`；CI 保留 **no-skip** 守卫。
- Phase 2 内核不变量不得削弱；既有 **476** 测试责任全绿（总数可因新增而增加）。
- 永不在 repr / 日志 / 错误响应中输出：密码、access/refresh **原文**、笔记 `body_md`。须有哨兵测试。
- **小 conventional commits**（计划 6 个）；本地全部验收通过后 **一次性 `git push`**；然后：
  ```powershell
  gh run list --repo LeiSureLyYrsc/Revnote --branch main --limit 5
  gh run watch <run-id> --repo LeiSureLyYrsc/Revnote --exit-status
  ```
  失败：`gh run view <run-id> --log-failed`；只追加修复提交，不强推、不改写已发布历史。
- **Murasame-PC**：禁止本机 `docker build`/`compose build`；本地测试用**仓库外**临时 PG（复用 Phase 2 生命周期控制器或等价工具），用完 `stop`；**绝不触碰 Clash**；不提交 `.env` 与凭据。
- 路由包拆分：现有 `api/routes.py` 与目录冲突——由 D 将健康路由迁到 `api/routes/health.py`，`api/router.py` 唯一注册入口（见计划 §4）。

---

## 4. 指挥官子任务与文件所有权

依赖：**D → A → B∥C → E**。共享文件指定唯一 owner；他人以补丁清单交给 owner。

| 子任务 | 职责 | 独占路径（按计划 §4，可按仓库实况微调但须说明） |
|---|---|---|
| **A 认证** | users 扩展、sessions/access/refresh、members；迁移 **0003**；Argon2id（min=**8**）；token 签发/轮换/R1；鉴权依赖 | `models/user.py`、`auth_*.py`、`workspace_member.py`、`security/`、`services/auth*.py`、`api/dependencies.py`、`api/schemas/auth.py`、`api/routes/auth.py`、`migrations/versions/0003_*` |
| **B push** | schema、逐 op 调内核、`execute_with_outcome`、结果映射、停止规则、P2 超时/取消/lock_wait | `api/routes/sync_push.py`、`api/schemas/sync_push.py`、`services/sync_push_adapter.py`；`services/sync_write.py` / `kernel/results.py` **仅**批准的适配与 P2 |
| **C pull/snapshot/只读** | 单语句 pull、RR snapshot、tree/note、`min_retained_seq` 路径 | `api/routes/sync_pull.py`、`api/routes/read.py`、`api/schemas/sync_pull.py`、`api/schemas/read.py`、`services/sync_read*.py` |
| **D 横切** | 信封、request id、配置、router、middleware、日志脱敏、CORS | `api/errors.py`、`api/middleware/`、`api/router.py`、`api/routes/health.py`、`api/schemas/common.py`、`config.py`、`main.py` |
| **E 测试/CI/文档** | API fixture、HTTP 并发、transport、no-skip、CI 分组、文档与 P2 文档项 | `tests/api/`、`tests/pg_support/`、`.github/workflows/`、`docs/`、`README.md` |

---

## 5. 必做 P2（五项）

1. **`SET LOCAL statement_timeout`**（及 lock_timeout）；SQLSTATE **57014** / **55P03** 在确认失败时 → **WriteBusy**（对外 `write_busy`、可重试）。连接/提交确认丢失仍走 **unknown**。
2. **WriteBusy 超时路径**也必须记录非零 **`lock_wait_ms`**（`try/finally`，含取消/WorkspaceNotFound 等异常分支）。
3. 对外错误**不得**区分跨工作区 ID 是否存在：EntityIdConflict/OpIdConflict → 统一 `conflict` 固定消息/结构（可忽略 request_id）；内部日志可区分但不含敏感内容。跨工作区同 `op_id` 本就合法，**勿**加全局唯一。
4. 文档按**真实写步骤顺序**更新（核对 `sync_write.py`）：事务 → 锁 workspace → 查 ledger → 业务/CAS → **推进 seq** → 写对象 → event → ledger → commit；并写明取消/超时/unknown 语义。
5. **HTTP 断开/取消**：处理中断开不伪造已回滚；COMMIT 期间取消 → 视为 **unknown**；客户端用**同一 `op_id` + 同 payload** 重试。不 shield 业务 COMMIT；可疑连接 invalidate，不归还连接池。

---

## 6. 提交计划（6 个）

| # | Conventional message | 内容要点 |
|---|---|---|
| 1 | `feat(api): define v1 contracts and harden write cancellation budgets` | 信封/request id/配置；outcome + P2；计划/规格落盘 |
| 2 | `feat(auth): add session token and owner membership schema` | 迁移 0003、回填、schema 测试 |
| 3 | `feat(auth): implement password login and rotating token sessions` | register/login/refresh/logout/me；密码 **8–256** |
| 4 | `feat(sync): expose ordered independently transactional push` | 八命令、预校验、六状态、停止规则 |
| 5 | `feat(sync): add consistent pull bootstrap and read endpoints` | pull/snapshot/tree/note |
| 6 | `test(api): enforce phase3 postgres acceptance and document protocol` | fixture、CI 组、no-skip、文档 |

每步本地验收通过再提交；全部通过后一次 push。

---

## 7. 验收命令与 CI

复用仓库外临时 PG 控制器（`start` / `custom` / `stop`），注入 `TEST_DATABASE_URL`、`TEST_MIGRATION_DATABASE_URL`、`REQUIRE_POSTGRES_TESTS=1`。`try/finally` 保证 stop。

最低本地门禁（最终全套）：

```powershell
uv sync --locked
uv run ruff check apps/api
uv run ruff format --check apps/api
uv run pytest apps/api/tests/unit -q
uv run pytest apps/api/tests/integration/kernel -v
uv run pytest apps/api/tests/api -m "api and not api_concurrency and not transport" -v
uv run pytest apps/api/tests/api -m api_concurrency -v
uv run pytest apps/api/tests/api -m transport -v
uv run pytest apps/api/tests/integration/test_migrations.py -v
uv run pytest apps/api/tests -v
```

（经 `$PgCtl custom` 包装执行；路径以本机 Phase 2 控制器为准。）

**必含承重用例（名称可等价，语义不可缺）：**

- `http_push_dense_seq` — 多连接并发 push，seq 连续无空洞无重复
- `http_pull_during_push` — 并发 push 期间 pull 无 gap、与 since 衔接
- `pull_cannot_observe_n_plus_one_first` — 不可先见 N+1 再见 N
- `snapshot_is_one_rr_view` / `snapshot_has_no_partial_success`
- `statement_timeout_real_57014` / `lock_timeout_metrics`
- `cancel_before_and_during_commit` / `socket_disconnect_stops_batch`
- `id_conflict_origin_hidden` / `secrets_never_echo`
- `workspace_access_hides_existence` / `refresh_rotates_once`

CI：保留 Kernel/并发/迁移组；新增 `api-http`、`api-http-concurrency`、`api-transport`（或等价）；每组用例非零、**0 skip / 0 fail**、必需用例存在；markers 先注册。Web 不改但仍跑原回归。镜像只经 Actions。

类型检查：对新增/改动生产模块跑 `ty check`（路径见计划 §14.2）；禁止靠全局 ignore 刷绿。

---

## 8. 完成定义

- `docs/plan-phase3.md` 已落盘且含密码 **8–256** 覆盖与全部锁定项
- 0003 迁移可升降；旧笔记/事件数据保留
- 认证 + push/pull/snapshot/只读按契约可用
- P2 五项落地并有测试证据
- 本地全套绿 → 一次 push → `gh run watch --exit-status` 绿
- 无密码/token/正文泄漏；无 skip；未触碰 Clash；未改前端业务

## 9. 迁移 0003 与数据模型

### 9.1 表与约束

- users 保留原字段，新增 nullable password_hash TEXT、disabled_at TIMESTAMPTZ；以 lower(btrim(email)) 表达式唯一索引替换 email 唯一约束。旧账号 hash=NULL，不能本地登录。迁移遇旧邮箱规范化碰撞明确失败，不自动合并或删账号。新注册 ASCII 邮箱并规范化。
- auth_sessions：UUID PK、user_id FK(users, RESTRICT)、device_name、created_at、expires_at、revoked_at、revoke_reason。session 即 family，绝对到期创建后30d。
- auth_access_tokens：UUID PK、session_id FK(auth_sessions, RESTRICT)、token_hash BYTEA UNIQUE（32字节CHECK）、created_at、expires_at。
- auth_refresh_tokens：UUID PK、session_id FK、token_hash BYTEA UNIQUE（32字节CHECK）、parent_id nullable self FK、created_at、expires_at、used_at。used token 保留用于R1复用检测；access/refresh到期不晚于session。
- workspace_members：PK(workspace_id,user_id)、双方FK RESTRICT、role TEXT CHECK(role='owner')、created_at；现有workspace.owner_id回填；不开放成员管理。
- session.user_id、token.session_id、member.user_id建索引；敏感字段排除repr；models/__init__.py统一注册。

### 9.2 升降级

升级不得改写folders/notes/change_events/op_ledger或游标。降级先删member/token/session，再撤销users扩展和表达式索引、恢复email原唯一约束。迁移测试覆盖有数据0002→0003→0002、head↔base；旧“六表/两迁移”断言随阶段升级而非削弱内核测试。

## 10. 认证状态机与依赖接口

注册关闭403 registration_closed；开启时事务内创建user/workspace/owner，201仅返回user/workspace，不登录。密码哈希在事件循环外执行且限制并发，固定Argon2id参数65536KiB/3/4；不存在账号也dummy verify。不存在/hash=NULL/disabled/密码错统一401 invalid_credentials。

登录创建session并返回随机至少256bit的opaque access/refresh，只存SHA-256。成功token响应no-store。refresh同一事务先定位session，再统一锁session、锁token并重读used_at：active→used+新token pair原子提交；used→撤销session并使全部token失效，**提交后**返回401 token_reused，无宽限。logout采用相同session锁顺序并提交撤销后204。

api/dependencies.py提供get_auth_context（user_id/session_id）、require_workspace_member与每op可调用的成员重查；使用request.app.state.settings创建factory。短命成员检查session在内核执行前关闭，不占双连接。无效access401；缺失与非成员workspace统一404；验证token/session/user到期、撤销及禁用。

## 11. HTTP线格式与错误

### 11.1 通用

业务前缀/api/v1，健康/与/health不变；UUID字符串，seq/rev/cursor十进制字符串，UTC RFC3339。字段见phase3-sync-api-spec.md。固定错误信封{error:{code,message,request_id,retryable,details}}；不返回验证input/ctx/原始异常/请求体。request id外层中间件涵盖500/413。禁止请求、原始query、Authorization、SQL参数日志；engine hide_parameters，敏感模型repr排除。

### 11.2 Push映射

八命令由entity_type+op区分，字段在payload，workspace以路径为准；无upsert/REST写包装。整批先验证。execute_with_outcome返回result/replayed，不预查ledger；execute保持WriteResult。RevConflict/EntityDeleted→conflict；EntityIdConflict/OpIdConflict→统一conflict固定消息空details；业务拒绝rejected继续。WriteBusy→rejected/write_busy；StorageFailure→rejected/storage_failure；CommitOutcomeUnknown→unknown/outcome_unknown；三者停止且后缀not_attempted，整体200。30s预算仅op间检查。取消向上传播、不shield COMMIT；疑似提交连接invalidate，不伪造回滚；同op_id+payload重试。

## 12. 读取与一致性

### 12.1 Pull

一条SQL（CTE+JSON或等价）同时读workspace水位/floor、完整事件页和页内去重的当前对象；不得先水位后事件。约4MiB预算内缩短连续事件前缀；next_cursor为末事件seq，无事件等于since；has_more来自同一快照。单事件+对象超预算明确413，不跳过事件。当前对象可领先页内事件版本，以其真实updated_seq/rev返回。

workspace锁持有到COMMIT，后继seq在前继提交或回滚后才能分配，回滚同时撤销last_seq。因此已提交集恒[1..K]，单语句快照不能先见N+1再见N。410条件since<min_retained_seq-1，since>last_seq为400 cursor_ahead；min_retained_seq默认1不变。

### 12.2 Snapshot和只读

单REPEATABLE READ READ ONLY事务读水位及所有活跃对象，返回as_of_seq/min_retained_seq/objects；不分页，最多10k对象/16MiB完整JSON。先在同RR事务检查count和内容字节上界以避免超大物化，再按序列化JSON真实字节检测；超限整次409，不返回半快照。随后pull since=as_of_seq。Tree返回活跃folders/notes元数据无body_md；note缺失/已删统一404。

## 13. 所有权与实施补充

本计划和规范先落盘，再恢复bf8440d可复用横切/P2代码并审查；不直接cherry-pick未验证提交。D独占router/main/config/middleware及.env.example；B的kernel P2子阶段归commit1，push归commit4；A的schema与业务分commit2/3。models/__init__.py及manifest/uv.lock归A；pytest markers早期由D注册；schema相关既有测试期望归A随commit2更新。router注册仅D/指挥官串行处理。E新增tests/api、CI及文档，最终跨HTTP/PG/transport承重证据由E提供。

## 14. 验证责任与预算

### 14.1 责任

D中间件/脱敏；A迁移与refresh竞争；B kernel P2/停止/取消；C单语句pull与RR快照；E真实PG多连接与真实uvicorn socket（不能ASGITransport冒充断连）。每提交适用局部门禁后提交，合并后指挥官最终全套一次。每组非零/0skip/0fail，承重名称守卫；Web原回归保留；镜像只Actions。

### 14.2 类型检查和发布门禁

uvx ty check覆盖apps/api/src/revnote_api下api/、security/、models/、services/auth*.py、services/sync_push_adapter.py、services/sync_read*.py、services/sync_write.py、kernel/results.py、config.py、main.py；以实际拆分扩展新增生产模块列表，禁止全局ignore。其余最终门禁见§7，另运行现有Web lint/typecheck/test/build及JUnit no-skip。证据记录docs/phase3-validation.md，不含凭据/本机DSN。临时PG每验收批次start、finally stop。全部本地通过后一次push，gh run watch --exit-status等绿；失败仅追加修复提交。
