# Revnote 第二阶段 Plan 任务：正式领域模型 + 事务写路径（只出计划，不改代码）

> 状态说明：以下为原始规划任务，现已在会话中获准进入 Build，确认方案见 `plan-phase2.md`。本说明只澄清阶段状态，不改写原任务。

你现在处于 Plan 模式（指挥官）。**本轮只产出实施计划，不写业务代码、不改文件、不提交、不推送。** 计划需要我确认后才进入 Build。

## 背景
- 仓库 D:\Github\Revnote（GitHub: LeiSureLyYrsc/Revnote），脚手架已完成且 CI 全绿（API/Web/GHCR）。
- 请先阅读：`README.md`、`docs/plan-scaffold.md`（含领域表草图与 P0 不变量）、`docs/build-conventions.md`、`docs/consensus-brief.md`、`docs/stack-blueprint.md`，以及本次新增的 `docs/phase2-sync-kernel-spec.md`（规格，优先级最高）。
- 现状：数据库只有空 Alembic 基线，无业务表；无 CRUD、同步、认证。

## 本阶段目标
实现 Sync Kernel 的最小可验证核心，且只做这一块：
1. 正式领域模型与 Alembic 迁移（users / workspaces / folders / notes / change_events / op_ledger，范围以规格文档为准）。
2. 单对象写路径：工作区行锁分配 `seq`；对象更新与 `change_events` 同事务；`op_id` 唯一约束幂等；`rev` CAS 乐观并发；软删除与 tombstone。
3. 针对真实 PostgreSQL 的并发与不变量测试。
4. 内核以 service 层/仓储层形式存在，**不做** HTTP 路由、认证、附件、Joplin、E2EE、CRDT、前端改动。

## 硬约束（沿用并强化）
- 不使用 SERIAL/SEQUENCE 作为同步游标；`seq` 在 `SELECT ... FOR UPDATE` 锁住 workspace 行后分配。
- 对象变更与 change_events 必须同一事务提交或回滚。
- 幂等靠数据库唯一约束（如 `(workspace_id, op_id)`），不靠应用层先查后写。
- 只用 PostgreSQL，不用 SQLite 替代；测试用独立可丢弃库（`TEST_DATABASE_URL`），skip 不算通过。
- `body_md` 是持久化正文真相，TipTap JSON 不入库作为真相。
- BIGINT 的 `seq`/`rev` 在 JSON 中的表示方式按规格文档决定（本阶段只在内核内保留 int，不暴露 API）。
- 本地禁止 docker build / docker compose build；镜像只走 Actions→GHCR。验证时先 push，再 `gh run watch --exit-status`，失败用 `gh run view <id> --log-failed`。
- 不提交 `.env`、凭据；不改动 Murasame-PC 上的 Clash 代理。

## 指挥官分工建议（你来编排，可调整）
- 子任务 A：领域模型与迁移设计（表、约束、索引、可逆迁移）。
- 子任务 B：写路径与事务边界设计（锁顺序、错误语义、重试策略）。
- 子任务 C：测试设计（并发、幂等重放、回滚原子性、CAS 冲突、tombstone），并明确哪些测试在 CI 的 PG 服务容器里跑。
- 子任务 D：CI 增量与文档更新计划。
最后由你汇总成一份统一计划，处理子任务间的冲突。

## 计划必须包含
1. 目标文件/目录清单（路径级别），及每个文件的职责。
2. 完整表结构：字段、类型、约束、索引、外键、`ON DELETE` 行为；迁移分几个 revision、是否可降级。
3. 写路径时序：事务开始→锁 workspace→校验 `op_id`→校验 `rev`→写对象→写 change_events→提交；每一步失败时的结果与返回给调用者的错误类型。
4. 对规格文档中"待决问题"逐条给出推荐方案与理由，并标出需要我拍板的点（不要自行默认）。
5. 测试矩阵：用例名、前置、断言、是否需要多连接并发；说明如何证明"并发下 seq 无空洞无重复"。
6. 分阶段提交计划（建议 3~4 个 commit，沿用 conventional commits）与每步验收命令。
7. 风险与回滚：锁竞争热点、长事务、迁移不可逆风险。
8. 明确"本阶段不做"的清单。

## 输出要求
- 中文，结构化，先给结论摘要再给细节。
- 完整计划先在回复中给出；我确认后再保存为 `docs/plan-phase2.md`。
- 有歧义或需要取舍的地方，列出选项让我选，不要擅自决定。
- 结尾问我：是否按计划进入 Build。
