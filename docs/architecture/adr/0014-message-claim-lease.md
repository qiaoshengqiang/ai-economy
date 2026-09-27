# ADR-0014：消息认领是短期租约

- **状态**：Accepted
- **时间**：Workspace Phase 0
- **相关**：ADR-0009（持久化幂等）

## Context

Step 4D.1 把消息去重从内存变成了持久化事实：

```sql
UPDATE communication_messages
   SET claim_id = ?, claimed_at = ?
 WHERE message_id = ?
   AND processed_once = 0
   AND claim_id IS NULL
```

认领条件是 `claim_id IS NULL` —— 也就是**永久认领**。

问题：如果 worker 认领之后被 `kill -9`，这个 claim **永远卡在那里**：

```
worker 认领 → 崩溃 → claim_id 留着 → 消息永远 BUSY
                                   → 再也没有人能处理它
```

Workspace 阶段会引入**更长的执行任务**，崩溃概率随之上升。

## Decision

把认领从「永久占用」改成「**短期租约**」：

```sql
WHERE message_id = ?
  AND processed_once = 0
  AND (claim_id IS NULL
       OR claimed_at IS NULL
       OR claimed_at < ?)        -- ← 租约过期线
```

租约时长可配置：`config.yaml` 的
`system.reliability.message_claim_lease_seconds`（默认 300 秒），
由组合根注入 `CommunicationRecorder`。

**回收仍然是一条原子条件 UPDATE** —— 不存在
「SELECT 判断过期 → 再 UPDATE」的 TOCTOU 窗口。

状态模型：

```
UNCLAIMED ──claim──→ CLAIMED ──success──→ PROCESSED
                        │
                        ├── failure ──→ UNCLAIMED
                        └── lease 过期 → UNCLAIMED
```

## Consequences

**正面**：
- 崩溃的 worker **不依赖任何清理代码**就能被恢复
- 恢复只靠租约到期 —— 崩溃进程不需要做任何配合
- 支持多进程（原子条件 UPDATE 让数据库裁决）

**负面 / 代价**：
- **租约时长必须大于最慢的 handler。**
  处理时间超过租约 → 别的 worker 会接手 → **重复处理**
- 这是本阶段**最重要的遗留风险**，因为 Workspace 阶段的
  重复处理意味着**重复的文件副作用**

**因此不能做的事**：
- ❌ 不许实现 heartbeat / lease renewal / worker liveness（超出本阶段）
- ❌ 不许把租约超时当成「处理失败」（`failure_count` 不加）
- ❌ 不许把租约超时当成「处理完成」（`processed_once` 不变）
- ❌ 不许用「删除消息」或「重置 `processed_once`」代替租约
- ❌ 不许把租约判定写进 Handler / 服务层（它属于持久化层）

## 语义分离（四条硬规则）

| 事件 | `claim` | `processed_once` | `failure_count` | 消息 |
|---|---|---|---|---|
| 租约过期 | 可被接手 | **不变** | **不变** | **不删** |
| 业务失败 | 立即释放 | 不变 | **+1** | 不删 |
| 处理成功 | 释放 | **→ 1（永久）** | 不变 | 不删 |

> **worker 崩溃 ≠ 业务失败。** 随意给 `failure_count` +1
> 会让这个计数失去意义。

## 备选方案

| 方案 | 为什么不选 |
|---|---|
| 心跳 / 续租 | 明确超出本阶段（会引入 worker liveness、分布式调度） |
| 认领超时后自动重试 | Retry 是独立阶段，本阶段禁止 |
| 不设租约，靠人工干预 | 无法支撑 Workspace 的长任务 |

## 证据

- `tests/test_message_claim_lease.py` —— 33 项：正常认领 / 边界 /
  原子回收 / **真进程崩溃恢复** / 配置
- `tests/test_multiprocess_guards.py::TestGuard3LeaseInPersistenceLayer`
  —— 反向测试：去掉租约条件 → 守卫变红
- 崩溃恢复用**真子进程直接退出**（不释放认领、不关连接、不跑任何清理）
