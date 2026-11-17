# ADR-0065：Worker Workspace Recon 与 Target Scope

- **状态**：Accepted（**V1 已实现**）
- **时间**：2026-11-17
- **Supersedes**：无（扩展 ADR-0060）
- **Superseded by**：无

## Context

ADR-0060 已经确定：project 模式下 Agent **直接编辑**用户项目目录，
shadow snapshot 使用独立的 `GIT_DIR`，不复制工作副本。

但 V1 把 `project_path` 直接当成 Agent 的 `workdir`，导致一个真实问题：

```text
Human 选择：/Users/.../默认项目
任务要求：在该目录下新建 pelican-cycling/ 子项目
实际 cwd：/Users/.../默认项目
实际快照：整个 /Users/.../默认项目
```

该父目录有 2.64GB / 7520 个文件，包含电子乐、WebGallery 等大量无关项目；
shadow snapshot 每次 `git add -A .` 都扫描整个父目录，产生：

- 2.9GB `snapshot.git`；
- `git add` 120 秒超时；
- 项目锁锁住整个父目录；
- diff / restore / manifest 都包含无关文件。

同时，是否“新建子目录”不应该由 Front 作为开关传给 Worker。
主流 coding agent 的做法是：

```text
Human 选择一个 workspace root
Agent 先 read / ls / grep / git status
Agent 自己判断是新建还是修改
```

## Decision

### 1. 区分 workspace_root 与 target

```text
workspace_root  Human 选择的硬边界，Worker 不能扩展
target_path     Worker recon 决定的相对路径，必须落在 root 内
```

Task 仍然只保存 `project_path`（workspace_root），不保存
`create_new` / `modify_existing` 模式。

### 2. Worker 先做只读 recon

project 模式下，Pi Work 正式执行前先跑一次只读侦察：

```text
cwd    = workspace_root
tools  = read, grep, find, ls
输出   = workspace_plan.json
```

计划形状：

```json
{
  "mode": "create | modify | mixed | unclear",
  "target_path": "pelican-cycling",
  "reason": "...",
  "confidence": 0.95
}
```

约束：

- `target_path` 必须是相对路径，不能是绝对路径，不能包含 `..`；
- resolve 后必须仍在 `workspace_root` 内；
- `create` 要求目标不存在或为空；
- `modify` / `mixed` 要求目标存在；
- `unclear` 时不创建、不上锁、不快照，等待人类确认。

### 3. 判断之后才 lock / create / snapshot

```text
recon → plan → validate
        ↓
lock(target_path)
        ↓
re-check target
        ↓
create target（create 模式）
        ↓
baseline snapshot(target)
        ↓
Pi work（cwd = target）
        ↓
final snapshot(target)
        ↓
release lock
```

- 锁 key 使用 target_path；锁文件不要求目录已存在；
- 在新目录任务中，锁先于目录创建，避免两个任务同时创建同一目标；
- baseline 在目标创建后、工作前捕获；
- snapshot、changed_files、manifest、restore 全部只覆盖 target；
- `ProjectLockManager` 不再锁父目录。

### 4. 不对 recon 强行限制 thinking / output

Recon 使用 Agent 当前配置（thinking / max output）。
如果模型在 recon 阶段因 `finish_reason=length` 截断：

- 明确失败为 `Workspace recon 输出被截断`；
- **不创建目录、不上锁、不快照**；
- 由人类调整模型设置后重试。

### 5. 计划可恢复

`tasks/<task_id>/.aieos/workspace_plan.json` 持久化。
`TaskDirectory` 在重新构造时读取已有 target，保证任务结束后
export / commit / manifest 仍使用同一个 target。

## Consequences

**正面**：

- 新建子项目时只快照该子项目，不再扫描父目录；
- 修改已有项目时 target 通常是 `.`，行为与旧版兼容；
- Front 不需要判断“新建/修改”，Worker 自己 recon；
- 不同子目录任务可以并行；
- 路径逃逸在进入写入前被拒绝。

**负面 / 代价**：

- project 模式多一次只读 Pi 调用；
- recon 可能与正式工作使用不同的 thinking 预算，需要人类调整；
- 如果 recon 判断错误，需要重新 recon 或人工确认；
- 父目录内容变化可能让 recon 结果过期，因此上锁后会再次检查目标状态。

**因此不能做的事**：

- 不能把 `create_new` / `modify_existing` 作为 Front 的开关；
- 不能让 Worker 把 target 扩展到 workspace_root 之外；
- 不能在 recon 之前对父目录做全量 snapshot；
- 不能在 `mode=create` 时覆盖已存在且非空的目标目录。

## 备选方案

| 方案 | 为什么不选 |
|---|---|
| Front 显式传 create/modify | 与主流 agent 相反；Front 不应替 Worker 判断 |
| 让 Worker 自己选择 workspace_root | 安全边界必须由 Human/UI 决定 |
| 继续快照整个父目录 | 2.64GB / 120s 超时 / 2.9GB 对象 |
| 复制项目到 Task Workspace | 违反 ADR-0060 的直接编辑原则，回写复杂 |

## 证据

- `tasks/task_43d06b72/.aieos/snapshot.git`：2.9GB，父目录 7520 files / 2.64GB；
- `snapshot-error.log`：`git add -A` 120 秒超时；
- `tests/test_workspace_recon.py`：计划解析、路径校验、target 创建；
- `tests/test_pi_work_runtime.py::test_pi_project_recon_creates_subdir_and_snapshots_only_it`：
  recon 使用 workspace_root，工作使用子目录，snapshot/manifest 只含子目录；
- 全量测试基线见 `PROJECT_STATUS.md`。
