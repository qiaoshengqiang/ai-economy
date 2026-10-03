# ADR-0036：Change Pipeline 的并发模型与 Crash Consistency

- **状态**：Accepted（**设计已定，尚未实现**）
- **相关**：ADR-0029（资源级并发）· ADR-0031（Database 线程模型）· ADR-0035（Lease）
- **设计文档**：[`CHANGE_PIPELINE_V1_DESIGN.md`](../implementation/CHANGE_PIPELINE_V1_DESIGN.md) §7 / §10 / §11 / §12

## Context

Workspace Core 已经证明「不需要全局串行锁」（ADR-0029，20 Agent 验证）。
第二阶段引入 Git 与 filesystem 后，有两个新风险：

```
风险 1：Git 本身有串行点 → 很容易退化成 Global Git Pipeline Lock
风险 2：DB / Git / Filesystem 是三个系统，**没有分布式事务**
```

## Decision

### 一、🔒 并发层级：局部串行，不是全局大锁

| 范围 | 行为 |
|---|---|
| 不同 Task | ✅ **完全并行**（不同 worktree · 不同 branch） |
| 同一 Task，不同 resource | ✅ **并行**（复用 Workspace 的资源级锁） |
| 同一 Patch | ⚠️ **协调**（同一时刻只有一个 TestRun / 一个 Merge） |
| 同一 Merge target | ⚠️ **必要处串行**（Git 本身要求） |
| 同一 Worktree 的写操作 | ⚠️ 串行（同一个物理目录） |

```
❌ with GLOBAL_GIT_LOCK: ...        （§27.11 禁止）
✅ with merge_target_lock("main"): ...    （按 target 串行）
✅ with worktree_lock(wt_id): ...         （按 worktree 串行）
```

> **Git 的限制可以形成局部串行点，但不能变成整个系统的大锁。**

**跨进程的协调靠 DB 状态**（不靠内存锁）：
`patch_id` 上的状态 + `merge_target_ref` 上的唯一约束。

### 二、🔒 绝不假设 Git / SQLite 天然原子 —— 用 **intent log**

```
1. DB: 写 INTENT      （state = PROVISIONING）
2. FS: 执行副作用      （git worktree add）
3. DB: 写 CONFIRM     （state = READY）
```

| 崩在哪 | 结果 | 恢复动作 |
|---|---|---|
| ① 之前 | 什么都没有 | 无 |
| ① 后 ② 前 | DB `PROVISIONING`，FS 无动静 | 重新执行（幂等） |
| ② 后 ③ 前 | DB `PROVISIONING`，FS **已建好** | **探测 FS** → 存在则直接 `READY`（不重复建）|
| ③ 之后 | 一致 | 无 |

**反向**（FS 变了、DB 事务失败）：
→ 视为**未确认的副作用**，由 reconciliation 回滚或接管，
  并且**必须**与 Workspace revision 对账（下面第三条）。

### 三、🔒 物理文件变化**必须**回写 Workspace revision

这是本设计**最容易出错的地方**：

```
Worktree 里的文件变了
        ↓
但 Workspace DB 的 revision **没变**
        ↓
→ Effective Lock 解析、Revision 比较**全部基于旧状态**
→ 静默不一致
```

**规则**：

```
任何经 Change Pipeline 造成的物理文件变化，
必须在**同一个逻辑操作**里回写 Workspace revision。

顺序：DB intent → FS → Workspace revision 回写 → DB confirm
```

崩在中间 → reconcile 时比较 FS 内容摘要与 Workspace revision；
不一致 → worktree 标 `NEEDS_RECONCILE`，**不允许**在它上面继续产 Patch。

> ⚠️ 这条规则意味着 **Change Pipeline 必须经 WorkspaceGateway 回写 revision**，
> 不能直接改 FS 就当完事了。这就是「Git 不得绕过 Gateway」的实质（§27.10）。

### 四、🔒 事实来源模型（四个系统，各管一段）

| 系统 | 负责 | **不负责** |
|---|---|---|
| **Workspace DB** | logical resource · **revision** · responsibility · audit · events | Git history · 文件内容 |
| **Git** | **commit history** · tree · branch · patch 内容 · merge ancestry | 责任 · revision |
| **Filesystem** | 实际 materialized worktree | **任何权威状态** —— 它是**派生**的，可重建 |
| **Test System** | execution result · artifacts · logs | 代码是否正确（那是 Acceptance 的判断） |

```
❌ revision 既看 Workspace DB 又看 Git 的 commit count
❌ 「谁负责」既看 lease 又看 lock
❌ worktree 是否存在既信 DB 又信 FS 而不管不一致
```

> **同一事实不能被两个系统无定义地同时当作 authority。**

### 五、Recovery 矩阵

| 场景 | 动作 | **不得做** |
|---|---|---|
| Agent 崩溃 | attempt → `EXPIRED`；失联事件 | ❌ 释放 Workspace Lock |
| 有 `IN_PROGRESS` 记录 | reconciliation 探测副作用 | ❌ 盲目重做 |
| Test crash | TestRun → `ERROR`，允许重跑（同 key） | ❌ 当成 `FAILED` 喂给 Acceptance |
| Worktree 损坏 | → `PROVISION_FAILED` → 重建（它是派生的） | ❌ 顺手改 responsibility |
| Process 重启 | 扫描非终态记录，按 §10.1 三种崩溃点处理 | ❌ 假设 FS 与 DB 一致 |
| Patch stale | 重新 Validation | ❌ 直接放行 |
| Acceptance reject | Patch → `REJECTED`；新 Patch 走新链路 | ❌ 自动改状态让它「看起来通过」 |
| Merge conflict | `CONFLICTED` + 详情 | ❌ 自动 resolve |
| Git 不可用 | Git 相关操作失败并保持原状态；Change Service 降级只读 | ❌ 阻塞 Workspace Core（它不依赖 Git）|

### 六、🔒 一条总原则

> **Recovery 可以改 attempt / worktree / patch 的状态，
> 但绝不自动改 Workspace responsibility。**
> 需要改责任时走正式入口：`Handoff` / `Release` / `Force Unlock`。

## Consequences

**正面**：

- 没有全局 Git pipeline lock
- 三个系统的边界清晰，`revision` 不会被 Git 的 commit 数污染
- 三种崩溃点都有确定的恢复动作
- Workspace Core **不依赖 Git** —— Git 坏了不影响它

**负面 / 代价**：

- intent log 让每个副作用操作变成三写（intent / FS / confirm）
- reconciliation 逻辑复杂，且需要**启动时扫描**
- 「物理变化必须回写 revision」增加了 Change Pipeline 对 Gateway 的耦合 ——
  但这是**正确方向**的耦合（它必须经受控入口）
- 同 target 串行会让大量并发 merge 排队

**因此不能做的事**：

- ❌ 全局 Git pipeline lock
- ❌ 假设 Git / SQLite 天然原子
- ❌ 直接改 FS 而不回写 revision
- ❌ 让两个系统同时当同一个事实的 authority
- ❌ 让 Recovery 修改 responsibility

## Guard

- 设计文档必须出现 intent log 的三步与三种崩溃点
- 必须出现「禁止全局 Git pipeline lock」
- 必须出现四系统的 source-of-truth 表
- 必须出现「物理变化必须回写 revision」
- 必须出现「Recovery 不改 responsibility」
