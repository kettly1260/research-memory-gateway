# Conversation Memory Pipeline 322-Session Rollout 与演练评估报告

- 报告日期：2026-09-12
- 执行项目：`G:\LLM\memory`
- 执行依据：`docs/CONVERSATION_MEMORY_ROLLOUT_TASKBOOK.md`
- 演练目录：`G:\LLM\memory\exports\conversation-staging\full-322-lexical`
- 配置文件：`G:\LLM\memory\config.rollout.yaml`

---

## 阶段门控总评 (Sign-off Summary)

| 门控阶段 | 状态 | 结论说明 |
|---|---|---|
| **A. Lexical-only Staging** | **READY (PASS)** | 全量 322 场导入稳定、第二轮 100% 幂等跳过、FTS 索引 26,100 chunks 且最大字符数严格 <= 1500、Manifest 与 Index 零异常闭环、检索与 Recall 预算达标、人工抽检 P0=0/P1=0。 |
| **B. Canonical Vault Import** | **READY (PASS)** | 用户已明确确认 Vault 路径（`Research-AI-Hub/Vault`），正式配置文件与安全门禁生效；10 场 Canary 导入全部成功且第二轮 100% 幂等跳过；archive-local manifest 与 manual 保留区完整闭环，零越界写入，检索可精确定位 canonical 笔记，人工 Obsidian 抽检 100% 通过。 |
| **C. Bulk BGE-M3 Embedding** | **NOT READY (继承 B 门控)** | 本阶段 embedding 强制关闭（0 次 NAS 调用）。须在 Gate-B 通过后，待用户显式确认运行配置并执行 NAS smoke 与小批量 Canary。 |

---

## 1. Baseline (技术基线冻结)

- **Repo Root**: `G:\LLM\memory`
- **Git Branch**: `v2/p0-hardening`
- **HEAD Commit**: `ba50e6296fb871468d868eda405c6855bfee3353`
- **Git Status**: Tracking clean, dirty working tree intact (未提交历史文件与测试脚手架均安全保留)
- **Python Version**: Python 3.12.0
- **Base Config**: `G:\LLM\memory\config.yaml`
- **Raw Export ZIP**: `D:\Partition\F\Study\博士文件\Research-AI-Hub\_codex_workspace\tmp\chat-export\codex-sessions-20260912-012538.zip`
- **Raw ZIP SHA-256 (Before)**: `E1A853E494856163A0CC7493DE4EC0C73480487A7A1EFB9C2C902E83CB97018E`
- **Raw ZIP SHA-256 (After)**: `E1A853E494856163A0CC7493DE4EC0C73480487A7A1EFB9C2C902E83CB97018E` (完全不可变，一致)
- **Pytest Suite**: `175 passed, 3 warnings in 28.48s` (0 failed, 0 error)
- **Git Diff Check**: `git diff --check` exit code 0 (无空白/冲突错误)

---

## 2. Runtime Config (演练专用运行配置)

创建独立配置文件 [`config.rollout.yaml`](file:///G:/LLM/memory/config.rollout.yaml)：
- **resolved staging root**: `G:\LLM\memory\exports\conversation-staging\full-322-lexical`
- **resolved index path**: `G:\LLM\memory\exports\conversation-staging\full-322-lexical\.ai-memory\index.sqlite`
- **archive-local manifest path**: `G:\LLM\memory\exports\conversation-staging\full-322-lexical\.ai-memory\manifest.sqlite`
- **retrieval.mode**: `keyword`
- **retrieval.embedding.enabled**: `false` (硬禁用向量)
- **conversation_archive.vault_root**: `null` (硬禁用正式 Vault)
- **sources.allowlist**: `['D:/Partition/F/Study/博士文件']` (严格限制在科研根目录，只读)

---

## 3. Raw Export Audit (原始数据包审计)

运行 `audit-export` 得到 [`full-322-audit.json`](file:///G:/LLM/memory/exports/conversation-staging/full-322-artifacts/full-322-audit.json)：
- **Archive SHA-256**: `e1a853e494856163a0cc7493de4ec0c73480487a7a1efb9c2c902e83cb97018e`
- **Package Version**: `1`
- **Exported At**: `2026-09-11T17:26:54.296156700+00:00`
- **Total Sessions**: `322` (真实会话总数与任务书要求 322 完全一致)
- **Estimated Subagents**: `0` (按导出包顶层分类初估)

---

## 4. Full-Session Staging Import (全量导入与统计)

全量导入在全新空目录 `exports/conversation-staging/full-322-lexical` 中执行，首轮生成 [`full-322-import-first.json`](file:///G:/LLM/memory/exports/conversation-staging/full-322-artifacts/full-322-import-first.json)：

### 4.1 首轮导入结果
- **Total Requested**: 322
- **Written**: 322
- **Skipped**: 0
- **Conflict**: 0
- **Failed Retryable**: 0
- **Index Stale**: 0
- **Dry Run**: 0

> **定向小修记录**：演练初期发现若会话标题超长（>150字），截断文件名时可能切掉尾部会话ID与 `.md` 后缀导致重名碰撞。在 `vault_writer.py` 中增加了对标题单独安全截断（120字符以内）而保持会话ID和扩展名完整的定向保护，测试套件 175 项持续全绿。首轮导入实现 322/322 100% 成功写入。

### 4.2 结构指标统计 ([full-322-import-stats.json](file:///G:/LLM/memory/exports/conversation-staging/full-322-artifacts/full-322-import-stats.json))
- **Notes Total**: 322
- **Notes By Year**: `2026`: 322
- **Notes By Thread Source**:
  - `user`: 87 (主用户交互会话)
  - `subagent`: 211 (子智能体会话)
  - `guardian_review`: 24 (守卫审查会话)
- **Notes With Parent Thread ID**: 229 (具有显式父子会话血缘)
- **Notes Without Parent Thread ID**: 93
- **Notes With Attachments**: 10
- **Notes With Tool Activity**: 200 (62.1% 包含工具调用记录)
- **Notes With Source Anchors**: 313 (97.2% 包含正文可回溯 source anchor，其余 9 场为纯工具/空操作子任务)
- **Failed Sessions**: 0
- **Candidate / Conflict Files**: 0

---

## 5. Idempotency (幂等性复测)

不改变任何 Markdown 笔记，立即执行第 2 轮导入，生成 [`full-322-import-second.json`](file:///G:/LLM/memory/exports/conversation-staging/full-322-artifacts/full-322-import-second.json)：
- **Written**: 0
- **Skipped**: 322 (100% 命中 `unchanged`)
- **Conflict**: 0
- **Failed Retryable**: 0
- **Index Stale**: 0
- **结论**: 幂等重跑零修改，满足任务书硬性要求。

---

## 6. Index & Chunk Health (索引与分块健康度)

### 6.1 索引轮次指标
- **首次索引** ([full-322-index-first.json](file:///G:/LLM/memory/exports/conversation-staging/full-322-artifacts/full-322-index-first.json)):
  - Files Scanned: 322
  - Indexed Files: 322
  - Skipped Unchanged: 0
  - Chunks Indexed: 26,100
  - Embedding Enabled: `false`
- **二次 changed-only 索引** ([full-322-index-second.json](file:///G:/LLM/memory/exports/conversation-staging/full-322-artifacts/full-322-index-second.json)):
  - Files Scanned: 322
  - Indexed Files: 0
  - Skipped Unchanged: 322
  - Chunks Indexed: 0
  - 判定结果: 100% `unchanged`，零重复索引开销。

> **定向小修记录**：分块算法在处理末尾带句号的超长文本分句时，曾因 `re.split` 产生空片段陷入无限递归。在 `chunking.py` 中增加了对切分后片段长度严格小于原长的条件约束，超长单句平滑回退至保底 `hard_split`，彻底根除了递归异常，全部单元测试继续 100% PASS。

### 6.2 Chunk Health 分布分析 ([full-322-chunk-health.json](file:///G:/LLM/memory/exports/conversation-staging/full-322-artifacts/full-322-chunk-health.json))
- **Total Sections**: 26,100
- **Min Chars**: 2
- **Mean Chars**: 843.74
- **Median Chars**: 1,039.0
- **P90 Chars**: 1,446.0
- **P95 Chars**: 1,477.0
- **P99 Chars**: 1,496.0
- **Max Chars**: 1,500 (硬上限 1500 字符)
- **Sections > 1500 Chars**: **0 (硬指标: PASS)**
- **Sections = 0 Chars**: **0**

### 6.3 Manifest/Index 闭环校验 ([full-322-manifest-closure.json](file:///G:/LLM/memory/exports/conversation-staging/full-322-artifacts/full-322-manifest-closure.json))
- **Total Records Checked**: 322
- **last_indexed_at 非空一致**: 322/322 (100%)
- **index_source_hash 非空一致**: 322/322 (100%)
- **content_section_hashes 非空一致**: 322/322 (100%)
- **Anomalies Count**: **0 (PASS)**

---

## 7. Attachment Inventory Audit (全量附件审计)

全量扫描生成 [`full-322-attachments.json`](file:///G:/LLM/memory/exports/conversation-staging/full-322-artifacts/full-322-attachments.json) 与 [`full-322-attachments.csv`](file:///G:/LLM/memory/exports/conversation-staging/full-322-artifacts/full-322-attachments.csv)：
- **Total Records**: 3,471
- **Status Counts**:
  - `found`: 1,374 (位于 allowlist 内且真实存在的文件，成功哈希)
  - `unresolved`: 1,824 (位于 allowlist 之外的安全隔离路径，不予读取或哈希)
  - `missing`: 215 (位于 allowlist 内但本地已不存在的历史文件，如已清理的临时文件)
  - `embedded`: 23 (内联数据，仅记哈希不解压)
  - `remote`: 35 (外部 HTTP/HTTPS 链接)
- **Unique Canonical Locators**: 1,222
- **Duplicates Collapsed**: 2,249
- **Hash Leaks Outside Allowlist**: **0 (完全符合安全边界，零违规读取)**
- **Synthetic Temp Fixture 复验**:
  - `found A -> content changed B -> attachment_changed -> file deleted -> attachment_changed` 全部在沙箱环境中自动化验证通过（退出码 0），未篡改用户任何真实文件。

---

## 8. Retrieval / Lineage / Provenance QA (检索与溯源验收)

生成详细检索报告 [`full-322-retrieval-qa.json`](file:///G:/LLM/memory/exports/conversation-staging/full-322-artifacts/full-322-retrieval-qa.json)，覆盖 27 个重点测试用例：

### 8.1 固定回归查询
1. `Fe3+`: 命中 10 条，Top: `019ea2b4` (score: 51.52)，首个 anchored 命中 `019ea2b4` (ordinal 26)。
2. `TOC_Focused.png`: 命中 10 条，Top: `01a08fdc` (score: 18.06)，首个 anchored 命中 `01a08fdc` (ordinal 1)。
3. `codex-mcp-config.dedup.toml`: 命中 10 条，Top: `019eab7a` (score: 18.06)，首个 anchored 命中 `019eab7a` (ordinal 1)。
4. `http://127.0.0.1:23120/mcp`: 命中 2 条，Top: `019eab7a` (score: 24.26)，首个 anchored 命中 `019eab7a` (ordinal 1)。
5. `01a08c1c-b49b-70a2-b1a4-cad68e4b1cd4`: 命中 1 条，Top: `01a08fdc` (score: 18.06)，首个 anchored 命中 `01a08fdc` (ordinal 1)。

### 8.2 分层抽样真实查询 (22 项)
覆盖文件路径（`OPEN_LOOPS.md`, `general-scope-of-1-3-dioxolanation`）、工具名（`shell_command`, `read_file`, `academic-research-suite`）、本地URL（`http://127.0.0.1`）、化学术语（`o-carborane`, `B2pin2`, `盐酸肼`, `二氯亚砜`）、错误码（`exit code`, `FileNotFoundError`）、项目名（`research-memory-gateway`, `Research-AI-Hub`）、会话UUID以及用户自然语句（`哈尔滨工业大学`, `毕业要求`, `SCI二区`），全部返回有效结构与精准 source anchor。

### 8.3 Recall Budget 压力测试
使用 `o-carborane` 在不同 `token_budget` 下对最终渲染 Agent Context（含标题、路径、源标记、分隔符）进行硬上限检验：
- `token_budget=10`: 字符上限 20，实际输出 20 字符 (PASS)
- `token_budget=100`: 字符上限 200，实际输出 200 字符 (PASS)
- `token_budget=1000`: 字符上限 2,000，实际输出 2,000 字符 (PASS)
- `token_budget=1500`: 字符上限 3,000，实际输出 3,000 字符 (PASS)
全部 100% 满足硬预算约束。

---

## 9. Manual Spot-Check (30 场人工分层抽检)

按照规范分层抽取 30 场会话，详见 [`full-322-manual-spotcheck.json`](file:///G:/LLM/memory/exports/conversation-staging/full-322-artifacts/full-322-manual-spotcheck.json)：
- **10 Ordinary User Threads**: `019e3ad1`, `019e3ba5`, `019e43a2`, `019e784f`, `019e78d9`, `019e915a`, `019e9222`, `019e959f`, `019e96c7`, `019ea4cd`
- **5 Lineage (Parent/Subagent)**: `019ea2b4`, `019ea2a1`, `019ea288`, `019ea4d0`, `019ea610`
- **5 Attachment-Heavy**: `01a09144`, `01a07eb4`, `019eb7b3`, `019ea2ad`, `019f6efa`
- **5 Tool-Heavy**: `01a09459`, `01a0808b`, `01a07e9c`, `01a00f89`, `01a00f6f`
- **5 Long / Compaction / Unusual**: `01a01289`, `019f71ab`, `019f4a5c`, `019f3b14`, `019f07a0`

### 9.1 专项安全与完整性检查
- **Base64 Payload 注入**: 0 处违规。所有图像数据均被有效替换为精简占位符，正文无大体积 Base64 膨胀。
- **Manual 区域存在性**: 30/30 均包含 `## 人工补充与关联笔记 (Related & Notes)` 标准保留区。
- **Tool Activity 膨胀控制**: 工具调用均以紧凑 Markdown 表格形式呈现，包含 call_id、摘要及负载哈希。
- **Raw Export ZIP 溯源验证**: 对前 10 场会话的 source anchor 向上反向检索原始 ZIP 中对应的 entry 和 JSONL 消息/事件，验证消息 ID、Turn ID 和序号一致性 **100% PASS**。

### 9.2 缺陷评级
- **P0** (数据错配 / 写错会话 / 严重丢内容 / 路径安全问题): **0**
- **P1** (lineage / provenance 明显错误): **0**
- **P2** (格式 / 摘要 / 可读性小瑕疵): **0**
- **P3** (cosmetic): **0**

---

## 10. Gate-A Decision (Lexical Staging 门控结论)

```text
=====================================================
LEXICAL STAGING SIGN-OFF: PASS
A. lexical-only staging: READY
=====================================================
```
全部 15 项技术硬门槛已 100% 达成。

---

## 11. Canonical Config & Canary (R7 / R8 执行与验收)

### 11.1 R7 正式配置确认与安全门禁
用户于 2026-09-12 明确确认正式 Obsidian Vault 挂载目录为：
`D:\Partition\F\Study\博士文件\Research-AI-Hub\Vault`
配置文件 [`config.conversation-production.yaml`](file:///G:/LLM/memory/config.conversation-production.yaml) 已落盘并解析：
- **vault_root**: `D:\Partition\F\Study\博士文件\Research-AI-Hub\Vault` (真实存在，包含 12 项已有文件/目录)
- **canonical_subdir**: `90_System/AI-Memory`
- **archive-local manifest**: `D:\Partition\F\Study\博士文件\Research-AI-Hub\Vault\90_System\AI-Memory\.ai-memory\manifest.sqlite`
- **allowlist**: `['D:/Partition/F/Study/博士文件']` (只读)
- **retrieval.embedding.enabled**: `false`
- **安全检查 1 (`--vault` 缺 `--confirm-vault` 必须拒绝)**: 命令行拦截成功 (PASS)
- **安全检查 2 (`--staging-dir` 越界指向 canonical vault 必须拒绝)**: 命令行拦截成功 (PASS)

### 11.2 R8 10-Session Canonical Canary 导入与验收
按照任务书要求及用户对候选名单的确认，选取 10 场代表性会话执行 Canary 演练：
- **5 场固定回归会话**:
  1. `019e3ad1-05d6-7382-972f-0d377e6092c6` (Nihao)
  2. `019eab7a-3a54-70b1-afd2-b89c0c98e8b2` (MCP配置)
  3. `01a08fdc-6da5-7f93-9119-ff79d9fea710` (TOC QA)
  4. `01a01289-e1b4-7242-98d9-368a75a16194` (TG图4)
  5. `019ea2ad-9a74-75c2-bf10-4246ca00ab25` (学术套件)
- **5 场新增代表性会话**:
  6. `019e3ba5-49d0-7340-9dee-ae6d62f9eaf1` (普通用户会话基准)
  7. `019ea610-6534-7992-8ef1-343aebf7682a` (父子智能体血缘 + 超长标题路径压力)
  8. `01a09144-1563-7053-91fc-4f8f2b824929` (附件密集型)
  9. `019f7401-5e87-75e1-af7a-fc909a3f4df6` (工具密集型，332 次调用)
  10. `019ea53b-63ed-7062-8cc4-30395b46bbe2` (长对话 / 密集上下文)

### 11.3 严格验收指标
1. **Dry-Run 路径检查**: 10 场目标 Markdown 文件在 Vault 中均不存在，零覆盖冲突，dry_run 全部通过。
2. **首轮 Canary 写入**: 10 场全部成功写入 `Vault\90_System\AI-Memory\Conversations\2026`，0 失败 / 0 冲突。
3. **第二轮幂等复测**: 不做任何修改立即重跑，**10 场全部命中 `skipped` (100% 幂等)**。
4. **Archive-Local Manifest 校验**: `manifest.sqlite` (24KB) 与 `manifest.csv` 包含完整 10 条记录，状态均为 `written` 且哈希一致。
5. **Manual/Machine 隔离区**: 10 篇笔记全部完整保留 `## 人工补充与关联笔记 (Related & Notes)` 区域，机器块被 `AUTOGEN_BEGIN`/`AUTOGEN_END` 隔离。
6. **越界防护检查**: 检查 Vault 根目录其它所有文件与文件夹，除 `90_System` 外**无任何其它文件被修改或覆盖 (100% PASS)**。
7. **检索与索引可定位性**: 索引建立 905 个 chunks，检索 `Nihao` 与 `01a09144` 均精准定位返回 Canonical Vault 内的笔记路径。
8. **Obsidian 渲染人工抽检**: 详细核验 5 场笔记的 Frontmatter、工具活动表格、Source 溯源区及 Markdown 结构，符合 Obsidian 最佳实践，评级为 PASS。

---

## 12. Gate-B Decision (Canonical Vault 门控结论)

```text
=====================================================
CANONICAL VAULT SIGN-OFF: PASS
B. canonical Vault import: READY
=====================================================
```
Gate-A PASS + 用户确认 Production Config + 10 场 Canonical Canary PASS，正式满足 Gate-B 准入条件。根据任务书安全规则，Gate-B 通过**不代表直接对剩余 312 场全量自动写入**，正式放量须经后续分批流程控制。

---

## 13. Embedding Config & Canary (R9 / R10 执行与验收)

### 13.1 R9 正式配置确认与 NAS Smoke 验证
配置文件 [`config.conversation-production.yaml`](file:///G:/LLM/memory/config.conversation-production.yaml) 已更新并确认：
```yaml
retrieval:
  mode: hybrid
  embedding:
    enabled: true
    base_url: http://192.168.22.102:28001/v1
    model: bge-m3
    endpoint_path: /embeddings
    timeout_seconds: 30
    max_retries: 2
```
- **网络与端口连通性**: ZeroTier 虚拟网络接口直连 `192.168.22.102:28001`，`TcpTestSucceeded: True`。
- **NAS BGE-M3 端点 Smoke**: 执行 `python scripts/smoke_bge_m3.py`，成功收到向量并返回：
  - `status: 200`
  - `dimension: 1024` (Expected=1024)
  - `exit code: 0` (PASS)

### 13.2 关键缺陷修复与代码加固
1. **任务书 16.1 节 Embedding 统计可信度修复 (`index.py` & `cli.py`)**:
   - 修复前：`embedded_chunks` 在启用 embedding 时直接取 `total_chunks`，无法区分实际新增请求与缓存复用。
   - 修复后：在 `ConversationIndexDatabase` 中引入严格的计数机制（`stats_new_embeddings`, `stats_cache_reused`, `stats_failures`, `stats_dimension_mismatches`）。
   - 数据库迁移：在 `conversation_index_runs` 表中无缝追加 `cache_reused_chunks` 和 `failed_chunks` 字段，并在 CLI 摘要及 `--json-report` 中精确暴露。
2. **候选路径多余 `/v1` 拼接缺陷修复 (`retrieval.py`)**:
   - 修复前：当 `base_url` 自身已包含 `/v1`（如 `http://...:28001/v1`）时，备选 URL 会错误生成 `.../v1/v1/embeddings` 导致 404。
   - 修复后：判断 `base_url` 是否已以 `/v1` 结尾，已有时坚决不追加备选路径。

### 13.3 R10 5-Session BGE-M3 Canary 导入统计
选定 5 场固定回归会话作为 Canary 批次（共 173 个 chunks，153 个唯一 embedding identities）：
- **首轮计算**:
  - `files_scanned`: 5
  - `indexed_files`: 5
  - `chunks_indexed`: 173
  - `new_embedding_requests`: 153 (与 `_estimate_missing_embeddings` 预估 153 100% 吻合)
  - `cache_reused_chunks`: 20
  - `embedding_failures`: 0
  - `dimension_mismatches`: 0
  - `vector_dimension`: 1024

### 13.4 增量与幂等验证 (Section 16.2)
1. **同批立即重跑 (Same-batch rerun)**:
   - 携带 `--changed-only`: 5 场全部判定为 `unchanged`，`skipped_unchanged: 5`，`new_embedding_requests: 0` (100% 幂等)。
   - 强制重刷索引 (Force re-index): `stats_new_embeddings: 0`，`stats_cache_reused: 100%`，0 次重复 NAS 调用。
2. **合成全生命周期验证 (`scratch/test_synthetic_canary_incremental.py`)**:
   - 初始索引：4 chunks -> 4 new requests
   - 立即重跑：0 new requests, 4 cache reused
   - 单 chunk 修改：**恰好 1 new request**, 3 cache reused
   - 升级至 v2：4 new requests, `embedding_version: v2`
   - 回滚至 v1：**0 new requests**, 4 cache reused, 激活历史 v1 向量
   - 活跃版本绑定：验证数据库中 live sections 与 active embedding 绑定严格一致，零混合版本。

### 13.5 向量与混合检索质量验证
- 向量检索测试：查询 `Nihao`，Top 1 命中 `019e3ad1-05d6-7382-972f-0d377e6092c6#1`（score=0.6962）。
- CLI 混合检索测试：`python -m research_memory_gateway.conversations.cli search "Nihao"` 返回精准的 `score_type: hybrid` 组合评分，精确定位返回正式 Vault 内部路径。

---

## 14. Gate-C Decision (Bulk BGE-M3 门控结论)

```text
=====================================================
BULK BGE-M3 EMBEDDING SIGN-OFF: PASS
C. bulk BGE-M3 embedding: READY
=====================================================
```
全部 8 项技术硬指标 100% 达成：
- Gate-A: PASS
- Gate-B: PASS
- NAS smoke: PASS (dimension=1024, exit code=0)
- embedding canary: PASS (5 sessions, 173 chunks, 153 identities)
- same-batch rerun: 0 unnecessary calls
- actual embedding request accounting trustworthy: PASS
- no dimension mismatch: PASS (0 mismatches)
- no mixed active versions: PASS (100% uniform binding)

根据任务书安全规则，Gate-C 通过**不代表直接启动全量 322 场自动 embedding**。正式批量写入必须遵循 Section 18 分批策略并在用户明确批准后方可执行。

### 14.1 R11 Batch 1 执行结果 (10 Sessions Canonical + BGE-M3 全量闭环)
用户批准继续后，系统完成了 Batch 1 全部 10 场 Canonical 会话的 BGE-M3 向量嵌入计算：
- **涉及会话**: 10 场完整 Canonical Canary 笔记（涵盖普通会话、长对话、父子血缘、多工具与多附件）。
- **累计向量索引**:
  - `files_scanned`: 10
  - `total_chunks`: 1,069 个
  - `unique_vector_identities`: 1,023 个
  - `actual_new_embedding_requests`: 1,023 项（首轮 153 + 次轮 870 = 1,023，100% 吻合）
  - `embedding_failures`: **0**
  - `dimension_mismatches`: **0** (全部严格为 1024 维)
- **多维度检索验证**:
  - 混合检索 `Nihao` 命中率 100%（文本与向量联合高分）。
  - 语义向量检索 `迁移` Top 1 精准命中 `019f7401-5e87-75e1-af7a-fc909a3f4df6#160`（《审计科研资料目录并规划迁移》，向量相似度 0.5352）。
- **当前状态**: Batch 1 10/10 场全部完成。

### 14.2 R11 Batch 2 执行结果 (25 Sessions Canonical + BGE-M3 全量闭环)
在 Batch 1 验证通过后，系统推进了第二批次（25 场会话，累计 35 场）的导入与 BGE-M3 向量嵌入：
- **涉及会话**: 25 场全新 Codex 会话（序号 11~35）。
- **Canonical Vault 导入**:
  - Dry-run: `dry_run: 25`, `conflict: 0`, `failed_retryable: 0` (`exports/batch-2-import-dry.json`)
  - 实写导入: `written: 25`, `conflict: 0`, `failed: 0` (`exports/batch-2-import.json`)
  - 幂等重跑: `written: 0`, `skipped: 25` (100% 幂等跳过，无多余写入)
  - 边界检查: 累计 35 个 `.md` 笔记全部严格位于 `Vault/90_System/AI-Memory/Conversations/2026/`，未触碰任何系统区外目录。
- **BGE-M3 向量索引**:
  - `files_scanned`: 35
  - `files_indexed`: 25
  - `skipped_unchanged`: 10 (前序 Batch 1 会话安全跳过)
  - `chunks_indexed`: 679 个
  - `unique_vector_identities`: 累计 1,553 个（Batch 1 1,023 + Batch 2 530）
  - `actual_new_embedding_requests`: 530 项（精准对应新增向量身份）
  - `cache_reused_chunks`: 149 项（会话内重复内容命中缓存）
  - `embedding_failures`: **0**
  - `dimension_mismatches`: **0** (全部严格为 1024 维)
  - `sections_without_embedding`: **0** (全库 1,748 个 sections 100% 具备有效向量嵌入)
- **当前状态**: Batch 2 (25/25 场，累计 35/35 场) 全部完成，准备就绪进入 Batch 3（50 场会话，累计 85 场）。

---

## 15. Remaining Risks (遗留风险说明)

1. **NAS 批量并发稳定性与耗时**: 全量剩余约 287 场会话包含约 23,000 个待嵌入 chunks，单并发下总耗时预计约 40~50 分钟。正式放行严格按照 Batch 3 (50 场) -> Batch 4 (100 场) -> Batch 5 (剩余 137 场) 分批推进，所有批次支持断点续传与幂等安全恢复。
2. **Windows 终端编码显示**: 在 Windows PowerShell 默认 GBK 编码下直接向 stdout 打印特殊字符（如立方符号 `³`）可能抛出编码错误，生产 CLI 调用建议始终指定 `--json-report` 或设置 `$env:PYTHONIOENCODING="utf-8"`。

---

## 16. Explicitly NOT Executed (明确未执行的操作)

为保障绝对安全，本次演练明确**未执行**以下行为：
- 未对剩余 287 场未授权会话执行全量自动写入（严格限制在已批准的 Batch 1+2 累计 35 场范围内）。
- 未修改原始 export ZIP 文件（SHA-256 哈希 `E1A853E494856163A0CC7493DE4EC0C73480487A7A1EFB9C2C902E83CB97018E` 严格未变）。
- 未删除或覆盖历史 Gateway 数据库。
- 未执行 `git commit` 或 `git push`。
- 未修改或重启 Unraid NAS。
- 未清理当前工作树中现有的未提交文件。

