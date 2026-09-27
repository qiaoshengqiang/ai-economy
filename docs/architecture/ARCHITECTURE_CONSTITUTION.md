# AI Economy OS — Architecture Constitution

> **这份文档是 AI Economy OS 的长期架构 Source of Truth。**
>
> 它回答的不是「代码现在长什么样」（那是代码自己的事），而是
> **「这个系统是什么、为什么这样设计、哪些边界不许越过」**。
>
> 任何一个新的开发者（人或 AI Coding Agent）在动代码之前，
> 都应该先读完这份文档 + [`AGENTS.md`](../../AGENTS.md)。

| | |
|---|---|
| **文档状态** | 长期维护（不是一次性报告） |
| **当前阶段** | Post-Workspace-Phase-0 / Pre-Shared-Context |
| **协议版本** | `protocol_version = "1"` |
| **基线测试** | 2333 passed |
| **最后更新** | Step 4E |

---

## 0. 怎么使用这份文档

### 0.1 它是规范，不是介绍

README 告诉你「怎么跑起来」。这份文档告诉你
**「什么是允许的、什么是不允许的、以及为什么」**。

冲突时以这份文档为准；如果代码与这份文档矛盾，
**要么改代码，要么先改这份文档并说明理由** —— 不允许默默不一致。

### 0.2 四态标注纪律（强制）

这份文档里每一条关于系统的陈述，都必须能归入下面四态之一。
**把未来设计写成「现在已有」是本项目最严重的文档错误。**

| 标记 | 含义 | 例子 |
|---|---|---|
| **`CURRENT`** | 代码里**已经实现**，有测试 | `TaskWorkspaceRef` 存在 |
| **`DESIGNED`** | 已经设计清楚，**尚未实现** | Task Shared Context 的数据模型 |
| **`FUTURE`** | 方向明确，但还没有设计 | Workspace Permission |
| **`FORBIDDEN NOW`** | 当前阶段**明确禁止**实现 | Git、代码执行、Payment |

> **本项目的真实例子（必须照此写）**：
>
> ✅ 正确：「`CURRENT` — 任务通过 `TaskWorkspaceRef` 持有一个
> `workspace_id`。它只是逻辑引用。」
>
> ❌ 错误：「系统已经存在完整的 Workspace Permission。」
>
> 后半句把 `DESIGNED`/`FUTURE` 写成了 `CURRENT`。这会直接误导
> 下一个开发者去调用一个不存在的 API。

### 0.3 导航

| 我想知道… | 去哪 |
|---|---|
| 项目是什么、当前做到哪、怎么开发 | [`AGENTS.md`](../../AGENTS.md) |
| 为什么这样设计、边界在哪 | **本文档** |
| Task Shared Context 的数据模型 | [`TASK_SHARED_CONTEXT.md`](TASK_SHARED_CONTEXT.md) |
| 某个具体架构决策的历史与理由 | [`adr/`](adr/README.md) |
| 怎么跑命令 | [`COMMANDS.md`](../../COMMANDS.md) |
| 消息协议的具体形状 | [`PROTOCOL.md`](../../PROTOCOL.md) |

---

## 1. AI Economy OS 是什么

### 1.1 它不是什么

正式排除这四种误解，因为它们会导致错误的架构决定：

- ❌ **不是一个 Chatbot** —— 这里没有「一个会话」的中心概念
- ❌ **不是一个 Multi-Agent Demo** —— 不是把几个 Agent 并排跑一遍给人看
- ❌ **不是一个 LLM 工作流** —— LLM 只是 Agent 的**外部生产资源**之一
- ❌ **不是一个把 Agent 并排运行的脚本** —— Agent 是**长期运行的经济主体**

### 1.2 它的长期目标

> 一个可以从**单机双 Agent**，逐步演化为**多 Agent、多机器、跨网络**，
> 具有**独立身份、能力、记忆、经济行为和自主协作能力**的
> **Agent Operating Environment / Economy System**。

### 1.3 演进方向

```
单机
  ↓
双 Agent          ← 当前已经在这里
  ↓
多 Agent
  ↓
多进程            ← CURRENT（Workspace Phase 0 已验证）
  ↓
多机器
  ↓
跨网络
  ↓
P2P / Distributed Agent Network
```

**`FORBIDDEN NOW`** —— 不要实现最终形态。
架构必须为它**保留演进空间**，但不要提前建好通向它的每一条路。

### 1.4 为什么「经济」是关键

这个项目与其他 Multi-Agent 系统的根本区别是：

> **Agent 是独立的经济主体，不是被调用的函数。**

它有**自己的**钱包、**自己的**议价策略、**自己的**判断。
它可以：
- 决定**接不接**一个任务
- 决定**报什么价**
- 决定**拒绝**
- 决定**外包**给别的 Agent

系统**不替它做这些决定**。系统的职责是：

| 系统负责 | 系统不负责 |
|---|---|
| 协议 | 「你应该怎么谈价」 |
| 状态 | 「你应该接受这个价格吗」 |
| 业务硬约束 | 「你应该信任谁」 |
| 持久化 | |
| 消息路由 | |

这条分界贯穿整个架构，是**最重要的一条设计原则**。

---

## 2. Agent 的正式定义

### 2.1 Agent 不是「LLM + Prompt」

`CURRENT`（部分）/ `DESIGNED`（整体）—— 一个 Agent 是一个**长期运行的主体**：

```
Agent
├── Identity            身份（我是谁）                    CURRENT
├── Address             地址（在哪里能找到我）             CURRENT
├── Capability          能力（我会做什么）                 CURRENT
├── Brain / Decision    决策（我怎么办）                   CURRENT
├── Private Memory      私有记忆（我经历过什么）           DESIGNED
├── Economy             经济环境（我在什么经济系统里）      CURRENT
├── Wallet              钱包（我有多少钱）                 CURRENT
├── Strategy            策略（我的偏好与底线）             CURRENT
├── Private Workspace   私有空间（我自己的文件）           DESIGNED
└── Runtime / Life Loop 运行时（我什么时候醒来）           FUTURE
```

> **注意 `Private Memory` 的标注是 `DESIGNED`。**
> 当前代码里**没有** Memory 层的实现。Agent 的配置（`config.yaml`）
> 不是记忆 —— 它是**静态声明**，不是**经验积累**。

### 2.2 必须记住的五个不等号

这五个不等号是本项目**最常被搞混**的地方，每一个都对应一次真实的设计风险：

| 不等号 | 含义 | 搞混的后果 |
|---|---|---|
| `Identity ≠ Address` | 我是谁 ≠ 在哪里找我 | 搬家就要换身份；身份不能跨传输层 |
| `Capability ≠ Permission` | 我会做 ≠ 我有权做 | Agent 声明会写代码，就自动获得文件写权限 |
| `Economy ≠ Intelligence` | 有钱 ≠ 聪明 | 用余额推断 Agent 的能力/可信度 |
| `Wallet Ownership ≠ Machine Permission` | 有资产 ≠ 有机器权限 | 有钱的 Agent 自动能操作宿主机 |
| `Private Memory ≠ Shared Task Context` | 我怎么想 ≠ 大家确认了什么 | 一个 Agent 的私下判断变成「共同事实」 |

---

## 3. 核心原则清单

**这十四条是项目的架构骨架。** 每一条都对应代码里的一个真实边界，
并且大部分有**架构守卫**在自动检查（见 §12）。

| # | 原则 | 当前由什么保证 |
|---|---|---|
| 1 | `Identity ≠ Address` | `core/identity.py` + `communication/address.py` 分离 |
| 2 | `Message ≠ Transport` | `communication/message.py` vs `transport.py` |
| 3 | `Communication ≠ Business Logic` | 守卫：`communication/` 不许 import `core.task` |
| 4 | `Capability ≠ Permission` | `core/capability.py` 是纯集合运算；守卫禁止它碰权限 |
| 5 | `Capability ≠ Economy` | Capability 不认识钱包/账本 |
| 6 | `Wallet Ownership ≠ Machine Permission` | Wallet 不提供任何文件/执行能力 |
| 7 | `Task Core ≠ Task Manager` | 守卫禁止 `TaskManager` / `Coordinator` |
| 8 | `Task Shared Context ≠ Message History` | `DESIGNED`（本文档 §6、§9） |
| 9 | `Task Shared Context ≠ Agent Private Memory` | `DESIGNED`（§6） |
| 10 | `Task Shared Context ≠ Task Workspace` | `DESIGNED`（§6、§14） |
| 11 | `Workspace ≠ Permission` | `workspace_id` 只是引用；守卫禁止文件 I/O |
| 12 | `Memory ≠ Alignment` | `DESIGNED`（§7） |
| 13 | `Agreement ≠ Payment` | `TaskAgreementRecord.describe()["is_payment"] == False` |
| 14 | `Budget ≠ Price` | `Task.budget` 与 `Task.agreed_price` 是两个字段 |

补两条从经验里长出来的：

| # | 原则 | 含义 |
|---|---|---|
| 15 | `DecisionPolicy ≠ Decision Execution` | 策略只**返回**决定，不执行 |
| 16 | `Domain ≠ Infrastructure` | `core/` 不依赖 `services/` / `storage/` / `communication/` |

---

## 4. 架构全景图

### 4.1 分层

```mermaid
graph TB
    subgraph External["④ External / World State  ·  FUTURE"]
        EXT[外部 API / 网络 / Git remote / 外部现实]
    end

    subgraph TaskLayer["Task 层"]
        TC[Task Core<br/>状态机 · 预算 · 角色 · 生命周期<br/>CURRENT]
        TSC[Task Shared Context<br/>共同世界模型<br/>DESIGNED]
        TW[Task Workspace<br/>实际共享的文件与代码<br/>FUTURE]
    end

    subgraph AgentA["Agent A"]
        AM_A[Private Memory<br/>DESIGNED]
        AS_A[Strategy / DecisionPolicy<br/>CURRENT]
        AW_A[Wallet<br/>CURRENT]
    end

    subgraph AgentB["Agent B"]
        AM_B[Private Memory<br/>DESIGNED]
        AS_B[Strategy / DecisionPolicy<br/>CURRENT]
        AW_B[Wallet<br/>CURRENT]
    end

    subgraph Comm["Communication · CURRENT"]
        MSG[Message / Address / Transport]
        JRNL[Message Journal +<br/>持久化处理事实 + Claim Lease]
    end

    subgraph App["Application Layer · CURRENT"]
        SVC[TaskService 门面]
        NEG[TaskNegotiationService]
        EXE[TaskExecutionService]
    end

    ECO[Economy / Ledger<br/>CURRENT]

    AM_A -.->|只有通过<br/>Proposal → Alignment<br/>才能进入| TSC
    AM_B -.->|同上| TSC
    AS_A --> TC
    AS_B --> TC
    TSC --> TC
    TSC -.->|引用但不复制| TW
    TW -.->|观察 / 记录来源| EXT
    AW_A --> ECO
    AW_B --> ECO
    Comm --> App
    App --> TC
    TC --> TSC
```

**看图的三条要点**：

1. **Private Memory 到 Shared Context 是虚线** —— 不能直连。
   必须经过 Proposal → Alignment（§8）。
2. **Shared Context 到 Task Workspace 是虚线** —— 前者说「我们认为是什么」，
   后者说「实际有哪些文件」。两者关联但不相等。
3. **只有 Application Layer 能改 Task** —— Communication 送消息，
   但它不认识 Task（§11.2）。

### 4.2 依赖方向（单向，无环）

```
main.py（组合根）
    ↓
services/   →  core/  +  communication/  +  storage/
communication/  →  不依赖 services/、不依赖 core/task
core/       →  谁都不依赖
storage/    →  只被 core/task_store 与 communication/recorder 使用
```

**守卫保证**：
- `core/` 不许 import `services/`（8 个 `*guards*.py` 里的 import 图检查）
- `communication/` 不许 import `core.task` / `core.economy`
- 只有 `main.py` 能调用 `register_type_handler`（接线只能在组合根）

---

## 5. Agent 的核心组成详解

### 5.1 Identity（`CURRENT`）

> **我是谁。**

`AgentIdentity` —— 稳定、不可变、与位置无关。
它是**经济主体**的身份：钱包挂在它下面，账本按它记账。

### 5.2 Address（`CURRENT`）

> **在哪里能找到我。**

`Address` 是 `scheme://identifier` 形式（本地、未来的 P2P 等）。
**搬家不换身份**：Address 可以变，Identity 不变。

### 5.3 Capability（`CURRENT`）

> **我会做什么。**

`CapabilityProfile` 是能力的**集合**。
它只支持集合运算（`covers` / `missing` / `union`…）。

**它刻意没有**：评分、排名、信誉、价格。
因为「谁会做」和「谁最合适 / 谁最便宜」是**两个不同的问题**
（后者属于 Agent 自己的策略）。

### 5.4 Brain / Decision（`CURRENT`）

> **我怎么办。**

两条不同的决策路径，**不要混**：

| | Brain | DecisionPolicy |
|---|---|---|
| 用在哪 | **执行阶段**（怎么写代码、怎么分析） | **议价阶段**（接不接、报多少） |
| 是否调 LLM | 是（这是 LLM 的用武之地） | **否**（确定性本地规则） |
| 为什么 | 任务内容需要智能 | 谈判成本必须低于任务价值 |

`CURRENT` —— 议价策略是 `RuleBasedDecisionPolicy`：
接受线 / 开口价 / 底线 / 最大轮数，参数来自**每个 Agent 自己的 config**。

> **`DecisionPolicy` 只返回 `Decision`，不执行它。**
> 把它落到状态上的是 `TaskNegotiationService`。
> 有守卫禁止策略 import 钱包/账本/通信/文件系统。

### 5.5 Economy / Wallet（`CURRENT`）

> **我在什么经济系统里 / 我有多少钱。**

**账本是唯一事实来源，余额永远是算出来的**
（`SUM(inflow) - SUM(outflow)` 按 `asset` 过滤）。
`wallets` 表**没有** `balance` 列 —— 这是刻意的，防止余额和账本漂移。

货币代码是 `AIC`（AI Coin）。

### 5.6 Strategy（`CURRENT`）

> **我的偏好与底线。**

对 Agent 而言，strategy **就是它的经济参数**：
最低可接受价格、目标价格、风险偏好、时间压力……

**它属于 Agent 自己**，不属于系统。
代码里**不许**出现 `if agent_id == "agent_b"` —— 有守卫。

### 5.7 Private Memory（`DESIGNED`）

> **我经历过什么。**

见 §6 Layer 1。**当前尚未实现。**

### 5.8 Private Workspace（`DESIGNED`）

> **我自己的文件。**

未来属于单个 Agent，与 Task Workspace **不同**。
**当前尚未实现。**

### 5.9 Runtime / Life Loop（`FUTURE`）

> **我什么时候醒来。**

Heartbeat / 事件唤醒 / 自主循环。
**`FORBIDDEN NOW`** —— 不要实现。

---

## 6. Memory 四层架构

> **这是 Step 4E 的核心架构决策。**

### 6.1 全景

| Layer | 名字 | 属于谁 | 状态 | 是不是 Source of Truth |
|---|---|---|---|---|
| **1** | Agent Private Memory | **单个 Agent** | `DESIGNED` | ❌ 不是 |
| **2** | Task Shared Context | **Task** | `DESIGNED` | ✅ 是（任务知识层） |
| **3** | Task Workspace | **Task** | `FUTURE` | ✅ 是（实际产物层） |
| **4** | External / World State | **系统外** | `FUTURE` | ✅ 是（但需记来源） |

### 6.2 Layer 1：Agent Private Memory

**属于单个 Agent。** 用于：

- 个人经验、历史经验、偏好
- 私人知识、私人推理缓存
- 经济经验、谈判策略
- 技能经验
- **对其他 Agent 的私人评价**
- 私人规划

**三条硬规则**：

1. **其他 Agent 默认无权读取。**
2. **它不是系统事实的 Source of Truth。**
3. **它不能自动升级成共享事实。**

> **例子（必须照此理解）**：
>
> Agent B 私下认为「Agent C 不太可靠」。
> 这属于 **Private Memory**。
> **不能**自动成为 Task Shared Context。
>
> 如果 B 想让它影响协作，必须走正式流程提出
> （见 §8）。否则这就是 B 的**偏见**，不是**共识**。

### 6.3 Layer 2：Task Shared Context

**属于 Task，不属于任何 Agent。**

它维护「多个参与 Agent 当前共同使用的 **Task World Model**」。

**它应该包含**：

| 类别 | 说明 |
|---|---|
| Objective | 这个任务要达成什么 |
| Constraints | 约束（预算、时间、技术限制） |
| Acceptance Criteria | 什么算通过 |
| **Decisions** | **已共同确认的决定** |
| **Assumptions** | **暂时假设（未确认）** |
| **Open Questions** | **当前还不知道什么** |
| **Assignments** | **谁负责什么** |
| Current Status | 当前进展 |
| Relevant Facts | 已确认的事实 |
| Terminology | 术语约定 |
| Verification Feedback | 验收意见 |
| Shared Artifacts References | 共享产物的**引用**（不是产物本身） |

> 🔒 **它不是一个 `shared_memory.txt`。**
>
> 它必须具有：`revision` · `provenance` · `change history` ·
> `authority` · `source` · `status` · `alignment semantics`。
>
> 详细数据模型见 [`TASK_SHARED_CONTEXT.md`](TASK_SHARED_CONTEXT.md)。

### 6.4 Layer 3：Task Workspace

**回答**：实际共享了**什么数据和代码**？

未来可能包含：source code · files · tests · patches ·
generated artifacts · worktrees。

`FUTURE` —— **当前只存在 `TaskWorkspaceRef`（一个 `workspace_id` 字符串）**。
它**没有**路径、没有权限、没有 I/O。

> **Workspace ≠ Shared Context**
>
> - Shared Context：「我们**认为**这个任务现在是什么」
> - Workspace：「**实际**有哪些东西」
>
> 一个说决定用 parser B，另一个是 `parser.py` / `tests/` / `fixtures/`。
> 两者相互关联，但**不能混为一体**。

### 6.5 Layer 4：External / World State

例如：外部 API · 网络 · 外部数据库 · Git remote ·
外部服务 · 外部现实环境。

**一条硬规则**：Agent 观察到的 External State
**不能自动等同于 Shared Context**。
**必须记录来源**。

> 为什么：如果一个 Agent 说「我查了，那个 API 支持幂等」，
> 这句话在进入共同模型之前**只是一个观察**，
> 而不是**经过确认的事实**。

---

## 7. Memory ≠ Alignment

> **这是本阶段最重要的区分之一。**

| | 回答的问题 | 是什么 |
|---|---|---|
| **Shared Context** | 我们共同认为现在是什么 | **结果 / 状态** |
| **Alignment** | 我们**如何**形成、确认、修改、争议、废弃共同信息 | **机制 / 过程** |

**Shared Memory 只是「存储共同信息」。**
**Alignment 是「多个 Agent 如何就共同信息达成一致」。**

```
Shared Context   ≠   Alignment Protocol
    （结果）              （产生和更新结果的机制）
```

**为什么必须分开**：

如果只有「共同存储」而没有「形成机制」，那么：

> 任何一条消息都可能悄悄变成「共同事实」。
> 那等于**没有共识，只有最后说话的人**。

---

## 8. 「说过」≠「确认过」

> **这是未来多 Agent 一致性的核心。**

### 8.1 反例（必须避免）

```
Agent A 说：「我觉得我们应该用方案 B。」

Communication History：✅ 存在这句话
Task Shared Context： ❌ 没有 Decision Record
```

**系统不能认为「团队已经决定方案 B」。**

### 8.2 正确的路径

```
Proposal
    ↓
Alignment
    ↓
DecisionRecord
    ↓
Shared Context Revision
```

**只有走完这四步，才算「共同决定」。**

### 8.3 Alignment Protocol 的基本模型

`DESIGNED` —— 先设计，不实现完整协议。

```
Agent A
   ↓  PROPOSE_CHANGE
Task Shared Context
   ↓  Validation / Alignment
Agent B
   ↓  ACCEPT / DISPUTE
Context Revision
```

### 8.4 四类变更：**不要强制所有变化都投票**

> 🔒 **不要把 Shared Context 做成「民主投票系统」。**
>
> 这不是政治制度。**不要**设计：多数票 / 所有人必须批准 /
> 复杂 quorum / blockchain consensus。

需要的是 **authority + provenance + revision + conflict handling**，
而不是复杂共识算法。

| 类型 | 例子 | 谁决定 | 需要对齐吗 |
|---|---|---|---|
| **自动事实** | `Task.status` | **Domain State** | ❌ 不需要（客观事实） |
| **契约约束** | 预算、验收要求 | Task / Agreement / User input | ❌ 不由 Agent 投票修改 |
| **协作决策** | 「选择方案 B」 | **需要明确的对齐机制** | ✅ 需要 |
| **个人信念** | 「我觉得 C 不可靠」 | —— | 留在 **Private Memory** |

### 8.5 冲突怎么办

`DESIGNED` —— 当前只定义原则：

1. **Domain State 优先** —— 如果 Shared Context 与 Task 状态矛盾，
   以 Task 为准，并把 Shared Context 标记为需要修正。
2. **不删除，只标记** —— 旧 Decision 被取代时标 `SUPERSEDED`，不删。
3. **记录争议** —— 如果两个 Agent 无法达成一致，那本身是一个
   **Open Question / Dispute**，而不是「谁先写谁赢」。

---

## 9. Source of Truth

### 9.1 权威优先级（正式定义）

```
Domain State / TaskAgreement
        >
Task Shared Context
        >
Communication History
        >
Agent Private Memory
        >
Agent Inference
```

### 9.2 每一层的含义

| 层 | 含义 | 例子 |
|---|---|---|
| **Domain State / Agreement** | **系统客观事实** | `Task.status` · `agreed_price` · `contractor` · `task_id` |
| **Task Shared Context** | **共同确认**的任务知识 | DecisionRecord · 已确认的 Fact |
| **Communication History** | **过去说过什么** | Message Journal |
| **Agent Private Memory** | **个体**的判断与经验 | 「我觉得 C 不可靠」 |
| **Agent Inference** | **推测** | 「他大概会拒绝」 |

### 9.3 三条推论

1. **Domain State 不能被 Agent Memory 推翻。**
   如果账本说成交价是 25 AIC，任何 Agent 的记忆都不能改变它。

2. **「有人说过」不等于「现在仍然有效」。**
   Communication History 是历史，不是当前状态。

3. **推测不能冒充事实。**
   Agent Inference 必须显式标记为推测。

---

## 10. Context Snapshot

`DESIGNED` —— 先定义概念和边界，**不实现**。

### 10.1 它解决什么问题

> **Agent 每次执行 Task 时，不可能把整个历史加载进 LLM context。**

```
Message 1
Message 2
...
Message 500     → LLM context 装不下，而且大部分是噪音
```

### 10.2 它是什么

**Shared Context 在某个时间点、给某个 Agent 的「可消费视图」。**

它是**派生物**，**不是新的 Source of Truth**：

```
Snapshot #42

Objective:            ...
Current constraints:  ...
Current decisions:    ...
Open questions:       ...
Your assignments:     ...
Latest verification feedback: ...
Relevant artifacts:   ...
```

### 10.3 必须写死的边界

- ❌ Snapshot **不是** Source of Truth（它来自 Shared Context）
- ❌ 本阶段**不实现**自动 LLM summarization
- ✅ Snapshot 必须带 `revision`（否则不知道它是哪一刻的视图）

---

## 11. 各层职责与边界

### 11.1 Task Core（`CURRENT`）

**负责**：状态 · 状态机 · 预算 · 角色 · 生命周期 · Task identity · 报价历史。

**不负责**：消息（`Message`）· 传输 · 目录（`AgentDirectory`）·
LLM · 网络 · 通信日志 · 应用服务。

> **Task Core 不依赖 Communication** —— 有守卫。
> 任务怎么改状态是领域规则；消息怎么送达是通信的事。

### 11.2 Communication（`CURRENT`）

**负责**：Message · Address · Identity · Transport · Delivery ·
协议 · Message Journal · **持久化的处理事实** · Claim Lease。

**不负责**：Agent 是否接受价格 · Task 是否 ACCEPTED ·
Agreement 是否成立 · Task 是否 VERIFIED。

> **通信层连 `budget` 这个词都不许出现** —— 有守卫。
> 它只负责把消息送到；「这个价格允不允许」是业务问题。

### 11.3 Application Layer（`CURRENT`）

`services/` —— **消息与领域之间的适配层**。

```
Message
   ↓
TaskService（薄门面 / 路由）
   ↓
Handlers（按消息类型）
   ├──→ TaskMessageMapper        协议 payload ↔ 应用数据
   ├──→ TaskNegotiationService   议价规则 + 状态落地
   └──→ TaskExecutionService     执行规则 + 状态落地
```

**议价与执行必须分开**（Step 4D 的核心决定），因为它们的**变化原因不同**：

| | 变化原因 |
|---|---|
| **议价怎么谈** | 会随着 Agent 的智能程度不断演化 |
| **执行怎么走** | 由协议和状态机固定 |

混在一起就会出现「想改议价策略时得动执行代码」。

### 11.4 Negotiation（`CURRENT`）

**负责**：OFFER · COUNTER_OFFER · ACCEPT · REJECT ·
报价历史 · 谈判轮次 · 成交凭证（TaskAgreement）。

**不负责**：执行。

`Agent Strategy` **不在这一层** —— 它在 Agent 自己的 `DecisionPolicy` 里。

### 11.5 Execution（`CURRENT`）

**负责**：START · SUBMIT · VERIFY · COMPLETE 的校验与状态落地。

**明确不负责**（`FORBIDDEN NOW`）：
❌ 执行代码 ❌ 调 LLM ❌ 碰钱 ❌ 碰 Workspace ❌ 碰 Git。

> **4D 的「执行」指的是任务生命周期进入执行状态，
> 不是「系统替 Agent 跑代码」。**

### 11.6 Economy（`CURRENT`）

**负责**：Agent · Wallet · Ledger · 余额计算 · 审计 · 转账。

**不负责**：任务 · 消息 · 策略。

> `Agreement ≠ Payment`：`TaskAgreementRecord` 记录**约定了什么**，
> **不是**支付记录。它有 `is_payment: False` 的显式声明。
> 结算是一个**独立的后续阶段**。

### 11.7 与 Shared Context 的边界（`DESIGNED`）

| 相邻层 | 边界规则 |
|---|---|
| **Private Memory** | 默认不能修改 Task / Agreement / Shared Context / 其他 Agent 的 Memory / 不能获得 Workspace 权限。只能通过 `Private Observation → Proposal → Alignment → Shared Context` 进入共享层 |
| **Workspace** | Shared Context 说「认为是什么」，Workspace 说「实际有什么」。相互关联，不能混为一体 |
| **Communication** | `Message → 可能的 Context Proposal → Alignment → Context Revision`，**不是** `Message → 自动写 Shared Context` |
| **Task Core** | **Shared Context 可以引用 Domain State，但不能成为 Domain State 的第二个来源。** 不要复制一套 `status` / `agreed_price` / `contractor` 然后让两边漂移 |
| **Economy** | 只保存**与这个 Task 相关的、需要共同知道的**经济事实（如 `agreed_price`，来自 `TaskAgreement`）。**Agent B 的全部余额不属于 Shared Context** |

---

## 12. Guard 规范

> **Step 4E 之前，本项目已经出现过多次「守卫本身有问题」。**
> 这一节是为了让后面的人不要再犯。

### 12.1 当前守卫现状（`CURRENT`）

8 个守卫文件，265 项：

| 文件 | 项数 | 守什么 |
|---|---|---|
| `test_capability_guards.py` | 30 | Capability / Directory 边界 |
| `test_communication_guards.py` | 27 | 通信层业务无关 |
| `test_execution_guards.py` | 39 | 4D 的 10 组边界 |
| `test_multiprocess_guards.py` | 35 | Phase 0 的 11 组边界 |
| `test_negotiation_guards.py` | 32 | 议价层边界（覆盖整个 services 包） |
| `test_reliability_guards.py` | 42 | 4D.1 的 10 组边界 + 迁移顺序 |
| `test_service_layer_guards.py` | 29 | 薄门面 / 无 God Module / 扩展点 |
| `test_task_guards.py` | 31 | Task Core 边界 |

### 12.2 检测手段优先级（**强制**）

**优先使用**：

1. **AST**（`ast.parse` / `ast.walk` / `ast.unparse`）
2. **import 图**（谁 import 谁）
3. **真实调用点**（`conn.execute(...)` 的参数，而不是文件里有没有这个词）
4. **具体的 DB 写入**（`UPDATE x SET y`，而不是「含 UPDATE」）
5. **运行时行为**（`hasattr`、真的跑一遍看不变量）

**不要主要依赖**：

```python
source.count("xxx")        # ❌ 会被注释、文档字符串、变量名骗到
```

### 12.3 如果必须做字符串扫描

三条**强制**要求：

1. **去掉文档字符串**（`ast.unparse` **不会**帮你做这件事）
2. **明确扫描范围**（是「整个文件」还是「某个方法体内」）
3. **做反向测试**（把违规注入进去，确认守卫真的变红）

### 12.4 本项目的真实教训（**必须读**）

#### 教训 A：子串匹配会误报（至少 4 次）

```
seen_tracker      含 "ack"
offer_amounts     含 "mount"
is_open           含 "open"
agent_directory   含 "agent_d"     ← 最阴的一次：它是 agent_d + irectory
```

**对策**：检查标识符要**按词切分**，或匹配**带引号的完整字面量**。

#### 教训 B：文档字符串不是代码（至少 8 次）

文档里会**合法地**写：

```markdown
❌ 不 import llm
❌ 不执行代码（没有 subprocess）
绝对不许写 `if agent_id == "agent_b": ...`
registry.register(SubmitHandler(negotiation))     ← 只是举例
```

如果守卫查原文，就会被自己的说明文字绊倒。

**对策**：`code_only()` —— 先删掉文档字符串节点，再 `ast.unparse`。

#### 教训 C：守卫查错了对象（至少 3 次，**都是假绿**）

| 假绿 | 为什么绿了 |
|---|---|
| `assert "claim_message" in code` | 方法自己就叫 `_claim_message` —— 名字永远都在 |
| `assert "BEGIN IMMEDIATE" in source` | 文档里为了解释这个修复，大量提到了它 |
| `assert body.count("UPDATE communication_messages") == 1` | `ast.unparse` **保留文档字符串**，那个 1 来自文档里的 SQL 示例；真正的 SQL 是 `UPDATE {TABLE_COMM_MESSAGES}` |

**对策**：
- 查**真实的调用**（`self.recorder.claim_message(`），不是方法名
- 查**纯代码**，不是原文
- 数**真正传给 `execute()` 的 SQL 字符串**，并按语句开头判断类型
  （`INSERT ... ON CONFLICT DO UPDATE` 里也有 "UPDATE"）

#### 教训 D：单进程测试发现不了多进程问题（Phase 0 的教训）

`BEGIN`（延迟事务）在单进程下完全正确。真多进程时才会死锁 →
`database is locked` → **并发保护被静默绕过**。

**对策**：并发相关的边界，必须有**真进程**测试（不是线程）。

---

## 13. 开发者上下文一致性机制

> **本阶段最重要的成果不是任何 Python 功能，
> 而是建立一个机制。**

### 13.1 要解决的问题

```
❌ 错误的方式：
    上一轮聊天
        ↓
    Coding Agent 大概记得
        ↓
    继续写代码
        ↓
    上下文丢失 → 架构逐渐漂移
```

### 13.2 正确的方式

```
Human / Project Owner
        ↓
Architecture Constitution          ← 本文档
        ↓
Developer Context                   ← AGENTS.md
        ↓
Current Stage Specification         ← 每一步的任务书
        ↓
Coding Agent
        ↓
Implementation
        ↓
Tests / Guards
        ↓
Updated Architecture Knowledge      ← ADR + 本文档的更新
```

**每一个新的 Coding Agent，都不应该需要依赖某一次聊天记录。**

### 13.3 三层文档的分工（不要重复）

| 文档 | 长度 | 作用 |
|---|---|---|
| **Architecture Constitution**（本文档） | 长 | 完整的 Source of Truth。**为什么这样设计** |
| **Developer Context**（`AGENTS.md`） | 短 | **导航和入口**。花几分钟知道这是什么、当前在哪、什么不能动 |
| **ADR**（`adr/`） | 中 | **单个决策**的历史与理由 |

> **不要把完整 Constitution 复制进 Developer Context。**
> 那会制造两个需要同步的真相来源。

---

## 14. 回答 25 个问题

> 这一节是**验收索引**：只读这份文档 + `AGENTS.md` +
> `TASK_SHARED_CONTEXT.md`，应该能回答下面每一个问题。
> 如果答不出来，说明文档还不完整。

| # | 问题 | 答案位置 |
|---|---|---|
| 1 | AI Economy OS 到底是什么？ | §1 |
| 2 | Agent 是什么？ | §2.1 |
| 3 | 一个 Agent 有哪些核心组成？ | §2.1、§5 |
| 4 | Identity / Address 有什么区别？ | §2.2、§5.1、§5.2 |
| 5 | Capability / Permission 有什么区别？ | §2.2、§5.3 |
| 6 | Wallet / Machine Permission 为什么分开？ | §2.2、§3 |
| 7 | Task Core 干什么？ | §11.1 |
| 8 | Communication 干什么？ | §11.2 |
| 9 | Negotiation 干什么？ | §11.4 |
| 10 | Execution 干什么？ | §11.5 |
| 11 | Agent 自己的 Strategy 在哪里？ | §5.6、§11.4 |
| 12 | Memory 有哪四层？ | §6 |
| 13 | Shared Context 为什么存在？ | §6.3、§10.1 |
| 14 | Shared Context 和 Message History 有什么区别？ | §9、§11.7 |
| 15 | Shared Context 和 Workspace 有什么区别？ | §6.4、§11.7 |
| 16 | Memory 和 Alignment 有什么区别？ | §7 |
| 17 | Source of Truth 是谁？ | §9 |
| 18 | Agent 如何把个人判断带进共享上下文？ | §6.2、§8.2、§11.7 |
| 19 | 一个共享 Decision 如何产生？ | §8.2、§8.3、`TASK_SHARED_CONTEXT.md` |
| 20 | 一个 Assumption 如何变成 Fact？ | `TASK_SHARED_CONTEXT.md` |
| 21 | 一个旧 Decision 如何被替代？ | §8.5、`TASK_SHARED_CONTEXT.md` |
| 22 | Context 如何处理 revision？ | `TASK_SHARED_CONTEXT.md` |
| 23 | Context 如何解决上下文窗口不足？ | §10 |
| 24 | 未来 Workspace 应该依赖什么？ | §6.4、§11.7 |
| 25 | 什么东西当前绝不能实现？ | §15 |

---

## 15. FORBIDDEN NOW

**当前阶段明确禁止实现。** 这份清单会被逐步解锁，
但**每一步都必须有明确的任务书**，不能顺手实现。

### 15.1 全阶段禁止（在明确解锁前）

| 领域 | 禁止内容 |
|---|---|
| **Workspace** | Task Workspace 实现 · Workspace Manager · Gateway · Permission · ACL · members · 文件系统访问 · artifact storage · 文件上传下载 |
| **Git** | worktree · branch · commit 自动化 · merge · patch 应用 |
| **代码执行** | `subprocess` · shell · Docker · sandbox · 代码执行器 · 测试运行器 |
| **经济结算** | Payment · Escrow · Settlement · Wallet transfer · Ledger append · AI Coin 扣款 |
| **通信可靠性** | Retry Queue · Retry Scheduler · ACK · Redis · Kafka · Celery · Message Broker |
| **网络** | P2P · 多机 · 分布式部署 |
| **Agent 生命周期** | Heartbeat · Scheduler · Life Loop · 自主唤醒 |
| **智能演化** | Adaptive Negotiation · Reinforcement Learning · 策略自学习 · Strategy Marketplace |
| **协调** | TaskManager · Coordinator · 中央调度 |

### 15.2 不要过度设计（Step 4E 特别要求）

看到 "Shared Context" 就设计成下面这些 —— **现在都不要**：

❌ Blockchain consensus
❌ CRDT
❌ Event sourcing everything
❌ Vector database
❌ Knowledge graph
❌ Distributed consensus
❌ LLM memory cloud
❌ Multi-agent voting protocol
❌ Complex ontology

> 这些未来都可能**有价值**。但现在**不要提前选择**。
>
> 本阶段的目标是：**建立最小但正确的概念边界。**
> 以后再根据真实需求演进。

### 15.3 当前仍然不存在的（不要假装它存在）

| 东西 | 实际状态 |
|---|---|
| Task Workspace | **只有 `TaskWorkspaceRef`**（一个 `workspace_id` 字符串） |
| Workspace Permission | 完全不存在 |
| Agent Private Memory | 只有设计，没有实现 |
| Task Shared Context | 只有设计，没有实现 |
| Shared Context 数据库表 | **不存在** |
| Workspace 相关的任何表 | **不存在** |
| Git / Worktree | 不存在 |
| 真实代码执行 | 不存在 |
| Payment / Settlement | 不存在 |
| P2P / 多机 | 不存在 |
| Agent Life Loop | 不存在 |

---

## 16. 当前架构的真实状态

### 16.1 已完成（`CURRENT`）

| 阶段 | 内容 | 提交 |
|---|---|---|
| Step 1 | Agent + Economy 基础骨架 | — |
| Step 2 / 2.5 | Brain / LLM 抽象 + 真实 DeepSeek Provider | — |
| Step 3 / 3.6 / 3.7 | Communication + Identity / Address / Transport / Protocol 冻结 | — |
| Step 4A | Task Core + Lifecycle + `TaskWorkspaceRef` + `Money` | — |
| Step 4B | Capability + `AgentDirectory` + 自主发现协作者 | — |
| Step 4C | `DecisionPolicy` + 多轮谈判 + ACCEPT / REJECT + `TaskAgreement` | `311138b` |
| Refactor | Task Application Layer（门面 / Handler / Mapper / Negotiation） | `b782b7b` |
| Step 4D | START / SUBMIT / VERIFY / COMPLETE + `TaskExecutionService` | `f391a1b` |
| Step 4D.1 | 持久化消息幂等 + Task 乐观锁 + 事务原子性 | `0a78794` |
| Workspace Phase 0 | 真实多进程验证 + Message Claim Lease | `82f4718` `4898bd8` |
| **Step 4E** | **Architecture Constitution + Shared Context 设计** | 本步骤 |

**基线**：2333 passed · Protocol `v1` · 15 个命令 · 10 张表

### 16.2 已知技术债（`CURRENT` 的真实问题）

> **不隐瞒。** 这些是**已经确认存在**的问题，
> 不是「以后可能要注意」。

| # | 技术债 | 严重度 | 说明 |
|---|---|---|---|
| 1 | **租约 vs 长任务** | 🔴 高 | 没有 lease renewal（本阶段禁止）。如果 handler 处理时间**超过租约**，别的 worker 会接手 → **重复处理**。Workspace 阶段的重复处理 = **重复的文件副作用** |
| 2 | **多线程共享 `Database` 不安全** | 🔴 高 | `_tx_depth` 是实例状态。两个线程共享同一个 `Database` 时会被交错修改 → 事务边界错乱。当前唯一安全用法是**每线程一个实例** |
| 3 | **Retry 未实现** | 🟡 中 | 租约到期只让**占用**失效。**没有任何东西会自动重新投递**。崩溃恢复的最后一步（谁来再送一次）还不存在 |
| 4 | **网络 / 多机未做** | 🟡 中 | Phase 0 验证的是**多进程、同一文件系统**。SQLite 也不适合多机共享 |
| 5 | **`result_ref` 没有 resolver** | 🟡 中 | 它只保证「不是路径、不是 URL」。**没有任何东西检查它指向的成果是否真的存在**。当前 `SUBMIT` 在物理上不可证伪 |
| 6 | **压力测试规模小** | 🟢 低 | 4 进程 × 15 轮。足以证明**正确性**，不足以说明**性能** |
| 7 | **写事务全串行** | 🟢 低 | `BEGIN IMMEDIATE` 的固有代价（SQLite 单写者模型）。当前负载无感 |

### 16.3 Workspace Phase 0 的遗留问题

Phase 0 完成了两个前置验证，但**留下了一件必须带进下一阶段的决策**：

> **租约时长必须大于最慢的 handler。**
>
> 现在默认 300 秒。一旦有长任务（Workspace 阶段很可能有），
> 必须**先做决定**：调长租约？实现续租？还是给长任务换一种占用模型？
>
> **这是 Workspace 阶段的第一个设计决策，不是遗留 bug。**

---

## 17. 变更这份文档的规则

1. **任何架构边界的改动，必须先改这份文档。**
   不是「先写代码，回头补文档」。

2. **每个重要决策写一条 ADR**（[`adr/`](adr/README.md)）。
   ADR 一旦写下**不修改**；被取代时写新 ADR 并标注 `superseded by ADR-XXXX`。

3. **不要把 `DESIGNED` / `FUTURE` 写成 `CURRENT`。**
   这是最容易被后来者误读的错误。

4. **不要在文档里写「大概」「可能已经」。**
   要么确认（读代码/跑测试），要么标注为不确定。

5. **文档与代码矛盾时，先确认真实状态，再决定改哪一边。**
   不允许默默不一致。
