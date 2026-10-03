# Change Pipeline V1 —— 架构设计（第二阶段）

- **状态**：`DESIGNED`（**尚未实现** —— 本阶段只做设计）
- **前置**：Workspace Core V1 已冻结（ADR-0029 / 0030 / 0031 · 3397 passed）
- **目标链路**：`Git Worktree → Patch → Test → Acceptance → Merge`
- **相关文档**：[`TASK_WORKSPACE.md`](../TASK_WORKSPACE.md) · [`RECOVERY_MODEL.md`](../RECOVERY_MODEL.md) · [`COMMUNICATION_MODEL.md`](../COMMUNICATION_MODEL.md) · [`LOCK_MODEL.md`](../LOCK_MODEL.md)

---

## §0 冻结边界（不改，只引用）

Workspace Core V1 已冻结。本设计**不修改它的语义**，只定义它**旁边**的那一层。

| Workspace Core **负责** | Workspace Core **不负责**（本层负责） |
|---|---|
| Resource · Resource ID · Logical Path | Git · branch · commit |
| Revision | Git worktree |
| Controller · Effective Lock | patch generation |
| Handoff · Release · Force Unlock | test execution |
| Participant | acceptance |
| Workspace Event · Audit | merge |
| Read / Write control | |
| concurrency / transaction integrity | |

> 🔒 **本设计的每一条决定都必须能回答：「它有没有偷偷改 Workspace Core 的语义？」**
> 答案必须是「没有」。

### §0.1 🔒 第一原则：`Workspace Revision ≠ Git Commit`

```
Workspace Revision  =  Workspace Resource 的**逻辑状态变化历史**
Git Commit          =  某一批代码/文件状态**进入 Git 历史**

它们是**两个不同的问题**，由**两个不同的系统**回答。
```

**绝不允许**：

```
❌ every Workspace revision = one Git commit
```

**允许（甚至是常态）**：

```
Workspace Revision 12
Workspace Revision 13   ──►  Patch P123  ──►  Git Commit C1
Workspace Revision 14
```

```
Workspace Revision 20   ──►  Patch P200  ──►  （还没提交）
Workspace Revision 21   ──►  （还没出 Patch）
```

> **Git commit 的粒度由 Change / Patch pipeline 决定，
> 而不是由 Workspace Core 强制决定。**
>
> 反过来也成立：Workspace Core **不知道** Git 的存在 ——
> 这是 §0 那张边界表的直接推论。

---

## §1 Architecture

### §1.1 模块关系

```
                    Agent
                      │  （只能看到 workspace_id / resource_id / 逻辑路径）
                      ▼
                   Runtime                       ← 组合根
                      │
        ┌─────────────┴─────────────┐
        ▼                           ▼
    Task / Task Core           Workspace Core  ← 🔒 已冻结
        │                           │
        │                    WorkspaceGateway
        │                           │
        └─────────────┬─────────────┘
                      ▼
              Change Service                     ← 本层唯一的**门面**
                      │
   ┌──────────┬───────┼────────┬──────────┬──────────┐
   ▼          ▼       ▼        ▼          ▼          ▼
Worktree   Patch   Test     Acceptance  Merge    Execution
Manager    Service Runner   Service     Service  / Lease
   │          │       │        │          │          │
   └──────────┴───────┴────────┴──────────┴──────────┘
                      │
        ┌─────────────┼─────────────┐
        ▼             ▼             ▼
    Git Manager   Workspace DB   Test System
    （git CLI）   （SQLite）     （subprocess）
```

### §1.2 分层规则

```
Agent
  ↓  只能用逻辑 ID
Runtime
  ↓  组装
Change Service（唯一门面）
  ↓
各子系统
  ↓
Git / Filesystem / SQLite
```

| 规则 | 内容 |
|---|---|
| **R1** | `Agent` **不得** import 任何 Change Pipeline 子系统 |
| **R2** | `Change Service` 是 Agent 访问本层的**唯一受控入口**（与 Workspace Gateway 同构） |
| **R3** | `Git Manager` **不得**绕过 `WorkspaceGateway` 去改共享资源（§27.10） |
| **R4** | 子系统之间**不互相 import** —— 经 Change Service 或经 **DB 状态**通信 |
| **R5** | 只有 `main.py` 组装它们 |

### §1.3 为什么 Change Service 是**一个**门面而不是五个

因为 Agent 的每一次「产出」都是**一条链**，而不是五次独立调用：

```
「我改完了」 → 生成 Patch → 跑测试 → 提验收
```

如果五个子系统各自暴露给 Agent，就会出现：
- Agent 拿到 worktree → 直接改文件 → **绕过 Workspace Gateway**（§27.10）
- Agent 自己标 "test passed" → **Test Pass = Acceptance**（§27.4）

**一个门面 = 一条链路 = 一个可审计的 provenance。**

---

## §2 Responsibility Matrix

| 子系统 | Owns（拥有什么） | Validates（校验什么） | Persists（持久化） | **May change**（有权改什么） |
|---|---|---|---|---|
| **Workspace Core** 🔒 | resource · revision · controller · lock · participant | 责任/锁/revision | Workspace DB（5 张表） | **只有它**能改 resource 与 responsibility |
| **Worktree Manager** | worktree 生命周期 · 物理路径映射 | worktree 可用性 · Git 仓库可用性 | `git_worktrees` | worktree 元数据 + **物理文件**（但物理文件变化**必须回写** Workspace revision） |
| **Patch Service** | patch 身份 · base revision · applicability | **logical applicability**（不是 git apply） | `patches` · `patch_resources` | patch 自己的状态 |
| **Test Runner** | test run 身份 · 结果 · 日志 | 「测的是哪个 Patch/revision」 | `test_runs` | test run 自己的状态 |
| **Acceptance Service** | acceptance 身份 · 决策 · 理由 | acceptance base 是否仍成立 | `acceptances` | acceptance 自己的状态 |
| **Merge Service** | merge operation 身份 · 结果 | **stale acceptance**（§7.4） | `merge_operations` | **Git history**（唯一有权写 commit 的组件） |
| **Execution / Lease** | attempt 身份 · lease | lease 是否仍有效 | `execution_attempts` · `leases` | attempt / lease 自己的状态 |
| **Git Manager** | Git 仓库访问 | Git 侧前置条件 | （不是 DB —— 是 Git 本身） | Git objects（**经 Merge Service 授权**） |
| **Test System** | 执行结果 · artifacts | —— | 文件系统 artifacts | 不影响 DB |

### §2.1 四条最重要的「谁不许改什么」

```
❌ Worktree Manager 不得改 controller / lock
❌ Test Runner     不得改 patch status（只能写 test_run）
❌ Acceptance      不得改 Git
❌ Merge Service   不得改 resource revision（它改的是 Git history）
```

> 🔒 **Merge Service 是唯一能写 Git history 的组件** ——
> 但它**仍然要经 Workspace Gateway** 建立「哪些 resource 被这次 merge 影响」的对应关系（§27.10）。

---

## §3 Worktree ↔ Task Workspace 的关系

### §3.1 两者的区别（必须先说清楚）

| | **Task Workspace** | **Git Worktree** |
|---|---|---|
| 是什么 | **协作语义**层：Agent 共享资源、责任、版本 | **Git materialization**：实际代码文件 |
| 拥有 | Resource / Revision / Lock / Audit / Event / logical path | Git index · branch base · patch generation |
| 持久化 | Workspace DB | Git objects + filesystem |
| 生命周期 | `UNPROVISIONED→…→ARCHIVED`（ADR-0022） | **另一套**（§4） |
| Agent 看到 | `workspace_id` + 逻辑路径 | ❌ **什么也不看到**（§6） |

> 🔒 **Worktree 不能取代 Task Workspace。**
> 反过来说也成立：Task Workspace 里**没有**「git commit」这个概念。

### §3.2 三方案对比

| 维度 | **A**：1 Task Workspace → 1 Worktree | **B**：每个 Agent 一个 Worktree | **C**：每个 Change 一个临时 Worktree |
|---|---|---|---|
| 并发复杂度 | **低** —— 复用 Workspace Core 的 File Lock | **高** —— 每个 Agent 一个工作区，冲突后置 | 中 —— 每次改动都要建/拆 |
| Lock 语义 | **一致** —— 同一 File 一个 Effective Holder | **失效** —— 两个 worktree 可写同一文件，无人协调 | 一致（临时区只读/独占） |
| Patch 语义 | 一个 worktree → 一个 patch（自然） | N 个 worktree → 需要**先合并 Agent 间冲突**才能出 patch | 每个临时 worktree 一个 patch，需 reconcile |
| Agent 协作 | **天然**（共享视图，看得到别人改了什么） | **割裂**（各自看不到对方，冲突后置到 merge） | 割裂 |
| 冲突概率 | 低（锁已在前面挡住） | **高**（冲突被推迟到最贵的地方） | 中 |
| 生命周期 | 跟 Task 走 | 跟 Attempt 走（attempt 多了 worktree 爆炸） | 极短 |
| Recovery | 简单（一个 worktree） | 复杂（N 个都要管） | 中 |
| Cleanup | 简单 | **复杂** | 中 |
| Git branch 数量 | **1 / task** | **1 / agent / attempt** | 1 / change（临时） |
| 未来分布式 | worktree 可放远端 | 更适合「每台机器一个」 | 适合 CI farm |

### §3.3 选择：**A 为主 + C 的一种受限形式用于测试**

```
V1 模型：

    Task Workspace（逻辑，1 个）
            │
            │ 1:1
            ▼
    Task Git Worktree（物理，1 个）      ← Agent 在这里改代码
            │
            │ 只读派生
            ▼
    Ephemeral Test Worktree（每次 TestRun 一个，用完即删）
```

**为什么选 A**

1. **Workspace Core 已经把危险情况挡在前面了。**
   「Agent A 改 file1、Agent B 改 file2」在 Workspace 层**已经是并行的**，
   因为锁是**资源级**的（ADR-0029）。给每个 Agent 一个 worktree（B）
   等于**再实现一遍**并发控制，而且是在更差的位置（文件系统/Git 层）。
2. **B 会把冲突推到最贵的地方。** 两个 worktree 各自改同一个文件时，
   Workspace 的「同一 File 单 Holder」保证**失效** —— 冲突只能在 merge 时发现。
3. **协作语义保留。** Agent 想看别人改了什么，直接读同一个 worktree 即可。
4. **`1 task = 1 branch` 是可控的**（§21 的 20 Agent 目标下是 20 个 branch，
   而不是 400 个）。

**为什么**额外**要 C 的受限形式**

测试**绝不能**污染共享 worktree：

```
❌ 在共享 worktree 里跑 pytest
   → 生成 __pycache__ / .pytest_cache / 测试产物
   → 这些**不是** Agent 的修改，却出现在 git status 里
   → 污染 patch
```

所以 `Test Runner` 每次从 patch 派生一个**临时只读 worktree**，
跑完即删。这是 C 的优点（隔离），但**不承担协作职责**。

> ⚠️ **临时测试 worktree 不是工作区** ——
> Agent 看不到它，也不能在里面改代码。它是 Test Run 的**实现细节**。

### §3.4 🔒 Agent 永远看不到 physical worktree path

引入真实 filesystem 之后，这条约束**必须继续成立**：

```
Agent
  ↓  只给 logical workspace_id / resource_id / 逻辑路径
Change Service / Worktree Manager
  ↓  内部持有 physical path
Git / Filesystem
```

**反例（绝不允许出现在任何 Agent 可见的接口里）**：

```
❌ /Users/xxx/project/.worktrees/task-123
```

⚠️ 注意这**不只是**「不要返回路径」这么简单：

```
worktree_path 会出现在 git 命令的输出里
    → git worktree list 会打印物理路径
    → Git 报错信息里会带绝对路径
    → 所以 Change Service **必须**过滤/重写这些输出，
      而不是把 subprocess 的 stdout 直接透给 Agent
```

这条与 Workspace Core 的既有约束**完全一致**（ADR-0029）。

### §3.5 不选 B 的遗留问题（记为 Open Question）

如果未来需要「Agent 之间真正的独立实验」，
B 会重新被考虑 —— 但那时需要先解决
「两个 worktree 改同一文件时谁说了算」。见 §14 OQ-3。

---

## §4 State Machines

> 🔒 **六个独立的状态机，不是一个大状态机。**（§19 明确要求）
> 每个对象的状态只描述**它自己**。

### §4.1 Worktree

```
UNPROVISIONED
      │ provision()
      ▼
PROVISIONING ──── crash / git 失败 ────► PROVISION_FAILED
      │ git worktree add 成功                    │ retry
      ▼                                          │
    READY ◄──────────────────────────────────────┘
      │ 有 Patch 正在生成 / Test 正在跑
      ▼
    ACTIVE
      │ Task 完成 / 冻结
      ▼
   FROZEN
      │ archive()
      ▼
  ARCHIVED
```

**与 Workspace Lifecycle 的关系**：**两个独立状态机**。

```
Workspace: UNPROVISIONED → … → ARCHIVED     （ADR-0022）
Worktree:  UNPROVISIONED → … → ARCHIVED     （本设计）

它们**不是**同一条链，也不要求同步：
    Workspace 可以 READY 而 Worktree 还没 PROVISIONING（Lazy）
    Worktree 可以 ARCHIVED 而 Workspace 仍是 ACTIVE（重建 worktree 即可）
```

### §4.2 Patch

```
   DRAFT ──► CREATED ──► VALIDATED ──► TESTING ──► TESTED
     │          │            │            │          │
     │          │            │            │          ├──► ACCEPTED ──► MERGED
     │          │            │            │          │
     │          │            │            │          └──► REJECTED
     │          │            │            │
     └──────────┴────────────┴────────────┴──────────► ABANDONED
                                                        │
   任何状态 ──── 被更新的 Patch 取代 ──────────────────► SUPERSEDED
```

⚠️ **`MERGED` 与 `ACCEPTED` 是不同状态**（§17）：
`ACCEPTED` = 验收通过；`MERGED` = 真的进了 Git history。

### §4.3 TestRun

```
QUEUED ──► RUNNING ──┬──► PASSED
                     ├──► FAILED        （测试跑了，断言失败）
                     ├──► ERROR         （测试本身崩了 / 环境问题）
                     ├──► TIMEOUT
                     └──► CANCELLED
```

🔒 **`FAILED` ≠ `ERROR`** —— 前者是「代码不对」，后者是「测试没跑成」。
把两者混起来会让 Acceptance 做出错误判断。

### §4.4 Acceptance

```
PENDING ──┬──► APPROVED
          ├──► REJECTED
          └──► NEEDS_REVISION
                    │
                    └──► （Patch 被改）→ 新的 Acceptance 记录

任何非 PENDING 状态 ──── 目标分支变了 ────► STALE（§7.4）
```

### §4.5 MergeOperation

```
PENDING ──► CHECKING ──┬──► READY ──► MERGING ──┬──► MERGED
                       │                        ├──► CONFLICTED
                       │                        └──► FAILED
                       ├──► STALE_ACCEPTANCE ──► （需重新验收）
                       └──► REJECTED
```

### §4.6 ExecutionAttempt

```
STARTING ──► RUNNING ──┬──► COMPLETED
                       ├──► FAILED
                       ├──► HEARTBEAT_MISSED ──► EXPIRED ──► RETRYING ──► STARTING（新 attempt）
                       └──► ABANDONED
```

> 🔒 **`EXPIRED` 只影响 attempt，不碰 Workspace Lock。**（§11 / §27.7）

---

## §5 Data Model

> 本阶段**只设计 schema，不实现**。所有表都属于 **Change Pipeline DB 域**
> （与 Workspace 表在同一个 SQLite 文件里，但**不同前缀**、**不同 owner**）。

### §5.1 `git_worktrees`

| 列 | 说明 | 事实来源 |
|---|---|---|
| `worktree_id` PK | 稳定身份 | DB |
| `task_id` FK | 属于哪个 Task | DB |
| `workspace_id` | 对应哪个逻辑 Workspace | DB |
| `kind` | `TASK` / `TEST_EPHEMERAL` | DB |
| `state` | §4.1 | DB |
| `branch_ref` | `refs/heads/task/<task_id>` | DB（**指向** Git） |
| `base_commit` | 从哪个 commit 派生 | **Git** |
| `worktree_path` | 🔒 物理路径 —— **绝不出现在任何 Agent 可见接口中** | DB（内部） |
| `created_at` / `updated_at` | | DB |
| `last_verified_at` | 上次确认物理目录存在 | DB |

**Source of truth 分裂（这是本表最重要的一点）**：

```
state / identity / 归属   → DB 是权威
base_commit / 实际 commit → Git 是权威
物理目录是否存在           → **Filesystem 是权威，但可重建**
```

### §5.2 `patches`

| 列 | 说明 |
|---|---|
| `patch_id` PK | |
| `task_id` / `workspace_id` FK | 回答「从哪来」 |
| `worktree_id` FK | 在哪个 worktree 产生的 |
| `base_workspace_revision` | 🔒 Patch 的**逻辑**基线 |
| `base_commit` | Git 基线（**与上面不是一回事**） |
| `head_commit` | 产出时的 commit |
| `diff_ref` | Git diff 的引用（**不是内容本身**） |
| `state` | §4.2 |
| `applicability` | §6.3（**派生**，不是权威） |
| `created_by_attempt` FK | §9 的 attempt（provenance） |
| `idempotency_key` | §8 |
| `superseded_by` FK nullable | |
| `created_at` | |

### §5.3 `patch_resources`（Patch 修改了什么）

| 列 | 说明 |
|---|---|
| `patch_id` FK · `resource_id` FK | |
| `logical_path` | 冗余但便于审计 |
| `op` | `ADD` / `MODIFY` / `DELETE` / `RENAME` |
| `before_revision` | 🔒 出 Patch 时该资源的 Workspace revision |
| `after_content_digest` | 不存内容，存摘要 |

> 这张表是 §6.3 applicability 与 §6 provenance 的**基础**。

### §5.4 `test_runs`

| 列 | 说明 |
|---|---|
| `test_run_id` PK | |
| `patch_id` FK | 🔒 **必填** —— 没有 patch 就没有 test run |
| `tested_commit` | 测的是哪个 commit |
| `tested_resource_revisions` | JSON：测的时候各资源的 revision |
| `command` / `environment_ref` | |
| `worktree_id` FK | 临时测试 worktree |
| `state` | §4.3 |
| `exit_code` / `started_at` / `finished_at` | |
| `artifact_ref` | 日志/产物的**逻辑**引用（不是物理路径） |
| `idempotency_key` | |

### §5.5 `acceptances`

| 列 | 说明 |
|---|---|
| `acceptance_id` PK | |
| `task_id` / `patch_id` FK | |
| `test_run_ids` | JSON 数组 |
| `acceptance_criteria` | 从 Task 来（快照） |
| `evaluator` | `HUMAN:<id>` / `AGENT:<id>` / `SYSTEM` |
| `decision` | `APPROVED` / `REJECTED` / `NEEDS_REVISION` |
| `reason` | 🔒 **必填** —— 决策必须可追溯 |
| `acceptance_base_commit` | 🔒 §7.4 stale 检查用 |
| `created_at` | |

### §5.6 `merge_operations`

| 列 | 说明 |
|---|---|
| `merge_operation_id` PK | |
| `patch_id` / `acceptance_id` FK | |
| `merge_target_ref` | 目标分支 |
| `target_base_commit` | 🔒 merge 时的目标 HEAD |
| `source_commit` | |
| `merge_commit` | 成功后填 |
| `strategy` | `FF` / `MERGE_COMMIT` |
| `state` | §4.5 |
| `conflict_detail` | JSON |
| `idempotency_key` | |
| `created_at` / `finished_at` | |

### §5.7 `execution_attempts`

| 列 | 说明 |
|---|---|
| `attempt_id` PK | |
| `task_id` / `agent_id` | |
| `operation_id` | 🔒 **Agent 提供的逻辑操作 id**（幂等的基础） |
| `idempotency_key` | 唯一索引 |
| `status` | §4.6 |
| `retry_count` / `started_at` / `finished_at` | |
| `last_heartbeat_at` | |
| `predecessor_attempt_id` | retry 链 |

### §5.8 `leases`

| 列 | 说明 |
|---|---|
| `lease_id` PK | |
| `attempt_id` FK | 🔒 **租约属于 attempt，不属于 resource** |
| `scope` | `TASK` / `WORKTREE` / `TEST` —— **不是** `RESOURCE` |
| `acquired_at` / `expires_at` / `renewed_at` | |
| `heartbeat_interval_ms` | |
| `state` | `ACTIVE` / `RENEWED` / `EXPIRED` / `RELEASED` |

> 🔒 **`scope` 里没有 `RESOURCE`** —— 这是「Execution Lease ≠ Workspace Lock」
> 在 schema 层的体现。它**不可能**表达「我持有 resource X」。

### §5.9 `idempotency_records`（§12 的基础设施）

| 列 | 说明 |
|---|---|
| `idempotency_key` PK | |
| `operation_type` | `PROVISION_WORKTREE` / `CREATE_PATCH` / `RUN_TEST` / `MERGE` |
| `attempt_id` FK | 谁做的 |
| `status` | `IN_PROGRESS` / `COMPLETED` / `FAILED` / `ABANDONED` |
| `result_ref` | 指向产出的对象 id |
| `created_at` / `finished_at` | |

---

## §6 Provenance

### §6.1 完整链路

```
Workspace Revision R12 ─┐
Workspace Revision R13 ─┼──► Patch P123 ──► TestRun T1 ──► Acceptance A1
Workspace Revision R14 ─┘         │              │              │
                                  │              │              ▼
                                  │              │      MergeOperation M1
                                  │              │              │
                                  └──────────────┴──────────────┴──► Git Commit C1
```

> 🔒 **多个 Workspace Revision → 一个 Patch → 一个 Git Commit。**
> **绝不**是 `1 revision = 1 commit`（§27.1）。

### §6.2 Patch 必须能回答三个问题（§8）

| 问题 | 由谁回答 |
|---|---|
| **1. 它从哪里来？** | `patches.task_id` · `workspace_id` · `base_workspace_revision` · `created_by_attempt` |
| **2. 它改了什么？** | `patch_resources`：`resource_id` · `logical_path` · `before_revision` · `after_content_digest` |
| **3. 它最终去了哪里？** | `test_runs` → `acceptances` → `merge_operations.merge_commit` |

### §6.3 Patch Applicability（§9 的核心）

**五种状态，且必须区分「Git 能 apply」与「逻辑上仍有效」：**

| Applicability | 含义 | 判定 |
|---|---|---|
| `VALID` | 所有 affected resource 的当前 revision **==** `before_revision` | 逻辑基线未动 |
| `STALE` | 有资源的 revision **已前进**，但 Git 仍能 apply | 🔒 **不能自动视为有效** |
| `CONFLICTED` | Git **apply 不了** | 内容冲突 |
| `APPLIED` | 已经进过 Git history | Git ancestor 检查 |
| `SUPERSEDED` | 有更新的 Patch 取代它 | `superseded_by` |

> 🔒 **`STALE` 是最容易被忽略、也最危险的一种。**
>
> ```
> ❌ 禁止：git apply --check 通过 ⇒ 认为 Patch 仍然有效
> ```
>
> 理由：`git apply` 只看**文本**能不能落下去。
> 但 Workspace 的 revision 已经前进意味着
> **有人在这个资源上做了别的语义修改** ——
> 文本恰好还放得下，不代表**逻辑意图**还成立。
>
> **`STALE` 必须重新走一遍 Validation**，不能被 `Acceptance` 直接放行。

### §6.4 `applicability` 是**派生值**，不是权威

与 Effective Lock 同样的道理（ADR-0029）：

```
权威：base_workspace_revision（存）+ 当前 revision（存）
派生：applicability（每次算，不缓存为事实）
```

---

## §7 Concurrency

### §7.1 并发层级

| 范围 | 行为 |
|---|---|
| **不同 Task** | ✅ **完全并行**（不同 worktree · 不同 branch） |
| **同一 Task，不同 resource** | ✅ **并行**（复用 Workspace 的资源级锁） |
| **同一 Patch** | ⚠️ **协调** —— 同一时刻只允许一个 TestRun / 一个 Merge |
| **同一 Merge target** | ⚠️ **必要处串行**（Git 本身要求） |
| **同一 Worktree 的写操作** | ⚠️ 串行（对同一个物理目录） |

### §7.2 同步机制

| 层 | 机制 | 说明 |
|---|---|---|
| Workspace 层 | `ResourceLockRegistry`（**已冻结**） | 资源级，复用它，不重新发明 |
| Patch 层 | `patch_id` 上的 **DB 状态** | 不靠内存锁 —— 要跨进程 |
| Merge 层 | `merge_target_ref` 上的 DB 唯一约束 | 同一 target 同时只有一个 `MERGING` |
| Worktree 目录 | 每 worktree 一把**进程内**锁 | 防止同进程内两个操作写同一目录 |

### §7.3 🔒 禁止全局 Git pipeline lock（§27.11）

```
❌ with GLOBAL_GIT_LOCK:
       ... 所有 Git 操作 ...

✅ 按 merge target 串行：
       with merge_target_lock("main"):
           ...

✅ 按 worktree 串行：
       with worktree_lock(wt_id):
           ...
```

**Git 的限制可以形成局部串行点，但不能变成整个系统的大锁。**

### §7.4 防止 stale acceptance（§18）

```
Acceptance A1 记录 acceptance_base_commit = C_target@T1
                                              │
                          （期间 main 前进到 C_target@T2）
                                              ▼
Merge M1 检查：
    if current_target_head != acceptance.acceptance_base_commit:
        → MergeOperation = STALE_ACCEPTANCE
        → **必须重新验收**（不是自动 merge）
```

> 🔒 **禁止「曾经通过测试和验收，所以现在无条件 merge」。**

---

## §8 Lease 与长任务（§10 / §11）

### §8.1 两层，必须分离

```
┌─────────────────────────────────────────────────┐
│  Responsibility        「谁**有权**负责这个资源？」│
│  ─────────────────────────────────────────────  │
│  由 **Workspace Core** 管理（已冻结）             │
│      Controller · Explicit Lock · Handoff ·     │
│      Release · Force Unlock                     │
│  变化**只**由责任操作触发                         │
└─────────────────────────────────────────────────┘
                     ↕  🔒 没有任何自动通道
┌─────────────────────────────────────────────────┐
│  Execution Lease       「这次**执行机会**还有效吗？」│
│  ─────────────────────────────────────────────  │
│  由 **Execution / Lease** 管理（本层）            │
│      Attempt · Lease · Heartbeat · Expiry ·     │
│      Retry · Reclaim                            │
│  变化**只**影响 attempt                          │
└─────────────────────────────────────────────────┘
```

### §8.2 「Agent 长时间思考」怎么办

**问题**：Agent 思考 20 分钟，lease 5 分钟，会怎样？

```
❌ 错误做法（§27.7 明确禁止）：
    lease expired
        ↓
    release workspace lock          ← 绝不

    后果：Agent 还在思考，锁没了，
          别人改了同一个文件，Agent 回来一写 → **静默覆盖**。
```

```
✅ 正确做法：
    1. Lease 到期 → ExecutionAttempt = HEARTBEAT_MISSED → EXPIRED
    2. Workspace Lock **完全不动**
    3. 系统产生一个「attempt 失联」事件（不是「资源释放」事件）
    4. 该 attempt 的**产出**（如果有）被标记为 in-flight，等待 reconciliation
    5. 人的/治理的介入决定：
         · 恢复 attempt（新 attempt，继承 operation_id）
         · 或者正式 Force Unlock / Handoff（§27「Recovery 不得悄悄改责任」）
```

> 🔒 **`Recovery ≠ 自动改变 Workspace responsibility`。**
> 这与 [`RECOVERY_MODEL.md`](../RECOVERY_MODEL.md) 的
> `异常检测 ≠ 治理决策` 是同一条原则。

### §8.3 Heartbeat 设计

| 项 | 值 |
|---|---|
| `heartbeat_interval_ms` | 建议 `lease_ttl / 3` |
| `lease_ttl` | **必须 > 最慢的单次操作**（这是技术债 #1 的正面回应） |
| 判定 `HEARTBEAT_MISSED` | 超过 `2 × interval` 没有心跳 |
| 判定 `EXPIRED` | 超过 `lease_ttl` |
| 续租 | 长任务**必须显式**续租（`renew_lease`），不能靠「自动无限延长」 |
| ⚠️ | **不做**「超时自动重投递」（§27.12）—— 只**标记**，不自动动作 |

### §8.4 🔒 Lease 的 `scope` 里**没有** resource

§5.8 已经体现了这一点：`leases.scope ∈ {TASK, WORKTREE, TEST}`。
**结构上不可能**表达「我持有 resource X」。

这比「文档里写一句不要混」更强 —— **schema 层就挡住了**。

---

## §9 幂等与重复副作用（§12 / §13）

### §9.1 三层 ID

```
operation_id        ← Agent 提供的**逻辑操作**标识
                      「同一个意图」在 retry 前后**不变**
        ↓
idempotency_key     ← hash(operation_id + operation_type)
                      唯一索引 —— DB 层强制
        ↓
attempt_id          ← 每一次**尝试**都有新 id
                      retry 会产生新的 attempt
```

### §9.2 危险场景与阻断

```
Attempt A456: create file X → lease expires
        ↓
Attempt B789（retry，**同一个 operation_id**）
        ↓
1. 查 idempotency_records[key]
2. 若 status = COMPLETED  → 直接返回 result_ref，**不重复执行**
3. 若 status = IN_PROGRESS → 进入 reconciliation（不是盲目重做）
4. 若不存在               → 执行，并先写 IN_PROGRESS
```

> 🔒 **「先写 IN_PROGRESS，再执行」** —— 这就是 crash consistency 的基础（§10）。

### §9.3 Patch 的幂等（§13）

```
Patch P123 created by Attempt A456
        ↓
A456 被 retry
        ↓
❌ 不会产生一个完全不可追踪的 P456
✅ 新的 attempt 拿到**同一个** operation_id
   → 查到 P123 已经存在
   → 返回 P123（或把它标记为 SUPERSEDED 后再产生新的，**且记录 superseded_by**）
```

`patches.created_by_attempt` + `patches.idempotency_key` +
`patches.superseded_by` 三者合起来保证：
**任何 Patch 都能追到「谁在什么时候为了哪个逻辑操作产生的」。**

---

## §10 Crash Consistency（§23）

### §10.1 🔒 绝不假设 Git / SQLite 天然原子

**没有分布式事务。** 所以用 **intent log + reconciliation**：

```
1. DB: 写 INTENT（state = PROVISIONING）
2. FS: 执行副作用（git worktree add）
3. DB: 写 CONFIRM（state = READY）
```

**崩溃点分析**：

| 崩在哪 | 结果 | 恢复动作 |
|---|---|---|
| ① 之前 | 什么都没有 | 无 |
| ① 之后 ② 之前 | DB 有 `PROVISIONING`，FS 没动静 | 重新执行（幂等）|
| ② 之后 ③ 之前 | DB `PROVISIONING`，FS **已经建好** | **探测 FS** → 存在则直接进 `READY`（不重复建）|
| ③ 之后 | 一致 | 无 |

**反向情况**（FS 变了、DB 事务失败）：
→ FS 的变化是**未确认的副作用** → 由 reconciliation **回滚或接管**，
   并且**必须**与 Workspace revision 对账（§10.2）。

### §10.2 物理文件变化 ↔ Workspace revision 对账

这是**本设计最容易出错的地方**：

```
Worktree Manager 让 worktree 里的文件变了
        ↓
但 Workspace DB 里的 revision **没有变**
        ↓
→ Effective Lock 解析、Revision 比较全部基于**旧**状态
→ 静默不一致
```

**规则**：

```
🔒 任何**经 Change Pipeline**造成的物理文件变化，
   必须在**同一个逻辑操作**里回写 Workspace revision。

   顺序：DB intent → FS → Workspace revision 回写 → DB confirm

   如果中间崩了：
        reconcile 时比较 FS 的实际内容摘要 与 Workspace 记录的 revision
        不一致 → 该 worktree 标记为 NEEDS_RECONCILE
                 **不允许**在它上面继续产 Patch
```

> ⚠️ **这条规则意味着 Change Pipeline 必须经 WorkspaceGateway 回写 revision** ——
> 不能直接改 FS 就当完事了。这也是 §27.10 的实质。

---

## §11 事实来源模型（§24）

| 系统 | 负责 | **不负责** |
|---|---|---|
| **Workspace DB** | logical resource · **revision** · responsibility · task context · audit · events | Git history · 文件内容 |
| **Git** | **commit history** · tree · branch · patch 内容 · merge ancestry | 责任 · revision · 谁该改哪个文件 |
| **Filesystem** | 实际 materialized worktree | **任何权威状态** —— 它是**派生**的，可重建 |
| **Test System** | execution result · artifacts · logs | 代码是否正确（那是 Acceptance 的判断） |

### §11.1 关键判定表

| 事实 | Authority |
|---|---|
| 这个资源现在的 revision | **Workspace DB** |
| 这个资源谁负责 | **Workspace DB** |
| 某个 commit 的内容 | **Git** |
| 两个 commit 的祖先关系 | **Git** |
| 某个 Patch 声称改了什么 | `patch_resources`（DB）+ Git diff（内容） |
| 这个 worktree 目录现在存在吗 | **Filesystem**（但可重建，不是"事实"） |
| 测试跑过了吗 | **DB（`test_runs`）**；日志在 FS 但只是 artifact |

> 🔒 **同一事实不能被两个系统无定义地同时当作 authority。**
>
> 特别地：
> ```
> ❌ revision 既看 Workspace DB 又看 Git 的 commit count
> ❌ 「谁负责」既看 lease 又看 lock
> ❌ worktree 是否存在既信 DB 又信 FS 而不管不一致
> ```

---

## §12 Recovery（§20）

| 场景 | 检测 | 动作 | **不得做** |
|---|---|---|---|
| **Agent 崩溃** | heartbeat 超时 | attempt → `EXPIRED`；产生失联事件 | ❌ 释放 Workspace Lock |
| **Attempt 失联后** | 有 `IN_PROGRESS` 的 idempotency record | reconciliation：探测副作用做了什么 | ❌ 盲目重做 |
| **Test crash** | TestRun → `ERROR`（不是 `FAILED`） | 允许重跑（同 `idempotency_key`） | ❌ 当成 FAILED 喂给 Acceptance |
| **Worktree 损坏** | 目录不可用 / git 报错 | → `PROVISION_FAILED` → 重建（可重建，因为它是派生的） | ❌ 顺手改 responsibility |
| **Process 重启** | DB 里有非终态记录 | reconciliation 扫描 §10.1 的三种崩溃点 | ❌ 假设 FS 与 DB 一致 |
| **Patch stale** | applicability = `STALE` | 重新 Validation | ❌ 直接放行 |
| **Acceptance reject** | decision = `REJECTED` | Patch → `REJECTED`；产生新 Patch 走新链路 | ❌ 自动改 Patch 状态让它"看起来通过" |
| **Merge conflict** | Git 返回冲突 | `CONFLICTED` + 记录详情 | ❌ 自动 resolve |
| **Git 仓库不可用** | git 命令失败 | 所有 Git 相关操作 → 失败并**保持原状态**；Change Service 降级（只读） | ❌ 阻塞 Workspace Core（它不依赖 Git） |

### §12.1 一条总原则

> 🔒 **Recovery 可以改 attempt / worktree / patch 的状态，
> 但**绝不自动**改 Workspace responsibility。**
>
> 需要改责任时，走正式入口：`Handoff` / `Release` / `Force Unlock`。

---

## §13 与既有系统的接口（§26）

| 概念 | 是什么 | **不是**什么 |
|---|---|---|
| **Workspace Event** | Workspace **状态**发生的变化（`RESOURCE_UPDATED` …） | ❌ 不是 Patch 事件 · ❌ 不是 Agent Message |
| **Agent Message** | Agent 之间的通信（「我做完 P123 了，帮我跑测试」） | ❌ 不是系统事件 |
| **Patch Event**（新） | Change Pipeline **自己**的事件（`PATCH_CREATED` · `TEST_PASSED` …） | ❌ 不进 Workspace Event 表 |
| **Task Shared Context** | 共同确认的任务知识（可以记录「当前候选 Patch = P123」） | ❌ 不是 Patch 的存储 |

### §13.1 四者关系

```
Agent 完成修改
      ↓
Workspace Gateway → RESOURCE_UPDATED（**Workspace Event**）
      ↓
Change Service → PATCH_CREATED（**Patch Event**，另一张表）
      ↓
Agent 发消息：「P123 好了，帮我跑测试」（**Agent Message**）
      ↓
Acceptance 通过 → Task Shared Context 记录「候选 Patch = P123」
```

> 🔒 **四个不同的东西，四张不同的表，四条不同的订阅关系。**
> 混起来会让「谁该被通知」无法回答。

### §13.2 Patch Event 是本阶段新增的第三类事件

```
Workspace Event  → Workspace 状态变化          （已冻结）
Communication    → Agent 之间说了什么          （已存在）
Patch Event      → Change Pipeline 状态变化     （**本层新增**）
```

---

## §14 Open Questions

| # | 问题 | 为什么现在不定 |
|---|---|---|
| **OQ-1** | Branch 命名与数量：`task/<task_id>` 是否会在长任务下爆炸？何时清理已 merge 的 branch？ | 取决于真实 task 数量，现在没有数据 |
| **OQ-2** | Test environment 的隔离级别（venv？容器？） | §27.12 禁止现在实现全部 recovery automation；环境隔离属于实现细节 |
| **OQ-3** | 何时需要方案 B（per-Agent worktree）？ | 需要先有「两个 Agent 独立实验」的真实需求；且需先解决「同一文件两个 worktree」的仲裁 |
| **OQ-4** | Reconciliation 由谁触发？（启动时扫描？Guardian？） | Guardian 属于 `FUTURE`（ADR-0028 的范围外） |
| **OQ-5** | Patch 的 `STALE` 重新 Validation 是否可以自动？（还是必须人/治理介入？） | 「自动改变状态」与「Recovery 不悄悄改责任」的边界需要先定 |
| **OQ-6** | 多个 Patch 同时指向同一个 merge target 时的排队策略 | 可能是 FIFO，也可能是「先 acceptance 先 merge」，需要真实场景 |
| **OQ-7** | `artifact_ref` 的过期与 GC | §27 禁止现在实现 GC |
| **OQ-8** | Acceptance 是否可以是**多个** evaluator（人类 + Agent）的合成？ | 现在只允许一个 evaluator；多元验收是未来 |
| **OQ-9** | Git repository 不可用时，Change Service 的「降级只读」具体降级到什么程度？ | 需要先明确哪些操作是只读安全的 |
| **OQ-10** | Worktree 与 Workspace 的 Lazy Provisioning 是否要对齐？ | ADR-0022 只管 Workspace；Worktree 的 lazy 策略需要单独定 |

---

## §15 实施顺序（**下一阶段**，现在不做）

```
1. Worktree Manager        （含 §10 的 intent log + reconciliation）
2. Patch                   （含 §6.3 applicability）
3. Test Runner             （含临时只读 worktree）
4. Acceptance              （含 §7.4 acceptance base）
5. Merge                   （含 stale 检查）
6. Execution Lease / Retry （含 §9 幂等）
7. Integration             （Change Service 门面 + 组合根）
```

---

## §16 本设计明确**不做**的事（§27 十二条）

```
❌ 1  Workspace Revision == Git Commit 的映射
❌ 2  Task Workspace == Git branch
❌ 3  Git Worktree 当作 Workspace Core
❌ 4  Test Pass == Acceptance
❌ 5  Acceptance == Merge
❌ 6  Execution Lease == Workspace Lock
❌ 7  Lease 到期自动释放 Workspace Lock
❌ 8  retry 产生不可追踪的重复副作用
❌ 9  Agent 获得 physical worktree path
❌ 10 Git 子系统绕过 Workspace Gateway 改共享资源
❌ 11 全局 Git pipeline lock
❌ 12 现在就实现所有 recovery automation
```

---

## §17 相关 ADR

| ADR | 主题 |
|---|---|
| [ADR-0032](../adr/0032-git-worktree-workspace-boundary.md) | Git Worktree / Task Workspace 边界与 V1 模型选择 |
| [ADR-0033](../adr/0033-patch-and-workspace-revision-provenance.md) | Patch 与 Workspace Revision 的 provenance 与 applicability |
| [ADR-0034](../adr/0034-test-run-acceptance-separation.md) | Test Run / Acceptance / Merge 三者分离 |
| [ADR-0035](../adr/0035-execution-lease-vs-workspace-responsibility.md) | Execution Lease 与 Workspace Responsibility 的分离 |
| [ADR-0036](../adr/0036-change-pipeline-concurrency-and-crash-consistency.md) | Change Pipeline 的并发模型与 crash consistency |
