# Pi Work Layer Design

> 状态：**V1 pilot 已实现**（PiWorkRuntime · 真实任务目录 · 记忆文件桥 · LLM Proxy · Pi 总结回传 · Worker Role）
> 目标：Pi 只做 Work 执行底座；Life 层、Task Core、经济与治理仍属于 AIEOS。

## 1. 边界

```text
Life 层（AIEOS）
  Front / Human 对话 · 私有 workspace / drafts · artifact_read
  Task Core · Ledger · 验收 · 治理
        │
        │ 创建 Task + 真实任务目录
        ▼
Work 层（Pi）
  Pi RPC · 原生 read/write/edit/bash
  cwd = tasks/<task_id>/workspace
  生产内容直接落盘
        │
        ▼
AIEOS
  manifest · usage · memory ingest · Task COMPLETED
```

Pi Work 路径**不使用**：

- Task Shared Context / Alignment；
- AIEOS ActionDispatcher；
- `work_complete` / `workspace_write` 等 Work 动作；
- 逻辑 Workspace 的锁 / revision。

Task Core 仍是状态、预算、contractor、iteration 的唯一权威。

## 2. 真实任务目录

```text
tasks/<task_id>/
  workspace/              ← Pi cwd；交付物
  .aieos/
    TASK.md
    manifest.json
    pi-session/           ← Pi session，不进交付物
    pi-stderr.log
```

- 磁盘是内容权威；
- DB 只保存 Task 元数据和 manifest/hash；
- Solo Pi 任务不引入逻辑锁；Team 共享目录以后再加协调；
- `tasks/` 是运行产物，已加入 `.gitignore`。

## 3. Pi 进程

- 每个 Task 一个 Pi RPC 进程；
- `--provider` / `--model` 来自 Agent Brain（可覆盖）；
- 原生 `read/write/edit/bash`，`--tools read,write,edit,bash,grep,find,ls`；
- `--no-skills`：不加载用户全局 skills（网文/写作等与 Work 无关的技能）；
- `--no-context-files`：不加载项目 `AGENTS.md` / `CLAUDE.md`；
  只保留 Pi 基础提示 + AIEOS Work addendum；
- `--session-dir tasks/<id>/.aieos/pi-session` + `--session-id`；
- Agent credential 只通过子进程环境变量传入，不写日志；
- `agent_settled` 视为本次 Pi run 结束；
- `abort` / 进程终止用于取消。

## 4. 记忆协议

Pi 不直接访问结构化记忆 DB；使用文件协议：

```text
agents/<agent_id>/memory/pi/
  context.md      AIEOS 生成，只读快照
  working.md      Pi 可读写，任务级工作记忆
  inbox/*.md      Pi 写入候选记忆
  README.md
```

- 开始时 AIEOS 渲染 Working / Short-term / Long-term 到 `context.md`；
- 结束时 AIEOS 读取 `working.md` + `inbox/*.md`；
- 工作记忆写入 `pi_work` 节点；
- 短期记忆通过 `add_from_final` 摄取；
- 短期记忆超过保留阈值时，自动调用 `consolidate` 合并为 Long-term；
- inbox 不直接成为权威；AIEOS 决定如何合并；
- Pi session 不是长期记忆，只是任务执行记录。

## 4.4 Work Phases（Prototype → Engineering → Evolution）

Pi Work 不再是一段无阶段连续执行，而是三段式：

```text
Prototype 原型生成
  → Engineering 实现验证
  → Evolution 持续迭代
  → COMPLETED
```

- 三段复用同一个 Pi session，阶段通过 user prompt 切换；
- 阶段状态写入 `.aieos/work_state.json`（含 history）；
- Phase 1 成功条件：Workspace 有非 `PROTOTYPE.md` / `RESULT.md` 的产物；
- Phase 1 结束前确保存在 `PROTOTYPE.md`；模型没写时，从最后输出提取；
- Phase 2 结束前确保存在 `ENGINEERING.md`；
- Phase 3 产出 `RESULT.md`；
- 阶段记录写入 round log 的 `phase` 字段。

环境事实分层：

| 阶段 | 环境上下文 |
|---|---|
| Prototype | 最小事实：路径 / 文件树 / 工具 / 当前为空 |
| Engineering | 文件树 + `PROTOTYPE.md` + 运行验证要求 |
| Evolution | 文件树 + `ENGINEERING.md` + 未解决问题 |

角色 prompt 只在 Engineering / Evolution 注入；Prototype 不给完整工程规范。

思考强度：

- Prototype 固定 `max`（模型不支持时取最近支持档）；
- Engineering / Evolution 使用 Agent 卡片设置；
- `Auto` → Engineering `high`、Evolution `medium`（不支持时取最近档）；
- 输出上限统一取模型 `max_output_tokens`。

## 4.5 Worker Role（任务级专业角色）

Role 是**任务属性**，不是 Agent 身份、权限、能力或预算：

```text
Role       这次任务用什么专业视角工作
Agent      谁来做
Permission 能访问什么
Budget     花多少
```

- Front 的 system prompt 注入角色目录（id / name / 一句话描述）；
- Front handoff 输出 `role`；未填 → `generalist`；未知角色 → 失败；
- 角色注册表在 `work/worker_roles.py`，V1 有 7 个：
  `generalist` · `frontend_engineer` · `game_developer` ·
  `backend_engineer` · `researcher` · `writer` · `qa_verifier`；
- Worker 的 system prompt 由三部分拼成：

```text
全局 Work 契约（工具 / 落盘 / RESULT.md）
  + 角色 prompt（专业视角 + 额外总结项）
  + 具体 Task
```

- 角色 prompt 只**追加**，不覆盖全局规则；
- Pi Task 把角色写入 `task.acceptance["role"]`，resume 时回退读取；
- 全局总结契约固定 `RESULT.md` 的章节，角色只追加专业总结项。
- 系统提供的 Front / Work / Worker 角色 prompt 使用中文；
  JSON 字段、动作名、错误码等协议标识保持英文，避免改变协议。

## 5. 完成与 usage

当前 pilot：

```text
Pi agent_settled
  → manifest / RESULT.md
  → memory ingest
  → Pi usage 回写 llm_calls
  → WorkLifecycle.complete_solo
```

- `RESULT.md` 作为人类摘要；没有时用 Pi 最后一条 assistant 文本；
- Pi 的 usage 事件转换为 `LLMResponse`，写入 `llm_calls` 与 Ledger；
- **LLM Proxy 已接入**：Pi 指向本地 AIEOS Proxy；**不做预算 gate**，
  只解析 usage 写入 `llm_calls` / Ledger；Work 只负责生产；
- **不再做 AIEOS 独立验收**：Pi 结束后以 `RESULT.md` / 最后一条
  assistant 文本作为总结；AIEOS 写入 `.aieos/result.json` 并完成 Task；
- Team 共享目录、多 Agent 文件锁不在 V1 pilot。

## 6. 开关

`AIEOSController` 新增：

```text
pi_work_agents     # 只有这些 Agent 的 Solo Task 走 Pi
pi_cli_path
pi_provider
pi_model
pi_agent_dir
pi_timeout_seconds
task_root
```

UI 对应参数：

```bash
python -m ui.server \
  --pi-agents agent_c \
  --pi-cli /path/to/pi \
  --pi-agent-dir /path/to/pi-agent \
  --task-root /path/to/tasks
```

未开启的 Agent 继续走 `SoloWorkRuntime`，可回滚。

## 7. 已验证

Proxy pilot（agent_c）：

```text
Task task_9e46a230
  → Pi 4 turns / 3 tools
  → LLM Proxy 记录 agent_c 4 calls / 65,968 tokens / 0.65968 AIC
  → tasks/task_9e46a230/workspace/{hello.txt,RESULT.md}
  → .aieos/{manifest.json,pi-session}
  → Task COMPLETED
```

更早的 pilot（无 proxy）：

```text
Task task_2cb3eac3
  → Pi 4 turns / 3 tools
  → memory/pi/{context.md,working.md,README.md}
  → llm_calls 4 条 / 65,627 tokens
  → Task COMPLETED
```

> 已知问题：`task_1a1f8608` 的真实赛车任务被旧 `PiAcceptance`
> 的 8,000 字符截断误判为失败；该 acceptance gate 已移除。

## 8. 后续

1. Pi 事件实时映射到 UI（当前为 run 结束后批量写 round）；
2. Team 共享任务目录与文件锁；
3. 多轮/多任务对比，确认是否扩大 `pi_work_agents`；
4. 总结质量校验（RESULT.md 缺失/为空时的行为）；
5. Proxy 对流式响应的兼容性继续压测。
