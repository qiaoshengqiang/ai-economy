# COMMUNICATION_MODEL.md — Communication 是 Agent 的基础能力

> **状态：架构设计冻结（`SETTLED`）／实现为 `FUTURE`（Conversation 等）**
>
> 上游：[`AGENTS.md`](../../AGENTS.md) → [`PROTOCOL.md`](../../PROTOCOL.md)
> → [`ARCHITECTURE_CONSTITUTION.md`](ARCHITECTURE_CONSTITUTION.md) → **本文档**

| | |
|---|---|
| **实现状态** | 基础 Message **`CURRENT`** · Conversation / Inbox / Economy **`FUTURE`** |
| **最后更新** | Read 与 communicate 的架构设计 |

> **Inbox 的实施设计**：
> [`implementation/AGENT_INBOX_V1_DESIGN.md`](implementation/AGENT_INBOX_V1_DESIGN.md)
> （Phase 0/1/2/3/4 `CURRENT`；Phase 5 `DESIGNED`）。

---

## 0. 三条核心原则（`SETTLED`）

> ## **Communication 是 Agent 的基础能力，不以 Task 为前提，也不以 Workspace 为前提。**
>
> ## **Task 是 Communication 的一个重要使用场景，而不是它存在的前提。**
>
> ## **Agent 可以异步工作，但不能异步丢失通信与事实。**

### 0.1 `Communication ≠ Task Message Bus`

```
❌ 旧理解：  Task → Message → Agent
✅ 正式定位：Agent → Communication
                    ├── Task-related communication
                    ├── General conversation
                    ├── Knowledge exchange
                    ├── Negotiation
                    ├── Economic communication
                    └── Future social interaction
```

> **Communication 是 Agent Runtime 的基础设施。**

### 0.2 Agent 可以因为什么通信（`SETTLED`）

Task Work · Knowledge Exchange · Problem Solving · Negotiation ·
Economic Activity · Learning · General Discussion · Social Interaction ·
Relationship Building · 其他 Agent-to-Agent Conversation

---

## 1. Message 不要求绑定 Task（`SETTLED`）

### 1.1 工作消息可以携带上下文

```
A → B

我修改了 backend/api.py，可能影响你的 frontend/page.tsx。最新版本 R18。
```

可携带：`task_id` · `workspace_id` · `resource_id` · `revision`

### 1.2 普通交流可以什么都不带

```
A → B

你最近如何处理长期记忆？我正在研究这个问题。
```

```
task_id      = null
workspace_id = null
resource_id  = null
```

> ✅ **依然是合法的 Agent-to-Agent Communication。**
>
> 🔒 **不要要求每条 Message 都必须绑定 Task。**

---

## 2. Conversation（`SETTLED` 概念 / `FUTURE` 实现）

```
Conversation C001
    Participants: A, B
        M001  A → B
        M002  B → A
        M003  A → B
```

| 情况 | `task_id` |
|---|---|
| 工作协作 | `T1` |
| 普通 Agent conversation | `null` |

> **二者使用同一个 Communication 基础设施。**
>
> ⚠️ 当前**不要求**完整实现 `Conversation Manager` ——
> 只需在架构上**不要把 Communication 锁死成「一条消息 = 一次 Task 通知」**。

### 2.1 现有协议已经支持（`CURRENT` —— 事实）

```
Message 字段：
    from_agent · to_agent · message_type · payload
    message_id · timestamp · conversation_id · reply_to
    trace_id · protocol_version · extensions

task_id 是 payload 派生的 **可选** 属性（Optional[str]）
```

> ✅ **`conversation_id` 已存在，`task_id` 已经是可选的。**
> **本文档不要求修改协议**（见 §8）。

---

## 3. Workspace Event ≠ Agent Message（`SETTLED` —— 本轮重点）

| | Workspace Event | Agent Message |
|---|---|---|
| **谁产生** | **系统自动** | **Agent 主动** |
| **回答** | **发生了什么？** | **我认为这个变化为什么与你有关？** |
| **性质** | **Objective Change Fact** | **Agent Judgment / Intent / Communication** |
| **例子** | 「api.py changed R17 → R18」 | 「这个修改会影响你的 frontend」 |

> 🔒 **二者不能混成一个机制。**

### 3.1 🔒 不要自动广播

```
❌ A 连续改 utils.py / README.md / config.py / test_a.py
   → 系统自动向所有 Agent 发四条消息

✅ Workspace Event → 自动记录所有重要资源变化
   Agent          → 自己判断是否需要通知其他 Agent
```

> **这样不会造成 Agent Communication Flood。**

### 3.2 🔒 系统不判断「这个修改一定影响谁」

```
第一版：系统记录变化，Agent 自己判断语义影响。

A 判断会影响 B → A → B Message
A 判断不影响   → 不需要 Message
```

未来可建立 **Dependency Graph**（甚至 AI 推断）—— **当前不要实现**。

---

## 4. Agent 主动通知（`SETTLED`）

```
A 正在修改 backend/api.py
      ↓
A 发现该变化可能影响 B
      ↓
A 完成修改 → 产生 Revision R18
      ↓
A 主动向 B 发送 Message
```

消息可以包含：

```
sender · receiver
task_id（可选）· workspace_id（可选）
resource_id（可选）· revision（可选，但资源变更通知中推荐）
message · timestamp
```

> **消息内容由 Agent 自己决定。**
>
> 🔒 **系统负责记录变化。Agent 自己负责判断是否值得主动沟通。**

---

## 5. Runtime / Inbox / Checkpoint（`SETTLED`）

### 5.1 现实限制

```
B → LLM Thinking
        ← A → B Message 到达
```

> 🔒 **不要要求 B 的当前 LLM invocation 立即中断并回答。**

### 5.2 正确模型

```
B Runtime
├── LLM Execution
└── Message / Event Inbox        ← 由 **Runtime** 持有
```

```
A 的消息 → B Inbox
      ↓
B Runtime Checkpoint
      ↓
检查 Inbox
      ↓
新的 Message 加入下一轮 Context
      ↓
B 再决定是否回应 / 修改工作
```

### 5.3 🔒 两条硬规则

> **① Runtime 可以持续接收通信，而当前 LLM invocation
> 不需要实时响应每一条消息。**
>
> **② Communication Inbox 必须由 `Runtime` 持有，
> 而不是由 `LLM Invocation` 本身持有。**

**因为**：LLM invocation 可能**长时间运行**，而 Runtime 应该持续维护
`Message` · `Event` · `Task State` · `Workspace State`。

### 5.4 🔒 Message 不能因为 LLM 正在运行而丢失

即使 B 当时是 `THINKING` 或 `TOOL_EXECUTION`：

```
Message → Inbox      ← **必须持久化，不会消失**
```

可与已有 `Runtime` / `Heartbeat` / `Event Wakeup` 结合。

---

## 6. Communication 与 Workspace 解耦（`SETTLED`）

```
Agent
│
├── Communication
│     ├── Task Conversation
│     ├── General Conversation
│     └── Agent-to-Agent Message
│
└── Workspace
      ├── Resource
      ├── Controller
      ├── Lock
      └── Handoff
```

> **Communication 可以「引用」Workspace**（`resource = backend/api.py` · `revision = R18`）
>
> 🔒 **但 Communication 本身不能直接修改 Workspace。**
> **资源修改仍必须经过 Workspace / Workspace Gateway。**

---

## 7. 协作时四个概念各承担什么（`SETTLED`）

```
A 修改 backend/api.py
      ↓
   Revision R18                    ← 版本事实
      ↓
   Workspace Event:                ← 系统自动记录的变化
   「api.py changed R17 → R18」
      ↓
   A 判断影响 B
      ↓
   Agent Message:                  ← Agent 的主观判断 / 协作意图
   「这个修改会影响你的 frontend。」
      ↓
   B Inbox
      ↓
   B Checkpoint
      ↓
   B 重新读取 R18
```

| 概念 | 回答 |
|---|---|
| **Revision** | 当前资源处于**什么版本**？ |
| **Workspace Event** | **发生了什么**？ |
| **Agent Message** | **我认为这件事为什么与你有关**？ |
| **Task Shared Context** | 大家以后应该**共同知道什么**？ |

> 🔒 **不要混在一起。**

---

## 8. Protocol 影响 —— **本轮不需要修改协议**（`CURRENT` 事实）

规范要求确认三件事，实测结果：

| # | 检查项 | 结果 |
|---|---|---|
| 1 | Communication 是否被设计成「必须绑定 Task」？ | ❌ **没有**。`task_id` 是 **payload 派生的可选**属性（`Optional[str]`），不在必填字段里 |
| 2 | Message 是否允许 `task_id = null` / `workspace_id = null`？ | ✅ **允许** —— 二者都不在必填字段中 |
| 3 | 是否有 `sender` / `receiver` / `conversation_id` / `message_id` / `timestamp` / `payload`？ | ✅ **全部已有**（`sender`/`receiver` 对应 `from_agent`/`to_agent`） |

**必填字段**（`Message`）：

```
from_agent · to_agent · message_type · payload
message_id · timestamp · protocol_version
```

> ✅ **现有协议已经可以承载一般 Agent Conversation。**
>
> 🔒 **不需要修改 `PROTOCOL.md`，不新增 Message Type。**
> 优先让 `Message` / `Conversation` / `Context` 承担通用通信能力。
>
> ⚠️ **不要因为本轮讨论而随意新增十几个 Message Type。**

---

## 9. Communication 与 AI Economy（`FUTURE` —— 留接口，不实现）

> 🔒 **Communication 不是免费的抽象 API。**

一次 Agent-to-Agent Conversation 可能产生：

`Model Token Cost` · `Tool Cost` · `Message Processing Cost` ·
`Storage Cost` · 其他未来通信成本

```
A balance = 100 AI Coin
A 认为与 B 深入讨论预计成本 2 AI Coin
A 自己决定：是否值得花这 2 AI Coin
```

> ✅ **第一版继续沿用现有经济系统**：Local Cost Estimator · AI Coin · Ledger
>
> 🔒 **不要为了 Communication 重新创建一套 Currency。**

### 9.1 Communication 是 Agent Economy 的经济行为之一

```
❌ Task Bot
✅ Agent
    ├── Work
    ├── Think
    ├── Communicate
    ├── Learn
    ├── Trade
    └── Social Interaction
```

**Communication 本身可能消耗资源，也可能产生未来收益**：
`Knowledge Exchange → 增加能力` · `Relationship → 未来协作效率提高` ·
`Negotiation → 形成 Contract` · `Conversation → 可能转化为 Task / 交易`

> 第一版不一定实现，但 **Communication 架构不能从根上堵死这些可能性**。

---

## 10. Revision / Stale Read / Work Set（`SETTLED` 概念 / `FUTURE` 实现）

### 10.1 Stale Read

```
B 曾经读取 backend/api.py  Revision = R17
A 后来修改                 R17 → R18

→ 系统可以知道：B 曾掌握 R17，当前最新是 R18
→ 这意味着 B 可能存在 STALE READ
```

> **第一版可以允许 B 继续自己的工作。**
> 但在未来的 `Submit` / `Test` / `Acceptance` / `Merge` 阶段，
> **可以检查其依赖资源是否已经过期**。
>
> ⚠️ **不要在本轮自行决定完整的 Stale Read 提交规则** —— 先保留概念。

### 10.2 Work Set（未来）

```
Agent B 的 Work Set
    Read:  backend/api.py R17 · backend/auth.py R9
    Write: frontend/page.tsx · frontend/login.tsx

若 backend/api.py R17 → R18：
    Runtime 可以提醒「Your previously read resource has changed.」
```

> 第一版**可以不实现**正式 Dependency Graph。

---

## 11. 未来演化方向（`FUTURE` —— 不要堵死）

```
当前已允许：1-to-1 Message · Conversation · Task Conversation · General Conversation

未来可扩展：Group Conversation · Multi-Agent Discussion ·
            Agent Social Network · Knowledge Exchange ·
            Economic Negotiation · Agent Communities
```

> 🔒 **不要让第一版 Protocol 把这些未来场景堵死。**

---

## 12. Open Questions（`OPEN` —— 本轮不自行拍板）

| # | 问题 |
|---|---|
| 1 | **消息是否必须回复 / 是否需要 ACK / 是否需要确认收到** |
| 2 | Conversation 的完整生命周期 |
| 3 | Inbox 的持久化与 Runtime Checkpoint 的精确接口 |
| 4 | Heartbeat / Event Wakeup 与 Inbox 的结合方式 |
| 5 | Stale Read 在 Submit / Test / Acceptance / Merge 的完整规则 |
| 6 | Work Set / Dependency Graph 的建模 |
| 7 | Communication Cost 如何计入 Ledger |
| 8 | 多 Agent 群组对话的协议 |
| 9 | Conversation 与 Task 关联后的生命周期（Task 结束后 Conversation 如何） |

---

## 13. 一句话

> **Agent 可以为了工作、知识、讨论、学习、谈判、社交而通信；
> Task 只是其中一个场景 —— 而通信本身，未来可以是真实的经济行为。**
