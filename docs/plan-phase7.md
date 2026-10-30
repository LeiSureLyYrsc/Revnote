# Revnote Phase 7 实施计划

> **状态：已批准并锁定。D1–D16 全部按推荐默认值执行；Phase 7 Build 不再向用户索取计划或确认。**
>
> 状态：Plan 输出（未改代码、未提交、未推送）。**助手已代用户起草并锁定推荐默认值**；文中 **取舍点** 仅标注取舍，不再等待用户补写计划。Build 可直接按本文与 `phase7-build-prompt.md` 启动。
>
> 基线：Phase 6 DONE — tip `25a6d5f`；CI run `41026703789` 同一 SHA 连续两轮五 job 全绿；对象存储仍为 **RustFS 1.0.4**（永久，不再回退 MinIO）；Joplin 桥已交付（`adapters/joplin`、迁移 `0005`、`JOPLIN_BRIDGE_ENABLED` 默认 `false`）。

---

## 一、基线核对与前置条件

### 1.1 已阅读材料

`README.md`、`docs/consensus-brief.md`、`docs/stack-blueprint.md`、`docs/build-conventions.md`、`docs/plan-phase2.md`–`docs/plan-phase6.md`、`docs/phase3-sync-api-spec.md`、`docs/phase6-joplin-bridge.md`、`docs/phase6-validation.md`、`.slim/deepwork/phase6-build/progress.md`（PHASE 6 DONE）。

并核对：`apps/api/src/revnote_api/services/sync_read.py`（`SnapshotView` 仍全量、超限整次 `409 snapshot_too_large`）、`api/routes/sync_pull.py`（明确拒绝 `cursor`/`limit`/`offset`/`page`/`since`）、`apps/web` sync engine 单次 `snapshot()` bootstrap、CI 五 job + `api-joplin` 组。

### 1.2 基线事实（Build 第一步复核，不能沿用旧结论）

| 项 | 实际观测 | 对 Phase 7 的影响 |
|---|---|---|
| 工作区 HEAD | `25a6d5f docs: note the web runner flake and the two green reruns`，应与 `origin/main` 同步 | Phase 7 建在绿基线 + Joplin 桥 + RustFS 上 |
| CI | run `41026703789`：api / web / web-e2e / api-attachments（含 joplin）/ image 连续两轮 success | 新工作不得削弱五 job 与 joplin 守卫 |
| Snapshot 契约（Phase 3 D15） | 单 RR 只读、**不分页**；默认 10,000 对象 / 16 MiB；超限整次 409；路由拒绝分页查询参数 | Phase 7 **加法**引入分页，并更新规格/能力声明；旧「无参全量」路径行为保持 |
| Pull | 已有 `since`/`limit`/`next_cursor`/`has_more`（同事务页） | Snapshot 分页应对齐十进制字符串游标习惯，但语义是对象页而非事件页 |
| Joplin 桥 | 默认关；原生 UUID 权威；资源走附件+RustFS | 本阶段不扩桥；关桥回归仍须绿；桥若依赖原生 snapshot 则间接受益，不单独改适配器除非硬阻塞 |
| 对象存储 | RustFS 1.0.4；本机 exe SHA-256 `3396617e…d899`；CI zip `4924ad4b…c31b5c` | **本阶段以 PG/同步读为主**；不换存储；若跑附件/joplin 回归仍用真 RustFS |
| 本机 | 无 Docker；真 PG 仓库外临时实例；PATH 需显式加 `D:\Git\cmd` 等；TEMP=`D:\tmp\…` | 与 Phase 5/6 相同 |
| 契约守卫 | 精确路由/错误码/表集合、报告守卫 no-skip | Snapshot 响应与查询参数为**加法**；精确断言按加法更新 |

### 1.3 前置条件（Build 前必须满足，缺一即停）

1. **P1 — 真 PostgreSQL**：业务/迁移/E2E 库彼此独立；`REQUIRE_POSTGRES_TESTS=1`；每批 `finally` 停止。本阶段承重以真 PG 为主（大快照分页不依赖对象存储）。
2. **P2 — RustFS（回归时）**：若本地/CI 跑 attachments 或 joplin 组，仍只用已登记 `D:\tools\rustfs\rustfs.exe` 与 CI pin 1.0.4。**禁止 MinIO、禁止下载未登记二进制。**
3. **P3 — 真实浏览器**（Web bootstrap 接线验收时）：Playwright Chromium 已装。
4. **P4 — 工具路径**：`git`=`D:\Git\cmd\git.exe`、`gh`、`uv`、`pnpm.cmd`；`TEMP`/`TMP`=`D:\tmp\opencode-temp`；不覆盖用户未跟踪的历史 `phase*.md`；**不提交**根目录提示草稿除非明确是本阶段交付文档进 `docs/`。
5. **P5 — 基线绿**：HEAD=`25a6d5f`（或以当时 `origin/main` 为准）且近期同 SHA 双轮 CI 结论已知；若 tip 已前进，以新 tip 为基线并在 progress 记录。

---

## 二、候选范围比较与推荐

### 2.1 比较

| 候选 | 价值 | 主要依赖与新增风险 | 依赖顺序判定 |
|---|---|---|---|
| **E. Snapshot 分页** | 解除「大工作区无法 bootstrap」硬限制（README/Phase 3 已知）；原生 Web 与任何依赖 snapshot→pull 的客户端直接受益 | 跨 HTTP 请求保持与单 RR 等价的一致性；游标稳定排序；Web 消费多页；加法改 Phase 3 规格 | **Phase 7 主范围**（产品路径上最清晰的未完成项） |
| **D. 自动三方合并** | 减少人工冲突；共识差异化候选 | 需可信 `merge_base_rev`；Web 冲突 UI；与分页正交且更大 | 后置 |
| **I. 参考 CLI** | 第二官方消费者；共识「Web + API + CLI」 | 新包/发布面；维护成本；不解除 bootstrap 上限 | 后置（分页之后更有价值） |
| **F. 公网加固** | TLS/真限流/备份 | 无部署目标；限流需共享状态；发布等级仍为受控预览 | 后置 |
| **J. 附件进事件流** | 附件可见于 pull/snapshot | 扩 `change_events.entity_type`；动摇「附件非内核实体」不变量 | **明确不做**（Phase 5/6 已否决） |
| **K. Joplin 桥加深** | GUI 矩阵、Data API、E2EE | 非主路径；Phase 6 刚收口 | 后置/不做 |

### 2.2 推荐结论

> **Phase 7 = E（原生 sync snapshot 分页 bootstrap）+ 最小 Web 消费接线。**
>
> 本阶段交付：*超过默认对象/字节上限的工作区，可通过带游标的 snapshot 分页在同一逻辑水位（`as_of_seq`）下拉完全部活跃对象，再 `pull since=as_of_seq` 追上增量；无分页参数的小工作区行为与 Phase 6 完全一致；超硬顶（若保留）仍整次拒绝半成功；Web 同步引擎能多页 bootstrap；原生五 job + joplin 守卫不削弱；RustFS/Joplin 默认关路径不变。*
>
> **未选原因**：D/I/F 均有价值，但 E 是文档反复标注的阻塞性已知限制，且不扩大产品面；I 在大库可 bootstrap 之后更稳；D 需要独立冲突协议与 UI，范围更大；F 与「受控预览」定位不符。

### 2.3 依赖顺序

```text
复核基线（HEAD/CI/RustFS pin）+ 读 sync_read / sync_pull / Web engine
        ↓
接口冻结：分页查询参数、响应字段、游标编码、一致性模型、capabilities、硬顶策略
        ↓
内核/服务：SyncReadService 分页读（稳定序 + as_of_seq 水位过滤）
        ↓
HTTP：snapshot 路由加法接受分页参数；拒绝歧义组合；错误码表更新
        ↓
真 PG 契约测试（小库无参不变；大库多页拼出全集；中途写入不污染已钉水位）
        ↓
Web：transport + engine 多页 snapshot → activateSnapshot；E2E/单测覆盖超限路径
        ↓
文档 / 报告守卫 / CI → 提交 → 一次 push → CI 连续两轮全绿
```

**明确不选**：D、I、F、J、K（本阶段）。

---

## 三、目标与验收

### 3.1 本阶段实现

1. **一致性模型（推荐默认，D4）**
   - 第一页在 `REPEATABLE READ, READ ONLY` 下读取并钉死 `as_of_seq` / `min_retained_seq`。
   - 后续页各自新开 RR 只读事务，但**只返回在 `as_of_seq` 水位下仍活跃的对象**（`created/生效 seq ≤ as_of_seq`，且未被该水位前的 tombstone 删除），按稳定全序（推荐 `(entity_kind, entity_id)` 或既存只读排序键）+ 加密游标分页。
   - 客户端拼完所有页后得到该水位的完整活跃集，再 `pull since=as_of_seq`。中途并发写入只出现在 pull，不出现在已钉 snapshot 页里。

2. **HTTP 加法**
   - `GET /api/v1/workspaces/{id}/sync/snapshot`：
     - **无分页参数**：行为与 Phase 6 相同（全量或 `409 snapshot_too_large`）。
     - **带分页参数**（推荐名：`limit` + 可选 `cursor`；十进制字符串水位若需回传则用响应字段而非要求客户端回传 since）：返回一页 `objects` + `next_cursor` + `has_more` + 与首页相同的 `as_of_seq`/`min_retained_seq`。
   - 非法游标 → `400`（固定 code，写入映射表）；游标与 `as_of_seq` 不匹配/过期策略在 D 表锁定（推荐：游标内嵌 as_of_seq，不匹配则 `410` 或明确 `resync` 类 code，迫使从头 bootstrap）。
   - 仍拒绝会与「无参全量」语义冲突的歧义组合（在计划/规格写清）；**禁止**半页成功却声称全量。

3. **限额**
   - 每页对象数/字节沿用或拆出 `SYNC_SNAPSHOT_PAGE_MAX_OBJECTS` / `SYNC_SNAPSHOT_PAGE_MAX_BYTES`（默认建议低于或等于原全量上限，防止单页过大）。
   - 可选硬顶：`SYNC_SNAPSHOT_MAX_TOTAL_OBJECTS`（跨页总和）；超硬顶整次 bootstrap 拒绝（固定 code）。若本阶段不设总和硬顶，须在文档写明「仅受页循环与部署超时约束」。

4. **Capabilities**
   - `GET /api/v1/capabilities` **加法**声明 `snapshot_pagination: {enabled, page_max_objects, page_max_bytes, …}`；旧客户端忽略未知字段。

5. **Web**
   - sync transport/engine：若 capabilities 支持分页（或首屏收到 `has_more`/`snapshot_too_large` 后的升级路径按 D 表），循环拉取至 `has_more=false`，再进入既有 pull 流程。
   - 保持：冲突仍人工处理；不自动合并；不引入 PWA。

6. **测试与 CI**
   - 真 PG：无参小库回归；强制多页大库（构造 > page limit 对象）；页拼接后集合等于水位全集；bootstrap 中写入新笔记不出现在 snapshot 页而出现在后续 pull；坏游标/错水位；报告守卫加法。
   - Web 单测（多页解码/循环）；如可行，E2E 标签证明超 page-limit 工作区可进入可编辑态（真 PG）。
   - 对象存储：本阶段新承重不依赖 RustFS；跑既有 attachments/joplin 回归时仍要真 RustFS。

### 3.2 必须成立的验收结果

| 目标 | 验收结果（真实环境） |
|---|---|
| 小库兼容 | 无分页参数时响应形状与错误行为与 Phase 6 一致（含仍可 409 的全量路径） |
| 大库可 bootstrap | 页数 ≥2 的工作区经分页拉全后，对象集合 = 该 `as_of_seq` 活跃集；再 pull 无空洞 |
| 水位隔离 | bootstrap 期间并发 push 的新对象不出现在已钉 snapshot 页；可在 pull 中见到 |
| 游标诚实 | `has_more` 与 `next_cursor` 不陷阱（空页+has_more 禁止）；坏游标固定错误码 |
| 能力探测 | capabilities 加法；旧客户端可忽略 |
| Web | 多页 bootstrap 后可编辑/可同步；既有冲突/队列不变量不削弱 |
| 回归 | API 各组 + joplin + attachments（若跑）+ Web 单测 + E2E + 报告守卫不削弱 |
| 发布 | 本地全门后一次 push；同一 SHA **连续两轮** CI 全绿 |

---

## 四、决策锁定表 D1–D16

| ID | 决策（推荐默认值） | 取舍点 |
|---|---|---|
| **D1** | 范围 = **E**（snapshot 分页）+ 最小 Web 消费；D/I/F/J/K 后置或不做 | 不宣称公网就绪、不做自动合并、不做参考 CLI |
| **D2** | Phase 2–6 不变量只增不改；RustFS 1.0.4 + `S3_*` + boto3 保持；Joplin 默认关 | 禁止 MinIO 回潮；不扩 `change_events.entity_type` 纳附件 |
| **D3** | 无分页参数 = 遗留全量语义（含 `409 snapshot_too_large`）；显式 `limit`（+可选 `cursor`）启用分页 | 另一方案「永远分页」会强迫所有旧客户端改代码，兼容性更差 |
| **D4** | 一致性：首页钉 `as_of_seq`；续页按水位过滤活跃对象 + 稳定序；不跨请求长持同一 DB 事务 | 长事务跨 HTTP 不可用；纯「每页最新 RR」会让中途写入泄漏进 bootstrap |
| **D5** | 游标：不透明（或 HMAC/签名）串，内嵌 `as_of_seq` + 最后键；客户端当黑箱；一律十进制/安全 charset 上线文档化 | 明文 offset 易被篡改导致漏对象 |
| **D6** | 单页超限：缩页或 `413`/`409` 固定码（与 pull 预算哲学对齐，优先「连续前缀 + has_more」，禁止空页 has_more） | 与 pull 字节预算行为保持家族相似 |
| **D7** | 总和硬顶：本阶段设 `SYNC_SNAPSHOT_MAX_TOTAL_OBJECTS`（默认建议 100k 量级，可配置）；超顶明确错误，不静默截断 | 无硬顶则极端库可打满 API；可在 validation 记录默认值 |
| **D8** | `snapshot_too_large` 保留给**无参全量**路径；分页路径用不同 code 区分「单页装不下」与「超过总和硬顶」（名称以实现为准，写入错误表与 OpenAPI） | 避免一个 code 表示三种失败 |
| **D9** | Capabilities 加法 `snapshot_pagination`；Web 以 capability 或首轮协议协商走多页 | 未声明则 Web 保持旧单次 snapshot |
| **D10** | 不改 push/pull/tree/note 既有语义；不改 Joplin 适配器除非发现硬阻塞（默认零改） | 桥非本阶段目标 |
| **D11** | 测试：真 PG 多页拼集 + 并发隔离 + 无参回归；Web 单测必过；E2E 至少一条超限 bootstrap（真 PG）；**skip≠通过** | — |
| **D12** | CI：新用例进既有 api job 报告守卫（或独立标记组）；五 job 形状保持；同一 SHA 连续两轮全绿 | 不把「手工灌 10 万笔记」当 CI 硬门禁；用可重复工厂造 N=page*2+ε |
| **D13** | 迁移：本阶段**默认无新表**（游标无状态）；若实现选择服务端续读租约表再单列迁移 `0006`，须在 progress 说明——推荐无状态游标以免新表 | 无状态优先 |
| **D14** | Orchestrator **禁止派 SubAgent**；模型 `deepseek-official/deepseek-flash#max`；**永不** `gpt-6.1-sol`；designer 仅必要时 `murasameai-vip/gemini-3.8-flash-high` | Standing |
| **D15** | 不打印/不提交 API keys、S3/RustFS 凭据、session token、游标实现密钥；诊断只固定码与计数 | — |
| **D16** | 文档：`docs/plan-phase7.md`、`docs/phase7-snapshot-pagination.md`（或等价附录进 sync API spec）、`docs/phase7-validation.md`；更新 `docs/phase3-sync-api-spec.md` **附录/修订说明**（不偷偷改写已锁定历史正文的含义而不加注）；README 已实现/未实现分区 | 正式文档进 `docs/`；根目录 `phase7-*.md` 可不提交 |

---

## 五、模块与文件边界（拟新增/修改）

| 边界 | 路径 | 职责 |
|---|---|---|
| 读服务 | `apps/api/src/revnote_api/services/sync_read.py` | 分页 snapshot；水位过滤；游标编解码 |
| HTTP | `apps/api/src/revnote_api/api/routes/sync_pull.py` | 查询参数加法；错误映射 |
| Schema | `apps/api/src/revnote_api/api/schemas/sync_*.py` | `has_more`/`next_cursor` 等响应字段 |
| 配置 | `config.py`、`.env.example` | 页限额/总和硬顶（非敏感） |
| 能力 | capabilities 路由 | 加法字段 |
| 错误 | `errors.py` 等 | 新 code 登记 |
| 测试 | `tests/api/test_*snapshot*`、`tests/unit/…`、integration RR 证明（若需） | 契约与并发 |
| Web | `apps/web/src/**/sync*`、`client.ts`、`engine.ts`、contracts | 多页 bootstrap |
| E2E | `apps/web/e2e/**` | 超限 bootstrap 标签（真 PG） |
| CI/守卫 | `.github/workflows/ci.yml`、`check_test_reports.py` | 加法 |
| 文档 | `docs/plan-phase7.md`、`docs/phase7-*.md`、`phase3-sync-api-spec.md`（标注修订）、`README.md` | 规格与证据 |

**禁止越界**：削弱内核八命令/fingerprint；改 pull 事件游标语义；附件进 `change_events`；预签名；替换 RustFS；自动三方合并；参考 CLI；公网 TLS/限流产品化；本地 docker build；碰 Clash；提交 `.env`/凭据/未跟踪历史 `phase*.md` 草稿；派 SubAgent。

---

## 六、数据与协议变更（摘要）

### 6.1 迁移

- **推荐**：无迁移（D13）。
- 若实现必须服务端游标租约：单一加法迁移 `0006_*`，精确表集合断言更新；不改既有表列语义。

### 6.2 Snapshot 协议（加法示意）

| 模式 | 请求 | 响应要点 |
|---|---|---|
| 遗留全量 | 无 `limit`/`cursor` | 今日形状；过大 → `409 snapshot_too_large` |
| 分页首页 | `limit=…` | `as_of_seq`/`min_retained_seq`/`objects`/`next_cursor`/`has_more` |
| 分页续页 | `limit=…&cursor=…` | 同水位字段必须一致；`objects` 为后续页 |

错误：坏游标 / 水位不匹配 / 单页预算 / 总和硬顶 — 分 code 列入映射表与单测。

### 6.3 兼容策略

- 旧 Web/原生客户端不传分页参数 → 零行为变化（仍受全量上限约束）。
- 新 Web 读 capabilities 后走分页，解除大库 bootstrap 失败。
- Joplin 桥默认不改；若其某处误依赖全量 snapshot，仅做最小适配并在 validation 记录。

---

## 七、测试策略与证据责任

| 承重主张 | 最小证据 |
|---|---|
| 无参兼容 | 真 PG：小库 snapshot 与 Phase 6 同形状；过大仍 409 |
| 多页完备 | 真 PG：N=2*page+ε 对象；拼页 == 水位活跃集 |
| 水位隔离 | 真 PG：页间插入笔记；snapshot 拼集不含；pull 含 |
| 游标 | 单测编解码 + API 坏游标/错水位 |
| Web | 单测多页循环；E2E 超限可 bootstrap（真 PG） |
| 回归 | 既有 api/joplin/attachments/web/e2e/image 不削弱；报告守卫 |

---

## 八、执行方式（Standing）

> **禁止 SubAgent。** Orchestrator（`deepseek-official/deepseek-flash#max`）独占实现、测试、文档、提交与发布。  
> 不得使用 `gpt-6.1-sol`。不得打印 API keys。限额/鉴权错误立即停并写 progress。  
> 进度文件：`.slim/deepwork/phase7-build/progress.md`。  
> TEMP/TMP：`D:\tmp\opencode-temp`。不碰 Clash。Murasame-PC 禁止 docker build。

串行点：基线复核 → 接口冻结 → 服务+HTTP → 真 PG 门禁 → Web → 文档/CI → 提交 → push → 双轮 CI。

---

## 九、建议 Conventional Commits（可按实况拆合 4–7 个）

1. `docs: capture the phase7 snapshot pagination plan`
2. `feat(api): page workspace snapshots under a pinned as_of_seq`
3. `feat(api): advertise snapshot pagination on capabilities`
4. `test(api): prove multi-page snapshot bootstrap on real postgres`
5. `feat(web): consume paged snapshots during sync bootstrap`
6. `test(web): cover oversized workspace bootstrap`
7. `docs: record phase7 snapshot pagination scope and acceptance`

全部本地验收通过后**一次 push**；失败只追加修复；不强推；同一 SHA 连续两轮 CI 全绿。

---

## 十、风险与已知限制

| 风险 | 应对 |
|---|---|
| 跨请求一致性被误做成「每页最新」 | D4 强制水位过滤；并发测试 |
| 游标可伪造导致漏读/重读 | 签名或不透明游标 + 错水位失败 |
| 单测造大数据过慢 | CI 用小 page limit 夹具，不靠十万行灌库 |
| Web 半成品 bootstrap | 引擎须原子切换到拼完的快照；失败可重建 |
| 公网滥用 | 真限流仍未做；发布等级仍为受控预览 |
| SubAgent 供应商缓存 | 本阶段禁用 SubAgent |

---

## 十一、本阶段不做

- 自动三方合并 / CRDT / 冲突副本自动上传
- 参考 CLI、搜索、时间机器、OIDC、Passkey、成员管理中心
- 公网加固（TLS、真限流、备份演练、生产反向代理）
- 附件进入 `pull`/`snapshot` / 扩内核 `entity_type`
- Joplin Data API、GUI 自动化硬门禁、桥功能扩张（除非硬阻塞最小修复）
- 预签名直传、MinIO、第二对象存储
- PWA / 全离线冷启动 / 页面关闭后后台同步
- 本地 Docker 镜像构建；碰 Clash；打印或提交密钥；派 SubAgent

---

## 十二、批准记录

本计划由助手根据 Phase 6 收口状态、README 未实现清单、共识简报与 Phase 3 snapshot 已知限制起草。**在 E / D / I / F 中选定 E（snapshot 分页）为 Phase 7**，理由见 §2.2；**D1–D16 按推荐默认值锁定**，供 OpenCode Build（orchestrator-only）直接执行，无需再向用户索取 phase7-plan/build-prompt。

Build 启动顺序：**P1–P5 → 基线复核 → 冻结 → 实现 → 全门 → 提交 → 一次 push → CI 双轮全绿**。
