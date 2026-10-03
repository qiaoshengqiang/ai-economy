# ADR-0035：Execution Lease 与 Workspace Responsibility 的分离

- **状态**：Accepted（**设计已定，尚未实现**）
- **相关**：ADR-0021（Lock 不用自动过期）· ADR-0027（设计简化）· [`RECOVERY_MODEL.md`](../RECOVERY_MODEL.md)
- **设计文档**：[`CHANGE_PIPELINE_V1_DESIGN.md`](../implementation/CHANGE_PIPELINE_V1_DESIGN.md) §8 / §9

## Context

技术债 #1（🔴）从 Phase 0 起就存在：

> **租约 vs 长任务**：无 lease renewal。handler 超租约 → 别的 worker 接手
> → **重复处理**（Workspace 阶段 = **重复文件副作用**）。

天真的修法是：

```
❌ lease expired
       ↓
   release workspace lock
```

**这是错的**，后果是静默覆盖：

```
Agent 还在思考（只是心跳慢）
    → 锁被自动释放
    → 别人改了同一个文件
    → Agent 回来一写 → **静默覆盖**
```

已经有两条冻结原则在禁止这个方向：

- **ADR-0021**：Workspace Lock **不用时间租约**（无 `EXPIRED` 状态）
- **RECOVERY_MODEL**：`异常检测 ≠ 治理决策`

## Decision

### 一、🔒 两层概念，职责完全不同

```
┌──────────────────────────────────────────────────────┐
│  Responsibility        「谁**有权**负责这个资源？」    │
│  由 **Workspace Core** 管理（已冻结）                  │
│      Controller · Explicit Lock · Handoff ·          │
│      Release · Force Unlock                          │
│  变化**只**由责任操作触发                              │
└──────────────────────────────────────────────────────┘
              ↕  🔒 **没有任何自动通道**
┌──────────────────────────────────────────────────────┐
│  Execution Lease       「这次**执行机会**还有效吗？」  │
│  由 **Execution / Lease** 管理（本层）                 │
│      Attempt · Lease · Heartbeat · Expiry ·          │
│      Retry · Reclaim                                 │
│  变化**只**影响 attempt                               │
└──────────────────────────────────────────────────────┘
```

### 二、🔒 用 **schema** 挡住混淆，而不是靠文档纪律

```sql
leases.scope ∈ {TASK, WORKTREE, TEST}
                          ↑
                注意：**没有** RESOURCE
```

**它结构上不可能表达「我持有 resource X」。**

> 这比「文档里写一句不要混」强得多 ——
> ADR-0029 也是靠 `workspace_explicit_locks` 的主键来保证「同一 File 单 Holder」的。

### 三、Agent 长时间思考时怎么办

```
1. Lease 到期 → ExecutionAttempt = HEARTBEAT_MISSED → EXPIRED
2. Workspace Lock **完全不动**
3. 产生「attempt 失联」事件（**不是**「资源释放」事件）
4. 该 attempt 的产出（若有）标记为 in-flight，等待 reconciliation
5. 由人 / 治理决定：
       · 恢复（新 attempt，**继承同一个 operation_id**）
       · 或走正式入口 Force Unlock / Handoff
```

### 四、Heartbeat 参数与「不做自动动作」

| 项 | 取值 |
|---|---|
| `heartbeat_interval_ms` | `lease_ttl / 3` |
| `lease_ttl` | **必须 > 最慢的单次操作** |
| `HEARTBEAT_MISSED` 判定 | 超过 `2 × interval` 无心跳 |
| `EXPIRED` 判定 | 超过 `lease_ttl` |
| 续租 | 长任务**必须显式**续租；**没有**「自动无限延长」 |
| ⚠️ | **不做**超时自动重投递 —— 只**标记**，不自动动作 |

### 五、🔒 幂等：三层 ID

```
operation_id        ← Agent 提供的**逻辑操作**标识
                      「同一个意图」在 retry 前后**不变**
        ↓
idempotency_key     ← hash(operation_id + operation_type)，DB 唯一索引
        ↓
attempt_id          ← 每一次**尝试**都有新 id
```

**「先写 `IN_PROGRESS`，再执行」** —— 这是 crash consistency 的基础（ADR-0036）。

```
Attempt A456: create file X → lease expires
        ↓
Attempt B789（retry，**同一个 operation_id**）
        ↓
查 idempotency_records[key]：
   COMPLETED   → 直接返回 result_ref，**不重复执行**
   IN_PROGRESS → 进入 reconciliation（不是盲目重做）
   不存在      → 先写 IN_PROGRESS，再执行
```

### 六、🔒 总原则

> **Recovery 可以改 attempt / worktree / patch 的状态，
> 但绝不自动改 Workspace responsibility。**

## Consequences

**正面**：

- 「长任务」与「锁」解耦 —— Agent 可以安心思考
- 静默覆盖的路径被**结构性地**挡住（不是靠约定）
- retry 不再产生不可追踪的重复副作用
- 与 ADR-0021 / RECOVERY_MODEL 完全一致，没有推翻任何冻结决策

**负面 / 代价**：

- 需要维护 heartbeat（Agent 或 Runtime 要定期上报）
- 失联后需要 **reconciliation** 逻辑（比「直接重试」复杂）
- 失联时资源仍然被锁着 —— **期间别人不能改**。
  这是**有意的**（宁可阻塞也不要静默覆盖），
  但需要人工/治理介入来解阻塞 → 运维成本

**因此不能做的事**：

- ❌ `Execution Lease == Workspace Lock`
- ❌ Lease 到期自动释放 Workspace Lock
- ❌ 超时自动重投递 / 自动 task reassignment
- ❌ retry 产生不可追踪的重复副作用
- ❌ 给 `leases.scope` 加 `RESOURCE`

## Guard

- 设计文档必须有 Responsibility / Execution Lease 的双层图
- 必须显式出现「禁止 lease 到期释放 lock」
- `leases.scope` 的取值必须**不含** `RESOURCE`（结构性断言）
- 必须出现 operation_id / idempotency_key / attempt_id 三层
