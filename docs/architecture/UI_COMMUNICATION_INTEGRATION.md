# UI 通信接入设计（Phase 1 前）

> **状态：`DESIGNED`**
>
> Phase 1 **尚未开始**；先记录设计、边界和待确认问题。
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
- Human 不被当成普通 Agent；
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

### 2.2 UI 链路没有走通信层

`AIEOSController` 和 Work 层没有引用 `communication`：

```text
work/controller.py       无 communication
work/runtime.py          无 communication
work/pi_runtime.py       无 communication
ui/server.py             无 communication
```

UI 实际使用的是：

```text
Human → Front
  AIEOSController._messages
  → _conversation()
  → Front prompt

Front → Worker
  TaskCreationService / TaskStore 直接调用

Worker → Front
  Pi CLI stdout / prompt / result
```

### 2.3 数据事实

UI 数据库 `runs/ui/aieos.db`：

```text
communication_messages = 0 行
```

UI 对话保存在：

```text
runs/ui/jobs/<job_id>.json
  ├─ job
  └─ messages
```

也就是说，UI 对话目前没有进入 Message Journal。

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

## 5. Human Gateway

### 5.1 为什么需要

`CommunicationManager` 当前的 `Message` 是 Agent-to-Agent 模型：

```text
from_agent
to_agent
```

Human 不是 Agent，不能直接把 `human` 当成 Agent ID，否则会混掉：

```text
Human ≠ Task Creator
Identity ≠ Address
```

### 5.2 定位

```text
UI
 ↓
Human Gateway（Life 层端点）
 ↓ Message
Front Agent
```

Human Gateway 负责：

- 人类身份 / 会话；
- 将 UI 输入转成 `Message`；
- 将 Front 回复投影回 UI；
- 不创建 Task；
- 不持有钱包；
- 不参与 Agent Directory / Availability。

### 5.3 待确认：Gateway 的 Actor 模型

两种候选：

**A. 通信层增加 `ActorKind`**

```text
Message.from_actor = ActorRef(kind=agent|human, id=...)
```

- 语义最干净；
- 需要协议升级（`protocol_version`）；
- 影响所有 Message 调用方。

**B. Human Gateway 注册为技术端点**

```text
from_agent = "human:local"
metadata = {"actor_kind": "human"}
```

- 不改协议；
- 但 `AgentRegistry` / `AgentDirectory` 需要明确排除 Human；
- 发送方校验仍要求注册，需新增 `register_endpoint` 或类似入口。

> 推荐 Phase 1 先用 B 的技术端点方案，但必须加
> `actor_kind=human` 并在 Agent Directory 中过滤；长期再评估 A。

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

- Human Gateway；
- Human → Front 的 Message；
- Front → Human 的 Reply Message；
- `conversation_id` / `reply_to` / `trace_id`；
- UI 从 Message Journal 读取历史；
- 失败重试 / 去重。

### 7.2 不做

- 不改 Front → Worker handoff；
- 不改 Task 创建 / 状态机；
- 不做 Worker ↔ Worker；
- 不做 Pi inbox/outbox；
- 不迁移旧 UI 对话；
- 不做多用户认证。

### 7.3 Phase 1 验收

- Human 消息写入 `communication_messages`；
- Front 回复写入同表；
- UI 刷新/重连后能从 Journal 恢复对话；
- 同一条消息重试不会产生重复；
- Task 创建行为不变；
- `AIEOSController._messages` 不再是唯一事实源。

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

## 11. 待确认问题

Phase 1 开始前需要确认：

1. **Human 身份模型**：单本地 Human，还是多用户？
   Actor ID 用什么格式（`human:local` / `user_<id>`）？
2. **Human Gateway 注册方式**：技术端点（不改协议）还是
   `ActorKind`（协议升级）？
3. **Phase 1 是否只做 Human ↔ Front**，还是同时镜像系统事件？
4. **UI 事实源切换策略**：先双写，还是一次性切到 Journal？
5. **Conversation ID 语义**：一个 UI 会话一个，还是 Human+Front 长期一个？
6. **历史保留**：UI 对话保留多久？是否需要分页/归档？
7. **Front 空闲收件**：轮询、wakeup，还是只在下一次输入时处理？
8. **消息类型**：复用 `TEXT` + payload，还是新增
   `HUMAN_MESSAGE` / `FRONT_REPLY`？
9. **Worker ↔ Worker**：只允许显式消息，确认不做自动广播？
10. **Pi bridge**：inbox/outbox 用文件、JSON-RPC 还是数据库？
11. **旧 UI 对话 / 测试任务**：迁移、保留，还是清空？
12. **多用户 / 跨机器**：Phase 1 是否只支持本机单用户？

---

## 12. 非目标

- 不把 Message 变成第二套 Task Core；
- 不把 Message 变成第二套 Shared Context；
- 不自动广播 Workspace Event；
- 不把 Human 注册成普通 Agent；
- 不让 UI 直接操作 Transport；
- 不在 Phase 1 改 Pi 执行模型。
