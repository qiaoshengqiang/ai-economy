# ADR-0038：Agent / World / Asset / Capability 的核心边界

> **状态**：Accepted
> **日期**：—
> **来源**：[`AGENT_CORE_MODEL_V1.md`](../AGENT_CORE_MODEL_V1.md) 中所有标 `CONFIRMED` 的条目
> **本 ADR 冻结的是**：**Architecture Invariants**（架构不变量）
> **本 ADR 冻结的不是**：任何具体实现方式

---

## Context

项目在完成 Agent Identity / Agent-specific Credential / Private Workspace V1 /
Task Workspace / Workspace Core / Communication / Event Wakeup / WorktreeManager /
真实三 Agent 协作 之后，上层架构认知一直没有进入正式 ADR。

后果有两个，而且都是真实的：

1. **概念每次都要重新发明。** 讨论 Autonomous Agent / Tool / Browser / Game /
   Multi-World / 生产资料 时，没有一个共同的出发点。
2. **边界只写在设计文档里，不写在 ADR 里。** 而 ADR 才是
   「以后任何开发 Agent **必须遵守**」的那一层。设计文档可以被当作建议，
   ADR 不行。

[`AGENT_CORE_MODEL_V1.md`](../AGENT_CORE_MODEL_V1.md) 已经把认知整理出来了，
但它里面混着 `CONFIRMED` / `PROVISIONAL` / `OPEN` 三种状态。
**ADR 的职责是把其中 `CONFIRMED` 的那部分单独冻起来** ——
让「已经确定的」和「还在讨论的」在制度上分开。

> ⚠️ **术语澄清**：本 ADR 说的是**项目**的架构边界。
> 它与 [`AGENT_CORE_MODEL_V1.md`](../AGENT_CORE_MODEL_V1.md) §14 里
> 讨论的「**Agent 自己的** Constitution」（Agent 的长期行为原则）是**两件事**，
> 不要混淆。

---

## Decision

### 一、冻结 10 条 Architecture Invariants

以下 10 条是**架构不变量**，不是实现方式。它们描述的是
**概念之间不能相等**，因此不受具体实现选型影响。

```
1.  Agent Identity  ≠  Runtime
2.  Agent Identity  ≠  LLM
3.  Agent Identity  ≠  Provider Account
4.  Agent Identity  ≠  API Credential
5.  Agent Identity  ≠  Workspace
6.  Agent Identity  ≠  Capability
7.  Agent-owned Assets  ≠  Machine Permissions
8.  Task Responsibility  ≠  Agent Identity
9.  World  ≠  Agent
10. Private Workspace  ≠  Task Workspace
```

它们的地基是 [`AGENT_CORE_MODEL_V1.md`](../AGENT_CORE_MODEL_V1.md) §1：

> **Agent 是一个持续存在的数字主体（persistent digital subject）。**

上面 1–6 条都是这一条的直接推论 —— **载体、工具、环境会变，主体不变。**
第 7 条与项目既有的 `Wallet Ownership ≠ Machine Permission` **同源**。
第 8 条与既有的 `Task responsibility` 归属规则一致。
第 9 条是第 1 条的 World 版本。
第 10 条与既有的 Memory 四层边界一致。

#### 为什么第 4 条（API Credential）要单独列出来

因为它在代码里**已经被实现过**（每个 Agent 可以有独立 Credential），
所以容易被误读成「Credential 就是 Agent 的身份」。
它**不是** —— Credential 是 Agent 用来接入某个 Provider 的**凭据**，
换一把 key，Agent 还是同一个 Agent。

---

### 二、Capability **不是** Permission

> 🔴 **本 ADR 明确拒绝把 Capability 定义成传统的 Permission。**

采用的定义：

> **Capability 是 Agent 与 World 之间进行感知与行动的受控接口。**

至少分成两类：

```
Perception Capability     World → Agent
    read file · observe game state · browser view
    receive message · sensor input · database query

Action Capability         Agent → World
    write file · send message · browser click · browser type
    game move · database write · robot movement
```

#### `Authorization ≠ Capability`

`Authorization` 可以决定 Agent **是否获得 / 是否可以使用**某个 Capability。
但它**不构成** Capability 本身：

```
Authorization  →  能不能拿 / 能不能用
Capability     →  感知与行动的接口本身
```

> 换句话说：**授权是门禁，Capability 是器官。**
> 有门禁不等于有器官；有器官也不等于门禁已开。

#### 与既有边界的关系

这一条与项目既有的 `Capability ≠ Permission`（Constitution §2.2）
是**同一条原则的加强版** —— 既有的那条说的是「会做 ≠ 有权做」，
本 ADR 补充的是「Capability 这个概念的**内涵**本身就不是权限」。

#### 🔒 不要因此设计新的 Capability Framework

本 ADR **只冻结概念边界**。它**不**要求、也**不**授权去实现：

```
❌ Capability Registry
❌ Capability 生命周期
❌ Capability Framework
❌ Cost / Risk / Latency 模型
```

---

### 三、Gateway 是接口层（但**组合方式不冻结**）

冻结的部分：

> **Gateway 是 Agent 与 World 之间的接口层。**
> 它把某个 World 的**状态、规则和交互能力**，以**受控方式**暴露给 Agent。

```
Agent
   ↕
Capability
   ↕
Gateway
   ↕
World
```

**不冻结**的部分（继续 `PROVISIONAL` / `OPEN`）：

```
Gateway 与 Capability 的代码关系       PROVISIONAL
一对一 / 一对多                        PROVISIONAL
Capability Registry                    OPEN
Capability 生命周期                    OPEN
Cost / Risk / Latency 模型             OPEN
Gateway 是否注册 Capability            OPEN
Capability 是否独立存在                OPEN
Capability 是否跨 World 复用           OPEN
Capability 是否收费                    OPEN
```

---

### 四、World 是外部环境（Workspace 的 World 化保持 `PROVISIONAL`）

冻结的部分：

> **World 是 Agent 可以进入、观察、与之交互并受到其规则约束的外部环境。**

保留的当前架构模型（六要素，不要求所有 World 完全相同）：

```
World
├── State
├── Rules
├── Resources
├── Actors / Entities
├── Events
└── Interaction Surface
```

**明确不冻结**：

> ❌ **不要把 Workspace 强制重构成通用 World。**

「Workspace 可以被理解为一种 World」这一理解
**继续保持 `PROVISIONAL`** —— 它是未来 World / Gateway 统一抽象的
重要基础，但现在**不动 Workspace Core**。

---

## Consequences

### 正面

1. **边界进入 ADR 层**：以后任何开发 Agent 都必须遵守这 10 条，
   而不只是「设计文档里提过」。
2. **`CONFIRMED` 与 `PROVISIONAL` 在制度上分开了**：
   ADR 管前者，[`AGENT_CORE_MODEL_V1.md`](../AGENT_CORE_MODEL_V1.md) 管后者。
3. **Capability 不会被简化成权限系统**：这一条最容易在实现时被走偏
   （「反正都是控制访问，做个 Permission Engine 吧」），现在有 ADR 挡着。
4. **World 抽象不会过早实体化**：六要素模型被记录，但「实现一个 World 类」
   被明确排除。

### 代价 / 遗留

1. **这些不变量目前大部分没有自动守卫。** 它们靠 ADR + 代码评审约束。
   （对比：`Task Shared Context ≠ 第二套 Task Core` 是有守卫的。）
   是否要为其中某几条加守卫，属于后续工作。
2. **`World` / `Gateway` / `Capability` 在代码里还只有局部对应物**
   （Workspace Gateway 是最接近的一个），所以这些边界暂时
   「有制度、无实现」。这正是本 ADR 想固定的状态。
3. **Workspace 的 World 化被推迟**，未来若要统一抽象，
   需要一篇新 ADR 来推翻或细化本文的 §四。

---

### 六、明确不属于 ADR-0038 的内容

以下内容**不在本 ADR 冻结范围内**，它们的 `PROVISIONAL` / `OPEN` 状态
继续由 [`AGENT_CORE_MODEL_V1.md`](../AGENT_CORE_MODEL_V1.md) 管理：

```
Core Drives
Constitution（Agent 自己的）
Self Model
Cognitive Substrate implementation
Autonomous Task Planner
Generic Capability Framework
World Framework
Cross-World Asset System
Personal Tool Manager
```

> 🔒 换句话说：**本 ADR 只冻结「什么不相等」，不冻结「怎么实现」。**

---

## Guard

**本 ADR 目前没有自动守卫。** 如实记录，不含糊。

可被守卫化的候选（**尚未实现**）：

| 不变量 | 可守卫的形态 |
|---|---|
| `Agent Identity ≠ Runtime` | `agents/` 不出现 runtime 生命周期概念 |
| `Agent Identity ≠ API Credential` | `agents/` 不持有明文 key（**已有部分覆盖**：BrainSettings 无 `api_key` 字段） |
| `Agent-owned Assets ≠ Machine Permissions` | `core/workspace.py` 不出现跨 Agent 访问 |
| `Private Workspace ≠ Task Workspace` | `core/workspace.py` 不依赖 Workspace Core（**已实现**，见 `test_task_workspace_docs_guards.py`） |

---

## 证据

- [`AGENT_CORE_MODEL_V1.md`](../AGENT_CORE_MODEL_V1.md) —— 本 ADR 的来源，
  其中 §20 就是这 10 条不变量的原文
- Constitution §2.2「必须记住的五个不等号」 —— 既有的 `Capability ≠ Permission`
- `tests/test_task_workspace_docs_guards.py::TestNoWorkspaceImplementation::test_core_workspace_stays_private_workspace_only`
  —— `Private Workspace ≠ Task Workspace` 的已有守卫
- `tests/test_llm_credentials.py` —— `Agent Identity ≠ API Credential` 的已有部分覆盖
