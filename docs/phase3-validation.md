# Phase 3 验收记录

日期：2026-10-29。仅记录实际运行证据；尚未执行的最终门禁不视为通过。

## 环境与边界

- Windows/Python3.12，uv0.13.6；真实 PostgreSQL 临时实例位于仓库外，由外部 start/custom/stop 控制器运行，所有验收批次 finally stop。
- 两个不同的可丢弃库分别承载业务与迁移；REQUIRE_POSTGRES_TESTS=1；无SQLite替代、无skip验收。
- 未执行本机docker build/compose build，未修改Clash，未提交.env或凭据，未改前端业务。

## 逐提交证据

1. `ff19de6 feat(api): define v1 contracts and harden write cancellation budgets`：精确暂存快照导出至仓库外，530项unit与164项真实PG kernel通过、0skip；生产横切/内核增量Ruff格式/ty通过。覆盖真实57014、锁等待超时指标、提交前/中取消及invalidate。
2. `dcf1469 feat(auth): add session token and owner membership schema`：103项schema unit通过，53项PG迁移/连接检查中52项首次通过；唯一失败为CHECK总数误写20（实际19），修正后该用例在新PG实例通过。升降级、旧数据保留、成员回填、邮箱碰撞拒绝及alembic check有实际PG证据。
3. `5e8332b feat(auth): implement password login and rotating token sessions`：88项认证专项unit通过，35项真实PG HTTP认证测试连续两次通过。首次PG运行发现无ORM关系时token INSERT早于session导致23503，显式flush session后修复，新增3项顺序回归测试；未将初次失败隐藏为通过。
4. `1435122 feat(sync): expose ordered independently transactional push`：142项push/中间件unit通过；真实PG push与读取联合专项28项通过，包含批内重复零执行、逐op事务、完整错误对象、unknown同op重试及ID冲突来源隐藏。
5. `4e47d7a feat(sync): add consistent pull bootstrap and read endpoints`：147项读取/应用契约unit通过；同上28项PG HTTP联合专项通过。另独立真实PG并发+socket专项7项通过，包括dense seq、并发pull、N+1不可先见、RR单视图与真实TCP断连。

## 已可复用的 Web 回归

未改动Web源码。pnpm lint:web、typecheck:web、test:markdown（7项）、build:web全部exit0。既有859kB入口chunk警告仍存在，不影响构建；本阶段不增加前端优化范围。

## 最终门禁

全部实现与测试写者收敛后，以下命令均实际exit0。最终完整回归为 **1157 passed、0 skipped、0 failed、0 errors**（312.40s）。

| 门禁 | 结果 | 耗时 |
|---|---|---|
| uv sync --locked | 锁文件一致 | — |
| Ruff check / format --check apps/api | 全部通过（111文件） | — |
| ty==0.0.94 check，计划§14.2增量生产路径 | 全部通过，无全局ignore | — |
| unit | 867 passed | 7.52s |
| integration/kernel（完整） | 164 passed | 124.80s |
| CI普通PG组 | 129 passed | 75.99s |
| CI内核并发组 | 48 passed | 54.27s |
| api and not api_concurrency and not transport | 64 passed | 24.99s |
| api_concurrency | 6 passed | 6.26s |
| transport（真实uvicorn TCP） | 3 passed | 4.94s |
| integration/test_migrations.py | 40 passed | 148.89s |
| pytest apps/api/tests -v | 1157 passed | 312.40s |

逐一解析实际JUnit：所有9个报告均非零、skipped/failure/error均0；同时从工作流提取required-name集合核对，missing均为空。包含真实57014、lock_wait指标、提交前/中取消、六状态与部分批次unknown重试、dense seq、并发pull、N+1可见性、RR单视图、快照整次拒绝、20MiB旧压缩正文拒绝、ID来源隐藏、秘密哨兵、成员存在性隐藏、并发refresh单赢家且family撤销已持久化、规范化邮箱注册竞争、真实socket断开及提交后丢失响应。

验收使用仓库外临时PG和两个独立库，整个批次finally stop成功。测试耗时仅为验证环境数据，不是生产性能指标。Web未改，沿用本轮已通过的四项回归证据。

## 发布

本地门禁已通过；本记录随第6个提交发布，随后执行一次push，并以gh run watch --exit-status实际结果认定CI完成。最终交付提供对应运行链接；本地全绿不冒充尚未完成的远端CI结果。

## 风险复核

认证/同步核心的独立风险审查未发现需要改变锁定决策的设计缺口。审查后将登录拆成短读取事务→无连接Argon2验证/rehash→锁用户重查状态并签发，避免匿名登录排队占满数据库连接池；对应8项连接持有/TOCTOU专项测试通过。读取字节预检采用逻辑内容长度，不以压缩存储大小估算；预检不是PG执行器内存硬上限，巨型旧压缩数据仍可能有解压成本，验收不声称RSS已受限。
