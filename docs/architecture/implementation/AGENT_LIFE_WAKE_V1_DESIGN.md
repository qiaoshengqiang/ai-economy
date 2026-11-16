# Agent Life Wake V1 —— 消息 / 闹钟唤醒与 Life Turn

> **状态：V1 `CURRENT`（2026-11-16）**
>
> 本文定义 Agent 在 **不进入 Work / Task** 的情况下，如何因为
> 消息到达或闹钟到期而醒来、做一次很小的生活层处理，然后继续休眠。
>
> 相关文档：
> [`AGENT_WAKEUP_V1_DESIGN.md`](AGENT_WAKEUP_V1_DESIGN.md) ·
> [`AGENT_INBOX_V1_DESIGN.md`](AGENT_INBOX_V1_DESIGN.md) ·
> [`COMMUNICATION_MODEL.md`](../COMMUNICATION_MODEL.md) ·
> [`AGENT_CORE_MODEL_V1.md`](../AGENT_CORE_MODEL_V1.md) §22.27 ·
> ADR-0064

---

## 0. 一句话

**消息到达或闹钟到期 → Wakeup → 一次有边界的 Life Turn → 回复 / 发消息 / 管自己的闹钟 / 什么都不做。**

```text
Message arrival          Alarm due
       │                     │
       └──────────┬──────────┘
                  ▼
          LifeWakeService
      （空闲 / 限流 / 预算 / 会话轮数）
                  ▼
           LifeTurnRuntime
        （Agent Brain，一次小预算）
                  ▼
   reply / message_send / alarm_* / no_op
                  ▼
        Activity Record + Inbox 消费
```

---

## 1. 边界

```text
Alarm      ≠ Message       闹钟不是通信事实
Message    ≠ Task          消息到达不创建 Task
Wakeup     ≠ LLM Call      被唤醒不等于必须深度思考
Life       ≠ Work          Life Turn 不进入 Task / Workspace
Capability ≠ Permission    能发消息 ≠ 能发任何消息
Creator    ≠ Subject       设闹钟的人 ≠ 被唤醒的人
```

- Life Turn **不创建 Task**、不修改 Task 状态、不读写 Task Workspace；
- Life Turn 不给模型 shell / 文件工具；
- 模型只输出 JSON 信封，由 Runtime 校验后执行；
- `from_agent` 永远由 Runtime 注入，模型不能伪造；
- 委托闹钟的 `instruction` 是建议，不是命令；被唤醒者可以忽略或取消。

---

## 2. 触发源

### 2.1 消息到达

| Agent 类型 | 消息落点 | 唤醒来源 |
|---|---|---|
| Pi 引擎 | `agents/<id>/inbox/pi-inbox.md` | `LifeWakeService` 读取 inbox 文件 |
| 内置引擎 | `communication_messages` pending | `LifeWakeService` 扫描 pending |
| Front | Front InboxConsumer | 已有 Front Job / 中转逻辑 |

规则：

- 只处理 `message_type = TEXT` 的普通消息；
- 跳过 `system` / `human_gateway` 等系统发送者；
- Agent 忙（WORKING）时**不打断**，消息留在 Inbox，空闲后再唤醒；
- 同一 Agent 短时间多条消息合并成一次 Life Turn；
- 消费成功后才从 inbox / pending 中移除；失败保留，退避重试。

### 2.2 闹钟

- 自设闹钟：`agent_id == created_by`；
- 委托闹钟：`agent_id != created_by`，只有 Front 可以创建；
- 目标必须开启：

```yaml
life:
  enabled: true
  wake_on_alarm: true
  accept_delegated_alarms: true
```

- 目标忙时：`skipped_busy`，不补发（沿用 Agent Wakeup V1 语义）；
- 目标本人和创建者都可以取消。

---

## 3. Life Turn

### 3.1 输入

```text
Agent 身份 / 配置的 system prompt
唤醒类型：message | alarm | mixed
消息列表（from / text / conversation）
闹钟原因 / 指令 / 创建者 / 相关 Task 只读引用
```

不注入 Task / Workspace / Work checkpoint / 项目目录。

### 3.2 输出协议

模型只输出一个 JSON 对象：

```json
{
  "reply": "可选，回复给唤醒来源",
  "actions": [
    {"type": "message_send", "to": "agent_b", "text": "..."},
    {"type": "alarm_create", "reason": "...", "instruction": "...",
     "delay_seconds": 600},
    {"type": "alarm_cancel", "alarm_id": "alarm_..."}
  ]
}
```

- `reply` 回到唤醒来源：
  - 消息唤醒 → 唯一发送者；
  - 闹钟唤醒 → `created_by`；
  - 多个发送者时不猜，要求显式 `message_send`。
- 动作最多 3 个；
- 只允许 `message_send` / `alarm_create` / `alarm_list` / `alarm_cancel` / `no_op`；
- 只能给自己设闹钟；
- 只发 TEXT，不允许 Task 生命周期 / 协商消息；
- 普通文本输出降级为「只回复、无动作」。

### 3.3 执行

```text
LifeTurnRuntime
  → agent.generate(call_purpose="life", cognitive=False,
                   thinking=disabled, max_tokens=policy.max_tokens_per_turn)
  → 解析 / 校验
  → 执行动作
  → 写 agent_wakeup_runs
```

Life Turn 使用 Agent 自己的 **Brain**，与 Work 引擎无关：

- Pi Agent 也走 `Brain`，不启动 Pi CLI；
- 因此不需要给 Life Turn 文件 / shell / MCP 工具；
- 成本记在 Agent 自己的 `llm_calls` / 钱包。

---

## 4. 限流与防循环

```text
life.max_turns_per_hour          每小时最多 Life Turn 次数
life.min_interval_seconds        两次 Life Turn 最小间隔
life.max_tokens_per_turn         单次输出上限
life.max_cost_per_turn           单次成本告警线
life.max_conversation_turns      单 conversation 自动回复上限
```

- 同一 conversation 的自动生活消息达到上限 → 不再唤醒，消费并记 `throttled`；
- 超过每小时次数 → 暂停 60 秒再试；
- LLM 连续失败 → 指数退避（15s / 30s / ...，最多 5 分钟）；
- 已经产生成功副作用（发消息 / 建闹钟）后失败 → 消费，避免重试重复执行。

---

## 5. 配置

```yaml
agents:
  agent_e:
    life:
      enabled: true
      wake_on_message: true
      wake_on_alarm: true
      accept_delegated_alarms: true
      max_turns_per_hour: 20
      min_interval_seconds: 5
      max_tokens_per_turn: 1024
      max_cost_per_turn: 1.0
      max_conversation_turns: 6
      allowed_actions:
        - message_send
        - alarm_create
        - alarm_list
        - alarm_cancel
        - no_op
```

默认：

```text
life.enabled = false
life.wake_on_message = false
```

升级后不会让所有 Agent 突然开始消耗 token；按 Agent 显式开启。

---

## 6. Front 的两种角色

1. **发起者**：`message_send` / `alarm_create(agent_id=...)`；
2. **中转者**：Life 回复到 Front 时，Front Inbox 把 `payload.life=true`
   的回复转成人类可见的中转消息，不创建 Task / Job。

---

## 7. 可观测性

每次唤醒写 `agent_wakeup_runs`：

```text
kind       alarm | message
outcome    replied | no_notify | skipped_busy | throttled | failed
summary
detail     raw_output / reply / actions / errors / tokens / cost_ai /
           message_ids / conversation_id
```

`llm_calls.call_purpose = "life"`，与 Front / Work / Cognition 区分。

---

## 8. 非目标

- 不实现完整 Life Loop / 自主目标；
- 不允许 Life Turn 创建 Task / 改 Task 状态；
- 不提供 shell / 文件 / 项目目录工具；
- 不做群聊 / 广播 / 多 Agent 同时对话；
- 不做跨机器 Alarm / Wake（仍是单进程 scheduler）。

---

## 9. 验收

- `tests/test_life_turn.py`
- `tests/test_life_wake_service.py`
- `tests/test_life_controller.py`
- `tests/test_delegated_alarms.py`
- `tests/test_pi_comm.py`（peek / consume 不丢并发消息）
- 全量测试基线见 `PROJECT_STATUS.md`
