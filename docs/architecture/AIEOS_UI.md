# AIEOS UI —— Human ↔ Front ↔ Team

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
--cognitive-budget 32768
```

---

## 2. 交互模型

```text
Human
  ↓ 输入框 / 提交按钮
Front Agent（固定）
  ├─ direct_reply / clarify → 直接显示在输出框
  └─ handoff → Brief
                  ↓
            选择一个 IDLE Agent 作为 Leader
                  ↓
            Leader 规划 + 从 IDLE Agent 中选择成员
                  ├─ 选了成员 → TEAM Task → TeamWorkRuntime
                  └─ 没选成员 → SOLO Task → SoloWorkRuntime
                  ↓
            FrontResult → 输出框
```

> Leader 决定独立完成（只把 item 分给自己）不是失败：控制面按
> `SOLO` 执行，避免为了凑团队而制造无意义分工。

### 角色约束

- Front Agent **永不创建 Task**；
- Front Agent 不参与组队；
- Leader 是普通 Agent，也是 Task Creator；
- Leader 只能从当前 `IDLE` 的 Agent 中选择成员；
- Task 就是团队容器，不新增 Team 实体；
- 工作结束时释放所有 Agent 回到 `IDLE`。

---

## 3. 界面元素

| 元素 | 作用 |
|---|---|
| 输入框 | 输入 Human Demand |
| 提交按钮 | 提交给 Front Agent |
| 输出显示框 | Human / Front / 系统消息 |
| Task 卡片列表 | task_id · 状态 · Leader · 成员 · 迭代 |
| Task 卡片点击 | 在输出框打印：执行历史 / Workspace 文件 / 公共 WM / LLM 调用汇总 |
| Agent 状态 | `IDLE` / `WORKING` · `FRONT` / `LEADER` / `MEMBER` |
| 设置按钮 | 修改 Front Agent / 最大并行 Team |

---

## 4. 后端结构

```text
ui/server.py          标准库 HTTP server + 静态文件
ui/static/            无构建 HTML / CSS / JS
work/controller.py    AIEOSController：Front / Leader / Team 编排
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
- 选择空闲 Leader；
- Leader 用 `build_lead_plan_prompt` 规划并选择成员；
- 有成员时调用 `TaskCreationService` 创建 TEAM Task，
  调用 `TeamWorkRuntime(initial_plan=...)` 执行；
- Leader 决定独立完成时创建 SOLO Task，调用 `SoloWorkRuntime` 执行；
- 把 `WorkResult` 转成 `FrontResult` 显示。

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
- 完整 prompt / response / phase 明细尚未落盘；
- 「Leader 运行中重新计划」沿用 `work_plan_update`；
- 设置中的最大并行 Team 在进程内生效；重启按启动参数。

---

## 6. 持续改造原则

新增机制时：

1. 先判断它属于 Life / Work / Memory / Interaction 哪一层；
2. 优先复用已有服务与数据契约；
3. 不因为换 UI / 换入口而复制一套 Task 或 Work runtime；
4. 把新机制写进对应架构文档与守卫测试。

> **UI 是入口，不是新的执行框架。**
