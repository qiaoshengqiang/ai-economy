# ADR-0057：Task Snapshot —— 任务私有 Git 历史

- **状态**：Accepted（**核心已实现；UI 待接入**）
- **时间**：2026-11-13
- **Supersedes**：无
- **Superseded by**：无

## Context

Pi Work 直接写真实目录：

```text
tasks/<task_id>/workspace/
```

这带来两个问题：

- 任务过程中没有可查看的历史，失败 / 中断只能看到最后一版；
- 无法回答「这个文件上一阶段是什么样」「能不能回到某个阶段」。

同时，项目已有两条边界：

- `tasks/` 是运行产物，已在 `.gitignore`；
- AIEOS 主仓库不应被 Agent 自动提交。

OpenCode 的做法提供了一个参考：用**独立的 shadow Git 目录**保存快照，
不碰用户仓库、不在工作区放 `.git`，用 tree / diff / restore 提供历史。

## Decision

### 1. 每个 Task 一个私有快照仓库

```text
GIT_DIR        tasks/<task_id>/.aieos/snapshot.git
GIT_WORK_TREE  tasks/<task_id>/workspace
```

- 不在 `workspace/` 创建 `.git`；
- 不写 AIEOS 主仓库，不自动提交主分支；
- 所有 git 调用都是受控子进程：显式 `--git-dir` / `--work-tree`、
  不经过 shell、有超时、隔离用户全局 git 配置。

### 2. 快照是 workspace 的派生物

```text
Snapshot ≠ Task Workspace State
```

- Task Workspace 仍是内容权威；
- 快照只提供历史 / diff / restore；
- 验收看 workspace + manifest，不看快照；
- 不引入第二套可独立修改的状态。

### 3. 用 commit 记录快照

每个快照是 shadow repo 里的一个 commit：

```text
tree / parent / label / phase / turn / files / bytes / skipped
```

`refs/aieos/main` 指向最新快照，便于 `git log` 和对象可达性。
快照索引存放在 `.aieos/snapshots.json`，任务目录自包含。

### 4. 抓取时机

- `baseline`：Task 启动、Pi 写入前；
- `phase:<name>:end`：每个阶段完成；
- `interrupted`：中断 / 超时 / 异常；
- `waiting_human`：Agent 中途提问；
- `final`：完成前。

### 5. 恢复是 Human 动作

- 恢复前自动抓 `before-restore`，恢复本身可撤销；
- 恢复后抓 `after-restore`；
- 只恢复指定文件，或在 `exact=True` 时与快照完全一致；
- Front 暂不开放恢复，避免把「历史」变成对话副作用。

### 6. 忽略与限制

- 默认忽略 `.aieos/`、`.git/`、`node_modules/`、`.venv/`、
  `__pycache__/`、`.env`、`*.key`、`*.pem` 等；
- 未跟踪文件超过 2MB 默认跳过；
- 快照失败 fail-open：写 `snapshot-error.log`，不阻断 Task；
- 快照仓库是运行产物，不 push、不 merge、不导出到主仓库。

## Consequences

**正面**：

- 每个 Task 有可查看、可回滚的阶段历史；
- 不污染 AIEOS 主仓库，也不改变 Pi 的直接落盘方式；
- 任务目录自包含，删除任务时历史一起清理；
- 为后续 UI diff / restore / patch 导出提供稳定底座。

**负面 / 代价**：

- 需要依赖 `git`；
- 快照会占磁盘，需要后续做保留策略和 GC；
- 恢复 `exact=True` 会删除快照外的文件，必须 Human 确认；
- 目前只有核心 API，没有 UI 入口；
- 不解决「把成果合并回主仓库」的问题，那属于 Change Pipeline。

**因此不能做的事**：

- 不能把快照当 Task Workspace 的状态源；
- 不能让 Front 自动恢复快照；
- 不能在快照流程里自动提交 AIEOS 主仓库。

## 备选方案

| 方案 | 为什么不选 |
|---|---|
| `workspace/.git` 自动 commit | Pi 可能自己改历史；任务本身是 Git 仓库时冲突；导出多一个 `.git` |
| 主仓库 worktree + commit | 只适合改某个仓库的代码任务；小说 / 游戏 / 文档类任务不适用 |
| 只存 manifest hash | 只能判断变化，不能恢复旧版本 |

## 证据

- `tests/test_task_snapshot.py`：capture / diff / restore / 忽略 /
  大文件 / 路径逃逸 / 并发 / 继承 Git 环境隔离；
- `tests/test_pi_work_runtime.py`：Pi 跑完后快照标签与 manifest 元数据；
- 反向验证：临时去掉显式 `--git-dir` / `--work-tree` 后，
  守卫测试会因主仓库 HEAD 变化而失败。
