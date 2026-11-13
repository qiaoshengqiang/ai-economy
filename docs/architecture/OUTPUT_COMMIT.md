# Output Commit（输出仓库提交）

> **状态：`CURRENT`（V1）**
> `manual` / `limited` / `auto` 三种模式已实现；自动 push、复杂 merge 仍为 `FUTURE`。
>
> 相关 ADR：[ADR-0058](adr/0058-output-commit-and-authorization-modes.md)

---

## 1. 它解决什么

Task 完成后，产物目前只存在于：

```text
tasks/<task_id>/workspace/
```

这适合运行和验收，但不适合长期版本化。Output Commit 把**已完成的 final snapshot**
提交到一个**独立输出仓库**：

```text
aieos-output/
  tasks/<task_id>/...
```

输出仓库不是 AIEOS 主仓库，也不是 Task Workspace。

---

## 2. 三条边界

```text
Task Core        = 生命周期权威（COMPLETED）
Task Workspace   = 当前内容权威
Output Repo      = 交付物的外部版本历史
```

- 不新增 `COMMITTED` Task 状态；
- 不把提交结果写进 Worker 私有记忆；
- 不自动合并 AIEOS 主仓库；
- 不修改 `workspace/` 内容。

---

## 3. 提交时机

```text
Task COMPLETED
  → 创建 commit_request
      manual  → AWAITING_HUMAN
      limited → APPROVED → 立即执行
      auto    → APPROVED → 立即执行
  → OutputCommitService
  → 输出仓库 commit
```

Task 完成条件：

- `Task.status == COMPLETED`
- `final` snapshot 存在
- manifest 与 snapshot 一致
- summary 存在
- 不是 `interrupted` / `waiting_human`

`interrupted` / `waiting_human` / 失败的 Task 不创建提交请求。

---

## 4. 三种模式

| 模式 | 人类确认 | 自动提交 | 自动合并 | 自动 push |
|---|---|---|---|---|
| `manual` | 每次 | 否 | 否 | 否 |
| `limited` | 一次性策略 | 是，到任务分支 | 否 | 否 |
| `auto` | 一次性策略 | 是 | 可选 fast-forward | 否（`FUTURE`） |

- `manual`：完成时只创建待确认请求；
- `limited`：完成后自动提交到 `aieos/task-<task_id>`；
- `auto`：在 `limited` 基础上，可选 fast-forward 合并到基线分支；
- 三种模式都受同一套硬门禁约束；
- `auto` 不是「无门禁」，只是不需要每次人类点确认。

模式归属：

```text
Settings（人类） → 全局默认 / Agent 覆盖
Task 创建时快照 → acceptance.commit_mode
Front / Worker   → 不能修改
```

---

## 5. 状态机

```text
AWAITING_HUMAN
      │ 人类确认
      ▼
   APPROVED
      │
      ▼
   COMMITTING
      │
      ├── 门禁失败 → BLOCKED
      ├── 执行失败 → FAILED
      ▼
   COMMITTED

STALE       Task Reopen / iteration 变了 / 快照不再是最新 final
CANCELLED   人类取消
```

同一 `(task_id, iteration, snapshot_tree, output_repo, target_ref)` 只允许一条请求。

---

## 6. 输出仓库布局

```text
output_repo/
  .git/
  tasks/
    task_876b88f4/
      RESULT.md
      hello.txt
```

分支：

```text
main                        基线
aieos/task-<task_id>        每个 Task 一条任务分支
```

提交内容：

- 只包含 final snapshot 的交付文件；
- `TASK_DOC.md` 等 AIEOS 元数据默认排除；
- `.aieos/`、`node_modules/`、`__pycache__/` 不进入快照，也不会进入输出仓库。

`auto` 模式下，如果任务分支可以 fast-forward 到基线，才更新基线；
不能 fast-forward 时保留任务分支，不强制 merge。

---

## 7. 门禁

所有模式共享：

1. 只提交 `final` snapshot；
2. snapshot / tree / commit 与请求一致；
3. 空快照拒绝；
4. 单文件大小上限（默认 20MB）；
5. 总大小上限（默认 50MB）；
6. 输出仓库不能是 AIEOS 主仓库；
7. 同一请求幂等；
8. 记录 `mode / authorized_by / commit_sha / merge_sha / error`。

失败只更新 `commit_requests`，不回滚 Task。

---

## 8. Front 与 Worker

```text
Human 确认
   ↓
Front: commit_confirm
   ↓
Controller
   ↓
OutputCommitService
```

- Front 负责转达确认；git 由系统 `OutputCommitService` 执行；
- 提交请求来自人类确认或预授权策略；
- Worker 看不到 `commit_mode`；
- 提交结果留在 `commit_requests` / 审计 / 输出仓库。

如果以后需要让 Worker 知道提交结果，应作为**外部事实**注入，而不是自我评价。

---

## 9. 配置

```text
commit_mode             manual | limited | auto
commit_output_repo      输出仓库路径；空 = 不启用
commit_base_ref         基线分支，默认 main
commit_auto_merge       auto 模式下是否 fast-forward
commit_max_file_bytes   单文件上限
commit_max_total_bytes  总大小上限
```

UI「设置 → 运行」可以修改；也可以写入 `runs/ui/ui_settings.json`。

---

## 10. 非目标（V1）

- 自动 push 到远程（`FUTURE`）；
- 复杂 merge / 冲突解决（`FUTURE`）；
- 每个 Task 一个独立仓库（当前是一个输出仓库 + 任务子树）；
- 快照 diff / restore 的 UI（快照核心已有，UI 后续）；
- 把提交结果写回 Task 生命周期。

---

## 11. 证据

- `tests/test_commit_service.py`：manual / limited / auto / 幂等 / 返工 /
  空快照 / 大小门禁 / Reopen stale / 主仓库保护；
- `tests/test_commit_controller.py`：完成钩子 / Front `commit_confirm` 路由；
- 真实任务：`task_651450c9` → manual 确认 → 输出仓库分支提交，
  基线不变；详情见 `docs/PROJECT_STATUS.md`。

---

## 12. Project Repo Mode（`DESIGNED` / `FUTURE`）

当前实现是 **Output Repo Mode**：

```text
selected repo
  tasks/<task_id>/...      ← 只写任务子树
```

它适合交付物、文档、独立小项目，**不会改项目原有文件**。

如果目标是「直接修改另一个项目」，需要单独的 **Project Repo Mode**：

```text
Task
  target_repo   = /path/to/project
  base_ref      = main
  repo_mode     = project
```

### 12.1 任务创建

- 校验目录是 Git 仓库，且不是 AIEOS 主仓库；
- 校验 base_ref 存在、工作区干净；
- 通过 `WorktreeManager` 从项目创建一个任务 worktree；
- Pi 的 cwd 指向 worktree，而不是 `tasks/<id>/workspace`；
- Task 记录 `target_repo / base_ref / base_commit / worktree_id`。

### 12.2 执行与快照

- Pi 在 worktree 中读写项目文件；
- 现有 Task Snapshot 继续记录阶段性历史；
- 不允许一个项目同时有多个 live worktree（沿用 Workspace Core 的资源级约束）。

### 12.3 完成与提交

- final snapshot 对应 worktree 的最终状态；
- 在项目仓库中提交到：

```text
aieos/task-<task_id>
```

- 不再写 `tasks/<task_id>` 子树；
- 代码任务先跑测试；
- Human 确认后合并到 base_ref；
- `auto` 仍然只做 fast-forward，复杂冲突交给 Human。

### 12.4 与 Output Repo Mode 的区别

| | Output Repo Mode（当前） | Project Repo Mode（未来） |
|---|---|---|
| 目标 | 独立输出仓库 | 现有项目仓库 |
| 提交内容 | `tasks/<id>` 子树 | 项目文件修改 |
| 是否改项目 | 否 | 是 |
| 需要 worktree | 否 | 是 |
| 需要测试/验收 | 可选 | 代码任务必须 |
| 适合 | 产物 / 文档 / 独立项目 | 改现有代码库 |

### 12.5 非目标

- 不在 V1 直接修改任意项目；
- 不自动合并 AIEOS 主仓库；
- 不做跨项目依赖解析；
- 不做远程 push（仍为 `FUTURE`）。
