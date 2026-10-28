# Revnote 五模型共识简报（2026-10-29）

## 可行性评分（同步内核）
| 模型 | 分 |
| DeepSeek | 7.0 |
| Grok | 7.5 |
| ChatGPT | 8.5 |
| Gemini | 8.2 |
| Kimi | 8.0 |
| **共识中位 ~7.8–8.0** | 建议立项，前提是砍范围 |

## 五家一致（P0）
1. 工作区级锁分配 seq，禁止 SERIAL 当游标
2. 首版只认证 PostgreSQL，不做真多库
3. Joplin = Server 协议适配器，非 Data API；非产品主路径
4. 不上 CRDT；整篇/块级三方合并 + 冲突副本
5. E2EE 与服务端搜索/明文编辑互斥
6. 对象更新与 change_events 同事务；op_id 幂等靠 DB 唯一约束
7. MinIO 不可变 + 事务外上传 + 宽限期 GC
8. 专用客户端走原生内核 API；Joplin 只是兼容桥

## 差异化共识候选（选 2）
- Sync Doctor / 时间机器（可解释变更）
- AST/块级三方合并 + Web 冲突 UI
- 开放变更订阅 / CAS 附件 / Passkey 签名锁（次选）

## 专用客户端
用户明确：Joplin 仅兼容，自有客户端必做。落地：先 Web + 开放 API + 参考 CLI，再桌面/移动。
