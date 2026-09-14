# Conversation Memory ChatGPT Export Importer — v0.2.6 Agent Taskbook

日期：2026-09-13  
项目：`G:\LLM\memory`  
建议基线：`49c39694a191267d13974e00ce58773440d65361`  
当前生产：NAS `v0.2.5` / `a97f80b655ee5024abe2d609fb39fa0c54896313`  
目标：在不破坏 Codex 322-session production 语义的前提下，实现 **ChatGPT Data Export 全量导入器**，支持整包重复导入、分支、continuation、stale snapshot、跨来源 duplicate candidate，并完成本地/副本 rehearsal。  
执行模式：**本地开发 + 测试 + 副本 rehearsal + 本地 commit 后停止。禁止 push/tag/GHCR/NAS production migration，除非用户再次明确授权。**

---

## 0. 与并行 WebUI Agent 的边界

本任务允许与 `CONVERSATION_MEMORY_WEBUI_PRODUCTION_ACCEPTANCE_TASKBOOK_V025.md` 同时执行。

ChatGPT Importer Agent 独占或主要修改范围：

```text
src/research_memory_gateway/conversations/readers.py
src/research_memory_gateway/conversations/models.py
src/research_memory_gateway/conversations/identity.py        （仅必要的通用扩展）
src/research_memory_gateway/conversations/identity_store.py  （仅必要的通用扩展）
src/research_memory_gateway/conversations/pipeline.py        （仅平台无关扩展）
src/research_memory_gateway/conversations/cli.py
src/research_memory_gateway/conversations/chatgpt_export.py  （新增）
src/research_memory_gateway/conversations/attachments.py     （仅通用能力不足时）
tests/test_chatgpt_*.py                                      （新增）
docs/CONVERSATION_MEMORY_CHATGPT_IMPORTER_RELEASE_REPORT_V026.md
```

默认禁止修改：

```text
src/research_memory_gateway/webui/**
NAS config / manifest / index / container
production Markdown
production ZIP
```

如果确实发现 Reader contract 存在平台无关缺陷，可以修改公共 conversation 层，但必须写回测试证明 Codex 行为零回归。

建议使用独立 worktree：

```powershell
git worktree add G:\LLM\worktrees\rmg-chatgpt-importer -b feat/chatgpt-importer-v026 49c3969
```

任务书本身可从主工作区读取：

```text
G:\LLM\memory\docs\CONVERSATION_MEMORY_CHATGPT_IMPORTER_TASKBOOK_V026.md
```

---

# 1. 不可妥协原则

1. **ChatGPT 整包导出不具有选择性不是用户问题。** Gateway 必须支持“每次把完整 export ZIP 再扔进来”。
2. **同来源稳定 ID 优先于内容相似。** 同 ChatGPT account namespace + provider conversation id 才是 source identity 基础。
3. **禁止跨平台自动 merge。** Codex 与 ChatGPT 即使 transcript 完全一样，也只能产生 duplicate candidate，不能自动合并 canonical。
4. **ChatGPT 分支不能静默丢弃。** regenerate / edited message / alternate branch 必须有确定性处理策略与测试。
5. **不能把 branch id 塞进 provider conversation id 假装唯一。** `source_conversation_id` 必须保留 ChatGPT 原 provider conversation id；branch 使用独立 `source_branch_id`。
6. **raw export 永远只读。** 不改 ZIP、不改 `conversations.json`、不原地修文件。
7. **不把 email、账号名、token、cookie 等原始 account identifier 写进 manifest/frontmatter/log/report。** 只能保存稳定 namespace hash。
8. **不从网络补附件。** 只处理 export ZIP 内已有资产；缺失资产只能记录 unresolved，不得访问 ChatGPT/OpenAI CDN。
9. **不因 packaging 变化重写所有会话。** archive SHA 变化但某 conversation/branch snapshot 内容不变时必须 unchanged。
10. **不让旧 export 截断新会话。** stale snapshot 必须 skip。
11. **不把 divergence 当 continuation。** 历史中间消息被改写必须 fail-closed 或进入 branch 语义，不得覆盖现有 source note。
12. **Codex v0.2.5 全部行为必须保留。** 现有 322-session 测试与 rehearsal 不得退化。

---

# 2. 完成态架构

```text
ChatGPT Data Export ZIP
       |
       v
ChatGPTExportReader
       |
       +-- archive/package provenance
       +-- conversations.json audit
       +-- conversation graph traversal
       +-- branch extraction
       +-- attachment inventory
       |
       v
ConversationExportReader contract
       |
       v
NormalizedConversation
       |
       v
existing v0.2.5 identity/dedup pipeline
       |
       +-- source_key
       +-- canonical_conversation_id
       +-- snapshot ledger
       +-- continuation/stale/divergence
       +-- duplicate candidate review
       |
       v
canonical Markdown + derived index
```

平台特定逻辑必须尽量止于 `ChatGPTExportReader`；pipeline 只接受必要的通用 contract 扩展。

---

# 3. C0 — 基线与停机条件

开始前记录：

```powershell
git status --short
git branch --show-current
git rev-parse HEAD
git log -5 --oneline
python -m pytest -q
git diff --check
```

预期当前正式基线包含：

```text
49c3969 docs: record v0.2.5 production cutover [skip ci]
a97f80b fix: backfill identity for legacy conversation reads
23adf3a test: stabilize CI test package imports
```

不得 reset/rebase/drop 用户已有历史。

如果基线已经前进，记录新的 `BASE_SHA`，但必须确认 v0.2.5 identity/dedup 语义仍存在。

立即停止并报告的条件：

- production 文件或 NAS mount 被意外修改；
- raw ChatGPT export 被修改；
- 需要保存 raw email/token 才能实现 identity；
- branch 只能靠丢弃才能继续；
- 同 ChatGPT provider id 被不同 account namespace 错误覆盖；
- 跨 ChatGPT/Codex 自动 merge；
- stale export 覆盖 newer source note；
- divergence 被误写成 continuation；
- Codex 现有测试出现数据语义回归。

---

# 4. C1 — 先审计真实 ChatGPT export 格式，不先写 parser

## 4.1 支持目标

首个正式支持目标：ChatGPT Data Export ZIP 中的结构化 conversation 数据，优先 `conversations.json`。

必须先做 schema audit，输出：

```text
archive file inventory
conversations.json presence / top-level type
conversation object field inventory
mapping node field inventory
message field inventory
content_type inventory
role inventory
metadata key inventory
attachment/file reference inventory
timestamp shape
branch/current-node shape
malformed/null count
```

禁止把 `chat.html` 当主解析源。若 ZIP 只有 HTML 而没有结构化 JSON：

```text
UNSUPPORTED_EXPORT_FORMAT
```

显式失败，不静默 HTML scraping。

## 4.2 私有数据规则

真实用户 export 如果存在，只允许：

- 在本机外部路径只读访问；
- 输出 schema/count/hash 类摘要；
- 不把 transcript、账号 email、真实附件内容加入 repo fixture；
- 不把真实 ZIP 放进 git。

tests 中只能使用 synthetic/sanitized fixtures。

如果当前没有真实 ChatGPT export：

- 仍可完成 synthetic contract/tests；
- release report 必须写 `REAL CHATGPT EXPORT REHEARSAL: NOT RUN — fixture unavailable`；
- 不能声称“真实 export 已验证”。

---

# 5. C2 — Reader contract 必须支持“provider conversation + branch”双层 identity

当前 contract 的 legacy 参数名围绕 `conversation_id`，Codex 一条 provider conversation 基本对应一条 linear session；ChatGPT 的 mapping graph 不一定如此。

必须做到：

```text
provider conversation id  !=  reader/import item id
```

建议新增一个 additive 概念，例如：

```text
ExportSessionRef.import_key
```

要求：

- Codex 默认 `import_key == conversation_id`，行为不变；
- ChatGPT 每个可导入 branch 有唯一 `import_key`；
- `source_conversation_id` 永远保存 provider conversation id；
- `source_branch_id` 保存 branch identity；
- pipeline 内部按 `import_key` 调 reader；
- CLI 过滤 bare provider conversation id 时，默认选中该 conversation 的全部 branches，除非显式 `--branch-id`。

参数名是否重命名不重要，语义必须清楚且有测试。

不得使用：

```text
conversation_id = "<provider-id>@<branch-id>"
```

然后让 source identity 误以为这是 provider id。

---

# 6. C3 — ChatGPT graph / branch 处理

## 6.1 Graph validation

对每个 ChatGPT conversation 的 mapping：

1. 构建 `node_id -> node`；
2. 验证 parent 指向；
3. children 中未知 node 记录 schema warning；
4. cycle 必须 fail-closed，不能无限遍历；
5. orphan node 必须计数；
6. 不依赖 JSON object insertion order；
7. traversal 必须 deterministic。

## 6.2 Primary branch 与 alternate branches

如果 export 提供 `current_node`：

- 从 `current_node` 反向 parent 链得到 primary branch；
- primary branch 必须导入。

除此之外：

- 所有包含 human-visible user/assistant 内容的 terminal leaf branch 都必须可表示；
- 与 primary 完全同路径的 leaf 去重；
- system-only / metadata-only orphan 不制造空 branch source record；
- alternate branch 必须有稳定 `source_branch_id`。

branch id 优先级：

1. provider terminal leaf node id（若 export 稳定提供）；
2. provider branch/thread id（若存在）；
3. deterministic hash of ordered provider node/message ids；
4. 最后才允许 hash normalized visible message sequence。

不能用 title、更新时间或 ZIP 顺序生成 branch id。

## 6.3 同 provider conversation 的 branch canonical 关系

ChatGPT 同一 provider conversation 的不同 branches 属于**同来源、同 provider conversation 的分支家族**，这与“跨来源 duplicate merge”不同。

要求：

- branches 各自拥有不同 `source_key`；
- branches 的 `source_branch_id` 不同；
- branches 必须能自动归属同一 canonical conversation family；
- 不能通过 manual duplicate review 才把同一 provider conversation 的 branch 拼起来；
- 这个自动 grouping 只适用于能够由 provider stable identity 证明是同一 conversation 的 branch siblings；
- 绝不能扩展成 ChatGPT 与 Codex 的自动 grouping。

如需通用扩展，新增一个明确的 provider-family canonical key/transaction，不能改变 Codex legacy canonical id。

必须测试：

```text
ChatGPT conversation A branch 1 -> source_key X / canonical C
ChatGPT conversation A branch 2 -> source_key Y / canonical C
ChatGPT conversation B same text -> source_key Z / canonical D
Codex exact same transcript       -> canonical E + duplicate candidate only
```

---

# 7. C4 — NormalizedConversation 映射

## 7.1 Session identity/meta

建议：

```text
source_system      = chatgpt
source_originator  = ChatGPT Data Export
source_surface     = chatgpt_export
source_version     = export/schema version if present, else empty
thread_source      = user
model_provider     = openai (only if supported by export evidence)
model_name         = deterministic dominant/latest assistant model_slug if present
```

不得凭模型知识猜 `model_name`。只有 export metadata 支持时才填。

额外 session_meta 可以保留：

```text
provider_conversation_id
branch_id
branch_kind primary|alternate
primary_current_node
model_histogram
content_type_histogram
hidden_message_count
unsupported_content_count
```

## 7.2 Message roles

human-visible fingerprint roles仍以：

```text
user
assistant
```

为核心。

system/developer/tool 类消息：

- 不得伪装成 user/assistant；
- 可保存在 injected/system metadata 或 tool event；
- 是否出现在 Markdown 取决于现有 writer schema 的安全扩展；
- 不得因系统提示变化制造 visible transcript continuation/divergence 假信号。

## 7.3 Timestamps

ChatGPT export 常见 Unix seconds/float 形式必须转换为明确 UTC ISO-8601。

规则：

- null -> empty，不用当前时间补；
- invalid -> schema warning；
- 不因 timezone locale 改 hash。

## 7.4 Content types

至少对实际 audit 中出现的类型做显式处理。

文本类：规范化成稳定 text。

多模态/结构化 part：

- 字符串 part 正常拼接；
- image/file/audio 等非文本 part 不得静默消失；
- 在 normalized message 中加入稳定占位或附件引用，例如 `[attachment: ...]`，并将真实资产进入 AttachmentRef；
- unsupported content type 要计数并在 report 中列出。

不能直接 `str(dict)` 把 Python dict repr 写进 transcript。

---

# 8. C5 — Per-conversation snapshot provenance，避免“整个 conversations.json 一变所有会话都变”

ChatGPT export 可能把全部会话放在一个 `conversations.json`。

因此绝对不能：

```text
source_sha256 = SHA256(conversations.json whole file)
```

否则任何一场新增都会让全部旧会话 `source_changed`。

必须定义 deterministic per-import-item snapshot：

```text
source_entry = conversations.json#conversation=<provider-id>#branch=<branch-id>
source_sha256 = hash(canonical serialized provider conversation branch snapshot)
source_size_bytes = canonical serialized snapshot byte length
archive_sha256 = whole ZIP sha256
```

canonical serialization 要：

- object keys deterministic；
- 忽略 packaging-only 顺序；
- 保留语义字段；
- parser_version/schema_version 变化不伪造 source payload hash。

验证：

```text
Export A: conversations 1..500
Export B: conversations 1..520
conversation 1..500 unchanged

=> old 500 source_sha256 unchanged
=> 500 skip
=> only 20 new/write
```

---

# 9. C6 — Account namespace

必须支持多 ChatGPT account 不碰撞。

优先级：

1. export 内存在稳定 provider account GUID 且确认不是 secret/email -> hash 后使用；
2. 用户显式提供稳定 namespace label -> `account_namespace_hash("chatgpt", label)`；
3. 没有稳定 account signal 时，不得用 archive SHA 当 namespace（会破坏 repeat export identity）。

建议 CLI：

```text
--account-namespace <stable-local-label>
```

原始 label 不写入 manifest/frontmatter/report，只持久化 hash。

如果缺 namespace 且 export 不提供稳定安全 account id：

- 默认 fail with clear error；或
- 只有用户显式 `--use-default-account-namespace` 才使用文档化的 `chatgpt-default-v1`。

禁止静默按 email hash，除非项目明确把 email 当允许输入；本任务默认不允许。

---

# 10. C7 — Attachments / exported assets

只允许访问 ZIP 内资产。

要求：

- zip-slip/path traversal 防护；
- UTF-8 filename length 安全沿用现有 writer；
- asset content SHA-256；
- stable attachment id；
- 引用不存在 -> unresolved record，不访问网络；
- duplicate asset content 可复用 hash，不重复复制；
- 文件名变化但内容相同不影响 visible transcript fingerprint；
- 附件 inventory hash 仍参与 snapshot identity。

如果 ChatGPT export 使用 file id / asset pointer，必须先从实际 schema audit 得出解析规则，不能靠猜测文件路径。

---

# 11. C8 — Ingestion / dedup / continuation 语义

必须覆盖：

### 11.1 Same full export twice

```text
first import   -> N written/new
second import  -> N skipped/unchanged
```

### 11.2 New complete export with old + new conversations

旧 conversation/branch per-snapshot hash 相同 -> unchanged。

### 11.3 Same branch strict continuation

旧 visible message sequence 是新 sequence 严格前缀：

```text
source_continued
same source_key
same canonical
same Markdown path
```

### 11.4 Old stale export

newer source 已有更多消息，再导旧 snapshot：

```text
stale_snapshot
skip
new note preserved
```

### 11.5 Edited/regenerated branch

如果 provider graph 表明这是 alternate branch：

- 新 branch source record；
- 不覆盖旧 branch note；
- 同 provider-family canonical。

如果 export 无法证明是 branch，且 same source/branch 历史中间 message 被改：

```text
source_diverged
conflict / fail-closed
```

### 11.6 Packaging-only change

ZIP archive/hash/ordering 改变，但 conversation branch snapshot 相同：

```text
unchanged or source_packaging_changed
0 Markdown rewrite
```

### 11.7 Cross-source exact transcript

ChatGPT vs Codex：

```text
possible duplicate candidate
NO automatic merge
```

---

# 12. C9 — CLI / auto-detection

CLI 必须有明确 reader selection。

建议：

```text
--format auto|codex|chatgpt
--account-namespace <label>
--conversation-id <provider id>
--branch-id <branch id>
```

`auto` 必须基于 archive content 检测，而不是文件名猜测。

歧义时：

```text
AMBIGUOUS_EXPORT_FORMAT
```

不任选一个 parser。

现有 Codex CLI 默认行为必须保持。

本任务不要求修改 WebUI import 页面，以保持与并行 WebUI Agent 零冲突；后续 integration 可以把 ChatGPT reader 暴露到 WebUI。

---

# 13. C10 — Release-blocker 测试矩阵

至少新增以下 synthetic tests。

## A. Reader/schema

1. conversations.json list 正常解析。
2. ZIP 缺 conversations.json -> clear unsupported error。
3. malformed top-level -> fail closed。
4. deterministic list order。
5. timestamp null/float/int。
6. unknown content_type 不静默丢失。
7. mapping missing parent 可报告。
8. graph cycle 被拒绝。

## B. Branches

9. simple linear -> 1 branch。
10. current_node primary branch。
11. regenerate assistant -> 2 branches。
12. edited user branch -> 2 branches。
13. shared prefix 不重复制造错误 provider id。
14. branch source_keys distinct。
15. branch canonical same family。
16. branch ordering deterministic。

## C. Identity/account

17. same provider id + same account namespace -> same source identity。
18. same provider id + different account namespace -> distinct source_key。
19. account raw label/email 不出现在 persisted metadata。
20. no stable namespace -> fail/explicit opt-in default。

## D. Full-export dedup

21. exact same ZIP twice -> second all skipped。
22. new ZIP with 20 new conversations -> old snapshots unchanged。
23. JSON object reorder -> unchanged。
24. title changed, transcript unchanged -> path remains stable。

## E. Continuation/stale/divergence

25. strict continuation -> source_continued。
26. stale snapshot -> skip。
27. middle-message edit same branch -> source_diverged unless graph proves alternate branch。
28. alternate branch never overwrites sibling note。

## F. Duplicate safety

29. exact ChatGPT/Codex transcript -> candidate only。
30. near duplicate -> pending candidate only。
31. rejected pair reimport does not resurrect。
32. confirmed canonical collapse still preserves both source notes。

## G. Attachments

33. safe exported asset -> AttachmentRef/hash。
34. missing asset -> unresolved, no network。
35. zip traversal rejected。
36. duplicate content stable hash。

## H. Codex regressions

37. existing Codex import tests all pass。
38. v0.2.5 legacy read identity test passes。
39. formal migration rehearsal tests pass。
40. full `python -m pytest -q` pass。

---

# 14. C11 — Real-export rehearsal（如果真实 ChatGPT export 可用）

必须在独立 scratch/workdir；不得对 NAS production 操作。

步骤：

```text
A. SHA256 raw ZIP before
B. schema audit
C. dry-run all import items
D. real import to isolated staging/manifest
E. count Markdown/source/canonical/branches
F. same ZIP repeat -> all unchanged
G. rebuild lexical index in isolated SQLite
H. search/read/recall smoke
I. raw ZIP SHA256 after == before
```

如果随后有第二份更新后的 ChatGPT export：

```text
old unchanged count
new conversation count
continued branch count
new branch count
stale count
conflict count
```

必须能解释每一类。

不要在本任务中做全量 embedding backfill；lexical/index correctness 先验收，向量策略沿用 production 渐进模式。

---

# 15. C12 — 性能与资源门禁

ChatGPT 全量 export 可能明显大于 322 sessions。

要求：

- list/audit 不把附件全部读入内存；
- conversations.json 可一次加载时记录峰值；若文件明显大，考虑 streaming parser，但不要过度工程；
- 单个 branch canonical serialization 不能造成全 archive O(N²)；
- same full export 第二次不做无必要 Markdown 重写；
- 不新增后台线程/定时任务；
- 不在 import hot path 调 LLM；
- 不自动全量 vector backfill。

报告至少记录：

```text
archive bytes
conversation objects
branch import items
visible messages
attachments
first import seconds
repeat import seconds
peak RSS if easy to measure
```

---

# 16. 文档与报告

新增：

```text
docs/CONVERSATION_MEMORY_CHATGPT_IMPORTER_RELEASE_REPORT_V026.md
```

报告必须包含：

```text
BASE SHA
final local SHA
files changed
reader/parser/schema version
real export audit status
account namespace strategy
branch strategy
source/canonical strategy
attachment strategy
full-export dedup results
continuation/stale/divergence results
cross-source duplicate safety
Codex regression status
pytest count
diff-check
raw ZIP immutable proof if real rehearsal ran
production modified? NO
```

不得包含：

- ChatGPT account email；
- session token；
- private transcript excerpts；
- raw attachment content；
- secret config。

---

# 17. 最终 gate

必须同时满足：

```text
ConversationExportReader platform abstraction preserved     PASS
ChatGPT full export recognized                              PASS
per-conversation/branch snapshot hash deterministic         PASS
same full export repeat = all unchanged                     PASS
provider conversation id preserved                          PASS
branch identity deterministic                               PASS
branch siblings source-distinct / canonical-family          PASS
same account identity stable                                PASS
different account namespaces isolated                       PASS
strict continuation                                         PASS
stale snapshot protection                                   PASS
divergence fail-closed                                      PASS
cross-source auto merge                                     ZERO
raw account identifier persisted                            ZERO
network attachment fetch                                    ZERO
Codex regressions                                           ZERO
full pytest                                                 PASS
git diff --check                                            PASS
production modified                                         NO
```

如果没有真实 ChatGPT ZIP，允许：

```text
REAL EXPORT REHEARSAL = NOT RUN
```

但不能写 `PRODUCTION READY`；只能写：

```text
IMPLEMENTATION READY / REAL EXPORT VALIDATION PENDING
```

---

# 18. Git / 停机规则

完成后：

1. 检查 staged 文件；
2. 确认没有 ZIP/DB/config/secret；
3. `git diff --cached --check`；
4. 创建本地 commit；
5. 停止并汇报。

禁止：

```text
git push
git tag
GHCR publish
NAS production import
修改 production manifest/index/Markdown
```

除非用户再次明确授权。

---

# 19. Agent 最终汇报模板

```text
TARGET:
  v0.2.6 ChatGPT Export Importer

GIT:
  base SHA:
  final local SHA:
  pushed?: NO

FORMAT AUDIT:
  real export available?:
  conversations.json:
  conversations:
  branches:
  content types:
  attachment refs:

IDENTITY:
  account namespace strategy:
  provider-id preserved?:
  branch source keys distinct?:
  branch canonical family?:
  raw account identifiers persisted?: NO

DEDUP:
  same full export repeat:
  continuation:
  stale:
  divergence:
  cross-source auto merge?: NO

TESTS:
  targeted:
  full pytest:
  diff-check:

REAL REHEARSAL:
  status:
  raw ZIP unchanged?:
  first import:
  repeat import:

PRODUCTION:
  modified?: NO

READINESS:
  IMPLEMENTATION READY / REAL EXPORT VALIDATION PENDING
  or
  READY FOR INTEGRATION REVIEW
```

