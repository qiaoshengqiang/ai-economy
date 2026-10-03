# Workspace Core v1 —— 实现设计（Step 2 产出）

> **本轮实现的设计依据。** 上游规范：
> [`TASK_WORKSPACE.md`](../TASK_WORKSPACE.md) · [`RESOURCE_LAYER.md`](../RESOURCE_LAYER.md) ·
> [`LOCK_MODEL.md`](../LOCK_MODEL.md) · [`RECOVERY_MODEL.md`](../RECOVERY_MODEL.md) ·
> ADR-0021 / 0025 / 0026 / 0027 / 0028

---

## 0. Step 1 调查结论（事实）

| 项 | 现状 |
|---|---|
| 测试基线 | **3130 passed, 30 deselected** |
| 表数 | **13** |
| 现有表 | `agents` · `wallets` · `ledger_transactions` · `llm_calls` · `communication_messages` · `communication_events` · `tasks` · `task_offers` · `task_agreements` · `task_executions` · `task_shared_contexts` · `task_context_revisions` · `task_context_changes` |
| `Database` API | `transaction(write=True)` · `execute` · `fetchone` · `fetchall` · `columns_of` · `migrate` · `table_names` |
| `core/task_workspace.py` | **只有 `TaskWorkspaceRef`**（一个 `workspace_id` 字符串，**无 I/O**） |
| `core/workspace.py` | Agent **Private** Workspace（有真实 I/O + 路径边界）—— **不要改** |
| 迁移机制 | `COLUMN_MIGRATIONS` · `DATA_MIGRATIONS` · `POST_MIGRATION_INDEXES`（幂等） |
| 并发底座 | SQLite `BEGIN IMMEDIATE`（单写者）· 每线程一个 `Database` 实例 |

### 0.1 ⚠️ 发现的冲突（1 处，已按任务书要求「先报告、最小修正」处理）

> **任务书 §48 要求**：`Explicit Lock` 是持久化事实，**Effective Lock 优先运行时解析/派生**，
> **不要同时维护两份容易漂移的状态**。
>
> **但 §36–§39 要求并发安全**：同一 File 的「检查 + 修改」必须**原子**。

**冲突点**：如果 Effective Lock **纯运行时派生**（每次向上遍历父目录），
那么「检查 Effective Lock == Agent」与「写入」之间**没有可锁的持久化锚点** ——
两个并发 edit 可能都通过检查。

**最小修正方案（不扩大模型）**：

```
Effective Lock 仍然是「派生」的 —— 它没有自己的表、没有自己的列。
但派生过程必须在 **同一个 SQLite 写事务** 内完成，
并且该事务**同时覆盖**：
    ① 解析 Effective Lock（读 resources 父链）
    ② 校验 base_revision
    ③ 写入新 revision
    ④ 写 audit + event
```

> **这满足两条要求**：Effective Lock 仍然**不持久化**（无第二份事实）；
> 原子性由**事务边界**保证，而不是由缓存保证。
>
> **对 File 的并发**：`BEGIN IMMEDIATE` 让写事务串行 —— 但注意任务书 §37
> **禁止全局串行**。所以还要加一层**资源级互斥**（见 §4）。

---

## 1. 模块划分

```
core/workspace_core/            ← 新增包（领域模型，不碰 storage）
├── __init__.py
├── resource.py                 Resource · ResourceType · ResourceState
├── lock.py                     Lock 语义 + Effective Lock Resolution（纯函数）
├── revision.py                 Revision
├── operation.py                OperationResult · ErrorCode
└── events.py                   WorkspaceEventType · Event/Audit 记录

core/workspace_store.py         ← 持久化（像 core/task_store.py 一样的唯一边界）
core/workspace_audit.py         ← Audit + Event 记录

services/workspace_gateway.py   ← Gateway（唯一受控入口）
services/workspace_concurrency.py ← 资源级互斥（见 §4）

storage/models.py               ← 新增 3 张表（见 §2）
```

> **依赖方向**：`services → core` · `core/workspace_store → storage`。
> **`services/` 不直接依赖 `storage/`**（沿用现有守卫规则）。

---

## 2. Storage 设计（3 张新表 → 共 16）

### 2.1 `workspace_resources`

```sql
CREATE TABLE IF NOT EXISTS workspace_resources (
    resource_id     TEXT PRIMARY KEY,      -- RES-<n>，**稳定身份**
    workspace_id    TEXT NOT NULL,
    parent_id       TEXT,                  -- NULL = workspace 根
    name            TEXT NOT NULL,
    path            TEXT NOT NULL,         -- 逻辑路径（非物理路径）
    resource_type   TEXT NOT NULL,         -- 'directory' | 'file'
    state           TEXT NOT NULL,         -- 'ACTIVE' | 'DELETED'
    revision        INTEGER NOT NULL,      -- 当前 revision number
    content         TEXT,                  -- file 内容（directory = NULL）
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
-- 索引
UNIQUE (workspace_id, path)
INDEX  (workspace_id, parent_id)
```

> **`path` 是派生自 `parent_id` + `name` 的缓存**（便于 list/解析），
> 但 **`resource_id` 才是身份** —— rename/move 改 path 不改 id。

### 2.2 `workspace_explicit_locks` ← **唯一的锁事实**

```sql
CREATE TABLE IF NOT EXISTS workspace_explicit_locks (
    resource_id   TEXT PRIMARY KEY,     -- 一个资源最多一条显式锁
    holder        TEXT NOT NULL,        -- agent_id
    kind          TEXT NOT NULL,        -- 'file' | 'directory'
    acquired_at   TEXT NOT NULL,
    acquired_by   TEXT NOT NULL         -- 操作者（Handoff 时 = from）
);
```

> 🔒 **只有 Explicit Lock 落盘。**
> **`Effective Lock` 永远派生** —— 它没有表、没有列、没有缓存。
> 这直接满足「不要维护两份会漂移的事实」。

### 2.3 `workspace_revisions`

```sql
CREATE TABLE IF NOT EXISTS workspace_revisions (
    revision_id     TEXT PRIMARY KEY,
    resource_id     TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    change_ref      TEXT,               -- 关联 operation_id
    change_type     TEXT NOT NULL       -- CREATE/EDIT/DELETE/RENAME/MOVE/HANDOFF/RELEASE
);
INDEX (resource_id, revision_number)
```

### 2.4 `workspace_audits` + `workspace_events`

```sql
CREATE TABLE IF NOT EXISTS workspace_audits (
    audit_id      TEXT PRIMARY KEY,
    operation_id  TEXT NOT NULL,
    workspace_id  TEXT NOT NULL,
    action        TEXT NOT NULL,        -- EDIT / EDIT_ATTEMPT / CREATE / ...
    actor         TEXT NOT NULL,
    resource_id   TEXT,
    result        TEXT NOT NULL,        -- SUCCESS / FAILED
    error_code    TEXT,
    detail        TEXT,                 -- JSON
    task_id       TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspace_events (
    event_id      TEXT PRIMARY KEY,
    workspace_id  TEXT NOT NULL,
    event_type    TEXT NOT NULL,        -- RESOURCE_CREATED / ... / RESPONSIBILITY_HANDOFF
    resource_id   TEXT,
    revision      INTEGER,
    actor         TEXT NOT NULL,
    detail        TEXT,                 -- JSON；**不含文件内容**
    created_at    TEXT NOT NULL
);
```

> **表数 13 → 16。** 迁移用现有 `POST_MIGRATION_INDEXES` 机制（幂等）。

---

## 3. Effective Lock Resolution（Step 4 —— 纯函数，可单测）

```python
def resolve_effective_lock(resource, ancestors, explicit_locks) -> str | None:
    """
    Rule 1  自身有 Explicit Lock        → 返回它
    Rule 2  自身没有                      → 沿 parent 链向上
    Rule 3  找到最近的显式 Directory Lock → 返回它
    Rule 4  更近的胜出                    → 由「从近到远」的遍历顺序保证
    Controller 同理（仅 Directory 有意义）
    """
```

> **纯函数**：输入是已经查出来的祖先链 + 显式锁表，**不访问数据库**。
> 这样 Step 8 可以对它做密集的单元测试。

**Controller 解析**：`resolve_effective_controller(dir)` —— **仅对 Directory 定义**。
File 的 `list()` 返回的 `effective_controller` =
**其父目录链上最近的 Controller**（满足「File 不需要 Controller」）。

---

## 4. 并发模型（Step 7 —— 任务书 §36–§41）

### 4.1 三层保证

| 层次 | 机制 | 保证 |
|---|---|---|
| **① 资源级互斥** | 进程内 `dict[(workspace_id, resource_id), threading.Lock]` | **同一 File 互斥**；不同资源不阻塞 |
| **② 目录结构串行** | 同一 `parent_id` 上的结构操作共用该目录的锁 | Namespace 一致，无中间损坏 |
| **③ SQLite 事务** | `db.transaction(write=True)`（`BEGIN IMMEDIATE`） | 检查+修改原子；Revision Race 确定失败 |

### 4.2 🔒 禁止全局串行

```
❌ Global Gateway Lock        ← 会让所有 Workspace 排队
✅ 资源级锁 + SQLite 事务
```

**不同 Workspace → 不同的 `(workspace_id, resource_id)` 键 → 天然并行。**
**不同 File → 不同的键 → 天然并行。**

### 4.3 ⚠️ 必须如实记录的底层瓶颈

> **SQLite 是单写者。** `BEGIN IMMEDIATE` 意味着**同一时刻只有一个写事务**。
>
> 这对**正确性没有影响**（竞争会确定地串行化），但**会影响并发度**：
> 两个不同 Workspace 的写操作在**底层仍会排队**。
>
> 任务书 §39 明确要求：「如果有这种瓶颈，请在代码/文档中明确，
> **而不要伪装成 Workspace Lock 语义**」。
>
> → **本设计如实记录**：Workspace 的**锁语义是资源级的**（W1 的锁冲突
> 不会让 W2 得到 `LOCKED_BY_OTHER_AGENT`），但**写吞吐受 SQLite 单写者限制**。
> 这是基础设施瓶颈，**不是** Workspace 语义。

---

## 5. Gateway 接口（Step 5）

```python
class WorkspaceGateway:
    # READ（不产生 Event / 不记录读 Audit）
    def list(self, workspace_id, path, agent_id) -> OperationResult
    def read(self, workspace_id, path, agent_id) -> OperationResult

    # RESOURCE
    def create(self, workspace_id, parent_path, name, resource_type, agent_id) -> OperationResult
    def edit(self, workspace_id, path, base_revision, content, agent_id) -> OperationResult
    def delete(self, workspace_id, path, agent_id) -> OperationResult
    def rename(self, workspace_id, path, new_name, agent_id) -> OperationResult
    def move(self, workspace_id, path, new_parent_path, agent_id) -> OperationResult

    # RESPONSIBILITY
    def handoff(self, workspace_id, path, from_agent, to_agent, reason, actor) -> OperationResult
    def release(self, workspace_id, path, agent_id) -> OperationResult

    # GOVERNANCE（可只做接口 + 状态 + 测试）
    def force_unlock(self, workspace_id, path, reason, actor) -> OperationResult
```

### 5.1 统一 `OperationResult`

```python
@dataclass
class OperationResult:
    operation_id: str
    success: bool
    result: dict | None      # resource_id / revision / ...
    error: dict | None       # code / message / current_revision / ...
    references: dict
```

### 5.2 结构化错误码

`STALE_REVISION` · `LOCKED_BY_OTHER_AGENT` · `NOT_TASK_PARTICIPANT` ·
`RESOURCE_NOT_FOUND` · `OUTSIDE_WORKSPACE` · `INVALID_PARENT` ·
`RESOURCE_BOUNDARY_VIOLATION` · `NOT_EFFECTIVE_LOCK_HOLDER` ·
`NOT_CONTROLLER` · `INVALID_RESOURCE_TYPE`

### 5.3 🔒 不提供的 API（任务书明确禁止）

```
❌ lock(resource)          —— Lock 是工作责任，不是 mutex
❌ git_commit / checkout / branch / merge
❌ 任何暴露宿主机物理路径的参数或返回值
```

---

## 6. 操作前置条件（速查）

| 操作 | 前置条件 |
|---|---|
| `list` / `read` | Agent ∈ Participants |
| `create` | `Effective Directory Lock(parent) == Agent` |
| `edit` | Participant + 是 File + `Effective File Lock == Agent` + `base_revision == current` |
| `delete`(file) | `Effective File Lock == Agent` **且** 父目录结构责任在 Agent |
| `delete`(dir) | `Effective Directory Lock == Agent` **且** 不越过更具体的 Resource Boundary |
| `rename` | `Effective Directory Lock(parent) == Agent` **且** 目标不是他人独立负责的资源 |
| `move` | source parent **与** dest parent 的 Directory Lock 都是 Agent |
| `handoff` | 当前责任人 / Controller / Human Governance |
| `release` | 当前持有该资源**显式**责任者 |

---

## 7. 测试规划（Step 8）

| 文件 | 内容 |
|---|---|
| `tests/test_workspace_lock_resolution.py` | **纯函数单测**（Explicit/Implicit/Effective · Rule 1–4 · Controller） |
| `tests/test_workspace_store.py` | 持久化 + 迁移 + 索引 |
| `tests/test_workspace_gateway.py` | 9 个操作 + 错误码 + Audit/Event |
| `tests/test_workspace_concurrency.py` | **真实并发**：多线程竞争同一 File |

### 7.1 🔒 真正测试并发（任务书 §45 明确要求）

```python
def test_two_agents_race_same_file():
    """两个线程**真实**竞争同一 File —— 必须恰好一个成功。"""
    barrier = threading.Barrier(2)
    # 两个线程各自 barrier.wait() 后同时 edit
    # 断言：success_count == 1
    #       loser.error.code in (STALE_REVISION, LOCKED_BY_OTHER_AGENT)
```

> **必须真的用线程 + barrier**，不能只串行调用两次假装并发。

---

## 8. 8 个验收场景 → 测试映射

| 场景 | 测试 |
|---|---|
| A 创建/读/建文件/编辑/Revision/Audit+Event | `test_scenario_a_*` |
| B Handoff 后 B 可读整个、可改自己的、不能改 A 的 | `test_scenario_b_*` |
| C A·B 同时改不同文件都成功 | `test_scenario_c_*` |
| D A·B 同时改同一 File 只有一个成功 | `test_scenario_d_*`（并发） |
| E B 基于 R17、A 先到 R18 → STALE_REVISION | `test_scenario_e_*` |
| F Directory Handoff 时 C 的 Explicit Lock 不变 | `test_scenario_f_*` |
| G rename/move 后 resource_id 不变 | `test_scenario_g_*` |
| H W1 竞争不破坏 W2 | `test_scenario_h_*` |

---

## 9. 本轮明确的 Open Questions（不自行拍板）

| # | 问题 |
|---|---|
| 1 | Root / top-level Directory `release()` 后没有父级 —— 状态如何处理（任务书 §25 明确不许随意分配） |
| 2 | 物理文件何时真正删除（留待 Git/Storage 阶段） |
| 3 | `force_unlock()` 本轮实现到什么程度 |
| 4 | 失败的 `delete` / `handoff` 是否也记 `*_ATTEMPT` Audit（edit 已明确要记） |
| 5 | 并发读与 move/delete 的精确一致语义（已定为「旧 Revision 或明确 not found」，具体选哪个） |

---

## 10. 本轮不实现（任务书 §3 / §46 / §47）

Authorization ACL · AuthorizationRequest · 通用权限集合 · Cascade Revocation ·
复杂 IAM/Role · 复杂 Visibility ACL · Multi-Agent 同文件并行编辑 ·
自动超时释放 Lock · 自动 Task Reassignment · 自动 Recovery ·
完整 AI Governance · 完整 Conversation Manager · Communication Economy Engine ·
Dependency Graph · Git Merge Conflict · 完整 Acceptance Pipeline · 复杂 Settlement ·
Recovery Automation · Git operations
