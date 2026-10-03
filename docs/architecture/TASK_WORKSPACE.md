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

> ⚠️ **机制已精确化**（[`LOCK_MODEL.md`](LOCK_MODEL.md) §6.3）：
> **原子性要求不变**（绝不允许 `CREATE → 竞争窗口 → 以后再 LOCK`），
> 但实现机制由「新建 Explicit Lock」改为**继承父 Directory Lock**。
> 因为能在该目录 `CREATE` 的 Agent **必须已是该目录 Directory Lock 持有者**，
> 所以继承到的 Effective Lock **就是创建者自己** —— **结论相同**。

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

### 2.11 Write Lock 采用显式持有模型（`SETTLED`）

**Write Lock 不因为 Agent 长时间没有操作而自动失效。**

第一版**不实现**时间 Lease / Lease Renewal / Heartbeat /
自动 Lock Expiry / 自动判断 Agent 已死亡。见 §16。

### 2.12 Lazy Provisioning（`SETTLED`）

**Logical Workspace 与 Physical Workspace 分离。**

`workspace_id = WS-001` **不代表**物理目录 / Backend 已经创建。
物理 Workspace 在**第一次真正需要**时才 provision。见 §15。

### 2.13 Workspace Protocol ≠ Host OS Filesystem Permission（`SETTLED`）

```
Workspace Permission / Write Lock  =  Workspace Protocol-level authorization
Host OS Filesystem Permission      =  Infrastructure / OS-level control
```

**两者不能混为一谈。** 见 §14.5.8。

### 2.14 Human 是正式协作 Actor（`SETTLED`）

**Authorized Human Modification 是一等协作方式**，
不是「Workspace 外面的神秘管理员」所为。见 §14.5。

### 2.15 Observation ≠ Conclusion（`SETTLED`）

**系统不假装知道 Agent 是否「死了」。**

```
Agent A 最近 2 小时没有修改文件
    → 系统只能知道：last activity = 2h ago
    → 不能推出：Agent A = DEAD          ❌
```

这与 **`Agent Inference ≠ Domain Fact`**（Constitution §9）
是同一种架构原则。见 §16.6。

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

## 7.5 Task Workspace Read / Visibility（`SETTLED` —— 填补 Authorization 移除后的空档）

> ⚠️ **设计简化移除了 Authorization，曾留下「Workspace 没有 Read 模型」的空档。
> 本节正式填补它 —— 但**不**重新引入 Authorization。**

### 7.5.1 第一版**不建立资源级 Read ACL**

> 🔒 **第一版不建立资源级 Read ACL。**
>
> 🔒 **Task Workspace 中，所有正式 Task Participants
> 默认拥有该 Task Workspace 普通共享资源的读取权。**

❌ **不要**重新引入 `READ` / `READ_FILE` / `READ_DIRECTORY` /
`READ_RESOURCE` 之类的独立 Authorization 权限系统。
❌ **也不要**为普通 Task Workspace 文件建立复杂 ACL。

### 7.5.2 `Task Participation → Workspace Read`

```
Task T1 · Workspace W1
W1/ ├── requirements/  ├── src/  ├── tests/  └── artifacts/

A 创建 Task  →  A ∈ Participants(T1)  →  A 可读 W1/*

A 给 B 创建 Child Task T1.1（Assignee = B）
当 B **正式成为 Task Participant** 后：
B ∈ Participants(T1)  →  B 自动获得 READ(W1/*)
```

> **Agent 参加一个 Task，就可以读取该 Task 对应 Workspace 的
> 全部普通共享资源。不需要 A 再逐个文件授权。**

### 7.5.3 🔒 Read 与 Write 必须彻底分开

```
Task Participation → Read Task Workspace
Lock               → Write / Modify
Controller         → Long-term Directory Responsibility
Handoff            → Responsibility Transfer
```

```
A, B, C 都是 Task Participants
    → 三者默认都可以读 requirements / source / tests / artifacts

但**不能因为参与 Task 就自动修改所有内容**：
    src/main.py          File Lock = A
    frontend/page.tsx    File Lock = B
    tests/               Directory Lock = C
    → 修改责任仍由 Lock 决定
```

> 🔒 **`Read` = 「你可以了解这个项目。」
> `Lock` = 「你现在负责修改这项工作。」**

### 7.5.4 Read 的边界

**默认 Read 范围**：Task Workspace 的**普通共享资源**

**不在默认范围**：

| ❌ | |
|---|---|
| Agent Private Workspace | |
| Agent Private Memory | |
| Agent Private Artifacts | |
| System Workspace | |
| 其他不属于该 Task 的 Workspace | |

```
Agent A
├── Private Workspace（memory / identity / private artifacts）
└── Task Workspace T1（requirements / src / tests）

B 参加 T1 后：
    B → ✅ 可以读取 T1 Workspace
    B → ❌ 不能读取 A Private Workspace
```

> 🔒 **不要为了实现这个简单模型再引入复杂的 Visibility ACL。**

### 7.5.5 什么时候获得 Read？

> **在 Agent 正式成为 `Task Participant` 时获得。**

```
T1 Participants: A
B 尚未参与  →  B 不能读取 T1 Workspace

A 创建 Child Task T1.1（Assignee = B）
当 B **正式加入 / 接受**该 Task 后：
    B ∈ Participants(T1)  →  B → Read T1 Workspace
```

> ⚠️ **「加入 / 接受」的生命周期，如果现有 Task 模型已有定义，
> 优先复用，不要重复建立机制。**

### 7.5.6 Human 也是 Task Participant 时

```
T1 Participants: A · B · Human
    → Human 可以读取 T1 Workspace
```

> Human 是否拥有**直接修改**能力，仍由
> **Workspace / Governance / Force Unlock** 等机制决定。
> 🔒 **不要因为 Read 模型而改变既有 Human Governance 设计。**

### 7.5.7 正式原则

> ## **Task Workspace 第一版不实现资源级 Read ACL。
> ## Task Participants 默认可以读取该 Task Workspace 中的普通共享资源。**
>
> ## **Read Visibility 与 Write Responsibility 是两个独立概念。**

---

## 8. Resource Model

> ⚠️ **本节的资源级控制细节（Controller / Authorization /
> Delegation / Cascade Revocation）见 [`RESOURCE_LAYER.md`](RESOURCE_LAYER.md)。**
> 那是对 §8–§11 的**下层细化**，不是另一套模型。

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

> ⚠️ **设计简化后，Workspace 第一版的核心模型是
> `Controller + Lock + Handoff + Audit`。**
> `Authorization` / `Delegation` 降级为 `FUTURE`，
> `Cascade Revocation` 第一版**移除**。
> 见 [`RESOURCE_LAYER.md`](RESOURCE_LAYER.md) 顶部横幅与
> [`RECOVERY_MODEL.md`](RECOVERY_MODEL.md)。

> ⚠️ **Lock 的层级 · 继承 · Effective Lock · Edit Session · Handoff**
> 见 [`LOCK_MODEL.md`](LOCK_MODEL.md)。那是本节的**增量补充**，
> 不改变本节与 ADR-0021 的任何锁状态或流程。

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

### 10.6 Lock 状态（`SETTLED`）

第一版至少能够表达三种状态：

| 状态 | 语义 |
|---|---|
| `ACTIVE` | 当前有效锁 |
| `RELEASED` | 持有者**正常释放** |
| `REVOKED` | 由**治理操作**强制解除（见 §16.8） |

> ⚠️ **不要增加 `EXPIRED`。**
> 那会暗示当前存在自动 Lease —— 而 v1 明确没有（§16.2）。

### 10.7 释放的两种形态（`SETTLED`）

| 操作 | 谁可以 | 条件 |
|---|---|---|
| `UNLOCK` | **当前锁主** | `caller == current_lock_owner` |
| `FORCE_UNLOCK` | **治理主体** | authorized governance actor（v1 = Human Operator） |

> **普通 Agent 不得因为拥有 `WRITE` 就自动拥有 `FORCE_UNLOCK`。**
> 而且 `FORCE_UNLOCK` 必须留下审计记录（§16.10）。

### 10.8 本阶段明确不实现（`NOT IMPLEMENTED`）

❌ Lock Manager
❌ 锁表（`workspace_locks`）
❌ 锁的获取 / 释放 / Force Unlock API
❌ 崩溃恢复的**实现**（语义已在 §16 确定）

---

## 11. Permission vs Lock

> ⚠️ 本节确立 **`Permission ≠ Write Lock`**。
> [`RESOURCE_LAYER.md`](RESOURCE_LAYER.md) §5 在此之上再分出
> **Controller**（谁负责）与 **Authorization**（谁有资格）—— 三者不可合并。

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

## 14.5 Human Participation —— Human 是正式协作 Actor

> **状态：`SETTLED`（定位已定）；实现为 `FUTURE`**
>
> 决策记录：[ADR-0022](adr/0022-lazy-provisioning-and-workspace-lifecycle.md)

### 14.5.1 不要假设 Human 永远不能直接修改 Workspace（`SETTLED`）

第一版 AI Economy OS 的初始用户群体是
**熟悉 AI Agent 使用的专业用户**。

因此 Workspace 应支持 **Human 作为正式协作参与者**：

```
Human
  ↓
Task Workspace
  ↓
Permission
  ↓
Write Lock
  ↓
Resource Modification
```

**这种修改属于 `Authorized Human Modification`。**

### 14.5.2 Authorized vs Unauthorized Modification（`SETTLED`）

> ⚠️ **不要使用法律意义上的「合法 / 违法」作为系统内部术语。**

建议使用：

| 术语 | 含义 |
|---|---|
| **`Authorized Modification`** | Human Operator **拥有相应 Permission**，**通过 Workspace 正式机制**修改资源 |
| **`Unauthorized / Out-of-band Modification`** | Finder / Terminal / 其他程序**直接修改物理文件**，没有经过 Workspace Protocol |

`Authorized` 是**正常协作行为**，不是例外。

### 14.5.3 第一版不负责阻止 Unauthorized Physical Modification（`SETTLED`）

> **第一版不试图建立宿主机级别的绝对文件系统防篡改。**

Workspace Permission / Write Lock 约束的是：

```
✅ 通过 AI Economy OS Workspace Protocol 执行的操作
```

而**不是**：

```
❌ 宿主操作系统超级用户也绝对无法绕过的物理文件系统控制
```

### 14.5.4 第一版不主动监控（`SETTLED`）

本阶段**不做**：

| ❌ |
|---|
| Filesystem Watcher |
| Hash Tamper Detection |
| Kernel-level Enforcement |
| Sandbox 强制隔离 |
| 试图阻止管理员直接改文件 |

**原因**：

- 初期用户是**熟悉 Agent 的专业人士**
- 当前优先验证 **Agent / Human 正常协作路径**
- 绕过系统的异常情况**未来再处理**

因此：

> **Unauthorized Physical Modification 是 `Future Concern`。**

### 14.5.5 Authorized Human Modification 是一等协作方式（`SETTLED`）

> **不要把 Human 理解成「Workspace 外面的神秘管理员」。**

未来 Human 可以作为**正式 Workspace Actor**：

```
Workspace Actor
├── Agent
├── Human
├── System
└── Future Guardian
```

**不同 Actor 拥有不同 Permission。**

### 14.5.6 Human Force Unlock 保持现有定义（`SETTLED`）

上一阶段（ADR-0021）已经正式确定，**本次不变**：

```
Agent Owner:        UNLOCK
Authorized Human:   FORCE_UNLOCK
```

`Force Unlock`：

- ❌ 不是普通文件写权限
- ❌ 不是 Agent 权限
- ❌ 不是自动超时

而是**治理操作**，并且**要求有审计记录**（§16.10）。

### 14.5.7 Human 也遵守 Permission + Write Lock（`SETTLED`）

> ⚠️ **不要改变此前已正式确定的 `Permission ≠ Write Lock`。**

```
Human A
    WRITE Permission  ✅
    但没有 Lock        ❌
→ 不能通过 Workspace Protocol 写
```

如果 Human 是高权限治理者：

> **`FORCE_UNLOCK` 也必须是独立的 `Governance Capability`** ——
> 它不由 `WRITE Permission` 推导而来。

### 14.5.8 Workspace Protocol ≠ Host OS Filesystem Permission（`SETTLED`）

**必须明确分层**：

| | 属于 |
|---|---|
| **Workspace Permission / Write Lock** | **Workspace Protocol-level authorization** |
| **Host OS Filesystem Permission** | **Infrastructure / OS-level control** |

**两者不能混为一谈。**

这也意味着：

```
Write Lock 表达的是：
    「在 Workspace Protocol 中谁拥有排他写入权」

不是：
    「谁在整个 MacBook 上物理上绝对不能碰这个文件」
```

### 14.5.9 Future audit direction（`FUTURE` —— 本次不实现）

目前**不实现监控**。但文档记录未来方向：

未来**可能**区分：

- `Agent Operation`
- `Human Authorized Operation`
- `External / Unknown Operation`

未来**再考虑**：

- Resource Fingerprint
- Version
- Mutation Detection
- Audit
- Out-of-band Detection

> **本次不要实现。**

---

## 15. Workspace Lifecycle

> **状态：`SETTLED`（生命周期模型已定）**
>
> 决策记录：[ADR-0022](adr/0022-lazy-provisioning-and-workspace-lifecycle.md)

### 15.1 Logical Workspace 与 Physical Workspace 分离（`SETTLED`）

> **Task 可以拥有一个逻辑 Workspace Identity，而物理 Workspace 采用
> Lazy Provisioning。**

```
Task
  ↓
workspace_id = WS-001
```

**这并不意味着物理目录 / Backend 已经创建。**

初始状态可能是：

```
Workspace = UNPROVISIONED
```

只有当 Task **第一次真正需要** Workspace Resource 时，才：

```
UNPROVISIONED
      ↓
PROVISIONING
      ↓
READY
```

```
Agent A
    ↓
第一次需要访问 / 创建 Workspace Resource
    ↓
Provision Workspace
    ↓
READY
```

### 15.2 为什么采用 Lazy Provisioning（`SETTLED`）

**AI Economy OS 的 Task 是通用 Task，不是所有 Task 都需要 Workspace。**

例如这些可能**完全不需要**真实 Workspace Resource：

- 查询
- 比较
- 简单计算
- 翻译
- 普通问答

因此：

> ❌ **不要**因为 Task 被创建 / `ACCEPTED` 就无条件创建一个物理 Workspace。
>
> ✅ **Logical Workspace Identity 可以存在。
> Physical Workspace 不需要提前存在。**

### 15.3 生命周期模型（`SETTLED`）

```
UNPROVISIONED
      ↓
PROVISIONING
      ↓
READY
      ↓
FROZEN
      ↓
ARCHIVED
```

| 状态 | 语义 |
|---|---|
| `UNPROVISIONED` | Logical Workspace 已经存在，但实际 Backend / Resource Space **尚未 provision** |
| `PROVISIONING` | 系统正在建立实际 Workspace Backend / Resource Space |
| `READY` | Workspace 可以按照 **Permission + Write Lock** 规则正常使用 |
| `FROZEN` | Workspace **保留、可读**，但**不允许普通写入** |
| `ARCHIVED` | Workspace 进入**长期历史 / 保存**状态，不再作为正常活跃 Task Resource 使用 |

> ⚠️ **这套状态描述的是「这个资源空间当前是否可以被正常操作」**，
> 不是任务进展。见 §15.4。

### 15.4 Workspace Lifecycle ≠ Task Lifecycle（`SETTLED`）

**再强调一次**：`Task.status` 和 `Workspace.lifecycle` **不是同一个状态机**。

```
✅ 正常：
    Task:       IN_PROGRESS
    Workspace:  READY

✅ 也允许：
    Task:       COMPLETED
    Workspace:  FROZEN
```

**禁止**在 Workspace 里复制 Task 的生命周期：

```
❌ CREATED / OFFERED / NEGOTIATING / ACCEPTED / IN_PROGRESS /
   SUBMITTED / VERIFIED / COMPLETED
```

> **Workspace Lifecycle 只描述：这个资源空间当前是否可以被正常操作。**
> 见 §3.3。

### 15.5 Task 结束后的 Workspace（`SETTLED`）

| Task 状态 | Workspace 原则上 |
|---|---|
| `COMPLETED` | → `FROZEN` |
| `FAILED` | → `FROZEN` |
| `CANCELLED` | → `FROZEN` |

**而不是立即删除。**

保留：

- files
- artifacts
- history
- evidence

供未来使用：

- 复盘
- 审计
- 经验学习
- Artifact 查询

### 15.6 不因 Task 结束立即删除（`SETTLED`）

> **Task 完成、失败或取消，不等于 Workspace 立即删除。**
>
> **第一版不建立普通 Task Workspace Delete Lifecycle。**

如果未来需要：

- storage cleanup
- retention policy
- physical deletion

**单独设计。**

> ❌ **不要**因为生命周期设计就提前实现：
> **GC · TTL · 自动物理删除**

### 15.7 Task Workspace 可以重新打开进行迭代（`SETTLED`）

> **Task 不是一次性执行、完成之后永远不能继续修改的对象。**

允许：

```
Task
    ↓
Iteration 1
    ↓
COMPLETED
    ↓
Reopen
    ↓
Iteration 2
    ↓
继续执行
```

因此：

> **Task 完成不是 Task 永久死亡。**
>
> **Workspace 也不应该被设计成
> `FROZEN` = 永久只读、永远不能恢复。**
>
> **当前正式原则：允许对已完成 Task 重新打开进行后续迭代。**

### 15.8 Iteration 必须保留历史（`SETTLED`）

**重新打开不能**：

- ❌ 清空原历史
- ❌ 覆盖旧执行
- ❌ 覆盖旧 Verification

而应该：

```
Task-001

    Iteration 1
        execution history
        submissions
        verification
        completion

    Iteration 2
        new execution
        new submissions
        new verification
```

**旧历史必须保留。**

这样未来 Agent 才可以知道：

- 上一次为什么失败？
- 为什么再次打开？
- **人类修改了什么要求？**
- 之前用了什么方案？

> 这也是未来 **Agent Experience / Learning** 的潜在数据来源。

### 15.9 Reopen 的具体细节仍然是 `OPEN`（`OPEN`）

**当前只正式确定：允许重新打开。**

以下问题**目前仍保持 `OPEN`**：

| # | 问题 |
|---|---|
| 1 | 是谁可以 Reopen？ |
| 2 | 什么时候必须新建 Task？ |
| 3 | Reopen 是否产生新的 Iteration ID？ |
| 4 | 是否恢复原 Workspace？ |
| 5 | Reopen 时旧 Lock 是否全部无效？ |
| 6 | Permission 是否重新评估？ |

> ⚠️ **不要凭感觉把这些细节一次性决定。**
> 把它们列入 [§21 Open Design Questions](#21-open-design-questions)，
> 等待后续 Task / Iteration 设计讨论。

### 15.10 Iteration ≠ Child Task（`SETTLED`）

以后会存在**两个**概念：

| | 含义 |
|---|---|
| **Iteration** | **仍然是 same Task**。`Iteration 1 / 2 / 3` 表示同一个任务不断改进 |
| **Child Task** | 从 Parent Task **独立派生**出来的局部任务 |

```
Iteration：            Child Task：
    Task-001               Parent Task
      Iteration 1              ↓
      Iteration 2          new Task
      Iteration 3
```

> **Iteration ≠ Child Task**
>
> **本次不实现 Child Task。**
> 但必须知道：后续架构设计将专门讨论
> **Task Origin / Task Lineage / Parent-Child Task**（见 §15.11）。

### 15.11 Task Origin / Task Lineage 是 `OPEN DESIGN TOPIC`（`OPEN`）

近期架构讨论已经发现：**Task 的来源将不一定只有 Human。**

目前只正式保持：

| 概念 | 当前说法 |
|---|---|
| **Root Task** | 初始的大任务，通常源自 **Human Demand**；**由 Agent 创建**。见 [TASK_ORIGIN_LINEAGE.md](TASK_ORIGIN_LINEAGE.md) §2.4 |
| **Child Task** | 未来执行中：`Agent A → 需要局部工作 → Create Child Task → Agent B` |

这意味着未来需要正式定义：

```
Task Origin · Task Creator · Parent Task · Root Task
Task Lineage · Iteration
以及 Parent Task ↔ Child Task 之间的关系
```

> ⚠️ **本次绝对不要自行决定 Task Lineage。**
> 这是下一轮专门讨论的架构主题。
>
> **目前只记录**：
>
> ```
> OPEN DESIGN TOPIC: Task Origin & Task Lineage
> ```
>
> ❌ **不要自行实现**：`parent_task_id` · `root_task_id` · `child task` ·
> `task tree` · `task graph` · `delegation lifecycle`
> （除非当前项目已经存在明确设计）。

### 15.12 Message ≠ Task Creation（`SETTLED`）

**不要把 Task Creation 和普通 Message 混在一起。**

```
Agent → 「帮我看看数据库」          → 只是 **Message**

Agent → 正式创建 Child Task
      → Agreement
      → Agent B 接受                 → 才是 **Task Creation**
```

> **本次只记录**：未来需要区分
> **Message · Task Proposal · 正式 Child Task**。
>
> 这个边界以后会在 Task Origin / Lineage 主题中正式讨论。

---

## 16. Lock Recovery

> **状态：`SETTLED`（v1 治理方式已定）**
>
> v1 的恢复语义**已经确定**。自动恢复属于 `FUTURE`。
> 决策记录：[ADR-0021](adr/0021-explicit-write-lock-and-human-force-unlock.md)

### 16.1 问题（回顾）

> 如果 Agent 持有 Write Lock 时进程崩溃 / 失联，
> Workspace 如何判断锁失效并恢复？

### 16.2 v1 决策：显式持有模型，**不采用自动时间失效**（`SETTLED`）

> **Write Lock 默认采用显式持有模型。**
>
> **Write Lock 不因为 Agent 长时间没有操作而自动失效。**

**明确禁止**把 `5 分钟` / `10 分钟` / `300 秒` 之类的固定时间
直接作为「**Agent 已经死亡**」的判断依据。

### 16.3 v1 明确不实现（`NOT IMPLEMENTED`）

| ❌ 不实现 | 为什么 |
|---|---|
| 时间 Lease | 见 §16.5 |
| Lease Renewal | 需要心跳（当前禁止） |
| Heartbeat | 当前阶段明确禁止 |
| 自动 Lock Expiry | 会把「慢」误判成「死」 |
| 单纯基于 inactivity 的 Crash Detection | 见 §16.6 |
| 自动判断 Agent 已死亡 | 见 §16.6 |

> ⚠️ **尤其不能**因为 `Message Claim` 用了 Lease，
> 就直接复制成 `Workspace Lock Lease`。
> 两者语义不同（见 §16.4）。

### 16.4 两个问题完全不同（`SETTLED`）

| | **Message Claim** | **Workspace Write Lock** |
|---|---|---|
| 回答 | 谁**暂时负责处理**某条 Message？ | 当前谁**拥有某个可写资源的排他修改权**？ |
| 持有者 | 一个正在处理消息的 worker | 一个**意图修改资源**的 Agent |
| 时长量级 | 一次消息处理（秒级） | 一次编辑会话（分钟～小时） |
| 过短的失败模式 | **重复处理**（幂等可吸收，可见） | **丢失写入 / 静默数据损坏** |
| 自然终点 | 处理完成 / 失败 | 显式释放 / Task 结束 |
| 当前状态 | ✅ `CURRENT`（ADR-0014） | `FUTURE`（本设计） |

> **不要把二者的数据结构、超时逻辑、恢复策略直接复制。**

### 16.5 为什么不能用时间租约（`SETTLED`）

**根本原因**：

> 持有写锁的 Agent 可能**长时间没有 CPU 活动** ——
> 它在等 LLM 响应、在想、在等外部 API、在等其他 Agent、
> 在做长时间任务。

```
Message Claim 的持有者：活跃的 worker   → 「没有活动」≈ 出问题了
Write Lock 的持有者：可能在想事情的 Agent → 「没有活动」完全正常
```

而基于时间租约的恢复机制**恰恰依赖「没有活动 = 已崩溃」这个假设** ——
对 Write Lock 来说，**这个假设不成立**。

**如果强行用时间租约**：

```
A 还在写（正在等 LLM 生成）
    ↓ 锁过期
B 拿到锁开始写
    ↓
A 的写入覆盖 B 的（或反过来）
    ↓
**静默数据损坏** —— 可能很久以后才发现
```

这和「消息被重复处理」不是一个量级的后果。

### 16.6 系统不假装知道 Agent 是否「死了」（`SETTLED`）

> **第一版系统不把「长时间无活动」解释为「Agent 已崩溃」。**

例如：

```
Agent A 最近 2 小时没有修改文件

系统只能知道：
    last activity = 2h ago

不能自动推出：
    Agent A = DEAD          ← ❌ 禁止
```

**这两个概念必须区分**：

```
Observation   ≠   Conclusion
```

> 这与项目已经确立的
> **`Agent Inference ≠ Domain Fact`**（Constitution §9）
> 是**同一种**架构原则。
>
> 「观察到什么」不等于「事实是什么」。

### 16.7 v1 正常释放模型（`SETTLED`）

```
Agent A
  ↓
LOCK resource
  ↓
工作
  ↓
完成
  ↓
UNLOCK
```

**只有 Lock Owner 才可以正常释放自己的 Lock。**

```
Agent A owns Lock X
    A → UNLOCK X   →  SUCCESS

Agent B → UNLOCK X →  **DENIED**
```

### 16.8 v1 异常恢复模型：Human Force Unlock（`SETTLED`）

当 Agent 无法继续工作、进程异常退出、长期失联，
或**人类认为它已经不应继续持有资源**时：

```
Human / Authorized Operator
  ↓
Force Unlock
  ↓
Lock Revoked
  ↓
Resource becomes available
```

> ### **Force Unlock 是人为治理操作，**
> ### **不是系统根据「超时」自动执行。**

这是 v1 唯一可用的异常恢复路径
（另一条是 Agent 自己 unlock）。

### 16.9 Force Unlock 必须是高权限操作（`SETTLED`）

**普通 `UNLOCK` 和 `FORCE_UNLOCK` 必须严格区分。**

| 操作 | 条件 |
|---|---|
| `UNLOCK` | `caller == current_lock_owner` |
| `FORCE_UNLOCK` | 必须是 **authorized governance actor** |

**v1 的 governance actor = Human / Authorized Human Operator。**

> ⚠️ **普通 Agent 不得因为自己拥有 `WRITE` 就自动拥有 `FORCE_UNLOCK`。**

```
Agent B: WRITE permission = true
         FORCE_UNLOCK      = ❌ 不因此获得
```

### 16.10 Force Unlock 必须可审计（`SETTLED`）

**不能让 Force Unlock 变成一条不可追踪的操作。**

因此定义 **Lock Revocation / Force Unlock Event**，
至少需要表达：

| 字段 | 含义 |
|---|---|
| `lock_id` | 被撤销的锁 |
| `workspace_id` | 属于哪个 Workspace |
| `resource_id` | 哪个资源 |
| `previous_owner` | 原锁主 |
| `revoked_by` | 谁撤销的（治理主体） |
| `revoked_at` | 何时 |
| `reason` | **为什么** |

> 具体字段名可以等实现阶段确定。
> **核心要求：Force Unlock 必须留下可追踪的治理记录。**

### 16.11 Lock 状态（`SETTLED`）

第一版至少能够表达三种状态：

| 状态 | 语义 |
|---|---|
| `ACTIVE` | 当前有效锁 |
| `RELEASED` | 持有者**正常释放** |
| `REVOKED` | 由**治理操作**强制解除 |

> ⚠️ **不要增加 `EXPIRED`。**
> 那会暗示当前存在自动 Lease —— 而 v1 明确没有。

### 16.12 未来演进（`FUTURE`）

```
v1（`SETTLED`）：
    Explicit Lock  +  Owner Unlock  +  Human Force Unlock  +  Audit

Future（`FUTURE`）：
    Guardian Agent  +  Policy  +  Automated Recovery
```

见 §16.5（下一节）与 §22。

---

### 16.13 Guardian / Observer Agent（`FUTURE`）

> **状态：`FUTURE` —— 本次只记录架构方向，不实现任何代码。**
>
> 规范明确：**不实现 Guardian Agent 实体代码**。

#### 16.13.1 是什么（`FUTURE`）

未来 AI Economy OS **可以**存在一种专门的 Agent 角色：

**Observer / Guardian Agent**

它的职责**不是直接完成业务 Task**，而是：

```
观察其他 Agent 的运行状态
    ↓
综合多个运行信号
    ↓
进行状态评估
    ↓
提出治理建议 / Recovery Request
```

#### 16.13.2 它可以观察什么（`FUTURE`）

- last message
- task state
- LLM activity
- tool activity
- workspace lock
- recent resource operations
- communication responsiveness
- runtime information
- historical behavior

#### 16.13.3 它形成什么（`FUTURE`）

类似这样的判断结果：

```
NORMAL
WAITING
LONG_RUNNING
SUSPECTED_FAILURE
UNRESPONSIVE
UNKNOWN
```

> ⚠️ **这些只是 Observer 的判断结果，不是 Domain State。**
>
> 它们**不写入 Task Core**，也**不取代**任何领域状态。

#### 16.13.4 它可以运行本地小模型（`FUTURE`）

正式允许未来架构考虑：

```
Guardian Agent
    +  local small model
    +  runtime facts
    +  rules
```

**它不需要使用与主工作 Agent 同等级别的大模型。**

```
MacBook

    Agent A
    Agent B
    Agent C

    Guardian Agent
        └── local lightweight model
```

这符合**低成本运行**与**长期自治**的方向。

> **本次不实现 Guardian Agent。** 只作为未来架构能力记录。

#### 16.13.5 ⚠️ Guardian 的权限边界必须写死（`SETTLED`——这是边界，不是实现）

**Guardian 不是「系统最高权威」。**

```
❌ 禁止的设计：
    Guardian says DEAD
        ↓
    system blindly kills Agent

✅ 正确的链路：
    Observation
        ↓
    Assessment
        ↓
    Recommendation / Recovery Request
        ↓
    Policy / Authorization
        ↓
    Actual Action
```

**两条硬约束**：

1. **Guardian 的判断不能自动等于系统事实。**
2. **Guardian 不能仅因为自己判断异常，就拥有任意系统权限。**

#### 16.13.6 Guardian 与 Source of Truth（`SETTLED`）

必须保持 Constitution §9 的优先级：

```
Domain State / TaskAgreement
        >
Task Shared Context
        >
Communication History
        >
Agent Private Memory
        >
Agent Inference
```

**Guardian 的判断属于 `Observation / Assessment`，
不是新的 Source of Truth。**

**不要产生 `Guardian State` 并让它凌驾于 Task Core。**

```
✅ 允许（两者并存，各说各的）：
    Task Core:  IN_PROGRESS
    Guardian:   SUSPECTED_FAILURE

❌ 禁止（Guardian 自动改写领域状态）：
    Guardian:   DEAD
    Task Core:  → 自动变成 FAILED
```

> 「Task Core 自动变成 FAILED」**第一版不能自动发生** ——
> 必须经过明确的治理机制。

#### 16.13.7 Guardian 的未来 Lock Recovery（`FUTURE`）

```
Agent A 持有 Lock
    ↓
Guardian 观察
    ↓
评估：可能异常
    ↓
Recovery Request
    ↓
Authorization Policy
    ↓
Force Unlock / Recovery
```

> **但当前版本**：
>
> | | 状态 |
> |---|---|
> | Guardian | `FUTURE` |
> | 当前真正可用的恢复路径 | **Agent self-unlock** 或 **Authorized Human Force Unlock** |

#### 16.13.8 ⚠️ 不要因为 Guardian 就重新引入 Heartbeat（`SETTLED` 限制）

以后 Guardian 可以观察很多信号，但**本次**：

| ❌ 不实现 |
|---|
| heartbeat |
| periodic ping |
| automatic lease renewal |
| crash detector |
| Guardian runtime |

> ⚠️ **尤其不能**「为了让 Guardian 将来能够工作」，
> **反向把 Heartbeat 提前实现**。
>
> 本次仍然**只做架构文档**。

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
| **生命周期** | GC · TTL · 自动物理删除 · Workspace Delete Lifecycle |
| **篡改监控** | Filesystem Watcher · Hash Tamper Detection · Kernel-level Enforcement · Sandbox 强制隔离 · 阻止管理员直接改文件 |
| **Task Lineage** | `parent_task_id` · `root_task_id` · child task · task tree · task graph · delegation lifecycle（**OPEN DESIGN TOPIC**，见 §15.11） |

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
| 1 | ~~Workspace 何时创建？~~ → **已 SETTLED：Lazy Provisioning**（§15.1/§15.2）。**仅剩**「第一次真正需要」的精确定义 | 实现阶段 |
| 1b | **Reopen 的 6 个细节**（谁可 reopen / 何时新建 Task / 新 Iteration ID / 是否恢复原 Workspace / 旧 Lock 是否失效 / Permission 是否重评）—— 见 §15.9 | Task / Iteration 设计讨论 |
| 1c | **Task Origin & Task Lineage** —— `OPEN DESIGN TOPIC`，见 §15.11。需定义 Task Origin / Task Creator / Parent Task / Root Task / Task Lineage / Iteration 及其关系 | **下一轮专门讨论** |
| 2 | ~~Workspace lifecycle 的完整状态集~~ → **已 SETTLED**：`UNPROVISIONED → PROVISIONING → READY → FROZEN → ARCHIVED`（§15.3） | 转换触发条件仍待实现阶段确认 |
| 3 | ~~Task 完成后是否冻结？~~ → **已 SETTLED**：`COMPLETED/FAILED/CANCELLED → FROZEN`（§15.5），**不删除**（§15.6）。**何时 → `ARCHIVED` 仍 OPEN** | Archive policy |
| 4 | ~~Lock lease / 崩溃恢复~~ → **v1 已确定**，见 §16 + ADR-0021。**仅剩 Future 部分 OPEN**（Guardian / Automated Recovery / Crash Detection / Lease·Renewal） | 见 §21.2 |
| 5 | **多资源锁** —— 一个操作需要改多个文件时怎么办？ | 死锁避免 vs 原子性 |
| 6 | **目录锁语义** —— 锁一个目录是否等于锁其下所有资源？ | 与 #5 相关 |
| 7 | **Agent 中途加入 / 退出** —— 权限、锁、已写资源如何处理 | §9.3 权限授予模型 |
| 8 | **权限集合与授予规则** —— 见 §9.2 / §9.3 | §19 执行模型 |
| 9 | **Artifact 设计** —— 见 §17.2 | — |
| 10 | **逻辑资源 id 的确切形式** —— 见 §8.3 | Backend 设计 |
| 11 | **并发创建资源的具体 SQL / CAS 形态** —— 见 §12.4 | 实现阶段 |
| 12 | **Workspace Root 的物理布局** | 实现阶段 |

### 21.1 Lock Recovery 的状态变化（本次更新）

| | 状态 |
|---|---|
| **v1 治理方式** | ✅ **`SETTLED`** —— Explicit Lock + Owner Unlock + Human Force Unlock + Audit（§16） |
| Guardian Agent | `FUTURE` |
| Automated Recovery | `FUTURE` |
| Crash Detection | `FUTURE` |
| Lease / Renewal | `FUTURE`（且**不得**因为 Guardian 而提前实现，§16.5.8） |

> ⚠️ **不要把未来的自动恢复机制误写成当前已确定实现。**
>
> Lock Recovery 解决了「v1 用什么治理方式」，
> **不代表**所有 Lock 数据结构已经实现 ——
> 生产代码里**一行 Lock 都没有**（§20）。

### 21.2 为什么其余问题继续保留 OPEN

规范明确要求本次**不要顺手决定**：Workspace creation timing /
full lifecycle / Task completion freeze / Archive policy /
Agent join·leave / Multi-resource lock / Directory lock / Artifact design。

理由同 §15.4：这些问题互相耦合，且取决于实现阶段才知道的事实。

### 21.3 处理原则（`SETTLED`）

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

### 22.1.5 Guardian / Observer Agent 在演进中的位置（`FUTURE`）

```
v1（`SETTLED`）：
    Explicit Lock
        ├── Agent self-unlock
        └── Authorized Human Force Unlock
                └── Audit Event

Future（`FUTURE`）：
    Guardian / Observer Agent
        └── Local Small Model + Runtime Facts
                ↓
            Observation
                ↓
            Assessment
                ↓
            Recovery Request
                ↓
            Policy / Authority
                ↓
            Actual Recovery
```

> **最重要的一条**：
> **Guardian 的判断不是系统事实，也不天然拥有最高权限。**
> （§16.5.5、§16.5.6）

### 22.1.8 生命周期与人的位置（`SETTLED` 模型 / `FUTURE` 实现）

```
Task
│
├── Shared Context        「我们共同知道什么」
│
└── Workspace             「我们共同操作什么」
      │
      ├── Permission
      └── Write Lock
              │
    ┌─────────┴──────────┐
    │                    │
Agent Unlock      Authorized Human
                       Force Unlock
                            │
                        Audit Event

Workspace Lifecycle：
    Logical Workspace
        ↓
    UNPROVISIONED → PROVISIONING → READY → FROZEN → ARCHIVED
                                            │
                                       允许 Reopen
                                            ↓
                                      Iteration N+1

Human 作为正式协作 Actor：
    Authorized Human
        ↓
    Workspace Protocol → Permission → Write Lock → Modification
```

**第一版不承担**：`Unauthorized Physical Modification Detection`。
**下一阶段独立主题**：`Task Origin / Task Lineage`。

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
