# Task Shared Context / Alignment — Specification

> **状态：`DESIGNED`（已设计，尚未实现）**
>
> 这份文档定义 Task Shared Context 的**数据模型与机制边界**。
> **它不描述已经存在的代码。** 当前代码里**没有** `TaskSharedContext`、
> 没有对应的数据库表、没有 Alignment 实现。
>
> 上游：[`ARCHITECTURE_CONSTITUTION.md`](ARCHITECTURE_CONSTITUTION.md)
> （Memory 四层、Source of Truth、Memory ≠ Alignment 在那里定义）

| | |
|---|---|
| **文档状态** | 长期维护 |
| **实现状态** | **`DESIGNED` — 零实现** |
| **前置阅读** | Constitution §6（Memory 四层）· §7（Memory ≠ Alignment）· §9（Source of Truth） |
| **最后更新** | Step 4E |

---

## 0. 一句话定义

> **Task Shared Context 是「多个参与 Agent 当前共同使用的 Task World Model」。**
>
> 它属于 **Task**，不属于任何 Agent。

### 0.1 它不是什么

| ❌ 不是 | 为什么 |
|---|---|
| `shared_memory.txt` | 那只是「共同存储」，没有权威、来源、版本、对齐语义 |
| Message History 的副本 | 历史是「说过什么」，它是「现在确认了什么」 |
| Agent Private Memory 的汇总 | 私有判断不能自动成为共识 |
| Task Workspace | Workspace 说「实际有哪些文件」，它说「我们认为是什么」 |
| Domain State 的第二份 | 它会**引用** `Task.status`，但**不复制**它 |
| LLM 的 prompt 缓存 | 它是有结构、可验证、带来源的**状态** |

---

## 1. 为什么需要它

### 1.1 问题

> **多 Agent 即使在数据库和协议层完全一致，
> 也可能对「这个任务到底是什么、现在已经决定了什么、
> 什么只是猜测、下一步该做什么」产生不同理解。**

协议保证的是**消息送达**。它**不保证理解一致**。

### 1.2 三条具体症状

| 症状 | 例子 |
|---|---|
| **历史 ≠ 当前状态** | 500 条消息里散落着 12 个决定和 3 次改主意。谁能说出「现在到底用哪个方案」？ |
| **说过 ≠ 确认过** | A 说「我觉得用 B 吧」，B 没回应。A 以为定了，B 以为还在讨论 |
| **猜测 ≠ 事实** | A 推测「那个 API 支持幂等」，B 当成事实去写代码 |

### 1.3 它提供的价值

把**大量历史**压缩成**结构化、可验证、带来源的当前状态**。

> **不能要求 Agent 每次都自己重新阅读几百条 Message，
> 然后自行推断「现在到底决定了什么」。**
>
> Shared Context 的目的正是**替它做这件事**。

---

## 2. 数据模型总览

`DESIGNED` —— 下面是设计，不是代码。

```
TaskSharedContext（某个 Task 的世界模型）
    │
    ├── revision          →  ContextRevision（当前是第几版）
    │
    ├── objective         ┐
    ├── constraints       │
    ├── acceptance_criteria│
    │                     │  这些是「当前状态」的各个面
    ├── facts             │  —— 但每一条都带来源与状态
    ├── decisions         │
    ├── assumptions       │
    ├── open_questions    │
    ├── assignments       │
    ├── current_status    │
    ├── terminology       │
    └── artifacts(refs)   ┘
```

**关键设计**：**不要把所有东西塞成一个 JSON。**

必须明确区分哪些是：

| 类型 | 语义 | 能不能被"推翻" |
|---|---|---|
| **Fact** | 已确认的事实 | 可以被新事实取代（标 `SUPERSEDED`） |
| **Decision** | 共同确认的决定 | 可以被新决定取代（标 `SUPERSEDED`） |
| **Assumption** | **暂时假设** | 会被 `CONFIRMED` / `REJECTED` |
| **Open Question** | **还不知道什么** | 会被回答（然后变 Fact/Decision） |
| **Assignment** | 谁负责什么 | 有生命周期 |
| **Status** | 当前进展 | 持续更新 |

> 不同类型的知识必须可以区分 —— 否则「猜测」和「事实」会混在一起，
> 而这正是多 Agent 系统最危险的一类错误。

---

## 3. 核心类型定义

> 命名遵循项目现有风格（`core/task_agreement.py`、`core/task_execution.py`
> 都用不可变的 frozen dataclass）。**实际命名在实现时确认。**

### 3.1 `TaskSharedContext`

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | `str` | 属于哪个任务（**不是**新建一个 id 体系） |
| `revision` | `int` | 当前版本号，**只增不减** |
| `objective` | `str` | 这个任务要达成什么 |
| `constraints` | `tuple[Constraint]` | 约束 |
| `acceptance_criteria` | `tuple[...]` | 什么算通过 |
| `facts` | `tuple[SharedFact]` | 已确认事实 |
| `decisions` | `tuple[DecisionRecord]` | 已确认决定 |
| `assumptions` | `tuple[AssumptionRecord]` | 未确认假设 |
| `open_questions` | `tuple[OpenQuestion]` | 还不知道什么 |
| `assignments` | `tuple[AssignmentRecord]` | 分工 |
| `current_status` | `str` | 当前进展 |
| `terminology` | `Mapping[str, str]` | 术语约定 |
| `artifact_refs` | `tuple[str]` | 共享产物的**引用**（不是产物） |

> 🔒 **`task_id` 不是新身份。** 它引用 `Task.task_id`。
> Shared Context **不创造第二个任务身份体系**。

### 3.2 `ContextRevision`

> **Shared Context 不能被静默修改。**

| 字段 | 类型 | 说明 |
|---|---|---|
| `revision_number` | `int` | 1, 2, 3, …（**单调递增**） |
| `created_at` | `str` | ISO 时间戳 |
| `created_by` | `str` | 哪个 Agent（或哪个自动流程）产生的 |
| `parent_revision` | `Optional[int]` | 从哪一版演化而来 |
| `change_reason` | `str` | **为什么**要改 |
| `changes` | `tuple[ContextChange]` | 这一版包含哪些变更 |

**两条硬规则**：

1. **只增不删。** 如果 Revision 18 取代了 17，
   **不能把 17 删除** —— 必须保留历史。
2. **每个 Revision 必须能回答「当前共享世界模型来自什么」。**
   一个 Agent 看到 `Revision 17` 时，应该能追溯它的来历。

### 3.3 `ContextChange`

> **不要让 Agent 直接 `context["decision"] = "B"`。**

**正确路径**：

```
Agent
  ↓
ContextChange Proposal
  ↓
Validation / Alignment
  ↓
New Revision
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `change_id` | `str` | 变更标识 |
| `task_id` | `str` | 属于哪个任务 |
| `proposed_by` | `str` | 谁提的 |
| `change_type` | `str` | 见下表 |
| `target` | `str` | 改哪个字段 / 哪一条记录 |
| `expected_value` | `Any` | **预期当前值**（用于冲突检测） |
| `new_value` | `Any` | 想改成什么 |
| `reason` | `str` | **为什么**（不能为空） |
| `source` | `str` | 依据是什么（消息 / 观察 / 外部） |
| `created_at` | `str` | |
| `status` | `str` | 见下 |

**`change_type`（初步，不要现在过度扩张）**：
`ADD` · `MODIFY` · `SUPERSEDE` · `RESOLVE`（回答问题）· `CONFIRM`（确认假设）· `REJECT`

**`status`**：
`PROPOSED` · `ACCEPTED` · `REJECTED` · `SUPERSEDED`

> `expected_value` 的作用类似乐观锁：如果提案基于的旧值与当前不符，
> 说明**上下文在你提案期间变了** —— 那应该是一个冲突，不是静默覆盖。

### 3.4 `DecisionRecord`

> **多个 Agent 共同做出的关键决定，必须与普通事实区别。**

| 字段 | 类型 | 说明 |
|---|---|---|
| `decision_id` | `str` | |
| `statement` | `str` | 决定了什么（如「选择 parser 方案 B」） |
| `proposed_by` | `str` | 谁提的 |
| `accepted_by` | `tuple[str]` | 谁确认的（**至少一个非提案人**） |
| `reason` | `str` | **为什么这么决定** |
| `created_at` | `str` | |
| `supersedes` | `Optional[str]` | 取代了哪条旧决定 |
| `status` | `str` | `ACCEPTED` / `SUPERSEDED` |

> 🔒 **未来 Agent 不应该只看到「之前决定用 B」，
> 而应该能知道「为什么决定 B」。**
>
> 这是「decision」和「有人说过一句话」的根本区别。

**`accepted_by` 至少一个非提案人** 是**对齐的最低要求** ——
它保证了「一个人自己说了算」不算共同决定。

### 3.5 `AssumptionRecord`

> **必须把「已确认事实」和「暂时假设」区分开。**

| 字段 | 类型 | 说明 |
|---|---|---|
| `assumption_id` | `str` | |
| `statement` | `str` | 假设什么 |
| `held_by` | `str` | 谁提出的 |
| `reason` | `str` | 为什么这样假设 |
| `status` | `str` | `OPEN` / `CONFIRMED` / `REJECTED` / `SUPERSEDED` |
| `created_at` | `str` | |
| `resolved_at` | `Optional[str]` | |
| `resolved_by` | `Optional[str]` | |

**例子**：

```
Fact:        Python version = 3.13
Assumption:  这个第三方 API 支持 idempotent request
```

> 🔒 **不能让一个 Agent 的猜测悄悄升级成系统事实。**
>
> Assumption 变 Fact 必须是一次**显式的 `CONFIRM` 变更**，
> 带来源（谁确认的、依据是什么）。

### 3.6 `OpenQuestion`

> **Task Shared Context 应该允许明确记录：当前还不知道什么。**

| 字段 | 类型 | 说明 |
|---|---|---|
| `question_id` | `str` | |
| `question` | `str` | 如「空 CSV 应该返回 `[]` 还是报错？」 |
| `owner` | `Optional[str]` | 谁负责回答 |
| `status` | `str` | `OPEN` / `ANSWERED` / `WITHDRAWN` |
| `created_at` | `str` | |
| `answer` | `Optional[str]` | 回答内容 |
| `answered_by` | `Optional[str]` | |
| `answered_at` | `Optional[str]` | |

> 🔒 **这对多 Agent 非常重要。**
>
> 因为：**「不知道」比「错误地以为自己知道」安全得多。**
>
> 一个显式的 Open Question 让所有人知道「这里有不确定性」；
> 一个缺失的 Open Question 让每个 Agent 各自猜一个答案，
> 然后在某个时刻发现猜的不一样。

### 3.7 `AssignmentRecord`

> **多个 Agent 协作时，必须明确：谁负责什么。**

| 字段 | 类型 | 说明 |
|---|---|---|
| `assignment_id` | `str` | |
| `agent_id` | `str` | 谁负责 |
| `responsibility` | `str` | 负责什么（如 `parser`、`tests`、`documentation`） |
| `status` | `str` | `ACTIVE` / `DONE` / `RELEASED` / `BLOCKED` |
| `assigned_at` | `str` | |
| `assigned_by` | `str` | 谁分的（可能是发起方） |
| `notes` | `str` | |

> 🔒 **不要只依赖聊天记录。**
>
> 「谁负责什么」如果在消息历史里，那么每次有人想知道都得重新读一遍。
>
> **`FORBIDDEN NOW`** —— 不要提前实现完整任务调度系统。
> Assignment 只是**记录**，不是调度器。

### 3.8 `SharedFact`

| 字段 | 类型 | 说明 |
|---|---|---|
| `fact_id` | `str` | |
| `statement` | `str` | 如「Python version = 3.13」 |
| `source` | `str` | **依据是什么**（外部观察 / 领域状态 / 确认） |
| `confirmed_by` | `Optional[str]` | 谁确认的 |
| `created_at` | `str` | |
| `status` | `str` | `ACTIVE` / `SUPERSEDED` |
| `supersedes` | `Optional[str]` | |

> 🔒 **Fact 必须带 `source`。**
> 否则它和 Assumption 就没有区别了。

---

## 4. Revision 语义

### 4.1 只增不删

```
Revision 1  ──┐
Revision 2  ──┤  全部保留
Revision 3  ──┤
   ...        │
Revision 17 ──┤
Revision 18 ──┘  ← 当前
```

**Revision 18 取代 17 时，17 不能被删除。**
必须保留历史，才能回答「当时为什么那样认为」。

### 4.2 谁会创建新 Revision

| 变更类型 | 谁产生 Revision | 需要 Alignment 吗 |
|---|---|---|
| **自动事实**（如 `Task.status` 变了） | **系统**（自动同步） | ❌ 不需要 |
| **契约约束**（预算、验收要求） | **Task / Agreement / User** | ❌ 不由 Agent 投票 |
| **协作决策** | **Alignment 流程** | ✅ 需要 |
| **个人信念** | —— | 留在 **Private Memory**，不进 Shared Context |

> 🔒 **不要强制所有变化都必须全员投票。**
>
> 「Domain State 说任务已经 COMPLETED」这件事**不需要投票** ——
> 它是客观事实。给它「投票」反而是错的：
> 如果投票结果是「还没完成」，系统该相信谁？

---

## 5. Alignment Protocol（初步设计）

> **`DESIGNED` —— 先设计，不实现完整协议。**

### 5.1 基本模型

```
Agent A
   │
   │  ① PROPOSE_CHANGE（带 reason + source + expected_value）
   ▼
Task Shared Context
   │
   │  ② Validation
   │     · 提案人是否是任务参与方？
   │     · 变更类型是否需要对齐？
   │     · expected_value 与当前值是否一致？（冲突检测）
   ▼
Alignment
   │
   │  ③ 需要对齐的变更 → 通知其他参与方
   ▼
Agent B
   │
   │  ④ ACCEPT / DISPUTE
   ▼
Context Revision（⑤ 产生新版本）
```

### 5.2 三类变更的不同处理

| 变更类型 | Validation | Alignment | 结果 |
|---|---|---|---|
| 自动事实 | 来源必须是 Domain State | 跳过 | 直接产生 Revision |
| 契约约束 | 来源必须是 Task/Agreement/User | 跳过 | 直接产生 Revision |
| 协作决策 | 提案人是参与方 | **需要** ACCEPT | ACCEPT 后产生 Revision |
| 个人信念 | —— | —— | **拒绝进入 Shared Context** |

### 5.3 冲突处理（原则）

`DESIGNED` —— 只定义原则，不定义算法：

1. **Domain State 优先。**
   Shared Context 与 Task 状态矛盾时，以 Task 为准，
   并把 Shared Context 标为**需要修正**（不是静默改掉）。
2. **不删除，只标记。**
   旧 Decision 被取代 → `SUPERSEDED`，不删。
3. **记录争议。**
   无法达成一致 → 那本身是一个 **Open Question / Dispute**，
   **不是**「谁先写谁赢」。
4. **`expected_value` 不匹配 = 冲突。**
   不是静默覆盖，而是回到提案人。

### 5.4 明确不做的事

> 🔒 **不要把 Shared Context 做成「民主投票系统」。**

❌ 多数票
❌ 所有人必须批准
❌ 复杂 quorum
❌ Blockchain consensus
❌ CRDT
❌ 分布式共识算法

**当前需要的是**：清晰的 `authority` + `provenance` + `revision` +
`conflict handling`。**而不是复杂共识算法。**

---

## 6. 与相邻层的边界

### 6.1 与 Agent Private Memory

**可以**：

```
Private Observation
    ↓
Proposal
    ↓
Alignment
    ↓
Shared Context
```

**不可以**：

- ❌ Private Memory 直接写 Shared Context
- ❌ 一个 Agent 的私下判断自动变成共识
- ❌ Shared Context 反向读取其他 Agent 的 Private Memory

### 6.2 与 Communication

```
Message
   ↓
可能成为 Context Proposal
   ↓
Alignment
   ↓
Context Revision
```

**不是**：

```
Message
   ↓
自动写 Shared Context        ❌
```

> 🔒 **Message Journal 是历史记录。
> Task Shared Context 是当前共享世界模型。**
> 两者不能划等号。

### 6.3 与 Task Core

| Task Core 负责 | Shared Context **不**负责 |
|---|---|
| `status` | ❌ 不复制一份 status |
| `budget` / `agreed_price` | ❌ 不复制一份价格 |
| `contractor` | ❌ 不复制一份承接方 |
| `task_id` | ❌ 不创造第二个任务身份 |
| 生命周期 | ❌ 不定义自己的状态机 |

> 🔒 **Shared Context 可以引用 Domain State，
> 但不能成为 Domain State 的第二个来源。**
>
> 如果两边各存一份 `status`，它们**一定会漂移** ——
> 而「哪个是真的」将无法回答。

### 6.4 与 Task Workspace

| | 回答 | 例子 |
|---|---|---|
| **Shared Context** | 我们共同**认为**这个任务现在是什么 | `Decision: 使用 parser B` |
| **Task Workspace** | 我们**实际**共享什么数据 / 文件 / 代码 | `parser.py` · `tests/` · `fixtures/` |

**两者相互关联，但不能混为一体。**

Shared Context 里的 `artifact_refs` **只是引用**
（和现在 `result_ref` 的语义一致：逻辑标识，不是路径、不是权限）。

### 6.5 与 Agent Economy

**可以放进 Shared Context**：与**这个 Task 相关**、
需要**共同知道**的经济事实，例如 `agreed_price`（来自 `TaskAgreement`）。

**不可以**：Agent B 的全部余额。

> 🔒 **Shared Context 不应该拥有 Agent 的全部经济状态。**
> 余额是 Agent 的私事；它只在这个任务需要共同知道时才出现。

### 6.6 与 Context Snapshot

| | 关系 |
|---|---|
| **Shared Context** | Source of Truth（任务知识层） |
| **Context Snapshot** | 派生物 —— Shared Context 在某一刻、给某个 Agent 的**可消费视图** |

Snapshot **不是**新的 Source of Truth。
它**必须带 `revision`**（否则不知道它是哪一刻的视图）。

---

## 7. 可以回答的问题（验收）

> 只读这份文档，应该能回答：

| 问题 | 答案位置 |
|---|---|
| Shared Context 是什么？不是什么？ | §0 |
| 为什么需要它？ | §1 |
| 它有哪些组成部分？ | §2、§3 |
| 不同类型的知识如何区分？ | §2（表格） |
| 一个 Decision 如何产生？ | §3.4、§5.1 |
| 一个 Assumption 如何变成 Fact？ | §3.5 |
| 一个旧 Decision 如何被替代？ | §4.1、§5.3 |
| Revision 为什么只增不删？ | §4.1 |
| 哪些变更需要对齐？ | §4.2、§5.2 |
| 冲突怎么办？ | §5.3 |
| 它和 Message History / Workspace / Private Memory / Domain State 的边界？ | §6 |
| 当前实现了吗？ | 顶部状态栏 —— **没有** |

---

## 8. 实现前必须确认的问题

> 这一节是**留给下一阶段的任务清单**，不是本阶段要做的事。

1. **表结构**：`task_shared_context` / `context_revisions` / `context_changes`？
   还是「一次一个 JSON 快照」？—— 需要根据真实查询模式决定。
2. **是否需要 `ContextChange` 的独立生命周期**，
   还是「提案即变更，只在 Revision 里留痕」？
3. **Alignment 的参与者是谁** —— 任务参与方？还是显式指定？
4. **自动事实同步** 是「事件驱动」还是「读取时对齐」？
5. **`expected_value` 的冲突**要不要像 `TaskStore.save()` 那样
   抛一个明确的异常（而不是静默合并）？
6. **`artifact_refs` 与 `result_ref` 的关系** ——
   是同一个概念，还是两个？

> **本阶段不要回答这些。** 等实现阶段根据真实需求决定。
