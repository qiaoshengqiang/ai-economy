# Demand Entry V1 —— 人类一句话 → Agent 自建 Task → 串行执行

> 状态：**`CURRENT`（V1 已实现）**
> 关联：ADR-0042 · `demand/` · `actions/task_create.py` · `./dev.sh demand`

---

## 1. 它解决什么

在此之前，任意 Human Demand 只能通过实验脚本提交：
`run_web.py` / `run_demand.py` 会预先写死 Agent 名单、命名空间和 DoD。
人类不能“只给 Agent A 一句话”，让 A 自己建 Task、选人、推进。

Demand Entry V1 提供：

```text
Human
  ↓ ./dev.sh demand --file prompt.txt
DemandRun（人类请求记录，不是 Task）
  ↓
Agent A（front agent）收到 Demand + Agent Directory
  ↓ task_create
Root Task（creator = Agent A）
  ↓
RunScheduler（串行轮询 participants）
  ↓
Agent 自己 START / 工作 / SUBMIT / VERIFY / COMPLETE
  ↓
run 目录 + deliverable + 成本
```

---

## 2. 关键边界

| 边界 | V1 事实 |
|---|---|
| Human ≠ Task Creator | CLI 只创建 `DemandRun`；Task 由 Agent A 通过 `task_create` 创建 |
| Demand ≠ Task | `DemandRun` 是运行记录，不复制 Task 状态 |
| Task Core 唯一权威 | `DemandRun.status` 是 Run 状态；Task.status 只读 Task Core |
| Scheduler ≠ TaskManager | Scheduler 只选 turn / 送消息 / 停；不建 Task、不设 DoD、不验收 |
| 生产者 ≠ 验收人 | 由 TaskExecutionService 强制；producer 不能 VERIFY/COMPLETE |
| Budget ≠ Price | 人类 `--budget` 写 TaskBudget（OBSERVE_ONLY）；Task.budget 保持 None |
| Workspace ≠ Permission | 仍走 WorkspaceGateway；root 的 Directory Lock 在创建时交给 producer |

### V1 的临时简化（`V1 TEMPORARY`）

- participants 由 Agent A **直接指定**，没有邀请/接受；
- 一个 DemandRun 只创建一个 **Root Task**；
- 没有 Child Task / Task Lineage；
- 没有价格谈判 / TaskAgreement；
- 调度串行；
- `DemandRun` / events 用 JSON 文件，不新增数据库表。

这些是 V1 的明确取舍，不是最终架构。

---

## 3. CLI

```bash
# 从文件读完整提示词
./dev.sh demand --file /tmp/prompt.txt --minutes 20 --budget 20

# 或直接给一句话
./dev.sh demand --demand "做一个 ..." --minutes 10
```

参数：

| 参数 | 默认 | 含义 |
|---|---|---|
| `--demand` / `--file` | 二选一 | Human Demand |
| `--minutes` | 20 | 总墙钟硬上限 |
| `--budget` | 20 | TaskBudget（AIC，观测） |
| `--max-turns` | 30 | 最多 Agent turn |
| `--max-agents` | 5 | 参与者上限 |
| `--front-agent` | `agent_a` | 接收 Demand 的 Agent |
| `--cognitive-budget` | 16384 | 单次输出上限 |
| `--db` | run 目录/demand.db | Runtime 数据库 |
| `--out` | `runs/demand/<ts>-<id>` | 输出目录 |
| `--quiet` | false | 只打最终结果 |

输出：

```text
runs/demand/<ts>-<id>/
├── demand.db
├── run.json
├── events.jsonl
├── result.json
└── deliverable/       ← Task Workspace 导出的文件
```

---

## 4. `task_create`

Agent A 在 intake 阶段唯一可用的动作：

```json
{
  "type": "task_create",
  "parameters": {
    "description": "...",
    "participants": ["agent_a", "agent_b"],
    "acceptance_criteria": ["交付物存在且非空", "测试通过"],
    "producer": "agent_b",
    "acceptance_assignee": "agent_a"
  }
}
```

规则：

- `creator` **只来自 `intent.agent_id`**，payload 不能伪造；
- participants 必须包含 creator，至少 2 人，最多 `--max-agents`；
- 所有 participants 必须存在于 Agent Directory 且 `available=True`；
- `producer` 必填，且必须是 participants 之一；
- `acceptance_criteria` 至少 1 条；
- `acceptance_assignee` 不能是 creator，也不能是 producer（验收人不能自验）；
- 一个 DemandRun 只能创建一个 Root Task。

创建时：

1. 创建 `WS-DEMAND-<run_id>`；
2. `Task.create(creator=..., participants=..., budget=None)`；
3. `task.claim_producer(producer)`；
4. `runtime.create_task(task)`（同时建 Shared Context）；
5. 写 `SET_OBJECTIVE` + `ADD_ACCEPTANCE_CRITERION`；
6. 把 Workspace root 的 Directory Lock 从 creator **handoff 给 producer**
   （否则 producer 能读、不能写）；
7. 可选：`assign_acceptance` 指派验收人；
8. 设置 TaskBudget（OBSERVE_ONLY）。

---

## 5. Scheduler

`RunScheduler` 只依赖两个回调：

```python
RunScheduler(
    participants=[...],
    run_turn=...,      # (agent_id) -> TurnOutcome
    task_status=...,   # () -> Task.status
    max_turns=30,
    deadline=...,
)
```

行为：

- 串行 round-robin；
- Task 进入终态（COMPLETED / FAILED / CANCELLED / REJECTED / EXPIRED）立即停；
- 墙钟 / 最大 turn / 连续无进展时停；
- 不建 Task、不选参与者、不设 DoD、不验收。

---

## 6. Agent 看到什么

每个 turn 的 prompt 只给环境事实：

- Human Demand / 运行约束；
- Agent Directory（id / 名称 / capabilities / available）；
- Task Core 事实（status / creator / contractor / participants / acceptors）；
- Task Shared Context（objective / DoD / assignments）；
- Workspace 文件列表；
- 本轮 inbox 消息；
- 可用动作与生命周期载荷示例。

Agent 自己决定：谁生产、怎么分工、写什么文件、何时 SUBMIT/VERIFY/COMPLETE。

---

## 7. 真实运行证据

```text
Run: demand_d10238e4
Demand: 创建 hello.py + pytest 测试
Result: COMPLETED
Turns: 21 / max 30
Tokens: 89,759
Cost: 0.897590 AIC
Task: task_72eb1c89
  creator=agent_a, contractor=agent_a
  participants=[agent_a, agent_e]
  acceptors=[agent_e]
Events: START → SUBMIT → VERIFY → COMPLETE
Deliverable: hello.py, tests/test_hello.py
```

这次运行还验证了两条守卫：

- agent_e（acceptor）尝试 SUBMIT 被拒（不是 contractor）；
- agent_a（producer）尝试 COMPLETE 被拒（生产者不能自验/结项）；
- 最终由 agent_e VERIFY + COMPLETE。

---

## 8. 已知限制

1. **直接指定参与者**，没有邀请/接受；被指定者没有拒绝权（V1 TEMPORARY）。
2. **单个 Root Task**；没有 Child Task / Lineage。
3. **串行调度**；慢 Agent 会阻塞其他人。
4. **没有价格谈判 / TaskAgreement**；只有 TaskBudget（OBSERVE_ONLY，不阻断）。
5. **没有中途加人**；participants 在创建时固定。
6. **Agent Directory 是启动快照**；运行中上下线不会刷新。
7. **DemandRun 用 JSON**，没有数据库历史/恢复；`--resume` 未实现。
8. **失败任务不自动收尾记忆**（Short-term/long-term 仍只在 Task COMPLETED 时处理）。
9. **Workspace root handoff 给 producer** 是 V1 简化；验收人只能读，不能写 root。

---

## 9. 未来（V1.1+）

- `TaskInvitation` + `task_invite` / `task_invite_response`；
- Child Task / Task Lineage（单独的大工程）；
- 价格谈判 / TaskAgreement；
- `agent_search`（目录很大时）；
- `--resume` / DemandRun 持久化；
- 并行调度 / 工具异步化；
- 只读 Web 观察台（run / task / messages / artifacts / cost）。

---

## 10. 测试

```bash
./dev.sh test tests/test_demand_entry.py
./dev.sh test tests/test_tool_runtime.py
```

覆盖：

- `task_create` creator 不可伪造；
- participants / producer / DoD / acceptor 校验；
- Root Task 创建 + root handoff；
- 重复 task_create 被拒；
- Scheduler 终态 / max turns / time limit / no progress；
- CLI 参数。
