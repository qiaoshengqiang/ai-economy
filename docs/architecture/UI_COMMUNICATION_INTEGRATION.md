# UI 通信接入设计（Phase 1 前）

> **状态：`CURRENT`（Phase 1）**
>
> 已实现：Human Gateway · Human → Front · Front → Human ·
> Message Journal · UI 从 Journal 投影。
>
> 未完成：UI 客户端幂等 / 重试去重；Front → Worker 与 Task 消息
> 仍属于后续阶段。
>
> 相关文档：
> [`COMMUNICATION_MODEL.md`](COMMUNICATION_MODEL.md) ·
> [`LIFE_WORK_ARCHITECTURE.md`](LIFE_WORK_ARCHITECTURE.md) ·
> [`PROTOCOL.md`](../../PROTOCOL.md) ·
> [`AIEOS_UI.md`](AIEOS_UI.md)

---

## 1. 目标

把 AIEOS UI 目前的 **进程内编排消息** 接入系统原本的
`CommunicationManager`，让 Human / Front / Worker 之间的通信具备：

- `message_id` / `trace_id` / `conversation_id`
- Message Journal
- 去重、回执、重试
- 断线/重启后的消息恢复
- 未来跨进程 / 跨机器的传输能力

同时保持：

- Task Core 是 Task 状态的唯一权威；
- Workspace / Shared Context 是工作事实的唯一来源；
- Human 作为系统原有协作角色的定义不变；
- 前端人类消息只是接入消息通道，不重新定义 Human；
- Workspace Event 不自动变成 Agent Message。

---

## 2. 当前事实

### 2.1 核心 Runtime 已接入

`main.py` 的组合根会：

```text
CommunicationManager
  → register(agent_a..e)
  → agent.attach_communication(communication)
  → _wire_task_handlers(communication, task_service)
```

所以 Agent 本身具备 `send` / `receive` 能力，`TaskService` 的
handler 也已注册到通信层。

### 2.2 UI 链路的当前接入范围

Phase 1 已接入：

```text
Human → Front
  HumanGateway
  → CommunicationManager
  → communication_messages
  → UI Journal 投影

Front → Human
  CommunicationManager
  → HumanGateway
  → UI Journal 投影
```

仍然没有接入：

```text
Front → Worker
  TaskCreationService / TaskStore 直接调用

Worker → Front
  Pi CLI stdout / prompt / result

Worker ↔ Worker
  当前没有显式消息
```

### 2.3 数据事实

Phase 1 之前：

```text
communication_messages = 0 行
```

Phase 1 之后：

```text
Human / Front 对话 → communication_messages
Work / Task / Pi  → runs/ui/jobs/<job_id>.json
```

UI 对话现在同时保留：

```text
communication_messages   ← Human / Front 的对话事实
runs/ui/jobs/*.json      ← job / round 观测
```

Phase 1 尚未做客户端幂等 / 重试去重；这一步仍在待办。

---

## 3. 设计原则

### 3.1 Life 层走消息

Life 层负责：

- Human ↔ Front 对话
- Front ↔ Worker 的任务邀约 / 接受 / 拒绝
- Agent ↔ Agent 的显式沟通
- Agent 可用状态 / 邀请 / 组队

这些都是 Agent（或 Human Gateway）之间的交互，适合走
`CommunicationManager`。

### 3.2 Work 层继续走领域机制

Work 层负责：

- Task 状态机
- Workspace 文件 / 锁 / revision
- Shared Context
- Working Memory
- Tool Runtime

这些事实继续由各自领域机制持有，**不通过消息同步状态**。

### 3.3 四条硬边界

```text
Message ≠ Task State
Message ≠ Shared Context
Message ≠ Workspace
Human ≠ Agent
```

消息可以**触发**领域动作，但不能成为第二套状态源。

---

## 4. 哪些 UI 内容走消息

| UI 内容 | 是否走消息 | 说明 |
|---|---|---|
| Human → Front 输入 | **是** | Life 层会话；`conversation_id` |
| Front → Human 回复 | **是** | `reply_to` 指向 Human 消息 |
| Front → Worker handoff / 邀请 | **是** | `TASK_OFFER` / `ACCEPT` / `REJECT` |
| Worker → Front 结果 / 提问 | **是** | `task_id` 可选 |
| Worker ↔ Worker 显式沟通 | **是，有限制** | 只承载 Agent 判断/通知，不承载任务状态 |
| Task START / SUBMIT / VERIFY / COMPLETE | **可以走消息触发** | Task Core 仍是状态权威 |
| UI“系统已开始 / 已完成” | **否** | 领域事件投影，不是 Agent 消息 |
| Job log / round log / 工具日志 | **否** | 观测数据 |
| Workspace 文件变化 | **否，不自动广播** | Workspace Event；Agent 自己判断是否通知 |
| Front inspect / artifacts / commit | **否** | 只读查询或系统服务 |
| Pi 内部 tool 日志 | **否** | 执行细节 |

---

## 5. Human 与“前端人类消息”

### 5.1 先区分两个概念

**Human（领域角色）** 是系统原本就有的设计：

```text
Demand Provider
Feedback Provider
Optional Collaborator
Governance Authority
```

边界不变（见 ADR-0022 / ADR-0024）：

```text
Human ≠ Task Creator
Human Demand ≠ Task
```

本次**不重新设计 Human**，不改 Human 的权限、需求、验收或治理模型。

**前端人类消息** 只是 Human 在 UI 里的一次输入：

```text
Human 在输入框说了一句话
  → 一条 Message 进入 CommunicationManager
  → Front 看到它
```

它是**消息通道的接入**，不是新的 Human 实体，也不是新的 Human 协议。

### 5.2 Human Gateway 是 UI 端点，不是 Agent

```text
UI
 ↓
Human Gateway（通信端点）
 ↓ Message
Front Agent
```

Human Gateway 的职责只有：

- 把 UI 输入转成 `Message`；
- 把 Front 的回复投影回 UI；
- 携带 `human_id` / `conversation_id`；
- 保证 UI 不能任意伪造发送方。

它**不是**：

- 普通 Agent；
- 经济主体；
- Task Creator；
- Agent Directory / Availability 的成员；
- 钱包或 Ledger 的参与者。

### 5.3 Phase 1 的 Actor 方案

Phase 1 不引入 `ActorKind`，不改 `protocol_version`，复用现有 Message：

```text
from_agent = "human_gateway"
to_agent   = "agent_a"
payload    = {
  "human_id": "local",
  "human_message_id": "...",
  "kind": "human_message",
  "text": "..."
}
conversation_id = "conv_local_agent_a"
```

`human_gateway` 是**技术端点**，不是 Agent：

- 只在通信层注册；
- 不进入 `AgentDirectory`；
- 不参与 `AgentAvailability`；
- 不参与经济 / Task / Workspace 权限。

如果未来要多用户或跨机器，再讨论：

- 多个 `human_id` 的认证；
- Human Gateway 的网络地址；
- 是否升级为正式 `ActorRef` / `ActorKind`。

---

## 6. Message / Conversation / Inbox

### 6.1 Message 类型

Phase 1 建议复用现有类型，不急于新增协议类型：

```text
Human ↔ Front        TEXT
Front → Worker       TASK_OFFER
Worker → Front       TEXT / SUBMIT / VERIFY / COMPLETE
Worker ↔ Worker      TEXT（payload.kind = agent_message）
```

如果需要更细的语义，再考虑新增：

```text
HUMAN_MESSAGE
FRONT_REPLY
AGENT_MESSAGE
```

新增类型必须评估 `protocol_version` 兼容性。

### 6.2 Conversation

```text
Human ↔ Front:
  conversation_id = "conv_<human_id>_<front_agent>"

Front ↔ Worker:
  conversation_id = "conv_<task_id>"（或 handoff 专用会话）
  task_id 可选

Worker ↔ Worker:
  conversation_id = "conv_<task_id>_<agent_a>_<agent_b>"
```

`conversation_id` 只是消息分组，不是 Task 状态。

### 6.3 Inbox

```text
Agent Runtime
  ├─ LLM Execution
  └─ Message / Event Inbox
```

规则：

- Inbox 由 **Runtime** 持有，不由 LLM invocation 持有；
- 消息在 turn 边界注入，不打断当前 LLM 调用；
- Front 空闲时也要能接收消息；
- Pi CLI 需要 Runtime-owned inbox/outbox 桥；
- 长任务需要考虑 Claim Lease 续租（当前技术债）。

---

## 7. Phase 1 范围

### 7.1 做

- Human Gateway（技术端点，不是 Agent）；
- Human → Front 的 Message（`kind=human_message`）；
- Front → Human 的 Reply Message（`kind=front_reply`）；
- `conversation_id` / `reply_to` / `trace_id`；
- UI 从 Message Journal 读取历史；
- 失败重试 / 去重。

### 7.2 不做

- 不改 Front → Worker handoff；
- 不改 Task 创建 / 状态机；
- 不改 Human 的领域角色 / 权限 / Demand 模型；
- 不做 Worker ↔ Worker；
- 不做 Pi inbox/outbox；
- 不迁移旧 UI 对话；
- 不做多用户认证。

### 7.3 Phase 1 验收

- [x] Human 消息写入 `communication_messages`；
- [x] Front 回复写入同表；
- [x] UI 重启后能从 Journal 恢复 Human / Front 对话；
- [ ] 同一条消息重试不会产生重复（UI 客户端幂等键）；
- [x] Task 创建行为不变；
- [x] `AIEOSController._messages` 不再是 Human / Front 对话的唯一事实源。

---

## 8. Phase 2–4

### Phase 2：Front ↔ Worker

- `TASK_OFFER` / `ACCEPT` / `REJECT` 接入；
- Worker 仍通过 Task Core 创建 Task；
- Front 不再直接调用 `TaskCreationService`。

### Phase 3：Task 生命周期消息

- `START / SUBMIT / VERIFY / COMPLETE` 走 Task handler；
- UI 不再直接调用 Task 状态方法；
- Task Core 仍是唯一权威。

### Phase 4：Worker ↔ Worker + Pi bridge

- Worker 显式消息走 `TEXT` / `payload.kind=agent_message`；
- Pi Runtime 增加 inbox/outbox；
- 不做 Workspace 自动广播；
- 长任务 lease 续期。

---

## 9. 数据与投影关系

目标：

```text
communication_messages     ← 对话事实
        ↓
UI conversation projection ← 只读缓存 / 展示
```

`AIEOSController._messages` 的处理：

- Phase 1 双写：先写 Message，再保留 `_messages` 作为缓存；
- Phase 1 验收后：`_messages` 改为从 Journal 构建的 projection；
- `runs/ui/jobs/<job_id>.json` 继续保存 job / round 观测，不承担对话权威。

---

## 10. 失败与安全

- `message_id` 去重；
- `reply_to` 维持会话；
- `trace_id` 串联 handoff / task；
- UI 重试使用客户端幂等键；
- Human Gateway 本地优先，不暴露公网；
- 多用户认证、Human 权限、跨机器 Human Gateway 均为后续阶段；
- 不允许 UI 任意伪造 `from_agent`。

---

## 11. 已确认与后续

### 11.1 已确认（Phase 1 前）

1. **区分 Human 领域角色与“前端人类消息”**：
   本次只把 UI 的人类消息接入 Message 机制，不重新设计 Human，
   不改 Human 的 Demand / 反馈 / 协作 / 治理角色。
2. **Human Gateway 是技术端点，不是 Agent**：
   不进入 Agent Directory / Availability / 经济 / Task 权限。
3. **Phase 1 只做 Human ↔ Front**：
   不改 Front → Worker handoff，不改 Task 创建 / 状态机，
   不做 Worker ↔ Worker，不做 Pi inbox/outbox。
4. **Phase 1 只支持本机单 Human**：
   多用户认证、跨机器 Human Gateway 后续再做。
5. **复用现有协议**：
   `TEXT` + `payload.kind=human_message/front_reply`；
   不改 `protocol_version`。
6. **先双写，再切投影**：
   Phase 1 先写 Message，同时保留 `AIEOSController._messages` 缓存；
   验收后再让 UI 以 Journal 为事实源。
7. **Conversation ID**：
   `conv_<human_id>_<front_agent>`；
   新会话再生成新的 `conversation_id`。
8. **Front 空闲收件**：
   Phase 1 在下一次 turn 检查 inbox；不做实时 wakeup。
9. **旧 UI 对话保留为 legacy**：
   不迁移到 Message Journal；新消息从接入后开始进入 Journal。
10. **Worker ↔ Worker 只做显式消息**：
    不做 Workspace 自动广播（Phase 4 再实现）。
11. **Pi inbox/outbox 放到 Phase 4**：
    优先文件方案，具体格式后续定。
12. **Phase 1 不自动清理消息**：
    归档 / 分页 / 保留策略后续再加。

### 11.2 不阻塞 Phase 1 的后续问题

- 多用户认证与 Human 权限；
- 跨机器 Human Gateway / Transport；
- 是否引入正式 `ActorRef` / `ActorKind`；
- Pi inbox/outbox 的文件或 JSON-RPC 格式；
- 消息归档、分页、保留期限；
- 旧 UI 对话是否需要一次性导入 Journal。

---

## 12. 非目标

- 不把 Message 变成第二套 Task Core；
- 不把 Message 变成第二套 Shared Context；
- 不自动广播 Workspace Event；
- 不把 Human 注册成普通 Agent；
- 不让 UI 直接操作 Transport；
- 不在 Phase 1 改 Pi 执行模型。
