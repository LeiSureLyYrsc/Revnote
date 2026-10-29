# Revnote

Sync Kernel 优先的自有笔记同步服务。自有 Web 客户端走原生 API；Joplin Server 协议仅是后续兼容适配器，非 Data API、非产品主路径。

**当前不是完整同步产品。** Phase 3 在事务内核上提供认证与原生 HTTP push/pull/snapshot、只读 API；Web 尚未接入这些业务接口，编辑器仍仅使用内存，刷新即丢失。API 可达不代表 Web 正文已保存。附件与完整部署不在本阶段范围内。

当前尚未选定项目许可证；公开源码不等于已授予开源许可。Compose/MinIO 的镜像拉取与运行也尚未完成验收，详见下文限制。

## 技术栈与目录

Python 3.12+、FastAPI、uv、SQLAlchemy 2.x async、asyncpg、Alembic；仅 PostgreSQL。附件存储方向固定为 MinIO。

Web：Vite、React 19、TypeScript、TanStack Router/Query、Tailwind CSS v4、TipTap。

```text
apps/api/                 FastAPI、认证/同步内核、迁移、PG/HTTP测试、Dockerfile
apps/web/                 暗色三栏、空TipTap、API状态与开发代理
docs/plan-scaffold.md      已确认计划、领域表草图及P0不变量
docs/plan-phase2.md        Phase 2 已确认领域模型与事务写路径契约
docs/plan-phase3.md        Phase 3 锁定计划、迁移与验收责任
docs/phase3-sync-api-spec.md 原生 HTTP API v1 协议
docs/build-conventions.md Build补充约定
.github/workflows/        检查与Actions→GHCR
docker-compose.yml        官方PostgreSQL、MinIO、一次性私有建桶
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

`.env` 不提交。API、迁移通过 `--env-file .env` 显式加载；Vite 从根目录读取配置。只有 `VITE_` 前缀值可公开，数据库和 MinIO 凭据严禁加该前缀。

### 启动 PostgreSQL 与 MinIO

```powershell
docker compose up -d
docker compose ps -a
docker compose logs minio-wait minio-init minio-private
```

预期：PostgreSQL 健康；`minio-wait` 就绪探测、`minio-init` 建桶、`minio-private` 私有策略设置均正常退出且退出码为 0。MinIO 服务本身不假定镜像内有探测工具，由官方 mc sidecar 检查就绪。若初始化失败，查看日志，不要把服务退出直接当成功。**MinIO 镜像尚未拉取验收，可能阻止整栈启动；可先用 `docker compose up -d postgres` 单独启动数据库。**

默认本机端口：PG `5432`、MinIO API `9000`、MinIO Console `9001`。bucket 私有，不开放匿名访问。宿主机 API 默认使用 `127.0.0.1`；未来若容器化运行，内部地址与浏览器公开地址必须分开配置。

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

Web 请求 `/api/health`，Vite 转发至 API `/health`，去掉 `/api`。`API_PROXY_TARGET` 只由开发服务器读取；默认目标为 `http://127.0.0.1:8000`。不使用通配 CORS。

`/health` 只证明进程存活，**不检查 PG 或 MinIO**。依赖未启动仍可体验空编辑器，但迁移和数据库测试需要真实 PG。

## 检查与验收

```powershell
uv run ruff check apps/api
uv run ruff format --check apps/api
uv run pytest apps/api/tests
pnpm --filter @revnote/web lint
pnpm --filter @revnote/web typecheck
pnpm --filter @revnote/web build
pnpm --filter @revnote/web check:markdown
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

### 认证与原生 API

协议和示例见 [API v1 规格](docs/phase3-sync-api-spec.md)，OpenAPI 在 `/docs` 与 `/openapi.json`。注册默认关闭；开发环境可在 .env 显式设 `AUTH_REGISTRATION_OPEN=true`，重启 API 后使用 `/api/v1/auth/register` 原子创建账号和默认 workspace（不自动登录），再 `/auth/login` 获取 token pair。

密码为 **8–256 字符、UTF-8≤1024 字节**，不 trim/截断，Argon2id（64 MiB/time3/parallelism4）。opaque access 默认600s；refresh轮换，session绝对30d、不续期。旧refresh复用会撤销整个session，已签发access也立即失效；客户端应串行刷新并安全存储token，不放URL或日志。

业务请求带 `Authorization: Bearer <access>`。写入仅走 `/api/v1/workspaces/{workspace_id}/sync/push`，每批1–50op、JSON≤2MiB、body_md≤256KiB，每op独立事务、按顺序执行。须逐项处理 applied/replayed/conflict/rejected/unknown/not_attempted；收到unknown或丢失响应时保留同一op_id与payload重试。

pull的since为排他十进制字符串（默认0），保留完整事件并独立去重objects；snapshot单RR事务、不分页，默认最多10,000对象/16MiB，超限409 snapshot_too_large（本阶段大workspace无法bootstrap的已知限制）。先snapshot取得as_of_seq，再pull since=as_of_seq。tree不含正文，note端点返回授权笔记正文。所有seq/rev/cursor使用十进制字符串，避免JavaScript精度损失。

通用错误信封含code/message/request_id/retryable/details；非成员与不存在workspace统一404。合法X-Request-ID原样回传，非法替换。CORS默认空列表、无cookie凭据；真实限流尚未实现，公网部署必须另行配置防护。

浏览器冒烟：

1. 桌面宽屏显示导航、空列表、编辑区三栏；窄屏可切换栏位且不横向溢出。
2. 空编辑器可输入、换行及基础格式；刷新后内容重置。
3. 明确显示内存演示与未实现保存/同步；冲突仅中性占位。
4. API 关闭时显示请求失败，重启后可重新检查恢复；不伪造保存状态。
5. Markdown 检查仅覆盖基础节点的语义往返，不承诺原始空白、LaTeX、复杂表格、HTML、自定义块无损。

### 当前验收与限制

- Phase 3 本地完整验收 **1157 passed、0 skipped、0 failed**；HTTP普通／并发／真实传输组分别64／6／3项，所有组no-skip及承重用例守卫通过。逐提交和命令证据见 [验收记录](docs/phase3-validation.md)；以下脚手架/Phase 2 数字为历史基线，不代替当前验收，远端CI以最终实际运行记录为准。
- 已用仓库外临时 Playwright 环境实测：真实 API→Vite 代理、桌面三栏、Markdown 输出、刷新清空、API 停止/恢复、无效 JSON 拒绝、390px 栏位切换与内存内容保留，无浏览器运行时错误。没有给仓库加入 E2E 平台依赖。
- 脚手架阶段已完成 API 56 项测试、Web lint/typecheck/build、7 项 Markdown 检查，并由 [Actions #40777468266](https://github.com/LeiSureLyYrsc/Revnote/actions/runs/40777468266) 验证 API/Web/GHCR 全绿；这是历史版本证据，不替代 Phase 2 验收。
- Phase 2 schema 提交 `fa99b83` 前，对精确暂存快照完成锁定安装、Ruff、105 项单元测试及 29 项真实 PostgreSQL 迁移测试（无 skip），`alembic check` 无差异，原基线仅版本表、新 head 恰好六张领域表。
- 事务写提交 `8948fd9` 前，Ruff、281 项单元测试及 109 项真实 PostgreSQL 非并发内核测试通过。
- 并发提交 `0473b17` 前，45 项真实 PostgreSQL 并发/原子性测试通过；完整 `pytest apps/api/tests -v` 为 **476 passed、0 skipped**（205.58s）。覆盖 8×20 连续 seq、同 op 并发重放、CAS 单赢家、事务故障回滚、末尾 ledger 真实冲突兜底、三连接提交/回滚可见性及提交确认丢失恢复。单独并发组耗时 51.56s，这些测试耗时不是生产吞吐指标。
- 最终 CI 的三组 no-skip/必需用例守卫已用该次真实 JUnit 报告验证：普通 PG 121 项、并发/原子性 45 项、迁移 29 项，均无 skip/failure/error；Actions 结果以推送后的实际运行记录为准。
- Web 构建通过，但编辑器入口约 859 kB（gzip 约 271 kB），仍有 chunk 大小警告；暂不为脚手架引入拆包优化工程。
- 本机未检测到 Docker CLI/运行时。本地 PostgreSQL 验证使用仓库外临时实例和两个独立可丢弃库，不使用 SQLite；这**不证明 Compose 或 MinIO 可运行**。本次验收的临时实例已停止。
- Compose 官方镜像在当前环境未完成拉取与运行验证；若官方 MinIO 分发源不可用，应停止并确认来源，不切换第三方镜像或其他 S3 产品。
- GitHub 仓库为 [LeiSureLyYrsc/Revnote](https://github.com/LeiSureLyYrsc/Revnote)。Actions、API 镜像构建和 GHCR 发布状态以实际运行记录为准，文件已生成不代表已验收。

## Actions 与 GHCR

工作流先检查 API、双库真实 PG/并发/迁移与 Web，再构建 API 镜像。发布目标为 `ghcr.io/<owner>/revnote-api`。PR 不推镜像；发布使用 GitHub Actions 的 `GITHUB_TOKEN`，不在仓库保存 PAT。

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
- MinIO 初始化失败：检查 `minio-wait`、`minio-init`、`minio-private` 日志、凭据、bucket 名称及固定官方镜像版本是否可获取；凭据放入 `MC_HOST_` URL 时，特殊字符需要 URL 编码。
- 生产构建后的静态文件没有 Vite 代理；生产同源反向代理另行规划。

## 内核边界

Phase 2 只做工作区锁分配 seq、对象/事件/成功账本同事务、DB 唯一 op_id 幂等、rev/CAS、软删除和树移动。Python 命令由调用方提供 entity_id/op_id；修改 base_rev 或业务内容需新 op_id。重放返回首次事件的结果，不能取当前对象冒充旧版本。

新 op 修改/移动/再删已删对象返回 `EntityDeleted`，原删除 op 重放仍返回原结果。非空文件夹拒绝删除；子树移动只更新根并发一条 move 事件，不批量修改后代。合法无值变化更新也递增 rev/seq。HTTP层校验认证和workspace_members，push每op前重查；内核跨workspace约束仍不等于身份授权。

Kernel 内 seq/rev 保持整数，对外 JSON 使用十进制字符串。`min_retained_seq` 默认1，不推进、不清理；过期条件since<min_retained_seq-1。延迟event FK和ledger限制未来事件清理，清理策略必须协调引用与幂等窗口。写入顺序及取消/超时语义见 [内核补充](docs/phase2-sync-kernel-spec.md)。不shield业务COMMIT，提交期间取消或确认丢失不能当作回滚，应同op_id+payload重试确认；可疑连接invalidate。

仍未实现：Web业务接入、客户端队列、独立服务端Outbox、三方合并、冲突副本、MinIO附件、搜索、E2EE及Joplin桥。预签名PUT不天然防覆盖，HEAD/ETag不等于SHA-256；这些后续另行设计，不扩入本阶段。
