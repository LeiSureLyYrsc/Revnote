# Revnote 补充约定（Build 必须遵守；与 Plan 冲突时以此为准）

> **历史记录 / historical record.** 本文撰写时的 S3 后端为 MinIO。Phase 5 已迁移到
> **RustFS 1.0.4**（官方 GitHub release，固定版本与 SHA-256），原因是 MinIO 官方镜像已下架
> （registry 401 / Hub 404 / 下载站 410，实测见 `docs/phase5-validation.md`）。
> 本文正文保留当时原样，未做改写。


## Docker / GHCR
- **本地禁止**对本仓做 `docker build` / `docker compose build`。
- 镜像只经 **GitHub Actions** 构建并推 **GHCR**（`ghcr.io/<owner>/revnote-api` 等）。
- 本地：`docker compose up` **仅拉官方** postgres/minio；API/Web 用 uv/pnpm 在宿主机跑。
- push 后用 **`gh run watch`**（或 list→watch id）盯到成功；失败 `gh run view <id> --log-failed`，修到绿。

## 相对 Plan 的调整
- Plan「不做 API/Web 容器化」改为：**要写 Dockerfile + Actions→GHCR**，但本地不构建；验收以 Actions 绿为准。
- 上述空 Alembic、无领域写逻辑限制仅适用于脚手架阶段。
- Phase 2 以 `plan-phase2.md` 的已确认范围为准：六张领域表、独立迁移、内核单对象事务写与真实 PostgreSQL 测试。保留原空基线，不新增 HTTP 路由/认证/附件/Joplin/E2EE/CRDT/前端改动。
- 本地验收使用两个独立可丢弃 PostgreSQL 测试库；迁移降级只允许在专用迁移测试库执行。无 Docker 时可使用仓库外临时 PostgreSQL 实例，用完停止；不能用 SQLite 或 skip 代替验收。
- 未完成四个本地阶段验收前不推送；推送后监听 Actions，不强推，不改写已发布历史，不改 Clash 配置。

## 前端 / 后端锁定
Vite+React19+TS+TanStack Router/Query+Tailwind v4+TipTap；Python3.12+FastAPI+uv+SQLAlchemy async+PG only+MinIO。
