# Codex 会话导出格式审计

审计日期：2026-09-12  
状态：第一阶段调查完成；未修改原始导出，未批量生成摘要，未写入正式 Obsidian Vault。

## 1. 审计对象

- 原始归档：`D:\Partition\F\Study\博士文件\Research-AI-Hub\_codex_workspace\tmp\chat-export\codex-sessions-20260912-012538.zip`
- ZIP SHA-256：`E1A853E494856163A0CC7493DE4EC0C73480487A7A1EFB9C2C902E83CB97018E`
- ZIP 大小：345,087,662 bytes
- 解压后条目总大小：666,253,250 bytes
- ZIP 条目：323 个；其中 1 个 `manifest.json`、322 个 `rollout.jsonl`
- 目录条目：0；软链接条目：0；JSON 解析错误：0

原始 ZIP 在全部调查和 prototype 中均以只读方式打开。

## 2. 顶层索引格式

顶层索引是 JSON 文件：`manifest.json`。

顶层字段：

| 字段 | 含义 |
|---|---|
| `kind` | 固定为 `codex-session-export` |
| `packageVersion` | 当前为 `1` |
| `exportedAt` | 导出时间，ISO 8601 |
| `sessions` | 322 个 session 索引对象 |

每个 `sessions[]` 对象实际出现的字段：

| 字段 | 审计结论 |
|---|---|
| `sessionId` | 当前导出对象 UUID；应作为 conversation 主键 |
| `title` | 会话标题；175/322 直接等于 UUID，不能依赖它完成分类 |
| `cwd` | 会话工作目录 |
| `updatedAt` | Unix epoch 秒 |
| `relativeRolloutPath` | Codex 原始 sessions 树中的相对路径，仅作来源提示 |
| `fileEntry` | ZIP 内真实 JSONL 条目路径，是恢复入口 |
| `sizeBytes` | JSONL 未压缩字节数 |
| `sha256` | JSONL 内容 SHA-256，可用于不可变性和增量判断 |
| `sessionIndexEntry` | 包含 `id`、`thread_name`、`updated_at` |
| `sourceInstance` | 导出来源实例 metadata，类型不固定，不应作为主键 |

322 个 JSONL 的 SHA-256 均不同，没有发现整场会话文件级重复。

## 3. UUID 目录代表什么

ZIP 中路径形式为：

```text
files/<四位序号>-<UUID>/rollout.jsonl
```

结论：一个 UUID 目录对应一场 Codex session/thread，而不是一个 message、attachment 或 artifact。

- 对普通用户任务，它对应根会话。
- 对 subagent，它对应一个单独的子 thread。
- 对 guardian/approval review，它对应一个单独的审查 thread。
- `manifest.sessions[].sessionId` 与 `session_meta.payload.id` 在 322 场中全部一致。
- `session_meta.payload.session_id` 有 216 场不等于当前 UUID；这些主要是 subagent/guardian 记录，它常保存父 thread ID。因此不能把该字段误当作当前 conversation ID。

线程类型：

| `thread_source` | 数量 |
|---|---:|
| `user` | 87 |
| `subagent` | 211 |
| `guardian_review` | 24 |

229 场有 `parent_thread_id`，涉及 63 个父 thread；本次导出中所有父 ID 均能在 manifest 找到。分支/子代理关系应通过 `parent_thread_id` 和 `source.subagent` 恢复，不应靠目录名猜测。

## 4. UUID 目录内部结构

每个 UUID 目录只有一个 `rollout.jsonl`。没有独立的图片、PDF、代码 artifact 或附件文件。

共 86,639 条 JSONL 事件：

| 外层 `type` | 数量 |
|---|---:|
| `response_item` | 45,992 |
| `event_msg` | 36,676 |
| `turn_context` | 2,495 |
| `token_usage_record` | 723 |
| `session_meta` | 322 |
| `world_state` | 250 |
| `inter_agent_communication_metadata` | 96 |
| `compacted` | 85 |

主要 payload 对象包括 `message`、`reasoning`、function/custom tool call/output、web/tool search、compaction、task start/complete/abort 和 thread settings。

消息角色：assistant 4,447；user 3,196；developer 857。assistant 中 `final_answer` 2,229、`commentary` 2,030、未标 phase 188。

## 5. 索引与 UUID 目录关联

恢复链路是：

```text
manifest.sessions[].sessionId
  -> manifest.sessions[].fileEntry
  -> files/NNNN-UUID/rollout.jsonl
  -> session_meta.payload.id 再校验
  -> manifest.sessions[].sha256 校验内容
```

`relativeRolloutPath` 指向导出前的原始位置，不是 ZIP 内读取路径。ZIP 内必须使用 `fileEntry`。

## 6. 一场完整会话的恢复规则

1. 校验 ZIP SHA-256 和 `manifest.kind/packageVersion`。
2. 用 `sessionId` 找到 `fileEntry`，读取 JSONL，并校验 entry SHA-256。
3. 以 `ordinal` 排序；只有缺失 ordinal 时才退回 JSONL 行序。
4. 用 `session_meta.payload.id` 校验当前 thread；读取 `parent_thread_id`、`thread_source`、`agent_path`、`cwd`、客户端版本等 metadata。
5. 以 `response_item.payload.type=message` 作为消息主记录，保留 role、phase、timestamp、message ID、turn ID 和文本。
6. `event_msg/item_completed` 中的 UserMessage/AgentMessage 是 UI/event 镜像，不能再次转成正文，否则会重复。
7. 用 `internal_chat_message_metadata_passthrough.turn_id` 将消息归入 turn；工具调用用 `call_id` 配对。
8. 子代理/审查分支按 `parent_thread_id` 连接为 session tree。当前格式没有稳定的逐消息 parent/child 图，不应伪造消息级分支。
9. `phase=final_answer` 才能标记最终答复。只有 commentary 或 turn_aborted 时应标记为 `incomplete_or_unknown`。
10. `input_image` 作为附件引用处理；不把 base64 直接写入 Markdown 或 embedding。

## 7. 重复、噪声和不应直接 embedding 的内容

### 7.1 重复消息表示

`response_item/message` 与 `event_msg/item_completed` 的 UserMessage/AgentMessage 大量重复。后者只保留事件统计和来源 ordinal。

Codex 还会把插件清单、app/environment/skills context、AGENTS.md、turn aborted、guardian transcript 和旧模型 handoff summary 记录成 `role=user`。这些内容必须保留可追溯信息，但默认不进入“用户目标”、人类可读正文和 embedding。

### 7.2 重复 system/developer 内容

- 322 份 `base_instructions` 共 6,060,671 bytes，但只有 11 个唯一哈希；重复实例 311。
- 250 个 `world_state` 共约 10,319,029 字符，最大单条 106,302 字符。
- developer 文本中存在大量重复 tool schema、app context 和权限说明。

这些内容应按哈希去重并从默认语义索引排除。

### 7.3 巨型工具输出

- 工具 payload：25,596 个
- >=10,000 字符：2,141 个
- >=100,000 字符：281 个
- >=1,000,000 字符：85 个
- 最大单个 payload：10,586,907 字符
- 最大单条 JSONL 记录：10,587,261 bytes

工具调用参数、错误码、命令、路径和关键 stdout 仍有价值，但完整 stdout、网页全文、图像返回、DOM/schema 和重复日志不应原样 embedding。推荐保存类型、工具名、call ID、ordinal、字符数、完整 payload 哈希和短摘要；原文由 ZIP 回溯。

### 7.4 图片和附件

- `input_image`：25 个
- 全部为 `data:` base64 URI
- 总长度约 12,814,958 字符
- 最大单图 3,015,606 字符

ZIP 没有独立附件对象。会话中提到的 PDF、DOCX、CSV、图片和代码文件，多数只是本地路径或工具参数；如果原路径文件已移动/删除，仅靠该 ZIP 无法恢复文件本体。这是最重要的数据丢失风险之一。

prototype 当前只记录附件类型、ordinal、message ID、哈希和大小，不复制图片，也不把 base64 写入 Markdown。

### 7.5 compaction / reasoning

- 外层 `compacted` 记录：85
- payload `compaction`：13
- reasoning payload：10,948

部分 compaction/reasoning 只有加密内容，公开导出中不能解密恢复。可恢复的是 compaction 前后仍存在的消息、event summary 和来源位置。不能声称已恢复内部思维全文。

## 8. 潜在数据丢失与误判风险

| 风险 | 影响 | 处理 |
|---|---|---|
| 外部附件未打包 | 路径存在但文件本体可能丢失 | 后续单独扫描路径并建立 attachment manifest；缺失必须报告 |
| 加密 compaction/reasoning | 无法恢复内部内容 | 只记录存在性和 ordinal，不伪造 |
| UI event 与 response 重复 | 双倍消息和错误权重 | `response_item/message` 为主，event 只作审计 |
| 运行时注入伪装为 user | 错把 AGENTS/环境当用户目标 | 注入分类并默认排除 |
| 会话标题为 UUID | 分类和浏览质量差 | 从首个真实用户目标生成候选标题，人工可覆盖 |
| commentary 被误当最终结论 | 中断任务形成虚假“最终结果” | 只认 `phase=final_answer`，否则标 incomplete |
| tool stdout 本身已截断 | ZIP 可能只保存 UI 可见输出 | 记录截断标志和哈希，不推断完整结果 |
| 不同时间字段语义不同 | 创建/更新时间混淆 | 分别保留 manifest、session_meta 和事件时间 |

## 9. 推荐 ingestion 方案

```text
Immutable ZIP
  -> manifest/hash validation
  -> CodexExportReader
  -> normalized conversation + session tree
  -> injected/duplicate/tool/attachment classification
  -> semantic conversation/topic segmentation
  -> Obsidian staging writer
  -> manifest ledger
  -> Markdown heading chunks
  -> SQLite FTS + pluggable vector index
```

第一版应以完整可见对话和精确 source anchors 为基础，摘要/项目/主题只能是附加层，不能覆盖原始证据。

## 10. Prototype 证明

| 类型 | conversation ID | 验证点 |
|---|---|---|
| 普通短会话 | `019e3ad1-05d6-7382-972f-0d377e6092c6` | 消息顺序、注入过滤 |
| 精确命令/工具会话 | `019eab7a-3a54-70b1-afd2-b89c0c98e8b2` | call ID、参数/输出摘要、哈希 |
| subagent | `01a08fdc-6da5-7f93-9119-ff79d9fea710` | parent thread、子代理来源 |
| 含图片附件 | `01a01289-e1b4-7242-98d9-368a75a16194` | 3 个附件引用、base64 排除 |
| 含 compaction | `019ea2ad-9a74-75c2-bf10-4246ca00ab25` | incomplete/final 判定、精确路径保留 |

staging：`G:\LLM\memory\exports\conversation-staging\2026-09-12-export-audit`

验证结果：5/5 frontmatter 完整；5/5 source entry 存在且 SHA-256 匹配；全部 source ordinal 和 message ID 可回溯；0 篇含原始 base64；第二次相同导入全部跳过。
