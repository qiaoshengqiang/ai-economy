# ADR-0024：Human Demand ≠ Task；Human ≠ Task Creator

- **状态**：Accepted（架构纠偏；**实现为 `FUTURE`**）
- **时间**：Task 起源模型修正与架构同步
- **修正**：**ADR-0023 §五** 中「Root Task 通常来自 Human」的表述被本 ADR 精确化
- **规范**：[`TASK_ORIGIN_LINEAGE.md`](../TASK_ORIGIN_LINEAGE.md) §2.4–§2.8

> 本 ADR **不修改** ADR-0020 / ADR-0021 / ADR-0022 的历史决策内容。
> 它**精确化** ADR-0023 里一处不够严格的表述，并保留演进痕迹。

## Context

ADR-0023 定义了 Task Origin / Lineage 的七个逻辑角色，其中 §五 写着：

```
第一版的现实默认：Human → Root Task
```

这句话**看起来**只是描述默认情况，但它隐含了一个**错误的架构模型**：

> **把 Human 当成了 Root Task 的直接 Creator。**

这个模型有三个问题：

1. **职责边界错** —— Human 提供的是**需求**，不是**可执行的工作单元**。
   Task 是 OS 内部的、可协作、可跟踪、**可产生经济责任**的对象。
2. **Creator 归属错** —— 真实链路里，是 **Agent** 理解需求、做结构化与判断、
   然后**正式创建** Task。Human 没有"创建 Task"这个动作。
3. **与 AGENT_INITIATIVE 冲突** —— 如果 Root Task 天然来自 Human，
   那么「Agent 自主发起 Root Task」就变成了一个例外；实际上它是**并列的正常来源**。

这个模型**已经写进了 3 个文件**（TASK_ORIGIN_LINEAGE.md 的 7 处图示与表格、
TASK_WORKSPACE.md 一处、ADR-0022 一处）。

## Decision

### 一、正式确立正确链路

```
Human Demand
      ↓
Agent Interpretation / Structuring / Decision
      ↓
Agent creates Root Task
      ↓
Task Execution / Collaboration
      ↓
Result
      ↓
Human receives result / provides feedback
```

### 二、Human Demand ≠ Task

Human 提供：需求 · 目标 · 约束 · 偏好 · 验收标准 · 反馈 · 治理指令 ——
这些是**外部输入（Demand 层）**。

Task 是 OS **内部**的可执行工作单元。

```
❌ Human Demand = Task
❌ Human = Task Creator
```

### 三、引入 **Demand Source**，与 Creator 分离

| | 回答 |
|---|---|
| **Demand Source** | 需求**从哪来**？（Human，或 Agent 自己） |
| **Creator** | 谁把它**正式创建成 Task**？（**始终是 Agent**） |

```
Demand Source = Human
Creator       = Agent
```

**两者不能合并。**

### 四、Root Task 有两种并列来源，Creator 都是 Agent

```
A. HUMAN_DEMAND
   Human → Demand → Agent（创建）→ Root Task

B. AGENT_INITIATIVE
   Agent → 发现机会 → 判断收益/成本/风险 → Root Task
```

> **Root Task 不应该天然绑定 Human。**

### 五、Human 的四个角色

| # | 角色 |
|---|---|
| 1 | **Demand Provider**（主要入口） |
| 2 | **Feedback Provider** |
| 3 | **Optional Collaborator**（参与者身份，非 Creator 默认身份） |
| 4 | **Governance Authority**（Force Unlock 等，属治理层） |

**两条硬约束**：

- **Feedback ≠ 直接修改 Task Core** —— 由 Agent 决定 Continue / Reopen /
  Iteration / Derived Task / 调整方案 / 结束
- **Governance ≠ 普通 Task Creation / Execution**

### 六、Human 不应该成为 Task Manager

Human 不负责持续拆解、调度、管理 Agent 的 Task Tree。

| Human 提供 | Agent 负责 |
|---|---|
| Demand · Constraints · Feedback · Governance | Interpretation · Task Creation · Decomposition · Collaboration · Negotiation · Execution · Iteration |

> ❌ 不引入 **Human Task Manager** · **Human Task Coordinator** ·
> **Human Central Scheduler** · **Human-driven Task Tree Management**。

**理由**：系统目标本身就是 **Agent 能够独立组织复杂工作**。

### 七、OS 限制结构，Agent 决定策略

| OS 限制结构 | Agent 决定策略 |
|---|---|
| Task 生命周期 · lineage 结构 · 最大深度 | 是否创建 Child Task · 是否外包 · 给谁 |
| 权限 · Workspace 隔离 · Write Lock | 是否自己完成 · 是否继续迭代 |
| Task Agreement · 数据一致性 · 持久化 | 是否投资能力 · 如何分配预算 · 如何组织团队 |

> 不要因为本次修正而重新增加大量**中心化决策逻辑**。

### 八、保持不变的部分

- **Child Task 模型不变**（独立 Task · 自己的 Core/Agreement/Context/Workspace/
  Execution/Verification/Economic Record · 经 Parent/Root 关联 ·
  父 Task 不自动获得 Child Workspace 全部访问权 ·
  Child Result 经 result/artifact reference 返回）
- **Task Creation ≠ Message**
- **Iteration ≠ Child Task**（Human Feedback 可**促成** Iteration，
  但 **Human Feedback ≠ Human 直接创建 Child Task**）
- **最大三层 = `CURRENT` / TEMPORARY**
- **八个概念保持独立**（Demand Source · Origin · Creator · Parent · Root ·
  Funding Source · Responsible · Participants），特别 **Creator ≠ Responsible**

### 九、本轮**不实现**

- ❌ Demand DB table / Service / API / lifecycle / persistence
  （只建立概念，标 **`OPEN DESIGN`**）
- ❌ 新增 Task 字段或 schema
- ❌ Task lineage service / tree manager / graph engine / Origin service

## Consequences

**正面**：

- 职责边界清楚了：**Human 提需求，Agent 建 Task**
- `AGENT_INITIATIVE` 从「例外」变成**并列的正常来源**
- Human 的四个角色分开后，`Feedback` 不会被误当成「改 Task」
- **阻止了 Human 变成中心调度者**这一退化方向
- 保留了演进历史（ADR-0023 的表述被标注为「已精确化」，不是被删掉）

**负面 / 代价**：

- **Demand 层是一个新概念，但本轮不实现** ——
  未来需要一个地方承载「需求」，否则 `Demand Source` 无处落地
- 文档里「Human」将长期**同时**出现在两个位置（Demand 层 / 参与者层），
  读者必须自己分清 —— 这是引入这个概念的必要代价
- `Creator` 在**所有**情况下都是 Agent，这意味着
  **Human 无法直接建 Task** —— 如果将来需要「Human 直接建 Task」，
  那是**另一个决策**，不是本 ADR 的延伸

**因此不能做的事**：

- ❌ 不许写 `Human → Root Task`
- ❌ 不许写 `Human-created Task` / `Human Task Creator`
- ❌ 不许把 `Demand Source` 与 `Creator` 合并
- ❌ 不许把 `Feedback` 当成直接修改 Task Core
- ❌ 不许把 `Governance` 当成普通 Task 操作
- ❌ 不许引入 Human Task Manager / Coordinator / Central Scheduler
- ❌ 不许因为本次修正而增加中心化决策逻辑

## Guard

新增结构化文档守卫 **Guard A–F**：

| Guard | 内容 |
|---|---|
| **A** | 架构正文不允许把 **Human = Task Creator** 作为正式模型 |
| **B** | 必须**允许**（不误杀）：Human provides Demand / creates Demand / provides Feedback / participates / Force Unlock |
| **C** | 必须允许 `Human Demand → Agent → Root Task` |
| **D** | 必须允许 `Agent Initiative → Root Task` |
| **E** | 必须理解**否定语义与历史说明**（「Human is NOT the Task Creator」不能被误判） |
| **F** | **反向测试**：把旧模型写回文档 → Guard 必须失败 |

**不使用 substring-only 检查。**
并避免已积累的缺陷：否定盲 · 多处关键词 · 章节错位 ·
空集合 continue · 代码块内 `#` 被当标题。

## Future evolution

```
今天（本 ADR）：
    Human Demand ≠ Task · Human ≠ Task Creator（SETTLED）
    Demand Source 概念已定（未实现）
    Human 四角色已区分
    Human ≠ Task Manager（SETTLED）
    Demand 实体 / 字段 / service = OPEN DESIGN · FUTURE

下一步：Task Origin & Task Lineage 正式架构设计
```
