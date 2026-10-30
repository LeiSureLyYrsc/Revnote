# Revnote Phase 8 实施计划

> **D1–D16 已按推荐默认值锁定。** 本文件是 Build 的正式计划基线：不含待定取舍，不再向用户索取计划。
>
> 状态：Plan 已锁定，Build 已启动。**助手已代用户起草并锁定推荐默认值**；文中 **取舍点** 仅标注取舍，不再等待用户补写计划。Build 可直接按本文与 `phase8-build-prompt.md` 启动。
>
> 基线：Phase 7 DONE — tip `feb1aff`；CI run `41035249235` 同一 SHA 连续两轮（attempt 2+3）五 job 全绿；对象存储仍为 **RustFS 1.0.4**（永久，不再回退 MinIO）；原生 sync snapshot 分页 + Web 多页 bootstrap 已交付；Joplin 桥默认关（`JOPLIN_BRIDGE_ENABLED=false`）。

---

## 一、基线核对与前置条件

### 1.1 已阅读材料

`README.md`、`docs/consensus-brief.md`、`docs/stack-blueprint.md`、`docs/build-conventions.md`、`docs/plan-phase2.md`–`docs/plan-phase7.md`、`docs/phase3-sync-api-spec.md`、`docs/phase7-snapshot-pagination.md`、`docs/phase7-validation.md`、`.slim/deepwork/phase7-build/progress.md`（PHASE 7 DONE）。

并核对：`apps/` 目前仅 `api` + `web`；根 `pyproject.toml` 的 `[tool.uv.workspace].members` 仅 `apps/api`；无 `apps/cli` / `revnote-cli`；原生认证与 sync 端点（login/refresh/logout、me、tree、note、push/pull/snapshot 分页、capabilities、attachments）已由 Phase 3–7 交付。

### 1.2 基线事实（Build 第一步复核，不能沿用旧结论）

| 项 | 实际观测 | 对 Phase 8 的影响 |
|---|---|---|
| 工作区 HEAD | `feb1aff docs: record phase7 snapshot pagination protocol and validation`，应与 `origin/main` 同步 | Phase 8 建在绿基线 + 分页 snapshot + RustFS + Joplin 默认关上 |
| CI | run `41035249235`：api / web / web-e2e / api-attachments（含 joplin）/ image 连续两轮 success（attempt 2+3；attempt 1 为既有 Vitest runner flake） | 新工作不得削弱五 job 与 joplin 守卫；CLI 加法须进报告守卫 |
| 客户端面 | Web 已是第一官方消费者；共识「Web + 开放 API + 参考 CLI」 | Phase 8 补第二官方消费者 |
| Snapshot | Phase 7 分页可用；capabilities 有 `snapshot_pagination` | CLI 必须消费分页（大库 bootstrap），并保留无参遗留路径兼容探测 |
| Joplin 桥 | 默认关；非主路径 | **CLI 只走原生 `/api/v1`**，不实现 Joplin 协议客户端 |
| 对象存储 | RustFS 1.0.4；本机 exe 已登记；CI pin 不变 | CLI 若做附件子命令则用真 RustFS；本阶段推荐最小附件读/写可选，非阻塞主路径 |
| 本机 | 无 Docker；真 PG 仓库外临时实例；PATH 需显式加 `D:\Git\cmd` 等；TEMP 在 `D:\tmp\…`；**C: 常仅 ~0.7GB 可用** | 一律优先 `D:\tmp`；禁止把大缓存/venv/测试产物落到 C: |
| 契约守卫 | 精确路由/错误码/表集合、报告守卫 no-skip | CLI 不得改服务端契约；只加法客户端与其测试 |

### 1.3 前置条件（Build 前必须满足，缺一即停）

1. **P1 — 真 PostgreSQL**：CLI 对真实 API 的集成/契约测试须打真 PG 上的 API；`REQUIRE_POSTGRES_TESTS=1`；每批 `finally` 停止。禁止 SQLite、禁止 skip。
2. **P2 — RustFS（若跑附件 CLI/回归）**：只用已登记 `D:\tools\rustfs\rustfs.exe` 与 CI pin 1.0.4。**禁止 MinIO、禁止下载未登记二进制。** 纯 auth/sync CLI 主路径可不启 RustFS。
3. **P3 — 工具路径**：`git`=`D:\Git\cmd\git.exe`、`gh`、`uv`=`D:\Dev\uv\uv.exe`、Python `D:\Dev\Python312\python.exe`、`pnpm.cmd`；`TEMP`/`TMP`=`D:\tmp\opencode-temp`（先 `mkdir`）；不覆盖用户未跟踪的历史 `phase*.md`；**不提交**根目录提示草稿除非明确是本阶段交付文档进 `docs/`。
4. **P4 — 磁盘**：C: 往往 critically low（约 0.7GB）。venv、uv cache、pytest 临时、OpenCode TEMP、PG 数据目录一律落在 **D:**（`D:\tmp\…` / 既有 D: 工具链）。禁止把新重大产物写到会撑爆 C: 的路径（若不可避免，改用 D: 或显式 env）。
5. **P5 — 基线绿**：HEAD=`feb1aff`（或以当时 `origin/main` 为准）且近期同 SHA 双轮 CI 结论已知；若 tip 已前进，以新 tip 为基线并在 progress 记录。

---

## 二、候选范围比较与推荐

### 2.1 比较

| 候选 | 价值 | 主要依赖与新增风险 | 依赖顺序判定 |
|---|---|---|---|
| **I. 参考 CLI** | 兑现共识「Web + 开放 API + 参考 CLI」；第二官方消费者；可脚本化验收原生 API（含 Phase 7 分页） | 新 uv 包/入口；token 落盘安全；CI 加法；维护面 | **Phase 8 主范围**（分页刚解除大库 bootstrap，CLI 此时最有价值） |
| **D. 自动三方合并** | 减少人工冲突；共识差异化候选（AST/块级合并 + Web UI） | 需可信 `merge_base_rev`；服务端可选合并路径；Web 冲突 UI；范围显著更大 | 后置（CLI 可先作为合并实验的消费者，但不在本阶段做合并） |
| **F. 公网加固** | TLS/真限流/备份 | 无部署目标；限流需共享状态；发布等级仍为受控预览 | 后置 |
| **J. 附件进事件流** | 附件可见于 pull/snapshot | 扩 `change_events.entity_type`；动摇「附件非内核实体」 | **明确不做**（Phase 5–7 已否决） |
| **K. Joplin 桥加深** | GUI 矩阵、Data API、E2EE | 非主路径；Phase 6 已收口且默认关 | 后置/不做 |

### 2.2 推荐结论

> **Phase 8 = I（参考 CLI：原生 `/api/v1` 第二官方消费者）。**
>
> 本阶段交付：*可安装的 `revnote` / `revnote-cli` 命令行客户端（uv workspace 成员 `apps/cli`），能对真实 API 完成登录与 token 轮换、列出工作区、拉 tree、读笔记、按 Phase 7 规则分页 snapshot bootstrap、pull 增量、最小 push（至少创建/更新笔记；文件夹按需）、探测 capabilities；冲突仍返回服务端 409 并由 CLI 诚实展示、不自动合并；凭据不进仓库、不打印到日志；原生五 job + joplin 守卫不削弱；RustFS/Joplin 默认关路径不变。*
>
> **未选原因**：Phase 7 已写明 I「分页之后更有价值」——现已满足；D 需要独立冲突协议与 Web UI，体积更大且不阻塞「第二消费者」；F 与「受控预览」定位不符；J/K 已否决或非主路径。

### 2.3 依赖顺序

```text
复核基线（HEAD/CI/RustFS pin/磁盘）+ 读 auth/sync/capabilities 契约与 Web transport 分页行为
        ↓
接口冻结：CLI 子命令、配置/token 目录、退出码、JSON/人读输出、分页消费规则
        ↓
骨架：apps/cli + uv workspace member + console_script 入口
        ↓
HTTP 客户端：login/refresh/logout、me、tree、note、snapshot(分页)、pull、push、capabilities
        ↓
真 PG：对 ASGI 或本机 uvicorn 的 CLI 契约/集成测试（含多页 snapshot）
        ↓
（可选）附件 upload/download 子命令 — 仅当不拖慢主路径；要真 RustFS
        ↓
文档 / README / CI 加法 / 报告守卫 → 提交 → 一次 push → CI 连续两轮全绿
```

**明确不选**：D、F、J、K（本阶段）。

---

## 三、目标与验收

### 3.1 本阶段实现

1. **包与入口（推荐默认，D3）**
   - 新增 `apps/cli`，包名 `revnote-cli`（import 包建议 `revnote_cli`）。
   - 根 `pyproject.toml` `[tool.uv.workspace].members` **加法** `"apps/cli"`；`[tool.uv.sources]` 加法。
   - Console script：`revnote = "revnote_cli.__main__:main"`（或等价）；`uv run --package revnote-cli revnote …` 可用。
   - **不**发布到 PyPI 作为本阶段硬门禁；不打包桌面安装器。

2. **认证与配置**
   - `login`：email/password → 保存 access/refresh（及可选 workspace 默认）到**用户配置目录**（Windows：可 `%APPDATA%\revnote\`，测试必须可用 `REVNOTE_CONFIG_DIR` 指到 `D:\tmp\…`）。
   - `logout` / `refresh`：撤销或轮换；过期自动 refresh 一次（失败则明确非 0 退出）。
   - **永不**把 token 写入仓库、progress、commit message、CI 日志；测试用临时目录。
   - 支持 `REVNOTE_API_BASE`（默认 `http://127.0.0.1:8000`）与 `--api-base`。

3. **只读命令**
   - `whoami` / `me`：用户与工作区列表。
   - `workspaces list`（可与 me 合并，须有一处稳定入口）。
   - `tree`：指定 workspace 的活跃树。
   - `note get <id>`：正文 + rev。
   - `capabilities`：打印服务端能力（含 `snapshot_pagination` / attachments / joplin_bridge 加法字段）。

4. **同步命令**
   - `sync snapshot`：若 capabilities 支持分页（或显式 `--page-size`），循环 `limit`+`cursor` 直至 `has_more=false`，拼出同一 `as_of_seq` 活跃集；否则走无参遗留路径并诚实处理 `409 snapshot_too_large`。
   - `sync pull --since <seq>`：消费既有 pull 游标语义。
   - `sync push`：至少支持创建/更新笔记（`body_md` + `base_rev`）；文件夹 create/rename/move 按最小集合实现（不足则文档标明「未实现」且命令隐藏或明确失败，禁止假成功）。
   - 冲突：服务端 `409` → CLI **非 0 退出**，stdout/stderr 打印固定可脚本解析的错误码/摘要；**不**自动三方合并、不静默覆盖。

5. **输出与退出码**
   - 默认人读；`--json` 机器可读（JSON Lines 或单 JSON，在计划/CLI help 写死一种）。
   - 退出码：0 成功；2 用法/参数；3 认证失败；4 冲突；5 API/网络；其它非 0 允许，但须在 `--help`/文档表列出。

6. **附件（推荐默认：最小可选，D10）**
   - **推荐**：本阶段实现 `attachment upload` / `attachment download` 最小闭环（走既有 API 代理，不预签名），便于 CLI 作为附件 API 第二消费者；测试需真 RustFS。
   - 若实现中发现严重拖慢或阻塞主路径，可降级为「文档标明未做」并在 progress 记录——但须先完成 auth+sync 主路径。**禁止**因此引入 MinIO 或新存储。

7. **测试与 CI**
   - 单元：参数解析、分页循环、错误映射（可 httpx mock）。
   - 真 PG 集成：启动/对接真实 API（沿用仓库既有 PG harness / ASGI+真库模式），覆盖 login→tree→paged snapshot→pull→push note→冲突 409。
   - CI：加法 `cli` 测试进入既有 api job **或** 独立 job，但**不得削弱**现有五 job；报告守卫 no-skip；同一 SHA 连续两轮全绿。
   - Web/E2E/joplin/attachments 回归不削弱。

### 3.2 必须成立的验收结果

| 目标 | 验收结果（真实环境） |
|---|---|
| 可安装入口 | `uv sync --locked` 后 `uv run --package revnote-cli revnote --help` 成功 |
| 认证 | 真 API 上 login/refresh/logout；token 仅在配置目录；日志无 token |
| 只读 | me/tree/note/capabilities 对真数据正确 |
| 分页 snapshot | 大库（N≥2*page+ε）经 CLI 拼页完备；水位字段一致 |
| pull/push | pull 增量正确；push 创建/更新笔记成功；冲突 409 → 非 0 + 可解析错误 |
| 回归 | 既有 api/web/e2e/attachments/joplin/image 不削弱 |
| 发布 | 本地全门后一次 push；同一 SHA **连续两轮** CI 全绿 |
| 安全/本机 | 未碰 Clash；未 docker build；未下载未登记二进制；C: 无新增巨型缓存 |

---

## 四、决策锁定表 D1–D16

| ID | 决策（推荐默认值） | 取舍点 |
|---|---|---|
| **D1** | 范围 = **I**（参考 CLI）；D/F/J/K 后置或不做 | 不做自动合并、不宣称公网就绪 |
| **D2** | Phase 2–7 不变量只增不改；RustFS 1.0.4 + `S3_*` + boto3 保持；Joplin 默认关；CLI **只**调原生 `/api/v1` | 禁止 MinIO；CLI 不做 Joplin 协议方言 |
| **D3** | 新包 `apps/cli`（`revnote-cli`）+ uv workspace 成员 + `revnote` 入口 | 不塞进 `revnote-api` 包以免服务端依赖膨胀 |
| **D4** | HTTP 客户端推荐 `httpx`（同步 CLI 足够；若全仓已有偏好可对齐）；超时/User-Agent `revnote-cli/<version>` | 不引入重型 SDK 生成器（本阶段手写最小客户端） |
| **D5** | Token 存用户配置目录；可用 `REVNOTE_CONFIG_DIR` 覆盖（测试必须覆盖到 D:\tmp 下） | 禁止存仓库内 `.revnote-token` 并提交 |
| **D6** | Snapshot：capabilities 有分页则强制走分页循环；`--page-size` 可调但不超过服务端 page max；无能力则遗留无参 | 与 Web transport 行为家族一致 |
| **D7** | 冲突策略：展示并退出非 0；无 merge、无「自动选 local/remote」 | 自动合并留给未来 D 阶段 |
| **D8** | 输出：默认 text；`--json` 稳定 schema（字段名写入 docs）；seq/rev 一律十进制字符串 | 避免精度问题在脚本中重现 |
| **D9** | 退出码表锁定（0/2/3/4/5…）并测至少认证失败与冲突 | 便于 CI/脚本 |
| **D10** | 附件子命令：努力最小 upload/download；若阻塞可砍并文档化——主路径是 auth+sync | 不做预签名、不做删除/GC |
| **D11** | 测试：真 PG 集成必过；mock 单测可辅；**skip≠通过** | — |
| **D12** | CI：CLI 测试进守卫；五 job 形状不削弱；同一 SHA 连续两轮全绿 | 不把「发布 PyPI」当门禁 |
| **D13** | 迁移：本阶段**无新服务端迁移**（纯客户端）；若误改 API 须另开决策并停 | 无 `0006` 除非发现硬阻塞服务端 bug 的最小修复 |
| **D14** | Orchestrator **禁止派 SubAgent**；模型 `deepseek-official/deepseek-flash#max`；**永不** `gpt-6.1-sol`；designer 仅必要时 `murasameai-vip/gemini-3.8-flash-high` | Standing |
| **D15** | 不打印/不提交 API keys、S3/RustFS 凭据、session token、密码；诊断只固定码与计数；**C: 磁盘紧张时所有 TEMP 在 D:\tmp** | — |
| **D16** | 文档：`docs/plan-phase8.md`、`docs/phase8-reference-cli.md`、`docs/phase8-validation.md`；README 已实现/未实现分区更新 | 正式文档进 `docs/`；根目录 `phase8-*.md` 可不提交 |

---

## 五、模块与文件边界（拟新增/修改）

| 边界 | 路径 | 职责 |
|---|---|---|
| CLI 包 | `apps/cli/`（`pyproject.toml`、`src/revnote_cli/**`） | 入口、子命令、配置、HTTP 客户端、输出 |
| Workspace | 根 `pyproject.toml`、`uv.lock` | members/sources/lock 加法 |
| 测试 | `apps/cli/tests/**`（及必要时 api 侧夹具复用） | 单测 + 真 PG 集成 |
| CI/守卫 | `.github/workflows/ci.yml`、`check_test_reports.py` | 加法 CLI 报告 |
| 文档 | `docs/plan-phase8.md`、`docs/phase8-reference-cli.md`、`docs/phase8-validation.md`、`README.md` | 规格与证据 |
| （可选）API | 仅当发现 CLI 暴露的硬阻塞 bug | 最小修复；默认零改服务端 |

**禁止越界**：削弱内核八命令/fingerprint；改 pull/snapshot 既有语义；附件进 `change_events`；预签名；替换 RustFS；自动三方合并；公网 TLS/限流产品化；Joplin 客户端模式；本地 docker build；碰 Clash；提交 `.env`/凭据/未跟踪历史 `phase*.md` 草稿；派 SubAgent；把 TEMP/venv 灌满 C:。

---

## 六、数据与协议变更（摘要）

### 6.1 迁移

- **无服务端迁移**（D13）。

### 6.2 客户端协议消费

| 能力 | CLI 行为 |
|---|---|
| Auth | `/api/v1/auth/login|refresh|logout`、`/api/v1/me` |
| Read | `tree`、`notes/{id}`、`capabilities` |
| Sync | `snapshot`（分页加法）、`pull`、`push` |
| Attachments（可选） | Phase 5 代理上传/下载 |

不新增服务端端点除非硬阻塞（须在 progress 证明）。

### 6.3 兼容策略

- 旧部署无 `snapshot_pagination`：CLI 走无参 snapshot，过大则明确失败（退出码非 0），提示升级服务端。
- Joplin 桥开关不影响 CLI（CLI 不调用桥路由）。

---

## 七、测试策略与证据责任

| 承重主张 | 最小证据 |
|---|---|
| 入口/help | `uv run --package revnote-cli revnote --help` 退出 0 |
| 认证 | 真 PG+API：login 存 token；refresh；logout；失败退出码 |
| 分页 snapshot | 真 PG：N=2*page+ε；CLI 拼集 == 水位活跃集 |
| push/冲突 | 真 PG：更新成功；制造 base_rev 冲突 → 退出 4 + 可解析码 |
| 无泄漏 | 测试日志/progress 抽样无 token/密码 |
| 回归 | 既有 api/joplin/attachments/web/e2e/image 不削弱；报告守卫 |

---

## 八、执行方式（Standing）

> **禁止 SubAgent。** Orchestrator（`deepseek-official/deepseek-flash#max`）独占实现、测试、文档、提交与发布。  
> 不得使用 `gpt-6.1-sol`。不得打印 API keys。限额/鉴权错误立即停并写 progress。  
> 进度文件：`.slim/deepwork/phase8-build/progress.md`。  
> TEMP/TMP：`D:\tmp\opencode-temp`。不碰 Clash。Murasame-PC 禁止 docker build。  
> **C: ~0.7GB**：优先 D:\tmp；不要往 C: 灌缓存。

串行点：基线复核 → 冻结 CLI 契约 → 包骨架 → HTTP 客户端+子命令 → 真 PG 门禁 →（可选附件）→ 文档/CI → 提交 → push → 双轮 CI。

---

## 九、建议 Conventional Commits（可按实况拆合 4–7 个）

1. `docs: capture the phase8 reference cli plan`
2. `feat(cli): add revnote-cli workspace package and entrypoint`
3. `feat(cli): implement auth, read and paged sync commands`
4. `test(cli): prove reference cli against real postgres api`
5. `feat(cli): add minimal attachment upload download`（若做）
6. `ci: gate reference cli tests in reports`
7. `docs: record phase8 reference cli scope and acceptance`

全部本地验收通过后**一次 push**；失败只追加修复；不强推；同一 SHA 连续两轮 CI 全绿。

---

## 十、风险与已知限制

| 风险 | 应对 |
|---|---|
| C: 磁盘耗尽导致 uv/pytest/OpenCode 失败 | TEMP/配置/PG 全在 D:；开始前检查可用空间 |
| Token 泄漏进 CI 日志 | 测试用临时配置目录；禁止打印 token；守卫抽查 |
| CLI 与 Web 分页行为漂移 | 同 capabilities 字段；共享文档用例 |
| 范围膨胀成「完整桌面同步客户端」 | D1/D10 砍 TUI/后台守护/自动合并 |
| 公网滥用 | 真限流仍未做；发布等级仍为受控预览 |
| SubAgent 供应商缓存 | 本阶段禁用 SubAgent |
| 本机 RustFS 曾被清理停掉 | 需要时再启已登记 exe；**禁止下载新二进制** |

---

## 十一、本阶段不做

- 自动三方合并 / CRDT / 冲突副本自动上传 / Web 冲突 UI 重做
- 公网加固（TLS、真限流、备份演练、生产反向代理）
- 附件进入 `pull`/`snapshot` / 扩内核 `entity_type`
- Joplin 协议 CLI、Data API、GUI 自动化硬门禁、桥功能扩张
- 预签名直传、MinIO、第二对象存储
- PWA / 全离线冷启动 / 页面关闭后后台同步 / 桌面或移动端
- 搜索、时间机器、OIDC、Passkey、成员管理中心、PyPI 正式发布流水线
- 本地 Docker 镜像构建；碰 Clash；打印或提交密钥；派 SubAgent

---

## 十二、批准记录

本计划由助手根据 Phase 7 收口状态（tip `feb1aff`）、README 未实现清单、共识简报（「Web + 开放 API + 参考 CLI」）与 Phase 6/7 候选表起草。**在 I / D / F 中选定 I（参考 CLI）为 Phase 8**，理由见 §2.2；**D1–D16 按推荐默认值锁定**，供 OpenCode Build（orchestrator-only）直接执行，无需再向用户索取 phase8-plan/build-prompt。

Build 启动顺序：**P1–P5 → 基线复核 → 冻结 → 实现 → 全门 → 提交 → 一次 push → CI 双轮全绿**。