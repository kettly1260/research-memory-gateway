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

`research-memory-gateway-pre-v022`

切换前主 DB 备份：

`/mnt/user/appdata/research-memory-gateway/data/research_memory.pre-v022.db`

旧主配置与旧 WebUI runtime 配置未覆盖。

因此如需回滚，可停止/移除新容器并重新启动 `research-memory-gateway-pre-v022`；无需 Windows 在线。

## 11. 安全与清理

- GitHub 未提交 NAS config、SQLite、Vault、raw ZIP 或 secrets。
- 用于复制旧容器环境变量的 `/tmp/rmg-v022.env` 已删除。
- 未继续 Batch 3。
- 未导入剩余 287 sessions。
- 未复制 Windows conversation-index.sqlite 到 NAS。

## 12. 最终状态

```text
GitHub v0.2.2                 PASS
GHCR v0.2.2                   PASS
NAS self-contained archive    PASS
35-session import             PASS
35-session index              PASS
BGE-M3 vector retrieval       PASS
Conversation search/read      PASS
Conversation recall           PASS
Source Identity               PASS
WebUI Conversations route     PASS
Windows-independent runtime   PASS
Production cutover            PASS
Rollback preserved            PASS
```
