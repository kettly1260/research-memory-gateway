# Conversation Memory Multi-Source Identity & Dedup — v0.2.4 Implementation & Release Report

任务书：`CONVERSATION_MEMORY_MULTISOURCE_DEDUP_TASKBOOK_V024.md`
执行日期：2026-09-13
执行范围：W0 → W12（本地实现、测试、322-session 副本 migration rehearsal）。
**W13（push / tag / GHCR / NAS production migration）未执行，等待用户明确授权。**

---

## 1. 版本与 Git

```text
VERSION TARGET: v0.2.4
baseline SHA:  de1e646 (local main, 保留未动)
remote main:   d6e44f0 (= v0.2.3 tag)
final SHA:     <本次 commit>
working tree:  既有未跟踪文件 (.local/, 0, 0) 全程保留，未 stage，未删除
```

## 2. 新增架构层

| 模块 | 职责 |
|---|---|
| `conversations/identity.py` | `ConversationSourceIdentity`、deterministic `source_key`（`srcv1_<sha256>`，版本化 domain separator，禁用 runtime `hash()`）、UUIDv5 `canonical_conversation_id`、版本化 message/transcript fingerprints（`fingerprint_version = 1`）、序列关系判定（equal / strict-prefix / diverged）。 |
| `conversations/readers.py` | 平台无关 `ConversationExportReader` Protocol（source_system / parser_version / schema_version / list_sessions / get_session_ref / parse）。`CodexExportReader` 实现该协议；pipeline 不再硬编码 Codex。 |
| `conversations/identity_store.py` | additive v2 表：`canonical_conversations`、`conversation_source_records`、`conversation_source_snapshots`、`conversation_message_fingerprints`、`conversation_duplicate_candidates`、`canonical_aliases`、`dedup_decisions`。全部与 legacy `conversation_imports` 共存于同一 archive-local SQLite；legacy 表与数据永不 drop/改写。 |
| `conversations/decisions.py` | W4 import 决策状态机：unchanged / same_source_in_new_archive / source_packaging_changed / source_continued / stale_snapshot / source_diverged / output_missing / managed_output_modified / attachment_changed / parser_upgrade。stale snapshot 不可被 `--no-resume` 覆盖。 |
| `conversations/duplicates.py` | W5 candidate detector：5 种 candidate type，deterministic evidence_json，score 仅用于 review 排序；rejected pair 永不复活。 |
| `conversations/pipeline.py` | 多来源编排：先 parse → fingerprints → v2 决策 → 执行；同步 legacy ledger（跨系统同裸 ID 时拒绝抢占 legacy 行）；新来源写入后非阻塞跑 candidate detection。 |
| `conversations/cli.py`（扩展） | `dedup-audit` / `dedup-list` / `dedup-show` / `dedup-resolve`（强制 `--confirm-same` 或 `--reject`）/ `identity-show` / `migrate-identity`。 |
| `conversations/index.py`（扩展） | documents/sections additive 列（source_key、canonical_conversation_id、source_conversation_id、source_thread_id、source_branch_id）；新文档 id 采用 source key 防止跨平台裸 ID 覆盖；sections 删除改按 vault_path；search filters 新增 canonical/source 维度；`resolve_conversation_ambiguity`。 |
| `conversations/retrieval.py`（扩展） | SearchResult/HybridSearchResult/RecallContextItem 返回 source_key + canonical id；`conversation_recall` 默认 `collapse_canonical=true`（仅针对已 canonical-linked 的跨源组，单源组不受影响），`collapse_canonical=false` 为审计 opt-out；read() metadata 补齐。 |
| `conversations/vault_writer.py`（扩展） | 新写 note frontmatter 增加 canonical/source 字段；branch 非空时文件名带 branch 段；不同 source record 之间的 note 覆盖防御 + source_key 后缀 fallback 路径。 |
| `webui/app.py`（扩展） | `conversation_identity` 统计（canonical/source/pending/confirmed/rejected/source_system 分布）；search/recall API 透传新 filters 与 collapse 参数。 |
| `agent_surface/tools.py`（扩展） | `conversation_search` / `conversation_recall` additive 参数（canonical_conversation_id / source_key / source_conversation_id / collapse_canonical）。 |

## 3. 决策状态机（摘要）

```text
source record 不存在：
    provider ID 缺失 -> 以 normalized transcript 派生确定性 synthetic id；
    与既有同源 record 完全同 transcript -> 复用既有 source/canonical；
    否则 new source record + new canonical（UUIDv5）。

source record 存在：
    entry hash 相同   -> unchanged / same_source_in_new_archive（更新 last_seen/snapshot seen_count）
                         （managed note 被手工改动 -> conflict；index 过期 -> index_stale）
    entry hash 不同：
        transcript 相同                    -> source_packaging_changed（不重写 note）
        旧 sequence 是新 sequence 严格前缀   -> source_continued（原 path 原地更新）
        新 sequence 是旧 sequence 严格前缀   -> stale_snapshot（绝不截断；--no-resume 也不可绕过）
        其余                               -> source_diverged（candidate copy + review，不覆盖原 note）
```

## 4. 测试（W10）

```text
pytest: 240 passed（基线 187 + 新增 53），0 failed
git diff --check: clean
frontend: 未修改，lint/build 不适用
```

新增测试文件与任务书 13 节对应：

| 测试文件 | 覆盖 |
|---|---|
| `test_conversation_identity_v24.py` | 13.1 #1-5 + fingerprint 规范（5.1-5.4）：跨进程确定性、PYTHONHASHSEED 无关、保守 normalization 禁令、tool/attachment 独立 evidence |
| `test_conversation_identity_migration_v24.py` | 13.2 #6-10：322-style legacy → v2 记录数一致、幂等重放 0 duplicate、output path 不变、legacy 表不 drop、裸 conversation_id 仍可解析 |
| `test_conversation_ingestion_v24.py` | 13.3 #11-14 + 13.4 #15-20：首导/重复 skip/新 archive last_seen/packaging 变化不重写；continuation 原 path 稳定、标题变化 path 稳定、manual region 保留、stale 不可截断（含 forced）、中途改写消息 → conflict/source_diverged、branch divergence 独立 record 不互覆 |
| `test_conversation_multisource_v24.py` | 13.5 #21-25：跨源同文本 candidate-only 不 auto merge、跨系统同裸 ID 无 collision、exact cross-source evidence 正确、near-duplicate pending only、rejected pair 再导入不复活 |
| `test_conversation_dedup_review_v24.py` | 13.6 #26-30：confirm same 单 canonical、双 note 保留、loser canonical alias 可解析、reject 不变 canonical、无显式参数拒绝执行 |
| `test_conversation_retrieval_v24.py` | 13.7 #31-36：search 返回 source_key/canonical、confirmed collapse 默认生效、pending/rejected 不 collapse、`collapse_canonical=false` opt-out、ambiguous bare id 显式拒绝、Codex-only 排名不回归 |
| `test_conversation_index_v24.py` | 13.8 #37-40：旧 index 打开即 additive 迁移、旧 note 无 canonical frontmatter 经 identity mapping 补齐索引、source/canonical 写入 documents/sections、二次 changed-only 全 unchanged |

## 5. 真实 322-session 副本 rehearsal（W11）

环境：本地副本 `scratch/v024-rehearsal/copy`（SQLite backup API 复制 manifest + 322 个 Markdown；raw ZIP 只读未动；production manifest/notes 零接触）。

```text
[B] migration: PASS
    legacy imports           322
    source records           322
    canonical conversations  322
    pending duplicates         0
    source key collisions      0
    canonical collisions       0
    output path changes        0
    markdown files changed     0

[C] repeat full-export import: PASS（含一项环境漂移说明，见下）
    skipped / unchanged      317
    written                    5   （全部 reason=attachment_changed，见 §6）
    conflict                   0
    failed_retryable           0
    index_stale                0
    非 drift note 改动数        0

[D] synthetic continuation overlay: PASS
    同 source ID + 新 user/assistant 消息 + 新标题
    -> reason=source_continued，原 path 原地更新，note 总数保持 322（无 orphan duplicate）

[E] stale replay: PASS
    再导旧 snapshot -> reason=stale_snapshot，continuation 内容完好（note 未被截断）

overall: PASS
```

## 6. 环境漂移说明（repeat import 中 5 个 attachment_changed）

9 月 12 日基线（"322/322 skipped"）之后，5 个会话（01a09144…、01a07eb4…、019e9221…、01a009b1…、019f46ab…）所引用的 vault 附件文件内容发生了外部变化（例：`Research-AI-Hub/vault/meta/ai-context/open_loops.md` 由 23,240 字节增长至 35,909 字节，content_hash 随之改变）。

对照实验：在 `d6e44f0`（v0.2.3）代码上对同一 manifest 副本与同一环境重放 decide，得到**完全相同的 5 个 `write / attachment_changed`**。结论：该差异为外部数据漂移触发的既有语义，**不是 v0.2.4 引入的回归**；identity 迁移后的 317 个其余会话全部 clean skip。若需字面 322 skipped，可（a）恢复这 5 个引用文件的历史内容，或（b）接受 5 个 note 按 attachment_changed 语义原 path 重写后重测。

## 7. 安全与隐私

```text
raw account identifiers 出现在 Markdown/API/日志/JSON 报告中: 否（只存 namespace hash）
secrets / DB / ZIP / NAS config 被 stage: 否
raw export ZIP 被修改: 否（只读 sha256 校验使用）
duplicate source 被物理删除: 否（confirm same 仅 alias/merged_into）
```

## 8. 兼容性

- MCP 工具名不变（`conversation_search` / `conversation_read` / `conversation_recall`），仅 additive 参数/字段。
- legacy `conversation_imports` 表继续维护；旧客户端裸 `conversation_id` 在 Codex-only 场景行为不变；多来源命中同裸 ID 时返回显式 `ambiguous_conversation_id`，不静默选择。
- 既有 Source Identity（source_originator/surface/version、thread_source、parent_thread_id、agent_path 等）全部保留；新 canonical/source key 为补充层。
- embedding identity 语义未变，不触发全量 re-embedding；不做 vector backfill / ANN（后续任务）。

## 9. Release blocker 核对

```text
pytest all green                                  PASS (240)
git diff --check                                  PASS
frontend lint/build                               N/A（前端未改）
legacy migration rehearsal                        PASS
322 repeat import                                 PASS（317 skip + 5 环境漂移 attachment_changed，v0.2.3 同行为）
continuation path stable                          PASS
stale snapshot cannot truncate                    PASS
cross-source exact transcript does not auto merge PASS
same raw ID across systems does not collide       PASS
dedup resolve preserves source notes              PASS
recall canonical collapse tests                   PASS
no secrets / DB / ZIP / NAS config staged         PASS
```

## 10. 结论

**RELEASE READINESS: READY（本地）** — v0.2.4 identity/dedup core 满足任务书 §23 的 20 条 Definition of Done（其中第 20 条"不触碰 production"由本次停机保证）。

未执行（等待用户明确授权后按 W13 执行）：
- 不创建 `v0.2.4` tag；不 push GitHub；不更新 GHCR；
- 不切 NAS production container；不在 production manifest 上执行首次 schema migration。
