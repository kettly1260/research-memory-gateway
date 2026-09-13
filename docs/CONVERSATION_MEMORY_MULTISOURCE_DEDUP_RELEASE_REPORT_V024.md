# Conversation Memory Multi-Source Identity & Dedup — v0.2.4 Implementation / v0.2.5 Production Release Report

任务书：`CONVERSATION_MEMORY_MULTISOURCE_DEDUP_TASKBOOK_V024.md`
执行日期：2026-09-13（含独立审计、remediation、正式 rehearsal、v0.2.5 hotfix 与 W13 production cutover）
执行范围：W0 → W13。**最终生产版本为 v0.2.5，NAS production 已完成切换并通过上线验收。**

> **版本状态说明**：`76e0466`（首次 W0-W12 commit）经独立审计判定**尚不能进入 W13**。
> 其 5 项 release blocker 已在本 remediation commit 中修复，本报告取代此前声称的
> "76e0466 READY" 结论。
>
> v0.2.4 发布候选在正式 NAS canary 中又发现一个 legacy compatibility 缺口：旧 322 份 Markdown
> 未被重写，因此 `conversation_read()` 仅从 frontmatter 读取新 identity 字段时会返回空
> `source_key/canonical_conversation_id`。该问题以 v0.2.5 hotfix 修复：read 路径在 frontmatter
> 缺字段时按 `vault_path` 从 conversation index 回填 identity；最终 production 使用 v0.2.5。

## 0. Remediation（审计 blocker 修复记录）

| # | Blocker | 修复 |
|---|---|---|
| 1 | Legacy migration fingerprint hydration：migrated record 为空 fingerprints 却伪装 v1；空 stored sequence 被误判为 strict continuation | migration 现在写 `fingerprint_version = 0`（显式 unknown，见 `FINGERPRINT_VERSION_UNHYDRATED`）。same-entry 首次重放触发 `identity_hydrated`：只更新 identity metadata（transcript/ordered/set hash、message_count、message fingerprint sequence、fingerprint_version=1 及对应 snapshot 字段），不重写 Markdown、不改变 output path。different-entry 且 unhydrated 一律 `conflict/fingerprints_unhydrated` fail-closed（空 sequence 不再可能被当成前缀）。`record_snapshot()` 对 unhydrated snapshot row 现在补齐 fingerprint 字段。`_source_record_from_row` 的 `or 1` 默认值 bug（把 0 当 falsy）已修复。write 路径（attachment_changed 等）同样会顺带 hydrate。 |
| 2 | stale snapshot ledger 把旧 entry SHA 配上最新 transcript fingerprints | pipeline 将本次实际 parse 的 `fingerprints` 传入全部 skip 路径；stale entry 的 snapshot row 现在记录 incoming stale export 自身的 normalized_transcript_sha256 / ordered_message_hash / message_set_hash / message_count；source record 的最新 fingerprints 不回退（`mark_seen` 不改 content hash）。回归测试断言 stale row fingerprint == incoming stale fp 且 != 当前 record fp。 |
| 3 | dedup confirm 幂等 / self-merge：同一 candidate 二次 `--confirm-same` 产生 self-alias + self-merged_into，`resolve_canonical()` 返回 None | 新增 `ConversationIdentityStore.confirm_candidate_link()`：candidate decision 与 canonical link 在**单个 SQLite 事务**内完成，事务内含 post-condition 断言（无 self-alias、winner 仍 active、无 merged_into 指向自身）。`link_sources_to_canonical()` 自身有防御：两 source 已在同一 active canonical 时 no-op；`_link_in_conn` 拒绝 `loser == winner`。已 `confirmed_same` 的 candidate 再 confirm 是安全 no-op（不重复 decision 行）；已 `rejected` 的 candidate confirm 被拒绝（无显式 reopen 流程）；`resolve_candidate()` 同样幂等且拒绝状态翻转。index metadata refresh 作为事务后的可重试步骤。 |
| 4 | 恢复任务书 §14.3 / §18.1 原始 322 repeat-import gate | rehearsal 流程改为：首次 replay 允许吸收 5 个环境漂移 `attachment_changed`（该差异已用 v0.2.3 baseline `d6e44f0` 对照证实为外部 vault 文件内容漂移，非代码回归）→ 对这 5 个 note 执行常规 post-import re-index（reconcile）→ 对**完全相同 ZIP + 相同环境**立即二次导入，`repeat_import_after_reconcile` 严格得到 **322 skipped / 0 written / 0 conflict / 0 failed**。未修改 raw ZIP，未恢复/篡改任何用户 Vault 文件。 |
| 5 | 重新执行全部 gate | 见 §5。 |

## 1. 版本与 Git

```text
CORE TARGET:        v0.2.4
FINAL RELEASE:      v0.2.5
baseline SHA:       de1e646（保留）
first commit:       76e0466（W0-W12 初版，未 amend/reset）
remediation:        da85220
formal rehearsal:   ce64455
CI stabilization:   23adf3a
v0.2.5 hotfix:      a97f80b
release tag:        v0.2.5 -> a97f80b
working tree:       既有未跟踪文件 (.local/, 0, 0) 全程保留，未 stage
```

## 2. 新增架构层

| 模块 | 职责 |
|---|---|
| `conversations/identity.py` | `ConversationSourceIdentity`、deterministic `source_key`（`srcv1_<sha256>`，版本化 domain separator，禁用 runtime `hash()`）、UUIDv5 `canonical_conversation_id`、版本化 fingerprints（v1）+ `FINGERPRINT_VERSION_UNHYDRATED=0` 显式 unknown 标记、序列关系判定。 |
| `conversations/readers.py` | 平台无关 `ConversationExportReader` Protocol；`CodexExportReader` 实现该协议；pipeline 不再硬编码 Codex。 |
| `conversations/identity_store.py` | additive v2 表（canonical / source records / snapshots / message fingerprints / duplicate candidates / canonical aliases / dedup decisions）。含幂等 legacy migration（fp_version=0）、`hydrate_source_record()`、hydration-aware `record_snapshot()`、事务化 `confirm_candidate_link()`、self-merge-proof `_link_in_conn()`。legacy 表与数据永不 drop/改写。 |
| `conversations/decisions.py` | import 决策状态机：unchanged / same_source_in_new_archive / identity_hydrated / source_packaging_changed / source_continued / stale_snapshot / source_diverged / fingerprints_unhydrated / fingerprint_version_mismatch / output_missing / managed_output_modified / attachment_changed / parser_upgrade。stale 不可被 `--no-resume` 覆盖。 |
| `conversations/duplicates.py` | candidate detector：5 种 candidate type、deterministic evidence、score 仅排序；rejected pair 永不复活。 |
| `conversations/pipeline.py` | 多来源编排；legacy ledger 同步（跨系统同裸 ID 防抢占）；HYDRATE 路径；skip/write 路径全部使用真实 incoming fingerprints。 |
| `conversations/cli.py` | `dedup-audit` / `dedup-list` / `dedup-show` / `dedup-resolve`（强制显式决策；confirm 走事务） / `identity-show` / `migrate-identity`。 |
| `conversations/index.py` | additive identity 列；source-scoped 文档 id；vault_path 定位删除；filters + `resolve_conversation_ambiguity`。 |
| `conversations/retrieval.py` | search/read/recall 返回 source_key + canonical id；recall 默认 canonical collapse（仅 confirmed 跨源组）+ opt-out。 |
| `conversations/vault_writer.py` | canonical/source frontmatter；branch 文件名段；跨 source note 覆盖防御 + source_key fallback 路径。 |
| `webui/app.py` / `agent_surface/tools.py` | identity 统计可见性；MCP additive 参数。 |

## 3. 决策状态机（摘要）

```text
source record 不存在：
    provider ID 缺失 -> deterministic synthetic id（transcript 派生）；
    同源同 transcript -> 复用既有 source/canonical；否则 new source record。

source record 存在：
    entry hash 相同：
        managed note 被改 -> conflict
        attachment 变化   -> write（顺带 hydrate）
        fp_version == 0   -> identity_hydrated（metadata-only，不重写 Markdown）
        index 过期        -> index_stale
        否则              -> unchanged / same_source_in_new_archive
    entry hash 不同：
        fp_version == 0               -> conflict / fingerprints_unhydrated（fail closed）
        fp_version != 1               -> conflict / fingerprint_version_mismatch
        transcript 相同               -> source_packaging_changed（不重写）
        stored 是 incoming 的严格前缀   -> source_continued（原 path 原地更新）
        incoming 是 stored 的严格前缀   -> stale_snapshot（绝不截断）
        其余                          -> source_diverged（candidate copy，不覆盖）
```

## 4. 测试（W10 + remediation）

```text
pytest: 250 passed（基线 187 + W10 53 + remediation 7 + formal rehearsal 2 + legacy-read hotfix 1），0 failed
git diff --check: clean
frontend: 未修改
```

新增测试文件与任务书对应：

| 测试文件 | 覆盖 |
|---|---|
| `test_conversation_identity_v24.py` | 13.1 #1-5 + fingerprint 规范 |
| `test_conversation_identity_migration_v24.py` | 13.2 #6-10 |
| `test_conversation_ingestion_v24.py` | 13.3 #11-14 + 13.4 #15-20 |
| `test_conversation_multisource_v24.py` | 13.5 #21-25 |
| `test_conversation_dedup_review_v24.py` | 13.6 #26-30 |
| `test_conversation_retrieval_v24.py` | 13.7 #31-36 |
| `test_conversation_index_v24.py` | 13.8 #37-40 |
| `test_conversation_remediation_v24.py` | 审计 blocker 回归：A) migrate→same export：Markdown hash 不变、record+snapshot hydrate、sequence 非空、二次 replay 为 unchanged；B) hydrate 后严格 continuation → source_continued + 原 path 更新；C) hydrate 后中间消息改写 → source_diverged + conflict + 原 note 不覆盖；D) hydrate 后较短 snapshot → stale_snapshot + 不截断 + stale snapshot ledger 断言（stale entry → stale fp ≠ 当前 fp）+ unhydrated different-entry fail-closed；confirm twice 幂等 no-op、resolve_canonical(loser) 正常、无 self-alias/self-merged_into、notes 数量不变、decision 不重复；rejected→confirm 拒绝；link API 直调自防御 |
| `test_conversation_rehearsal_script.py` | 正式 migration rehearsal 工具可连续运行两次且每次使用独立 workdir/index；成功后 Windows 下 SQLite 句柄可清理；`--keep-workdir` 可保留证据；源 Markdown 与 raw ZIP 均保持不变。 |

## 5. 真实 322-session 副本 rehearsal（W11，remediation 后重跑）

环境：`scratch/v024-rehearsal/copy`（SQLite backup API 复制 manifest + 322 Markdown；raw ZIP 只读）。

```text
[B] migration: PASS
    legacy imports 322 / source records 322 / canonical 322
    pending duplicates 0；source key collisions 0；canonical collisions 0
    output path changes 0；markdown files changed 0
    （migrated records 现在为 fingerprint_version=0 未 hydrated 状态）

[C] repeat import（第一次，当前环境）: PASS
    317 skipped / identity_hydrated
      5 written / attachment_changed（环境漂移吸收，v0.2.3 baseline 对照一致）
    conflict 0 / failed 0 / index_stale 0；非 drift note 改动 0

[C2] reconcile index: 5 个 drifted note 按常规 post-import 流程重新索引

[C3] repeat_import_after_reconcile（严格 §14.3 / §18.1 gate）: PASS
    322 skipped / 0 written / 0 conflict / 0 failed
    notes touched: 0

[D] synthetic continuation overlay: PASS
    source_continued；原 path 原地更新；note 总数 322（无 orphan）

[E] stale replay: PASS
    stale_snapshot；continuation 内容完好（note 未被截断）

overall: PASS
```

环境漂移说明：5 个会话引用的 vault 文件在 9/12 基线后内容变化（例：`open_loops.md` 23,240 → 35,909 字节）。v0.2.3 baseline（`d6e44f0`）在同等环境给出完全相同的 5 个 `write/attachment_changed`，证明非 v0.2.4 回归；该差异只出现在首次 replay，reconcile 后的严格 gate 为 322 skipped。

### 5.1 正式可重复 rehearsal 工具

一次性的 `scratch/v024-rehearsal/run_rehearsal.py` 已整理为正式入口：

```text
scripts/rehearse_conversation_migration.py
src/research_memory_gateway/conversations/rehearsal.py
```

正式工具不再硬编码工作副本或共享 `copy-index.sqlite`：每次运行都会创建新的隔离临时目录，使用 SQLite backup API 复制 archive-local manifest，只复制 Markdown，不修改 source root；reconcile index、synthetic continuation ZIP 都位于该次独立 workdir。成功后默认清理 workdir；失败时自动保留证据，`--keep-workdir` 可显式保留成功运行的工作目录。

示例：

```powershell
python scripts/rehearse_conversation_migration.py `
  --source-root exports/conversation-staging/full-322-lexical `
  --archive "D:\Partition\F\Study\博士文件\Research-AI-Hub\_codex_workspace\tmp\chat-export\codex-sessions-20260912-012538.zip" `
  --config config.conversation-production.yaml `
  --expected-count 322 `
  --report .local/conversation-migration-rehearsal-report.json
```

正式脚本已再次对真实 322-session 数据执行，结果 `overall=PASS`；严格 gate 仍为 `322 skipped / 0 written / 0 conflict / 0 failed`，synthetic continuation/stale replay 均 PASS，raw ZIP 前后 SHA-256 均为 `e1a853e494856163a0cc7493de4ec0c73480487a7a1efb9c2c902e83cb97018e`。成功运行的临时 workdir 已确认实际删除，而不是静默忽略 Windows SQLite 句柄问题。

## 6. 安全与隐私

```text
raw account identifiers 泄露: 否
secrets / DB / ZIP / NAS config staged: 否
raw export ZIP 修改: 否（sha256 e1a853e494856163… 与审计记录一致）
production Markdown notes 修改: 否（aggregate SHA-256 前后不变）
production manifest 修改: 是（经授权 W13：legacy 322 -> v2 identity 322/322，原子切换）
production index 修改: 是（仅 additive identity columns / metadata backfill；sections / embeddings 数量不变）
rollback container / backups: 保留（v0.2.3 container + pre-v024/pre-v025 manifest + pre-v024 index）
duplicate source 物理删除: 否
```

## 7. 兼容性

- MCP 工具名不变；additive 参数/字段。
- legacy `conversation_imports` 继续维护；裸 `conversation_id` 在 Codex-only 场景不变；多来源命中同裸 ID 显式 ambiguous。
- 既有 Source Identity 字段全部保留；canonical/source key 为补充层。
- embedding identity 语义未变；无全量 re-embedding；无 vector backfill / ANN。

## 8. Release blocker 核对（remediation 后）

```text
pytest all green                                  PASS (250)
git diff --check                                  PASS
frontend lint/build                               N/A
legacy migration rehearsal                        PASS（含 fingerprint hydration）
322 repeat import（严格二次 gate）                 PASS（322 skipped / 0 written / 0 conflict / 0 failed）
continuation path stable                          PASS
stale snapshot cannot truncate                    PASS
stale snapshot ledger correctness                 PASS（新增断言）
cross-source exact transcript does not auto merge PASS
same raw ID across systems does not collide       PASS
dedup resolve preserves source notes              PASS
dedup confirm idempotent / no self-merge          PASS（新增）
recall canonical collapse tests                   PASS
no secrets / DB / ZIP / NAS config staged         PASS
production untouched before authorized W13       PASS（程序化验证）
```

## 9. 结论

**FINAL RELEASE STATUS: RELEASED / NAS PRODUCTION PASS（v0.2.5）**。

v0.2.4 的多来源 identity/dedup 核心、审计 remediation 与正式 migration rehearsal 均完成；
随后在 NAS 正式 canary 中发现并修复 legacy `conversation_read()` identity 回填缺口，形成 v0.2.5。
最终 production cutover 详见 §10。

## 10. W13 最终发布与 NAS production cutover

### 10.1 Git / GitHub / GHCR

```text
v0.2.4 core/rehearsal release chain:
  de1e646  docs: record full conversation import stability
  76e0466  v0.2.4 multi-source core
  da85220  audit blocker remediation
  ce64455  formal migration rehearsal
  23adf3a  stabilize Linux CI test package imports

v0.2.5 hotfix:
  a97f80b  fix: backfill identity for legacy conversation reads

GitHub main Tests:              PASS
GitHub main Docker/GHCR:        PASS
remote tag v0.2.5:              PASS
tag workflow:                   PASS
GHCR image:                     ghcr.io/kettly1260/research-memory-gateway:v0.2.5
OCI revision:                   a97f80b655ee5024abe2d609fb39fa0c54896313
GHCR digest:                    sha256:2da88e5b4442c9a418d89a82aa8a4112364342653eff9eb7e2e879d091116638
```

`tests.yml` 只监听 `main` branch，不监听 tag；因此最终 release gate 为：

```text
main/a97f80b Tests          PASS
main/a97f80b Docker build   PASS
tag v0.2.5 Docker workflow  PASS
```

### 10.2 NAS migration / candidate preparation

NAS 沿用 Windows-off architecture A：所有 runtime data 都是 NAS 本地副本，不依赖 Windows share。

首次尝试直接通过 `/mnt/user` 对 archive-local SQLite 做 production migration 时再次出现 Unraid
`shfs/FUSE` D-state / request wait。该尝试在完整切换前停止；检测到 partial v2 rows 后，使用
pre-v024 SQLite backup 恢复到纯 legacy 322 状态，旧 v0.2.3 production 随即恢复在线。

最终采用 rollback-safe candidate 流程：

```text
pre-v024 manifest backup
  -> tmpfs migration (322 legacy -> 322 source -> 322 canonical)
  -> same full export hydration: 322 identity_hydrated / 0 written
  -> second full export:          322 unchanged / 0 written / 0 conflict / 0 failed
  -> candidate manifest quick_check=ok
  -> candidate index additive identity backfill
  -> canary validation
  -> short atomic production cutover
```

9 个 source record 的 `message_count=0` 经确认是合法的“无可指纹化 visible user/assistant message”
会话；它们均为 `fingerprint_version=1`、fingerprint hashes 非空，snapshot 已 hydrated，且
message fingerprint row count 与 `message_count` 严格一致。因此 hydration gate 不再错误要求
`message_count > 0`。

candidate manifest 验收：

```text
quick_check                 ok
legacy imports              322
source records              322
active canonical            322
unhydrated                  0
unhydrated snapshots        0
message-count mismatch      0
source-key collisions       0
duplicate output paths      0
pending duplicate candidates 0
```

candidate index 验收：

```text
quick_check                 ok
documents                   322
sections                    26,269
embeddings                  1,559  (unchanged)
documents with source/canonical identity   322 / 322
sections with source/canonical identity    26,269 / 26,269
```

### 10.3 v0.2.5 legacy-read hotfix

v0.2.4 canary 实测发现：旧 Markdown frontmatter 没有 `source_key` / `canonical_conversation_id`，
因此 `conversation_read()` 返回空 identity；search/recall 因直接来自 index 已正常。

v0.2.5 增加 `ConversationIndexDatabase.document_identity_for_file()`，`read()` 在 frontmatter
缺新字段时按 `vault_path` 回填 index identity。新增回归测试后：

```text
targeted retrieval tests: 8 passed
full pytest:              250 passed, 3 warnings
git diff --check:         clean
```

正式 `v0.2.5` tag canary 实测：

```text
WebUI /admin                   303
MCP unauthenticated            401
legacy read source_key match   True
legacy read canonical match    True
source_system                  codex
search identity                True
recall identity                True
```

### 10.4 Production cutover result

正式 production 容器：

```text
name:      research-memory-gateway
image:     ghcr.io/kettly1260/research-memory-gateway:v0.2.5
revision:  a97f80b655ee5024abe2d609fb39fa0c54896313
ports:     18787 -> 8787 (MCP), 18788 -> 8788 (WebUI)
network:   ai_network
restart:   unless-stopped
status:    running
```

production post-cutover gate：

```text
WebUI /admin                 303
MCP unauthenticated          401
manifest quick_check         ok
source records               322
active canonical             322
unhydrated                   0
pending duplicate candidates 0
index quick_check            ok
documents                    322
sections                     26,269
embeddings                   1,559
documents identity coverage  322 / 322
sections identity coverage   26,269 / 26,269
search count (Fe3+)          3
search identity              True
recall identity              True
legacy read source_key       True
legacy read canonical        True
```

BGE-M3 在最终生产验收时已恢复：真实 `/v1/embeddings` POST 返回 HTTP 200；生产 `Fe3+`
搜索实际走 hybrid：

```text
count               3
fallback_to_lexical False
fallback_reason      None
identity             True
```

### 10.5 Immutability / rollback

切换前后 322 Markdown 未被 production migration 重写：

```text
Markdown aggregate SHA-256:
508b7038f05b01fdea73f44b5d3f728f81b2f4f81ae35728548e1e0971153e1a

raw Codex export ZIP SHA-256:
e1a853e494856163a0cc7493de4ec0c73480487a7a1efb9c2c902e83cb97018e
```

回滚点保留：

```text
container:
  research-memory-gateway-pre-v024
  image v0.2.3
  stopped / preserved

manifest backups:
  manifest.pre-v024.sqlite
  manifest.pre-v025-cutover.sqlite

index backup:
  conversation-index-v023-full.pre-v024.sqlite
```

本次 W13 产生的 `rmg-v024-*` migration/rehearsal work containers 与 v0.2.5 canary 已清理；
正式 production 之外不保留后台 sleep/work 容器。

**FINAL: Conversation Memory multi-source identity/dedup is live on NAS production with v0.2.5.**
