用户已批准 D1–D18 推荐方案。

# Revnote Phase 4 实施计划（已批准）

## 一、结论与推荐范围

**推荐 Phase 4 选择 A：让现有 Web 成为具备可靠本地保存、离线写队列和原生同步能力的客户端。**

本阶段交付：

> 登录后读取真实工作区；笔记与文件夹操作先可靠落到本机，再通过既有 API v1 同步；刷新能够恢复已落盘内容；网络故障、响应丢失、并发修改或远端删除不会静默丢弃本地修改。

**不同时加入 MinIO 附件、Joplin 桥、自动三方合并或 snapshot 分页。**

已阅读要求的八份文档，并核对相关 Web、HTTP schema、测试及 CI 文件。仓库中的 main 引用为 `77a20c2b6e7050d0918f8d723bd222eb216c7747`，与背景一致。`1157 passed / 0 skipped` 来自现有验收记录；CI run `40847922220` 沿用用户提供的基线，**本轮没有重新运行测试或查询远端 CI**。

以上为 Plan 阶段基线记录；Build 实际执行结果以 docs/phase4-validation.md 为准。

---

## 二、Phase 4 候选范围比较

| 候选 | 价值 | 主要依赖与新增风险 | 推荐顺序 |
|---|---|---|---|
| **A. Web 接入原生 API** | 兑现自有客户端主路径，解决刷新丢正文；验证现有内核/API能否构成实际产品 | 浏览器持久化、不可变命令队列、会话恢复、多标签互斥、冲突及编辑器保真 | **Phase 4 主范围** |
| **B. MinIO/S3 附件** | 图片和文件能力，是后续完整笔记体验的重要组成 | 不可变对象、上传/确认状态机、真实 SHA-256 校验、预签名覆盖风险、引用关系、宽限期 GC；本机无 Docker，MinIO 运行尚未验收 | **A 之后独立阶段** |
| **C. Joplin 兼容桥** | 接入现有 Joplin 客户端 | Server 协议兼容、ID 映射、delta/locks、资源及客户端兼容验收；不应反向改造内核 | **原生闭环稳定后**，完整兼容通常还依赖附件 |
| **D. 自动三方合并** | 减少人工冲突，有差异化价值 | 客户端必须持有可靠 base；Markdown/AST 合并正确性、结构冲突和恢复体验复杂 | 本阶段仅人工处理，自动合并后置 |
| **E. snapshot 分页与大工作区 bootstrap** | 解除当前 10k 对象/16MiB 限制 | 需要跨请求一致性、快照寿命与协议兼容设计 | 后续独立设计；本阶段明确阻断与恢复出口 |
| **F. 公网运行加固** | TLS、真实限流、备份恢复、同源部署 | 属于对公网发布的必要门禁，但不是客户端算法的前置依赖 | Phase 4 默认受控预览；公网发布前单独完成 |
| **G. Web 自动化验收平台** | 证明真实浏览器持久性、会话和同步边界 | 少量开发依赖、真实 API/PG 生命周期、报告守卫 | **随 A 必做，不作为可选附加项** |

依赖顺序：

```text
客户端契约与代理修复
        ↓
本地持久化 + 会话协调
        ↓
冻结操作队列 + push/pull/bootstrap
        ↓
真实业务 UI + 人工冲突处理
        ↓
真实浏览器 / API / PG 验收
        ↓
后续附件 → 更完整的 Joplin 兼容
```

早期 `stack-blueprint.md` 中的 Basic Auth、REST 写接口、失败账本和附件校验草图不作为当前实施契约。**Phase 2/3 已锁定计划及 API v1 规格优先。**

---

## 三、目标与完成验收

### 3.1 本阶段实现

1. **登录与会话**
   - 密码登录、`/me`、工作区选择、refresh、退出。
   - 不新增注册页面；注册仍默认关闭。
   - refresh 串行执行，结果不确定时要求重新登录，不重试可疑旧 refresh。

2. **本地持久化**
   - IndexedDB 保存远端镜像、本地草稿、操作意图、冻结命令、冲突/恢复记录、同步游标。
   - 明确区分“正在写入本地”“已写入本地”“待同步”“已同步”。
   - 本地写入失败不能退回内存后继续显示保存成功。

3. **基本业务操作**
   - 文件夹：创建、改名、移动、删除。
   - 笔记：创建、编辑标题/正文、移动、删除。
   - 对应现有八类 push 命令；移动使用明确选择器即可，不扩拖拽工程。
   - 文件夹非空删除仍拒绝，不做递归删除或恢复旧 ID。

4. **同步循环**
   - snapshot bootstrap、连续 pull、持久 push 队列、故障重试和恢复。
   - 正确处理 push 六状态及整请求错误。
   - 已发送命令不变，后续编辑独立保存。

5. **人工冲突处理**
   - 展示本地、base、远端版本。
   - 显式采用远端、以本地内容重新 CAS 提交，或另存为新笔记。
   - 不自动合并、不静默覆盖、不自动创建大量远端冲突副本。

6. **编辑器接入**
   - Markdown 字符串为正文真相。
   - 提供可编辑源码模式。
   - 打开笔记、切换视图、仅修改标题不得自动重写正文。

### 3.2 必须成立的验收结果

| 目标 | 验收结果 |
|---|---|
| 保存真实有效 | 已显示“写入本地”的内容刷新后仍在；新浏览器上下文能从服务器取得已同步正文 |
| 离线写可恢复 | API 关闭、静态资源可达时，编辑→刷新→恢复草稿/队列→API恢复→同步成功 |
| 不重复写 | 服务端提交后丢失响应，重试仍使用同一命令；真实 PG 最终只有一条对应 event/ledger |
| 不清除后续编辑 | A 正在发送时继续编辑 B，A 确认不能把 B 清掉或标成已同步 |
| 冲突不丢内容 | 两个客户端修改同一版本，失败方保留本地内容，人工处理仍受 CAS 保护 |
| 删除安全 | 远端删除与本地脏稿并存时保留恢复稿，只能显式另存新 ID，不复活旧对象 |
| 游标安全 | pull 对象领先事件、旧结果重放、事务失败均不会跳过事件或倒退本地镜像 |
| 身份隔离 | 另一账号不能展示或发送原账号的草稿、命令与恢复记录 |
| 回归保持 | Phase 2/3 原有测试责任、API契约、迁移及 no-skip 门禁不减弱 |
| 发布完成 | 本地全部通过后一次 push；对应 Actions 全绿，镜像发布被所有检查门禁阻挡 |

### 3.3 “离线”的准确边界

**承诺：**

- 已加载页面在 API 不可达或浏览器断网时，可编辑已缓存内容并持久化。
- 静态资源可达时，重新打开页面可恢复已落盘草稿与队列。
- 应用再次运行且网络恢复后继续同步。

**不承诺：**

- 完全断网后的应用冷启动。
- 首次访问时离线登录、初始化或读取未缓存内容。
- 页面关闭后的后台同步。
- 崩溃前尚未完成本地提交的最后输入。

本阶段不引入 Service Worker/PWA，不能把 IndexedDB 持久化表述为“保证全离线启动”。

---

## 四、决策锁定表

以下 D1–D18 推荐方案均已获用户批准；约束锁定项继续生效。

| ID | 状态 | 决策与推荐默认值 |
|---|---|---|
| **D1** | **已批准** | Phase 4 只做 A + 必需 Web 验收平台；附件、Joplin、自动合并后置 |
| **D2** | 约束锁定 | Phase 2 不变量及 Phase 3 API v1 不削弱；不新增 REST 写包装 |
| **D3** | **已批准** | IndexedDB + `idb` 薄封装；TanStack Query 仅用于查询/UI缓存，不作为草稿或队列事实源 |
| **D4** | **已批准** | access 仅内存；“记住登录”默认不勾选，勾选后才将 refresh 保存至 IndexedDB；密码永不保存。未勾选时重载需重登才能同步，本地草稿仍保留 |
| **D5** | **已批准** | 同 origin 单个可写标签页，其他只读；Web Locks 互斥，不以 localStorage 心跳冒充严格锁；不支持所需能力时阻止写入并说明原因 |
| **D6** | **已批准** | 未退出的本机已激活 profile 可离线恢复缓存；退出后隐藏并锁住其内容，重新认证同账号后恢复。此机制不是磁盘加密或本地身份认证 |
| **D7** | **已批准** | refresh 前持久化 inflight 标记；失响应、超时、崩溃窗口或新 token 保存失败时重登，不自动重发旧 refresh |
| **D8** | **已批准** | 全客户端单发送器；每次 push 默认 **1 op**；每实体最多一个冻结未决命令，后续编辑/动作另存 |
| **D9** | **已批准** | 同实体后继操作及父文件夹依赖按确认结果串行，不预测未来 base_rev；保留 create/delete 等生命周期意图 |
| **D10** | **已批准** | 人工冲突；不做 last-write-wins，不自动 rebase 脏稿，不自动上传冲突副本 |
| **D11** | **已批准** | 另存使用新 entity/op ID；采用远端或放弃本地修改需确认，保留本地恢复记录，不自动清理恢复稿 |
| **D12** | **已批准** | Markdown 原文为真相；已有笔记默认走源码安全路径。富文本转换需要显式动作与原文恢复点，不能靠语法猜测承诺无损 |
| **D13** | **已批准** | snapshot 超限、pull 单项超限、游标异常均明确暂停；不提高上限、不拼 tree/note 冒充一致快照 |
| **D14** | **已批准** | 已确认暂时性错误采用有界指数退避；默认连续失败 5 次后暂停自动尝试，保留手动重试；无隐藏无限重试 |
| **D15** | **已批准** | 初始认证浏览器为桌面 Chromium/Edge 安全上下文；自动化以 Playwright Chromium 为准；390px 只证明响应式布局，不外推移动 Safari 支持 |
| **D16** | **已批准** | 发布等级为受控预览，不宣称公网生产就绪；全离线冷启动和后台同步不在范围 |
| **D17** | **已批准** | 新增 `idb`、Vitest、fake-indexeddb、最小 React 测试依赖及 Playwright；版本必须兼容当前 Vite 6/Node 24，不能为安装测试框架顺带大版本升级 |
| **D18** | 约束锁定 | 所有执行车道使用 `murasameai-vip/gpt-6.1-sol#high`；遵守 PG、Docker、Clash、凭据、命名与发布硬约束 |

---

## 五、模块与文件边界

以下为**拟新增或拟修改路径**，不表示已经实现。

| 边界 | 路径 | 职责 |
|---|---|---|
| 共享契约 | `apps/web/src/domain/{contracts,ids,decimal}.ts` | 本地接口、UUID helper、十进制串比较与校验；不以 UUID/时间排序同步 |
| API 客户端 | `apps/web/src/api/{contracts,client,auth,sync}.ts` | Bearer、错误信封、运行时解码、请求超时；认证与写入不采用通用自动重试 |
| 会话 | `apps/web/src/auth/{session,sessionRepository,SessionProvider}.tsx/ts` | 登录、`/me`、refresh 状态机、退出、凭据代次 |
| 本地存储 | `apps/web/src/storage/{database,schema,repository,recovery}.ts` | 版本化 IndexedDB、短事务、账号隔离、恢复与显式导出 |
| 同步 | `apps/web/src/sync/{operations,reducer,engine,coordinator,reconcile}.ts` | 纯状态机、意图调度、冻结命令、push/pull/bootstrap/resync、多标签写权 |
| 工作区绑定 | `apps/web/src/workspace/{WorkspaceProvider,useWorkspace,useNoteDraft}.tsx/ts` | 选择状态、持久层订阅、草稿版本与组件绑定 |
| 登录/同步/冲突 UI | `apps/web/src/components/auth/`、`components/sync/`、`components/conflicts/` | 登录、真实状态、错误补救、人工版本比较 |
| 现有 UI | `src/components/layout/AppLayout.tsx`、`navigation/FolderSidebar.tsx`、`notes/NotesSidebar.tsx`、`editor/EditorPane.tsx` | 替换内存占位，保留暗色三栏与窄屏切换 |
| 编辑器适配 | `src/components/editor/markdownAdapter.ts` | 加载回声抑制、源码保护、异步保存防串稿 |
| 应用入口 | `apps/web/src/App.tsx`、`src/router.tsx` | provider、登录/工作区入口、账号切换缓存清理 |
| 代理 | `apps/web/vite.config.ts` | 修复业务前缀，隔离 health 重写 |
| Web 测试 | `apps/web/tests/{unit,components}/`、`apps/web/e2e/` | 状态机、存储、组件和真实浏览器测试 |
| 测试配置 | `apps/web/{vitest,playwright}.config.ts`、`apps/web/scripts/check-test-reports.ts` | 报告、承重清单、no-skip、禁止 only |
| E2E 生命周期 | `apps/api/scripts/run_web_e2e.py` | 验证一次性数据库、迁移、启动真实 uvicorn/Vite、运行浏览器测试及 finally 清理 |
| 工程配置 | 根与 Web `package.json`、`pnpm-lock.yaml`、Web TS/ESLint 配置、`.gitignore` | 统一新增命令、依赖和产物排除 |
| CI/文档 | `.github/workflows/ci.yml`、`README.md`、`docs/plan-phase4.md`、`docs/phase4-validation.md` | 门禁、限制、真实验收证据 |

**后端业务默认零改动：**不修改 kernel、写 service、认证语义、API schema 或 Alembic revision。发现真实缺陷时先提出兼容修复及回归证据，不借机扩展契约。

---

## 六、数据与协议设计

### 6.1 本地数据模型

本地分区包含固定部署命名空间、`user_id`、`workspace_id`。对象键必须包含 `entity_type`，因为 note 与 folder 可以共享 UUID。

| Store | 内容 |
|---|---|
| `profiles` | 已知身份、激活/锁定状态、部署命名空间；不存密码 |
| `credentials` | 可选 refresh、session/凭据代次、refresh inflight 标记；不导出 |
| `workspaces` | bootstrap 状态、活动镜像代次、pull cursor、最后错误/成功时间 |
| `server_objects` | 已知远端对象、真实 rev/updated_seq、删除标记；按镜像代次隔离 |
| `drafts` | 用户最新标题/Markdown/位置、编辑版本、原始编辑依据、脏状态 |
| `intents` | 未完成的创建、移动、删除等动作及依赖；不能仅用字段差异代替生命周期动作 |
| `operations` | 冻结 wire 命令、覆盖的本地版本、发送状态、结果、重试信息 |
| `recovery_records` | base/local/remote、远端删除恢复稿、显式转换前原文；不自动回收 |

基本原子性：

- 草稿与相应同步意图同事务提交。
- 冻结命令与其覆盖版本同事务记录。
- ACK 与对应操作状态、被确认的本地版本同事务更新。
- pull 页应用、冲突保全与 cursor 同事务提交。
- **只有 `tx.done` 成功后才显示“已写入本地”。**
- IndexedDB 事务内不等待 HTTP、定时器或其他外部异步。
- 配额、事务中止、版本升级阻塞、异常断连均显式处理；不自动删脏稿腾空间。

部署后端发生变化时不得直接复用旧分区；本阶段不提供任意服务器切换功能。

### 6.2 冻结操作与后继意图

```text
用户编辑
  → 本地保存 draft + intent
  → 等待前驱和父对象依赖
  → 生成并持久化冻结命令
  → 发送
  → 确认原命令
  → 再处理后续 draft / intent
```

冻结后不能修改 `op_id`、`entity_id`、`base_rev` 或 payload；允许变化的只有发送状态、错误和重试元数据。

必须覆盖：

- create 未确认后又删除：保留 create，确认后再决定是否发送 delete。
- update A 发送中继续编辑 B：B 独立保存，A 的 ACK 不清除 B。
- delete 未确认后继续编辑：若删除已成功，内容转为恢复稿，只能另存新 ID。
- 新建父文件夹尚未确认：子对象命令等待父 create 成功。
- 远端已变化：不能仅取得新 rev 就自动把旧草稿换 base 再提交。

### 6.3 Push 处理

| 结果 | 客户端行为 |
|---|---|
| `applied` / `replayed` | 确认该冻结命令及其覆盖版本；不将返回值当作当前正文 |
| `conflict` | 暂停该实体后继命令，保存本地依据并进入人工处理 |
| `rejected` | 区分永久业务拒绝与可重试错误；拒绝不等于丢弃草稿 |
| `unknown` | 保留同一命令，后续同 op_id、同 payload 确认 |
| `not_attempted` | 保留原命令，不解释为业务失败 |
| 超时、断连、响应解析失败 | 可能已执行；保留所有可能发送的冻结命令，不换 ID 重发 |

额外规则：

- 批次整体 400/413 不能当作成功；展示具体阻断，保留内容。
- 401 进入会话协调，不让网络层自行循环 refresh。
- workspace 404 不能解释为全部对象已删除。
- 旧重放结果 rev=2、镜像已是 rev=7 时，只确认旧命令，不回退镜像。
- **push 的 seq 不推进 pull cursor。**

### 6.4 Pull、bootstrap 与重建

**Pull：**

- seq/rev/cursor 始终以十进制字符串持久化和传输；比较使用受控 BigInt，不经 JavaScript Number。
- cursor 仅采用 `next_cursor`，不能使用对象 `updated_seq` 或响应 `last_seq` 跳页。
- 对象按真实版本防回退；完整事件仍连续消费。
- 当前对象可能领先页内事件；暂缺父对象时使用待补全/未归类呈现，不能让笔记消失。
- 页事务失败则不推进 cursor，下次重读。

**首次 bootstrap：**

- snapshot 完整成功后写入新镜像代次。
- 原子激活该代次与 `as_of_seq`，随后 pull。
- 失败不显示成“空工作区”。

**410、cursor_ahead 或重建：**

1. 暂停普通发送和后继命令生成。
2. 保全 draft、意图、冻结命令及编辑依据。
3. 对可能已发送的命令，以原命令确认；必要时先重登。
4. snapshot 写 staging，完整成功后切换远端镜像。
5. 对照保留的本地状态处理差异，再恢复同步。

可以提前下载 snapshot，但**snapshot 不能证明某个未决 op 是否执行**。确认长期不可行时保持明确阻塞并允许导出，不清库“恢复”。

### 6.5 API v1 兼容性

**不需要 API v1 变更，也不需要服务端 schema 迁移。**

客户端每次只发送 1 op，属于现有 1–50 op 契约的合法子集。保留全部既有上限、错误、授权、CAS、重放及删除语义。

必须修复的前端路径问题：

```text
/api/v1/...  → 原样转发到 API
/api/health  → 精确重写为 /health
```

取消当前广义 `/api` 剥前缀规则，否则业务请求会错误变成 `/v1/...`。生产构建仍没有 Vite 代理，只记录同源反向代理要求，不宣称已完成生产托管。

---

## 七、会话、冲突与 UI 约定

### 7.1 会话与多标签

- 写标签持有 Web Lock，覆盖编辑、业务持久化、同步及凭据变更。
- 只读标签不独立运行 refresh 或同步循环；BroadcastChannel 仅作失效通知，不作锁。
- 正常交接先停止调度、落盘，再释放锁；使用 ownership generation 防旧异步回调写库。
- 崩溃释放锁不等于服务端请求终止；接管者先处理遗留 inflight。
- refresh 标记落盘失败则不发请求；新 pair 落盘失败或结果不确定则重登。
- 明确认证失效不会清除笔记队列。
- 离线退出只保证本机退出；未成功调用服务端 logout 时，不宣称远端 session 已撤销。

IndexedDB 不抵御同源脚本/XSS，保存 refresh 必须明确告知风险；本阶段不暗中改成 cookie/BFF。

### 7.2 冲突动作

| 动作 | 安全语义 |
|---|---|
| 采用远端 | 先处理旧未决命令，再明确放弃本地意图；保留恢复记录 |
| 以本地内容提交 | 用户明确确认覆盖意图；基于当前远端 rev，用新 op_id 做 CAS；再次冲突继续保留 |
| 另存为新笔记 | 新 entity/op ID，原对象不被覆盖；本地创建成功不等于服务端已同步 |
| 远端删除 | 无脏稿则移出活动列表；有脏稿则转恢复记录，不自动复活 |
| 本地待删遇远端更新 | 要求重新确认，不自动提高 base_rev 删除 |

### 7.3 界面原则

保留现有暗色三栏，不做视觉重构。新增：

- 登录与重新认证入口。
- 工作区加载/失败状态。
- 当前笔记本地保存状态与独立的工作区同步摘要。
- 待处理项目、错误原因和重试入口。
- 冲突/恢复面板。
- 源码编辑与显式富文本转换入口。
- 本地保存失败时的复制/导出补救。

不新增当前 API 没有的“归档”“快速备忘”等伪功能；搜索仍不接入。

---

## 八、测试策略与证据责任

### 8.1 验证矩阵

| 承重主张 | 责任车道 | 最小证据 |
|---|---|---|
| 草稿/意图原子保存，失败不谎报 | 本地存储 | Vitest + fake-indexeddb 故障测试；真实浏览器 reload |
| 已发命令不可变，ACK不吞后续编辑 | 同步 | 纯状态机用例 + 浏览器延迟响应 |
| 丢响应不重复写 | 同步 + E2E | 真实 API 已提交后丢响应，重放结果及 PG event/ledger 断言 |
| refresh 无并发复用、失响应不盲重试 | 会话 | 单测 + 同 BrowserContext 两 page；真实 refresh 响应丢失 |
| pull 不跳游标、不回退对象 | 同步 | 领先对象、旧 ACK、页事务失败、>2^53 用例 |
| 重建不清脏数据 | 存储 + 同步 | 410/异常游标/超限场景，staging 切换故障 |
| 账号隔离、单写标签 | 会话 + UI | 双账号与双标签真实浏览器测试 |
| Markdown 不静默损坏 | UI | 原文逐字节断言；打开、切换、改标题不入正文更新 |
| 既有内核/API不减弱 | 验证 | 全部既有真实 PG、并发、迁移、HTTP、transport 门禁 |

### 8.2 Web 单测与组件测试

使用 Vitest、fake-indexeddb、最小 React 测试环境，覆盖：

- 存储提交/中止、配额错误映射、schema blocked/versionchange。
- 冻结命令不可变、create/delete 生命周期、父依赖。
- 六状态、整请求失败、重试预算。
- old ACK、新草稿与远端新版本竞争。
- cursor/对象/恢复记录原子更新。
- refresh inflight 与凭据代次、退出和身份隔离。
- 异步加载/保存不串笔记，加载不触发 autosave 回声。
- 本地保存失败与网络失败文案分离。

fake-indexeddb 不替代浏览器事务、跨标签及实际持久化验证。

### 8.3 真实浏览器 E2E

拓扑：

```text
Playwright Chromium
    → Vite 同源代理
    → 真实 uvicorn
    → Alembic 创建的真实 PostgreSQL 测试库
```

必含：

1. 代理登录、`/me`、snapshot、八操作基本闭环。
2. 刷新保留正文；新浏览器上下文从服务器读取一致正文。
3. API 关闭但静态资源可达：编辑、刷新、恢复队列、恢复 API 后同步。
4. 已加载页面全离线编辑，恢复网络后同步。
5. 提交成功但响应丢失、客户端重载接管、原命令重放。
6. A 发送中编辑 B，A 的确认不清 B。
7. 两独立客户端 CAS 冲突、远端删除与本地脏稿。
8. 同 origin 两标签单写与崩溃接管；旧请求结果安全处理。
9. refresh 正常恢复、失响应重登；另一账号不能接走队列。
10. snapshot 超限、410、游标异常不清数据。
11. 扩展 Markdown、CRLF、尾空白、HTML/LaTeX/未知块的原文保护。
12. 390px 栏位切换、键盘操作、只读限制、无浏览器运行时错误。

异常通过确定性网络拦截、测试专用适配器或受控测试数据库准备构造；**不新增生产 failpoint 或调试端点**。响应丢失必须发生在真实服务端处理之后，不能仅中止尚未发出的请求。

### 8.4 真实 PG 与生命周期

- 本地继续使用仓库外临时 PG。
- 原业务测试库与迁移库保持不同。
- Web E2E 使用第三个独立一次性库，避免与 pytest/迁移互相污染。
- `REQUIRE_POSTGRES_TESTS=1`。
- 所有 schema 通过 Alembic，不用 `create_all()`，禁止 SQLite。
- E2E runner 只接收显式测试 DSN，验证目标隔离；不读取开发 `.env`。
- PG 由外部控制器管理；runner 管理自己创建的 API/Vite/浏览器子进程。
- 所有退出路径均 finally 清理；只停止本次拥有的实例和进程，不碰已有用户服务。

现有仓库外控制器已找到，支持 `start/custom/stop`。Build 需先核验其 Python 运行环境，并补齐第三个测试库准备，不能假定项目虚拟环境已安装控制器依赖。

### 8.5 No-skip、CI 与证据安全

- Vitest：禁止 only、零测试失败；输出 JUnit/JSON。
- Playwright：`forbidOnly: true`、`retries: 0`、不复用不明来源服务。
- 报告守卫要求各组非零、无 skipped/todo/failure/error，并核对承重用例清单。
- 保留现有 Python 分组与名称守卫，不硬编码新的总测试数。
- 新增独立 `web-e2e` job，使用官方 PG service；**`image.needs` 增加该 job**，不能 E2E 红而仍发布。
- 默认不录制含认证交互的 trace/HAR/storageState；不上传 token、密码、正文或原始请求体。
- 失败诊断优先保存经审查的状态、request_id、计数与安全断言。

最终记录到 `docs/phase4-validation.md`，只写实际运行结果。相同代码/输入/环境的有效专项证据可复用；最终合并状态仍跑一次完整本地门禁。

---

## 九、SubAgent 车道与串行点

所有车道统一模型：`murasameai-vip/gpt-6.1-sol#high`。

| 车道 | Agent | 文件所有权 | 验证责任 |
|---|---|---|---|
| **R：前置核查** | explorer | 只读，无业务文件写权 | 核对依赖兼容、当前脚本、测试入口；输出差异清单 |
| **A：本地数据** | fixer | `src/storage/**`、`tests/unit/storage/**` | 本地 schema、事务、恢复、配额/升级失败 |
| **B：API/会话** | fixer | `src/api/client.ts`、`auth.ts`、`src/auth/**`、`tests/unit/auth/**` | 会话状态机、refresh、凭据隔离；不改 coordinator |
| **C：同步核心** | fixer | `src/api/sync.ts`、`src/sync/**`、`tests/unit/sync/**` | 队列、八操作依赖、ACK、pull、bootstrap/resync、写权协调 |
| **D：业务 UI** | designer | `src/components/**`、`src/workspace/**`、`App.tsx`、`router.tsx`、`tests/components/**` | 设计并实现保存/冲突交互、源码保护、响应式和可访问性 |
| **E：跨边界验收** | fixer | Web 测试配置、`e2e/**`、报告守卫、`apps/api/scripts/run_web_e2e.py` | 真实浏览器→API→PG、生命周期、no-skip |
| **F：独立审查** | oracle | 默认只读 | 在同步核心和最终集成两个闸口审查吞稿、身份、游标和未决结果 |
| **Orchestrator** | 主调度 | `src/domain/**`、`src/api/contracts.ts`、manifest/锁文件、Vite/TS/ESLint配置、CI、README/docs | 接口冻结、共享文件合并、最终完整验收、提交与发布 |

串行关系：

```text
R 核查 → Orchestrator 冻结接口/测试脚手架
                    ↓
          A ∥ B ∥ C骨架 ∥ D组件骨架 ∥ E设施
                    ↓
            A/B稳定 → C真实集成
                    ↓
               Oracle闸口
                    ↓
            D业务接入 → E全链路
                    ↓
     主调度复核改动文件 → 最终门禁 → 发布
```

规则：

- C 可先用仓储/会话接口测试替身实现纯状态机，但真实集成必须等 A/B 稳定。
- D 不拥有同步算法；C 不直接修改 UI。
- 共享文件由唯一 owner 写入，其他车道提交修改清单。
- 子代理不自行提交、推送或启动不受控 PG。
- 子代理总结不是验收证据；主调度合并前读取其修改文件和实际测试报告。

---

## 十、六个 Conventional Commits 与逐步验收

新增脚本将在第 1 步定义为稳定入口：

- `pnpm test:web`：单测及组件测试。
- `pnpm test:e2e`：生命周期 runner + Playwright。
- `pnpm check:web-reports`：Web 报告守卫。
- `pnpm check:api-reports`：复用既有 API 分组报告规则。

以下命令均为 **Build 时执行**；PowerShell 如拦截 `pnpm.ps1`，使用 `pnpm.cmd`，不修改执行策略。

### Commit 1

**`chore(web): define phase4 client contracts and verification tooling`**

内容：确认后落盘计划、共享接口、测试脚手架、依赖锁定、代理修复、脚本与报告格式。

```powershell
uv sync --locked
pnpm install --frozen-lockfile
pnpm lint:web
pnpm typecheck:web
pnpm test:web -- tests/unit/contracts tests/unit/proxy
pnpm test:markdown
pnpm build:web
```

依赖新增时正常更新锁文件，提交前以 frozen 安装重新验证。若候选测试框架要求升级 Vite，暂停确认，不顺带升级。

### Commit 2

**`feat(web): persist local drafts intents and recovery records`**

内容：IndexedDB、账号/工作区分区、草稿/意图事务、冻结操作存储、恢复记录和导出。

```powershell
pnpm lint:web
pnpm typecheck:web
pnpm test:web -- tests/unit/storage
pnpm test:web
pnpm build:web
```

### Commit 3

**`feat(web): add coordinated login and rotating sessions`**

内容：API客户端、登录/`/me`、可选记住登录、refresh inflight、退出及账号隔离。

```powershell
pnpm lint:web
pnpm typecheck:web
pnpm test:web -- tests/unit/auth
uv run pytest apps/api/tests/api/test_auth.py -v
```

最后一条须在真实 PG 环境执行；**Build 先核实认证测试实际文件名**，若不同，使用当前仓库等价认证测试集合，不创建空路径或用零收集通过。

### Commit 4

**`feat(web): implement durable native sync and safe reconciliation`**

内容：八操作调度、冻结命令、六状态、重试、pull、snapshot、重建与单写协调。

```powershell
pnpm lint:web
pnpm typecheck:web
pnpm test:web -- tests/unit/sync
pnpm test:web
uv run pytest apps/api/tests/api -m "api and not api_concurrency and not transport" -v
pnpm build:web
```

提交前完成 Oracle 同步风险闸口。

### Commit 5

**`feat(web): connect workspace editing and manual conflict recovery`**

内容：真实树和列表、编辑器、源码保护、保存状态、人工冲突、删除恢复与响应式交互。

```powershell
pnpm lint:web
pnpm typecheck:web
pnpm test:web -- tests/components
pnpm test:markdown
pnpm build:web
pnpm test:e2e -- --grep "workspace_flow|markdown_preservation|manual_conflict|mobile_layout"
```

E2E 用例标签和报告清单在第 1 步固定；上述为对应选择表达式，不允许选择零用例成功。

### Commit 6

**`test(web): enforce phase4 postgres e2e and release gates`**

内容：全链路承重用例、CI、no-skip、镜像依赖、README 与实际验收记录。

最终门禁：

```powershell
uv sync --locked
pnpm install --frozen-lockfile

uv run ruff check apps/api
uv run ruff format --check apps/api
uv run pytest apps/api/tests -v --junitxml=api-full.xml

pnpm lint:web
pnpm typecheck:web
pnpm test:markdown
pnpm test:web
pnpm build:web

pnpm --filter @revnote/web exec playwright install chromium
pnpm test:e2e
pnpm check:web-reports
pnpm check:api-reports
```

并执行：

- 当前 CI 的 Python 精确分组与承重名称守卫。
- 固定 `ty==0.0.94`，覆盖现有生产检查路径及新增 Python runner，不使用全局 ignore。
- 普通测试库上的 Alembic `upgrade head/current/check`。
- 专用迁移库往返验收。
- API/Vite/浏览器以及临时 PG 的停止确认。
- 暂存差异、凭据、生成物、命名及文件范围审查。

本地完整测试和 CI 分组存在必要重复：完整回归覆盖最终状态，分组报告证明发布门禁未被绕过。除此之外不因纯文案修改机械重复全部耗时验收。

---

## 十一、环境、提交与发布硬约束

Build 必须原样继承：

- 模型统一 `murasameai-vip/gpt-6.1-sol#high`。
- Murasame-PC 无 Docker；**本地不执行 docker build/compose build，也不要求本机 Docker 跑验收**。
- 真实 PG 用仓库外临时实例和一次性库，所有路径用完停止。
- 禁止 SQLite，不以 skip 通过验收。
- 不触碰 Clash 配置，不调整代理规避工具问题。
- 不提交 `.env`、凭据、认证状态、测试数据库目录或未审查浏览器产物。
- 测试、代码及提示遵守命名禁词要求，统一采用 `secret_*` 等命名。
- 不改写已发布历史，不强推。

当前 shell 无法直接解析 `git`；**Build 前置门禁必须先定位 Git/gh 可执行文件、确认分支和工作区状态**。不能依据 ref 文件推断工作区干净，也不能覆盖现有用户修改。

发布流程：

1. 各步本地验收后形成六个提交。
2. 最终全套通过、临时服务清理完成后，一次性 push。
3. 精确匹配本次最终 SHA 的 Actions run：

```powershell
git push origin main
gh run list --repo LeiSureLyYrsc/Revnote --branch main --limit 5
gh run watch <本次SHA对应的run-id> --repo LeiSureLyYrsc/Revnote --exit-status
```

4. CI 失败：

```powershell
gh run view <run-id> --repo LeiSureLyYrsc/Revnote --log-failed
```

只追加修复提交，重跑受影响本地门禁后推送修复；不强推、不用旧 run 绿色结果替代本次验收。

---

## 十二、风险、限制与本阶段不做

### 主要风险与应对

| 风险 | 应对与剩余限制 |
|---|---|
| 浏览器存储被清理、驱逐或用户删除 | 提示“此设备保存”不是永久备份；提供显式导出，不承诺断电级持久性 |
| refresh 服务端提交与本地落盘之间崩溃 | 保守重登；不以重试旧 token 追求无感恢复 |
| 写标签关闭时请求仍在服务器执行 | 接管先确认冻结命令，不因拿到新锁就生成替代命令 |
| TipTap 破坏未支持 Markdown | 源码真相、加载不写回、显式转换和恢复点；不承诺任意 Markdown 富文本无损 |
| snapshot/pull 超限 | 明确暂停并保全本地数据；大工作区仍可能不能 bootstrap |
| 单写标签长期隐藏 | 不自动抢占；显示写权所在状态，允许正常交接，不引入复杂 lease |
| 本地恢复记录持续增长 | 显式清理与空间告警，不自动淘汰脏稿；容量压力可能要求用户处理 |
| 明文缓存与可选 refresh 受 XSS 威胁 | 无原始 HTML 注入、危险链接防护、不持久化密码；不宣称本地加密或生产安全闭环 |
| 公网滥用与运维未完成 | 受控预览；公网发布仍需 TLS、真实限流、同源部署、备份恢复验收 |

### 本阶段不做

- MinIO/S3 附件及上传/确认/GC。
- Joplin Server 协议桥。
- 自动三方合并、CRDT、自动上传冲突副本。
- 服务端独立 Outbox、WebSocket、实时协同。
- snapshot 分页、事件清理、幂等窗口收缩。
- 搜索、时间机器、完整历史正文。
- E2EE、OIDC、Passkey、成员管理、会话管理中心。
- PWA、Service Worker、全离线冷启动、后台同步。
- 递归删除、恢复旧 ID、跨对象原子操作。
- 前端视觉重构、性能工程或为消除 chunk 警告扩范围。
- 本地镜像构建及完整生产部署。

回退时不自动删除 IndexedDB 或降级服务端 schema。旧客户端不能理解新本地版本时应停止并提示，不清库；涉及协议或本地 schema 的修复优先前向兼容。

---

## 十三、批准记录（原确认入口）

**用户已批准完整范围与 D1–D18 推荐默认值，本轮直接 Build，不再请求确认。**

批准内容：Phase 4 按 D1–D18 推荐方案执行。

**批准现已生效，当前为 Build orchestrator。** 不存在待确认的产品决策；原附件的确认入口已由此批准记录替代。
