# Conversation Memory Multi-Source Identity & Dedup Taskbook v0.2.4

任务日期：2026-09-13  
执行项目：`G:\LLM\memory`  
目标版本：`v0.2.4`  
建议执行 Agent：本地 Flash Agent / Codex CLI（如使用 Codex CLI，模型固定为 `gemini-3.8-flash-high`）  
任务性质：**Conversation Memory 多来源身份层、整包重复导入去重、continuation / branch / duplicate review 基础设施**。  
本任务**不是** ChatGPT / Claude / Gemini importer 实现任务，也**不是** bulk vector backfill / ANN 优化任务。

---

## 0. 本任务为什么现在必须做

Conversation Memory 当前已经从 35-session pilot 扩展为 322-session Codex 历史库，并完成：

```text
322 conversations
26,269 sections
322/322 repeat full-export import -> skipped/unchanged
322/322 repeat lexical index -> skipped_unchanged
v0.2.3 production on NAS
```

当前 Codex importer 对“同一平台提供稳定 conversation ID”的重复整包导入已经可靠：

```text
same conversation_id + same content
    -> skip

same conversation_id + new archive
    -> skip / same conversation in new archive

same conversation_id + continued content
    -> source_changed / update existing canonical path

same conversation_id + title changed
    -> v0.2.3 keeps the old canonical output path
```

但是下一阶段若直接接入 ChatGPT / Claude / Gemini / Cursor 等 importer，当前数据模型会立刻暴露两个结构问题：

1. `conversation_imports` 仍以裸 `conversation_id` 为主键，不具备 source namespace。
2. 不同平台对同一逻辑会话可能使用不同 ID；反过来，不同平台也可能碰巧出现相同 ID 字符串。

因此 v0.2.4 必须先建立：

```text
source identity
        +
canonical conversation identity
        +
snapshot history
        +
deterministic fingerprints
        +
conservative duplicate review
```

最终目标是以后用户可以直接把“每次完整导出的历史包”扔给 Gateway：

```text
Codex full export
ChatGPT full export
Claude full export
Gemini full export
...
        ↓
Gateway 自己判断：
  unchanged
  continuation
  stale snapshot
  branch/divergence
  new conversation
  possible duplicate
        ↓
不要求用户在导出阶段手工挑选“哪些是新的”
```

---

## 1. 强制执行边界

### 1.1 本任务必须做

- 抽象平台无关的 Conversation Export Reader contract。
- 建立 source-scoped identity。
- 建立内部 `canonical_conversation_id`。
- 建立 source snapshot ledger。
- 建立 deterministic transcript/message fingerprints。
- 实现严格 continuation / stale / divergence 判定。
- 建立 `possible_duplicate` review queue。
- 让 index / search / recall 能返回 source key + canonical ID。
- 让 recall 对已确认属于同一 canonical conversation 的重复 source 默认不重复灌 context。
- 对现有 Codex 322 场做无损 additive migration。
- 保持现有 v0.2.3 Codex full-export 幂等行为。
- 补足单元、迁移、端到端和回归测试。

### 1.2 本任务明确不做

- 不实现正式 ChatGPT importer。
- 不实现正式 Claude importer。
- 不实现正式 Gemini importer。
- 不做 semantic embedding-based 自动 merge。
- 不把近似相似度当作自动去重依据。
- 不物理删除/合并两个 source Markdown note。
- 不做 18k+ full BGE-M3 backfill。
- 不改 vector search 为 ANN；ANN/vector-index 单列后续任务。
- 不修改 Research-AI-Hub 的科研事实/实验记录。
- 不删除 NAS 现有 322-session archive、index 或 rollback container。

### 1.3 自动 merge 的硬禁令

以下情况**绝对不得自动合并**：

```text
Codex vs ChatGPT 文字高度相似
ChatGPT vs Claude 完全相同的一段复制粘贴文本
标题相同
时间接近
embedding similarity 很高
MinHash / SimHash / Jaccard 很高
```

跨 source system 的相似只能进入：

```text
possible_duplicate
```

除非有明确 deterministic identity/link evidence 或人工确认，否则保持两个独立 source records。

---

## 2. 执行前当前状态核对（W0）

### 2.1 Git 基线

执行前必须先记录：

```powershell
git remote -v
git branch --show-current
git rev-parse HEAD
git status --short
git log --oneline --decorate -n 15
git ls-remote --heads --tags origin
```

截至本任务书生成时，预期背景是：

```text
remote main / v0.2.3 code: d6e44f0...
local main may additionally contain:
  de1e646 docs: record full conversation import stability
```

如果本地仍为 `de1e646` 且 remote main 仍为 `d6e44f0`：

- **保留 `de1e646`**；
- 不 reset / 不 rebase 掉该报告 commit；
- v0.2.4 从当前本地 main 继续即可；
- 最终 push 时让该报告自然成为 v0.2.4 ancestry 的一部分。

当前工作树可能仍有：

```text
?? .local/
?? 0
?? 0)
```

这些是既有未跟踪文件：

- 不删除；
- 不修改；
- 不 stage；
- 不提交。

### 2.2 发布/认证规则

GitHub 操作必须走用户指定的 **PowerShell 本地通道**。

若使用 Git Credential Manager：

- 明确选择 `kettly1260`；
- 不改全局 credential 配置；
- 不打印 token/password；
- 若认证、网络或权限失败，**立即停并汇报一次**；
- 禁止盲目循环 push。

### 2.3 基线测试

执行：

```powershell
python -m pytest -q
git diff --check
```

若 frontend 被修改，则同时：

```powershell
cd src/research_memory_gateway/webui/frontend
npm ci
npm run lint
npm run build
```

先记录现有测试数量；不要把“原有失败”误认为 v0.2.4 引入。

---

## 3. 目标身份模型

### 3.1 Source Identity 与 Canonical Identity 必须分开

必须建立两个概念：

```text
Source Conversation Record
    = 某个平台/账号/线程真正导出的 provenance record

Canonical Conversation
    = Gateway 内部用于把“已确认属于同一逻辑会话”的多个 source record 关联起来的逻辑组
```

不得把 canonical record 当成“覆盖 source provenance 的新真相”。

source records 永远保留。

### 3.2 新增平台无关 SourceIdentity

建议在 `conversations/models.py` 增加类似：

```python
@dataclass(frozen=True)
class ConversationSourceIdentity:
    source_system: str
    source_account_namespace_hash: str
    source_conversation_id: str
    source_thread_id: str = ""
    source_branch_id: str = ""
```

并提供 deterministic：

```text
source_key
```

建议格式：

```text
srcv1_<sha256>
```

hash 输入必须包含版本化 domain separator，例如：

```text
rmg-source-v1\0
source_system\0
source_account_namespace_hash\0
source_conversation_id\0
source_thread_id\0
source_branch_id
```

不得使用 Python runtime `hash()`。

必须使用稳定 SHA-256 / UUID 等 deterministic 算法。

### 3.3 source_account_namespace_hash

目的：防止两个不同账号下 provider ID namespace 发生潜在碰撞。

要求：

- 不在 Markdown/API 中暴露原始 email、账号名、token、账户 secret。
- importer 若能读取稳定 provider account GUID，可将其规范化后 hash。
- importer 若没有 provider account identity，应允许用户给稳定 namespace label。
- Codex 现有 322 场 migration 使用明确、稳定的 legacy namespace，例如：

```text
codex / legacy-default-v1
```

并将它 hash 后存储。

不要在 migration 时臆造用户邮箱。

### 3.4 canonical_conversation_id

新增：

```text
canonical_conversation_id
```

建议使用固定 namespace 的 UUIDv5：

```text
uuid5(RMG_CANONICAL_NAMESPACE_V1, source_key)
```

第一条 source record 首次创建 canonical 时，canonical ID 必须 deterministic。

之后若人工确认两个 source record 属于同一 canonical：

- 选一个 canonical 作为 winner；
- loser canonical 不物理删除；
- 保存 alias / merged-into 关系；
- 所有旧 canonical ID 仍应可解析到 active canonical。

---

## 4. Reader 抽象（W1）

### 4.1 移除 Pipeline 对 CodexExportReader 的硬编码

当前：

```python
class ConversationIngestionPipeline:
    def __init__(self, reader: CodexExportReader, ...)
```

必须改为平台无关 reader protocol / ABC，例如：

```python
class ConversationExportReader(Protocol):
    source_system: str
    parser_version: str
    schema_version: str
    archive_path: Path
    archive_sha256: str

    def list_sessions(...) -> ...: ...
    def get_session_ref(...) -> ExportSessionRef: ...
    def parse(...) -> NormalizedConversation: ...
```

CodexExportReader 实现该 contract。

### 4.2 ExportSessionRef 扩展

`ExportSessionRef` 至少增加：

```text
source_identity / source_key
source_system
source_conversation_id
source_thread_id
source_branch_id
```

兼容要求：

- `ref.conversation_id` 暂时继续保留，兼容现有 API/测试；
- 对 Codex，`conversation_id == source_conversation_id`；
- 新代码内部 identity lookup 不得再只用裸 `conversation_id`。

### 4.3 parser/schema version 不再全局绑定 Codex

当前：

```text
PARSER_VERSION = codex-export-v1.3
```

未来每种 importer parser version 都不同。

要求：

- reader 暴露自己的 parser version；
- manifest decision 使用当前 reader parser version；
- 旧 Codex 常量可保留 compatibility alias，但 pipeline 不应把它当成所有 source 的版本。

### 4.4 本阶段不写 ChatGPT parser

只增加一个 test-only fake reader / synthetic source reader，用于证明：

```text
codex: id=abc
chatgpt: id=abc
```

不会产生 identity collision。

---

## 5. Fingerprint 规范（W2）

Fingerprint 用于辅助 identity / continuation / duplicate detection。

**Fingerprint 不等于 source identity。**

### 5.1 Message fingerprint

实现版本化函数，例如：

```text
message_fingerprint_v1(message)
```

最低要求：

- role 规范化；
- text 做 Unicode normalization；
- CRLF/LF 归一；
- trailing whitespace 归一；
- 不使用 timestamp 作为主要内容 hash 输入；
- 不使用 provider message ID 作为内容 hash 的必要条件；
- 算法必须 deterministic。

禁止过度 normalization：

- 不把所有空白删除；
- 不大小写强制折叠正文；
- 不移除标点；
- 不做语义 embedding 归一。

否则不同代码/化学式/数值内容可能被误判为相同。

### 5.2 Transcript fingerprints

每个 normalized conversation 至少计算：

```text
ordered_message_hash
message_set_hash
normalized_transcript_sha256
message_count
```

推荐语义：

`ordered_message_hash`
: 对按顺序排列的 message fingerprints 计算 hash。

`message_set_hash`
: 对排序后的 message fingerprints 计算 hash，供候选比较；不得单独作为自动 merge 依据。

`normalized_transcript_sha256`
: 对版本化 normalized transcript canonical representation 做 hash。

### 5.3 Tool/attachment 证据

不要把 tool event/attachment 完全混进“人类可见 transcript hash”导致导出格式小变就全部失效。

建议单独保留：

```text
tool_event_set_hash
attachment_inventory_hash
```

它们用于加强 evidence，而不是替代 source identity。

### 5.4 Fingerprint versioning

所有 fingerprint schema 必须带版本：

```text
fingerprint_version = 1
```

未来 normalization 算法升级时：

- 不静默重解释旧 hash；
- 可以重新计算；
- snapshot ledger 仍能区分算法版本。

---

## 6. Manifest / Identity DB additive migration（W3）

### 6.1 禁止破坏现有 conversation_imports

当前 `conversation_imports`：

```text
PRIMARY KEY(conversation_id)
```

不能直接改成多来源 composite key 而冒险破坏现有 322 records。

采用 additive v2 tables。

建议新增：

```text
canonical_conversations
conversation_source_records
conversation_source_snapshots
conversation_message_fingerprints
conversation_duplicate_candidates
canonical_aliases
dedup_decisions
```

名称可以微调，但职责必须完整。

### 6.2 canonical_conversations

至少：

```text
canonical_conversation_id TEXT PRIMARY KEY
created_at
updated_at
status                 active / merged
merged_into_id
primary_source_key
```

### 6.3 conversation_source_records

至少：

```text
source_key TEXT PRIMARY KEY
canonical_conversation_id TEXT NOT NULL
source_system TEXT NOT NULL
source_account_namespace_hash TEXT NOT NULL
source_conversation_id TEXT
source_thread_id TEXT
source_branch_id TEXT
output_path TEXT
first_seen_archive_sha256 TEXT
last_seen_archive_sha256 TEXT
last_source_entry_sha256 TEXT
normalized_transcript_sha256 TEXT
ordered_message_hash TEXT
message_set_hash TEXT
message_count INTEGER
fingerprint_version INTEGER
parser_version TEXT
schema_version TEXT
status TEXT
first_seen_at TEXT
last_seen_at TEXT
```

增加唯一约束：

```text
UNIQUE(
  source_system,
  source_account_namespace_hash,
  source_conversation_id,
  source_thread_id,
  source_branch_id
)
```

注意 SQLite 对空字符串 / NULL 的 unique 语义；必须在代码层统一 normalize，避免一部分写 NULL、一部分写 `""`。

### 6.4 conversation_source_snapshots

记录“一个 source conversation 在一次导出中看到的版本”。

至少：

```text
snapshot_id
source_key
source_archive_sha256
source_entry_sha256
normalized_transcript_sha256
ordered_message_hash
message_set_hash
message_count
first_seen_at
last_seen_at
seen_count
```

相同 source entry 重复出现在不同 full export ZIP 时：

- 不无限插入完全相同 snapshot row；
- 可以 update `last_seen_at` + `seen_count`；
- 仍更新 source record 的 `last_seen_archive_sha256`。

### 6.5 message fingerprint table

用于严格 prefix / divergence 判断。

至少：

```text
source_key
ordinal
message_fingerprint
role
```

不需要重复存大段 message text。

必须能按 ordinal 重建 fingerprint sequence。

### 6.6 duplicate candidates

至少：

```text
candidate_id
left_source_key
right_source_key
candidate_type
score
evidence_json
status       pending / confirmed_same / rejected
created_at
reviewed_at
```

唯一性要求：

- `(min(left,right), max(left,right), candidate_type)` 不重复创建 pending candidate。

### 6.7 dedup decisions / aliases

人工确认/拒绝是持久决策，不能只存在 WebUI 内存。

要求：

- confirmed same 的 link 决策持久化；
- rejected 决策持久化，未来再次整包导入不要反复重新提示同一 pair；
- canonical merge 使用 alias/merged_into，不物理删 source。

### 6.8 legacy 322 migration

Migration 必须：

1. transaction 内创建 v2 tables；
2. 读取所有 legacy `conversation_imports`；
3. 每条现有记录映射为：

```text
source_system = codex
source_account_namespace_hash = hash(legacy-default-v1)
source_conversation_id = legacy conversation_id
thread/branch = normalized empty value
source_key = deterministic srcv1 hash
canonical_id = deterministic UUIDv5(source_key)
```

4. 复制 output path / parser / source hashes / status 等必要 metadata；
5. 建立 snapshot record；
6. 保留 legacy `conversation_imports` 原表和数据，不 drop；
7. migration 可重复执行，第二次不得新增 duplicate rows。

生产 migration 验收要求：

```text
legacy conversation_imports = 322
source_records              = 322
canonical_conversations     = 322
source_key duplicates       = 0
canonical_id duplicates     = 0
output path changes         = 0
```

迁移不能重写 322 个 Markdown 正文。

---

## 7. Import decision state machine（W4）

这是本任务最关键部分。

### 7.1 Exact source identity exists

如果 `source_key` 已存在：

#### A. source entry hash 相同

```text
action = skip
reason = unchanged / same_source_in_new_archive
```

同时更新 snapshot `last_seen_at/seen_count` 与 source `last_seen_archive_sha256`。

#### B. source entry hash 不同，但 normalized transcript 完全相同

可能只是 exporter packaging/metadata 变化。

```text
action = skip
reason = source_packaging_changed
```

不得无意义重写 canonical Markdown。

#### C. 新 transcript 是旧 transcript 的严格 continuation

定义：旧 message fingerprint sequence 是新 sequence 的**完整严格前缀**。

```text
action = write/update existing output path
reason = source_continued
```

要求：

- path 保持稳定；
- manual region 保留；
- 新内容进入 managed section；
- manifest/source record 更新；
- 已索引过则标 `index_stale`。

#### D. 新 transcript 是旧 transcript 的严格前缀

这是旧/截断 export 覆盖较新 snapshot 的典型风险。

```text
action = skip
reason = stale_snapshot
```

绝对禁止用较短旧 export 截断已经保存的较长 conversation。

#### E. 同 source identity 但旧消息序列发生 divergence

例如：

```text
A: m1 m2 m3 m4
B: m1 m2 X  Y
```

若 provider 没有提供 branch ID 解释该 divergence：

```text
action = conflict / review
reason = source_diverged
```

不得直接 overwrite 原 note。

写 candidate copy 或 review record。

### 7.2 Source identity 不存在

#### A. 有稳定 provider conversation ID

默认：

```text
new source record
new canonical conversation
```

然后运行 candidate detector，但 candidate detector 不得阻止 source provenance 落盘。

#### B. provider ID 缺失/不可靠

只在以下严格条件可同来源自动 dedup：

```text
same source_system
same account namespace
provider ID missing/unreliable on both sides
exact normalized transcript fingerprint
```

可复用已有 source/canonical。

除此之外进入 candidate review。

### 7.3 Same provider ID string, different source system

必须通过测试：

```text
codex / id=abc
chatgpt / id=abc
```

得到两个不同 `source_key`。

不得因为裸 ID 相同自动合并。

### 7.4 Branch semantics

若 importer 能提供 branch/thread ID：

- branch ID 进入 source key；
- 同一 provider conversation 下不同 branch 可以属于独立 source records；
- 可以通过 candidate/link 关联到同一 canonical；
- 不得用一个 branch 覆盖另一个 branch 的 Markdown。

如果 branch ID 不可得且发生 divergence：fail closed 到 review。

---

## 8. Duplicate candidate detector（W5）

### 8.1 Candidate 不是 merge

任何 heuristic 只负责产生：

```text
possible_duplicate
```

不得修改 source_key、canonical ID 或 output path。

### 8.2 candidate types

至少支持：

```text
exact_transcript_same_source_missing_id
cross_source_exact_transcript
strict_prefix_possible_continuation
high_message_overlap
same_title_time_window
```

其中：

- `cross_source_exact_transcript` 仍只 candidate；
- `same_title_time_window` 证据权重必须很低；
- semantic similarity 不进入 v0.2.4 自动决策链。

### 8.3 deterministic evidence

`evidence_json` 最少记录：

```json
{
  "same_source_system": false,
  "same_account_namespace": false,
  "exact_transcript": true,
  "ordered_prefix": false,
  "message_overlap": 1.0,
  "left_message_count": 24,
  "right_message_count": 24,
  "title_similarity": 0.91
}
```

score 仅用于排序 review queue，不用于自动 merge。

### 8.4 rejected pair suppression

如果用户已经把 pair 标记为 `rejected`：

- 后续完整导出再次出现时，不要重复创建 pending candidate；
- 除非 fingerprint/version/identity evidence 实质变化，才允许产生新的 candidate revision。

---

## 9. Dedup Review CLI（W6）

v0.2.4 至少增加 CLI 管理面，名称可微调，但能力必须具备。

建议：

```text
conversation ... dedup-audit
conversation ... dedup-list
conversation ... dedup-show
conversation ... dedup-resolve
conversation ... identity-show
```

### 9.1 dedup-audit

只读扫描：

```text
pending candidates
source collisions
canonical aliases
orphan source records
duplicate output paths
source_key collisions
```

支持 `--json-report`。

### 9.2 dedup-list

支持：

```text
--status pending|confirmed_same|rejected
--source-system
--candidate-type
```

### 9.3 dedup-show

显示 candidate 两边：

```text
source_key
canonical_id
source_system
source_conversation_id
title
date
message_count
fingerprints
evidence
output_path
```

禁止默认输出原始 secret/account identifier。

### 9.4 dedup-resolve

必须要求显式确认参数，例如：

```text
--confirm-same
--reject
```

不可默认确认。

`--confirm-same`：

- 将两 source records 链接到同一个 canonical；
- loser canonical 记录 alias/merged_into；
- 不删除任何 Markdown；
- 不移动 source note；
- 标记 index canonical metadata stale / refresh needed。

`--reject`：

- 持久化 rejected decision；
- source/canonical 均不变。

### 9.5 不提供 `--auto-merge-near-duplicates`

v0.2.4 明确禁止此类参数。

---

## 10. Markdown / source note 策略（W7）

### 10.1 source note 保持 provenance-first

即使两个 source records 人工确认属于同一 canonical conversation：

```text
Codex note   保留
ChatGPT note 保留
Claude note  保留
```

不要把它们拼成一个超大 Markdown。

Canonical identity 是逻辑关联，不是物理文件 merge。

### 10.2 新写入 frontmatter

未来新写/更新的 note machine frontmatter 增加：

```yaml
canonical_conversation_id: ...
source_key: ...
source_system: codex
source_conversation_id: ...
source_thread_id: ...
source_branch_id: ...
fingerprint_version: 1
```

现有兼容字段继续保留。

### 10.3 不强制 mass rewrite 322 notes

v0.2.4 migration **不要为了 frontmatter 美观而一次性重写 322 份 note**。

要求：

- identity DB migration 即可让现有 322 条 source/canonical 关系生效；
- existing notes 可在未来正常 continuation/update 时写入新字段；
- indexer 若旧 note 缺 canonical field，应能从 identity/manifest mapping 补齐；
- 不因为缺新 frontmatter 导致 322 notes 全部 `source_changed`。

### 10.4 manual region

所有 continuation / identity metadata update 必须继续保留人工区。

现有 `managed_output_sha256` 与 manual region 冲突保护不能退化。

---

## 11. Index Schema / Search / Recall（W8）

### 11.1 additive index columns

`conversation_documents` 与 `conversation_sections` 增加：

```text
source_key
canonical_conversation_id
source_conversation_id
source_thread_id
source_branch_id
```

旧 source identity 字段继续保留：

```text
source_system
source_originator
source_surface
source_version
...
```

### 11.2 API/SearchResult

`conversation_search` / WebUI API result 至少返回：

```text
conversation_id              # legacy compatibility
source_conversation_id
source_key
canonical_conversation_id
source_system
thread_source
parent_thread_id
```

### 11.3 filters

增加：

```text
canonical_conversation_id?
source_key?
source_system?
source_conversation_id?
```

现有 `conversation_id` 参数兼容：

- 在当前 Codex-only 数据中行为不变；
- 多来源后如果裸 ID 命中多个 source，API/CLI 不得静默选错；
- 应返回明确 ambiguous 状态或要求额外 `source_system/source_key`。

### 11.4 Recall canonical collapse

`conversation_recall` 默认：

```text
collapse_canonical = true
```

目的：如果 Codex source 与 ChatGPT source 已经人工确认属于同一 canonical conversation，不要把两份高度重复内容都塞进 Agent context。

要求：

- collapse 只针对**已经 canonical-linked** 的 source；
- pending duplicate candidate 不 collapse；
- rejected pair 不 collapse；
- 每个 canonical group 选择最终 score 最高的 section 作为代表；
- source provenance 仍附在 item metadata 中；
- 提供 opt-out `collapse_canonical=false` 供审计使用。

### 11.5 Search UI

普通 search 可以继续显示 source-level results，但应显示：

```text
Canonical: <short id>
Source: CODEX / CHATGPT / ...
```

同 canonical 的多 source 可用 group badge，但不要求 v0.2.4 做复杂可视化。

---

## 12. WebUI 最小补齐（W9）

本任务不做完整 Dedup 管理后台，但要让运维状态可见。

### 12.1 Conversations Status

增加：

```text
canonical_conversations
source_records
pending_duplicate_candidates
confirmed_duplicate_links
rejected_duplicate_candidates
source_system_distribution
```

### 12.2 Reader/Search detail

来源身份卡片增加：

```text
canonical_conversation_id
source_key
source_conversation_id
source_thread_id / branch_id
```

### 12.3 Duplicate review UI

v0.2.4 **可选**。

若时间充足，可做只读 candidate list。

不要为了 WebUI 按钮延迟核心 identity/dedup correctness。

真正的 confirm/reject 先由 CLI 完成即可。

---

## 13. 测试矩阵（W10）

以下测试为 release blocker。

### 13.1 Identity

1. 同 source/account/provider ID -> 同 source_key。
2. 同裸 provider ID、不同 source system -> 不同 source_key。
3. 同 source system、不同 account namespace -> 不同 source_key。
4. source key 跨进程运行 deterministic。
5. canonical UUIDv5 deterministic。

### 13.2 Legacy migration

6. legacy manifest -> v2 tables，记录数一致。
7. migration 第二次运行 0 duplicate。
8. 旧 output path 不变。
9. legacy table 不被 drop。
10. 旧 322-style Codex result 仍可通过 legacy `conversation_id` 搜索。

### 13.3 Full-export dedup

11. 第一次 full export -> new。
12. 相同 full export 第二次 -> unchanged/skip。
13. 相同 source 出现在新 archive -> skip + last_seen 更新。
14. packaging hash 变化但 normalized transcript 不变 -> 不重写 note。

### 13.4 Continuation / stale / divergence

15. A 是 B prefix -> B 更新原 path。
16. B 标题变化 -> 原 path 仍稳定。
17. manual region 在 continuation 后保留。
18. 较短旧 snapshot 再导入 -> `stale_snapshot`，不得截断现有 note。
19. 同 source ID 中途修改旧 message -> `source_diverged`，不得自动 overwrite。
20. 有 branch ID 的 divergence -> 两 source records，不互相覆盖。

### 13.5 Multi-source safety

21. Codex 与 ChatGPT synthetic record 文本完全相同 -> candidate only，不 auto merge。
22. Codex 与 ChatGPT provider ID 碰巧相同 -> 不 collision。
23. 跨 source exact transcript candidate evidence 正确。
24. near duplicate -> pending candidate only。
25. rejected pair 再次整包导入 -> 不重复刷 pending queue。

### 13.6 Manual resolution

26. confirm same -> 两 source records 指向同 active canonical。
27. source notes 都仍存在。
28. loser canonical 通过 alias 可解析。
29. reject -> canonical 不变。
30. resolve 命令没有 confirm 参数时拒绝执行。

### 13.7 Retrieval

31. search 返回 source_key/canonical_id。
32. recall 对 confirmed canonical duplicates 默认 collapse。
33. pending candidate 不 collapse。
34. rejected pair 不 collapse。
35. `collapse_canonical=false` 返回 source-level results。
36. 现有 Codex-only recall 排名不出现明显回归。

### 13.8 Index migration

37. 旧 index additive migration 可打开。
38. 旧 322 note 无 canonical frontmatter 仍能由 manifest/identity mapping index。
39. source/canonical metadata 写入 documents/sections。
40. second changed-only index 全 unchanged。

---

## 14. 真实 322-session 回归门（W11）

单元测试完成后，必须在**副本**上用现有 NAS/本地 322-session 数据做一次真实 migration rehearsal。

严禁直接拿 production manifest 当第一试验品。

### 14.1 副本要求

复制：

```text
archive-local manifest.sqlite
322 Markdown archive
conversation index（如需要）
```

使用 SQLite backup API 或安全复制方式。

### 14.2 migration rehearsal 验收

要求：

```text
legacy imports              322
source records              322
canonical conversations     322
pending duplicates            0  # Codex-only 初始 migration 预期
output paths changed          0
Markdown files changed        0  # migration 本身
source key collisions         0
canonical collisions          0
```

### 14.3 相同 322 ZIP 再导入

迁移后的 v2 manifest 上，再导当前完整 Codex ZIP：

```text
322 skipped / unchanged
0 written
0 conflict
0 failed
```

### 14.4 continuation synthetic overlay

至少用一个从现有 322 session 派生的 synthetic continuation：

- 同 source ID；
- 增加一条 user/assistant message；
- 修改 title；
- 验证原 path 更新；
- 验证不存在 orphan duplicate。

### 14.5 stale replay

在 continuation 写入后，再导旧 snapshot：

```text
stale_snapshot
```

Markdown 不得回退/截断。

---

## 15. 性能与数据库约束

### 15.1 不在本任务继续 full vector backfill

当前已知：

```text
322 conversations
26,269 sections
19,712 unique embedding identities
约 18,159 new vectors still needed
```

现有 `search_vector()` 仍是 Python brute-force cosine。

因此 v0.2.4：

- 不追求 100% vector coverage；
- 不因为新 identity schema 触发全量 re-embedding；
- identity/fingerprint hash 不得改变已有 embedding identity 语义，除非明确 migration；
- ANN / sqlite-vec / FAISS / HNSW 单独立项。

### 15.2 Unraid SQLite 路径

当前生产大 index 已验证：

```text
/mnt/user/...  # shfs/FUSE, 大 SQLite 存在明显 I/O stall
/mnt/disk3/... # direct path, 可用
```

本任务若需要 NAS rehearsal：

- SQLite 副本优先直盘/真实 pool path；
- 不把新的 100MB+ SQLite 实验库放 `/mnt/user` 做性能结论；
- 不改 Unraid share policy。

---

## 16. 安全 / 隐私要求

### 16.1 不泄露账号 identity

不得在：

```text
Markdown frontmatter
WebUI
search API
logs
JSON reports
```

输出原始 account email / auth token / account secret。

只输出 hash namespace 或用户定义的非敏感 label（若明确允许）。

### 16.2 Fingerprint 不是用户隐私原文

fingerprint tables 只存 hash/ordinal/role 等最低必要信息。

不要为了 dedup 另存一份完整 transcript。

### 16.3 Raw export immutable

不得修改：

```text
codex-sessions-20260912-012538.zip
```

以及未来 importer 的 raw export。

### 16.4 不自动删除 duplicate source

人工 confirm same 后仍保留两个 source records 与 source notes。

任何物理清理必须另立任务并单独确认。

---

## 17. 兼容性要求

### 17.1 现有 MCP 工具名称不变

继续：

```text
conversation_search
conversation_read
conversation_recall
```

只做 additive 参数/返回字段扩展。

### 17.2 旧客户端

旧客户端只认识：

```text
conversation_id
```

在 Codex-only / 非歧义场景继续正常工作。

### 17.3 现有 Source Identity

不能退化：

```text
source_system
source_originator
source_surface
source_version
model_provider
model_name
thread_source
parent_thread_id
agent_path
```

新 canonical/source key 是补充层，不替换已有 provenance。

---

## 18. 版本、文档与 Release Gate（W12）

目标：

```text
0.2.4
```

需要更新：

```text
pyproject.toml
uv.lock
README / relevant docs
Conversation Memory implementation/release report
```

如果 frontend 改动，必须提交最新 static dist。

### 18.1 Release blocker

必须全部 PASS：

```text
pytest all green
git diff --check
frontend lint/build green（若改前端）
legacy migration rehearsal PASS
322 repeat import = 322 skipped
continuation path stable
stale snapshot cannot truncate
cross-source exact transcript does not auto merge
same raw ID across systems does not collide
dedup resolve preserves source notes
recall canonical collapse tests PASS
no secrets / DB / ZIP / NAS config staged
```

### 18.2 默认不要直接发布

本任务书默认授权 Flash Agent：

- 修改代码；
- 修改测试；
- 修改 docs；
- 本地 commit（如果任务完成且 staged 清单确认安全）。

但在用户再次明确说“发布 / push / tag / 部署”之前：

- 不创建 `v0.2.4` tag；
- 不 push GitHub；
- 不更新 GHCR；
- 不切 NAS production container；
- 不在 production manifest 上执行第一次 schema migration。

如果用户明确授权发布，再执行 W13。

---

## 19. 可选发布阶段（W13，仅用户明确授权后）

### 19.1 Push 前

```powershell
git status --short
git diff --cached --name-only
git diff --cached --check
```

禁止 stage：

```text
.local/
0
0)
config*.yaml（本机/NAS 私有配置）
data/
exports/
scratch/
*.sqlite
*.db
*.zip
token / password / secret
```

### 19.2 GitHub

PowerShell local channel + GCM `kettly1260`。

推荐：

```text
main commit
  -> GitHub Tests PASS
  -> Docker/GHCR PASS
  -> tag v0.2.4
  -> tag workflows PASS
```

不 force push。

### 19.3 NAS migration

生产 migration 前：

1. 备份当前 v0.2.3 config；
2. SQLite backup 当前 manifest/index；
3. 保留 `research-memory-gateway-pre-v024` rollback container；
4. 在独立副本跑 schema migration + 322 regression；
5. only then migrate production identity tables；
6. 不触发 full vector backfill；
7. 验证 WebUI/MCP/search/read/recall；
8. 失败即回滚 container/config/DB backup。

---

## 20. Flash Agent 执行顺序

严格按以下顺序，不要跳阶段：

```text
W0  Freeze / inspect baseline
 ↓
W1  Reader protocol abstraction
 ↓
W2  Versioned fingerprints
 ↓
W3  Additive identity DB schema + legacy migration
 ↓
W4  Import decision state machine
 ↓
W5  Duplicate candidate detector
 ↓
W6  Dedup review CLI
 ↓
W7  Markdown/source-note compatibility
 ↓
W8  Index/search/recall canonical identity
 ↓
W9  Minimal WebUI visibility
 ↓
W10 Full automated test matrix
 ↓
W11 Real 322-session rehearsal on COPY
 ↓
W12 Local release gate + report
 ↓
STOP and report to user
 ↓
W13 only if user explicitly authorizes release
```

---

## 21. Flash Agent 停机/汇报条件

遇到以下任一情况立即停，不要自行“修到能跑”为止：

1. migration 发现 legacy record 数 != 预期 source record 数。
2. 任何现有 Markdown output path 被无意改变。
3. stale old export 会覆盖较新 note。
4. source key collision。
5. canonical ID collision。
6. cross-source exact transcript 被自动 merge。
7. manual region 丢失。
8. 322 repeat import 出现 written/conflict/failed。
9. production DB 被首次 migration 误操作。
10. GitHub 认证/网络 push 失败。
11. staged files 包含 DB/ZIP/config/secret/untracked user files。
12. 需要 destructive schema migration / DROP legacy table 才能继续。

汇报时必须给：

```text
失败阶段
具体命令/测试
实际结果
期望结果
是否改过 production
rollback 状态
建议下一步
```

禁止无上限重试。

---

## 22. 最终验收报告模板

Flash Agent 完成 W0-W12 后，至少输出：

```text
VERSION TARGET:
  v0.2.4

GIT:
  baseline SHA:
  final local SHA:
  working tree preserved:

TESTS:
  pytest:
  frontend lint/build:
  diff-check:

IDENTITY MIGRATION:
  legacy records:
  source records:
  canonical records:
  source collisions:
  canonical collisions:
  output path changes:

322 REAL REHEARSAL:
  first migrated import:
  repeat full export import:
  conflicts:
  failures:

CONTINUATION:
  strict continuation:
  title change path stability:
  stale snapshot protection:
  divergence protection:

MULTI-SOURCE SYNTHETIC:
  same provider ID across systems:
  exact cross-source transcript:
  possible duplicate candidate:
  auto merge occurred?: MUST BE NO

DEDUP REVIEW:
  confirm same:
  reject:
  source notes preserved:
  canonical alias resolution:

RETRIEVAL:
  source_key returned:
  canonical_id returned:
  recall canonical collapse:
  collapse opt-out:

SECURITY:
  raw account identifiers leaked?: NO
  secrets staged?: NO
  raw exports modified?: NO

PRODUCTION:
  modified?: MUST BE NO before explicit W13 authorization

RELEASE READINESS:
  READY / NOT READY
```

---

## 23. 本任务完成定义（Definition of Done）

只有同时满足以下条件，v0.2.4 identity/dedup core 才算完成：

1. Pipeline 已不硬编码 Codex reader。
2. source identity 包含 source system/account namespace/provider conversation/thread/branch。
3. source key deterministic 且跨平台 namespace-safe。
4. canonical ID 与 source provenance 分离。
5. legacy 322 records 可 additive、idempotent migration。
6. 相同 full export 第二次导入全部 skip。
7. continuation 更新原 note/path。
8. stale export 不能截断较新 note。
9. divergence fail-closed，不覆盖原 note。
10. 跨 source 相同 ID 不 collision。
11. 跨 source 相同文本不自动 merge。
12. near duplicate 只进入 review queue。
13. confirm/reject decisions 持久化。
14. source notes 永不因 canonical link 被物理删除。
15. search/recall 返回 source_key + canonical ID。
16. recall 对 confirmed canonical duplicate 默认去重，pending/rejected 不去重。
17. 原有 Source Identity / manual region / path stability 无回归。
18. 真实 322-session rehearsal PASS。
19. 全量测试 PASS。
20. 未经授权不触碰 production migration / GitHub release。

完成后，下一张任务书才进入：

```text
ChatGPT Export Importer
```

届时 importer 只需要负责：

```text
raw ChatGPT export
  -> NormalizedConversation
  -> ConversationSourceIdentity
```

其余 exact dedup / continuation / snapshot / canonical linking / review / retrieval 逻辑应全部复用本任务建立的平台无关层。

