# ADR-0022：Lazy Provisioning、Workspace Lifecycle 与 Human Participation

- **状态**：Accepted（架构决策；**实现为 `FUTURE`**）
- **时间**：Lifecycle / Human Participation / Task Iteration
- **相关**：ADR-0020（共享资源模型）· ADR-0021（显式持有 + Human Force Unlock）
- **规范**：[`TASK_WORKSPACE.md`](../TASK_WORKSPACE.md) §14.5 · §15

> 本 ADR **不修改** ADR-0020 / ADR-0021 的历史决策内容。

## Context

ADR-0020 确立了 Task Workspace 的共享资源模型，ADR-0021 确立了
Write Lock 的显式持有与 Human Force Unlock。

但留下三类**尚未正式同步进文档**的结论：

1. **Workspace 什么时候物理存在？** —— 之前只把
   「creation timing」列为 Open，没有决定方向。
2. **Task 结束之后 Workspace 怎么办？** —— 没有说。
3. **Human 在 Workspace 里是什么位置？** ——
   ADR-0021 引入了「Authorized Human Operator」作为治理主体，
   但没有说 Human 能不能**正常修改资源**。

第 3 点尤其重要：如果不明确，很容易默认成
「Human 是系统外的管理员，只负责 Force Unlock」——
那会与「第一版面向熟悉 Agent 的专业用户」这个前提不符。

## Decision

### 一、Logical Workspace 与 Physical Workspace 分离，采用 Lazy Provisioning

```
Task
  ↓
workspace_id = WS-001        ← Logical Identity，可以立刻存在
```

**这不代表物理目录 / Backend 已经创建。**

```
初始：  Workspace = UNPROVISIONED

第一次真正需要资源时：
        UNPROVISIONED → PROVISIONING → READY
```

### 二、为什么是 Lazy

**Task 是通用 Task，不是所有 Task 都需要 Workspace。**

查询 · 比较 · 简单计算 · 翻译 · 普通问答 —— 可能完全不需要真实资源。

> ❌ **不要**因为 Task 被创建 / `ACCEPTED` 就无条件创建物理 Workspace。
>
> ✅ Logical Workspace Identity 可以存在，Physical Workspace 不必提前存在。

### 三、Workspace Lifecycle 模型

```
UNPROVISIONED → PROVISIONING → READY → FROZEN → ARCHIVED
```

| 状态 | 语义 |
|---|---|
| `UNPROVISIONED` | 逻辑已存在，实际 Backend / Resource Space 尚未 provision |
| `PROVISIONING` | 正在建立实际 Workspace Backend / Resource Space |
| `READY` | 可按 Permission + Write Lock 正常使用 |
| `FROZEN` | **保留、可读，但不允许普通写入** |
| `ARCHIVED` | 进入长期历史 / 保存状态，不再作为正常活跃 Task Resource 使用 |

**Workspace Lifecycle 只描述「这个资源空间当前是否可以被正常操作」。**

### 四、Workspace Lifecycle ≠ Task Lifecycle

```
✅ Task: IN_PROGRESS   + Workspace: READY
✅ Task: COMPLETED     + Workspace: FROZEN
```

**禁止**在 Workspace 里复制 Task 的
`CREATED / OFFERED / NEGOTIATING / ACCEPTED / IN_PROGRESS /
SUBMITTED / VERIFIED / COMPLETED`。

### 五、Task 结束后 → FROZEN，不删除

| Task | Workspace |
|---|---|
| `COMPLETED` | → `FROZEN` |
| `FAILED` | → `FROZEN` |
| `CANCELLED` | → `FROZEN` |

保留 `files / artifacts / history / evidence`，
供**复盘 · 审计 · 经验学习 · Artifact 查询**。

**第一版不建立普通 Task Workspace Delete Lifecycle。**
未来如需 storage cleanup / retention policy / physical deletion，**单独设计**。

> ❌ 不提前实现 **GC / TTL / 自动物理删除**。

### 六、允许重新打开进行迭代（Reopen / Iteration）

> **Task 完成不是 Task 永久死亡。**
> **`FROZEN` ≠ 永久只读、永远不能恢复。**

```
Iteration 1 → COMPLETED → Reopen → Iteration 2 → 继续执行
```

**Iteration 必须保留历史**：不得清空原历史 / 覆盖旧执行 / 覆盖旧 Verification。

```
Task-001
    Iteration 1   execution history · submissions · verification · completion
    Iteration 2   new execution · new submissions · new verification
```

这样未来 Agent 才能知道：上一次为什么失败 · 为什么再次打开 ·
**人类修改了什么要求** · 之前用了什么方案。

> 这也是未来 **Agent Experience / Learning** 的潜在数据来源。

**但 Reopen 的 6 个细节仍然 `OPEN`**（谁可 reopen / 何时新建 Task /
是否新 Iteration ID / 是否恢复原 Workspace / 旧 Lock 是否失效 /
Permission 是否重评）—— 见 TASK_WORKSPACE.md §15.9。

### 七、Iteration ≠ Child Task

| | 含义 |
|---|---|
| **Iteration** | **仍是 same Task**，同一任务不断改进 |
| **Child Task** | 从 Parent Task **独立派生**的局部任务 |

**本次不实现 Child Task。**
Task Origin / Task Lineage / Parent-Child Task 是**下一轮的独立主题**。

### 八、Human 是正式协作 Actor

第一版初始用户是**熟悉 AI Agent 的专业用户**。

Workspace 应支持 **Human 作为正式协作参与者**：

```
Human → Workspace → Permission → Write Lock → Resource Modification
```

这属于 **`Authorized Human Modification`**。

### 九、Authorized vs Unauthorized Modification

> ⚠️ **不要使用法律意义上的「合法 / 违法」作为系统内部术语。**

| 术语 | 含义 |
|---|---|
| **`Authorized Modification`** | Human Operator 拥有相应 Permission，**通过 Workspace 正式机制**修改 |
| **`Unauthorized / Out-of-band Modification`** | Finder / Terminal / 其他程序**直接修改物理文件**，未经 Workspace Protocol |

### 十、第一版不阻止、不监控 Unauthorized Physical Modification

> **第一版不试图建立宿主机级别的绝对文件系统防篡改。**

Workspace Permission / Write Lock 约束的是**通过 Workspace Protocol 执行的操作**，
**不是**「宿主 OS 超级用户也绝对无法绕过」的物理控制。

**本阶段不做**：

❌ Filesystem Watcher · ❌ Hash Tamper Detection ·
❌ Kernel-level Enforcement · ❌ Sandbox 强制隔离 ·
❌ 试图阻止管理员直接改文件

理由：初期用户是专业人士；当前优先验证 **Agent / Human 正常协作路径**；
绕过系统的异常情况**未来再处理**。

→ `Unauthorized Physical Modification` 是 **`Future Concern`**。

### 十一、Workspace Protocol ≠ Host OS Filesystem Permission

```
Workspace Permission / Write Lock  =  Workspace Protocol-level authorization
Host OS Filesystem Permission      =  Infrastructure / OS-level control
```

**因此 Write Lock 表达的是「在 Workspace Protocol 中谁拥有排他写入权」，**
**不是「谁在整个 MacBook 上物理上绝对不能碰这个文件」。**

### 十二、Human 也遵守 Permission + Write Lock

`Permission ≠ Write Lock` **不变**：

```
Human A: WRITE Permission ✅ 但没有 Lock ❌
    → 不能通过 Workspace Protocol 写
```

高权限治理者的 `FORCE_UNLOCK` 也必须是**独立的 Governance Capability**，
不由 `WRITE Permission` 推导。

### 十三、Task Origin / Task Lineage = OPEN DESIGN TOPIC

Root Task 原则上从 **Human Demand / Human-created Task** 开始；
未来 Agent 执行中可能需要 Create Child Task。

未来需正式定义：`Task Origin · Task Creator · Parent Task · Root Task ·
Task Lineage · Iteration` 及 Parent-Child 关系。

> ⚠️ **本次绝对不要自行决定 Task Lineage。**
>
> ❌ 不实现 `parent_task_id` · `root_task_id` · `child task` ·
> `task tree` · `task graph` · `delegation lifecycle`。

### 十四、Message ≠ Task Creation

```
Agent → 「帮我看看数据库」    → 只是 Message
Agent → 正式创建 Child Task → Agreement → Agent B 接受 → 才是 Task Creation
```

本次只记录：未来需区分 **Message · Task Proposal · 正式 Child Task**。

## Consequences

**正面**：

- 不需要 Workspace 的 Task（查询/翻译/问答）**零 provisioning 成本**
- Task 结束后资源保留 → 复盘 / 审计 / 学习数据不丢
- 允许 Reopen 迭代 → Task 不再是「一次性的」
- Human 被明确为**一等 Actor**，而不是系统外的神祕管理员
- 「协议层授权」与「OS 层权限」分层 → 不会承诺做不到的事
- 生命周期模型定下 → 实现阶段不必再猜

**负面 / 代价**：

- Lazy Provisioning 引入一个 `UNPROVISIONED` 状态，
  调用方必须处理「第一次访问会触发 provisioning」这件事
- **不删除**意味着存储会持续增长（明确接受的代价）
- **不监控 out-of-band 修改**意味着：如果有人在 Finder 里改了文件，
  系统**不知道**（明确接受的边界）
- Reopen 的细节未定 → 实现前仍需一轮讨论

**因此不能做的事**：

- ❌ 不许因为 Task `ACCEPTED` 就无条件创建物理 Workspace
- ❌ 不许在 Workspace 里复制 Task 生命周期
- ❌ 不许因为 Task 结束就删除 Workspace
- ❌ 不许把 `FROZEN` 设计成永久只读、不可恢复
- ❌ 不许在 Reopen 时清空或覆盖旧历史
- ❌ 不许实现 GC / TTL / 自动物理删除
- ❌ 不许禁止 Human 参与 Workspace
- ❌ 不许用「合法/违法」作系统术语
- ❌ 不许把 `Unauthorized Physical Modification` 监控写成 `CURRENT`
- ❌ 不许把 Write Lock 说成宿主机级别的物理不可触碰
- ❌ 不许自行实现 Task Lineage / `parent_task_id` 等

## Future evolution

```
今天（本文档，`SETTLED` 模型 / `FUTURE` 实现）：
    Logical Workspace + Lazy Provisioning
    UNPROVISIONED → PROVISIONING → READY → FROZEN → ARCHIVED
    Authorized Human Modification（一等协作）
    Unauthorized Physical Modification = 不阻止、不监控

下一轮独立主题：
    OPEN DESIGN TOPIC: Task Origin & Task Lineage

更远：
    Out-of-band Detection / Resource Fingerprint / Mutation Audit
    Guardian Agent（ADR-0021）
```

## 仍然 OPEN 的问题

- Reopen 的 6 个细节（§15.9）
- 何时从 `FROZEN` → `ARCHIVED`（Archive policy）
- Task Origin / Task Lineage（下一轮专门讨论）
- 多资源锁 / 目录锁 / Agent 加入退出 / Artifact 设计
- Permission 集合与授予规则
- 逻辑资源 id 的确切形式

## Guard

文档一致性守卫（含反向测试）必须能抓：

| 违规 | |
|---|---|
| 把 Lazy Provisioning 写成「Accepted 必定立即创建物理 Workspace」 | ✅ |
| 把 Workspace Lifecycle 写成 Task Lifecycle | ✅ |
| 把 `FROZEN` 写成永久不可 reopen | ✅ |
| 把 Human Participation 写成禁止 | ✅ |
| 把 Unauthorized Monitoring 写成 `CURRENT` | ✅ |
| 把 Workspace 标为 `CURRENT` | ✅ |
| 把 Task Lineage 写成已经实现 | ✅ |

并遵守已积累的 Guard 工程经验（ADR-0018）：
避免否定句误报 · 多处关键词导致错误通过 · 空集合 continue ·
substring false positive。
