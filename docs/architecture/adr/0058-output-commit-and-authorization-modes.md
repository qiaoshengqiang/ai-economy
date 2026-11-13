# ADR-0058：Output Commit 与三种授权模式

- **状态**：Accepted（**V1 已实现；push / 复杂 merge 为 FUTURE**）
- **时间**：2026-11-13
- **Supersedes**：无
- **Superseded by**：无

## Context

Task 完成后，产物只在：

```text
tasks/<task_id>/workspace/
```

`tasks/` 是运行产物，不进入 AIEOS 主仓库。Pi 直接写真实目录，
没有 Git 历史，也没有「把交付物放进一个可版本化仓库」的路径。

同时，项目已有三条约束：

- Task Core 是生命周期权威；
- Task Workspace 是内容权威；
- Merge Service（Change Pipeline）是唯一能写 Git history 的组件。

OpenCode 的做法提供了参考：它把 diff 给人类看，提交仍走普通 Git；
AIEOS 有持久化 Agent 身份和私有记忆，不能直接照搬「Agent 自己 commit」。

## Decision

### 1. 独立输出仓库

```text
output_repo/
  tasks/<task_id>/...
```

每个 Task 一条分支：

```text
aieos/task-<task_id>
```

- 不写 AIEOS 主仓库；
- 不是每个 Task 一个仓库；
- 提交内容来自 final snapshot，不是 live workspace；
- `TASK_DOC.md` 等 AIEOS 元数据默认排除。

### 2. 提交时机 = Task COMPLETED

Task 完成（COMPLETED + final snapshot + manifest + summary）后创建
`commit_request`：

- `manual`：`AWAITING_HUMAN`，人类确认后执行；
- `limited`：创建后立即执行，只到任务分支；
- `auto`：创建后立即执行，可 fast-forward 合并到基线。

Task 状态不新增 `COMMITTED`；提交失败不回滚 Task。

### 3. Front 只转达，系统执行

```text
Human → Front(commit_confirm) → Controller → OutputCommitService
```

- Front 不执行 git；
- Front 不能发起提交；
- Worker 看不到 `commit_mode`；
- 提交结果只写 `commit_requests` / 审计 / 输出仓库。

### 4. 三种模式

| 模式 | 授权 | 自动提交 | 自动合并 | 自动 push |
|---|---|---|---|---|
| manual | 每次人类确认 | 否 | 否 | 否 |
| limited | 一次性策略 | 是 | 否 | 否 |
| auto | 一次性策略 | 是 | 可选 fast-forward | 否 |

模式由人类设置，Task 创建时快照；Front / Worker 不能修改。

### 5. 记忆边界

提交结果是**外部副作用**，不是 Worker 自我评价：

- 不写 `memory/pi/inbox`；
- 不写 Task summary / note；
- 不新增 Shared Context Decision；
- 不新增 Task status `COMMITTED`。

## Consequences

**正面**：

- 任意交付物可以进入一个独立、可浏览的 Git 仓库；
- Human 可以在 manual / limited 下先看任务分支，再决定是否合并；
- 不污染 AIEOS 主仓库，也不改变 Pi 的直接落盘方式；
- 提交时机明确绑定 Task COMPLETED，避免半成品进入历史。

**负面 / 代价**：

- 需要一个独立输出仓库和额外的磁盘空间；
- V1 不支持自动 push / 复杂 merge；
- 大文件仍受 Git 限制，超过门禁只能导出；
- 需要维护 `commit_requests` 表和确认码；
- 每次提交都会增加一个任务分支，需要后续清理策略。

**因此不能做的事**：

- 不能把 `commit_requests` 当 Task 状态；
- 不能让 Front / Worker 自己决定提交；
- 不能自动合并 AIEOS 主仓库；
- 不能把提交结果写进 Worker 记忆。

## 备选方案

| 方案 | 为什么不选 |
|---|---|
| Front 直接 `git commit` | 需要给 Front 写权限 / git 执行能力；角色和审计都会混 |
| Worker 自己提交 | 生产者自确认，记忆偏差最大 |
| 每个 Task 一个仓库 | 仓库数量爆炸，难以统一浏览和备份 |
| 直接提交到 AIEOS 主仓库 | 任意任务产物不适合主仓库；风险不可控 |

## 证据

- `tests/test_commit_service.py`：三种模式、幂等、返工、门禁、stale、主仓库保护；
- `tests/test_commit_controller.py`：完成钩子与 Front `commit_confirm` 路由；
- 真实任务 `task_651450c9`：manual 模式下人类确认后提交到任务分支，
  基线 `main` 未变化。
