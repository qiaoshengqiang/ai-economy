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
  ├─ follow_up / rework / cancel → 已有 Task
  └─ handoff → Brief
                  ↓
            选择一个 IDLE Agent
                  ↓
            创建 SOLO Task
                  ↓
            SoloWorkRuntime
                  ↓
            FrontResult → 输出框
```

### 角色约束

- Front Agent **永不创建 Task**；
- Front Agent 不参与执行；
- 承接的 Agent 是普通 Agent，也是 Task Creator；
- 同一任务只占用一个 Agent；
- 工作结束时释放 Agent 回到 `IDLE`。

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

### Solo 的决策 / 工作 / 总结

一轮固定为：

```
decision（决定下一步）
  ↓ 读取 / 工具返回 → 同轮原始回喂
work（最多 2 步）
  ↓
summary（只产文字，不执行动作）
  ↓
下一轮 prompt 注入上一轮完整 summary
```

- `summary` 是独立 phase，可以使用自己的 thinking / cognitive_budget；
- summary 输出完整保留，下一轮以「上一轮总结（完整）」注入；
- 读取 / 工具结果在同一轮直接回喂；写 / 改结果在下一轮
  `action_feedback` 中确定性回显；
- Agent 可以在 JSON 顶层设置：
  - `thinking`: `enabled` + `effort`（DeepSeek 支持 `low` / `high` / `max`），
    支持 `decision` / `work` 分相；
  - `cognitive_budget`: `max_tokens`，支持分相 map；
- `cognitive_budget` 硬上限 **65,536**，实际值取
  `min(偏好, 65536, Provider 上限)`；
- Playwright 产生的截图通过 `image_resolver` 以 `image_url`
  回喂给 Solo Agent；
- Solo 默认最多 **24** 个外层 turn。

---

## 3. 界面元素

| 元素 | 作用 |
|---|---|
| 输入框 | 输入 Human Demand |
| 提交按钮 | 提交给 Front Agent |
| 输出显示框 | Human / Front / 系统消息 |
| Task 卡片列表 | task_id · 状态 · 执行 Agent · 参与者 · 迭代 |
| Task 卡片点击 | 原地展开 / 收起：每轮 decision / work / summary 原文、动作与结果、执行历史、文件、公共 WM、LLM 汇总 |
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
- 每轮 phase 明细（decision / work / summary 原文与动作）已落盘；
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
