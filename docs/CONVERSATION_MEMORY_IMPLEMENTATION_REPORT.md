# Conversation Memory Pipeline V3 最终收尾与生产前验收报告

报告日期：2026-09-12  
项目：`G:\LLM\memory`  
分支：`v2/p0-hardening`  
基线 HEAD：`ba50e6296fb871468d868eda405c6855bfee3353`  
执行依据：`docs/CONVERSATION_MEMORY_FINALIZATION_TASKBOOK_V3.md`

## 1. 最终结论

V3 已按任务书完成最后一轮定向收尾。未重构 conversation 子系统，也未扩大到 322-session 正式 Vault 导入或批量 NAS embedding。

最终全库回归：

```text
175 passed, 3 warnings in 27.81s
```

`git diff --check` 退出码为 0，仅有 Git 的 LF/CRLF 提示，无 whitespace error。

固定 5-session V3 验收重新从新的派生目录执行：

```text
G:\LLM\memory\exports\conversation-staging\finalization-v3-validation
```

结果：首次 import `5 written`，幂等重跑 `5 skipped`；首次 index `5 indexed / 173 chunks`，再次 index `5 skipped_unchanged`。

生产前分级结论：

| 门槛 | 状态 | 结论 |
|---|---|---|
| A. lexical-only staging | **READY** | V3 自动化、真实 5-session、E2E 隔离、chunk hard cap、final-context budget、manual-note/index lifecycle 全部通过。 |
| B. canonical Vault import | **NOT READY（仅剩运行配置门控）** | 代码门控与 archive-local manifest 已通过，但当前正式 `vault_root` 尚未由用户确认，正式 `sources.allowlist` 也尚未写入当前运行配置。V3 明确禁止代替用户执行该确认。 |
| C. bulk BGE-M3 embedding | **NOT READY（继承 B 门控）** | embedding 技术门槛本轮已通过，包括 v1→v2→v1 rollback、live identity、metadata、vector binding、cache reuse、dimension guard、NAS 1024-d smoke；但任务书要求 C 必须建立在 A+B 上，因此在 B 未确认前不建议开启 322-session 批量 embedding。 |

## 2. V3 独立复核问题与修复结果

| V3 项 | 原问题 | 本轮结果 |
|---|---|---|
| V3-1 | 历史 v1 cache 存在时，v2→v1 rollback 被误判 `unchanged`。 | `check_changed_reason()` 同时检查 live section identity 与 cache；cache 已存在但 live identity 不符时返回 `embedding_activation_changed`。回切后 0 新 embedding 调用。 |
| V3-2 | `index_metadata_for_file()` 按 chunk id join，可能读到历史版本。 | 改为 live `embedding_identity` + 当前 model/version；完整性不足或混合 metadata 时 fail closed。 |
| V3-3 | vector search 对 active model/version/dimension 约束不足。 | SQL 显式绑定 live identity、active model、active version、query dimension。 |
| V3-4 | recall 只限制 `items[].content`，最终 Agent context 可超预算。 | budget 改为直接约束最终渲染 context；heading/source/anchors/separator 都进入预算。 |
| V3-5/6 | Pipeline 未使用 `cfg.sources.allowlist`，附件内容变化不能稳定触发增量。 | `AttachmentInventory` 依赖注入到 Pipeline；CLI 从 `cfg.sources.allowlist[].path` 构造；内容 A→B、found→missing 均触发 `attachment_changed`。 |
| V3-7 | `manifest_path` 配置存在但生产不使用。 | 最终采用 **archive-local manifest** contract：`<conversation_root>/.ai-memory/manifest.sqlite`。旧字段仅兼容读取，不用于生产 import/index。 |
| V3-8 | `manifest.decide(action=index)` 会落入普通 write。 | Pipeline 显式返回 `status=index_stale`，不写 Note；`index --changed-only` 负责恢复。 |
| V3-9 | manual 区域第一次会被索引，但后续修改不触发 reindex。 | 最终选择“**manual notes 属于 conversation searchable knowledge**”。import conflict hash 仍仅看 managed 区；index freshness 使用完整 searchable Markdown hash。 |

## 3. Embedding active identity、历史 cache 与 rollback

### 3.1 两层语义已分离

`conversation_embeddings` 是历史 cache，可保留多个 model/version 的 identity：

```text
UNIQUE(embedding_identity, model, version)
```

`conversation_sections.embedding_identity` 是当前 live/active identity，只能代表当前索引激活的 model/version。

`check_changed_reason()` 在 embedding enabled 时，对每个期望 chunk 同时检查 expected live identity 与 expected cache identity/model/version。

```text
live mismatch + expected cache exists  -> embedding_activation_changed
live mismatch/cache missing            -> embedding_identity_changed
live match + expected cache exists     -> unchanged
```

### 3.2 v1 → v2 → v1 自动化证据

`tests/test_conversation_index_p6.py` 覆盖：

```text
index v1 -> N embedding calls
same v1 -> 0 extra calls
activate v2 -> +N calls
v2 metadata -> v2
switch back to v1 -> embedding_activation_changed
activate cached v1 -> 0 new calls
live section identities == expected v1
metadata -> v1
post-activation check_changed_reason -> unchanged
```

随后只修改一个 chunk，仍只新增 1 次 embedding 调用；历史 cache 不删除。

### 3.3 Metadata fail-closed

`index_metadata_for_file()` 不再使用 `s.id = e.id` 选择 embedding，而是绑定：

```sql
e.embedding_identity = s.embedding_identity
AND e.model = active_model
AND e.version = active_version
```

并检查 live section 覆盖数。若出现缺失或混合 active metadata，不再 `LIMIT 1` 静默选值，而是 fail closed。

### 3.4 Vector search active binding

向量候选 SQL 要求：

```text
s.embedding_identity = e.embedding_identity
e.model = active model
e.version = active version
e.dimension = len(query_vector)
```

因此保留的历史 v1/v2 cache 不会因 chunk id 或其他版本重复进入 live 候选。

## 4. Recall 最终 Agent context budget

V3 将预算对象从 `items[].content` 改为最终返回 Agent 的 `context`。预算包括 conversation id、heading、source path、source anchors、separators 和 content。

仍使用保守近似：

```text
2 chars/token
```

当完整 provenance metadata 放不下时，context 使用压缩 metadata；完整路径和 anchors 仍保留在结构化 `items` 中，不丢 provenance。

自动化覆盖 budget 10/100、超长绝对路径、超长 heading、anchors metadata。

真实 5-session `Fe3+`：

| token_budget | items | item content chars | final context chars | 验收 cap |
|---:|---:|---:|---:|---:|
| 10 | 1 | 9 | 20 | 84 (`budget*2+64`) |
| 100 | 1 | 46 | 200 | 264 |
| 1500 | 2 | 2333 | 3000 | 3064 |

本次真实数据实际均满足更严格的 `context_chars <= token_budget*2`。

## 5. Attachment allowlist 与内容级增量

### 5.1 Pipeline 接线

`ConversationIngestionPipeline` 现在接收可注入的 `AttachmentInventory`。生产 CLI 使用 `cfg.sources.allowlist[].path`。allowlist 为空时保持 `unresolved`，不会恢复宽泛默认磁盘读取。

### 5.2 Pipeline-level 自动化

新增测试完整跑 Pipeline：

```text
allowlist 内 temp file 内容 A
-> import / manifest hash A

同一路径改为内容 B
-> decision=attachment_changed
-> manifest hash B != A

删除文件
-> found -> missing
-> 再次 attachment_changed
```

allowlist 外本地路径仍为 `unresolved`，没有 resolved path/content hash。

### 5.3 真实 5-session allowlist 验收

V3 人工验收显式允许 `D:\Partition`，共识别 24 条 attachment inventory record。

`TOC_Focused.png`：

```text
status       = found
size_bytes   = 181452
content_hash = 59a0e220066407594b5e61a7091b012cbe6c3ad0dff9f4ae554e8bc6dec52c60
```

真实 D: 临时 clipboard 文件若已不存在，状态为 `missing`；例如：

```text
D:/Partition/TEMP/codex-clipboard-86a5c9a3-2c39-419a-9d8c-000cb767fee2.png
status = missing
```

allowlist 外 `C:\Users\YING\.codex\...` 路径保持：

```text
status        = unresolved
content_hash  = null
resolved_path = null
```

真实 manifest inventory hash：

| Conversation | V3 inventory hash |
|---|---|
| `01a08fdc-6da5-7f93-9119-ff79d9fea710` | `d8e0f929653d9678573c3fafc57ca8d1241ba9ec6c84cc5a8bac3f20a8abcfa7` |
| `01a01289-e1b4-7242-98d9-368a75a16194` | `c4724a624179d23665ea76e8ec0a06f76c01293540895b61e8c0555a88c8749d` |
| `019ea2ad-9a74-75c2-bf10-4246ca00ab25` | `7d412655b794c3af262e0da33f2a6885d8c06badee8890a24868244864e7d3fa` |

其余两场无 inventory record，因此空 hash 合理。

## 6. Manifest path 最终 contract

最终采用任务书推荐的方案 A：**manifest 跟随 conversation archive root**。

生产路径唯一语义：

```text
<conversation_root>/.ai-memory/manifest.sqlite
```

具体变更：

1. `ConversationArchiveConfig.resolve_archive_manifest_path(root)` 是生产 locator。
2. import、index metadata 回写、index-stale 检查都使用 archive-local ledger。
3. `config.example.yaml` 已删除 `manifest_path:`，并明确 archive-local path。
4. README 明确 legacy `conversation_archive.manifest_path` 仅保留配置向后兼容，不用于生产 import/index。
5. 自动化显式设置 legacy global manifest 路径，验证 import 后只创建 archive-local manifest，legacy 文件不产生。

全仓生产调用检查：`resolve_manifest_path()` 只剩 config 兼容定义；实际 conversation CLI 使用 `resolve_archive_manifest_path()`。

## 7. `index_stale` orchestration 与 manual notes 搜索语义

### 7.1 Import 不再误写

Pipeline 现在显式处理：

```text
decision.action == index
```

返回 `status=index_stale / reason=index_stale`。Import 不运行 index，也不调用 `writer.write()`。

### 7.2 Manual notes 最终语义

选择 V3 推荐方案：manual notes 属于 conversation 派生搜索知识。

两类 hash 分工：

```text
machine-managed hash
  -> ingestion conflict/source coexistence

index input hash = SHA-256(完整 searchable Markdown)
  -> index freshness
```

因此只改 manual notes 不产生 managed conflict；若此前已有 index，则 import 报 `index_stale` 且不覆盖 Note，随后 `index --changed-only` 重建该文件，新人工关键词可被 FTS 搜索。

### 7.3 集成测试链路

新增 CLI integration test 完整覆盖：

```text
import
-> index
-> manifest fresh
-> append unique manual keyword
-> import reports index_stale / written=0
-> manual text remains intact
-> index --changed-only reason=changed_file
-> search finds manual keyword
-> import again reason=unchanged
```

这消除了 V2 “第一次人工内容可能被索引、后续修改却不触发更新” 的混合语义。

## 8. 固定 5-session V3 最终验收

只读原始 ZIP：

```text
D:\Partition\F\Study\博士文件\Research-AI-Hub\_codex_workspace\tmp\chat-export\codex-sessions-20260912-012538.zip
```

新的 V3 派生目录：

```text
G:\LLM\memory\exports\conversation-staging\finalization-v3-validation
```

固定 IDs：

```text
019e3ad1-05d6-7382-972f-0d377e6092c6
019eab7a-3a54-70b1-afd2-b89c0c98e8b2
01a08fdc-6da5-7f93-9119-ff79d9fea710
01a01289-e1b4-7242-98d9-368a75a16194
019ea2ad-9a74-75c2-bf10-4246ca00ab25
```

### 8.1 Import

首次：

```text
written=5
skipped=0
index_stale=0
conflict=0
failed_retryable=0
```

幂等重跑：

```text
written=0
skipped=5
index_stale=0
conflict=0
failed_retryable=0
```

### 8.2 Lexical index

首次：

```text
files_scanned=5
indexed_files=5
skipped_unchanged=0
chunks_indexed=173
change_reasons={"new_document": 5}
```

再次：

```text
files_scanned=5
indexed_files=0
skipped_unchanged=5
chunks_indexed=0
change_reasons={"unchanged": 5}
```

5 条 manifest 均为 `status=written`，且 `last_indexed_at/index_source_hash` 非空。本次真实验收禁用 embedding，因此 embedding model/version/dimension 为空是预期结果。

### 8.3 Chunk health

```text
total_sections = 173
max_chars      = 1498
p95_chars      = 1431
over_1500      = 0
```

### 8.4 Parent thread 与 `TOC_Focused.png`

父线程查询 `01a08c1c-b49b-70a2-b1a4-cad68e4b1cd4` 命中 9 个 section，全部属于 child：

```text
conversation_id  = 01a08fdc-6da5-7f93-9119-ff79d9fea710
parent_thread_id = 01a08c1c-b49b-70a2-b1a4-cad68e4b1cd4
```

`TOC_Focused.png` 结果继续分开记录：

**Top result**

```text
conversation_id = 01a08fdc-6da5-7f93-9119-ff79d9fea710
section          = 工具活动
score            ≈ 0.25056
source_anchors   = []
```

**First anchored result**

```text
conversation_id = 01a08fdc-6da5-7f93-9119-ff79d9fea710
section          = 关键原始内容 > 用户
score            ≈ 0.22317
ordinal          = 6
message_id       = msg_01a08fdc-7a4c-7810-a287-b9a6b806baa6
turn_id          = 01a08fdc-6eb0-7f03-9681-fedc4394fdd7
```

## 9. 自动化与 E2E 最终结果

重点 V3 测试集合：

```text
47 passed
```

覆盖 index/embedding rollback、recall final-context budget、attachment Pipeline、manifest/config contract、CLI index-stale/manual-note lifecycle。

Real E2E 使用 `tmp_path`，最终再次连续执行两次：

```text
1 passed in 2.93s
1 passed in 3.23s
```

最终全库：

```text
175 passed, 3 warnings in 27.81s
```

3 条 warning 仍全部来自第三方 Starlette/httpx 与 websockets/uvicorn deprecation，不是 conversation pipeline 失败。

## 10. NAS BGE-M3 smoke

V3 只执行任务书允许的单次 live smoke，不执行批量 embedding：

```text
python scripts/smoke_bge_m3.py
```

实际：

```text
Testing NAS embedding endpoint: http://192.168.22.102:28001/v1/embeddings (model=bge-m3)
[PASS] Successfully received embedding vector! Dimension=1024 (Expected=1024)
```

exit code = 0。

## 11. 仍保持的生产安全门控

本轮没有执行且不得误写为已执行：

1. 322-session 正式 canonical Vault 全量导入。
2. 批量 NAS BGE-M3 embedding。
3. Unraid 修改或重启。
4. 原始 export ZIP 修改。
5. Git commit / push。
6. dirty working tree 清理。

下一生产步骤不是继续改架构，而是由用户确认正式运行配置：

```text
vault_root
canonical_subdir
sources.allowlist
```

在 B 门控被显式确认后，当前 embedding 技术验证结果已足以进入小批量受控 embedding，再决定是否放开 322-session bulk run。
