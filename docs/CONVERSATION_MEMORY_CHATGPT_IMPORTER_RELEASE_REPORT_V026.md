# Conversation Memory ChatGPT Export Importer — v0.2.6 Release Report

任务书：`CONVERSATION_MEMORY_CHATGPT_IMPORTER_TASKBOOK_V026.md`
执行日期：2026-09-13 → 2026-09-14
执行范围：C0 → C12 全阶段，独立 worktree `G:\LLM\worktrees\rmg-chatgpt-importer`（分支 `feat/chatgpt-importer-v026`）。

> **版本状态：`IMPLEMENTATION READY / REAL EXPORT VALIDATION PENDING`**
> `REAL CHATGPT EXPORT REHEARSAL: NOT RUN — fixture unavailable`（本机未找到真实
> ChatGPT Data Export ZIP，见 §3）。全部验证基于按真实 export 结构构造的 synthetic
> ZIP 与既有 Codex 回归。**本报告不声称真实 export 格式已验证，不声称 PRODUCTION READY。**

---

## 1. Git / 范围

```text
BASE SHA:          49c39694a191267d13974e00ce58773440d65361 (v0.2.5 production baseline)
worktree:          G:\LLM\worktrees\rmg-chatgpt-importer (feat/chatgpt-importer-v026)
final local SHA:   本报告所在的 commit（本地 commit，未 push/未 tag）
pushed?:           NO    tag?: NO    GHCR?: NO    NAS production 操作?: NONE
```

Files changed（全部处于任务书 §0 授权范围内；**未触碰 `webui/**`**）：

```text
新增:
  src/research_memory_gateway/conversations/chatgpt_export.py   ChatGPT reader（graph/branch/audit/asset）
  tests/chatgpt_fixtures.py                                     synthetic export builder
  tests/test_chatgpt_reader.py                                  矩阵 A/B + C4 映射 + C5 provenance
  tests/test_chatgpt_ingestion.py                               矩阵 C/D/E/F
  tests/test_chatgpt_attachments.py                             矩阵 G
  tests/test_chatgpt_cli.py                                     C9 CLI / auto-detection
  scripts/rehearse_chatgpt_import_v026.py                       C11 rehearsal 脚本（可复用于真实 ZIP）
修改（additive，均向后兼容）:
  conversations/models.py        ExportSessionRef.import_key + CHATGPT_PARSER_VERSION/SCHEMA_VERSION
  conversations/readers.py       Protocol 文档化 import_key 语义（签名不变）
  conversations/identity.py      provider-family canonical key（新 domain，不改 legacy canonical）
  conversations/identity_store.py find_family_records() / message_fingerprint_sequences()（新增只读查询）
  conversations/decisions.py     NEW_SOURCE canonical 走 family key；family 分支 continuation/stale 解析
  conversations/pipeline.py      import_key 贯通；legacy ledger row key 按 source system 隔离
  conversations/manifest.py      record(record_key=...) 覆盖参数（默认行为不变）
  conversations/attachments.py   AttachmentInventory(archive_asset_resolver=...) 可选钩子
  conversations/duplicates.py    candidate 检测批量化（行为等价，消 O(N²) 连接churn）
  conversations/cli.py           --format auto|codex|chatgpt、namespace、branch 过滤
  conversations/__init__.py      导出新符号
```

`git diff --check`：**PASS**（本地与 staged 均无 whitespace 错误）。

## 2. Reader / parser / schema

```text
source_system:   chatgpt
parser_version:  chatgpt-export-v1.0
schema_version:  chatgpt-conversations-v1
主解析源:        conversations.json（结构化 mapping graph）
chat.html:       仅作 ZIP 内文件清点，永不解析
缺 conversations.json 或 top-level 非 list -> UNSUPPORTED_EXPORT_FORMAT / MALFORMED_EXPORT fail-closed
ZIP 全程只读（zipfile mode "r"；rehearsal 以 SHA-256 before/after 证明）
```

## 3. Real export audit status

```text
real export available?:  NO
检索范围:                Downloads / Documents / Desktop / G:\ 根（*chatgpt*、*openai*export*、*user_data*.zip）
audit 输出:              reader.schema_audit() 已实现并通过 synthetic 验证：
                         archive file inventory、conversation/mapping-node/message field inventory、
                         content_type / role / metadata-key / asset-locator-scheme inventory、
                         timestamp shapes、branch shape、graph validation、malformed counts。
隐私:                    audit 只输出 inventory/count/hash/shape，不含 transcript、账号 email、
                         raw GUID、附件内容。
```

由于无真实 export，format 审计结论以 synthetic fixtures（按公开已知的 ChatGPT export
结构：`conversation_id/title/create_time/update_time/current_node/mapping`、node
`id/message/parent/children`、message `author.role/content.parts/metadata`）为基准；
reader 同时接受 `conversation_id` 与 `id` 两种 provider id 字段，降低真实格式差异风险。

## 4. Account namespace strategy（C6）

优先级（严格按任务书）：

1. export 内稳定 provider account GUID（`user.json.id`，严格 UUID 正则且拒绝 email 形态）
   → `account_namespace_hash("chatgpt", guid)`，raw GUID 只在内存中参与哈希，绝不持久化/打印；
2. 用户显式 `--account-namespace <label>` → hash 持久化，raw label 不落盘（有测试扫描全部
   产物文件验证泄漏为零）；
3. 仅显式 `--use-default-account-namespace` 时使用文档化 `chatgpt-default-v1`；
4. 都没有 → fail-closed `ACCOUNT_NAMESPACE_REQUIRED`（不静默、不用 archive SHA 当 namespace）。

## 5. Branch strategy（C3）

* mapping graph 校验：parent 引用、未知 children 计数、orphan 计数、non-dict node 计数；
  parent 链环检测 fail-closed（`GRAPH_CYCLE`，不可无限遍历）；traversal 不依赖 JSON
  object 顺序，children 仅有序遍历。
* **primary branch**：`current_node` 反向 parent 链；primary 永远导入（缺 current_node 时取
  最小 leaf id 兜底并标记 primary）。
* **alternate branches**：所有含可见 user/assistant 内容的 terminal leaf；与 primary 同路径
  去重；system/metadata-only leaf 不制造空 branch record。
* **branch id 优先级**：provider terminal leaf node id → leaf message 内 provider
  branch/thread id → 有序 node/message id 的 SHA-256 → normalized visible message sequence
  的 SHA-256。不用 title/update_time/ZIP 顺序。
* **同 provider conversation 的 branch 归属同一 canonical family**（见 §6），无需 manual
  duplicate review；该 grouping 仅适用于 (system, namespace, provider conversation id) 可
  证明同源的 branch siblings，绝不跨系统/跨账号。

## 6. Source / canonical strategy（C2/C5）

* `source_conversation_id` 永远是 bare provider conversation id；branch 使用独立
  `source_branch_id`；**禁止** `<provider-id>@<branch-id>` 伪装 provider id。
* 每个 importable branch 有唯一 `import_key`（`<provider-id>#branch=<branch-id>`），Codex
  默认 `import_key == conversation_id` 行为不变；pipeline/CLI 全部按 import_key 调 reader。
* **per-branch snapshot provenance**：`source_entry =
  conversations.json#conversation=<id>#branch=<branch_id>`；`source_sha256 =
  SHA256(canonical branch snapshot)`（path 节点的 node_id/parent/message canonical JSON，
  sort_keys，排除 children 兄弟结构——新增 sibling 永不扰动既有 branch 的 snapshot hash）；
  parser/schema version 不参与 payload hash（parser 升级不伪造 source 变更）。
  整包 conversations.json 的 hash 绝不用作 source_sha256 → 一场新会话不会让旧会话全部
  `source_changed`。
* **canonical family**：branch 记录的 canonical = `uuid5(CANONICAL_NAMESPACE_V1,
  provider_family_key)`；Codex legacy canonical 推导路径零改动。

## 7. Attachment strategy（C7）

* 只读 ZIP 内资产：`resolve_asset(locator)` 从 ZIP central directory 解析（file-service://
  file-XXX 等 pointer → basename/stem 匹配），zip-slip/绝对路径/盘符 entry 名直接拒绝。
* `AttachmentRef.content_hash` = 解析成功时资产字节的 SHA-256（不存在时退回 pointer 哈希）；
  `attachment_id` 以 pointer 为 identity → 内容相同的两个不同 pointer 保持两条 inventory
  记录（hash 复用，不重复复制）。
* 引用不存在 → unresolved record；**零网络访问**（代码路径中无任何 HTTP/CDN 访问）。
* 附件 inventory hash 参与 fingerprints；消息正文占位 `[attachment: ...]`，非文本 part 永不
  静默消失，未知类型计数为 `[unsupported-content: <type>]`（绝不写 Python dict repr）。

## 8. Full-export dedup / continuation / stale / divergence results（C8）

全部由 synthetic 全量 ZIP + pipeline 实测（详见 tests/test_chatgpt_ingestion.py）：

| 场景 | 结果 |
|---|---|
| 同一 ZIP 重复导入（same file） | 58/58 `skipped/unchanged`，0 Markdown 改写（字节级比对） |
| 新导出含旧+新会话（+20） | 旧 5 个全部 skipped unchanged；20 个 written |
| conversations.json 重排序 / 重打包 | snapshot hash 不变 → skipped（unchanged / same_source_in_new_archive） |
| title 变化、transcript 不变 | skipped；note path 稳定不变 |
| strict continuation（同 branch lineage 延长） | `source_continued`：same source_key / same canonical / same Markdown path，message_count 2→4 |
| stale snapshot（先新后旧） | `skipped/stale_snapshot`，新 note 字节级不变 |
| 同 branch 历史中间消息被改（非 branch 证据） | `conflict/source_diverged` fail-closed |
| regenerate / edited 分支 | 独立 source record（family-aware 解析：同族内 strict-prefix 延长=continuation，divergent=新 sibling），sibling note 字节级不受影响 |
| ChatGPT vs Codex 相同 transcript | 仅 `cross_source_exact_transcript` candidate；canonical 始终 2 个；confirmed collapse 后双方 note 均保留；rejected pair 再导入不复活 |

**关键设计说明**：leaf-node branch id 下，conversation 的自然延续会产生新 leaf。为满足
任务书 11.3（同 branch strict continuation → same source_key），决策层新增
family-aware 解析 `_family_branch_relation()`：同一 provider conversation family 内，存储
序列是 incoming 严格前缀 ⟺ 同 lineage 延长（树结构上 sibling 分支只可能 diverge，不可能
prefix-extend），因此 continuation/stale 判定由 provider stable identity 证明，不是内容
相似 merge；跨 system/跨 namespace 永远不进入该逻辑。

## 9. Cross-source duplicate safety

自动 merge = **ZERO**（沿用 v0.2.4/v0.2.5 candidate-only 机制；本轮新增测试 F29/F31/F32
在 ChatGPT×Codex 场景下复核）。branch family grouping 与 duplicate candidate 机制相互
隔离：同 canonical group 的 siblings 不会生成 candidate。

## 10. C11 Rehearsal（synthetic）

`python scripts/rehearse_chatgpt_import_v026.py --workdir <scratch> [--archive <zip>]`
—— 同一脚本直接支持真实 ZIP（A–I 全 checklist 内建）。synthetic 运行结果：

```text
archive: synthetic（脚本内建生成器）：330 conversations = 300 graph 型（每 3 场含 regenerate
         分支、每 4 场含追加轮次）+ 30 linear；全部带 image asset pointer（file-service://）
archive bytes: 86,732        import items: 430（branch 级 = 330 primary + 100 alternate）
schema audit: content_type / role / metadata-key / locator-scheme / timestamp / branch /
              graph / malformed 清单全部输出，且不含任何 transcript 或账号信息
dry-run:      430/430 dry_run，无副作用
first import: 33.9s（430 items, D: 盘）；repeat: 11.5s → 430/430 skipped、0 Markdown rewrite
counts:       430 Markdown notes / 430 source records / 330 canonical families（branch
              siblings 正确折叠为 conversation 级 family）/ 430 branch records
visible messages: 1,010
lexical index:  isolated SQLite，4,850 chunks；FTS search smoke 全部命中
raw ZIP:        SHA-256 before == after ✓（immutable proof）
peak RSS:       79.3 MB
环境说明:       同一脚本在 G: 盘 staging 曾测得 44s/58 items，为该盘小文件 sync-IO
                慢（D: 同负载 3.4s），非代码路径问题；已在报告如实记录。
```

## 11. C12 性能与资源门禁

* list/audit 不读附件内容进内存（仅 central directory 清点；内容仅在 resolve 时读取并缓存 per-locator）。
* conversations.json 一次加载（`json.loads`，330 conv / 86KB synthetic；真实大文件场景
  建议先跑 audit 观察峰值——`peak_rss_mb` 已内建于 rehearsal）。
* snapshot serialization 为每 branch O(path)，无全 archive O(N²)；duplicate candidate
  检测本轮批量化（`message_fingerprint_sequences` 单查询 + candidates 单次读取），消除了
  逐 pair 连接/查询 churn（行为等价，dedup 回归全绿）。
* 重复导入零 Markdown 改写 ✓；无新增后台线程/定时任务 ✓；import 热路径无 LLM 调用 ✓；
  无自动向量 backfill ✓（rehearsal 索引为纯 lexical FTS，isolated DB）。

## 12. Codex 回归状态（矩阵 H）

```text
37 existing Codex import tests:        PASS
38 v0.2.5 legacy read identity tests:  PASS
39 formal migration rehearsal tests:   PASS
40 full pytest:                        306 passed（baseline 250 + 新增 56），3 warnings（既有）
git diff --check:                      PASS
```

修改过的共享层均为 additive（import_key、family canonical、manifest record_key、
attachment resolver、duplicates 批量化），Codex 路径在分支过滤、legacy ledger key、
canonical 推导上逐字节保持旧行为。

## 13. Production

```text
production modified?:  NO
NAS / production manifest / index / Markdown / ZIP:  未触碰
main workspace:  仅读取任务书；scratch rehearsal 输出位于 .local（未跟踪，已清理）与 D:\Partition\TEMP
```

## 14. 最终 gate 对照（任务书 §17）

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
full pytest                                                 PASS (306)
git diff --check                                            PASS
production modified                                         NO
```

## 15. 结论与后续

* **IMPLEMENTATION READY / REAL EXPORT VALIDATION PENDING**。
* 真实 ChatGPT Data Export ZIP 到位后，无需改代码：
  `python scripts/rehearse_chatgpt_import_v026.py --workdir <scratch> --archive <real.zip>`
  即可完成 C11 全 checklist（含 raw ZIP 不可变证明与 second-export 增量解释），通过后再
  评估 production 导入窗口。
* 本任务未做（须另行授权）：push、tag、GHCR、NAS production import/migration、WebUI
  import 页面暴露 ChatGPT reader、全量 embedding backfill。
