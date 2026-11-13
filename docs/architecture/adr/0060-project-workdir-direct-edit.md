# ADR-0060：Project Workdir —— 直接修改用户项目目录

- **状态**：Accepted（**Phase 1 已实现；Git worktree / 项目提交为 FUTURE**）
- **时间**：2026-11-13
- **Supersedes**：无
- **Superseded by**：无

## Context

Pi Work 一直固定在：

```text
tasks/<task_id>/workspace
```

这适合生成新产物，但不能直接修改用户已有项目。

OpenCode / coding harness 的通用原理是：

```text
Agent cwd = 用户项目目录
文件工具直接读写真实文件
Git 只负责快照 / 撤销 / 分支
```

AIEOS 需要在不放弃 Task / Snapshot / 锁语义的前提下支持同一工作方式。

## Decision

### 1. Task 增加 project workspace 模式

```text
workspace_mode = project
project_path   = /abs/path/to/project
```

- 空 = 继续 Task Workspace；
- 目录在 Task 创建时固定；
- 只影响新任务；
- 不要求项目是 Git 仓库。

### 2. Pi 直接使用项目目录

```text
cwd = project_path
AIEOS_TASK_WORKSPACE = project_path
```

- 项目元数据写入 `tasks/<task_id>/.aieos`；
- `TASK_DOC.md` / `QUESTION.json` / `RESULT.md` 不写进用户项目；
- 私有 workspace / 记忆目录保持独立。

### 3. Shadow snapshot

```text
GIT_DIR      tasks/<task_id>/.aieos/snapshot.git
GIT_WORK_TREE project_path
```

- 项目里不出现 `.git`；
- 非 Git 项目也有 baseline / final / diff / restore；
- manifest 只记录变更文件。

### 4. 项目级锁

同一项目目录同时只允许一个 Task：

```text
runs/project_locks/<sha256(path)>.lock
fcntl.flock(LOCK_EX | LOCK_NB)
```

锁文件在 AIEOS 运行目录，不写用户项目。

### 5. UI 显式选择

Human 用绝对路径或目录浏览器选择工作目录；
Front / Worker 不能自行选择项目路径。

## Consequences

**正面**：

- Agent 可以修改已有代码库；
- 不需要项目一定是 Git 仓库；
- 元数据不污染项目；
- 非 Git 项目仍有快照 / diff / restore；
- 同一项目串行执行，避免并发覆盖。

**负面 / 代价**：

- Agent 以当前 OS 用户权限直接写项目，没有 OS 沙箱；
- 同一项目暂时不能并行；
- 没有 Git worktree 的隔离和分支；
- 项目模式的自动 commit / merge 仍为 `FUTURE`；
- 非 Pi 引擎暂不支持。

**因此不能做的事**：

- 不能让 Front 自动选项目路径；
- 不能把 AIEOS 元数据写进项目根目录；
- 不能把 project 模式当成 Git worktree 模式；
- 不能在没有项目锁的情况下并行修改同一项目。

## 备选方案

| 方案 | 为什么不选（Phase 1） |
|---|---|
| 复制项目到 Task Workspace | 与用户当前工作区脱节，同步回写复杂 |
| 直接要求 Git worktree | 非 Git 项目无法使用，且 Pi cwd 暴露物理 worktree 路径 |
| 只提交 patch 到项目 | 用户需要先看到修改，patch 流程更重 |

## 证据

- `tests/test_pi_work_runtime.py`：project cwd / 修改 / 元数据隔离 / manifest；
- `tests/test_project_lock.py`：项目互斥；
- `tests/test_aieos_ui.py`：工作目录 API / UI 入口。
