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
Human → Task → Agent
```

未来会出现：

```
Human
  ↓
Root Task
  ↓
Agent A
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

### 5.2 通常来自 Human，但**不是永久硬限制**（`SETTLED`）

第一版的现实默认：

```
Human → Root Task
```

**未来允许**：

```
Agent → AGENT_INITIATIVE → Root Task
```

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
Human
  ↓
Task-001
  ↓
Agent A
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
| **Funding** | Human / Root Task budget | Parent Task allocation | `Task-002` allocation |
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
Human
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
Human / Agent
     ↓
Root Task
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
