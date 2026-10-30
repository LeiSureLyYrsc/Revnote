# Phase 5 实际验收记录

本文件仅记录实际运行结果。Phase 5 尚在实施，以下专项通过不代表完整阶段或发布已通过。

## 基线与约束

- 起始分支 `main`，起始 SHA `4abdcb04afa24693cf3d4c0c99f98d9c7e773f2f`（`docs: record phase4 stability evidence and scope limits`），与 `origin/main` 同步。
- 起始 CI 绿：run `40982876850`（`c57c77a`）与 run `40984522049`（`4abdcb0`）连续两轮 web/api/web-e2e/image 全 success。
- 前置 Git 状态仅有用户提示文件未跟踪（`phase*.md`）；这些文件不纳入提交。
- 用户已批准 D1–D19 全部按推荐默认值执行；实施计划见 [plan-phase5.md](plan-phase5.md)。
- 本机无 Docker，不改动 Clash；真实 PostgreSQL 为仓库外临时实例（`finally` 停止）；真实 MinIO 为本机已登记二进制。
- 模型策略：orchestrator 使用 `murasameai-vip/cn/deepseek-v4.1-flash#max`；所有 SubAgent 派发**不传 `model` 参数**（走插件默认）。本阶段**未使用** `gpt-6.1-sol`。

## 前置条件核查（P1–P5）

### P1 — 对象存储可执行文件（本机真实 MinIO）

| 项 | 实际值 |
|---|---|
| 路径 | `D:\tools\minio.exe` |
| 文件大小 | 113,115,136 bytes（约 107.88 MiB） |
| 版本标签 | `RELEASE.2025-09-07T16-13-09Z` |
| **SHA-256** | `af709e6ba68488404e85acdd22a3030d0f5e56a108d4b27d744f18ceb50861b4` |
| 来源 | GitHub Releases 官方 Windows amd64 资产（`dl.min.io` 返回 410 Gone） |
| 扫描结论 | PE `MZ`+`PE`、machine `0x8664`（AMD64）、8 个常规 Go 段、整体熵 6.598；SHA-256 与发布方 `.sha256sum` 逐字一致；本地 ClamAV 不可用（镜像 500），仅做轻量静态启发式，结论 PASS |

**为什么是 09-07 而不是 09-06**：`docker-compose.yml` 原先固定 `RELEASE.2025-09-06T17-38-46Z`，但该 release **没有 Windows 资产**，本机无法启动真实 MinIO。因此 compose 与 CI 的 MinIO 服务镜像 pin 统一更新为 `RELEASE.2025-09-07T16-13-09Z`，让「本机真实二进制」与「CI 容器」是同一 release。`mc` 镜像 tag 不变。

**启动前校验**：每次启动真实 MinIO 前重新计算 SHA-256 并与上表比对；不一致即拒绝启动并把 HARD STOP 写入 `.slim/deepwork/phase5-build/progress.md`。

### P2–P5

| 前置 | 状态 |
|---|---|
| P2 仓库外临时 PostgreSQL（业务库/迁移库/E2E 第三库彼此独立） | 沿用 Phase 2/3/4 控制器 `revnote_phase2_pg.py`；`REQUIRE_POSTGRES_TESTS=1`；每批 `finally` 停止 |
| P3 真实浏览器 | `playwright install chromium` |
| P4 CI 官方镜像可拉取性 | 固定官方 tag `RELEASE.2025-09-07T16-13-09Z`；本机无 Docker，能否拉取只能由 CI 运行结果证明（见下文「CI」一节）；**不替换第三方重建镜像或其他 S3 产品** |
| P5 工具定位 | `git`=`D:\Git\cmd\git.exe`、`gh`=`C:\Program Files\GitHub CLI\gh.exe`、`uv`=`D:\Dev\uv\uv.exe`、`node`/`pnpm` 经 `pnpm.cmd` |

## 实施记录

（以下各节在对应提交实际运行后填写；不得填写未运行的结果。）

### Orchestrator 共享层

已实际运行（本机，真实执行）：

| 检查 | 实际结果 |
|---|---|
| `ruff check` + `ruff format --check`（`config.py` / `errors.py` / `request_limits.py` / `main.py`） | 退出码 0，`All checks passed!`，`4 files already formatted` |
| `Settings` 字段不变量（`test_config.py`） | **42 passed**，含「无任何字段名含 `secret`」与「不得新增 `*_secret`/`*_password`/`*_token` 字段」 |
| `Settings` 默认值 | `attachments_enabled=False`、`attachment_max_bytes=26214400`、`attachment_pending_grace_s=86400`、MIME 白名单六项 |
| `image/svg+xml` 启动期拒绝 | `Settings(attachment_accepted_mime=[... 'image/svg+xml'])` 抛 `ValidationError`（匹配 `svg`） |
| 请求上限路径映射 | upload 路径 → `ATTACHMENT_MAX_BYTES`；push → 2 MiB；`/api/v1/workspaces/*/tree` → 2 MiB；`/api/v1/capabilities` → 1 MiB 默认 |
| 中间件组 | `test_middleware.py` **47 passed** |
| 单元全量 | `pytest apps/api/tests/unit -q` → **878 passed**，0 skipped |
| `ERROR_SPECS` 精确集合 | 加法更新为 32 项（原 26 + 新 6）；retryable 集合加法加入 `attachment_storage_unavailable` |

**P1 可执行文件的真实启动验证**（本机实际执行，非推断）：先按上表 SHA-256 比对通过，`minio.exe --version` 报告 `RELEASE.2025-09-07T16-13-09Z (commit-id=07c3a429bfed433e49018cb0f78a52145d4bedeb)`、`go1.24.6 windows/amd64`；随后以临时数据目录启动 `server --address 127.0.0.1:19100`，`/minio/health/ready` 在 0.5 s 内返回 200、`/minio/health/live` 返回 200；`Stop-Process` 后确认进程退出且 19100 端口已释放、临时目录已删除。**未使用 Docker**，未下载任何新二进制。

### H — 稳定性前置

（待填：focused ×5 + full ×2，各自全新一次性库）

### B — 附件全栈

（待填）

## 迁移 `0004_attachments` 真实 PG 往返（orchestrator 亲自执行）

真实临时 PostgreSQL 16.2（仓库外控制器启动，`127.0.0.1:58918`，`finally` 停止）。迁移库与业务库彼此独立。

| 步骤 | 实际结果 |
|---|---|
| `alembic upgrade head` | 依次 `0001 → 0002 → 0003 → 0004_attachments`，全部成功 |
| `alembic current` | `0004_attachments (head)` |
| `alembic downgrade 0003_auth_sessions_members` | 成功（只删新表及其索引） |
| `alembic upgrade head` | 成功 |
| `alembic downgrade base` | 成功：`0004 → 0003 → 0002 → 0001 → (空)` |
| `alembic upgrade head`（再次） | 成功，证明可重复升级 |
| `alembic current` | `0004_attachments (head)` |

真实目录核对（来自 `pg_catalog` / `information_schema`，非推断）：

| 项 | 实际值 |
|---|---|
| 列数 | **14**，顺序与 interface §7 一致（`id, workspace_id, state, mime, byte_size, content_hash, storage_key, upload_nonce, file_name, created_at, upload_started_at, ready_at, deleted_at, delete_reason`） |
| CHECK | `ck_attachments_state_known`、`ck_attachments_mime_len`、`ck_attachments_byte_size_positive`、`ck_attachments_content_hash_len`、`ck_attachments_storage_key_len`、`ck_attachments_ready_iff_hash`（共 6） |
| 外键 | `fk_attachments_workspace_id_workspaces`（`ON DELETE RESTRICT`） |
| 主键 / 唯一 | `pk_attachments`、`uq_attachments_storage_key` |
| 索引 | `ix_attachments_workspace_id_state`、`ix_attachments_workspace_id_created_at` |
| 域表总数 | **11**（原 10 + `attachments`）；`alembic_version` 除外 |

**已知未通过项（待车道 C 完成注册后复验）**：`alembic check` 此时报
`Detected removed table 'attachments'` —— 原因是 `models/attachment.py` 尚未在
`models/__init__.py` 中导入，表因此不在 `Base.metadata` 上。这不是迁移本身的缺陷（往返已证明可逆），
但**在 `alembic check` 转绿之前不计为通过**。

## 依赖与锁文件（D19）

| 项 | 实际值 |
|---|---|
| 存储 SDK | 官方 `minio` Python SDK（D19 首选），`apps/api/pyproject.toml` 固定 `minio>=7.2.9,<8` |
| 锁定版本 | `uv.lock` → **minio 7.2.20** |
| 新增传递依赖 | `pycryptodome 4.2.0`、`urllib3 2.8.0`（此前锁文件两者都没有）；`certifi`/`typing-extensions`/`argon2-cffi` 复用既有条目 |
| `boto3` / `botocore` | **未引入**，锁文件中零条目（D19：二者不得同时引入） |
| `uv sync --locked` | 退出码 **0**（`Resolved 47 packages` / `Checked 45 packages`）——锁文件一致，CI 的 `--locked` 门禁不会因陈旧锁变红 |

## 禁词扫描

| 检查 | 实际结果 |
|---|---|
| 复合禁词（`test` + 表示「敏感」的英文词）在 `apps/`、`docs/`、`.github/`、`pyproject.toml`、`.env.example` 全域 | **0 命中** |
| 裸词 `sensitive` | 5 处，**全部为 Phase 2/3 既有代码**（`case_sensitive=False`、`Case/space-insensitive uniqueness`、注释中的 “Sensitive columns”）。逐文件与 `git show HEAD:<file>` 比对，HEAD 命中数同为 1/1/1/1/1，**Phase 5 未新增任何一处** |

## CI

（待填：本次 SHA 对应的 run 链接与五 job 结论；不以旧 run 的绿色代替本次验收）

## 本轮暂停：配额（403 upstream）

2026-10-30 约 09:58，三条车道（B 存储层、C 附件域、E CI/门禁）同时以
`All accounts failed (last upstream error code 403)` 终止。按硬约束处理：
**立即停止全部工作、不切换模型、不硬重试、不再派发新车道**。

暂停时的实际状态（非推断）：

| 项 | 状态 |
|---|---|
| 车道 R（预检） | 完成 |
| 车道 D（Web UI） | 完成：lint 0 / typecheck 0 / `test:web` 211 例 0 错误 / `build:web` 0 |
| 车道 A（H 稳定性） | 被终止，**未写入任何文件** |
| 车道 B（存储层） | 被终止，`storage/` 与 5 个单测文件已落盘，SDK 已锁 |
| 车道 C（附件域） | 被终止，模型/迁移/服务/schema/路由/注册与 3 个测试文件已落盘，**精确集合断言未改** |
| 车道 E（CI/门禁） | 被终止，**未写入任何文件** |
| `pytest apps/api/tests/unit` | **11 failed / 1179 passed**；全部为被终止车道的未完成编辑，详见 `.slim/deepwork/phase5-build/progress.md` |
| OpenAPI 路由集 | **17 条**，5 个新端点齐全且与 interface §2 一致，既有 12 条未变 |
| 临时 PostgreSQL | 已由控制器停止：state 文件移除、58918 端口释放、**0 个残留 postgres 进程** |

本阶段**未提交、未推送、未运行 CI**，因此不宣称任何发布完成。恢复清单位于
`.slim/deepwork/phase5-build/progress.md`。

## 真实 MinIO + 真实 PG 附件组（orchestrator 亲自执行）

环境：真实临时 PostgreSQL 16.2（`127.0.0.1:52367`）+ 真实 `D:\tools\minio.exe`
（`RELEASE.2025-09-07T16-13-09Z`，`127.0.0.1:19300`，每次 SHA-256 校验后启动）。
`REQUIRE_POSTGRES_TESTS=1`、`REQUIRE_OBJECT_STORAGE_TESTS=1`。

**发现的环境陷阱（重要，写入本文件的理由）**：首次运行整组 14 个用例失败，全部为
`503 attachment_storage_unavailable`。适配器把 MinIO 的
`XMinioStorageFull`（HTTP 507）正确翻译成了安全错误码——**这不是代码缺陷**。
真实原因是本机 **C: 盘仅剩 0.02 GB**，而 MinIO 数据目录当时落在 `%TEMP%`（C: 盘），
低于 MinIO 的最小可用空间阈值。把数据目录改到 **D:（126 GB 可用）** 后同一批用例随即通过。
结论：本机跑真实 MinIO 必须把数据目录放在 D:，不要用 `%TEMP%`。

**数据污染陷阱**：早期失败运行在测试库里留下了 34 行 `attachments`（最老 `created_at`
为 2026-10-28），使 GC 用例的全局计数断言失真。清空该表后计数从 4 降到 2。
真正的计数问题见下。**结论：跑这批用例前应确保测试库的 `attachments` 为空。**

| 运行 | 实际结果 |
|---|---|
| 首次（数据目录在 C:） | **14 failed / 8 passed**，全部 503 `XMinioStorageFull` |
| 数据目录移到 D: 后 | **20 passed / 2 failed** |

### 两个未通过项（均已在 progress.md 立项）

| 用例 | 实际结果 | 判定 |
|---|---|---|
| `test_a_non_member_cannot_read_metadata_content_or_upload` | 上传路径返回 **409 `attachment_already_ready`**，期望 **404** | **测试缺陷，非产品缺陷**（orchestrator 先前误判为 IDOR，已更正）：`_read_upload_target` 的 SQL 确实是 `WHERE id = :attachment_id AND workspace_id = :workspace_id`，无越权。真实原因是该用例的 `app.upload(..., headers={})` 未传 token，`api.headers()` 回落到**属主**令牌，于是以属主身份对已 ready 的附件重传，409 属正确行为。已派回车道 C 修测试（改传他人 token） |
| `test_gc_removes_expired_pending_rows_and_leaves_ready_ones` | `rows_deleted == 1` 实际为 2 | **测试隔离问题，非 GC 缺陷**（已修）：`collect_expired` 是全局运维扫描，同会话其他用例留下 pending 行。现改为断言本工作区的 pending 被删、ready/fresh 保留 |

**在其余 20 项里已实际通过（真实字节）**：上传→ready→下载逐字节一致、声明摘要校验、
错误摘要拒绝且不留 ready 行、已 ready 再上传被拒且对象不变、并发双上传单赢家、
提交后重放仍是一行、鉴权（metadata/content 404）、全部端点需要 token、
附件不推进 `change_events`/`last_seq`、引用附件的笔记仍是普通笔记、
对象键符合 `ws/{ws}/att/{id}/{nonce}` 契约、GC dry-run 不删除。

## 本机环境阻塞（已排除，务必记录）

恢复会话时遇到两个**非代码**阻塞，都会伪装成产品故障：

| 现象 | 真实原因 | 处置与结果 |
|---|---|---|
| 附件组 14 个用例全部 503 `attachment_storage_unavailable` | **C: 盘仅剩 0.02 GB**，而 MinIO 数据目录落在 `%TEMP%`（C:）。MinIO 返回 `XMinioStorageFull`（507），适配器正确地翻译成安全 503 | 数据目录改到 **D:（126 GB 可用）**；同一批用例随即 **20 passed** |
| `pnpm.cmd` 不存在；`corepack pnpm` 报 `ENOSPC: no space left on device` | 同一根因：C: 满，corepack 无法下载 pnpm 二进制 | 清理 89 个**孤立** PG 数据目录（Phase 2 遗留，6.5 GB）后 C: 由 **0.001 GB → 6.08 GB** 可用；corepack 成功下载 pnpm **12.15.0** |

**pnpm 可执行位置**：corepack 的 shim 目录 `D:\Dev\nodejs\node_modules\corepack\shims` 必须加入
`PATH`，否则 `pnpm` 脚本内部再次解析 `pnpm` 会失败（`'pnpm' 不是内部或外部命令`）。
可用前缀：`$env:PATH="D:\Dev\nodejs\node_modules\corepack\shims;D:\Dev\nodejs;D:\Git\cmd;$env:PATH"`。

**真实 MinIO 数据目录一律放 D:，不要用 `%TEMP%`。**

## Web 门禁（车道 A + D 合并后，orchestrator 亲自运行）

| 命令 | 实际结果 |
|---|---|
| `pnpm lint:web` | 退出码 **0**（0 errors；2 条 `react-refresh/only-export-components` 警告为 `SessionProvider.tsx` 既有） |
| `pnpm typecheck:web` | 退出码 **0** |
| `pnpm test:web` | 退出码 **0**，`vitest/full: cases=213; errors=0`（较暂停前 211 增加 2，为接管就绪态新增用例） |
| `pnpm build:web` | 退出码 **0**，2203 modules，`dist/assets/index-*.js` 915.23 kB（>500 kB 警告为既有边界，未扩范围处理） |

### H（稳定性前置）实现要点（已读实际改动文件，非采信总结）

- `AppLayout.tsx`：`data-testid="workspace-ready"` 仅在**本棵树完成自身首次持久读之后**渲染，
  并携带 `data-ownership-generation={port.ownershipGeneration}`；接管/重新认证/身份重检导致
  `WorkspaceScreen` 按 `partitionKey:ownershipGeneration` 重挂时该节点随旧树卸载。**派生自已提交状态，无定时器**。
- `App.tsx`：接管提交期间渲染显式恢复态（`workspace-restoring`），使旧树被**卸载**而非仅陈旧。
- `fixtures.ts`：新增 `expectWorkspaceSettled(page)`（web-first，`toHaveCount(1)`，无固定 sleep），
  `openNote()` 与 `expectTakeoverComplete()` 均先等就绪；未拿到锁的接管仍会在 `stage=takeover_enter` 失败——
  这是诚实结果而非慢通过。
- 断言强度未放宽：`retries: 0`、`forbidOnly`、既有 16 个标签全部保留。

## H - 稳定性前置验收（orchestrator 亲自执行）

真实临时 PostgreSQL（仓库外控制器）+ 真实 Chromium。**每个 focused 循环与每次全量都使用全新一次性库**
（`DROP DATABASE IF EXISTS ... WITH (FORCE)` 后重建），旧的 `reports/` 由 runner 自身的报告守卫覆盖。

| 运行 | 库 | 实际结果 |
|---|---|---|
| focused `single_writer_takeover` ×1 | `revnote_web_e2e_local` | **PASS**（exit 0） |
| focused ×4（每次独立新库） | `revnote_web_e2e_run1..4` | **4/4 PASS**（exit 0） |
| **focused 合计** | 5 个不同的一次性库 | **5/5 PASS** |
| **full #1** | `revnote_web_e2e_full1` | **PASS**（exit 0），`Web E2E full: passed; browser report guarded` |
| **full #2** | `revnote_web_e2e_full2` | **PASS**（exit 0），同上 |
| `pnpm check:web-reports` | 承接 full #2 报告 | 退出码 **0**：`vitest/full: 213 passed; zero skips, retries or errors` / `playwright/full: 16 passed; zero skips, retries or errors` |

**未削弱任何承重断言**：`retries: 0` 与 `forbidOnly` 保持不变；16 个既有 E2E 标签全部保留且全部通过；
未新增 skip/fixme；未放宽任何 `toBeVisible`。修复方式是「等产品就绪态」而非「等更久」：
`workspace-ready` 派生自已提交状态并绑定 `ownershipGeneration`，接管期间旧树被**卸载**而非仅陈旧。

### 根因与修复（已读实际改动文件）

- 根因：接管后 `WorkspaceScreen` 按 `partitionKey:ownershipGeneration` 重挂，选中笔记被重置，而
  `openNote()` 直接点击，可能点进即将被替换的旧树。
- `AppLayout.tsx`：仅在 `!loading && !loadFailed && state !== null` 时渲染列与
  `data-testid="workspace-ready"`（携带 `data-ownership-generation`、`data-bootstrapped`），因此
  `disabled={readOnly || !loaded}` 只会在「已自证就绪」的树上求值。
- `App.tsx`：接管提交窗口内 `runtime` 置空（`active = takeoverBusy ? null : runtime`），旧树被卸载。
- `fixtures.ts`：新增 `expectWorkspaceSettled(page)`（`toHaveCount(1)`，web-first，无固定 sleep）；
  `expectWorkspaceReady` 保留原名同签名且**保留**原有 enabled 断言，只是改为在权威信号之后执行；
  `openNote()` 自身先等就绪，不再依赖调用方先跑 `expectTakeoverComplete`。
- 未拿到锁的接管仍会在 `stage=takeover_enter` 失败——这是诚实结果，不是慢通过。

### 附件组复跑（车道 C 修复后，真实 PG + 真实 MinIO）

| 运行 | 实际结果 |
|---|---|
| `pytest apps/api/tests/api -m attachments -q` | **22 passed, 0 failed** |
| 定向复跑（非成员 / GC / 并发双上传 / 摘要不符） | **3 passed**（`-k "non_member or gc_removes_expired or two_concurrent or hash"` 选中 3 项） |

非成员用例已改为真正携带**他人 token**，并新增更严的一例：调用者确实是**另一个工作区的成员**，
成员闸门通过，因此 404 只能来自服务自身的 `(workspace_id, attachment_id)` 检索——
这比仅靠路径级校验更能证明越权被挡住。属主附件在 404 之后仍逐字节可读且对象数为 1。

**凭证注意**：真实存储组必须使用与当前运行中的 external 模式实例**相同**的一组凭证（由环境变量提供，本文不记录其值）。凭证不一致会报 `InvalidAccessKeyId`，并表现为夹具错误而非产品失败——排障时先确认凭证来源，再怀疑代码。

### 宽限期 GC 运维脚本（orchestrator 亲自执行，真实 PG + 真实 MinIO）

`python -m revnote_api.tools.attachments_gc`，真实临时 PG + 真实 MinIO（`127.0.0.1:19300`）。
先置入两行**都已过期**的附件：一行 `pending`（期望被回收）、一行 `ready`（期望永不回收）。

| 步骤 | 实际结果 |
|---|---|
| `--dry-run` | `scanned=1 rows_matched=1 objects_matched=1 failed=0 (dry-run: would collect)` |
| 实际运行 | `scanned=1 rows_deleted=1 objects_deleted=1 failed=0 (collected)`，退出码 **0** |
| 运行后核对 | 过期 `ready` 保留 **2** 行；过期 `pending` 剩余 **0** 行 |

结论：**只删过期 `pending`（行 + 对象），`ready` 从不自动删除**；计数输出只含数字，不含文件名、
对象键或凭据。`--dry-run` 确实不删除。

**一次排障记录（避免误判为产品缺陷）**：首次运行时 `failed=1 stage=object_delete`，原因是我的
实验设置里 `OBJECT_STORAGE_BUCKET` 指向了一个**从未创建**的桶，`remove_object` 因此失败。
用 `ensure_bucket` 建桶后同一脚本 `failed=0`。适配器把「桶不可用」映射为 `StorageUnavailable`
是正确行为，不是缺陷。

### E2E runner 的 owned MinIO 模式（orchestrator 实测发现并修复的真实缺陷）

`apps/api/scripts/run_web_e2e.py` 的 `owned` 模式首次实测**超时失败**：

```
LifecycleError: Object storage at 127.0.0.1:61029 did not become ready before the deadline.
```

**根因（真实缺陷，非环境问题）**：`_await_storage_ready()` 轮询的是适配器的
`BlobStore.ready()`，而该方法是 `bucket_exists()`——它回答的是「**本次运行的桶**在不在」。
但桶正是**在等待返回之后**才由 `export_to()` 创建的，于是等待了一个只有下一步才能产生的东西，
在服务完全健康的情况下空转到整个 deadline。

实测佐证（真实 MinIO）：`ready()` 对**不存在**的桶返回 `False`，对**存在**的桶返回 `True`。

**修复**：新增 `storage/bootstrap.py::probe_store(store_or_settings) -> bool`，用
`list_buckets()` 作为最小可达性探针——它同时证明「服务可达 + 凭证有效 + SDK 接线正确」，
且**不要求任何桶存在**；`_await_storage_ready` 改为轮询它。`BlobStore.ready()` 语义保持不变
（服务进程判断「我的桶在不在」仍然是正确的问题）。

修复后 owned 模式实测通过：

| 检查 | 实际结果 |
|---|---|
| `verify_minio_binary()` | SHA-256 与 pin 一致（`af709e6b…1861b4`），不匹配则拒绝启动 |
| 启动 + 适配器就绪轮询 | **OK** |
| 承载凭证的对象 `repr` | **不含**凭证（`repr has credential: False`） |
| `ObjectStorageSettings` 的 `repr` | 凭证掩码为 `***` |
| 经项目自身 `ensure_bucket` 建桶 | **OK** |
| `finally` 停止子进程 | 端口**已释放**，临时数据目录**已移除** |

数据目录固定 `D:\tmp`（C: 空间不足会让 MinIO 返回 `XMinioStorageFull` 507）。
该缺陷若不修，CI 的 `api-attachments`/`web-e2e` 会在等桶上空转到 deadline。已补 ruff 检查（`All checks passed!`）。

### 附件报告守卫（真实运行）

真实 PG + 真实 MinIO，产出 `api-attachments.xml` 后运行守卫：

| 检查 | 实际结果 |
|---|---|
| `pytest apps/api/tests/api -m attachments --junitxml=api-attachments.xml` | **22 passed / 0 failed / 0 skipped**，`73 deselected` |
| JUnit 报告自身 | 22 个 `testcase`，**`<skipped>` 元素 0 个** |
| `check_test_reports.py --group attachments` | 退出码 **0**：`api/attachments: passed=22 required=12; zero skips/failures/errors` |
| 焦点模式提示 | 仍打印 `Focused group evidence only; the default eight-group gate is still required.`（未削弱） |

**no-skip 契约实测**：清空全部 `OBJECT_STORAGE_*` 而保留 `REQUIRE_OBJECT_STORAGE_TESTS=1` 时，
结果是 **22 errors**（而非 22 skipped）——「承诺了真存储却没有」是硬失败，符合 D14。

## 文档交付（orchestrator 抽查核对）

| 文件 | 状态 | 核对方式 |
|---|---|---|
| `docs/plan-phase5.md` | 已落盘 | 由获批草稿逐字复制 + 批准抬头 |
| `docs/phase5-attachments-api.md` | 已落盘（27 KB，12 节） | **抽查与真实代码一致**，非采信总结 |
| `docs/phase5-validation.md` | 本文件 | 仅记录实际运行结果 |

对 `phase5-attachments-api.md` 的抽查（防止文档与实现漂移）：

- 六个错误码的**状态码与 retryable** 与 `api/errors.py::ERROR_SPECS` 逐项一致：
  `attachment_already_ready` 409/False、`attachment_hash_mismatch` 409/False、`attachment_expired` 409/False、
  `attachment_too_large` 413/False、`attachment_unsupported_type` 400/False、
  `attachment_storage_unavailable` 503/**True**。
- 文档称「禁用部署返回 `503 attachment_storage_unavailable` 且 `details.reason="disabled"`」——
  与 `api/routes/attachments.py:217` 的真实实现一致，且由
  `test_a_disabled_deployment_refuses_to_create_but_still_reads` 覆盖。
- 文档称 `/capabilities` 恒为 `200` 且禁用时仍返回完整对象——与实现注释和用例一致。

未改动 `docs/phase3-sync-api-spec.md`（Phase 3 锁定契约保持原样）。

## ⚠ 风险闸口（Oracle，独立审查）与已发现缺陷

独立审查对 8 项承重主张的结论：**6 项 PASS，1 项 FAIL（单赢家），另有 2 个 CI 阻塞项**。
以下每条均由 orchestrator 亲自复验，而非采信总结。

### B1 — 严重数据丢失（已复现，修复中）

`upload_nonce` 在 `create` 时生成一次并**被所有上传尝试复用**，因此存储键是「每行」而非「每次尝试」；
并发双 PUT 写同一个键，输掉的一方在 `_discard_object` 中删掉**赢家**的对象，留下
`ready` + 有摘要 + **无字节**的永不可读附件。

真实 PG + 真实 MinIO，12 轮，两次并发 PUT 发送**不同**负载：

```
round 0    : statuses=[200,409] GET=200 bytes_ok
round 1..11: statuses=[200,409] GET=503 -> LOSS
--- rounds=12 unreadable_ready=11 corrupt=0
```

**11/12 轮丢失字节。** 既有 `test_two_concurrent_uploads_produce_one_winner_and_one_object` 抓不到，因为它
两次都上传**相同**字节。本文件早前记录的「并发双上传单赢家通过」因此**不足以证明该主张**——已如实更正。

### B2 — E2E runner 的 MinIO 生命周期是死代码（已确认，修复中）

`start_owned_storage` / `external_storage` / `stop_owned_storage` / `export_to` 均**无调用点**；
`main()` 直接从 `child_environment` 走到 `_alembic`/`_api`/Vite，没有任何子进程收到
`OBJECT_STORAGE_*` / `ATTACHMENTS_ENABLED`。后果：4 个新 E2E 标签无法通过（上传按钮保持禁用），
`check:web-reports` 会因缺少必需标签而失败，`web-e2e` 会变红。
（早前修的 `probe_store` 是必要的，但**不充分**：它修的是一个从未被调用的函数内部的死锁。）

### B3 — 已由车道 E1 修复

`test_attachments.py` 同时带 `api` 与 `attachments` 标记，`api` job 的
`-m "api and not api_concurrency and not transport"` 会收集全部附件用例，而该 job 无 MinIO 服务；
`GITHUB_ACTIONS` 下 `object_storage_is_required()` 为真，故是硬失败而非 skip。已改为
`... and not transport and not attachments`（ci.yml:188 已核实）。

### 已修：零字节上传曾返回 500

车道 G 提示、orchestrator 实测确认：空请求体曾返回 **500 `internal`**（CAS 写入 `byte_size=0`
违反 `CHECK (byte_size > 0)`）。修复后为 **400 `invalid_request`**，且行保持 `pending`（由 GC 负责）。
修复含两处：`store_content` 在写后拒绝 size 0 并删除对象；`_SAFE_CODES` 加入 `invalid_request`
——否则 `AttachmentError` 的强制归一会把它悄悄变回 `internal`。已加回归用例。

### 关于本文件早前数字的更正

前面记录的「附件组 22 passed」「真实字节并发单赢家通过」等结论，是在 **B1 未修复**的状态下取得的：
它们证明的是「相同字节的并发被正确拒绝」，而非「不同字节时赢家字节仍可读」。B1 修复后的数字将重新记录。

### B4 — 未配置对象存储时返回 500 而非文档承诺的 503（已复现，修复中）

由文档车道在核对时发现、orchestrator 实测确认：

```
ATTACHMENTS_ENABLED=true 且四个 OBJECT_STORAGE_* 均未设置
POST /api/v1/workspaces/{ws}/attachments  ->  500   code=internal
服务端日志: unhandled_exception route=/workspaces/{workspace_id}/attachments error_class=StorageUnavailable
GET  /api/v1/capabilities                 ->  200   （正确）
```

根因：`routes/attachments.py::build_store` 在**依赖**（`get_attachment_service`）中抛出
`storage.ports.StorageUnavailable`；依赖先于路由体求值，所以路由内的 `_require_enabled` /
`AttachmentError` 翻译根本不会执行。而 `api/errors.py` 注册了 `ApiError`/`SyncKernelError`/
`RequestValidationError`/`StarletteHTTPException`/`Exception` 五个处理器，**没有** `BlobStoreError`
的处理器，于是落到兜底 `internal`。

路由自身的 docstring 明确承诺相反的行为（「503 可重试，绝不为 500」），因此这是实现与契约不一致，
不是可接受的差异。修复方向：为 `StorageUnavailable` 注册处理器，复用**既有**错误码
`attachment_storage_unavailable`（`ERROR_SPECS` 中已是 503 且 `retryable=True`，
`Retry-After: 1` 与响应信封可免费复用）；不新增错误码、不改任何既有码的语义。

**这一条也是本轮第四个被「文档/审查」而非「测试」发现的缺陷**——现有用例都配置了真实存储，
因此没有任何一条覆盖「配置了开关但没配置存储」这个真实部署形态。

## B1 / B2 / B4 修复后复验（orchestrator 亲自执行）

### B1 — 单赢家（已修复并复验）

修复要点：对象键改为**每次尝试**一枚 nonce（`store_content` 内 `attempt_nonce = new_id()`），
CAS 谓词为 `id + workspace_id + state='pending'`，并把**赢家的 `storage_key` 写回行**
（`SET storage_key = :storage_key`）。输家只删自己刚写的键。

真实 PG + 真实 MinIO，**12 轮**、每轮两次并发 PUT 发送**不同**负载：

| 阶段 | 结果 |
|---|---|
| 修复前 | `unreadable_ready=11/12`（`ready` 行有摘要无字节，GET 503） |
| 修复后 | **`unreadable_ready=0/12`，`corrupt=0`**，每轮 `statuses=[200,409]` 且 `GET=200 bytes_ok` |

新增承重回归用例 `test_the_winner_of_a_concurrent_upload_keeps_its_own_bytes`：两次并发上传
**不同**字节，断言恰好一个对象、下载成功、内容逐字等于两次尝试之一、且行上摘要与下载内容一致。
（既有的 `test_two_concurrent_uploads_produce_one_winner_and_one_object` 两次上传**相同**字节，
因此永远抓不到此缺陷——已在用例 docstring 中说明。）

### B2 — E2E runner 的 MinIO 生命周期（已接线并复验）

`start_object_storage` / `prepare_object_storage` 在 `main()` 内、**任何子进程启动之前**调用
（run_web_e2e.py:1468-1469）；`finally` 中按「删对象 → 删桶 → 停进程」顺序调用
`storage.discard()` 与 `stop_owned_storage()`（:1581/:1585）。

真实全量 E2E（外部 MinIO 19300，全新一次性库）：**通过**，且运行后 MinIO 中**没有残留
`revnote-att-*` 桶**，证明清理路径确实执行了。

### B4 — 未配置存储的 500 → 503（已修复并复验）

新增 `StorageUnavailable` 异常处理器，复用既有错误码 `attachment_storage_unavailable`。

| 场景 | 修复前 | 修复后 |
|---|---|---|
| `ATTACHMENTS_ENABLED=true` 且四个 `OBJECT_STORAGE_*` 均未设置，POST 创建 | **500 `internal`** | **503 `attachment_storage_unavailable`** |
| `GET /capabilities` | 200 | 200（不变） |

`api/errors.py` 引入 `storage.ports.StorageUnavailable` 后仍然**不会**加载 SDK
（实测 `minio loaded: False`），内核边界测试保持通过。

## 最终门禁（本轮实际运行）

| 检查 | 实际结果 |
|---|---|
| `ruff check apps/api` | **All checks passed** |
| `ruff format --check apps/api` | **136 files already formatted** |
| `pytest apps/api/tests/unit -q` | **1192 passed** |
| `pytest apps/api/tests -q`（真实 PG + 真实 MinIO） | **1509 passed, 0 failed, 0 skipped** |
| `pytest apps/api/tests/api -m attachments -q` | **24 passed** |
| `check_test_reports.py --group attachments` | **passed=24 required=12; zero skips/failures/errors** |
| `pnpm lint:web` | 退出码 **0**（2 条既有警告） |
| `pnpm typecheck:web` | 退出码 **0** |
| `pnpm test:web` | 退出码 **0**，`cases=213; errors=0` |
| `pnpm build:web` | 退出码 **0** |
| `pnpm test:e2e`（全量，20 标签） | **通过**，`Web E2E full: passed; browser report guarded` |
| `pnpm check:web-reports` | 退出码 **0**：`vitest/full: 213 passed` / **`playwright/full: 20 passed; zero skips, retries or errors`** |

E2E 的 4 个附件标签（`attachment_upload_insert`、`attachment_cross_client_read`、
`attachment_authorization`、`attachment_offline_gate`）在**真实浏览器 + 真实 MinIO** 下全部通过；
规格文件 `apps/web/e2e/attachments.spec.ts` 共 4 个 `test(...)`，全仓 spec 标题总数正好 **20**，
`report-guard` 的自检因此自然转绿（未放宽任何断言）。
