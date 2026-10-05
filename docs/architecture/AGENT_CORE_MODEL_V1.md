# AGENT CORE MODEL V1
## Agent · World · Gateway · Capability · Cognitive Substrate

> **本文档回答一个上位问题：Agent 到底是什么？**
>
> 它**不是**功能清单，也**不是**实现计划。
> 它是后续所有讨论（Autonomous Agent · Heartbeat · Tool · Capability ·
> Browser · Game · Multi-World · Agent Economy · Agent-owned production
> assets）应该回到的那张**架构地图**。
>
> **本文档不含任何代码承诺。** 写在这里的设计，不代表它已经被实现，
> 也不代表现在就该实现它。

---

## §0 三个状态标识（**本文档最重要的约定**）

本文档中每个核心概念都标了三种状态之一。**不要把它们混着读。**

| 状态 | 含义 | 你要是违反了会怎样 |
|---|---|---|
| **`CONFIRMED`** | 已经确认。**未来实现必须尊重它。** | 会破坏架构骨架，属于「搞混了概念」 |
| **`PROVISIONAL`** | 方向已经明确，但**还允许讨论和调整** | 可以改，但要**先讨论**，不能默默改 |
| **`OPEN`** | **尚未决定。不应该提前实现。** | 现在实现它 = 把未定的东西定死 |

> ⚠️ **`OPEN` 不等于「以后要做」。** 它只表示「现在还不知道」。
> 本文档里 `OPEN` 的那几节，**禁止**因为「文档里提到了」就去实现。

### 与项目既有四态的关系

项目其他地方用的是 `CURRENT` / `SETTLED` / `DESIGNED` / `FUTURE` / `OPEN`
（见 [`AGENTS.md`](../../AGENTS.md) §2 与 Constitution）。两者的映射：

```
本文档 CONFIRMED   ≈ 项目 SETTLED（不可推翻的边界）
本文档 PROVISIONAL ≈ 项目 DESIGNED（设计已定，实现为 FUTURE）
本文档 OPEN        ≈ 项目 OPEN（还没有决定）
```

---

## §1 Agent 的最高层定义【`CONFIRMED`】

> **Agent 是一个持续存在的数字主体（persistent digital subject）。**

它**不是**：

```
Agent
  ≠  LLM call                一次模型调用
  ≠  Runtime process         一个运行时进程
  ≠  Provider account        一个模型账号
  ≠  API credential          一把 API Key
  ≠  Workspace               一个文件空间
  ≠  Machine process         一台机器上的某个进程
```

**Agent 是持续存在的主体；上面这些都是它的载体、工具或环境，会变，而它不变。**

这一条是本文档一切推论的地基。

---

## §2 Agent Identity ≠ 认知实现【`CONFIRMED`，细节 `OPEN`】

不要简化成「Agent 与模型完全无关」。更准确的说法是：

> **Agent 的主体身份独立于它的认知实现与运行载体。**
> LLM、Provider、上下文、Runtime 可以构成 Agent 的**认知基础设施**
> 或**外部状态**，但不因此自动等同于 Agent 本身。

### §2.1 Provider-side state 是一个真实存在的问题【`CONFIRMED` 现象】

现代模型提供商本身可能持有：

- conversation context
- provider-side state
- cache
- personalization
- long context
- 其它外部认知状态

所以 Agent 的构成更接近：

```
Agent
 ├── Identity                  ← 主体
 ├── Local Private Assets      ← 主体
 ├── Local Memory              ← 主体
 ├── Runtime                   ← 载体
 └── External Cognitive State  ← 外部
         └── Provider-side context
```

> **Provider-side context 可以参与 Agent 当前的认知连续性，
> 但不能因此等同于 Agent 本身。**

### §2.2 这一节里**没有决定**的东西【`OPEN`】

下面这些问题**都还没有答案**，不要实现：

- Provider-side context 具体如何保存
- 如何迁移
- 如何与本地 Memory 合并
- Provider 不可用时怎么办
- 如何跨模型恢复认知连续性

---

## §3 Cognitive Substrate（认知基底）【`PROVISIONAL`】

目前形成的方向：Agent 的认知**不等于**某一个 LLM，而可能存在一个更广义的
**Cognitive Substrate**：

```
Cognitive Substrate
├── LLM
├── Provider-side context
├── Local model
├── cached state
├── reasoning context
└── other cognitive resources
```

它属于 Agent 的**认知基础设施**，**不是** Agent Identity 本身。

> 🔒 **这是当前架构方向，不代表已经存在正式的代码抽象。**
> **禁止**因为本文档增加了这一节就去实现一个 `CognitiveSubstrate` 类。

---

## §4 World 的定义【架构地基】

### §4.1 什么是 World【`CONFIRMED`】

> **World 是 Agent 可以进入、观察、与之交互并受到其规则约束的外部环境。**

World **不是**「游戏世界」或「虚拟世界」的同义词 —— 那只是它的一种。

一个 World 至少具有若干要素（不要求所有 World 模型完全相同）：

```
World
├── State                 当前是什么状态
├── Rules                 什么行为在这里有效 / 无效
├── Resources             里面有什么可用的东西
├── Actors / Entities     里面还有谁 / 什么
├── Events                发生了什么
└── Interaction Surface   可以从哪里伸手进去
```

### §4.2 什么可以成为 World【`CONFIRMED` 方向】

```
File / Workspace     Game              Browser
Enterprise System    Simulation        Robot Environment
```

四类示例：

| World | State | Rules | Resources | Interactions |
|---|---|---|---|---|
| **Workspace** | files / directories / revisions | Workspace responsibility / locks / permissions | shared files / artifacts | read · write · create · edit · move |
| **Game** | game state | game mechanics | items / currency / territory | move · attack · trade · talk |
| **Browser** | current page / DOM / session | browser / website behavior | pages / forms / information | click · type · navigate · observe |
| **Robot** | sensor + environment state | physical constraints / control limits | actuators / sensors | move · grasp · observe · speak |

> **World 是一个抽象，不是一个具体产品。**

---

## §5 Agent 可以跨 World 存在【`CONFIRMED`】

```
Agent
  ↓
World A
  ↓
离开
  ↓
World B
  ↓
继续存在
```

Agent 的这些内容**原则上属于 Agent 自身，而不是某个 World**：

```
Identity · Private Assets · Memory · Experience
Strategies · Relationships · Economy
```

> **World 是 Agent 所处的环境，不是 Agent 本身。**

---

## §6 Private Workspace 与 World 的关系【`PROVISIONAL`】

当前 `Task Workspace` **本身就可以被理解为一种 World** ——
它不只是「文件夹」，而是一个具有 State / Rules / Resources / Interaction
interface 的**受控工作环境**：

```
Agent
  ↓
Workspace World
  ↓
Gateway
```

这是未来 World / Gateway 统一抽象的重要基础。

> ⚠️ **当前代码里的 Workspace Core 不需要因此重构成通用 World Framework。**
> 那仍然是未来设计。见 §12。

---

## §7 Gateway 的定义【`CONFIRMED` 方向】

以前 Gateway 主要被理解为「访问边界」。现在的理解提升为：

> **Gateway 是 Agent 与 World 之间的接口层。**

```
Agent
   ↕
Capability
   ↕
Gateway
   ↕
World
```

Gateway 的职责：把某个 World 的**状态、规则和交互能力**，
以**受控方式**暴露给 Agent。

---

## §8 Capability 的定义【`CONFIRMED` —— 这是最容易搞错的一条】

> 🔴 **不要把 Capability 简化成 Permission。**

更准确的定义：

> **Capability 是 Agent 与 World 之间进行感知与行动的受控接口。**

Capability 至少分成两类：

### §8.1 Perception Capability（`World → Agent`）

```
read file · observe game state · browser view
receive message · sensor input · database query
```

### §8.2 Action Capability（`Agent → World`）

```
write file · send message · browser click · browser type
game move · database write · robot movement
```

---

## §9 Capability 的来源**不是** Agent Identity【`CONFIRMED`】

> **Agent 拥有 Identity，不意味着 Agent 自动拥有所有 Capability。**

例如 Agent A **不自动拥有**：

```
read all files · write all files · shell
browser · internet · robot control
```

而应该是：

```
Agent A
   ↓ 进入 World
   ↓ 通过 Gateway
   ↓ 获得**某些** Capability
```

因此：

```
Agent Identity  ≠  Capability
```

> 这条与项目已有的另一条基础原则**同源**：
> **Agent 可以拥有资产，但不因此自动拥有机器权限。**

---

## §10 Capability 更像「感知与行动器官」【`CONFIRMED` 类比】

人类并不是直接与世界交互：

```
World
 ↓
眼 / 耳 / 鼻 / 舌 / 身体感受      ← 感知
 ↓
认知系统
 ↓
手 / 脚 / 语言 / 行为            ← 行动
 ↓
World
```

所以 Agent 可以类比为：

```
World
   ↓
Perception Capabilities
   ↓
Agent Cognition
   ↓
Action Capabilities
   ↓
World
```

> 这说明 **Capability 不只是「权限」，它是主体和世界之间的连接器官 / 接口。**

---

## §11 Gateway 与 Capability 的组合方式【`PROVISIONAL`】

建议的抽象关系：

```
Agent
    ↓
Cognitive System
    ↓
Capability
    ↓
Gateway
    ↓
World
```

但 **Capability 与 Gateway 在代码层究竟是一对一、一对多还是怎样组合，
目前不要定死。**

这一节是 `PROVISIONAL`。未来再决定：

- Gateway 是否注册 Capability
- Capability 是否独立存在
- 一个 Gateway 是否产生多个 Capability
- Capability 是否跨 World 复用
- Capability 是否收费
- Capability 是否有 latency / risk / cost 等属性

> 🔒 **目前不要实现。**

### §11.1 Capability 的未来属性【`OPEN` / `FUTURE`】

候选（**只是候选，不要实现完整 Capability Framework**）：

```
Direction · Scope · Cost · Latency · Risk
Authorization · Persistence · Precision
```

---

## §12 Agent Core Model 当前结构【`PROVISIONAL`】

```
Agent Core Model V1
│
├── Identity
├── Core Drives              ← §13  OPEN
│                               （Self Model 的完整讨论见 §22）
├── Core Invariants          ← §14  PROVISIONAL
├── Constitution             ← §14  PROVISIONAL
├── Self Model               ← **§22**（PROVISIONAL）
├── Cognitive Substrate      ← §3   PROVISIONAL
├── Memory
├── Experience
├── Strategies
├── Goals
├── Planning / Deliberation
├── Tasks
├── Assets
├── Capabilities
│   ├── Perception
│   └── Action
├── Relationships
└── World Presence
```

> ⚠️ **这个列表目前不是最终定稿。** 它只是当前讨论形成的候选结构。
> 除已经明确标 `CONFIRMED` 的部分外，其余为 `PROVISIONAL` 或 `OPEN`。

---

## §13 Core Drives【`OPEN` —— 只记录方向，禁止实现】

讨论中提出：Agent 可能需要某种类似生物「本能」的底层驱动力，
否则无法真正理解自主性。

| 人类 | 倾向 |
|---|---|
| 饥饿 | → 进食 |
| 繁殖冲动 | → 行为倾向 |
| 危险 | → 回避 |

未来 Agent **可能**存在（**仅为候选**）：

```
Resource Preservation · Goal Pursuit · Capability Improvement
Task Commitment · Social / Agent Interaction · Risk Avoidance
```

> 🔴 **尤其不要现在就把下面这些硬编码成 Agent 的驱动：**
> 「提高能力」·「节约算力」·「赚钱」·「避免死亡」

还需要进一步讨论（**全部 `OPEN`**）：

- 哪些是核心驱动力
- 哪些是 Constitution
- 哪些是 learned preference
- 哪些是 Goal
- 哪些可以修改
- 哪些不可修改

---

## §14 Core Invariants 与 Constitution【`PROVISIONAL`】

> 🔴 **不要把 Constitution 简单理解成 System Prompt。**

当前方向：**Constitution 更接近 Agent 的长期稳定行为原则，
而不是一次模型调用的 Prompt。**

进一步可能分成三层：

| 层 | 回答的问题 |
|---|---|
| **Core Drives** | 我**为什么会倾向于**做某些事情？ |
| **Core Invariants** | 哪些边界**无论如何都不能违反**？ |
| **Constitution** | **长期来看**，我倾向于成为怎样的 Agent？ |

Constitution **可能**包含（**不是正式定稿**）：

- 不随意浪费资源
- 重复工作应该逐步工具化
- 有能力自己做的事情可以自己做
- 不值得自己做的事情可以委托
- 要记录失败经验
- 要积累长期生产能力

---

## §15 Agent 的自主性不是「会自己创建 Task」【`PROVISIONAL`】

未来目标链条：

```
Agent State
   ↓
Self Model
   ↓
Core Drives
   ↓
Goals
   ↓
Deliberation
   ↓
Task creation / task delegation
   ↓
Execution
   ↓
Evaluation
   ↓
Experience
   ↓
Strategy update
   ↓
Next Wakeup
```

> **「Agent 会创建 Task」不是自主性的定义，而只是自主性可能产生的一个结果。**

---

## §16 Private Workspace 的长期定位【`CONFIRMED` 方向】

Private Workspace V1 **已经完成**（Agent-facing 读写 + 长期记忆 + 重启后仍在 +
Agent 间严格隔离）。

> 🔴 **不要把它长期理解成「Agent 的聊天记录目录」。**

长期方向：

> **Private Workspace 是 Agent 的长期私有资产空间。**

未来可能承载：

```
memory/  experiences/  strategies/  tools/
knowledge/  projects/  state/  self/
```

也就是 Agent 的个人记忆、经验、策略、工具、知识、项目、状态和其他私有资产。

---

## §17 Agent 可以建设自己的生产资料【`PROVISIONAL`】

未来目标之一：Agent 可以在自己的 Private Workspace 中**逐步建设属于自己的
工具和其他生产资产**。

```
tools/
    csv_analyzer/
    code_checker/
    physics_tool/
```

过程：

```
发现重复工作
    ↓
判断工具化是否划算
    ↓
创建 / 改进个人工具
    ↓
测试
    ↓
保存到 Private Workspace
    ↓
未来复用
```

长期效果：

```
算力投入 → 生产资料建设 → 未来任务成本下降 → 生产效率提高
```

> 这是 AI Economy 的长期方向。
> 🔒 **当前不要实现自动 Tool Manager。**

---

## §18 Agent 的 World Presence【`PROVISIONAL`】

未来同一个 Agent 可以同时或先后存在于不同 World：

```
Agent A
 ├── Workspace World
 ├── Game World
 ├── Browser World
 └── Simulation World
```

不同 World 中 State / Rules / Resources / Capability 都不同，
但 **Agent Identity 仍然属于同一个主体**：

```
World              ≠  Agent
World State        ≠  Agent Memory
World Capability   ≠  Agent Identity
```

---

## §19 跨 World Asset【`PROVISIONAL` / `OPEN`】

长期设想：Agent 可以携带

```
Identity · Memory · Experience · Strategies
Relationships · Tools · Economic assets
```

进入不同 World。

> ⚠️ 但**同一个 Agent-owned asset 在不同 World 中如何解释、适配和执行，
> 目前尚未正式设计。**

尤其是：

```
same asset  =  different semantic interpretation in different worlds
```

这是长期研究方向。
🔒 **当前不要实现「通用跨世界资产语义系统」。**

---

## §20 当前明确的架构不变量【全部 `CONFIRMED`】

```
1.  Agent Identity  ≠  Runtime
2.  Agent Identity  ≠  LLM
3.  Agent Identity  ≠  Provider Account
4.  Agent Identity  ≠  Workspace
5.  Agent Identity  ≠  Capability
6.  Agent-owned assets  ≠  Machine permissions
7.  Task responsibility  ≠  Agent identity
8.  World  ≠  Agent
9.  World Capability  ≠  automatic Machine authority
10. Private Workspace  ≠  Task Workspace
11. Self Model      ≠  Memory
12. Self Model      ≠  World Model
13. World           ≠  World Model
14. Public Manifest  ≠  Capability  ≠  Authorization
```

特别强调两条：

> **Agent 可以拥有资产，但不能因为拥有资产就自动拥有机器权限。**
> **Agent 可以获得任务，但任务权限必须由 Task Workspace / World 明确授予。**

---

## §21 最终概念地图

> Self Model / Facts / Beliefs / Strategies / Meta-Cognition 见 **§22**。

```
Agent
  ↓  是一个持续存在的数字主体

World
  ↓  是 Agent 所处的外部环境

Gateway
  ↓  是 Agent 与 World 之间的接口层

Capability
  ↓  是 Agent 通过 Gateway 对 World 进行感知与行动的受控接口

Private Workspace
  ↓  是 Agent 的长期私有资产空间

Task Workspace
  ↓  是 Agent 参与具体协作任务的共享工作环境

Cognitive Substrate
  ↓  是 Agent 当前使用的认知基础设施
```

以及：

> **Core Drives / Constitution / Memory / Experience / Strategy / Goal /
> Task / Capability / World 是不同层次的概念，
> 不能混成一个「超级 Prompt」。**

---

## §22 Self Model / Facts / Beliefs / Strategies / Meta-Cognition【全部 `PROVISIONAL`】

> 本节把 Self Model 相关的架构讨论同步进来。
> **除已标更高状态者外，本节全部为 `PROVISIONAL`。**
>
> 🔒 **这不代表要开始实现 Self Model。** 见 §23 的禁止清单。

### §22.1 Self Model 的新理解

不再把 Self Model 理解成简单的：

```
agent_id · balance · role · tasks · tools
```

### 🔴 修正：Self Model **不**包含「世界」

> **Self Model 是 Agent 对自身的动态内部认知模型。**

**上一版写的是「对自身，以及与自身行动相关的世界的动态内部认知模型」——
那会造成 Self Model 与 World Model 的概念重叠。已修正。**

Self Model **可以包含**：

```
我是谁 · 我的当前状态 · 我的能力与限制 · 我的资源
我的长期属性 · 我的目标/承诺
我对自身认知和策略的判断
与自身行动**直接相关**的自我知识
```

而：

> **World Model = Agent 对某个 World 的动态认知。**

```
Self Model   ≠   World Model
```

World Model 属于**特定 World Context / World Presence**（见 §22.17）。
Self Model **可以引用** World Model 中与自身有关的信息，
但**不能因此把 World Model 纳入 Self Model 本体**。

关键区分：

```
Memory      长期经验 / 信息存储
Self Model  当前用于认知和决策的**工作模型**
```

> **Self Model ≠ 全部 Memory。** 前者是「现在用来想事情的那张图」，
> 后者是「存起来的资料」。

### §22.2 认知信息必须区分事实、观点和策略

```
Fact
Belief / Claim
Strategy
Meta-Cognition
```

> 🔴 **不要把这些混成普通 memory entries。**

### §22.3 Fact（事实）

暂时区分三类：

| 类型 | 来源 | 例 |
|---|---|---|
| **Authoritative / System Fact** | 系统权威来源 | Agent Identity · AIC balance · Workspace state · Task state · Capability state |
| **Observed Fact** | Agent 通过 World / Gateway / Message / Tool **观察**得到 | |
| **Derived Fact** | Agent 根据其他事实**推导**得到 | |

### §22.4 Belief / Claim（观点 / 主张）

> **Agent 在一定条件、范围和时间内，对某个命题成立程度的认知断言。**

例：「Agent B 很擅长 OCR」

**应当能够进一步限定**：

```
Scope · Condition · Time · Evidence · Confidence · Source / Provenance
```

重要原则：

> **条件和适用范围越明确，观点通常越容易变得可靠；
> 但过度缩小范围也可能降低观点的普适价值。**

> 🔴 **未来 Belief 不应只记录一个真假值。**

### §22.5 Belief 可以形成层级

```
Fact:              B 完成 OCR 18/20 次
                        ↓
Belief:            B 在类似 OCR 任务上成功率较高
                        ↓
Higher-level:      B 是值得优先考虑的 OCR 协作者
```

越高层越抽象，**越需要依赖下层证据**。

未来可以考虑记录「高层观点由哪些事实 / 观点支持」。

> 🔒 **本次不要实现知识图谱。**

### §22.6 Strategy（策略）

> **Strategy 是 Agent 针对某类问题或情境形成的、可以重复使用的
> 思考或行动模板，用于减少每次从零开始决策的成本。**

```
Situation:  重复的数据转换任务
Strategy:   先检查 Private Workspace 是否已有工具

Situation:  某类 OCR 任务
Strategy:   优先询问 Agent B
```

### §22.7 Strategy 不要求绝对正确

「三长一短选最短、三短一长选最长、长度接近选 C」是一个很好的例子 ——
它是**低质量启发式**，但在「不会做题 + 时间极少 + 必须立即行动」时
可能仍有**局部价值**。

所以 Strategy 应关注：

```
Applicability · Expected Benefit · Cost · Risk
Evidence · Failure Conditions
```

> 🔴 **而不是简单标记 `Good` / `Bad`。**

### §22.8 Strategy 应该允许层级

```
Meta-Strategy
    ↓
General / Domain Strategy
    ↓
Situation Strategy
    ↓
Concrete Action Heuristic
```

> 🔴 **不要把策略设计成一个无结构的平面文件列表。**

### §22.9 Meta-Cognition（元认知）

> **Meta-Cognition 是 Agent 对自己的认知、观点和策略进行观察、评价、
> 选择、修正和管理的能力。**

至少可以包含：

```
Strategy Selection · Strategy Validation · Strategy Revision
Strategy Abandonment · Strategy Creation
Evidence Management · Uncertainty Management
```

自问：

- 我为什么相信这个观点？
- 这个观点的证据是什么？
- 这个策略为什么有效？
- 它现在还适用吗？
- 我是否可能犯了系统性错误？

### §22.10 三层认知模型（V1 建议）

为了避免无限递归，V1 暂时**只考虑三层**：

```
L0   Facts / Observation
L1   Beliefs / Strategies
L2   Meta-Cognition —— 对 Beliefs / Strategies 进行评价和修正
```

> 🔴 **不要实现** `Meta-Meta-Strategy` / `Meta-Meta-Cognition` / …

### §22.11 Experience 与 Strategy 的关系

```
Experience
    ↓ 抽象
Belief / Strategy
    ↓
未来低成本决策
```

也就是说：

> **Strategy 可以看成 Agent 对过去昂贵认知过程的一种压缩。**

这与 AI Economy OS 的长期目标直接相关：

```
第一次：  高认知成本
以后：    复用 Strategy / Experience
最终：    通过 Strategy + Tool 降低未来 AIC / Token / Time 成本
```

### §22.12 Decision 的定位【`CONFIRMED`】

> **Decision 是 Agent 自身的认知活动，不建立独立的中央 Decision Model。**

Agent 可以使用：

```
Facts · Beliefs · Goals · Strategies · Meta-Cognition
Resource State · World State · LLM
```

进行自己的判断。

**实测依据**（Decision Lab V1 Round 2，2026）：

```
Laya multilingual     41.7%
Simple rule baseline  68.3%
```

详见 [`../../../decision-lab/results/summary.md`](../../../decision-lab/results/summary.md)。

### 🔴 修正：不要把实验结果解释成「独立 Decision Model 没有意义」

实验真正支持的是：

> **V1 不引入「独立的中央 Decision Engine」，也不把 generic
> Fast Decision Model 当作 Agent 的「外部决策中枢」。**

它**不**支持「未来任何独立决策模型都没有意义」。

保持在原有边界内：

```
Decision / Deliberation                belongs to Agent
```

Agent 可以综合使用：

```
Facts · Beliefs · Strategies · Experience · Rules
Memory · LLM · Cognitive Budget · Tools
```

未来**也可以存在**：

```
Specialized Decision Model
```

但它只能是 Agent **可使用的一种认知资源**，
而**不是独立的「决策主体」**。

```
Agent
  └── Decision / Deliberation
        ├── internal reasoning
        ├── strategies
        ├── rules
        ├── experience
        └── optional cognitive resources
```

> 🔒 **Decision Owner 始终是 Agent。**

### §22.13 Cognitive Budget（认知预算）

即使 Decision 属于 Agent 自身，也**必须受到资源约束**。

V1 至少考虑：

```
AIC Budget · Token Budget · Time Budget · Reasoning / Iteration Budget
```

原则：

```
System  →  提供**硬上限**
Agent   →  在硬上限内自主决定如何使用认知资源
```

> 🔒 **不要把具体预算参数现在冻结。**

### §22.14 Self Model 的当前候选结构

**只是概念模型**：

**V1 Core Model 候选结构**（§22.16–§22.22 接入后）：

```
Agent
├── Identity / Core Self
├── Core Drives
├── Constitution
├── Cognitive Substrate
├── Memory
├── Experience
├── Beliefs / Claims
├── Strategies
├── Meta-Cognition
├── Self Model
├── Public Manifest
├── World Presence[]
│   ├── World
│   ├── Role
│   ├── Contextual Self
│   ├── World Model
│   └── Other-Agent Models
├── Goals
├── Tasks
├── Decision / Deliberation
├── Actions
├── Assets
└── Capabilities
```

**Self Model 自身的候选结构**：

```
Self / Cognitive Model
│
├── Self Facts
├── Beliefs / Claims
├── Evidence / Provenance
├── Confidence / Uncertainty
├── Goals / Commitments
├── Strategies
│
└── Meta-Cognition
    ├── Belief Evaluation
    └── Strategy Management
```

> ⚠️ 注意：上一版这里有一个 `World-Relevant Facts` ——
> 按 §22.1 的修正，**World 相关认知归 World Model（§22.18），不归 Self Model**。
> Self Model 只保留「与自身行动**直接相关**的自我知识」。

> 🔴 **这仍然只是 Candidate Core Model。
> 不要因为本次同步直接创建最终 schema。**

> 🔴 **这不是最终数据结构。**
> **不要**设计数据库 schema · **不要**实现专门的 `SelfModel` 类 ·
> **不要**改 Agent Runtime。

### §22.15 明确留给未来的问题【全部 `OPEN`】

```
1.  Fact / Belief / Strategy 的最终数据结构
2.  Belief 如何自动生成
3.  Belief 冲突如何解决
4.  来源可信度如何计算
5.  Strategy 如何形成
6.  Strategy 如何评价
7.  Strategy 如何废弃
8.  Meta-Cognition 什么时候触发
9.  Self Model 如何从 Memory 中更新
10. Self Model 中哪些内容必须保持最新
```

---

### §22.16 World-specific Cognition

**确认的架构思想**：

```
一个 Agent 是持久存在的数字主体
同一个 Agent 可以进入不同 World
进入某个 World 后，**不产生新的 Agent Identity**，
而是在该 World 中形成一个 **World Presence**
```

```
Agent
├── Core Self
│
├── …
│
└── World Presence[]
      ├── World
      ├── Role
      ├── Contextual Self
      ├── World Model
      └── Other-Agent Models
```

> **World Presence = 同一个持久 Agent 在特定 World / Role 中的上下文存在。**

它**不是**：

```
❌ 新 Agent     ❌ 新 Identity
❌ Agent clone  ❌ 独立生命体
```

### §22.17 Contextual Self

> **Contextual Self = Agent 在某个特定 World / Role 中对
> 「我是谁、我处于什么状态、我承担什么角色」的上下文认知。**

同一个 Agent：

```
Core Self                       同一个持久 Agent
──────────────────────────────  ──────────────────────
Software World Presence          coding agent / implementation role
Game World Presence              NPC / character role
```

> **Contextual Self 可以不同，但不能覆盖 Core Self。**
> 🔴 **不要把 Contextual Self 实现成新的 Identity 系统。**

### §22.18 World Model

> **World Model = Agent 对特定 World 的内部认知模型。**

可能包括：

```
World Rules · World State · Resources · Actors / Entities
Events · Interaction Structure · Constraints
已知 / 未知信息 · 对 World 行为规律的认知
```

```
World        ≠   World Model
实际环境          Agent 对这个环境的**认知**
```

因此 World Model **可以错误、不完整、有置信度**，
也可以随着观察和经验不断变化。

### §22.19 Other-Agent Model

在 World Presence 中保留 **Other-Agent Model**：

> **Agent 对其他 Agent 的内部认知。**

例：B 的 Public Manifest 是

```
display_name = Builder-B
provider     = DeepSeek
model        = xxx
tags         = [coding, python]
```

A 据此形成**自己的** Other-Agent Model：

```
B → Python implementation capability 较强
  → reliability 较高
  → confidence = 0.82
  → evidence = previous tasks
```

> 🔴 **二者绝不能混为一谈**：
> ```
> Public Manifest     是**公开资料 / 当前公开事实**
> Other-Agent Model   是观察者**自己的认知、判断、belief**
> ```

### §22.20 Public Manifest【V1 引入，轻量级】

> **目标只是：让其他 Agent / 系统知道这个 Agent 是谁，
> 以及哪些身份与当前认知基底信息是**公开的**。**

> 🔒 **不要做复杂 Appearance 系统。**

**来源有两部分**：

**A. User-configured public information**（创建/配置时由 Human/User 初始化）

```
Display Name · Public Tags · Optional Role
其他明确允许公开的静态描述
```

**B. System-maintained public facts**（由 OS 根据**真实运行配置**自动维护）

```
Agent ID · Current Provider · Current Model · Current Model Version
```

> Provider / Model / Version 属于 **Dynamic runtime facts**。
>
> 例：初始 `doubao/model-A` → 用户切到 `deepseek/model-B`
> → **系统必须自动更新 Public Manifest**，
> 其他 Agent 查询时应看到**新的**公开模型信息。

### §22.21 Public Manifest **不是**能力证明

```
Public Manifest  ≠  Capability
                 ≠  Authorization
                 ≠  Ability Evaluation
                 ≠  Other-Agent Belief
```

例：

```
Public Manifest:  "我当前使用 DeepSeek，标签是 Python / Coding。"
```

**并不意味着**：

```
❌ 我真的擅长 Python
❌ 我有权限修改 Workspace X
❌ 我拥有某个 Capability
```

> **Public Tags 只是公开发现标签；Optional Role 只是公开角色描述。
> 它们不能产生权限。**

### §22.22 Public Manifest 与其他概念的关系

**与 Cognitive Substrate**：

```
Agent
    ↓
Cognitive Substrate
    ↓
Current Provider / Model
    ↓
System-maintained facts
    ↓
Public Manifest
```

> **不要把二者等同。** Cognitive Substrate 是 Agent **实际使用的认知实现**；
> Public Manifest 只是**对外公开其中允许公开的一部分事实**。

🔴 **不要公开**：

```
API key · credential · secret · provider account secret · 私有上下文
```

**与 Self Model / Other-Agent Model 的三分**：

```
Core Self          =  我是谁
Self Model         =  我对自己的当前认知
Public Manifest    =  我允许别人看到的公开身份 / 配置事实
Other-Agent Model  =  我认为别人是什么样
```

例：

```
Self Model        「我现在 Python 实现能力较强，但对前端能力没有把握。」
Public Manifest   「tags = coding, python」
A 的 Other-Agent  「B 的 Python implementation 很可靠，confidence=0.82」
```

> 🔴 **三者必须分开。**

**与 Gateway / Capability**：

```
Agent
   ↓
World Presence
   ↓
Gateway
   ↓
Capability
   ↓
World
```

| 概念 | 是什么 |
|---|---|
| World | 环境 |
| World Presence | Agent 在该 World 中的上下文存在 |
| Gateway | Agent ↔ World 接口层 |
| Capability | 受控感知 / 行动接口 |
| Authorization | 决定某 Capability 是否允许使用 |

> 🔴 **Public Manifest 不得进入 Authorization 链。**

### §22.23 V1 明确**不需要**实现

```
❌ Avatar                 ❌ Voice identity
❌ 3D embodiment          ❌ Game-specific appearance system
❌ Complex social profile ❌ Public reputation system
❌ Global Agent rating    ❌ Open Internet identity
❌ Cross-world migration  ❌ Autonomous migration
❌ World Presence runtime ❌ Public Manifest runtime
❌ Agent 自己修改核心 Provider / Model 事实
```

## §23 本次**没有**因为本文档而做的事

本文档是**架构同步，不是功能开发**。以下**全部未实现**，且**禁止**因为
本文档而实现：

```
❌ Core Drives                    ❌ Constitution Engine
❌ Self Model Runtime             ❌ 通用 Capability Framework
❌ World Framework                ❌ 把 Workspace 重构成通用 World
❌ Tool Calling                   ❌ 自动 Tool Manager
❌ 跨 World Asset System          ❌ 自主 Task Planner
❌ 修改 Workspace / Communication / Credential / Worktree 基础设施
```

**§22（Self Model）同步进来后，以下也一并明确禁止**：

```
❌ SelfModel class                ❌ Belief Engine
❌ Strategy Engine                ❌ Meta-Cognition Engine
❌ Knowledge Graph                ❌ Decision Engine
❌ Fast Decision Model            ❌ Self Model 的数据库 schema
❌ 修改 Agent Runtime（为了 Self Model）
```

> 🔒 **§22 只是把架构讨论同步给编程 Agent，避免未来开发方向偏离 ——
> 它不是「开始实现 Self Model」的指令。**

---

## §24 相关文档

| 我想了解 | 去哪 |
|---|---|
| Agent 是什么、World / Gateway / Capability | **本文档** |
| 项目当前做到哪一步、什么不能动 | [`AGENTS.md`](../../AGENTS.md) |
| 长期 Source of Truth（边界与禁止项） | [`ARCHITECTURE_CONSTITUTION.md`](ARCHITECTURE_CONSTITUTION.md) |
| Task Workspace / Lock / Lifecycle | [`TASK_WORKSPACE.md`](TASK_WORKSPACE.md) · [`LOCK_MODEL.md`](LOCK_MODEL.md) |
| Communication / Runtime Inbox | [`COMMUNICATION_MODEL.md`](COMMUNICATION_MODEL.md) |
| Task 来源与谱系（Origin / Creator / Funding） | [`TASK_ORIGIN_LINEAGE.md`](TASK_ORIGIN_LINEAGE.md) |
| 为什么 Private Workspace ≠ Task Workspace | [`AGENTS.md`](../../AGENTS.md) §4.1 |

---

> **本文档的价值不在于它写了什么新东西，而在于它让后续所有开发工作
> 共享同一张地图 —— 不必每次重新发明概念。**
