# ADR-0062：Front message_send 与 live follow_up

- **状态**：Accepted（**V1 已实现**）
- **时间**：2026-11-16
- **Supersedes**：无
- **Superseded by**：无

## Context

Front 原来只有 Task 级控制：

```text
handoff / rework / follow_up / interrupt / cancel
inspect / artifacts / private_* / draft_* / project_* / web_*
```

它没有 `message_send`，不能给 Agent 发普通消息。

同时存在语义不一致：

- `FrontIntent.FOLLOW_UP` 注释写“对进行中 Task 的追问 / 补充”；
- Front prompt 写“follow_up 用于进行中 / INTERRUPTED”；
- 但 Controller 对 **live task** 直接拒绝：

```text
任务 ... 正在执行中（IN_PROGRESS），完成后我会把结果告诉你。
```

于是 Front 可能承诺“我去转告”，实际什么也没投递。

## Decision

### 1. live follow_up 投递到 Inbox

`follow_up` 遇到 live task 时：

- 不创建新 Job；
- 不停止当前执行；
- 把 `task_text`（或 Human 原话）作为 `TEXT` 消息发给
  `task.contractor`；
- payload.kind = `task_follow_up`，带 `task_id` / `job_id`；
- 回复说明“已投递，Agent 会在下一个工作轮次 / 阶段边界读取”。

没有 live Job 时，保持原有恢复执行逻辑。

`rework` 对 live task 仍然拒绝：返工必须等任务结束。

### 2. Front 增加 `message_send`

Front 的 `message_send` 只允许发给**空闲** Agent：

- 正在执行任务的 Agent 返回 `agent_busy`；
- 给正在执行的任务补充要求必须用 `follow_up`；
- 不允许发送 Task 生命周期 / 协商消息：
  `TASK_OFFER` / `COUNTER_OFFER` / `ACCEPT` / `REJECT` /
  `START` / `SUBMIT` / `VERIFY` / `COMPLETE` / `PAYMENT_REQUEST`；
- 不允许发给自己；
- 消息可以是 `text` 或 `payload`。

Front 仍然不是 Task 参与者，不能代替 contractor / acceptor
发送生命周期消息。

### 3. 异步投递

两种投递都不打断当前 LLM 调用：

- 内置引擎：在 run 开始 / 每轮 work 前 `_drain_inbox()`；
- Pi 引擎：在 phase / turn 边界读取 `pi-inbox.md`；
- 空闲 Agent：消息留在 Inbox，等下一次任务 / Life 消费。

## Consequences

**正面**：

- Front 终于可以把补充要求转给正在执行的任务；
- 普通消息与 Task 控制分离，避免用 `handoff` / `follow_up` 做所有事；
- 明确禁止 Front 发送生命周期消息，不绕过 Task Core；
- live follow_up 不创建新 Job，不改变 Task 状态。

**负面 / 代价**：

- 消息是异步的，不能立即打断当前步骤；
- 如果 Agent 在读取 Inbox 前完成任务，消息可能未被消费；
- `message_send` 到空闲 Agent 也不保证立即读取；
- 需要维护忙碌判定与消息类型黑名单。

**因此不能做的事**：

- 不能把 `message_send` 当作 Task 状态变更通道；
- 不能让 Front 通过 `message_send` 创建 Task 或发 SUBMIT/COMPLETE；
- 不能承诺“立即转告正在执行的 Agent”。

## 备选方案

1. 只改 follow_up 文案，不实现 live 投递：语义一致但仍无能力；
2. 允许 `message_send` 发给运行中 Agent：与 follow_up 职责重叠；
3. 让 follow_up 停止并重启 Task：会丢失当前执行上下文；
4. 只让 Human 发消息：Front 无法承担入口职责。

## 证据

- `tests/test_front_actions.py::test_front_can_message_idle_agent`
- `tests/test_front_actions.py::test_front_message_send_rejects_busy_and_lifecycle`
- `tests/test_work_resume.py::test_live_follow_up_is_delivered_to_inbox`
- `tests/test_front_agent.py::test_front_prompt_is_human_facing_and_does_not_invent_constraints`
