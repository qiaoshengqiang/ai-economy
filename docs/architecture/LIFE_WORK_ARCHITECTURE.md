# Life / Work 分层架构

> **状态：目标架构 `DESIGNED`；Solo / Team / Reopen / Work / Front 契约 `CURRENT`（2026-11-09）**
>
> 本文描述**目标架构**，不是全部已实现。
> 当前已实现：Task Core · Workspace Core v1 · Memory · Draft Buffer Phase 1 ·
> Tool Runtime · Demand Entry V1 · **Solo 自 SUBMIT/VERIFY/COMPLETE** ·
> **COMPLETED Reopen / Iteration V1** ·
> **BuiltinWorkRuntime + TeamWorkRuntime + Work 精简 prompt** ·
> **Front 意图 / Brief / Result 契约** · **AgentAvailability / TeamRegistry**。
> Front 的真实邀请协议 / 调度排队仍为 `DESIGNED`。
>
> 相关 ADR：
> [ADR-0055](adr/0055-life-work-layering-and-front-agent.md) ·
> [ADR-0056](adr/0056-solo-completion-and-iteration-reopen.md)
>
> **术语：** `Solo` 只表示**任务拓扑**（一个 Agent 独立完成）；
> 执行引擎是 `Builtin` / `Pi`，不是 `Solo` / `Pi`。

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
- Work 层不依赖 Agent 间消息同步任务状态；
- Task 生命周期消息只作为 Task Core 的协议触发入口（Phase 3）；
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
  ├─ Shared Context（精简：目标 / 验收 / 分工引用）
  ├─ Public Working Memory（抽象架构 / 接口 / 依赖 / 进度）
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
| Shared Context | 精简的 Task 级共同认知（目标 / 验收 / 分工引用） |
| Public Working Memory | **抽象架构**：目录 / 文件归属 / 类/方法接口 / 依赖 / 进度；**每个 worker prompt 直接注入**。不存代码、长 acceptance、完整任务描述 |
| Private Working Memory | 当前 Task 的工作结构 |
| Short / Long-Term Memory | 跨 Task 经验（Agent 全局） |
| Draft Buffer | 私有草稿 + 批量 promotion |
| Tool Runtime | 受控工具执行 |

Work 层**不包含**：

- 身份经济；
- 钱包 / 价格 / 议价 / 支付；
- 用 Agent 间消息同步任务状态；
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
- Shared Context（精简使用）；
- Public Working Memory（抽象架构 / 接口 / 依赖 / 进度，进入每个 worker prompt）；
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
Shared Context（精简）
Public Working Memory（抽象架构 / 接口 / 依赖 / 进度）
Private Working Memory
Draft
Result
```

> **为什么 Shared Context 可以精简**：团队真正需要共享的
> 抽象架构、接口、依赖与进度放在 Public Working Memory，并由
> runtime 直接注入每个 worker prompt；Shared Context 只保留目标、
> 验收和分工引用，不再作为每轮反复读取的全量上下文。
>
> Public Working Memory **不是 plan 副本**：具体 goal / acceptance /
> 实现说明留在 WorkPlan 与对应 worker prompt 里，公共记忆只保留
> 目录 / 文件归属 / 类方法签名 / 依赖这类小体积抽象信息。

Work 层用**结构化动作**代替消息：

```text
work_item_done      成员声明某个 item 完成
work_item_blocked   成员声明某个 item 被阻塞（带 reason）
work_qa_verdict     QA 给出整体结论
work_complete       Leader 汇总后完成 Task
```

阻塞不会变成一个“消息”，而是留在 runtime 的 item 状态与事件里；
Leader 汇总时能看到 blocked item 并决定修复 / 调整 / 收尾。

### 5.4 Team 工作方式（`CURRENT`）

```text
Leader（Task Creator）
  ├─ 计划 / 架构
  ├─ 把工作拆成 items，明确 assigned_to
  ├─ 可选：指定 QA（也可以不指定）
  └─ 汇总 / 接线 / 收尾
        ↓
多个成员并行执行自己的 items
        ↓
work_item_done / work_item_blocked
        ↓
（可选）QA：work_qa_verdict
        ↓
Leader：work_complete
```

- Task 就是团队容器，不新增 Team 实体；
- Leader 是 `creator`，并在 V1 中同时是 `contractor`（最终收尾人）；
- 成员只看到自己的 item，不看到其他人的完整上下文；
- Work 层没有消息机制，成员用结构化动作回报；
- **QA 是否设置由 Leader 决定**：
  - 设置 QA：QA 必须通过，Leader 才能完成；
  - 不设置 QA：Leader 汇总后自己完成（显式 Team 例外）；
- 旧消息路径 `TaskExecutionService` 的 Team `producer ≠ acceptor` 规则不变。

#### 运行中重新计划（`CURRENT`）

Leader 可以在两轮之间用 `work_plan_update` 调整计划：

```text
reassign  改派 blocked / pending item
add       增加补丁 / 集成 item
cancel    取消不再需要的 item
update    修改目标 / 验收 / 路径
```

- 必须带 `expected_revision`；
- `DONE` 的 item 不能改派 / 取消；
- 旧 plan revision 保留；
- 当前轮不强行中断，改计划只对下一轮生效；
- blocked / pending 未处理完时，Leader 不能 `work_complete`。

#### Team 调度 / 排队（`CURRENT`，内存版）

```text
Front → Brief → TeamScheduler.queue
                    ├─ QUEUED
                    ├─ WAITING_FOR_AGENTS
                    ├─ RUNNING（最多 3）
                    └─ COMPLETED / FAILED / CANCELLED
```

- 队列里放 Brief，不提前创建 Task；
- 启动前原子预占 Lead + members 的 `IDLE`；
- 最多 `max_parallel_teams = 3`；
- 全部参与者可用才启动，不做“半启动”；
- 完成后释放 Agent，再推进队列；
- V1 队列在进程内；持久化队列为 `FUTURE`。

---

## 6. Solo Task 完成语义

### 6.1 结论

Solo 不引入 QA，也不要求 Human 执行“接受”动作。

采用：

```text
Solo Agent 可以自己 SUBMIT / VERIFY / COMPLETE
Human 没有“接受”动作
Human 后续输入由 Front 识别为返工或其他意图
```

这是 `producer ≠ acceptor` 的**显式 Solo 例外**。

Team Task 的 Work 路径见 §5.4：

- Leader 决定是否设置 QA；
- 设置 QA 时必须通过；
- 不设置 QA 时 Leader 负责汇总收尾；
- 旧消息路径 `TaskExecutionService` 的 `producer ≠ acceptor` 保持不变。

### 6.2 流程

```text
Team Lead 创建 Solo Task（自己为 contractor）
        ↓
IN_PROGRESS
        ↓
Agent 自己 SUBMIT
        ↓
SUBMITTED
        ↓
Agent 自己 VERIFY + COMPLETE
        ↓
COMPLETED（当前 Iteration 关闭）
        ↓
Result 交给 Front → Human
        ↓
Human 再次输入
        ↓
Front 判断意图
  ├─ 返工 → 路由给 Task Creator → Reopen → Iteration +1
  ├─ 新目标 → 新 Brief / 新 Task
  ├─ 询问 → direct reply
  └─ 不明确 → 请 Human 澄清
```

### 6.3 边界

- `SUBMITTED` 是 Agent 自述“这一轮交付”；
- `VERIFIED` / `COMPLETED` 在 Solo 中允许承接方自己发送；
- `COMPLETED` 是当前 Iteration 关闭，不是永久状态；
- 没有团队内部 QA；
- Human 没有“接受”动作；
- Front 不创建 Task、不改 Task 状态、不替 Human 验收；
- Front 只识别意图并把反馈路由给 Task Creator；
- Team Task 中承接方仍不能自验。

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

### 7.2 Iteration V1（已实现）

| 项 | 状态 |
|---|---|
| `TaskStatus.COMPLETED` | **不再是终态**；`TRANSITIONS[COMPLETED] = {IN_PROGRESS}` |
| `Task.iteration` | `INTEGER`，从 1 开始；Reopen 时 +1 |
| Reopen | **已实现** `Task.reopen()` |
| `COMPLETED → Workspace FROZEN` | 设计方向保留；Workspace FROZEN 仍为 `FUTURE` |
| Reopen 的锁处理 | **旧锁不自动失效**（Lock ≠ Task 状态） |

### 7.3 Iteration 模型

```text
Iteration 1 → COMPLETED
        ↓ Human 通过 Front 要求返工
Reopen（Task Creator 执行）
        ↓ iteration += 1
Iteration 2（同一 Task / 同一 Workspace）
        ↓
IN_PROGRESS → SUBMITTED → VERIFIED → COMPLETED
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

### 7.4 Reopen 的 V1 约定

1. **谁可以 Reopen？** Task Creator（Team Lead；Solo 时 creator = contractor）。
2. **何时新建 Task？** 目标/交付物发生根本变化；普通修改用 Reopen。
3. **是否新 Iteration ID？** 不新增实体；`Task.iteration` +1。
4. **是否恢复原 Workspace？** 是，继续使用同一个 `workspace_id`。
5. **旧 Lock 是否失效？** 否；Lock 独立，不自动释放/过期。
6. **Permission 是否重新评估？** V1 participants 不变；变更按正常参与方规则处理。

> 未决：`COMPLETED → Workspace FROZEN` 的自动联动、FROZEN 后的
> Reopen 流程、陈旧锁的治理——都留到 Workspace lifecycle 实现时处理。

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
- **Work 层已实现** `TaskDraftStore`（`.drafts/<task_id>.json`）；
- 旧 `DemandRunner` 的 Draft Buffer Phase 1 仍是 run 内存，迁移时替换。

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

已实现的代码入口：

```text
front/contracts.py   FrontBrief / FrontDecision / FrontResult / FrontIntent
front/runtime.py     FrontAgentRuntime：direct_reply / handoff / rework / follow_up / clarify
front/parser.py      解析模型输出；rework 只接受 COMPLETED Task

work/runtime.py      BuiltinWorkRuntime：精简 prompt + Work 动作 + work_complete
work/team_runtime.py TeamWorkRuntime：Leader 计划 / 并行 / 可选 QA / 汇总收尾
work/plan.py         WorkPlan / WorkItem / Leader 计划解析
work/replan.py       运行中计划更新（reassign / add / cancel / update）
work/scheduler.py    TeamScheduler：队列 / 并行上限 / 原子预占
work/lifecycle.py    无消息的 START/SUBMIT/VERIFY/COMPLETE/Reopen（写 task_executions）
work/actions.py      只注册 Workspace/Draft/Tool/Memory；无消息/经济/验收指派
work/prompt.py       最小 Work prompt
work/availability.py IDLE/WORKING 原子切换
work/team_registry.py 并行上限 3、WORKING 不可加入
work/drafts.py       (agent_id, task_id) 私有草稿持久化
work/cli.py          ./dev.sh work：Human → Front → Solo Task → Work
```

---

## 10. 实施阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| 0 | 本文档 + ADR-0055/0056 | **已完成** |
| 1 | Front 意图 / Brief / Result 契约 | **已实现**（`front/`）+ `./dev.sh work` |
| 2 | Solo / Team Work runtime + 精简 prompt / 动作集 | **已实现**（`work/runtime.py` / `work/team_runtime.py`） |
| 3 | AgentAvailability + 并行 3 + Team 队列 | **已实现**（`work/availability.py` / `team_registry.py` / `scheduler.py`）；持久化队列为 `FUTURE` |
| 4 | Solo 自 SUBMIT / VERIFY / COMPLETE | **已实现** |
| 5 | Reopen / Iteration V1 | **已实现**（FROZEN/陈旧锁为 `FUTURE`） |
| 6 | 生活层邀请协议 | `FUTURE` |

---

## 11. 未决问题

1. Front 意图识别失败/低置信度时，如何向 Human 澄清？
2. `COMPLETED → Workspace FROZEN` 的自动联动。
3. Front 如何选择 Team Lead：直接指定 / 按能力 / 按负载？
4. 多 Team 共用同一个 Agent 的短/长期记忆时，是否需要并发锁？
5. QA 多次不通过时，是继续修复 / 重新计划 / 交给 Human？当前 V1 是修复一轮后再 QA，仍失败则不完成。
6. Team 队列持久化与进程重启恢复（当前 V1 是内存队列）。
7. Reopen 后陈旧 Workspace 锁的治理策略。

---

## 12. 参考

- `docs/architecture/TASK_WORKSPACE.md` §15.7–15.9
- `docs/architecture/adr/0021-*.md`（Lock 无自动过期）
- `docs/architecture/adr/0022-*.md`（FROZEN / Reopen）
- `docs/architecture/adr/0055-life-work-layering-and-front-agent.md`
- `docs/architecture/adr/0056-solo-completion-and-iteration-reopen.md`
- [`AIEOS_UI.md`](AIEOS_UI.md)（Human ↔ Front ↔ Team 交互界面）
- `core/task.py`（COMPLETED 已可 Reopen）
- `services/task_execution_service.py`（producer ≠ acceptor）
