# ADR-0020：Task Workspace 采用共享资源模型，Permission 与 Write Lock 分离

- **状态**：Accepted（架构定义；**实现为 FUTURE**）
- **时间**：Task Workspace Architecture Definition
- **相关**：ADR-0011（事务可重入）· ADR-0012（BEGIN IMMEDIATE）· ADR-0014（消息认领租约）· ADR-0019（Shared Context 与 Alignment 分离）
- **规范**：[`TASK_WORKSPACE.md`](../TASK_WORKSPACE.md)

## Context

Step 4A 引入了 `TaskWorkspaceRef` —— 一个只有 `workspace_id` 的类型。
它**刻意没有任何 I/O、权限、路径概念**：
「概念如果不落到代码里，就会在文档里漂」。

但到了要设计真正的 Task Workspace 时，有两条根本不同的路：

```
路 A（Git 式）：
    Task
    ├── Agent A branch/worktree
    ├── Agent B branch/worktree
    └── merge

路 B（共享资源）：
    Task
    └── Task Workspace
          ├── shared resources
          ├── Permission
          └── Write Lock
```

这个选择**不能推迟**，因为它决定了：

- 冲突在**什么时候**被发现（写入时 vs 合并时）
- 需要哪些机制（锁 vs diff/merge/conflict resolution）
- 资源是不是「稀缺的」

同时还有一个相关的问题是：

> 「谁能访问这个资源」和「现在谁在写这个资源」
> 是不是同一件事？

## Decision

### 一、采用共享资源模型，明确否决 Git 式分支模型

**Task Workspace 默认采用共享资源模型。**

第一版**不引入**：Git branch · worktree · merge · rebase · branch conflict resolution。

**理由**：

| | 共享资源模型 | Git 式分支模型 |
|---|---|---|
| 冲突发现 | **写入时**（有人持锁，别人等待） | 合并时 |
| 冲突代价 | 等待 / 明确的锁冲突 | 合并冲突，可能很大 |
| 需要的机制 | Permission + Write Lock | branch + merge + diff + conflict resolution |
| 与「Agent 是经济主体」的关系 | 资源是**稀缺的**，需要协商 | 每个 Agent 无限分支 —— 不像稀缺资源 |
| 第一版复杂度 | 低 | 高 |

**注意这**不**代表 Git 永远不出现。**
它代表「Task Workspace 的**基础语义**不是分支」。
未来 Git 可能作为**某种资源的 backend** 出现，
但不改变上层模型。

### 二、Permission 与 Write Lock 是两个不同机制

```
Permission  决定 Agent 是否有资格访问资源
Write Lock  决定当前哪个 Agent 对该可写资源拥有排他写入权
```

**`Permission ≠ Write Lock`**

组合真值表：

| Permission WRITE | 持有 Write Lock | 能写吗 |
|---|---|---|
| ❌ | — | ❌ |
| ✅ | ❌ | ❌ **（锁不在自己手里）** |
| ✅ | ✅ | ✅ |

**唯一的例外**是下面的「新资源创建原子规则」。

### 三、可并行读，写必须独占

```
A/B/C → READ X   ✅ 三者都行
A     → WRITE X  ✅（需 WRITE Permission + 持有有效锁）
B/C   → WRITE X  ❌
```

### 四、新建资源自动获得 Write Lock，且创建与获锁原子

```
CREATE /src/parser.py
  ↓
Permission Check
  ↓
Create Resource + Acquire Write Lock     ← 一个原子操作
```

**明确禁止**：

```
❌ CREATE → （竞争窗口）→ 以后再 LOCK
❌ created but unlocked
```

这是「Permission ≠ Lock」的**唯一例外**，
理由：**新资源在被创建的那一刻还没有任何写入者**，
所以「创建者即锁主」不违反互斥性 —— 它恰恰是避免竞争窗口的方式。

**并发创建同一资源时只能有一个成功**，
应复用项目已验证的 CAS 模式（见 ADR-0012）。

### 五、Agent 不直接访问文件系统，Local/Remote 共用同一抽象

```
Agent → Workspace API → Workspace Service
      → Permission → Write Lock → Backend → Resource
```

**Agent 不直接获得其他机器的文件系统路径或底层文件系统权限。**
Agent 不应该知道 `/Users/...`、`/mnt/...`、`/var/...`。

因此：

- `workspace_id` 是 **Logical Workspace Identity**（不是路径/URL/权限/句柄）
- **Resource Identity 与物理路径解耦**（倾向 `(workspace_id, logical_resource_id)`）
- **Local 与 Remote 使用同一个 Workspace API**

### 六、不采用「本地完成后再同步」作为基础模型

```
❌ local copy → edit → sync          （不是基础模型）
✅ logical Workspace + controlled access + backend
```

「本地编辑再同步」未来可作为 cache / offline mode /
batch synchronization / performance optimization 研究，
但不改变核心语义。

### 七、Task 隔离，且不是全局公共目录

不同 Task 默认隔离。参与 Task-001 **不会**让 Agent 看见 Task-002 的 Workspace。

可以有 **Task Workspace Root**，但那只是**基础设施存储根**，
不是 Agent-accessible 的全局工作区。

### 八、Workspace Lifecycle ≠ Task Lifecycle

Workspace 将来可以有 `PROVISIONING / READY / ARCHIVED`，
但**那不是 Task Lifecycle**。

**禁止**：

```
❌ Task.status = SUBMITTED
   Workspace.status = IN_PROGRESS
```

### 九、Lock Recovery 明确**不照搬** Message Claim Lease

调查 ADR-0014 后的结论：**两者语义不同**。

| | Message Claim Lease | Workspace Write Lock |
|---|---|---|
| 保护 | 一条消息不被重复处理 | 一个资源不被并发写入 |
| 过短的失败模式 | 重复处理（幂等可吸收） | **丢失写入 / 静默数据损坏** |
| 过长的失败模式 | 消息卡住（可见） | 资源被阻塞（可见） |
| 持有者是否活跃 | 是（worker 在处理） | **不一定**（可能在等 LLM 响应） |

**关键差异**：持有写锁的 Agent 可能**长时间没有 CPU 活动**
（等 LLM、在想、在等人）。「没有活动」**不等于**「已崩溃」——
而基于时间租约的恢复机制**恰恰依赖这个假设**。

Message Claim 没有这个问题。

**因此本阶段把它记为 `OPEN DESIGN QUESTION`**，
列出 6 个候选方向（时间租约 / 显式释放 / 租约+续租 / 崩溃检测 /
乐观并发 / 人工解锁）但**不选择**。理由中有一条很硬：

> 候选「租约 + 续租」需要**心跳**，
> 而**心跳在当前阶段是被明确禁止的**。

## Consequences

**正面**：
- 冲突在**写入时**被发现，代价低且明确
- 不需要 branch/merge/diff/conflict-resolution 这一整套机制
- 资源是稀缺的 —— 与「Agent 是经济主体」的模型一致
- 上层 Agent 的代码不会因为 backend 从 local 变 remote 而重写
- 「谁有资格」和「现在谁能写」分开 → 授权模型可以独立演化

**负面 / 代价**：
- 需要实现 Permission + Write Lock 两套机制（比「无锁共享」复杂）
- **锁的崩溃恢复语义未定**（§16）——这是进入实现阶段前的**硬阻塞项**
- 写操作会**阻塞**其他 Agent（可见的等待，而不是静默的并行）
- 需要 `OPEN DESIGN QUESTIONS` 全部解决后才能实现

**因此不能做的事**：
- ❌ 不许把 Task Workspace 做成 Git 分支/worktree 模型
- ❌ 不许把 `workspace_id` 做成文件系统路径
- ❌ 不许让 Agent 直接访问物理路径或远程文件系统
- ❌ 不许把 WRITE permission 等同于持有锁
- ❌ 不许留下「created but unlocked」的中间状态
- ❌ 不许给 Workspace 加一套和 Task 并列的状态机
- ❌ 不许因为 Message Claim Lease 存在就照搬到 Write Lock
- ❌ 不许把 `core/workspace.py`（Agent Private Workspace）改造成 Task Workspace

## 本阶段**没有**决定的事

明确记录为 Open Design Question（详见 [`TASK_WORKSPACE.md`](../TASK_WORKSPACE.md) §21）：

1. Workspace 何时创建（Accepted vs lazy provision）
2. Workspace lifecycle 的完整状态集
3. Task 完成后是否冻结 / 何时归档
4. **Lock lease / 崩溃恢复**（§16 的 6 个候选未选）—— **硬阻塞项**
5. 多资源锁
6. 目录锁语义
7. Agent 中途加入 / 退出
8. 权限集合与授予规则
9. Artifact 设计
10. 逻辑资源 id 的确切形式
11. 并发创建资源的具体 CAS 形态
12. Workspace Root 的物理布局

> 规范要求：**已知的写确定，未知的明确标 OPEN。**
> 不要因为文档缺少答案而写出一个自己猜的实现。

## 本阶段状态

**Task Workspace 仍然是 `FUTURE`。** 本次只完成架构定义。

项目阶段保持 **`Post-Step-4F / Pre-Task-Workspace`**。

下一步不是「开始写 Workspace 代码」，而是 **Workspace Readiness Gate** ——
先解决 Open Design Question #1 / #4 / #7，再进入实现。

## 备选方案

| 方案 | 为什么不选（**当前**） |
|---|---|
| Git 式分支 + merge | 冲突代价高；需要一整套 diff/merge 机制；与「资源稀缺」的模型不符 |
| 无锁共享（谁都能写） | 静默覆盖；与 §2.3 矛盾 |
| 只有一个「Permission」概念（有 permission 即可写） | 无法表达「有资格但现在锁在别人手里」 |
| 乐观并发（不锁，写时检测） | 与「写必须独占」矛盾；见 ADR-0020 §16.4 候选 E |
| 照搬 Message Claim Lease 做锁恢复 | 失败模式不同（见 §16.2）—— 会造成静默数据损坏 |
| 现在就决定所有 OPEN 问题 | 缺少实现阶段的事实依据；猜错比晚定更贵 |

## Guard

新增文档一致性守卫（含反向测试），钉住：

- Task Workspace 仍标为 `FUTURE`（不得写成 CURRENT）
- 五个边界：Task Core ≠ Workspace · Shared Context ≠ Workspace ·
  Private Workspace ≠ Task Workspace · Permission ≠ Lock ·
  Workspace ID ≠ Filesystem Path
- `TASK_WORKSPACE.md` 不得声称 Permission Engine / Lock Manager /
  Backend / Artifact Store 已实现
- 生产代码里**没有** workspace 权限 / 锁 / backend 的实现
