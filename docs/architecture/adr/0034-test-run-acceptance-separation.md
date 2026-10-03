# ADR-0034：Test Run / Acceptance / Merge 三者分离

- **状态**：Accepted（**设计已定，尚未实现**）
- **相关**：ADR-0033（Patch）· ADR-0028（Read / Communication）
- **设计文档**：[`CHANGE_PIPELINE_V1_DESIGN.md`](../implementation/CHANGE_PIPELINE_V1_DESIGN.md) §4.3 / §4.4 / §4.5 / §5.4 / §5.5 / §5.6 / §7.4

## Context

三个非常容易被串成一条自动流水线的概念：

```
tests passed → automatically accepted → automatically merged
```

这条链**每一步都是错的**：

| 混淆 | 为什么错 |
|---|---|
| `Test Pass == Acceptance` | 测试只回答「机器可验证的部分是否满足」，不回答「是否满足 Task 的定义」 |
| `Acceptance == Merge` | 验收是**判断**；merge 是**改变 Git history** |
| `Acceptance 永久有效` | 验收之后目标分支可能已经前进 |

## Decision

### 一、🔒 三者的定义必须分开

```
Test
  = 「程序是否满足**机器可验证**的测试？」

Acceptance
  = 「这个 Change 是否满足 **Task 的定义与验收标准**？」

Merge
  = 「把这个**已经过验证与批准**的 Change 正式进入 Git history」
```

**Acceptance 可以使用**：test results · task requirements · artifacts ·
human feedback · acceptance agent · future governance agent。

> 🔒 **当前架构仍必须允许 Human 做最终反馈** ——
> 本轮**不**让 Acceptance Agent 取代 Human。

### 二、🔒 `TestRun.FAILED` ≠ `TestRun.ERROR`

```
FAILED  = 测试跑了，断言失败          → 「代码不对」
ERROR   = 测试本身崩了 / 环境问题     → 「测试没跑成」
TIMEOUT = 超时
```

把 `ERROR` 当成 `FAILED` 喂给 Acceptance，
会让「环境坏了」被误判成「这个 Patch 不好」——
**决策依据被污染**。

### 三、🔒 TestRun 必须回答「测的是**哪一个**状态」

```
❌ pytest passed
✅ Passed against Patch P123 @ commit C1 @ resource revisions {R1:5, R2:2}
```

所以 `test_runs` 必须有：

```
tested_commit
tested_resource_revisions        ← JSON
patch_id                          ← **必填**（没有 patch 就没有 test run）
```

### 四、🔒 防止 stale acceptance

```
Acceptance A1 记录 acceptance_base_commit = target@T1
                        │
        （期间 main 前进到 target@T2）
                        ▼
Merge 检查：
    if current_target_head != acceptance.acceptance_base_commit:
        → MergeOperation = STALE_ACCEPTANCE
        → **必须重新验收**，不是自动 merge
```

```
🔒 禁止：「曾经通过测试和验收，所以现在无条件 merge」
```

### 五、Merge 的完整输入输出

| 项 | 内容 |
|---|---|
| **输入** | `patch_id` · `acceptance_id` · `merge_target_ref` |
| **校验** | acceptance 未 stale · patch applicability 仍 `VALID` · 目标分支可写 |
| **输出** | `merge_commit` · 或 `CONFLICTED` + 冲突详情 |
| **记录** | `merge_operations` 一行 + Patch Event |
| **重试** | 靠 `idempotency_key`，不重复 merge |
| **审计** | 谁、什么时候、从哪个 commit 到哪个 commit |

### 六、状态机是**三个**，不是一条链

```
TestRun:      QUEUED → RUNNING → PASSED / FAILED / ERROR / TIMEOUT / CANCELLED
Acceptance:   PENDING → APPROVED / REJECTED / NEEDS_REVISION → (STALE)
MergeOp:      PENDING → CHECKING → READY → MERGING → MERGED / CONFLICTED / FAILED
```

> 🔒 **不要把它们揉成一个 `ChangePipelineState`。**
> 三个对象各有自己的生命周期、自己的 owner、自己的重试语义。

## Consequences

**正面**：

- 每一层的决策依据可追溯（谁基于什么批准了什么）
- 「环境坏了」不会被误判成「代码不好」
- stale acceptance 被显式建模 —— 长期分支上的静默错误 merge 被挡住
- Human 仍然是最终验收者（符合 ADR-0022 / ADR-0028）

**负面 / 代价**：

- 三段式比「tests passed → merge」慢得多
- 需要维护 acceptance base 一致性
- `NEEDS_REVISION` 会产生新的 Patch 与新的 Acceptance 记录 —— 记录会变多

**因此不能做的事**：

- ❌ `Test Pass == Acceptance`
- ❌ `Acceptance == Merge`
- ❌ 把 `ERROR` 当 `FAILED`
- ❌ 一个 Patch 永久有效
- ❌ 让 Acceptance Agent 现在取代 Human

## Guard

- 设计文档必须同时出现三者的定义
- 必须出现 `FAILED` vs `ERROR` 的区分
- 必须出现 `STALE_ACCEPTANCE` 与「禁止无条件 merge」的显式表述
- 必须写明 TestRun 要记录 tested revision / commit
