# ADR-0045：Agent 自主认知预算 + 可见的截断/裁剪反馈

- **状态**：Accepted（**已实现**）
- **时间**：Demand Entry V1 真实运行修复
- **Supersedes**：无
- **Superseded by**：无

## Context

两次真实 Demand 运行暴露了同一类问题：

1. `--cognitive-budget` 被当成“固定请求值”，Agent 无法按任务难度调整；
2. Agent 不知道 `run_tool` 的写不会回写 Workspace；
3. Agent 不知道 Playwright 必须先写 `*.spec.js`；
4. 验收人无法复跑时不发 `VERIFY(passed=false)`，只反复读；
5. `RunScheduler` 把任何 Action 都算进展，重复读文件也会重置 NO_PROGRESS；
6. `MAX_TURNS` 只打印状态，不说明卡在谁/等什么；
7. 信封解析失败没有原始输出，无法诊断。

本 ADR 只记录 **1 / 5 / 6 / 7** 的结构决定。工具边界见 ADR-0040 与
`resources/public/tools/*.md`。

## Decision

### 1. 认知预算分三层

```text
Agent 偏好（信封 cognitive_budget / Brain phase policy / config 默认）
        ↓
Human / 系统的 cognitive_budget_cap（硬上限）
        ↓
Provider 结构裁剪（MIN=256 / MAX=32768）
        ↓
requested / actual / budget_clipped
```

- `Brain.set_cognitive_budget(value, phase=...)` 保存**偏好**；
- `requested_cognitive_budget` 是显式请求；`cognitive_budget_cap` 是硬上限；
- `generate()` 计算 `actual = min(偏好, 上限)`；
- Provider 仍负责自己的结构裁剪；
- 响应暴露 `requested_cognitive_budget` / `actual_max_tokens` /
  `budget_clipped`；
- 下一轮 prompt 显示上一轮的 `requested / actual / clipped / truncated`。

### 2. 截断的段不执行动作

保持既有规则：`finish_reason=length` → 该段 `cognitive_status=TRUNCATED`
→ 即使解析出合法 Action 也不执行。截断是事实，不是“Agent 不行动”。

### 3. 无进展只认真实进展

`TurnOutcome` 增加 `progress`。DemandRunner 只在以下情况置 True：

- Task 状态变化；
- 成功 `workspace_write` / `workspace_edit`；
- 成功 `resource_create`；
- **首次出现**的消息（重复同一内容不算）。

`workspace_read` / `run_tool` 是观察，不算进展。

### 4. 结束报告回答“卡在哪”

`MAX_TURNS` / `TIME_LIMIT` 时写 `blocking`：

- 当前 Task 状态；
- 等待的角色与动作（START / SUBMIT / VERIFY / COMPLETE）；
- 最后进展 turn；
- 最近消息。

### 5. 信封失败落盘

`events.jsonl` 新增 `envelope_error`，含原始输出的有界 `head` / `tail`
（各 1200 字符）。CLI 同时保存 `run.log`（终端输出 + logging）。

### 明确不做

- 不由 AIC 余额自动换算 token 预算；
- 不自动替 Agent 决定预算/思考强度；
- 不把 `workspace_read` / `run_tool` 当作进展；
- 不把截断包装成 Provider 失败；
- 不新增第二套 Task 状态机。

## Consequences

### 正面

- Agent 可以按任务难度自主调节预算与思考强度；
- 裁剪/截断对 Agent 可见，下一轮可自行调整；
- 重复读文件不会再把 NO_PROGRESS 骗过去；
- 失败运行能直接看到阻塞点与原始信封片段。

### 负面 / 代价

- 预算可超过 config 的 `brain.max_tokens`（Provider MAX 内），
  复杂任务成本可能上升；
- no-progress 更严格，可能提前停止“长期只读不写”的运行；
- `run.log` / `envelope_error` 增加少量磁盘占用（有界）。

## References

- `agents/brain.py`
- `actions/parse.py` · `actions/run.py`
- `llm/base.py` · `llm/openai_compatible.py`
- `demand/runner.py` · `demand/scheduler.py` · `demand/cli.py`
- `resources/public/tools/*.md`
