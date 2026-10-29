# Revnote API 与 Sync Kernel

Python3.12+、FastAPI、uv、SQLAlchemy2.x async、asyncpg、Alembic；只支持 PostgreSQL。

## 范围

- HTTP 仍只有 hello `/` 与进程存活 `/health`；后者不访问 PG/MinIO。
- Phase2 的 Python service 提供单工作区、单对象写路径，不挂载业务 HTTP。
- 六表：users、workspaces、folders、notes、change_events、op_ledger。
- 保留空迁移 `0001_baseline`，新增 `0002_sync_kernel_core`；所有 schema 来自 Alembic，不调用 `create_all()`。
- 没有认证、附件、Joplin、E2EE、CRDT、合并、清理或前端接入。

完整已确认契约见 [Phase2计划](../../docs/plan-phase2.md)，运行及实测结果见 [根README](../../README.md)。

## 根目录命令

```powershell
uv sync --locked
uv run --env-file .env uvicorn revnote_api.main:create_app --factory --reload --host 127.0.0.1 --port 8000
uv run --env-file .env alembic -c apps/api/alembic.ini upgrade head
uv run ruff check apps/api
uv run ruff format --check apps/api
uv run pytest apps/api/tests/unit
```

根 `.env` 需显式 `--env-file`。`apps/api/.env` 是可选隐式配置，生产镜像不包含 dotenv。`APP_ENV` 默认local，兼容ENVIRONMENT；DATABASE_URL必须postgresql+asyncpg。DB pool和SERVER_*等可选配置仍由Settings管理。

## Python 写入口

```python
from revnote_api.db.session import get_sessionmaker
from revnote_api.kernel.commands import CreateNote
from revnote_api.kernel.ids import new_entity_id, new_op_id
from revnote_api.services.sync_write import SyncWriteService

service = SyncWriteService(get_sessionmaker())
command = CreateNote(
    workspace_id=existing_workspace_id,
    entity_id=new_entity_id(),
    op_id=new_op_id(),
    title="标题",
    body_md="正文",
)
# 异步调用方内：result = await service.execute(command)
# 重试必须原样保留command及其op_id/entity_id。
```

users/workspaces 需由受信任的上层预先准备；本阶段不提供注册/工作区管理API。service自己拥有新session与事务，勿包在调用方 `session_scope()` 中。UUIDv7只作标识，seq按工作区行锁分配；rev/CAS、对象/event/成功ledger在同一事务。返回不可变首次操作结果，不包含旧正文历史。

## 真实 PG 验收

在运行前建立两个不同、独立、可丢弃库：

```powershell
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://revnote:开发密码@127.0.0.1:5432/revnote_test'
$env:TEST_MIGRATION_DATABASE_URL = 'postgresql+asyncpg://revnote:开发密码@127.0.0.1:5432/revnote_migrations_test'
$env:REQUIRE_POSTGRES_TESTS = '1'
uv run pytest apps/api/tests
```

迁移往返只允许专库，普通测试库只升不降。CI/强制模式缺任一URL失败，连接失败不得skip。不要对非测试数据执行降级；schema可逆不等于数据可恢复。每个并发任务独立session/真实连接；无SQLite替代。

本地禁止docker build/compose build，镜像由Actions→GHCR。工作区隔离不是身份授权；软删与事件/账本均不清理，min_retained_seq不推进。其余边界和已实测事实以根README为准。
