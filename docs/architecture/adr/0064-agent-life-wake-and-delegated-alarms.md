# ADR-0064：Agent Life Wake 与委托闹钟

- **状态**：Accepted（**V1 已实现**）
- **时间**：2026-11-16
- **Supersedes**：无
- **Superseded by**：无

## Context

Agent Wakeup V1 已经有持久化闹钟、deadline-driven 调度器和
Activity Record，但：

- `alarm_create` 只允许 Front 给自己设闹钟；
- `WakeupDispatcher` 只注册了 Front；
- Pi 的消息只写进 `agents/<id>/inbox/pi-inbox.md`，没有东西把它变成
  一次 Agent 处理；
- 内置引擎的 `LifeInboxService` 只做 `reply_mode=auto` 的简短回复，
  且明确排除 Pi。

真实后果（2026-11-16）：

```text
Human → Front: 给小伊问声好
Front → agent_e: msg_bd0716c2
agent_e inbox: 文件里有消息
agent_e: IDLE，无 Life Turn
人类：没有收到回复
```

所以「Front 能给其他 Agent 设闹钟」本身不够；还缺 **消息到达唤醒**
和 **Worker Life Turn**。

## Decision

### 1. 消息到达和闹钟到期都走 Wakeup

```text
Message arrival / Alarm due
        ↓
LifeWakeService（空闲 / 限流 / 预算 / 会话轮数）
        ↓
LifeTurnRuntime（Agent Brain，一次小预算）
        ↓
reply / message_send / alarm_create / no_op
```

- 消息唤醒只在 Agent IDLE 时执行；WORKING 时不打断，消息留在 Inbox；
- 闹钟唤醒忙时 `skipped_busy`，不补发；
- 同一 Agent 的消息合并成一次 Life Turn；
- 成功后才消费 Inbox；失败保留并退避。

### 2. Life Turn 是受控信封，不是 Work

模型只输出 JSON：

```json
{"reply": "...", "actions": [
  {"type": "message_send", "to": "agent_b", "text": "..."},
  {"type": "alarm_create", "reason": "...", "instruction": "..."}
]}
```

- 不创建 Task、不修改 Task Workspace；
- 不提供 shell / 文件 / 项目目录工具；
- 只允许普通 TEXT，不允许 Task 生命周期 / 协商消息；
- 只能给自己设闹钟；
- `from_agent` 由 Runtime 注入，模型不能伪造；
- Life Turn 使用 Agent 自己的 Brain，成本记在该 Agent 名下。

### 3. 委托闹钟

`agent_alarms` 已有 `created_by`；V1 把它从硬编码改为显式：

```text
agent_id   = 被唤醒者
created_by = 创建者
```

- 只有 Front 可以创建 `agent_id != created_by` 的委托闹钟；
- 目标必须显式开启：

```yaml
life:
  enabled: true
  wake_on_alarm: true
  accept_delegated_alarms: true
```

- 目标本人和创建者都可以取消；
- 目标自己的闹钟对别人不可见；Front 只能列出自己创建的委托闹钟。

### 4. 默认关闭

`life.enabled = false`、`life.wake_on_message = false`。
升级后不会让所有 Agent 突然开始消耗 token；按 Agent 显式开启。

## Consequences

**正面**：

- Agent 可以对消息作出反应，不再只是把消息写进文件；
- Front 可以安排未来唤醒，而不需要自己反复醒来；
- Life 与 Work 保持隔离，不污染 Task 上下文；
- 不需要给 Life Turn 文件 / shell 权限。

**负面 / 代价**：

- 消息到达可能触发一次小 LLM 调用；
- 需要限流、预算和会话轮数防止 Agent 互聊死循环；
- Pi 的 `pi-inbox.md` 仍是文件型队列，消息的「已读」语义需要
  额外 Activity 观察；
- 单进程 scheduler / LifeWakeService，多进程需要后续租约设计。

**因此不能做的事**：

- 不能用闹钟冒充消息；
- 不能让 Life Turn 创建 Task 或写 Task Workspace；
- 不能按 provider / adapter 一刀切开关 Life；
- 不能让 Front 冒充被唤醒者，或查看别的 Agent 的自设闹钟。

## 备选方案

**A. 只允许 Front 自设闹钟，闹钟到期后 Front 再发消息给目标**
- 仍然需要目标侧 Wakeup；而且 Front 每次都要醒来烧 token，
  不符合「让目标自己处理」的模型。

**B. 把消息到达做成新 Task**
- 违反 `Message ≠ Task`；普通问候不应该产生工作单元。

**C. 给 Pi Life Turn 开放 shell / 文件工具**
- 风险过高，且 Life 层没有必要读写项目文件。

## 证据

- 失败场景：`msg_bd0716c2` 落入 `agents/agent_e/inbox/pi-inbox.md`，
  Agent 未醒来；
- 修复后真实运行：`agent_e` 产生 `call_purpose=life` 调用，
  `agent_wakeup_runs.kind=message`、`outcome=replied`，
  回复经 Front 中转进入人类聊天；
- `tests/test_life_turn.py` / `tests/test_life_wake_service.py` /
  `tests/test_life_controller.py` / `tests/test_delegated_alarms.py`；
- 全量测试基线见 `PROJECT_STATUS.md`。
