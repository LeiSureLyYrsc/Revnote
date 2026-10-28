# Revnote

Sync Kernel 优先的自有笔记同步服务。自有 Web 客户端走原生 API；Joplin Server 协议仅是后续兼容适配器，非 Data API、非产品主路径。

**当前仅为开发脚手架，不是可用的同步产品。** 编辑器仅使用内存，刷新即丢失；没有笔记保存、同步、认证或附件业务。API 可达不代表正文已保存。

当前尚未选定项目许可证；公开源码不等于已授予开源许可。Compose/MinIO 的镜像拉取与运行也尚未完成验收，详见下文限制。

## 技术栈与目录

Python 3.12+、FastAPI、uv、SQLAlchemy 2.x async、asyncpg、Alembic；仅 PostgreSQL。附件存储方向固定为 MinIO。

Web：Vite、React 19、TypeScript、TanStack Router/Query、Tailwind CSS v4、TipTap。

```text
apps/api/                 FastAPI、异步DB骨架、空Alembic基线、测试、Dockerfile
apps/web/                 暗色三栏、空TipTap、API状态与开发代理
docs/plan-scaffold.md      已确认计划、领域表草图及P0不变量
docs/build-conventions.md Build补充约定
.github/workflows/        检查与Actions→GHCR
docker-compose.yml        官方PostgreSQL、MinIO、一次性私有建桶
pyproject.toml / uv.lock   根Python workspace与唯一锁文件
pnpm-workspace.yaml       Web workspace，根pnpm锁文件
```

完整范围见 [脚手架计划](docs/plan-scaffold.md)。领域表只在文档里：**数据库不会创建业务表**，空基线只建立 Alembic 版本记录。没有 `create_all()`。

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

### 执行空基线迁移

```powershell
uv run --package revnote-api --env-file .env alembic -c apps/api/alembic.ini upgrade head
uv run --package revnote-api --env-file .env alembic -c apps/api/alembic.ini current
```

不要重复执行 `alembic init` 或生成新的 scaffold revision。基线 `upgrade/downgrade` 无业务 DDL；后续正式建模另行生成迁移。

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
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://revnote:你的开发密码@127.0.0.1:5432/独立测试库'
uv run pytest apps/api/tests
Remove-Item Env:TEST_DATABASE_URL
```

该库须事先存在。未配置 `TEST_DATABASE_URL` 时，集成测试明确跳过；配置后连不上必须失败。**skip 不是数据库验收通过。** 不使用 SQLite 替代 PG。

浏览器冒烟：

1. 桌面宽屏显示导航、空列表、编辑区三栏；窄屏可切换栏位且不横向溢出。
2. 空编辑器可输入、换行及基础格式；刷新后内容重置。
3. 明确显示内存演示与未实现保存/同步；冲突仅中性占位。
4. API 关闭时显示请求失败，重启后可重新检查恢复；不伪造保存状态。
5. Markdown 检查仅覆盖基础节点的语义往返，不承诺原始空白、LaTeX、复杂表格、HTML、自定义块无损。

### 当前验收与限制

- 已用仓库外临时 Playwright 环境实测：真实 API→Vite 代理、桌面三栏、Markdown 输出、刷新清空、API 停止/恢复、无效 JSON 拒绝、390px 栏位切换与内存内容保留，无浏览器运行时错误。没有给仓库加入 E2E 平台依赖。
- 已完成根锁定安装、Ruff、API 全套测试（56 passed，包含临时真实 PostgreSQL 上的 4 项集成测试）、Alembic upgrade/current、Web lint/typecheck/build 和 7 项基础 Markdown 往返检查。空基线实际检查仅有 `alembic_version`，无业务表。
- Web 构建通过，但编辑器入口约 859 kB（gzip 约 271 kB），仍有 chunk 大小警告；暂不为脚手架引入拆包优化工程。
- 本机未检测到 Docker CLI/运行时。上述 PostgreSQL 来自仓库外临时实例，验收后已停止；这**不证明 Compose 或 MinIO 可运行**。
- Compose 官方镜像在当前环境未完成拉取与运行验证；若官方 MinIO 分发源不可用，应停止并确认来源，不切换第三方镜像或其他 S3 产品。
- GitHub 仓库为 [LeiSureLyYrsc/Revnote](https://github.com/LeiSureLyYrsc/Revnote)。Actions、API 镜像构建和 GHCR 发布状态以实际运行记录为准，文件已生成不代表已验收。

## Actions 与 GHCR

工作流先检查 API、真实 PG/空迁移和 Web，再构建 API 镜像。发布目标为 `ghcr.io/<owner>/revnote-api`。PR 不推镜像；发布使用 GitHub Actions 的 `GITHUB_TOKEN`，不在仓库保存 PAT。

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

镜像只在 Actions 构建，本地不做替代构建。镜像文件不等于完整生产部署：TLS、代理、认证、密钥、备份和运维仍未实现。

## 停止与常见故障

- API/Web：各终端 `Ctrl+C`。
- 基础设施：`docker compose down`，保留命名卷；不要加 `-v`，除非明确确认删除开发数据。
- `docker` 不存在/daemon 未启动：先安装并启动 Docker，再验收基础设施；不影响无 DB 的 hello/health 与编辑器演示。
- API 请求失败：检查 API 终端、8000 端口和 `API_PROXY_TARGET`；修改 Vite 环境配置后重启 Web。
- DB 连接失败：检查 PG 健康、端口、URL 与卷中原有凭据；修改 `.env` 不会自动重置已有 PG 数据卷的密码。
- MinIO 初始化失败：检查 `minio-wait`、`minio-init`、`minio-private` 日志、凭据、bucket 名称及固定官方镜像版本是否可获取；凭据放入 `MC_HOST_` URL 时，特殊字符需要 URL 编码。
- 生产构建后的静态文件没有 Vite 代理；生产同源反向代理另行规划。

## 后续内核边界

工作区锁分配 seq；对象/事件同事务；DB 唯一 op_id 幂等；客户端队列、服务端 Outbox 与幂等账本分开；rev/CAS、tombstone、三方合并与冲突副本；MinIO 事务外不可变上传与宽限 GC。

这些是设计约束，**本轮未实现**。预签名 PUT 不天然禁止覆盖，HEAD/ETag 不能普遍替代 SHA-256；正式附件业务前必须验证。v1 面向明文可搜，E2EE 后置且与服务端明文能力互斥。Web Cookie/OIDC/Passkey、Joplin PAT/app password 后续单独设计。
