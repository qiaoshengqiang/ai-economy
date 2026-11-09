# ADR-0050：NO_PROGRESS 截断宽限 + 最小窗口 + 读缓存

- **状态**：Accepted（**已实现**）
- **时间**：Demand Entry V1 真实运行修复
- **Supersedes**：无
- **Superseded by**：无

## Context

真实 run `demand_65c51fe6`：

- `exit_reason=NO_PROGRESS`，6m21s，14 turns，2.69 AIC；
- Task 仍 `IN_PROGRESS`；
- A 在 turn 9 写了 `index.html` R1（10,135 字符 / 221 行）；
- 之后 A 两次各连续 4 次 `workspace_read index.html`；
- turn 15 A 的调用 `finish_reason=length` / `EMPTY_CONTENT`，
  16,384 output tokens、56,263 reasoning chars、0 actions；
- A 的截断压力已经记录，准备在**下一轮**显示；
- 但 E 的 turn 16 只是私有 spec / 消息（弱进展），
  2 参与者 × 3 cycles = 6 turn 无强进展，scheduler 停止。

关键问题：

> **系统在 A 即将看到截断反馈、准备重试之前，把它停掉了。**

另外，A 和 E 都在同一 turn 内对同一个 `index.html` R1 各读 4 次；
每次 `workspace_read` 都重新从 SQLite 取整份正文，纯属重复 IO。

## Decision

### 1. `TurnOutcome.truncated`

`TurnOutcome` 新增：

```python
truncated: bool = False
```

由 `budget["truncated"]` 派生（含 `finish_reason=length` 与
`EMPTY_CONTENT`）。它只表示“这次认知不完整”，**不是**强进展，
也**不是**“Agent 选择不做”。

### 2. 每个 Agent 一次截断恢复宽限

Scheduler 只在**本来就要触发 NO_PROGRESS** 时使用宽限：

```text
would_stop = no_progress + 1 >= no_progress_cycles
             and turns_since_strong >= min_no_progress_turns
graceful = 本 cycle 截断的 Agent - 已用过宽限的 Agent
if would_stop and graceful:
    消耗这些 Agent 的宽限
    不增加 no_progress
    继续下一 cycle（让它们看到截断压力并恢复）
else:
    no_progress += 1
```

- 每个 Agent 最多享受一次宽限；
- 连续截断不会无限延长 run；
- 如果 cycle 内有强进展，宽限不消耗；
- 如果还没到最小窗口，宽限也不消耗。

### 3. 最小无强进展窗口

`RunScheduler` 新增 `min_no_progress_turns=8`：

- 即使 `no_progress_cycles` 已到，也要至少经过 8 个 turn
  无强进展才停；
- 1-2 人团队的 cycle 很短（3 cycles 可能只有 3-6 turn），
  8 turn 给 producer 至少一次恢复/重试机会；
- 3 人以上团队通常已经 ≥9 turn，不受影响。

### 4. Workspace Read 缓存

`WorkspaceReadExecutor` 新增可选 `stat_fn`：

- `services/artifacts.stat_artifact()` → `WorkspaceGateway.stat()`；
- `stat()` 只查 metadata：`resource_id / revision / chars`，
  **不返回 content**；
- 缓存 key：`(path, resource_id, revision, offset, limit)`；
- 命中时直接返回缓存的输出，不再调用 `read_fn` / `gateway.read()`；
- 每次读取仍先做 revision 检查 —— **Workspace 是唯一权威**，
  不允许无检查地返回进程内缓存；
- revision 变化 / resource_id 变化 / 范围不同 → 重新读；
- 失败不缓存；
- 缓存有上限：默认 32 条 / 256,000 字符；
- 未传 `stat_fn` 的调用方（实验 / 旧测试）行为完全不变。

Demand 组合根：

```python
WorkspaceReadExecutor(
    read_artifact, gateway=..., workspace_id=...,
    stat_fn=stat_artifact)
```

### 明确不做

- 不给 Agent 墙钟剩余时间/时间压力（正式 AIEOS 不限制任务时间）；
- 不用 `TIME_LIMIT` 修改 Task 状态；
- 不自动重试/自动改预算/自动替 Agent 写；
- 不在没有 revision 检查的情况下返回缓存；
- 不把缓存命中伪装成新的读取事实（ActionResult 的语义仍是“读到了”）。

## Consequences

### 正面

- A 在截断后至少能拿到一轮恢复机会，看到压力后可以拆小/提高预算；
- 1-2 人团队不会在 3-6 turn 就被过早停掉；
- 同一 revision 的重复读取不再重复搬运正文；
- 多个 Agent 读同一文件同一 revision 可共享缓存；
- 缓存仍以 Workspace revision 为准，不会返回过期正文。

### 负面 / 代价

- 每次读多一次 metadata 查询（比读整份 content 便宜得多）；
- 缓存是进程内、按 task dispatcher 生命周期；跨进程/重启不共享；
- 截断宽限会让真正停摆的 run 多跑最多一个 cycle；
- `min_no_progress_turns=8` 会让 1 人团队比之前晚停几轮。

## References

- `demand/models.py`
- `demand/runner.py`
- `demand/scheduler.py`
- `actions/workspace_read.py`
- `services/artifacts.py`
- `services/workspace_gateway.py`
- `core/workspace_store.py`
- `demand/actions.py`
- `tests/test_demand_entry.py`
- `tests/test_workspace_read_action.py`
- `tests/test_workspace_gateway.py`
