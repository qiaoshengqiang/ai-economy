# Production Stage V1 架构对齐审查
## Architecture Alignment Review

> **基线**：`19ace9f` · 3804 passed / 0 failed
> **本轮只审查，不改生产代码。**

---

## 0. 一句话结论

> **Production Stage V1 几乎不需要新架构 —— 它需要的是
> 「在已有 Task / Workspace / Wakeup / Experience 之上加一层薄的阶段语义」。
> 但有一个**必须现在就守住的边界**：Stage 不能变成 Workflow Script。**

### 实证：当前代码里有什么

| 概念 | 代码命中 |
|---|---|
| `Stage` / `stage` | **0 个文件** |
| `Checkpoint` / `checkpoint` | **0 个文件** |

**也就是说：Production Stage 目前完全是设计，零实现。**
（这与之前的 Self Model 阶段不同 —— 那里至少已有 Memory 目录形态。）

---

## A. Current Alignment —— 现有架构哪些可以直接复用

| 需求 | 已有能力 | 复用得如何 |
|---|---|---|
| 长期任务本体 | `core/task.py` 的 Task + 12 态状态机 | ✅ **直接复用**，Stage 挂在 Task 下 |
| 中断后恢复 | `communication/agent_runtime.py` 的 Event Wakeup | ✅ **直接复用** —— 唤醒与进程无关 |
| 产出物版本 | Workspace revision（`change/` 注入的 revision/lock/responsibility） | ✅ **复用，不要重造**（§二十二 明确要求） |
| 反馈 → 认知 | Experience → Cognitive Context → Reflection → Self Model | ✅ **已跑通**（真实 API 实验证明） |
| 受控写入 | `SelfModelWriteService` | ✅ 复用 |
| 经济后果 | Ledger + `charge_llm_cost` | ✅ 复用（本轮不实现 Stage 预算） |
| 外部反馈入口 | Communication + Wakeup | ✅ 复用（§十二） |
| Artifact | **Private / Task Workspace 已是数字资源层** | ✅ **不要重实现文件系统**（§十一） |

🔴 **一句话：Production Stage 需要的是「编排语义」，不是「新基础设施」。**

---

## B. Stage Model —— V1 最小语义

必须区分四个**不同层**的东西（§一）：

```
Task          完成一部长篇小说                 ← 长期目标
  └ Stage     完成第 1～10 章                  ← 阶段目标 + 交付结构
Runtime Session  今天运行 70 分钟               ← 一次连续执行
Checkpoint    当前已完成到第 8 章               ← 可恢复的状态快照
Artifact      chapter_01.md …                 ← 数字产出物（属于 Workspace）
Feedback      「节奏偏慢」                      ← 外部输入
```

> **Stage 完成 ≠ Task 完成**（§二）。
> Stage 完成后：Task 可继续 · Agent 可暂停 · Runtime 可退出 ·
> 外部反馈可进入 · 后续 Stage 可**以后**再启动。

### V1 最小语义（建议）

```
Stage = {
    stage_id
    task_id
    goal              # 「完成第 1～10 章」—— 目标，**不是步骤**
    expected_deliverables   # 交付结构（哪些 artifact 算完成）
    acceptance        # 谁 / 按什么判定
    status
    checkpoint_ref    # 指向最近一次 Checkpoint
    created_at / updated_at
}
```

🔴 **`goal` + `expected_deliverables` 是「目标 + 交付结构」，
不是「Step 1 / Step 2 / Step 3」**（§十七）。

---

## C. Hard Limits —— 属于系统硬边界

```
max_continuous_runtime = 90 min      ← 示例，**必须可配置**
```

判定：**Rule / Structure**（§二十六 要求明确）。

- 由 User / System 设定，**Agent 不能通过策略绕过**
- 达到后：Runtime **强制停止生产** → 保存状态 → 生成 Checkpoint
- 🔴 **Hard Limit reached ≠ Stage Failed ≠ Stage Completed**（§十八 / §二十一）

> ⚠️ 现有代码**没有**这类硬上限机制。`system.reliability` 里有的是
> lease / busy timeout，语义不同。**这是真实缺口之一。**

---

## D. Agent Stop Judgment —— 如何保持 Decision Owner = Agent

🔴 **本审查最重要的一条：不要把它叫「软约束」。**（§五）

```
Hard Limit           = Rule / Structure    （系统）
Agent Stop Judgment  = Strategy / Decision （Agent）
```

Agent 可以因为以下原因**主动**停止：
```
阶段目标已达成 · 当前质量已足够 · 继续修改收益很低
当前信息不足 · 等待反馈更合理 · 资源成本过高 · 需要其他 Agent 帮助
```

**Runtime 只能执行 Hard Limit。** 以下全部由 Agent Decision 决定：

```
continue · stop · pause · request_feedback · submit_stage
```

❌ **禁止**：
```python
if progress > 80%: stop
if 60 min passed: stage_complete
```

> ✅ 现有架构**天然支持**这一点：`actions/` 已经能让 Agent 表达 Decision，
> 只要**不要把 stop/pause 做成 Runtime 判断**即可。
> §二十 已明确：**不要实现 Meta-Cognition Engine / 中央决策器。**

---

## E. Lifecycle —— Stage 状态机建议

### 先看已有 Task 状态机（**必须结合，不要另起一套**）

```
CREATED → OFFERED → NEGOTIATING → ACCEPTED → IN_PROGRESS
        → SUBMITTED → VERIFIED → COMPLETED
        （REJECTED / CANCELLED / EXPIRED / FAILED）
```

### Stage V1 建议状态

```
PENDING          尚未开始
ACTIVE           正在生产
INTERRUPTED      Hard Limit / Crash 打断，**可恢复**（≠ FAILED）
WAITING          Agent 主动等待外部反馈（可含「等 Human」）
COMPLETED        Stage Goal + Deliverables 都满足
FAILED           任务本身无法继续
CANCELLED        被取消
```

### 🔴 `PAUSED` vs `WAITING_FOR_FEEDBACK` 是否要分开？（§七 要求给出判断）

**建议：V1 不分开，合并为 `WAITING`，但必须记录「在等什么」。**

理由：
1. 从**可恢复性**看两者完全一样（都只需一个 Stimulus 就能唤醒），
   分开会增加状态转换的组合数而**不增加能力**。
2. 区分「为什么在等」是**信息**，不是**状态** —— 放进
   `waiting_for` 字段（`feedback` / `human` / `other_agent` / `resource`）。
3. ⚠️ 但如果未来「等待反馈」会触发**不同的经济或调度行为**
   （例如等待期间免收 AIC），那时再拆不迟 —— 现在拆是过早抽象。

> 📌 这是一个**判断**，不是事实。如果认为两类等待未来一定分叉，可以现在拆。

---

## F. Checkpoint —— 保存哪些状态

### 必须区分（§九）

```
Persistent State            ← Checkpoint 保存这个
Runtime Ephemeral State     ← **不要**保存（§九 明确）
```

**建议保存**：

| 项 | 为什么 |
|---|---|
| Task State / Stage State | 恢复后要知道「我在哪」 |
| Current Goal | `goal` + `expected_deliverables` |
| Produced Artifacts（**引用**，不是内容） | 指向 Workspace revision，**不复制文件** |
| Progress | 例如「已完成到第 8 章」 |
| **Workspace revision** | 🔴 **复用已有机制，不要重造**（§二十二） |
| Pending Feedback | 已到但未处理的反馈 |
| Action history（近期） | 恢复认知上下文用 |
| Self Model 变更 | 已由 `SelfModelWriteService` 持久化，Checkpoint **只引用** |
| Relevant Experience | 已有持久层，**只引用** |

**不建议保存**：LLM session / Python object / 未落盘的中间推理。

🔴 **Checkpoint ≠ 完成结果**（§十）：
```
计划 10 章 → 实际到第 7 章 → Hard Limit 停止
→ 这是 Checkpoint，Stage 仍 ACTIVE/INTERRUPTED，**不是** COMPLETED
```

---

## G. Pause / Resume —— Runtime 停止后如何恢复

```
Stage = WAITING
Runtime = **stopped**          ← 不需要持续运行（§十四）
Agent Identity = persistent
        ↓  新的 Stimulus / Feedback 到达
Wakeup → Resume Cognitive Run
```

必须支持间隔：**几分钟 / 几小时 / 几天 / 几周**（§十五）。

🔴 **恢复不能依赖 Python process / memory object / LLM session。**

> ✅ 现有 Event Wakeup **正是为此设计的** —— 它只依赖持久状态 + 传入事件。
> 这是本架构里复用度最高的一块。

---

## H. External Feedback —— 反馈如何重新进入 Task

```
Artifact 对外发布 → 外部收集反馈 → 反馈进入 AIECO
   → Task 收到新 Stimulus → Wakeup → Agent 看到反馈
```

来源预留：`Human` / `Reader` / `Market` / `Evaluation` / `External system`
（V1 **不必全部实现**，但接口要留 —— §十二）

🔴 **Feedback ≠ Experience**（§十三）：

```
Feedback → Agent Observation / Experience → Reflection → （可能的）Self Model
```

❌ **不能**把 Human Feedback 直接写成 Self Model。

> ✅ 现有链路**完全支持**：`ExperienceStore.record(source=feedback)`
> 已经是这么做的（真实 API 实验里用过）。

---

## I. Task / Stage / Runtime Session 边界

| | 是什么 | 生命周期 | 谁拥有 |
|---|---|---|---|
| **Task** | 长期目标 + 经济契约 | 可跨天/周/月 | Task System（已有） |
| **Stage** | Task 下的**阶段性生产单元**（目标 + 交付结构） | 短于 Task，可中断恢复 | **新建**，挂在 Task 下 |
| **Runtime Session** | **一次连续执行** | 分钟~小时 | Runtime（已有） |

🔴 **三者不可混为一谈。**
`Runtime Session` 结束**不改变** Task / Stage 的语义（除非 Hard Limit 写了 Checkpoint）。

---

## J. Required Implementation Gaps —— 只列真正必须补的

| # | 缺口 | 现有基础 | 说明 |
|---|---|---|---|
| **G-1** | **Stage 实体 + 状态机 + 持久化** | 无 | 必须新建（最小） |
| **G-2** | **Hard Limit（连续运行上限）** | 无 | `system` 配置 + Runtime 侧强制停止 |
| **G-3** | **Checkpoint 结构 + 保存/恢复** | Workspace revision 已有 | **引用** revision，不重造 |
| **G-4** | **Agent 的 stop / pause / submit_stage Decision** | `actions/` 已能表达 Decision | 只需**新增 action_type**，不要加 Runtime 规则 |
| **G-5** | **Stage 等待 → Wakeup 恢复路径** | Event Wakeup 已有 | 只需让 Stage 状态进入 Context |
| **G-6** | **外部 Feedback 入口** | Communication + Experience 已有 | 留接口即可，V1 可只支持 Human |

🔴 **明确不在本清单里**：Stage Planning AI · Workflow Engine ·
Gantt · Reader Platform · Web Publishing · Multi-Agent Stage ·
Economy Stage Auction（§二十三）

---

## K. Risks —— 重点检查（§二十五）

| # | 风险 | 严重度 | 判据 |
|---|---|---|---|
| **R-1** | **Stage 变成 Workflow Script** | 🔴🔴 **最高** | 一旦 `Stage` 里出现 `step_1/step_2/…`，Agent 就从「决定怎么做」退化成「执行脚本」，**直接违背 §0.5.2** |
| **R-2** | **Hard Limit 被误当成 Stage Completion** | 🔴 高 | §十八 明确禁止。Hard Limit 只产生 **Checkpoint**，不产生 COMPLETED |
| **R-3** | **Runtime 替 Agent 决定 Stop** | 🔴 高 | 任何 `if progress > X: stop` 都是越界（§六） |
| **R-4** | **Checkpoint 与 Workspace / Revision 重复** | ⚠️ 中 | Checkpoint **必须引用** revision，不能自带一套文件版本 |
| **R-5** | **Human 重新变成 Task Creator** | ⚠️ 中 | §十六：Human 给 Demand / Constraint / Approval / Feedback，**不逐步安排 Stage** |
| **R-6** | **把 Agent Stop Judgment 叫「软约束」** | ⚠️ 中 | §五 明确要求用 Stop Judgment —— 措辞会诱导实现者把它做成 Runtime 判断 |
| **R-7** | **Stage 状态与 Task 状态双轨冲突** | ⚠️ 中 | 必须明确 Stage 状态**派生**自 Task 状态机，不是并行第二套 |

---

## L. Recommended Implementation Order

```
Step 0  Stage 实体 + 状态机 + 持久化（G-1）
        —— 先只有 PENDING / ACTIVE / WAITING / COMPLETED
Step 1  Stage 进入 Cognitive Context
        —— 否则 Agent 看不到「我在哪个阶段」
Step 2  Agent 的 stop / pause / submit_stage（G-4）
        —— **必须是 Action，不是 Runtime 规则**
Step 3  Hard Limit（G-2）
        —— 系统结构边界，与 Step 2 的 Decision 严格分开
Step 4  Checkpoint（G-3）
        —— 引用 Workspace revision，不重造
Step 5  恢复路径：Stage WAITING → Wakeup（G-5）
Step 6  外部 Feedback 入口（G-6）
```

🔑 **Step 1 不可省**：Stage 若不进 Context，Agent 就不知道自己在一个
长期任务的哪个阶段 —— 那样 Step 2 的「Agent 自己决定停不停」无从谈起。

---

## §二十六 最高设计原则检查

> **Production Stage V1 是否真正符合
> 「规则限制结构，策略决定行为，激励塑造行为，反馈推动演化」？**

| 原则 | 本设计中的落点 | 判定 |
|---|---|---|
| **规则限制结构** | `Hard Limit`（连续运行上限）· Task/Stage 状态机 · Deliverables 结构 | ✅ 是结构边界，**不是**行为规定 |
| **策略决定行为** | `Agent Stop Judgment` · `continue/stop/pause/submit_stage` | ✅ **Owner 是 Agent**（只要不加 `if progress > X`） |
| **激励塑造行为** | 未来：`Stage + AIC Budget + Expected Reward + Time Cost` | ⚠️ **接口已留，本轮不实现**（§二十）—— 这是**故意的** |
| **反馈推动演化** | `Feedback → Experience → Reflection → Self Model` | ✅ **已有真实 API 证据**（阶段 2.1 N=5） |

### 🔴 唯一会硬编码 Agent 生产行为的地方

> **`Stage` 里出现步骤序列（step_1 / step_2 / …）。**

只要 Stage 保持为「**目标 + 交付结构**」，本设计就符合最高原则；
一旦它变成步骤清单，**Stage 就变成了 Workflow Script**，
Agent 的生产行为被系统硬编码 —— 这正是 §十七 警告的场景，也是 **R-1**。

**建议**：在实现 Step 0 时，就给 `Stage` 加一条**守卫测试**：
`Stage` 不得包含有序步骤字段（`steps` / `sequence` / `workflow`）。

---

## §二十五 报告完整性自查

| 要求 | 状态 |
|---|---|
| A. Current Alignment | ✅ |
| B. Stage Model | ✅ |
| C. Hard Limits | ✅ |
| D. Agent Stop Judgment | ✅ |
| E. Lifecycle（含 PAUSED vs WAITING 判断） | ✅ |
| F. Checkpoint | ✅ |
| G. Pause / Resume | ✅ |
| H. External Feedback | ✅ |
| I. Task / Stage / Runtime Session 边界 | ✅ |
| J. Required Implementation Gaps | ✅ |
| K. Risks | ✅ |
| L. Recommended Implementation Order | ✅ |
| §二十六 最高设计原则检查 | ✅ |
