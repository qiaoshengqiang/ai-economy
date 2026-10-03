# RESOURCE_LAYER.md — Resource · Controller · Authorization · Delegation

> **状态：架构设计冻结（`SETTLED`）／实现为 `FUTURE`**
>
> 这份文档定义 **Workspace 内部的资源级控制模型**。
> 它是 [`TASK_WORKSPACE.md`](TASK_WORKSPACE.md) 的**下层细化**，不是替代品。
>
> 上游：[`AGENTS.md`](../../AGENTS.md) → [`ARCHITECTURE_CONSTITUTION.md`](ARCHITECTURE_CONSTITUTION.md)
> → [`TASK_WORKSPACE.md`](TASK_WORKSPACE.md) §8–§11 → **本文档**
> → [`adr/0025-resource-control-authorization-delegation.md`](adr/0025-resource-control-authorization-delegation.md)

| | |
|---|---|
| **文档状态** | 长期维护 |
| **实现状态** | **`FUTURE`** —— 零实现 |
| **当前阶段** | `Post-Step-4F / Pre-Task-Workspace` |
| **最后更新** | Resource 层核心设计 |

---

## 0. 怎么读这份文档

### 0.1 与 TASK_WORKSPACE.md 的分工

| 文档 | 回答 |
|---|---|
| [`TASK_WORKSPACE.md`](TASK_WORKSPACE.md) | Workspace **是什么** · 归属 · 隔离 · 生命周期 · Write Lock 基本语义 · **Permission ≠ Write Lock** |
| **本文档** | Workspace **内部**：Resource 怎么被**控制** · **授权** · **传递** · **撤销** |

> ⚠️ **不要在两份文档里各写一套 Permission 定义。**
> 本文档 §2–§4 与 TASK_WORKSPACE.md §8–§11 是**同一模型的上下层**。

### 0.2 四态标注

沿用 Constitution §0.2：`SETTLED` / `OPEN` / `FUTURE` / `NOT IMPLEMENTED`。

> **本文档全部内容都是 `NOT IMPLEMENTED`。**

### 0.3 一句话

> **「谁负责」·「谁有资格」·「现在轮到谁写」是三个不同的问题。**

---

## 1. 为什么 Workspace 需要 Resource 层（`SETTLED`）

### 1.1 只有 Workspace-level 权限是不够的

如果 Workspace 只有**一个**权限列表，那么：

```
❌ 参与 Task → 自动拥有整个 Workspace 的写权限
```

这在真实协作里立刻失效：

> 「B 负责前端，但这**不意味着** B 可以改 `backend/` 或
> `internal_secret.py`。」

### 1.2 所以需要第三层

```
Workspace            ← 受控资源边界（TASK_WORKSPACE.md）
    ↓
Resource             ← 本文档：资源级的控制与授权单位
    ↓
Controller / Authorization / Delegation / Write Lock
```

---

## 2. Resource（`SETTLED` 语义 / `OPEN` 字段细节）

### 2.1 第一版支持的类型

| 类型 | 说明 |
|---|---|
| **File** | 单个文件 |
| **Directory** | 目录（一个资源**范围**） |

### 2.2 未来可扩展

`Repository` · `Artifact` · `Test Result` · `Build` · `Patch` · `Dataset` · `Log` · 其他受控资源

### 2.3 建议抽象

```
Resource
    resource_id
    workspace_id
    path / logical_path
    resource_type
    controller                  ← §3
    authorization / delegation   ← §4 / §5
    current write lock           ← TASK_WORKSPACE.md §10
    version information
    audit information
```

> **具体字段可根据现有架构调整，但不要破坏这个核心语义。**
> 字段形式仍是 `OPEN`（见 §10）。

### 2.4 与 TASK_WORKSPACE.md §8 的关系

`TASK_WORKSPACE.md` §8 已经确立：

- Resource 是什么
- **Resource Identity 必须与物理路径解耦**（`SETTLED`）

本文档**在此之上**增加 `controller` 与 `authorization` 维度。

---

## 3. Controller / Control（`SETTLED` —— 本轮新增）

### 3.1 定义

> **Controller 表示：谁负责管理这组 Resource，
> 以及谁有权决定这组 Resource 是否可以继续授权给其他 Agent。**

### 3.2 转移

```
frontend/  controller = A
        ↓  A 把 frontend/ 的控制权交给 B
frontend/  controller = B
```

### 3.3 Controller 意味着什么

B 不只是「能写」，而是：

- **负责**这部分资源
- 可以**修改**其中内容
- 可以**创建**新的文件 / 目录
- 在自己拥有足够 `DELEGATE` 权限时，可以**继续授权**给其他 Agent
- 可以**收回**自己发出的下级授权
- 如需自己没有的其他资源，则**向上级 Controller / 授权者申请**

### 3.4 🔒 Control 必须有明确来源

> **Control 不能凭空出现。**

每一个 Controller 关系都必须能回答：

> **「你是从谁那里得到这个控制权的？」**

这直接推出 §5 的 Delegation 必须是**正式对象**而不是消息。

### 3.5 Controller ≠ Owner

| | Controller | Owner |
|---|---|---|
| 含义 | **被授权**管理这组资源 | 资源的最终归属 |
| 可否撤销 | ✅ 可以（§7） | ❌ 不适用 |
| 是否可传递 | ✅ 可以 | 不适用 |

> ⚠️ 当前设计倾向：**Control 是授权关系，不是不可撤销的永久所有权**（§7）。

---

## 4. Authorization（`SETTLED` 语义 / `OPEN` 枚举）

### 4.1 定义

> **Authorization 表示：某个 Agent 对某个 Resource / Resource Scope
> 被允许执行什么操作。**

### 4.2 候选权限（`OPEN` —— 未拍板）

| 权限 | 含义 |
|---|---|
| `READ` | 读 |
| `WRITE` | 写 |
| `CREATE` | 创建 |
| `DELETE` | 删除 |
| `EXECUTE` | 执行 |
| `DELEGATE` | **继续授权**（§5 的前提） |

> ⚠️ **第一版不要过度设计复杂 ACL**，但架构**必须允许资源级授权**。

### 4.3 🔒 授权不自动扩散

```
frontend/
    A = controller
    B = WRITE
```

> **并不意味着 C 也自动获得 WRITE。**

### 4.4 与 TASK_WORKSPACE.md §9 的关系

TASK_WORKSPACE.md §9 已确立「Permission 回答什么」与候选权限种类。
本文档补充的是：**授权从哪来**（Delegation）与**如何传递 / 撤销**。

---

## 5. Write Lock（`SETTLED` —— 强调与 Authorization 分离）

### 5.1 定义

> **Write Lock 表示：在当前时间点，谁拥有对某个 Resource 的实际写入控制。**

### 5.2 三者的经典对照

```
frontend/
    controller = B
    authorized writers:  B · C
    current write lock:  C
```

解读：

| 问题 | 答案 |
|---|---|
| 谁**负责**这组资源？ | **B**（Controller） |
| 谁**有资格**写？ | **B 和 C**（Authorization） |
| **现在轮到谁写**？ | **C**（Write Lock） |

> **B 和 C 都可能拥有写资格，但当前实际写入者只有 C。**

### 5.3 🔒 硬规则

> **Permission / Authorization 决定「你有没有资格」。
> Write Lock 决定「现在轮到谁写」。**
>
> **不要把 Authorization 和 Write Lock 混成一个概念。**

这与 [`TASK_WORKSPACE.md`](TASK_WORKSPACE.md) §11 的
**`Permission ≠ Write Lock`** 是同一条边界的两个视角。

### 5.4 完整锁语义

Write Lock 的持有 / 释放 / 撤销 / 状态集 / Human Force Unlock
见 [`TASK_WORKSPACE.md`](TASK_WORKSPACE.md) §10 与
[`ADR-0021`](adr/0021-explicit-write-lock-and-human-force-unlock.md)。
**本文档不重复，也不改变那些语义。**

---

### 5.5 三者的正式区分：Controller ≠ Authorization ≠ Write Lock

> **本节是四个概念分离的**收口**。**
> 规范要求：**不能把它们混成一个「锁」。**

| | Controller | Authorization | Write Lock |
|---|---|---|---|
| **回答** | 谁**负责**这组资源 | 谁**有资格**操作 | 现在**轮到谁**写 |
| **粒度** | 资源范围（含子树） | 资源范围 + 操作种类 | 单个资源（当前时点） |
| **时间性** | 持续（直到撤销 / 转移） | 持续（直到撤销 / 过期） | **瞬时**（持有即独占） |
| **可否转移** | ✅ 可转移（§3.2） | ✅ 可通过 Delegation 传递（§6） | ✅ 可释放 / 可被强解锁 |
| **来源** | 必须有来源（§3.4） | 必须有来源（Delegation 链） | 必须**先有资格**（否则不能获得） |
| **撤销影响** | 级联影响下级 Control | 级联失效下级 Delegation（§7.3） | 只影响**当前持有者** |

#### 三个独立的反例（说明为什么不能合并）

```
① Controller 不是 Authorization
     frontend/  controller = B
                authorized = { B }        ← B 甚至可能暂时没有 WRITE
     B 「负责」不等于「此刻有资格写」

② Authorization 不是 Write Lock
     frontend/  authorized writers = { B, C }
                current write lock =  C
     有资格 ≠ 正在写

③ Controller 不是 Write Lock
     frontend/  controller = B
                current write lock =  C   ← C 在写，但 B 负责
     负责的人不一定正在写
```

> 🔒 **Controller ≠ Authorization ≠ Write Lock。**
> 把它们合并成一个「锁」字段，
> 就再也无法回答「谁负责」与「谁在写」这两个不同的问题。

---

## 6. Delegation（`SETTLED` —— 本轮最重要的新增）

### 6.1 为什么需要

Agent 之间**不仅可以执行任务，也可以进行资源授权**：

```
A 负责整个 Task
    A 创建 frontend/
    A → 将 frontend/ 的 Control / 相关权限交给 B
        B 负责前端实现
            B 发现 frontend/components/ 适合让 C 开发
            B → 将 frontend/components/ 的部分权限交给 C
```

### 6.2 树结构

```
A
└── frontend/               ← B 从这里开始负责
    └── B
        └── frontend/components/   ← C 从这里开始负责
            └── C
```

> **Workspace 权限结构实际上是一棵 Delegation Tree。**

### 6.3 🔒 硬规则：权限不能放大

> ## **Delegated Authority ≤ Grantor Authority**

Agent **只能在自己已有的资源范围和权限范围内**继续授权。

**例**：A 给 B `frontend/ READ + WRITE + CREATE + DELETE + DELEGATE`

| B 可以给 C | B **不能**给 C |
|---|---|
| `frontend/components/ READ + WRITE` ✅ | `backend/` ❌（不属于 B） |
| 在自己权限允许时继续传 `DELEGATE` ✅ | `ADMINISTRATE_WORKSPACE` ❌（不属于 B） |

> **必须把这一规则写进架构 / 协议设计。**

### 6.4 Delegation 必须是**正式对象**，而不是普通消息

```
Delegation
    delegation_id
    grantor                  ← 谁给的
    grantee                  ← 给谁
    resource_scope           ← 范围
    granted_rights           ← 哪些权限
    parent_delegation        ← 上级授权（§7）
    created_at
    status
    task relationship        （如需要）
    audit information
```

> **关键语义**：系统必须能够回答
> **「为什么 Agent B/C 现在有权访问这个资源？」**

```
A → B → C
```

那么 **C 的权限来源必须可以被追溯**。

> 🔒 **不要只保存 `controller = C` 而不保存授权来源。**

---

## 7. Delegation Chain 与 Cascade Revocation（`SETTLED`）

### 7.1 Delegation Chain

```
Delegation D001:  A → B    scope = frontend/
Delegation D002:  B → C    scope = frontend/components/
                           parent = D001
```

于是可以审计：

> **「C 为什么能够操作 `components/`？—— 因为 `A → B → C`。」**

### 7.2 授权不是永久所有权

> 当前设计倾向：
> **Control / Delegation 是授权关系，而不是不可撤销的永久所有权。**

A 在**合理治理条件**下应该能够 `REVOKE` B 的授权。

### 7.3 🔒 Cascade Revocation

```
A → B → C

A 撤销 B 的上级 Delegation
    ↓
B 下发给 C 的 Delegation **也应失效**
```

> **上级授权撤销 → 下级 Delegation 级联失效。**

```
REVOKE D001
    ↓
D001 失效  →  D002（parent = D001）失效  →  C 失去权限
```

### 7.4 为什么必须级联

如果不级联，就会出现：

```
❌ A 已经撤销了 B
   但 C 仍然凭着「B 给的授权」在写 frontend/components/
   —— C 的权限来源已经不存在，但权限还在
```

> 这直接违反 §6.3 的「权限不能放大」：
> **C 的权限不可能比它的来源更持久。**

---

## 8. 主动授权 vs 申请授权（`SETTLED` —— 两种不同机制）

### 8.1 Active Grant（主动授权）

授权者**主动**执行：

```
GRANT / DELEGATE
    resource_scope = frontend/
    grantee        = B
```

**授权立即建立。**

### 8.2 Request（申请授权）

> 🔒 **Agent 不能因为任务需要就自行扩大权限。**

```
B 正在开发 frontend，但需要访问 shared/design-system/
B 没有权限
    ↓
B 应该：AuthorizationRequest → 提交给拥有相应权限的 Agent（例如 A）
```

```
AuthorizationRequest
    request_id
    requester
    resource_scope
    requested_rights
    task_id
    reason
    status
    approver
```

**流程**：

```
REQUEST → APPROVE / REJECT → 若批准则产生正式 GRANT / DELEGATION
```

> 🔒 **不要简单把它实现成一条普通 Message。**

（与 [`TASK_ORIGIN_LINEAGE.md`](TASK_ORIGIN_LINEAGE.md) §13
「Task Creation ≠ Message」是同一条原则。）

### 8.3 为什么两者必须分开

| | Active Grant | Request |
|---|---|---|
| 谁发起 | **授权者** | **需要权限的人** |
| 是否立即生效 | ✅ 是 | ❌ 需批准 |
| 是否可被拒绝 | 不适用 | ✅ 可以 |
| 风险 | 授权者主动判断 | **申请人可能高估自己的需要** |

---

## 9. Task ↔ Authorization 的关系（`SETTLED`）

### 9.1 两者应该结合

```
Root Task T1 → A

A 创建 Child Task  T1.2 「完成前端页面」  Assignee = B
同时：
A → Delegates Control / Authorization → frontend/ → B

于是 B 同时拥有：Task responsibility + Resource authority
```

然后：

```
B 创建 T1.2.1 「完成公共组件」  Assignee = C
同时：
B → Delegates → frontend/components/ → C
```

### 9.2 两条层次结构并存

```
Task hierarchy                    Authorization hierarchy
T1                                A
└── T1.2        → B               └── frontend/        → B
    └── T1.2.1  → C                   └── components/  → C
```

### 9.3 互相引用

Child Task 可以拥有：

```
resource_scope
authorization_source
```

从而表示：

> **「为什么这个 Agent 可以操作这些资源？
> —— 是因为哪个 Task + 哪条授权链？」**

### 9.4 🔒 但**不自动等价**

| ❌ 错误推理 | ✅ 正确 |
|---|---|
| 拿到 Task → 自动拿到资源权限 | Task responsibility 与 Resource authority **可以关联，但不自动等价** |
| 完成 Task → 权限自动扩散给所有 Agent | **Agent 完成 Task 不应自动导致资源权限被无条件扩散** |

> 这条与 [`TASK_ORIGIN_LINEAGE.md`](TASK_ORIGIN_LINEAGE.md) §7.3
> 「Child Task 默认不继承 Parent 的整个 Workspace」一致。

---

## 10. 权限粒度与继承（`SETTLED` 原则 / `OPEN` 精确规则）

### 10.1 不要只有 Workspace-level ACL

应该允许：

```
Workspace → Directory → File
```

### 10.2 继承 + 覆盖

```
frontend/                     B = WRITE
    ↓ 下面默认继承
frontend/internal_secret.py   B = NONE      ← 覆盖
```

> **权限可以继承，但越靠近底层可以越具体。**

### 10.3 ⚠️ 第一版不要过度设计

> **第一版不要为了权限继承而引入过于复杂的规则系统。**
>
> 精确的继承 / 覆盖规则仍是 `OPEN`（见 §11）。

---

## 11. 与 SVN 思想的关系（`SETTLED`）

借用 SVN 式思想：

> **共享资源并不意味着所有人天然拥有修改权。**

但 AI Economy OS **不是**简单复制 SVN。它需要进一步记录：

```
Resource → Controller → Authorization → Delegation
        → Write Lock → Task → Audit
```

即：**「谁负责 · 谁可以操作 · 谁可以继续授权 ·
当前谁在写 · 为什么拥有权限 · 做了什么」全部可追踪。**

---

## 12. 未确定的问题（`OPEN`）

> ⚠️ **以下问题本轮没有决定。不要凭感觉实现。**

| # | 问题 |
|---|---|
| 1 | **Write Lock 的具体粒度** —— Workspace / Directory / File？ |
| 2 | **并行编辑** —— 多个 Agent 同时写同一资源时如何处理 |
| 3 | **Directory Lock 与 File Lock 的关系** |
| 4 | **Git Worktree 如何接入** 这条链 |
| 5 | **Permission inheritance 的精确规则**（含覆盖与冲突） |
| 6 | **Task 完成后授权是否自动回收** |
| 7 | **Controller 转移机制** —— `<div>` 具体协议与治理条件 |
| 8 | **Human 在 Delegation Tree 中的特殊角色** |
| 9 | Resource 字段的最终形式（§2.3） |
| 10 | 权限枚举的最终形式（§4.2） |
| 11 | 是否需要新 Message Type（§13） |
| 12 | Revoke 的治理条件 —— 什么算「合理治理条件」（§7.2） |

---

## 13. 协议影响（`OPEN` —— 本轮不新增消息类型）

### 13.1 候选（**研究，不是决定**）

```
AUTHORIZATION_REQUEST
AUTHORIZATION_GRANT
AUTHORIZATION_REVOKE
DELEGATION_CREATED
DELEGATION_REVOKED
```

### 13.2 🔒 但不要为了「看起来完整」而随意增加消息类型

> **如果现有协议已经可以承载某些语义，就优先复用现有设计。**

判断顺序：

```
① 现有 Message Type 能否承载？      → 复用
② 现有 payload 能否扩展？           → 扩展
③ 都不行                            → 才考虑新增
```

**本轮结论**：`OPEN` —— 留到 Workspace 实现前的协议设计轮次。

---

## 14. 测试需求（设计，不实现）

> 实现阶段至少需要覆盖以下 15 条。
> 现在**只登记需求**，不写测试。

| # | 场景 |
|---|---|
| 1 | A 创建资源，默认由 A 控制 |
| 2 | A 主动授权 B |
| 3 | B 可以在自己范围内继续授权 C |
| 4 | **B 不能授权超出自己范围的资源** |
| 5 | **B 不能自己给自己增加权限** |
| 6 | B 没权限时可以向 A 发起 Authorization Request |
| 7 | A 批准后产生正式 Delegation |
| 8 | A 拒绝后 B 仍不能访问 |
| 9 | A 撤销 B 后，B 失去授权 |
| 10 | **A → B → C 时，A 撤销 B 后 C 的下级授权也级联失效** |
| 11 | **Authorization 和 Write Lock 独立** |
| 12 | 拥有 WRITE 不代表当前自动拥有 Write Lock |
| 13 | Controller 与普通 Writer 区分 |
| 14 | Task responsibility 与 Workspace authorization 可关联但**不自动等价** |
| 15 | Agent 完成 Task **不自动**导致资源权限扩散给所有 Agent |

---

## 15. 完整关系图

```
Workspace
    ↓
Resource
    ↓
Controller          谁负责
    ↓
Authorization       谁有资格（READ/WRITE/CREATE/DELETE/EXECUTE/DELEGATE）
    ↓
Delegation          授权从哪来（可成树 · 可追溯 · 可级联撤销）
    ↓
Write Lock          现在轮到谁写
    ↓
Task                为什么需要这些资源
    ↓
Audit               做过什么
```

```
Agent A
    ↓
Task T1
    ↓
Resource Control（frontend/）
    ↓
Agent B
    ↓
Child Task T1.2
    ↓
Resource Delegation（frontend/components/）
    ↓
Agent C
```

---

## 16. 一句话

> **资源不只是「谁能写」，
> 而是「谁负责、谁有资格、从谁那里得到、现在轮到谁、为什么、做过什么」。**
