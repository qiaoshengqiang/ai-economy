# ADR-0019：Task Shared Context 与 Alignment 分离

- **状态**：Accepted
- **时间**：Step 4F
- **相关**：ADR-0008（议价不调 LLM）· ADR-0010（Task 乐观锁）· ADR-0012（BEGIN IMMEDIATE）· ADR-0014（认领租约）· ADR-0018（Guard 标准）

## Context

到 Step 4E 为止，项目已经解决了「任务怎么定义、怎么谈、怎么执行、
消息怎么送达、怎么防并发覆盖」。

但暴露出一个更高层的问题：

> **多 Agent 即使在数据库和协议层完全一致，
> 也可能对「这个任务现在是什么、已经决定了什么、
> 什么只是猜测、下一步做什么」产生不同理解。**

协议保证的是**消息送达**，它**不保证理解一致**。

三条具体症状：

| 症状 | 表现 |
|---|---|
| 历史 ≠ 当前状态 | 500 条消息里散落 12 个决定和 3 次改主意，没人说得清现在用哪个 |
| 说过 ≠ 确认过 | A 说「我觉得用 B」，B 没回应；A 以为定了，B 以为还在讨论 |
| 猜测 ≠ 事实 | A 推测「那个 API 支持幂等」，B 当成事实去写代码 |

Step 4E 只做了**设计**（Constitution + TASK_SHARED_CONTEXT.md），
零实现。Step 4F 要实现最小可运行原型。

## Decision

### 一、三层职责分离

```
core/task_shared_context.py     纯领域：数据结构 + apply_change()
core/task_context_store.py      持久化 + 乐观锁 + 事务边界
services/task_alignment_service.py   业务规则：谁能做什么
```

**为什么事务边界在 store 而不是 service：**
第一版把 `with self.db.transaction()` 写在 service 里，
于是 service 必须 `import storage.database` ——
依赖图变成 `services → storage`，违反 AGENTS.md 的明确约定。

把事务下移到 store 之后：
- service 只依赖 `core`（不知道 Database 存在）
- 事务边界只在一个文件里需要被理解

### 二、三张表，各自规则不同

| 表 | 规则 | 为什么 |
|---|---|---|
| `task_shared_contexts` | **会 UPDATE**（只改 `current_revision`） | 它是「当前指针」，CAS 的落点 |
| `task_context_revisions` | **只 INSERT / SELECT** | 历史不可改写：「18 取代 17」≠「17 可删」 |
| `task_context_changes` | **UPDATE status 一次**（PROPOSED → 终态），**不 DELETE** | 提议有生命周期；但被拒绝的提议要留着 |

Revision 存**物化全文**（JSON）而不是 diff：
读「Revision 5 长什么样」应该是 O(1)，不该回放 5 条 diff。
代价是存储冗余，但 Shared Context 是**小对象**。

### 三、Agent 不能直接改 Context

```
禁止：  Agent ──直接 UPDATE──▶ Shared Context
必须：  Agent ──ContextChange──▶ Alignment ──▶ Revision
```

**结构上保证**：`task_shared_contexts` 表里**没有内容列**，
只有 `current_revision`。内容只能通过 `apply_change()`
产生新版本。所以「绕过 Alignment 直接改内容」**做不到**。

### 四、`TaskSharedContext` 没有 `status` 字段

Task Core 是生命周期和领域状态的**唯一权威**。

要展示任务状态时，`ContextSnapshot.domain_state` **现场读 Task Core**：

```
Task Core: status = SUBMITTED
Context  : 不存 status，只在快照里引过来（domain_state_source="task_core"）
```

于是 Task 状态一变，下一次 snapshot 立刻反映 —— 不可能漂移。

### 五、对齐最低要求：DECIDE 不能自批

- **声明类**（FACT / ASSUME / QUESTION / ASSIGN）：可自批
  —— 「我观察到 X」「我假设 Y」本来就是个人声明
- **决定类**（DECIDE）：批准人必须是**非提案人**
  —— 「一个 Agent 自己说了算，不算共同决定」

**不实现**投票 / quorum / CRDT / blockchain consensus（规范 §9 禁止）。

### 六、并发：沿用 TaskStore 已验证的 CAS

```sql
UPDATE task_shared_contexts
   SET current_revision = ?
 WHERE task_id = ? AND current_revision = ?     ← 关键
```

影响行数 0 → `ConcurrentContextModification`。

**顺序必须是先 UPDATE 再 INSERT** ——
反过来冲突时会留下一条「从没被 current 指向过」的孤立历史版本。

**不自动 retry / merge / rebase / LLM merge**（规范 §13）：
那会让「共同认知」变成一个自己会变的东西。

### 七、`artifact_refs` 与 `result_ref` 共用同一条安全规则

Step 4D 把「不许是路径、不许是 URL」写在 `TaskResultRef.__post_init__` 里。
4F 需要同一条规则。**抽到 `core/logical_ref.py`**，
`TaskResultRef` 改为调用它 —— 行为完全不变（有回归测试）。

复制两遍的话，改了 A 忘了 B 就留下一个安全漏洞。

## Consequences

**正面**：
- Task 第一次有了正式的、可验证的「共同认知层」
- Said ≠ Confirmed 从原则变成了**结构约束**
- Shared Context 不可能成为第二套 Task Core（没有内容列、没有 status）
- 复用现有 Database / transaction / CAS / 领域错误模式，零新基础设施

**负面 / 代价**：
- `task_context_changes` 不是纯 append-only（有 status 转移）——
  这是**必须的**，因为提议有生命周期。守卫只钉「不许 DELETE」
- Revision 存物化全文有冗余（可接受，对象小）
- 多了一层 `TaskContextStore`，和 `TaskStore` 平行

**因此不能做的事**：
- ❌ 不许给 `task_shared_contexts` 加内容列或 status 列
- ❌ 不许让 Message 自动写入 Context（规范 §11）
- ❌ 不许自动 retry / merge / rebase / LLM merge
- ❌ 不许做投票 / quorum / 分布式共识
- ❌ 不许把 `AssignmentRecord` 做成 Scheduler
- ❌ 不许让 `services` 直接依赖 `storage`
- ❌ 不许为了「更原子」就绕开 `transaction()` 自己管提交

## 顺带修掉的一个真实缺陷

`Database.execute()` 原来写的是：

```python
conn = self.connect()
with conn:                      # ← 无条件 commit
    return conn.execute(sql, params)
```

`with conn:` 在退出时**总是提交**。所以任何人在显式事务里调用它：

```python
with db.transaction():
    db.execute("UPDATE a ...")   # ← 这里就提交了
    db.execute("UPDATE b ...")   # ← 之后失败
    # 回滚 → 但 a 已经提交，回不去
```

→ **半成功状态**，而且不报错。

这个缺口一直存在，只是之前没人踩到：`TaskStore` 在事务里总是用
`with db.transaction() as conn` 拿到的那个 conn，从不调 `db.execute()`。

4F 的 `decide_change` 第一次踩上了，表现为
「change 是 ACCEPTED，但对应的 revision 没提交」。

**修法**：`execute()` 改成事务感知 —— 深度 > 0 时不提交，
边界归外层事务。事务外的调用方行为完全不变。

## Guard

新增 `tests/test_shared_context_guards.py`（52 项），
覆盖规范 §25 要求的五项检查：

| 检查 | 内容 |
|---|---|
| Boundary | Shared Context ≠ Task Core / Message History / Private Memory / Task Workspace / Permission |
| Dependency | 没有新的违规依赖方向；事务边界只在 store |
| Source of Truth | 没有第二套生命周期；reject 不改版本；Snapshot 是派生的 |
| Concurrency | CAS 条件存在；UPDATE 在 INSERT 之前；append-only；无自动重试 |
| Naming | Private Workspace 没被重构成 Task Workspace |

**10 项反向测试全部通过**（把违规注入进去，守卫确实变红）。

反向测试中**发现并修掉了三个守卫缺陷**：

1. `test_update_before_insert` 用 `ast.unparse` 查 "UPDATE" ——
   而函数文档里为了解释这个顺序**本身就提到了 UPDATE**，
   于是 `find("UPDATE")` 永远命中文档，断言恒成立（假绿）。
   改用去文档字符串的取码函数。
2. `test_revisions_and_changes_are_append_only` 从**表名**推常量名
   （`TASK_CONTEXT_REVISIONS`），但代码用的是
   `TABLE_CONTEXT_REVISIONS` —— 模式永远匹配不到（假绿）。
   改成遍历真实 SQL 字符串 + 同时认常量占位符和字面表名。
3. `test_no_filesystem_access` 用 `"open(" in source` ——
   匹配到了 `AssumptionRecord.is_open()`（子串误报，ADR-0018 记过 4 次）。
   改成 AST 调用点检测。

另外，第 2 条守卫的修正过程还暴露出：**`task_context_changes` 不是纯
append-only**（它有 status 转移）。这让我修正了 store 里写错的文档说明 ——
守卫不仅防代码漂移，也防文档说谎。

## 备选方案

| 方案 | 为什么不选 |
|---|---|
| 把 Context 塞进 `tasks` 表的一个 JSON 列 | 无法保留修订历史，也无法区分「提议过但没通过」 |
| 4 个独立状态源（Context / Revision / Snapshot / Memory 都能改） | 规范 §14 明确禁止 |
| 每次裁决新插一行 change | 查询时要先推理「哪行最新」，自找麻烦 |
| 用 LLM 从消息里发现「可能的决定」 | 规范 §11：必须经 Proposal → Alignment，不能绕过 |
| 投票 / quorum | 规范 §9 明确禁止；需要的是 authority + provenance + revision |
| 给 service 传 `Database` | 会引入 `services → storage` 依赖 |
