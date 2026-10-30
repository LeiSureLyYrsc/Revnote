# Revnote 脚手架实施计划

> **历史记录 / historical record.** 本文撰写时的 S3 后端为 MinIO。Phase 5 已迁移到
> **RustFS 1.0.4**（官方 GitHub release，固定版本与 SHA-256），原因是 MinIO 官方镜像已下架
> （registry 401 / Hub 404 / 下载站 410，实测见 `docs/phase5-validation.md`）。
> 本文正文保留当时原样，未做改写。


> 本计划经会话确认后进入 Build；文中的任务清单是实施范围，不代表验收已通过。实际启动方法和验证限制见根 README。
>
> 依据：已确认的会话需求、`docs/consensus-brief.md`、`docs/stack-blueprint.md`。本地任务提示文件不随仓库发布。
> Build 补充约定：`docs/build-conventions.md` 优先；需添加 API Dockerfile 与 GitHub Actions→GHCR，但**禁止本地构建本仓镜像**。本地 Compose 仅运行官方 PostgreSQL、MinIO 及其初始化工具，API/Web 在宿主机运行。

## 1. 目标与非目标

### 1.1 产品与架构定位

Revnote 是自有笔记同步服务。Sync Kernel 负责正确性，自有 Web 客户端是主路径。

```text
自有 Web 客户端 → 原生 API → services / Sync Kernel → PostgreSQL
                                      └──────────→ MinIO
Joplin Server 协议适配器 → services / Sync Kernel
```

- Joplin 仅是后置 Server 协议兼容桥，不采用 Data API 作为产品基础。
- Kernel 不依赖 HTTP 细节或 Joplin 协议。
- PostgreSQL 是业务状态与变更记录的权威存储；MinIO 保存附件字节。
- 本轮仅建立边界，不实现同步算法或业务写路径。
- 蓝图中的单包布局迁至 monorepo；旧多库预留和 Basic Auth 示例不进入本轮。

### 1.2 本轮交付目标

1. 启动 PostgreSQL、MinIO，并幂等初始化私有开发 bucket。
2. 用 uv 运行 FastAPI，访问 hello 和 `/health`。
3. 通过 SQLAlchemy 异步引擎连接 PostgreSQL。
4. 执行 Alembic 真正的空基线迁移。
5. 启动暗色三栏 Web 与空 TipTap 编辑器。
6. 通过 Vite 代理请求 API，呈现真实连通状态。
7. 记录领域模型草图、P0 不变量及后续验证要求。
8. 按 Build 补充约定提供 API Dockerfile、Actions 检查与 GHCR 发布工作流；镜像只在 Actions 构建。

完成标准是“可安装、可启动、可联通、可验证”，不是“可以可靠保存和同步笔记”。

### 1.3 锁定技术栈

| 层级 | 决策 |
| --- | --- |
| Python | 声明 `>=3.12`，首轮验证使用 Python 3.12 |
| API | FastAPI，uv 管理依赖 |
| ORM | SQLAlchemy 2.x async、AsyncSession、asyncpg |
| 数据库 | 仅 PostgreSQL，不安装 SQLite/MySQL 驱动 |
| 迁移 | Alembic 异步环境，空基线 |
| 对象存储 | MinIO，本轮仅部署、配置、建桶 |
| Web | Vite、React 19、TypeScript |
| 路由/请求 | TanStack Router、TanStack Query |
| 样式/编辑器 | Tailwind CSS v4、TipTap |
| 仓库 | 根 uv workspace、根 pnpm workspace |
| 本地运行 | PG/MinIO 在 Compose，API/Web 在宿主机 |

不引入 Turborepo、Nx、Redis、消息代理或共享类型发布流程。

### 1.4 不做清单

- 真实笔记、文件夹、附件 CRUD 或持久化。
- push/pull、游标分页、重试、恢复、离线同步等真实协议。
- seq 分配、幂等摄取、CAS、Outbox worker、tombstone 清理。
- 三方合并算法、冲突副本生成及真实冲突处理 UI。
- Joplin 适配、兼容认证、E2EE、CRDT、协同编辑。
- 服务端搜索 API/索引；只保留明文可搜方向。
- 登录、OIDC、Passkey、PAT/app password 和用户管理。
- 附件预签名上传、确认、授权下载或 GC。
- 桌面端、移动端、参考 CLI。
- 本地 API/Web 镜像构建、生产反向代理和完整生产部署。

页面明确提示：**演示编辑区，仅内存内容，刷新即丢失；保存与同步未实现。**

## 2. 目录树

下列为目标职责结构；具体文件以落地结果为准。已有文档原样保留。

```text
Revnote/
├── .gitignore / .env.example / .python-version / .node-version
├── README.md
├── docker-compose.yml
├── .dockerignore
├── pyproject.toml / uv.lock
├── package.json / pnpm-workspace.yaml / pnpm-lock.yaml
├── .github/workflows/             # 检查与 Actions→GHCR
├── docs/
│   ├── consensus-brief.md
│   ├── stack-blueprint.md
│   ├── build-conventions.md
│   └── plan-scaffold.md
└── apps/
    ├── api/
    │   ├── pyproject.toml         # revnote-api
    │   ├── Dockerfile             # 根 context；只经 Actions 构建
    │   ├── alembic.ini
    │   ├── migrations/
    │   │   ├── env.py / script.py.mako
    │   │   └── versions/          # scaffold_baseline，无业务 DDL
    │   ├── src/revnote_api/
    │   │   ├── main.py / config.py
    │   │   ├── db/                # Base、engine、session
    │   │   └── api/               # router、health
    │   └── tests/
    └── web/
        ├── package.json / index.html / vite.config.ts
        ├── tsconfig*.json / eslint.config.js
        ├── scripts/              # 最小 Markdown 检查（如需要）
        └── src/
            ├── main.tsx / styles.css
            ├── app/              # providers、代码式 Router
            ├── lib/              # API fetch 边界
            └── features/
                ├── health/       # API 状态
                └── workspace/    # 三栏、空态、冲突占位、TipTap
```

蓝图中 `notesync` 的职责迁至 `revnote_api`。`models/`、`schemas/`、`kernel/`、`services/`、`storage/`、`api/v1/`、`adapters/joplin/` 只约定后续归属，不批量创建空业务文件。

本轮不创建多库 `dialects.py` 或 `packages/`。将来优先评估 OpenAPI 生成 TS 类型，避免两套手写 DTO。

## 3. 依赖与命令

### 3.1 工具与锁定策略

- Python `>=3.12`，开发版本记录于 `.python-version`。
- Node 24 LTS；核对实际 Vite 的 engines，记录于 `.node-version`。
- pnpm 版本写入根 `package.json` 的 `packageManager`。
- React 19、Tailwind 4、SQLAlchemy 2.x；TipTap 包保持兼容版本组合。
- PostgreSQL、MinIO、mc 采用明确可获取版本，不用浮动 latest。
- 只保留根 `.venv`、`uv.lock`、`pnpm-lock.yaml`；锁文件需提交。
- Windows 若 `pnpm.ps1` 被执行策略拦截，使用 `pnpm.cmd`，不修改全局策略。

### 3.2 首次初始化（只执行一次）

```powershell
uv init --bare --name revnote-workspace --python 3.12 --vcs none
uv init --package --name revnote-api --python 3.12 --vcs none apps/api
uv add --package revnote-api fastapi "uvicorn[standard]" "sqlalchemy[asyncio]>=2,<3" asyncpg alembic pydantic-settings
uv add --package revnote-api --dev pytest pytest-asyncio httpx ruff

pnpm create vite apps/web --template react-ts
```

根项目非发布、不承载业务；workspace 只包含当前实际成员。API 是可安装 src 包，无临时 PYTHONPATH。

Web 包名 `@revnote/web`，配置 pnpm workspace 后：

```powershell
pnpm --filter @revnote/web add react@19 react-dom@19
pnpm --filter @revnote/web add @tanstack/react-router @tanstack/react-query
pnpm --filter @revnote/web add @tiptap/react @tiptap/pm @tiptap/starter-kit @tiptap/markdown
pnpm --filter @revnote/web add -D @types/react@19 @types/react-dom@19
pnpm --filter @revnote/web add -D tailwindcss@4 @tailwindcss/vite@4
```

- Tailwind 用官方 Vite 插件与 CSS `@import "tailwindcss"`，不照搬 v3。
- Router 采用代码式路由，不引入 route 生成插件。
- Query 只负责 health 请求及其加载、成功、错误状态。
- TipTap StarterKit + Markdown 仅覆盖基础编辑与转换，不引入协作。
- 不安装暂时没有调用需求的对象存储 SDK、认证 SDK、合并库或多库驱动。

### 3.3 环境契约

根 `.env.example` 包含：

| 变量 | 语义 |
| --- | --- |
| APP_ENV | 开发环境标识 |
| DATABASE_URL | postgresql+asyncpg 连接 |
| POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB | 本地 PG 配置 |
| MINIO_ROOT_USER / MINIO_ROOT_PASSWORD | 仅开发用管理凭据 |
| MINIO_ENDPOINT | 服务端访问地址 |
| MINIO_PUBLIC_ENDPOINT | 浏览器可达地址，未来预签名使用 |
| MINIO_BUCKET | 私有开发 bucket |
| API_PROXY_TARGET | Vite 服务端转发目标 |
| VITE_API_BASE_PATH | 公开路径 /api |
| TEST_DATABASE_URL | 可选的独立 PostgreSQL 集成测试连接 |

规则：

- 忽略 `.env`；不得将数据库、MinIO 秘密放入 VITE_ 变量。
- API、迁移、数据库测试显式加载根环境文件。
- Vite 读取根环境配置，但只有明确公开值进入浏览器。
- 默认本地端口 PG 5432、API 8000、MinIO 9000、Console 9001、Web 5173，限制本机访问。
- Compose 包含 PG、MinIO、一次性幂等建桶 mc；bucket 私有，命名卷默认保留。
- 健康检查需核对镜像实际提供的命令，不能假定有 curl。

### 3.4 API 契约

- `GET /`：服务标识与 hello。
- `GET /health`：仅进程存活，不访问数据库或对象存储。
- SQLAlchemy：Base、async engine/session factory、请求级 session、异常回滚、退出 dispose。
- session 依赖不隐藏 commit；后续业务事务显式控制。
- 不调用 `metadata.create_all()`。
- Web `/api/health` 经 Vite 去掉 `/api`，到 FastAPI `/health`。
- 未来 `/api/v1/*` 同理到 `/v1/*`。
- 不启用通配 CORS；Vite 代理不是生产部署方案。

### 3.5 日常启动与检查

实际可复制命令以 README 为准。以下在根运行：

```powershell
Copy-Item .env.example .env
uv sync --locked --package revnote-api
pnpm install --frozen-lockfile
docker compose up -d
docker compose ps -a
uv run --package revnote-api --env-file .env alembic -c apps/api/alembic.ini upgrade head
```

两终端分别启动：

```powershell
uv run --package revnote-api --env-file .env uvicorn revnote_api.main:create_app --factory --reload --host 127.0.0.1 --port 8000
pnpm --filter @revnote/web dev --host 127.0.0.1
```

首次创建 Alembic 环境采用 async 模板；空 revision 不带 autogenerate；路径按 ini 目录解析。已提交基线后日常只运行 upgrade，不重复 init/revision。

检查命令：

```powershell
uv run --package revnote-api ruff check apps/api
uv run --package revnote-api ruff format --check apps/api
uv run --package revnote-api --env-file .env pytest apps/api/tests
pnpm --filter @revnote/web lint
pnpm --filter @revnote/web typecheck
pnpm --filter @revnote/web build
pnpm --filter @revnote/web check:markdown
```

无 TEST_DATABASE_URL 时数据库集成测试明确跳过；设置后连接失败必须失败，不能伪报通过。

## 4. 分阶段任务清单

### 阶段 0：固定边界与领域草图

- [ ] 保留现有提示文件、共识、蓝图及补充约定。
- [ ] 落盘确认后的计划，区分目标与已验收事实。
- [ ] 统一包名、workspace、配置、端口及代理契约。
- [ ] 模型只写以下草图，不创建业务 ORM 类或 DDL。

#### 最小模型草图

统一 UUID 实体 ID、UTC TIMESTAMPTZ，rev/seq 用 BIGINT 且语义独立。关联必须校验工作区一致性。

| 领域/表 | 字段草图 | 关键约束 |
| --- | --- | --- |
| workspace / workspaces | id, owner_user_id, name, change_seq, created_at, updated_at | change_seq 初值0，工作区行锁事务内递增；用户关联随认证落地 |
| note / notes | id, workspace_id, parent_id, title, body_md, rev, deleted_at, created_at, updated_at, user_updated_at | rev初值1、更新CAS；parent文件夹模型待确认 |
| revision / note_revisions | id, workspace_id, note_id, rev, base_rev, title, body_md, parent_id, created_at | 唯一(workspace_id,note_id,rev)，追加式快照，支持合并基线 |
| change_event / change_events | id, workspace_id, seq, entity_type, entity_id, op, rev_after, payload_meta, actor_user_id, created_at | 唯一(workspace_id,seq)，seq无自增默认，工作区游标索引 |
| attachment / attachments | id, workspace_id, note_id, content_hash, byte_size, mime, state, storage_key, rev, created_at, ready_at, deleted_at | pending/ready/orphaned/deleted；活动内容去重参考蓝图；对象不可变 |
| outbox / outbox | id, workspace_id, change_seq, topic, payload_meta, status, attempts, available_at, processed_at, created_at | 服务端事务副作用队列，有投递需求后建表；外部投递不阻塞业务提交 |
| processed_operations | id, workspace_id, op_id, request_hash, result_entity_id, result_rev, status, response_status, response_snapshot, created_at | DB唯一(workspace_id,op_id)，历史结果重放，payload改变不得静默复用key |
| tombstones | id, workspace_id, entity_type, entity_id, deleted_at, change_seq | 删除事件、软删除同事务；保留期与游标过期共同设计 |

Outbox 消歧：

1. 客户端待发队列：未来持久化未确认操作，本轮不接 IndexedDB。
2. 服务端事务 Outbox：有后台副作用投递需求时才建，不引入消息系统。
3. processed_operations：幂等账本，不是待发队列。
4. change_events：权威变更流，游标不依赖 worker 进度。
5. 蓝图 kernel/outbox.py 是摄取/幂等职责，不要求立即实现后台队列。

#### Sync Kernel P0 不变量

以下仅约束后续实现，**不声称本轮已实现或测试通过**。

| P0 | 设计约束 | 后续验证 |
| --- | --- | --- |
| 工作区seq | 同事务锁工作区、递增change_seq、写事件，持锁至提交；禁止SERIAL/IDENTITY/nextval作游标 | 双连接交错提交和回滚不永久漏较小seq |
| 原子写/幂等 | 实体、revision、event、成功操作账本及必要outbox同PG事务；DB唯一op_id | 并发同op_id只应用一次；失败无部分状态；唯一冲突不覆盖旧结果 |
| Outbox/删除 | 未确认可重试；服务端消费者幂等；删除有tombstone | 响应丢失、重启、删除重放仍收敛 |
| rev/CAS | base_rev条件更新，竞争失败冲突，不做静默LWW | 同base并发仅一方成功 |
| 合并 | 不CRDT；整篇/块级三方合并，失败保留冲突副本 | base/local/remote样例不丢文 |
| 明文/E2EE | v1明文Markdown面向后续搜索，不宣称E2EE | 能力说明与存储一致，启用E2EE前重新设计边界 |
| MinIO | 事务外上传、不可变key、确认后ready、宽限期GC | 中断、重复、确认失败及GC竞争不损坏引用 |
| 认证 | Web Cookie/OIDC/Passkey；后置Joplin PAT/app password | CSRF、工作区授权、会话隔离和桥认证分别验证 |

统一工作区锁/实体锁顺序；客户端游标取返回事件最后seq，不取预分配号或后台水位。

### 阶段 1：仓库基础与 Compose

依赖阶段0。

- [ ] 安装前先忽略.env、.venv、node_modules、构建/测试缓存。
- [ ] 根workspace单锁，配置开发环境契约。
- [ ] 官方PG/MinIO/mc，固定版本、私有建桶、健康检查、本机绑定、命名卷。
- [ ] README写前置工具、首次安装、启停及常见故障。
- [ ] 补充Dockerfile/Actions，禁止本地build。

验收：基础设施健康、建桶成功且可重复、无匿名权限、重启保留卷、无秘密待提交。

### 阶段 2：API 与空迁移

依赖阶段1。

- [ ] revnote-api 可安装包、app factory、hello/health。
- [ ] PG-only配置、Base/engine/session及生命周期。
- [ ] 独立真实PG SELECT1测试。
- [ ] async Alembic共用配置；空基线upgrade/downgrade均无业务DDL。
- [ ] hello/health自动测试、Ruff与pytest。

边界：Base.metadata无业务表是有意设计；upgrade只可创建alembic_version。不使用create_all。正式建模另交模型与DDL。

验收：API正常，DB不可用时health仍存活；真实PG连通；upgrade/current/重复upgrade成功且无业务表。

### 阶段 3：Web 三栏与编辑器

依赖阶段1，联调依赖阶段2。

- [ ] Vite React TS、React19类型、Tailwindv4。
- [ ] 代码式Router、Query Provider、真实health状态。
- [ ] /api代理、失败状态和手动重试。
- [ ] 暗色三栏、空列表、空TipTap、内存提示、中性冲突占位。
- [ ] 基础Markdown转换检查，不接保存/同步。
- [ ] lint/typecheck/build。

UX草图：

```text
Revnote · Scaffold                       API: 检测中/可达/失败
┌────────────────┬────────────────────┬─────────────────────────┐
│ 笔记本/文件夹树 │ 笔记列表           │ TipTap空编辑区          │
│ 示例导航占位   │ 尚未加载笔记       │ 可输入，仅内存          │
│ 新建未实现     │ 无虚构数量         │ 冲突处理未接入（占位）  │
└────────────────┴────────────────────┴─────────────────────────┘
仅内存演示，刷新即丢失；API可达不代表内容已保存或同步。
```

- 宽屏约240px/300px/minmax(0,1fr)，独立滚动。
- 暗色中性背景、表面层次、单一强调色，正文约16px/1.7行高。
- 窄屏简单栏位切换，无复杂抽屉/拖拽/调宽。
- 无假同步成功、在线用户、更新时间、远端冲突警报。
- 未实现操作隐藏或禁用。
- 地标、标签、焦点可见、足够对比度；普通嵌套导航列表，不虚挂ARIA tree。
- body_md是未来正文源，TipTap JSON只是编辑态。

Markdown只检查标题、段落、粗体、列表、引用、代码块的语义往返；不保证原始空白、LaTeX、表格、HTML或自定义块无损。

验收：宽窄屏可用、可输入/换行/基础格式、刷新重置、API失败/恢复正确、无运行时错误、构建成功。

### 阶段 4：集成验收与文档收口

依赖阶段2和3。

- [ ] 依据README复现安装启动，核对根锁唯一性。
- [ ] 检查数据库真实状态，不只阅读空迁移文件。
- [ ] 浏览器代理与编辑器冒烟、秘密泄漏检查。
- [ ] Actions跑API/PG迁移/Web检查，成功后构建推GHCR。
- [ ] 获授权且有远端后push，用gh run watch等待；失败看log-failed再修。
- [ ] README区分已实现、未实现、已验证及未验证。

主代理负责最终验证；实现者的总结需以文件和输出复核。

| 主张 | 最小证据 | 不覆盖 |
| --- | --- | --- |
| 安装可复现 | uv locked、pnpm frozen | 所有未来平台 |
| 基础设施可用 | Compose健康、PG连接、私有bucket | 附件业务安全 |
| API可用 | hello/health测试、真实PG测试 | CRUD/鉴权/同步 |
| 迁移可用 | upgrade/current、实际仅版本表 | 正式领域约束 |
| Web可用 | lint/typecheck/build、浏览器冒烟 | 完整业务E2E |
| 联通 | 浏览器/api/health成功和失败 | 已保存/同步 |
| 编辑器可用 | 输入与基本Markdown往返 | 复杂格式保真 |
| 发布可用 | Actions全绿、GHCR发布成功 | 本地构建（禁止） |

不为本轮安装整套项目E2E平台。降级只在可丢弃测试库验证。不清空已有数据。缺少Docker/PG/远端时明确标记阻塞，不把skip算通过。

## 5. 风险与开放问题

### 5.1 风险

| 风险 | 处理 |
| --- | --- |
| 脚手架范围膨胀 | 模型只草图，迁移无业务DDL，不创建CRUD/worker |
| Markdown与TipTap不等价 | 基础节点语义往返；正式保存前决定不支持语法的保护策略 |
| 官方Markdown扩展边界 | 锁定版本，不承诺完整Markdown/LaTeX保真 |
| health被当readiness | 只测存活，依赖另测 |
| 预签名PUT可覆盖 | hash key不天然不可变；验证条件写/staging校验发布方案 |
| HEAD/ETag冒充hash | 真实校验checksum，不假定ETag等于SHA256 |
| 存储网络拖住写锁 | 上传、校验、对象删除不占工作区锁 |
| 镜像获取/检查差异 | 核实具体官方版本与命令，不静默替换MinIO |
| 内外地址混淆 | 分开内部endpoint与浏览器公开地址 |
| 无认证公网暴露 | 本机绑定，正式业务先做授权 |
| 多锁/多环境 | 根单锁、显式加载.env、ini相对路径 |
| BIGINT/JS安全整数 | 原生协议前决定seq/rev无损JSON表示 |

### 5.2 不阻塞骨架的后续决策

1. CommonMark/GFM、LaTeX、自定义块与不支持内容的保护。
2. 笔记本/文件夹模型、层级循环与跨工作区约束。
3. 整篇或块级合并、revision保留与基线过期后的冲突副本。
4. 幂等失败重放、key保留期、payload变化语义。
5. tombstone/event/revision保留与全量重同步条件。
6. 附件工作区前缀、不可变发布、预签名有效期、GC宽限和引用竞争。
7. Cookie生命周期、CSRF、OIDC、Passkey、成员授权、桥凭据映射。
8. Sync Doctor/时间机器、块级合并等差异化候选仍待选择。

前端栈、PG-only、MinIO、自有客户端定位不重新选型。

### 5.3 参考

- https://docs.astral.sh/uv/concepts/projects/workspaces/
- https://vite.dev/config/server-options.html#server-proxy
- https://tailwindcss.com/docs/installation/using-vite
- https://tanstack.com/router/latest/docs/framework/react/quick-start
- https://tanstack.com/query/latest/docs/framework/react/installation
- https://tiptap.dev/docs/editor/markdown
- https://alembic.sqlalchemy.org/en/latest/cookbook.html#using-asyncio-with-alembic

## 6. 建议提交粒度

建议三个可审查提交，**没有授权不自动commit或push**：

1. `chore: establish scaffold docs and local infrastructure`
   - gitignore、环境示例、Compose、README基础部分、确认计划。
   - 验收：官方依赖可启动、私有建桶幂等、无秘密。
2. `feat(api): add FastAPI async database scaffold`
   - Python workspace/lock、API/DB骨架、空迁移、测试；API Dockerfile及Actions可并入。
   - 验收：API/PG/空迁移检查；镜像仅Actions构建。
3. `feat(web): add dark three-pane Tiptap scaffold`
   - pnpm workspace/lock、锁定Web栈、暗色三栏、空编辑器、代理、最终文档和CI联调。
   - 验收：lint/typecheck/build/Markdown/浏览器冒烟，不误导已保存同步。

最终里程碑：可复现运行的API/Web骨架，内核约束明确，可进入正式领域模型与事务写路径阶段；**尚不具备保存和同步能力**。
