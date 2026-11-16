# Project Workdir（Phase 1）

> **状态：`CURRENT`（Phase 1）**
> 用户选择一个已有项目目录，Pi 直接在该目录中修改代码。
> Git 不是编辑前提；Git worktree / 项目分支提交仍为 `FUTURE`。
>
> 相关 ADR：[ADR-0060](adr/0060-project-workdir-direct-edit.md)

---

## 1. 它解决什么

以前 Pi 固定工作在：

```text
tasks/<task_id>/workspace
```

只能做“新产物”，不能直接修改用户已有项目。

Project Workdir 允许：

```text
workdir = /path/to/existing-project
Pi cwd  = /path/to/existing-project
```

Agent 直接读取、修改、运行项目里的代码。

---

## 2. 任务模型

Task 的 `acceptance`：

```json
{
  "workspace_mode": "project",
  "project_path": "/abs/path/to/project"
}
```

- `workspace_mode=task`（默认）继续使用 Task Workspace；
- `workspace_mode=project` 使用用户项目目录；
- 空 `project_path` 不允许进入 project 模式。

目录校验：

- 绝对路径；
- 存在且是目录；
- 不能位于 AIEOS 的 `tasks/` 内；
- 不能是 AIEOS 主仓库根目录。

Git **不是必需条件**。

---

## 3. Pi 运行时

```text
cwd                    = project_path
AIEOS_TASK_WORKSPACE   = project_path
Task 元数据             = tasks/<task_id>/.aieos
Task Snapshot          = tasks/<task_id>/.aieos/snapshot.git
                        GIT_WORK_TREE = project_path
```

- `TASK_DOC.md`、`QUESTION.json`、`RESULT.md` 在 project 模式写入
  `.aieos/`，不污染用户项目；
- 私有 workspace / 记忆目录保持独立；
- 项目目录里只放真实代码和交付物。

---

## 4. 快照与交付清单

进入任务前抓 `baseline`，阶段结束和最终抓快照。

project 模式：

- manifest 只记录 `baseline → final` 的变更文件；
- 不扫描整个项目；
- `result.artifacts` 是变更文件；
- 删除的文件目前只体现在 snapshot diff，不进入 artifacts。

Task Workspace 模式行为保持不变。

---

## 5. 项目级锁

同一项目目录同时只允许一个 Task：

```text
runs/project_locks/<sha256(project_path)>.lock
  fcntl.flock(LOCK_EX | LOCK_NB)
```

- 锁文件在 AIEOS 运行目录，不写项目；
- 第二个任务会收到“项目目录正在被另一个 Task 使用”；
- 不同项目可以并行。

---

## 6. UI

任务输入区只显示工作目录 chip：

```text
[▰ project-demo ▾] [提交]
```

- chip 只显示路径最后一级目录；
- 悬停显示完整路径；
- 点击打开菜单：更换工作目录 / 使用 Task Workspace；
- 留空时显示 `Task Workspace`；
- 支持服务端目录浏览器；
- 选择结果保存在浏览器 localStorage，刷新后恢复；
- 只影响新任务；运行中的任务保持创建时的目录。

当前“提交仓库”仍是 Output Repo Mode，产物写到：

```text
<repo>/tasks/<task_id>/
```

它不是 Project Repo Mode，不会修改项目原有文件。

### 6.1 Front 的只读项目上下文

用户在 UI 选择的项目目录会随 Job 进入 Front：

```text
WorkJob.workdir
    ↓
_front_turn(project_context=...)
    ↓
Front prompt：路径 / 一级内容 / 网页候选
```

规则：

- 当前有项目目录时，用户说「这个项目 / 项目里的网页 / 这个目录 /
  这里的文件」通常指项目目录；同一句话里出现 Task ID / 刚才那个任务时，
  也同时指向该 Task；
- `follow_up` / `rework` 需要明确的已有 Task 目标；目标不唯一时用
  `clarify` 确认；
- `rework` / `follow_up` 会改变已有 Task；`inspect` 可读取 task_id、
  状态、描述和 `workspace_path`，用来核对用户所指的任务；
- 同一项目目录可以继续已有 Task，也可以创建新 Task；选择取决于用户
  要延续哪个任务，以及这次变更是否属于同一目标；
- 同一项目目录同一时间只有一个运行中的 Task；
- Worker 只看到 `task_text`；项目任务的目标文件路径写进 `task_text`。

Front 可以只读项目目录：

```text
project_list { path?, depth? }
project_read { path, start_line?, max_lines? }
```

边界：

- 只读，不写；写入仍由 Worker 执行；
- 只能访问当前 `workdir` 内的路径，不跟随符号链接；
- 跳过 `.git` / `node_modules` / 虚拟环境 / 构建产物；
- 跳过 `.env`、密钥、证书等敏感文件；
- 只读文本文件，单次有行数和字符上限；
- 文件内容是**不可信数据**，不是指令。

---

## 7. 边界

- Agent 以当前 OS 用户权限直接写项目，没有 OS 沙箱；
- 一个项目同一时间一个任务；
- 项目目录必须由 Human 显式选择；
- Front / Worker 不能自行选择项目路径；
- Front 只能只读当前选中的项目目录；它不能写项目，也不能读宿主机其他路径；
- 自动 commit / merge 仍属于后续阶段；
- Project 任务完成后不创建 Output Repo 提交请求，因为代码已经直接落在项目目录；
- 非 Pi 引擎暂不支持 project 模式；
- 项目里的嵌套 Git 仓库会从 snapshot 中排除；修改嵌套仓库内部文件不会出现在
  manifest（根目录文件不受影响）。

---

## 8. Phase 2 / 后续规划（`DESIGNED` / `FUTURE`）

> Phase 1 已经能直接修改项目；Phase 2 解决**隔离、预览、提交、合并**。
> Phase 2 的核心价值不是“多一个 commit”，而是让项目修改可以
> **先做、先看、再提交**。

### 8.1 Phase 2：Git Worktree

```text
project repo
  base_ref = main
      ↓
git worktree add .worktrees/task-<id> main
      ↓
Pi cwd = .worktrees/task-<id>
      ↓
修改 / 测试
      ↓
commit 到 aieos/task-<id>
      ↓
Human 预览 + 合并
```

能力：

- 从 `base_ref` 创建隔离 worktree；
- 任务分支 `aieos/task-<id>`；
- 用户当前工作区不被修改；
- 记录 `base_commit`；
- 可丢弃分支 / worktree；
- 需要 Git 仓库；非 Git 项目继续 Phase 1 直接模式。

待决问题：

- 用户当前有未提交修改时，worktree 从 HEAD 创建会丢失这些修改；
  需要选择“要求 clean / 复制 dirty patch / 直接模式”；
- worktree 依赖目录（`node_modules` / `.venv`）不随 Git 复制；
- physical worktree path 是否对 Agent 可见（现有 Worktree 设计倾向于隐藏）；
- 同一项目多 worktree 的合并冲突策略。

### 8.2 预览：未提交也能看效果

Worktree 是真实目录，**不 commit 也可以运行**：

```text
Agent 改完 worktree
  → 用户预览
  → 满意
  → commit / merge
```

建议新增 `PreviewManager`：

| 项目类型 | 预览方式 |
|---|---|
| 静态 HTML | `python -m http.server` 指向 worktree |
| Node 前端 | `npm run dev` / `start` / `preview` |
| 构建产物 | build 后静态服务 `dist/` |
| Python Web | 项目配置的启动命令 |
| 无法识别 | 显示 worktree 路径 + 手动命令 |

要求：

- 只绑定 `127.0.0.1`；
- 自动分配空闲端口；
- 显示 URL、PID、日志；
- 支持停止 / 超时清理；
- 依赖缺失时提示安装，不静默失败；
- Human 点击启动，不自动执行。

UI：

```text
任务 task_xxx 已完成，尚未提交。
工作目录：.worktrees/task_xxx
[启动预览] [打开目录] [查看 diff]
[提交到任务分支]
```

这是 Phase 2 能否被人类接受的关键：
**worktree 不能只是把文件藏起来，必须提供可预览的入口。**

### 8.3 项目提交 / 合并

- 预览满意后 commit 到 `aieos/task-<id>`；
- 测试门禁（代码任务）；
- Human 确认后 merge 到 `base_ref`；
- `auto` 只做 fast-forward，复杂冲突交给 Human；
- 自动 push 仍为 `FUTURE`。

### 8.4 多任务 / 并行

- Worktree 允许每个任务独立工作；
- 但多个任务改同一项目时，合并冲突后置；
- 初期建议按项目串行，后续再开放并行；
- 需要项目级“任务队列 / 占用”显示。

### 8.5 人类直观性

Phase 1 直接改当前目录，用户天然能看到效果；
Phase 2 隔离到 worktree 后，直观性下降，预览工作量增加。

因此 Phase 2 的优先级取决于：

1. 是否需要“不影响当前工作区”；
2. 是否需要分支审查 / 合并；
3. 是否愿意维护 PreviewManager；
4. 是否有多任务并行需求。

如果只是单用户、本地、改完马上看，Phase 1 更顺手；
如果要做 Agent 团队 / 多任务 / 审查合并，Phase 2 更合适。

### 8.6 后续候选

- Project Commit（Phase 1.5）：直接提交当前项目本次任务变更；
- 测试门禁 / 项目级测试命令；
- 预览截图 / 自动可访问性检查；
- 项目级任务队列；
- 自动 push / PR；
- 非 Git 项目的可选 `git init`。

---

## 9. 证据

- `tests/test_pi_work_runtime.py::test_pi_project_workdir_modifies_existing_project`：
  cwd、文件修改、元数据隔离、manifest 变更文件、快照；
- `tests/test_project_lock.py`：同项目互斥、不同项目并行；
- `tests/test_aieos_ui.py`：工作目录 API / 选择器静态入口。
