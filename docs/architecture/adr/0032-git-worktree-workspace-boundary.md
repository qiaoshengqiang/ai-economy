# ADR-0032：Git Worktree 与 Task Workspace 的边界

- **状态**：Accepted（**设计已定，尚未实现**）
- **相关**：ADR-0022（Workspace Lifecycle）· ADR-0029（Workspace 并发）· ADR-0031（Database 线程模型）
- **设计文档**：[`CHANGE_PIPELINE_V1_DESIGN.md`](../implementation/CHANGE_PIPELINE_V1_DESIGN.md) §1 / §3 / §4.1

## Context

Workspace Core V1 已冻结。第二阶段引入真实 filesystem 与 Git，
第一个必须回答的问题是：

> **Git Worktree 与 Task Workspace 是什么关系？会不会其中一个吃掉另一个？**

两者确实容易被混为一谈，因为它们都叫「工作区」：

| | Task Workspace | Git Worktree |
|---|---|---|
| 是什么 | **协作语义**层 | **Git materialization** |
| 拥有 | Resource · Revision · Lock · Audit · Event · logical path | Git index · branch base · 实际文件 |
| 持久化 | Workspace DB | Git objects + filesystem |

## Decision

### 一、🔒 两者**互不取代**，边界写死

```
Task Workspace 负责：协作语义
     Resource · Resource ID · Logical Path · Revision · Controller
     Effective Lock · Handoff · Release · Force Unlock · Participant
     Workspace Event · Audit · Read/Write control · 事务完整性

Git Worktree 负责：Git materialization
     实际代码文件 · Git index · branch / commit base · patch generation
```

**相互的禁止**（两条都要说，因为它们是对称的）：

```
❌ Worktree 不能取代 Task Workspace
   —— 它没有 revision / lock / responsibility 的概念

❌ Task Workspace 里不能出现「git commit」这个概念
   —— 那会让 Workspace Core 依赖 Git
```

### 二、V1 选择：**1 Task Workspace ↔ 1 Git Worktree**（+ 派生的临时测试 worktree）

```
Task Workspace（逻辑，1 个）
        │ 1:1
        ▼
Task Git Worktree（物理，1 个）        ← Agent 在这里改代码
        │ 只读派生
        ▼
Ephemeral Test Worktree（每次 TestRun 一个，用完即删）
```

**比较过的三个方案**（完整表见设计文档 §3.2）：

| | A 1:1 | B 每 Agent 一个 | C 每个 Change 临时 |
|---|---|---|---|
| 并发复杂度 | 低（复用 Workspace 资源级锁） | **高** | 中 |
| Lock 语义 | **一致** | **失效** | 一致 |
| Agent 协作 | 天然共享视图 | 割裂 | 割裂 |
| 冲突概率 | 低 | **高**（后置到 merge） | 中 |
| branch 数 | 1/task | 1/agent/attempt | 1/change（临时） |

**选 A 的三条理由**：

1. **Workspace Core 已经把危险情况挡在前面了。**
   「A 改 file1、B 改 file2」在 Workspace 层**已经是并行的**，
   因为锁是**资源级**的（ADR-0029）。
   方案 B 等于在**更差的位置**（文件系统/Git 层）**再实现一遍**并发控制。
2. **B 会把冲突推到最贵的地方。** 两个 worktree 改同一文件时，
   Workspace 的「同一 File 单 Holder」保证**失效** ——
   冲突只能等到 merge 才发现。
3. **协作语义保留**：Agent 看得到别人改了什么。

**为什么额外要「临时测试 worktree」**（C 的受限形式）：

```
❌ 在共享 worktree 里跑 pytest
   → 生成 __pycache__ / .pytest_cache / 测试产物
   → 这些**不是** Agent 的修改，却出现在 git status 里
   → **污染 patch**
```

所以 Test Runner 每次从 Patch 派生一个**临时只读** worktree，跑完即删。

> ⚠️ **临时测试 worktree 不是工作区** ——
> Agent 看不到它，也不能在里面改代码。它是 Test Run 的**实现细节**。

### 三、🔒 Worktree Lifecycle 与 Workspace Lifecycle 是**两个独立状态机**

```
Workspace: UNPROVISIONED → … → ARCHIVED     （ADR-0022，已冻结）
Worktree:  UNPROVISIONED → PROVISIONING → READY → ACTIVE
                              → FROZEN → ARCHIVED   （本 ADR）
```

**不要求同步**：

```
Workspace READY 而 Worktree 还没 PROVISIONING   ← Lazy 是允许的
Worktree ARCHIVED 而 Workspace 仍 ACTIVE        ← 重建 worktree 即可
```

> 🔒 **不要直接复制 Workspace lifecycle。** 两者的触发条件完全不同：
> Workspace 跟 **Task** 走；Worktree 跟 **Git 可用性**走。

### 四、🔒 Agent 永远看不到 physical worktree path

```
Agent
  ↓  只给 logical workspace_id / resource_id / 逻辑路径
Change Service / Worktree Manager
  ↓  内部持有 physical path
Git / Filesystem
```

```
❌ /Users/xxx/project/.worktrees/task-123
```

与 Workspace Core 的既有约束**完全一致**（ADR-0029 §「Agent 看不到宿主机路径」）。

### 五、🔒 1 Task Workspace = 1 branch，但 **Workspace ≠ branch**

branch 是 worktree 的**属性**，不是 Workspace 的**身份**：

```
❌ Task Workspace 就是 Git branch          （§27.2 禁止）
✅ Worktree 被 checked out 在 task/<task_id> 上，
   而 Workspace 的身份仍然是 workspace_id
```

## Consequences

**正面**：

- 复用 Workspace Core 已经验证过的资源级并发 —— **不重新发明锁**
- Agent 协作语义保留（共享视图）
- branch 数量可控：20 Agent → 20 branch（不是 400）
- 临时测试 worktree 让「测试污染 patch」这个坑**结构上不存在**

**负面 / 代价**：

- 同一 Task 内两个 Agent 想**独立实验**（互不干扰的沙箱）时做不到 ——
  需要先做完再看（方案 B 留给未来，见 OQ-3）
- 每次 TestRun 要建/拆一个 worktree —— 有 Git 操作开销
- `1 worktree` 是**单点**：它坏了，整个 Task 的产出都停了
  （但它是派生的、可重建的 —— 这缓解了严重性）

**因此不能做的事**：

- ❌ 让 Worktree 承担 revision / lock / responsibility
- ❌ 在 Task Workspace 里引入 git commit 概念
- ❌ 让 Agent 拿到 physical worktree path
- ❌ 把 Worktree lifecycle 和 Workspace lifecycle 合成一条
- ❌ 直接 `Task Workspace == Git branch`

## Guard

设计阶段的守卫（`tests/test_change_pipeline_design_guards.py`）：
- 设计文档必须同时出现「1:1 模型」与「临时测试 worktree」
- 必须写明「两个独立状态机」
- 必须写明 Agent 看不到 physical path
- **反向**：必须出现「禁止 Workspace == branch」的显式表述
