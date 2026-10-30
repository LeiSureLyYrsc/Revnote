# Revnote Phase 5 实施计划

> **用户已批准 D1–D19 推荐方案。**
>
> 状态：Plan 输出（未改代码、未提交、未推送）。Build 期基线以实际 `4abdcb0` 为准（起草时为 `3509b7c`）。
> MinIO release 由 `RELEASE.2025-09-06T17-38-46Z` 改为 `RELEASE.2025-09-07T16-13-09Z`（09-06 无 Windows 资产，
> 而本机真实验收必须使用官方 Windows 二进制，见 [phase5-validation.md](phase5-validation.md)）。

> 状态：Plan 输出（未改代码、未提交、未推送）。用户已授权 D1…D19 全部按推荐默认值自动锁定；文中 **取舍点** 仅标注取舍，不再等待确认。

---

## 一、基线核对与前置条件

### 1.1 已阅读材料

`README.md`、`docs/consensus-brief.md`、`docs/stack-blueprint.md`、`docs/build-conventions.md`、`docs/plan-phase2.md`、`docs/plan-phase3.md`、`docs/phase3-sync-api-spec.md`、`docs/plan-phase4.md`（第二节候选范围与依赖顺序）、`docs/phase4-validation.md`、`.slim/deepwork/phase4-build/progress.md`（已知问题）。

并核对了相关代码：`apps/api/src/revnote_api/{config,kernel/commands,services/{sync_read,sync_write,sync_push_adapter},models/*,api/{router,errors,dependencies,middleware}}`、`apps/api/tests/**`、`apps/api/scripts/{run_web_e2e,check_test_reports}.py`、`apps/web/{src,e2e,tests,scripts}`、`.github/workflows/ci.yml`、`docker-compose.yml`、`.env.example`、`apps/api/Dockerfile`。

### 1.2 基线事实（必须在 Build 第一步复核，不能沿用旧结论）

| 项 | 实际观测 | 对 Phase 5 的影响 |
|---|---|---|
| 工作区 HEAD | `3509b7c docs: record the phase4 ci result and preview limits`，`origin/main` 同为 `3509b7c` | 与任务书写的 `b92f70c` 不同：其上多了一个 docs-only 提交 |
| `b92f70c` 的 CI | run `40974724185`：api / web / web-e2e / image 全 success（曾连绿两轮） | 任务书背景成立 |
| `3509b7c` 的 CI | run `40975758621`：api success、web success、**web-e2e failure**、image skipped。受限诊断：`Browser failure: e2e/durability.spec.ts:122 assertion=e2e/fixtures.ts:186 labels=single_writer_takeover` | **docs-only 提交不可能改变测试行为 ⇒ 已知 flake 未消除**；`fixtures.ts:186` 是 `openNote()` 里的 `expect(getByTestId('note-source')).toBeVisible()` |
| progress.md 已知问题 | `manual_conflict` 曾被记录 FLAKY；`single_writer_takeover` 接管后工作区按 `ownershipGeneration` 重挂、选中笔记重置，与断言竞争 | Phase 5 的第 1 个提交必须先消除该 flake，否则新工作全部建在红基线上 |
| 本机 | 无 `docker`；`D:\tools` 不存在；PATH 无 `minio`/`mc`；`gh` 可用 | 附件真实验收必须走「仓库外 MinIO 可执行文件 + 临时 PG 实例」，CI 才用容器 |
| 既有约束代码事实 | `test_kernel_boundaries.FORBIDDEN_MODULES` 含 `minio`（kernel 与 `services/` 均禁）；`test_config` 断言 `Settings` 中不得存在字段名含 `secret`；`test_app_contract.EXPECTED_PATHS`、`test_api_contract.ERROR_SPECS`、`test_models_metadata`、`test_migrations` 用「精确集合」断言 schema/路由/错误码 | 附件设计必须**绕开内核、绕开 `Settings` 的 secret 命名、绕开 `services/` 直接依赖存储客户端**，并把精确集合测试按**加法**更新（不是放宽） |

### 1.3 前置条件（Build 前必须满足，缺一即停）

1. **P1 — 对象存储可执行文件（本机）**：因无 Docker，本地真实 MinIO 需要服务端程序。**不得由 OpenCode 自行下载或运行**。由助手完成安全扫描后放到 `D:\tools\`，并在 `docs/phase5-validation.md` 记录：文件名、版本号、SHA-256、来源（与 `docker-compose.yml` 固定的官方 release 标签一致）、扫描结论。Build 使用前**重新计算并比对 SHA-256**，不一致即拒绝启动。可选 `mc.exe`（仅用于交叉核验桶策略），同样走该流程。
2. **P2 — 仓库外临时 PostgreSQL**（沿用 Phase 2/3/4 控制器）：业务库、迁移库、以及 Web E2E 第三个一次性库彼此独立；`REQUIRE_POSTGRES_TESTS=1`；每批 `finally` 停止。
3. **P3 — 真实浏览器**：`pnpm --filter @revnote/web exec playwright install chromium`（本地验收用）。
4. **P4 — CI 官方镜像可拉取性**：`quay.io/minio/minio:<与 compose 相同的固定标签>`。若 runner 无法拉取，**停止并报告，不得替换第三方重建镜像或其他 S3 产品**（继承 README 第 180 行与 compose 注释的硬约束）。
5. **P5 — Git/gh/uv/pnpm 可执行文件定位**：`git`、`gh`、`uv`、`pnpm` 的绝对路径先确认；分支为 `main`、工作区仅含用户提示文件未跟踪；不覆盖用户修改，不提交根目录 `phase*.md` / `plan-prompt.md`。

---

## 二、候选范围比较与推荐

### 2.1 比较

| 候选 | 价值 | 主要依赖与新增风险 | 依赖顺序判定 |
|---|---|---|---|
| **B. MinIO/S3 附件** | 补齐笔记产品最缺的能力（图片/文件）；Phase 4 计划已定为下一独立阶段 | 不可变对象、上传/确认状态机、**真实 SHA-256** 校验、预签名覆盖风险、引用关系、宽限期 GC；本机无 Docker | **Phase 5 主范围**（在 H 之后） |
| **H. 稳定性前置（其他必要项）** | 让阶段建在绿基线上：`single_writer_takeover` 已知 flake | 只改客户端接管时序与 helper 就绪等待；不得加 retries/skip/放宽断言 | **Phase 5 第 1 步（B 的硬前置）** |
| **C. Joplin 兼容桥** | 接入现有 Joplin 客户端 | Server 协议 delta/locks/资源语义、ID 映射、客户端兼容验收；**完整兼容依赖附件** | B 之后（Phase 6+） |
| **F. 公网运行加固** | TLS、真实限流、备份恢复、同源部署 | 需要真实部署目标与反向代理；真实限流需共享状态（Redis 或等价），当前明确无 | B 之后独立阶段；**本阶段只保留受控预览声明** |
| **E. snapshot 分页/大工作区 bootstrap** | 解除 10k 对象/16MiB 限制 | 跨请求一致性、快照寿命、协议兼容；与附件无依赖关系 | 可独立后置；当前 409 + 显式暂停已是安全出口 |
| **D. 自动三方合并** | 减少人工冲突 | 客户端必须持有可信 base；Markdown/AST 合并正确性、结构冲突、恢复体验 | 最后（依赖 Phase 4 恢复记录成熟 + 更长观察期） |
| **I. 参考 CLI** | 第二个官方消费者，验证 API 可用性 | 新增客户端与维护成本；Web E2E 已真实消费 API | 未选入本阶段 |
| **J. 附件专属可观测/运维脚本** | 让「宽限期 GC」可执行 | 需要真实存储 + 真 PG 证明只删 pending | **随 B 必做，非可选** |

### 2.2 推荐结论

> **Phase 5 = H（稳定性前置）+ B（附件及其客户端/验收闭环）。**
> 本阶段交付：*用户可以把文件真正上传到对象存储，服务端以真实 SHA-256 校验后把附件标记为 ready，正文以标准 Markdown 引用它；另一个客户端 pull 到该笔记后能下载到**字节一致**的同一份内容；非成员、超限、非 ready、哈希不符都被明确拒绝，既不多写也不吞掉正文；同步内核八命令、事件流、pull/snapshot、Phase 4 客户端行为与 E2E 门禁全部不削弱。*

### 2.3 依赖顺序

```text
H 稳定性前置（接管→就绪→打开笔记的确定性）
        ↓
接口/契约冻结（OpenAPI 增量、存储端口、错误码、能力端点）
        ↓
迁移 0004 + 模型 + 存储适配器（真 MinIO 探针）∥ Web 组件骨架 ∥ E 验收设施
        ↓
附件服务 + 路由 + 请求上限 + 运维脚本（真 PG + 真 MinIO 并发/授权/GC）
        ↓
Oracle 风险闸口（内核不受影响、授权、单赢家、不吞正文）
        ↓
Web 接入（上传/引用/预览/离线禁用）→ 全链路 E2E（20 标签）
        ↓
本地全量门禁 → 六个提交 → 一次 push → CI 绿（含新 job 与 image 依赖）
```

**明确不选**：C、D、E、F、I 不在本阶段；理由见上表与 §12。

---

## 三、目标与验收

### 3.1 本阶段实现

1. **附件存储（服务端）**
   - 新增 `attachments` 表（迁移 `0004`）；**不是**内核实体：不写 `change_events`、不分配 `seq`、不进 `pull`/`snapshot`。
   - 两阶段状态机 `pending → ready → deleted`，上传与校验在**事务外**完成，只有短事务更新状态。
   - 上传经 **API 代理流式写入**对象存储（不使用预签名 URL）；完成后**回读复算 SHA-256** 与写入时摘要比对。
   - 对象键不可变：`ws/{workspace_id}/att/{attachment_id}/{upload_nonce}`；同一附件的并发上传由 nonce + 行级 CAS 决出**唯一赢家**。
   - 宽限期 GC：**仅**清理过期 `pending`（运维脚本，非后台调度），`ready` 永不自动删除。

2. **协议（API v1 增量）**
   - `GET /api/v1/capabilities`：声明附件是否启用、单文件上限、允许的 MIME。
   - `POST /api/v1/workspaces/{workspace_id}/attachments`：创建 `pending`。
   - `PUT  /api/v1/workspaces/{workspace_id}/attachments/{attachment_id}/content`：流式上传原始字节。
   - `GET  /api/v1/attachments/{attachment_id}`：元数据（扁平路径，正文引用可直接使用）。
   - `GET  /api/v1/attachments/{attachment_id}/content`：流式下载，`Cache-Control: no-store`、`X-Content-Type-Options: nosniff`。
   - **八类 push 命令、六个 push 状态、pull/snapshot/tree/note 语义、错误信封结构完全不变**；错误码只做加法。

3. **Web 客户端**
   - 能力门控：`capabilities` 缺失/关闭时隐藏附件入口并说明原因（旧服务上必须优雅降级）。
   - 选择文件 → 上传（有上限与进度/错误状态）→ **ready 之后**才把标准 Markdown 引用插入正文并触发既有冻结队列。
   - 预览用**带 Bearer 的 `fetch` + `blob:` URL**（`<img>` 无法携带 Authorization），组件卸载即 `revokeObjectURL`；不把任何二进制写入 IndexedDB。
   - 离线/无存储/只读标签页：上传入口显式禁用并给出原因，正文与既有队列不受影响。

4. **稳定性前置（H）**
   - 修复 `single_writer_takeover`：接管流程进入「正在恢复工作区」状态，就绪信号出现后才允许打开笔记；helper 等待**就绪**而非固定延时。**不加 retries、不加 skip、不放宽任何承重断言。**

### 3.2 必须成立的验收结果

| 目标 | 验收结果（真实环境） |
|---|---|
| 附件不进内核 | 上传并 ready 后：`change_events` 新增 0 行、`workspaces.last_seq` 不变、`pull`/`snapshot` 响应体不变 |
| 真实校验 | 声明哈希/实际字节不一致 → 拒绝（无 ready 行）；ready 后回读摘要与服务端记录一致；**不依赖 ETag/HEAD** |
| 不可变 + 单赢家 | 同一附件两个并发 PUT → 恰好一个 ready、一个 409；存储中只保留赢家对象 |
| 授权 | 非成员/不存在 workspace 的元数据与内容读取统一 404；token 不出现在 URL、日志、报告 |
| 收敛 | 上传→ready→push 笔记更新→另一浏览器上下文 pull 到引用→下载字节与源文件逐字节一致 |
| 宽限期 GC | 真实 PG + 真 MinIO：过期 pending 的对象与行被删；`ready` 对象与行不受影响 |
| 不吞正文 | 上传失败/超限/离线时正文、草稿、既有命令队列均保留，且 UI 明确说明原因 |
| 客户端降级 | 对端服务无 `/capabilities` 时，Phase 5 客户端隐藏入口且不报错，Phase 4 行为不变 |
| 回归保持 | 既有七组 API JUnit 报告名称与数量守卫、Phase 4 的 16 个 E2E 标签、`check:web-reports`/`check:api-reports` 语义不削弱 |
| 发布完成 | 本地全部通过后一次 push；CI 五 job 全绿，`image` 依赖全部检查 |

---

## 四、决策锁定表 D1–D19（全部按推荐默认值锁定，标注取舍点）

| ID | 决策（推荐默认值） | 取舍点 |
|---|---|---|
| **D1** | 本阶段范围 = H + B（附件及其客户端与验收闭环）；C/D/E/F/I 后置 | 与「先做公网加固」的另一条路线相比，本阶段不宣称公网就绪 |
| **D2** | 约束锁定：Phase 2 内核八命令与 `change_events.entity_type ∈ {folder,note}`、Phase 3 全部端点响应与错误语义、Phase 4 客户端行为与 16 个 E2E 标签**只增不改** | — |
| **D3** | **附件不是内核实体**：不写事件、不占 seq、不进 pull/snapshot；发现路径 = 笔记正文引用 + 扁平附件端点 | 另一方案是扩 `entity_type='attachment'`，要改内核词汇、既有 CHECK 与大量精确断言，风险显著更高 |
| **D4** | 上传/下载**经 API 代理流式转发**，浏览器不直连 MinIO，**不使用预签名 URL** | 代价是 API 承担带宽/CPU；收益是消灭「预签名 PUT 可覆盖」「桶 CORS」「内外地址分歧」三类风险 |
| **D5** | 两阶段 `pending→ready→deleted`；对象键含 `upload_nonce`，状态推进用 `UPDATE ... WHERE upload_nonce=:mine AND state='pending'` 决胜 | 单步上传更简单，但失去宽限期 GC、并发保护与「不可变」证据 |
| **D6** | 真实 SHA-256：写入时流式计算 + 完成后**回读复算**比对；**不信任** ETag/HEAD/客户端声明 | 回读带来双倍带宽；用 `ATTACHMENT_MAX_BYTES` 与 64KiB 分块把成本限定在常数倍 |
| **D7** | 正文引用使用**标准 Markdown 相对 URL**：`![alt](/api/v1/attachments/{id}/content)`；正文是唯一引用真相，**不建 `note_id` 列** | 自定义 scheme 更短，但与通用 Markdown 渲染/Joplin 兼容方向相悖 |
| **D8** | 本阶段**不去重、不提供附件删除、不回收未引用 ready 对象**；只有 pending 过期 GC | 对象可能长期占用；换来引用一致性与零误删 |
| **D9** | 读取需成员身份；元数据与内容均 `no-store`；MIME 白名单（`image/png,image/jpeg,image/webp,image/gif,application/pdf,text/plain`），**明确拒绝 `image/svg+xml`** | 白名单较窄；安全优先 |
| **D10** | 迁移 `0004` 只新增 `attachments` 与其索引，**不改十张既有表**；既有精确表集/约束集/索引集断言按加法更新 | 更新精确集合断言属计划内编辑，必须显式列出新增项 |
| **D11** | 存储凭据放在**独立** `ObjectStorageSettings`（`repr` 掩码），`Settings` 不新增任何含 `secret` 的字段名 | 保持 `test_config` 的 phase-3 不变量测试原样通过 |
| **D12** | `ATTACHMENTS_ENABLED` 默认 `false`；新增 `GET /api/v1/capabilities`；Web 依能力显示/隐藏入口 | 默认关闭 = 需要显式开启；避免未配置对象存储的部署「看起来支持」 |
| **D13** | 本地无 Docker：MinIO 由 `D:\tools\minio.exe`（P1）以 runner **own 子进程** + 临时数据目录 + 每 run 唯一 bucket 启动；CI 用**官方镜像 service 容器**（不设 health 选项，改用适配器轮询就绪） | 两条路径都是真存储；放弃任何内存假实现 |
| **D14** | 测试分层：单测可用存储端口替身证明状态机；**真实存储组必须真 MinIO**，`REQUIRE_OBJECT_STORAGE_TESTS=1`，缺失即失败，**skip 不算通过** | — |
| **D15** | CI 新增独立 job `api-attachments`（PG + MinIO service），`image.needs` 追加该 job；新 JUnit 组进入 `check_test_reports.py`，既有七组不动 | job 数增加；发布门禁更严，不是更松 |
| **D16** | Web **不在 IndexedDB 排附件**：离线时上传入口禁用并说明；不静默排队二进制 | 更「完整」的做法是持久化待传 blob，但配额/身份隔离/明文缓存风险大 |
| **D17** | 稳定性前置先修 `single_writer_takeover`；`retries: 0`、`forbidOnly`、断言语义不变；以**重复运行**（focused ×5 + full ×2，独立新库）作为证据 | 修复可能触及产品接管 UX（增加就绪态），属允许的加法 |
| **D18** | 附件可观测只记录：计数、字节数、耗时、结果码；**不记录**文件名、MIME 原值、正文、字节内容、token | 排障信息较少 |
| **D19** | 存储客户端：优先官方 `minio` Python SDK（sync，全部调用 `asyncio.to_thread` + 显式超时）；由 R 车道按 `uv.lock` 兼容性定稿；若冲突则退 `boto3`，**二者不得同时引入** | 单一 SDK 便于审计；取舍点在于 botocore 版本锁定风险 |

---

## 五、模块与文件边界（拟新增/修改，非已实现）

| 边界 | 路径 | 职责 |
|---|---|---|
| 模型 | `apps/api/src/revnote_api/models/attachment.py` | `attachments` 行、状态常量；`repr` 不含文件名/哈希原文 |
| 模型注册 | `models/__init__.py`（**Orchestrator 独占**） | 十表 → 十一表；`MODEL_TABLES` 加法更新 |
| 迁移 | `migrations/versions/0004_attachments.py` | 仅新增表与索引；升降级可逆；不改既有表 |
| 存储端口 | `apps/api/src/revnote_api/storage/ports.py` | `BlobStore` Protocol + `BlobStat`；**纯类型，无客户端依赖** |
| 存储实现 | `apps/api/src/revnote_api/storage/s3.py` | 真实 S3/MinIO 客户端；`put/open/stat/delete/digest`；超时、分块、异常到安全码的映射 |
| 存储配置 | `apps/api/src/revnote_api/storage/settings.py` | `ObjectStorageSettings`（endpoint/bucket/凭据/超时），`repr` 掩码 |
| 存储工具 | `apps/api/src/revnote_api/storage/bootstrap.py` | `ensure_bucket` / `purge_prefix`：**仅**运维与测试用，不暴露 HTTP |
| 附件服务 | `apps/api/src/revnote_api/services/attachments.py` | 状态机、单赢家 CAS、GC 判定；只接收 `BlobStore` 端口（**不 import 客户端，不 import `db.engine`/`db.session`**） |
| 附件路由 | `api/routes/attachments.py`、`api/schemas/attachments.py` | 创建/上传/元数据/内容/能力；流式 I/O；`no-store`；安全错误码 |
| 横切（**Orchestrator 独占**） | `api/router.py`、`api/errors.py`、`api/middleware/request_limits.py`、`config.py` | 路由注册、错误码加法、上传路径字节上限、非敏感配置项 |
| 运维脚本 | `apps/api/src/revnote_api/tools/attachments_gc.py` | `python -m revnote_api.tools.attachments_gc`：只删过期 pending（行 + 对象） |
| API 测试 | `apps/api/tests/unit/test_attachments_*.py`、`tests/api/test_attachments*.py`、`tests/integration/test_migrations.py`（加法） | 状态机单测、真实 PG+MinIO HTTP 组、并发单赢家、授权、GC、迁移往返 |
| 测试支撑 | `apps/api/tests/pg_support/object_storage.py` | 真实 MinIO 环境契约：`REQUIRE_OBJECT_STORAGE_TESTS`、endpoint、bucket、掩码诊断 |
| Web 客户端 | `apps/web/src/api/attachments.ts`、`src/workspace/attachmentPorts.ts`、`src/components/attachments/**`、`src/components/editor/EditorPane.tsx` | 能力探测、上传、引用插入、blob 预览与释放、离线禁用 |
| Web 测试 | `apps/web/tests/unit/attachments/**`、`tests/components/attachments*.test.tsx` | 解码、门控、失败分支、objectURL 释放 |
| E2E | `apps/web/e2e/attachments.spec.ts`、`e2e/fixtures.ts`（加法 helper）、`scripts/check-test-reports.ts`（新标签） | 4 个新标签的真实浏览器证据 |
| E2E 生命周期 | `apps/api/scripts/run_web_e2e.py` | 新增 `owned` / `external` 两种 MinIO 模式；bucket 每 run 唯一；finally 清理 |
| 报告守卫 | `apps/api/scripts/check_test_reports.py` | 新增 `api-attachments.xml` 组与承重用例名 |
| CI | `.github/workflows/ci.yml` | 新 job `api-attachments`（PG + MinIO service），`image.needs` 追加 |
| 依赖/清单 | 根与 `apps/api` 的 `pyproject.toml`、`uv.lock`、`.env.example`、`package.json` | 单一锁文件；新配置名与文档同步 |
| 文档 | `docs/phase5-attachments-api.md`、`docs/phase5-validation.md`、`README.md` | 协议附录、实际运行证据、限制 |

**禁止的越界**：不修改 `kernel/**`（八命令、fingerprint、results、errors 语义）、不改 `services/sync_{write,read,push_adapter,repository}.py` 的行为、不改既有端点响应体、不改 `apps/web/src/sync/**` 的 ACK/游标/冲突语义、不给生产加 failpoint 或调试端点。

---

## 六、数据与协议变更

### 6.1 数据模型（迁移 `0004_attachments`，仅加法）

```text
attachments
  id              UUID  PK                       -- 应用侧 uuid7
  workspace_id    UUID  NOT NULL FK workspaces(id) ON DELETE RESTRICT
  state           TEXT  NOT NULL CHECK IN ('pending','ready','deleted')
  mime            TEXT  NOT NULL CHECK octet_length(mime) BETWEEN 3 AND 127
  byte_size       BIGINT          CHECK (byte_size IS NULL OR byte_size > 0)
  content_hash    BYTEA           CHECK (content_hash IS NULL OR octet_length = 32)
  storage_key     TEXT  NOT NULL  CHECK octet_length(storage_key) BETWEEN 8 AND 512
  upload_nonce    UUID  NOT NULL                 -- 每次上传尝试唯一，决定对象键
  file_name       TEXT                           -- 用户内容，永不进日志/repr
  created_at / upload_started_at / ready_at / deleted_at / delete_reason
约束：UNIQUE(storage_key)；状态-字段一致性 CHECK ((state='ready') = (content_hash IS NOT NULL))；
索引：(workspace_id, state)、(workspace_id, created_at)（GC 扫描）、UNIQUE(storage_key)；
刻意不含 rev / note_id / seq：附件不是内核实体（D3、D7）。
```

**不改动**：`change_events` 的 `entity_type_known` / `op_known` CHECK、十张既有表的列、约束、索引；`alembic check` 必须仍无差异。

### 6.2 端点（API v1 增量，全部新增）

| 方法/路径 | 输入 | 成功 | 关键失败 |
|---|---|---|---|
| `GET /capabilities` | — | `200 {attachments:{enabled,max_bytes,accepted_mime[]}}` | 未启用时 `enabled:false`（仍 200） |
| `POST /workspaces/{ws}/attachments` | `{mime, byte_size?, file_name?}` ≤16 KiB | `201 {attachment_id,state:"pending",upload:{method:"PUT",url},expires_at}` | 400 非法 MIME/超限；503 `attachment_storage_unavailable`；403/404 由成员校验决定 |
| `PUT /workspaces/{ws}/attachments/{id}/content` | 原始字节，`Content-Type` 必须等于声明 MIME；`X-Content-SHA256`（可选，base64/hex） | `200 {attachment_id,state:"ready",content_hash,byte_size}` | 409 `attachment_already_ready`（提前短路，不读正文）；409 `attachment_hash_mismatch`；413 `payload_too_large`（中间件）；409 `attachment_expired`；404 |
| `GET /attachments/{id}` | — | `200 {id,state,mime,byte_size,content_hash,created_at,ready_at}` | 404（不存在/非成员/未 ready 统一） |
| `GET /attachments/{id}/content` | —，可选 `Range`（**本阶段不支持，返回 200 全量**） | `200` 字节流 | 404；`no-store`；`nosniff` |

**错误码加法**（`ERROR_SPECS` 精确集合断言按加法更新）：`attachment_storage_unavailable`(503, retryable)、`attachment_already_ready`(409)、`attachment_hash_mismatch`(409)、`attachment_expired`(409)、`attachment_too_large`(413，仅业务层判断，与中间件 413 文案分离)、`attachment_unsupported_type`(400)。

### 6.3 兼容性与版本策略

- **仍在 `/api/v1`**：删/改都没有，只新增端点、字段与错误码，属向后兼容的加法；因此**不引入 v2**。
- **旧客户端（Phase 4 Web）在新服务上**：不会调用新端点，行为不变（回归由既有 16 个 E2E 标签与七组 API 报告证明）。
- **新客户端在旧服务上**：`GET /capabilities` 404 ⇒ 客户端按「不支持附件」降级并隐藏入口；必须有单测与该降级的真实浏览器用例。
- **数据库前向兼容**：旧服务代码在新 schema 上可运行（新表无人引用）；`0004` 降级只删新表与其索引，不触碰既有表与数据。
- **文档**：`docs/phase3-sync-api-spec.md` 保持锁定内容不变，新增 `docs/phase5-attachments-api.md` 作为附录级补充（避免改写已锁定的 Phase 3 契约）。

### 6.4 状态机与时序（服务端）

```text
POST 创建          短事务：INSERT attachment(state=pending, upload_nonce=新值, expires_at)
PUT  上传          ① 前置：状态非 pending → 提前 409（不读正文）
                   ② 事务外：流式写 ws/{ws}/att/{id}/{nonce}，边写边算 sha256 与长度，超限即中止
                   ③ 事务外：回读该键复算 sha256 与长度（真实校验，不信任 ETag/HEAD）
                   ④ 短事务：UPDATE ... WHERE id=? AND workspace_id=? AND upload_nonce=? AND state='pending'
                            SET state='ready', content_hash=?, byte_size=?, ready_at=now()
                       影响 0 行 ⇒ 自己输掉竞争/已过期 ⇒ 删除自己刚写的键 → 409
                   ⑤ 校验失败 ⇒ 删除键 → 409 attachment_hash_mismatch（不留下 ready 行）
GET  下载          解析 attachment → 取 workspace → 要求成员；state!='ready' → 404；流式返回
GC   运维脚本      删除 created_at < now()-grace 且 state='pending' 的行与其对象；ready 不动
```

**事务外上传**（继承共识 P0 #7）：任何代码路径都不得在持有 PG 连接/事务时做对象存储 I/O。

---

## 七、测试策略与证据责任

### 7.1 承重矩阵

| 承重主张 | 责任车道 | 最小证据 |
|---|---|---|
| 附件不推进内核 | C/E | 真实 PG：上传+ready 后 `change_events` 计数不变、`last_seq` 不变；`pull`/`snapshot` 响应逐字段不变 |
| 真实 SHA-256 | B/C | 真 MinIO：声明哈希不符 → 拒绝且无 ready 行；ready 后回读摘要一致；测试用**直接写入对象的测试专用路径**构造不一致 |
| 单赢家 + 不可变 | C | 两个并发 PUT 同一附件：1 个 200、1 个 409；桶中只有赢家键；已 ready 再 PUT → 409 且对象未变 |
| 丢响应不重复写 | C | 真实服务端已提交后丢弃响应 → 客户端重放（同附件 id）→ 仍恰好 1 个 ready 行、1 个对象 |
| 授权隔离 | C/D | 非成员：元数据、内容、上传均 404；token 不出现在 URL/日志/JUnit/Playwright 报告 |
| 宽限期 GC 只删 pending | C | 真 PG+MinIO：过期 pending 行与对象消失；ready 行与对象保留 |
| 引用收敛 | D/E | 上传→ready→push 笔记→第二浏览器上下文 pull→下载字节等于源文件 |
| 客户端不吞正文 | D/E | 离线/超限/服务不可用：正文与命令队列保留，UI 说明原因，无二进制进 IndexedDB |
| 接管确定性（H） | A/E | focused ×5 + full ×2（各自全新一次性库），`single_writer_takeover` 全过，无 retry/skip |
| 既有门禁不削弱 | E | 七组 API 报告 + 新 `api-attachments` 组 + 20 个 E2E 标签 + 两报告守卫全绿 |

### 7.2 分层

- **单测（无外部服务）**：状态机转移表、错误码映射、上传上限判定、`BlobStore` 端口替身、MIME 白名单、`ObjectStorageSettings` 掩码、能力端点契约、客户端解码与门控、objectURL 释放。
- **真实 PostgreSQL（`REQUIRE_POSTGRES_TESTS=1`）**：`0004` 升降级往返、`alembic check`、精确表/约束/索引集合（加法）、状态机 CAS、GC 选行、与既有内核测试并跑不回归。
- **真实 MinIO（`REQUIRE_OBJECT_STORAGE_TESTS=1`）**：put/digest/open/delete、桶私有、回读校验、幂等与单赢家、GC 真删除、`storage_key` 不可变。
- **HTTP 组**：新增 `tests/api/test_attachments*.py`（普通组）与并发用例（放进既有 `api_concurrency` 组或新组，二者都保留 no-skip 守卫）。
- **真实浏览器 E2E**：见 7.3；**不得**用 fake 存储替代。

### 7.3 E2E 新标签（16 → 20，既有 16 个全部保留且语义不变）

| 标签 | 责任 | Spec |
|---|---|---|
| `attachment_upload_insert` | 真实文件上传→ready→正文插入标准 Markdown→本地保存→同步→刷新→预览拉取真实字节 | `attachments.spec.ts` |
| `attachment_cross_client_read` | 第二个 `BrowserContext` pull 到引用并下载，字节逐字节一致 | `attachments.spec.ts` |
| `attachment_authorization` | 另一账号 404；超限 413；MIME 不在白名单被拒；均不改正文 | `attachments.spec.ts` |
| `attachment_offline_gate` | 断网/无存储：入口禁用并说明，无二进制入 IndexedDB，正文保留 | `attachments.spec.ts` |

报告守卫新增这 4 个标签为**必需**，并继续要求 16 个旧标签；`scope=focused` 的证据不能替代 full。

### 7.4 真实存储生命周期

- **本地**：runner 以 own 子进程启动 `D:\tools\minio.exe server <临时目录> --address 127.0.0.1:<port> --console-address 127.0.0.1:<port>`，环境变量注入随机凭据；就绪轮询用**项目自己的适配器**；创建 `revnote-att-<runid>` 私有桶；`finally`：删对象→删桶→停进程→确认端口释放。PG 仍由外部控制器拥有。
- **CI**：`services: minio` 使用与 compose 相同的固定官方标签 + 端口映射，**不设 `--health-*` 选项**（服务镜像未验证内置探针），由 step 用适配器轮询就绪并建桶；只读 runner 通过 `WEB_E2E_MINIO_ENDPOINT`/`ATTACHMENT_*` 以外的方式注入，浏览器进程**永不**拿到存储凭据。
- **绝不用内存实现冒充真实存储验收**；单测替身只在 unit 层，且报告守卫把真实组列为必需。

### 7.5 证据安全与命名

- 报告与诊断只含：标签、`source:line`、`assertion`、`stage`、用例哈希、计数、状态码。**不含** message/stack/正文/文件名/凭据/字节。
- 诊断格式沿用 Phase 4 的受限通道；附件失败只追加**固定码**（`hash_mismatch`、`already_ready`、`storage_unavailable`、`too_large`）。
- 命名统一用 `secret` / `private` 替代敏感词（例如 `secret_access`、`private_bucket`）；测试名、注释、提示同一规则。
- 不录制 trace/HAR/storageState/视频/截图；`reports/` 已在 `.gitignore`。

---

## 八、SubAgent 车道与串行点

> 模型约束：**Orchestrator 与 Plan 使用 `murasameai-vip/cn/deepseek-v4.1-flash#max`；派任何 SubAgent 一律不传 `model` 参数**（走插件默认：explorer/fixer/oracle/librarian → `murasameai-vip/cn/deepseek-v4.1-flash`；designer → `murasameai-vip/gemini-3.8-flash-high`）。**不得使用 `gpt-6.1-sol`。** 子代理不自行提交、不推送、不启动不受控 PG/MinIO、不下载任何可执行文件。

| 车道 | Agent | 独占文件所有权 | 验证责任 |
|---|---|---|---|
| **R：前置核查** | explorer | 只读；仅可写 `docs/phase5-validation.md` 的「前置核查」小节 | 核对 `minio` SDK 与 `uv.lock` 兼容性、官方镜像标签可获取性、`D:\tools` 二进制哈希、既有报告守卫/精确集合断言清单、当前 flake 复现步骤；输出差异清单 |
| **A：稳定性前置（H）** | fixer | `apps/web/src/App.tsx`、`src/workspace/WorkspaceScreen.tsx`、`src/components/layout/AppLayout.tsx`、`e2e/fixtures.ts`、`e2e/durability.spec.ts`（仅等待/就绪）、`tests/components/**`（相关） | 接管→就绪→打开笔记的确定性；focused ×5 + full ×2；不加 retry/skip，不放宽断言 |
| **B：存储层** | fixer | `src/revnote_api/storage/**`、`tests/unit/test_storage_*.py`、`tests/pg_support/object_storage.py`、`src/revnote_api/tools/attachments_gc.py`（脚本骨架） | 真实 MinIO put/digest/delete、超时、错误映射、桶私有、GC 选行 |
| **C：附件域与协议** | fixer | `models/attachment.py`、`services/attachments.py`、`api/routes/attachments.py`、`api/schemas/attachments.py`、`migrations/versions/0004_attachments.py`、`tests/api/test_attachments*.py`、`tests/unit/test_attachments_*.py` | 状态机、CAS 单赢家、授权、GC、迁移往返；不得改内核与既有无状态语义 |
| **D：Web 接入** | designer | `src/api/attachments.ts`、`src/workspace/attachmentPorts.ts`、`src/components/attachments/**`、`src/components/editor/EditorPane.tsx`、`tests/unit/attachments/**`、`tests/components/attachments*.test.tsx` | 能力门控、上传/失败/离线 UI、Markdown 引用不破坏原文、blob 预览释放、可访问性与 390px |
| **E：验收设施与门禁** | fixer | `apps/api/scripts/run_web_e2e.py`、`apps/api/scripts/check_test_reports.py`、`apps/web/scripts/check-test-reports.ts`、`apps/web/e2e/attachments.spec.ts`、`.github/workflows/ci.yml`、`apps/web/e2e/README.md` | 两模式 MinIO 生命周期、报告守卫、no-skip、CI job 与 `image.needs`、20 标签覆盖 |
| **F：独立审查** | oracle | 只读 | 两道闸口：①设计闸口（冻结前）②风险闸口（合并前）：内核零影响、事务外 I/O、授权、单赢家、不吞正文、证据安全 |
| **G：文档与规格一致性** | librarian | `docs/phase5-attachments-api.md`、`README.md`、`docs/phase5-validation.md`（除 R 小节） | 协议附录、限制、与 Phase 3/4 文档不冲突；若该 agent 不可用，由 Orchestrator 兼管 |
| **Orchestrator** | 主调度 | `config.py`、`.env.example`、`api/router.py`、`api/errors.py`、`api/middleware/request_limits.py`、`models/__init__.py`、根与 api 的清单/锁文件、`package.json`、CI 最终合并、docs 归档 | 接口冻结、共享文件合并、最终全量门禁、提交与发布、逐文件复核（不采信子代理总结） |

### 串行点

```text
R 核查 → Orchestrator 冻结接口（OpenAPI 增量、BlobStore 端口、错误码、能力端点、测试脚手架）
                                  ↓
        A(H) ∥ B(存储骨架) ∥ C(迁移+模型) ∥ D(组件骨架) ∥ E(设施骨架)
                                  ↓
                     B + C 真实集成（真 PG + 真 MinIO）→ Oracle 设计/风险闸口
                                  ↓
                     D 业务接入（依赖 C 的稳定契约）→ E 全链路（20 标签）
                                  ↓
              Orchestrator 复核改动文件 → 最终门禁 → 六提交 → 一次 push → CI 绿
```

规则：共享文件唯一 owner；其他车道提交「修改清单」由 Orchestrator 落地；C 可先用端口替身跑通状态机，但真实集成必须等 B 稳定；D 不写同步算法，C 不改 UI；子代理总结不是验收证据。

---

## 九、六个 Conventional Commits 与逐步验收

> 所有命令在 `D:\Github\Revnote` 执行。PowerShell 若拦截 `pnpm.ps1`，使用 `pnpm.cmd`，**不修改执行策略**。PG 由仓库外控制器提供，`finally` 停止；MinIO 本地由 `D:\tools\minio.exe`（P1）提供。

### Commit 1 — `fix(web): settle writer takeover on a ready workspace`

内容：H 稳定性前置。接管后进入「正在恢复工作区」状态，就绪信号出现后才允许打开笔记；helper 等待就绪（有界），**不加 retries、不加 skip、不放宽任何承重断言**；补一条组件测试证明接管后状态机顺序。

```powershell
pnpm install --frozen-lockfile
pnpm lint:web
pnpm typecheck:web
pnpm test:web
pnpm build:web
# 每个 focused 循环前由外部控制器准备全新一次性库，且删除旧报告
foreach ($i in 1..5) {
  pnpm test:e2e -- --grep "single_writer_takeover"
  if ($LASTEXITCODE -ne 0) { throw "focused run $i failed" }
}
# 两次全量（各自全新库、各自删报告），以退出码判定
pnpm test:e2e ; pnpm check:web-reports
pnpm test:e2e ; pnpm check:web-reports
```

### Commit 2 — `feat(api): add the attachment schema without touching the sync kernel`

内容：迁移 `0004_attachments`、`models/attachment.py`、`models/__init__.py` 注册、精确集合断言按加法更新、迁移往返与数据保全测试、`alembic check`。

```powershell
uv sync --locked
uv run ruff check apps/api
uv run ruff format --check apps/api
uvx --from ty==0.0.94 ty check apps/api/src/revnote_api/models apps/api/src/revnote_api/config.py
uv run pytest apps/api/tests/unit -q
$env:REQUIRE_POSTGRES_TESTS = '1'
uv run pytest apps/api/tests/integration/test_migrations.py -v --junitxml=api-migrations.xml
uv run alembic -c apps/api/alembic.ini upgrade head ; uv run alembic -c apps/api/alembic.ini current ; uv run alembic -c apps/api/alembic.ini check
Remove-Item Env:REQUIRE_POSTGRES_TESTS
```

### Commit 3 — `feat(api): stream attachment bytes through real object storage`

内容：`storage/{ports,s3,settings,bootstrap}.py`、`services/attachments.py`、`api/routes|schemas/attachments.py`、`config.py` 非敏感项、`.env.example`、请求上限前缀规则、错误码加法、`tools/attachments_gc.py`、真实 PG + 真 MinIO 的 API/并发/授权/GC 测试。

```powershell
uv sync --locked
uv run ruff check apps/api ; uv run ruff format --check apps/api
uvx --from ty==0.0.94 ty check apps/api/src/revnote_api/api apps/api/src/revnote_api/storage apps/api/src/revnote_api/services/attachments.py apps/api/src/revnote_api/models apps/api/src/revnote_api/config.py apps/api/src/revnote_api/tools
uv run pytest apps/api/tests/unit -v
$env:REQUIRE_POSTGRES_TESTS = '1' ; $env:REQUIRE_OBJECT_STORAGE_TESTS = '1'
uv run pytest apps/api/tests/api -m "api and not api_concurrency and not transport" -v --junitxml=api-http.xml
uv run pytest apps/api/tests/api -m api_concurrency -v --junitxml=api-http-concurrency.xml
uv run pytest apps/api/tests/api -m attachments -v --junitxml=api-attachments.xml
Remove-Item Env:REQUIRE_POSTGRES_TESTS, Env:REQUIRE_OBJECT_STORAGE_TESTS
```

提交前完成 **Oracle 设计/风险闸口**（内核零影响、事务外 I/O、单赢家、授权、不吞正文）。

### Commit 4 — `feat(web): attach files and keep the markdown source as truth`

内容：能力探测与门控、上传/失败/离线状态、Markdown 引用插入不重写既有正文、blob 预览与释放、`capabilities` 404 降级。

```powershell
pnpm lint:web
pnpm typecheck:web
pnpm test:web
pnpm test:markdown
pnpm build:web
```

### Commit 5 — `test(web): prove attachment sync and storage gates end to end`

内容：runner 的 `owned`/`external` MinIO 模式、每 run 唯一桶与清理、4 个新 E2E 标签与报告守卫、`check_test_reports.py` 新组、CI 新 job 与 `image.needs`、`e2e/README.md`。

```powershell
pnpm --filter @revnote/web exec playwright install chromium
$env:WEB_E2E_DATABASE_URL = 'postgresql+asyncpg://<测试账号>:<测试密码>@127.0.0.1:5432/revnote_web_e2e_local'
$env:WEB_E2E_MINIO_EXECUTABLE = 'D:\tools\minio.exe'   # P1 前置条件
$env:REQUIRE_POSTGRES_TESTS = '1'
pnpm test:e2e
pnpm check:web-reports
pnpm check:api-reports
Remove-Item Env:WEB_E2E_DATABASE_URL, Env:WEB_E2E_MINIO_EXECUTABLE, Env:REQUIRE_POSTGRES_TESTS
```

### Commit 6 — `docs: record phase5 attachment scope limits and acceptance`

内容：`docs/phase5-attachments-api.md`、`docs/phase5-validation.md`（只写实际运行结果）、`README.md` 的已实现/未实现/已验证/未验证分区；随后执行最终全量门禁并发布。

```powershell
uv sync --locked
pnpm install --frozen-lockfile

uv run ruff check apps/api
uv run ruff format --check apps/api
uvx --from ty==0.0.94 ty check apps/api/src/revnote_api/api apps/api/src/revnote_api/storage apps/api/src/revnote_api/services apps/api/src/revnote_api/models apps/api/src/revnote_api/config.py apps/api/src/revnote_api/main.py apps/api/src/revnote_api/tools apps/api/scripts

$env:REQUIRE_POSTGRES_TESTS = '1' ; $env:REQUIRE_OBJECT_STORAGE_TESTS = '1'
uv run pytest apps/api/tests -v --junitxml=api-full.xml
uv run pytest apps/api/tests/unit -v --junitxml=api-unit.xml
uv run pytest apps/api/tests/integration -v -m "integration and not concurrency and not migrations" --junitxml=api-integration.xml
uv run pytest apps/api/tests/integration/kernel -v -m concurrency --junitxml=api-concurrency.xml
uv run pytest apps/api/tests/integration/test_migrations.py -v --junitxml=api-migrations.xml
uv run pytest apps/api/tests/api -v -m "api and not api_concurrency and not transport" --junitxml=api-http.xml
uv run pytest apps/api/tests/api -v -m api_concurrency --junitxml=api-http-concurrency.xml
uv run pytest apps/api/tests/api -v -m transport --junitxml=api-transport.xml
uv run pytest apps/api/tests/api -v -m attachments --junitxml=api-attachments.xml
uv run python apps/api/scripts/check_test_reports.py

pnpm lint:web ; pnpm typecheck:web ; pnpm test:markdown ; pnpm test:web ; pnpm build:web
pnpm test:e2e ; pnpm check:web-reports
```

并必须完成：迁移库 `upgrade head → downgrade base → upgrade head → check`；临时 PG 与 MinIO 进程停止确认；暂存差异/凭据/生成物审查；禁词扫描 0 命中；`apps/api/src/revnote_api/kernel/**`、`services/sync_*.py`、`apps/web/src/sync/**` **零差异**复核。

### 发布

```powershell
git push origin main
gh run list --repo LeiSureLyYrsc/Revnote --branch main --limit 5
gh run watch <本次SHA对应run-id> --repo LeiSureLyYrsc/Revnote --exit-status
# 失败时：
gh run view <run-id> --repo LeiSureLyYrsc/Revnote --log-failed
```

一次 push；失败只追加修复提交并重跑受影响本地门禁；**不强推、不改写已发布历史、不用旧 run 绿色结果替代本次验收**。由于存在已记录的 flake 史，本次绿的标准是：**同一 SHA 连续两轮五 job 全绿**（`image` 覆盖 api / web / web-e2e / api-attachments）。

---

## 十、风险与已知限制

| 风险 | 应对与剩余限制 |
|---|---|
| 官方 MinIO 分发不可达（本地二进制缺失 / CI 拉取失败） | P1 前置哈希校验；CI pull 失败即**停止并报告**，不替换第三方镜像或其他 S3 产品；本地与 CI 两条路径互不依赖 |
| 已知 E2E flake | Commit 1 必须先修；证据要求 focused ×5 + full ×2（各自新库）；`manual_conflict` 仍需持续观察 |
| API 承担上传/下载带宽与 CPU | 明确上限 `ATTACHMENT_MAX_BYTES`（默认 25 MiB）、64 KiB 分块、O(chunk) 内存；回读校验带来约 2× 流量；受控预览下可接受 |
| 预签名覆盖/桶 CORS/内外地址分歧 | 本阶段全部规避（D4）；直传优化后置，且届时必须做条件写与真实校验 |
| 引用真相在正文的代价 | 无附件删除 UI、无去重、未引用 ready 对象长期占用；已在「本阶段不做」与 README 限制中写明 |
| `<img>` 无法携带 Bearer | Web 用 `fetch` + `blob:` 预览；MIME 白名单 + `nosniff`；**拒绝 SVG**；`no-store` 防共享缓存 |
| 客户端离线时不能附附件 | 显式禁用并说明；不静默排队二进制；文末限制注明「离线看不到已加载图片预览」 |
| 单写标签/身份切换后附件 UI 状态 | 复用 Phase 4 的 `ownershipGeneration`/lease；上传句柄绑定分区，迟到响应按代次丢弃 |
| 公网滥用面扩大 | 真实限流仍未实现（`RATE_LIMIT_ENABLED` 仍是占位）；**发布等级继续是受控预览**，公网前必须完成 F |
| 报告/日志泄漏 | 诊断只出固定码与计数；`file_name`、MIME 原值、字节、token 不落日志；哨兵测试覆盖存储凭据与附件字节 |
| 新 job 拉长 CI 时间 | 仅新增一个 job，`image` 等待它；不以减少既有检查换速度 |

---

## 十一、本阶段不做

- 预签名直传/直读、桶 CORS、`MINIO_PUBLIC_ENDPOINT` 浏览器分流。
- 附件去重、附件删除 UI、未引用 ready 对象回收、对象生命周期策略、后台调度器。
- 服务端解析/重写正文、附件事件流、附件出现在 `pull`/`snapshot` 中、扩 `change_events.entity_type`。
- `Range` 请求、断点续传、分片上传、后台续传。
- Joplin Server 协议桥（C）、自动三方合并/CRDT/冲突副本自动上传（D）、snapshot 分页与事件清理（E）。
- 公网加固：TLS、真实限流、备份恢复演练、同源生产部署与反向代理（F）。
- 参考 CLI（I）、搜索、时间机器、E2EE、OIDC、Passkey、成员管理、会话管理中心。
- PWA/Service Worker、全离线冷启动、页面关闭后的后台同步、附件二进制在 IndexedDB 的离线排队。
- 前端视觉重构、chunk 拆分工程（既有 859–896 kB 警告继续记录，不扩范围）。
- 本地镜像构建、完整生产部署、任何 Docker 本地运行。

回退时不自动删除 `attachments` 数据或降级 schema；旧客户端遇到不认识的能力端点必须降级而不是清库；涉及协议或本地行为的修复优先前向兼容。

---

## 十二、批准记录

用户已预先授权：**Phase 5 按本文 D1–D19 的推荐默认值全部自动锁定**，不需要再次逐项确认，本轮输出即为可执行的 Build 输入。D1 的范围取舍（H + B，且 C/D/E/F/I 后置）与 D4（放弃预签名直传）是本阶段两个最需要注意的取舍点，均已按推荐默认值锁定。

Build 启动顺序固定为：**P1–P5 前置条件 → R 核查 → 接口冻结 → 车道并行 → Oracle 闸口 → 全链路 → 六提交 → 一次 push → CI 连续两轮全绿**。
