# ADR-0025：Controller / Authorization / Delegation / Write Lock 必须四者分离

- **状态**：⚠️ **第一版核心地位已被 [ADR-0027](0027-workspace-responsibility-model-simplification.md) 取代**
  —— 本 ADR 的 **Authorization / Delegation** 降级为 `FUTURE`，
     **Cascade Revocation** 第一版**移除**。
     本文档**保留为历史记录与未来扩展依据**。
- **时间**：Resource 层核心设计
- **相关**：ADR-0020（Workspace 共享资源模型）· ADR-0021（显式 Write Lock + Human Force Unlock）· ADR-0024（Human ≠ Task Creator）
- **规范**：[`RESOURCE_LAYER.md`](../RESOURCE_LAYER.md)

## Context

ADR-0020 确立了 Workspace 是**受控共享资源边界**，
ADR-0021 确立了 Write Lock 的显式持有模型，
`TASK_WORKSPACE.md` §11 已明确 **`Permission ≠ Write Lock`**。

但真实协作暴露出一个**更细的层次**：

> 「B 负责前端」这句话里其实藏了**三个**不同的问题。

```
① 谁负责这组资源？          → Controller
② 谁有资格写？              → Authorization
③ 现在轮到谁写？            → Write Lock
```

现有设计只有 ②③，**没有 ①**。

而 ① 缺失会立刻导致两个无法回答的问题：

1. **「B 能不能把 `frontend/components/` 交给 C？」**
   —— 没有 Controller 概念时，无法判断 B 是否有资格转授。
2. **「C 为什么有权访问这个资源？」**
   —— 只保存 `authorization` 而不保存**授权来源**时，权限链断了。

同时，如果不给「转授」定规则，就会出现：

```
❌ B 把自己都没有的 backend/ 授权给 C
❌ B 给自己增加权限
❌ A 撤销了 B，但 C 还在用 B 给的授权写文件
```

## Decision

### 一、引入 Resource 层

Workspace 内部进一步管理 Resource。第一版支持 **File** / **Directory**；
未来可扩展 Repository · Artifact · Test Result · Build · Patch · Dataset · Log。

```
Resource
    resource_id · workspace_id · path / logical_path · resource_type
    controller · authorization / delegation · current write lock
    version information · audit information
```

> 字段形式仍是 `OPEN`，但**核心语义不能破坏**。

### 二、引入 **Controller / Control**

> **Controller 表示：谁负责管理这组 Resource，
> 以及谁有权决定这组 Resource 是否可以继续授权给其他 Agent。**

Controller **不只是「能写」**，而是：负责 · 可修改 · 可创建 ·
在自己有足够 `DELEGATE` 权限时可继续授权 · 可收回自己发出的下级授权 ·
需要别的东西时向上级申请。

> 🔒 **Control 必须有明确来源，不能凭空出现。**

**Controller ≠ Owner**：Control 是**授权关系**，不是不可撤销的永久所有权。

### 三、Authorization 保持为「资格」

候选权限：`READ` · `WRITE` · `CREATE` · `DELETE` · `EXECUTE` · `DELEGATE`。

> 🔒 **授权不自动扩散**：`frontend/` 上 B = WRITE **不意味着** C 也自动获得 WRITE。

第一版**不要过度设计复杂 ACL**，但架构必须允许资源级授权。

### 四、Write Lock 保持为「当前实际写入控制」

```
frontend/
    controller = B              ← 谁负责
    authorized writers: B · C   ← 谁有资格
    current write lock: C       ← 现在轮到谁
```

> 🔒 **`Permission / Authorization` 决定「你有没有资格」。
> `Write Lock` 决定「现在轮到谁写」。**

这与 `TASK_WORKSPACE.md` §11 的 `Permission ≠ Write Lock` 是同一边界。
**本文档不改变 ADR-0021 的任何锁语义。**

### 五、引入 **Delegation** 作为正式对象

```
Delegation
    delegation_id · grantor · grantee · resource_scope · granted_rights
    parent_delegation · created_at · status · task relationship
    audit information
```

**权限结构是一棵 Delegation Tree**：

```
A
└── frontend/      → B
    └── components/ → C
```

> 🔒 **关键语义**：系统必须能回答
> **「为什么 B/C 现在有权访问这个资源？」**
> ❌ 不要只保存 `controller = C` 而不保存授权来源。

### 六、🔒 硬规则：`Delegated Authority ≤ Grantor Authority`

Agent **只能在自己已有的资源范围和权限范围内**继续授权。

| B 可以给 C | B **不能**给 C |
|---|---|
| 自己范围内的子集 | `backend/`（不属于 B） |
| 在权限允许时继续传 `DELEGATE` | `ADMINISTRATE_WORKSPACE`（不属于 B） |

**必须写进架构 / 协议设计。**

### 七、Delegation Chain 与 **Cascade Revocation**

```
D001: A → B   scope = frontend/
D002: B → C   scope = frontend/components/   parent = D001
```

> **上级授权撤销 → 下级 Delegation 级联失效。**

```
REVOKE D001 → D001 失效 → D002（parent=D001）失效 → C 失去权限
```

**为什么必须级联**：不级联会出现「C 的权限来源已不存在，但权限还在」，
直接违反 §六 —— **C 的权限不可能比它的来源更持久**。

### 八、主动授权（Active Grant）与申请授权（Request）是两种机制

| | Active Grant | Request |
|---|---|---|
| 谁发起 | **授权者** | **需要权限的人** |
| 是否立即生效 | ✅ | ❌ 需批准 |

```
REQUEST → APPROVE / REJECT → 若批准则产生正式 GRANT / DELEGATION
```

> 🔒 **Agent 不能因为任务需要就自行扩大权限。**
> 🔒 **不要简单把 AuthorizationRequest 实现成一条普通 Message。**

### 九、Task ↔ Authorization 可关联，但**不自动等价**

Child Task 可以拥有 `resource_scope` 与 `authorization_source`，
表示「因为哪个 Task + 哪条授权链」。

```
Task hierarchy              Authorization hierarchy
T1                          A
└── T1.2        → B         └── frontend/      → B
    └── T1.2.1  → C             └── components/ → C
```

> 🔒 **拿到 Task ≠ 自动拿到资源权限。**
> 🔒 **完成 Task 不自动导致权限扩散给所有 Agent。**

这与 ADR-0020 的 `Task Shared Context ≠ Task Workspace`、
`TASK_ORIGIN_LINEAGE.md` §7.3「Child 不继承 Parent 整个 Workspace」一致。

### 十、权限粒度：Workspace → Directory → File（可继承 + 可覆盖）

```
frontend/                    B = WRITE
    ↓ 默认继承
frontend/internal_secret.py  B = NONE     ← 覆盖
```

> ⚠️ **第一版不要为了权限继承而引入过于复杂的规则系统。**
> 精确规则仍是 `OPEN`。

### 十一、借鉴 SVN 思想，但记录更多

> **共享资源并不意味着所有人天然拥有修改权。**

但 AI Economy OS 需要进一步记录：

```
Resource → Controller → Authorization → Delegation
        → Write Lock → Task → Audit
```

### 十二、协议：**本轮不新增消息类型**

候选（`AUTHORIZATION_REQUEST` / `AUTHORIZATION_GRANT` /
`AUTHORIZATION_REVOKE` / `DELEGATION_CREATED` / `DELEGATION_REVOKED`）
**只研究，不决定**。

> 🔒 **不要为了「看起来完整」而随意增加消息类型。**
> 判断顺序：① 现有 Message Type 能承载？→ 复用
> ② 现有 payload 能扩展？→ 扩展 ③ 都不行 → 才新增

## Consequences

**正面**：

- 「谁负责 / 谁有资格 / 现在轮到谁」**三个问题各有答案**
- 权限来源**可追溯**（Delegation Chain）→ 能回答「C 为什么有权」
- `Delegated Authority ≤ Grantor Authority` 阻止**权限放大**
- Cascade Revocation 阻止**权限比来源更持久**
- 资源级授权让「参与 Task ≠ 全 Workspace 写权限」成立
- 与 ADR-0020/0021 兼容 —— 是**细化**，不是推翻

**负面 / 代价**：

- **实现复杂度显著上升** —— 从「一个权限表」变成
  Controller 关系 + Delegation Tree + Chain + 级联撤销 + 请求流程
- **Delegation Tree 需要持久化**，未来很可能需要新表
- Cascade Revocation 的**性能与一致性**是真实工程问题
- 权限继承 + 覆盖的**精确规则未定** → 实现前仍需一轮设计
- 「合理治理条件」下才能 Revoke 这句话**目前不可执行**（§12 #12）

**因此不能做的事**：

- ❌ 不许把 Controller / Authorization / Write Lock **合并成一个「锁」**
- ❌ 不许让 Control **凭空出现**（每个 Control 必须有来源）
- ❌ 不许授权**超出自己的范围**（`Delegated Authority ≤ Grantor Authority`）
- ❌ 不许只保存结果而不保存**授权来源**
- ❌ 不许让下级授权在上级撤销后**仍然有效**
- ❌ 不许让 Agent **自己给自己**增加权限
- ❌ 不许把 `AuthorizationRequest` 当成普通 Message
- ❌ 不许让 Task responsibility **自动等价**于 Resource authority
- ❌ 不许为了「看起来完整」随意增加 Message Type

## Future evolution

```
今天（本 ADR）：
    四个概念已分离（SETTLED）
    Resource 层语义已定
    Delegation Tree / Chain / Cascade Revocation 已定
    12 个细节仍 OPEN（RESOURCE_LAYER.md §12）

下一步（专门讨论，再实现）：
    Write Lock 粒度 · Directory Lock vs File Lock ·
    并行 Agent 修改与冲突处理 · Git Worktree 接入 ·
    Permission inheritance 精确规则 · Message Type 决策

更远：
    Repository / Artifact / Build / Patch 等 Resource 类型
    Controller 转移的完整治理机制
    Human 在 Delegation Tree 中的角色
```

## Guard

文档守卫（含反向测试）必须能抓：

| 违规 | |
|---|---|
| 把 Controller / Authorization / Write Lock 合并成一个概念 | ✅ |
| 授权超出自己的范围 | ✅ |
| 不保存授权来源（只存 `controller = C`） | ✅ |
| 上级撤销后下级仍有效 | ✅ |
| 把 AuthorizationRequest 写成普通 Message | ✅ |
| 让 Task responsibility 自动等价于 Resource authority | ✅ |
| 把 Resource 层写成已实现（`CURRENT`） | ✅ |
| 为了「看起来完整」随意增加 Message Type | ✅ |

**不使用 substring-only 检查。**
