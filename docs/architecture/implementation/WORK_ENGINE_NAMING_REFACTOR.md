# Work 引擎语义改造 —— Solo ≠ 内置引擎

> **状态：`CURRENT`（2026-11-16）**
>
> 目标：把「任务拓扑」和「执行引擎」拆成两个独立概念。
> `Solo` 只表示**一个 Agent 独立完成**，不再表示 AIEOS 自己搭的执行循环。
>
> 相关文档：
> [`LIFE_WORK_ARCHITECTURE.md`](../LIFE_WORK_ARCHITECTURE.md) ·
> [`PI_WORK_LAYER_DESIGN.md`](../PI_WORK_LAYER_DESIGN.md) ·
> [`AIEOS_UI.md`](../AIEOS_UI.md) ·
> [`UI_SETTINGS_DESIGN.md`](../UI_SETTINGS_DESIGN.md) ·
> ADR-0055 · ADR-0056

---

## 0. 一句话

> **Solo 是「谁来完成」，不是「用什么来完成」。**

```text
任务拓扑：SOLO / TEAM          ← 一个 Agent 独立完成，还是多个 Agent 协作
执行引擎：BUILTIN / PI         ← AIEOS 内置循环，还是 Pi 底座
```

改造前，`PiWorkRuntime` 和 `SoloWorkRuntime` **都只接受 `TaskMode.SOLO`**。
因此「Solo」不能用来命名其中一种执行引擎。
改造后，内置引擎为 `BuiltinWorkRuntime`，Pi 引擎为 `PiWorkRuntime`。

---

## 1. 问题

改造前，`Solo` 同时承担三个含义：

1. **任务拓扑**：`TaskMode.SOLO` —— 一个 Agent 独立完成；
2. **执行引擎**：`SoloWorkRuntime` / `engine="solo"` —— AIEOS 自己的动作循环；
3. **角色**：`WorkRole.SOLO` / “Solo worker” —— 单 Agent 任务里的 worker。

由此产生的直接矛盾：

| 现象 | 问题 |
|---|---|
| Agent 卡片显示 `SOLO` / `PI` | Pi 引擎 Agent 执行的也是 SOLO Task，`SOLO` 不是引擎 |
| “单任务最大轮次” | 只对内置引擎生效，Pi 任务完全不看这个值 |
| “Work 模式 = 自主 / 工作流” | 只对 Pi 生效；内置引擎没有 autonomous |
| `Pi Agent` | 把执行引擎当成了 Agent 身份 |
| `Team Lead（= Solo Agent）` | 当前 UI 没有 Team Lead，是旧 Team 路径残留 |

---

## 2. 术语

| 概念 | 取值 | 说明 |
|---|---|---|
| **任务拓扑** | `SOLO` / `TEAM` | 单 Agent 独立完成 / 多 Agent 协作 |
| **执行引擎** | `BUILTIN` / `PI` | AIEOS 内置循环 / Pi Work |
| **Pi 执行方式** | `workflow` / `autonomous` | **仅 Pi 引擎** |
| **Front 方式** | `autonomous` / `relay` | Front 自己的行为 |
| **Worker 角色** | `LEAD` / `MEMBER` / `QA` / `SOLO` | Work 层角色；`SOLO` 是单 Agent 任务的 worker |

矩阵：

```text
                执行引擎
任务拓扑     BUILTIN              PI
SOLO         BuiltinWorkRuntime   PiWorkRuntime
TEAM         TeamWorkRuntime（已搁置，仅 TEAM）
```

---

## 3. 改造范围

### Phase 1：用户可见 / API / prompt

#### 3.1 Agent 引擎值

| 位置 | 当前 | 改为 |
|---|---|---|
| `work/controller.py:954` | `"engine": ... else "solo"` | `"builtin"` |
| `ui/static/settings.js:705` | `... ? "pi" : "solo"` | `"builtin"` |
| `ui/static/settings.js:1182` | 默认 `"solo"` | `"builtin"` |
| `ui/static/settings.js:790` | `Pi` / `Solo` | `Pi` / `内置` |
| `ui/static/app.js:887` | 徽标 `SOLO` | `BUILTIN` |
| `ui/static/index.html:960` | radio `value="solo"` / `Solo` | `value="builtin"` / `内置` |

#### 3.2 文案

| 位置 | 当前 | 改为 |
|---|---|---|
| `ui/static/index.html:880` | 默认引擎为 Solo | 默认引擎为内置 |
| `ui/static/index.html:956` | Solo 走内置循环；Pi 走 Pi Work | 内置引擎使用 AIEOS 动作循环；Pi 引擎使用 Pi Work |
| `ui/static/index.html:796` | Front / Solo / Pi | Front / 内置 / Pi |
| `ui/static/index.html:611` | 单任务最大轮次 | 内置引擎最大轮次 |
| `ui/static/settings.js:517` | 摘要“单任务” | “内置引擎” |

#### 3.3 `Work 模式` → `Pi 工作模式`

`work_mode` 只影响 Pi Work；内置引擎不读取它。

| 位置 | 当前 | 改为 |
|---|---|---|
| `ui/static/index.html` | Work 模式 | Pi 工作模式（仅 Pi 引擎生效） |
| `ui/static/settings.js:862` | Work 模式 | Pi 工作模式 |
| `front/runtime.py:660-664` | Work 模式 = … | Pi 工作模式 = …；仅 Pi 引擎 |
| `front/runtime.py:727-729` | `work_mode` | 明确“只影响之后创建的 Pi Task” |
| `work/controller.py:2062` | 当前 Work 模式 | 当前 Pi 工作模式（仅 Pi 引擎生效） |

### Phase 2：代码符号 / 设置键

#### 3.4 `SoloWorkRuntime` → `BuiltinWorkRuntime`

约 53 处引用：

```text
work/runtime.py        类名 / docstring / 错误信息
work/__init__.py       import / __all__
work/controller.py     import / 3 处构造 / docstring
work/cli.py            import / 局部变量 solo
tests/test_work_layer.py
tests/test_aieos_ui.py
tests/test_work_resume.py
tests/test_communication_inbox.py
docs/...
```

保留：

```text
TaskMode.SOLO
complete_solo
_create_solo_task        ← 拓扑分发器
_run_handoff_solo        ← 拓扑路径
```

只把非 Pi 分支拆成：

```text
_create_builtin_task
```

#### 3.5 `max_solo_turns` → `max_builtin_turns`

约 30 处引用：

```text
work/controller.py      init / load / update / settings_dict / 3 处构造
ui/server.py            argparse / settings POST / controller 参数
ui/static/settings.js   state / save / render / reset
ui/static/index.html    input id
docs / tests
```

兼容：

- `ui_settings.json` 读取新键，缺失时回退旧键 `max_solo_turns`；
- CLI 保留 `--solo-turns` 作为旧别名，新增 `--builtin-turns`；
- 保存时只写新键。

#### 3.6 局部命名

| 位置 | 当前 | 改为 |
|---|---|---|
| `work/controller.py:2` | Human → Front → Solo Agent | Human → Front → Worker Agent |
| `work/runtime.py:10` | Team Lead（= Solo Agent） | Front 选中的 Worker Agent |
| `ui/server.py:11` | Front / Solo Agent | Front / Worker Agent |
| `ui/server.py:466` | 这些 Agent 的 Solo Task 使用 Pi Work | 这些 Agent 的 Work 使用 Pi 引擎 |
| `work/cli.py` | `solo = SoloWorkRuntime(...)` | `builtin = BuiltinWorkRuntime(...)` |
| `work/__init__.py` | WorkTeamRuntime（Solo 先实现） | 当前 Work 入口说明 |

### Phase 3：文档 / 注释

| 文件 | 需要改 |
|---|---|
| `docs/PROJECT_STATUS.md` | “非 Pi Solo” → “内置引擎（非 Pi）”；“Solo 默认最多 24” → “内置引擎默认最多 24”；“Solo 的任务认知” → “内置引擎的任务认知” |
| `docs/architecture/AIEOS_UI.md` | `SoloWorkRuntime` / `--solo-turns` / “Solo 的任务认知” / “Solo 默认最多 24” |
| `docs/architecture/UI_SETTINGS_DESIGN.md` | “引擎 Solo / Pi” → “引擎 内置 / Pi”；“Work 模式” → “Pi 工作模式” |
| `docs/architecture/LIFE_WORK_ARCHITECTURE.md` | `SoloWorkRuntime` → `BuiltinWorkRuntime`；保留 “Solo Task 完成语义” |
| `docs/architecture/PI_WORK_LAYER_DESIGN.md` | “Solo Pi 任务” → “Pi 执行的 SOLO Task”；`SoloWorkRuntime` |
| `docs/architecture/PROMPT_SPECIFICATION.md` | “Front / Solo / Pi” → “Front / 内置 / Pi” |
| `docs/architecture/PROMPT_WRITING_GUIDELINES.md` | 同上 |
| `docs/architecture/implementation/AGENT_WAKEUP_V1_DESIGN.md` | “Pi / Solo Worker” / `SoloWakeupHandler` → `BuiltinWakeupHandler` |
| `docs/architecture/implementation/AGENT_INBOX_V1_DESIGN.md` | “Solo 只入队” / `SoloInboxHandler` → `BuiltinInboxHandler` |
| `resources/public/tools/README.md` | “Work / Solo 模式” → “Work / 内置引擎” |
| `llm/prompt_log.py` | “Front / Solo / Pi” → “Front / 内置 / Pi” |
| `work/task_directory.py` | “Solo Pi 任务” → “Pi 执行的 SOLO Task” |
| `README.md` / `docs/PROJECT_STATUS.md` | “Pi Agent” → “Pi 引擎 Agent” / “使用 Pi 引擎的 Agent” |
| `ui/static/memory-preview.html` | role “Solo” → “Worker” |
| `ui/static/layout_preview.html` | “Solo Agent” → “Worker Agent” |

---

## 4. 不改变

| 保留 | 原因 |
|---|---|
| `TaskMode.SOLO` / `TEAM` | 任务拓扑权威 |
| `tasks.mode` 的 `SOLO` / `TEAM` | 已持久化，协议兼容 |
| `WorkLifecycle.complete_solo` | Solo Task 的完成语义 |
| ADR-0056 “Solo 完成语义” | 正确使用 Solo |
| `LIFE_WORK_ARCHITECTURE.md` §6 | 同上 |
| `solo_only` | 只允许单 Agent Task，拓扑概念 |
| `_create_solo_task` / `_run_handoff_solo` | 拓扑路径 |
| `WorkRole.SOLO` | 单 Agent 任务的 worker 角色 |
| `protocol_version` | 不受影响 |

**禁止**全局替换 `solo`：会误伤 `TaskMode.SOLO`、DB 值、`complete_solo` 和历史任务语义。

---

## 5. 兼容与迁移

| 项 | 迁移方式 |
|---|---|
| `TaskMode.SOLO` | 不变 |
| `engine` API 值 | `"solo"` → `"builtin"`，仅 UI/API；不改 DB |
| `max_solo_turns` | 读新键，缺失回退旧键；保存写新键 |
| `--solo-turns` | 保留为 deprecated alias，新增 `--builtin-turns` |
| `pi_work_agents` | 持久化键不变；语义是“使用 Pi 引擎的 Agent 列表” |
| `work_mode` | 持久化键不变；只把 UI / prompt 标成 “Pi 工作模式” |
| `WorkRole.SOLO` | 本次保留；若后续要统一，可改 `WORKER` |

---

## 6. 验收

```text
[x] /api/state 中 engine 只有 builtin / pi
[x] UI 引擎标签为 内置 / Pi
[x] “内置引擎最大轮次”只影响内置引擎
[x] “Pi 工作模式”只影响 Pi
[x] SoloWorkRuntime 不再存在
[x] max_solo_turns 只作为读取兼容 / CLI alias
[x] TaskMode.SOLO / complete_solo / DB 值未变
[x] tests/test_work_layer.py / test_aieos_ui.py / test_settings_controller.py 通过
[x] ./dev.sh test 全绿
```

---

## 7. 执行记录

| 阶段 | 状态 | 说明 |
|---|---|---|
| Phase 1：用户可见 / API / prompt | `DONE` | engine=builtin；UI 内置 / Pi；Pi 工作模式；内置引擎最大轮次 |
| Phase 2：代码符号 / 设置键 | `DONE` | BuiltinWorkRuntime；max_builtin_turns + 旧键 / CLI 兼容 |
| Phase 3：文档 / 注释 | `DONE` | 文档、prompt、预览页、注释统一 |
| 全量测试 + 基线 | `DONE` | 5848 passed, 2 skipped, 30 deselected |
