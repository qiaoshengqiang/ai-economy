# ADR-0029：Workspace Core v1 的并发模型与「责任变化不产生 Revision」

- **状态**：Accepted（**已实现**）
- **时间**：Workspace Core 第一阶段
- **相关**：ADR-0021（锁语义）· ADR-0026（Lock 层级）· ADR-0027（设计简化）· ADR-0028（Read / Communication）
- **规范**：[`WORKSPACE_CORE_V1_DESIGN.md`](../implementation/WORKSPACE_CORE_V1_DESIGN.md)

## Context

Workspace、Resource、Controller、Lock、Handoff、Revision、Audit、
Event、Read/Visibility 都已冻结，任务书授权实现第一阶段。

实现过程中浮现了**三个必须记录的架构决定** —— 它们不是设计阶段
能预见的，而是写代码 + 写测试才暴露出来的。

## Decision

### 一、并发模型：资源级，不是全局串行

四层同时起作用：

| 层 | 机制 | 保证什么 |
|---|---|---|
| ① 资源级锁 | 进程内 `(workspace_id, resource_id)` → `RLock` | 同一 File 串行；不同 File 并行 |
| ② 命名空间锁 | 结构操作共用**父目录**的锁 | Namespace 一致，无中间损坏 |
| ③ 多资源有序获取 | move 的 source/dest 锁**先排序再获取** | 相反方向的 move 不死锁 |
| ④ SQLite 写事务 | `BEGIN IMMEDIATE` | 「检查 + 修改」原子 |

🔒 **禁止 Global Gateway Lock** —— 那会让所有 Workspace 排队。

### 二、⚠️ 底层瓶颈必须如实记录，不得伪装成 Workspace 锁语义

> **SQLite 是单写者。** 不同 Workspace 的**写**在底层仍会排队。

**这是基础设施瓶颈，不是 Workspace 语义** ——
它**不会**让 W2 得到 `LOCKED_BY_OTHER_AGENT`。

任务书明确要求「如果有这种瓶颈，请在代码/文档中明确，
而不要伪装成 Workspace Lock 语义」。本 ADR 就是那条明确记录。

### 三、🔒 责任变化**不产生**新的 Revision

这是**从任务书 §27 的流程差异推出的结论**，不是拍脑袋：

```
Mutation:        ... → Update State → **Create Revision** → Audit → Event
Responsibility:  Validate → Change Responsibility → Audit → Event
                                                   ↑ 没有 Revision 步骤
Read:            简化
```

**Revision 是「内容 / 结构」的版本事实。**
「谁负责」变了、内容没变 —— 让 `revision` 递增会导致：

> B 接手后必须先 `read()` 才知道能改哪个版本，
> 而那次 `read` 其实什么新内容都没有。

**因此 `handoff()` / `release()` / `force_unlock()` 不改 `revision`。**

> ⚠️ 第一版实现**做错了**（三个操作都递增），
> 是测试把它抓出来的。这条记录保留这个修正过程。

### 四、`Effective Lock` 永远派生，但原子性由事务边界保证

任务书 §48 要求「Effective Lock 优先运行时派生，不维护两份事实」，
§36–§39 又要求「检查 + 修改原子」。两者看似拉扯。

**解法**：`Effective Lock` **不持久化**（无表、无列、无缓存），
但派生过程必须在**同一个 SQLite 写事务**内完成，且该事务同时覆盖：

```
① 解析 Effective Lock（读 resources 父链）
② 校验 base_revision
③ 写入新 revision
④ 写 audit + event
```

→ 既没有第二份事实，又有原子性。

### 五、边界检查必须「遇到第一条边界就停」

`is_within_foreign_boundary` 第一版继续向上遍历，
于是 B 独立负责 `data/processed/` 时，仍被判成「在 A 的边界里」——
**B 连自己的文件都动不了**。

**正确语义**：沿 parent 链从近到远，**遇到第一条显式 Directory Lock 就停**。

> 这正是「**Explicit 优先于 Inherited**」在**边界检查**上的应用：
> **更近的边界决定归属，外层的边界被内层的遮住。**

### 六、`force_unlock` 作用于**显式**责任

只靠继承获得责任的文件（A 持有 `data/` → `data/a.csv` 的锁来自继承）
**没有自己的显式锁** → 「解锁 a.csv」没有可撤销的对象。

🔒 **本轮不自行发明**「解锁继承责任」的语义
（那需要新概念，例如负数显式锁 → 属于扩大模型）。
返回明确错误 + 记为 **Open Question**。

## Consequences

**正面**：

- 8 个验收场景全部真实跑通，**包括两个线程用 `Barrier` 真并发竞争同一 File**
- 同一 File 单 Holder 由 **schema 主键**保证，不靠应用层自觉
- Revision Race 确定地失败（`STALE_REVISION`），且有测试验证败者不留痕迹
- Audit 与 Event 职责清晰：失败留 Audit、不留 Event
- 资源级并发 ⇒ W1 的锁冲突不阻塞 W2（有测试）

**负面 / 代价**：

- `Effective Lock` 每次操作都要**向上遍历父链** → 深树有性能成本
- SQLite 单写者限制写吞吐（已如实记录）
- `force_unlock` 对继承责任无解（Open Question）
- 6 个「设计阶段」守卫按授权过渡 —— 过渡本身有维护成本

**因此不能做的事**：

- ❌ 不许 Global Gateway Lock
- ❌ 不许把 SQLite 单写者伪装成 Workspace 锁语义
- ❌ 不许让 handoff / release / force_unlock 递增 revision
- ❌ 不许为 Effective Lock 建表 / 建列 / 加缓存
- ❌ 不许在边界检查里「继续向上找 foreign」
- ❌ 不许自行发明「解锁继承责任」的语义

## Guard

实现阶段的守卫：
- 8 个验收场景 + 真实并发竞争（线程 + Barrier）
- Audit 含失败尝试 / Event 不含失败
- Event 不含文件内容
- 读不产生 Event / 不写 Audit
- 不泄漏宿主机物理路径
- 设计阶段守卫已过渡（禁的是被移除的概念，不是所有 Workspace 类）
