# Phase 7 实际验收记录（snapshot 分页 bootstrap）

> 只记录**实际运行**的结果。未运行的小节明确写「未运行」；不把计划、推断或历史绿色当作本阶段证据。
>
> 基线：`25a6d5f`（Phase 6 DONE）。对象存储保持 RustFS 1.0.4；本阶段新承重以真 PostgreSQL 为主，附件/Joplin 回归另跑真 RustFS。

## A. 本地环境

- Windows / Murasame-PC，无 Docker；未执行任何本机 `docker build` / `compose build`。
- 真 PostgreSQL：仓库外临时实例（`pgserver` 控制器，`C:\Users\Murasame\AppData\Local\Temp\opencode\revnote_phase2_pg.py`），业务库 `revnote_phase2_test`、迁移库 `revnote_phase2_migrations`、E2E 库 `revnote_web_e2e` 彼此独立；实例由本轮拥有并在门禁结束后 `finally` 停止。
- `REQUIRE_POSTGRES_TESTS=1`；附件/Joplin/API 全量组另设 `REQUIRE_OBJECT_STORAGE_TESTS=1`，连接真实 RustFS 1.0.4（`D:\tools\rustfs\rustfs.exe`，SHA-256 `3396617e…d899`，启动前核对）。
- `TEMP`/`TMP` = `D:\tmp\opencode-temp`；未提交 `.env`、凭据、trace/HAR/storageState。
- 模型 `deepseek-official/deepseek-flash#max`；未派 SubAgent；未使用 `gpt-6.1-sol`。

## B. 静态检查与 API 单元

| 命令 | 结果（2026-10-30） |
|---|---|
| `uv run ruff check apps/api` | **All checks passed** |
| `uv run ruff format --check apps/api` | **154 files already formatted** |
| `uv run pytest apps/api/tests/unit -q` | **1250 passed** |

单元覆盖：游标编解码/签名/workspace 绑定、首页与续页参数、SQL 形状、页顶/总顶/空页禁令、路由分页响应与预算裁剪、错误码表、配置与新 capabilities 字段。

## C. API 真实 PostgreSQL + 真实 RustFS

| 命令 | 结果（2026-10-30） |
|---|---|
| `uv run pytest apps/api/tests/api -q` | **118 passed** |

Phase 7 新用例（同一真 PG 库；分页用例用小页上限夹具，不灌十万行）：

| 用例 | 结果 | 承重主张 |
|---|---|---|
| `test_snapshot_pagination_bootstraps_a_workspace_larger_than_one_page` | PASS | 8 对象 / 每页 3，页数 ≥3；拼页集合 = 水位活跃集；无重复；终止页 `next_cursor=null`；`pull since=as_of` 为空 |
| `test_snapshot_pages_exclude_writes_after_the_first_page` | PASS | 首页后新建对象不出现在任何 snapshot 页；`pull since=as_of` 交付它 |
| `test_snapshot_pagination_cursor_verdicts_and_page_limits` | PASS | 超页上限 400；空/坏/篡改/超长/跨 workspace 游标 400 `invalid_cursor`；floor 前进后 410 `cursor_expired` |
| `test_snapshot_pagination_refuses_the_total_cap_and_a_single_oversize_page` | PASS | 跨页硬顶 409 `snapshot_total_too_large`；单对象超页预算 413 `snapshot_page_too_large`；均无 `objects` 数组 |
| `test_snapshot_bootstraps_from_one_watermark`（更新） | PASS | 无参遗留响应精确为 `{as_of_seq,min_retained_seq,objects}`；`offset`/`page`/`since` 仍 400 |

Joplin 报告守卫（与 CI 相同入口）：

```text
uv run pytest apps/api/tests/api -m joplin -q --junitxml=…\api-joplin.xml
17 passed, 101 deselected
uv run python apps/api/scripts/check_test_reports.py --report-dir … --group joplin
api/joplin: passed=17 required=17; zero skips/failures/errors
```

## D. 集成 / 并发 / 迁移（真实 PostgreSQL）

| 命令 | 结果（2026-10-30） |
|---|---|
| `uv run pytest apps/api/tests/integration -q -m "integration and not migrations"` | **177 passed, 43 deselected** |
| `uv run pytest apps/api/tests/integration/kernel -q -m concurrency` | **48 passed, 116 deselected** |
| `uv run pytest apps/api/tests/integration/test_migrations.py -q` | **43 passed**（含 `alembic check` 无 diff 与 0005 往返；本阶段无新迁移） |

## E. Web 单元 / 组件 / 构建

| 命令 | 结果（2026-10-30） |
|---|---|
| `pnpm --filter @revnote/web lint` | 0 error（2 条既有 fast-refresh warning） |
| `pnpm --filter @revnote/web typecheck` | 通过 |
| `pnpm --filter @revnote/web test` | **215 cases, errors=0**；报告守卫 `vitest/full: 215 passed; zero skips, retries or errors` |
| `pnpm --filter @revnote/web build` | 通过（仅既有的 chunk-size 提示） |
| `pnpm --filter @revnote/web check:markdown` | 7/7 通过 |

新增/更新覆盖：

- `decodeSnapshotPageResponse`：水位一致、floor 只前进不倒退、终止游标、空页 + has_more 禁止、活跃对象、`updated_seq <= as_of`、页内/跨页重复。
- transport：capability 探测一次；多页 `limit`/`cursor` 循环；合并对象并保持单一 `as_of_seq`；旧服务端回退。

## F. Web E2E（真实浏览器 + 真 PG + 真 RustFS）

| 命令 | 结果（2026-10-30） |
|---|---|
| `uv run python apps/api/scripts/run_web_e2e.py --database-url …` | **21 passed**；报告守卫 `playwright/full: 21 passed; zero skips, retries or errors` |

标签 `snapshot_pagination`（`apps/web/e2e/pagination.spec.ts`）实际运行并通过：owned API 以 `SYNC_SNAPSHOT_PAGE_MAX_OBJECTS=2` 重启，预写 7 条笔记；断言 ≥4 次真实分页请求、所有请求带 `limit=2`、至少一次 `cursor`、bootstrap 后 7 条对象齐全且可编辑。该标签已加入 `apps/web/scripts/check-test-reports.ts` 的必需集合。

## G. 附件 / Joplin 回归（真 RustFS）

- `apps/api/tests/api -q` 的 118 passed 包含附件组与 Joplin 组，均连接真实 PostgreSQL + 真实 RustFS 1.0.4；不是 fake，也没有 skip。
- Joplin 报告守卫 `passed=17 required=17` 单独复跑通过（见 §C）。
- 本阶段未改附件/Joplin 适配器；capabilities 只增加 `snapshot_pagination` 兄弟字段，Phase 5/6 对象未改。

## H. CI

- **推送后运行并在 `.slim/deepwork/phase7-build/progress.md` 记录**：同一 SHA 连续两轮 `api` / `api-attachments`（含 `-m joplin`）/ `web` / `web-e2e` / `image` 五 job 全绿；第二轮使用 `gh run rerun`。
- 不以旧 run 绿色代替本次证据；失败只追加修复提交，不强推、不改写历史。

## I. 未运行 / 未宣称

- 公网 TLS、真实限流、备份恢复演练：本阶段不做，未验证。
- 本机 Docker 镜像构建：禁止，未运行；CI `image` job 是唯一镜像证据。
- 行级历史内容时间旅行：本阶段不实现；并发更新由 `pull since=as_of_seq` 补齐。
