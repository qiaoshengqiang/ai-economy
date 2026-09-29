# ADR-0021：Write Lock 采用显式持有模型 + Human Force Unlock

- **状态**：Accepted（架构决策；**实现为 FUTURE**）
- **时间**：Write Lock Recovery & Future Guardian Agent
- **相关**：ADR-0014（消息认领租约）· ADR-0020（Task Workspace 共享资源模型）
- **规范**：[`TASK_WORKSPACE.md`](../TASK_WORKSPACE.md) §16

## Context

ADR-0020 确立了 Task Workspace 的核心模型：

```
shared resources + Permission + Write Lock
```

其中 **Write Lock（写独占）是第一次引入的能力** ——
它要求「同一可写资源同一时刻只能由一个 Agent 修改」。

于是立刻出现一个必须回答的问题：

> **如果 Agent 持有 Write Lock 时崩溃 / 失联，锁怎么恢复？**

当时把它记为 **OPEN DESIGN QUESTION / 硬阻塞项**，理由是：

```
Message Claim Lease
    ≠
Workspace Write Lock
```

并在 [`TASK_WORKSPACE.md`](../TASK_WORKSPACE.md) §16.2 记录了为什么不同 ——
**但没有决定 v1 用什么。**

这个问题不解决，Workspace 实现就无法开始 ——
因为**锁的恢复语义决定了锁的数据结构**：

- 如果有租约 → 锁需要 `expires_at` 字段 + 续租路径
- 如果没租约 → 锁只需要 `owner` + `state`

## Decision

### 一、v1 Write Lock 采用**显式持有模型**，不采用自动时间失效

> **Write Lock 不因为 Agent 长时间没有操作而自动失效。**

**明确禁止**把 `5 分钟` / `10 分钟` / `300 秒` 之类的固定时间
直接作为「**Agent 已经死亡**」的判断依据。

### 二、v1 明确不实现

| ❌ | |
|---|---|
| 时间 Lease | |
| Lease Renewal | |
| Heartbeat | |
| 自动 Lock Expiry | |
| 单纯基于 inactivity 的 Crash Detection | |
| 自动判断 Agent 已死亡 | |

### 三、正常释放

```
Agent A → LOCK → 工作 → UNLOCK
```

**只有 Lock Owner 可以释放自己的 Lock。**

```
A owns Lock X → A: UNLOCK X → SUCCESS
B: UNLOCK X                → DENIED
```

### 四、异常恢复 v1 = **Human Force Unlock**

```
Human / Authorized Operator
    ↓
Force Unlock
    ↓
Lock Revoked
    ↓
Resource becomes available
```

> **Force Unlock 是人为治理操作，不是系统根据「超时」自动执行。**

### 五、Force Unlock 是高权限操作

| 操作 | 条件 |
|---|---|
| `UNLOCK` | `caller == current_lock_owner` |
| `FORCE_UNLOCK` | **authorized governance actor**（v1 = Human / Authorized Human Operator） |

**普通 Agent 不得因为拥有 `WRITE` 就自动拥有 `FORCE_UNLOCK`。**

### 六、Force Unlock 必须可审计

定义 **Lock Revocation / Force Unlock Event**，至少表达：

```
lock_id · workspace_id · resource_id · previous_owner
revoked_by · revoked_at · reason
```

（字段名可留待实现阶段；**核心是必须留下可追踪的治理记录**。）

### 七、Lock 状态只有三种

```
ACTIVE    当前有效锁
RELEASED  持有者正常释放
REVOKED   由治理操作强制解除
```

> ⚠️ **不加 `EXPIRED`** —— 那会暗示当前存在自动 Lease。

### 八、Guardian / Observer Agent 是 `FUTURE`

未来可以有专门的 Observer/Guardian Agent，
综合多个运行信号形成**评估**（`NORMAL` / `WAITING` / `LONG_RUNNING` /
`SUSPECTED_FAILURE` / `UNRESPONSIVE` / `UNKNOWN`），
并**提出治理建议 / Recovery Request**。

但它必须经过 `Policy / Authorization` 才能产生 `Actual Action`。

**本次不实现 Guardian Agent 实体代码。**

## Why not automatic lease

这是本 ADR 最重要的部分。

### 1. 「没有活动」不能可靠证明 Agent 已经崩溃

持有写锁的 Agent **可能长时间没有 CPU 活动**：

- 在等 LLM 响应
- 在「思考」
- 在等外部 API
- 在等其他 Agent
- 在做长时间任务

```
Message Claim 的持有者：一个活跃的 worker
    → 「没有活动」≈ 出问题了

Write Lock 的持有者：一个可能在想事情的 Agent
    → 「没有活动」完全正常
```

**基于时间租约的恢复机制恰恰依赖「没有活动 = 已崩溃」这个假设。**
对 Write Lock 来说，**这个假设不成立**。

### 2. 失败模式不对称，而且 Workspace 那一侧更危险

| | 租约太短 | 租约太长 |
|---|---|---|
| **Message Claim** | 重复处理（幂等可吸收，**可见**） | 消息卡住（可见） |
| **Write Lock** | **丢失写入 / 静默数据损坏** | 资源被阻塞（可见） |

如果强行用时间租约：

```
A 还在写（正在等 LLM 生成）
    ↓ 锁过期
B 拿到锁开始写
    ↓
A 的写入覆盖 B 的（或反过来）
    ↓
**静默数据损坏** —— 可能很久以后才发现
```

「消息被重复处理」和「文件被静默写坏」不是一个量级的后果。

### 3. 续租（心跳）当前被明确禁止

「租约 + 显式续租」能区分「活跃但慢」与「已崩溃」——
但它**需要心跳机制**，而心跳在当前阶段**是被明确禁止的**。

> 不能为了让租约方案成立，反向把禁止的东西提前实现。

### 4. 观察不等于结论

这条本身就是一个已经确立的架构原则：

```
Observation   ≠   Conclusion
```

与 Constitution §9 的 **`Agent Inference ≠ Domain Fact`** 同源。

```
Agent A 最近 2 小时没有修改文件
    → 系统只能知道：last activity = 2h ago
    → 不能推出：Agent A = DEAD          ❌
```

**系统不假装知道 Agent 是否「死了」。**

## Why Human Force Unlock

如果既没有自动过期，又不能自动判断崩溃，那异常情况怎么办？

**用治理，而不是用猜测。**

| 方案 | 为什么是它 / 不是它 |
|---|---|
| **Human Force Unlock** ✅ | 人类有**系统外**的信息（知道那个进程真的崩了）。这是唯一能可靠判断的来源。 |
| 自动超时 ❌ | 见上 —— 会把「慢」误判成「死」，代价是静默数据损坏 |
| 自动崩溃检测 ❌ | 需要分布式 liveness，极复杂；且仍然可能误判 |
| 乐观并发（不锁） ❌ | 与 ADR-0020 §2.3「写必须独占」矛盾 |

**核心判断**：

> 当系统**无法可靠判断**时，
> 正确的做法是**把判断权交给有更多信息的一方**（人），
> 而不是**猜一个**然后承担静默损坏的代价。

**代价是明确的**：崩溃的 Agent 会一直占着锁，直到有人 Force Unlock。
这是一个**可见的、可治理的**阻塞 —— 比**不可见的**数据损坏好。

## Why Guardian is future

Guardian / Observer Agent 是「让系统逐渐获得自动判断能力」的方向，
但它**现在不能被实现**，原因有三：

1. **它需要观察信号，而很多信号现在不存在** ——
   `LLM activity` / `tool activity` / `recent resource operations`
   目前都没有可以观察的实现。
2. **它的权限边界需要先确立**（本 ADR 已写：判断 ≠ 事实、需经授权）
3. **很容易变成滑坡** —— 「为了让 Guardian 能工作，先加个心跳吧」
   → 那正是本 ADR 明确禁止的（§16.5.8）

> **先定边界，再实现能力** —— 这与项目一贯做法一致。

## Consequences

**正面**：

- 不会因为「Agent 在等 LLM」而误判崩溃 → **不会静默数据损坏**
- 锁的数据结构简单（`owner` + `state`，**不需要 `expires_at`**）
- 恢复路径明确且**可审计**
- 不需要心跳 / 续租 / crash detector —— 与当前禁止项一致
- **Workspace Readiness Gate 的硬阻塞项解除**：v1 不需要等自动恢复机制

**负面 / 代价**：

- **崩溃的 Agent 会一直占着锁**，直到有人 Force Unlock
  → 这是**有意的**：可见的阻塞 > 不可见的损坏
- 需要「授权的人类操作员」这个角色（v1 还没有实现）
- 在高频长任务场景下，人工介入可能成为瓶颈
  → 那是 Guardian 要解决的问题（`FUTURE`）

**因此不能做的事**：

- ❌ 不许给 Write Lock 加 `expires_at` / 时间租约
- ❌ 不许实现 Heartbeat / Lease Renewal / 自动 Expiry
- ❌ 不许把 `Message Claim Lease` 复制成 `Workspace Lock Lease`
- ❌ 不许把 inactivity 当作崩溃证据
- ❌ 不许让非锁主 `UNLOCK`（必须 DENIED）
- ❌ 不许让有 `WRITE` 的 Agent 自动拥有 `FORCE_UNLOCK`
- ❌ 不许有不留审计记录的 Force Unlock
- ❌ 不许加 `EXPIRED` 状态
- ❌ 不许让 Guardian 的判断变成 Domain Fact
- ❌ 不许让 Guardian 自动改写 Task Core（如 `DEAD` → Task `FAILED`）
- ❌ 不许「为了让 Guardian 将来能工作」而提前实现 Heartbeat

## Future evolution

```
v1（本 ADR，`SETTLED`）：
    Explicit Lock
        ├── Agent self-unlock
        └── Authorized Human Force Unlock
                └── Audit Event

Future（`FUTURE`）：
    Guardian / Observer Agent
        └── Local Small Model + Runtime Facts + Rules
                ↓
            Observation
                ↓
            Assessment
                ↓
            Recovery Request
                ↓
            Policy / Authority
                ↓
            Actual Recovery
```

**Guardian 的两条硬边界（现在就写死）**：

1. Guardian 的判断**不能自动等于**系统事实
2. Guardian **不能仅因为自己判断异常就拥有任意系统权限**

**Guardian 的判断属于 `Observation / Assessment`，
不是新的 Source of Truth。**

```
✅ 允许并存：
    Task Core:  IN_PROGRESS
    Guardian:   SUSPECTED_FAILURE

❌ 禁止自动发生：
    Guardian:   DEAD
    Task Core:  → 自动变成 FAILED
```

## 仍然 OPEN 的问题

本次**只**解决了「v1 用什么治理方式」。仍然 OPEN：

- 多资源锁（一个操作改多个文件）
- 目录锁语义
- 锁的数据结构细节
- Workspace creation timing / full lifecycle
- Task completion freeze / Archive policy
- Agent join / leave
- Artifact design
- Guardian 的实现细节（信号、模型、策略）

> **Lock Recovery 解决了不代表所有 Lock 数据结构已实现** ——
> 生产代码里一行 Lock 都没有。

## Guard

新增/扩展文档一致性守卫，必须能抓：

| 违规 | |
|---|---|
| 把 Explicit Lock 改成 Automatic Lease | ✅ |
| 删除 Human Force Unlock | ✅ |
| 把 Guardian 写成 CURRENT | ✅ |
| 把 Heartbeat 写成当前实现 | ✅ |
| 把 Guardian 判断写成 Domain Fact | ✅ |
| 把 Task Workspace 写成 CURRENT | ✅ |

并遵守本项目已积累的 Guard 工程经验（ADR-0018）：
避免否定句误报、多处 FUTURE 导致目标状态被改后仍绿、
空集合 continue 后通过、substring false positive。
