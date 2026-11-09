# ADR-0056：Solo 完成语义 + Iteration / Reopen

- **状态**：Accepted（**Solo 自 COMPLETE + Iteration V1 已实现**）
- **时间**：2026-11-09
- **Supersedes**：无
- **Superseded by**：无

## Context

用户确认：

- 任务没有绝对完成；Human 随时可能要求返工；
- `COMPLETED` 不代表任务不能再动；
- Solo Work 也应由 Agent 自己给出完成状态；
- 但当前 `TaskExecutionService` 规定 `producer ≠ acceptor`，
  contractor 不能发 VERIFY / COMPLETE。

同时，文档已经确定：

- `TASK_WORKSPACE.md` §15.7 / ADR-0022：Task 完成不是永久死亡，
  允许 Reopen / Iteration 2；
- `COMPLETED → Workspace FROZEN`；
- `FROZEN` 不是永久只读；
- Reopen 的 6 个细节仍 `OPEN`。

实现前存在的差距（本 ADR 已解决前两项）：

- `TaskStatus.COMPLETED` 曾是终态，`TRANSITIONS[COMPLETED] = ∅`；**已修正**
- Reopen 未实现；**已实现 Iteration V1**
- Workspace FROZEN 未实现；仍为 `FUTURE`
- 锁与 Task 状态是分离机制；Reopen 时旧锁**不自动失效**（V1 明确）

## Decision

### 1. Solo 由 Agent 自己 COMPLETE

```text
Solo Task:
  IN_PROGRESS
    → SUBMITTED        ← Agent 自己可以发
    → VERIFIED         ← Agent 自己确认这一轮完成
    → COMPLETED        ← Agent 自己关闭这一轮
```

- `SUBMITTED` = Agent 自述“这一轮交付”；
- `VERIFIED` / `COMPLETED` 在 Solo 中允许由承接方自己发送；
- **Human 没有“接受”动作**；
- 不引入 QA Agent；
- 不要求 Front 替人类做验收；
- 这是对 `producer ≠ acceptor` 的**显式 Solo 例外**；
- Team Task 的 Work 路径中，Leader 决定是否设置 QA：
  - 设置 QA → QA 必须通过；
  - 不设置 QA → Leader 汇总后自己完成；
- 旧消息路径 `TaskExecutionService` 的 Team `producer ≠ acceptor` 不变。

> 为什么不让 Front 验收：Front 是入口和路由，不是验收责任方；
> 让它验收会把“人类入口”变成“隐藏 QA”，职责再次混在一起。

### 2. 人类反馈通过 Front 触发返工

```text
Human 后续输入
    ↓
Front 识别意图
    ├─ 新需求 / 不同目标 → 新 Brief → 新 Task
    ├─ 对已完成 Task 的修改 → Reopen 原 Task
    ├─ 询问结果 → direct reply
    └─ 其他 → 转给原 Team / 请人类澄清
```

- Human 不直接操作 Task；
- Front 不创建 Task，也不改 Task 状态；
- Front 把返工意图路由给原 Task 的 **Creator（Team Lead）**；
- 由 Team Lead 执行 Reopen，并组织下一轮工作；
- Solo 时 Creator 就是承接方，由它自己 Reopen。

### 3. COMPLETED 不是永久状态

- `COMPLETED` 表示**当前 Iteration 关闭**；
- Task 可以在 Human 要求下 Reopen；
- Reopen 必须保留旧执行 / SUBMIT / VERIFY / 完成历史；
- Reopen 不覆盖旧 Verification；
- 目标发生根本变化时新建 Task；普通返工用 Iteration。

### 4. Iteration V1（已实现）

```text
Task-001
  iteration = 1
    execution · submissions · verification · completion
  Reopen
  iteration = 2
    new execution · submissions · verification · completion
```

实现约定：

| 问题 | V1 决定 |
|---|---|
| 谁可以 Reopen？ | Task Creator（Team Lead；Solo 时 creator = contractor） |
| 何时新建 Task？ | 目标/交付物发生根本变化；普通修改用 Reopen |
| 是否产生新 Iteration ID？ | 不新增实体；`Task.iteration` 整数 +1 |
| 是否恢复原 Workspace？ | 是，继续使用同一个 `workspace_id` |
| 旧 Lock 是否全部无效？ | **否**。Lock 独立于 Task 状态，不自动释放/过期 |
| Permission 是否重新评估？ | V1 participants 不变；若变更按正常参与方规则处理 |

> `COMPLETED → Workspace FROZEN` 仍是设计方向，但 Workspace
> lifecycle FROZEN 尚未实现；本轮不把 Task 状态与 Workspace
> lifecycle 强行耦合。Reopen 时 Workspace 继续使用原空间。

### 5. Task Core 的终态重新定义

```text
terminal      = REJECTED · CANCELLED · EXPIRED · FAILED
settled       = COMPLETED（当前 Iteration 关闭，可 Reopen）
TRANSITIONS[COMPLETED] = { IN_PROGRESS }
```

- `COMPLETED` 不再属于 `terminal()`；
- `Task.is_terminal` 对 COMPLETED 返回 **false**；
- 新增 `Task.is_settled` / `Task.is_closed`；
- `is_open` 仍然表示“还有活跃 Iteration”，COMPLETED 返回 false；
- Reopen 是**显式领域动作** `Task.reopen()`，不是普通 `transition_to`。

## 明确不做

- 不让 Front 创建 Task 或直接改 Task 状态；
- 不在 Solo 中要求 Human 接受；
- 不让旧消息路径的 Team 承接方自验；Work Team 路径由 Leader 决定 QA；
- 不把 Reopen 当成新 Task；
- 不把锁自动过期/自动释放混进 Reopen；
- 不在本轮实现 Workspace FROZEN。

## Consequences

### 正面

- Solo 有明确的 Agent 自完成路径；
- Human 不再承担 Task 操作负担；
- 返工保留原 Task 与历史；
- 旧消息路径 Team 的 `producer ≠ acceptor` 仍然成立；Work Team 的 QA 由 Leader 决定；
- `COMPLETED` 的语义与 `TASK_WORKSPACE.md` / ADR-0022 对齐。

### 负面 / 代价

- `COMPLETED` 不再是终态，旧的“终态不可离开”测试需要更新；
- Reopen 后旧锁不会自动失效，长任务可能留下陈旧锁；
- Workspace FROZEN 仍未与 COMPLETED 联动；
- Front 的意图识别质量会直接影响返工是否正确路由。

## References

- `docs/architecture/LIFE_WORK_ARCHITECTURE.md`
- `docs/architecture/TASK_WORKSPACE.md` §15.7–15.9
- `docs/architecture/adr/0021-*.md`
- `docs/architecture/adr/0022-*.md`
- `core/task.py`
- `services/task_execution_service.py`
