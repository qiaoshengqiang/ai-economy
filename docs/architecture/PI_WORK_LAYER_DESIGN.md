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
    snapshot.git/         ← 任务私有快照仓库（派生）
    snapshot.index
    snapshot-ignore
    snapshots.json
```

- 磁盘是内容权威；
- DB 只保存 Task 元数据和 manifest/hash；
- Pi 执行的 SOLO Task 不引入逻辑锁；Team 共享目录以后再加协调；
- `tasks/` 是运行产物，已加入 `.gitignore`。

## 3. Pi 进程

- 每个 Task 一个 Pi RPC 进程；
- `--provider` / `--model` 来自 Agent Brain（可覆盖）；
- 原生 `read/write/edit/bash`，`--tools read,write,edit,bash,grep,find,ls`；
- `--no-skills`：不加载用户全局 skills（网文/写作等与 Work 无关的技能）；
- `--no-context-files`：不加载项目 `AGENTS.md` / `CLAUDE.md`；
  只保留 Pi 基础提示 + AIEOS Work addendum；
- `--session-dir tasks/<id>/.aieos/pi-session` + `--session-id`；
- `pi-agent/settings.json` 显式启用 Pi 自动压缩：`reserveTokens=16384`、
  `keepRecentTokens=20000`；触发阈值 = `contextWindow - reserveTokens`，
  `contextWindow` 来自 `pi-agent/models.json`；
- DeepSeek V4.1 Flash 的 `context_window` 默认 **600,000**，因此 Pi 压缩阈值
  约 **583k**；`max_output_tokens` 为 32k，是 AIEOS 的单次输出上限，
  不是模型能力上限；
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

### 任务文档与恢复

Workspace 根目录维护 `TASK_DOC.md`：

- 任务目标；
- 当前 iteration / phase / status / thinking；
- 阶段历史；
- 设计意图；
- 工程记录；
- 未解决问题；
- 当前文件。

`work_state.json` 保存 `iteration / phase / phase_status / attempt /
resume_reason / history`。恢复时不再重跑三阶段：

- 中断在哪个阶段，就从该阶段继续；
- 恢复 prompt 注入 `Recovery Brief`：上次阶段、状态、文件、TASK_DOC、
  未解决问题；
- 已完成任务的 rework 按 `rework_scope` 选择起点：
  `fix → direct`、`enhance → engineering`、`redesign → prototype`；
- 中断恢复复用同一个 Pi session；新 iteration 使用新 session
  （`<task_id>.i<N>`）+ 旧 RESULT/ENGINEERING/TASK_DOC 作为交接；
- `work_state.json` 记录 `session_id` / `last_error`；恢复简报注入上次错误，
  避免 Agent 只看到「failed」却不知道失败原因。

### Agent 中途提问

Agent 确实需要人类确认时，在 Task Workspace 写 `QUESTION.json`：

```json
{"question":"...","options":["A","B"],"allow_free_text":true}
```

然后结束本轮。AIEOS：

1. 消费问题并移到 `.aieos/question.json`；
2. `work_state.phase_status = "waiting_human"`；
3. Task 保持 `IN_PROGRESS`，不完成、不失败；
4. UI Agent 卡片显示气泡 + 音效；
5. Human 点击气泡直接回答，不经过 Front；
6. 回答写入 `.aieos/ANSWER.json` 和 `.aieos/questions.json`；
7. 从当前阶段恢复，不重跑已完成的阶段。

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
- Phase prompt 只定义阶段责任；角色要求在对应阶段追加：

```text
Phase 责任
  + Role 的本阶段专业要求
  + 环境事实 / 当前任务
```

- Role 不再是一整段固定 prompt，而是 `phase_requirements`：
  Prototype / Engineering / Evolution 分别提供本阶段的专业要求；
- 角色要求只**追加**，不覆盖 Phase 责任和人类原话；
- Role 注入条件：Work 模式为 `workflow` 且阶段是
  `prototype` / `engineering` / `evolution`；`autonomous` 模式不注入 Role，
  `workflow` 的 `direct` 阶段也不注入；
- 旧 `system_prompt` 作为通用专业要求保留，兼容用户已有角色；
- Pi Task 把角色写入 `task.acceptance["role"]`，resume 时回退读取；
- 全局总结契约固定 `RESULT.md` 的章节，角色只追加专业总结项。
- 系统提供的 Front / Work / Worker 角色 prompt 使用中文；
  JSON 字段、动作名、错误码等协议标识保持英文，避免改变协议。

## 4.6 Web Search（外部事实）

Front 与 Pi 共用同一个只读搜索服务，provider key 只在 AIEOS 主进程：

```text
Front                          Pi
  web_search / web_fetch         bash: aieos_web_search.py
        │                              │
        ▼                              ▼
   FrontActionExecutor          AIEOS 本地端点
        │                       /internal/web-search
        │                       /internal/web-fetch
        └────────────┬─────────────────┘
                     ▼
             WebSearchClient
                     ▼
        DeepSeek /v1/tools/search · /v1/tools/fetch
```

- `llm/web_search.py` 是唯一搜索实现；
- Front 动作：`web_search {query, count?, need_content?, ori_question?}`、
  `web_fetch {url}`；结果只作为本轮证据，不进入 Task Core / Shared Context；
- Pi 不获得 DeepSeek key。UI 启动时生成任务级
  `AIEOS_WEB_SEARCH_TOKEN`，Pi 通过 `scripts/aieos_web_search.py` 调用
  `127.0.0.1` 内部端点；`AIEOS_AGENT_ID` 决定使用哪个 Agent 的 credential；
- Pi prompt 同时提供 `search` 与 `fetch` 两种 CLI 用法：
  `search` 用于发现信息，`fetch` 用于已确定 URL 的单个公开网页；
- `web_fetch` 只允许公开 `http/https` URL，拒绝 localhost / 私有 IP /
  带用户名密码的地址；URL 长度上限 8192；
- Search / Fetch 结果按条数和字符数截断；搜索失败按 action error 回喂，
  不中断 Task。

## 4.7 自主模式（Autonomous）

常规设置里新增 `Work 模式`：

```text
workflow   当前阶段化模式（Prototype → Engineering → Evolution）
autonomous 单阶段自主模式
```

- 模式写入 Task 的 `acceptance.work_mode`，只影响新任务；旧任务默认 `workflow`；
- 自主模式只有一个 phase：`autonomous`；
- 不给 Agent 注入 Prototype / Engineering / Evolution 的阶段责任、
  Worker Role 的 phase requirements、验收提示、范围守卫或验证次数要求；
- 用户 prompt 就是 Task 的 `task_text`（有中途人类回答时追加该回答）；
- 仍保留最小系统提示：身份、Task Workspace / 私有 workspace / 记忆目录、
  私有 workspace 的用途（草稿 / 笔记 / 中间产物 / 可复用脚本）、
  原生工具列表、操作范围，以及可用的本地 web search CLI；
- 思考强度使用 Agent 卡片；`Auto` 在自主模式取 `high`；
- 中断恢复复用同一个 autonomous session；返工按新 iteration 开新 session；
- 完成判定仍是 Pi `agent_settled` + Workspace 产物 + 最后 assistant 文本 /
  `RESULT.md` 作为总结。

自主模式不引入新的调度器、预算 gate 或验收器；它只是不再给 Agent
注入工作流阶段提示。

## 4.8 Task Snapshot（任务私有 Git 历史）

Pi 仍然直接写 `workspace/`；AIEOS 在旁边维护一个**任务私有**的 Git 快照仓库：

```text
GIT_DIR      tasks/<task_id>/.aieos/snapshot.git
GIT_WORK_TREE tasks/<task_id>/workspace
```

- 不在 `workspace/` 里创建 `.git`；
- 不提交 AIEOS 主仓库，也不自动 merge 到主分支；
- 每个快照是 shadow repo 里的一个 commit，记录
  `tree / parent / label / phase / turn / files / bytes / skipped`；
- 抓取时机：`baseline`、`phase:<name>:end`、`interrupted`、
  `waiting_human`、`final`；
- `manifest.json` 增加 `snapshot` 字段（`seq / commit / tree / label`）；
- 默认忽略 `.aieos/`、`.git/`、`node_modules/`、`.venv/`、
  `__pycache__/`、`.env`、`*.key`、`*.pem` 等；
- 未跟踪文件超过 2MB 默认跳过；
- 快照失败 fail-open：写 `.aieos/snapshot-error.log`，不阻断 Task。

> 🔒 **Snapshot ≠ Task Workspace State**
> 快照是 workspace 的派生物，只用于历史 / diff / restore；
> 验收仍然看 workspace + manifest，不把快照当第二个状态源。

恢复能力按 Human 显式操作设计（UI 后续接入），恢复前会自动抓
`before-restore`，因此恢复本身可撤销。Front 暂不开放快照恢复。

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
pi_work_agents     # 只有这些 Agent 的 Work 走 Pi 引擎
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

未开启的 Agent 继续走 `BuiltinWorkRuntime`，可回滚。

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
