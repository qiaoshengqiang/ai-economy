# Life / Work 分层架构

> **状态：`DESIGNED`（2026-11-09）**
>
> 本文描述**目标架构**，不是当前已实现状态。
> 当前实现：Task Core · Workspace Core v1 · Memory · Draft Buffer Phase 1 ·
> Tool Runtime · Demand Entry V1。Front Agent / Team Runtime / 可用状态 /
> Reopen 尚未实现。
>
> 相关 ADR：
> [ADR-0055](adr/0055-life-work-layering-and-front-agent.md) ·
> [ADR-0056](adr/0056-solo-completion-and-iteration-reopen.md)

---

## 1. 目标

把「Agent 社会」与「具体工作执行」分开：

```text
Life 层：身份 · 经济 · 通信 · 可用状态 · 组队
Work 层：Task · Workspace · 角色 · 工具 · 记忆 · 草稿
Front：人类入口 + 结果回传
```

目标不是增加更多协议，而是：

- 具体工作时不再注入生活层的身份/经济/议价；
- Work 层不再使用 Agent 间消息机制；
- 多个 Team 可以并行执行不同 Task；
- 保留 Task Core / Workspace / Memory / Draft / Tool Runtime 这些有效机制。

### 非目标

- 本阶段不实现新的生活层邀请协议；
- 本阶段不实现 Reopen/Iteration 的具体代码；
- 本阶段不引入 `Team` 实体；
- 本阶段不让 Front Agent 创建 Task；
- 本阶段不改变 Task Core 的状态机。

---

## 2. 三层结构

```text
Human
  ↕
Front Agent（人类入口）
  ↕
Life 层（Agent 社会）
  ├─ Identity / Wallet / Ledger
  ├─ Communication
  ├─ Agent Directory
  ├─ Availability
  └─ 组队（未来）
        ↓ 形成 Task
Work 层（具体执行）
  ├─ Task（= 团队容器）
  ├─ Workspace
  ├─ Shared Context
  ├─ Private Working Memory
  ├─ Short / Long-Term Memory
  ├─ Draft Buffer
  └─ Tool Runtime
```

### 2.1 Life 层

保留：

| 能力 | 说明 |
|---|---|
| 持久身份 | Agent ID、公开资料、能力 |
| 经济资产 | Wallet / Ledger / AIC |
| 通信 | 消息、投递、回执 |
| 可用状态 | `IDLE` / `WORKING` |
| 组队 | 邀请 / 接受 / 拒绝（未来） |

Life 层是 Agent **持续存在**的地方。  
Agent 完成任务后回到 Life 层，仍然保留身份、资产和历史。

### 2.2 Work 层

只保留具体工作需要的：

| 能力 | 说明 |
|---|---|
| Task | 工作单元 + 团队容器 |
| Workspace | 文件、锁、revision、audit |
| Shared Context | Team 内共同认知 |
| Private Working Memory | 当前 Task 的工作结构 |
| Short / Long-Term Memory | 跨 Task 经验（Agent 全局） |
| Draft Buffer | 私有草稿 + 批量 promotion |
| Tool Runtime | 受控工具执行 |

Work 层**不包含**：

- 身份经济；
- 钱包 / 价格 / 议价 / 支付；
- Agent 间消息机制；
- 生活层邀请协议。

> 技术 Agent ID 仍用于 Workspace 权限、锁和审计；
> 它只是技术引用，不是经济身份。

### 2.3 Front Agent

Front Agent 是 Life 层的入口，也是普通 Agent。

职责：

1. 接收 Human 输入；
2. 判断：
   - `direct_reply`：直接回复，不创建 Task；
   - `handoff`：生成结构化 Brief，交给一个 `IDLE` Team Lead；
3. 接收 Team Result，整理为 Human 可读输出；
4. 把 Human 后续反馈转回对应 Team。

**Front Agent 永远不创建 Task。**

---

## 3. 角色

### 3.1 Human

- 提供 Demand；
- 不创建 Task；
- 不直接操作 Work 层；
- 通过 Front Agent 接收结果、提出返工/修改。

### 3.2 Front Agent

- Life 层；
- 只做意图、Brief、结果回传；
- 不生产、不验收、不创建 Task。

### 3.3 Team Lead

- 普通 Agent；
- 接收 Front 的 Brief；
- 决定：
  - 自己 Solo；
  - 邀请其他 `IDLE` Agent 组队；
- 作为 **Task Creator** 创建 Task；
- 负责计划、集成、最终 Result。

### 3.4 Team Member

- 普通 Agent；
- 由 Team Lead 邀请/指定；
- 在 Work 层执行自己的部分；
- 不携带经济身份/议价上下文。

---

## 4. 可用状态与组队

### 4.1 AgentAvailability

```text
IDLE     可被邀请
WORKING  已在某个 Task 中，不可接受新邀请
```

规则：

- 只有 `IDLE` 可以被邀请；
- `WORKING` 收到邀请 → 拒绝/忽略；
- 接受邀请是原子操作：第一个接受成功，其余邀请失效；
- Task 完成 / 中止 / 退出 → 回到 `IDLE`；
- 一个 Agent 同时只能属于一个 Work Task。

> 当前 V1 的 Task participants 仍是直接指定；  
> 邀请协议作为生活层后续内容，不在本阶段修改协议。

### 4.2 Task 就是团队容器

不新增 `Team` 实体。

```text
Task
  ├─ creator = Team Lead
  ├─ contractor = producer（可以是 Team Lead 或成员）
  ├─ participants = 团队成员
  └─ acceptance / roles
```

团队信息通过 Task 的参与者、角色和 Shared Context 表达。

### 4.3 并行

```yaml
max_parallel_teams: 3
```

- Front 最多同时运行 3 个 Team；
- 每个 Team 有独立 Task / Workspace / runtime / budget；
- 共享 Identity / Wallet / Directory / LLM / Tool Runtime / Database；
- 没有足够 `IDLE` Agent 时，Front 排队或告知 Human。

---

## 5. Work 层精简

### 5.1 保留

- Task Core（生命周期权威）；
- Workspace Core v1（资源树 / 锁 / revision / audit）；
- Shared Context；
- Private Working Memory；
- Short / Long-Term Memory；
- Draft Buffer + batch promotion；
- Tool Runtime。

### 5.2 不进入 Work 层 prompt

- Wallet / Ledger / AIC 余额；
- 价格 / 议价 / 支付；
- 身份经济；
- 生活层邀请协议；
- Front / Human 交互规则。

### 5.3 不保留消息机制

Work 层没有 Agent 间 `message_send`。

Work 层的协作面：

```text
Task
Workspace
Shared Context
Private Working Memory
Draft
Result
```

> ⚠️ `OPEN`：Work 层无消息后，Worker 遇到阻塞如何表达？
> 候选：
> 1. 写入 Task / Shared Context 的结构化阻塞项；
> 2. Team Lead 将阻塞作为 Result 返回 Front；
> 3. 允许退出 Work 层，在 Life 层发消息（但这会重新引入协调成本）。
>
> 本阶段先不定；实现前需要一个明确选择。

---

## 6. Solo Task 完成语义

### 6.1 结论

Solo 不引入 QA，也不让生产者自验。

采用：

```text
Solo Agent 可以自己声明「工作完成」= SUBMITTED
COMPLETED 仍然是 Human/非生产者接受后的关闭
```

### 6.2 流程

```text
Team Lead 创建 Solo Task（自己为 contractor）
        ↓
IN_PROGRESS
        ↓
 Agent 自己发送 SUBMIT
        ↓
SUBMITTED
        ↓
Result 交给 Front
        ↓
Front 回复 Human
        ↓
Human 接受 → COMPLETED
Human 要求返工 → IN_PROGRESS（下一 Iteration）
```

### 6.3 边界

- `SUBMITTED` 是 Agent 自述“我完成了这一轮工作”；
- `COMPLETED` 是生命周期关闭，不是永久状态；
- 没有团队内部 QA；
- Human 通过 Front 做接受/返工决定；
- Front 只转达 Human 决定，不替 Human 判断。

> 当前代码：`SUBMIT` 允许 contractor；`VERIFY/COMPLETE` 不允许 contractor。
> 因此“Solo 自给 SUBMITTED、Human 接受后 COMPLETED”与现有
> `producer ≠ acceptor` 边界兼容。
>
> 不采用“Solo Agent 自己发 COMPLETED”，除非显式修改该边界。

---

## 7. 完成、Reopen 与 Iteration

### 7.1 原则

文档 `TASK_WORKSPACE.md` §15.7 / ADR-0022 已经确定：

```text
Task 完成不是永久死亡
COMPLETED 可以 Reopen
Iteration 2 继续执行
```

`COMPLETED` 的语义是：

```text
当前 Iteration 关闭
≠
以后不能再改
```

### 7.2 当前实现差距

| 项 | 当前状态 |
|---|---|
| `TaskStatus.COMPLETED` | 终态，`TRANSITIONS[COMPLETED] = ∅` |
| Reopen | **未实现** |
| `COMPLETED → Workspace FROZEN` | 设计已定，代码未实现 |
| Reopen 6 个细节 | `OPEN`（ADR-0022 / TASK_WORKSPACE §15.9） |

### 7.3 Reopen 的目标模型（`PROPOSED`，未实现）

```text
Iteration 1 → COMPLETED
        ↓ Human 通过 Front 要求返工
Reopen
        ↓
Iteration 2（同一 Task）
        ↓
IN_PROGRESS → SUBMITTED → ...
```

必须保留：

- 旧执行历史；
- 旧 SUBMIT；
- 旧 VERIFY / 完成记录；
- 人类修改后的要求。

**不能**：

- 清空旧历史；
- 覆盖旧 Verification；
- 把 Reopen 当成新 Task（除非目标发生根本变化）。

### 7.4 仍需确认的 6 个细节

1. 谁可以 Reopen？  
2. 何时必须新建 Task，而不是 Reopen？  
3. Reopen 是否产生新的 Iteration ID？  
4. 是否恢复原 Workspace？  
5. 旧 Lock 是否全部无效？  
6. Permission 是否重新评估？

> 本阶段只固定“允许 Reopen / COMPLETED 非永久”的原则；
> 上述 6 项保持 `OPEN`，实现前需专门 ADR。

---

## 8. 记忆编排

### 8.1 分层

| 记忆 | 作用域 | 生命周期 |
|---|---|---|
| Private Working Memory | `(agent_id, task_id)` | Task 活跃/暂停期间保留 |
| Team Shared Context | `task_id` | Task 生命周期 |
| Short-Term Memory | `agent_id` | 跨 Task |
| Long-Term Memory | `agent_id` | 跨 Task |
| Draft Buffer | `(agent_id, task_id)` | 私有空间，Task 暂停保留 |
| Task Workspace | `task_id` | Task 生命周期 + FROZEN 后保留 |

### 8.2 Task甲 → Task乙 → 回到 Task甲

```text
Task甲 暂停：
  - Private WM(task甲) 保留，不 finalize；
  - Task 状态保留；
  - 不把活动任务状态写进 Short-Term；
  - Agent 回到 IDLE，去做 Task乙。

Task乙：
  - 使用自己的 Private WM / Shared Context / Workspace；
  - 完成后可写 Short-Term 摘要（task_id=乙）。

回到 Task甲：
  - 从 Private WM(task甲) + Shared Context(task甲) + Workspace(task甲) 恢复；
  - Short/Long-Term 只提供跨任务经验。
```

### 8.3 关键原则

> 活动任务的“我在哪、下一步是什么”必须留在 Task-scoped memory，
> 不能靠 Short-Term Memory 保存。

Short-Term 容量有限；如果用它存活动任务状态，切换几次任务就会丢失。

### 8.4 Task 完成后返工

- 保留 final snapshot + revisions；
- Short-Term 可以有一条 Task 摘要；
- Reopen 时从 final snapshot + 历史恢复，不从摘要猜。

### 8.5 持久化

- Private WM：按 Task 持久化；
- Short/Long-Term：按 Agent 全局持久化；
- Draft：按 `(agent_id, task_id)` 持久化到私有空间；
- 当前 Draft Buffer Phase 1 是 run 内存；目标实现改为私有空间持久化。

---

## 9. Runtime 目标结构

```text
FrontAgentRuntime
  ├─ intent → direct_reply / brief
  ├─ team_registry: task_id → TeamRuntime
  └─ result_inbox → Human

TeamRuntime（每个 Task 一个）
  ├─ Task
  ├─ Workspace
  ├─ participants / roles
  ├─ team-local scheduler
  ├─ memory scope
  ├─ draft scope
  ├─ budget
  └─ result → Front
```

共享：

```text
Identity / Wallet / Ledger
Communication（仅 Life 层）
Agent Directory
WorkspaceGateway
Tool Runtime
LLM Providers
Database
```

> 当前 `DemandRunner` 是 Front/Creator/Producer/Reviewer 混合的单体，
> 不作为目标架构。它保留为旧实验路径，逐步迁移。

---

## 10. 实施阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 本文档 + ADR-0055/0056 | **本次完成** |
| 1 | FrontAgentRuntime + Brief/Result 契约 | `DESIGNED` |
| 2 | TeamRuntime + Work 层精简 prompt | `DESIGNED` |
| 3 | AgentAvailability + 并行 3 Team | `DESIGNED` |
| 4 | Solo SUBMITTED + Human 接受 | `DESIGNED` |
| 5 | Reopen / Iteration | `FUTURE`（6 细节 OPEN） |
| 6 | 生活层邀请协议 | `FUTURE` |

---

## 11. 未决问题

1. Work 层无消息时，阻塞如何表达？
2. Solo Task 的 Human 接受如何记录：Front 代理事件 / 新 HumanDecision 记录？
3. Reopen 的 6 个细节。
4. Draft 持久化位置：私有 Workspace 文件 vs 私有 DB 表。
5. Front 如何选择 Team Lead：直接指定 / 按能力 / 按负载？
6. Team Lead 自己 Solo 时，是否需要 `SUBMITTED` 之外的 `DELIVERED` 状态？
7. 多 Team 共用同一个 Agent 的短/长期记忆时，是否需要并发锁？

---

## 12. 参考

- `docs/architecture/TASK_WORKSPACE.md` §15.7–15.9
- `docs/architecture/adr/0021-*.md`（Lock 无自动过期）
- `docs/architecture/adr/0022-*.md`（FROZEN / Reopen）
- `docs/architecture/adr/0055-life-work-layering-and-front-agent.md`
- `docs/architecture/adr/0056-solo-completion-and-iteration-reopen.md`
- `core/task.py`（当前 COMPLETED 终态）
- `services/task_execution_service.py`（producer ≠ acceptor）
