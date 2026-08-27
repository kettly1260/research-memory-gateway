# Research Memory Gateway V2：Agent 主动记忆架构改造执行计划

> 项目目录：`G:\LLM\memory`  
> 当前仓库：`https://github.com/kettly1260/research-memory-gateway.git`  
> 当前状态：`main` 工作区干净，并跟踪 `origin/main`  
> V2 核心目标：解决“Agent 明明接入了 Memory MCP，但不主动调用，长期记忆实际等于不存在”的问题。

---

## 1. 改造目标

现有 Research Memory Gateway 已经具备较完整的科研可信记忆能力，包括：

- SQLite 持久化
- SQLite FTS 检索
- Embedding + Rerank 混合检索
- Claim / Evidence / SourceRef
- VerificationStatus
- Proposal / Confirmation
- 冲突、撤回、Superseded 生命周期
- WebUI
- 审计与完整性检查
- 导入导出
- Streamable HTTP / SSE MCP
- API Key / Token 鉴权

当前最大的实际问题不是“后端能力不足”，而是：

> **是否调用记忆完全依赖 Agent 自己想起来。**

当前 Agent 保存一条记忆时，需要完成：

```text
发现这是长期资产
    ↓
判断应该调用 Memory
    ↓
可能调用 check_overlap
    ↓
理解 taxonomy
    ↓
构造完整 ResearchMemory
    ↓
构造 claims / evidence / source_refs
    ↓
propose_save
    ↓
询问用户确认
    ↓
save_research_memory
```

对于普通 Agent 来说，这是一条高成本旁路，因此非常容易被主任务忽略。

V2 的目标不是重新做数据库，而是把项目改造成：

```text
用户
  ↓
Agent
  ↓
只需要理解 3 个核心记忆动作
  ↓
recall_memory
capture_memory
verify_memory
  ↓
Research Memory Gateway Core
  ↓
自动完成检索、分类、证据组织、查重、proposal 和生命周期管理
```

---

# 2. Git 分支策略

## 2.1 原则

V2 **不使用长期 feature 分支作为未来主线**。

按照以下策略：

- 当前现有 `main`：冻结为 V1 老版本。
- 老版本分支名称：
  - `legacy/research-memory-gateway-v1`
- 新架构：
  - 继续使用 `main`
- 后续所有 V2 主开发：
  - 直接以新的 `main` 为主线。
- V1 legacy 分支：
  - 默认冻结。
  - 仅允许必要的严重 bug / 数据迁移修复。
  - 不再增加新功能。

最终结构：

```text
legacy/research-memory-gateway-v1
        │
        └── V1：当前 evidence-first MCP 实现
             冻结 / 仅严重维护

main
 │
 ├── V2.0 Agent Surface
 ├── V2.1 Capture / Proposal Queue
 ├── V2.2 ChatGPT / Codex Benchmark
 └── V2.x 后续长期主线
```

---

## 2.2 分支切换操作

### 第一步：确认当前状态

```powershell
cd G:\LLM\memory

git status
git branch -vv
git remote -v
git fetch origin
```

要求：

- 当前分支为 `main`
- working tree clean
- `main` 正常跟踪 `origin/main`

---

### 第二步：给当前 V1 打固定 Tag

推荐：

```powershell
git tag v1-pre-agent-memory-refactor
git push origin v1-pre-agent-memory-refactor
```

该 tag 作为绝对不会移动的 V1 基线。

---

### 第三步：把当前 main 改成 legacy 分支

```powershell
git branch -m legacy/research-memory-gateway-v1
git push -u origin legacy/research-memory-gateway-v1
```

此时：

```text
本地：
legacy/research-memory-gateway-v1

远端：
origin/main
origin/legacy/research-memory-gateway-v1
```

二者暂时都指向相同的 V1 commit。

---

### 第四步：重新创建新的 main

从当前 V1 基线建立新的主线：

```powershell
git switch -c main
git branch --set-upstream-to=origin/main main
```

此时本地重新拥有：

```text
main
```

后续所有 V2 改造直接提交到新的 `main`。

第一次 V2 commit 完成后：

```powershell
git push origin main
```

由于新的 `main` 是从原 V1 commit 直接向前发展，一般不需要 force push。

---

## 2.3 V1 分支管理规则

`legacy/research-memory-gateway-v1` 只承担：

- 旧版本回滚
- 数据兼容参考
- V1 安全修复
- V1 严重 bug 修复
- V1 与 V2 行为对照

禁止：

- 在 legacy 上继续开发 Agent Surface
- 在 legacy 上加入 EverOS
- 在 legacy 上大改 schema
- 在 legacy 上大改 WebUI
- 将 legacy 再作为主要开发线

---

# 3. V2 总体架构

V2 将现有项目逻辑拆成三层：

```text
                         ┌─────────────────────────┐
                         │ Research Memory Core    │
                         │                         │
                         │ SQLite / FTS            │
                         │ Embedding / Rerank      │
                         │ Claim / Evidence        │
                         │ SourceRef / Audit       │
                         │ Proposal / Lifecycle    │
                         └────────────┬────────────┘
                                      │
                     ┌────────────────┴────────────────┐
                     │                                 │
                     ▼                                 ▼
          Agent-facing Surface                  Admin Surface
                     │                                 │
             recall_memory                       WebUI
             capture_memory                      proposal 管理
             verify_memory                       merge
             get_project_state                   delete/update
                                                 audit
                                                 export
                                                 backfill
                                                 integrity
```

核心原则：

> **Agent-facing Surface 越简单越好。**

---

# 4. V2 不做什么

第一阶段明确不重新实现：

- SQLite
- FTS
- Embedding
- Rerank
- WebUI 主框架
- Claim / Evidence schema
- SourceRef
- Proposal 数据结构
- 安全鉴权
- 数据导出
- 数据库完整性检查

这些都继续复用现有 V1 Core。

V2 重点改：

1. Agent 看见什么工具。
2. Agent 怎么检索历史。
3. Agent 怎么提交值得保存的信息。
4. Gateway 怎么替 Agent 完成复杂 schema。
5. 如何衡量 Agent 是否真的主动使用 Memory。

---

# 5. Phase 0：V1 冻结与基线测试

## 目标

在 V2 开发前确保 V1 可随时恢复。

## 工作内容

- 完成上述 legacy 分支建立。
- 建立 `v1-pre-agent-memory-refactor` tag。
- 记录当前 DB schema version。
- 记录现有 MCP tool list。
- 记录当前 WebUI 功能。
- 记录当前 config schema。
- 补齐开发测试依赖。
- 执行完整 regression tests。
- 保存 baseline report。

当前仓库已有：

```text
test_models.py
test_webui.py
```

此前检查到约：

```text
test_models.py：45 tests
test_webui.py：21 tests
```

但当前 `.venv` 中缺少 pytest，需要在正式 V2 开发前修正开发依赖。

## 验收

V1 以下能力均应能够正常运行：

- SQLite
- FTS
- Hybrid retrieval
- Proposal
- Evidence validation
- WebUI
- MCP Server
- Export
- Audit

---

# 6. Phase 1：实现 Agent-facing `recall_memory`

这是整个 V2 的最高优先级。

## 6.1 Tool

新增：

```text
recall_memory
```

推荐输入：

```json
{
  "query": "之前 Fe 的硝酸溶液怎么配的？",
  "project": null,
  "limit": 5,
  "context_mode": "compact"
}
```

原则：

- 只有 `query` 必填。
- 其他参数尽量可省略。
- Agent 不需要知道 memory_type。
- Agent 不需要手动选择 FTS / vector / rerank。
- Agent 不需要理解 verification filtering。

---

## 6.2 Gateway 内部流程

```text
User Query
    ↓
Query normalization
    ↓
Project hint / project inference
    ↓
Memory lifecycle filtering
    ↓
FTS recall
      +
Vector recall
    ↓
Hybrid fusion
    ↓
Rerank
    ↓
Deduplicate
    ↓
Compact context
    ↓
Agent
```

---

## 6.3 返回结果必须短

默认返回：

```json
{
  "results": [
    {
      "memory_id": "mem_xxx",
      "title": "Fe3+ nitrate stock preparation",
      "content": "10 mM Fe3+ stock was prepared in 0.1 M HNO3...",
      "verification": "evidence_backed",
      "project": "Fe3-probe",
      "updated_at": "2026-08-13T...",
      "source_available": true
    }
  ]
}
```

第一次 recall 不要默认塞入：

- 全部 Evidence
- 全部 SourceRef
- relations
- 所有 metadata
- 原始完整对话
- embedding 内容

目标：

> **Recall 便宜，Verify 才昂贵。**

---

# 7. `recall_memory` Tool Description 重写

这是影响 Agent 是否主动调用的核心。

Tool description 不应只是：

```text
Search research memories.
```

建议明确写成：

```text
Retrieve relevant long-term memory from the user's previous research,
projects, experiments, workflows and prior decisions.

Use this whenever previous user-specific or project-specific context may
materially affect the answer.

Especially use when the user refers to:
- previous / earlier / last time / before
- continue / resume
- what we decided
- how something was previously prepared or configured
- existing experiment data
- prior files, paths, tools or settings
- established user preferences
- 之前 / 上次 / 以前 / 继续 / 还记得 / 原来 / 我们做过 / 怎么配的

Do not guess past user-specific facts when recall_memory can retrieve them.
```

---

# 8. Phase 2：实现 `verify_memory`

`recall_memory` 解决：

> “我以前做过什么？”

`verify_memory` 解决：

> “这个历史记忆到底可靠吗？”

接口：

```json
{
  "memory_id": "mem_xxx"
}
```

可扩展：

```json
{
  "memory_id": "mem_xxx",
  "claim_id": "claim_xxx"
}
```

返回：

- Claim
- VerificationStatus
- Confidence
- Evidence
- SourceRef
- DOI
- File path
- Conversation excerpt
- Conflict information
- Superseded information

典型触发：

```text
你确定吗？
这个数字哪里来的？
之前真的是 0.1 M HNO3？
这条结论是哪篇论文里的？
这个 SOP 有没有原始来源？
```

---

# 9. Phase 3：实现 `capture_memory`

这是 V2 第二重要模块。

## 9.1 当前问题

目前 Agent 保存长期记忆时，需要自行构造完整：

```text
ResearchMemory
├── project
├── topic
├── memory_type
├── summary
├── claims
├── evidence
├── source_refs
├── entities
├── relations
├── tags
├── next_actions
└── metadata
```

成本过高。

---

## 9.2 V2 Agent 输入

Agent 只需要告诉 Gateway：

```json
{
  "content": "本次 Pyr PL 测试使用新购买的 HPLC 级 DMSO，之前使用的是化学纯 DMSO。",
  "project": "Fe3-probe",
  "source_context": "current conversation",
  "importance": "auto"
}
```

---

## 9.3 Gateway 自己负责

```text
capture_memory
      ↓
Memory classifier
      ↓
Project resolver
      ↓
Claim extraction
      ↓
Evidence extraction
      ↓
Entity extraction
      ↓
SourceRef generation
      ↓
Overlap detection
      ↓
Conflict detection
      ↓
Storage policy
```

重要原则：

> **Agent 负责说“这个值得记住”。**

> **Gateway 负责决定“应该怎么记”。**

---

# 10. Phase 4：建立双层记忆

V2 不再要求所有内容都走同一个保存门槛。

## 10.1 Ambient Memory

允许自动保存。

适合：

- 项目路径
- 工具路径
- 当前项目状态
- 用户稳定偏好
- 软件配置
- Agent troubleshooting 经验
- 已完成任务
- 下一步任务
- workflow
- 可复用工程经验

例如：

```text
Origin MCP repository is located at G:\LLM\originlab-jx.
```

这类内容不应该每次弹出：

```text
是否保存？
```

否则用户和 Agent 都会倾向于放弃保存。

---

## 10.2 Trusted Research Memory

继续保持 evidence-first。

包括：

- 实验结果
- 实验条件
- 定量数据
- LOD
- 峰位
- 配液浓度
- 合成路线
- 文献结论
- 机理结论
- SOP
- 论文可以引用的结论

流程：

```text
capture_memory
      ↓
Research extraction
      ↓
Proposal Queue
      ↓
User review
      ↓
Trusted Memory
```

---

# 11. Phase 5：Proposal Workflow 改造

当前：

```text
Agent
 ↓
propose_save
 ↓
立即询问用户
 ↓
用户确认
 ↓
save
```

问题：

- 打断主任务
- Agent 不愿主动做
- 用户频繁确认会疲劳

V2：

```text
capture_memory
 ↓
Gateway 判断属于 Trusted Research Memory
 ↓
生成 Pending Proposal
 ↓
不打断当前任务
```

用户以后集中在 WebUI 审核：

```text
Approve
Reject
Needs Edit
Approve Selected
```

若用户明确说：

```text
把这个记下来
保存这个结论
这是确定的，存下来
```

则允许立即确认。

---

# 12. Phase 6：拆分 MCP Surface

## 12.1 Agent Surface

普通 Agent 默认只看到：

```text
recall_memory
capture_memory
verify_memory
get_project_state
```

其中 `get_project_state` 可视实现效果决定是否保留。

目标：

> 默认工具数量控制在 3–4 个。

---

## 12.2 Admin Surface

现有高级工具保留，但不默认暴露给普通 Agent：

```text
search_research_memory
get_research_memory
propose_save
save_research_memory
list_memory_proposals
get_memory_proposal
update_memory_proposal_status
check_overlap
merge_research_memories
mark_memory_status
audit_unverified
audit_database_integrity
export_memories
delete_research_memory
...
```

供：

- WebUI
- Admin Agent
- Debug
- 数据修复
- 高级管理

---

# 13. Phase 7：缩短 Skill / System Prompt

现有 Skill / Prompt 的主要问题：

- 太长
- taxonomy 信息过多
- Agent 被要求理解内部 schema
- 保存工作流步骤太复杂

V2 Agent Skill 应缩短至约 20–40 行。

核心规则：

```text
Long-term memory is available through recall_memory.

Before answering, use recall_memory whenever previous user-specific or
project-specific context, prior research, earlier experiments, project
state, past decisions, workflows, configurations, files or preferences
could materially affect the answer.

Always strongly consider recall_memory when the user refers to:
previous / last time / earlier / before / continue / resume /
之前 / 上次 / 以前 / 继续 / 还记得.

Do not guess historical user-specific facts if recall_memory can retrieve them.

When durable reusable information is produced, call capture_memory.

Use verify_memory when provenance, evidence or scientific reliability matters.
```

以下内容移出 Agent Skill：

- taxonomy 全表
- proposal 生命周期
- JSON schema
- evidence 数据结构细节
- overlap 实现
- status 全量解释
- metadata 字段说明

这些应成为 Gateway 内部规则。

---

# 14. Phase 8：建立 Agent 调用 Benchmark

V2 的成功标准不再是：

```text
Tool 能正常执行
```

而是：

```text
Agent 会不会自己调用 Tool
```

## 14.1 Recall Benchmark

至少建立 30 个测试问题。

### 明确历史型

```text
之前 Fe 的硝酸溶液怎么配的？
上次 HEPES 配多少 mM？
之前那个 PDF 下载问题后来怎么解决的？
```

### 继续型

```text
继续上次 Origin MCP 的工作。
我们接着之前的实验计划做。
```

### 隐式历史型

```text
这个还按之前的方法做吗？
用我们之前确认过的条件。
```

### 非历史型负样本

```text
什么是 FTS5？
解释一下 HEPES。
```

记录：

```text
Should Recall
Did Recall
Correct Memory
False Positive
Latency
Token Usage
```

---

## 14.2 Capture Benchmark

至少 30 个会话片段。

覆盖：

- 实验事实
- 研究决定
- 软件配置
- 工作流
- 普通聊天
- 临时错误
- 猜测
- 无价值信息

记录：

```text
Should Capture
Did Capture
Ambient / Trusted
Duplicate
False Capture
Proposal Correctness
```

---

# 15. V2 目标指标

## Recall

明显历史问题：

```text
Agent 主动调用率 ≥ 90%
```

Top-5 relevant memory：

```text
≥ 90%
```

无关问题错误 recall：

```text
< 10%
```

## Capture

明确持久信息：

```text
捕获率 ≥ 80%
```

普通聊天误保存：

```text
< 10%
```

## Context Budget

普通 recall：

```text
目标：500–1000 tokens
硬上限建议：1500 tokens
```

---

# 16. Phase 9：ChatGPT 接入验证

Agent Surface 完成后，用：

```text
Streamable HTTP MCP
```

作为主要远程接口。

目标接入：

```text
ChatGPT Custom App / Workspace Agent
```

测试重点：

- Tool 是否被识别
- Tool description 是否触发正确
- 历史问题是否主动 recall
- 科研来源质疑时是否 verify
- 产生稳定信息后是否 capture

测试时不要只用：

```text
请调用 recall_memory……
```

这种显式提示。

必须使用自然用户语言。

---

# 17. Phase 10：Codex / Cherry / Kilo 跨客户端验证

最低测试矩阵：

| Client | Recall | Capture | Verify |
|---|---:|---:|---:|
| ChatGPT | 必测 | 必测 | 必测 |
| Codex | 必测 | 必测 | 必测 |
| Cherry Studio | 必测 | 必测 | 建议 |
| KiloCode | 必测 | 必测 | 建议 |

不同客户端可以使用不同的极短 Skill，但 Gateway API 保持一致。

---

# 18. 建议代码组织

第一阶段不要同时大规模搬文件。

优先通过 adapter 新增：

```text
src/research_memory_gateway/
    agent_surface/
        recall.py
        capture.py
        verify.py
        tools.py
```

复用现有：

```text
models.py
service.py
backends.py
retrieval.py
policy.py
taxonomy.py
source_refs.py
```

V2 稳定后再考虑整理成：

```text
src/research_memory_gateway/

    core/
        models.py
        service.py
        policy.py
        taxonomy.py

    storage/
        sqlite.py
        retrieval.py
        embeddings.py

    agent/
        recall.py
        capture.py
        verify.py
        tools.py

    admin/
        tools.py
        audit.py
        export.py

    extraction/
        classifier.py
        claims.py
        evidence.py
        entities.py

    webui/
```

不要在 V2.0 同时进行：

```text
功能重构 + 大规模目录重构
```

否则回归问题难以定位。

---

# 19. EverOS 的处理方式

V2.0 阶段：

> **暂时不将 EverOS 加入主依赖。**

当前首要问题不是 memory engine 不够复杂，而是：

> **Agent 根本没有稳定使用 memory。**

先完成：

```text
recall
capture
verify
Agent Benchmark
```

之后再评估 EverOS 是否负责：

- conversation ingestion
- Episode
- Reflection
- Profile
- Agent Case
- Agent Skill
- automatic consolidation

可能的未来 V3：

```text
                   Memory Gateway
                        │
            ┌───────────┴───────────┐
            │                       │
         EverOS              Research Core
            │                       │
      Ambient Memory         Trusted Memory
```

---

# 20. 开发阶段安排

## V2.0 — Agent Surface

P0：

1. V1 legacy 分支冻结。
2. 新 `main` 建立。
3. Baseline regression tests。
4. `recall_memory`
5. Tool description
6. Recall benchmark
7. `verify_memory`
8. `capture_memory`

V2.0 的成功标准：

> Agent 开始主动调用 Memory。

## V2.1 — Memory Workflow

1. Ambient / Trusted 分层。
2. Proposal Queue。
3. Agent/Admin Surface 分离。
4. Skill 缩短。
5. WebUI Proposal 批量审核。

## V2.2 — Client Integration

1. ChatGPT
2. Codex
3. Cherry Studio
4. KiloCode
5. 跨客户端 invocation benchmark

## V2.3 — Automatic Ingestion

研究：

- conversation lifecycle hook
- Memory Bridge
- background extraction
- session flush
- automatic project state

## V3.0 — EverOS / Reflection Evaluation

仅在 V2 调用问题解决之后考虑：

- EverOS Adapter
- Episode
- Reflection
- Profile
- Agent cases / skills
- Markdown-first source-of-truth
- automatic consolidation

---

# 21. 明确暂缓事项

以下工作在 V2 P0 阶段暂停：

- Nocturne 深度集成
- PostgreSQL
- 新向量数据库
- 复杂知识图谱
- 更多 taxonomy 类型
- 大规模 WebUI 美化
- 多用户 SaaS
- EverOS 完整迁移
- 高级自动推理
- 无关重构

判断原则：

> 如果一个功能不能直接提高 Memory 的调用率、召回质量或自动捕获能力，则不属于 V2.0 P0。

---

# 22. V2 核心设计原则

## 原则 1：Agent 看到的工具越少越好

普通 Agent 默认：

```text
recall_memory
capture_memory
verify_memory
```

## 原则 2：Recall 必须便宜

第一次 recall 只返回 compact memory context。

完整 evidence 等到 verify 再取。

## 原则 3：数据库结构不能泄漏给 Agent

Agent 不应该被迫理解：

```text
memory_type
claim schema
evidence schema
proposal status
verification lifecycle
```

## 原则 4：科研事实继续 evidence-first

V2 自动化不能牺牲：

```text
evidence_backed
inferred
unverified
conflicting
superseded
retracted
```

的区分。

## 原则 5：自动化“采集”，不自动化“科研事实认证”

自动 capture：

```text
可以
```

自动把推测升级为 evidence-backed：

```text
禁止
```

## 原则 6：调用率是一级质量指标

以后 CI / Benchmark 不仅测试：

```text
tool works
```

还要测试：

```text
agent chooses tool
```

---

# 23. 最终目标体验

## 场景 A：历史召回

用户：

```text
之前 Fe 的硝酸溶液怎么配的？
```

期望：

```text
Agent
 ↓
recall_memory
 ↓
命中 Fe3+ 配液记忆
 ↓
直接回答
```

不应该依赖 Agent 自己“模糊记得”。

## 场景 B：自动捕获

用户：

```text
这次 DMSO 换成了新买的 HPLC 级，之前的是化学纯。
```

期望：

```text
Agent 正常回答
 ↓
capture_memory
 ↓
Gateway 判断为长期实验上下文
 ↓
Ambient Memory 或 Trusted Proposal
```

不要求用户主动说：

```text
帮我记住。
```

## 场景 C：证据验证

用户：

```text
你确定之前 Fe 是放在 0.1 M HNO3 里面吗？
```

期望：

```text
Agent
 ↓
recall_memory
 ↓
verify_memory
 ↓
Claim + Evidence + SourceRef
 ↓
可追溯回答
```

---

# 24. 最终验收定义

Research Memory Gateway V2 不是以“增加多少功能”为完成标准。

真正完成标准：

> **即使用户从不主动说“记住这个”，Agent 仍然会在需要历史时主动调用 recall，在产生长期价值时主动 capture，并在科研可靠性重要时调用 verify。**

如果仍然必须依赖用户写：

```text
调用 memory MCP 查一下
```

那么 V2 就不算成功。

---

# 25. 执行顺序总结

```text
当前 main
   ↓
Tag: v1-pre-agent-memory-refactor
   ↓
重命名
legacy/research-memory-gateway-v1
   ↓
重新创建 main
   ↓
Baseline Test
   ↓
recall_memory
   ↓
Recall Tool Description
   ↓
Agent Recall Benchmark
   ↓
verify_memory
   ↓
capture_memory
   ↓
Ambient / Trusted Memory
   ↓
Proposal Queue
   ↓
Agent/Admin Surface Split
   ↓
Skill Simplification
   ↓
ChatGPT / Codex Integration
   ↓
Cross-client Benchmark
   ↓
Automatic Ingestion
   ↓
EverOS Evaluation
```

---

## 最终分支定义

### `legacy/research-memory-gateway-v1`

代表：

> 当前已经实现的、以 Evidence-first + 手动 Agent MCP workflow 为核心的稳定 V1。

长期冻结，只做必要维护。

### `main`

代表：

> Research Memory Gateway V2 及以后唯一主要开发线。

V2 的第一优先级不是增加记忆功能，而是彻底解决：

> **Agent 不主动调用 Memory。**
