# Docker Host Resource Hygiene SOP (P0)

Research Memory Gateway 跨 Host Docker 资源生命周期管理规范与通用自动化执行标准。

---

## 1. 适用范围与双 Host 职责划分

本规范严格适用于所有涉及 Docker 操作的自动化 Agent、CI/CD Pipeline 及运维人员。

| Host | 角色与定位 | 允许操作 | 严格禁止 |
|---|---|---|---|
| **Unraid 主机** (`192.168.22.102`) | **Production / Canary Runtime Host**<br>承载正式生产服务与金丝雀验证 | `docker pull`<br>`docker run`<br>`docker stop`/`start`<br>Canary 验证<br>Health check<br>Production cutover<br>Rollback | 默认**严禁**本地构建：<br>❌ `docker build`<br>❌ `docker compose build`<br>❌ `docker buildx build --load`<br>❌ 反复本地构建 candidate<br>*(除非用户非常明确的单次授权)* |
| **`gau-unraid` VM** (`192.168.22.202`) | **Build / Test / Rehearsal Host**<br>隔离的镜像编译、自动化测试与演练机 | `docker build`<br>`docker buildx build`<br>`docker compose build`<br>运行临时测试容器<br>集成演练与冒烟测试 | ❌ 永久保留构建垃圾<br>❌ 无限制保留旧 candidate<br>❌ 保留失败/已废弃的测试镜像与容器 |

> [!IMPORTANT]
> **“不是生产机”不等于允许积累垃圾。** `gau-unraid` VM 虽为构建机，任务结束时仍必须严格执行资源清理。

---

## 2. 资源所有权与通用生命周期标签 (Universal Labeling Policy)

任何 Agent 在任意 Docker Host 创建资源前，必须定义清晰的 Task Manifest，并在创建资源时携带通用标签：

### 2.1 强制标准通用 Label 规范

所有支持 Label 的资源（Images、Containers、Volumes、Networks）必须携带以下通用规范标签：

```text
io.agent.managed=true
io.agent.task=<task-id>
io.agent.project=<project-name>
io.agent.created-by=<agent|ci|human>
io.agent.lifecycle=temporary|candidate|production|rollback
```

项目可按需附加项目级标签（例如 `io.rmg.release=0.2.7`），但**全局清理与门禁判定引擎严禁硬编码或仅依赖特定项目名称**。

### 2.2 Task Manifest 规范

无法直接打 Label 的资源（例如中间构建层、匿名卷、构建缓存）必须由创建该任务的 Agent 自动写入 Task Manifest。

Manifest 存储路径：
```text
.local/docker-hygiene/<task-id>/manifest.json
.local/docker-hygiene/<task-id>/preflight.json
.local/docker-hygiene/<task-id>/postflight.json
```

Manifest Schema 至少包含：
- `task_id`: 任务全局唯一标识
- `project`: 项目标识（如 `research-memory-gateway`）
- `host`: 目标主机 (`unraid` 或 `gau-unraid`)
- `start_time`: 任务启动时间 (ISO 8601)
- `created_containers`: 本任务创建的容器 ID/Name 列表
- `created_images`: 本任务创建的镜像 ID/Tag 列表
- `created_volumes`: 本任务创建的卷 ID/Name 列表
- `created_networks`: 本任务创建的网络 ID/Name 列表
- `created_builders`: 本任务创建的 buildx builder 实例列表
- `retained_resources`: 策略允许保留的资源清单（含保留原因与 lifecycle）
- `cleanup_result`: 清理阶段执行日志与删除列表

> [!WARNING]
> Manifest 严禁记录任何敏感信息（如 API Key、Token、密码）。

---

## 3. 操作前后强制 Inventory 审计机制

所有 Docker 任务执行前后，必须执行完整的全量清单采集：

### 3.1 清单采集范围

必须覆盖以下全量对象，并记录依赖关系：
1. **Containers**：包含所有运行与停止容器、容器 Labels、Image 引用、挂载点及网络绑定。
2. **Images & Dangling**：包含所有 Tagged Image、Dangling Image (`<none>:<none>`)、Labels、Parent/Child 依赖树。
3. **Volumes**：所有 Local 卷及其 Labels。
4. **Networks**：所有 Network 及其 Labels、Driver。
5. **Buildx Builders**：所有活动构建器实例。
6. **BuildKit Cache**：构建缓存大小、可回收空间及最后访问时间。
7. **System DF**：整体磁盘占用基准。

### 3.2 审计报告格式

任务结束时必须比对 `preflight.json` 与 `postflight.json`，并在最终报告中明确列出：
1. **Pre-existing and untouched**：先前存在且未触碰的第三方项目资源。
2. **Created by this task**：本任务创建的所有资源列表。
3. **Retained intentionally**：本任务依照策略保留的资源（如新 Production、Rollback）。
4. **Removed by cleanup**：清理阶段成功删除的临时资源。
5. **Residual task-owned resources**：残留的本任务资源（Release 门禁要求必须为 0）。
6. **Disk usage before & after**：执行前后磁盘使用量与净释放空间（Reclaimed space）。

---

## 4. 镜像与资源保留策略 (Retention Policy)

### 4.1 `gau-unraid` VM 构建机保留策略

`gau-unraid` 在任务结束前，默认**最多只允许保留**：

```text
1 个当前仍在验证或最新交付的 candidate
+
必要时 1 个最近可复现的 build artifact
```

必须立即清理的资源：
- 失败的构建产物 (`failed build image`)
- 已被新 RC 替代的旧候选镜像 (`superseded RC / old candidate`)
- 临时标签 (`temporary tags`)
- 本任务产生的 Dangling 镜像 (`<none>:<none>`)
- 测试专用容器及测试镜像 (`test-only container / image`)

### 4.2 `gau-unraid` 受控共享构建缓存 (Approved Shared Cache)

不要把 `gau-unraid Build Cache = 0 B` 当作盲目目标。合理的共享构建缓存有助于大幅提升后续构建速度。

允许保留 **Approved Shared Cache**，但必须受控：
1. **与 Builder 关联**：必须属于指定的常规 builder 实例。
2. **容量上限**：
   - **Soft Limit**: 15 GB（超过时发出预警并建议淘汰）
   - **Hard Limit**: 25 GB（超过时必须执行 LRU 清理）
3. **淘汰机制**：超限时优先淘汰最旧、未使用、被新层替代的历史缓存。
4. **门禁统计**：受控共享缓存独立统计为 `approved_shared_cache`，不计入 `task_owned_build_cache`（任务遗弃垃圾）。

### 4.3 Unraid 生产宿主机保留策略

Unraid 主机长期**严格只保留**：

```text
Current production (当前正式生产镜像)
+
Last-known-good rollback (上一版本稳定回滚镜像)
```

Canary 完成后：
- 失败的 Candidate 立即删除；
- 已被新 Candidate 替代的旧镜像立即删除；
- Production Cutover 完成后失去用途的 RC 镜像立即删除；
- 严禁长期残留 `rc1`, `rc2`, `dev`, `test`, `tmp`, `old`, `backup` 等临时镜像或容器。

---

## 5. 安全清理语义 (Safe Cleanup Semantics)

为防止意外误删，所有清理命令必须遵循严格的安全防护：

1. **默认 Dry-Run**：
   ```bash
   python scripts/docker_hygiene.py cleanup --task-id <task-id>
   ```
   默认仅输出拟删除资源的 Deletion Plan，**绝不执行实际删除**。
2. **显式 `--apply` 授权**：
   只有显式附加 `--apply` 标志时，才真正执行物理删除：
   ```bash
   python scripts/docker_hygiene.py cleanup --task-id <task-id> --apply
   ```
3. **Unraid 生产机强安全护栏**：
   - `task_id` 必须明确存在；
   - 归属（Ownership）必须 100% 确定为本任务所属；
   - `production` 与 `rollback` 镜像及容器自动受底线防护保护，坚决拒绝删除；
   - 归属不明（`UNKNOWN`）资源一律拒绝自动删除；
   - 严禁通配符（Wildcard）删除；
   - 严禁任何形式的全局 Prune 命令。

---

## 6. 构建与任务失败强制 Fail-Closed Cleanup

资源清理不是“全部测试通过后的可选步骤”，而是任务流水线的核心环节：

```text
Prepare -> Build / Deploy -> Test / Verify -> [Cleanup Phase (Always Run)]
```

无论发生何种异常：
- `docker build` 失败
- `pytest` 测试失败
- Canary Health check 超时
- Agent 异常中断
- 部署主动 Rollback

**都必须无条件进入 Cleanup Phase 清理临时容器与镜像。任务失败绝不得成为留下 Docker 垃圾的借口。**

---

## 7. Release-ready 硬门禁清单 (Hard Gate)

任何 Agent 或部署流程在声称任务 `complete`、`release-ready` 或 `production-ready` 之前，必须调用：

```bash
python scripts/docker_hygiene.py gate --task-id <task-id> --host all
```

门禁引擎必须对以下 8 大维度进行物理审计并输出具体明细：

```text
[ ] temporary_containers: 0
[ ] superseded_candidate_images: 0
[ ] task_owned_dangling_images: 0
[ ] disposable_builders: 0
[ ] task_owned_build_cache: 0
[ ] temporary_networks: 0
[ ] temporary_volumes: 0
[ ] unknown_resources: 0
```

只有当：
1. **所有任务临时资源数量均为 0**；
2. **阻碍门禁判定的未知归属资源 (`unknown_resources`) 为 0**；

门禁才允许评定为 `PASS`。若存在任何项非零，任务不得宣布完成。
