# Conversation Memory Pipeline 补漏与最终验收任务书

任务日期：2026-09-12  
执行项目：`G:\LLM\memory`  
建议执行 Agent：Flash Agent  
任务性质：针对 `CONVERSATION_MEMORY_IMPLEMENTATION_TASKBOOK.md` 的独立复核结果进行补漏；保留现有实现，不重构、不重做、不扩大任务边界。

## 0. 为什么需要本补漏任务

现有 `docs/CONVERSATION_MEMORY_IMPLEMENTATION_REPORT.md` 声称 P0-P8 全部完成、145 项测试通过。

独立复核确认：

- 当前仓库全部测试确实可以达到 `145 passed`。
- 5 篇 staging Markdown 的现有 `validate` 也可通过。
- Parser、Obsidian writer、FTS、向量缓存底层、hybrid retrieval、MCP surface 等基础框架已经存在，应继续复用。

但原任务书若干验收项尚未真正完成，且实施报告中“Exact Query 检索结果表”把 synthetic fixture 数据误写成了真实 staging 验证结果，因此当前不能认定 P0-P8 已最终验收。

本轮只修这些已经确认的差距。

## 1. 不可违反的边界

### 1.1 保留现有实现

不得删除、回退或平行重写现有 conversation 子系统：

```text
src/research_memory_gateway/conversations/
  models.py
  codex_export.py
  attachments.py
  manifest.py
  vault_writer.py
  pipeline.py
  chunking.py
  index.py
  retrieval.py
  cli.py
```

以及：

```text
scripts/conversation_cli.py
scripts/prototype_codex_conversations.py
scripts/smoke_bge_m3.py
tests/test_conversation_*.py
```

只做最小补丁。

### 1.2 原有安全门控继续有效

本轮不得：

- 全量导入 322 场会话。
- 向正式 Vault 批量写入。
- 批量调用 NAS embedding。
- 修改或重启 Unraid 服务。
- 删除现有 Gateway 数据库或旧 embedding。
- commit / push / release。
- 修改原始 export ZIP。

原始 ZIP 仍为 immutable：

```text
D:\Partition\F\Study\博士文件\Research-AI-Hub\_codex_workspace\tmp\chat-export\codex-sessions-20260912-012538.zip
```

正式 canonical Vault 路径仍未确认，保持门控。

### 1.3 验收证据必须分层

从本轮开始，所有报告中的验证结果必须明确区分：

1. `synthetic fixture test`
2. `real 5-session staging validation`
3. `optional live NAS smoke`

严禁把测试 fixture 中人为插入的 `P0-4`、`324 nm`、`60 min` 等字符串写成真实 staging 会话检索结果。

## 2. 已确认缺口清单

本轮必须修复以下问题。

### R1. canonical_subdir 配置未进入正式 Vault 路径

现状：

```yaml
conversation_archive:
  vault_root: ...
  canonical_subdir: 90_System/AI-Memory
```

但 `conversation import --vault` 当前直接把 `vault_root` 交给 writer，未进入 `canonical_subdir`。

风险：正式解锁 Vault 后可能写到 Vault 根下 `Conversations/`，与设计不一致。

### R2. staging / canonical 路径边界未形成统一安全解析

`validate_safe_path()` 存在，但 conversation archive 自身的 staging、canonical、index、manifest 路径解析没有形成统一约束。

要求避免：

- 配置 `../` 逃逸。
- canonical 子目录逃逸 vault root。
- index/manifest 意外落入未允许目录。

### R3. Attachment inventory 类型和去重未完成

当前主要覆盖：embedded、local path、remote URL 的部分情形。

原任务书还要求稳定区分：

- embedded data URI
- local absolute path
- ZIP entry
- URL
- tool/artifact reference
- unresolved locator

并且实际 inventory 中存在同一 Windows 路径的：

```text
D:/Partition/TEMP/...
D:\Partition\TEMP\...
```

重复记录。

### R4. Writer 的人工区域保留与 manifest 冲突检测互相打架

Writer 本身可以保留 `AUTOGEN_END` 后的人工内容。

但是 manifest 当前保存整文件 hash；用户只修改人工区域后，下一次 pipeline 会判断：

```text
managed_output_modified -> conflict -> .candidate.md
```

这不满足“人工区域和 machine-managed 内容长期共存”的目标。

同时人工 frontmatter 字段当前不能可靠保留。

### R5. Manifest 状态机未达到原任务书要求

原任务书要求：

```text
new
unchanged
changed_source
parser_upgrade
output_missing
managed_output_modified
attachment_changed
index_stale
failed_retryable
```

当前缺少真正的 `index_stale` 判定闭环。

并且：

- `attachment_inventory_hash`
- `content_section_hashes`
- `last_indexed_at`
- `embedding_model`
- `embedding_version`
- `embedding_dimension`

虽然 schema 已有，但正常 pipeline/index 流程并未完整回写和使用。

### R6. failed_retryable 记录路径可能二次失败

`ImportManifest.record()` 当前会对 `output_path` 直接计算文件 SHA-256。

如果失败发生在 output 尚未创建之前，异常处理再次调用 `record()` 可能再次失败，导致真正错误无法稳定落 ledger。

### R7. `conversation index --changed-only` 是无效开关

CLI 定义了 `--changed-only`，但当前 `cmd_index()` 仍遍历全部 Markdown 并执行 `index_file()`；没有根据 document/file hash 跳过 unchanged 文件。

### R8. 正常 CLI 索引流程没有接入 embedding client

底层 `ConversationIndexDatabase` 支持 embedding cache 和 dimension guard，但：

```python
cmd_index()
cmd_rebuild_index()
```

当前创建 `ConversationIndexDatabase(db_path)` 时没有配置 embedding client。

因此真实 CLI 流程实际上是：

```text
Markdown -> FTS
```

而不是：

```text
Markdown -> FTS + optional embedding
```

### R9. source anchor 在真实 staging index 中没有形成稳定闭环

真实 Markdown 中存在：

```html
<!-- source ordinal=... message_id=... turn_id=... -->
```

但对真实 staging 建立的 index 中，部分 section 的 `source_anchors_json` 为空。

必须修正 heading-aware chunking 的 anchor 归属逻辑，并确保每个从关键原始内容形成的 chunk 能回到 ordinal/message ID。

### R10. parent thread 查询未实现

原任务书固定查询集要求 parent thread 查询。

真实样本：

```text
conversation_id:
01a08fdc-6da5-7f93-9119-ff79d9fea710

parent_thread_id:
01a08c1c-b49b-70a2-b1a4-cad68e4b1cd4
```

当前搜索 parent ID 返回 0。

### R11. CLI 的增量/续跑能力不完整

原任务书要求批处理支持：

```text
config
session filter
limit
dry-run
resume
JSON report
```

当前各子命令支持不一致；`resume` 没有形成稳定行为，JSON report 也不是所有关键命令都有。

### R12. 实施报告真实验证部分需要重写

必须删除或改写当前把 synthetic fixture 当真实 staging 的 Exact Query 表。

真实 staging 验证只能报告真实存在的数据。

## 3. 执行顺序

按以下顺序执行：

```text
R0 baseline
-> R1 path safety
-> R2 attachments
-> R3 writer/manifest coexistence
-> R4 manifest lifecycle
-> R5 index/chunking/embedding
-> R6 retrieval/MCP
-> R7 CLI/resume/report
-> R8 real 5-session E2E validation
-> R9 report correction
```

每一阶段先补测试，再进入下一阶段。

---

## 4. R0：重新冻结基线

### 任务

1. 读取：

```text
docs/CONVERSATION_MEMORY_IMPLEMENTATION_TASKBOOK.md
docs/CONVERSATION_MEMORY_IMPLEMENTATION_REPORT.md
docs/EXPORT_FORMAT_AUDIT.md
docs/CONVERSATION_MEMORY_ARCHITECTURE_AUDIT.md
docs/CONVERSATION_INGESTION_PROTOTYPE.md
```

2. 记录：

```text
git root
branch
HEAD
git status
```

3. 运行完整测试。

### 验收

现有基线必须保持：

```text
145 passed
```

如果数量因新增测试增加，旧测试必须全部继续通过。

---

## 5. R1：路径与 canonical_subdir 修复

### 任务

为 `ConversationArchiveConfig` 增加明确的路径解析 API，建议至少包括：

```python
resolve_staging_dir(...)
resolve_canonical_root(confirmed=False)
resolve_manifest_path(...)
resolve_index_path(...)
```

`resolve_canonical_root()` 应返回：

```text
<vault_root>/<canonical_subdir>
```

要求：

- `vault_root` 必须已经存在。
- 未显式确认时拒绝 canonical write。
- `canonical_subdir` 解析后必须仍在 `vault_root` 内。
- staging/index/manifest 的相对路径必须以配置文件基准目录或项目基准目录稳定解析，不能依赖随机 cwd。
- 不自动创建一个新的 vault root。

修改 CLI：

```text
conversation import --vault --confirm-vault
```

必须写入：

```text
<vault_root>/<canonical_subdir>/Conversations/...
```

而不是 Vault 根目录。

### 新增测试

至少覆盖：

- canonical_subdir 正常解析。
- `canonical_subdir: ../escape` 被拒绝。
- staging `../escape` 被拒绝或明确限制在允许根。
- 不存在的 vault root 不会自动创建。
- `--vault` 没有 `--confirm-vault` 必须失败。

---

## 6. R2：Attachment inventory 完整化与 canonical dedup

### 任务

扩展 inventory locator schema：

```text
embedded
local_path
zip_entry
remote_url
tool_artifact
unresolved
```

不要只扫描 message.text；还要对已经 normalized 的 tool/attachment metadata 提取可解释 locator。

禁止保存大型 tool payload，只保存：

```text
ordinal
message_id / tool_call_id
locator_type
original_locator（必要时脱敏/截断）
canonical_locator
mime/extension
size
hash
status
resolved_path
```

### Windows 路径去重

同一 Windows 路径必须 canonicalize 后去重，例如：

```text
D:/Partition/TEMP/a.png
D:\Partition\TEMP\a.png
```

不得被计为两个 missing file。

但 report 中可以保留 `observed_locators` 记录不同原始写法。

### ZIP entry

如果 locator 明确指向当前 export ZIP 内 entry：

```text
status = found
locator_type = zip_entry
```

不得解压整个 ZIP。

### Tool/artifact

如果是工具产物 ID / artifact reference 而非本地路径：

```text
locator_type = tool_artifact
status = unresolved | referenced
```

具体枚举可自行定义，但要稳定、可测试。

### 验收

- 现有 5 场真实样本 inventory 能重新生成。
- 3 个临时 clipboard 路径不应因 `/` 与 `\` 形式被重复计数。
- base64 不进入 JSON/CSV。
- allowlist 外路径不读取文件内容。
- 至少有 unit fixture 覆盖全部 locator 类型。

---

## 7. R3：Writer 与人工修改长期共存

### 目标

区分：

```text
machine-managed content hash
manual content hash
whole file hash
```

Manifest 的冲突判断不能再只依赖整个文件 SHA-256。

### 任务

为 writer 增加稳定解析：

```python
parse_managed_regions(text)
compute_managed_hash(text)
extract_manual_regions(text)
```

最低要求：

1. `AUTOGEN_BEGIN ... AUTOGEN_END` 是机器管理区域。
2. 用户只修改 marker 外内容时：

```text
不应触发 managed_output_modified
```

3. 用户修改 marker 内内容时：

```text
必须触发 conflict
```

4. 冲突仍输出 `.candidate.md`，不得覆盖。

### Frontmatter

不要允许用户任意修改机器 provenance 字段，但允许保留一组人工字段，例如：

```yaml
manual_projects:
manual_topics:
manual_related:
manual_notes:
```

也可选择更合理的 `x_manual_*` 命名。

要求明确 machine-owned / manual-owned field 列表。

### 验收

- 用户仅编辑 Related/Notes：下一次 source changed 时机器区域可正常更新，人工区域保留。
- 用户编辑 machine-managed block：进入 conflict/candidate。
- 用户人工字段重复导入后不消失。
- 相同输入真正幂等，whole file hash 不变。

---

## 8. R4：Manifest 状态机和失败续跑闭环

### Schema 补充

建议增加或明确：

```text
managed_output_sha256
whole_output_sha256
manual_region_sha256
index_source_hash
last_indexed_at
attachment_inventory_hash
content_section_hashes
embedding_model
embedding_version
embedding_dimension
status
error
```

### `index_stale`

必须实现真正的判定：

当 Markdown 中机器管理内容已经变化，而对应 index 的 source/file/content hash 尚未更新时：

```text
status = index_stale
```

### attachment_changed

Pipeline 必须实际计算 inventory hash，并传给 manifest，而不是只在 API 中预留参数。

### failed_retryable

失败记录必须允许：

```text
output file 尚不存在
```

不得在异常记录阶段再次因 `_sha256_file(output)` 崩溃。

建议：

- `output_sha256` 允许 nullable / empty。
- 或 `record_failure()` 单独实现。

### 验收状态集

必须自动化测试覆盖全部：

```text
new
unchanged
changed_source
parser_upgrade
output_missing
managed_output_modified
attachment_changed
index_stale
failed_retryable
```

### 失败恢复测试

构造 writer 抛异常且 output 从未创建：

- pipeline 返回 `failed_retryable`。
- manifest 成功落盘错误。
- 第二次运行可以继续。
- 已成功 conversation 不重做。

---

## 9. R5：Heading chunking、changed-only 与 embedding 接线

### 9.1 修复 source anchor 归属

当前真实 staging index 存在 section `source_anchors_json=[]` 的情况。

修复原则：

- anchor 应跟随其后的 message heading/content。
- anchor 不应被前一个 section 的 `flush_section()` 提前吃掉。
- oversize split 后，每个由同一 anchored message 拆出的 chunk 都应保留该 anchor。

增加真实 staging 回归测试：

至少选择一个真实 note 的“关键原始内容” section，确认：

```text
ordinal 非空
message_id 可用时非空
```

### 9.2 真正实现 `--changed-only`

`conversation_documents.file_hash` 已存在，可用于跳过 unchanged Markdown。

要求：

```text
conversation index --changed-only
```

只能 index：

- 新文件
- file hash 改变文件
- index 缺失文件
- embedding identity/model 变化需要补向量的文件/section

未变化文件必须明确计入 `skipped_unchanged`。

### 9.3 接入 embedding client

CLI 的 index/rebuild 必须使用现有 gateway embedding 配置解析，而不是写死 NAS IP/model。

要求：

- embedding disabled：只建 FTS。
- embedding enabled：创建 `EmbeddingClient` 并传给 `ConversationIndexDatabase`。
- live NAS 不进入自动 pytest。
- tests 使用 mock HTTP/client。
- dimension mismatch 仍拒绝混写。

### 9.4 index run ledger

实际使用 `conversation_index_runs`，至少记录：

```text
run_at
indexed_files
skipped_files
total_chunks
embedded_chunks
model
status
error
```

### 9.5 rebuild 安全

`rebuild-index --confirm-rebuild` 必须：

- 只从 Markdown 重建。
- 不依赖 raw ZIP。
- embedding disabled 时不联网。
- 若 embedding enabled，显式报告预计需要 embedding 的 chunk 数；dry-run 绝不调用模型。

---

## 10. R6：Retrieval / MCP 补齐 parent-thread 与 provenance

### Metadata schema

把以下 frontmatter/index metadata 纳入 document/section 索引：

```text
conversation_id
parent_thread_id
thread_source
projects
topics
date
```

必要时扩展 `conversation_documents` / `conversation_sections`。

### Search filters

`ConversationRetrievalService.search()` 至少支持：

```python
conversation_id=
parent_thread_id=
project=
```

CLI：

```text
conversation search QUERY --conversation-id ...
conversation search QUERY --parent-thread-id ...
conversation search QUERY --project ...
```

如果希望支持“仅按 parent thread 列出子 conversation”而无 query，可新增明确子命令/模式，不要偷偷改变现有 query 语义。

### MCP

`conversation_search` 和 `conversation_recall` 同步暴露 parent-thread filter。

### Provenance

每个结果必须包含：

```text
vault_path
conversation_id
heading
source_anchors
```

并能通过 Markdown frontmatter 回到：

```text
source_entry
archive_hash
```

可以在 `conversation_read` 或结果 metadata 中暴露这些字段。

### 验收

真实样本 parent thread：

```text
01a08c1c-b49b-70a2-b1a4-cad68e4b1cd4
```

必须能够稳定定位到子会话：

```text
01a08fdc-6da5-7f93-9119-ff79d9fea710
```

---

## 11. R7：CLI 与 resume 语义补齐

### 所有批处理统一支持

能合理支持的命令应统一具备：

```text
--config
--session-id
--limit
--dry-run
--json-report
```

### resume

不要为了满足参数名机械添加空 `--resume`。

定义稳定语义：

- import：根据 manifest 跳过 unchanged，重试 `failed_retryable`。
- index：根据 documents/index hash 跳过 unchanged，继续 index stale。

若默认行为已经是 resume-safe，可以：

```text
--resume / --no-resume
```

或文档明确“resume 默认开启”。

但必须有测试证明中断后续跑不重做成功项。

### JSON report

至少这些命令支持机器可读 report：

```text
audit-export
import
inventory-attachments
index
rebuild-index
search
validate
```

report 中必须区分：

```text
processed
written
skipped
conflict
failed_retryable
indexed
embedded
fallback
```

---

## 12. R8：真实 5-session 端到端验收

这是本轮最重要的验收，不能只依赖 synthetic fixture。

### 固定真实样本

继续使用：

```text
019e3ad1-05d6-7382-972f-0d377e6092c6
019eab7a-3a54-70b1-afd2-b89c0c98e8b2
01a08fdc-6da5-7f93-9119-ff79d9fea710
01a01289-e1b4-7242-98d9-368a75a16194
019ea2ad-9a74-75c2-bf10-4246ca00ab25
```

### 创建隔离 staging

不要覆盖当前已存在 staging 作为唯一验证依据。

使用新目录，例如：

```text
exports/conversation-staging/remediation-validation/
```

只写这 5 场。

### 必测链路

```text
Raw ZIP
-> parser
-> attachment inventory
-> manifest
-> Markdown writer
-> heading chunker
-> FTS index
-> optional mocked vector index
-> retrieval
-> read/recall provenance
```

### 必须验证

1. 5/5 source entry hash 与 manifest 一致。
2. 5/5 Markdown 无 base64。
3. 5/5 重复 import 不改变 machine-managed hash。
4. 真实人工区编辑后 source 更新仍能安全 merge。
5. parent thread 查询真实命中。
6. 至少一个真实 message chunk 有有效 source ordinal。
7. attachment inventory 的 Windows 路径无 slash-style 重复计数。
8. 删除派生 index DB 后可从这 5 篇 Markdown 完整重建 FTS。
9. embedding disabled 时全流程完全不访问 NAS。
10. mocked embedding enabled 时只对需要的 chunk 计算 embedding。

### 真实查询集合的生成规则

不要预先指定不存在于真实样本中的字符串。

先从 5 篇 Markdown 自动或人工选择实际存在的精确文本，分成：

```text
path
error text
numeric value
chemical term
tool name
conversation_id
parent_thread_id
```

然后记录：

```text
query
expected conversation
expected heading
actual result
source anchor
```

synthetic 固定查询 `P0-4 / 324 nm / 60 min / ERROR_NO_SYSTEM_RESOURCES / 35984` 可以继续保留在 unit tests，但报告必须明确标注为 fixture validation。

---

## 13. R9：修订实施报告

更新：

```text
docs/CONVERSATION_MEMORY_IMPLEMENTATION_REPORT.md
```

不要删除旧问题历史，但要修正结论。

### 报告必须包含

1. 本轮补漏前的独立审核结论。
2. 实际修改文件。
3. 新增 migration/schema。
4. 全部自动化测试最终数量。
5. synthetic fixture 测试结果。
6. 真实 5-session E2E 结果。
7. optional live NAS smoke 单独一节。
8. attachment missing 去重后的真实数量。
9. parent-thread 真实查询结果。
10. source-anchor 真实查询结果。
11. changed-only/resume 实际统计。
12. 仍未解除的正式 Vault/全量导入门控。

### 禁止写法

未经真实执行不得写：

```text
P0-P8 全部完成
全绿
真实命中
完整闭环
```

每个结论必须能指向：

- 测试名；或
- CLI 命令输出；或
- staging 文件；或
- SQLite 行；或
- 原 ZIP anchor。

---

## 14. 自动化测试最低新增覆盖

新增/扩展测试至少覆盖：

### Config / path

- canonical_subdir。
- canonical path traversal。
- staging path traversal。

### Attachment

- Windows slash canonical dedup。
- ZIP entry。
- tool/artifact ref。
- URL。
- missing/unresolved。

### Writer

- manual body edit 不触发 managed conflict。
- managed region edit 触发 candidate。
- manual frontmatter 字段保留。

### Manifest

- 9 个状态全部测试。
- output 不存在情况下 failed_retryable 可落盘。
- index_stale。
- attachment hash 真正参与 decision。

### Index

- source anchor oversize split 保留。
- changed-only 真正跳过未变文件。
- index_runs 落盘。
- embedding disabled 零网络调用。
- mock embedding enabled。
- dimension mismatch。

### Retrieval

- parent_thread_id filter。
- conversation_id filter。
- project filter。
- path allowlist。
- embedding fallback。
- provenance source anchor。

### CLI

- JSON report。
- resume 中断续跑。
- index --changed-only。
- canonical vault path 使用 canonical_subdir。

### Real samples

- 独立 `test_conversation_real_e2e.py` 或等价集成测试。
- 若真实 ZIP 不存在允许 skip，但开发机存在时必须实际运行。

---

## 15. 最终验收条件

只有全部满足以下条件，才允许报告“原任务书 P0-P8 已完成”：

### A. 测试

- 原 145 项测试无回归。
- 新补漏测试全部通过。
- 完整 pytest 0 failed。

### B. 真实数据

- 5 场真实 staging E2E 通过。
- 真实查询表只包含真实 note 中存在的信息。
- parent-thread 真实查询通过。
- source-anchor 真实追溯通过。

### C. 增量

- unchanged import 跳过。
- changed source 只重写 machine-managed 部分。
- manual note 不丢失。
- changed-only index 真正跳过 unchanged Markdown。
- failed_retryable 可续跑。
- index_stale 可检测并修复。

### D. 派生索引

- FTS 可从 Markdown 重建。
- embedding disabled 可独立工作。
- embedding enabled 走配置化 client。
- embedding cache/model identity 有效。
- 维度不一致拒绝混写。

### E. 安全

- 无正式 Vault 写入。
- 无 322 场全量导入。
- 无批量 NAS embedding。
- 无 Unraid 配置更改。
- 无 commit/push。

---

## 16. 建议实际执行命令

PowerShell 环境，不使用 Bash-only 命令。

基线：

```powershell
git rev-parse --show-toplevel
git status --short
python -m pytest -q
```

补漏后：

```powershell
python -m pytest -q
python scripts/conversation_cli.py validate --staging-dir exports/conversation-staging/remediation-validation
```

真实 5 场导入时必须显式 session filter，不得省略成全量：

```powershell
python scripts/conversation_cli.py import <EXPORT_ZIP> `
  --session-id 019e3ad1-05d6-7382-972f-0d377e6092c6 `
  --session-id 019eab7a-3a54-70b1-afd2-b89c0c98e8b2 `
  --session-id 01a08fdc-6da5-7f93-9119-ff79d9fea710 `
  --session-id 01a01289-e1b4-7242-98d9-368a75a16194 `
  --session-id 019ea2ad-9a74-75c2-bf10-4246ca00ab25 `
  --json-report exports/conversation-staging/remediation-validation/import-report.json
```

如果 CLI 目前不能直接指定 staging output root，应先补对应安全参数或使用隔离 config；不要覆盖旧 staging 作为唯一验收证据。

---

## 17. 给 Flash Agent 的直接指令

```text
在 G:\LLM\memory 中执行 docs/CONVERSATION_MEMORY_REMEDIATION_TASKBOOK.md。

这是对 docs/CONVERSATION_MEMORY_IMPLEMENTATION_TASKBOOK.md 的补漏任务，不允许重做现有架构。先读取原任务书、实施报告和三份审计文档，然后从当前未提交工作树继续。

重点修复：canonical_subdir/path safety、attachment locator 完整分类和 Windows 路径去重、writer 人工区域与 manifest 共存、完整 manifest 9 状态和 failed_retryable、真实 changed-only、CLI embedding client 接线、source anchor 归属、parent-thread retrieval、resume/JSON report，以及真实 5-session E2E 验收。

实施报告当前的 Exact Query 表把 synthetic fixture 数据误写成了真实 staging 验证，必须纠正。fixture 测试和 real staging 验证必须分开报告；不得再用 Nihao 会话声称命中 P0-4、324 nm、60 min 等并不存在的真实内容。

禁止全量导入 322 场、禁止正式 Vault 写入、禁止批量 embedding、禁止修改或重启 Unraid、禁止 commit/push。真实 NAS 只允许显式 smoke；pytest 必须 mock 网络。

完成后更新 docs/CONVERSATION_MEMORY_IMPLEMENTATION_REPORT.md，并附上：完整 pytest、真实 5-session E2E、parent-thread 查询、source-anchor 回溯、attachment 去重结果、changed-only/resume 统计和仍未解除的正式批量门控。
```

