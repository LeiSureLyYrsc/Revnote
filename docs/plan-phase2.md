# Phase 2 实施计划：Sync Kernel 最小核心（已确认）

本计划在会话中经用户确认进入 Build。它与本次 Phase 2 规格优先于上一阶段“不能建业务表、不能实现内核”的限制；本地禁止构建镜像、不改 Clash、只用 PostgreSQL 等约定继续生效。原始任务要求保留于 `phase2-sync-kernel-spec.md`，其中 Plan 模式措辞是历史上下文，不阻止此次已授权实施。

## 1. 范围与验收

做：六领域表、可逆迁移、单对象事务写 service、真实 PostgreSQL 并发与不变量测试、CI/文档更新。

不做：新 HTTP 路由、认证/授权、附件、MinIO 业务、Joplin、E2EE、CRDT、合并、前端、批量/跨工作区操作、恢复删除、递归删除、清理任务、独立 revision/tombstones/outbox 表。

users/workspaces 是基础数据，只建模与测试准备，不纳入笔记变更流，也不提供注册或工作区管理产品接口。已有 hello/health 保持不变。Kernel 完成不代表 Web 可以保存或完整同步协议已完成。

本阶段完成需 Ruff、pytest（含真实 PG，无 skip 作为验收）、Alembic 往返、Actions 全绿。README 只记录真实执行的结果与限制。

## 2. 已确认 D1–D8

| 决策 | 已确认方案 |
| --- | --- |
| D1 | 应用侧 UUIDv7，统一 helper；可新增 uuid-utils，使用标准库 UUID 兼容接口。Python3.12 不假定 uuid.uuid7 存在；DB 无 UUID 生成默认。UUID 不是同步顺序。 |
| D2 | Kernel 内 int；未来对外 JSON seq/rev 用十进制字符串。本阶段无 HTTP serializer。 |
| D3 | parent_id 邻接树；移动只更新根并发一条 move 事件，后代 rev/updated_seq 不变；锁内校验循环。 |
| D4 | 同 key 异命令 OpIdConflict；带版本规范化命令 JSON 做 SHA-256；账本只保存成功结果。 |
| D5 | 调用方提供 entity_id/op_id，调用前可用统一 helper 生成；重试保持两者。 |
| D6 | 单工作区单对象，一成功操作一事件；非空文件夹禁止删，不隐式级联。 |
| D7 | deleted_at+updated_seq 是 tombstone；workspaces.min_retained_seq DEFAULT1 占位，本阶段不推进、不清理，ledger/event保留。 |
| D8 | 接受每工作区串行写，不预承诺生产吞吐；记录锁等待/事务耗时与结果诊断，不接监控平台、不优化锁。 |

额外确认：新 op 对已删对象更新/移动/再删返回 EntityDeleted；原删除 op 重放返回首次结果。同名允许。值未变化的合法更新/移动仍成功并递增 rev/seq。接受 ledger 末尾完整 INSERT 和对象 updated_seq→event 的延迟外键。保留 move，不加工作区软删除。

## 3. 文件与职责

```text
apps/api/src/revnote_api/
  db/base.py                       修正 column_0_N_name，显式命名 CHECK
  models/{__init__,user,workspace,folder,note,change_event,op_ledger}.py
  kernel/{__init__,commands,errors,fingerprint,ids}.py
  services/{__init__,sync_write,sync_repository}.py
apps/api/migrations/env.py          显式导入 models，再使用 Base.metadata
apps/api/migrations/versions/0002_sync_kernel_core.py
apps/api/tests/unit/                metadata、命令、hash、分层与迁移链
apps/api/tests/integration/conftest.py
apps/api/tests/integration/test_migrations.py
apps/api/tests/integration/kernel/  写、幂等、CAS、原子性、并发、树、隔离
.github/workflows/ci.yml            两库、no-skip、精确表集、往返
pyproject.toml                     markers；公共工具仍在根
apps/api/pyproject.toml / uv.lock   UUIDv7依赖
README.md / apps/api/README.md / docs/build-conventions.md
```

不建通用仓储框架；kernel不导入FastAPI/HTTP/adapters，service是唯一公开写入口。models显式注册，不能靠偶然导入顺序。

## 4. 完整表结构与约束

通用：UUID实体主键，change_events/op_ledger按规格复合PK为例外。时间TIMESTAMPTZ，created_at默认CURRENT_TIMESTAMP。整数同步列BIGINT，不用SERIAL/IDENTITY/nextval。外键显式ON DELETE，默认RESTRICT，无ORM级联；延迟事件FK用NO ACTION。正文TEXT真相，不做Markdown归一化。时间不是游标。

### users
- id UUID PK NN；email TEXT NN UNIQUE 非空；created_at TIMESTAMPTZ NN DEFAULT now()。
- 不含认证字段，不额外定义邮箱大小写归一，按PG TEXT唯一语义。

### workspaces
- id UUID PK NN；owner_id UUID NN FK users.id ON DELETE RESTRICT。
- last_seq BIGINT NN DEFAULT0 CHECK>=0。
- min_retained_seq BIGINT NN DEFAULT1 CHECK>=1 且 min_retained_seq-1<=last_seq。
- created_at TIMESTAMPTZ NN DEFAULT now()；owner_id索引。

### folders
- id UUID PK NN；workspace_id UUID NN FK workspaces.id RESTRICT。
- parent_id UUID NULL；name TEXT NN 非空白。
- rev BIGINT NN DEFAULT1 CHECK>0；deleted_at TIMESTAMPTZ NULL。
- updated_seq BIGINT NN CHECK>0，无默认。
- created_at TIMESTAMPTZ NN DEFAULT now()；updated_at TIMESTAMPTZ NN，service显式设置。
- UNIQUE(workspace_id,id)。FK(workspace_id,parent_id)→folders(workspace_id,id) RESTRICT。
- CHECK parent_id IS NULL OR parent_id<>id。
- 索引(workspace_id,updated_seq)、(workspace_id,parent_id)。

### notes
- id UUID PK NN；workspace_id UUID NN FK workspaces.id RESTRICT。
- folder_id UUID NULL；title TEXT NN；body_md TEXT NN，均允许空字符串。
- rev BIGINT NN DEFAULT1 CHECK>0；deleted_at TIMESTAMPTZ NULL。
- updated_seq BIGINT NN CHECK>0，无默认。
- created_at TIMESTAMPTZ NN DEFAULT now()；updated_at TIMESTAMPTZ NN，service显式设置。
- FK(workspace_id,folder_id)→folders(workspace_id,id) RESTRICT。
- 索引(workspace_id,updated_seq)、(workspace_id,folder_id)。

### change_events
- workspace_id UUID NN FK workspaces.id RESTRICT；seq BIGINT NN CHECK>0 无默认。
- PK(workspace_id,seq)，此PK索引覆盖游标扫描，不重复建同列索引。
- entity_type TEXT NN CHECK IN('folder','note')；entity_id UUID NN（多态引用不伪造普通FK）。
- op TEXT NN CHECK IN('create','update','delete','move')；rev BIGINT NN CHECK>0；op_id UUID NN。
- created_at TIMESTAMPTZ NN DEFAULT now()。
- UNIQUE(workspace_id,op_id)；事件只追加，无修改/删除service。

folders/notes另加FK(workspace_id,updated_seq)→change_events(workspace_id,seq) ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED，使对象先写、event后写可在commit验证存在。

### op_ledger
- workspace_id UUID NN FK workspaces.id RESTRICT；op_id UUID NN；PK(workspace_id,op_id)非延迟。
- request_hash BYTEA NN CHECK octet_length=32。
- result_seq BIGINT NN CHECK>0；FK(workspace_id,result_seq)→change_events(workspace_id,seq) RESTRICT。
- UNIQUE(workspace_id,result_seq)。created_at TIMESTAMPTZ NN DEFAULT now()。
- 无pending/status/失败账本/当前实体快照；由不可变事件重建首次结果。

所有CHECK显式命名。第一实现步骤修base.py的无效column_0_list_name为column_0_N_name，并加复合元数据构建测试。DB约束保证声明的引用与隔离，不独立保证多态event实体/rev完全对应；完整语义由统一service事务+测试保证，不承诺绕过service的任意SQL安全。

## 5. 命令、hash、结果、错误

八种命令：Folder Create/UpdateName/Move/Delete，Note Create/UpdateContent/Move/Delete。公共workspace_id、op_id、entity_id；创建无base_rev，其他必须正整数base_rev。内容更新不夹带移动，移动不夹带正文更新。

hash：带hash_schema_version，包含workspace/type/id/op/base_rev和全部业务字段。UUID标准小写；固定键排序、分隔符、UTF-8；默认先展开、null明确；整数无损十进制形式；不trim正文、不统一换行、不Unicode归一，不接受NUL/NaN/未知类型。op_id是外部key不必纳入hash。改base_rev属于新命令，使用新op_id。

结果：不可变workspace_id/op_id/entity_type/entity_id/op/seq/rev/created_at，不返回ORM实体。重放同值，不读当前对象假冒历史；如需replayed诊断放在结果外，不改变结果。不承诺旧正文重放，不加revision表。

错误：InvalidCommand、WorkspaceNotFound、EntityNotFound、EntityIdConflict、OpIdConflict、RevConflict(expected/current)、EntityDeleted、InvalidParent、FolderCycle、FolderNotEmpty、CounterOverflow、WriteBusy、StorageFailure、CommitOutcomeUnknown。全是Python契约，不预埋HTTP码。

## 6. 单事务写路径

service接session factory，每调用独立AsyncSession，READ COMMITTED，自己拥有一个事务，不与session_scope双重提交，不共享并发session。

1. 锁外结构校验与hash。
2. BEGIN；固定READ COMMITTED并SET LOCAL lock_timeout=5s、statement_timeout=10s（Phase3 P2补充）；SELECT workspace FOR UPDATE，先于任何业务行锁。
3. 查ledger：同hash→由result_seq事件重建结果（早于CAS）；异hash→OpIdConflict。
4. workspace限定读取实体/父节点；校验存在、deleted、base_rev、循环/非空。
5. 检查BIGINT溢出，seq=last_seq+1更新工作区。
6. 创建rev1或条件UPDATE(workspace,id,rev,deleted_at)，检查rowcount，rev+1、updated_seq=seq。
7. INSERT event。
8. INSERT完整ledger ON CONFLICT DO NOTHING RETURNING，result_seq NOT NULL。
9. COMMIT，延迟FK通过后才返回成功。

正常路径同ws锁使step8竞争通常不可达，但DB唯一是兜底；event/ledger唯一冲突不能提交候选写，先回滚整笔，再新事务读ledger比hash重放/冲突。存在孤立event无合法ledger则报一致性错误。不覆盖旧结果。此兜底必须构造测试。

新op错误优先级：结构→工作区→ledger→实体存在→deleted→base_rev→父/树业务校验→分号写。合法no-op也发事件。删除soft标记+rev/seq，不物理删；原delete重放稳定，新op再删EntityDeleted；非空folder拒绝，无恢复、无ID复用。目标parent必须同ws且活动；树检查在ws锁内。

失败在提交前整事务回滚，不留对象/event/ledger/last_seq变化。RevConflict/OpIdConflict/业务错误不自动重试；超时可原命令重试，不加隐藏无限重试。COMMIT确认丢失不等于回滚：CommitOutcomeUnknown，调用方保留原op_id确认结果。锁内不作网络、格式转换、日志正文或外部服务。

Phase3 P2补充：57014/55P03确认失败映射WriteBusy，锁等待指标在finally包含超时/取消/缺失工作区路径。不shield业务COMMIT；CancelledError原样上抛，提交期间取消按unknown处理，可疑连接async invalidate而非归还池。HTTP断开也不证明回滚，客户端保留同op_id+同payload重试。新execute_with_outcome仅额外标记applied/replayed，原execute/WriteResult不变，详见[Phase3计划](plan-phase3.md)。

## 7. 迁移与保留边界

保留0001_baseline不变，新增唯一0002_sync_kernel_core。先users/workspaces/folders/notes/events/ledger，再ALTER增加延迟event FK。逆序先删FK再删六表。无种子、扩展、RLS、trigger、create_all。

降级schema可逆但数据不可恢复；只在可丢弃库往返，已有业务库不自动downgrade。有数据回退优先前向修复/备份。

延迟event FK与ledger引用限制未来事件清理：本阶段min_retained_seq不推进，未来清理需设计所有引用和幂等窗口，不能只改数字删历史。

## 8. 真实PG测试矩阵

两个显式不同的独立可丢弃库：TEST_DATABASE_URL用于普通与并发；TEST_MIGRATION_DATABASE_URL专用于DDL往返。schema由Alembic创建，不用SQLite/create_all，不自动降级普通库。无参数可本地skip但不能作验收；CI/本次验收缺任一URL必须失败。NullPool测试engine、每任务独立session/实际连接，barrier不受pool限制；有界超时，Event/Barrier/pg_blocking_pids编排，不sleep猜时序。

| 用例 | 证据 | 多连接 |
| --- | --- | --- |
| create/update/move/delete | seq1、rev1或+1，对象updated_seq/event/ledger一致 | 否 |
| N=8,M=20 seq密集 | 空ws160成功，sorted(seq)==1..160，count==distinct==last_seq==ledger_count==160 | 是 |
| 同op并发 | 仅一event/ledger，结果同值 | 是 |
| 同op异内容 | 单方成功其余OpIdConflict，无额外seq | 是 |
| 重放优先CAS/删除 | 陈旧base或后续已删仍返原结果 | 否 |
| CAS竞争 | 不同op同base，仅一成功其余RevConflict | 是 |
| CAS失败零写 | 对象/event/ledger/last_seq全不变 | 否 |
| tombstone | 行保留deleted_at，原delete重放，新op更新/移/再删均EntityDeleted | 是/否 |
| 树自环/间接环 | 失败零写；相反并发move至多一成功 | 是/否 |
| 子树move | 仅根一event，后代rev/seq不变 | 否 |
| 非空folder删除 | 活动子folder/note使删除失败，无级联 | 否 |
| ws隔离 | service限定与真实复合FK，B不被A命令改变 | 是/否 |
| workspace无全局锁 | A锁未放，B可完成 | 是 |
| DB唯一兜底 | 裸SQL约束+构造末尾ledger冲突验证全事务回滚 | 是/否 |
| BIGINT | >2^53无损，溢出不写 | 否 |
| 故障矩阵 | seq更新/对象/event/ledger后及commit前抛错，新连接四元组不变，再试无空洞 | 是 |
| 提交确认丢失 | 提交成功结果未交付，原op重试稳定 | 是 |

提交可见性三连接：T1写完未提交；T2真实阻塞（pg_blocking_pids观测）；T3看不到未提交写或更大号。T1提交后T2取下一号；另测T1回滚后T2复用该候选号。

迁移库：空→baseline仅版本表→head恰六表+版本表→baseline业务表全无→再head；重复upgrade；alembic check无diff；catalog核对PK/FK/check/index/deferrable/无nextval；独立哨兵用例确认不误删无关表。

故障注入仅测试monkeypatch/包装真实仓储SQL，生产无调试端点或env failpoint。N*M用独立对象避免CAS竞争污染成功计数，结果完成后新连接完整扫描，不从返回顺序猜提交顺序。

## 9. CI与旧断言

改test_db metadata空、test_alembic_config仅基线/禁止models/kernel断言为正向6表/单head/分层，仍不新增业务HTTP/adapters。test_pg_connectivity迁移降级移专库。

CI准备两个库；unit→普通PG+并发→迁移专库；JUnit每组非零、无skip/error/failure，关键并发用例收集检查。精确表集users/workspaces/folders/notes/change_events/op_ledger+alembic_version；baseline仍仅版本表。无需硬编码总测试数。Web原job不改，image仍needs api/web全绿。

## 10. 四提交与命令

所有四步本地通过才推送一次；不强推、不改写已推历史。每步提交前运行命令并保留真实输出。迁移/PG测试必须配置两个独立URL。

1. `feat(kernel): add core domain schema and migration`
   - 先保存本计划和用户spec；base命名修复、六表/迁移、meta/旧断言、迁移测试/最低CI schema调整。
   - `uv sync --locked`；`uv run ruff check apps/api`；`uv run ruff format --check apps/api`；`uv run pytest apps/api/tests/unit -q`；`uv run pytest apps/api/tests/integration/test_migrations.py -v`。
2. `feat(kernel): implement transactional single-object writes`
   - 命令/hash/结果/错误、service/repository、CAS/重放/树/软删、顺序PG测试。
   - `uv run pytest apps/api/tests/unit -q`；`uv run pytest apps/api/tests/integration/kernel -m "integration and not concurrency" -v`；受影响Ruff。
3. `test(kernel): prove concurrency and rollback invariants on postgres`
   - N*M/重放/CAS/三连接/故障/ledger兜底/ws隔离。
   - `uv run pytest apps/api/tests/integration/kernel -m concurrency -v`；`uv run pytest apps/api/tests -v`；Ruff。
4. `ci: enforce phase2 postgres acceptance and document kernel limits`
   - 两库/no-skip/精确schema与报告、README实测结果、文档与范围收口。
   - 最终Ruff+真实PG全pytest+往返+CI等价验证（复用未变代码证据，不因文案重复全套）。

获准推送后`gh run list --repo LeiSureLyYrsc/Revnote --branch main`取run，`gh run watch <id> --repo LeiSureLyYrsc/Revnote --exit-status`到完成；失败`gh run view <id> --log-failed`修复并追加提交，不改已推历史。本地不docker build。最终报四commit哈希、实际验收摘要、Actions链接结论与剩余限制。

## 11. 风险、诊断、回滚

- 热点：每ws串行，锁内短事务；记录workspace_lock_wait_ms/transaction_duration_ms与成功/重放/CAS/op冲突/超时，不承诺未实测吞吐。不接监控平台，不记录正文/邮箱/secret，高基数ws不作指标标签。
- session新建避免ORM身份缓存旧值；SQL条件写兜底。READ COMMITTED锁等待返回已提交最新行。
- 文件夹循环service校验、DB自环check；多态event对应靠唯一入口，非任意SQL安全。
- 保留无限增长有意，未来清理必须协调引用和幂等窗口。
- 跨ws隔离不是用户授权；本阶段不认证。
- 提交确认丢失只可原op重试确认，不谎称回滚。
- 生产降级破坏数据，禁止自动；测试库严格隔离，用完停止临时PG。
