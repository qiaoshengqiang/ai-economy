# ADR-0023：Task Origin / Creator / Lineage / Funding 是五个独立概念

- **状态**：Accepted（逻辑架构；**实现为 `FUTURE`**）
- **时间**：Task Origin & Task Lineage
- **相关**：ADR-0019（Shared Context 与 Alignment）· ADR-0020（Workspace 共享资源模型）· ADR-0022（Lazy Provisioning / Lifecycle / Human）
- **规范**：[`TASK_ORIGIN_LINEAGE.md`](../TASK_ORIGIN_LINEAGE.md)

## Context

到目前为此，Task 已经具备 Task Core · Discovery · Negotiation ·
TaskAgreement · Execution · Verification · Shared Context ·
Workspace（`FUTURE`）。

但**从未正式回答过**四个不同的问题：

1. 这个 Task **为什么会出现**？
2. **谁**把它正式创建成 Task？
3. 它**从哪个 Task 派生**、属于**哪条谱系**？
4. **谁出钱**、**谁负责执行结果**？

现在的代码只有一个 `Task.creator` —— 它在「Human 提需求、Agent A 建 Task」
这个最简单的场景下**看起来够用**。但只要出现：

```
Human 提需求 → Agent A 建 Root Task → A 外包给 B → B 再外包给 C
```

`creator` 就**同时被要求表达四个不同的东西**，而它们在每一层都不同。

同时，架构讨论里出现了「三层限制」「外包」「中间层利润」等话题，
必须**明确划清**哪些是系统硬约束、哪些是 Agent 自己的经济选择 ——
否则很容易把策略判断写死进系统。

## Decision

### 一、正式定义七个逻辑概念

| 概念 | 回答 |
|---|---|
| **Origin** | 为什么这个 Task 会出现？ |
| **Creator** | 谁**正式创建**了它？ |
| **Parent** | 它**直接从哪个 Task 派生**？ |
| **Root** | 它属于**哪棵 Task Lineage**？ |
| **Funding** / Economic Responsibility | 谁承担它的**经济资源**？ |
| **Responsible Agent** | 谁对**实际执行结果**负责？ |
| **Participant** | 谁**参与**这个 Task？ |

> 🔒 **绝对不要把这些全部压缩成一个 `owner` 字段。**

### 二、Origin ≠ Creator

**这是本主题最重要的概念。**

```
Human 提出需求
        ↓
Agent A 理解需求并正式创建 Task

Origin  = HUMAN_DEMAND
Creator = Agent A
```

**这合法且正常。**

❌ 不要设计成 `Creator = Origin`。

### 三、Origin 的三类（语义已定，枚举形式留待实现）

| Origin | 例子 |
|---|---|
| `HUMAN_DEMAND` | Human：「帮我开发一个 CSV 分析工具。」 |
| `AGENT_INITIATIVE` | Agent A 发现经济机会，决定主动投资 |
| `CHILD_TASK_DERIVATION` | `Task-001` → 需要数据库设计 → `Task-002` |

### 四、Parent ≠ Root

| | 含义 |
|---|---|
| **Parent** | **直接**上一层 |
| **Root** | 整个 Lineage 的**最初** Task |

```
Task-001 → Task-002 → Task-003

002: parent=001, root=001
003: parent=002, root=001
```

### 五、Child Task 是**独立** Task

即使 `Task-002 ← Task-001`，它**也不是**一个函数调用，而是：
拥有自己的 Task Core · TaskAgreement · Shared Context ·
Workspace（`FUTURE`）· Execution History · Verification · Economic Record。

**Child Task 默认不继承 Parent 的整个 Workspace。**
需要共享时通过**明确的资源引用 / Artifact / Permission 机制**。

**结果回传**：Child 产出的 Artifact 被 Parent **引用**，
而不是 Parent 直接操作 Child Workspace。

### 六、Funding ≠ Creator、Responsible ≠ Creator、Participant 独立

```
Task-002
    Creator     = Agent A
    Funding     = Parent Task allocation
    Responsible = Agent B
    Participants = {A, B, ...}
```

**完全成立。**

> 🔒 **不要让 Budget 在任务树中无限、无记录地向下透传。**

### 七、外包 ≠ 责任自动转移

```
Human → Task-001 → Agent A
A 派发 Task-002 给 B
B 失败 → Task-002 FAILED
    ≠  Task-001 自动免除 A 的责任
```

> **Parent Task 的承接 Agent 仍然要对 Parent Task 的最终结果负责。**
>
> **外包转移工作，不自动转移 Parent Task Responsibility。**

### 八、结构性硬规则：Maximum Task Depth = 3

```
Layer 1 Root · Layer 2 Child · Layer 3 Grandchild
Layer 3 不允许继续派生
```

> ## ⚠️ 这是 **`CURRENT` / TEMPORARY RULE**，不是永久架构真理。

未来可以根据**真实运行结果**调整为 2 / 4 / 5 层或动态深度。

### 九、Agent 可以主动创建 Root Task（`FUTURE`）

```
Agent A + 足够经济资源 + 发现市场机会 + 决定承担风险
    ↓
创建新的 Root Task（Origin = AGENT_INITIATIVE）
```

> **Agent 不只是被动执行人类需求，未来可以成为任务与经济活动的发起者。**
>
> 但**本次只定义逻辑模型，不实现。**

### 十、系统边界 vs Agent 策略

> ### **规则限制结构，策略决定行为，激励塑造结果。**

**系统只负责**：结构合法 · 层级不无限 · 责任/资金关系可追踪 ·
协议合法 · 权限合法。

**系统不得硬编码**（这些属于 Agent Strategy / DecisionPolicy / Economic Choice）：

❌ 「Task 超过 X AIC 必须外包」 · ❌ 「Agent 必须自己完成至少 50%」 ·
❌ 「Child Task 预算不得超过 Parent 的 30%」 ·
❌ 「所有 Task 至少拆一个子任务」 ·
❌ 「Agent 必须寻找最便宜的承包者」 ·
❌ 「中间层不得盈利」 · ❌ 「Child Task 必须得到 X%」 ·
❌ 「Creator 最多保留 X%」

也不要在 Task Creation Mechanism 里写：

```python
if outsourcing_roi > X: allow        # ❌
if own_cost < child_cost: reject     # ❌
```

> **系统真正要防的是无限结构，不是策略选择。**
> 所以硬规则是 `Maximum depth = 3`，
> 而不是「Agent 不得过度外包」。

### 十一、中间层盈利是合法的

```
Parent Revenue = 100, Child Cost = 60, Creator retains 40
```

❌ **不能仅因为 Creator 是中间层就认定这是「抽水」。**

它可能真实承担了 Task decomposition · Agent discovery · Negotiation ·
Coordination · Integration · Verification · **Risk** —— **这些本身就是经济价值**。

### 十二、Task Creation ≠ Message

```
A →「帮我看看数据库」        →  只是 Message

真正 Child Task：Task Proposal → Negotiation / Agreement → Formal Creation
```

**B 不接受就不要自动形成有效 TaskAgreement。**
**沿用现有 TaskAgreement / Negotiation 模型，不要重新发明协议。**

### 十三、Iteration ≠ Child Task

| Iteration | Child Task |
|---|---|
| **同一个 Task** 继续演进 | 派生一个**新的独立**工作单元 |

本次**两者都不实现**。

### 十四、与现有代码的映射（**不改代码**）

| 逻辑概念 | 现有字段 |
|---|---|
| Creator | `Task.creator` |
| Responsible（近似） | `Task.contractor` |
| Participant | `Task.participants` |
| Budget | `Task.budget` / `Task.agreed_price` |

**不存在**：`origin` · `parent` · `root` · `funding` · `responsible` · `depth`。

> 本次**不新增任何字段**。

## Consequences

**正面**：

- 「谁创建 / 谁出钱 / 谁负责 / 从哪来」四个问题终于**各自有答案**
- 外包场景下**责任链不会断**（§七）
- 结构限制明确（深度 3）→ 不会出现无限递归的任务树
- 系统与策略的边界写清楚 → **不会把 Agent 的经济选择写死成系统规则**
- 中间层盈利被明确为合法 → 不会误伤真实价值创造

**负面 / 代价**：

- 七个概念在未来实现时需要**至少五个独立字段**（+ 关系），
  比一个 `owner` 复杂
- **深度限制是临时的** —— 未来调整时可能影响已有数据
- `Responsible` 与现有 `contractor` **只是近似**，
  未来需要明确两者是否合并（当前 `OPEN`）
- 12 个 Open Questions 未答 → 实现前仍需一轮讨论

**因此不能做的事**：

- ❌ 不许把七个概念压缩成一个 `owner`
- ❌ 不许设计成 `Creator = Origin`
- ❌ 不许把 Root 写成 Parent
- ❌ 不许让 Child Task 自动继承 Parent Workspace
- ❌ 不许让 Budget 无限向下透传
- ❌ 不许因为外包就免除 Parent 承接方的责任
- ❌ 不许把「3 层」写成永久架构原则
- ❌ 不许把 Agent 策略（外包 ROI / 利润上限 / 自做比例）硬编码进系统
- ❌ 不许把 Message 当成 Task Creation
- ❌ 不许自行回答 §16 的 12 个 Open Questions

## Future evolution

```
今天（本文档）：
    七个逻辑概念已定义（SETTLED）
    Maximum Depth = 3（TEMPORARY）
    Agent 自主创建 Root Task = 方向已定，未实现
    Child Task = 未实现

下一步：决定 12 个 Open Questions + Origin 枚举形式 + Lineage 持久化形态

更远：Agent 自主创建 Root Task · 动态深度 · 多来源 Funding
```

## Guard

文档一致性守卫（含反向测试）必须能抓：

| 违规 | |
|---|---|
| `Origin = Creator`（被合并） | ✅ |
| `Parent = Creator` | ✅ |
| `Root = Parent` | ✅ |
| `Funding = Creator` | ✅ |
| `Responsible = Creator` | ✅ |
| 把 3 层规则改成 4 层 | ✅ |
| 把「暂时三层规则」写成「永久架构原则」 | ✅ |

**不使用「某个词出现」式 Guard。**
优先：结构化 Markdown parsing · 明确规范句匹配 · 概念独立性检查。
并遵守已积累经验（ADR-0018）：避免否定盲 · 多处关键词 ·
章节错位 · 空集合 continue · substring false positive。
