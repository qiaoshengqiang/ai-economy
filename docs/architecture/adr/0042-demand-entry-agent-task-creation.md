# ADR-0042：Demand Entry V1 —— Agent 自主建 Task + 串行调度

- **状态**：Accepted（**已实现**）
- **时间**：Demand Entry V1
- **Supersedes**：无
- **Superseded by**：无

## Context

系统已经有：

- Task Core / 生命周期 / 验收；
- Workspace / Communication / Economy；
- Agent Core / Tool Runtime / Vision；
- 多个实验 Runner（`run_web.py` / `run_demand.py` / `run_forest_cabin.py`）。

但**没有正式的人类入口**：

- `./dev.sh run` 只打印状态；
- `hello-demo` / `snake-demo` / `three-agent-demo` 是固定任务；
- 任意提示词只能走实验脚本；
- 实验脚本预先写死 Agent 名单、命名空间、DoD；
- Agent **没有** `task_create` 动作，Task 由 runner 创建；
- 没有一个通用 Scheduler 按动态参与者驱动 turn。

用户要求：

> 人类给 Agent A 一句话，A 自己建 Task、选参与者、推进到完成；
> 先做 CLI，不做复杂前端；子任务留到以后。

## Decision

新增 **Demand Entry V1**，作为独立 `demand/` 包 + `./dev.sh demand`：

1. **DemandRun ≠ Task**
   - CLI 只创建 `DemandRun`（JSON 运行记录）；
   - Task 由 Agent A 通过 `task_create` 创建；
   - `DemandRun` 不复制 Task.status。

2. **`task_create` 动作**
   - `creator` 只取自 `intent.agent_id`；
   - participants 由 Agent A 直接指定（V1 TEMPORARY）；
   - `producer` 必填；`acceptance_criteria` 至少 1 条；
   - 创建 Root Task + Workspace + Shared Context；
   - root Directory Lock handoff 给 producer；
   - Task.budget 保持 None；人类预算写 TaskBudget（OBSERVE_ONLY）。

3. **RunScheduler**
   - 串行 round-robin；
   - 只看 Task 终态 / 墙钟 / 最大 turns / 无进展；
   - 不建 Task、不选参与者、不设 DoD、不验收。

4. **CLI 先行**
   - `./dev.sh demand --file ... --minutes ... --budget ...`；
   - 不做 Web UI；
   - 输出 run.json / events.jsonl / result.json / deliverable。

5. **动作集**
   - `task_create`（仅 intake）；
   - `workspace_read/write`、`message_send`、`assign_acceptance`、
     `shared_context_read`、`run_tool`（task 阶段）。

### 明确不做（V1）

- 显式邀请/接受（V1.1）；
- Child Task / Task Lineage（以后大工程量任务再做）；
- 价格谈判 / TaskAgreement；
- 并行调度；
- Web UI / 管理后台；
- DemandRun 数据库表 / resume。

## Consequences

### 正面

- 人类第一次可以只给 Agent A 一句话，A 自己创建 Task、选参与者并推进。
- 入口不依赖 experiments；实验 Runner 仍可独立存在。
- Task Core 仍是唯一生命周期权威；Scheduler 不越界。
- 生产者不能自验由现有 TaskExecutionService 强制，真实运行已验证。
- CLI 可脚本化、可测试、可复现；以后 UI 可复用同一套服务。

### 负面 / 代价

- 直接指定参与者没有拒绝权；这是 V1 的临时简化，不是最终经济模型。
- participants 创建时固定，不能中途加人。
- 单个 Root Task，不能派生 Child Task。
- 串行调度；慢 Agent 会阻塞。
- Agent Directory 是启动快照；运行中变化不刷新。
- DemandRun 只有 JSON，没有恢复/历史查询。
- 失败任务仍不会自动做记忆收尾（既有机制只在 COMPLETED 时 finalize）。
- root handoff 给 producer 是 V1 简化；验收人只能读 root。

## Alternatives considered

1. **直接包装 `run_web.py`** —— 拒绝：仍是实验脚手架，预写死团队/DoD。
2. **先做 Web UI** —— 拒绝：核心 Task 创建/调度未稳定，UI 会锁死 API。
3. **让 CLI 直接 `Task.create`** —— 拒绝：违反 ADR-0024（Creator 始终是 Agent）。
4. **把 Scheduler 放进 TaskService** —— 拒绝：Task Core 不负责调度。
5. **V1 就做 Child Task** —— 拒绝：Task Lineage 是独立大工程；先 Root Task 跑通。

## References

- [`DEMAND_ENTRY_V1.md`](../DEMAND_ENTRY_V1.md)
- [`TASK_ORIGIN_LINEAGE.md`](../TASK_ORIGIN_LINEAGE.md)
- [`TOOL_RUNTIME_V1_DESIGN.md`](../TOOL_RUNTIME_V1_DESIGN.md)
- ADR-0024（Human Demand ≠ Task；Creator 始终是 Agent）
