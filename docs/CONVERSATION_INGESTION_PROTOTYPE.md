# Codex 会话导入 Prototype

验证日期：2026-09-12  
范围：5 场代表会话；仅写入 Research Memory Gateway 自身的 staging，不写正式 Vault，不批量 embedding。

## 1. 输入与输出

输入：

`D:\Partition\F\Study\博士文件\Research-AI-Hub\_codex_workspace\tmp\chat-export\codex-sessions-20260912-012538.zip`

输出：

`G:\LLM\memory\exports\conversation-staging\2026-09-12-export-audit`

实现：

```text
src/research_memory_gateway/conversations/
  models.py          normalized dataclasses
  codex_export.py    ZIP/manifest/JSONL reader
  manifest.py        SQLite ledger、增量决策、CSV export
  vault_writer.py    Obsidian Markdown writer
  pipeline.py        parse -> decide -> write -> record

scripts/prototype_codex_conversations.py
tests/test_conversation_ingestion.py
```

parser version：`codex-export-v1.3`

## 2. 代表样本

| 类型 | conversation ID | 选择理由 |
|---|---|---|
| 普通短会话 | `019e3ad1-05d6-7382-972f-0d377e6092c6` | 验证消息顺序、标题和注入过滤 |
| 工具/精确字符串 | `019eab7a-3a54-70b1-afd2-b89c0c98e8b2` | 验证 call ID、参数、stdout 摘要、hash |
| subagent | `01a08fdc-6da5-7f93-9119-ff79d9fea710` | 验证 parent thread 和 thread source |
| 图片附件 | `01a01289-e1b4-7242-98d9-368a75a16194` | 验证 3 个 base64 图片只保存引用 |
| compaction/长任务 | `019ea2ad-9a74-75c2-bf10-4246ca00ab25` | 验证 compaction 记录和完成状态 |

## 3. 恢复和过滤规则

### 3.1 主消息

- 使用 `response_item.payload.type=message` 作为正文主记录。
- 以 ordinal 排序；保留 message ID、turn ID、timestamp、role、phase。
- `event_msg/item_completed` 的 UserMessage/AgentMessage 只统计，不重复写正文。
- `phase=final_answer` 才形成完成结果；只有 commentary 或 abort 的会话标记为 `incomplete_or_unknown`。

### 3.2 注入内容

以下内容即使以 `role=user` 出现，也默认从“用户目标”和人类可读对话中排除：

- AGENTS.md/project instructions
- environment/app context
- plugins/recommended plugins
- guardian transcript
- tool schema 和权限说明
- compacted handoff metadata

原始 ordinal、类型计数和 ZIP 来源仍保留，可从 source entry 回查。

### 3.3 工具和附件

工具 call/output 保存：工具名、call ID、ordinal、payload 字符数、SHA-256 和有限摘要。完整巨型 stdout 不复制到 Markdown。

`input_image` 保存为附件引用：MIME、长度、hash、message ID、ordinal。prototype 不复制 base64，也不把它送入 embedding。

## 4. Markdown 可读性与追溯

生成 note 包含 YAML frontmatter、会话概况、用户目标、过程/答复、工具活动、附件引用、特殊对象统计和 Source。

可追溯链路：

```text
Markdown conversation_id
  -> source_entry
  -> source_entry_sha256
  -> source ordinal / message ID / tool call ID
  -> ZIP rollout.jsonl 原始记录
```

frontmatter 中的 source hash 用于证明解析对象没有被替换；正文 source anchor 用于定位单条消息。Markdown 不取代原始 ZIP。

## 5. 增量与冲突保护

manifest 位于：

```text
.ai-memory/manifest.sqlite
.ai-memory/manifest.csv
```

prototype 已实现：

| 状态 | 条件 | 行为 |
|---|---|---|
| new | conversation ID 未记录 | 生成 Markdown 并登记 |
| unchanged | ID 和 source entry hash 均一致 | 跳过 |
| same source in new archive | entry hash 相同、archive hash 改变 | 跳过内容重算，可更新归档来源 |
| changed | 同 ID 的 source entry hash 改变 | 重新生成机器管理 note |
| conflict | 已登记 note 被人工修改 | 报冲突，不静默覆盖 |

## 6. 自动化测试

`tests/test_conversation_ingestion.py` 覆盖：

1. 消息、工具、附件、encrypted reasoning/compaction 和 provenance 恢复。
2. Markdown 可读性、精确字符串保留、base64 排除和非管理文件碰撞保护。
3. manifest 的 new/skip/change/conflict 判定。
4. pipeline 幂等和可读 CSV manifest。

验证结果：新增 ingestion 测试 `4 passed`；完整项目测试 `125 passed`。完整测试从 D 盘受控临时工作目录运行，使测试中的相对 `exports/` 输出不会修改 G 盘项目现有导出。

## 7. 已验证项目

- 5/5 note 的 frontmatter 具备 conversation/source/parser 字段。
- 5/5 source entry 存在，entry SHA-256 与 manifest 一致。
- note 中记录的 source ordinal 和 message ID 可以回查 JSONL。
- 0 篇包含 `data:image/` 或 `;base64,` 原文。
- 注入型 user message 未进入用户目标。
- 相同输入重复运行会返回 `skipped`。
- v1.3 最终 validation：5 篇 note、5 条 manifest；frontmatter、必需字段、source hash、ordinal 和 message ID 全部通过。

## 8. 尚未做的工作

- 未处理其余 317 场会话。
- 未写 `G:\LLM\obsidian`，因为该路径当前不存在且真实 Vault 尚未确认。
- 未复制或扫描外部附件；缺失附件报告尚未生成。
- 未建立 Vault heading FTS/chunk 表。
- 未对 staging 执行 `bge-m3` 批量 embedding。
- 未启用 reranker。
- 未增加 MCP `memory_search`/`memory_read`/`memory_recall` 的 Vault-backed 实现。

## 9. 下一次实施门控

开始批量导入前必须同时满足：

1. 用户确认真实 Obsidian Vault 根路径及 Research Hub 挂载关系。
2. 后续代码变更继续保持完整 pytest 回归通过。
3. 对 5 篇 staging note 完成人工可读性抽查。
4. 固定 parser/schema version 和 manifest 备份策略。
5. 建立附件路径 inventory，能够明确输出 found/missing/unresolvable。
6. 先验证从 Markdown 全量重建 FTS/vector 的流程，再启用全量 embedding。
