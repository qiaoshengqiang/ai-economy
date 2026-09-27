# ADR-0012：写事务使用 `BEGIN IMMEDIATE`

- **状态**：Accepted
- **时间**：Workspace Phase 0
- **相关**：ADR-0011（事务可重入）

## Context

Step 4D.1 引入了 `TaskStore.save()` 的 compare-and-set：

```sql
UPDATE tasks SET ..., version = version + 1
 WHERE task_id = ? AND version = ?
```

在**单进程**下它完全正确，1915 → 2140 项测试全绿。

Workspace Phase 0 用真正的 `multiprocessing.Process` 验证时发现：
两个进程并发 `save()` 时，**失败者拿到的不是
`ConcurrentTaskModification`，而是 `OperationalError: database is locked`**。

原因：`transaction()` 用的是 `BEGIN`（= `BEGIN DEFERRED`），
它**先只拿读锁**，等到第一次写才尝试升级：

```
A: BEGIN → SELECT（拿 SHARED）→ UPDATE（想升 RESERVED）
B: BEGIN → SELECT（拿 SHARED）→ UPDATE（想升 RESERVED）
        ↑ 两边都持 SHARED，都在等对方释放 → 死锁
```

SQLite 检测到这种升级死锁后会**立刻**返回 `SQLITE_BUSY`，
**不理会 `busy_timeout`**（等下去只会永远等）。

**后果**：并发保护在真实多进程下**被静默绕过**。
而单进程测试**完全测不出来** —— 这是关键。

## Decision

`Database.transaction()` 增加 `write: bool = True` 参数：

- `write=True`（默认）→ `BEGIN IMMEDIATE`（事务开始就拿写锁）
- `write=False` → `BEGIN`（延迟读事务，可并发读）

默认 `True`，因为本项目的 `transaction()` **绝大多数**用于写
（`TaskStore` 和 `recorder` 的每个写方法都自己开事务）。

`TaskStore` 的三个**纯读**方法显式传 `write=False`：
`executions_for` · `find_execution_by_message` · `count_executions`。

## Consequences

**正面**：
- 并发保护在真实多进程下**真正生效**（失败者拿到正确的领域异常）
- 不存在「先读后升级」这一步，升级死锁**结构上不可能**
- 只读事务仍可并发

**负面 / 代价**：
- 写事务之间**完全串行**。这是 SQLite 单写者模型的固有特性，
  不是本决定引入的
- 默认 `True` 意味着「忘了传 `write=False`」的代价是
  「读也串行化了」—— 慢一点，但**不会错**

**因此不能做的事**：
- ❌ 不许把默认值改成 `False`（那会让随机 `database is locked` 回来）
- ❌ 不许把 `BEGIN IMMEDIATE` 改回 `BEGIN`

## 备选方案

| 方案 | 为什么不选 |
|---|---|
| 保留 `BEGIN`，对 `SQLITE_BUSY` 做重试 | 那是**自动重试**，本项目明确禁止（会掩盖冲突、可能重复状态变化） |
| 全部用 `BEGIN EXCLUSIVE` | 比 `IMMEDIATE` 更严格，会阻塞读者。`IMMEDIATE` 已经够了 |
| 换存储引擎 | 远超本阶段范围 |

## 证据

- `tests/test_multiprocess_database.py` —— 2/4 个真进程的 CAS 竞争
- `tests/test_multiprocess_guards.py::TestGuard10WriteTransactionsAreImmediate`
  —— 反向测试：改回 `BEGIN` → 守卫变红
- 反向测试时发现**守卫本身有假绿**（查原文，而文档里为了解释这个修复
  大量提到了 `BEGIN IMMEDIATE`），改成查纯代码后才真正抓到

## 顺带发现

同一次多进程验证还发现：`PRAGMA journal_mode = WAL` 是**排他操作**，
却在每个连接上无条件执行 → 多进程同时连接时互相撞成
`database is locked`，**在 `connect()` 阶段就炸**。

修法：先读当前模式，只有不是 WAL 时才切换。
同一组守卫（Guard 10）也钉住了这一条。
