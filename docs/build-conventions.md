# Revnote 补充约定（Build 必须遵守；与 Plan 冲突时以此为准）

## Docker / GHCR
- **本地禁止**对本仓做 `docker build` / `docker compose build`。
- 镜像只经 **GitHub Actions** 构建并推 **GHCR**（`ghcr.io/<owner>/revnote-api` 等）。
- 本地：`docker compose up` **仅拉官方** postgres/minio；API/Web 用 uv/pnpm 在宿主机跑。
- push 后用 **`gh run watch`**（或 list→watch id）盯到成功；失败 `gh run view <id> --log-failed`，修到绿。

## 相对 Plan 的调整
- Plan「不做 API/Web 容器化」改为：**要写 Dockerfile + Actions→GHCR**，但本地不构建；验收以 Actions 绿为准。
- 其余仍按 Plan：空 Alembic **不建业务表**；无 CRUD/同步/Joplin/E2EE/CRDT/登录。

## 前端 / 后端锁定
Vite+React19+TS+TanStack Router/Query+Tailwind v4+TipTap；Python3.12+FastAPI+uv+SQLAlchemy async+PG only+MinIO。
