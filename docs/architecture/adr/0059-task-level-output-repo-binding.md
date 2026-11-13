# ADR-0059：Task 级输出仓库绑定

- **状态**：Accepted（**已实现**）
- **时间**：2026-11-13
- **Supersedes**：无
- **Superseded by**：无

## Context

ADR-0058 的输出仓库是全局设置：

```text
commit_output_repo = <one path>
```

这适合「一个默认输出仓库」，但不适合：

- 不同任务属于不同项目；
- Human 想临时把某个任务提交到另一个目录；
- 任务完成前切换全局设置，导致历史任务的目标不清楚。

同时，必须区分：

```text
Output Repo Mode   提交到 tasks/<task_id> 子树（当前）
Project Repo Mode  直接修改项目文件（FUTURE）
```

## Decision

### 1. Task 创建时绑定目标仓库

Task 的 `acceptance` 记录：

```json
{
  "commit_mode": "manual",
  "commit_output_repo": "/abs/path/to/repo",
  "commit_output_repo_mode": "output"
}
```

- 空 `commit_output_repo` = 使用全局默认；
- 非空 = 本 Task 的显式目标；
- 目标在 Task 创建时固定，运行中切换设置不影响它。

### 2. 显式目标必须是现有 Git 仓库

- 绝对路径；
- 目录存在；
- 是 Git worktree 或 bare repo；
- 不能是 AIEOS 主仓库；
- 不自动把普通目录初始化为项目仓库。

默认输出仓库仍可由 CommitService 创建/初始化。

### 3. CommitRequest 保存绝对路径与 repo_mode

```text
output_repo = /abs/path/to/repo
repo_mode   = output | project
```

`repo_mode=project` 时，提交前再次校验目标仍是 Git 仓库；
`repo_mode=output` 时，允许初始化空输出仓库。

### 4. UI 显示与选择

- 聊天框下方显示默认仓库的绝对路径；
- 支持输入绝对路径；
- 支持服务端目录浏览；
- 选择只影响新任务；
- 提交结果消息显示：仓库 / 分支 / commit。

## Consequences

**正面**：

- 不同任务可以提交到不同输出仓库；
- 任务的目标仓库在创建时固定，可审计；
- 默认仓库和临时项目目录可以共存；
- 为未来 Project Repo Mode 预留 `repo_mode`。

**负面 / 代价**：

- 每个任务多存两个 acceptance 字段；
- 显式目标需要 UI 校验，非法目录会在提交前被拒绝；
- 旧任务没有这些字段，回退到全局默认；
- Project Repo Mode 仍未实现，当前显式目标仍然只写 `tasks/<id>` 子树。

**因此不能做的事**：

- 不能把普通目录自动当成项目仓库；
- 不能在运行中悄悄换掉 Task 的目标仓库；
- 不能把 AIEOS 主仓库作为目标。

## 证据

- `tests/test_commit_service.py`：显式项目仓库、repo_mode、recorded path；
- `tests/test_commit_controller.py`：Task acceptance 目标优先、绝对路径校验；
- `tests/test_aieos_ui.py`：默认仓库 API、目录浏览 API、选择器静态入口。
