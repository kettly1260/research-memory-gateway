# Conversation Memory Pipeline 第二轮 Hardening 与最终验收任务书

任务日期：2026-09-12  
执行项目：`G:\LLM\memory`  
建议执行 Agent：Flash Agent  
任务性质：在第一轮补漏基础上继续修复独立复核发现的剩余阻塞项；只做定向 hardening，不重构现有架构，不扩大任务边界。

## 0. 当前状态与本轮目标

第一轮补漏后，以下事实已经独立复核成立：

- 全库 `python -m pytest -q` 当前为 `160 passed, 3 warnings`。
- `parent_thread_id` 检索已经能真实命中子会话。
- `Fe3+` 等带特殊符号词已经不再触发 FTS5 syntax error。
- attachment inventory 已能把 3 个 clipboard 文件从 slash/backslash 重复中正确去重。
- writer 的 machine-managed / manual region 基础机制已经存在。
- `canonical_subdir`、`--confirm-vault`、changed-only、JSON report 等基础功能已经加入。

但独立复核确认仍有若干会阻塞正式全量导入的工程问题，因此当前仍不能宣称：

```text
R0-R12 全部完成
P0-P8 已最终验收
可以开始 322 场全量导入
可以开始批量 embedding
```

本轮目标只有一个：

> 把当前系统从“主体功能可运行”修到“可安全进入正式全量导入前最后确认”的状态。

---

## 1. 本轮不可违反的安全边界

继续严格禁止：

- 全量导入 322 场会话。
- 向正式 Vault 批量写入。
- 批量调用 NAS embedding。
- 修改或重启 Unraid。
- commit / push。
- 修改原始 export ZIP。
- 删除旧 Gateway 数据库或历史 embedding。

原始 ZIP 仍为 immutable：

```text
D:\Partition\F\Study\博士文件\Research-AI-Hub\_codex_workspace\tmp\chat-export\codex-sessions-20260912-012538.zip
```

正式 canonical Vault 路径仍未由用户确认，因此不得解除 Vault 门控。

---

## 2. 本轮独立复核确认的剩余缺口

以下问题均已实际复现，不是推测。

### H1. Real E2E 测试并不真正隔离

当前：

```python
test_real_5_sessions_isolated_e2e(tmp_path)
```

虽然接收 `tmp_path`，但内部实际上硬编码：

```text
G:/LLM/memory/exports/conversation-staging/remediation-validation
```

结果：

- pytest 会修改持久 staging。
- 测试依赖上一次已经生成的数据。
- 测试可能因为 manifest 返回 `skipped` 而没有真正执行完整导入链路。

### H2. E2E 测试正在污染真实验收数据

测试会向 Nihao note 追加：

```text
- 人工添加的研究待办 [[TODO]]
```

每运行一次测试就再追加一次。

独立复核时当前文件已经累计 6 条重复 TODO。

### H3. `index_stale` 没有接到真实 pipeline/index 生命周期

`ImportManifest.record_indexing()` 当前只在测试中调用。

实际 CLI：

```text
conversation index
conversation rebuild-index
```

没有把 indexing 结果回写 `.ai-memory/manifest.sqlite`。

真实 5-session manifest 中：

```text
last_indexed_at = ''
index_source_hash = ''
embedding_model = ''
embedding_version = ''
embedding_dimension = NULL
```

因此 `index_stale` 仍只是 isolated unit behavior，不是完整业务闭环。

### H4. Heading chunking 的 oversize 限制实际失效

当前 `_split_oversize()` 只按 paragraph 分割。

若单个 paragraph 本身超过 `max_chunk_chars`，不会进一步拆分。

独立复核：

```text
max_chunk_chars = 1500
实际得到 chunk = 2399 chars / 2399 chars
```

真实 5-session index 中最大 section 达：

```text
56,265 chars
```

这不满足原任务书：

```text
heading -> subheading -> oversize token split
```

### H5. Recall token budget 可被首条大结果绕过

当前逻辑：

```python
if accumulated_chars + len(content) > char_budget and recalled_items:
```

当第一条结果已经超预算时，`recalled_items` 为空，于是整条直接加入。

独立复核：

```text
recall("Fe3+", token_budget=10)
```

仍返回首条约：

```text
56,265 chars
```

因此 P7 的 token-budget 目标尚未完成。

### H6. `--resume` 当前是空参数

CLI 定义：

```text
--resume
```

但生产逻辑没有读取 `args.resume` 来决定行为。

当前只是：

```json
"resume": true/false
```

写入 JSON summary。

Pipeline 本身虽然默认具备部分幂等续跑能力，但不能把一个没有语义的 CLI flag 视为完成。

### H7. `--staging-dir` 可绕过 canonical Vault 确认门控

当前：

```python
if args.staging_dir:
    output_root = Path(args.staging_dir).resolve()
```

随后直接：

```python
output_root.mkdir(...)
```

没有验证该路径属于允许的 staging 根。

因此用户理论上可以把正式 Vault 路径通过 `--staging-dir` 传入，从而绕过：

```text
--vault --confirm-vault
```

### H8. Attachment inventory hash 没有真正参与完整 pipeline

Pipeline 目前只根据：

```python
conversation.attachments
```

构造简单 hash。

但完整 `AttachmentInventory.scan_*()` 还能发现 message/tool 中的其他 local path、unresolved 等 locator。

真实 5-session 中：

```text
019ea2ad -> inventory 8 条
01a08fdc -> inventory 2 条
```

但对应 manifest 的：

```text
attachment_inventory_hash = ''
```

因此 `attachment_changed` 仍未完整接入真实 inventory 生命周期。

### H9. Index run ledger 丢失 skipped_files

`record_index_run()` 函数接收：

```python
skipped_files=
```

但 `conversation_index_runs` 表没有该列，INSERT 也没有写该值。

因此报告中的 changed-only 统计无法从 ledger 追溯。

### H10. `rebuild-index` 缺少 dry-run

当前 `rebuild-index`：

- 有 `--confirm-rebuild`。
- 没有 `--dry-run`。

一旦未来 embedding enabled，确认 rebuild 后可能直接开始 embedding。

任务书要求：

```text
dry-run 不调用模型
dry-run 不修改 index
报告预计需要 embedding 的 chunk 数
```

### H11. 新增 path resolver 没有在生产 CLI 全部使用

虽然 config 已有：

```python
resolve_manifest_path()
resolve_index_path()
```

但生产 CLI 仍存在：

```python
Path(cfg.conversation_archive.index_path)
```

直接使用配置字符串的路径。

因此新安全 resolver 并未成为唯一入口。

### H12. Embedding incremental identity 检查不完整

`check_changed()` 当前主要检查：

```text
file_hash
是否存在同 model 的 embedding
```

但没有完整核对：

```text
embedding_identity
embedding_version
model version
section content identity
```

同 model、不同 embedding version 时，可能错误判 unchanged。

### H13. Source anchor 报告表述仍有混淆

真实查询：

```text
TOC_Focused.png
```

最高分结果是：

```text
工具活动
score ~= 0.2366
source_anchors = []
```

有 `ordinal=6` anchor 的是后面的“关键原始内容 > 用户”结果。

报告当前把最高分结果与有 anchor 的第二结果合并描述。

### H14. Report 中 embedding 配置路径描述错误

报告写：

```text
conversation_archive.embedding.enabled
```

实际代码读取：

```python
cfg.retrieval.embedding.enabled
```

### H15. NAS smoke 的“连通性正常”缺少可追溯证据

实施报告写：

```text
连通性正常
```

但本轮执行记录未见明确 `smoke_bge_m3.py` 成功输出。

另外脚本当前网络不可达时返回 `0`，容易把：

```text
SKIP
```

误当：

```text
PASS
```

### H16. Guardian parser fixture 仍未覆盖

原任务书 P2 要求 fixture 覆盖：

```text
ordinary
subagent
guardian
tool
image
compaction
abort
corrupt JSONL
```

当前 conversation tests 中仍没有 guardian fixture。

---

## 3. 执行顺序

必须按以下顺序修复：

```text
H0 baseline freeze
-> H1 true isolated E2E
-> H2 chunk + recall budget hardening
-> H3 manifest <-> index lifecycle
-> H4 path safety + CLI semantics
-> H5 attachment inventory integration
-> H6 embedding/rebuild/index run hardening
-> H7 parser coverage
-> H8 real validation rebuild
-> H9 report correction
```

禁止一边修测试、一边继续使用被测试污染的旧 `remediation-validation` 目录作为唯一真值。

---

## 4. H0：冻结当前基线

### 必做

记录：

```text
git root
branch
HEAD
git status --short
```

执行：

```powershell
python -m pytest -q
git diff --check
```

### 当前独立复核基线

```text
160 passed, 3 warnings
```

本轮最终测试数可以增加，但已有 160 项不得回归。

---

## 5. H1：把 Real E2E 改成真正隔离、无副作用

### 5.1 不得再写固定 staging

修改：

```text
tests/test_conversation_real_e2e.py
```

必须真正使用：

```python
tmp_path
```

例如：

```python
staging_dir = tmp_path / "real-e2e-staging"
```

测试不得写：

```text
G:\LLM\memory\exports\conversation-staging\remediation-validation
```

### 5.2 测试必须从空目录开始

测试前断言：

```text
manifest 不存在
Markdown 不存在
index 不存在
```

第一轮导入必须真实产生：

```text
written = 5
skipped = 0
```

第二轮才允许：

```text
written = 0
skipped = 5
```

### 5.3 禁止测试污染持久 staging

E2E 中人工编辑必须修改 `tmp_path` 内文件。

测试结束后，不得在仓库 staging 目录留下：

```text
TODO
candidate
test artifact
extra manifest
```

### 5.4 新增防回归测试

增加断言：

```python
assert "remediation-validation" not in str(staging_dir)
```

或更合理的路径隔离断言。

### 验收

- 连续运行 `pytest tests/test_conversation_real_e2e.py -q` 两次。
- 仓库 `exports/conversation-staging/remediation-validation` 文件 hash 不发生变化。
- E2E 每次第一轮都能真实 `written=5`。

---

## 6. H2：修复 oversize chunk 与 recall token budget

### 6.1 Chunker 必须真正有 hard upper bound

当前只按 blank-line paragraph split 不够。

要求层级：

```text
heading
-> paragraph
-> sentence / line
-> hard split fallback
```

推荐：

```text
soft target: token-based
hard safety cap: chars
```

如果当前暂不引入 tokenizer，也至少保证：

```python
len(chunk.content) <= max_chunk_chars
```

对任何输入都成立。

不能出现：

```text
max=1500
actual=2399
```

### 6.2 超大 tool activity table 也要拆

真实最大 56k chunk 主要来自工具活动。

不能只修普通 paragraph fixture。

至少增加：

- 超长 Markdown table。
- 超长单行 payload excerpt。
- 无空行长段落。
- 中文长文本。
- code block。

### 6.3 Source anchor 继承

如果一个 anchored message 被拆成多个 chunk：

```text
所有子 chunk 必须继承相同 anchor
```

如果工具活动没有 message anchor，可保持空，但不得把别的 message anchor 错挂上去。

### 6.4 Recall budget 必须从第一条就生效

重写 budget 逻辑，第一条超预算时也必须：

- 截断；或
- 跳过；或
- 分块后返回。

不能整条越界。

建议 helper：

```python
truncate_to_budget(content, remaining_budget)
```

### 验收

自动化测试至少断言：

```python
all(len(c.content) <= max_chunk_chars for c in chunks)
```

以及：

```python
recall(..., token_budget=10)
```

最终 context 不能超过设计允许的安全误差范围。

真实 5-session 重建后：

```text
max section length << 56,265
```

报告给出实际最大值。

---

## 7. H3：Manifest 与 Index 生命周期真正闭环

### 7.1 CLI index 必须定位对应 manifest

对于 staging：

```text
<staging>/.ai-memory/manifest.sqlite
```

对于 canonical：按配置安全解析。

### 7.2 每个成功 index file 后回写 manifest

调用：

```python
manifest.record_indexing(...)
```

至少写入：

```text
index_source_hash
last_indexed_at
content_section_hashes
embedding_model
embedding_version
embedding_dimension
```

### 7.3 `index_stale` 必须变成真实状态

真实流程：

```text
import Markdown
-> index
-> manifest indexed metadata updated
-> manual-only edit
-> 不应 stale machine content
-> managed/source change
-> index_stale
-> reindex
-> stale cleared
```

### 7.4 CLI changed-only 要优先利用 manifest + index

至少区分：

```text
new_document
changed_file
index_stale
embedding_identity_changed
unchanged
```

### 7.5 真实 E2E 必须检查 manifest

5 场 index 后断言：

```text
last_indexed_at 非空
index_source_hash 非空
content_section_hashes 非空
```

若 embedding disabled：

```text
embedding_model/version/dimension 可为空
```

若 mock enabled：必须有值。

---

## 8. H4：路径安全和 CLI resume 语义修复

### 8.1 `--staging-dir` 必须走安全解析

不得：

```python
Path(args.staging_dir).resolve()
```

直接信任。

设计一个明确规则：

方案 A：

```text
--staging-dir 只能位于 configured staging root 内
```

方案 B：

```text
--staging-dir 必须位于 project root / explicit allowed roots 内
```

无论选哪个，都必须保证：

```text
正式 vault_root 不可通过 --staging-dir 绕过 --confirm-vault
```

### 8.2 `resolve_manifest_path()` / `resolve_index_path()` 成为生产唯一入口

CLI 中不应再直接：

```python
Path(cfg.conversation_archive.index_path)
```

统一改为 resolver。

### 8.3 `--resume` 必须有真实语义

两种可接受方案：

#### 方案 A：resume 默认开启

```text
--resume / --no-resume
```

默认：

- unchanged skip。
- failed_retryable retry。

`--no-resume`：明确从所选 subset 强制重新评估，但仍必须保留安全冲突机制。

#### 方案 B：删除无意义 flag

如果系统设计就是天然 resume-safe：

- 删除 `--resume`。
- 文档明确“resume 默认行为”。

不能保留一个只出现在 JSON report 里的假 flag。

### 8.4 路径测试

新增：

- staging 逃逸。
- staging 指向 vault root。
- staging 指向 canonical subdir。
- absolute 外部 path。
- manifest/index relative escape。

---

## 9. H5：AttachmentInventory 真正接入 ingestion lifecycle

### 9.1 Pipeline 不得只 hash normalized attachments

当前：

```python
conversation.attachments
```

不足以覆盖完整 inventory。

需要把：

```text
AttachmentInventory.scan_one(conversation)
```

纳入 ingestion orchestration，或引入等价 deterministic inventory hash helper。

### 9.2 Hash 输入必须稳定

推荐对标准化 records：

```text
locator_type
canonical_locator
status
content_hash
size
message_id/tool_call_id/ordinal
```

排序后 canonical JSON hash。

不要 hash：

```text
observed_locators 的非稳定顺序
临时时间戳
绝对 resolved path 中随机 temp root
```

### 9.3 `attachment_changed` E2E

自动化 fixture：

1. 首次 inventory hash A。
2. locator/status/content hash 变化。
3. manifest decide -> `attachment_changed`。

### 9.4 真实 5-session

重新导入后 manifest 应合理出现 inventory hash。

至少：

```text
019ea2ad
01a08fdc
01a01289
```

这些 inventory 有记录的会话不应继续为空。

---

## 10. H6：Index run、rebuild 与 embedding 增量 hardening

### 10.1 修复 index run schema

增加：

```text
skipped_files INTEGER NOT NULL DEFAULT 0
```

如有需要还可增加：

```text
failed_files
dry_run
embedding_requested
embedding_reused
embedding_failed
```

但不要过度设计。

Migration 必须兼容现有 DB。

### 10.2 `rebuild-index --dry-run`

新增：

```text
--dry-run
```

dry-run 时：

- 不删除表。
- 不写 index。
- 不调用 embedding。
- 统计 Markdown file 数。
- 统计预计 chunks。
- 若 embedding enabled，统计预计需要新 embedding 的 chunk/identity 数。

### 10.3 Embedding identity 完整判定

`check_changed()` 不能只判断：

```text
model 是否已有 embedding
```

必须核对：

```text
section embedding_identity
model
embedding_version
dimension
```

如果：

```text
内容不变
model 不变
version 改变
```

应只补新的 embedding，不必重做 FTS content。

### 10.4 旧 embedding 行清理策略

明确以下语义：

- stale section 被删除后，其 orphan embedding 是否保留 cache。
- rebuild 后是否允许历史 identity cache 保留。

建议：

```text
允许按 identity 保留 cache，但 search 只能 join live sections
```

并补测试。

### 10.5 Mock embedding tests

至少覆盖：

- disabled -> 0 network call。
- first index -> N calls。
- identical rerun -> 0 new calls。
- one chunk changed -> 1 new call。
- embedding version changed -> affected chunks 重新生成。
- dimension mismatch -> fail closed。

---

## 11. H7：Parser guardian fixture 补齐

新增 guardian fixture 或等价真实格式 fixture。

至少验证：

```text
thread_source / agent role
parent thread
injected content classification
message visibility
tool/event mirror
completion status
```

不要只添加名为 guardian 的空测试。

必须基于 export schema 中真实可出现的 guardian 结构。

---

## 12. H8：重新建立干净的真实 5-session 验收集

当前 `remediation-validation` 已被旧 E2E 测试污染，不再作为唯一验收源。

创建新的人工验收目录，例如：

```text
exports/conversation-staging/final-hardening-validation/
```

注意：

- 这是人工 CLI 验收目录。
- pytest 不得写这个目录。

### 12.1 从零导入

只导入固定 5 场：

```text
019e3ad1-05d6-7382-972f-0d377e6092c6
019eab7a-3a54-70b1-afd2-b89c0c98e8b2
01a08fdc-6da5-7f93-9119-ff79d9fea710
01a01289-e1b4-7242-98d9-368a75a16194
019ea2ad-9a74-75c2-bf10-4246ca00ab25
```

首次必须报告：

```text
written = 5
```

二次：

```text
skipped = 5
```

### 12.2 Index

首次：

```text
indexed_files = 5
```

二次：

```text
skipped_unchanged = 5
```

并核对 manifest：

```text
last_indexed_at
index_source_hash
content_section_hashes
```

### 12.3 Chunk health report

输出：

```text
total sections
max chars
p95 chars
sections over configured cap
sections with anchors
sections without anchors
```

要求：

```text
sections over hard cap = 0
```

### 12.4 Recall budget

至少测试：

```text
token_budget = 10
token_budget = 100
token_budget = 1500
```

不得出现第一条直接返回几十 KB。

### 12.5 Parent thread

继续验证：

```text
01a08c1c-b49b-70a2-b1a4-cad68e4b1cd4
-> 01a08fdc-6da5-7f93-9119-ff79d9fea710
```

### 12.6 Source anchor

报告必须分别写：

```text
top result
first anchored result
```

不能再把二者合并描述。

---

## 13. H9：重新修订实施报告

更新：

```text
docs/CONVERSATION_MEMORY_IMPLEMENTATION_REPORT.md
```

### 必须修正

1. 删除 `R0-R12 全部完成`，直到本轮全部验收通过后才允许重新写。
2. 修正 embedding 配置路径：

```text
cfg.retrieval.embedding.enabled
```

3. NAS smoke 必须标记为：

```text
PASS
SKIP/unreachable
FAIL
NOT RUN
```

四者之一。

不能把 SKIP 写成“连通性正常”。

4. `TOC_Focused.png` 查询表分别说明：

- top result 是否有 anchor。
- anchored result 排名与 anchor。

5. 报告新增：

```text
max chunk chars
p95 chunk chars
recall budget verification
manifest-index linkage verification
E2E temp isolation verification
```

6. 若 `--resume` 被删除，应说明 resume 默认语义。

7. 若 `--resume` 被保留，应说明其实际行为及测试。

---

## 14. 本轮最低新增测试要求

### E2E isolation

- real E2E 使用 tmp_path。
- first run written=5。
- second run skipped=5。
- repo staging 不被修改。

### Chunking

- single paragraph > cap。
- single line > cap。
- Markdown table > cap。
- code block > cap。
- Chinese text > cap。
- 每个 chunk <= cap。

### Recall

- first result oversize。
- budget=10。
- budget=100。
- context 不突破安全上限。

### Manifest/index

- CLI index 后 manifest last_indexed_at 非空。
- index_source_hash 与 file/index source 对齐。
- managed/source change -> index_stale。
- reindex -> stale cleared。

### Attachment

-完整 inventory hash deterministic。
- locator/status change -> attachment_changed。

### Path safety

- `--staging-dir` 指向 vault 被拒绝。
- staging traversal 被拒绝。
- config index/manifest resolver 被 CLI 实际使用。

### Embedding

- version change re-embed。
- identity cache reuse。
- dry-run zero calls。

### Index run

- skipped_files 真正写入 SQLite。

### Parser

- guardian fixture。

---

## 15. 最终验收门槛

只有全部满足以下条件，才可以建议用户进入正式全量导入前确认阶段。

### A. 自动化测试

```text
python -m pytest -q
0 failed
0 error
```

旧 160 项全部保留通过。

### B. 测试隔离

- pytest 不修改 `exports/conversation-staging/final-hardening-validation`。
- pytest 不修改旧 `remediation-validation`。
- real E2E 每次从 tmp_path 空目录开始。

### C. Chunk / Recall

- section hard cap 100% 生效。
- 无 56k 单 chunk。
- token budget 对第一条结果也生效。

### D. Manifest / Index

- 5 场真实 manifest 的 `last_indexed_at` 非空。
- `index_source_hash` 非空。
- `index_stale` 可真实产生并修复。
- attachment inventory hash 有效。

### E. Incremental

- import second run：5 skipped。
- index second run：5 skipped_unchanged。
- embedding second run：0 unnecessary calls。

### F. Path safety

- `--staging-dir` 不可绕过 Vault confirmation。
- 所有 manifest/index path 都走 resolver。

### G. Reporting

- synthetic / real / smoke 三类证据完全分离。
- SKIP 不写成 PASS。
- top result 与 anchored result 不混淆。

### H. Safety

- 未导入其余 317 场。
- 未写正式 Vault。
- 未批量 NAS embedding。
- 未改 Unraid。
- 未 commit/push。

---

## 16. 建议执行命令

### 基线

```powershell
git status --short
python -m pytest -q
git diff --check
```

### 重点测试

```powershell
python -m pytest tests/test_conversation_real_e2e.py -q
python -m pytest tests/test_conversation_index_p6.py -q
python -m pytest tests/test_conversation_retrieval_p7.py -q
python -m pytest tests/test_conversation_manifest_p5.py -q
python -m pytest tests/test_conversation_cli_p8.py -q
```

### 最终

```powershell
python -m pytest -q
git diff --check
```

真实人工验收使用新的：

```text
exports/conversation-staging/final-hardening-validation
```

不要让 pytest 使用这个目录。

---

## 17. 给 Flash Agent 的直接指令

```text
继续在 G:\LLM\memory 当前未提交工作树上执行第二轮 hardening。

先读取：
docs/CONVERSATION_MEMORY_IMPLEMENTATION_TASKBOOK.md
docs/CONVERSATION_MEMORY_REMEDIATION_TASKBOOK.md
docs/CONVERSATION_MEMORY_HARDENING_TASKBOOK_V2.md
docs/CONVERSATION_MEMORY_IMPLEMENTATION_REPORT.md

不要重做现有 conversation 架构，只修本任务书确认的剩余问题。

最高优先级：
1. 把 tests/test_conversation_real_e2e.py 改成真正使用 tmp_path，禁止再写固定 remediation-validation；当前测试已经导致 Nihao note 重复追加多条 TODO。
2. 修复 HeadingChunker 单段落/单行 oversize 不拆的问题；max_chunk_chars 必须成为 hard upper bound。当前真实 index 最大 chunk 约 56,265 chars。
3. 修复 recall 第一条结果可绕过 token_budget 的 bug。
4. 把 ImportManifest.record_indexing 真正接入 CLI index/rebuild 流程；真实 5-session manifest 当前 last_indexed_at/index_source_hash 全为空。
5. 修复 --staging-dir 可绕过 --vault --confirm-vault 的安全问题，并让 resolve_index_path/resolve_manifest_path 成为生产 CLI 的统一入口。
6. 把完整 AttachmentInventory hash 接入 pipeline/manifest，不要只 hash conversation.attachments。
7. 修复 index_runs skipped_files schema/写入；增加 rebuild-index --dry-run。
8. 完整检查 embedding identity/model/version/dimension 的增量行为。
9. 补 guardian parser fixture。
10. 最后新建独立人工验收目录 exports/conversation-staging/final-hardening-validation，只导入固定 5 场；pytest 不得写这个目录。

完成后更新 docs/CONVERSATION_MEMORY_IMPLEMENTATION_REPORT.md。

报告必须区分 synthetic fixture、real 5-session staging、optional NAS smoke；SKIP 不能写 PASS。TOC_Focused.png 的 top result 和有 source anchor 的后续 result 必须分开描述。

禁止全量导入 322 场、禁止正式 Vault 写入、禁止批量 embedding、禁止修改/重启 Unraid、禁止 commit/push。

最终必须附：
- 全库 pytest；
- git diff --check；
- true isolated E2E first-run written=5 / second-run skipped=5；
- max/p95 chunk length；
- recall budget=10/100/1500 验证；
- manifest-index linkage；
- attachment inventory hash；
- changed-only / embedding cache 统计；
- parent-thread 与 source-anchor 真实查询；
- 所有仍未解除的生产门控。
```

