# Conversation Memory Multi-Source Identity & Dedup — v0.2.4 Implementation & Release Report

任务书：`CONVERSATION_MEMORY_MULTISOURCE_DEDUP_TASKBOOK_V024.md`
执行日期：2026-09-13（含独立审计后的 remediation commit）
执行范围：W0 → W12 + 审计 remediation。**W13（push / tag / GHCR / NAS production migration）未执行，等待用户明确授权。**

> **版本状态说明**：`76e0466`（首次 W0-W12 commit）经独立审计判定**尚不能进入 W13**。
> 其 5 项 release blocker 已在本 remediation commit 中修复，本报告取代此前声称的
> "76e0466 READY" 结论。

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
VERSION TARGET: v0.2.4
baseline SHA:   de1e646（保留）
first commit:   76e0466（W0-W12 初版，未 amend/reset）
remediation:    <本 commit>（在 76e0466 之上的新本地 commit）
remote main:    d6e44f0（= v0.2.3 tag，未 push）
working tree:   既有未跟踪文件 (.local/, 0, 0) 全程保留，未 stage
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
pytest: 249 passed（基线 187 + W10 53 + remediation 7 + formal rehearsal 2），0 failed
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
production manifest / index / notes / rollback container 修改: 否
  （production manifest 无任何 v2 表；legacy 322 行原样；migration 从未在生产执行）
duplicate source 物理删除: 否
```

## 7. 兼容性

- MCP 工具名不变；additive 参数/字段。
- legacy `conversation_imports` 继续维护；裸 `conversation_id` 在 Codex-only 场景不变；多来源命中同裸 ID 显式 ambiguous。
- 既有 Source Identity 字段全部保留；canonical/source key 为补充层。
- embedding identity 语义未变；无全量 re-embedding；无 vector backfill / ANN。

## 8. Release blocker 核对（remediation 后）

```text
pytest all green                                  PASS (249)
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
production untouched                              PASS（程序化验证）
```

## 9. 结论

**RELEASE READINESS: READY（本地，remediation commit 之后）** — 审计 5 项 blocker 全部修复并有回归测试覆盖；任务书 §23 Definition of Done 20 条满足。

W13 仍未执行，等待再次独立验收与用户明确授权：
- 不创建 `v0.2.4` tag；不 push GitHub；不更新 GHCR；
- 不切 NAS production container；不在 production manifest 上执行首次 schema migration。
