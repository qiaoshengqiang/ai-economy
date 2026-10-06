# §0.5 架构影响审计
## Architecture Impact Audit

> **审计对象**：现有 AI Economy OS 架构与实现，是否与
> [`AGENT_CORE_MODEL_V1.md`](../AGENT_CORE_MODEL_V1.md) §0.5
> 「规则限制结构，策略决定行为，激励塑造行为，反馈推动演化」一致。
>
> **审计基线**：`948592e`（§0.5 落盘）· 前置 `3750ba9`
> **本轮不改代码。** 只审计。
>
> **审计日期**：本次会话

---

## A. Executive Summary

# **部分符合 —— 结构层健康，但有一个重大结构性缺口**

```
ALIGNED   6 项
TENSION   3 项
CONFLICT  2 项
UNKNOWN   1 项
```

### 一句话结论

> **AI Economy OS 已经是一个「规则限制结构」的系统，
> 但它**还不是**一个「Agent 可以在其中自主行动」的环境 ——
> 因为 Agent 目前**没有任何通往 Action Space 的路径**。**

§0.5 三条总纲的现状：

| 总纲 | 现状 |
|---|---|
| **规则限制结构** | ✅ **做得好**。无隐藏 Coordinator · 无 auto-assign · Lock/Permission/Gateway 都是结构约束 |
| **策略决定行为** | ⚠️ **有策略，但没有行动出口**。`DecisionPolicy` 确实是 Agent 自己的（有守卫），但 Agent 无法执行任何 action |
| **激励塑造行为** | ❌ **结构性不成立**。AI Coin 与 LLM 成本**没有挂钩** → Agent 感受不到认知成本 |

### 最重要的那个问题

> **当前是在构建「控制 Agent 的流程系统」，还是「允许 Agent 自主行动的数字环境」？**

**答案：两者都不是。** 它是一个**边界正确、但行动通道尚未接通**的系统。

- 它**不是**「控制 Agent 的流程系统」—— 没有隐藏调度器，没有硬编码策略（ALIGNED 部分很扎实）
- 它**也还不是**「自主行动的数字环境」—— **Agent 没有行动手段**

> 🔑 **这不是因为有太多规则，而是因为缺一条通道。**
> 对 §0.5 而言，这是一个**好消息**：不需要「删掉控制逻辑」，
> 需要的是**「把已有的能力接到 Agent 手上」**。

---

## B. Conflict Table

| ID | 模块 | 当前机制 | §0.5 要求 | 状态 | 建议 |
|---|---|---|---|---|---|
| **A-01** | LLM Layer / Agent | **LLM 没有 tool 暴露** —— Agent **无法主动执行任何 action**；只有 Demo/CLI 代码能调系统 API | 「OS 应该设计行动空间」—— 行动空间要能被 Agent 使用 | **CONFLICT** | **必须修改**（见 H-1） |
| **A-02** | Ledger / AIC | `LLMCallRecorder.cost_ai` **恒为 0**（「还没算」）—— 认知成本**不产生 AIC 后果** | §0.5.3 激励必须能被 Agent **感受到**；§0.5.6 AI Coin = 未来行动能力的有限资源表示 | **CONFLICT** | **必须修改**（见 H-2） |
| **A-03** | Agent Runtime | Runtime 只做「唤醒 → drain → 交给 `handle_message`」—— **不含任何策略** | 策略决定行为 | **ALIGNED** | 保持现状 |
| **A-04** | Task Negotiation | `RuleBasedDecisionPolicy` 来自**每个 Agent 自己的 config**；有守卫禁止价格阈值出现在 Service 里 | Agent Strategy ≠ Negotiation Mechanism | **ALIGNED** | 保持现状 |
| **A-05** | Task System | **无** auto-assign / auto-dispatch / best_agent 逻辑（已搜证） | 不设隐性 Coordinator | **ALIGNED** | 保持现状 |
| **A-06** | Workspace Core | Gateway 检查 Participant / Lock / Revision —— **拒绝了就是做不到**，不做行为指导 | 「无权限 → 做不到」 | **ALIGNED** | 保持现状 |
| **A-07** | Communication | A→B→C 由 Agent 主动 `send_message`；Event Wakeup 只唤醒，不决定做什么 | 合作是 Agent 的主动行为 | **ALIGNED** | 保持现状 |
| **A-08** | Action Space | 现有 Action **全部由 Demo/CLI 编排代码发起**（hello/snake/three-agent-demo） | 这些应是 **Agent 的 Action**，不是编排代码的行为 | **TENSION** | 应该修改（见 H-3） |
| **A-09** | Child Task / Delegation | **完全不存在**（与 `FORBIDDEN NOW` 一致） | §0.5.2 列了「是否创建 Child Task」为 Agent 应自主决定的事 | **TENSION** | 暂不修改（有意为 FUTURE） |
| **A-10** | Bootstrap | `DEFAULT_AGENTS` **硬编码在 main.py** | 「规则限制结构」—— 但这是**启动配置**，不是策略 | **TENSION** | 应该修改（见 H-4） |
| **A-11** | Reputation / History | `Reputation` 存在但**没有真实反馈回路**（无结算、无成功率累积） | §0.5.7 专业化应由**长期反馈**涌现 | **UNKNOWN** | 暂不修改 |

---

## C. Harness ↔ OS Responsibility Boundary

> ⚠️ **现状说明**：DSH 集成**尚未发生**（§22.36 标的是 `CONFIRMED` **方向**，
> 不是实现）。所以下表是**边界定义**，不是现状描述。

### Harness（ExecutionContext）应负责

```
Observe → Think → Plan → Evaluate → Choose Action
Execute → Observe Result → Continue / Finish
```

**即：产生与执行行动。**

### AI Economy OS 应负责

```
Identity · Permission · Workspace · Task State · Communication
Ledger · Assets · Reputation · Resource Constraints · Locks
Validation · Environment State
```

**即：让行动发生在一个有身份、资源、产权、规则和经济后果的世界中。**

### 两个方向的越界检查

| 越界方向 | 当前是否存在 | 判定 |
|---|---|---|
| **OS 替 Harness 做 Strategy Selection** | ❌ **未发现** | ✅ 没有 `auto_assign` / `select_agent` / 硬编码外包规则 |
| **Harness 绕过 OS 做结构约束** | ⚠️ **尚不适用**（DSH 未集成） | 🔶 但这是**未来集成时必须守住的边界** —— 见 §22.42 `Sandbox ≠ Gateway` |

> 🔒 **未来集成 DSH 时最容易犯的错**：让 DSH 的 filesystem/shell tool
> **直接**落到 Host，绕过 Workspace Gateway。
> §22.38 已把这条写成 `Tool ≠ Capability`，但**目前没有守卫**。

---

## D. Agent Action Space

### Existing（Agent 现在**真的能**发起的 Action）

```
（空）
```

🔴 **这是本次审计最重要的发现。**

**核实方式**：搜遍 `agents/` `llm/` `services/` 的 `tools=` /
`tool_call` / `function_call` / `def *_tool` —— **零匹配**。
`Brain.generate()` 只返回文本。

> 系统里有 `Task.offer()` / `agent.send()` / `gateway.create()` 这些**能力**，
> 但 **Agent 没有任何途径表达「我要调用它们」**。
> 只有 Demo / CLI 编排代码在调。

### Missing（架构已经需要、但 Agent 还拿不到的 Action）

对照 §0.5.2 列出的 12 个「应由 Agent 自主决定」的问题：

| §0.5.2 的问题 | 需要什么 Action | 现状 |
|---|---|---|
| 是否外包 / 外包给谁 / 给多少钱 | `find_agent` · `offer_task` | ❌ 无 |
| 是否接受别人的任务 / 是否讨价还价 | `accept_task` · `reject_task` · `counter_offer` | ❌ 无 |
| 自己完成还是找别的 Agent | （取决于上面几条） | ❌ 无 |
| 是否创建 Child Task | `create_child_task` | ❌ 无（且 FORBIDDEN NOW） |
| 是否值得继续投入 Token/时间/AIC | `wait` · `resume` · `finish` | ❌ 无 |
| 是否应该建立长期合作关系 | `communicate` | ⚠️ 能力有，通道无 |

### Implicit（当前被 OS / Demo **隐式执行**、没有暴露给 Agent 的 Action）

| Action | 谁在替 Agent 做 |
|---|---|
| `offer_task` | `./dev.sh demo`（CLI 编排） |
| `accept_task` / `counter_offer` | `cmd_demo` 里的显式调用序列 |
| `write`（Private Workspace） | `three-agent-demo` 的 handler |
| `write`（Task Workspace） | 同上 |
| `communicate` | Demo handler 里的 `await agent.send(...)` |

> 🔴 **注意**：这些**不是**「OS 在替 Agent 决策」——
> 它们是 **Demo 编排代码**在替 Agent 决策。
> 问题一样（Agent 没有自主性），但**归因不同**：
> **不是架构错了，是通道没接。**

### Forbidden（**不应**进入 Agent Action Space 的）

按 §0.5.5「硬边界，软行为」，以下**必须留在结构层**，不能变成 Agent 可选的 Action：

```
❌ grant_permission        （授权是治理，不是 Agent 的选择）
❌ force_unlock            （Human Force Unlock 属于治理）
❌ bypass_lock             （Lock 是结构约束）
❌ edit_ledger             （账本不能被 Agent 直接改）
❌ set_reputation          （信誉应由行为累积，不能自设）
❌ host_restart            （§22.40：V1 不暴露）
❌ 任何「修改自己或他人的授权/余额/信誉」的 Action
```

> 判据（§0.5.10 Q1）：这些是 **Rule / Structure**，不是 **Strategy / Behavior**。

---

## E. Cooperation Architecture

### 问题：合作现在是 Agent 的 Action，还是系统的流程？

**回答：都不是。**

| | 现状 |
|---|---|
| 是 Agent 的 Action 吗？ | ❌ **不是** —— Agent 没有可以发起 Offer 的通道 |
| 是系统的流程吗？ | ❌ **也不是** —— **没有任何自动派单逻辑**（已搜证） |
| 那是谁在做？ | ⚠️ **Demo / CLI 编排代码** |

### 已经有的是（**正常架构能力**）

```
✅ Task 状态机（CREATED → OFFERED → …）    core/task.py
✅ TASK_OFFER / COUNTER_OFFER / ACCEPT / REJECT 协议   PROTOCOL.md
✅ DecisionPolicy 纯函数（Agent 自己的策略）           core/decision_policy.py
✅ TaskNegotiationService（机制，不含价格阈值）        services/
✅ Communication + Event Wakeup                       communication/
```

> **这一层是健康的。** 机制齐全、策略归 Agent、没有硬编码。

### 最小结构缺口

> **只差一件事：让 Agent 能发起这些 Action。**

具体说，**[D. Existing] 是空的**。
合作能力**已经存在于系统 API 层**，但 Agent 够不到。

### 🔴 明确建议：**不要**为此新增合作机制

按 §0.5「最小结构变化，获得最大的自主决策空间」：

> **已有的合作机制一个都不用改。**
> 需要的是给 Agent **一条表达 Action 的通道** ——
> 而不是一套「合作流程」。

⚠️ **同时明确反对**：
```
❌ 不要实现「任务复杂 → 自动找 B」
❌ 不要实现「超过 X AIC → 必须外包」
❌ 不要实现「系统发现任务适合 B → 自动派给 B」
```
§0.5.8 已把这类逻辑定性为「**最多只能是实验性 baseline**」。

---

## F. Security / Autonomy Audit

### ✅ 正确的结构约束（**保持现状**）

| 机制 | 为什么是结构约束 |
|---|---|
| Workspace `_resolve()` | 拒绝绝对路径 / `..` / symlink 逃逸 —— **做不到**，不是「不该做」 |
| Workspace Gateway 的 Lock 检查 | `NOT_EFFECTIVE_LOCK_HOLDER` —— 并发写者**成为不了** |
| Participant 检查 | 非参与方**读不到** |
| Ledger 余额 | 余额不足 → 支付**完不成** |
| Private Workspace 隔离 | Agent B **读不到** A 的文件（三种逃逸路径实测全拒） |
| Agent 间 Credential 隔离 | 不同 Credential → 不同 provider 实例（实测无串线） |

> **这些完全符合 §0.5.5「硬边界」。没有一处是「Prompt 告诉 Agent 不要这样做」。**

### ⚠️ 可能「过度行为控制」的地方

| 位置 | 判断 |
|---|---|
| **`snake-demo` / `three-agent-demo` 的编排代码** | ⚠️ **是编排代码在决定流程** —— 但它是**明确标注的 Demo**，不是架构。**建议保持**（改动它没有架构收益） |
| **`DEFAULT_AGENTS` 硬编码** | ⚠️ 属于**启动配置**，不是行为控制。但写在 `main.py` 里与「可变参数进配置」原则不一致 |
| **Guard 体系（`tests/*guards*.py`）** | ✅ **不是行为控制** —— 它约束的是**开发者**，不是 Agent |

### 🔴 未发现「有权限 → OS 再要求 Agent 按预设流程操作」

这是审计**特意找**的一类问题，**没有找到**。
Workspace Gateway 在允许之后**不再干预** —— 符合 §0.5.5 的理想状态。

---

## G. Incentive Audit

### ✅ 已经形成的真实激励

| 机制 | 是否真实 |
|---|---|
| Ledger 余额 | ✅ **真实** —— 余额不足支付直接失败 |
| 转账 | ✅ 真实（`Economy.transfer` 是 OS 级校验） |
| Workspace Lock 竞争 | ✅ 真实 —— 有人在写，你就是写不进去 |
| Task 失败 | ⚠️ 部分 —— 有状态机，但**没有结算** |
| LLM token 消耗 | ❌ **记录在案，但不产生 AIC 后果** |

### ❌ 仍然只是 Prompt 层面的

| §0.5 期望 Agent 感受到的 | 现状 |
|---|---|
| **认知成本**（Token → AIC） | ❌ `cost_ai = 0`（「还没算」） |
| **机会成本** | ❌ 无法计算 |
| **返工成本** | ❌ 无 |
| **失败成本** | ❌ 无结算 |
| **时间成本** | ❌ 无 |
| **Reputation 变化** | ❌ 有字段，无反馈回路 |
| **未来机会** | ❌ 无 |

### 🔴 这是本次审计的第二大发现

> **§0.5.3「激励塑造行为」目前是空的。**
>
> Agent 完全可以**无限调用 LLM 而没有任何经济后果** ——
> 这不是「激励不足」，是**根本没有激励通道**。
>
> 结果：如果今天给 Agent 接通 Action Space，
> 它会**没有任何理由**节约 Token、比较价格、或选择合作 ——
> 因为这些选择的**代价和收益都不存在**。

> ⚠️ **两条发现是耦合的**：
> 只做 A-01（接通 Action Space）而不做 A-02（接通成本），
> 会得到一个「能乱花别人钱」的 Agent。
> **建议 A-02 先于或同步于 A-01。**

---

## H. Recommended Next Changes

### 1. 必须修改

| # | 内容 | 原因 |
|---|---|---|
| **H-1** | **给 Agent 一条表达 Action 的通道**（最小形态：结构化输出 + 有限解析，或 tool calling） | 没有它，§0.5 的「策略决定行为」**无从生效** —— Agent 有策略但没有手 |
| **H-2** | **把 LLM token 消耗接到 AIC**（`cost_ai` 不再是 0） | 没有它，§0.5.3「激励塑造行为」是空的；且 H-1 单独做会放大风险 |

> ⚠️ **H-1 与 H-2 应当在同一阶段做**，理由见 §G 末尾。

### 2. 应该修改

| # | 内容 | 原因 |
|---|---|---|
| **H-3** | 把 Demo 编排代码里的「Action 序列」**逐步移交给 Agent** | 让 `three-agent-demo` 从「Demo 决定顺序」变成「Agent 自己决定」—— 这才真正验证 §0.5.8 的核心实验目标 |
| **H-4** | `DEFAULT_AGENTS` 移入 `config.yaml` | 与「可变参数进配置」一致；成本极低 |

### 3. 暂不修改

| # | 内容 | 原因 |
|---|---|---|
| **H-5** | Child Task / Delegation | `FORBIDDEN NOW`，属 `FUTURE`；§0.5.2 提到它但不要求现在实现 |
| **H-6** | Reputation 反馈回路 | 依赖 H-2（结算）先成立 |
| **H-7** | DSH 集成 | §22.36 只是**方向**；且集成时必须先解决 §C 末尾的越界问题 |
| **H-8** | Gateway 的 Capability 化 | §22.38 已定义边界，但组合方式仍是 `PROVISIONAL` |

### 4. 保持现状

```
✅ Workspace Core（结构约束做得对）
✅ Lock 模型（不是行为控制）
✅ Communication / Event Wakeup（Agent 主动、Runtime 无策略）
✅ TaskNegotiationService（无价格阈值，有守卫）
✅ DecisionPolicy（Agent 自己的，纯函数）
✅ Credential 隔离
✅ Demo 编排代码（明确标注为 Smoke Demo，改动无架构收益）
```

---

## 最终回答

> **当前 AI Economy OS 是在构建「控制 Agent 的流程系统」，
> 还是「允许 Agent 在规则、资源、激励和反馈中自主行动的数字环境」？**

# **都不是。**

**它已经是一个「规则限制结构」的系统** —— 这一半做得很扎实：
无隐藏 Coordinator · 无硬编码策略 · Lock/Permission/Gateway 都是真结构约束。

**但它还不是那个「自主行动的数字环境」**，缺的是**两件互相关联的结构**：

```
① Agent → Action 的通道              （现在完全断开）
② 行动 → 经济后果 的通道             （LLM 成本没有接到 AIC）
```

> 🔑 **§0.5 的价值恰好在这里**：它没有要求我们「删掉控制逻辑」
> （本来就没什么控制逻辑），而是让我们**看清了缺的是哪两条通道**。
>
> 而且 §0.5 给出了明确的**优先级**：
> 先接通道（结构），而不是先写更多 Prompt（行为）。

---

## 审计方法说明

- 全部结论来自**读代码与搜索实证**，不是文档推测
- 明确标注了 **ALIGNED / TENSION / CONFLICT / UNKNOWN** 四种状态
- 未把 `PROVISIONAL` / `OPEN` 的内容当成事实
- **未修改任何代码或测试**
