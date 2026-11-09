# ADR-0053：Workspace Plan 阶段 / 依赖 / 检查点 / 占位标记

- **状态**：Accepted（**已实现**）
- **时间**：Demand Entry V1 真实运行修复
- **Supersedes**：无
- **Superseded by**：无

## Context

真实 run `demand_da850bbc`：

- `workspace_plan` 只声明了“文件 → owner”；
- A 先写 `index.html`，它引用 `src/main.js`；
- A 又写了 `src/engine.js` / `src/world.js`，但两者没有被 index 引用；
- `src/main.js`、`train.js`、`effects.js`、`ui.js`、`README.md` 都没写；
- 最后尝试 handoff `src/ui.js` 时，文件不存在且 `to` 为空；
- 24 分钟后 `NO_PROGRESS`，Task IN_PROGRESS。

问题不是“文件清单不存在”，而是：

> **没有开发顺序、依赖关系、阶段检查点和“占位 vs 已实现”的区分。**

同时，不能把“每阶段跑 DOM/import + smoke”写死进系统 ——
那是 Web 任务的检查，不是 AIEOS 的通用规则。

## Decision

### 1. `workspace_plan` 可选 `phases`

```json
{
  "phases": [
    {
      "name": "skeleton",
      "depends_on": [],
      "files": ["index.html", "src/main.js"],
      "checkpoint": "入口能加载，本地 import 可解析"
    },
    {
      "name": "world",
      "depends_on": ["skeleton"],
      "files": ["src/world.js"],
      "checkpoint": "world 能渲染"
    }
  ]
}
```

- `name` 唯一；
- `depends_on` 必须引用已存在的 phase，不能自依赖/成环；
- `files` 必须是 `workspace_plan.files` 中声明过的文件；
- `checkpoint` 是 **Creator 写的自由文本目标**，系统不解释、不硬编码检查方式；
- `phases` 可选：旧 plan 不写时结构完全不变。

### 2. `placeholders`

```json
{"placeholders": ["src/world.js"]}
```

- 标记“已创建但尚未实现”的文件；
- 计划完成度显示 `⬜ placeholder（已创建，尚未实现）`，不再当成 ✅；
- 实现后由 Creator 从 `placeholders` 移除。

### 3. `workspace_plan_checkpoint` 动作

```json
{
  "type": "workspace_plan_checkpoint",
  "parameters": {
    "phase": "skeleton",
    "passed": true,
    "evidence": {"path": "index.html", "revision": 2},
    "note": "smoke + import graph"
  }
}
```

- 任何 Task participant 都可以记录；
- phase 必须存在；
- `passed` 必须是布尔值；
- `evidence` 可选；若带 `path`/`revision`，系统校验 artifact 存在与 revision；
- 记录写入 `run.plan_checkpoints` 和 `run.json`；
- 它是 **Agent 的阶段检查记录，不是 Task 验收**；
- 检查方式完全由 Agent 决定：`run_tool`、`workspace_read`、人工检查都可以。

### 4. 计划完成度显示阶段状态

System 派生：

```text
✅ checkpoint passed (by agent_a；self-report)
⏳ pending；waiting=skeleton
❌ files missing: src/world.js
▶ files present；checkpoint not recorded
```

- checkpoint passed 但文件缺失 → 额外警告；
- checkpoint failed → 显示 failed；
- 有 placeholder → 额外标记。

系统**不**判断检查是否充分，只显示“谁记录了什么”。

### 5. Plan 更新会失效旧 checkpoint

Creator 更新 `workspace_plan` 时：

- phase 被删除 → 对应 checkpoint 删除；
- phase 定义改变 → 对应 checkpoint 删除；
- 未变化的 phase 保留 checkpoint。

### 6. Provisioning 是 Creator 的显式动作，不是 task_create 的副作用

- `task_create` 仍然只创建 Task / Workspace / Shared Context；
- 不自动创建所有空文件；
- Creator 按 phases 自己执行：
  1. 建目录；
  2. 建入口 skeleton + 模块最小骨架；
  3. 需要交接的文件/目录先创建，再 `resource_handoff`；
  4. 用 `placeholders` 标记未实现文件；
  5. 每个 phase 自己选择检查方式并记录 checkpoint。

## 明确不做

- 不把每阶段检查硬编码为 DOM/import/Playwright；
- 不自动创建空文件（避免“存在=完成”的假绿）；
- 不把 `phases` / checkpoint 变成 Task 状态或 Shared Context；
- 不把 checkpoint 当成系统验收；Task 验收仍由 acceptor 负责；
- 不自动重排/猜依赖；顺序由 Creator 声明。

## Consequences

### 正面

- 文件清单变成可执行的开发顺序；
- 入口 skeleton 与模块顺序可见；
- 需要 handoff 的资源可以先创建再交接；
- 占位文件不会冒充“已实现”；
- 阶段检查点通用，不绑定 Web 任务；
- Plan 变更后旧 checkpoint 不会残留。

### 负面 / 代价

- plan schema 变大，Creator 需要写 phases；
- checkpoint 是 Agent 自报，不是系统验证；
- `placeholders` 需要 Creator 主动维护；
- 系统仍不能强制 Producer 按 phases 执行。

## References

- `demand/task_creation.py`
- `demand/runner.py`
- `demand/prompt.py`
- `demand/models.py`
- `demand/actions.py`
- `actions/workspace_plan_checkpoint.py`
- `tests/test_workspace_plan_phases.py`
- `tests/test_demand_prompt_facts.py`
- `docs/architecture/DEMAND_ENTRY_V1.md`
