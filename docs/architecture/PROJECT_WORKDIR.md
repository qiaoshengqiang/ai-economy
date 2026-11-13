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

聊天输入区上方新增：

```text
工作目录  [/abs/path]  [选择目录]  状态
提交仓库  [/abs/path]  [选择目录] [默认]
```

- 工作目录留空 = Task Workspace；
- 支持绝对路径输入；
- 支持服务端目录浏览器；
- 选择结果保存在浏览器 localStorage，刷新后恢复；
- 只影响新任务；运行中的任务保持创建时的目录。

当前“提交仓库”仍是 Output Repo Mode，产物写到：

```text
<repo>/tasks/<task_id>/
```

它不是 Project Repo Mode，不会修改项目原有文件。

---

## 7. 边界

- Agent 以当前 OS 用户权限直接写项目，没有 OS 沙箱；
- 一个项目同一时间一个任务；
- 项目目录必须由 Human 显式选择；
- Front / Worker 不能自行选择项目路径；
- 自动 commit / merge 仍属于后续阶段；
- 非 Pi 引擎暂不支持 project 模式。

---

## 8. 未来

- Git worktree：从项目 base_ref 创建隔离 worktree；
- 项目分支提交与 Human merge；
- 测试门禁；
- 自动 commit / push；
- 多任务并行与冲突处理。

---

## 9. 证据

- `tests/test_pi_work_runtime.py::test_pi_project_workdir_modifies_existing_project`：
  cwd、文件修改、元数据隔离、manifest 变更文件、快照；
- `tests/test_project_lock.py`：同项目互斥、不同项目并行；
- `tests/test_aieos_ui.py`：工作目录 API / 选择器静态入口。
