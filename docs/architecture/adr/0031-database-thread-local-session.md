# ADR-0031：Database 的线程模型 —— 每线程一个 Session

- **状态**：Accepted（**已实现**）
- **相关**：ADR-0012（SQLite 并发）· ADR-0029（Workspace 并发）· ADR-0030（Hardening）
- **背景**：Workspace Core Freeze 前的收尾修复（技术债 #2）

## Context

Workspace Core Hardening 阶段**因为同一个根因产生过两个 flaky 测试**：

```
Database._tx_depth = 0     # 一个整数，全进程共用
Database._conn     = ...   # 一个连接，全进程共用
```

两者都是**实例状态**。多线程下会被交错修改。

原诊断是「`_tx_depth` 不是 thread-local」。但在动手前先做了实验，
结论是：**只把深度改成 thread-local 并不够。**

## Decision

### 一、🔒 划分单位必须是 `(连接, 深度)`，不是单独的深度

如果两个线程共用**同一个连接**：

```
Thread A: BEGIN IMMEDIATE           → 连接上开了一个事务
Thread B: （共享深度读到 0，于是）BEGIN IMMEDIATE
          → ❌ "cannot start a transaction within a transaction"
```

更糟的是：

```
Thread A: BEGIN → 写一些行
Thread B: 直接写（以为不在事务里，其实在）
Thread A: ROLLBACK → **把 Thread B 的写入一起回滚**
```

**「独立的 transaction scope」在共享连接上根本无法成立 ——
无论深度记在哪里。**

所以引入：

```
_ThreadSession = (conn, depth, pid)
```

```
一个线程 → 一个 session → 一个连接 + 一个 depth
```

- 深度天然隔离
- 事务天然隔离（每个连接有自己的 BEGIN / COMMIT / ROLLBACK）
- **不需要任何全局锁** —— 线程之间根本不在同一个事务上下文里

> ⚠️ 注册表用了一把 `threading.Lock`，但它**只保护 session 列表的增删**
> （记账），**不保护事务** —— 不是在给并发"上锁"。

### 二、⚠️ 内存库结构上只能单连接（实测，不是推断）

最直觉的做法是「所有库都每线程一个连接」。对文件库成立；
对 `:memory:` **不成立**：

> SQLite 的 `:memory:` 是**每个连接一个独立的库**。

标准答案是 shared-cache URI。**实测它在并发写下不成立**：

```
20 线程 × 5 次 BEGIN IMMEDIATE + INSERT
→ 17 个线程抛 database table is locked
→ 最终只写进 25/101 行
```

原因：shared-cache 用**表级锁**，冲突返回 `SQLITE_LOCKED` ——
而 `SQLITE_LOCKED` **不受 `busy_timeout` 影响**，等多久都不会好。

**因此**：

| | 线程模型 | 保证来自 |
|---|---|---|
| **文件库**（生产：`data/economy.db`） | 每线程一个 session | **本 ADR（基础设施层）** |
| **内存库**（测试） | 全进程一个 session | 测试环境约束（meta-guard） |

这两件事被**明确分开**：内存库的单线程约束由测试守卫保证，
**不再承担「防止 Database 本身出错」的职责**。

### 三、PID 感知保留，且改成行为断言

fork 之后子进程继承的连接不能用。session 里记 `pid`：
PID 相同 → 复用；不同 → 重建并告警。

⚠️ 原来有 3 条守卫是**查源码字符串**的
（`assert "_owner_pid" in source`）。重构后保护的行为**完全没变**，
守卫却**假红**了 —— 典型 ADR-0018 反例。
已全部改成**行为断言**（改 session 上的 pid，断言连接被重建）。

## Consequences

**正面**：

- **判别性测试**：反向验证（临时还原旧实现）确认新测试**真的会红**
  - 深度测试：旧模型得到 `{'B': 2, 'A': 1}` —— B **递增了 A 的计数器**
  - 回滚测试：旧模型下 A 的 ROLLBACK **抹掉了 B 已提交的写入**
- 写事务在 SQLite 层仍然串行（单写者），但**20 线程全部成功**
  （`busy_timeout` 负责等待）
- 读事务可以**真正并发**（WAL 下读不阻塞写）——
  用 `Barrier` 放在事务内部证明，**不用计时**
- 不再需要「每线程一个 `Database` 对象」这种调用方纪律

**负面 / 代价**：

- 文件库下每个线程多一条连接（fd 成本）
- `close()` 要遍历所有 session
- 内存库仍是单线程（**无法**用基础设施解决，已如实记录）

**因此不能做的事**：

- ❌ 不许只把 `_tx_depth` 改成 thread-local 而共用连接
- ❌ 不许用一把全局锁串行化事务来"解决"线程安全
- ❌ 不许让内存库假装支持并发（shared-cache 已证伪）
- ❌ 不许用「查源码字符串」的方式守卫 PID 感知

## Guard

- `tests/test_database_thread_safety.py`（20 项）
  - 深度按线程隔离 · 嵌套语义不变 · 每线程一条连接 ·
    **20 线程共享同一个 `Database` 实例** · 回滚隔离 ·
    未提交写入对其他线程不可见 · 并发读事务重叠（Barrier）· 重复 5 轮稳定
  - 内存库约束的三条事实记录（含 shared-cache 实测数据）
- `test_workspace_composition_guards.py::TestConcurrencyTestsUseFileDatabase`
  —— 定位已改为**测试环境约束**，并新增两条：
  断言基础设施测试存在 · 结构性断言 `Database` 用 `threading.local()`
  且**没有**全局事务锁
