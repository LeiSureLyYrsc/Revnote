# Phase 8 参考 CLI（`revnote`）

> 状态：**已实现**。原生 `/api/v1` 的第二官方消费者，与 Web 共用同一套协议；不实现 Joplin 协议方言，不引入第二对象存储。
> 计划与决策：`docs/plan-phase8.md`（D1–D16 已按推荐默认值锁定）。
> 实际验收证据：`docs/phase8-validation.md`（只写实跑结果）。

---

## 1. 范围

`apps/cli` 是一个 uv workspace 成员，distribution `revnote-cli`，import 包 `revnote_cli`，console script `revnote`。它调用：

- 认证：`POST /api/v1/auth/login|refresh|logout`、`GET /api/v1/me`
- 只读：`GET /api/v1/workspaces/{id}/tree`、`GET .../notes/{note_id}`、`GET /api/v1/capabilities`
- 同步：`GET .../sync/snapshot`（Phase 7 分页）、`GET .../sync/pull`、`POST .../sync/push`
- 附件（最小闭环）：Phase 5 的代理上传/下载/元数据端点

**不做**：自动三方合并、冲突自动选边、Joplin 协议客户端、预签名 URL、附件删除/GC、后台守护进程、TUI、PyPI 发布流水线、公网加固。

---

## 2. 安装与入口

在仓库根目录：

```powershell
uv sync --locked
uv run --package revnote-cli revnote --help
```

`uv sync` 会把 `revnote` 入口装进共享 `.venv`；激活 `.venv` 后也可直接 `revnote ...`。本阶段**不**要求发布到 PyPI。

环境变量：

| 变量 | 作用 |
|---|---|
| `REVNOTE_API_BASE` | API 根地址；默认 `http://127.0.0.1:8000` |
| `REVNOTE_CONFIG_DIR` | 凭据目录；测试与脚本必须指到临时目录（本机用 `D:\tmp\...`） |

优先级：命令行 `--api-base` > `REVNOTE_API_BASE` > 已保存的地址 > 默认值。

---

## 3. 凭据

`login` 成功后写入 `<配置目录>/config.json`：

- Windows 默认 `%APPDATA%\revnote`；
- 其他平台 `$XDG_CONFIG_HOME/revnote` 或 `~/.config/revnote`；
- `REVNOTE_CONFIG_DIR` / `--config-dir` 覆盖。

文件以临时文件 + `os.replace` 原子替换；POSIX 上尽力设为 `0o600`。内容含 access/refresh token、session id、默认 workspace id；**不含密码**。`logout` 撤销会话并删除文件；网络失败时也删除本地凭据，但进程以 5 退出，避免把「未完成撤销」伪装成成功。

CLI 永不打印 token、密码或凭据文件内容；测试会断言 stdout/stderr 不含保存的 access token。任何命令都可用 `--json` 输出机器可读结果。

---

## 4. 命令

全局选项可放在子命令前或后：`--api-base`、`--config-dir`、`--workspace`、`--json`、`--timeout`、`--version`。

| 命令 | 作用 |
|---|---|
| `login --email E [--password P \| --password-stdin] [--device-name N]` | 登录并保存 token；只有一个 workspace 时记为默认 |
| `logout` | 撤销当前 session 并删除本地凭据 |
| `refresh` | 显式轮换 refresh token |
| `me`（别名 `whoami`） | 当前用户与 workspace 列表 |
| `tree` | 活跃文件夹 + 笔记元数据（无正文） |
| `note get NOTE_ID` | 一篇笔记的正文与 rev |
| `capabilities` | 部署能力探测（无需登录） |
| `sync snapshot [--page-size N]` | bootstrap 活跃对象集；有分页能力时自动拼页 |
| `sync pull [--since SEQ] [--limit N] [--once]` | 增量事件与对象；默认连续拉完游标 |
| `sync push --ops-file FILE` | 提交一批原始操作（1–50 个，八个内核形状） |
| `sync push note create --title T [--body-file F \| --body-text S] [--folder-id ID] [--entity-id ID]` | 创建一篇笔记 |
| `sync push note update NOTE_ID --base-rev R --title T [--body-file F \| --body-text S]` | 更新一篇笔记 |
| `attachment upload PATH [--workspace ID] [--mime TYPE] [--name NAME]` | 建 pending 行并代理上传（发送 `X-Content-SHA256`） |
| `attachment download ATTACHMENT_ID --output PATH` | 流式下载到文件 |
| `attachment info ATTACHMENT_ID` | 附件元数据 |

workspace 解析顺序：`--workspace` > 已保存默认 > `/me` 返回的唯一成员 > 报错（`workspace_required`，退出 2）。不会猜测。

---

## 5. 输出与 JSON 契约

默认输出人读文本；`--json` 输出**每个进程一个 JSON 文档**到 stdout（不是 JSON Lines）。错误在 stderr：`--json` 时是 `{"error": {code, message, request_id?, retryable?, details?}}`，文本模式是 `revnote: <code>: <message>`。

所有 `seq`/`rev`/`cursor` 按服务端协议保持十进制字符串；CLI 不转成数字，避免脚本里的精度损失。分页游标是不透明字符串，CLI 原样回传，不解析。

主要 JSON 形状：

```jsonc
// login
{"user": {...}, "workspaces": [{"id": "...", "role": "owner"}],
 "workspace_id": "...", "expires_in": 600, "session_id": "...", "session_expires_at": "..."}

// tree
{"workspace_id": "...", "folders": [...], "notes": [...]}

// note get
{"workspace_id": "...", "id": "...", "folder_id": null, "title": "...",
 "body_md": "...", "rev": "2", "updated_seq": "7", "created_at": "...", "updated_at": "..."}

// sync snapshot（paged 或 legacy 同一外壳）
{"workspace_id": "...", "as_of_seq": "9", "min_retained_seq": "1",
 "objects": [...], "has_more": false, "next_cursor": null, "pages": 3, "paged": true}

// sync pull
{"workspace_id": "...", "since": "9", "next_cursor": "11", "last_seq": "11",
 "min_retained_seq": "1", "has_more": false, "events": [...], "objects": [...], "pages": 1}

// sync push（原样保留服务端 results；create 便捷命令额外给 created_note_id）
{"workspace_id": "...", "results": [{"op_id": "...", "status": "applied",
  "seq": "10", "rev": "1", "error": null}], "created_note_id": "..."}

// capabilities
{"attachments": {"enabled": false, "max_bytes": 26214400, "accepted_mime": [...]},
 "joplin_bridge": {"enabled": false, "protocol_baseline": "3.7"},
 "snapshot_pagination": {"enabled": true, "page_max_objects": 1000,
  "page_max_bytes": 4194304, "max_total_objects": 100000}}
```

`sync snapshot` 的 `paged` 表示本次是否走了分页；`pages` 是实际请求页数。legacy 路径返回 `paged=false`、`pages=1`。

---

## 6. 退出码（冻结）

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 未预期的内部错误 |
| 2 | 用法/参数错误（argparse 也用 2） |
| 3 | 认证失败：凭据错误、refresh 失效、未登录 |
| 4 | 冲突：任何 409 协议码（`rev_conflict` 等），或 push 结果里出现 `conflict` |
| 5 | 其它 API/网络失败（含 `rejected`/`unknown`/`not_attempted` 的 push 结果） |
| 6 | 未找到（`not_found`） |

Push 是特例：服务端对整批返回 `200` + 每 op 的 `status`。CLI **不会**把 `conflict` 当成功：它在 stdout 保留全部 results，在 stderr 写对应的 `{"error": ...}`，并以 4 退出。`rejected`/`unknown`/`not_attempted` 以 5 退出。CLI 不自动重试、不自动合并、不覆盖远端。

---

## 7. 分页 snapshot 规则

1. 先 `GET /api/v1/capabilities`（无需 token）。
2. `snapshot_pagination.enabled == true` 时强制分页；`--page-size` 覆盖请求页大小，但不得超过 `page_max_objects`（超过即退出 2，不发请求）。
3. 未给 `--page-size` 时使用 `min(page_max_objects, 500)`。
4. 每页带 `limit`（续页再带 `cursor`），循环到 `has_more == false`。
5. 首页的 `as_of_seq` 被钉住；任何续页返回不同水位即 `invalid_response`（退出 5），绝不把不一致的页拼成一个状态。
6. 跨页对象按 `(entity_type, id)` 去重；重复即 `invalid_response`。
7. `has_more=true` 但 `next_cursor` 为空、或页面为空却声称还有更多，均视为协议错误。
8. 上限 1000 页；超出即 `snapshot_page_budget`（退出 5），防止坏服务端把 CLI 拖死。
9. 无分页能力且未显式给 `--page-size` 时走 legacy 无参 snapshot；服务端 409 `snapshot_too_large` 原样以退出 4 报告。

`sync pull` 同样链接 `next_cursor`，默认拉完（`--once` 只取一页）；`min_retained_seq` 取已见页的最大值。

---

## 8. 附件

- 上传走两阶段：`POST .../attachments` 建 pending → `PUT` 服务端返回的**相对** `upload.url`。没有预签名 URL。
- CLI 在 PUT 时发送 `X-Content-SHA256`（本地复算的 SHA-256）、声明 MIME 与长度；服务端仍以回读复算为准。
- 下载走 `GET /api/v1/attachments/{id}/content`，流式写临时文件后原子替换目标文件。
- 附件不是内核实体：不产生 `seq`、不出现在 `tree`/`pull`/`snapshot`。
- 本阶段附件 CLI 的自动化覆盖是 HTTP 协议级单测（mock transport）；真实 RustFS 的存储证明仍由 `api-attachments` job 的 API 测试承担，未新增 CLI 的真存储自动化用例（见验收记录「未运行」）。

---

## 9. 明确不做

- 自动三方合并 / CRDT / 冲突副本上传 / 自动选 local 或 remote。
- Joplin 协议客户端或 Data API 客户端。
- 预签名直传/直读、附件删除、GC 调度。
- 后台同步守护进程、TUI、桌面/移动端。
- 公网加固（TLS、真实限流、备份）。
- PyPI 正式发布、桌面安装器。
- 第二对象存储（MinIO 或其它）；RustFS 仍是唯一存储。
