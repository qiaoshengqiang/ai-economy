# AIEOS UI —— Human ↔ Front ↔ Solo Agent

> **状态：`CURRENT`（2026-11-10）**
>
> 这是 AIEOS 的**人类交互入口**，不是一次性 demo。
> 它复用现有 Task / Workspace / Memory / Work runtime，不另造一套执行机制。

---

## 1. 启动

```bash
./dev.sh ui
# 或
.venv/bin/python -m ui.server --port 8000
```

默认：

```text
http://127.0.0.1:8000/
Front Agent: agent_a
DB: runs/ui/aieos.db
```

可选参数：

```text
--db <path>
--front-agent agent_a
--max-teams 3
--worker-turns 8
--integration-turns 8
--solo-turns 24
--cognitive-budget 65536
```

---

## 2. 交互模型

> **当前模式：`solo_only=True`。** 一个任务只交给一个 Agent 独立完成，
> 不做 Leader 规划、不组队。Team 代码仍保留，但 UI 不会触发。

```text
Human
  ↓ 输入框 / 提交按钮
Front Agent（固定）
  ├─ direct_reply / clarify → 直接显示在输出框
  ├─ follow_up / rework / cancel / artifacts → 已有 Task
  └─ handoff → 只识别意图
                  ↓
            Front 选择 task_text（原文或忠实重述）→ Task description
                  ↓
            选择一个 IDLE Agent
                  ↓
            创建 SOLO Task
                  ↓
            SoloWorkRuntime
                  ↓
            FrontResult → 输出框
```

> Front 自己决定转交方式：可以逐字使用 Human 原文，也可以写一个
> **不改变原意**的 `task_text`；可选的 goal / deliverables /
> constraints / acceptance 作为结构化补充。`human_text` 永远保留原文。
> Front 的 thinking 固定为 `low`，因为它的工作主要是识别意图与沟通。

### 角色约束

- Front Agent **永不创建 Task**；
- Front Agent 不参与 Task 执行；它可以对话、使用自己的**私有空间 / 草稿**准备文件；
- handoff 的 `brief.files` 可以把 Front 准备的文件交给 Work Agent
  （`content` / `from_private` / `draft_id` 三种来源）；
- 承接的 Agent 是普通 Agent，也是 Task Creator；
- 同一任务只占用一个 Agent；
- 工作结束时释放 Agent 回到 `IDLE`。

### Front 的多步能力

Front 在一次 Human 输入内可以**连续调用多次 LLM**：

```text
LLM → 动作（private_* / draft_*）→ 真实结果回喂
    → 继续动作 / 最终 intent（direct_reply / handoff / ...）
```

- 可用动作：`private_read` / `private_write` / `private_list` /
  `private_delete`；`draft_read` / `draft_write` / `draft_append` /
  `draft_list` / `draft_delete`。
- 动作只碰 Front 自己的私有空间与草稿，不写 Task Workspace。
- 最终 handoff 的 `brief.files` 把文件交给 Work Agent；Work prompt
  会看到文件内容。
- Front 一次可以给出多个最终动作：
  ```json
  {"decisions":[
    {"intent":"cancel","task_id":"task_a","reply":"task_a 已停"},
    {"intent":"cancel","task_id":"task_b","reply":"task_b 已停"}
  ]}
  ```
  控制类动作（direct_reply / clarify / cancel / artifacts）按顺序执行；
  handoff / rework / follow_up 每回合最多一个，并在控制动作之后执行。
- 每个最终动作自己的 `decision.reply` 都会显示为 Front 消息；
  cancel 失败时用控制面事实纠正，不照抄可能错误的回复。
- 每次 decide 的 `FrontDecision`（含 `raw_output`）和动作结果写入
  `runs/ui/jobs/<job_id>.json` 的 `front_decision` / `front_actions`。
- Front LLM 调用失败或输出无法解析时，Job 标记 `INTERRUPTED` +
  `retryable`，不伪装成 `clarify`，也不创建 Task；UI 提供「重试」。

### 已有 Task 的后续输入

| Front 判断 | 控制面行为 |
|---|---|
| `follow_up`，Task 有 Job 在跑 | 只回复真实状态，不重复执行 |
| `follow_up`，Task 已中断 | 用原 Agent 从当前状态恢复执行 |
| `rework`，Task 已完成 | 追加本轮要求 → Reopen（iteration +1）→ 原 Agent 重跑 |
| `rework`，Task 未完成 | 按 `follow_up` 恢复执行 |
| `cancel`，Task 有 Job 在跑 | 标记停止 → Task `CANCELLED`；Agent 显示 `STOPPING`，当前步骤结束后释放 |
| `cancel`，Task 没有 Job | 直接把 Task 置为 `CANCELLED` |
| `artifacts` | 从 DB 读取 Task Workspace → 导出到 `runs/exports/<task_id>/`，回复绝对路径 |

> `direct_reply` **不能**声称“已经停止”——停止必须由 Front 输出
> `cancel`，控制面才真的取消 Task 并释放 Agent。
>
> Solo 模式下，旧的 TEAM 任务不能继续 / 返工；Front 会被控制面
> 明确告知，Human 需要重新提交一个新任务。

Front 看到的开放 Task 状态来自两个事实：Task Core 的状态 + 是否有
Job 正在执行。没有 Job 的开放 Task 标记为 `INTERRUPTED`，
不会被描述成「正在处理中」。

### Solo 的任务认知 + 决策 / 工作 / 总结

启动时先做一次**任务认知**（只读）：

```
cognition（thinking=max）
  ├─ 读环境：workspace 结构 / 已有代码 / 依赖
  ├─ 查工具：tool_help
  └─ 输出完整任务认知报告
        ↓
  报告原文注入后续所有 Work prompt
        ↓
decision（决定下一步）
  ↓ 读取 / 工具返回 → 同轮原始回喂
work（最多 4 步）
  ↓
summary（只产文字，不执行动作）
  ↓
下一轮 prompt 注入上一轮完整 summary + 任务认知快照
```

任务认知是**启动时快照**；执行中环境可能变化，Agent 仍需按当前
Workspace 事实重新 read / search。

- `summary` 是独立 phase，可以使用自己的 thinking；
- summary 输出完整保留，下一轮以「上一轮总结（完整）」注入；
- 读取 / 工具结果在同一轮直接回喂；写 / 改结果在下一轮
  `action_feedback` 中确定性回显；
- Agent 可以在 JSON 顶层设置：
  - `thinking`: `enabled` + `effort`（DeepSeek 支持 `low` / `high` / `max`），
    支持 `decision` / `work` 分相；
- `cognitive_budget` **不由 Agent 设置**：Work 每次调用使用运行时
  `--cognitive-budget`（UI 默认 **65,536**）作为实际输出预算；
  prompt 只把 effective budget 作为事实展示，不提供调参说明；
- Playwright 产生的截图通过 `image_resolver` 以 `image_url`
  回喂给 Solo Agent；
- Solo 默认最多 **24** 个外层 turn。

---

## 3. 界面元素

| 元素 | 作用 |
|---|---|
| 输入框 | 输入 Human Demand |
| 提交按钮 | 提交给 Front Agent |
| 输出显示框 | Human / Front / 系统消息；Front 回复下方显示本次 **thinking 强度 + 耗时**（Front 决策耗时或整次 Job 耗时） |
| Task 卡片列表 | task_id · 状态 · 执行 Agent · 参与者 · 迭代 · **Front → Work 提示词前 3 行**（超出省略） |
| Task 卡片点击 | **同一标签页**打开 `/task.html?task_id=...`：任务认知 + 每轮 decision / work / summary 的 thinking、输出、动作与结果；每个 phase 显示 **thinking 强度 + LLM 调用耗时**，round 标题显示本轮总耗时，动作显示**工具执行耗时**；外加执行历史、文件、公共 WM、LLM 汇总（calls / tokens / 总耗时 / 平均耗时 / AIC） |
| Agent 状态 | `IDLE` / `WORKING` / `STOPPING` · `FRONT` / `WORKER` |
| 设置按钮 | 修改 Front Agent / 最大并行任务 |

---

## 4. 后端结构

```text
ui/server.py          标准库 HTTP server + 静态文件
ui/static/            无构建 HTML / CSS / JS
work/controller.py    AIEOSController：Front / Solo Agent 编排
```

### API

```text
GET  /api/state            前端状态（agents / tasks / jobs / messages / settings）
GET  /api/task/<task_id>   任务详情（执行历史 / 文件 / 公共 WM / LLM 汇总）
POST /api/submit           {"text": "..."}
POST /api/settings         {"front_agent": "...", "max_parallel_teams": 3}
GET  /api/health
```

`AIEOSController` 只做编排：

- 调用 `FrontAgentRuntime`（先对照进行中 / 已完成 Task，
  避免把同一请求重复建任务）；
- `solo_only=True` 时选择一个空闲 Agent；
- 调用 `TaskCreationService` 创建 SOLO Task；
- 调用 `SoloWorkRuntime` 执行；
- 把 `WorkResult` 转成 `FrontResult` 显示；
- `solo_only=False` 时保留旧的 Leader / Team 路径（仅用于对照实验）。

不复制：

- Task 状态机；
- Workspace 锁 / revision / audit；
- Memory / Draft；
- Work 生命周期；
- TeamScheduler / AgentAvailability 的语义。

---

## 5. 当前边界

- Workspace 仍然是**逻辑资源树**，不自动落成宿主机目录；
- 绝对路径暂不暴露给 Front（按当前阶段决定）；
- 任务结果直接来自 DB / runtime，不生成 `result.json`；
- 每次 UI Job 的可见日志保存到 `runs/ui/jobs/<job_id>.json`（job + messages）；
- 每轮 decision / work / summary 明细保存到 `runs/ui/rounds/<task_id>.json`；
- Task 详情页用 `/api/task/<task_id>` 每 2 秒轮询，暂不引入 SSE；
  当前轮次数据量下，轮询不影响现有实现；
- 任务认知 + 每轮 phase 明细（decision / work / summary 原文与动作）已落盘；
  完整 system prompt / 原始 HTTP response 仍未保存；
- Front 收到 `artifacts` 时把产物从 DB 导出到宿主机
  `runs/exports/<task_id>/`，并回复绝对路径；
- 工具用法通过 `tool_help` 自查，文档源在
  `resources/public/tools/<tool>.md`，不在 prompt 里重复维护；
- Team 路径的「运行中重新计划」仍沿用 `work_plan_update`（当前 UI 不触发）；
- 设置中的最大并行任务数在进程内生效；重启按启动参数。

---

## 6. 持续改造原则

新增机制时：

1. 先判断它属于 Life / Work / Memory / Interaction 哪一层；
2. 优先复用已有服务与数据契约；
3. 不因为换 UI / 换入口而复制一套 Task 或 Work runtime；
4. 把新机制写进对应架构文档与守卫测试。

> **UI 是入口，不是新的执行框架。**
