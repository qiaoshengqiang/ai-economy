# ADR-0056：Solo 完成语义 + Iteration / Reopen

- **状态**：Accepted（**原则已确认；Reopen 实现为 `FUTURE`**）
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

当前实现与文档存在差距：

- `TaskStatus.COMPLETED` 是终态，`TRANSITIONS[COMPLETED] = ∅`；
- Reopen 未实现；
- Workspace FROZEN 未实现；
- 锁与 Task 状态是分离机制，Reopen 时旧锁是否失效未定。

## Decision

### 1. Solo 采用“SUBMITTED + Human 接受”

```text
Solo Task:
  IN_PROGRESS
    → SUBMITTED        ← Agent 自己可以发
    → Front → Human
    → Human 接受
    → COMPLETED
    → Human 要求返工
    → IN_PROGRESS（下一 Iteration）
```

- `SUBMITTED` = Agent 自述“这一轮工作完成”；
- `COMPLETED` = Human/非生产者接受后的生命周期关闭；
- 不引入 QA Agent；
- 不让 Solo Agent 自己发 COMPLETED；
- `producer ≠ acceptor` 保持不变；
- Front 只转达 Human 的决定，不替 Human 判断。

> 本阶段不新增 `DELIVERED` 状态。  
> 如果未来需要更明确的“交付”状态，再单独设计。

### 2. COMPLETED 不是永久状态

- `COMPLETED` 表示**当前 Iteration 关闭**；
- Task 可以在 Human 要求下 Reopen；
- Reopen 必须保留旧执行 / SUBMIT / VERIFY / 完成历史；
- Reopen 不得覆盖旧 Verification；
- 目标发生变化时才新建 Task，普通返工用 Iteration。

### 3. 目标 Iteration 模型（`PROPOSED`，未实现）

```text
Task-001
  Iteration 1
    execution · submissions · verification · completion
  Iteration 2
    new execution · new submissions · new verification
```

### 4. Reopen 的 6 个细节仍 `OPEN`

1. 谁可以 Reopen？
2. 何时必须新建 Task？
3. 是否产生新的 Iteration ID？
4. 是否恢复原 Workspace？
5. 旧 Lock 是否全部无效？
6. Permission 是否重新评估？

本 ADR 只确认：

- 允许 Reopen；
- `COMPLETED` 非永久；
- 历史必须保留；
- 6 个细节在实现前继续讨论。

## 明确不做

- 不修改 Task Core 的状态机（本阶段）；
- 不实现 Reopen（本阶段）；
- 不让 Solo Agent 自己 VERIFY/COMPLETE；
- 不把 Reopen 当成新 Task；
- 不把锁自动过期/自动释放混进 Reopen；
- 不改变 `COMPLETED → FROZEN` 的设计方向。

## Consequences

### 正面

- Solo 有明确的 Agent 完成状态 `SUBMITTED`；
- 不破坏 producer ≠ acceptor；
- Human 仍然是最终接受者；
- 为 Reopen/Iteration 留下清晰的历史模型；
- 不再把 `COMPLETED` 误读为永久死亡。

### 负面 / 代价

- Human 接受如何记录到 Task Core 仍需实现设计；
- Reopen 6 个细节未定，不能提前编码；
- 当前代码仍把 COMPLETED 当终态，文档与实现存在短期差距。

## References

- `docs/architecture/LIFE_WORK_ARCHITECTURE.md`
- `docs/architecture/TASK_WORKSPACE.md` §15.7–15.9
- `docs/architecture/adr/0021-*.md`
- `docs/architecture/adr/0022-*.md`
- `core/task.py`
- `services/task_execution_service.py`
