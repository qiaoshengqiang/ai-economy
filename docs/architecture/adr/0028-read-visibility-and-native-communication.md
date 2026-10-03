# ADR-0028：Read 靠 Task Participation；Communication 是 Agent 基础能力

- **状态**：Accepted（架构设计冻结；**实现为 `FUTURE`**）
- **时间**：Read 与 communicate 的架构设计
- **相关**：ADR-0027（设计简化）· ADR-0019（Shared Context）· ADR-0020（Workspace）
- **规范**：[`COMMUNICATION_MODEL.md`](../COMMUNICATION_MODEL.md) · [`TASK_WORKSPACE.md`](../TASK_WORKSPACE.md) §7.5

## Context

ADR-0027 把 Authorization 降级为 `FUTURE`，留下**两个空档**：

1. **Read 空档** —— 移除 `READ` 权限后，Workspace 的读语义没有替代品。
   可能出现「没有 Authorization，因此 Workspace 完全没有 Read 模型」。
2. **Communication 定位不清** —— 过去很容易被理解成
   `Task → Message → Agent`（Task Message Bus）。

## Decision

### 一、Read 靠 `Task Participation`，不重建 Authorization

> 🔒 **Task Workspace 第一版不实现资源级 Read ACL。
> Task Participants 默认可以读取该 Task Workspace 中的普通共享资源。**

```
Task Participation → Read Task Workspace
Lock               → Write / Modify
```

**不引入** `READ` / `READ_FILE` / `READ_DIRECTORY` / `READ_RESOURCE`，
**不建立**复杂 ACL，**不恢复** `Authorization.READ`。

### 二、Read Visibility ≠ Write Responsibility

> `Read` = 「你可以了解这个项目。」 · `Lock` = 「你现在负责修改这项工作。」

A、B、C 都是 Participant → 都能**读**；但修改仍由 **Lock** 决定。

### 三、Read 的边界

**默认可读**：Task Workspace 普通共享资源
**不在默认范围**：Agent Private Workspace / Private Memory /
Private Artifacts / System Workspace / 其他 Task 的 Workspace

> 🔒 **不要为此再引入复杂的 Visibility ACL。**

### 四、何时获得 Read

**在 Agent 正式成为 `Task Participant` 时。**
「加入 / 接受」的生命周期**优先复用现有 Task 模型**，不重复建机制。

### 五、Human 作为 Participant

可以读；但**是否可直接修改**仍由 Workspace / Governance / Force Unlock 决定。
🔒 **不因 Read 模型而改变既有 Human Governance 设计。**

### 六、🔒 `Communication ≠ Task Message Bus`

> ## **Communication 是 Agent 的基础能力，
> ## 不以 Task 为前提，也不以 Workspace 为前提。**

```
❌ Task → Message → Agent
✅ Agent → Communication ├── Task-related
                         ├── General conversation
                         ├── Knowledge exchange
                         ├── Negotiation
                         ├── Economic communication
                         └── Future social interaction
```

**Message 不要求绑定 Task**：`task_id` / `workspace_id` / `resource_id`
**都可以是 `null`**，依然是合法的 Agent-to-Agent Communication。

### 七、Conversation 成为可存在的概念

```
Conversation C001 · Participants: A, B
    M001 A→B · M002 B→A · M003 A→B
```

`task_id` 可以是 `T1`，也可以是 `null`。
**当前不要求完整实现 Conversation Manager**，
只需**不把 Communication 锁死成「一条消息 = 一次 Task 通知」**。

### 八、🔒 `Workspace Event ≠ Agent Message`

| | Workspace Event | Agent Message |
|---|---|---|
| 谁产生 | **系统自动** | **Agent 主动** |
| 回答 | **发生了什么？** | **我认为这为什么与你有关？** |
| 性质 | **Objective Change Fact** | **Agent Judgment / Intent** |

🔒 **不要把所有 Workspace Change 自动广播成 Agent Message**
（否则造成 Agent Communication Flood）。
🔒 **系统不判断「这个修改一定影响谁」** —— 系统记录变化，
**Agent 自己判断语义影响**。

### 九、`Runtime Inbox` 与异步通信

```
B Runtime
├── LLM Execution
└── Message / Event Inbox        ← 由 **Runtime** 持有
```

> 🔒 **Runtime 可以持续接收通信，而当前 LLM invocation
> 不需要实时响应每一条消息。**

> 🔒 **Communication Inbox 必须由 `Runtime` 持有，
> 而不是由 `LLM Invocation` 本身持有。**

```
Message → Inbox → B Checkpoint → 加入下一轮 Context → B 决定是否回应
```

> 🔒 **Message 不能因为 LLM 正在运行而丢失** —— 必须持久化。

### 十、🔒 Communication 与 Workspace 解耦

Communication 可以**引用** Workspace（`resource` / `revision`），
但🔒 **不能直接修改 Workspace** —— 资源修改仍经 Workspace / Gateway。

### 十一、四个概念各司其职

| 概念 | 回答 |
|---|---|
| Revision | 当前资源处于**什么版本**？ |
| Workspace Event | **发生了什么**？ |
| Agent Message | **我认为这为什么与你有关**？ |
| Task Shared Context | 大家应该**共同知道什么**？ |

### 十二、Protocol **不需要修改**（实测）

| 检查项 | 结果 |
|---|---|
| 是否必须绑定 Task？ | ❌ 没有 —— `task_id` 是 payload 派生的**可选**属性 |
| 允许 `task_id = null` / `workspace_id = null`？ | ✅ 允许，二者都不在必填字段 |
| 是否有 `conversation_id` / `message_id` / `timestamp` / `payload`？ | ✅ 全部已有 |

> ✅ **现有协议已可承载一般 Agent Conversation。**
> 🔒 **不修改 `PROTOCOL.md`，不新增 Message Type。**

### 十三、Communication 可能产生经济成本（`FUTURE`）

`Model Token Cost` · `Tool Cost` · `Message Processing Cost` · `Storage Cost`

> ✅ **沿用现有经济系统**（Local Cost Estimator · AI Coin · Ledger）。
> 🔒 **不要为 Communication 重新创建一套 Currency。**
> 🔒 **不要假定 Agent-to-Agent Communication 永远免费。**

### 十四、Revision / Stale Read 概念保留

```
B 曾读 backend/api.py R17 → A 改为 R18 → B 可能存在 STALE READ
```

第一版允许 B 继续工作；未来在 `Submit` / `Test` / `Acceptance` / `Merge`
可检查依赖资源是否过期。
⚠️ **不要在本轮自行决定完整 Stale Read 提交规则。**

## Consequences

**正面**：

- **填补了 Authorization 移除后的 Read 空档** —— 且没有重建权限系统
- Read 规则**极简**：「是 Participant 就能读普通共享资源」
- `Communication` 从 Task 的附属功能提升为 **Agent 基础能力** ——
  为未来的知识交换 / 谈判 / 社交 / 经济行为留出空间
- `Workspace Event ≠ Agent Message` 防止 **Communication Flood**
- `Runtime Inbox` 明确解决「LLM 运行中消息丢失」这个**真实工程风险**
- **协议零修改** —— 现有设计已经支持，说明前期抽象是对的

**负面 / 代价**：

- **Read 边界依赖 `Task Participant` 的定义** ——
  如果 Membership 规则变化，Read 范围会跟着变
- 「普通共享资源」的**精确边界未定**（哪些算「普通」）→ `OPEN`
- Inbox **必须持久化** → 未来需要存储设计
- `Conversation` 概念已定但**无实现** → 中间期可能出现
  「有 `conversation_id` 但没有 Conversation 语义」的模糊状态
- Communication 经济化后，**Agent 可能因成本拒绝必要沟通** —— 这是真实权衡

**因此不能做的事**：

- ❌ 不许引入 `READ` / `READ_FILE` / `READ_DIRECTORY` / `READ_RESOURCE`
- ❌ 不许为普通 Task Workspace 文件建立复杂 ACL
- ❌ 不许恢复 `Authorization.READ`
- ❌ 不许让 Private Workspace / System Workspace 进入默认 Read 范围
- ❌ 不许要求每条 Message 绑定 Task
- ❌ 不许把 `Workspace Event` 与 `Agent Message` 混成一个机制
- ❌ 不许把每个 Workspace Change 自动广播成 Message
- ❌ 不许让系统**自动判断**「这个修改一定影响谁」
- ❌ 不许要求 LLM invocation 实时响应消息
- ❌ 不许让 Message 因 LLM 正在运行而**丢失**
- ❌ 不许让 Communication **直接修改** Workspace
- ❌ 不许为 Communication **新建货币**
- ❌ 不许随意新增十几个 Message Type

## Future evolution

```
今天（本 ADR）：
    Read = Task Participation（极简）· 无 READ ACL
    Communication = Agent 基础能力（不绑 Task）
    Workspace Event ≠ Agent Message
    Runtime Inbox（异步、持久化）
    协议零修改

未来：
    Conversation Manager · Group Conversation · Agent Social Network
    Dependency Graph / Work Set / Stale Read 检查
    Communication Cost 计入 Ledger
    AI Governance（见 RECOVERY_MODEL.md）
```

## Guard

| 违规 | |
|---|---|
| 引入 READ ACL / 恢复 Authorization.READ | ✅ |
| Private / System Workspace 进入默认 Read | ✅ |
| Read 等于 Write | ✅ |
| Message 必须绑定 Task | ✅ |
| Workspace Event 与 Agent Message 混合 | ✅ |
| 自动广播所有 Workspace Change | ✅ |
| Message 因 LLM 运行而丢失 | ✅ |
| Communication 直接修改 Workspace | ✅ |
| 假定通信永远免费 | ✅ |
| 随意新增 Message Type | ✅ |
