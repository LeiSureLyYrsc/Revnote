# Phase 4 实际验收记录

本文件仅记录实际运行结果。Phase 4 尚在实施，以下专项通过不代表完整阶段或发布已通过。

## 基线与约束

- 起始分支 `main`，起始 SHA `77a20c2b6e7050d0918f8d723bd222eb216c7747`。
- 前置 Git 状态仅有用户提示文件未跟踪；这些文件不纳入提交。
- 用户已批准 D1–D18，实施计划见 [plan-phase4.md](plan-phase4.md)。
- 所有已派发车道均使用 `murasameai-vip/gpt-6.1-sol#high`。
- 本机不使用 Docker，不改动 Clash；真实 PG 为仓库外临时实例。

## 已运行专项

| 检查 | 实际结果 | 边界 |
|---|---|---|
| `uv sync --locked` | 退出码 0，42 个已安装包核验通过 | Python 锁定环境 |
| `pnpm install` | 退出码 0，新增测试/持久化依赖完成，Vite 保留 6.x | 首次更新 pnpm 锁文件 |
| `pnpm install --frozen-lockfile` | 退出码 0，锁文件一致 | 安装重现性 |
| `pnpm test:markdown` | **7/7 通过**，退出码 0 | 原有 TipTap 基础语义往返，不代替源码原文验证 |
| 仓库外 PG 控制器环境 | `pgserver==0.1.4` 在独立环境安装且 import 成功 | 未加入业务依赖 |
| `pytest apps/api/tests/api/test_auth.py -v --junitxml=api-auth.xml` | **35 passed，0 skipped，11.40s**，退出码 0 | 真实临时 PG + Alembic；`REQUIRE_POSTGRES_TESTS=1` |
| 上述 PG 生命周期 | finally 执行 stop，控制器确认临时实例已停止 | 原业务库、迁移库、第三 E2E 库分离 |
| `pytest apps/api/tests/api -m "api and not api_concurrency and not transport" -v --junitxml=api-http.xml` | **64 passed，9 deselected，0 skipped，24.72s**，退出码 0 | 真实临时 PG；并发/传输另组运行，非 skip |
| API 普通组 PG 生命周期 | finally stop 成功，临时实例已停止 | 未复用用户服务 |
| `playwright install chromium` | 退出码 0，Chromium build 1194 安装完成 | 浏览器运行前置条件，不代表 E2E 通过 |

早期整工作区 `build:web` 在 C 车道尚在写入的 `sync/operations.ts` 遇到 TS2322，已转交原车道修复；该次退出码为 1，不能计为构建通过。逐提交门禁将针对精确提交快照执行，避免并行后续车道污染前一步证据。

## 逐步门禁完成情况

六步本地门禁、完整真实 PG 回归、Web 单测/组件测试、真实浏览器全链路与报告守卫均已实际运行完成，结果见下文各节。远端 CI 以精确 SHA 的运行记录为准；本次尚未 push 之前不宣称发布完成。

## 逐提交精确快照门禁

并行后续车道代码未纳入前一步时，从 Git index 导出独立临时快照并执行检查；不暂存或清除其他车道文件。快照独立 `pnpm install --frozen-lockfile`，不复用可变工作区源码。

### Commit 1 — `8d84523`

- `uv sync --locked`、frozen pnpm 安装通过。
- 精确快照 lint、typecheck、contracts/proxy **17 passed / 0 skipped**、Markdown **7/7**、生产 build 均退出 0。
- Vite 保持 **6.4.5**；859.27 kB 编辑器 chunk 警告为既有已知边界，不为消除警告扩范围。
- 第一次快照门禁使用 node_modules junction，被 pnpm 自身安全策略拒绝（退出 1）；改用独立 frozen 安装后通过，未更改 pnpm 安全策略。

### Commit 2 — `26608e6`

- 已读取全部存储源码与测试，权威 workspace aggregate 与五类事实投影在同一短事务更新；凭据不进入恢复导出。
- 精确快照 frozen install、lint、typecheck、storage **17 passed**、当时全部已实现用例 **34 passed / 0 skipped**、Markdown **7/7**、build 全部退出 0。
- 初次 lint 发现生命周期回调持有变量触发 `prefer-const`；改为 const holder，保持生命周期行为后复验通过。
- 当时尚无 auth/sync/components 组，因此 34 项结果仅为第二提交覆盖，不冒充完整 Phase 4 报告守卫通过。

### Commit 3 — `9902730`

- 已读取会话、API 客户端、React provider 与全部专项测试；身份 lease 对账号切换、退出与交接的旧回调立即失效。
- 精确快照 frozen install、lint（0 error，2 个 Fast Refresh 提示）、typecheck、auth **27 passed**、当时全部已实现用例 **61 passed / 0 skipped**、Markdown **7/7**、build 全部退出 0。
- 真实 PG 认证 **35 passed / 0 skipped** 专项证据适用：后端认证代码与契约未改。
- 61 项结果不代替尚未实现的 sync/components 与浏览器门禁。

### Commit 4 — `5c2f391`

- 已读取同步五模块、API transport/decoder 与承重测试。修复报告参数化同名用例 hash 碰撞后，精确快照 frozen install、lint、typecheck、sync **67 passed / 0 skipped**、当时全部用例 **128 passed / 0 skipped**、Markdown **7/7**、build 全部退出 0。
- 严格重复 ID 守卫保留：Vitest reporter 改用框架唯一 `test.id` 生成 hash，不放宽报告规则。
- Oracle 实现风险闸口已重做并给出 **PASS**：8 条承重主张逐条成立（事务边界、所有权与身份、冻结命令不可变/单发送器/1 op、ACK 覆盖范围、六状态与整请求错误、pull/cursor、snapshot/resync、冲突与删除），无 blocker。修复前发现的重复恢复记录缺陷已修复并经真实浏览器验证。

## Python 静态检查

- 新 runner / API 报告守卫 Ruff 与格式检查退出 0。
- 固定 `ty==0.0.94` 对既有 CI 全部明确生产路径，加新 runner / 报告守卫执行检查，退出 0。新增 runner 后续变化须重验受影响检查。

## 最终后端全量回归（真实临时 PostgreSQL）

Phase 4 后端业务零改动，本组证据用于证明 Phase 2/3 不变量未削弱。全部运行在仓库外临时 PG 上（业务库、迁移库、以及为浏览器验收准备的第三个一次性库彼此独立），`REQUIRE_POSTGRES_TESTS=1`，结束后在 `finally` 中停止实例并确认状态文件消失。

| 组 | 命令 | 实际结果 |
|---|---|---|
| 全量 | `pytest apps/api/tests -v --junitxml=api-full.xml` | **1157 passed，0 skipped，0 failed** |
| unit | `pytest apps/api/tests/unit` | 867 passed |
| integration | `-m "integration and not concurrency and not migrations"` | 129 passed（88 deselected） |
| concurrency | `tests/integration/kernel -m concurrency` | 48 passed（116 deselected） |
| migrations | `tests/integration/test_migrations.py` | 40 passed |
| http | `tests/api -m "api and not api_concurrency and not transport"` | 64 passed（9 deselected） |
| http-concurrency | `tests/api -m api_concurrency` | 6 passed |
| transport | `tests/api -m transport` | 3 passed |

报告守卫 `apps/api/scripts/check_test_reports.py` 与 `--full-report` 均退出 0：

```text
api/unit: passed=867 required=0; zero skips/failures/errors
api/integration: passed=129 required=3
api/concurrency: passed=48 required=10
api/migrations: passed=40 required=2
api/http: passed=64 required=22
api/http-concurrency: passed=6 required=6
api/transport: passed=3 required=3
api/full: passed=1157 required=46; zero skips/failures/errors
```

迁移门禁：普通测试库 `alembic upgrade head` → `current`（`0003_auth_sessions_members (head)`）→ `check`（No new upgrade operations detected）；专用迁移库 `upgrade head` → `downgrade base` → `upgrade head` → `check` 全部成功。

## 浏览器验收设施（E 车道，静态门禁已复验）

真实浏览器 E2E 尚未运行；以下为本轮修复与独立复验结果。

修复的真实缺陷（均可复核）：

1. `safeSource` 原先接受相对路径，结果依赖当前工作目录；改为必须绝对路径。
2. Playwright 报告的用例行号与实际文件不符（`workspace.spec.ts` 仅 132 行，`mobile_layout` 却报第 201 行）；改为按声明标题从磁盘解析真实行号，runner 行号仅作「该行存在于本文件内」的兜底。
3. `validateReport` 原先放行任意额外键，`message`/`stack` 可被追加进报告并通过门禁；改为报告与用例的严格键白名单、每例恰好一个本 runner 标签、`source`/`line` 同现同缺。
4. runner 的 `finally` 清理可能覆盖原始失败；argparse 会原样回显未知参数（可能携带凭据）。改为固定文案，且不覆盖原始异常。

16 个固定承重标签逐一核对：均存在真实浏览器用例，标题可被 `labelsIn` 识别，无 skip/todo/only，无重复标签，无未打标签用例。

独立复验（orchestrator 亲自运行，非采信车道总结）：

| 命令 | 退出码 |
|---|---|
| `pnpm typecheck:web` | 0 |
| `pnpm lint:web` | 0（0 error，2 个既有 Fast Refresh 提示） |
| `pnpm test:web` | 0（`vitest/full: cases=160`） |
| `pnpm --filter @revnote/web check:reports --unit` | 0（160 passed，零 skip/retry/error） |
| `uv run ruff check apps/api/scripts` | 0 |
| `uv run ruff format --check apps/api/scripts` | 0 |
| `uvx --from ty==0.0.94 ty check <runner> <report script>` | 0 |

诊断输出格式为 `Browser failure: e2e/<file>.spec.ts:<line> assertion=<位置或 unknown> stage=<阶段名或 unknown> labels=<label> case=<sha256>`，上限 25 行；不输出 message、stack、正文、请求体或凭据。`assertion` 位置除各 spec 外还允许 `e2e/fixtures.ts`（Playwright 报告最深的用户抛错帧，而崩溃定时器、写锁轮询、follower 可见性等有界守卫都在该共享 helper 中，此前拒绝该文件本身就是 `assertion=unknown` 的成因之一）；放宽范围仅为 `e2e/` 下这一个确切文件，相对路径、嵌套目录、`..`、产品源码与配置仍全部拒绝，行号必须在文件中真实存在，且 `source`（用例声明位置）不随之放宽。`stage` 只在用例没有断言行号时出现，取值来自固定词表（`apps/web/scripts/check-test-reports.ts` 的 `STAGES`，`run_web_e2e.py` 中有同序镜像），先按标识符正则、再按词表成员双重校验，非法值一律降级为 `unknown`。报告目录 `apps/web/reports/` 已被忽略，仓库中无任何报告、HAR 或认证状态被跟踪。

## 真实浏览器 E2E（首次运行，未通过）

首次门禁运行：`pnpm test:e2e` → **5 passed / 11 failed**，退出码 1。runner 按设计只输出受限诊断（label + source:line + case hash），不输出正文或凭据。

为区分真实缺陷与级联误报，用仓库外临时脚本（不提交、非验收）复用 runner 自身的 `WindowsJob`/`TestControl`/`child_environment` 并换用可见 reporter，逐个单跑复核：

| 用例 | 单跑结果 | 结论 |
|---|---|---|
| workspace_flow | fail（缺 `folder:move`） | 用例构造问题：新建子文件夹后 UI 已选中父文件夹，移动到同一父级不产生 move 意图（空操作是已批准语义） |
| draft_reload | pass | 级联误报 |
| markdown_preservation | pass | 级联误报 |
| mobile_layout | pass | 级联误报 |
| api_restart_recovery | pass | 级联误报 |
| offline_recovery | pass | 级联误报 |
| lost_response_replay | pass | 级联误报 |
| ack_preserves_later_edit | pass | 级联误报 |
| manual_conflict | pass | 级联误报 |
| cursor_integrity | pass | 级联误报 |
| refresh_rotation | pass | 级联误报 |
| refresh_uncertainty | pass | 级联误报 |
| resync_preservation | fail（fixture IndexedDB 写法） | 真实缺陷，且**污染后续用例** |
| remote_delete_recovery | fail（冲突面板出现「以本地内容提交」） | 真实缺陷（同步核心：恢复记录使用了 pull 之前的陈旧远端） |
| single_writer_takeover | fail（崩溃后接管页已关闭） | 真实缺陷（崩溃/接管生命周期） |
| session_identity_isolation | fail（点击退出后登录表单不出现） | **真实产品缺陷**：同步失败时 `quiesceWorkspacePorts` 拒绝，退出登录被永久阻塞 |

级联机理：`resync_preservation` 在 `control(action:'stop')` 之后、`try/finally` 之前抛错，API 再未重启，导致其后所有用例的 `POST /api/v1/auth/register` 连接失败（`Real API registration succeeded` 断言失败）。该断言本身是正确的——它正确暴露了 API 不可达。

另确认：直接对 owned API 连续注册 40 次全部返回 `201`，证明注册端点本身没有状态性缺陷。

结论：**真实浏览器验收尚未通过**，不得据其宣称 Phase 4 完成。三个真实缺陷正在分车道修复，修好后必须重跑全量。

## 全量真实浏览器验收（第二轮）

第二轮门禁运行：`pnpm test:e2e` → **15 passed / 1 failed**，退出码 1。首次运行的 11 个失败降到 1 个，证明级联污染与三处产品缺陷修复有效。

剩余失败 `resync_preservation` 的真实根因（用仓库外临时脚本复用 runner 自身 `TestControl` 停/启 API 的路径复现）：用例在 3 分钟超时后卡在第 72 行的正文编辑器定位。原因是重新认证会轮换所有权代次，`WorkspaceProvider` 随之重挂，`AppLayout` 的选中笔记状态按设计重置为未选中——因此之后的编辑动作找不到编辑器。这是**用例假设失效**，不是产品缺陷：重新认证后不自动恢复选中是合理行为。

修复方式（不放宽任何承重断言）：在恢复段之后显式重新打开笔记，再执行编辑；注释说明选中状态为何不应跨越身份代次存活。脏稿保留、意图/命令不缩水、410 恢复、`cursor_ahead` 保留等断言原样保留。

单跑复核：该用例改为 9.3s 通过（此前 3 分钟超时）。随后在**全新一次性数据库**上重跑全量门禁。

另需说明两处修复中的取舍：
- `session_identity_isolation` 中「新建笔记」在 bootstrap 未完成时禁用，是**已批准的产品行为**（snapshot 上限使其永远无法 bootstrap）。曾有的断言把「认证成功」与「工作区已就绪」耦合；已拆分：`enterLogin` 只证明认证，`expectWorkspaceReady` 单独承载可编辑前置，生产代码未改。
- 报告守卫用例原本硬编码 spec 的行号，导致任何无关的 spec 编辑都会让门禁变红。改为从文件解析期望行号，既保持「定位必须正确」的严格性，又不产生虚假失败。

诊断脚本仅存在于仓库外临时目录，不属于仓库内容，也不作为验收证据。

## 缺陷修复与真实浏览器复核

| 缺陷 | 修复内容 | 真实浏览器复核 |
|---|---|---|
| 恢复记录用陈旧远端 | `preserve` 按「实体+本地版本」去重；tombstone 恒胜；`confirmRemote` 使同实体记录不矛盾；`entity_deleted` 不再把缓存活镜像当远端；`not_found` 明确排除 | `remote_delete_recovery` **通过（21.2s）**；`manual_conflict` 无回归 |
| 同步失败永久阻塞退出登录 | `quiesceWorkspacePorts` 改为「等待静止」而非「必须成功」；并注入可中止退避，避免退出要等满 15s 退避周期；`flushNoteWrites` 的失败传播保留 | `session_identity_isolation` **通过（19.5s）** |
| 崩溃接管生命周期 | 隔离崩溃页与跟随页、接管前等待锁释放 | `single_writer_takeover` **通过（4.2s）** |
| 级联污染 | 停/重启 owned API 的用例把恢复放进任何退出路径 | 全量 16/16 通过 |
| fixture 写 IndexedDB 缺 key | 读取真实 key 后 `put(state, key)` | 不再出现 `Bootstrap setup aborted` |
| `workspace_flow` 移动为同父级空操作 | 先复位选中再建第二个根文件夹，断言 `parentId` 确实改变；八操作断言原样保留 | 全量 16/16 通过 |
| 登录断言耦合 bootstrap | `enterLogin` 只证明认证；新增 `expectWorkspaceReady` 承载可编辑前置 | 全量 16/16 通过 |
| 重认证后选中笔记丢失导致编辑失败 | 显式重新打开笔记再编辑；承重断言未放宽 | `resync_preservation` 由 3 分钟超时变为 **9.3s 通过** |

另修正两处自身问题：报告守卫用例不再硬编码 spec 行号（改为从文件解析，保持严格但不受无关编辑影响）；`resync_preservation` 标题按 Oracle A 项改为「保留」语义。

## 全量真实浏览器验收（本地最终通过）

本地门禁（已提交代码状态，全新一次性数据库）：`pnpm test:e2e` →

```text
Web E2E full: passed; browser report guarded; child output redacted.
Owned API/Vite/browser groups stopped; external PostgreSQL remains caller-owned.
```

报告核对（`apps/web/reports/full/playwright.json`）：`scope=full`、`complete=true`、`errors=0`、`cases=16`、**16 passed**；16 个固定承重标签全部覆盖；`attempts` 全为 1（无重试）；报告中无 `message`/`stack`/正文/凭据字段。崩溃注入修复后连跑两次真实 E2E 均 `cases=16 passed=16 errors=0`、退出码 0。

拓扑为真实 Chromium → Vite 同源代理 → 真实 uvicorn → Alembic 迁移的真实临时 PostgreSQL（第三个一次性库，与 pytest 业务库、迁移库相互独立）。响应丢失用例先在真实服务端完成提交并确认 event/ledger 各 1 条，再丢弃响应。

## 远端 CI 结果（阶段未完成，进行中）

### 平台差异修复已被真实 CI 验证

`b92f70c` 对应 run **40974724185**（`push`，headSha 精确匹配）四个作业全部 success，并对同一 SHA 重跑一次（`attempt 2`）仍全部 success：

| 作业 | 结果 |
|---|---|
| api（lint + types + unit + postgres integration + http + migrations） | **success** |
| web（lint + typecheck + build + markdown + unit + components） | **success** |
| web-e2e（Chromium + native API + real PostgreSQL） | **success** |
| image（build + push to GHCR） | **success** |

这证明下列平台缺陷的修复在真实 CI 上有效。

### 但随后仅改文档的提交再次失败 —— 该结论不足

提交 `3509b7c` 只修改 `README.md` 与本文档，其 run **40975758621** 仍失败：api 与 web success、**web-e2e failure**、image skipped（镜像被门禁正确阻挡）。

```text
Health probes: api=200 web-proxy=200   Control: last_ok=stats last_failure=none
Browser failure: e2e/durability.spec.ts:122 assertion=e2e/fixtures.ts:186
                 labels=single_writer_takeover
```

失败点是 `openNote()` 内的 `expect(page.getByTestId('note-source')).toBeVisible()`（`fixtures.ts:186`），调用处在点击「接管写入」并确认只读提示消失之后。此前失败点为 `fixtures.ts:388`（崩溃事件等待），因此崩溃修复本身没有回退，这是**另一个**失败点。

纯文档改动不可能改变测试行为，故这是**用例不稳定（flaky）**，而不是产品回归。据此：

- 先前「连续两轮 CI 全绿即稳定」的判据**不充分**，已撤回；有已知 flaky 用例时，两轮通过可能只是运气。
- 新的稳定性判据：重复全量运行 + 对可疑用例反复聚焦运行，证明竞态消失；再以最终 SHA 的 CI 稳定全绿确认。
- 当前 `manual_conflict` 与 `single_writer_takeover` 均按 **open / flaky** 记录，未关闭。

**因此 Phase 4 尚未交付完成，镜像未被发布。**

## 本轮 CI 失败的真实原因与修复（逐条有证据）

初次推送后 CI 连续失败三轮，每次都用 `gh run view --log-failed` 取真实原因后修复，而非猜测：

| 失败 | 真实原因 | 修复 |
|---|---|---|
| api / ty | `error[unresolved-attribute]: Module ctypes has no member WinDLL`：新增 runner 使用 win32 专属成员，Linux 类型桩下不可判定 | 按平台拆分 `load_kernel32()`，非 Windows 抛固定错误；以 `ty check --python-platform linux` 覆盖完整 CI 路径清单验证 |
| web / 单测 | 171 项用例全过，失败在报告守卫：`replaceAll('/', '\\')` 在 POSIX 下是相对路径，被守卫正确拒绝 | 断言按平台分支；POSIX 下断言守卫必须拒绝该形式。守卫严格性未放宽 |
| web-e2e（14 例） | 诊断给出 `Control: last_failure=start/port_unavailable`：API 重启无法重绑同端口 | POSIX 设 `SO_REUSEADDR` + 有界重试，Windows 保持 `SO_EXCLUSIVEADDRUSE`。实测确认仅「双方都设 SO_REUSEADDR」时才会抢占活跃端口，故必须平台二选一 |
| web-e2e（2 例） | `manual_conflict` 不稳定；`single_writer_takeover` 报 `assertion=unknown` | 先把断言位置与阶段标记纳入受限诊断，再据实定位 |
| web-e2e（1 例） | 诊断给出 `assertion=e2e/fixtures.ts:388`：`crashPage()` 等待 Playwright `crash` 事件 20s 超时。源码依据：Playwright 由 CDP `Inspector.targetCrashed` 驱动该事件，事件丢失时 `evaluate` 不会快速报错而是挂住 | 注入仍用 CDP `Page.crash`，确认改为三路先到者胜（事件 / `Inspector.targetCrashed` / 连续两次有界无响应），必要时升级 `chrome://crash`。承重保证由跟随页独立观测 `revnote-writer-v1` 持有者为 0 断言，不依赖夹具自述 |

`manual_conflict` 曾在一次运行中失败、后续运行通过且期间无代码变更，因此按 **flaky** 记录，未视为已修复。

### 提交列表（`77a20c2` 之后，共 11 个）

```text
8d84523 chore(web): define phase4 client contracts and verification tooling
26608e6 feat(web): persist local drafts intents and recovery records
9902730 feat(web): add coordinated login and rotating sessions
5c2f391 feat(web): implement durable native sync and safe reconciliation
023f21e feat(web): connect workspace editing and manual conflict recovery
afeb521 test(web): enforce phase4 postgres e2e and release gates
b3bd704 fix(web): keep phase4 gates portable across ci platforms
9189094 fix(web): rebind the e2e api port on linux after restart
9dcb857 test(web): record the failing assertion location without error text
e4058c3 test(web): locate failures inside e2e helpers and record stage markers
b92f70c test(web): confirm the writer crash for real on linux ci
```

前 6 个为计划规定的提交；后 5 个为 CI 真实失败暴露的平台差异修复，均为追加提交，未改写已发布历史、未强推。

## 提交与推送前门禁

提交后状态门禁：`uv sync --locked` 0、`pnpm install --frozen-lockfile` 0、`ruff check` 0、`ruff format --check` 0、`lint:web` 0（0 error / 2 既有 Fast Refresh 提示）、`typecheck:web` 0、`test:markdown` 7/7、`build:web` 0、`test:web` 176 passed、报告守卫 `vitest/full 176` 与 `playwright/full 16` 均零 skip/retry/error。

范围核验：`apps/api/src` 与 `alembic/versions` 零改动；禁词扫描 0 命中；无 `.env`、HAR、storageState、报告产物被跟踪；仓库根用户提示文件保持未跟踪且未删除。

## 首次整合 Web 检查（浏览器前）

- D 完成当前 partition 恢复导出及跨标签 profile 读取 guard 后，工作区 `lint:web`（0 error，2 个 Fast Refresh 提示）、`typecheck:web`、`test:web`、`build:web` 均退出 0。
- Web full **155 passed / 0 skipped / 0 retry / 0 error**；完整 unit/components 报告守卫退出 0，全部六组承重标签齐全。
- build 主入口 896.81 kB（gzip 286.06 kB），仍有 chunk 提示，不开展性能扩围。
- 此检查尚未执行真实浏览器；Oracle 与 E2E 后如有相关代码变化，将重验受影响项并对最终状态完成发布门禁。
