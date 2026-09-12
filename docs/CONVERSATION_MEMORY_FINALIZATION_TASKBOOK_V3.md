# Conversation Memory Pipeline V3 最终收尾与生产前验收任务书

任务日期：2026-09-12  
执行项目：`G:\LLM\memory`  
建议执行 Agent：Flash Agent  
任务性质：在 V2 hardening 已基本通过的基础上，只修复独立复核发现的最后一组边缘状态和生命周期缺口；禁止扩大架构、禁止重写已有 conversation 子系统。

---

## 0. 当前独立复核结论

V2 之后，以下结果已经由独立复核重新确认，不需要重做：

- 全库测试：`171 passed, 3 warnings`。
- `git diff --check`：通过。
- Real E2E 已使用 `tmp_path`，不再污染固定 staging。
- 真实 5-session：首次 `5 written`，第二次 `5 skipped`。
- 真实 5-session index：首次 `5 indexed / 173 chunks`，第二次 `5 skipped_unchanged`。
- Chunk hard cap 已真正生效：真实最大 `1498 chars`，无 `>1500` section。
- `last_indexed_at / index_source_hash / content_section_hashes` 已真实回写 manifest。
- `conversation_index_runs.skipped_files` 已真实落库。
- parent thread 查询真实可命中。
- `TOC_Focused.png` 的 top result 与 first anchored result 已在报告中分开。
- guardian fixture 已补齐。
- NAS `bge-m3` 单次 smoke 已由独立复核重新执行并 PASS，维度 `1024`。

因此当前系统已经具备：

```text
Raw ZIP
-> parser
-> Markdown staging
-> manifest
-> heading-aware FTS index
-> lexical retrieval
-> incremental staging import/index
```

的可靠工程基础。

但独立复核仍确认以下问题会阻塞：

```text
正式 canonical Vault 全量导入
+
完整 attachment 内容级增量
+
BGE-M3 批量 embedding
```

本 V3 任务只修这些最后问题。

---

## 1. 安全边界继续保持

本轮仍然禁止：

- 322 场正式 canonical Vault 全量导入。
- 批量 NAS embedding。
- 修改或重启 Unraid。
- 修改原始 export ZIP。
- 删除现有 Gateway 数据库或历史 embedding cache。
- commit / push。
- 擅自清理当前 dirty working tree。

允许：

- synthetic fixture。
- `tmp_path` E2E。
- 固定 5-session staging 人工验收。
- 单次 NAS embedding smoke。
- 小规模 mock embedding 测试。
- 如有必要，重新生成独立 5-session validation index/manifest。

正式 Vault 路径仍必须保持显式确认门控。

---

## 2. V3 独立复核确认的剩余缺口

### V3-1. Embedding version rollback 会把旧 cache 误判为 live identity 已切回

当前正向升级：

```text
v1 -> v2
```

基本可工作。

但独立复核实际复现：

```text
v1 index
-> v2 index
-> 使用 v1 ConversationIndexDatabase 再次 check_changed_reason()
```

返回：

```text
unchanged
```

但 live `conversation_sections.embedding_identity` 仍是 v2 identity。

独立复核输出：

```text
reason unchanged
live_matches_expected_v1 False
```

原因：当前 `check_changed_reason()` 只检查：

```text
期望 identity 是否已经存在于 conversation_embeddings cache
```

没有检查：

```text
live conversation_sections.embedding_identity
是否等于当前 model/version 生成的期望 identity
```

因此：历史 cache 的存在被错误当成“当前 live section 已处于该版本”。

这会导致：

```text
query vector 使用 v1
document live vector 实际仍对应 v2
```

属于批量 embedding 前的 BLOCKER。

### V3-2. `index_metadata_for_file()` 会读错 embedding version

新 schema 已允许历史 identity cache 共存：

```text
UNIQUE(embedding_identity, model, version)
```

这是正确方向。

但当前 metadata 查询仍存在基于 chunk id 的 join：

```sql
JOIN conversation_sections s ON s.id = e.id
```

同一个 chunk id 历史上可能有：

```text
v1 embedding
v2 embedding
```

独立复核实测，在 live section 已经切到 v2 后：

```text
index_metadata_for_file()["embedding_version"] == "v1"
```

因此 manifest 可能记录错误 embedding metadata。

### V3-3. Vector search 必须显式绑定当前 live identity + model + version

当前 vector search 已按：

```sql
s.embedding_identity = e.embedding_identity
```

join，比旧版安全。

但 V3 必须补足以下约束：

- 当前 active model。
- 当前 embedding version。
- dimension 与 query vector 一致。
- 同一 identity 历史上不允许因为其他 model/version 行而重复进入候选。

### V3-4. Recall budget 只约束 item content，不约束最终 Agent context

V2 已修复第一条 56k chunk 绕过正文 budget 的问题。

这一点成立。

但当前 budget 逻辑只计算：

```text
sum(len(item["content"]))
```

最后构造 context 时又额外加入：

```text
### [conversation] heading
Source: absolute_path
anchor metadata
--- separators
```

独立复核真实 5-session：

```text
token_budget=10
item content=20 chars
final context=439 chars

token_budget=100
item content=200 chars
final context=619 chars

token_budget=1500
item content=3000 chars
final context=4271 chars
```

所以现在已经没有“几十 KB 首条爆炸”，但 `token_budget` 仍不是最终 Agent context budget。

### V3-5. Pipeline AttachmentInventory 没有使用配置 allowlist

当前 ingestion pipeline：

```python
AttachmentInventory().scan_one(conversation)
```

没有传入：

```text
cfg.sources.allowlist
```

结果：所有本地路径默认：

```text
status = unresolved
content_hash = None
```

即使文件实际存在。

独立复核对真实 `01a08fdc`：

默认 pipeline inventory：

```text
TOC_Focused.png -> unresolved / content_hash=None
inventory hash -> 1916d5fe...
```

显式允许 `D:\Partition` 后：

```text
TOC_Focused.png -> found / content_hash=<real sha256>
inventory hash -> d8e0f929...
```

因此当前 manifest 的非空 attachment hash 主要证明“引用集合稳定”，还不能证明 allowlist 内本地附件内容稳定。

### V3-6. 本地附件内容变化当前可能无法触发 `attachment_changed`

独立复核使用临时 allowlist 内文件验证：

默认无 allowlist 的 Pipeline 模式：

```text
文件内容 A -> unresolved
文件内容 B -> unresolved
inventory hash 不变
```

正确 allowlist 模式：

```text
文件内容 A -> found / sha256 A
文件内容 B -> found / sha256 B
inventory hash 改变
```

因此完整附件内容级增量仍未真正接入生产 ingestion。

### V3-7. `resolve_manifest_path()` 仍没有生产调用者

独立复核全仓搜索确认：

```text
resolve_manifest_path()
```

只出现在：

- `config.py`
- config unit tests

生产 CLI 实际仍固定使用：

```text
<staging>/.ai-memory/manifest.sqlite
```

而 `config.example.yaml` 仍公开：

```yaml
manifest_path: ./data/conversation-imports.sqlite
```

因此当前配置 contract 不一致。

V3 必须明确二选一：

#### 方案 A：manifest 跟随每个 archive root

```text
<staging or canonical root>/.ai-memory/manifest.sqlite
```

则：

- 删除/废弃 `conversation_archive.manifest_path`。
- 文档说明 manifest 是 archive-local ledger。

#### 方案 B：manifest_path 是正式配置

则：

- import/index/rebuild/service 全部使用 `resolve_manifest_path()`。
- 如果 staging 与 canonical 共用同一个 manifest，要明确 namespace/路径语义。

不要继续保留“配置存在但生产完全不用”的状态。

### V3-8. `index_stale` 业务生命周期仍未完全闭环

当前：

```python
manifest.decide(...)
```

可以返回：

```text
action = "index"
reason = "index_stale"
```

但 `ConversationIngestionPipeline.run()` 没有：

```python
if decision.action == "index":
```

分支。

因此如果 ingestion 阶段遇到真正 `index_stale` decision，当前会继续落入普通 write 路径。

这和状态机定义不一致。

### V3-9. 人工笔记是否进入搜索索引，当前语义自相矛盾

当前 index file hash 使用：

```python
compute_managed_hash(...)
```

因此人工区域变化不会触发 reindex。

但 `HeadingChunker` 又会继续索引 `AUTOGEN_END` 之后的人工正文。

独立复核：

```text
已 index
-> 在 manual region 新增独特关键词
-> check_changed_reason() == unchanged
-> FTS 搜索新关键词 == 0 results
```

所以当前实际行为是：

```text
人工区域“会被索引一次”
但后续人工修改不会触发索引更新
```

这是不一致的。

V3 必须明确产品语义：

#### 推荐方案 A：人工区域也属于可搜索知识

则：

- machine-managed conflict hash 继续忽略人工内容。
- index freshness 单独使用 `index_input_hash`，覆盖 machine + manual searchable content。
- manual edit -> reindex。
- manual edit 不触发 import conflict。

#### 方案 B：人工区域永远不进入 conversation 派生索引

则：

- chunker 明确跳过 `AUTOGEN_END` 后人工区域。
- 文档写清人工笔记由 Obsidian 自身或其他索引器负责。

禁止保持当前混合语义。

---

## 3. 本轮执行顺序

严格按以下顺序：

```text
F0 baseline
-> F1 embedding active identity
-> F2 embedding metadata/vector search
-> F3 recall final-context budget
-> F4 attachment allowlist lifecycle
-> F5 manifest path contract
-> F6 index_stale orchestration
-> F7 manual-search semantics
-> F8 clean 5-session validation
-> F9 final report
```

不要在 F1–F7 未通过时开始 322-session embedding 或 Vault 写入。

---

## 4. F0：冻结 V2 基线

执行：

```powershell
git status --short
python -m pytest -q
git diff --check
```

预期当前基线：

```text
171 passed, 3 warnings
```

新增测试后总数可以增加，但已有 171 项必须继续通过。

不要清理当前未提交文件。

---

## 5. F1：Embedding active identity 修复

### 5.1 明确两层概念

必须区分：

```text
embedding cache
active/live embedding identity
```

`conversation_embeddings`：

```text
可以保留所有历史 cache
```

`conversation_sections.embedding_identity`：

```text
必须始终代表当前 active model/version 对该 section 的 identity
```

### 5.2 `check_changed_reason()` 正确逻辑

embedding enabled 时，对每个 expected chunk 至少检查：

```text
expected_identity = sha256(content + model + version)
live_section.embedding_identity == expected_identity
cache has (expected_identity, model, version)
```

推荐状态：

```text
live identity mismatch + cache exists
    -> embedding_activation_changed

live identity mismatch + cache missing
    -> embedding_identity_changed

live identity match + cache exists
    -> unchanged
```

如果不想增加新 reason，也至少让：

```text
v1 -> v2 -> v1
```

第三步不能返回 unchanged。

### 5.3 切回历史版本不应重新请求网络

如果 v1 cache 已存在：

```text
v1 -> v2 -> v1
```

回切 v1 应：

- 更新 live section identity。
- 重用 v1 cache。
- 0 新 embedding HTTP 调用。

### 5.4 Embedding cache 仍保留

不要为修 rollback 删除历史 cache。

历史 cache 保留是正确设计。

### 自动化验收

新增测试：

```text
index v1 -> N calls
index v2 -> +N calls
switch back v1 -> 0 new calls
live section identities == expected v1
check_changed_reason == unchanged after activation completes
```

---

## 6. F2：修正 embedding metadata 和 vector search 绑定

### 6.1 `index_metadata_for_file()`

禁止再用：

```sql
s.id = e.id
```

来决定当前 embedding metadata。

应按 live identity join：

```sql
s.embedding_identity = e.embedding_identity
```

并且绑定当前：

```text
model
version
```

如果同一 conversation 中所有 live section 使用同一 model/version/dimension，metadata 可返回单值。

如果出现混合状态：

```text
必须 fail closed 或明确报告 mixed
```

不能静默取 LIMIT 1。

### 6.2 Vector search

查询必须只使用：

```text
live section identity
active model
active embedding version
query vector dimension
```

建议 SQL 条件明确：

```sql
e.embedding_identity = s.embedding_identity
AND e.model = ?
AND e.version = ?
AND e.dimension = ?
```

避免历史 cache 行被重复候选。

### 6.3 Manifest metadata

index 完成后写入：

```text
embedding_model
embedding_version
embedding_dimension
```

必须反映当前 live sections，而不是任一历史 cache。

### 验收

测试：

```text
v1 metadata -> v1
v2 activation -> metadata v2
switch back v1 -> metadata v1
```

历史 cache 同时包含 v1/v2 时仍正确。

---

## 7. F3：Recall 的最终 context budget

### 7.1 定义 budget 对象

`token_budget` 必须约束最终返回给 Agent 的：

```text
context
```

而不只是 `items[].content`。

预算应包含：

- heading。
- conversation id。
- source path。
- source anchors。
- separators。
- content。

### 7.2 推荐实现

建立 helper，例如：

```python
render_context_item(...)
truncate_context_to_budget(...)
```

逐 item：

1. 先计算固定 metadata 开销。
2. 计算剩余 content 预算。
3. 截断 content。
4. 如果连 metadata 本身都装不下：
   - 输出更短 metadata；或
   - 跳过 item。

### 7.3 Token 估算

若暂不引入 tokenizer，可继续近似：

```text
2 chars/token
```

但验收必须针对最终 context。

推荐允许固定小误差：

```text
final_context_chars <= token_budget * 2 + 64
```

或者更严格的自定义常数。

### 7.4 绝对路径过长

如果 source path 占预算过大，可考虑：

- 保留 vault-relative path；
- 或 metadata 中单独返回完整路径，context 中缩短显示。

但 provenance 不能丢。

### 验收

真实 5-session：

```text
budget 10
budget 100
budget 1500
```

记录：

```text
content chars
final context chars
items
```

最终 context 必须符合定义好的 hard cap。

---

## 8. F4：Attachment allowlist 和内容级增量真正接入 Pipeline

### 8.1 Pipeline 必须接收 AttachmentInventory 或 allowlist

不要在 pipeline 内硬编码：

```python
AttachmentInventory()
```

建议构造：

```python
ConversationIngestionPipeline(
    reader,
    writer,
    manifest,
    attachment_inventory=AttachmentInventory(allowlist_roots=...),
)
```

或等价依赖注入。

### 8.2 CLI 使用 `cfg.sources.allowlist`

生产 import 必须把配置 allowlist 传入。

如果 allowlist 为空：

推荐：

```text
保持 unresolved，不读取本地文件内容
```

不要再次恢复过宽默认：

```text
G:\LLM
D:\Partition
```

除非用户在 config 中明确配置。

### 8.3 Inventory hash 的预期

allowlist 内存在文件：

```text
status=found
size_bytes=真实值
content_hash=SHA-256
```

内容变化：

```text
inventory hash 改变
-> manifest decide attachment_changed
```

路径相同但文件被删除：

```text
found -> missing
inventory hash 改变
```

### 8.4 不允许超范围读取

allowlist 外：

```text
status=unresolved
不 stat/hash 文件内容
```

### 自动化验收

新增 Pipeline-level test：

```text
temp file A
-> import
-> manifest attachment hash A

modify file to B
-> import
-> decision attachment_changed
-> new manifest hash B
```

不是只测试 `AttachmentInventory.inventory_hash()` helper。

---

## 9. F5：统一 manifest path contract

这是 V3 必须做出的设计决策，不允许继续两套语义并存。

### 推荐方案：Archive-local manifest

建议最终采用：

```text
<conversation root>/.ai-memory/manifest.sqlite
```

原因：

- staging 和 canonical 可以有独立 ledger。
- 5-session validation 已经按这个模式运行。
- index lifecycle 自然能定位同 root manifest。

如果采用该方案：

1. 将 `conversation_archive.manifest_path` 标记 deprecated 或删除。
2. `config.example.yaml` 不再宣称该字段有效。
3. README/报告说明：manifest 是 archive-local rebuildable ledger。
4. 如需 migration，保持旧配置读取兼容但打印 warning。

### 如果坚持配置 manifest_path

则必须：

- CLI import 使用 `resolve_manifest_path()`。
- index/rebuild 使用同一 manifest。
- service 或相关工具保持一致。
- staging/canonical namespace 不冲突。

### 验收

全仓只能有一个明确 contract。

不得再出现：

```text
config.example 写 manifest_path
但 production 永远忽略
```

---

## 10. F6：`index_stale` 真实 orchestration

### 10.1 Import pipeline 必须显式处理 `action=index`

当前不能让：

```text
index_stale
```

落入普通 writer.write。

至少：

```python
if decision.action == "index":
    return IngestionResult(... status="index_stale" ...)
```

Import 本身不应偷偷运行 index。

推荐职责：

```text
import 告知 index_stale
index --changed-only 负责恢复
```

### 10.2 状态转换集成测试

必须真实覆盖：

```text
import
-> index
-> manifest fresh

managed/source content change
-> manifest/index 判 stale
-> import 不重复覆盖错误内容
-> index --changed-only 重新 index
-> manifest fresh
```

### 10.3 `status` 不要滥用

如果 manifest `status` 同时表达：

```text
ingestion status
index freshness
```

导致逻辑难以稳定，可以增加：

```text
index_status
```

但仅在必要时增加，不进行大重构。

### 验收

增加 CLI/Pipeline integration test，不能只测：

```python
manifest.decide(...)
```

helper 返回值。

---

## 11. F7：明确人工笔记和 conversation index 的关系

### 推荐：人工区域进入搜索

因为 taskbook 原始目标包括：

```text
人工 frontmatter / Related link 与机器内容长期共存
```

并且实际 Obsidian memory 使用场景通常希望人工注释可搜索。

建议采用：

```text
machine-managed hash -> import conflict / source freshness
index_input_hash      -> index freshness
```

### 11.1 `index_input_hash`

可定义为：

```text
sha256(all searchable Markdown content)
```

但排除：

- 非搜索意义的 transient metadata。
- 纯格式变化可自行决定是否忽略。

### 11.2 结果语义

用户只改 manual notes：

```text
import -> unchanged / no conflict
index -> changed_file or manual_changed
```

用户改 machine-managed 区域：

```text
import -> managed conflict/candidate
```

### 11.3 如果选择人工区域不进 index

则 chunker 必须明确只处理：

```text
AUTOGEN_BEGIN ... AUTOGEN_END
```

且报告与 README 明确：

```text
manual notes are not part of conversation derived index
```

### 验收

无论选哪个方案，都必须新增测试证明语义一致。

不能再出现：

```text
第一次人工内容被索引
以后修改却永久 stale
```

---

## 12. F8：重新做固定 5-session 最终验收

继续使用：

```text
exports/conversation-staging/final-hardening-validation
```

但不要让 pytest 写它。

如果 V3 修改了 manifest/index schema，建议新建：

```text
exports/conversation-staging/finalization-v3-validation
```

以免旧派生状态影响验收。

### 12.1 Import

首次：

```text
written=5
```

第二次：

```text
skipped=5
```

### 12.2 Index lexical-only

首次：

```text
indexed_files=5
```

第二次：

```text
skipped_unchanged=5
```

### 12.3 Attachment

使用真实 config allowlist 后，至少验证：

- `TOC_Focused.png` 如果仍存在，必须是 `found` 且有 content hash。
- 真实 missing clipboard 仍是 missing。
- allowlist 外 `.codex` 文件保持 unresolved。

### 12.4 Recall

真实：

```text
Fe3+
TOC_Focused.png
parent thread UUID
```

并记录最终 context budget。

### 12.5 Embedding

不要做 5-session 全量 NAS embedding。

只做：

- mock integration；
- 单次 NAS smoke；
- 如确有必要最多 1–2 个短 chunk 小规模 live smoke。

### 12.6 Embedding rollback smoke

用 mock 或本地 fake client 验证：

```text
v1 -> v2 -> v1
```

最终：

```text
live identity = v1
metadata = v1
0 extra network calls on cached rollback
```

---

## 13. F9：最终实施报告修订

更新：

```text
docs/CONVERSATION_MEMORY_IMPLEMENTATION_REPORT.md
```

### 报告必须新增

1. V3 独立复核发现的问题。
2. embedding rollback 测试。
3. live identity 与历史 cache 的区别。
4. metadata 当前 version 的验证。
5. final `context` budget，而非只报 item content。
6. attachment allowlist 与 content-hash E2E。
7. manifest path 最终 contract。
8. index_stale orchestration 集成测试。
9. manual notes 是否属于 conversation search index 的最终语义。

### 报告结论分级

必须使用以下三种状态，而不是一个“全部完成”：

```text
A. lexical-only staging ready
B. canonical Vault import ready
C. bulk embedding ready
```

只有全部相关门槛通过时才能分别写 READY。

---

## 14. 最低新增测试要求

### Embedding rollback

- v1 -> v2 -> v1。
- cached rollback 0 new calls。
- live section identity 正确。
- metadata version 正确。
- vector search 不读历史错误 version。

### Recall

- final context budget=10。
- final context budget=100。
- long absolute path。
- long heading。
- anchors metadata。

### Attachment Pipeline

- allowlist 内 found + SHA。
- 内容变化 -> attachment_changed。
- found -> missing。
- allowlist 外不读取。

### Manifest contract

- 最终 path contract 对应 CLI 实际行为。
- config example 与代码一致。

### Index stale

- import/index/managed change/index stale/reindex/fresh 全链路。

### Manual notes

- 人工编辑不会触发 import conflict。
- 根据最终产品语义：要么触发 reindex，要么明确永不进入 index。

---

## 15. 最终生产前验收门槛

### A. Lexical-only staging READY

要求：

- 全部 pytest 通过。
- E2E 隔离。
- chunk hard cap。
- final recall context budget 生效。
- manifest/index 生命周期稳定。
- manual note indexing 语义一致。

### B. Canonical Vault import READY

在 A 基础上要求：

- vault_root 用户确认。
- canonical_subdir 正确。
- staging 不能绕过 Vault 门控。
- manifest path contract 明确。
- writer manual/machine coexistence 通过。
- attachment allowlist 按正式配置工作。

### C. Bulk BGE-M3 embedding READY

在 A+B 之外要求：

- active identity 与 cache 分离正确。
- v1 -> v2 -> v1 rollback 通过。
- metadata model/version/dimension 正确。
- vector search 只读 live active version。
- identical rerun 0 unnecessary embedding calls。
- content change 仅重算受影响 chunk。
- dimension mismatch fail closed。
- NAS smoke PASS。

只有 C 通过，才允许建议开启 322-session 批量 embedding。

---

## 16. 建议执行命令

基线：

```powershell
git status --short
python -m pytest -q
git diff --check
```

重点：

```powershell
python -m pytest tests/test_conversation_index_p6.py -q
python -m pytest tests/test_conversation_retrieval_p7.py -q
python -m pytest tests/test_conversation_attachments.py -q
python -m pytest tests/test_conversation_manifest_p5.py -q
python -m pytest tests/test_conversation_cli_p8.py -q
python -m pytest tests/test_conversation_real_e2e.py -q
```

最终：

```powershell
python -m pytest -q
git diff --check
```

单次 NAS smoke：

```powershell
python scripts/smoke_bge_m3.py
```

如果 smoke 返回 SKIP/FAIL，不得在报告写 PASS。

---

## 17. 给 Flash Agent 的直接执行指令

```text
继续在 G:\LLM\memory 当前未提交工作树上执行：

docs/CONVERSATION_MEMORY_FINALIZATION_TASKBOOK_V3.md

这是最后一轮定向收尾，不允许重构 conversation 架构，不允许扩大任务边界。

V2 的 171 tests、tmp_path E2E、1498-char hard cap、真实 5-session import/index、parent-thread、guardian、manifest-index 基础回写均已独立确认成立，不要重做。

本轮只修：

1. embedding v1 -> v2 -> v1 rollback：不能因为历史 v1 cache 存在就误判 unchanged；live conversation_sections.embedding_identity 必须切回当前期望 identity，且复用 cache 时 0 新网络调用。
2. index_metadata_for_file() 不能再按 s.id=e.id 读取历史 embedding；必须按 live identity + active model/version/dimension 得到当前 metadata。
3. vector search 只允许 active live identity/model/version/dimension，不得混入历史 cache。
4. recall token_budget 必须约束最终返回 Agent 的 context，不只是 items[].content；当前独立复核 budget=10 时 item content 20 chars 但 final context 439 chars。
5. Pipeline AttachmentInventory 必须注入 cfg.sources.allowlist。allowlist 内真实文件要 found + content SHA；内容变化必须触发 attachment_changed；allowlist 外禁止读取。
6. resolve_manifest_path / archive-local manifest 必须做出一个最终 contract，不能继续 config.example 有 manifest_path 但生产完全不用。
7. ConversationIngestionPipeline 必须显式处理 manifest action=index / index_stale，不得把它落入普通 write。
8. 明确 manual notes 是否进入 conversation index。推荐进入：import conflict hash 继续只看 machine region，但 index_input_hash 覆盖 searchable manual region，使人工修改触发 reindex。若选择不进入，则 chunker 必须永远排除 manual region并在文档明确。

完成后重新做固定 5-session validation，并更新 docs/CONVERSATION_MEMORY_IMPLEMENTATION_REPORT.md。

报告最终必须分别给出：
A lexical-only staging READY/NOT READY
B canonical Vault import READY/NOT READY
C bulk BGE-M3 embedding READY/NOT READY

禁止执行 322 场正式 Vault 全量导入、禁止批量 NAS embedding、禁止修改/重启 Unraid、禁止 commit/push、禁止修改原始 ZIP。

完成后附：完整 pytest、git diff --check、v1-v2-v1 rollback 证据、live identity/metadata、final context budget、attachment content-change E2E、manifest contract、index_stale integration、manual note indexing behavior、5-session real validation、NAS smoke 状态。
```

