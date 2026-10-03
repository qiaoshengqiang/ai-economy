# TASK_ORIGIN_LINEAGE.md — Task Origin & Task Lineage 逻辑架构

> **状态：逻辑架构定义（`SETTLED` 概念）／实现为 `FUTURE`**
>
> 这份文档定义 **Task 从哪里来、谁创建、属于哪条谱系、谁承担经济责任**。
> **它不描述已经存在的代码。** 当前代码里**没有** `origin` / `parent` /
> `root` / `funding` 字段（见 §12）。
>
> 上游：
> [`AGENTS.md`](../../AGENTS.md)（入口）
> → [`ARCHITECTURE_CONSTITUTION.md`](ARCHITECTURE_CONSTITUTION.md)（长期 Source of Truth）
> → **本文档**
> → [`adr/0023-task-origin-and-lineage.md`](adr/0023-task-origin-and-lineage.md)

| | |
|---|---|
| **文档状态** | 长期维护 |
| **实现状态** | **`FUTURE`** —— 零实现 |
| **当前阶段** | `Post-Step-4F / Pre-Task-Workspace` |
| **最后更新** | Task Origin & Task Lineage |

---

## 0. 怎么读这份文档

### 0.1 四态标注（沿用 Constitution §0.2）

| 标记 | 含义 |
|---|---|
| **`SETTLED`** | **逻辑架构已确定**。实现时必须遵守，不得重新发明。 |
| **`OPEN`** | **尚未决定**。列出了问题，**但没有拍板**。 |
| **`FUTURE`** | 方向明确，尚未设计。 |
| **`NOT IMPLEMENTED`** | 当前一行代码都没有。 |

> ⚠️ **`SETTLED` ≠ `IMPLEMENTED`。**
> 本文档**全部**内容都是 `NOT IMPLEMENTED`。

### 0.2 一句话

> **一个 Task 从哪里来、谁创建、属于哪条谱系、谁出钱、谁负责**
> —— 这是**五个不同的问题**，必须有**五个不同的答案**。

---

## 1. 为什么需要这一层

### 1.1 当前状态（`CURRENT`）

Task 已经具备：Task Core · Capability Discovery · Negotiation ·
TaskAgreement · Execution · Verification · Shared Context ·
Workspace（`FUTURE`）。

但**还没有正式回答**：

> 一个 Task 到底**从哪里来**？
> 这个 Task 是**谁**创建的？它是不是某个更大 Task 的**子任务**？
> **谁为它承担经济责任**？**谁最终负责它的执行**？

### 1.2 为什么现在必须回答（`SETTLED`）

随着系统发展，Task 不会永远只有：

```
Human Demand → Agent → Task
```

> ⚠️ **注意「Human Demand」不是「Human」。**
> Human 提供的是 **Demand**；**Agent** 才创建 Task。见 §2.4。

未来会出现：

```
Human                （提供 Demand）
  ↓
Demand
  ↓
Agent A              （Interpretation / Structuring / Decision）
  ↓
Root Task            （由 Agent A 创建）
  ↓
Child Task
  ↓
Agent B
  ↓
Grandchild Task
  ↓
Agent C
```

同时，**未来 Agent 自己也可能**：

```
Agent A
  ↓
发现经济机会
  ↓
自己创建 Root Task
```

### 1.3 长期总原则（`SETTLED`）

> ### **规则限制结构，策略决定行为，激励塑造结果。**

| 层 | 负责什么 |
|---|---|
| **系统硬规则** | 定义必要的**结构 · 安全 · 边界** |
| **Agent Strategy** | 决定**如何行动** |
| **Economic Incentives** | 通过**收益 · 成本 · 风险 · 成功率 · 返工** 等后果，让 Agent **自主**形成更有效的行为 |

> ⚠️ **不要因为讨论了外包 / Child Task / Task Economy，
> 就把 Agent 的具体策略硬编码成系统规则。**
> 见 §9。

---

## 2. 七个正式概念（`SETTLED`）

### 2.1 关系表

| 概念 | 回答什么问题 |
|---|---|
| **Origin** | 为什么这个 Task 会出现？ |
| **Creator** | 谁**正式创建**了它？ |
| **Parent** | 它**直接从哪个 Task 派生**？ |
| **Root** | 它属于**哪棵 Task Lineage**？ |
| **Funding** / Economic Responsibility | 谁承担它的**经济资源**？ |
| **Responsible Agent** | 谁对**实际执行结果**负责？ |
| **Participant** | 谁**参与**这个 Task？ |

### 2.4 ⚠️ Human Demand ≠ Task；Human ≠ Task Creator（`SETTLED` —— 本轮核心修正）

> **这是 Task 模型中的职责边界，不是措辞调整。**

#### 正确链路

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

#### Human 提供的是 **Demand**，不是 Task

Human 提供：

- 需求 · 目标 · 约束 · 偏好 · 验收标准 · 反馈 · 必要时的治理指令

**这些属于「外部输入 / Demand 层」。**

而 **Task** 是 AI Economy OS **内部**的：

> 可执行、可协作、可跟踪、**可产生经济责任**的工作单元。

因此：

```
❌ Human Demand = Task
❌ Human = Task Creator
```

#### Root Task 的正确模型

```
❌ 旧模型（本轮修正掉）：
    Human → 创建 Root Task

✅ 正确模型：
    Human
      │  Demand
      ▼
    Agent
      │  Interpretation / Structuring / Decision
      ▼
    Root Task
```

> **Root Task 的正式 Creator 默认应该是 Agent，而不是 Human。**
> **Human 是 Root Demand 的来源之一，但不是 Task 的直接 Creator。**

#### Demand Source ≠ Creator

| | 回答 |
|---|---|
| **Demand Source** | 这个需求**从哪来**？（可能是 Human，也可能是 Agent 自己） |
| **Creator** | 谁把它**正式创建成一个 Task**？（**始终是 Agent**） |

```
Demand Source = Human
Creator       = Agent
```

**两者不能合并。**

### 2.5 Human 的四个角色（`SETTLED`）

> **Human 不应该再作为普通 Task Creator 被建模。**

| # | 角色 | 说明 |
|---|---|---|
| **1** | **Demand Provider** | 向系统提供需求、目标、约束和期望。**这是 Human 最主要的入口。** |
| **2** | **Feedback Provider** | 交付后提供真实体验反馈 / 验收结果 / 指出问题 / 修改需求 / 提出新约束 |
| **3** | **Optional Collaborator** | 作为 Task **参与者**：提供专业知识 · 特殊信息 · 完成 Agent 当前无法完成的环节 · 人工验证 |
| **4** | **Governance Authority** | 在系统规定的治理边界内做高权限操作：Force Unlock · 异常恢复 · 治理授权 · 人工裁决 |

**两条关键约束**：

> 🔒 **Feedback 本身不等于直接修改 Task Core。**
>
> Agent 根据反馈**决定**：是否继续当前 Task · 是否 Reopen / Iteration ·
> 是否创建 Derived Task · 是否调整执行方案 · 是否结束当前 Task。

> 🔒 **Governance 不是普通 Task Creation / Execution。**
> 它属于治理层，见 §2.6。

### 2.6 Human 不应该成为 Task Manager（`SETTLED`）

> **Human 不负责持续拆解、调度、管理 Agent 的 Task Tree。**

原因不是禁止 Human，而是**系统目标本身就是**：

> **Agent 能够独立组织复杂工作。**

| Human 提供 | Agent 负责 |
|---|---|
| Demand | Interpretation |
| Constraints | Task Creation |
| Feedback | Task Decomposition |
| Governance | Collaboration |
| | Negotiation |
| | Execution |
| | Iteration |

> ❌ **不要为了方便实现而重新引入**：
> **Human Task Manager** · **Human Task Coordinator** ·
> **Human Central Scheduler** · **Human-driven Task Tree Management**。
> **这些都不应该成为默认架构。**

### 2.7 Root Task 不只有一种来源（`SETTLED`）

#### A. `HUMAN_DEMAND`

```
Human Demand → Agent（理解并正式创建）→ Root Task
```

- **Demand Source = Human**
- **Task Creator = Agent**

**两者不能合并。**

#### B. `AGENT_INITIATIVE`

```
Agent → 发现机会 → 判断收益 / 成本 / 风险 → 自行创建 Root Task
```

> **Root Task 不应该天然绑定 Human。**
> 来源可以是 Human Demand，也可以是 Agent Initiative。

### 2.8 OS 限制结构，Agent 决定策略（`SETTLED`）

| **OS 负责限制结构** | **Agent 决定策略** |
|---|---|
| Task 生命周期 | 是否创建 Child Task |
| Task lineage 结构 | 是否 Outsource / 给谁 |
| 最大深度 | 是否自己完成 |
| 权限 | 是否继续迭代 |
| Workspace 隔离 | 是否投资能力 |
| Write Lock | 如何分配预算 |
| Task Agreement | 如何组织团队 |
| 数据一致性 · 持久化 | |

> ⚠️ **不要因为本次 Human Demand 修正，
> 而重新增加大量中心化决策逻辑。**

### 2.2 绝对不要压缩成一个 `owner`

> 🔒 **绝对不要把这些全部压缩成一个 `owner` 字段。**

理由见 §3–§8：每一个概念的**更换时机**都不同 ——
用一个字段表示它们，会在第一次出现「A 创建、B 执行」时就崩掉。

### 2.3 概念模型（`SETTLED`）

```
Task
│
├── Origin                       为什么会出现？
├── Creator                      谁正式创建？
├── Parent                       直接从哪个 Task 派生？
├── Root                         属于哪棵 Lineage？
├── Funding / Economic Resp.     谁承担经济资源？
├── Responsible Agent            谁对执行结果负责？
├── Participants                 谁参与？
└── Task Relationships
```

> 第一版**最终很可能**自然映射成数据库字段，
> 但**先定义逻辑语义，不要先以数据库表结构反向定义概念**。

---

### 2.9 ⚠️ Current V1 vs Future Front Agent（`SETTLED` 边界 / Future 不实现）

> **V1 先把 Agent Economy 核心闭环跑通，不是先解决 Human Experience。**
>
> 本节回答：**当前架构会不会把未来的 Front Agent 写死？**

#### 2.9.1 当前 V1 的真实目标

V1 **不是**面向普通用户的完整自然语言 AI 产品。它面向：

> **专业的 Agent 使用者** —— 能比较明确地提出任务，
> 并理解 Agent · Task · 协作 · 预算 · 验收这些概念。

```
Professional User
        │ 明确任务 / 约束 / 验收要求
        ▼
     Agent A
        ├── 自己执行
        └── 发现需要协作 → 寻找 Agent B
                 │
            Negotiation → Task Agreement
                 │
             Child Task → Agent B 执行 → 返回结果
                 ▼
             Agent A 整合 → Root Task Result
                 ▼
             Human 验收 / 反馈
```

**V1 真正要验证的是**：

> 一个人类用户能否把一个明确任务交给 Agent A，
> Agent A 能否**作为独立经济主体自主决定如何完成**，
> 并通过 **Agent-to-Agent 协作**完成复杂工作。

#### 2.9.2 V1 **不是**的东西

```
❌ Human → 中央 Task Manager → 调度 Agent A / B
❌ Human → 系统自动拆任务 → 自动指定 Agent
```

> **Agent A 必须真正承担 Task 的组织和执行责任。**

#### 2.9.3 V1 的 Human 角色（很简单）

| ✅ V1 需要 | ❌ V1 不要求做得好（以后可以增加） |
|---|---|
| 提出任务 · 提供约束 · 提供上下文 | 模糊意图 · 开放式探索 |
| 提供预算 / 经济承诺（需要时） | 自然语言需求澄清 |
| 最终验收 · 提供反馈 | 自动方案比较 · 普通用户教育 · 长期陪伴式交互 |

#### 2.9.4 ⚠️ V1 必须避免的 4 个错误假设

> 虽然 Front Agent 现在**不实现**，但当前代码与架构
> **绝对不能隐含**以下假设。

| # | 错误假设 | 正确理解 |
|---|---|---|
| **1** | `Human → Agent A` 是**系统永远成立**的架构规则 | 那只是 **V1 的部署方式**。不要硬编码成架构常量 |
| **2** | Human 可以直接成为 Task Creator | 保持已定边界：**Human = Requester**，**Agent = Task Creator**（§2.4） |
| **3** | Human 永远直接操作 Task | 不要在 Task Core 里硬编码 `Task.creator = human`；也**不要**让未来的 Front Agent 变成一个「必须新增的特殊 Task Creator」 |
| **4** | Front Agent 是一个**中央 Coordinator** | 见 §2.9.6 |

#### 2.9.5 未来 Front Agent 的构想（`FUTURE` —— 本轮不实现）

```
                    Human
                      │
                      ▼
              ┌────────────────┐
              │  Front Agent   │   ← Human-facing Agent
              │  (仍是 Agent)  │
              └───────┬────────┘
                      │ 理解 / 澄清 / 探索
                      │ 方案 / 成本 / 报价
                      │ Human 选择
                      │ Economic Commitment
                      ▼
                 Root Task
                      │
             ┌────────┴────────┐
             ▼                 ▼
          Agent A           Agent B
             └──── 协作 / 外包 ────┘
```

**未来它可能负责**：理解模糊目标 · 提供可选方案 · 澄清目标与约束 ·
比较方案 · 解释成本/风险/预计消耗 · 判断是否进入正式工作流程。

```
普通对话 → 咨询 → 查询 → 方案探索 → 明确委托 → Economic Commitment → Root Task
```

> ⚠️ **这只是未来产品设计方向。本轮不实现。**

#### 2.9.6 **Front Agent 本质上仍然是 Agent**

> ❌ **不要把 Front Agent 设计成特殊的「上帝组件」。**

它仍然遵循 AI Economy OS 的基本哲学：

> **Agent 是独立经济主体，不是被调用的函数。**

因此未来 Front Agent **可以**：拥有自己的 Identity · Capability ·
经济账户 · Memory · 使用 Tools · 与其他 Agent 通信 ·
为 Human 提供服务 · 自己承担成本。

只是它的**专业角色偏向**：Human Interface · Human Intent Understanding ·
Proposal · Planning。

#### 2.9.7 Front Agent **不是** Agent Economy 的中央控制器

未来即使有 Front Agent，也**必须允许**：

```
✅ Agent A → Agent Initiative → Root Task      （不经过 Front Agent）
✅ Agent A ↔ Agent B ↔ Agent C                 （直接协作）
```

> **Front Agent 只负责 `Human → AI Economy OS`，
> 不是 `所有 Agent → Front Agent → 再分发`。**

#### 2.9.8 当前代码的**兼容性要求**（不允许写死未来）

| # | 要求 |
|---|---|
| **1** | ❌ 不把 Agent A 写死成「Human Agent」—— 不要 `class HumanAgent`，不要 `if agent_id == "agent_a": human_request = True` |
| **2** | ❌ 不把 Human 入口写死在 Task Core —— Task Core 继续表达 `Task · Creator · Origin · Parent · Root · Funding · Responsible · Participants`，**不要**增加 `HumanTask` 这种特殊 Task |
| **3** | ✅ Communication 保持**角色中立** —— 现在 `Human → Agent A`，未来 `Human → Front Agent → Agent A`，甚至 `External System → Agent`，**不应该要求 Protocol 重新设计** |
| **4** | ❌ Task Creation **不依赖 Human 类型** —— **Front Agent 也是 Agent**，所以不要建立 `Human-created Task` / `Front-created Task` / `Agent-created Task` 这种特殊 Task 机制。**需要区分来源时用现有的 `Origin` / `Demand Source` / `Creator` 概念**，而不是创建不同 Task 类 |

#### 2.9.9 Demand 是否成为独立 Domain Entity —— 仍 `OPEN DESIGN`

Human → OS 的入口可能经历：

```
模糊意图 → 交流 → 查询 → 方案探索 → 明确委托 → 经济承诺 → Task
```

因此 **Demand 是否应该成为独立 Domain Entity，目前没有结论。**

> **本轮不创建**：Demand · Demand 表 · Demand Service ·
> Demand Lifecycle · Demand API。保持 **`OPEN DESIGN` / `FUTURE`**。

#### 2.9.10 开发优先级**不因 Front Agent 而改变**

```
Agent → Communication → Task → Capability / Discovery → Negotiation
  → Agreement → Execution → Verification → Shared Context
  → Task Workspace → 可靠协作
```

> **Front Agent 不插入当前主链路。**

#### 2.9.11 V1 核心验收场景

> 一个专业用户直接向 Agent A 提出明确任务：
> **「请完成 X，约束是 Y，验收条件是 Z。」**

Agent A：① 理解任务 → ② 判断自己能否完成 → ③ 如需协作，**自主**寻找 Agent B
→ ④ 与 B 协商 → ⑤ 为 B 创建**合法的 Child Task** → ⑥ B 执行 →
⑦ B 返回结果 → ⑧ A 整合结果 → ⑨ A 完成 Root Task → ⑩ Human 验收。

> **整个过程中 Human 不需要管理 Agent B。**
> 这就是目前最核心的 AI Economy OS 能力。

#### 2.9.12 「未来兼容」的正确理解

```
❌ 把「未来兼容」理解成「现在就把 Front Agent 写进去」

✅ 正确理解：
    当前架构不要**阻止**未来增加 Front Agent

    V1 简单实现 + 边界清楚 + 接口抽象稳定 = 未来可扩展
    ≠ V1 + 大量 Future abstraction + 提前实现 = 过度设计
```

## 3. Origin ≠ Creator（`SETTLED` —— 本主题最重要的概念）

### 3.1 定义

| | 回答 |
|---|---|
| **Origin** | 这个 Task **最初为什么存在、需求从哪里产生**？ |
| **Creator** | 是谁把这个**逻辑需求正式创建成一个 Task**？ |

### 3.2 关键例子

```
Human 提出了需求
        ↓
Agent A 理解需求并正式创建 Task
```

则：

```
Origin  = HUMAN_DEMAND
Creator = Agent A
```

> ✅ **这是合法且正常的。**

### 3.3 明确禁止

```
❌ 不要设计成 Creator = Origin
❌ 不要用一个 owner 字段同时承担 Origin / Creator / Funding / Responsible
```

---

## 4. Origin 的三类（`SETTLED` 语义 / `OPEN` 枚举形式）

第一版至少需要**逻辑上**能够表达：

| Origin | 例子 |
|---|---|
| **`HUMAN_DEMAND`** | Human：「帮我开发一个 CSV 分析工具。」 |
| **`AGENT_INITIATIVE`** | Agent A 观察市场后认为某个任务有经济机会，决定主动投资 |
| **`CHILD_TASK_DERIVATION`** | `Task-001` → Agent A 需要数据库设计 → `Task-002` |

> ⚠️ **具体枚举形式可以在实现阶段调整。**
> **本次首先固定逻辑语义，不要急着设计代码 Enum。**

---

## 5. Root Task（`SETTLED`）

### 5.1 定义

**Root Task 是一个 Task Lineage 的最顶层 Task。**

```
Task-001
    parent = null
    root   = Task-001      ← 它自己就是 Root
```

### 5.2 通常来自 Human Demand，但**不是永久硬限制**（`SETTLED`）

> 🔒 **Human Demand ≠ Task；Human ≠ Task Creator。** 见 §2.4。

第一版的现实默认：

```
Human（Demand Provider）
  ↓
Demand
  ↓
Agent（Interpretation / Structuring / Decision）
  ↓
Root Task            ← Creator = Agent
```

**同时允许**：

```
Agent → AGENT_INITIATIVE → Root Task
```

> **两种情况下的 Creator 都是 Agent。**
> 区别只在 **Demand Source**：一个是 Human，另一个是 Agent 自己。

> ✅ 应该写：
> **「Root Task 通常来自 Human Demand；
> 未来允许具备足够经济能力和系统权限的 Agent 发起自己的 Root Task。」**
>
> ❌ **不要写**：「Root Task 只能由 Human 创建。」

### 5.3 Agent 主动创建新的 Root Task（`FUTURE`）

这是一个**正式的长期设计方向**：

```
Agent A
    拥有足够经济资源
        +
    发现值得投资的市场机会
        +
    决定承担风险
        ↓
    创建新的 Root Task
```

则：

```
Origin      = AGENT_INITIATIVE
Creator     = Agent A
Parent      = None
Root        = itself
Funding     = Agent A
Responsible = Agent A
```

这代表：

> **Agent 不只是被动执行人类需求，
> 未来可以成为任务和经济活动的发起者。**

> ⚠️ **本次只定义逻辑模型，不实现 Agent 自主创建 Root Task。**

---

## 6. Parent Task 与 Root Task（`SETTLED`）

### 6.1 Parent Task 是**直接**父节点

```
Task-001
  ↓
Task-002
```

则 `Task-002.parent = Task-001`。

它回答：**「这个 Task 是从哪个 Task 直接派生出来的？」**

### 6.2 Parent ≠ Creator

```
Task-002

Parent      = Task-001
Creator     = Agent A
Responsible = Agent B
```

**完全成立。**

### 6.3 Parent ≠ Root

| | 含义 |
|---|---|
| **Parent** | **直接**上一层 |
| **Root** | 整个 Task Lineage 的**最初** Task |

```
Task-001
 └── Task-002
      └── Task-003
```

| Task | Parent | Root |
|---|---|---|
| `Task-001` | `none` | `Task-001` |
| `Task-002` | `Task-001` | `Task-001` |
| `Task-003` | `Task-002` | `Task-001` |

> ❌ **不要把 Root 写成 Parent。**

---

## 7. Child Task 是独立 Task（`SETTLED`）

### 7.1 核心原则

即使：

```
Task-002 ← Task-001
```

它**也不是** `Task-001` 的一个普通函数调用，
而是一个**真正独立的 Task**。

### 7.2 原则上它拥有自己的

- Task Core
- TaskAgreement
- Shared Context
- Workspace（`FUTURE`）
- Execution History
- Verification
- Economic Record

**通过 `Parent` / `Root` 建立任务谱系关系。**

### 7.3 Child Task 默认**不继承** Parent 的整个 Workspace（`SETTLED`）

```
Task-001                    Task-002
  ├── Shared Context          ├── Shared Context
  └── Workspace               └── Workspace
```

**默认**：

```
Task-002 Workspace  ≠  Task-001 Workspace
```

> ❌ **Child Task 不得因为来自 Parent Task，
> 就自动获得 Parent Workspace 的全部资源访问权。**

如果 Parent Task 需要 Child Task 使用某项资源：

> **通过明确的资源引用 / Artifact / Permission 机制共享。
> 不要直接共享整个 Workspace。**

### 7.4 Child 的结果如何回到 Parent（`SETTLED` 逻辑）

```
Child Task
    ↓
Execution
    ↓
Result / Artifact
    ↓
Parent Task
```

**Parent 不需要获得 Child Workspace 的全部访问权。**

例如：

```
Task-002  「设计数据库」
    → Artifact = db-schema-v2

Task-001  引用：
    db-schema-v2
```

而**不是**：

```
❌ Task-001 直接操作 Task-002 Workspace
```

> 具体 Artifact 体系目前仍然属于**未来 Workspace 设计**，
> **不要本次实现。**

---

## 8. Funding / Responsible / Participant（`SETTLED`）

### 8.1 Funding ≠ Responsible（两个问题，两个答案）

> 🔒 **Funding 与 Responsible 是**两个不同的问题**：
> 一个问「谁出钱」，一个问「谁对结果负责」。**
> 两者可以相同，也可以不同（见 §8.3）。

**每个 Child Task 都必须能够逻辑上回答：
「谁为这个 Task 的经济资源承担责任？」**

```
Task-001
  ↓
Agent A
  ↓
创建 Task-002
  ↓
为 Task-002 提供 40 AIC 的任务预算
```

则 `Task-002`：

```
Funding = Parent Task allocation
```

未来**可以**：

```
Funding = Creator's own funds
```

甚至未来支持 **multiple funding sources**。

> ⚠️ **本次不设计复杂融资系统。**

> 🔒 **关键原则**：
> **不要让 Budget 在任务树中无限、无记录地向下透传。**

### 8.2 Funding ≠ Creator

```
Task-002

Creator  = Agent A
Funding  = Parent Task allocation
Responsible = Agent B
```

**完全合理。**

也可能：

```
Creator  = Agent A
Funding  = Agent A              ← 自费
Responsible = Agent B
```

**也合理。**

> ❌ **不要使用 `owner` 把三者混成一个概念。**

### 8.3 Responsible ≠ Creator

**谁对这个 Task 的实际执行结果承担主要责任？**

```
Creator     = Agent A     ← A 创建了任务
Responsible = Agent B     ← B 承接并执行任务
```

> **Responsible 与 Creator 不必相同。**

### 8.4 Participant ≠ Creator ≠ Responsible ≠ Funding Source

Task 还可能有多个 Participant：

```
Agent A · Agent B · Agent C
```

但是：

> 🔒 **`Participant` ≠ `Creator` ≠ `Responsible` ≠ `Funding Source`**
>
> **这些角色必须保持逻辑独立。**

---

## 9. 完整逻辑示例（`SETTLED`）

### 9.1 任务树

```
Human                （Demand Provider —— 提需求，**不建 Task**）
  ↓
Demand
  ↓
Agent A              （Interpretation → **创建** Root Task）
  ↓
Task-001
  ↓
Agent A              （外包）
  ↓
Task-002
  ↓
Agent B
  ↓
Task-003
  ↓
Agent C
```

### 9.2 逻辑属性

| | `Task-001` | `Task-002` | `Task-003` |
|---|---|---|---|
| **Origin** | `HUMAN_DEMAND` | `CHILD_TASK_DERIVATION` | `CHILD_TASK_DERIVATION` |
| **Creator** | Agent A | Agent A | Agent B |
| **Parent** | `None` | `Task-001` | `Task-002` |
| **Root** | `Task-001` | `Task-001` | `Task-001` |
| **Funding** | Root Task budget（源自 Human Demand） | Parent Task allocation | `Task-002` allocation |
| **Responsible** | Agent A | Agent B | Agent C |

### 9.3 三层之后

```
Task-003
  ↓
❌ 不能继续创建 Child Task
```

**这是当前阶段规则。** 见 §10。

---

## 10. 当前结构性硬规则：Maximum Task Depth = 3

> ## ⚠️ `CURRENT` / **TEMPORARY RULE**
>
> **这不是 AI Economy OS 永久不可修改的架构真理。**

### 10.1 规则

```
Layer 1   Root Task
Layer 2   Child Task
Layer 3   Grandchild Task
```

```
Root
 ├── Child
 │    └── Grandchild
 └── Child
```

> **Layer 3 不允许继续派生 Child Task。**

### 10.2 为什么这是**临时**规则（`SETTLED`）

这是当前**实现阶段**的：

> **Temporary Structural Rule**

**不是长期不可改变的哲学。**

未来可以**根据真实运行结果**调整为：

- 2 层
- 4 层
- 5 层
- **动态深度**

> 🔒 **不要把「3 层」写成 AI Economy OS 永久不可修改的架构真理。**
> 必须明确标记：**`CURRENT` / `TEMPORARY RULE`**。

### 10.3 系统真正要防的是**无限结构**，不是策略选择（`SETTLED`）

因此当前硬规则是：

```
Maximum depth = 3
```

而**不是**：

```
❌ 「Agent 不得过度外包」
```

> **因为后者属于策略判断。**

---

## 11. 外包 ≠ 责任自动转移（`SETTLED`）

**必须明确**：Agent 把工作外包给 Child Task，
**并不意味着 Parent Task 的责任自动消失。**

```
Human（Demand）
  ↓
Agent A（创建）
  ↓
Task-001
  ↓
Agent A

A 派发 Task-002 给 B

如果 B 失败：
    Task-002 FAILED
    并不意味着：
    Task-001 自动免除 A 的责任
```

> ### **Parent Task 的承接 Agent
> ### 仍然要对 Parent Task 的最终结果负责。**
>
> **外包转移工作，不自动转移 Parent Task Responsibility。**

---

## 12. 与现有代码的映射（`CURRENT` —— 事实说明）

**当前代码里已经存在的**：

| 逻辑概念 | 现有字段 | 位置 |
|---|---|---|
| **Creator** | `Task.creator` | `core/task.py` |
| **Responsible（近似）** | `Task.contractor` | `core/task.py`（negotiation 后确定） |
| **Participant** | `Task.participants` | `core/task.py` |
| **Budget（金额）** | `Task.budget` / `Task.agreed_price` | `core/task.py` |
| **Initiator（Agreement 侧）** | `TaskAgreementRecord.initiator` | `core/task_agreement.py` |

**当前代码里【不存在】的**：

| 逻辑概念 | 状态 |
|---|---|
| `origin` | ❌ `NOT IMPLEMENTED` |
| `parent` / `parent_task_id` | ❌ `NOT IMPLEMENTED` |
| `root` / `root_task_id` | ❌ `NOT IMPLEMENTED` |
| `funding` / Funding Source | ❌ `NOT IMPLEMENTED` |
| `responsible`（显式字段） | ❌ `NOT IMPLEMENTED`（`contractor` 只是近似） |
| `depth` / `lineage_depth` | ❌ `NOT IMPLEMENTED` |

> ⚠️ **不要在本次新增任何字段。**
> 这一节只是**如实说明现在有什么、没有什么**。

---

## 13. Task Creation ≠ Message（`SETTLED`）

**必须保持**：

```
A → 「帮我看看数据库」        →  只是 **Message**
```

**不是 Task Creation。**

真正的 Child Task：

```
Task Proposal
    ↓
Negotiation / Agreement
    ↓
Formal Task Creation
```

> **尤其如果 B 不接受：
> 不要自动形成有效 Task Agreement。**
>
> **具体流程沿用当前 TaskAgreement / Negotiation 模型。
> 不要重新发明一套 Task Creation 协议。**

---

## 14. Iteration ≠ Child Task（`SETTLED`）

| | 含义 |
|---|---|
| **Iteration** | **同一个 Task** 继续演进。例如 `Task-001: Iteration 1 → COMPLETED → Iteration 2 → REOPEN` |
| **Child Task** | 从一个 Task 中**派生一个新的独立工作单元** |

> **本次不要实现 Iteration，也不要实现 Child Task。
> 只把两者概念边界写清楚。**（Iteration 详见
> [`TASK_WORKSPACE.md`](TASK_WORKSPACE.md) §15.7–§15.10）

---

## 15. 系统边界 vs Agent 策略（`SETTLED`）

> ## **规则限制结构，策略决定行为，激励塑造结果。**

### 15.1 系统硬约束（**只有这些**）

| ✅ 系统负责 |
|---|
| 结构合法 |
| 层级不无限（`Maximum depth = 3`） |
| 责任 / 资金关系**可追踪** |
| 协议合法 |
| 权限合法 |

### 15.2 Agent Strategy / Economic Choice（**系统不得硬编码**）

| ❌ 不要自行规定 |
|---|
| 「Task 超过 X AIC 必须外包」 |
| 「Agent 必须自己完成至少 50%」 |
| 「Child Task 预算不得超过 Parent 的 30%」 |
| 「所有 Task 至少拆一个子任务」 |
| 「Agent 必须寻找最便宜的承包者」 |
| 「中间层不得盈利」 |
| 「Child Task 必须得到 X%」 |
| 「Creator 最多保留 X%」 |

### 15.3 不要在 Task Creation Mechanism 里写策略判断

```python
# ❌ 不要
if outsourcing_roi > X:
    allow

# ❌ 不要
if own_cost < child_cost:
    reject_child_task
```

> 这些判断应该属于 **Agent Strategy / DecisionPolicy / Economic Choice**。

### 15.4 中间层盈利是**合法**的（`SETTLED`）

```
Parent Task Revenue = 100
Child Task Cost     =  60
Creator / Contractor retains 40
```

> ❌ **不能仅因为 Creator 是中间层，就认定这是「抽水」。**

它可能**真实承担了**：

- Task decomposition
- Agent discovery
- Negotiation
- Coordination
- Integration
- Verification
- **Risk**

**这些本身就是经济价值。**

### 15.5 「系统是否允许」与「Agent 是否选择」不是一回事（`SETTLED`）

> 🔒 **不要因为一个行为可能低效，就自动把它定义为非法。**

---

## 16. Open Questions（`OPEN` —— 本次**不回答**）

| # | 问题 |
|---|---|
| 1 | 谁可以创建 Root Task？ |
| 2 | Agent 创建 Root Task 的最低经济条件是什么？ |
| 3 | 谁可以创建 Child Task？ |
| 4 | Child Task 创建是否需要 **Parent Authority**？ |
| 5 | Child Task Funding 是否**只能**来自 Parent Task？ |
| 6 | 是否允许 Agent **自费补贴** Child Task？ |
| 7 | Parent Task 如何看到 Child Task 的**状态**？ |
| 8 | Child Task **失败**时 Parent Task 如何处理？ |
| 9 | Parent Task 是否可以**取消** Child Task？ |
| 10 | Child Task 完成后如何**正式返回** Parent？ |
| 11 | **Reopen / Iteration** 如何与 Lineage 区分？ |
| 12 | **三层限制**未来是否应该动态化？ |

> ⚠️ **不要自行回答以上问题。**
> 尤其注意 §15.5 那条区分。

---

## 17. 未来演进（`FUTURE`）

```
今天（本文档，`SETTLED` 概念 / `NOT IMPLEMENTED` 实现）：
    7 个逻辑角色已定义
    Maximum Depth = 3（TEMPORARY）
    Agent 自主创建 Root Task = 方向已定，未实现
    Child Task = 未实现

下一步（仍未排期）：
    决定 §16 的 Open Questions
    决定 Origin 的枚举形式
    决定 Lineage 的持久化形态

更远：
    Agent 自主创建 Root Task
    Task Lineage 的动态深度
    多来源 Funding
```

### 17.1 最终架构思想

```
                     Task
                       │
       ┌───────────────┼────────────────┐
       │               │                │
     Origin          Creator         Lineage
                                       │
                                 ┌─────┴─────┐
                                 │           │
                               Parent       Root
                                       │
                              ┌────────┴────────┐
                              │                 │
                           Funding        Responsible
                              │                 │
                              └────────┬────────┘
                                       │
                                  Participants
```

```
Human（Demand）/ Agent（Initiative）
     ↓
     ↓  经 Agent Interpretation / Decision
     ↓
Root Task            ← Creator 始终是 Agent
     ↓
Child Task
     ↓
Grandchild Task
        （当前最多 3 layers，TEMPORARY）

规则限制结构，策略决定行为，激励塑造结果。
```

**系统：** 保证 Task 结构合法 · 保证层级不无限 ·
保证责任/资金关系可追踪 · 保证协议成立。

**Agent：** 自己决定是否外包 · 自己决定是否创建 Child Task ·
自己决定预算怎么分配 · 自己决定自己做多少 · 自己决定选择哪个 Agent。

**经济系统** 最终通过 收益 · 成本 · 风险 · 成功率 · 返工 · 时间 · 声誉
等**实际结果**反馈，逐渐塑造 Agent 的策略。
