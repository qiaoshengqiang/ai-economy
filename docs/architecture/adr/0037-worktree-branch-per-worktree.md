# ADR-0037：Worktree 的 branch 是「每个 worktree 一个」，不是「每个 Workspace 一个」

- **状态**：Accepted（**已实现**）
- **相关**：ADR-0032（Worktree / Workspace 边界）· ADR-0036（并发与崩溃一致性）
- **细化**：设计文档 [`CHANGE_PIPELINE_V1_DESIGN.md`](../implementation/CHANGE_PIPELINE_V1_DESIGN.md) §3.3
- **触发**：实现 Phase 1 时**真实撞上**的 Git 硬规则

## Context

设计文档 §3.3 选了「1 Task Workspace ↔ 1 Git Worktree」，
并在 §3.4 写下「1 Task Workspace = 1 branch」。

实现时第一版照此实现：

```python
branch = f"aieco/{workspace_id}"      # 每个 workspace 一个稳定分支名
```

**它当场就崩了** —— 而且是**真实 Git** 报的错，不是我们猜的：

```
$ git worktree add .worktrees/WS-001/WT-new aieco/WS-001
fatal: 'aieco/WS-001' is already checked out at
       '.worktrees/WS-001/WT-old'
```

原因：任务书 §3 明确允许「历史 / 归档 Worktree 可以存在」，
而 **Git 硬性禁止同一个 branch 被两个 worktree 同时 checkout**。

```
归档的 worktree  → 仍 checked out 在 aieco/WS-001
新建 live worktree → 想用同一个 branch  → ❌ Git 拒绝
```

这不是「我们的选择」，是 **Git 的规则**。

## Decision

### 🔒 branch 名由 `worktree_id` 派生 ⇒ 天然唯一

```python
branch = f"aieco/{workspace_id}/{worktree_id}"
```

- 不同的 worktree **永远不抢同一个 branch**
- 归档 worktree 可以保留它的 branch（它是历史，不该被改）
- 新建 live worktree 拿到自己的 branch

### 这是**细化**，不是推翻

设计文档 §3.3 说的「1 branch」原意是：

> **同一个 Workspace 在任一时刻只有一个「活的」工作分支。**

新方案完全保留了这个语义 —— 只是把「活的那个是谁」编码进了 branch 名，
而不是让所有历史 worktree 共用一个名字。

### 代价

**branch 数量 = worktree 数量**（含归档的）。

这与任务书 §18 一致：

```
20 Agents ≠ 20 worktrees
worktree 数由 Task / Workspace 决定，不由 Agent 决定
```

所以 branch 数也不会因为 Agent 数而爆炸 ——
它只随 **Task 数** 增长，且归档的那些在下次 reconcile 时是可见、可审计的。

### 为什么不用 detached HEAD

任务书 §16 让在 `branch` / `detached HEAD` / `dedicated ref` 里选
「最简单、最符合标准 Git Worktree 的做法」。

选 branch 的理由：**Patch 阶段需要一个稳定的 ref 作为 diff 基线**。
detached HEAD 上的 commit 不属于任何 ref ——
下一步（Patch）会需要 `git diff <base_commit> <ref>`，
而在 detached HEAD 上拿这个 ref 得额外造一个。

> ⚠️ 这条选择**留待 Patch 阶段验证**：如果那时发现 branch 太多是负担，
> 可以改成「只在生成 Patch 时打 ref」。记为 Open Question。

## Consequences

**正面**：

- 归档 worktree 与新建 live worktree **可以共存**（任务书 §3 的要求）
- branch 名自描述：一眼看出属于哪个 workspace 的哪个 worktree
- 不需要为了复用 branch 名去删/改历史 worktree 的 branch
  （那是**破坏历史**，而且违反 §13「不覆盖 Git branch」）

**负面 / 代价**：

- branch 会累积（每个 worktree 一个）
- 长期运行的仓库需要 branch GC —— **本阶段明确不做**
  （§31 禁止现在实现 recovery automation；GC 同理）

**因此不能做的事**：

- ❌ 用「每个 workspace 一个固定 branch 名」
- ❌ 为了复用 branch 名去 force-move 归档 worktree 的 branch
- ❌ 用 detached HEAD（Patch 阶段拿不到稳定 ref）

## Guard

- `tests/test_worktree_manager.py::TestFunctional::test_archived_allows_a_new_live_worktree`
  —— **就是这条测试抓出了这个 bug**（第一版会在这里失败）
- `tests/test_worktree_manager.py::TestIsolation` —— branch 名不得含物理路径
