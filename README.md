# Revnote

Sync Kernel 优先的自有笔记同步服务。自有 Web 客户端走原生 API；Joplin Server 协议是**可选的兼容适配器**（Phase 6，默认关闭），不是 Data API、不是产品主路径。

**当前为受控预览，不是公网生产部署。** Phase 4 已将 Web 接入 Phase 3 的认证与原生 HTTP push/pull/snapshot：本机持久化（IndexedDB）、会话轮换、冻结命令队列、离线可恢复、人工冲突处理与真实浏览器验收均已完成，验收证据见 [Phase 4 实际验收记录](docs/phase4-validation.md)。仍不提供完全离线冷启动、后台同步、附件、Joplin 桥与自动合并。API 可达不代表 Web 正文已保存；本地缓存不是备份。附件与完整部署不在本阶段范围内。

当前尚未选定项目许可证；公开源码不等于已授予开源许可。Compose/the S3-compatible store 的镜像拉取与运行也尚未完成验收，详见下文限制。

## 技术栈与目录

Python 3.12+、FastAPI、uv、SQLAlchemy 2.x async、asyncpg、Alembic；仅 PostgreSQL。附件存储方向固定为 the S3-compatible store。

Web：Vite、React 19、TypeScript、TanStack Router/Query、Tailwind CSS v4、TipTap。

```text
apps/api/                 FastAPI、认证/同步内核、迁移、PG/HTTP测试、Dockerfile
apps/web/                 暗色三栏、Markdown 源码编辑、IndexedDB、原生 API 同步
docs/plan-scaffold.md      已确认计划、领域表草图及P0不变量
docs/plan-phase2.md        Phase 2 已确认领域模型与事务写路径契约
docs/plan-phase3.md        Phase 3 锁定计划、迁移与验收责任
docs/phase3-sync-api-spec.md 原生 HTTP API v1 协议
docs/plan-phase4.md        已批准的 Web 持久化、会话、同步与浏览器验收计划
docs/phase4-validation.md  Phase 4 实际运行与发布证据
docs/build-conventions.md Build补充约定
.github/workflows/        检查与Actions→GHCR
docker-compose.yml        官方PostgreSQL、the S3-compatible store、一次性私有建桶
pyproject.toml / uv.lock   根Python workspace与唯一锁文件
pnpm-workspace.yaml       Web workspace，根pnpm锁文件
```

Phase 2 范围见 [内核计划](docs/plan-phase2.md)。`0001_baseline` 保持原空基线，`0002_sync_kernel_core` 创建六张内核表；`0003_auth_sessions_members` 扩展 users 并新增 auth_sessions/auth_access_tokens/auth_refresh_tokens/workspace_members，总计十张领域表。所有 schema 来自 Alembic，不使用 `create_all()`。

## 前置条件

- Python 3.12（uv 可管理安装）、uv。
- Node.js 24 LTS（具体开发版本见 `.node-version`）。
- pnpm，版本以根 `package.json` 的 `packageManager` 为准。
- Docker Engine/Desktop 和 Compose v2：只运行官方基础设施镜像。

Windows PowerShell 若拒绝执行 `pnpm.ps1`，将下文 `pnpm` 替换为 `pnpm.cmd`；无需调整系统执行策略。

**本地禁止 `docker build` 或 `docker compose build`。** Compose 不包含本仓镜像构建；API/Web 在宿主机运行。镜像仅由 GitHub Actions 构建并发布到 GHCR。

## 首次安装

以下命令从仓库根目录执行：

```powershell
Copy-Item .env.example .env
uv sync --locked
pnpm install --frozen-lockfile
```

已存在 `.env` 时不要覆盖；示例凭据仅供本机开发。修改 PG 凭据时同步调整 `DATABASE_URL`，密码包含特殊字符时需 URL 编码。

`.env` 不提交。API、迁移通过 `--env-file .env` 显式加载；Vite 从根目录读取配置。只有 `VITE_` 前缀值可公开，数据库和 the S3-compatible store 凭据严禁加该前缀。

### 启动 PostgreSQL 与 the S3-compatible store

```powershell
docker compose up -d
docker compose ps -a
docker compose logs object store-wait object store-init object store-private
```

预期：PostgreSQL 健康；`object store-wait` 就绪探测、`object store-init` 建桶、`object store-private` 私有策略设置均正常退出且退出码为 0。the S3-compatible store 服务本身不假定镜像内有探测工具，由官方 mc sidecar 检查就绪。若初始化失败，查看日志，不要把服务退出直接当成功。**the S3-compatible store 镜像尚未拉取验收，可能阻止整栈启动；可先用 `docker compose up -d postgres` 单独启动数据库。**

默认本机端口：PG `5432`、the S3-compatible store API `9000`、the S3-compatible store Console `9001`。bucket 私有，不开放匿名访问。宿主机 API 默认使用 `127.0.0.1`；未来若容器化运行，内部地址与浏览器公开地址必须分开配置。

### 执行迁移

```powershell
uv run --package revnote-api --env-file .env alembic -c apps/api/alembic.ini upgrade head
uv run --package revnote-api --env-file .env alembic -c apps/api/alembic.ini current
```

不要重复执行 `alembic init`。升级 head 会建立 Phase 3 十表并回填旧 workspace 的 owner member；旧用户 password_hash=NULL，不能本地密码登录。旧邮箱按 lower(btrim(email)) 碰撞时迁移明确失败，不合并账号。0003→0002 保留原笔记/事件，但删除认证数据；降到 `0001_baseline` 会删除所有领域表及其数据，不得在日常开发库或生产库自动降级。

### 启动 API 与 Web

终端一：

```powershell
uv run --package revnote-api --env-file .env uvicorn revnote_api.main:create_app --factory --reload --host 127.0.0.1 --port 8000
```

终端二：

```powershell
pnpm --filter @revnote/web dev --host 127.0.0.1
```

打开 `http://127.0.0.1:5173`；API hello 在 `http://127.0.0.1:8000/`，存活检查在 `/health`。

Web 请求 `/api/health` 时，Vite **仅精确将该路径**转发至 API `/health`；业务 `/api/v1/...` 保留完整前缀原样转发。`API_PROXY_TARGET` 只由开发服务器读取；默认目标为 `http://127.0.0.1:8000`。不使用通配 CORS。生产静态构建不包含 Vite 代理，部署仍需同源反向代理。

`/health` 只证明进程存活，**不检查 PG 或 the S3-compatible store**。依赖未启动仍可体验空编辑器，但迁移和数据库测试需要真实 PG。

## 检查与验收

Phase 4 的批准范围见 [实施计划](docs/plan-phase4.md)，进行中的实际运行结果见 [Phase 4 验收记录](docs/phase4-validation.md)。记录中的专项通过不等于整个阶段或发布已通过。

```powershell
uv run ruff check apps/api
uv run ruff format --check apps/api
uv run pytest apps/api/tests
pnpm --filter @revnote/web lint
pnpm --filter @revnote/web typecheck
pnpm --filter @revnote/web build
pnpm --filter @revnote/web check:markdown
pnpm test:web
pnpm check:api-reports
```

真实数据库测试使用 **独立可丢弃 PostgreSQL 测试库**：

```powershell
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://revnote:你的开发密码@127.0.0.1:5432/revnote_test'
$env:TEST_MIGRATION_DATABASE_URL = 'postgresql+asyncpg://revnote:你的开发密码@127.0.0.1:5432/revnote_migrations_test'
$env:REQUIRE_POSTGRES_TESTS = '1'
uv run pytest apps/api/tests
Remove-Item Env:TEST_DATABASE_URL
Remove-Item Env:TEST_MIGRATION_DATABASE_URL
Remove-Item Env:REQUIRE_POSTGRES_TESTS
```

两库须事先存在且相互独立，均必须可丢弃：普通库运行写入与并发，迁移库独占升降级。CI 或 `REQUIRE_POSTGRES_TESTS=1` 缺少任一 URL 直接失败；已配置的连接错误不能跳过。无 DB 时只运行 `apps/api/tests/unit`；**skip 不是验收通过**。不使用 SQLite 替代 PG，也不将外层测试回滚当作多连接隔离。

### Web 持久化与浏览器验收

- access 仅在内存。“记住登录”默认不勾选；只有显式勾选才在 IndexedDB 保存 refresh，密码不保存。未勾选时刷新页面可读取未退出账号的本机缓存，但需要重新登录才能同步。
- 同 origin 仅一个标签持有 Web Lock，其他标签只读。关闭原写标签后可显式接管；不支持 Web Locks 的环境禁止写入。初始支持桌面 Chromium/Edge 安全上下文，不外推移动 Safari 支持。
- “正在写入本地”“已写入本地”与工作区的待同步/同步状态独立。只有 IndexedDB 事务完成才报告本地保存；存储失败时保留页面输入，离开前复制或导出。
- 冻结命令先持久化后发送；未知结果使用原 op ID 和原 payload 确认。冲突需要人工选择，不自动合并或覆盖；远端删除的脏稿只能显式另存新 ID。
- Markdown 原文为正文真相；打开、切换查看、仅改标题不重新序列化正文。富文本转换需显式确认并先保存原文恢复点，不保证任意 Markdown 富文本无损。
- 退出会隐藏并锁住该账号本机缓存，不自动删除队列。未确认服务端 logout 成功时只宣称本机退出。本机缓存不是磁盘加密，也不是本地身份认证。
- 已加载页面可断网编辑；静态资源可达时重新打开可恢复已落盘草稿。**不支持完全离线冷启动、页面关闭后的后台同步或 PWA。** 浏览器清理/驱逐存储仍可能丢失本机数据，应另行备份。

浏览器验收使用第三个**新建、空、独占的一次性 PostgreSQL 库**，不能复用普通测试库、迁移库或开发库。测试库名必须为 `revnote_web_e2e` 或 `revnote_web_e2e_<id>`，连接只允许 loopback：

```powershell
# 由外部生命周期工具创建并拥有临时 PG 与此一次性库；凭据通过环境注入，不写入仓库。
$env:WEB_E2E_DATABASE_URL = 'postgresql+asyncpg://测试账号:测试密码@127.0.0.1:5432/revnote_web_e2e_local'
$env:REQUIRE_POSTGRES_TESTS = '1'
pnpm --filter @revnote/web exec playwright install chromium
pnpm test:e2e
pnpm check:web-reports
Remove-Item Env:WEB_E2E_DATABASE_URL
```

runner 显式迁移并拥有 API/Vite/浏览器子进程，在 finally 清理，不接管既有服务、不读取开发 `.env`；PG 的停止责任属于外部控制器。不要在含适用根 `.env` 的工作区运行此 runner；使用独立测试工作区，不删除用户开发配置。详见 [浏览器验收清单](apps/web/e2e/README.md)。默认关闭 trace、HAR、storageState、视频与截图，报告仅保存 hash、固定标签和计数，不提交认证状态、正文或数据库目录。

Web 报告门禁要求各组非零、无 skip/todo/failure/error、无 retries/only 且覆盖承重标签。focused 报告与 full 分目录，局部绿色不代替全量验收。`check:api-reports` 要求当前 CI 七组 JUnit 报告及原有承重名称，不以总数替代覆盖。

### 认证与原生 API

协议和示例见 [API v1 规格](docs/phase3-sync-api-spec.md)，OpenAPI 在 `/docs` 与 `/openapi.json`。注册默认关闭；开发环境可在 .env 显式设 `AUTH_REGISTRATION_OPEN=true`，重启 API 后使用 `/api/v1/auth/register` 原子创建账号和默认 workspace（不自动登录），再 `/auth/login` 获取 token pair。

密码为 **8–256 字符、UTF-8≤1024 字节**，不 trim/截断，Argon2id（64 MiB/time3/parallelism4）。opaque access 默认600s；refresh轮换，session绝对30d、不续期。旧refresh复用会撤销整个session，已签发access也立即失效；客户端应串行刷新并安全存储token，不放URL或日志。

业务请求带 `Authorization: Bearer <access>`。写入仅走 `/api/v1/workspaces/{workspace_id}/sync/push`，每批1–50op、JSON≤2MiB、body_md≤256KiB，每op独立事务、按顺序执行。须逐项处理 applied/replayed/conflict/rejected/unknown/not_attempted；收到unknown或丢失响应时保留同一op_id与payload重试。

pull的since为排他十进制字符串（默认0），保留完整事件并独立去重objects。snapshot 在**无 `limit`/`cursor`** 时仍是 Phase 3 的单 RR 全量：默认最多 10,000 对象 / 16 MiB，超限整次 409 `snapshot_too_large`。显式 `limit`（+可选 `cursor`）启用 Phase 7 分页 bootstrap：首页钉住 `as_of_seq`，续页按同一水位过滤、稳定序、不透明签名游标；默认单页 1,000 对象 / 4 MiB，跨页硬顶 100,000 对象，超限分别 413 `snapshot_page_too_large` / 409 `snapshot_total_too_large`。分页拼完后仍以 `pull since=as_of_seq` 追上增量。详见 [Phase 7 snapshot 分页](docs/phase7-snapshot-pagination.md)。tree 不含正文，note 端点返回授权笔记正文。所有 seq/rev/cursor 使用十进制字符串，避免 JavaScript 精度损失；分页游标是不透明字符串，客户端不得解析。

通用错误信封含code/message/request_id/retryable/details；非成员与不存在workspace统一404。合法X-Request-ID原样回传，非法替换。CORS默认空列表、无cookie凭据；真实限流尚未实现，公网部署必须另行配置防护。

浏览器承重场景覆盖登录、八操作、刷新持久化、API 停止/恢复、断网编辑、提交后丢响应、A ACK 不吞 B、CAS 冲突、删除恢复、双标签接管、refresh 不确定性、账号隔离、游标/重建、Markdown 原文与 390px 栏位切换。是否已通过只以实际验收记录为准；测试文件存在不等于运行通过。

### 当前验收与限制

- Phase 3 本地完整验收 **1157 passed、0 skipped、0 failed**；HTTP普通／并发／真实传输组分别64／6／3项，所有组no-skip及承重用例守卫通过。逐提交和命令证据见 [验收记录](docs/phase3-validation.md)；以下脚手架/Phase 2 数字为历史基线，不代替当前验收，远端CI以最终实际运行记录为准。
- 已用仓库外临时 Playwright 环境实测：真实 API→Vite 代理、桌面三栏、Markdown 输出、刷新清空、API 停止/恢复、无效 JSON 拒绝、390px 栏位切换与内存内容保留，无浏览器运行时错误。没有给仓库加入 E2E 平台依赖。
- 脚手架阶段已完成 API 56 项测试、Web lint/typecheck/build、7 项 Markdown 检查，并由 [Actions #40777468266](https://github.com/LeiSureLyYrsc/Revnote/actions/runs/40777468266) 验证 API/Web/GHCR 全绿；这是历史版本证据，不替代 Phase 2 验收。
- Phase 2 schema 提交 `fa99b83` 前，对精确暂存快照完成锁定安装、Ruff、105 项单元测试及 29 项真实 PostgreSQL 迁移测试（无 skip），`alembic check` 无差异，原基线仅版本表、新 head 恰好六张领域表。
- 事务写提交 `8948fd9` 前，Ruff、281 项单元测试及 109 项真实 PostgreSQL 非并发内核测试通过。
- 并发提交 `0473b17` 前，45 项真实 PostgreSQL 并发/原子性测试通过；完整 `pytest apps/api/tests -v` 为 **476 passed、0 skipped**（205.58s）。覆盖 8×20 连续 seq、同 op 并发重放、CAS 单赢家、事务故障回滚、末尾 ledger 真实冲突兜底、三连接提交/回滚可见性及提交确认丢失恢复。单独并发组耗时 51.56s，这些测试耗时不是生产吞吐指标。
- 最终 CI 的三组 no-skip/必需用例守卫已用该次真实 JUnit 报告验证：普通 PG 121 项、并发/原子性 45 项、迁移 29 项，均无 skip/failure/error；Actions 结果以推送后的实际运行记录为准。
- Web 构建通过，但编辑器入口约 859 kB（gzip 约 271 kB），仍有 chunk 大小警告；暂不为脚手架引入拆包优化工程。
- 本机未检测到 Docker CLI/运行时。本地 PostgreSQL 验证使用仓库外临时实例和两个独立可丢弃库，不使用 SQLite；这**不证明 Compose 或 the S3-compatible store 可运行**。本次验收的临时实例已停止。
- Compose 官方镜像在当前环境未完成拉取与运行验证；若官方 the S3-compatible store 分发源不可用，应停止并确认来源，不切换第三方镜像或其他 S3 产品。
- GitHub 仓库为 [LeiSureLyYrsc/Revnote](https://github.com/LeiSureLyYrsc/Revnote)。Actions、API 镜像构建和 GHCR 发布状态以实际运行记录为准，文件已生成不代表已验收。

**Phase 5（附件）已实现**，协议细节见 [附件 API 附录](docs/phase5-attachments-api.md)，范围与决策见 [实施计划](docs/plan-phase5.md)：

- 附件上传与下载**全部经 API 代理**，浏览器不直连对象存储；`upload.url` 是相对路径，协议里没有预签名。
- `GET /api/v1/capabilities` 提供能力探测（永远 200，关闭时 `enabled:false` 仍带限额）。
- `pending → ready` 两阶段生命周期，上传与校验在**事务外**完成；`ready` 以**真实 SHA-256** 为准——边写边算，完成后回读复算，不采信 ETag/HEAD/客户端声明。
- `python -m revnote_api.tools.attachments_gc` 运维脚本，**只**清理过期 `pending`（行 + 对象），`ready` 永不自动回收。
- Web 上传界面带能力门控：旧服务缺 `/capabilities`、离线、只读或无存储时入口显式禁用并说明原因。
- 附件**不是内核实体**：不写 `change_events`、不分配 `seq`、不出现在 `pull`/`snapshot`。

本轮补充（同属已实现，细节以附录为准）：

- 新增 5 个端点：`POST /workspaces/{workspace_id}/attachments`、`PUT /workspaces/{workspace_id}/attachments/{attachment_id}/content`、`GET /attachments/{attachment_id}`、`GET /attachments/{attachment_id}/content`、`GET /capabilities`。不删除、不改写 Phase 3 的任何端点或错误码，因此仍是 `/api/v1`，不引入 v2。
- `GET /capabilities` **不要求 token**（客户端在拿到 token 前就要决定是否显示附件入口），且不暴露任何工作区数据。
- 摘要校验的比对对象是**服务端回读复算**的 SHA-256；不一致时返回 `attachment_hash_mismatch`（409），并先删除对象、不留下 `ready` 行。
- GC 脚本支持 `--dry-run`、`--limit`、`--grace-seconds`，以运维命令运行，**不是后台调度器**；输出只有计数，从不打印对象键、文件名或 id。
- 附件不出现在 `tree`，也不推进 `workspaces.last_seq`；正文 Markdown 里的相对 URL 是唯一引用关系。

**Phase 5 明确不做**（不是遗漏，是范围决定）：

- 预签名直传/直读、桶 CORS、浏览器直连对象存储。
- 附件去重、附件删除（无端点、无 UI）、未引用 `ready` 对象的回收、后台调度器。
- `Range` 请求、断点续传、分片上传。
- 附件事件流、附件进入 `pull`/`snapshot`、扩 `change_events.entity_type`。
- Joplin 桥、自动三方合并、snapshot 分页。
- 公网加固：TLS、真实限流、备份恢复演练、同源生产部署与反向代理。
- 参考 CLI。

**Phase 5 已验证 / 未验证**（数字不在此复述，一律以 [Phase 5 实际验收记录](docs/phase5-validation.md) 为准；该文件中仍标「待填」或写明尚未运行的小节即尚未运行）：

- 已验证：**以 [Phase 5 实际验收记录](docs/phase5-validation.md) 各节的实际运行结果为准**；本文不复述任何未从该文件读到的数字或结论。该文件同时记录了已确认但尚未修复的缺陷（并发上传丢字节、E2E runner 存储生命周期未接线），阅读时须一并采信。**专项通过不等于整个阶段或发布已通过。**
- 未验证：Docker 镜像在真实 Docker 主机上的可拉取与可运行性（本机无 Docker/daemon，无法验证，只能由 CI 运行结果证明）；公网部署（TLS、真实限流、备份恢复演练、生产反向代理）。
- 未验证（附件 E2E）：该条为 Phase 5 中段记录；Phase 5 收口前的修复已把 `S3_*` 与 `ATTACHMENTS_ENABLED` 注入 E2E 子进程（`run_web_e2e.py` 的 `ObjectStorage.child_values`/`export_to`），CI `web-e2e` 以真 RustFS 运行。Phase 6 复核未再改动该接线，证据见 [Phase 6 实际验收记录](docs/phase6-validation.md) §H。

**Phase 6（Joplin Server 兼容桥）已实现**，范围与限制见 [桥说明](docs/phase6-joplin-bridge.md)，协议基线见 [Joplin 协议笔记](docs/joplin-protocol-notes.md)，范围决策见 [实施计划](docs/plan-phase6.md)：

- 默认关闭：`JOPLIN_BRIDGE_ENABLED=false` 时兼容路由**不注册**，部署行为与 Phase 5 完全一致；开启后挂载 `/api/sessions`、`/api/items/root:/...`、`/api/locks`、`/api/batch_items`。
- 身份：`POST /api/sessions` 用真实 `AuthService` 校验邮箱密码，随后**立即撤销**签发的原生 token 对；桥自己只保存随机会话 token 的 SHA-256。会话绑定登录时确定的唯一工作区。
- 条目：笔记/文件夹按 Joplin item 文本格式读写，全部经 `SyncWriteService`（`seq`、ledger、CAS、tombstone 均沿用内核）；原生 UUID 仍是权威 id，`joplin_id_map` 只记录客户端自选 key 的映射。
- Delta：把 `change_events` 压缩成 Joplin delta（create/update/delete 合并规则与上游一致），游标是原始 `seq`；非法游标返回 `400 resyncRequired`。
- 锁：`sync` 可并存、`exclusive` 互斥，按客户端给出的 `<type>_<clientType>_<clientId>` 释放；锁表与 `change_events` 完全隔离。
- 资源：`.resource/<key>` 走 Phase 5 附件状态机与 RustFS，**没有第二套存储**；仅接受既有 MIME 白名单。
- `GET /api/v1/capabilities` 仅**加法**声明 `joplin_bridge {enabled, protocol_baseline, mount}`；原生 `/api/v1` 契约、错误封套与 Phase 5 附件接口不变。

**Phase 6 明确不做**：Joplin Data API 作为产品基础、E2EE 服务端解密、分享、revision、附件删除（`.resource` 删除为已记录的 no-op）、自动三方合并、snapshot 分页、公网加固、参考 CLI、第二对象存储或 MinIO。

验证以 [Phase 6 实际验收记录](docs/phase6-validation.md) 为准；该文件只写实际运行结果，未跑的小节会明确写「未运行」。

**Phase 7（snapshot 分页 bootstrap）已实现**，协议与一致性边界见 [Phase 7 说明](docs/phase7-snapshot-pagination.md)：

- 无参 snapshot 路径与 Phase 3 字节兼容；响应形状不变，仍可返回 `409 snapshot_too_large`。
- 显式 `limit`/`cursor` 走分页：首页在 `REPEATABLE READ, READ ONLY` 中钉住 `as_of_seq`；续页各自新事务，只返回 `updated_seq <= as_of_seq` 且当前活跃的对象，按 `(folder,id)` 后 `(note,id)` 稳定排序。
- 游标是 HMAC 签名的不透明串，内嵌 workspace / `as_of_seq` / 最后键；坏游标 400 `invalid_cursor`，水位超前 400 `cursor_ahead`，低于保留 floor 410 `cursor_expired`。
- `GET /api/v1/capabilities` **加法**声明 `snapshot_pagination {enabled, page_max_objects, page_max_bytes, max_total_objects}`。
- Web transport 感知能力后循环拉页并原子合并，再进入既有 `activateSnapshot` + pull；旧服务端或关闭能力时回退到遗留单次 snapshot。
- 本阶段**没有**新迁移、没有服务端快照租约表、没有跨请求长事务；并发写入由 `pull since=as_of_seq` 补齐，不把更新泄漏进已钉页。

**Phase 7 明确不做**：自动三方合并、参考 CLI、公网加固、附件进入事件流、Joplin 功能扩张、MinIO/第二对象存储。验证以 [Phase 7 实际验收记录](docs/phase7-validation.md) 为准。

## Actions 与 GHCR

工作流先检查 API、双库真实 PG/并发/迁移、Web 单元/组件及独立 `web-e2e` 真实浏览器组，再构建 API 镜像；`image.needs` 同时依赖三组。发布目标为 `ghcr.io/<owner>/revnote-api`。PR 不推镜像；发布使用 GitHub Actions 的 `GITHUB_TOKEN`，不在仓库保存 PAT。

仓库需允许 Actions 写 Packages。工作流会在 `main` 推送或手动触发后检查并发布；没有成功运行记录时不应视为 CI 或 GHCR 已验收。

获准提交并推送后：

```powershell
gh run list --limit 5
gh run watch <run-id> --exit-status
```

失败时：

```powershell
gh run view <run-id> --log-failed
```

镜像只在 Actions 构建，本地不做替代构建。镜像文件不等于完整生产部署：TLS、生产反向代理、token安全存储、备份、真实限流和运维仍需配置。

## 停止与常见故障

- API/Web：各终端 `Ctrl+C`。
- 基础设施：`docker compose down`，保留命名卷；不要加 `-v`，除非明确确认删除开发数据。
- `docker` 不存在/daemon 未启动：先安装并启动 Docker，再验收基础设施；不影响无 DB 的 hello/health 与编辑器演示。
- API 请求失败：检查 API 终端、8000 端口和 `API_PROXY_TARGET`；修改 Vite 环境配置后重启 Web。
- DB 连接失败：检查 PG 健康、端口、URL 与卷中原有凭据；修改 `.env` 不会自动重置已有 PG 数据卷的密码。
- the S3-compatible store 初始化失败：检查 `object store-wait`、`object store-init`、`object store-private` 日志、凭据、bucket 名称及固定官方镜像版本是否可获取；凭据放入 `MC_HOST_` URL 时，特殊字符需要 URL 编码。
- 生产构建后的静态文件没有 Vite 代理；生产同源反向代理另行规划。

## 内核边界

Phase 2 只做工作区锁分配 seq、对象/事件/成功账本同事务、DB 唯一 op_id 幂等、rev/CAS、软删除和树移动。Python 命令由调用方提供 entity_id/op_id；修改 base_rev 或业务内容需新 op_id。重放返回首次事件的结果，不能取当前对象冒充旧版本。

新 op 修改/移动/再删已删对象返回 `EntityDeleted`，原删除 op 重放仍返回原结果。非空文件夹拒绝删除；子树移动只更新根并发一条 move 事件，不批量修改后代。合法无值变化更新也递增 rev/seq。HTTP层校验认证和workspace_members，push每op前重查；内核跨workspace约束仍不等于身份授权。

Kernel 内 seq/rev 保持整数，对外 JSON 使用十进制字符串。`min_retained_seq` 默认1，不推进、不清理；过期条件since<min_retained_seq-1。延迟event FK和ledger限制未来事件清理，清理策略必须协调引用与幂等窗口。写入顺序及取消/超时语义见 [内核补充](docs/phase2-sync-kernel-spec.md)。不shield业务COMMIT，提交期间取消或确认丢失不能当作回滚，应同op_id+payload重试确认；可疑连接invalidate。

本阶段不实现：独立服务端 Outbox、自动三方合并、自动上传冲突副本、the S3-compatible store 附件、搜索、E2EE、Joplin 桥、snapshot 分页与 PWA。预签名 PUT 不天然防覆盖，HEAD/ETag 不等于 SHA-256；这些后续另行设计，不扩入本阶段。
