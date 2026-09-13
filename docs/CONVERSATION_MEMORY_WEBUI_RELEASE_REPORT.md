# Conversation Memory WebUI + NAS Release Report

日期：2026-09-13
仓库：`kettly1260/research-memory-gateway`
正式版本：`v0.2.2`
Release commit：`27688936c4bcf0d7e864a44fe8b56895fb3c8d83`

## 1. 发布结论

**PRODUCTION CUTOVER: PASS**

- GitHub `main` 已包含 Conversation Memory、MCP、WebUI Conversations 与 Source Identity。
- GitHub Actions Linux tests：PASS。
- GHCR multi-arch build：PASS。
- NAS 已切换到 `ghcr.io/kettly1260/research-memory-gateway:v0.2.2`。
- Conversation Memory 在 NAS 上自包含运行，不依赖 Windows Vault、Windows SQLite 或 `D:/`、`G:/` 路径。
- 仅部署既有 35-session pilot；剩余 287 sessions 未继续导入。

## 2. Release 修复历史

### v0.2.0

- GitHub tag 已发布，但 fresh CI resolve 到 MCP SDK 2.x。
- 项目仍使用 MCP v1 API（`mcp.server.fastmcp`），导致 pytest collection failure。
- 后续固定依赖为 `mcp>=1.2.0,<2`。

### v0.2.1

- MCP 依赖门控修复后，GitHub Tests / GHCR 均通过。
- NAS Linux 原生导入暴露 ext4/Unix 单文件名组件 255-byte 限制。

### v0.2.2

- Conversation Markdown 文件名按 UTF-8 bytes 控制长度。
- 原子写入临时文件名不再复制超长目标文件名。
- manifest 对无法 stat 的 output path fail-closed，避免错误处理阶段二次崩溃。
- 本地全量测试：186 passed。
- GitHub Linux Tests：PASS。
- GHCR Docker build：PASS。

## 3. NAS 原始数据与自包含边界

NAS raw export：

`/mnt/user/appdata/research-memory-gateway/exports/imports/codex-sessions-20260912-012538.zip`

SHA-256：

`E1A853E494856163A0CC7493DE4EC0C73480487A7A1EFB9C2C902E83CB97018E`

与 Windows immutable raw ZIP 一致。

生产 Conversation archive：

`/app/exports/conversations-v022-canary`

生产 Conversation index：

`/app/data/conversation-index-v022-canary.sqlite`

虽然目录名保留 `canary`，该目录现已作为已验证 production pilot 数据层使用；没有依赖 Windows path。

## 4. 35-session pilot 导入验收

NAS 原生第一次导入：

- written: 35
- failed: 0
- Markdown files: 35
- 最长文件名：239 UTF-8 bytes

第二次导入：

- skipped: 35
- written: 0
- failed: 0

说明 import idempotency PASS。

## 5. Conversation index / embedding 验收

最终生产 index 状态：

- documents: 35
- sections: 1,750
- embeddings: 1,553
- embedding model: `bge-m3`
- embedding version: `v1`
- dimension: 1,024
- SQLite `quick_check`: `ok`

最终 changed-only index：

- files_scanned: 35
- indexed_files: 0
- skipped_unchanged: 35
- new_embedding_requests: 0
- embedding_failures: 0
- dimension_mismatches: 0

曾尝试将 Conversation index 激活为 `bge-m3-i8`，但 NAS 模型服务在连续请求中出现 timeout / server disconnect；该尝试已回滚到完整 `bge-m3` active identity。遗留的少量 i8 historical cache 不参与 active `bge-m3` 检索。

## 6. 主 Research Memory DB

旧生产 DB 已保留并用于回滚：

`/app/data/research_memory.db`

新生产 DB：

`/app/data/research_memory.v022.db`

新 DB 从切换前一致性备份构建，并强制重新生成 `bge-m3` embeddings：

- memories: 4
- embeddings: 4
- dimensions: 1,024 × 4
- service_errors: 0
- dimension_mismatches: 0
- SQLite `quick_check`: `ok`

## 7. 生产配置

新生产主配置：

`/mnt/user/appdata/research-memory-gateway/config.v022.yaml`

新 WebUI runtime 配置：

`/mnt/user/appdata/research-memory-gateway/data/web_config.v022.yaml`

关键有效配置：

- `conversation_archive.enabled: true`
- `retrieval.mode: hybrid`
- embedding model: `bge-m3`
- embedding base URL: `http://192.168.22.102:28001/v1`
- embedding endpoint: `/embeddings`
- Conversation staging/index 均指向 NAS local persistent volume
- `vault_root: null`
- config 中无 `D:/` / `G:/` Windows runtime path

旧 `config.yaml` / `web_config.yaml` 未覆盖，继续作为 rollback 资产保留。

## 8. Reranker 状态

旧 runtime 配置的 `/rerank` endpoint 返回 404；测试 `/v1/rerank` 时模型服务返回 503。

因此 v0.2.2 production runtime 已将 rerank 显式关闭：

- lexical + vector hybrid：启用
- rerank：关闭

这样避免每次检索产生无意义的 retry latency；Conversation hybrid search 本身不受影响。

## 9. Production functional smoke

容器：

`research-memory-gateway`

镜像：

`ghcr.io/kettly1260/research-memory-gateway:v0.2.2`

WebUI：

- `/admin` unauthenticated -> 303 login redirect
- `/admin/conversations` unauthenticated -> 303 login redirect

MCP：

- unauthenticated `/mcp` -> 401，符合 Bearer auth 预期

生产配置下 direct service smoke：

- backend embedding model: `bge-m3`
- backend embedding URL: `/v1`
- rerank enabled: false
- `conversation_search("Fe3+")`: 3 results，`fallback_to_lexical=false`
- top hit conversation: `01a08c1c-b49b-70a2-b1a4-cad68e4b1cd4`
- top hit source identity: `codex / user`
- `conversation_read`: PASS，返回完整 Markdown
- `conversation_recall`: PASS，3 items，来源身份保留

生产容器最近日志未发现 traceback / error。

## 10. Rollback

旧生产容器仍保留为停止状态：

`research-memory-gateway-pre-v023`

更早的 v0.2.2 发布前回滚点也仍保留：

`research-memory-gateway-pre-v022`

切换前主 DB 备份：

`/mnt/user/appdata/research-memory-gateway/data/research_memory.pre-v022.db`

旧主配置与旧 WebUI runtime 配置未覆盖。

因此如需回滚，可停止/移除当前 v0.2.3 容器并重新启动
`research-memory-gateway-pre-v023`；无需 Windows 在线。

## 11. 安全与清理

- GitHub 未提交 NAS config、SQLite、Vault、raw ZIP 或 secrets。
- 用于复制旧容器环境变量的临时 env 文件已删除。
- 一次性导入剩余 287 sessions；未再按 Batch 3/4/5 拆分。
- 临时 batch-embedding 预填脚本已从 NAS 与本地 scratch 清理，未提交产品代码。
- 未复制 Windows conversation-index.sqlite 到 NAS。

## 12. 最终状态

```text
GitHub v0.2.3                    PASS
GHCR v0.2.3                      PASS
NAS self-contained archive       PASS
322-session import               PASS
repeat full-export dedup 322/322 PASS
322-session lexical/FTS index    PASS
35-session BGE-M3 vector cache   PASS
progressive hybrid retrieval     PASS
Conversation search/read         PASS
Conversation recall              PASS
Source Identity                  PASS
WebUI Conversations route        PASS
Windows-independent runtime      PASS
Production cutover               PASS
Rollback preserved               PASS
```

## 13. v0.2.3 全量 322 场导入稳定性验收

用户要求不再按 50/100/137 分批，而是一次性导入剩余 287 场，以验证未来处理
Codex、ChatGPT 等“不可选择、每次导出全部历史”的数据包时是否稳定。

### 13.1 第一次整包 dry-run

同一个 322-session Codex ZIP 对已有 35-session archive 执行整包 dry-run：

```text
total             322
unchanged          35
new_conversation  287
conflict             0
failure              0
```

### 13.2 第一次真实整包导入

```text
total             322
skipped            35
written           287
unchanged          35
new_conversation  287
conflict             0
failure              0
```

### 13.3 第二次相同整包重复导入

```text
skipped    322
written      0
unchanged  322
conflict      0
failed        0
```

这证明当前 Codex importer 对“再次导出完整历史再导入”的常见工作流具有整包幂等性；
不需要用户在导出时手动挑选新会话。

### 13.4 v0.2.3 continued-conversation path stability

v0.2.3 增加了 canonical path stability：当相同 `conversation_id` 在后续完整导出中
继续产生新消息，甚至标题变化时，`source_changed` 会更新原 canonical Markdown，沿用
manifest 中已登记的 output path，而不是按新标题生成第二份 Markdown。

回归测试同时覆盖：

- continuation 内容写入原 note；
- 原 output path 不变；
- manual region 保留；
- 最终仍只有一份 Markdown；
- manifest 指向原 canonical path。

## 14. 322 场索引结果与渐进 Hybrid 策略

v0.2.3 全量 lexical/FTS index 首次运行：

```text
files_scanned       322
indexed_files       287
skipped_unchanged    35
new chunks        24519
embedding_enabled  false
failures               0
```

加上原 35 场的 1,750 sections，最终数据库计数为：

```text
conversation_documents     322
conversation_sections    26269
conversation_sections_fts 26269
```

第二遍正式 index：

```text
files_scanned       322
indexed_files         0
skipped_unchanged   322
failures               0
```

已有 BGE-M3 vector cache 保留，当前 active vector coverage 为：

```text
embedded conversations     35 / 322
active vector sections   1750 / 26269
```

检索实现允许 lexical-only section 与 vector-backed section 同时参加融合，因此生产采用
progressive hybrid：322 场全部立即可被 FTS 检索，原 35 场继续获得 vector/hybrid 增强；
未嵌入的新 287 场不会因为没有 vector 而从结果中消失。

实测新增会话 `01a07eb4-b10c-7011-9705-ac89c398c195`（《审计 Research-AI-Hub
状态》）在 embedding client 开启时仍可 lexical 命中，`fallback_to_lexical=false`，来源身份为
`codex / user / Codex Desktop / vscode / gpt-5.6-sol`。原 35 场中的 Fe3+ 查询仍能返回
vector results。

## 15. NAS SQLite / Vector 扩容发现

### 15.1 `/mnt/user` 不适合大型 SQLite

112 MB 的 full index 在 `/mnt/user/appdata/...`（Unraid `fuse.shfs`）上执行普通 count 或
完整性扫描时出现明显 I/O stall，检查进程可进入 Linux `D` state / `folio_*` 等待。

同一数据库从实际物理路径
`/mnt/disk3/appdata/research-memory-gateway/data/conversation-index-v023-full.sqlite`
读取可正常完成：

```text
documents 322
sections  26269
fts       26269
quick_check ok
```

`PRAGMA quick_check` 在直盘路径耗时约 136 秒，但成功完成。因此生产 v0.2.3 将
`/app/data` 直接 bind 到 `/mnt/disk3/appdata/research-memory-gateway/data`，绕过 shfs/FUSE。

当前 Unraid `appdata` share 设置为 `shareUseCache="no"`，没有 cache/pool；未来如增加 SSD/pool，
应优先把 SQLite data 迁到 pool 的真实路径，而不是重新放回 `/mnt/user`。

### 15.2 当前 full-vector backfill 不宜继续扩大

322 场 dry-run 估算：

```text
markdown files                322
chunks                      26269
unique embedding identities 19712
new vectors still needed    18159
```

BGE-M3 `/v1/embeddings` 已验证支持 array input 批量请求，但真实长 chunk 压测显示 batch size
过大可发生长时间等待；小 batch 可稳定续跑但吞吐仍不足。更重要的是当前
`search_vector()` 会从 SQLite 取出匹配向量，在 Python 中逐条 brute-force cosine。

因此现在不应为了“覆盖率 100%”盲目生成约 20k vectors。随着 vector 数量增长，查询本身也会
线性变慢。下一阶段应先实现真正的 ANN/vector index，或至少 lexical candidate prefilter +
bounded vector scoring，再继续大规模 embedding backfill。

## 16. 当前去重边界与未来多平台设计

### 16.1 已实现：同来源稳定 ID 的精确去重

当前 import manifest 以 `conversation_id` 查找已有记录，并结合 source entry SHA-256、archive
SHA-256、parser/schema version、managed output hash 与 attachment inventory 做决策。

因此同一个平台只要提供稳定 conversation ID：

1. 同 ID + 同 source hash：直接 skip；
2. 同 ID + 相同内容但来自新的整包 ZIP：仍 skip，可标记为同会话出现在新 archive；
3. 同 ID + 内容增长/变化：`source_changed`，更新同一 canonical note；
4. v0.2.3 即使标题变化，也沿用原 canonical path，不产生 orphan duplicate。

### 16.2 尚未实现：Codex / ChatGPT / Claude / Gemini 之间的 canonical dedup

目前 `conversation_id` 仍是 importer 的一等身份。不同平台若给同一逻辑会话分配不同 ID，
即使文字高度相似，也不会自动合并。这是有意的安全边界：复制粘贴到另一个 Agent 的相同文本
并不一定代表同一 provenance record。

在接入下一种 importer 前，建议增加内部 canonical identity 层：

```text
canonical_conversation_id
source_system
source_account_namespace_hash
source_conversation_id
source_thread_id / branch_id
first_seen_archive_sha256
last_seen_archive_sha256
normalized_transcript_sha256
message_set_hash
```

并实施分层去重：

1. **Exact source identity**：`source_system + account namespace + source_conversation_id`，自动去重/更新；
2. **Exact transcript fingerprint**：仅在 provider ID 缺失或不稳定时，对完全一致的规范化 transcript 自动去重；
3. **Continuation/prefix detection**：同来源、同账号、同 branch 且 B 是 A 的严格 continuation 时更新同一 canonical conversation；
4. **Near duplicate**：MinHash/SimHash/semantic overlap 只标记 `possible_duplicate`，不得自动 merge；
5. **Cross-source duplicate**：跨 Codex/ChatGPT 等默认保留独立 source record，只有强证据或人工确认后链接到同一个 canonical conversation。

这样以后用户可以持续导入“整包历史”，而不需要在导出阶段做选择；系统负责精确跳过已见 snapshot、
更新 continued conversation，并把不确定的跨平台重复交给 review，而不是冒险误合并。
