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

### Front 模式

`Front 模式` 是常规设置里的全局开关，影响之后所有 Front 决策：

```text
autonomous  自主：可以补全 brief（goal / deliverables / constraints /
            acceptance / files），也可以重写 task_text
relay       只传达：只保留用户的核心任务要求，不添加需求
```

`relay` 模式：

- `task_text` 保留用户明确提出的任务细节，删除对话性、路由性文字；
- 不添加用户没有提出的功能、技术栈、文件、验收标准、约束或实现建议；
- handoff 的 brief 只保留 `goal` 和 `task_text`；
- Controller 会再清空 `deliverables` / `constraints` /
  `acceptance_expectations` / `files` 等字段，避免 Front 扩写进入 Task；
- `role` / `work_profile` / `preferred_agent` 仍可作为路由元数据；
- 它只控制 Front 的传达方式；如果希望 Worker 也只看到原始需求，
  应配合 Work 的 `autonomous` 模式使用。

Front 还有一个 `set_mode` 控制 intent，可以在对话里修改：

```json
{"intent":"set_mode","front_mode":"relay","work_mode":"autonomous",
 "reply":"已切换"}
```

- 两个字段都可选；未提供的不变；
- 只修改全局设置，不创建 Task；
- 修改后对后续 Front 调用 / 新任务生效；
- 同一回合里先 `set_mode` 再 handoff 时，handoff 按新模式处理；
- 当前模式会作为只读上下文注入 Front prompt，Front 可以回答“现在是什么模式”。

### Output Commit 确认

Task 完成后，如果配置了输出仓库，Front 会看到待确认提交的短码。
人类可以用自然语言确认，例如「提交吧」「可以」「就这个」。
Front 从完成消息与待确认列表推断 `task_id` / `commit_code`；
多个候选时先追问清楚。

```json
{"intent":"commit_confirm","task_id":"task_x","commit_code":"abc12345",
 "reply":"已提交"}
```

- Front 负责转达人类确认；
- 实际提交由系统 `OutputCommitService` 执行；
- 提交请求来自人类确认或预授权策略；
- 提交结果留在 `commit_requests` / 审计 / 输出仓库。

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

- 可用动作：
  - `artifact_read {task_id, path, offset?, limit?}`：只读 Task Workspace
    文件内容；不创建 Task，不读宿主绝对路径；
  - `private_read` / `private_write` / `private_list` /
    `private_delete`；`draft_read` / `draft_write` / `draft_append` /
    `draft_list` / `draft_delete`。
- `artifacts` intent 只负责把 Task 文件导出到宿主机并返回路径，
  **不返回内容**；要看源码/前 N 行用 `artifact_read`。
- 动作只碰 Front 自己的私有空间 / 草稿，以及只读的 Task Workspace；
  不写 Task Workspace。
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
| Agent 状态 | `IDLE` / `WORKING` / `STOPPING` · `FRONT` / `WORKER`；Work Agent 卡片可直接切换思考强度（Prototype 固定 max） |
| Agent 中途提问 | Agent 卡片显示气泡并播放提示音；点击气泡直接回答，不经过 Front；Task 保持 IN_PROGRESS |
| 设置按钮 | **系统设置**：服务商 / 模型 / API 凭据 / 系统信息；**常规设置**：Front Agent / 最大并行任务 / 单任务轮次 / Work 总时长开关 / Agent 启停与引擎 / Worker Roles / Prompt 日志开关。详见 [`UI_SETTINGS_DESIGN.md`](UI_SETTINGS_DESIGN.md) |

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
- 轮询按内容签名增量重绘：内容没变时不重建 DOM；
- 检测到文本选区（含拖选中的瞬时状态）时暂停列表重绘，
  用户点掉选区后的下一次轮询再补上最新数据 ——
  否则 1.2s / 2s 的整页重绘会清掉手动选择；
- 复制按钮容器不拦截文字选择，只有按钮本身可点击；
- 任务认知 + 每轮 phase 明细（decision / work / summary 原文与动作）已落盘；
  完整 system prompt / 原始 HTTP response 仍未保存；
- Front 收到 `artifacts` 时把产物从 DB 导出到宿主机
  `runs/exports/<task_id>/`，并回复绝对路径；
- 工具用法通过 `tool_help` 自查，文档源在
  `resources/public/tools/<tool>.md`，不在 prompt 里重复维护；
- Team 路径的「运行中重新计划」仍沿用 `work_plan_update`（当前 UI 不触发）；
- 常规设置中的最大并行任务数在进程内生效；Front Agent / 轮次 / Agent 启停 / 引擎写入 `runs/ui/ui_settings.json`；
- 系统设置写入 `config/llm.yaml` 与 `.env`，需要重启 UI 进程；UI 显示“重启后生效”；
- Worker Roles 写入 `config/worker_roles.yaml`，下一个任务生效；删除使用归档；
- Prompt 日志默认关闭；开启后写入 `runs/ui/prompts/prompts-YYYY-MM-DD.jsonl`，
  记录每次模型输入的 system / messages，不记录 API key。

---

## 6. 持续改造原则

新增机制时：

1. 先判断它属于 Life / Work / Memory / Interaction 哪一层；
2. 优先复用已有服务与数据契约；
3. 不因为换 UI / 换入口而复制一套 Task 或 Work runtime；
4. 把新机制写进对应架构文档与守卫测试。

> **UI 是入口，不是新的执行框架。**

### 产物仓库

任务输入区**不显示**产物仓库选择。任务输入区只保留「工作目录」。
产物仓库默认值来自 Settings 的 `commit_output_repo`：

- Task Workspace 任务完成后，Output Commit 使用该默认仓库；
- Project Workdir 任务直接修改项目目录，不创建 Output Commit；
- 目标必须是现有 Git 仓库，AIEOS 主仓库会被拒绝；
- 提交结果消息仍会显示实际仓库路径、`tasks/<task_id>` 目录、分支和 commit。
