# Revnote API 脚手架

FastAPI + uv + SQLAlchemy 2.x（异步）+ **仅 PostgreSQL**（`asyncpg`）。

本服务当前只是可运行骨架：没有业务接口、没有 ORM 业务模型、没有业务表迁移，
也没有认证与同步逻辑。完整范围、启动方式与验收说明见[仓库根 README](../../README.md)。

## 目录

```
apps/api/
├── alembic.ini               # 在仓库根用 -c apps/api/alembic.ini 调用
├── migrations/
│   ├── env.py                # 异步 Alembic 环境，DSN 来自 Settings
│   ├── script.py.mako
│   └── versions/
│       └── 0001_baseline.py  # 空基线：不建任何表
├── src/revnote_api/
│   ├── _version.py           # 版本号单一来源
│   ├── config.py             # pydantic-settings（忽略无关 env、仅 PG）
│   ├── main.py               # create_app() 与 lifespan（退出 dispose 引擎）
│   ├── app.py                # 模块级 app，供 uvicorn 直接引用
│   ├── __main__.py           # uv run revnote-api
│   ├── schemas.py
│   ├── api/routes.py         # 仅 GET / 与 GET /health
│   └── db/
│       ├── base.py           # DeclarativeBase + 命名约定（无表）
│       ├── engine.py         # 惰性异步引擎，退出时统一 dispose
│       └── session.py        # sessionmaker、session_scope、get_session
└── tests/
    ├── conftest.py
    ├── unit/                 # 无需任何外部服务
    └── integration/          # 需要 TEST_DATABASE_URL（否则跳过）
```

Python 命令统一在**仓库根**执行（单一 `.venv/`、单一 `uv.lock`）：

```powershell
uv sync
uv run uvicorn revnote_api.app:app --reload
uv run revnote-api                    # 同上，读取 .env 中的 SERVER_* 配置
uv run pytest                         # 单元测试，无需数据库
uv run ruff check .
uv run alembic -c apps/api/alembic.ini current
```

## 接口

| 路由 | 行为 |
| --- | --- |
| `GET /` | hello 占位，返回 JSON |
| `GET /health` | **仅进程存活**，返回 JSON，不建立任何数据库连接 |

`/health` 不做任何 I/O，因此 PostgreSQL 与 MinIO 未启动时它仍返回 `200`。
将来如需依赖检查，请新增 `/health/ready`，不要把它混入 `/health`。

## 配置

`revnote_api.config.Settings` 读取环境变量与 `apps/api/.env`（`extra="ignore"`），
因此 compose 根 `.env` 里的 `POSTGRES_*`、`MINIO_*` 等无关键不会导致启动失败。
所有字段都有本机开发可用的默认值：**没有 `.env`、没有数据库也能启动**。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_ENV` | `local` | `local` \| `test` \| `production`；旧名 `ENVIRONMENT` 仍兼容，两者同设时 `APP_ENV` 优先 |
| `DATABASE_URL` | `postgresql+asyncpg://revnote:revnote@127.0.0.1:5432/revnote` | 必须是 `postgresql+asyncpg://`，否则启动即失败 |
| `DB_ECHO` | `false` | 输出 SQL |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `5` / `10` | 连接池容量 |
| `DB_POOL_TIMEOUT_SECONDS` / `DB_POOL_RECYCLE_SECONDS` | `30` / `1800` | 连接池超时与回收 |
| `SERVER_HOST` / `SERVER_PORT` / `SERVER_RELOAD` | `127.0.0.1` / `8000` / `false` | `uv run revnote-api` 使用 |

其他驱动（`sqlite`、`mysql`、`postgresql+psycopg`、裸 `postgresql://`）在参数校验阶段
即被拒绝：v1 只用 PostgreSQL。

仓库根 `.env` 由 compose 与根 README 管理，本服务**不会隐式读取**它，避免出现第二份
会漂移的配置来源；需要时用 `uv run --env-file .env ...` 显式传入。

## 迁移

唯一版本是空基线 `0001_baseline`，**不创建任何表**。业务表会作为后续独立迁移引入，
schema 不会由 ORM 元数据批量生成。

```powershell
uv run alembic -c apps/api/alembic.ini upgrade head
uv run alembic -c apps/api/alembic.ini downgrade base
```

`alembic.ini` 使用 `%(here)s` 相对路径，因此在仓库根以 `-c apps/api/alembic.ini` 调用可
正确解析；DSN 由 `env.py` 从 Settings 注入，不写在 ini 里。

## 测试

```powershell
uv run pytest                                   # 单元测试；集成测试自动跳过
uv run pytest -m integration                     # 只跑集成测试
$env:TEST_DATABASE_URL = "postgresql+asyncpg://revnote:revnote@127.0.0.1:5432/revnote_test"
uv run pytest -m integration                      # 此时必须连通
```

`TEST_DATABASE_URL` 未设置 → 集成测试跳过；一旦设置 → 连接失败即**测试失败**，
不会被转成跳过。集成测试会执行 `SELECT 1`，并对空基线跑真实的
`upgrade`/`downgrade`，随后断言除 `alembic_version` 外没有其他表。

## 本脚手架明确不含

CRUD 接口、同步内核（`seq`/rev/CAS/outbox/tombstone）、Joplin 适配、E2EE、CRDT、
认证登录、附件业务、ORM 业务模型与业务表迁移。部署与镜像相关配置见根 README。
