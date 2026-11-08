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
| `--cognitive-budget` | 32768 | 单次输出**硬上限**（Agent 可自己选更低） |
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
├── run.log             ← 终端输出 + logging（warning / 截断 / 工具错误）
└── deliverable/       ← Task Workspace 导出的文件
```

---

## 3.1 预算、截断与无进展（2026-11-08）

**认知预算由 Agent 自主控制，Human 只给硬上限：**

```text
Agent 偏好（信封 cognitive_budget / Brain phase policy）
        ↓
min(偏好, --cognitive-budget)
        ↓
Provider 结构裁剪（MIN=256 / MAX=32768）
        ↓
requested / actual / budget_clipped 下一轮可见
```

- 单相：`"cognitive_budget": 32768`（当前相）；
- 分相：`"cognitive_budget": {"work": 32768, "decision": 8192}`；
- 截断（`finish_reason=length`）的段**不执行动作**；
- 下一轮 prompt 显示上一轮 `requested / actual / clipped / truncated`；
- **工作单元压力**：截断或 `clipped=above_*` 时，prompt 明确显示
  “提高预算只改变单次上限，不改变工作单元大小”，并给出可选方向：
  拆成更小的文件/模块/步骤、交给其他 participant、或继续加预算
  （硬上限已到时会说明）；连续 2 次以上会升级提示重新评估工作拆分；
- **历史坐标**：截断 / 预算压力的 critical observation 带
  `observed_at` 与 `cognition_turn_id`，不再使用永久的“上一轮”措辞；
  Context 也标明这是历史事实，可能来自更早的 turn。

**信封修复**：Demand 打开 `repair_malformed=True`，格式坏掉时最多重试一次。

**真实进展**：**强进展** = Task 状态变化 / 成功
`workspace_write|edit` / `resource_create`；只有强进展会重置
`NO_PROGRESS`。普通协调消息只是弱进展，不能无限延长 run。

**2026-11-09 细化**：

- **强进展**（可重置 `NO_PROGRESS`）= 状态变化 / 写入 / 编辑 / 新目录；
  普通协调消息只算弱进展，不能无限延长 run；
- **重复读取去重**：同一 `workspace_read` 的 `path + revision + 行范围`
  跨 turn 不再重复回喂全文，只保留一条“内容未重复发送”；
- **未回应的 REQUEST/QUESTION** 会持续出现在 prompt 顶部的
  “未回应的请求（优先处理）”，直到 Agent 回复或采取
  `resource_create` / `resource_handoff` 等结构动作。

**结束报告**：`MAX_TURNS` / `TIME_LIMIT` 时打印当前 Task 状态、
等待的角色与动作（START / SUBMIT / VERIFY / COMPLETE）、最后进展 turn、
最近消息；同时写进 `result.json` 的 `blocking`。

**信封失败落盘**：`events.jsonl` 新增 `envelope_error` 事件，含原始输出的
有界 `head` / `tail`（各 1200 字符），不需要再靠猜。

**消息类型（2026-11-09）**：

- 普通协调消息用 `TEXT / INFO / NOTE / QUESTION / REQUEST`；
- `START` 只用于 contractor 在 `CREATED/ACCEPTED` 开工，不是“开始说话”；
- `message_send` 返回 `delivery=DELIVERED` 与 `handler_result=DEFERRED`：
  投递成功 ≠ 生命周期动作被接受；接受结果以 Task 状态或 `ERROR` 回执为准；
- prompt 会把 `ERROR` 回执展开成“协议拒绝原因”，并提示不要重发同一类型；
- 生命周期消息不再计入 `new_message` 进展，避免非法 START/ERROR 往返
  阻止 `NO_PROGRESS`。

**路径格式**：Workspace 事实展示相对路径（`index.html`、`src/app.js`、
`tests`），与 `workspace_read/write/edit` / `resource_handoff` 的参数一致；
不要带前导 `/`。

**修复后的截断**：`repair_truncated` 单独记录“原始调用撞上限、修复成功”，
仍然计入工作单元压力，但不会误报“动作未执行”。

---

## 3.2 两阶段 intake（2026-11-09）

V1 的入口 Agent 同时承担 Front 与 Creator，但**不再在同一次 prompt /
同一次调用里既解读 Demand，又做架构/分工**。

```text
Stage 1 Front / 解读
    Human Demand
        → brief:
          goal / deliverables / constraints /
          acceptance_expectations / ambiguities / non_goals
    不创建 Task，不选 participants/producer，不输出 action

Stage 2 Creator / 规划
    brief + Agent Directory + Workspace 锁规则
        → workspace_plan:
          root_owner
          directories: path → owner
          files: path → owner
        → task_create
```

`task_create` 新增**必填** `workspace_plan`，`TaskCreationService` 校验：

- `root_owner` 必须等于 `producer`（V1 root lock 交给 producer）；
- `workspace_plan` 的目录/文件 owner 必须都是 participants；
- 根级文件的 owner 必须是 `root_owner`；
- 文件/子目录的父目录必须在 `directories` 里声明；
- 不合法返回结构化错误，Creator 带错误重试。

这样 A 计划“自己写根级 index.html，但 producer 是 C”会在 `task_create`
时被拒绝，而不是等运行时才发现 root lock 不在自己手里。

Front brief 写入 `run.json` 的 `run.brief`；Creator 计划写入
`run.workspace_plan`，便于事后审计。

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
- **无进展 = 没有真实进展**：状态变化 / 成功写入或编辑 / 新产物 /
  首次出现的有意义消息；单纯的 `workspace_read` / `run_tool` 不算；
- 不建 Task、不选参与者、不设 DoD、不验收。

---

## 6. Agent 看到什么

每个 turn 的 prompt 只给环境事实：

- Human Demand / 运行约束；
- Agent Directory（id / 名称 / capabilities / available）；
- Task Core 事实（status / creator / contractor / participants / acceptors）；
- Task Shared Context（objective / DoD / assignments）；
- Workspace 文件列表；
- **Workspace 责任事实**：root controller / root lock、每个子项的
  effective controller / lock；创建子目录需要父目录锁；
  root 不在自己手里时，走 `resource_handoff` 或 private workspace；
- 本轮 inbox 消息；
- **认知预算事实**：Human 上限 / 当前分相偏好 / 上一轮
  `requested / actual / clipped / truncated`；
- **工具边界**：`run_tool` 在一次性副本执行、写不回 Workspace；
  Playwright 先写 spec；验收人无法复跑时发 `VERIFY(passed=false)`；
- **系统目标与协作事实**：AI Economy OS 的目标之一是低 AIC、短墙钟、
  高质量；合理分工通常能减少单 Agent 上下文负担、匹配 capabilities、
  减少返工；但当前调度是**串行轮转**，增加参与者也有协调成本；
- **拆分与协作（可选方向）**：大块写入前判断能否拆成文件/模块/步骤；
  独立模块可 `resource_handoff`；计划可写进 public working memory；
  一个 producer + 一个巨大文件不是默认方案，是否拆分由 Agent 决定；
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

修复「turn 开始先 drain 再读 Task」后复跑：

```text
Run: demand_613968b8
Result: COMPLETED
Turns: 21 task turns（含 intake 共 22）
Tokens: 118,657
Cost: 1.186570 AIC
Events: START → SUBMIT → VERIFY → COMPLETE
```

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
9. **工作记忆是 Agent 的自主记录**：系统只注入/提供读写，不自动写、不自动总结；
   search 结果也不会自动写进工作记忆。
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
./dev.sh test tests/test_demand_prompt_facts.py
./dev.sh test tests/test_cognitive_budget_autonomy.py
./dev.sh test tests/test_tool_docs.py
./dev.sh test tests/test_tool_runtime.py
```

覆盖：

- `task_create` creator 不可伪造；
- participants / producer / DoD / acceptor 校验；
- Root Task 创建 + root handoff；
- 重复 task_create 被拒；
- Scheduler 终态 / max turns / time limit / no progress；
- **只有真实进展才重置 no-progress**；
- 预算偏好 / 硬上限 / Provider 裁剪；
- prompt 真的投递工具边界 + 预算/截断事实；
- 每个工具 manifest 都有使用说明；
- CLI 参数。
