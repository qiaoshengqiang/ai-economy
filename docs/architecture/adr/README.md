# Architecture Decision Records

> **每个重要架构决策一条 ADR。**
>
> ADR 记录的是**为什么**这样决定，以及**代价是什么**。
> 代码能告诉你「是什么」，git log 能告诉你「什么时候」，
> 但只有 ADR 能告诉你「当时为什么不选另一条路」。

---

## 规则

1. **一个决策一条 ADR。** 不要把三件事塞进一篇。
2. **ADR 一旦写下不修改。** 被取代时写新 ADR，并在两边互相标注
   `Superseded by ADR-XXXX` / `Supersedes ADR-YYYY`。
3. **必须写 `Consequences`。** 包括**负面**后果 ——
   「这个决定带来什么代价」比「它有多好」更重要。
4. **`Status` 只有四种**：`Accepted` · `Superseded` · `Deprecated` · `Proposed`
5. **不大规模重写历史。** 这份索引是**回溯**建立的 ——
   它记录的是**已经发生过的**决定，不是现在才发明出来的。

---

## 索引

| ADR | 标题 | 状态 | 时间 | 由哪个阶段产生 |
|---|---|---|---|---|
| [0001](0001-identity-address-separation.md) | Identity 与 Address 分离 | Accepted | Step 3.6 | Communication |
| [0002](0002-communication-transport-separation.md) | Communication 与 Transport 分离 | Accepted | Step 3.6 | Communication |
| [0003](0003-communication-business-free.md) | Communication 层保持业务无关 | Accepted | Step 3.6 | Communication |
| [0004](0004-ledger-as-source-of-truth.md) | 账本是唯一事实来源，余额算出来 | Accepted | Step 1 | Economy |
| [0005](0005-capability-permission-separation.md) | Capability 与 Permission 分离 | Accepted | Step 4B | Task / Directory |
| [0006](0006-task-application-layer-refactor.md) | Task 应用层拆分（门面 / Handler / Mapper） | Accepted | Refactor | Refactor |
| [0007](0007-negotiation-execution-separation.md) | 议价与执行分离 | Accepted | Step 4D | Task |
| [0008](0008-negotiation-without-llm.md) | 议价不调用 LLM | Accepted | Step 4C | Task |
| [0009](0009-persistent-message-idempotency.md) | 消息幂等是持久化事实 | Accepted | Step 4D.1 | Reliability |
| [0010](0010-task-optimistic-concurrency.md) | Task 乐观锁（compare-and-set） | Accepted | Step 4D.1 | Reliability |
| [0011](0011-reentrant-transactions.md) | 事务可重入 | Accepted | Step 4D.1 | Reliability |
| [0012](0012-begin-immediate.md) | 写事务用 BEGIN IMMEDIATE | Accepted | Workspace Phase 0 | Reliability |
| [0013](0013-pid-aware-connection-cache.md) | 连接缓存 PID 感知 | Accepted | Workspace Phase 0 | Reliability |
| [0014](0014-message-claim-lease.md) | 消息认领是短期租约 | Accepted | Workspace Phase 0 | Reliability |
| [0015](0015-memory-layering.md) | Memory 四层架构 | Accepted | Step 4E | Architecture |
| [0016](0016-source-of-truth-priority.md) | Source of Truth 优先级 | Accepted | Step 4E | Architecture |
| [0017](0017-task-shared-context-alignment.md) | Task Shared Context 与 Alignment 分离 | Accepted | Step 4E | Architecture |
| [0018](0018-guard-verification-standards.md) | Guard 检测手段标准 | Accepted | Step 4E | Engineering |
| [0019](0019-task-shared-context-alignment.md) | Task Shared Context 与 Alignment 分离 | Accepted | Step 4F | Task / 共享认知 |
| [0020](0020-task-workspace-shared-resource-model.md) | Task Workspace 采用共享资源模型；Permission ≠ Write Lock | Accepted（实现为 FUTURE） | Workspace 架构定义 | Task Workspace |
| [0021](0021-explicit-write-lock-and-human-force-unlock.md) | Write Lock 采用显式持有模型 + Human Force Unlock | Accepted（实现为 FUTURE） | Write Lock Recovery | Task Workspace |
| [0022](0022-lazy-provisioning-and-workspace-lifecycle.md) | Lazy Provisioning + Workspace Lifecycle + Human Participation | Accepted（实现为 FUTURE） | Lifecycle / Human / Iteration | Task Workspace |
| [0023](0023-task-origin-and-lineage.md) | Task Origin / Creator / Lineage / Funding 是独立概念 | Accepted（实现为 FUTURE） | Task Origin & Lineage | Task |
| [0024](0024-human-demand-not-task-creator.md) | **Human Demand ≠ Task；Human ≠ Task Creator** | Accepted（实现为 FUTURE） | Task 起源模型修正 | Task |

---

## 模板

```markdown
# ADR-XXXX：<决策标题>

- **状态**：Accepted | Superseded | Deprecated | Proposed
- **时间**：Step X.Y
- **Supersedes**：ADR-ZZZZ（可选）
- **Superseded by**：ADR-WWWW（被取代时补）

## Context

当时面对什么问题？有哪些约束？为什么必须做决定？

## Decision

决定了什么。（一句话能说清最好）

## Consequences

**正面**：
- ...

**负面 / 代价**：
- ...

**因此不能做的事**：
- ...

## 备选方案

考虑过但没选的方案，以及为什么不选。

## 证据

哪个测试 / 哪次真实失败支撑了这个决定。
