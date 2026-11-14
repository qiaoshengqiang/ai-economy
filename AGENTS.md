# AGENTS.md — AI Economy OS Developer Context

> **这是一个 AI Coding Agent 的入口文件。**
> 花几分钟读完它，你就知道这个项目是什么、当前做到哪、当前范围与边界。
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
| **阶段** | **`v0.2.0`** · Workspace Core V1 已冻结 · Agent Core V1 · Tool Runtime V1 · Vision V1 · Demand Entry V1 · Pi Work（workflow / autonomous）· Output Commit V1 · Project Workdir V1 · 观察实验 |
| **测试基线** | 以 `./dev.sh test` 当前输出为准（最近：**5720 passed, 2 skipped, 30 deselected**） |
| **协议版本** | `protocol_version = "1"` |
| **命令数** | 25（`./dev.sh` 查看全部） |
| **数据库表** | **30** |
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
Change Pipeline  Phase 1 WorktreeManager · hello-demo（真实 LLM 端到端）
Agent Core    Actions（14 执行器）· Cognition · Experience · Reflection · Self Model · Stage · Budget · Delegation
Tool Runtime  V1 · 公共只读工具库（python/pytest/node/playwright）· 独立 run 目录 · 受控执行（ADR-0040）· 认知预算自主（ADR-0045）
Pi Work      workflow / autonomous · Front autonomous / relay · web search · 恢复 / Reopen
实验线        Pi 自主工作观察 · Human Demand → Agent 自主分工（`experiments/`）  ← 当前工作重心
```

### 下一步

> **下一步**：① 当前重心 —— Pi autonomous / Front relay 观察与收敛（[现状](docs/PROJECT_STATUS.md)）；② 冻结路线图 —— Change Pipeline Patch（[设计](docs/architecture/implementation/CHANGE_PIPELINE_V1_DESIGN.md)）。

---

## 2.5 ⚠️ Current V1 vs Future Human-facing Architecture

> **V1 先把 Agent Economy 核心闭环跑通，不是先解决 Human Experience。**

| | **`CURRENT` / V1** | **`FUTURE`** |
|---|---|---|
| 入口 | **Professional User**（能明确提任务） | 普通用户 + **Front Agent** |
| 链路 | `User → Agent A → Root Task → 自执行 / 协作 → Result → 验收` | `Human → Front Agent → Agent Economy` |
| Human 角色 | Requester · 约束 · 预算 · 验收 · 反馈 | 模糊意图 · 方案选择 · 经济承诺 |

**V1 的四条边界**（避免把临时部署当成永久架构）：

① `Human → Agent A` 是 V1 的部署方式；未来入口是 `Human → Front Agent → Agent Economy`。
② Human 提供 Demand；Task Creator 始终是 Agent（ADR-0024）。
③ Front Agent 也是 Agent；系统使用同一套 Task / Agent 模型，不引入 `HumanTask`。
④ Front Agent 承担 `Human → OS` 入口；Agent 之间仍然可以直接协作。

> **兼容性 ≠ 提前实现**：`V1 简单 + 边界清楚 + 接口稳定 = 可扩展`。Life/Work/Front（部分 `CURRENT`）见 [`LIFE_WORK_ARCHITECTURE.md`](docs/architecture/LIFE_WORK_ARCHITECTURE.md)（ADR-0055/0056）；人类交互界面见 [`AIEOS_UI.md`](docs/architecture/AIEOS_UI.md)；设置页设计见 [`UI_SETTINGS_DESIGN.md`](docs/architecture/UI_SETTINGS_DESIGN.md)。

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
tool_runtime/   →  core/ + 标准库（subprocess）；不依赖 services/
```

**四条有守卫的规则**（✅ 全部成立）：`core/` 与 `communication/`
**不**依赖 `services/`；`services/` **不直接**依赖 `storage/`（只经
`core/task_store`）；只有 `main.py` 调用 `register_type_handler`。

**三个容易混淆的事实**：`services` 经 `core/task_store` 访问存储，不直接依赖 `storage/`；
`core/` **确实**依赖 `storage/` 的叶子模块；`storage/ → core/` 的叶子依赖是允许的。

**两个已知例外**（历史遗留，改动前先读相关测试与 ADR）：① `core/economy.py` →
`agents/base_agent.py`（可达环）；② `storage/database.py` **函数内** import `core.config`。

> 重新扫描用 **AST**，见 [Constitution §4](docs/architecture/ARCHITECTURE_CONSTITUTION.md)。

---

## 4. Memory 四层

> ⚠️ **Agent Private Workspace 不在这四层里**（独立的资源/文件空间概念，见 §4.1）。

| Layer | 属于谁 | 状态 | 是什么 / 当前实现 |
|---|---|---|---|
| **1** Agent Private Memory | 单个 Agent | `DESIGNED` | 私人经验 / 判断 / 策略。**默认无权读**。有目录与极简文件读写（`Workspace.write_memory`），**无**结构化记忆、检索、经验积累 |
| **2** Task Shared Context | **Task** | **`CURRENT`** | 共同的任务世界模型。**已实现**：持久化 + 修订历史 + Alignment + 乐观锁。见 [`TASK_SHARED_CONTEXT.md`](docs/architecture/TASK_SHARED_CONTEXT.md) |
| **3** Task Workspace | Task | **`FUTURE`**（完整模型） | 实际共享的文件与代码。Workspace Core v1 已实现资源树 / Controller / Lock / Revision / Audit / Gateway；Patch / Test / Acceptance 等完整生命周期仍为 `FUTURE`。见 [`TASK_WORKSPACE.md`](docs/architecture/TASK_WORKSPACE.md) |
| **4** External / World State | 系统外 | `FUTURE` | 外部 API / 网络 / Git remote。**无** |

> 注：**Workspace Core v1** 是 `CURRENT`；Memory 四层里的 **Task Workspace** 是完整模型（`FUTURE`）。
> **Public / Private Working Memory（`CURRENT`）不在这四层里** —— 见 [`PUBLIC_WORKING_MEMORY.md`](docs/architecture/PUBLIC_WORKING_MEMORY.md) · [`PRIVATE_WORKING_MEMORY.md`](docs/architecture/PRIVATE_WORKING_MEMORY.md) · [`PRIVATE_SHORT_TERM_MEMORY.md`](docs/architecture/PRIVATE_SHORT_TERM_MEMORY.md) · [`PRIVATE_LONG_TERM_MEMORY.md`](docs/architecture/PRIVATE_LONG_TERM_MEMORY.md)。

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

**三条边界**：Private Workspace 不属于 Memory 四层；
它与 Task Workspace 相互独立（`Private Workspace ≠ Task Workspace`，后者是任务共享的）；
目录存在只代表资源边界，不代表 Workspace Permission 已实现。

> **`Workspace ≠ Permission`。** `core/workspace.py` 提供一个 Agent
> **对自己**目录的访问；「谁能看什么」的授权体系仍是 `FUTURE`。
> 私有 workspace 目录与 Task Workspace 是两件事。

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

**三条推论**：① Domain State 的权威高于 Memory；② Communication History 是历史，不代表仍然有效；③ Inference 必须显式标记为推测。

**「说过」≠「确认过」**：

```
A 说「我觉得用方案 B」   →   Communication History 里有这句话
                        →   Shared Context 里还没有 Decision Record
                        →   当前状态仍然只是「A 提出过一个建议」
```

只有走完 `Proposal → Alignment → DecisionRecord → Revision`，
「团队已决定方案 B」才成为共同事实。

---

## 6. 不可破坏的边界

**这些是项目的架构骨架。完整定义与后果见 Constitution §3、§2.3、§11.7。**
编码时优先核对下面这些不等号：

- **身份 / 通信**：`Identity ≠ Address` · `Message ≠ Transport` · `Communication ≠ Business Logic`
- **能力 / 权限 / 经济**：`Capability ≠ Permission` · `Capability ≠ Economy` ·
  `Wallet Ownership ≠ Machine Permission` · `Workspace ≠ Permission` ·
  `Permission ≠ Force Unlock` · `Workspace Protocol ≠ Host OS Permission`
- **Task / 共享知识**：`Task Core ≠ Task Manager` ·
  `Shared Context ≠ Message History / Private Memory / Task Workspace / 第二套 Task Core` ·
  `Memory ≠ Alignment` · `Agreement ≠ Payment` · `Budget ≠ Price`
- **Agent / 策略 / 判断**：`DecisionPolicy ≠ Decision Execution` ·
  `Agent Strategy ≠ Negotiation Mechanism` · `Private Memory ≠ Private Workspace` ·
  `Observation ≠ Conclusion` · `Guardian 判断 ≠ Domain Fact`
- **空间 / 来源**：`Logical Workspace ≠ Physical Workspace` · `Origin ≠ Creator` ·
  `Parent ≠ Root` · `Funding ≠ Responsible ≠ Creator` · `Human Demand ≠ Task` ·
  `Human ≠ Task Creator` · `Demand Source ≠ Creator`

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

**`DecisionPolicy` 的职责**：读取 Agent 自己的参数，返回一个 `Decision`。
Task / Wallet / Ledger / Workspace 的落地由 `TaskNegotiationService` 等系统机制完成。

Agent 的价格阈值 / 风险偏好 / 让价策略属于 **Agent Strategy**，放在 Agent 自己的
config / DecisionPolicy 中；`TaskNegotiationService` 只执行协议与硬约束，
不把价格策略硬编码进机制层。

> **实测**：`TaskNegotiationService` 里没有任何价格阈值
> （`accept_price` / `opening_ask` / `walk_away` 都不出现）；
> 那些参数全在 `RuleBasedDecisionPolicy` 上，来自**每个 Agent 自己的 config**。
> 有守卫在盯这条线。

---

## 6.2 Task Core 与 Shared Context 的分工

> **Task Core 是 Task 客观生命周期和领域状态的唯一权威来源。**

Shared Context 可以**引用、解释、总结、补充**共同知识；
两者通过「引用 + 解释」协作，而不是各自维护一套可修改的状态。

| 谁 | 拥有什么 |
|---|---|
| **Task Core**（`CURRENT`） | `task_id` · `state` · `budget` · `agreed_price` · `initiator` · `contractor` · lifecycle · `TaskAgreement` 的正式结果 |
| **Shared Context**（**`CURRENT`**） | objective · constraints · acceptance 的**解释** · decisions · assumptions · open questions · assignments · terminology · facts · verification feedback · **对 Domain State 的引用** |

```text
禁止形态（第二条可独立修改的状态）：
    Task.status                 = SUBMITTED
    SharedContext.current_status = IN_PROGRESS

正确形态：
✅  Task Core:       status = SUBMITTED
✅  Shared Context:  「当前处于等待发起方验收阶段。」   ← 语义解释
```

> **Shared Context 解释 Domain State，Domain State 保持唯一权威。**
> 有守卫检查 Shared Context 不复制 `status` / `agreed_price` / `contractor`
> 作为第二个可独立修改的事实源。

---

## 7. V1 范围与硬边界

> **当前阶段只实现到这里。** 下表中的「下一阶段」需要明确任务书才解锁
> （等同旧称 `FORBIDDEN NOW`）；在此之前保持 `DESIGNED` / `FUTURE` 标注。

### 7.1 V1 已实现 / 下一阶段

**V1 已实现**：Agent Core · Task Core · Shared Context · Workspace Core v1 ·
Worktree Manager · **Tool Runtime V1** · **Vision Perception V1**（ADR-0041）· **Demand Entry V1**（ADR-0042）· **Workspace Read/Edit V1**（ADR-0043）· **Workspace Search V1**（ADR-0044）·
**Output Commit V1**（快照 + 输出仓库 + manual / limited / auto，ADR-0057/0058）· **Project Workdir V1**（直接改已有项目 + shadow snapshot + 项目锁，ADR-0060）· Economy（Agent / Wallet / Ledger / LLM 成本）· 本地通信 / Message Journal / Claim Lease。

**下一阶段（需要任务书）**：Workspace Permission / ACL / IAM · OS 级执行沙箱 / 网络出口策略 / 工具安装与校验 ·
Task Payment / Escrow / Settlement · Retry / Queue / Broker · P2P / 多机 ·
Heartbeat / Scheduler · Guardian · TaskManager / GC · Task Lineage / Demand 实体 ·
Human Task Manager / Coordinator。

> 完整状态表与逐项说明见 **Constitution §15.1**。下一阶段能力保持
> `DESIGNED` / `FUTURE`；不要写成 `CURRENT`。

### 7.2 硬边界（安全 · 权限 · 数据完整性 · 兼容性）

1. **代码执行边界**：代码/测试只经 **Tool Runtime** 的公共工具库执行；`shell=False`、独立 run 目录、allowlist 工具、超时与输出上限。Task / Workspace 层不执行任意宿主 shell，Agent 不能下载/安装/替换工具（WorktreeManager 的受控 `git` 调用与 ToolRunner 的受控工具调用不在此列）。
2. **权限边界**：Workspace 是逻辑资源树；宿主机文件系统写权限与 ACL/IAM/Role 不作为 Workspace 权限的替代。
3. **验收边界**：生产者不验收自己的成果；验收责任必须由非生产者承担。
4. **数据完整性**：Task Core 与 Ledger 只经领域/服务写入；没有旁路写状态或余额。
5. **密钥边界**：配置文件只保存环境变量名；secret 只从 `.env` / 环境变量读取。
6. **兼容性**：`protocol_version = "1"`；破坏线协议兼容性的改动必须显式升级协议版本并更新 [`PROTOCOL.md`](PROTOCOL.md)。

### 7.3 V1 设计取舍

V1 优先**最小但正确的边界**。以下机制在具体需求出现前不引入：
Blockchain · CRDT · Event sourcing · Vector DB · Knowledge graph ·
分布式共识 · 多 Agent 投票 · 复杂 ontology。

---

## 8. 开发流程

```
1. 读本文档 + Constitution
        ↓
2. 先调查真实代码，再形成设计
        ↓
3. 跑基线：./dev.sh test   （对照 §2 的当前基线）
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

1. **先调查，再动手。** 设计基于真实代码调查。
2. **每一步结束跑全量测试。** 不是只跑自己改的那个文件；
   基线见 §2。
3. **新增守卫必须配反向测试。** 把违规注入进去，确认守卫**真的变红**。

---

## 9. 测试要求

```bash
./dev.sh test                           # 全量（不含真实 API 测试）
./dev.sh test tests/test_xxx.py         # 单个文件
./dev.sh test tests/test_xxx.py -k 名字  # 单个测试
```

基线见 §2；守卫文件位于 `tests/*guards*.py`；真实 API 测试 30 项（单独跑，需要 Key）。

**使用 `./dev.sh test` 运行测试** —— 它负责设置项目环境；直接 `pytest` 可能缺少环境变量。

---

## 9.5 ⚠️ Context Loading Rule —— 按任务加载相关文档

**为什么有这一节**：为了解决「上下文不足」，很容易反过来把所有架构文档
一次性塞进 Coding Agent —— 那浪费上下文，而且淹没真正相关的那几页。
**不要一次加载全部文档**；按任务只读相关规范与 ADR。

```text
AGENTS.md → 判断任务领域 → 只读相关 Specification → 必要时读对应 ADR
```

| 我要做… | 重点读 | 不必读 |
|---|---|---|
| **Shared Context / Alignment** | [`TASK_SHARED_CONTEXT.md`](docs/architecture/TASK_SHARED_CONTEXT.md) + §4 · §6.2 · ADR-0019 | Storage / Reliability ADR |
| **SQLite / 并发 / 事务** | ADR-0012 · ADR-0014 · §3 · §11 | TASK_SHARED_CONTEXT.md |
| **Negotiation / DecisionPolicy** | §6.1 · `core/decision_policy.py` · `task_negotiation_service.py` | Workspace 相关 |
| **Execution（START/SUBMIT/…）** | `task_execution_service.py` · `core/task_execution.py` · `core/task.py` | Alignment 设计 |
| **Communication / 协议 / UI 消息 / Inbox** | [`PROTOCOL.md`](PROTOCOL.md) · `communication/` · [`UI_COMMUNICATION_INTEGRATION.md`](docs/architecture/UI_COMMUNICATION_INTEGRATION.md) · [`AGENT_INBOX_V1_DESIGN.md`](docs/architecture/implementation/AGENT_INBOX_V1_DESIGN.md) · ADR-0002 · ADR-0003 | Economy |
| **Workspace / Lifecycle / Human** | [`TASK_WORKSPACE.md`](docs/architecture/TASK_WORKSPACE.md) + ADR-0020/0021/0022（§4.1 · §7 · §14.5 · §15） | TASK_SHARED_CONTEXT.md |
| **Task Origin / Task Lineage** | [`TASK_ORIGIN_LINEAGE.md`](docs/architecture/TASK_ORIGIN_LINEAGE.md) + **ADR-0023** —— 概念已定（`SETTLED`），**实现为 `FUTURE`** | — |
| **代码执行 / 测试工具** | [`TOOL_RUNTIME_V1_DESIGN.md`](docs/architecture/TOOL_RUNTIME_V1_DESIGN.md) + **ADR-0040** · `tool_runtime/` · `resources/public/tools/` | 与 Task Execution 无关 |
| **Guard / 测试 / Prompt** | ADR-0018 · `tests/*guards*.py` · [`PROMPT_WRITING_GUIDELINES.md`](docs/architecture/PROMPT_WRITING_GUIDELINES.md) | — |
| **Git Worktree / Patch / Test / Acceptance / Merge** | [`CHANGE_PIPELINE_V1_DESIGN.md`](docs/architecture/implementation/CHANGE_PIPELINE_V1_DESIGN.md) + **ADR-0032…0037** —— 设计已定；**Worktree Manager 已实现（ADR-0037）**，其余仍 `DESIGNED` | Workspace Core 内部细节 |

**Constitution 只在三种情况下通读**：第一次接触项目 · 要**新增**一条边界 ·
要**推翻**某条边界。日常改一个模块读对应章节 + 相关 ADR 就够。

---

## 10. 必读文件（按优先级）

### 推荐阅读顺序

```
1. AGENTS.md（本文档）              ← 入口
2. ARCHITECTURE_CONSTITUTION.md    ← 长期 Source of Truth
3. Task-specific Specification     ← 按任务领域选（见下表）
4. Necessary ADR                   ← 只读相关的那几篇
```

> **从 AGENTS.md 开始**：它的价值之一是告诉你**哪些不用读**。
> **每个事实保留一个权威来源**：Constitution 负责长期架构，AGENTS.md 负责导航，
> 任务规范负责领域细节。

### Task-specific Specification（按任务领域选）

| 我要做… | 规范 | 状态 |
|---|---|---|
| Task Shared Context / Alignment | [`TASK_SHARED_CONTEXT.md`](docs/architecture/TASK_SHARED_CONTEXT.md) | **`CURRENT`**（已实现） |
| **Task Workspace / Read / Lock** | [`TASK_WORKSPACE.md`](docs/architecture/TASK_WORKSPACE.md) + [`LOCK_MODEL.md`](docs/architecture/LOCK_MODEL.md) | **Core v1 `CURRENT`** · 完整 Task Workspace `FUTURE` |
| **代码执行 / 测试工具** | [`TOOL_RUNTIME_V1_DESIGN.md`](docs/architecture/TOOL_RUNTIME_V1_DESIGN.md) + **ADR-0040** + `tool_runtime/` | **`CURRENT`（V1，受控执行）** |
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
> | Lifecycle：**Lazy Provisioning** + `UNPROVISIONED→…→ARCHIVED`；结束后 `FROZEN`（不删除），**允许 Reopen**（ADR-0022） | 完整 Task Workspace · Permission · Conversation · Recovery |
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
9. [`main.py`](main.py) —— **组合根**（唯一把各层接起来的地方） · 配置见 [`core/config_resolver.py`](core/config_resolver.py)（Canonical：Agent→Model→Provider→Adapter；`./dev.sh config` 看解析结果）

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
> **Workspace Write Lock 不使用时间租约**；两者是不同的可靠性模型。

### Workspace Readiness（架构决策状态）

| 项 | 状态 |
|---|---|
| **Write Lock v1** | ✅ `SETTLED`：显式持有 + 锁主释放 + **Human Force Unlock** + 审计；状态 `ACTIVE`/`RELEASED`/`REVOKED`（无 `EXPIRED`）（ADR-0021） |
| **Lifecycle** | ✅ `SETTLED`：Lazy Provisioning + `UNPROVISIONED→…→ARCHIVED`；结束后 `FROZEN`（不删除），**允许 Reopen**（ADR-0022） |
| **Human 协作** | ✅ `SETTLED`：Authorized Human Modification 是一等方式；out-of-band **不阻止不监控**（ADR-0022） |
| **7 个角色独立** | ✅ `SETTLED`（ADR-0023）·`Maximum Depth = 3`（**TEMPORARY**） |
| **Human Demand ≠ Task** | ✅ `SETTLED`（ADR-0024）—— Human 提供 Demand，**Creator 始终是 Agent** |
| Human 的四角色 | ✅ `SETTLED`：Demand Provider / Feedback Provider / Optional Collaborator / Governance Authority；Task 管理由 Agent 负责 |
| Guardian · 自动恢复 · Reopen 6 细节 · Demand 实体 · Child Task · Agent 自建 Root Task | `FUTURE` / `OPEN`（见各文档） |
| Heartbeat / Lease Renewal | 未实现；需要任务书并先解决长任务租约决策 |
| **Workspace Core v1** | ✅ **已冻结**：资源树 · Controller · Explicit/Effective Lock · Revision ·
Audit · Event · Gateway（9 操作）· 资源级并发 · 组合根接线 · 20 Agent 并行 ·
`workspace-demo` · `agent_exists` fail-closed · Database 每线程 session（ADR-0029/0030/**0031**） |
| **Change Pipeline Phase 1** | ✅ **WorktreeManager 已实现**：真实 Git worktree · intent log · 幂等 · reconciliation · 崩溃一致性 · resource 级并发（ADR-0032…**0037**）· Patch/Test/Acceptance/Merge 仍 `DESIGNED` |
| Task Workspace 其余部分 | `FUTURE`：Patch · Acceptance · Conversation Manager · Recovery Automation · AI Governance |

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
> 这些是长期观察信号；V1 不实现 Reputation · Ranking · 自动最优选择 · 策略学习。

---

## 13. 一句话总结

> **项目的核心不是「功能越来越多」，而是「边界越来越清楚」。**
> 优先保持边界清晰，再增加功能。
