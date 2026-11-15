# Agent Wakeup V1 —— 定时唤醒与 Agent 闹钟设计

> **状态：`DESIGNED`（2026-11-15）**
>
> V1 先接入 **Front Agent**；核心数据模型、调度器和
> `WakeupDispatcher` 从第一天就是**通用 Agent 能力**。
> Pi / Solo Worker 的 handler 后续接入，不重新设计核心。
>
> 相关文档：
> [`ARCHITECTURE_CONSTITUTION.md`](../ARCHITECTURE_CONSTITUTION.md) §5.9 ·
> [`AGENT_CORE_MODEL_V1.md`](../AGENT_CORE_MODEL_V1.md) §22.27 ·
> [`COMMUNICATION_MODEL.md`](../COMMUNICATION_MODEL.md) §5 ·
> [`implementation/AGENT_INBOX_V1_DESIGN.md`](AGENT_INBOX_V1_DESIGN.md) Phase 5 ·
> ADR-0014 · ADR-0028 · ADR-0055

---

## 0. 一句话

**Agent 可以为自己创建一个持久化闹钟；系统在到期时唤醒对应 Agent，
但不替它决定做什么，也不把闹钟伪装成消息、Task 或心跳。**

```text
Agent
  ↓ alarm_create
agent_alarms（持久化）
  ↓ 到期
WakeupEvent
  ↓
WakeupDispatcher
  ↓
FrontWakeupHandler / PiWakeupHandler / SoloWakeupHandler
  ↓
Agent 被唤醒后自行决定：
  inspect / 通知人类 / 不通知 / 取消闹钟 / 继续设闹钟
```

---

## 1. 背景：已有设计只到“原则”

### 1.1 已经确认的原则

| 文档 | 已确认内容 |
|---|---|
| [`ARCHITECTURE_CONSTITUTION.md`](../ARCHITECTURE_CONSTITUTION.md) §5.9 | `Runtime / Life Loop = 我什么时候醒来`；Heartbeat / Event Wakeup / 自主循环属于这一层 |
| [`AGENT_CORE_MODEL_V1.md`](../AGENT_CORE_MODEL_V1.md) §22.27 | **Wakeup ≠ LLM Call**；Agent 保持响应能力，但不需要持续高成本思考 |
| [`COMMUNICATION_MODEL.md`](../COMMUNICATION_MODEL.md) §5 | **Wakeup ≠ Message**；Runtime 持有 Inbox；当前 LLM 调用不要求实时中断；恢复间隔可以是几分钟到几周 |
| [`PRODUCTION_STAGE_V1_ALIGNMENT_REVIEW.md`](../audits/PRODUCTION_STAGE_V1_ALIGNMENT_REVIEW.md) §G | Runtime 可以是 stopped；恢复只依赖持久状态 + Stimulus |
| [`implementation/AGENT_INBOX_V1_DESIGN.md`](AGENT_INBOX_V1_DESIGN.md) Phase 5 | **Heartbeat / 定时唤醒** 仍为未实现项 |

### 1.2 当前代码缺口

```text
communication/agent_runtime.py
  ├─ 已有 sleep / wake 循环
  └─ WakeupReason 只有 MESSAGE；UI 生产路径没有用它驱动 Front

work/controller.py
  ├─ Front 由 Human Inbox / Job 触发
  ├─ _front_lock 保证一次只有一个 Front turn
  └─ 没有时间触发路径

work/scheduler.py
  └─ TeamScheduler：团队队列 / 并行上限，不是时间调度

demand/scheduler.py
  └─ RunScheduler：轮询参与者 turn，不是定时唤醒
```

**结论**：这次不是改造 TeamScheduler，也不是把闹钟做成 Message；
需要新增 **Life / Runtime 层的 Alarm / Scheduled Wakeup**。

---

## 2. 目标与非目标

### 2.1 目标

1. Agent 可以创建 **一次性 / 周期性** 的持久化闹钟；
2. 系统在到期时唤醒对应 Agent，**不替它做决定**；
3. 核心 `agent_id` 通用，Front 只是第一个 handler；
4. 进程重启后闹钟仍在，过期只补触发一次，不补发所有错过周期；
5. 每次唤醒都有 **Activity Record**，人类能知道 Agent 最近活动过；
6. Agent 可以查看 / 取消自己的闹钟；
7. 不把闹钟写成 Message、Task、Heartbeat 或 Workspace 事件；
8. 不中断正在执行的 LLM / Tool；
9. V1 不引入新的 `protocol_version`。

### 2.2 非目标

- 不实现完整 Life Loop；
- 不实现 Heartbeat / Lease Renewal / 存活检测；
- 不在 V1 实现 Pi / Solo 的 handler；
- 不让闹钟自动创建 Task、返工、follow-up；
- 不让系统冒充 Human / Agent 发消息；
- 不做没有资源保护的无限唤醒（测试用 `WakeupGuard`）；
- 不在 Worker 工作时唤醒或注入 Work 上下文；
- 不把 Activity Record 做成聊天消息。

---

## 3. 核心边界

### 3.1 五个不等号

```text
Alarm  ≠ Message       闹钟不是通信事实
Alarm  ≠ Task          闹钟不是工作单元
Alarm  ≠ Heartbeat     闹钟不是存活检测 / 租约续期
Alarm  ≠ Decision      系统只唤醒，Agent 决定做什么
Wakeup ≠ LLM Call      被唤醒不等于必须调用模型
```

### 3.2 Alarm 与 Message 的区别

| 维度 | Message | Alarm |
|---|---|---|
| 语义 | 谁对谁说了什么 | 未来某时刻叫醒我 |
| 发起者 | Human / Agent / System actor | **Agent 自己** |
| 接收者 | 另一个 Agent / Human | **自己** |
| 生命周期 | append-only 通信事实 | 可变的调度状态 |
| 需要 sender/recipient | 是 | 否 |
| 需要 conversation | 是 | 否 |
| 支持 `next_fire_at` / 周期 | 否 | 是 |
| 取消 | 不能撤回历史事实 | 可以取消未来触发 |
| 消费语义 | read / processed / ACK | wake / run outcome |
| UI | 聊天 / 收件箱 | 活动记录 |

> **Alarm 是 Agent 的私有调度状态，不是通信事实。**

### 3.3 Activity ≠ Chat Message

`no_notify` 只表示“不往人类聊天里发消息”；
系统仍然要写一条 **Activity Record**，让人类知道 Agent 刚刚活动过。

---

## 4. 总体架构

```text
┌──────────────────────────────────────────────────────┐
│ AlarmStore（SQLite）                                 │
│   agent_alarms · agent_wakeup_runs                   │
└──────────────────────────────────────────────────────┘
                     ↑ create / cancel / list
                     │
Agent ── alarm_create / alarm_cancel / alarm_list
                     │
                     ▼
┌──────────────────────────────────────────────────────┐
│ AlarmScheduler                                       │
│   等到最早的 next_fire_at；没有闹钟就无限等待         │
│   create / cancel 会 signal 它重算                   │
└──────────────────────────────────────────────────────┘
                     │ due
                     ▼
┌──────────────────────────────────────────────────────┐
│ WakeupDispatcher                                     │
│   agent_id → WakeupHandler                           │
└──────────────────────────────────────────────────────┘
        │                    │                    │
        ▼                    ▼                    ▼
FrontWakeupHandler   PiWakeupHandler      SoloWakeupHandler
   （V1）              （以后）               （以后）
        │
        ▼
FrontAgentRuntime.wakeup_turn(...)
        │
        ├─ inspect / artifact_read
        ├─ alarm_cancel / alarm_create
        ├─ direct_reply
        └─ no_notify
```

### 4.1 组件职责

| 组件 | 职责 | 不负责 |
|---|---|---|
| `AlarmStore` | 持久化 CRUD、due 查询、原子 claim | 不调用 LLM、不建 Task |
| `AlarmScheduler` | 等到最近到期时间、发出 `WakeupEvent` | 不决定 Agent 行为 |
| `WakeupDispatcher` | 按 `agent_id` 路由到 handler | 不知道 Agent 类型细节 |
| `WakeupHandler` | 把事件交给对应 Agent Runtime | 不创建 Task、不改 Task 状态 |
| `FrontWakeupHandler` | 触发一次 Front wake turn | 不冒充 Human 消息 |
| `ActivityStore` | 记录每次 wake 的结果 | 不是聊天消息 |

### 4.2 为什么没有固定 System tick

固定每 2 秒扫一次、又不调用 LLM，只是空转，没有独立价值。

V1 使用 **deadline-driven**：

```text
没有闹钟 → 线程无限等待
有闹钟   → wait(timeout = next_fire_at - now)
create / cancel → signal，重算最早时间
到期     → dispatch，再重算
```

这不是 Heartbeat，也不是轮询；它是真正的闹钟调度器：

```text
什么时候该叫醒谁
```

V1 假设只有一个 scheduler 进程（UI 组合根）。
以后多进程再增加 DB 通知或低频 reconciliation。

---

## 5. 数据模型

### 5.1 `agent_alarms`

```sql
CREATE TABLE agent_alarms (
    alarm_id           TEXT PRIMARY KEY,
    agent_id           TEXT NOT NULL,
    status             TEXT NOT NULL,
    reason             TEXT NOT NULL,
    instruction        TEXT NOT NULL,
    task_ids_json      TEXT NOT NULL DEFAULT '[]',
    schedule_kind      TEXT NOT NULL,       -- once / every
    interval_seconds   INTEGER,             -- every 时必填
    next_fire_at       TEXT NOT NULL,
    last_fired_at      TEXT,
    fire_count         INTEGER NOT NULL DEFAULT 0,
    max_runs           INTEGER,             -- NULL = 不按次数停止
    expires_at         TEXT,
    dedupe_key         TEXT,
    last_error         TEXT,
    created_by         TEXT NOT NULL,       -- V1 必须等于 agent_id
    source_message_id  TEXT,
    conversation_id    TEXT,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
```

索引：

```sql
CREATE INDEX idx_agent_alarms_due
    ON agent_alarms (status, next_fire_at);

CREATE INDEX idx_agent_alarms_agent
    ON agent_alarms (agent_id, status);
```

### 5.2 `agent_wakeup_runs`

这是 Activity Record，也是每次唤醒的审计事实。

```sql
CREATE TABLE agent_wakeup_runs (
    run_id          TEXT PRIMARY KEY,
    wakeup_id       TEXT NOT NULL,          -- V1 = alarm_id
    agent_id        TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- alarm
    scheduled_for   TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    outcome         TEXT NOT NULL,          -- replied / no_notify /
                                             -- skipped_busy / guard_limit /
                                             -- failed / rejected
    summary         TEXT,
    job_id          TEXT,
    detail_json     TEXT NOT NULL DEFAULT '{}',
    UNIQUE (wakeup_id, scheduled_for)
);
```

`UNIQUE (wakeup_id, scheduled_for)` 保证同一个闹钟的同一个到期时刻
最多产生一条 run 记录，避免重复唤醒。

### 5.3 `task_ids` 语义

- `task_ids` **可选**，默认空数组；
- 最多 **5 个**；
- 只作为唤醒时的只读上下文，不是闹钟的生命周期依据；
- **不因为 Task 结束而自动停止**；
- Agent 在唤醒后自行判断是否 `alarm_cancel`。

兼容旧写法：只传 `task_id` 时归一化为 `["task_x"]`。

### 5.4 状态

```text
ACTIVE      等待下一次触发
RUNNING     已被某个 scheduler 领走，正在 dispatch
COMPLETED   一次性已触发 / 达到 max_runs / 已过期
CANCELLED   Agent 主动取消
FAILED      dispatch 失败且不再重试
```

---

## 6. 调度语义

### 6.1 `once` / `every`

```text
once    next_fire_at = now + delay
every   next_fire_at = now + interval
```

触发后：

```text
once   → COMPLETED
every  → ACTIVE，next_fire_at = now + interval
```

### 6.2 资源边界与测试 Guard

**生产语义**：闹钟一直存在，直到 Agent 主动 `alarm_cancel`。

- `max_runs` / `expires_at` 是**可选参数**，不是系统默认生命周期；
- Agent 可以自己设置，也可以不设置；
- 系统不会因为 Task 结束而替 Agent 停止。

**测试 / 防过度工作**：

测试中 Agent 可能创建过密闹钟，导致重复唤醒和 token 浪费。
这不是产品语义，而是测试运行时的安全网。建议增加独立的 `WakeupGuard`：

```yaml
runtime:
  alarms:
    guard:
      enabled: true          # 测试组合根开启；生产可关闭
      max_wakes_per_agent: 10
      max_wakes_total: 50
```

行为：

- 超过 guard 上限 → 暂停该 Agent 的闹钟，写 Activity
  `outcome = "guard_limit"`；
- 不修改 Task，不伪装成 Agent 的决定；
- 测试结束后由测试运行器清理；
- 生产默认可以使用更高阈值或关闭。

> Guard 是**测试/运行资源保护**，不是 Agent 的停止策略。
> Agent 仍然必须被提示可以 `alarm_cancel`。

### 6.3 进程重启

```text
启动
  ↓
加载 ACTIVE / 过期 RUNNING
  ↓
next_fire_at <= now ？
  ├─ 否 → 等到 next_fire_at
  └─ 是 → 只补触发一次
           every 的下一次 = now + interval
```

不补发错过的所有周期，避免进程重启后出现“补跑风暴”。

### 6.4 原子 claim

```sql
UPDATE agent_alarms
SET status = 'RUNNING', last_fired_at = ?
WHERE alarm_id = ?
  AND status = 'ACTIVE'
  AND next_fire_at = ?
```

只有 `rowcount = 1` 的 scheduler 可以 dispatch。
`agent_wakeup_runs` 的 `UNIQUE` 约束再提供一层幂等保护。

### 6.5 多进程

V1 只有一个 scheduler 进程。
以后多进程需要：

- DB 通知 / 唤醒；
- 或低频 reconciliation；
- 但不引入 Heartbeat / Lease Renewal。

---

## 7. Agent 能力接口

### 7.1 动作

```text
alarm_create
alarm_list
alarm_cancel
```

`alarm_create` 参数：

```json
{
  "reason": "检查任务状态",
  "instruction": "查看 task_x / task_y 的状态和最近产物；有变化或失败时通知我。",
  "task_ids": ["task_x", "task_y"],
  "schedule": "every",
  "interval_seconds": 300
}
```

返回：

```json
{
  "alarm_id": "alarm_...",
  "status": "ACTIVE",
  "next_fire_at": "2026-11-15T10:35:00+00:00",
  "fire_count": 0,
  "max_runs": null
}
```

### 7.2 关闭闹钟必须被提示

Agent prompt 必须明确写出：

```text
你有 alarm_cancel 能力。
当任务结束、目标不再需要检查，或你不再需要这个闹钟时，
应主动调用 alarm_cancel 关闭它。
系统不会因为 Task 结束而替你自动关闭。
```

这是本设计的关键：**停止权属于 Agent，不属于 Task 状态机。**

### 7.3 校验

- `reason` / `instruction` 非空；
- `task_ids` 最多 5 个；
- `once` 需要 `delay_seconds` / `at`；
- `every` 的 `interval_seconds >= min_interval`；
- `max_runs` 可选，设置时必须在允许范围内；
- `expires_at` 可选，设置时必须在未来；
- `created_by` 只能来自 `ActionIntent.agent_id`；
- `dedupe_key` 可选，用于幂等。

### 7.4 V1 可用范围

- `alarm_create / list / cancel` 只挂到 **Front 动作集**；
- 核心 service 支持任意 `agent_id`；
- Pi / Solo 的 action 和 handler 后续接入。

---

## 8. WakeupEvent 与 Handler

### 8.1 事件形状

```json
{
  "wakeup_id": "alarm_...",
  "agent_id": "agent_a",
  "kind": "alarm",
  "reason": "检查任务状态",
  "instruction": "查看 task_x / task_y 的状态...",
  "scheduled_for": "2026-11-15T10:35:00+00:00",
  "fire_count": 3,
  "max_runs": null,
  "tasks": [
    {"task_id": "task_x", "found": true, "status": "IN_PROGRESS"},
    {"task_id": "task_y", "found": false, "status": ""}
  ]
}
```

事件只描述事实，不写“你应该做什么”。

### 8.2 Handler 契约

```python
class WakeupHandler(Protocol):
    def can_wake(self, agent_id: str) -> bool: ...
    def wake(self, event: WakeupEvent) -> WakeupResult: ...
```

`WakeupResult`：

```text
outcome  replied / no_notify / skipped_busy / guard_limit /
         failed / rejected
summary  一句话
job_id   可选
```

### 8.3 V1 Handler

```text
FrontWakeupHandler
  → 创建 FrontWakeupJob
  → 复用 _front_lock
  → FrontAgentRuntime.wakeup_turn(...)
```

不经过 `communication_messages`。

### 8.4 以后的 Worker Handler

**Pi Worker**

```text
IDLE    → 可以进入一次 Life wake turn
WORKING → 不唤醒、不注入；
          记录 outcome = "skipped_busy"；
          下一次周期照常计算
```

**Solo Worker**

```text
IDLE    → 可以进入 Life wake turn
WORKING → 不唤醒、不注入；
          记录 outcome = "skipped_busy"
```

Worker handler 必须遵守：

- **只在 Agent 不工作时唤醒**；
- 不中断当前 LLM / Tool；
- **不把 WakeupEvent 注入 Work prompt / checkpoint**；
- 不改变 Task 状态；
- 不在 Work prompt 里注入经济 / 钱包；
- 唤醒结果写 Activity Record。

> 这条边界是有意的：闹钟属于 Life 层，不能改变 Worker 当前工作的上下文。
> 如果 Worker 正在工作，这次唤醒直接跳过；工作结束后不会补发。

---

## 9. Front 被唤醒后的行为

### 9.1 Prompt

Front prompt 增加：

```text
========== 系统闹钟（不是 Human 消息） ==========
alarm_id: ...
reason: ...
instruction: ...
task_ids: task_x, task_y
scheduled_for: ...
本次是第 3 / 24 次

这是系统定时唤醒，不是新的用户请求。
不要创建 Task；不要发起 handoff / rework / follow_up。

你可以：
- inspect / artifact_read / project_read / web_search
- alarm_list / alarm_cancel / alarm_create
- direct_reply：值得告诉人类时
- no_notify：不值得打扰人类时

你有 alarm_cancel 能力。
任务结束、目标不再需要检查，或你不再需要这个闹钟时，
请主动关闭它。系统不会因为 Task 结束而自动关闭。
```

### 9.2 允许的最终决定

V1 只允许：

```text
direct_reply   通知人类
no_notify      不通知聊天，但写 Activity Record
inspect        只读查看 Task 事实，继续判断
```

不允许：

```text
handoff        不能从闹钟创建新 Task
rework         不能从闹钟发起返工
follow_up      不能从闹钟推进 Task
cancel         不能从闹钟取消 Task
artifacts      不主动导出文件
commit_confirm 不能代替人类确认提交
set_mode       不修改全局模式
```

原因：闹钟是 Agent 自己的定时检查，不是新的 Human Demand。

### 9.3 `no_notify`

```json
{"intent": "no_notify", "reason": "状态没有变化，不需要打扰人类"}
```

行为：

- 不产生 Front 聊天消息；
- 写一条 `agent_wakeup_runs`，`outcome = "no_notify"`；
- UI 的“Agent 活动记录”显示这次检查；
- 不影响 Task 状态。

普通 Human 消息不能使用 `no_notify`。

### 9.4 Activity Record 与 UI

每次唤醒都写 Activity Record：

```json
{
  "run_id": "wake_...",
  "agent_id": "agent_a",
  "alarm_id": "alarm_...",
  "scheduled_for": "2026-11-15T10:35:00+00:00",
  "outcome": "no_notify",
  "summary": "task_x / task_y 状态未变化，未通知人类",
  "job_id": "job_..."
}
```

UI 建议：

- 独立的**消息通知图标**；
- 图标上显示未读活动记录数量；
- 点击后打开 Activity Record 列表；
- Front 卡片显示最近一次活动时间；
- 如果 `direct_reply`，聊天里出现 Front 消息，并带“定时检查”标签；
- 如果 `no_notify`，聊天不增加消息，但活动记录增加一行、通知计数增加。

未读计数建议：

- `agent_wakeup_runs` 保持 append-only；
- UI 在本地设置或单独的 `ui_activity_state` 中保存
  `last_seen_run_id` / `last_seen_at`；
- 未读数 = `last_seen_at` 之后的 run 数量；
- 不修改 Activity Record 本身。

具体图标位置、列表样式、已读交互后续单独讨论。

示例：

```text
10:30  Front 定时检查 · task_x, task_y
       状态未变化 · 未通知

10:35  Front 定时检查 · task_x
       task_x 已从 IN_PROGRESS 变成 COMPLETED · 已通知
```

---

## 10. 为什么不用系统的消息机制

这是本设计最重要的边界问题之一。

### 10.1 消息机制解决的是什么

现有 `CommunicationManager` / `communication_messages` / Inbox 解决的是：

```text
谁 → 对谁 → 说了什么 → 什么时候说的 → 是否送达 / 处理
```

它是**通信事实**的持久化与投递系统，包含：

- `from_agent` / `to_agent`
- `conversation_id` / `reply_to`
- 消息状态 / 事件 / claim lease
- Inbox 消费 / 去重

### 10.2 闹钟不是通信事实

闹钟是：

```text
Agent 自己 → 未来某个时刻 → 叫醒自己
```

它没有外部发送者，没有接收者，没有对话，没有“已读 / 未读”语义。

如果强行做成消息，会遇到：

| 问题 | 后果 |
|---|---|
| `from_agent` 填谁？ | 填自己 = 自己给自己发消息；填 system = 系统冒充发送者 |
| 未来消息是否提前写入？ | 会污染通信历史，出现“还没发生的事实” |
| 取消闹钟怎么办？ | 通信历史是 append-only，不能撤回未来消息 |
| `deliver_at` / 周期 / max_runs 放哪？ | 要把 `communication_messages` 改造成调度表 |
| `WakeupReason` 怎么区分？ | 所有定时唤醒都会变成 `MESSAGE`，破坏 `Wakeup ≠ Message` |
| Inbox consumer 怎么处理？ | 需要新增 alarm kind，且会与 Human/Agent 消息混在一起 |
| Activity 怎么记？ | 消息状态不等于 Agent 活动结果 |

### 10.3 备选方案对比

| 方案 | 优点 | 问题 | 结论 |
|---|---|---|---|
| A. 把闹钟写成未来 Message | 复用持久化 / Inbox / wakeup | 语义污染、需要 `deliver_at`、取消会改历史、破坏 `Wakeup ≠ Message` | ❌ |
| B. 到期时由 System 发一条 Message | 复用投递与唤醒 | 系统需要冒充发送者；每次检查都成为通信事实；与 Human/Agent 消息混淆 | ❌ |
| C. 独立 `agent_alarms` + `WakeupEvent` | 语义干净、可取消、可周期、可审计 | 需要新增 store / scheduler / dispatcher | ✅ V1 |
| D. 扩展 `AgentRuntime` 内存定时器 | 代码少 | 进程重启丢失；与持久化恢复冲突；生产 Front 不走它 | ❌ V1 |

### 10.4 复用什么，不复用什么

**复用**：

- `direct_reply` 仍然走现有 Human 消息 / Front 回复路径；
- 以后可以让 `AgentRuntime` 接收 `WakeupEvent`，但事件本身不是 Message；
- `CommunicationManager` 仍然负责 Human / Agent 的真实通信。

**不复用**：

- 不把 `agent_alarms` 写进 `communication_messages`；
- 不让 AlarmScheduler 产生 `WakeupReason.MESSAGE`；
- 不把 Activity Record 写成聊天消息。

---

## 11. 配置

建议新增：

```yaml
runtime:
  alarms:
    enabled: true
    min_interval_seconds: 60
    max_active_per_agent: 5
    guard:
      enabled: true          # 测试组合根开启；生产可关闭
      max_wakes_per_agent: 10
      max_wakes_total: 50
```

V1 只注册 Front handler；`enabled` 仍可整体关闭。

> `max_runs` / `expires_at` 不再有系统默认值。它们是 Agent
> 可选设置的参数；测试防过度工作由 `guard` 负责。

---

## 12. 失败与边界情况

| 情况 | 行为 |
|---|---|
| 进程重启 | 加载 ACTIVE；过期只补一次 |
| scheduler 崩溃在 RUNNING | 启动时恢复为 ACTIVE 或 FAILED，写 Activity |
| Front 被禁用 / 归档 | 不唤醒；闹钟保留，到期后仍 ACTIVE，等待恢复或过期 |
| Task 不存在 | 事件 `found=false`；是否停止由 Agent 决定 |
| Task 已结束 | **不自动停**；Agent 应在看到后 `alarm_cancel` |
| Worker 正在工作 | 不唤醒、不注入；`outcome = skipped_busy` |
| 达到测试 guard 上限 | 暂停闹钟；`outcome = guard_limit` |
| 多个 scheduler 同时触发 | 原子 claim + run 唯一约束，只有一个 dispatch |
| Front 正在处理 Human Job | 闹钟 Job 排队，`_front_lock` 串行 |
| 多个闹钟同时到期 | 串行 dispatch；可合并同一 Agent 的连续唤醒 |
| 告警失败 | 写 FAILED / Activity，不静默 |

---

## 13. 实施阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase W0 | 本文档 + 边界确认 | **`DESIGNED`** |
| Phase W1 | `agent_alarms` / `agent_wakeup_runs` + `AlarmStore` | 未实现 |
| Phase W2 | `AlarmScheduler` + `WakeupDispatcher` + 重启恢复 + `WakeupGuard` | 未实现 |
| Phase W3 | Front `alarm_create / list / cancel` + prompt | 未实现 |
| Phase W4 | `FrontWakeupHandler` + `no_notify` + Activity UI | 未实现 |
| Phase W5 | Pi / Solo `WakeupHandler` | `FUTURE` |

---

## 14. 测试与守卫

### 14.1 核心测试

- `once` / `every` 触发时间正确；
- `max_runs` / `expires_at` 停止；
- `task_ids` 为空 / 单个 / 多个；
- Task 结束 **不会**自动停止；
- `alarm_cancel` 幂等；
- 重启后 ACTIVE 闹钟仍存在；
- 过期闹钟只补触发一次；
- 多 scheduler 原子 claim 只有一个 dispatch；
- `agent_wakeup_runs` 唯一约束防重复；
- Front 忙时闹钟排队；
- Worker WORKING 时 `outcome = skipped_busy`，且不注入 Work prompt；
- WakeupGuard 超限后暂停闹钟，写 `guard_limit`；
- `no_notify` 不产生聊天消息，但产生 Activity Record；
- `direct_reply` 产生 Front 消息；
- 闹钟唤醒不允许 `handoff` / `rework` / `follow_up`；
- Worker handler 后续接入时，Work 状态不被闹钟改变。

### 14.2 守卫

- `agent_alarms` 不写入 `communication_messages`；
- `AlarmScheduler` 不创建 Task；
- `alarm_create` 不出现在 Work action set；
- `no_notify` 只允许在 alarm wakeup 中使用；
- `communication/` 不出现 Heartbeat / lease renewal 术语；
- Activity Record 不是聊天消息。

---

## 15. 已确认决策

1. `task_ids` 可选，最多 **5 个**；为空表示通用闹钟；
2. **不因为 Task 结束自动停止闹钟**；
3. Agent 必须被提示拥有 `alarm_cancel`，由 Agent 自行决定停止；
4. `no_notify` 不在聊天里刷消息，但必须有 Activity Record / UI 活动记录；
5. Wakeup 核心从第一天就是通用 Agent 能力；V1 只接 Front；
6. 不设计固定 System tick；使用 deadline-driven `AlarmScheduler`；
7. `max_runs` / `expires_at` 不再有系统默认值；测试防过度工作由
   `WakeupGuard` 负责；
8. Worker 只在 **不工作 / IDLE** 时唤醒，不注入 Work prompt 或 checkpoint；
9. Activity UI 使用独立通知图标 + 未读数量；具体 UI 后续讨论。

## 16. 仍待确认

1. `WakeupGuard` 的测试阈值（例如每 Agent 10 次 / 每次运行 50 次）；
   是否需要同时挂 token 预算，还是只数唤醒次数；
2. Activity 通知图标的已读水位存放位置（UI settings / 专用表）。

### 16.1 多进程 scheduler（`OPEN`）

V1 只假设一个 UI 进程持有 scheduler。多进程方案尚未决定。

候选方案：

| 方案 | 做法 | 优点 | 代价 |
|---|---|---|---|
| A. 单 scheduler owner | 只允许一个进程跑 scheduler | 简单、无重复 | 需要 owner 选举 / 锁文件 / DB 锁 |
| B. 每进程 scheduler + DB claim | 每个进程都可 tick，靠原子 claim 去重 | 无单点、实现直接 | 需要 DB 通知或低频轮询；会有空转 |
| C. 独立 scheduler 服务 | scheduler 独立进程 / 服务 | 职责最清楚 | 部署复杂度最高 |

暂不拍板。当前 `agent_alarms` 的原子 claim 已经为 B 留了基础；
但 V1 不应同时启动多个 scheduler。

### 16.2 Activity 已读水位（`OPEN`）

通知图标需要“未读数”。候选：

- UI 本地 `last_seen_at`；
- `ui_settings.json` 保存每个 Agent 的水位；
- 单独 `ui_activity_state` 表。

Activity Record 本身保持 append-only，不因“已读”被修改。
