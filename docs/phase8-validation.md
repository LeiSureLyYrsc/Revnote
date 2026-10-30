# Phase 8 验收记录（参考 CLI）

> 本文件只写**实际运行过**的结果。没有运行的项目明确标注「未运行」，不以计划、推断或历史数字代替。
> 范围与决策见 `docs/plan-phase8.md`，CLI 契约见 `docs/phase8-reference-cli.md`。

## 0. 环境与基线

| 项 | 实际值 |
|---|---|
| 日期 | 2026-10-30 |
| 仓库 | `D:\Github\Revnote`（GitHub `LeiSureLyYrsc/Revnote`） |
| 基线 HEAD | `feb1aff754174220f3f2782c974dd31f12675f0f`（= `origin/main`） |
| 平台 | Windows，Python 3.12.10，pytest 9.2.0，anyio 4.15.1 |
| PostgreSQL | 仓库外临时实例（`pgserver` 控制器），`127.0.0.1:62096`，两个独立可丢弃库 `revnote_phase2_test` / `revnote_phase2_migrations` |
| 对象存储 | 本机 RustFS **未启动**（本阶段主路径 auth/sync/CLI 不需要；附件 CLI 用例走 mock transport） |
| Docker | 未使用；本机无 Docker |
| 临时目录 | `TEMP`/`TMP` = `D:\tmp\opencode-temp`；pytest 临时目录落在 `D:\tmp\...` |
| 凭据 | 测试统一指向 pytest `tmp_path` 下的 `config/`；未读取或提交任何 `.env` |

---

## 1. 包与入口

```powershell
uv lock --check          # 0
uv sync --locked         # 0，安装 revnote-cli 0.1.0
uv run --package revnote-cli revnote --help   # 0
```

`--help` 输出包含全部子命令、全局选项与退出码表；`--version` 输出 `revnote 0.1.0`。

---

## 2. 静态检查

```powershell
uv run ruff check apps/api apps/cli        # All checks passed
uv run ruff format --check apps/api apps/cli  # 173 files already formatted
uv run --with "ty==0.0.94" ty check ... apps/cli/src   # All checks passed
```

`ty` 使用 CI 相同的显式路径清单（API 生产模块 + `apps/cli/src`），未添加忽略文件。

---

## 3. CLI 自动化测试

### 3.1 单元（mock transport，无外部服务）

```powershell
uv run pytest apps/cli/tests/unit -q
# 41 passed
```

覆盖：配置目录优先级、凭据原子读写/repr 不泄露 token、退出码表、错误信封映射、401 自动 refresh 一次、snapshot 分页拼装/水位不一致/重复对象/空页+has_more、legacy 回退、页大小上限、pull 游标链与对象取最新、push 冲突结果、网络错误、login/logout/me/tree/note/capabilities、attachment upload/download。

### 3.2 真 PostgreSQL + 真 socket + 子进程入口

```powershell
$env:TEST_DATABASE_URL = 'postgresql+asyncpg://postgres:@127.0.0.1:62096/revnote_phase2_test'
$env:TEST_MIGRATION_DATABASE_URL = 'postgresql+asyncpg://postgres:@127.0.0.1:62096/revnote_phase2_migrations'
$env:REQUIRE_POSTGRES_TESTS = '1'
uv run pytest apps/cli/tests -v -m cli --junitxml=api-cli.xml
# 48 passed in 48.32s（41 unit + 7 integration）
```

7 个真库用例（每个 `open_app` 一个独立账号/workspace，Alembic 迁移到 head，uvicorn 真 socket，CLI 以 `python -m revnote_cli` 子进程运行）：

1. `test_cli_login_me_tree_logout_keep_tokens_in_the_config_dir` — 登录、`me`、`tree`、`logout`；token 只落配置目录，stdout/stderr 不含保存的 access token；logout 删除文件后 `me` 退出 3。
2. `test_cli_paged_snapshot_collects_every_object_and_pins_the_watermark` — 2 文件夹 + 7 笔记，`--page-size 3`，对象数 9、页数 ≥3、`as_of_seq` 一致、无重复、`updated_seq <= as_of_seq`；随后 `pull --since as_of_seq` 为空且 `next_cursor == as_of_seq`。
3. `test_cli_pull_returns_the_increment_after_the_snapshot` — snapshot 后新增 2 篇，pull 得到 2 事件/2 对象，`last_seq = as_of_seq + 2`。
4. `test_cli_push_note_create_and_update_round_trip` — 创建（rev 1）→ `note get` 正文一致 → 更新（rev 2）→ 再读正文为第二次内容。
5. `test_cli_push_conflict_exits_four_and_leaves_the_note_untouched` — 错误 `base_rev` → 退出 4；stdout `status=conflict`，stderr JSON `rev_conflict`；笔记 rev/正文未变。
6. `test_cli_capabilities_are_available_without_a_session` — 未登录可读能力；`snapshot_pagination.enabled=true`，`joplin_bridge.enabled=false`，attachments 字段存在。
7. `test_cli_me_without_a_session_exits_three` — 未登录 `me` 退出 3、`not_logged_in`。

报告守卫：

```powershell
uv run python apps/api/scripts/check_test_reports.py --group cli
# api/cli: passed=48 required=7; zero skips/failures/errors
```

---

## 4. 既有 API 回归（真 PostgreSQL）

按 CI `api` job 的八组原样运行，使用同一实例：

| 组 | 命令（摘要） | 实际结果 |
|---|---|---|
| unit | `pytest apps/api/tests/unit` | **1250 passed** |
| integration | `-m "integration and not concurrency and not migrations"` | **129 passed, 91 deselected** |
| concurrency | `pytest apps/api/tests/integration/kernel -m concurrency` | **48 passed, 116 deselected** |
| migrations | `pytest apps/api/tests/integration/test_migrations.py` | **43 passed** |
| http | `pytest apps/api/tests/api -m "api and not api_concurrency and not transport and not attachments"` | **81 passed, 37 deselected** |
| http-concurrency | `-m api_concurrency` | **6 passed, 112 deselected** |
| transport | `-m transport` | **3 passed, 116 deselected** |
| cli | `pytest apps/cli/tests -m cli` | **48 passed** |

八组均 0 skip / 0 failure / 0 error。报告守卫：

```powershell
uv run python apps/api/scripts/check_test_reports.py \
  --group unit --group integration --group concurrency --group migrations \
  --group http --group http-concurrency --group transport --group cli
# 八组全部通过；api/cli required=7
```

> 注：`--group` 是有针对性的证据，不等于默认十组全量门禁；attachments / joplin 两组本机未运行（见 §6）。

---

## 5. Web 回归（未改 Web 代码）

```powershell
pnpm --filter @revnote/web lint        # 0 errors（2 条既有 react-refresh warning）
pnpm --filter @revnote/web typecheck   # 0
pnpm test:web                          # vitest/full: cases=215; errors=0
```

---

## 6. 未运行 / 不声称

以下项目本机**未运行**，本记录不据此声称通过；它们由推送后的 CI 或后续环境验证：

- `api-attachments`（真 PostgreSQL + 真 RustFS）与 `api-joplin`：本机未启动 RustFS，未运行。服务端代码本阶段未改。
- `web-e2e`（Chromium + 真 RustFS + 一次性 PG 库）：未运行。
- `image`（GHCR 构建/推送）：本机无 Docker，只在 CI 运行。
- 公网部署：TLS、真实限流、备份恢复、反向代理均未实现/未验证。
- CLI 附件的真实 RustFS 端到端：未运行；只有 mock transport 的单测。附件存储证明仍由 `api-attachments` 的 API 测试承担。
- 两次同 SHA CI 全绿：推送后补充实际 run id 与结论。

---

## 7. 安全与边界核对

- 未打印 API key、RustFS 凭据、session token 或密码；CLI 测试断言 stdout/stderr 不含保存的 access token。
- 未提交 `.env`、凭据、trace/HAR/storageState。
- 未碰 Clash；未执行 `docker build` / `compose build`。
- 未下载任何未登记二进制；RustFS 保持 1.0.4（本机未启动）。
- 未新增服务端迁移；未改 `/api/v1` 契约、内核八命令、pull/snapshot 语义、附件事件边界。
- 根目录未跟踪的 `phase*.md` 草稿保持原样，未提交、未删除。

---

## 8. 本地收尾

- 临时 PostgreSQL 由外部控制器停止（`revnote_phase2_pg.py stop`），状态文件删除。
- pytest 临时目录与配置目录位于 `D:\tmp\...`；未向 C: 写入缓存或大产物。
- 本记录将在推送与 CI 完成后追加「CI 证据」小节（同一 SHA 连续两轮）。
