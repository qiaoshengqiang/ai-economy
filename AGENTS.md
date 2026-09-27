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

**它最关键的一点**：

> **Agent 是独立的经济主体，不是被调用的函数。**
>
> 系统负责：协议 · 状态 · 业务硬约束 · 持久化 · 消息路由
> 系统**不**负责：替 Agent 决定「你应该怎么谈价」

---

## 2. 当前阶段

| | |
|---|---|
| **阶段** | `Post-Workspace-Phase-0 / Pre-Shared-Context` |
| **测试基线** | **2333 passed, 30 deselected** |
| **协议版本** | `protocol_version = "1"` |
| **命令数** | 15（`./dev.sh` 查看全部） |
| **数据库表** | 10 |
| **工作区状态** | 应当干净 |

### 已完成

```
Step 1        Agent + Economy 基础骨架
Step 2 / 2.5  Brain / LLM 抽象 + 真实 DeepSeek Provider
Step 3 / 3.6  Communication + Identity / Address / Transport / Protocol 冻结
Step 4A       Task Core + Lifecycle + TaskWorkspaceRef + Money
Step 4B       Capability + AgentDirectory + 自主发现协作者
Step 4C       DecisionPolicy + 多轮谈判 + ACCEPT / REJECT + TaskAgreement
Refactor      Task 应用层拆分（门面 / Handler / Mapper / Negotiation）
Step 4D       START / SUBMIT / VERIFY / COMPLETE + TaskExecutionService
Step 4D.1     持久化消息幂等 + Task 乐观锁 + 事务原子性
Phase 0       真实多进程验证 + Message Claim Lease
Step 4E       Architecture Constitution + Shared Context 设计  ← 当前
```

### 下一步

> **Task Shared Context / Alignment 的最小可运行原型设计。**
>
> **不是** Workspace 实现。

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

### 依赖方向（单向，无环）

```
core/           →  不依赖任何人
communication/  →  不依赖 services/、不依赖 core/task
services/       →  core/ + communication/ + storage/
storage/        →  只被 core/task_store 与 communication/recorder 使用
```

**有守卫自动检查这些方向。**

---

## 4. Memory 四层（`DESIGNED` —— 只有设计，没有实现）

| Layer | 属于谁 | 状态 | 是什么 |
|---|---|---|---|
| **1** Agent Private Memory | 单个 Agent | `DESIGNED` | 私人经验 / 判断 / 策略。**其他 Agent 默认无权读** |
| **2** Task Shared Context | **Task** | `DESIGNED` | 共同的任务世界模型（决策 / 假设 / 问题 / 分工） |
| **3** Task Workspace | Task | `FUTURE` | 实际共享的文件与代码 |
| **4** External / World State | 系统外 | `FUTURE` | 外部 API / 网络 / Git remote |

> **当前代码里这四层都没有实现。**
> 唯一相关的是 `TaskWorkspaceRef` —— 它只是**一个 `workspace_id` 字符串**。

**`Memory ≠ Alignment`** —— Shared Context 是**结果**，
Alignment 是**产生和更新它的机制**。两者必须分开。

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

**三条推论**：

1. Domain State 不能被 Agent Memory 推翻
2. 「有人说过」≠「现在仍然有效」
3. 推测不能冒充事实

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
| `Task Shared Context ≠ Message History` | 把「说过」当成「确认过」 |
| `Task Shared Context ≠ Private Memory` | 私人偏见变成共识 |
| `Task Shared Context ≠ Task Workspace` | 把「认为是什么」和「实际有什么」混起来 |
| `Workspace ≠ Permission` | 有 `workspace_id` 就以为能读文件 |
| `Memory ≠ Alignment` | 只有共同存储，没有形成机制 |
| `Agreement ≠ Payment` | 成交凭证被当成支付记录 |
| `Budget ≠ Price` | 预算被当成成交价 |
| `DecisionPolicy ≠ Decision Execution` | 策略开始改状态 |
| `Domain ≠ Infrastructure` | `core/` 依赖 `services/` |

---

## 7. 当前禁止项（`FORBIDDEN NOW`）

**除非有明确的任务书，否则不要实现下面任何一项。**

| 领域 | 禁止内容 |
|---|---|
| **Workspace** | Task Workspace 实现 · Manager · Gateway · Permission · ACL · 文件系统访问 · artifact storage |
| **Git** | worktree · branch · commit 自动化 · merge |
| **代码执行** | `subprocess` · shell · Docker · sandbox · 测试运行器 |
| **经济结算** | Payment · Escrow · Settlement · Wallet transfer |
| **通信可靠性** | Retry Queue · Retry Scheduler · ACK · Redis · Kafka · Celery |
| **网络** | P2P · 多机 · 分布式 |
| **生命周期** | Heartbeat · Scheduler · Life Loop |
| **智能演化** | Adaptive Negotiation · RL · 策略自学习 |
| **协调** | TaskManager · Coordinator |

### 也不要过度设计

看到 "Shared Context" 就上 ❌ Blockchain · CRDT · Event sourcing ·
Vector DB · Knowledge graph · 分布式共识 · Multi-agent 投票 · 复杂 ontology。

**当前目标：建立最小但正确的概念边界。**

---

## 8. 开发流程

```
1. 读本文档 + Constitution
        ↓
2. 先调查真实代码（不要凭空设计）
        ↓
3. 跑基线：./dev.sh test   （必须是 2333 passed）
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
3. **新增守卫必须配反向测试。** 把违规注入进去，确认守卫**真的变红**。

---

## 9. 测试要求

```bash
./dev.sh test                          # 全量（不含真实 API 测试）
./dev.sh test tests/test_xxx.py        # 单个文件
./dev.sh test tests/test_xxx.py -k 名字 # 单个测试
```

| | |
|---|---|
| **基线** | **2333 passed, 30 deselected** |
| **测试文件** | 48 个 |
| **守卫文件** | 8 个，265 项 |
| **真实 API 测试** | 30 项，单独跑（需要 Key） |

**必须用 `./dev.sh test`，不要直接 `pytest`** ——
`dev.sh` 会正确设置虚拟环境和环境变量。

---

## 10. 必读文件（按优先级）

### 第一优先（理解架构）

1. [`docs/architecture/ARCHITECTURE_CONSTITUTION.md`](docs/architecture/ARCHITECTURE_CONSTITUTION.md) ← **长期 Source of Truth**
2. 本文档
3. [`docs/architecture/TASK_SHARED_CONTEXT.md`](docs/architecture/TASK_SHARED_CONTEXT.md)

### 第二优先（理解当前实现）

4. [`core/task.py`](core/task.py) —— 状态机（**唯一的**生命周期定义）
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
| 1 | **租约 vs 长任务** | 🔴 | 没有 lease renewal。handler 处理时间**超过租约** → 别的 worker 接手 → **重复处理**。Workspace 阶段的重复处理 = **重复的文件副作用** |
| 2 | **多线程共享 `Database` 不安全** | 🔴 | `_tx_depth` 是实例状态。跨线程共享会被交错修改 → 事务边界错乱。**当前唯一安全用法是每线程一个 `Database` 实例** |
| 3 | **Retry 未实现** | 🟡 | 租约到期只让**占用**失效。**没有任何东西会自动重新投递** |
| 4 | **网络 / 多机未做** | 🟡 | Phase 0 验证的是**多进程、同一文件系统** |
| 5 | **`result_ref` 没有 resolver** | 🟡 | 只保证「不是路径、不是 URL」。**不检查它指向的成果是否真的存在** |
| 6 | 压力测试规模小 | 🟢 | 4 进程 × 15 轮。证明正确性，不证明性能 |
| 7 | 写事务全串行 | 🟢 | `BEGIN IMMEDIATE` 的固有代价（SQLite 单写者） |

### Phase 0 的遗留决策

> **租约时长必须大于最慢的 handler。**
>
> 默认 300 秒。一旦有长任务，必须**先做决定**：
> 调长租约？实现续租？还是给长任务换一种占用模型？
>
> **这是 Workspace 阶段的第一个设计决策**，不是遗留 bug。

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

## 13. 一句话总结

> **项目的核心不是「功能越来越多」，
> 而是「边界越来越清楚」。**
>
> 宁愿少做功能，也不要破坏边界。
