# AGENTS.md — AI Economy OS Developer Context

> **这是一个 AI Coding Agent 的入口文件。**
> 花几分钟读完它，你就知道这个项目是什么、当前做到哪、什么不能动。
>
> **完整的架构 Source of Truth 在
> [`docs/architecture/ARCHITECTURE_CONSTITUTION.md`](docs/architecture/ARCHITECTURE_CONSTITUTION.md)。**
> 本文档**只做导航**，不复制它的内容。
>
> 两者矛盾时，以 Constitution 为准。

---

## 1. 这个项目是什么

**AI Economy OS** —— 一个可以从**单机双 Agent**逐步演化为
**多 Agent、多机器、跨网络**，具有**独立身份、能力、记忆、
经济行为和自主协作能力**的 **Agent Operating Environment**。

**它不是**：Chatbot · Multi-Agent Demo · LLM 工作流 · 并排跑 Agent 的脚本。

> **Agent 是独立的经济主体，不是被调用的函数。**
> 系统负责：协议 · 状态 · 业务硬约束 · 持久化 · 消息路由；
> 系统**不**负责：替 Agent 决定「你应该怎么谈价」。

---

## 2. 当前阶段

| | |
|---|---|
| **阶段** | `Workspace Core V1 已冻结 / Change Pipeline Phase 1 已实现` |
| **测试基线** | **3592 passed, 2 skipped, 30 deselected** |
| **协议版本** | `protocol_version = "1"` |
| **命令数** | 17（`./dev.sh` 查看全部） |
| **数据库表** | **21** |
| **工作区状态** | 应当干净 |

### 已完成

```
Step 1·2/2.5  Agent + Economy 骨架 · Brain / LLM 抽象 + 真实 DeepSeek Provider
Step 3/3.6/3.7 Communication + Identity / Address / Transport / Protocol 冻结
Step 4A–4C    Task Core + Lifecycle + TaskWorkspaceRef + Money ·
              Capability + AgentDirectory · DecisionPolicy + 谈判 + TaskAgreement
Refactor      Task 应用层拆分（门面 / Handler / Mapper / Negotiation）
Step 4D/4D.1  START / SUBMIT / VERIFY / COMPLETE + 幂等 + 乐观锁 + 事务原子性
Phase 0·4E   真实多进程验证 + Message Claim Lease · Constitution + Shared Context 设计
Step 4F      Task Shared Context / Alignment 最小可运行原型
Workspace v1  资源树 · Controller · Explicit/Effective Lock · Revision · Audit ·
              Event · Gateway · 资源级并发 · Hardening · 组合根接线 · 20 Agent 并行
Change Pipeline  Phase 1 WorktreeManager · hello-demo（真实 LLM 端到端）  ← 当前
```

### 下一步

> **下一阶段：Change Pipeline 的 Patch**（Test / Acceptance / Merge 在其后）。
> Worktree Manager 已实现 —— 见 [`CHANGE_PIPELINE_V1_DESIGN.md`](docs/architecture/implementation/CHANGE_PIPELINE_V1_DESIGN.md)。

---

## 2.5 ⚠️ Current V1 vs Future Human-facing Architecture

> **V1 先把 Agent Economy 核心闭环跑通，不是先解决 Human Experience。**

| | **`CURRENT` / V1** | **`FUTURE`** |
|---|---|---|
| 入口 | **Professional User**（能明确提任务） | 普通用户 + **Front Agent** |
| 链路 | `User → Agent A → Root Task → 自执行 / 协作 → Result → 验收` | `Human → Front Agent → Agent Economy` |
| Human 角色 | Requester · 约束 · 预算 · 验收 · 反馈 | 模糊意图 · 方案选择 · 经济承诺 |

**V1 必须避免的 4 个错误假设**：① ❌ `Human → Agent A` **不是**永久架构，
只是 V1 部署方式；② ❌ Human **不是** Task Creator（ADR-0024）；
③ ❌ 不硬编码 `Task.creator = human` —— **Front Agent 也是 Agent**，
不该有 `HumanTask` / `Front-created Task`；④ ❌ Front Agent **不是**中央
Coordinator —— 只做 `Human → OS` 入口，Agent 间仍可直接协作。

> **兼容性 ≠ 提前实现**：`V1 简单 + 边界清楚 + 接口稳定 = 可扩展`。

---

## 3. 关键架构（一屏看懂）

```
                    main.py（组合根）
                          │
          ┌───────────────┼───────────────┐
          ▼               ▼               ▼
      Economy        Communication      Brain / LLM
     (Wallet/Ledger)  (Message/Transport)  (外部生产资源)
          │               │
          └───────┬───────┘
                  ▼
           services/（应用层：消息 → 领域）
                  │
          TaskService（薄门面 / 路由）
                  │
          Handlers（按消息类型）
             ├──→ TaskMessageMapper        协议 payload ↔ 应用数据
             ├──→ TaskNegotiationService   议价
             └──→ TaskExecutionService     执行
                  │
                  ▼
             Task Core（状态机）
                  │
                  ▼
             TaskStore（数据库唯一边界）
                  │
                  ▼
               SQLite
```

### 依赖方向（**真实 import graph**，AST 扫出来的）

```
agents/         →  core/ + llm/
services/       →  core/ + communication/     ← 不直接依赖 storage/
communication/  →  core/ + storage/
core/           →  storage/ + agents/         ← 不是「谁都不依赖」
storage/        →  core/ 的**叶子**（paths / logging_setup / config）
llm/            →  core/ + storage/
```

**四条有守卫的规则**（✅ 全部成立）：`core/` 与 `communication/`
**不**依赖 `services/`；`services/` **不直接**依赖 `storage/`（只经
`core/task_store`）；只有 `main.py` 调用 `register_type_handler`。

**三个最容易被写错的**：`services → storage` ❌（真实是经 `core/task_store`）；`core/` **确实**依赖 `storage/`；`storage/ → core/` 允许。

**两个已知例外**（历史遗留，**不要顺手改**）：① `core/economy.py` →
`agents/base_agent.py`（可达环）；② `storage/database.py` **函数内** import `core.config`。

> 重新扫描用 **AST**，见 [Constitution §4](docs/architecture/ARCHITECTURE_CONSTITUTION.md)。

---

## 4. Memory 四层

> ⚠️ **Agent Private Workspace 不在这四层里**（独立的资源/文件空间概念，见 §4.1）。

| Layer | 属于谁 | 状态 | 是什么 / 当前实现 |
|---|---|---|---|
| **1** Agent Private Memory | 单个 Agent | `DESIGNED` | 私人经验 / 判断 / 策略。**默认无权读**。有目录与极简文件读写（`Workspace.write_memory`），**无**结构化记忆、检索、经验积累 |
| **2** Task Shared Context | **Task** | **`CURRENT`** | 共同的任务世界模型。**已实现**：持久化 + 修订历史 + Alignment + 乐观锁。见 [`TASK_SHARED_CONTEXT.md`](docs/architecture/TASK_SHARED_CONTEXT.md) |
| **3** Task Workspace | Task | `FUTURE` | 实际共享的文件与代码。**只有 `TaskWorkspaceRef`** —— 一个 `workspace_id` 字符串。见 [`TASK_WORKSPACE.md`](docs/architecture/TASK_WORKSPACE.md) |
| **4** External / World State | 系统外 | `FUTURE` | 外部 API / 网络 / Git remote。**无** |

**`Memory ≠ Alignment`** —— Shared Context 是**结果**，Alignment 是**机制**。

---

### 4.1 ⚠️ Agent Private Memory ≠ Agent Private Workspace

**最容易被搞混的一对。** `core/workspace.py` **已经存在**，
它是**机器资源访问边界**，不是 Memory。

| | **Private Memory** | **Private Workspace** |
|---|---|---|
| 回答 | 这个 Agent **知道什么、相信什么、采用什么策略** | 这个 Agent **有哪些私有文件 / 代码 / 工具 / 实验空间** |
| 内容 | 经验 · 判断 · 偏好 · 私人知识 · 经济经验 · **议价策略** · 私人评价 · 私人规划 | 文件 · 代码 · 工具 · 运行数据 · 实验产物 |
| 位置 | `agents/<id>/memory/` | `agents/<id>/workspace/` |
| 状态 | **`DESIGNED`**（只有目录与极简文件读写，无结构化记忆） | **`CURRENT`** —— `Workspace.for_agent()`，由 `Economy` 创建 |
| 属于 | Memory 四层 **Layer 1** | **不在这四层里** |
| 边界 | 其他 Agent **默认无权读** | `WorkspaceViolationError` —— 路径不许跑出根目录 |

**三条不许越过的推论**：❌ 当成 Memory 的一层 ·
❌ 当成 Task Workspace（后者是任务共享的）·
❌ 因为「有 workspace 目录」就以为 Workspace Permission 已实现。

> **`Workspace ≠ Permission`。** `core/workspace.py` 给的是一个 Agent
> **对自己**目录的访问，**没有**「谁能看什么」的授权体系（`FUTURE`）。
> **有私有 workspace 目录 ≠ Task Workspace 已实现。**

```text
Memory（知道什么） ──┐                    Private Workspace（有哪些文件）
                     ├── 不在同一条链路上 ──┘
Task Shared Context ─┴─▶ Task Workspace ─▶ External / World State
```

---

## 5. Source of Truth

```
Domain State / TaskAgreement        ← 最权威（系统客观事实）
        >
Task Shared Context                 ← 共同确认的任务知识
        >
Communication History               ← 过去说过什么
        >
Agent Private Memory                ← 个体判断
        >
Agent Inference                     ← 推测（最不可靠）
```

**三条推论**：① Domain State 不能被 Memory 推翻；② 「说过」≠「仍有效」；③ 推测 ≠ 事实。

**「说过」≠「确认过」**：

```
A 说「我觉得用方案 B」   →   Communication History 里有这句话
                        →   Shared Context 里**没有** Decision Record
                        →   系统**不能**认为「团队已决定方案 B」
```

只有走完 `Proposal → Alignment → DecisionRecord → Revision`
才算共同决定。

---

## 6. 不可破坏的边界

**这些是项目的架构骨架。搞混它们 = 破坏架构。**

| 不等号 | 搞混的后果 |
|---|---|
| `Identity ≠ Address` | 搬家就要换身份 |
| `Message ≠ Transport` | 换传输就要改协议 |
| `Communication ≠ Business Logic` | 通信层开始判断「这个价格行不行」 |
| `Capability ≠ Permission` | 声明会写代码 → 自动获得文件写权限 |
| `Capability ≠ Economy` | 用能力推断财富 |
| `Wallet Ownership ≠ Machine Permission` | 有钱就能操作宿主机 |
| `Task Core ≠ Task Manager` | 领域层变成调度器 |
| `Task Shared Context ≠ Message History` | 把「说过」当成「确认过」· **有守卫** |
| `Task Shared Context ≠ Private Memory` | 私人偏见变成共识 |
| `Task Shared Context ≠ Task Workspace` | 把「认为是什么」和「实际有什么」混起来 |
| `Workspace ≠ Permission` | 有 `workspace_id` 就以为能读文件 |
| `Memory ≠ Alignment` | 只有共同存储，没有形成机制 |
| `Agreement ≠ Payment` | 成交凭证被当成支付记录 |
| `Budget ≠ Price` | 预算被当成成交价 |
| `DecisionPolicy ≠ Decision Execution` | 策略开始改状态 |
| `Domain ≠ Infrastructure` | `core/` 依赖 `services/` |
| **`Agent Strategy ≠ Negotiation Mechanism`** | 把 Agent 的价格阈值硬编码进系统 |
| **`Task Shared Context ≠ 第二套 Task Core`** | 出现两个都声称正确的 `status` · **有守卫** |
| **`Agent Private Memory ≠ Agent Private Workspace`** | 把「知道什么」和「有哪些文件」混起来 |
| **`Observation ≠ Conclusion`** | 把「2 小时没活动」当成「Agent 已死」 |
| **`Permission ≠ Force Unlock`** | 有 `WRITE` 就以为自己能强解锁 |
| **`Guardian 判断 ≠ Domain Fact`** | 让 Guardian 的评估自动改写 Task Core |
| **`Logical Workspace ≠ Physical Workspace`** | 以为 `workspace_id` 存在就代表物理目录已建 |
| **`Workspace Protocol ≠ Host OS Permission`** | 把 Write Lock 说成宿主机物理不可触碰 |
| **`Origin ≠ Creator`** | 把「谁提的需求」和「谁建的 Task」混为一谈 |
| **`Parent ≠ Root`** | 把「直接上一层」当成「整条谱系的起点」 |
| **`Funding ≠ Responsible ≠ Creator`** | 用一个 `owner` 表达三种责任 |
| **`Human Demand ≠ Task`** | 把「需求」当成「可执行的工作单元」 |
| **`Human ≠ Task Creator`** | 以为 Human 直接创建 Root Task |
| **`Demand Source ≠ Creator`** | 把「需求从哪来」和「谁建的 Task」混起来 |

---

## 6.1 Negotiation Strategy ≠ Negotiation Mechanism

```text
Agent Negotiation Strategy   ← 属于 Agent 自己
        ↓
DecisionPolicy               ← 把策略变成决定（纯函数）
        ↓
Decision                     ← ACCEPT / COUNTER / REJECT
        ↓
TaskNegotiationService       ← 系统机制（协议 + 硬约束 + 落地）
```

| | **Strategy**（Agent 自己） | **Mechanism**（系统） |
|---|---|---|
| 回答 | 「我**想怎么谈**」 | 「什么谈判行为在系统规则上**有效**」 |
| 内容 | 最低价 · 目标价 · 保留价 · 风险溢价 · 最大轮数 · 工作量偏好 · 现金压力 · 截止期敏感度 · 历史合作经验 | 协议合法性 · 谁能响应 · 轮次 · 报价是否**突破硬约束** · Agreement 落地 · 状态变化 · Offer 持久化 |

**`DecisionPolicy` 不能**修改 Task / Wallet / Ledger / Workspace，
也不能发 Message —— 它**只返回一个 `Decision`**。

```text
❌ 不要把 Agent 的价格阈值 / 风险偏好 / 让价策略硬编码进 TaskNegotiationService
❌ 不要让未来的 Strategy 绕过系统硬约束直接改 Task
```

> **实测**：`TaskNegotiationService` 里**没有**任何价格阈值
> （`accept_price` / `opening_ask` / `walk_away` 都不出现）；
> 那些参数全在 `RuleBasedDecisionPolicy` 上，来自**每个 Agent 自己的 config**。
> 有守卫在盯这条线。

---

## 6.2 Task Shared Context 不能成为第二套 Task Core

> **Task Core 是 Task 客观生命周期和领域状态的唯一权威来源。**

Shared Context 可以**引用、解释、总结、补充**共同知识，
但**不能建立一套可独立修改、独立生效的第二 Task 状态机**。

| 谁 | 拥有什么 |
|---|---|
| **Task Core**（`CURRENT`） | `task_id` · `state` · `budget` · `agreed_price` · `initiator` · `contractor` · lifecycle · `TaskAgreement` 的正式结果 |
| **Shared Context**（**`CURRENT`**） | objective · constraints · acceptance 的**解释** · decisions · assumptions · open questions · assignments · terminology · facts · verification feedback · **对 Domain State 的引用** |

```text
❌  Task.status                = SUBMITTED
    SharedContext.current_status = IN_PROGRESS   ← 两个都声称自己正确

✅  Task Core:       status = SUBMITTED
    Shared Context:  「当前处于等待发起方验收阶段。」   ← 语义解释
```

> **Shared Context 可以解释 Domain State，但不能取代 Domain State。**
> 有守卫禁止 Shared Context 复制 `status` / `agreed_price` / `contractor`
> 作为第二个可独立修改的事实源。

---

## 7. 当前禁止项（`FORBIDDEN NOW`）

**除非有明确的任务书，否则不要实现下面任何一项。**

| 领域 | 禁止内容 |
|---|---|
| **Workspace** | ✅ Core v1 **已冻结**（ADR-0029/0030/0031）· ❌ 仍禁止：Permission Engine · ACL · IAM · Role · Backend · 物理文件系统写权限 · Conversation Manager · Recovery Automation |
| **Git** | ✅ Worktree Manager **已实现**（ADR-0037）· ❌ 仍禁止：branch 自动化 · commit 自动化 · merge · Patch · Test Runner · Acceptance |
| **代码执行** | `subprocess` · shell · Docker · sandbox · 测试运行器 |
| **经济结算** | Payment · Escrow · Settlement · Wallet transfer |
| **通信可靠性** | Retry Queue · Retry Scheduler · ACK · Redis · Kafka · Celery |
| **网络** | P2P · 多机 · 分布式 |
| **生命周期 / 演化** | Heartbeat · Scheduler · Life Loop · Adaptive Negotiation · RL · 策略自学习。**不得因 Guardian / 未来自动恢复而提前实现** |
| **Guardian** | Guardian Agent 实体代码 · crash detector · 基于 inactivity 的自动判死 · 自动 Lock Expiry · Lease Renewal |
| **协调 / 清理** | TaskManager · Coordinator · GC · TTL · 自动物理删除 · Workspace Delete Lifecycle |
| **篡改监控** | Filesystem Watcher · Hash Tamper Detection · Kernel-level Enforcement · 阻止管理员直接改文件 |
| **Task Lineage** | `parent_task_id` · `root_task_id` · child task · task tree · task graph · delegation lifecycle · ChildTaskManager · TaskCreationService（概念见 [TASK_ORIGIN_LINEAGE.md](docs/architecture/TASK_ORIGIN_LINEAGE.md)，**实现为 `FUTURE`**） |
| **Human 中心化** | Human Task Manager · Human Task Coordinator · Human Central Scheduler · Human-driven Task Tree Management（**系统目标就是 Agent 能独立组织复杂工作**） |
| **Demand 实现** | Demand DB table · Demand Service · Demand API · Demand lifecycle · Demand persistence（概念见 [TASK_ORIGIN_LINEAGE.md](docs/architecture/TASK_ORIGIN_LINEAGE.md) §2.4，标 `OPEN DESIGN`） |
| **策略硬编码** | 「超过 X AIC 必须外包」·「必须自己完成至少 N%」·「Child 预算不得超过 Parent 的 N%」·「中间层不得盈利」·「必须找最便宜的承包者」（属于 **Agent Strategy**，不得写进系统） |

### 也不要过度设计

看到 "Shared Context" 就上 ❌ Blockchain · CRDT · Event sourcing · Vector DB ·
Knowledge graph · 分布式共识 · 多 agent 投票 · 复杂 ontology。**目标是建立最小但正确的边界。**

---

## 8. 开发流程

```
1. 读本文档 + Constitution
        ↓
2. 先调查真实代码（不要凭空设计）
        ↓
3. 跑基线：./dev.sh test   （必须是 3592 passed）
        ↓
4. 实施最小修改
        ↓
5. ./dev.sh test  → 全部通过
        ↓
6. 新增守卫 + **反向测试**
        ↓
7. 演示命令（如果这一步需要）
        ↓
8. 更新文档 / 写 ADR
        ↓
9. commit → 报告
```

### 三条硬要求

1. **先调查，再动手。** 不要在设计之前改代码。
2. **每一步结束跑全量测试。** 不是只跑自己改的那个文件。
   基线现在是 **3592 passed**。
3. **新增守卫必须配反向测试。** 把违规注入进去，确认守卫**真的变红**。

---

## 9. 测试要求

```bash
./dev.sh test                           # 全量（不含真实 API 测试）
./dev.sh test tests/test_xxx.py         # 单个文件
./dev.sh test tests/test_xxx.py -k 名字  # 单个测试
```

基线 **3592 passed / 2 skipped / 30 deselected** ·
守卫文件 10 个约 390 项 · 真实 API 测试 30 项（单独跑，需要 Key）。

**必须用 `./dev.sh test`，不要直接 `pytest`** —— 它会正确设置环境。

---

## 9.5 ⚠️ Context Loading Rule —— 不要一次加载全部文档

**为什么有这一节**：为了解决「上下文不足」，很容易反过来把所有架构文档
一次性塞进 Coding Agent —— 那浪费上下文，而且淹没真正相关的那几页。

```text
AGENTS.md → 判断任务领域 → 只读相关 Specification → 必要时读对应 ADR
```

| 我要做… | 重点读 | 不必读 |
|---|---|---|
| **Shared Context / Alignment** | [`TASK_SHARED_CONTEXT.md`](docs/architecture/TASK_SHARED_CONTEXT.md) + §4 · §6.2 · ADR-0019 | Storage / Reliability ADR |
| **SQLite / 并发 / 事务** | ADR-0012 · ADR-0014 · §3 · §11 | TASK_SHARED_CONTEXT.md |
| **Negotiation / DecisionPolicy** | §6.1 · `core/decision_policy.py` · `task_negotiation_service.py` | Workspace 相关 |
| **Execution（START/SUBMIT/…）** | `task_execution_service.py` · `core/task_execution.py` · `core/task.py` | Alignment 设计 |
| **Communication / 协议** | [`PROTOCOL.md`](PROTOCOL.md) · `communication/` · ADR-0002 · ADR-0003 | Economy |
| **Workspace / Lifecycle / Human** | [`TASK_WORKSPACE.md`](docs/architecture/TASK_WORKSPACE.md) + ADR-0020/0021/0022（§4.1 · §7 · §14.5 · §15） | TASK_SHARED_CONTEXT.md |
| **Task Origin / Task Lineage** | [`TASK_ORIGIN_LINEAGE.md`](docs/architecture/TASK_ORIGIN_LINEAGE.md) + **ADR-0023** —— 概念已定（`SETTLED`），**实现为 `FUTURE`** | — |
| **Guard / 测试** | ADR-0018 · `tests/*guards*.py` | — |
| **Git Worktree / Patch / Test / Acceptance / Merge** | [`CHANGE_PIPELINE_V1_DESIGN.md`](docs/architecture/implementation/CHANGE_PIPELINE_V1_DESIGN.md) + **ADR-0032…0037** —— 设计已定；**Worktree Manager 已实现（ADR-0037）**，其余仍 `DESIGNED` | Workspace Core 内部细节 |

**Constitution 只在三种情况下通读**：第一次接触项目 · 要**新增**一条边界 ·
要**推翻**某条边界。日常改一个模块读对应章节 + 相关 ADR 就够。

---

## 10. 必读文件（按优先级）

### 阅读顺序（**固定，不要颠倒**）

```
1. AGENTS.md（本文档）              ← 入口
2. ARCHITECTURE_CONSTITUTION.md    ← 长期 Source of Truth
3. Task-specific Specification     ← 按任务领域选（见下表）
4. Necessary ADR                   ← 只读相关的那几篇
```

> ⚠️ **不要跳过第 1 步直接读 Constitution。**
> AGENTS.md 的价值之一是告诉你**哪些不用读**。
> 也**不要把 Constitution 的内容复制回 AGENTS.md**。

### Task-specific Specification（按任务领域选）

| 我要做… | 规范 | 状态 |
|---|---|---|
| Task Shared Context / Alignment | [`TASK_SHARED_CONTEXT.md`](docs/architecture/TASK_SHARED_CONTEXT.md) | **`CURRENT`**（已实现） |
| **Task Workspace / Read / Lock** | [`TASK_WORKSPACE.md`](docs/architecture/TASK_WORKSPACE.md) + [`LOCK_MODEL.md`](docs/architecture/LOCK_MODEL.md) | **`FUTURE`** |
| 消息协议 | [`PROTOCOL.md`](PROTOCOL.md) | `CURRENT` |
| **Task Origin / Lineage / Funding** | [`TASK_ORIGIN_LINEAGE.md`](docs/architecture/TASK_ORIGIN_LINEAGE.md) | **`FUTURE`**（逻辑模型已定） |
| **Resource / Lock / Handoff** | [`RESOURCE_LAYER.md`](docs/architecture/RESOURCE_LAYER.md) + [`LOCK_MODEL.md`](docs/architecture/LOCK_MODEL.md) —— 第一版核心 = **`Controller + Lock + Handoff + Audit`**（Authorization / Delegation 降级，Cascade Revocation **移除**，ADR-0027） | **`FUTURE`** |
| **Recovery / Governance** | [`RECOVERY_MODEL.md`](docs/architecture/RECOVERY_MODEL.md) —— **异常检测 ≠ 治理决策**；当前 Human Confirmation，未来 AI Governance | **`FUTURE`** |
| **Read / Communication** | [`COMMUNICATION_MODEL.md`](docs/architecture/COMMUNICATION_MODEL.md) —— Read 靠 **Task Participation**（无 READ ACL）· Communication 是 **Agent 基础能力**（不绑 Task）· **Workspace Event ≠ Agent Message** · Runtime Inbox（ADR-0028） | 基础 Message `CURRENT` · 其余 `FUTURE` |

**Workspace 导航**（规范 §23.4）：

```text
Workspace
    ↓
docs/architecture/TASK_WORKSPACE.md        ← 正式模型（22 章）
    ↓
docs/architecture/adr/0020-*.md            ← 为什么是共享资源模型 + Permission≠Lock
    ↓
docs/architecture/adr/0021-*.md            ← 为什么 Lock 不用自动过期 + Human Force Unlock
    ↓
docs/architecture/adr/0022-*.md            ← Lazy Provisioning · Lifecycle · Human Participation
    ↓
docs/architecture/adr/0012 / 0014 / 0019   ← 并发 · 租约 · 对齐（前置）
```

**Task Origin / Lineage 导航**：

```text
Task Origin / Lineage
    ↓
docs/architecture/TASK_ORIGIN_LINEAGE.md   ← 7 个逻辑角色 + 深度规则
    ↓
docs/architecture/adr/0023-*.md            ← 为什么它们是独立概念
    ↓
docs/architecture/adr/0024-*.md            ← 为什么 Human ≠ Task Creator
```

> ⚠️ **Workspace Core v1 已实现**（见 [`WORKSPACE_CORE_V1_DESIGN.md`](docs/architecture/implementation/WORKSPACE_CORE_V1_DESIGN.md) + ADR-0029）；
> 其余 Workspace 能力仍是 `FUTURE`。
>
> | 已定（`SETTLED`） | 仍 `FUTURE` / `OPEN` |
> |---|---|
> | Write Lock v1：显式持有 + 锁主释放 + **Human Force Unlock** + 审计（ADR-0021） | Guardian · 自动恢复 · Reopen 的 6 个细节 |
> | Lifecycle：**Lazy Provisioning** + `UNPROVISIONED→…→ARCHIVED`；结束后 `FROZEN`（不删除），**允许 Reopen**（ADR-0022） | Workspace 实现 |
> | Human 是正式协作 Actor；out-of-band 修改**不阻止不监控**（ADR-0022） | out-of-band 检测 |
> | 7 个角色独立（`Origin`/`Creator`/`Parent`/`Root`/`Funding`/`Responsible`/`Participant`）；`Maximum Depth = 3`（**TEMPORARY**）（ADR-0023） | Child Task · Agent 自建 Root Task（12 个 Open 见 `TASK_ORIGIN_LINEAGE.md` §16） |

### 第二优先（理解当前实现）

4. [`core/task.py`](core/task.py) —— 状态机（**唯一的**生命周期定义）
4.5 [`core/task_shared_context.py`](core/task_shared_context.py) —— 共享上下文领域模型（Step 4F）
    · [`core/task_context_store.py`](core/task_context_store.py) —— 它的持久化 + 乐观锁
    · [`services/task_alignment_service.py`](services/task_alignment_service.py) —— Alignment
5. [`services/task_service.py`](services/task_service.py) —— 薄门面（看它多薄）
6. [`services/task_execution_service.py`](services/task_execution_service.py) —— 执行阶段
7. [`communication/message.py`](communication/message.py) —— 协议
8. [`storage/models.py`](storage/models.py) —— 表结构 + 迁移
9. [`main.py`](main.py) —— **组合根**（唯一把各层接起来的地方）

### 第三优先（动手之前）

10. `tests/*guards*.py` —— 你会碰到的边界检查
11. [`PROTOCOL.md`](PROTOCOL.md) —— 消息协议细节
12. [`COMMANDS.md`](COMMANDS.md) —— 全部命令

---

## 11. 当前技术债

> **不隐瞒。** 这些是**已确认存在**的问题，不是「以后可能要注意」。

| # | 技术债 | 严重度 | 说明 |
|---|---|---|---|
| 1 | **租约 vs 长任务** | 🔴 | 无 lease renewal。handler 超租约 → 别的 worker 接手 → **重复处理**（Workspace 阶段 = 重复文件副作用） |
| 2 | ~~多线程共享 `Database` 不安全~~ **✅ 已修复** | ✅ | 每线程一个 `_ThreadSession`（**连接 + 深度 + pid**）→ 事务真正独立且**无全局锁**。反向验证过（旧模型下测试真的变红）。证据：`tests/test_database_thread_safety.py`（20 项，含 **20 线程共享同一实例**）。剩余约束：内存库仍单连接（shared-cache 实测不成立）—— 那是**测试环境**约束 |
| 3-4 | **Retry / 网络未实现** | 🟡 | 租约到期只让占用失效，**没有东西会自动重投递**；Phase 0 验证的是**多进程、同一文件系统** |
| 5 | **`result_ref` 没有 resolver** | 🟡 | 只保证「不是路径、不是 URL」，**不检查成果是否真的存在** |
| 6-7 | 压力测试规模小 · 写事务全串行 | 🟢 | 4 进程 × 15 轮（证明正确性，不证明性能）· `BEGIN IMMEDIATE` 的固有代价 |
| 8 | **ADR 索引有 16 篇断链** | 🟡 | `adr/README.md` 链接了 25 篇，**实际只有 9 篇文件**。缺 0001–0011 · 0013 · 0015 · 0016 · 0017 —— 链接点开是 404。设计确实存在（见 Constitution），但 ADR 文件从未落盘。**有守卫阻止新增断链**，豁免名单见 `test_task_origin_lineage_guards.py` |

### Phase 0 的遗留决策

> **租约时长必须大于最慢的 handler。** 默认 300 秒。有长任务时必须先决定：
> 调长租约？实现续租？还是换一种占用模型？
>
> ⚠️ 这是 **Message Claim Lease**（`CURRENT`）。
> **Workspace Write Lock 明确不用时间租约** —— 不要混淆。

### Workspace Readiness（架构决策状态）

| 项 | 状态 |
|---|---|
| **Write Lock v1** | ✅ `SETTLED`：显式持有 + 锁主释放 + **Human Force Unlock** + 审计；状态 `ACTIVE`/`RELEASED`/`REVOKED`（无 `EXPIRED`）（ADR-0021） |
| **Lifecycle** | ✅ `SETTLED`：Lazy Provisioning + `UNPROVISIONED→…→ARCHIVED`；结束后 `FROZEN`（不删除），**允许 Reopen**（ADR-0022） |
| **Human 协作** | ✅ `SETTLED`：Authorized Human Modification 是一等方式；out-of-band **不阻止不监控**（ADR-0022） |
| **7 个角色独立** | ✅ `SETTLED`（ADR-0023）·`Maximum Depth = 3`（**TEMPORARY**） |
| **Human Demand ≠ Task** | ✅ `SETTLED`（ADR-0024）—— Human 提供 Demand，**Creator 始终是 Agent** |
| Human 的四角色 | ✅ `SETTLED`：Demand Provider / Feedback Provider / Optional Collaborator / Governance Authority |
| Guardian · 自动恢复 · Reopen 6 细节 · Demand 实体 · Child Task · Agent 自建 Root Task | `FUTURE` / `OPEN`（见各文档） |
| Heartbeat / Lease Renewal · Human 作 Task Manager | ❌ **Not implemented / 禁止** |
| **Workspace Core v1** | ✅ **已冻结**：资源树 · Controller · Explicit/Effective Lock · Revision ·
Audit · Event · Gateway（9 操作）· 资源级并发 · 组合根接线 · 20 Agent 并行 ·
`workspace-demo` · `agent_exists` fail-closed · Database 每线程 session（ADR-0029/0030/**0031**） |
| **Change Pipeline Phase 1** | ✅ **WorktreeManager 已实现**：真实 Git worktree · intent log · 幂等 · reconciliation · 崩溃一致性 · resource 级并发（ADR-0032…**0037**）· ❌ Patch/Test/Acceptance/Merge 仍 `DESIGNED` |
| Task Workspace 其余部分 | `FUTURE`：Patch · Acceptance · Conversation Manager · Recovery Automation · AI Governance |

> **v1 不需要等自动恢复机制就能进入 Workspace 实现。**

---

## 12. 写代码时最容易犯的错

按本项目**真实发生过**的频率排序：

1. **把未来设计写成现在已有** —— 文档里把 `DESIGNED` 说成 `CURRENT`
2. **守卫假绿** —— 查原文而不是纯代码；查方法名而不是真实调用点
3. **子串匹配误报** —— `agent_directory` 含 `agent_d`、`seen_tracker` 含 `ack`
4. **把业务逻辑写进门面** —— `TaskService` 必须保持薄（有行数守卫）
5. **单进程测试当多进程证据** —— `BEGIN` 的缺陷单进程测不出来
6. **忘了 `store.get()` 返回的是新对象** —— 读-改-写必须对**同一个**对象操作

---

## 12.5 关于 Agent Economy 的长期理念（**不是当前功能**）

> **经济表现不是 Intelligence 本身**，但长期收入/成本/成功率/返工率/谈判表现/
> 生存能力是观察 Agent 实际行为能力的**重要外部信号**。
> **只是理念**，不要因此实现：❌ Reputation · ❌ Ranking · ❌ 自动最优选择 · ❌ 策略学习。

---

## 13. 一句话总结

> **项目的核心不是「功能越来越多」，而是「边界越来越清楚」。**
> 宁愿少做功能，也不要破坏边界。
