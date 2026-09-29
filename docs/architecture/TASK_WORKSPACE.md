# TASK_WORKSPACE.md — Task Workspace 正式架构定义

> **状态：`FUTURE` —— 本次只做架构定义，零实现。**
>
> 这份文档定义 Task Workspace 的**正式模型与边界**。
> **它不描述已经存在的代码。** 当前代码里只有
> `TaskWorkspaceRef` —— 一个 `workspace_id` 字符串。
>
> 上游：
> [`AGENTS.md`](../../AGENTS.md)（入口）
> → [`ARCHITECTURE_CONSTITUTION.md`](ARCHITECTURE_CONSTITUTION.md)（长期 Source of Truth）
> → **本文档**（Task Workspace 专项规范）
> → [`adr/0020-task-workspace-shared-resource-model.md`](adr/0020-task-workspace-shared-resource-model.md)

| | |
|---|---|
| **文档状态** | 长期维护 |
| **实现状态** | **`FUTURE`** —— 当前只有 `TaskWorkspaceRef`（逻辑引用） |
| **当前阶段** | `Post-Step-4F / Pre-Task-Workspace` |
| **最后更新** | Task Workspace Architecture Definition |

---

## 0. 怎么读这份文档

### 0.1 四态标注（沿用 Constitution §0.2）

本文档每一条陈述都归入四态之一：

| 标记 | 含义 |
|---|---|
| **`SETTLED`** | **架构已正式确定**。实现时必须遵守，不得重新发明。 |
| **`OPEN`** | **尚未决定**。列出了问题与候选方案，**但没有拍板**。 |
| **`FUTURE`** | 方向明确，尚未设计。 |
| **`NOT IMPLEMENTED`** | 当前一行代码都没有。 |

> ⚠️ **`SETTLED` 不等于 `IMPLEMENTED`。**
> 本文档里**全部**内容都是 `NOT IMPLEMENTED` ——
> 区别在于其中一部分的**设计**已经确定（`SETTLED`），
> 另一部分连设计都还没有（`OPEN`）。

### 0.2 一句话

> **Shared Context = 我们共同知道什么。**
> **Workspace = 我们共同操作什么。**

---

## 1. Definition

### 1.1 是什么（`SETTLED` / `NOT IMPLEMENTED`）

**Task Workspace 是一个 Task 的资源边界。**

它回答的问题是：

> 这个任务**实际**在操作哪些文件、目录、数据、中间产物？

它属于 **Task**，不属于任何 Agent。

### 1.2 不是什么

| ❌ 不是 | 为什么 | 状态 |
|---|---|---|
| 一个共享文件夹 | 它是**受控边界**，不是目录。见 §9–§11 | `SETTLED` |
| Git 分支 / worktree 集合 | 见 §2.1 | `SETTLED` |
| Task Shared Context | 一个说「知道什么」，一个说「操作什么」 | `SETTLED` |
| Agent Private Workspace | 一个是「我的」，一个是「这件事的」 | `SETTLED` |
| Task Core 的第二份状态 | 见 §3 | `SETTLED` |
| 全局公共目录 | 见 §7.3 | `SETTLED` |
| 文件系统路径的别名 | `workspace_id` 是逻辑身份 | `SETTLED` |

### 1.3 一句话定位

```
Task Shared Context   =  我们共同知道什么
        ↓ logical reference
Task Workspace        =  我们共同操作什么
        ↓ controlled access
Permission            =  谁可以操作
        ↓
Write Lock            =  当前谁可以写
        ↓
Backend               =  资源实际上在哪里
```

---

## 2. Core Principles

> **这一节是本文档最重要的部分。**
> 以下 10 条是**已经正式确定**的架构决定，实现时不得重新发明其他模型。

### 2.1 共享资源模型，而非 Git 式独立分支（`SETTLED`）

**Task Workspace 默认采用共享资源模型。**

```
✅ 采用的模型：
    Task
    └── Task Workspace
          ├── shared resources
          ├── Permission
          └── Write Lock

❌ 不采用的模型：
    Task
    ├── Agent A branch/worktree
    ├── Agent B branch/worktree
    └── merge
```

Agent **可以共同访问同一个 Task Workspace**。

**第一版不引入**：

- ❌ Git branch
- ❌ Git worktree
- ❌ merge
- ❌ rebase
- ❌ branch conflict resolution

> **Git 不属于本阶段。** 它不是「以后再补」的细节 ——
> 它是**被明确否决的基础模型**。选它意味着整个资源模型
> （锁、权限、冲突）都要重写。

**为什么选共享模型：**

| | 共享资源模型 | Git 式分支模型 |
|---|---|---|
| 冲突发现时机 | **写入时**（有人持锁，另一个人等待） | 合并时 |
| 冲突代价 | 等待 / 明确的锁冲突 | 合并冲突，可能很大 |
| 需要的机制 | Permission + Write Lock | branch + merge + diff + conflict resolution |
| 与「Agent 是经济主体」的关系 | 资源是**稀缺的**，需要协商 | 每个 Agent 无限分支，不像稀缺资源 |
| 第一版复杂度 | 低 | 高 |

> **注意**：这不代表 Git 永远不出现。
> 它代表**「Task Workspace 的基础语义不是分支」**。
> 未来 Git 可能作为**某种资源的 backend** 出现（§13.4），
> 但不会改变「共享资源 + 锁」这个上层模型。

### 2.2 Permission ≠ Write Lock（`SETTLED`）

两个**不同**的机制：

| | 回答的问题 |
|---|---|
| **Permission** | Agent **是否有资格**访问资源 |
| **Write Lock** | 当前**哪个** Agent 对该可写资源拥有排他写入权 |

```
Agent B:
    WRITE permission = true      ← 有资格
    Resource X lock owner = Agent A  ← 但锁不在他手里

→ B 不能写 X
```

**有 WRITE permission 不等于现在能写。** 见 §9、§11。

### 2.3 可并行读，写必须独占（`SETTLED`）

```
A → READ X   ✅
B → READ X   ✅
C → READ X   ✅

A → WRITE X  ✅   （前提：A 有 WRITE Permission + 持有有效 Write Lock）
B → WRITE X  ❌
C → WRITE X  ❌
```

**同一可写资源在同一时刻只能由一个获得有效 Write Lock 的 Agent 修改。**

### 2.4 新建资源自动获得 Write Lock，且必须原子（`SETTLED`）

```
Agent A
  ↓
CREATE /src/parser.py
  ↓
Permission Check
  ↓
Create Resource  +  Acquire Write Lock     ← **一个原子操作**
  ↓
成功
```

最终：

```
/src/parser.py
    created_by     = Agent A
    write_lock.owner = Agent A
```

**绝不允许**这种模式：

```
CREATE
  ↓
（竞争窗口）
  ↓
以后再 LOCK          ← ❌ 禁止
```

**两个 Agent 同时创建同一资源时**：

```
Agent A → CREATE X
Agent B → CREATE X

→ 只能有一个成功
→ 不得留下「created but unlocked」中间状态
```

并发控制由 Workspace 层保证（具体机制见 §12）。

### 2.5 Agent 不直接访问文件系统（`SETTLED`）

**Agent 对 Task Workspace 的所有资源访问必须通过 Workspace 抽象执行。**

**Agent 不直接获得其他机器的文件系统路径或底层文件系统权限。**

```
✅ 正确：
    Agent
      ↓
    Workspace API / Client
      ↓
    Workspace Service
      ↓
    Permission → Write Lock
      ↓
    Workspace Backend

❌ 禁止：
    Agent
      ↓
    SSH / SCP / arbitrary path
      ↓
    remote filesystem
```

**Agent 不应该知道** `/Users/...`、`/mnt/...`、`/var/...` 等物理路径。

### 2.6 Local 与 Distributed 使用同一个抽象（`SETTLED`）

```
今天（Local）：
    Agent → Workspace API → Local Workspace Backend → Local filesystem

未来（Remote）：
    Agent → Workspace API → Network Transport
          → Workspace Service on Machine B
          → Remote Workspace Backend → Remote filesystem

更远（Distributed）：
    Agent → Workspace API → Distributed Workspace Service → Distributed Storage
```

**上层 Agent 不应该因为 Backend 改变而改变自己的任务逻辑。**

→ **Workspace API / logical workspace identity 必须与物理存储位置解耦。**

### 2.7 不采用「本地完成后再同步」（`SETTLED`）

**不是当前基础模型**：

```
local copy → edit → sync          ← ❌ 不是基础模型
```

**是当前基础模型**：

```
logical Workspace + controlled access + remote/local backend
```

即：**Agent 对 Workspace 发起资源操作，由对应 Workspace Backend 执行。**

> 「本地编辑再同步」未来可作为 **cache / offline mode /
> batch synchronization / performance optimization** 研究，
> 但**不改变核心 Workspace 语义**。

### 2.8 Task 隔离（`SETTLED`）

```
Task-001 → Workspace-001
Task-002 → Workspace-002
```

**不同 Task 默认隔离。**

```
❌ Agent B 参与 Task-001
   → 于是 B 自动看见 Task-002 的 Workspace
```

### 2.9 Workspace 不是全局公共目录（`SETTLED`）

```
❌ /global-workspace/          ← 所有 Task 共用（禁止）
```

系统**可以**存在 **Task Workspace Root** 作为基础设施存储根目录，
但**它只是 infrastructure implementation**，不是 Agent-accessible global workspace。

**真正的资源归属仍然是**：

```
Task → Task Workspace
```

### 2.10 Workspace 不得成为第二套 Task Core（`SETTLED`）

见 §3。

---

## 3. Relationship to Task Core

### 3.1 职责划分（`SETTLED`）

| Task Core 负责（`CURRENT`） | Task Workspace 负责（`FUTURE`） |
|---|---|
| Task identity | Task 的**实际工作资源** |
| Task lifecycle / status | 文件 |
| budget | 目录 |
| agreement | 数据 |
| contractor | 中间产物 |
| domain state | 后续 Artifact 等 |

**Workspace 不得成为第二套 Task Core。**

### 3.2 明确禁止（`SETTLED`）

```
❌  Task.status    = SUBMITTED
    Workspace.status = IN_PROGRESS
    ← 两套 Task 生命周期，两个都声称正确
```

### 3.3 Workspace Lifecycle ≠ Task Lifecycle（`SETTLED`）

Workspace 将来**可以**有自己的生命周期：

```
PROVISIONING → READY → ... → ARCHIVED
```

**但那只是 Workspace Lifecycle，不是 Task Lifecycle。**

两者的区别：

| | Task Lifecycle | Workspace Lifecycle |
|---|---|---|
| 权威 | **Task Core**（唯一） | Workspace 层 |
| 关心 | 任务谈成了没有、做完没有、验收了没有 | 资源空间准备好了没有、归档了没有 |
| 例子 | `CREATED` `ACCEPTED` `SUBMITTED` `VERIFIED` | `PROVISIONING` `READY` `ARCHIVED` |
| 会不会互相推导 | 否 | 否 |

> ⚠️ **`Task.status` 和 `Workspace.lifecycle` 之间不允许有自动同步逻辑。**
> 「任务 ACCEPTED 了所以 Workspace 就该 READY」是一个**策略**，
> 不是一个**不变量** —— 它是 §15 的 Open Design Question，
> 不是现在可以写死的规则。

### 3.4 当前实现状态（`CURRENT`）

```
Task.workspace_id  →  一个字符串（Nullable）
                      存于 tasks.workspace_id 列
                      由 TaskWorkspaceRef 包装（只有 workspace_id 一个字段）
```

`TaskWorkspaceRef` **没有**任何 I/O、权限、路径概念。有守卫在检查这一点。

---

## 4. Relationship to Task Shared Context

### 4.1 一句话区分（`SETTLED`）

> **Shared Context 描述「我们共同知道什么」；**
> **Workspace 保存「我们共同操作什么」。**

### 4.2 具体例子

```
Task Shared Context（CURRENT，已实现）:
    Decision:      使用状态机解析 CSV
    Assignment:    A → parser, B → tests
    Open Question: 是否支持超大文件

Task Workspace（FUTURE）:
    /src/parser.py
    /tests/test_parser.py
    /data/sample.csv
    /docs/design.md
```

**二者必须独立。**

### 4.3 Shared Context 可以引用 Workspace，但不能成为它（`SETTLED`）

```
✅ 允许：
    Shared Context
        ↓ logical reference
    Workspace / Artifact
        （artifact_ref / resource_ref）

❌ 禁止：
    Shared Context → 直接 open()
    Shared Context → 直接写文件
    Shared Context → 直接控制 filesystem
```

**当前 `workspace_id` 仍然只是逻辑引用**（`CURRENT`，有守卫）。

### 4.4 实现层面的现状（`CURRENT`）

`TaskSharedContext.artifact_refs` 已存在，
并且沿用与 `result_ref` **同一条**安全规则
（`core/logical_ref.py`：不是路径、不是 URL、不是 I/O 句柄）。

**这条规则的存在使得「Shared Context 变成 Workspace」在结构上做不到** ——
它根本没有可以表达物理目标的数据类型。

---

## 5. Relationship to Agent Private Workspace

### 5.1 两者完全不同（`SETTLED`）

```
Agent Private Workspace ≠ Task Workspace
```

| | Agent Private Workspace | Task Workspace |
|---|---|---|
| 属于 | **一个 Agent** | **一个 Task** |
| 当前实现 | **`CURRENT`** —— `core/workspace.py` | **`FUTURE`** —— 只有 `TaskWorkspaceRef` |
| 有文件 I/O 吗 | ✅ 有 | ❌ **没有**（尚未实现） |
| 别的 Agent 能看吗 | ❌ 默认不能 | ✅ 任务参与方可以（设计上） |
| 有路径边界检查吗 | ✅ `WorkspaceViolationError` | 设计上由 Workspace 层负责 |

### 5.2 明确禁止（`SETTLED`）

```
❌ 因为 Agent A 参与 Task-001
   → 就把 agents/A/workspace/ 直接变成 Task Workspace
```

### 5.3 正确模型（`SETTLED`）

```
Agent A
├── Private Workspace          ← 自己的东西
│
└── participates in
       ↓
   Task Workspace              ← 这件事的东西

多个 Agent：
    Agent A ─┐
    Agent B ─┼── Task Workspace
    Agent C ─┘
```

> **`core/workspace.py` 不得被重命名、重构或重新解释。**
> 它服务的是 Agent Private Workspace，与本文档描述的东西无关。

---

## 6. Workspace Identity

### 6.1 `workspace_id` 是什么（`SETTLED`）

**Logical Workspace Identity。**

**不是**：

- ❌ filesystem path
- ❌ URL
- ❌ permission
- ❌ I/O handle

```
WS-001
  ↓ 可以在下面任何一种 backend 上运行
LocalFilesystemBackend
RemoteFilesystemBackend
DistributedBackend
```

### 6.2 为什么必须解耦（`SETTLED`）

如果 `workspace_id` 长得像路径：

```
WS-001  →  /Users/foo/task-workspaces/WS-001/
```

那么：

1. 换机器 → 所有 id 失效
2. 换 backend → 所有 id 失效
3. 下游代码会倾向于 `open(workspace_id)` → **安全边界破裂**
4. 本地与远程无法用同一个抽象（违反 §2.6）

> 这条和 `result_ref` / `artifact_refs` 是同一条安全原则：
> **逻辑身份不能长得像一个可访问目标。**

### 6.3 当前状态（`CURRENT` / `NOT IMPLEMENTED`）

```
CURRENT:           TaskWorkspaceRef.workspace_id  （str）
                   tasks.workspace_id 列（TEXT，可空）
                   idx_tasks_workspace 索引

NOT IMPLEMENTED:   创建 / 分配 / 解析 workspace_id 的任何逻辑
                   没有 WorkspaceRegistry
                   没有 workspace 表
```

---

## 7. Workspace Ownership / Isolation

### 7.1 归属（`SETTLED`）

```
Task-001
└── Workspace-001        ← 一个 Task 一个 Workspace
```

**Task Workspace 属于一个 Task 的资源边界。**

### 7.2 隔离（`SETTLED`）

**不同 Task 默认隔离。**

参与 Task-001 **不会**让 Agent 自动看见 Task-002 的 Workspace。

### 7.3 不是全局目录（`SETTLED`）

```
❌ /global-workspace/     （禁止）
```

可以有 **Task Workspace Root** 作为基础设施存储根，但：

| | Task Workspace Root | Agent-accessible Global Workspace |
|---|---|---|
| 是什么 | **基础设施实现细节** | ❌ 不存在 |
| Agent 看得见吗 | ❌ 不应该知道 | — |
| 说明 | 「资源存在哪」 | 「Agent 能访问什么」 |

**真正的资源归属仍然是 `Task → Task Workspace`。**

### 7.4 与参与者列表的关系（`OPEN`）

`Task.participants` 已经存在（`CURRENT`）。

但**「参与方」是否等于「Workspace 成员」**这个问题**尚未决定**：

- 参与方是否自动获得 READ permission？
- contractor 是否自动获得 WRITE permission？
- creator 是否自动获得全部权限？
- 中途加入的 Agent 拿到什么？

见 §21。

---

## 8. Resource Model

### 8.1 Resource 是什么（`SETTLED` 原则 / `OPEN` 细节）

Task Workspace 里的一个**资源**可以是：

- 文件
- 目录
- 数据
- 中间产物
- 后续 Artifact

### 8.2 Resource Identity 必须与物理路径解耦（`SETTLED`）

```
❌ 不要把这个当 Resource Identity：
    "/Users/foo/task-workspaces/WS-001/src/a.py"

✅ 第一版倾向：
    (workspace_id, logical_resource_id)
```

例如：

```
("WS-001", "src/parser.py")
```

> **注意 `"src/parser.py"` 是 workspace 内的逻辑路径，**
> **不是宿主机的物理路径。** 它表达的是「Workspace 内的位置」，
> 由 backend 负责映射到真实存储。

### 8.3 尚未决定（`OPEN`）

- 逻辑资源 id 的确切形式（POSIX 风格相对路径？不透明 id？）
- 目录是否是一等 resource，还是只是路径前缀
- 资源是否需要独立的元数据（大小、hash、mime）
- 资源是否可重命名 / 移动 / 删除，以及这对锁意味着什么

见 §21。

---

## 9. Permission Model

### 9.1 Permission 回答什么（`SETTLED`）

> **Agent 是否有资格访问资源。**

### 9.2 权限种类（`OPEN`——候选，未拍板）

候选：

```
READ
WRITE
CREATE
DELETE
EXECUTE?      （与 §19 Controlled Execution 相关）
```

**尚未决定**：确切的权限集合、是否需要更细的粒度、
权限是否可以按资源粒度授予。

### 9.3 权限授予（`OPEN`）

**尚未决定**：

- 谁有权授予权限（Task creator？Agreement？）
- 权限是否可以动态变更
- 权限是否可以转授或撤销
- 参与的 Agent 中途退出时权限如何收回

见 §21。

### 9.4 本阶段明确不实现（`NOT IMPLEMENTED`）

❌ Permission Engine
❌ ACL 存储
❌ `workspace_permissions` 表
❌ 权限校验 API

---

## 10. Write Lock Model

### 10.1 Write Lock 回答什么（`SETTLED`）

> **当前哪个 Agent 对该可写资源拥有排他的写入权。**

### 10.2 基本语义（`SETTLED`）

| 方面 | 语义 |
|---|---|
| **Lock owner** | 持有锁的 Agent |
| **Lock resource** | 被锁定的资源 |
| **Lock state** | 有效 / 无效（释放、过期、被恢复） |
| **Lock acquisition** | 见 §10.3 |
| **Lock release** | 见 §10.4 |
| **Lock validity** | 见 §10.5 |

### 10.3 获得锁的条件（`SETTLED`）

```
获得 Write Lock 需要**同时**满足：

    ① Permission WRITE
    ② resource writable
    ③ 没有其他 Agent 的有效锁
```

三条**缺一不可**。

### 10.4 写操作（`SETTLED`）

```
WRITE  requires  调用方持有该资源的有效 Write Lock
```

没有有效锁 → **不能写**（即使有 WRITE permission）。

### 10.5 释放锁（`SETTLED`）

```
只有**当前锁主**可以释放自己的 Write Lock。
```

因此：

```
❌ 其他 Agent 没有锁所有权时不能写
❌ 其他 Agent 不能释放别人的锁
```

### 10.6 本阶段明确不实现（`NOT IMPLEMENTED`）

❌ Lock Manager
❌ 锁表（`workspace_locks`）
❌ 锁的获取 / 释放 API
❌ 崩溃恢复（§16）

---

## 11. Permission vs Lock

### 11.1 正式定义（`SETTLED`）

```
Permission 决定 Agent 是否有资格访问资源。
Write Lock 决定当前哪个 Agent 对该可写资源拥有排他的写入权。
```

**`Permission ≠ Write Lock`**

### 11.2 组合真值表（`SETTLED`）

| Permission WRITE | 持有 Write Lock | 能写吗 |
|---|---|---|
| ❌ | — | ❌ 不能（没资格） |
| ✅ | ❌ | ❌ **不能**（锁不在自己手里） |
| ✅ | ✅ | ✅ 能 |

### 11.3 明确禁止（`SETTLED`）

```
❌  Permission = lock
❌  WRITE permission → 自动拥有 lock
```

**唯一的例外**是 §12 定义的**新资源创建时的原子规则**。

### 11.4 为什么必须分开（`SETTLED`）

如果两者合并：

```
Agent B 有 WRITE permission
    ↓
B 永久拥有写权
    ↓
A 和 B 可以同时写同一文件
    ↓
静默覆盖 / 交错写入
```

分开之后：

```
B 有资格，但必须等 A 释放锁
    ↓
写入是排他的
    ↓
冲突在**写入时**被发现，不是一个更晚、更贵的时刻
```

---

## 12. Resource Creation + Auto-Lock

### 12.1 原子规则（`SETTLED`）

> **Agent 创建新的可写 Workspace Resource 后，
> 该 Resource 默认自动获得该 Agent 的 Write Lock。**
>
> **而且：创建和获得 Write Lock 必须是一个原子操作。**

这是 §11.3「唯一例外」的正式定义。

### 12.2 正确语义

```
Agent A
  ↓
CREATE /src/parser.py
  ↓
Permission Check
  ↓
Create Resource + Acquire Write Lock      ← 原子
  ↓
成功

最终：
    /src/parser.py
        created_by       = Agent A
        write_lock.owner = Agent A
```

### 12.3 明确禁止的中间状态（`SETTLED`）

```
❌  CREATE → （竞争窗口）→ 以后再 LOCK
❌  created but unlocked
```

### 12.4 并发创建同一资源（`SETTLED`）

```
Agent A → CREATE X
Agent B → CREATE X

必须由 Workspace 的并发控制保证：只能有一个创建成功。
```

**这是本项目已经解决过的一类问题**，可以直接复用已验证的模式：

| 已有的同类问题 | 采用的机制 | 位置 |
|---|---|---|
| 两个进程同时写同一个 Task | `UPDATE ... WHERE task_id=? AND version=?` | `TaskStore.save()` |
| 两个 worker 同时认领同一条消息 | 单条条件 UPDATE（`AND processed_once = 0 AND (claim_id IS NULL OR ...)`） | `CommunicationRecorder.claim_message()` |
| 两个 Agent 同时推进 Context 版本 | `UPDATE ... WHERE task_id=? AND current_revision=?` | `TaskContextStore.commit_revision()` |
| 两个进程同时裁决同一个 Change | `UPDATE ... WHERE change_id=? AND status='PROPOSED'` | `TaskContextStore.decide_change()` |

> **共同点：让数据库用一条带条件的语句来裁决，而不是「先查再写」。**
>
> 第一版实现应该**复用这个模式**，不要另造一套并发系统。
> 具体 SQL 留待实现阶段（见 §21）。

### 12.5 本阶段明确不实现（`NOT IMPLEMENTED`）

❌ Create API
❌ Auto-lock 实现
❌ `workspace_resources` 表

---

## 13. Local vs Remote Workspace Backend

### 13.1 同一个抽象（`SETTLED`）

```
今天：
    Agent → Workspace API → Local Workspace Backend → Local filesystem

未来：
    Agent → Workspace API → Network Transport
          → Workspace Service on Machine B
          → Remote Workspace Backend → Remote filesystem

更远：
    Agent → Workspace API → Distributed Workspace Service → Distributed Storage
```

### 13.2 上层不变（`SETTLED`）

> **上层 Agent 不应该因为 Workspace Backend 改变而改变自己的任务逻辑。**

这要求：

- Workspace API 与物理位置无关（§6）
- Resource Identity 与物理路径无关（§8.2）
- 错误语义与位置无关（本地 ENOENT 和远程 ENOENT 应该对 Agent 是同一种失败）

### 13.3 Backend 的职责边界（`SETTLED` 原则）

Backend 负责「资源实际在哪里、怎么存取」。

Backend **不负责**：

- ❌ 判断谁能访问（那是 Permission）
- ❌ 判断谁在写（那是 Write Lock）
- ❌ 决定 Task 是否完成（那是 Task Core）

> ⚠️ **但如果实现时发现有例外，实现阶段必须写 ADR** ——
> 不要静默地把判断逻辑塞进 backend。

### 13.4 Git 在未来可能出现的位置（`FUTURE`）

§2.1 否决的是「Git 式分支作为**基础模型**」。

Git **可能**在未来作为**某种资源的 backend** 出现：

```
Workspace API → GitBackend（对某些资源类型）
```

但这**不改变**上层模型：

```
shared resources + Permission + Write Lock      ← 仍然是这个
```

**本阶段不实现，也不设计。**

### 13.5 本阶段明确不实现（`NOT IMPLEMENTED`）

❌ 任何 Backend
❌ Local Workspace Backend
❌ Remote Workspace Backend
❌ Network Transport for Workspace
❌ Workspace Service

---

## 14. Agent Access Model

### 14.1 访问路径（`SETTLED`）

```
Agent
  ↓
Workspace API / Client
  ↓
Workspace Service
  ↓
Permission Check
  ↓
Write Lock Check（写操作时）
  ↓
Workspace Backend
  ↓
Resource
```

### 14.2 Agent 知道什么 / 不知道什么（`SETTLED`）

| Agent **知道** | Agent **不知道** |
|---|---|
| `workspace_id`（逻辑身份） | 物理路径（`/Users/...`） |
| 逻辑资源标识 | 宿主机文件系统布局 |
| 操作的语义结果 | 用的是哪个 backend |
| 错误（无权限 / 被锁） | SSH / SCP / 底层协议细节 |

### 14.3 明确禁止（`SETTLED`）

```
❌ Agent → SSH / SCP / arbitrary path → remote filesystem
❌ Agent 直接获得其他机器的文件系统权限
❌ Agent 直接 open() 一个物理路径
```

### 14.4 与现有 `core/workspace.py` 的关系（`CURRENT`）

`core/workspace.py` 服务的是 **Agent Private Workspace**：

- 它**确实**做真实文件 I/O
- 它**确实**有路径边界检查（`WorkspaceViolationError`）
- 它**只**让 Agent 访问**自己**的目录

**它不实现本文档描述的 Task Workspace 访问模型**，也不该被改造成那样。

> 未来 Task Workspace 的 `Workspace API` 是**另一个东西**，
> 与 `core/workspace.py` 平行存在，不是它的扩展。
> 这个名字上的相似是**已知的混淆风险**（见 §20.3）。

---

## 15. Workspace Lifecycle

### 15.1 候选模型（`OPEN` —— 只是候选，不是决定）

```
Created → Provisioning → Ready → ... → Archived
```

### 15.2 与 Task Lifecycle 的关系（`SETTLED`）

**Workspace Lifecycle ≠ Task Lifecycle。** 见 §3.3。

### 15.3 本阶段**不决定**的问题（`OPEN`）

> 规范 §21 明确要求：**如果尚未有足够架构依据，不要擅自决定。**

| # | 问题 | 状态 |
|---|---|---|
| 1 | 什么时候自动创建 Workspace？ | **OPEN** |
| 2 | 在 Task `ACCEPTED` 时创建，还是 **lazy provision**（第一次访问时）？ | **OPEN** |
| 3 | Task 完成后 Workspace 是否**冻结**（不可写）？ | **OPEN** |
| 4 | Task 完成后**何时**归档？ | **OPEN** |
| 5 | Agent **中途加入**如何处理（拿到什么权限、看见什么）？ | **OPEN** |
| 6 | Agent **中途退出**如何处理（锁怎么办、已写的资源怎么办）？ | **OPEN** |
| 7 | 归档后资源是保留、迁移还是删除？ | **OPEN** |
| 8 | Workspace 能否被**显式销毁**？谁有权？ | **OPEN** |

**§15.4 为什么不现在决定**

这些问题**互相耦合**，而且都取决于实现阶段才会知道的事实：

- 「何时创建」取决于 provisioning 的成本（本地几乎为零，远程不低）
- 「是否冻结」取决于 Task 完成后是否还可能返工（4D 的 VERIFY→返工流程说明**可能**）
- 「中途加入」取决于 §9.3 的权限授予模型（还没定）

**先猜一个再改，比晚一点再定更贵** —— 因为文档一旦写了
「ACCEPTED 时创建」，后续实现就会围着它写代码。

---

## 16. Lock Recovery

> **§20 of the task spec 明确要求：这一节不要凭感觉决定。**

### 16.1 问题（`OPEN`）

> 如果 Agent 持有 Write Lock 时进程崩溃，
> Workspace 如何判断锁失效并恢复？

### 16.2 调查结果：Message Claim Lease **不能**直接照搬

规范要求先调查现有 `Message Claim Lease` 设计（ADR-0014）。
调查结论是：**两者语义不同，不能假设相同。**

| | **Message Claim Lease**（`CURRENT`） | **Workspace Write Lock**（`FUTURE`） |
|---|---|---|
| 保护什么 | 一条**消息**不被重复处理 | 一个**资源**不被并发写入 |
| 持有者是谁 | 一个正在处理消息的 **worker** | 一个**意图修改资源**的 Agent |
| 时长量级 | 一次消息处理（秒级） | 一次编辑会话（分钟～小时） |
| 过期后会发生什么 | 另一个 worker **接手** | 另一个 Agent **开始写** |
| **过短的失败模式** | **重复处理**（可被幂等吸收） | **丢失写入 / 交错写入**（静默数据损坏） |
| **过长的失败模式** | 消息卡住（**可见**，可人工干预） | 资源被阻塞（**可见**） |
| 自然终点 | 处理完成 / 失败 | 显式释放 / Task 结束 |
| 崩溃者需要配合吗 | 不需要（Phase 0 已用真进程验证） | 未知 |
| 当前是否实现 | ✅ 已实现（ADR-0014） | ❌ 未实现 |

### 16.3 为什么这个差异是本质的（`OPEN`——分析，非决定）

Message Claim 的失败模式是**不对称**的：

```
租约太短 → 重复处理（烦，但幂等能兜）
租约太长 → 消息卡住（烦，但看得见）
```

Workspace Write Lock 的失败模式**更危险**：

```
租约太短 → A 还在写（比如在等 LLM 生成），锁过期，
           B 拿到锁开始写，A 的写入覆盖 B 的
           → **静默数据损坏**，而且可能过很久才被发现

租约太长 → 崩溃的 Agent 长时间占着资源
           → 可见的阻塞
```

**关键点**：持有写锁的 Agent 可能**长时间没有 CPU 活动**
（等待 LLM 响应、等待人工输入、在「思考」）。
「没有活动」**不等于**「已崩溃」——
而这正是基于时间租约的恢复机制所依赖的假设。

**Message Claim 没有这个问题**，因为 worker 在处理消息时是活跃的。

### 16.4 候选方向（`OPEN`——列出但不选择）

| # | 候选 | 优点 | 缺点 |
|---|---|---|---|
| A | 时间租约（照搬 Message Claim Lease） | 机制已有，简单 | 见 §16.3：可能造成静默数据损坏 |
| B | **显式释放 + Task 生命周期收尾** | 没有「租约太短」问题 | 崩溃后资源一直被占，直到 Task 结束 |
| C | 租约 + **显式续租**（心跳） | 能区分「活跃但慢」与「已崩溃」 | 引入心跳机制（**当前明确禁止**） |
| D | 崩溃检测（进程存活探测） | 精确 | 需要分布式 liveness，极复杂 |
| E | **乐观并发**（不锁，写入时检测冲突） | 无崩溃恢复问题 | 与 §2.3「写必须独占」矛盾 |
| F | 锁 + 人工/Task 级解锁 | 简单、可控 | 需要人介入 |

### 16.5 本阶段的结论（`OPEN DESIGN QUESTION`）

**不决定。**

理由：

1. `Message Claim Lease` 不能照搬（§16.2 的差异是本质的，不是量的）
2. 候选 C 需要心跳，而**心跳当前是被明确禁止的**
3. 候选 E 与 §2.3 冲突
4. 剩余候选（A / B / D / F）的取舍取决于**实现阶段才知道的事实**：
   - 一次写操作的实际时长分布
   - Agent 是否真的会长时间持有锁
   - 单机 vs 多机（崩溃检测的代价差别巨大）

> **这个问题被正式记录为 OPEN DESIGN QUESTION。**
> 它必须在 Workspace 实现阶段**开始之前**解决 ——
> 因为锁的恢复语义决定了锁的数据结构。

**同时明确**：

```
Message Claim Lease ≠ Workspace Write Lock
```

两者都可能有 lease / recovery，但**语义不同**，
不得因为一个已存在就假设另一个相同。

---

## 17. Artifact Relationship

### 17.1 概念区分（`SETTLED`）

| | Task Workspace | Artifact |
|---|---|---|
| 是什么 | 任务的**工作资源空间** | 任务的**产出** |
| 例子 | `/src/parser.py`（还在改） | `parser-v2-release`（已交付） |

**Workspace 是「正在操作的东西」，Artifact 是「交付出去的东西」。**

### 17.2 与现有 `result_ref` 的关系（`OPEN`）

`TaskResultRef.result_ref` **已经存在**（`CURRENT`，Step 4D）：

- 它是逻辑标识
- 它**不是**路径、**不是** URL
- 它**没有 resolver** —— 没有任何东西检查它指向的成果是否真的存在
  （这是已知技术债）

**尚未决定**：

- `result_ref` 将来是否解析到一个 Artifact？
- Artifact 是否属于 Workspace，还是独立于 Workspace？
- `artifact_refs`（Shared Context 里）与 `result_ref`（执行记录里）是否指同一类东西？

> ⚠️ **不要把「将来可能」写成「现在就是」。**
> 当前 `result_ref` 和 `artifact_refs` **都只是逻辑字符串**，
> 没有任何 resolver，没有任何存储。

### 17.3 本阶段明确不实现（`NOT IMPLEMENTED`）

❌ Artifact Store
❌ Artifact 表
❌ `result_ref` 的 resolver
❌ 文件上传 / 下载

---

## 18. Future Controlled I/O

### 18.1 是什么（`FUTURE`）

未来 Agent 通过 Workspace API 进行受控 I/O 的候选形态：

```
read(workspace_id, logical_resource_id)   → content
write(workspace_id, logical_resource_id, content)
list(workspace_id, prefix)
delete(workspace_id, logical_resource_id)
```

**每一次调用都经过**：Permission → Write Lock（写时）→ Backend。

### 18.2 与 `core/workspace.py` 的区别（`SETTLED`）

| | `core/workspace.py`（`CURRENT`） | 未来 Task Workspace I/O（`FUTURE`） |
|---|---|---|
| 谁的 | Agent 私有 | Task 共享 |
| 权限 | 无（只有路径边界） | Permission |
| 锁 | 无 | Write Lock |
| 多机 | ❌ | ✅（设计上） |

### 18.3 本阶段明确不实现（`NOT IMPLEMENTED`）

❌ 任何 Workspace I/O API

---

## 19. Future Controlled Execution

### 19.1 是什么（`FUTURE`）

未来可能在 Workspace 内执行代码（跑测试、跑构建）：

```
Agent → Workspace API → execute(resource, sandbox policy) → result
```

这需要：

- Sandbox
- 资源限额
- 网络策略
- 执行审计

### 19.2 当前状态（`NOT IMPLEMENTED` / 明确禁止）

> **代码执行在当前阶段是明确禁止的。**
>
> 没有任何 `subprocess` / shell / Docker / sandbox 相关代码 ——
> 有守卫在检查（`test_shared_context_guards.py::TestForbidden`）。

### 19.3 为什么现在就写这一节（`SETTLED`）

因为**执行能力**会影响 §9 的权限模型：

```
如果将来有 EXECUTE 权限，
那么 Permission 的种类就不是 {READ, WRITE, CREATE, DELETE}
```

把它列出来，是为了让 §9.2 的「`OPEN`」显得**有理由** ——
权限集合还没定，是因为执行模型还没定。

---

## 20. Non-Goals / Forbidden for current phase

### 20.1 本阶段明确不实现（`NOT IMPLEMENTED`）

| 领域 | 禁止内容 |
|---|---|
| **Backend** | Local / Remote / Distributed Workspace Backend · Workspace Service |
| **Manager** | Workspace Manager · Workspace Gateway |
| **Permission** | Permission Engine · ACL · `workspace_permissions` 表 · `workspace_members` 表 |
| **Lock** | Lock Manager · 锁表 · 获取/释放 API · 恢复逻辑 |
| **I/O** | Filesystem API · 文件上传下载 |
| **网络** | Remote Workspace · Network Workspace · P2P |
| **Artifact** | Artifact Store · resolver |
| **Git** | branch · worktree · merge · rebase |
| **执行** | Code Execution · Sandbox · 测试运行器 |

### 20.2 也不修改（生产代码）

Task Core · Task Shared Context · Task Execution · Negotiation ·
Wallet · Ledger · Communication Protocol。

### 20.3 已知的命名混淆风险（`SETTLED` —— 提醒）

项目里有**三个**都叫「workspace」的东西：

| 名字 | 文件 | 是什么 | 状态 |
|---|---|---|---|
| **Agent Private Workspace** | `core/workspace.py` | Agent 自己的文件空间（有 I/O） | `CURRENT` |
| **`TaskWorkspaceRef`** | `core/task_workspace.py` | Task 的 workspace **逻辑引用**（无 I/O） | `CURRENT` |
| **Task Workspace** | 本文档 | Task 的共享资源边界 | `FUTURE` |

**前两个已经存在且完全不同；第三个还没有实现。**

> ⚠️ **不要把这三个混为一谈。** 尤其是：
> `core/workspace.py` **不是** Task Workspace 的雏形，
> 它服务的是 Agent 私有空间。

---

## 21. Open Design Questions

> **这一节是本文档最重要的诚实部分。**
> 以下问题**尚未决定**。实现阶段必须先解决它们，
> 不得因为文档没写答案就自行猜一个。

| # | 问题 | 依赖 |
|---|---|---|
| 1 | **Workspace 何时创建？** Accepted 时 vs lazy provision | provisioning 成本 |
| 2 | **Workspace lifecycle 的完整状态集** 与转换 | §15.1 候选模型 |
| 3 | **Task 完成后是否冻结？何时归档？** | 返工流程（4D 的 VERIFY→返工说明可能返工） |
| 4 | **Lock lease / 崩溃恢复** —— 见 §16，6 个候选未选 | 写操作时长分布、单机 vs 多机 |
| 5 | **多资源锁** —— 一个操作需要改多个文件时怎么办？ | 死锁避免 vs 原子性 |
| 6 | **目录锁语义** —— 锁一个目录是否等于锁其下所有资源？ | 与 #5 相关 |
| 7 | **Agent 中途加入 / 退出** —— 权限、锁、已写资源如何处理 | §9.3 权限授予模型 |
| 8 | **权限集合与授予规则** —— 见 §9.2 / §9.3 | §19 执行模型 |
| 9 | **Artifact 设计** —— 见 §17.2 | — |
| 10 | **逻辑资源 id 的确切形式** —— 见 §8.3 | Backend 设计 |
| 11 | **并发创建资源的具体 SQL / CAS 形态** —— 见 §12.4 | 实现阶段 |
| 12 | **Workspace Root 的物理布局** | 实现阶段 |

### 21.1 处理原则（`SETTLED`）

> 规范 §25 / §30 的原则：
>
> **已知的写确定。未知的明确标 `OPEN`。**
> **不要因为文档缺少答案而写出一个自己猜的实现。**

有长期架构影响的决定，**必须写 ADR**。

---

## 22. Future Evolution

### 22.1 演进路径（`FUTURE`）

```
今天（NOT IMPLEMENTED）
    TaskWorkspaceRef（只有 workspace_id）
        ↓
第一步：Local Workspace（单机）
    Workspace API + Local Backend + Permission + Write Lock
        ↓
第二步：Remote Workspace
    同一个 Workspace API + Network Transport
        ↓
第三步：Distributed Workspace
    Distributed Workspace Service + Distributed Storage
```

> **§2.6 的约束**：上层 Agent 不应该因为 backend 改变而改变任务逻辑。
> 所以第一步就要把抽象放在正确的位置 —— 否则第二步要重写。

### 22.2 最终目标（规范 §30）

```
Agent
  ↓
Task
  ↓
Task Shared Context
  ↓
Task Workspace
  ↓
Permission
  ↓
Write Lock
  ↓
Resource
  ↓
Backend
```

对应关系：

| 层 | 回答 |
|---|---|
| **Shared Context** | 我们共同**知道**什么 |
| **Workspace** | 我们共同**操作**什么 |
| **Permission** | **谁可以**操作 |
| **Write Lock** | **当前谁可以写** |
| **Backend** | 资源**实际上在哪里** |

### 22.3 当前状态的一句话（`SETTLED`）

> **本文档只完成架构定义。Task Workspace 仍然是 `FUTURE`。**
>
> 项目当前阶段：**`Post-Step-4F / Pre-Task-Workspace`**
>
> 下一步不是「开始写 Workspace 代码」，
> 而是 **Workspace Readiness Gate** ——
> 先解决 §21 里的 **Open Design Questions**（尤其是 #1 / #4 / #7），
> 再进入实现。
