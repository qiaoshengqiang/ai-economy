# ADR-0033：Patch 与 Workspace Revision 的 Provenance 与 Applicability

- **状态**：Accepted（**设计已定，尚未实现**）
- **相关**：ADR-0029（Revision 语义）· ADR-0032（Worktree 边界）
- **设计文档**：[`CHANGE_PIPELINE_V1_DESIGN.md`](../implementation/CHANGE_PIPELINE_V1_DESIGN.md) §5.2 / §5.3 / §6

## Context

两个必须同时成立的约束：

```
1. Patch 是**一等对象** —— 有身份、有状态、有来源
2. Workspace Revision **不是** Git Commit
```

天真的做法会让它们合流：

```
❌ Workspace Revision 1 → Git Commit 1
   Workspace Revision 2 → Git Commit 2
   （every revision = one commit）
```

那会让 Git 的粒度**反向决定** Workspace 的粒度 —— 方向错了。

## Decision

### 一、🔒 多对一：多个 Workspace Revision → 一个 Patch → 一个 Git Commit

```
Workspace Revision R12 ─┐
Workspace Revision R13 ─┼──► Patch P123 ──► Git Commit C1
Workspace Revision R14 ─┘
```

也允许：

```
Workspace Revision R20 ──► Patch P200 ──► Git Commit C2
Workspace Revision R21 ──► （还没出 patch）
```

> **Commit 的粒度由 Change/Patch pipeline 决定，不由 Workspace Core 强制决定。**

### 二、Patch 必须回答三个问题（否则它不是一个对象）

| 问题 | 字段 |
|---|---|
| **从哪来？** | `task_id` · `workspace_id` · `base_workspace_revision` · `created_by_attempt` |
| **改了什么？** | `patch_resources`：`resource_id` · `logical_path` · `op` · `before_revision` · `after_content_digest` |
| **最终去哪？** | → `test_runs` → `acceptances` → `merge_operations.merge_commit` |

### 三、🔒 两个不同的 base，不要混

```
base_workspace_revision   ← 逻辑基线（Workspace DB 权威）
base_commit               ← Git 基线（Git 权威）
```

它们**几乎总是**不一致，而且**必须允许**不一致。
合并成一个字段会让 §9 的 applicability 无法正确判定。

### 四、🔒 Applicability 的五种状态，且**区分「能 apply」与「逻辑有效」**

| 状态 | 含义 |
|---|---|
| `VALID` | 所有 affected resource 的当前 revision **==** `before_revision` |
| **`STALE`** | 有资源的 revision **已前进**，但 Git 仍能 apply |
| `CONFLICTED` | Git apply 不了 |
| `APPLIED` | 已进 Git history |
| `SUPERSEDED` | 有更新的 Patch 取代它 |

```
🔒 禁止：git apply --check 通过 ⇒ 认为 Patch 仍然有效
```

**理由**：`git apply` 只看**文本**能不能落下去。
但 Workspace 的 revision 已经前进，意味着
**有人在这个资源上做了别的语义修改**；
文本恰好还放得下，不代表**逻辑意图**仍然成立。

> **`STALE` 必须重新走 Validation，不能被 Acceptance 直接放行。**
> 这是整份设计里最容易被忽略、也最危险的一条。

### 五、🔒 `applicability` 是**派生值**，不是权威

与 Effective Lock 同构（ADR-0029）：

```
权威：base_workspace_revision（存）+ 当前 revision（存）
派生：applicability（每次算，不缓存为事实）
```

**不建 `patch_applicability` 表** —— 那会是第二份会漂移的事实。

### 六、Patch 的幂等靠 provenance 字段，不靠「运气」

```
patches.created_by_attempt  ← 谁做的
patches.idempotency_key     ← 哪个逻辑操作
patches.superseded_by       ← 被谁取代
```

三者合起来保证：**任何 Patch 都能追到「谁在什么时候为了哪个逻辑操作产生的」**。
retry **不会**产生一个完全不可追踪的 Patch（ADR-0035 §幂等）。

## Consequences

**正面**：

- Git 与 Workspace 的粒度各自独立 —— 不会互相绑死
- Patch 是可审计的一等对象，不是「一段 diff」
- `STALE` 被显式建模 —— 静默覆盖的风险被结构性地挡住
- `applicability` 派生 ⇒ 不会有两份会漂移的事实

**负面 / 代价**：

- 需要同时维护两个 base（`base_workspace_revision` + `base_commit`），
  使用者必须理解它们的区别
- `patch_resources` 是冗余表（Git diff 里也有）——
  但它是**逻辑层**信息，Git 给不了
- `STALE` 的处理路径较长（重新 validation），
  会增加 Agent 的协作往返

**因此不能做的事**：

- ❌ `1 Workspace Revision = 1 Git Commit`
- ❌ 把 `base_workspace_revision` 与 `base_commit` 合成一个字段
- ❌ 用 `git apply --check` 的成功作为逻辑有效的证据
- ❌ 把 `applicability` 持久化成权威事实

## Guard

- 设计文档必须出现「多对一」的 provenance 图
- 必须显式出现「禁止 git apply 成功 ⇒ 逻辑有效」的表述
- 必须出现 `STALE` 的定义与「必须重新 validation」
- 必须写明 applicability 是**派生值**
