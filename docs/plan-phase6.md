# Revnote Phase 6 — Plan (locked)

> **D1–D16 已按推荐默认值锁定。** 本文件是 Build 的唯一计划来源；phase6-plan.md（会话工作目录）与本文件同源。

# Revnote Phase 6 实施计划

> 状态：Plan 输出（D1–D16 锁定后进入 Build）。**助手已代用户起草并锁定推荐默认值**；文中 **取舍点** 仅标注取舍，不再等待用户补写计划。Build 可直接按本文与 `phase6-build-prompt.md` 启动。
>
> 基线：Phase 5 DONE — tip `c93f994`；CI run `41018813701` 同一 SHA 连续两轮五 job 全绿；对象存储为 **RustFS 1.0.4**（永久，不再回退 MinIO）。

---

## 一、基线核对与前置条件

### 1.1 已阅读材料

`README.md`、`docs/consensus-brief.md`、`docs/stack-blueprint.md`、`docs/build-conventions.md`、`docs/plan-phase2.md`–`docs/plan-phase5.md`、`docs/phase3-sync-api-spec.md`、`docs/phase5-attachments-api.md`、`docs/phase5-validation.md`、`.slim/deepwork/phase5-build/progress.md`（PHASE 5 DONE / RustFS）。

并核对：`apps/api/src/revnote_api/{api/router,kernel,services,storage,models}`、`apps/api/tests/**`、`.github/workflows/ci.yml`、`docker-compose.yml`、`.env.example`。当前**无** `adapters/joplin/` 实现（蓝图已约定路径）。

### 1.2 基线事实（Build 第一步复核，不能沿用旧结论）

| 项 | 实际观测 | 对 Phase 6 的影响 |
|---|---|---|
| 工作区 HEAD | `c93f994 docs: record the final rustfs ci evidence and the conflict-scope fix`，应与 `origin/main` 同步 | Phase 6 建在绿基线 + RustFS 上 |
| CI | run `41018813701`：api / web / web-e2e / api-attachments / image 连续两轮 success | 新工作不得削弱五 job |
| 对象存储 | 代码/CI/compose 均为 RustFS 1.0.4 + boto3/`S3_*`；本机 `D:\tools\rustfs\rustfs.exe` SHA-256 `3396617e…d899`；CI zip SHA-256 `4924ad4b…c31b5c` | **RustFS 保持**；资源同步走既有附件端口，禁止引入 MinIO |
| 附件承重 | 附件不进内核；API 代理流式；`pending→ready`；正文 Markdown 引用 | Joplin `.resource/` 必须映射到同一附件服务，不得另起一套存储 |
| 已知遗留（Phase 5 记录） | README/`phase5-validation`：并发上传丢字节（B1 类）、E2E runner 未把存储 env 传给子进程导致 4 个附件标签缺真浏览器证据 | 若 Joplin 资源验收依赖真上传路径，Build 须先做最小修复（见 H）；否则不得宣称资源双向兼容 |
| 本机 | 无 Docker；真 PG 用仓库外临时实例；真 RustFS owned/external；PATH 需显式加 `D:\Git\cmd` 等 | 与 Phase 5 相同 |
| 契约守卫 | `FORBIDDEN_MODULES`、精确路由/错误码/表集合、报告守卫 no-skip | 适配器只能**加法**；内核零 Joplin import |

### 1.3 前置条件（Build 前必须满足，缺一即停）

1. **P1 — RustFS**：本机 `D:\tools\rustfs\rustfs.exe` 存在且 SHA 与 `docs/phase5-validation.md` 一致；CI 继续 pin 1.0.4 + zip digest。**禁止下载未登记二进制、禁止换回 MinIO、禁止其他 S3 产品替代验收。**
2. **P2 — 仓库外临时 PostgreSQL**：业务/迁移/E2E/（如有）Joplin 集成库彼此独立；`REQUIRE_POSTGRES_TESTS=1`；每批 `finally` 停止。
3. **P3 — 真实浏览器**（仅当本阶段跑 Web 回归或附件 E2E 接线时）：Playwright Chromium 已装。
4. **P4 — Joplin 协议参考**：以 Joplin Server 公开协议与客户端 `file-api-driver-joplinServer`（基线 **v3.7.x**）为准；在仓库落盘 `docs/joplin-protocol-notes.md`（摘录端点/形状/错误码，不整页粘贴第三方版权全文）。
5. **P5 — 工具路径**：`git`=`D:\Git\cmd\git.exe`、`gh`、`uv`、`pnpm.cmd`；不覆盖用户未跟踪的历史 `phase*.md`；**不提交**根目录提示草稿除非明确是本阶段交付文档进 `docs/`。

---

## 二、候选范围比较与推荐

### 2.1 比较

| 候选 | 价值 | 主要依赖与新增风险 | 依赖顺序判定 |
|---|---|---|---|
| **C. Joplin 兼容桥** | 让既有 Joplin 客户端把 Revnote 当 Joplin Server 同步目标；兑现「Joplin 仅兼容桥、非主路径」 | Server 协议 session/items/delta/locks/资源；ID 映射；不得反向改造内核；客户端行为差异 | **Phase 6 主范围**（附件已就绪） |
| **H. 附件验收接线/缺陷** | 资源双向证明需要可用的真上传路径 | 只修 Phase 5 已记录、阻塞资源映射的最小项；不加 retry/skip | **C 的硬前置（按需）** |
| **E. snapshot 分页** | 解除大工作区限制 | 与 Joplin 桥弱相关 | 后置 |
| **D. 自动三方合并** | 减少人工冲突 | 客户端可信 base；与桥正交 | 后置 |
| **F. 公网加固** | TLS/真限流/备份 | 无部署目标；限流需共享状态 | 后置；发布等级仍为受控预览 |
| **I. 参考 CLI** | 第二官方消费者 | 维护成本 | 未选 |

### 2.2 推荐结论

> **Phase 6 = H（仅阻塞资源/桥的最小附件修复，若复现仍在）+ C（Joplin Server 协议适配器）。**
>
> 本阶段交付：*启用桥之后，Joplin 客户端（基线 3.7.x 协议行为）可用兼容 session 登录，对同一工作区完成笔记/文件夹的 put/get 与 delta 游标增量同步；同步锁语义可被客户端理解；资源（附件）经适配器落到 Phase 5 附件状态机 + RustFS，字节可往返；原生 `/api/v1` 与 Web/E2E/内核不变量不被削弱；关闭桥时行为与 Phase 5 完全一致。*

### 2.3 依赖顺序

```text
复核基线（HEAD/CI/RustFS SHA）+ 协议笔记落盘
        ↓
H（若需要）：附件 E2E env 接线 / 并发上传丢字节最小修复
        ↓
接口冻结：挂载前缀、ID 映射表、locks 表、错误映射、能力开关
        ↓
迁移 0005 + adapters/joplin 骨架（auth/session → items → delta → locks → resources）
        ↓
真实 PG（+ 资源路径真 RustFS）协议测试与兼容脚本
        ↓
文档 / 报告守卫 / CI 加法 → 提交 → 一次 push → CI 连续两轮全绿
```

**明确不选**：D、E、F、I；完整 Joplin GUI 手工矩阵可记录为「本机可选证据」，不以 GUI 自动化为 CI 硬门禁（见 D12）。

---

## 三、目标与验收

### 3.1 本阶段实现

1. **适配器边界**
   - 新增 `apps/api/src/revnote_api/adapters/joplin/`：**只**做协议翻译，调用 `kernel.*` / `services.*` / 附件端口；**禁止**内核与 `services/sync_*` import 适配器。
   - 挂载 Joplin Server 风格路径（与原生 `API_V1_PREFIX=/api/v1` 并存）。推荐前缀策略见 **D3**：默认在应用根挂载兼容路由（如 `/api/sessions`、`/api/items…`、`/api/locks`），由 `JOPLIN_BRIDGE_ENABLED` 控制是否注册。
   - `GET /api/v1/capabilities` **加法**声明 `joplin_bridge: {enabled, protocol_baseline}`；旧客户端忽略未知字段。

2. **身份与会话**
   - 提供 Joplin 客户端可消费的 email/password session 端点（形状对齐 Server，不引入 OIDC/Passkey）。
   - 会话映射到既有 Revnote 用户与工作区成员；非成员统一 404；token/密码永不进 URL/日志/报告。

3. **Items + Delta**
   - Items：笔记/文件夹（及资源元数据）的读写路径对齐 Joplin Server item 命名（含 `root:/…` 类路径约定，细节写在 `docs/joplin-protocol-notes.md`）。
   - Delta：把工作区 `change_events`/`seq` 翻译为 Joplin delta（`items`/`cursor`/`has_more`）；无效游标 → `resyncRequired`；事件压缩规则在适配器内实现（update 折叠、create+delete 抹除等），**不改**内核事件表语义。

4. **Locks**
   - SYNC / EXCLUSIVE 锁：适配器自有表（或明确隔离的存储），超时与刷新语义对齐 Joplin sync_lock 规范；不进入 `change_events`，不推进 `last_seq`。

5. **ID 映射**
   - 表（迁移 `0005`）保存 `jop_id`（或 item path 键）↔ 原生 UUID（note/folder/attachment）；原生实体仍以 UUID 为权威。

6. **Resources**
   - `.resource/` 二进制经适配器调用既有附件服务（API 代理流式 + SHA-256 + RustFS）；不预签名；不新建第二套对象键方案（可在既有 `ws/{ws}/att/...` 上增加映射行）。

7. **H（按需）**
   - 若 Build 复核仍复现：修好 `run_web_e2e.py` 向子进程注入 `S3_*`/`ATTACHMENTS_ENABLED`（或现行等价名）；以及并发上传丢字节的最小修复。**不加 retries/skip、不放宽承重断言。**

### 3.2 必须成立的验收结果

| 目标 | 验收结果（真实环境） |
|---|---|
| 内核隔离 | `adapters` 外零 Joplin 符号；`kernel/**` 与 `services/sync_*.py` 行为/响应不变；附件仍不进 `change_events` |
| 开关默认关 | `JOPLIN_BRIDGE_ENABLED=false` 时兼容路由不暴露或统一关闭；原生五 job 与 Phase 5 行为不变 |
| Session | 合法用户拿到可续用会话；错误密码不泄漏用户是否存在的多余信息（对齐既有 auth 口径）；凭据不落日志 |
| 笔记往返 | 经 Joplin items API 写入的笔记可被原生 pull/snapshot 读到等价正文；反之原生写入可被 delta/items 读到 |
| Delta 游标 | 连续两次 delta 不丢不重（在压缩语义下）；坏游标 → `resyncRequired` 且全量可恢复 |
| Locks | SYNC 可并存；EXCLUSIVE 互斥；过期锁可被忽略/拒绝刷新（按规范） |
| 资源字节 | 经桥上传的资源 → 附件 `ready` → 另一客户端（原生或桥）下载字节一致；真 RustFS |
| 授权 | 非成员/跨工作区 404；报告无 token/正文/资源字节 |
| 回归 | 既有 API 报告组 + `api-attachments` + Web 单测 + E2E（含既有 20 标签，若 H 接线则附件 4 标签亦有真证据）+ 报告守卫不削弱 |
| 发布 | 本地全门后一次 push；同一 SHA **连续两轮** CI 全绿 |

---

## 四、决策锁定表 D1–D16

| ID | 决策（推荐默认值） | 取舍点 |
|---|---|---|
| **D1** | 范围 = H（按需最小）+ C（Joplin Server 桥）；D/E/F/I 后置 | 不宣称公网就绪、不做自动合并 |
| **D2** | Phase 2–5 不变量只增不改；RustFS 1.0.4 + `S3_*` + boto3 保持 | 禁止 MinIO 回潮 |
| **D3** | 兼容路由挂在应用根 `/api/...`（Joplin 习惯），原生保持 `/api/v1/...`；由 `JOPLIN_BRIDGE_ENABLED` 控制注册 | 另一方案是 `/joplin/api/...`，需客户端自定义 base，兼容性更差 |
| **D4** | 适配器只调 kernel/services/附件端口；协议压缩、path、`resyncRequired`、locks 留在适配器 | 不把 Joplin 形状泄漏进内核表 |
| **D5** | 迁移 `0005_joplin_bridge`：ID 映射表 + locks 表（名称以实现为准）；**不改**既有十一张表列语义 | 精确集合断言按加法更新 |
| **D6** | 权威 ID 仍为原生 UUID；`jop_id`/path 仅为映射 | 避免双权威 |
| **D7** | 资源走既有附件状态机 + RustFS；桥不实现第二存储 | 完整兼容依赖 Phase 5，已满足 |
| **D8** | 认证：email/password session 兼容面；不做 Joplin Data API、不做 E2EE 明文冲突策略扩展 | E2EE 笔记可按「不透明 blob」尽力传输，不承诺服务端解密/搜索 |
| **D9** | 基线客户端协议：**Joplin 3.7.x** Server driver 行为；以协议笔记 + HTTP 契约测试为准 | 不绑定单一安装包版本号到 CI 镜像 |
| **D10** | `JOPLIN_BRIDGE_ENABLED` 默认 `false`；capabilities 加法声明 | 未配置时不可「看起来支持」 |
| **D11** | 测试：单测（压缩/映射）+ 真 PG 协议组 `api-joplin` + 资源用例要真 RustFS；**skip≠通过** | — |
| **D12** | CI：新增报告组/用例进守卫；优先并入现有 job 或独立 `api-joplin`（若需 RustFS 则挂真实存储 job）；**不**把完整 Joplin GUI 自动化列为硬门禁 | GUI 可作为本机可选附录证据 |
| **D13** | H 仅修复阻塞桥/资源证明的 Phase 5 遗留；不扩附件产品范围（仍无删除 UI/去重/预签名） | — |
| **D14** | Orchestrator **禁止派 SubAgent**（Standing：主机 agent registry 曾缓存错误供应商导致 403）；必要时仅 designer 用 `murasameai-vip/gemini-3.8-flash-high` 且不传错模型；orchestrator 模型 `deepseek-official/deepseek-flash#max`；**永不** `gpt-6.1-sol` | 与 Phase 5 末段 resume 一致 |
| **D15** | 不打印/不提交 API keys、S3/RustFS 凭据、session token；诊断只固定码与计数 | — |
| **D16** | 文档：`docs/plan-phase6.md`、`docs/joplin-protocol-notes.md`、`docs/phase6-joplin-bridge.md`、`docs/phase6-validation.md`；README 分区更新已实现/未实现 | 不改写已锁定 Phase 3 契约正文，只附录 |

---

## 五、模块与文件边界（拟新增/修改）

| 边界 | 路径 | 职责 |
|---|---|---|
| 适配器包 | `apps/api/src/revnote_api/adapters/joplin/{__init__,routes,serialize,delta,locks,session,ids,resources}.py` | 协议翻译 |
| 挂载 | `api/router.py` 或 `main.py`（**Orchestrator 独占**） | 条件注册兼容路由 |
| 模型/迁移 | `models/joplin_*.py`、`migrations/versions/0005_*.py`、`models/__init__.py` | 映射 + locks |
| 配置 | `config.py`、`.env.example` | `JOPLIN_BRIDGE_ENABLED` 等非敏感项 |
| 能力 | `routes/attachments.py` capabilities 或独立 | 加法字段 |
| 测试 | `tests/unit/test_joplin_*.py`、`tests/api/test_joplin_*.py`、（资源）attachments 标记组 | 契约与往返 |
| 脚本 | `apps/api/scripts/check_joplin_compat.py`（可选） | 固化关键路径冒烟 |
| H 接线 | `apps/api/scripts/run_web_e2e.py` 等 | 仅最小修复 |
| CI/守卫 | `.github/workflows/ci.yml`、`check_test_reports.py` | 加法 |
| 文档 | `docs/plan-phase6.md`、`docs/joplin-protocol-notes.md`、`docs/phase6-joplin-bridge.md`、`docs/phase6-validation.md`、`README.md` | 规格与证据 |

**禁止越界**：改内核八命令/fingerprint/结果语义；改 Phase 3 push/pull/snapshot 响应；把附件重新做成内核实体；预签名直传；替换 RustFS；本地 docker build；碰 Clash；提交 `.env`/凭据/未跟踪历史 `phase*.md` 草稿。

---

## 六、数据与协议变更（摘要）

### 6.1 迁移 `0005`（加法）

- `joplin_id_map`（名可变）：`workspace_id`、`jop_key`、`entity_kind`（folder/note/attachment）、`entity_id` UUID、唯一约束与索引。
- `joplin_locks`：`workspace_id`/`user` 维度、`lock_type`（sync|exclusive）、`client_type`、`client_id`、`updated_at`；超时由服务逻辑判定。
- 不改 `change_events` CHECK、不扩 `entity_type`。

### 6.2 兼容端点（示意，以协议笔记定稿为准）

| 面 | 职责 |
|---|---|
| Session | 登录/登出/当前用户（Joplin Server 形状） |
| Items | PUT/GET/DELETE item content；path/`jop_id` 映射 |
| Delta | `GET …/delta?cursor=` → items + cursor + has_more；坏游标 resyncRequired |
| Locks | GET/POST/DELETE；SYNC 多持有者、EXCLUSIVE 单持有者 |
| Resources | 二进制读写 → 附件服务 |

错误信封：在 Joplin 客户端期望的 code/body 与原生错误码之间做**显式映射表**；映射表单测覆盖。

### 6.3 兼容策略

- 仍不引入原生 API v2；桥为平行面。
- 旧 Web/原生客户端不调用桥则零影响。
- 关桥：新迁移表可保留（无人写）；路由不注册。

---

## 七、测试策略与证据责任

| 承重主张 | 最小证据 |
|---|---|
| 内核零泄漏 | import 边界测试 + `kernel/**`/`sync_*` diff 为空 |
| Delta 压缩/游标 | 单测表驱动 + 真 PG 序列 | 
| 笔记双向 | 真 PG：桥写→原生读；原生写→桥 delta/items 读 |
| 资源字节 | 真 PG + 真 RustFS：上传→ready→下载一致 |
| Locks | 真 PG：SYNC 并存、EXCLUSIVE 冲突、过期刷新失败 |
| 关桥回归 | enabled=false 时兼容路径不可用；原生全门绿 |
| 报告守卫 | 新组进入守卫；旧组不减 |

E2E：不以完整 Joplin GUI 为 CI 硬门禁；若 H 接线完成，既有 4 个附件 Playwright 标签必须有真证据。

---

## 八、执行方式（Standing，覆盖 Phase 5 旧车道表）

> **禁止 SubAgent。** Orchestrator（`deepseek-official/deepseek-flash#max`）独占实现、测试、文档、提交与发布。  
> 不得使用 `gpt-6.1-sol`。不得打印 API keys。限额/鉴权错误立即停并写 progress。  
> 进度文件：`.slim/deepwork/phase6-build/progress.md`。

串行点：基线复核 →（H）→ 接口冻结 → 迁移+适配器 → 真 PG/RustFS 门禁 → 文档/CI → 提交 → push → 双轮 CI。

---

## 九、建议 Conventional Commits（可按实况拆合 4–7 个）

1. `fix(api): restore attachment e2e wiring`（仅当 H 需要）
2. `docs: capture joplin server protocol notes for phase6`
3. `feat(api): add joplin id map and lock tables`
4. `feat(api): expose joplin server session and item routes`
5. `feat(api): translate native change events into joplin delta`
6. `feat(api): map joplin resources onto attachment storage`
7. `test(api): prove joplin bridge gates on real postgres`
8. `docs: record phase6 joplin bridge scope and acceptance`

全部本地验收通过后**一次 push**；失败只追加修复；不强推；同一 SHA 连续两轮 CI 全绿。

本地全门（摘要）：

```powershell
$env:PATH = "D:\Git\cmd;C:\Program Files\GitHub CLI;D:\Dev\uv;D:\Dev\nodejs;D:\Dev\nodejs\node_modules\corepack\shims;$env:PATH"
uv sync --locked
pnpm install --frozen-lockfile
# ruff / ty / pytest 全量（含 api-joplin 与 attachments，真 PG + 真 RustFS）
# pnpm lint/typecheck/test/build + e2e + 报告守卫
git push origin main
gh run watch <run-id> --repo LeiSureLyYrsc/Revnote --exit-status
# 第二轮：gh run rerun <run-id> --failed 或空闲 rerun，再 watch；同一 SHA 两轮全绿
```

---

## 十、风险与已知限制

| 风险 | 应对 |
|---|---|
| Joplin 客户端小版本差异 | 锁 3.7.x 协议行为；差异记入 validation「未验证」 |
| E2EE / 分享 / 多端排他细节 | 本阶段不承诺；不透明 blob 尽力 |
| 附件遗留缺陷阻塞资源 | H 最小修复；修不好则 HARD STOP，不宣称资源兼容 |
| 路径/UTF-8/大笔记 | 单测 + 上限复用既有请求限制 |
| 公网滥用 | 真限流仍未做；发布等级仍为受控预览 |
| SubAgent 供应商缓存 | 本阶段禁用 SubAgent |

---

## 十一、本阶段不做

- 自动三方合并 / CRDT / 冲突副本自动上传
- snapshot 分页与事件清理
- 公网加固（TLS、真限流、备份演练、生产反向代理）
- 参考 CLI、搜索、时间机器、OIDC、Passkey、成员管理中心
- Joplin **Data API** 作为产品基础；把 Joplin 提升为主路径
- 预签名直传、MinIO、第二对象存储、附件删除 UI/去重
- PWA / 全离线冷启动 / 页面关闭后后台同步
- 本地 Docker 镜像构建；碰 Clash；打印或提交密钥

---

## 十二、批准记录

本计划由助手根据 Phase 4/5 候选表、共识简报、stack-blueprint 与 Phase 5 收口状态起草。**D1–D16 按推荐默认值锁定**，供 OpenCode Build（orchestrator-only）直接执行，无需再向用户索取 phase6-plan/build-prompt。

Build 启动顺序：**P1–P5 → 基线复核 →（H）→ 冻结 → 实现 → 全门 → 提交 → 一次 push → CI 双轮全绿**。

