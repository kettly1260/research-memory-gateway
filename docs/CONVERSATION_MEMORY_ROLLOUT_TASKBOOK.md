# Conversation Memory Pipeline 正式 Rollout 与 322-Session 分批演练任务书

任务日期：2026-09-12  
执行项目：`G:\LLM\memory`  
建议执行 Agent：独立执行 Agent / Flash Agent  
任务性质：生产前 rollout、分批演练、门控验证；**不是新一轮架构 hardening**。

---

## 0. 使用方式与审计对话隔离

本任务书用于把后续部署/演练工作从当前审计对话中完全拆开。

执行 Agent 必须：

- 只读取本任务书及项目内已经落盘的实施报告、配置和代码。
- 不依赖当前审计聊天中的隐式上下文。
- 不把执行过程写回当前审计对话。
- 所有运行记录、统计、异常、人工抽检和最终结论写入独立报告：

```text
G:\LLM\memory\docs\CONVERSATION_MEMORY_ROLLOUT_REPORT.md
```

如需中间状态，可另写：

```text
G:\LLM\memory\docs\CONVERSATION_MEMORY_ROLLOUT_CHECKPOINT.md
```

当前审计对话只作为只读历史，不作为 rollout 状态载体。

---

## 1. 已独立确认的技术基线

以下状态已由前序独立审计确认，本任务不重复做大规模代码审计：

```text
175 passed, 3 warnings
git diff --check PASS
```

固定 5-session V3 验收：

```text
first import   = 5 written
second import  = 5 skipped
first index    = 5 indexed / 173 chunks
second index   = 5 skipped_unchanged
max chunk      = 1498 chars
over 1500      = 0
```

同时已确认：

- Real E2E 使用 `tmp_path`，不污染人工验收 staging。
- archive-local manifest contract 为 `<conversation_root>/.ai-memory/manifest.sqlite`。
- attachment allowlist 已接入 production ingestion。
- manual notes 属于 searchable conversation knowledge。
- manual edit 会触发 reindex，但不会触发 managed-content conflict。
- parent-thread retrieval、source anchor、guardian fixture 已通过。
- recall 最终 Agent context 受 budget 约束。
- embedding active identity / historical cache 已分离。
- `v1 -> v2 -> v1` rollback 已通过。
- NAS `bge-m3` 单次 smoke 已确认 1024 维 PASS。

生产前状态：

```text
A. lexical-only staging = READY
B. canonical Vault import = NOT READY（运行配置门控）
C. bulk BGE-M3 embedding = NOT READY（继承 B + 运行配置门控）
```

本任务第一目标：

> 用完整 322-session 数据做 lexical-only staging 全量演练，并通过分级门控决定是否进入 canonical Vault 和 embedding。

---

## 2. 不可违反的安全边界

除非后续阶段明确满足门控并得到用户显式批准，否则禁止：

- 直接将 322 场写入正式 canonical Vault。
- 在 322 场上批量调用 NAS embedding。
- 修改或重启 Unraid。
- 修改原始 export ZIP。
- 删除历史 Gateway 数据库或 embedding cache。
- 清理当前 dirty working tree。
- Git commit / push。
- 擅自修改正式 `vault_root`。
- 擅自扩展 `sources.allowlist` 到整个磁盘。

原始 ZIP 保持 immutable：

```text
D:\Partition\F\Study\博士文件\Research-AI-Hub\_codex_workspace\tmp\chat-export\codex-sessions-20260912-012538.zip
```

本任务允许：

- 全量 322-session **lexical-only staging**。
- staging 内 archive-local manifest/index。
- attachment inventory。
- FTS index。
- 统计、质量报告和人工抽检。
- 必要时对 staging 做可逆重建。
- 单次 NAS smoke。
- 用户批准后的极小批 canonical Vault / embedding canary。

---

## 3. 总体阶段与放行门控

严格按顺序：

```text
R0 Baseline freeze
-> R1 Runtime config draft
-> R2 322-session lexical-only staging import
-> R3 Full lexical index
-> R4 Attachment inventory audit
-> R5 Retrieval / lineage / provenance QA
-> R6 Manual spot-check
-> GATE-A lexical staging sign-off
-> R7 Canonical Vault config confirmation
-> R8 Canonical canary import
-> GATE-B canonical import sign-off
-> R9 Embedding runtime confirmation
-> R10 Small BGE-M3 canary
-> GATE-C embedding sign-off
-> R11 Optional staged production rollout
```

任一阶段失败：

- 立即停止下一阶段。
- 保留 staging 和报告。
- 不修改原始数据。
- 记录 failure reason、影响范围和建议修复。

---

## 4. R0：冻结执行基线

记录：

```text
repo root
branch
HEAD
git status --short
Python version
current config path
raw ZIP path
raw ZIP SHA-256
```

执行：

```powershell
python -m pytest -q
git diff --check
```

要求 `0 failed / 0 error`。若失败，只报告，不自动开启新一轮 hardening。

---

## 5. R1：建立 rollout 专用运行配置

不要直接把日常 `config.yaml` 作为唯一 rollout 配置。

建议创建：

```text
G:\LLM\memory\config.rollout.yaml
```

第一阶段只配置 lexical-only staging，例如：

```yaml
conversation_archive:
  enabled: true
  staging_dir: ./exports/conversation-staging/full-322-lexical
  vault_root: null
  canonical_subdir: 90_System/AI-Memory
  require_explicit_vault_confirmation: true
  index_path: ./exports/conversation-staging/full-322-lexical/.ai-memory/index.sqlite

retrieval:
  mode: keyword
  embedding:
    enabled: false

sources:
  allowlist:
    - type: local
      path: D:/Partition/F/Study/博士文件
      readonly: true
```

注意：

- `vault_root` 必须仍为 `null`。
- embedding 必须 disabled。
- allowlist 只允许真实需要读取的根。
- 不允许宽泛默认整个用户盘或系统盘。

R1 报告：

```text
resolved staging root
resolved index path
archive-local manifest path
embedding_enabled=false
vault_root=null
allowlist roots
```

---

## 6. R2：322-session lexical-only 全量 staging import

### 6.1 独立 staging

建议：

```text
G:\LLM\memory\exports\conversation-staging\full-322-lexical
```

必须从空目录开始，不复用旧验证目录。

### 6.2 先 audit-export

生成 `full-322-audit.json`，至少记录：

```text
archive SHA-256
total_sessions
package version
export timestamp
estimated subagents / child sessions
```

若实际 session 数不是 322，不伪装为 322；报告真实值并先确认差异。

### 6.3 第一轮 import

生成：

```text
full-322-import-first.json
```

期望：

```text
written ~= total_sessions
skipped = 0
conflict = 0
failed_retryable = 0
index_stale = 0
```

任何 failure 必须逐条列 conversation_id、reason、error。

### 6.4 第二轮幂等 import

不修改 note，立即重跑，生成 `full-322-import-second.json`。

期望：

```text
written = 0
skipped = total_sessions
conflict = 0
failed_retryable = 0
index_stale = 0
```

出现非预期 write 时，不进入 R3。

### 6.5 Import 统计

输出：

```text
notes total
notes by year
notes by thread_source
notes with parent_thread_id
notes without parent_thread_id
notes with attachments
notes with tool activity
notes with source anchors
failed sessions
candidate/conflict files
```

---

## 7. R3：322-session lexical index

### 7.1 首次 index

embedding 继续 disabled，运行 `index --changed-only`，保存：

```text
full-322-index-first.json
```

报告：

```text
files_scanned
indexed_files
skipped_unchanged
chunks_indexed
change_reasons
```

### 7.2 二次 changed-only

不改 note，再跑一次，保存：

```text
full-322-index-second.json
```

期望：

```text
indexed_files = 0
skipped_unchanged = files_scanned
chunks_indexed = 0
```

### 7.3 Chunk health

直接查询 SQLite：

```text
total sections
min chars
mean chars
median chars
p90
p95
p99
max chars
sections > 1500
sections = 0 chars
```

硬门槛：

```text
sections > 1500 = 0
```

### 7.4 Manifest/index 闭环

所有成功导入并索引的 conversation 应满足：

```text
last_indexed_at 非空
index_source_hash 非空
content_section_hashes 非空
```

异常项逐条报告。

---

## 8. R4：全量 Attachment Inventory 审计

生成：

```text
full-322-attachments.json
full-322-attachments.csv
```

统计：

```text
total records
found
missing
unresolved
embedded
remote
zip_entry
tool_artifact
unique canonical locators
duplicates collapsed
largest 20 found files
top unresolved roots
top missing roots
```

QA：

- allowlist 内 existing files 应正确 `found`。
- allowlist 外 path 保持 `unresolved`。
- 不允许读取超出 allowlist 的内容 hash。
- slash/backslash 不得产生重复 locator。

内容级增量仅用 synthetic temp fixture 复验：

```text
found A
-> content changed B
-> attachment_changed
-> file deleted
-> attachment_changed
```

不要修改用户真实附件。

---

## 9. R5：Retrieval / Lineage / Provenance QA

### 9.1 固定回归查询

至少保留：

```text
Fe3+
TOC_Focused.png
codex-mcp-config.dedup.toml
http://127.0.0.1:23120/mcp
01a08c1c-b49b-70a2-b1a4-cad68e4b1cd4
```

### 9.2 全量新增抽样查询

从全量 session 中分层抽至少 20 个真实查询，覆盖：

```text
file path
tool name
local URL
chemical term
error code
project name
parent thread UUID
distinct user wording
```

### 9.3 每个查询记录

```text
query
result count
top conversation_id
top heading
top score
top source anchors
first anchored result
parent_thread_id
thread_source
```

禁止把 `top result` 与 `first anchored result` 合并描述。

### 9.4 Recall budget

至少测：

```text
token_budget=10
100
1000
1500
```

验证最终 `context` 满足硬预算。

---

## 10. R6：人工抽检

至少抽 30 场：

```text
10 ordinary user threads
5 child/subagent/parent lineage
5 attachment-heavy
5 tool-heavy
5 long / compaction / unusual completion
```

每场检查：

- title/date。
- conversation_id。
- parent_thread_id。
- thread_source。
- user goal 是否未被 injected transcript 污染。
- source anchors 是否真实。
- tool activity 是否保留但未无限膨胀。
- base64 是否未直写 Markdown。
- attachment path。
- manual region。
- final result 与原始 conversation 是否基本一致。

至少 10 场必须从 Markdown anchor 回溯 raw ZIP 中对应 ordinal/message。

问题分级：

```text
P0 = 数据错配 / 写错会话 / 严重丢内容 / 路径安全问题
P1 = lineage/provenance 明显错误
P2 = 格式/摘要/可读性问题
P3 = cosmetic
```

Gate-A 要求：

```text
P0 = 0
P1 = 0
```

---

## 11. GATE-A：Lexical-only Staging Sign-off

只有全部满足才可写：

```text
A. lexical-only staging = READY
```

要求：

- pytest 全绿。
- 全量 import 第一轮稳定。
- 第二轮全部 idempotent skip。
- 无未解释 conflict / failed_retryable。
- index 第一轮完整。
- index 第二轮全 skip unchanged。
- max chunk <= 1500。
- manifest/index linkage 完整。
- retrieval 回归正常。
- recall final context budget 正常。
- attachment allowlist 正常。
- 人工抽检 P0=0、P1=0。
- raw ZIP 未修改。
- 无正式 Vault 写入。
- 无批量 embedding。

通过后报告：

```text
LEXICAL STAGING SIGN-OFF: PASS
```

---

## 12. R7：正式 Canonical Vault 运行配置确认

用户已于 2026-09-12 明确确认 Hub 根路径为：

```text
D:\Partition\F\Study\博士文件\Research-AI-Hub
```

并说明 Obsidian 只挂载 Hub 内的 `Vault` 目录。`hub.config.json` 同时声明：

```text
obsidian_vault = Research-AI-Hub/Vault
```

因此 Gate-B 正式 `vault_root` 固定为：

```text
D:\Partition\F\Study\博士文件\Research-AI-Hub\Vault
```

不得把 `Research-AI-Hub` 项目根本身当成 Obsidian Vault。

用户同时确认本轮 Gate-B 采用以下 production 参数：

```text
conversation_archive.enabled = true
vault_root = D:\Partition\F\Study\博士文件\Research-AI-Hub\Vault
canonical_subdir = 90_System/AI-Memory
sources.allowlist = D:\Partition\F\Study\博士文件（readonly）
retrieval.embedding.enabled = false
```

建议另建：

```text
config.conversation-production.yaml
```

输出解析后的：

```text
vault_root resolved path
canonical root resolved path
archive-local manifest path
index path
embedding enabled/disabled
allowlist roots
```

安全检查：

- `vault_root` 已存在。
- 不是自动新建空目录。
- `canonical_subdir` 位于 vault_root 内。
- `--vault` 缺 `--confirm-vault` 必须失败。
- `--staging-dir` 指向 vault/canonical 必须失败。

建议 production 配置：

```yaml
conversation_archive:
  enabled: true
  vault_root: D:/Partition/F/Study/博士文件/Research-AI-Hub/Vault
  canonical_subdir: 90_System/AI-Memory
  require_explicit_vault_confirmation: true

retrieval:
  mode: keyword
  embedding:
    enabled: false

sources:
  allowlist:
    - name: doctoral_research
      type: local
      path: D:/Partition/F/Study/博士文件
      readonly: true
```

注意：本配置确认 **不等于授权 322 场 canonical 全量写入**。只允许进入 R8 的 10-session Canary。

---

## 13. R8：Canonical Vault Canary Import

即使 Gate-A 通过，也禁止直接全量写 Vault。

本轮固定为 **10 场**，采用：

```text
5 场固定回归样本
+
5 场新增代表性样本
```

### 13.1 固定回归 5 场

```text
019e3ad1-05d6-7382-972f-0d377e6092c6
019eab7a-3a54-70b1-afd2-b89c0c98e8b2
01a08fdc-6da5-7f93-9119-ff79d9fea710
01a01289-e1b4-7242-98d9-368a75a16194
019ea2ad-9a74-75c2-bf10-4246ca00ab25
```

### 13.2 新增代表性 5 场

从已完成 322-session lexical staging QA 的样本中固定：

```text
019e3ba5  ordinary user thread / 普通基线
019ea610  parent/subagent lineage + 超长标题路径压力
01a09144  attachment-heavy
01a09459  tool-heavy
019f71ab  long / compaction / unusual completion
```

执行时必须从 manifest / raw export 解析并记录这 5 个短 ID 对应的完整 conversation UUID；如果出现短 ID 非唯一或无法解析，立即停止，不得猜测 UUID。

选择逻辑：

- 固定 5 场用于验证与 V3 已知基准完全一致的 canonical 行为。
- 新增 5 场用于覆盖普通线程、lineage、长标题、附件、工具密集、长对话/compaction 等 production 风险。
- 不使用纯随机样本替代上述固定名单。

先 dry-run，确认：

```text
target root
candidate paths
count
no unexpected overwrite
```

用户批准后执行 canary。

验收：

- expected files 写入 canonical_subdir。
- archive-local manifest 正确。
- manual/machine region 正常。
- 无意外覆盖其他 Vault note。
- 再跑 import 为 skip。
- retrieval/index 可定位 canonical note。
- 人工在 Obsidian 中检查至少 5 场。

---

## 14. GATE-B：Canonical Vault Sign-off

只有：

```text
Gate-A PASS
+
用户确认 production config
+
canonical canary PASS
```

才可写：

```text
B. canonical Vault import = READY
```

Gate-B 前禁止全量 canonical import。

---

## 15. R9：Embedding Production Config 确认

Gate-B 通过后再处理 embedding。

必须由用户确认：

```text
retrieval.embedding.enabled = true
base_url
model = bge-m3
embedding version
expected dimension = 1024
timeout / retries
```

批量前再执行一次：

```text
python scripts/smoke_bge_m3.py
```

只有：

```text
PASS
dimension=1024
exit code=0
```

才可继续。

---

## 16. R10：BGE-M3 小批量 Canary

不要直接对全量 session 做 embedding。

建议先：

```text
5 sessions
```

最多：

```text
20 sessions
```

必须记录：

```text
sessions
chunks
unique embedding identities
new embedding requests
cache hits/reused
failures
dimension mismatches
elapsed time
```

### 16.1 Embedding 统计必须可信

当前已知非阻塞问题：

```text
conversation_index_runs.embedded_chunks
```

在 embedding enabled 时可能把 `total_chunks` 当成实际新 embedding 数，从而高估 NAS 请求。

在任何批量 embedding 容量估算前，先检查是否已能区分：

```text
indexed chunks
new embedding requests
cache reused
embedding failures
```

如果仍高估，只做小型定向修复和回归测试，不开启新一轮大重构。

### 16.2 Canary 增量验证

同一批立即重跑：

```text
new embedding requests = 0
```

synthetic 单 chunk 改变：

```text
new embedding requests = 1
```

并继续验证：

```text
v1 -> v2 -> v1
cached rollback = 0 new calls
live identity = current version
metadata = current version
```

---

## 17. GATE-C：Bulk BGE-M3 Sign-off

只有全部满足：

```text
Gate-A PASS
Gate-B PASS
NAS smoke PASS
embedding canary PASS
same-batch rerun = 0 unnecessary calls
actual embedding request accounting trustworthy
no dimension mismatch
no mixed active versions
```

才可写：

```text
C. bulk BGE-M3 embedding = READY
```

---

## 18. R11：可选的正式分批 rollout

只有用户再次明确批准后执行。

### Canonical import 建议批次

```text
Batch 1: 10
Batch 2: 25
Batch 3: 50
Batch 4+: 50~100
```

每批检查：

```text
written
skipped
conflict
failed_retryable
index_stale
```

出现 conflict/failure，停止下一批。

### Embedding 建议批次

初始建议：

```text
10 sessions
25 sessions
50 sessions
then remaining
```

每批记录真实：

```text
chunks
new calls
cache reuse
failed calls
latency
dimension
```

所有批次必须可安全 resume。

---

## 19. Rollout Report 结构

最终生成：

```text
docs/CONVERSATION_MEMORY_ROLLOUT_REPORT.md
```

建议结构：

```text
1. Baseline
2. Runtime config
3. Raw export audit
4. Full-session staging import
5. Idempotency
6. Index/chunk health
7. Attachment inventory
8. Retrieval/provenance QA
9. Manual spot-check
10. Gate-A decision
11. Canonical config/canary
12. Gate-B decision
13. Embedding config/canary
14. Gate-C decision
15. Remaining risks
16. Explicitly NOT executed
```

不得写模糊“全部完成”，必须分别写：

```text
A. lexical-only staging: READY / NOT READY
B. canonical Vault import: READY / NOT READY
C. bulk BGE-M3 embedding: READY / NOT READY
```

---

## 20. 明确不算失败的事项

以下不是技术失败：

- B 因用户尚未确认 `vault_root` 而 NOT READY。
- C 因用户尚未批准 embedding 而 NOT READY。
- allowlist 外路径保持 unresolved。
- 已不存在的 clipboard 文件为 missing。
- lexical staging 阶段 embedding metadata 为空。
- Git dirty tree 保持不变。

---

## 21. 必须立即停止的条件

出现任一情况立即停止 rollout：

```text
raw ZIP hash 改变
staging 写到 Vault
canonical path 越界
unexpected overwrite
P0/P1 provenance mismatch
chunk > 1500
manifest/index 大量不一致
idempotent rerun 大量重新写入
embedding dimension != 1024
mixed active embedding versions
NAS 连续失败
attachment allowlist 越界读取
```

停止后只报告，不自行扩大修复范围。

---

## 22. 给执行 Agent 的直接指令

```text
在 G:\LLM\memory 当前工作树执行：

docs/CONVERSATION_MEMORY_ROLLOUT_TASKBOOK.md

这是 rollout / deployment-readiness 任务，不是第四轮架构 hardening。

你必须只依赖项目内文件：
- docs/CONVERSATION_MEMORY_IMPLEMENTATION_REPORT.md
- docs/CONVERSATION_MEMORY_ROLLOUT_TASKBOOK.md
- config / source code / tests

不要依赖或写回用户当前的审计聊天。所有执行记录写入：
docs/CONVERSATION_MEMORY_ROLLOUT_REPORT.md

第一阶段只允许 full-session lexical-only staging：
- 独立 staging root
- embedding disabled
- vault_root null
- archive-local manifest
- full attachment inventory
- FTS index
- retrieval/provenance QA
- 至少 30 场人工抽检

在 Gate-A 通过之前，禁止正式 Vault 写入和批量 embedding。

Gate-A 通过后，等待用户显式确认：
- conversation_archive.enabled
- vault_root
- canonical_subdir
- sources.allowlist

然后只做 5-10 场 canonical canary；不得直接全量写 Vault。

Gate-B 通过后，等待用户显式确认 embedding runtime config，然后先做 NAS smoke 和 5-20 场 BGE-M3 canary；不得直接 full-session embedding。

特别注意：批量 embedding 前检查 conversation_index_runs 的 embedding 统计是否反映真实 NAS 新请求，而不是简单把 total_chunks 当成 embedded_chunks。若仍高估，只做定向小修和测试，不开启新一轮大规模重构。

禁止：
- 修改原始 ZIP
- 修改/重启 Unraid
- commit/push
- 清理 dirty tree
- 未授权 Vault 全量写入
- 未授权 bulk embedding

最终报告必须分别给出：
A lexical-only staging READY/NOT READY
B canonical Vault import READY/NOT READY
C bulk BGE-M3 embedding READY/NOT READY
```

---

## 23. 本任务书的最终目的

本任务不是追求“一次性把所有 session 全部上线”。

目标是把生产 rollout 变成可停止、可验证、可恢复的过程：

```text
先 staging 证明数据质量
再 canonical canary 证明写入安全
再 embedding canary 证明向量生命周期安全
最后才分批放量
```

任何阶段都不能用“测试全绿”替代真实数据验收，也不能用“5-session canary 成功”直接替代全量 rollout 的分批监控。
