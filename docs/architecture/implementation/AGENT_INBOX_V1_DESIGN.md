# Agent Inbox V1 —— Runtime 收件箱接入设计

> **状态：Phase 0–4 `CURRENT`；Phase 5 V1 `CURRENT`，其余 `DESIGNED`（2026-11-15）**
>
> Phase 0（边界 / flag / legacy cutover）、Phase 1（持久化 Inbox Core /
> 统计 / API）、Phase 2（Pi 持久 Inbox）、Phase 3（Front Inbox-first）
> 与 Phase 4（内置引擎入队 / checkpoint）已实现；
> Phase 5 V1（空闲内置引擎 Agent 可选自动回复）已实现，
> Conversation / 邀请 / 社交 / 经济通信仍为 `DESIGNED`。
>
> 相关文档：
> [`COMMUNICATION_MODEL.md`](../COMMUNICATION_MODEL.md) ·
> [`LIFE_WORK_ARCHITECTURE.md`](../LIFE_WORK_ARCHITECTURE.md) ·
> [`UI_COMMUNICATION_INTEGRATION.md`](../UI_COMMUNICATION_INTEGRATION.md) ·
> ADR-0014 · ADR-0055

---

## 0. 实施状态

| 阶段 | 状态 | 说明 |
|---|---|---|
| Phase 0：边界与迁移准备 | **`CURRENT`** | `runtime.inbox_enabled` · `communication_inbox_state` · legacy cutover |
| Phase 1：持久化 Inbox Core | **`CURRENT`** | pending / IGNORED / stats / startup recovery / `/api/inbox` |
| Phase 2：Pi 持久 Inbox | **`CURRENT`** | `PiInboxService` 常驻消费；写入 `agents/<id>/inbox/` |
| Phase 3：Front Inbox-first | **`CURRENT`** | Human → Front inbox → Job dispatch |
| Phase 4：内置引擎入队 / Life | **`CURRENT`** | 内置引擎只入队；work checkpoint 注入；默认不自动回复 |
| Phase 5：Life 通信 | **V1 `CURRENT`** | 空闲内置引擎 Agent 可选自动回复；其余 `DESIGNED` |

> Phase 1 的 `communication_inbox_state` 是第 **30** 张表。
> `runtime.inbox_enabled` 当前为 `true`；
> Front 与 Pi 引擎 Agent 已启用常驻消费，内置引擎只入队 / checkpoint 注入。

---

## 0.1 一句话

把 Inbox 从「通信层有一个 `receive()` API」提升为
**由 Agent Runtime 持有、持久化、可在 checkpoint 处理的收件箱**，
为 Life 层的空闲通信、邀请、社交和经济消息打基础。

```text
Communication 是 Agent 的基础能力
Task 只是 Communication 的一个使用场景
Inbox 属于 Runtime，不属于某次 LLM 调用
Message 不能因为 Agent 正在思考或执行而丢失
```

---

## 1. 现状

### 1.1 已经存在（`CURRENT`）

| 能力 | 状态 | 位置 |
|---|---|---|
| 每个 Agent 的本地信箱 | `CURRENT` | `LocalTransport.open_mailbox()` |
| 消息投递 / Journal / DB | `CURRENT` | `CommunicationManager.send()` · `CommunicationRecorder` |
| 收件箱消费 API | `CURRENT` | `CommunicationManager.receive()` |
| 去重 / claim / 租约 | `CURRENT` | `communication_messages` · ADR-0014 |
| 单 Agent Runtime 循环 | `CURRENT` | `communication/agent_runtime.py` |
| Pi 临时 Inbox Bridge | `CURRENT` | `work/pi_comm.py` |
| 默认 Agent handler | `CURRENT` | `BaseAgent.handle_message()`（默认只记录，不回复） |

### 1.2 缺口

| 缺口 | 影响 |
|---|---|
| UI 生产路径没有启动 `AgentRuntime.run_forever()` | Front / 内置引擎的信箱无人消费 |
| Pi Inbox Bridge 只在任务运行期间启动 | 空闲 Pi 引擎 Agent 的消息延迟到下一次任务 |
| 生产 Agent 没有注册 `on_message` handler | 即使取到消息，也不会进入 Brain |
| Human Gateway 与 controller 双路径 | 同一条 Human 消息在信箱里有一份，被直接使用一次 |
| 没有启动时重放 | 进程重启后，未消费的内存队列消失 |
| Task lifecycle 消息与 Agent inbox 混在同一张表 | 直接启动消费者可能重复执行 Task Core |

### 1.3 当前数据库证据（`CURRENT`）

`runs/ui/aieos.db` 中：

```text
agent_a 收件箱：
  human_gateway → TEXT     DELIVERED 25
  agent_b/c/d/o → ACCEPT   DELIVERED 11
  agent_b/c/d/o → ERROR    DELIVERED 7
  processed_at = NULL

agent_b 收件箱：
  TASK_OFFER DELIVERED 4
  TASK_OFFER PROCESSED 2
```

结论：

```text
邮箱存在
消息存在
Front 没有消费
Pi 只在任务期间消费一部分
```

---

## 2. 目标与非目标

### 2.1 目标

1. Inbox 由 Agent Runtime 持有，独立于当前 LLM 调用；
2. 消息持久化，进程重启后未处理消息可恢复；
3. Agent 在安全 checkpoint 处理收件箱，不中断当前 LLM / Tool；
4. Front 与 Pi 首先接入，内置引擎先只入队；
5. 未来 Life 层可以在没有 Task 的情况下收发消息；
6. 不改变 Task Core 的状态权威；
7. 不让系统冒充 Agent 发送 Agent 消息。

### 2.2 非目标（V1）

- 不实现完整 Conversation Manager；
- 不实现 Heartbeat / Scheduler；
- 不实现 Agent 自主社交策略；
- 不实现跨机器 / P2P Inbox；
- 不自动重放接入前的历史消息；
- 不让空闲 Agent 默认自动回复；
- 不改变 `protocol_version = "1"`。

---

## 3. 已确认决策

### D1. Human → Front 最终完全走 inbox

```text
Human
  → HumanGateway
  → Front Inbox
  → Front Runtime
  → Job / Decision
```

`CURRENT` 的 controller 直接调用 `FrontAgentRuntime.decide_turn()`
属于过渡路径，Phase 3 删除。

### D2. 旧消息不重放

接入前已经存在但未处理的 `DELIVERED` 消息：

```text
标记为 IGNORED / legacy
保留审计
不进入业务处理
```

原因：当前 Front 有 25 条旧 human 消息、11 条 ACCEPT、7 条 ERROR。
直接重放会重复建 Task、重复花钱。

### D3. 空闲 Agent 默认不自动回复

```text
reply_mode = never  （默认）
```

Agent 可以接收和记录消息，但不自动调用 LLM 回复。
自动回复必须由 Agent 级策略显式开启，并受轮数 / 预算 / 频率限制。

### D4. 第一版只接 Front + Pi

```text
Front   → 常驻 Inbox Runtime
Pi      → 持久 Inbox Bridge
内置引擎    → 只入队 / 观测，不自动调用 Brain
```

内置引擎在没有 Task 时的自主思考属于 Life 层后续能力。

---

## 4. 目标模型

### 4.1 两层 Inbox

```text
Transport Inbox（持久投递队列）
  communication_messages
  DELIVERED → CLAIMED → PROCESSED / FAILED

Agent Inbox（Runtime 持有的待处理队列）
  Front / 内置引擎：Runtime pending queue
  Pi：tasks/<task_id>/.aieos/pi-inbox.md 或
      agents/<id>/inbox/
```

V1 不新增“每条消息的 Agent 阅读状态表”。
`communication_messages.processed_once` 表示
**已被 Runtime 认领并交给 Agent / Adapter**，不表示 Agent 已经行动。

### 4.2 Agent 状态

```text
IDLE       可以立即处理 inbox
WORKING    正在执行 Task；消息暂存，在 checkpoint 处理
THINKING   LLM 调用中；不中断当前调用
```

### 4.3 处理时机

```text
Front IDLE
  → 立即消费

Pi Task 运行中
  → 在 phase / turn 边界读取

内置引擎 Task 运行中
  → 不消费；等任务结束或显式 checkpoint

Agent 空闲但未启用 inbox
  → 消息留在持久队列
```

---

## 5. 数据模型

### 5.1 复用 `communication_messages`

V1 不新建消息表。现有字段已足够：

```text
message_id · trace_id · conversation_id
from_agent · to_agent · message_type
payload · created_at · status
processed_once · processed_at
claim_id · claimed_at · failure_count
```

新增查询：

```text
list_pending(to_agent, after_cutover, limit)
mark_ignored(message_id, reason)
inbox_stats(to_agent)
```

### 5.2 `communication_inbox_state`（`CURRENT`）

每个 Agent 一行，控制是否启用与 legacy 边界：

```sql
communication_inbox_state (
    agent_id     TEXT PRIMARY KEY,
    enabled      INTEGER NOT NULL DEFAULT 0,
    cutover_at   TEXT NOT NULL,
    last_seen_at TEXT,
    updated_at   TEXT NOT NULL
)
```

用途：

- `enabled`：Phase 2 只开 Front + Pi；
- `cutover_at`：早于它的消息不进入 pending；
- `last_seen_at`：观测。

### 5.3 旧消息迁移

实施时执行一次显式迁移：

```text
所有 created_at < cutover_at 且 processed_once = 0 的消息
  → status = IGNORED
  → 保留 payload / 审计
  → processed_once 保持 0（不伪造“已处理”）
```

`list_pending` 只返回：

```text
status IN ('DELIVERED', 'FAILED')
AND processed_once = 0
AND created_at >= cutover_at
```

---

## 6. 组件与职责

### 6.1 `InboxRuntimeService`（新增）

```text
一个后台线程 + 一个 asyncio event loop
  → 为每个启用 inbox 的 Agent 运行一个 AgentRuntime.run_forever()
  → handler 通过 asyncio.to_thread() 执行阻塞 LLM / Job
  → 提供 start / stop / health / stats
```

不新增线程 per Agent，避免未来 Agent 数量增长后线程爆炸。

### 6.2 `AgentRuntime`（复用）

保留：

- 每个 Agent 一个处理循环的互斥；
- wakeup → receive → handler；
- 状态 `SLEEPING / RUNNING`。

扩展：

- 处理结果 `InboxOutcome`；
- 异常时 `mark_failed`，不阻塞后续消息；
- 支持 `DEFERRED`（暂不处理，下次 checkpoint 再来）。

### 6.3 Handlers（新增）

```text
work/inbox_handlers.py

FrontInboxHandler
  - human_message
  - handoff_accept / handoff_error
  - 未来 Worker → Front 的 Agent 消息

PiInboxHandler
  - 写入持久 pi-inbox
  - 任务运行时由 Pi 读取

BuiltinInboxHandler
  - V1 只记录 / 计入 unread
  - 不自动调用 Brain
```

### 6.4 `BaseAgent.handle_message`

`CURRENT` 默认只记录、不回复，保留。

生产路径必须显式注册 handler；不能把默认行为改成自动调用 Brain，
否则两个 Agent 会自动互回，形成无限 LLM 循环。

### 6.5 `TaskService`

V1 **不消费** `task_service` 邮箱。

原因：`TaskLifecycleClient` 当前是：

```text
发送 START / SUBMIT / VERIFY / COMPLETE
  → 直接调用 TaskService handler
  → recorder.mark_processed()
```

如果再启动邮箱消费者，旧消息或重复消息可能二次执行 Task Core。
Task lifecycle 消息驱动的改造属于后续独立阶段。

---

## 7. 分阶段实施

### Phase 0：边界与迁移准备

- 写本设计文档；
- 增加 `runtime.inbox_enabled = false`；
- 定义 `InboxOutcome` / handler 契约；
- 定义 `communication_inbox_state`；
- 准备 legacy 迁移命令；
- 不启动消费者。

**验收**：文档合并；无行为变化。

### Phase 1：持久化 Inbox Core

- 实现 `list_pending` / `mark_ignored` / `inbox_stats`；
- 启动时恢复 cutover 之后的 pending 消息；
- 旧消息标记 IGNORED；
- 增加 `/api/inbox/<agent_id>`；
- feature flag 仍为 false。

**验收**：

```text
[ ] 发消息 → 重启 → 仍可消费
[ ] 重复投递只处理一次
[ ] claim 租约过期可重新认领
[ ] 旧消息不会被重放
[ ] 无消费者时 pending 数量正确
```

### Phase 2：Pi 持久 Inbox（`CURRENT`）

- `PiInboxService` 在 UI 组合根启动；
- 每个 Pi 引擎 Agent 一个常驻 `PiInboxBridge`；
- 空闲 Pi 引擎 Agent 收到消息 → `agents/<id>/inbox/pi-inbox.md`；
- Pi 下一次 phase / turn 读取；
- 只启用 Pi 引擎 Agent 的 `communication_inbox_state.enabled`；
- Front / 内置引擎状态仍为 disabled。

**验收**：

```text
[ ] 空闲 Pi 引擎 Agent 收到消息，不丢
[ ] 下一次任务开始时能看到
[ ] 任务运行中不中断当前 LLM
[ ] 消息处理后 status / processed_at 正确
```

### Phase 3：Front Inbox-first（`CURRENT`）

- `AIEOSController.submit()` 在 Front Inbox 可用时只创建 Job + 发送 human_message；
- `InboxConsumer` 常驻消费 Front 信箱；
- `_on_front_inbox_message()` 按 `job_id` 找到 Job，再交给 executor；
- 重复消息通过 `job.status != RECEIVED` 防重；
- 没有 Front Consumer 时保留 direct executor 回退（测试 / CLI）；
- 启用 Front 的 `communication_inbox_state.enabled`。

**验收**：

```text
[ ] 一条 Human 消息只产生一次 Front decision
[ ] 只创建一个 Job
[ ] 不重复回复
[ ] Front 不可用时 Job 保持 QUEUED，不丢
[ ] UI 能显示 pending / processing
```

### Phase 4：内置引擎入队与 Life 准备（`CURRENT`）

- 内置引擎 Agent 启用 inbox 后只记录 unread；
- 不启动常驻消费者；
- `BuiltinWorkRuntime` 在 run 开始 / 每轮 work 前 drain pending；
- 注入后 `mark_processed_id()`；
- 新增 `InboxPolicy`（默认 `reply_mode = never`），但尚未自动回复；
- 内置引擎消息不会被自动转成 Task 或 Brain 调用。

**验收**：

```text
[ ] 内置引擎 Agent 不再“永远看不到消息”
[ ] 不自动创建 Task
[ ] 不自动调用 Brain
[ ] 重启后消息仍在
```

### Phase 5：Life 层通信（V1 `CURRENT`）

V1 已实现：

- `LifeInboxService` 只处理 `payload.kind = "agent_message"`；
- 只处理 **IDLE** 的非 Front / 非 Pi 引擎 Agent；
- 只有 Agent 配置 `inbox.reply_mode = auto` 才回复；
- 默认 `reply_mode = never`；
- 每个 conversation 最多 `max_auto_replies_per_conversation` 次；
- 单条回复超过 `max_cost_per_message` 只记录 warning；
- 回复通过 CommunicationManager 发出，不创建 Task，不改 Work 状态；
- 连续失败 3 次后标记 IGNORED，避免无限重试。

示例配置：

```yaml
agents:
  agent_x:
    inbox:
      reply_mode: auto
      max_auto_replies_per_conversation: 3
      max_cost_per_message: 0.1
```

仍未实现：

- Pi 引擎 Agent 的空闲自动回复（Pi 持久 Inbox 已消费消息，V1 不叠加）；
- Conversation Manager；
- 邀请协议；
- 社交 / 经济消息的专门策略；
- Heartbeat / 定时唤醒；
- 完整 Life / Work 状态切换。

---

## 8. Handler 契约

```python
@dataclass
class InboxOutcome:
    action: str          # processed | deferred | ignored | reply_sent | failed
    reply: str = ""
    error: str = ""
    next_checkpoint: str = ""
```

规则：

1. 一个 message_id 最多成功处理一次；
2. handler 抛异常 → `mark_failed`，`processed_once` 保持 0；
3. `deferred` 不标记 processed，留到下一 checkpoint；
4. handler 不得直接修改 Task Core；Task 状态仍通过 TaskService；
5. handler 不得冒充其他 Agent 发消息；
6. 阻塞 LLM 调用必须 `asyncio.to_thread()`。

---

## 9. Front / Pi / 内置引擎 / TaskService 边界

| 邮箱 | V1 是否消费 | 说明 |
|---|---|---|
| Front | ✅ Phase 3 | Human / handoff 回复 |
| Pi Worker | ✅ Phase 2 | 持久 pi-inbox |
| 内置引擎 Worker | ⚠️ 只入队 | 不自动 Brain |
| TaskService | ❌ | TaskLifecycleClient 已同步处理 |
| HumanGateway | ❌ | UI 从 Journal 读，不消费自己的信箱 |

Task 完成通知仍然是 **system message**，不是 Agent 消息。
系统不冒充 Worker 给 Front 发消息。

---

## 10. UI 与观测（Phase 1 `CURRENT`）

已提供：

```text
GET /api/inbox/<agent_id>
GET /api/inbox/stats
```

API 只读；不会启动消费者。

Agent 卡片显示：

```text
inbox_unread / inbox_failed
```

Inbox 面板显示：

```text
message_id · from · type · status · created_at
payload 摘要
processed_at / error
```

不显示 secret；payload 按现有 Journal 的脱敏规则处理。

---

## 11. 测试矩阵

```text
[ ] pending 查询
[ ] 重启后恢复
[ ] 去重 / 幂等
[ ] claim 租约
[ ] handler 异常 → FAILED
[ ] deferred 不丢
[ ] legacy 不重放
[ ] Front 单路径
[ ] Pi 空闲收件
[ ] Task lifecycle 不重复执行
[ ] 内置引擎只入队
[ ] 默认不自动回复
[ ] UI stats
```

---

## 12. 风险与回滚

| 风险 | 处理 |
|---|---|
| 旧消息重放导致重复 Task | cutover + IGNORED 迁移 |
| Human 双路径重复 | Phase 3 一次只启用一条 |
| Task lifecycle 二次执行 | V1 不消费 task_service |
| 自动回复无限循环 | 默认 never + 轮数 / 预算 / 频率 |
| 进程重启丢队列 | DB pending + 启动恢复 |
| Agent 忙时阻塞消息 | 只入队，checkpoint 处理 |
| Front 被 Worker 消息淹没 | 后续增加批处理 / 合并 |
| 消息内容 prompt injection | 视为数据，不作为指令 |
| handler 阻塞 event loop | `asyncio.to_thread()` |

回滚：

```text
runtime.inbox_enabled = false
  → 停止 InboxRuntimeService
  → 回到当前 controller 直接路径
```

不删除 DB 中的消息与审计。

---

## 13. 实施顺序（建议）

```text
1. Phase 0 文档 + 状态表 + flag
2. Phase 1 recorder 查询 / 恢复 / legacy
3. Phase 2 Pi 持久 inbox
4. Phase 3 Front inbox-first
5. Phase 4 内置引擎入队
6. Phase 5 Life 通信
```

每一步完成后跑全量测试，并保留前一步的回滚开关。

---

## 14. 非目标（再次强调）

- 不把 Inbox 做成 Task 通知总线；
- 不让系统冒充 Agent 发消息；
- 不让 Agent 自动回复变成默认行为；
- 不一次性重构 Task lifecycle；
- 不在 V1 实现 Heartbeat / Scheduler / P2P / Conversation Manager。
