# ADR-0046：两阶段 Intake + Workspace Plan 锁一致性

- **状态**：Accepted（**已实现**）
- **时间**：Demand Entry V1 真实运行修复
- **Supersedes**：无
- **Superseded by**：无

## Context

真实 run `demand_a3e3a626` 暴露了一个角色压缩问题：

- V1 的入口 Agent 同时承担 Front（解读 Human Demand）与 Creator（架构/分工）；
- 当前实现把两件事压在同一次 `build_intake_prompt` 调用里；
- A 在计划中把 root 级 `index.html` / `MODULE_CONTRACT.md` 分给自己，
  但 Task 创建时 root Directory Lock 会交给 producer（C）；
- 计划与锁不一致，A 后来只能依赖 C 的 handoff；
- 没有 `MODULE_CONTRACT.md`，集成 main.js 时只能反推模块签名；
- 最终 main.js 保持占位，Task 未完成。

## Decision

### 1. Intake 拆成两个阶段

```text
Stage 1 Front / 解读
    → brief（goal / deliverables / constraints /
      acceptance_expectations / ambiguities / non_goals）
    → 不创建 Task，不选 producer/acceptor，不执行 action

Stage 2 Creator / 规划
    → 输入 brief + Agent Directory + Workspace 锁规则
    → workspace_plan + task_create
```

同一个 Agent 可以承担两个阶段，但必须是**两次独立的 prompt/turn**。
Front 失败时可以用 `fallback_brief` 保底，但不会跳过 Creator 规划。

### 2. `task_create` 新增必填 `workspace_plan`

```json
"workspace_plan": {
  "root_owner": "agent_c",
  "directories": {"src": "agent_c", "tests": "agent_e"},
  "files": {"index.html": "agent_c", "src/main.js": "agent_a"}
}
```

`TaskCreationService` 校验：

- `root_owner == producer`（V1 root lock 交给 producer）；
- 所有 owner 都是 participants；
- 根级文件 owner == root_owner；
- 子目录/文件的父目录必须在 `directories` 里声明；
- plan 不能为空；
- 不合法返回结构化错误，Creator 带错误重试。

### 3. 不新增 Domain Entity

brief 与 workspace_plan 是 **DemandRun 的运行元数据**（`run.json`），
不是 Task、不是 Shared Context、不是新的持久化状态机。

### 明确不做

- 不引入 `HumanTask` / `FrontTask`；
- 不把 Front Agent 做成中央 Coordinator；
- 不自动创建目录/handoff（计划只做结构校验，执行仍由 Agent 动作）；
- 不自动修改 Task description / DoD。

## Consequences

### 正面

- 解读与架构规划在时间、上下文、prompt 上分开；
- 计划与 root lock/participant 边界在 Task 创建前被校验；
- 失败可带具体错误重试；
- `run.brief` / `run.workspace_plan` 可审计。

### 负面 / 代价

- Intake 多一次 LLM 调用；
- `workspace_plan` 增加 schema 与校验失败的重试成本；
- 仍是同一个 Agent 承担两个角色，不是真正的独立 Front Agent；
- 计划只保证“结构上可执行”，不保证后续 Agent 按计划行动。

## References

- `demand/intake.py`
- `demand/prompt.py`（`build_brief_prompt` / `build_intake_prompt`）
- `demand/task_creation.py`
- `actions/task_create.py`
- `docs/architecture/DEMAND_ENTRY_V1.md` §3.2
