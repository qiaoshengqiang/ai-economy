# ADR-0030：Workspace Core Hardening —— 一致性不变量与边界完整性

- **状态**：Accepted（**已实现**）
- **相关**：ADR-0021（锁语义）· ADR-0027（设计简化）· ADR-0029（并发与 Revision）
- **任务书**：Workspace Core 第一阶段收尾工作

## Context

Workspace Core v1 已实现并通过 3251 项测试。收尾阶段的 Hardening
不重新设计模型，而是问一个问题：

> **在异常、竞争、重启、多 Agent 并发的情况下，
> 它还守不守它已经定好的语义？**

为此建立了 `check_workspace_invariants()` —— 把任务书 §20 的不变量
变成**可执行断言**，并在每个测试场景后运行。
**这个检查器抓出了 5 个真实缺陷**，其中 3 个是读代码看不出来的。

## Decision

### 一、🔒 序列化 ≠ 一致性：加锁后必须**在事务内重读**

**抓到的 bug**：12 个并发 create 让根目录出现 **12 条
`revision_number` 都是 2** 的 revision 行。

根因：`create` 用的是**加锁之前**解析出的 `parent` 对象。
命名空间锁确实把它们**串行**了 —— 但每个线程读到的都是同一份陈旧快照，
于是都算出 `new_rev = 2`。

**结论（这一条比 bug 本身重要）**：

```
资源级锁只保证「不会同时进入」。
「进入之后看到最新状态」必须靠**在事务内重读**。
```

**规则**：任何 mutation 在拿到锁、进入事务后，
必须用 `tree.get(resource_id)` **重新读取**它要修改的对象，
不得直接使用加锁前解析出的领域对象。

### 二、🔒 递增 `revision` 必须**同时**写 `workspace_revisions`

**抓到的 bug**：`rename`（父目录）、`move`（source/dest 父目录）、
`delete`（整棵子树）都只递增了 `revision` **列**，没有写 `revision` **表**。

后果：`revisions_for()` 的历史出现**空洞**（列=5、表里最大=2 ——
中间三次变化查不到）。

**规则**：`_bump_revision()` 是**递增 revision 的唯一方式**，
它把「改列 + 写表」（+ 可选的 `state`）绑成一个动作，
让调用方**没有机会**只做一半。

### 三、🔒 边界检查不得 `pop` 掉资源**自身**的 foreign 边界

**抓到的 bug**：`rename` / `move` 里有这一行：

```python
foreign = tree.foreign_boundaries(fresh, agent_id)
foreign.pop(fresh.resource_id, None)     # ❌
```

那个 `pop` 本意是「自身的边界不算障碍」，但它把
**落在资源自身上的别人的边界**也一起抹掉了。后果：

> A 拥有父目录 `/src`、B 显式拥有 `/src/x.py`
> → **A 可以把 B 的文件改名或搬走**

这与任务书明确禁止的「不让 A 控制目录 / B 控制文件时
A 直接删 B 的文件」是**同一个错误**，只是换了 rename / move 两个入口。

**规则（三个操作统一）**：

> **资源和它的整棵子树里，不能有任何不属于 actor 的 Explicit Lock。**

**一个由此浮现的语义要点**：严格边界规则 ⇒
「A 控制目录、B 控制文件」时 **rename / move 也可能谁都做不了**，
直到责任被理清（B release，或 A 把父目录 handoff 给 B）。

这**不是死锁**，而是**正确的保守语义** ——
系统不替 Agent 决定「谁说了算」，它只拒绝越界，
并让 Agent 用正式的 Release / Handoff 去解决。

同时验证了 move 的两条看似矛盾的规则**并不矛盾**：

| 内部锁的情况 | 结果 |
|---|---|
| 都属于移动者 | 允许，锁**原样保留** |
| 有别人的 | **拒绝**（那等于动别人的资源） |

### 四、🔒 `handoff` 必须校验接收方**存在且是 Participant**

**抓到的 bug**：第一版 `handoff` **完全不校验** `to_agent`：

```python
handoff("W1", "/a.py", "A", "NOT_A_REAL_AGENT", "A")   → success=True
```

后果：资源责任挂在一个**虚构的 holder** 上，
而 Effective Lock 解析会忠实地返回那个名字。

**规则**：新增 `UNKNOWN_AGENT` 错误码，并在 `handoff` 里
**先查存在、再查 Participant** —— 因为两者的正确应对不同：

```
UNKNOWN_AGENT        「你把名字打错了」
NOT_TASK_PARTICIPANT 「这个人存在，但不在这个 Task 里」
```

身份存在性通过**注入的 `agent_exists` 回调**获得 ——
Gateway 仍然不知道 Economy / AgentDirectory。

### 五、文档记录的「底层瓶颈」定位

> **SQLite 是单写者 ⇒ 写事务在基础设施层排队。**

这是**已知的、如实记录的**瓶颈，**不等于** Workspace 的责任模型，
也**不会**让别的 Agent 得到 `LOCKED_BY_OTHER_AGENT`。

因此 20 Agent 测试**不声称**「20 个写操作在物理上同时落盘」——
那是假的。它证明的是：

```
Workspace 层：并发控制是**资源级**的（三层直接证据）
SQLite 层：   写事务是**单写者**，会排队
```

## Consequences

**正面**：

- 不变量检查器 `check_workspace_invariants()` 在每个场景后运行，
  把「不变量」从文档变成了**可执行的东西**
- 5 个真实缺陷被修掉，其中 3 个是**读代码看不出来的**
- 20 Agent 并行稳定通过（5 个场景 + 固定 seed 的混合 workload）
- 同一 File 的三种竞争形态都有**确定**的失败码
- Workspace Core 已成为真正可调用的基础设施（组合根 + CLI demo）

**负面 / 代价**：

- `create` 必须重读父目录 —— 一次额外查询
- 严格边界规则会让某些 rename/move 需要先做责任整理
  （这是**有意的**，但会增加 Agent 的协作步骤）
- `agent_exists` 默认 `None`（不检查）——
  纯 Workspace 语义测试方便，但**组合根必须注入**

**因此不能做的事**：

- ❌ 不许用加锁前的陈旧快照做 mutation
- ❌ 不许只改 `revision` 列而不写 revision 表
- ❌ 不许在边界检查里 pop 掉资源自身的 foreign 边界
- ❌ 不许让 `handoff` 指向未校验的接收方
- ❌ 不许把 SQLite single-writer 说成 Workspace Lock 语义

## Guard

- `tests/workspace_harness.py::check_workspace_invariants`
  —— Identity / Revision / Lock / Path / Atomicity / Audit
- `tests/test_workspace_hardening.py`（69 项）A–K 全覆盖
- `tests/test_workspace_20_agents.py`（15 项）5 个场景
- `tests/test_workspace_composition_guards.py`（18 项）
  —— 组合根 / singleton / Agent 边界 / 架构完整性 / demo 走真实链路
