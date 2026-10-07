# Economy Awareness V1 —— 报告

**Commit** `5981eeb` · **Tests** 3932 passed / 2 skipped / 30 deselected / **0 failed**

基线 `fab6af5` + `a051a26`。

---

## ⚠️ 读这份报告前必须知道的一件事

§十八 要求区分三件事。本报告全程按这个分层写：

| | 含义 | 本轮结论 |
|---|---|---|
| **① 看到** | Agent 的 Context 里**有没有**经济事实 | ✅ **有**（新接线，已测） |
| **② 理解** | Agent 是否把它当成有意义的信息 | ⚠️ **没有观察到** |
| **③ 改变行为** | Agent 是否因此改变了做法 | ❌ **没有观察到** |

**不要把 ① 读成 ② 或 ③。** 本轮的代码交付在 ①，
②③ 是真实实验的观察结果 —— 而且是**负结果**。

---

# 第一部分：Self Model 事实隔离

## A. Commit
`5981eeb`

## B. Tests
**3932 passed / 2 skipped / 30 deselected / 0 failed**

新增 `tests/test_economy_awareness.py`（**34 项**），覆盖任务书 §六 A–D 与 §十七 A–J。
每条守卫都配了**反向测试**，并**实际注入违规代码验证过会变红**。

## C. Self Model fact isolation

### C.1 漏洞（上一轮审计发现，本轮修复）

走**正常**的 Controlled Write 路径：

```
create_claim(content="我的 AIC 余额是 43")  →  committed=True
```

`selfmodel/models.py` 里写的那句 `❌「我的 AIC = 43」` **只是文档字符串**。
而且闭环是通的：写进去之后下一轮 `retrieve()` 会把它召回，
渲染成「Relevant beliefs I currently hold about myself」。
**同一个系统里会同时存在两个「余额」。**

### C.2 为什么不用关键词过滤（§四 明确禁止）

```python
if "AIC" in content: reject      # ❌ 被禁止的写法
```

这条规则会**同时**：

- 漏掉「我的钱包里有四十三枚」
- **误杀**「我发现自己过去经常过度消耗 AIC」——
  而 §五 **明确要求允许**这一类

脆弱的关键词表既挡不住变体，又会禁止真正属于 Agent 的认知。

### C.3 ✅ 修法：把「归属」写成一张**可枚举的表**

新增 `selfmodel/fact_domains.py`：

| domain | 谁拥有它（权威源） | 能否写进 Self Model |
|---|---|---|
| `self` | Agent 自己 | ✅ **允许** |
| `economy` | Ledger / Economy | ❌ 拒绝 |
| `identity` | Agent Identity | ❌ 拒绝 |
| `capability` | AgentDirectory / Capability | ❌ 拒绝 |
| `authorization` | Workspace Lock / Gateway | ❌ 拒绝 |

**每条拒绝理由都点名真权威在哪** —— 一句只回 `rejected` 的拒绝，
对 Agent 来说不可学习：

> `fact_domain='economy' 属于系统事实，Self Model 不能成为它的权威来源。`
> `它的权威是：Ledger / Economy（core/ledger.py）。`

## D. 新增 Guard / Validation

### D.1 三个明确的 validation code

| 情况 | code |
|---|---|
| 没有声明 `fact_domain` | `missing_fact_domain` |
| 声明了一个不存在的域 | `unknown_fact_domain` |
| 声明了一个**系统**事实域 | `system_fact_not_self_owned` |

### D.2 为什么 `fact_domain` 是**必填**而不是默认 `self`

```
默认 self  →  「未声明」与「声明为 self」无法区分
              → 而那正是漏洞的形状
必填       →  **沉默**也变成一次明确的表态，于是可以被单独拒绝并计数
```

效果：`「我的 AIC 余额是 43」`
**声明为 economy 被拒；不声明也被拒**。只有**谎报为 self** 才过得去。

### D.3 `fact_domain` 是**声明字段**，不落库 —— 所以必须从 `changed_fields` 排除

这是本轮**最容易埋雷**的一处：

`fact_domain` 参与校验，但 V1 没为它加表列（§四：不为一个测试造框架）。
如果它进了 `changed_fields`，就会出现
「`changed_fields=['fact_domain']` 而 `committed=True`，但数据库里什么都没变」——
**又一种 silent semantic no-op**（本项目反复栽的那一类）。

所以引入 `persisted_fields()`，并加两条测试钉住：
`test_fact_domain_is_a_declaration_not_a_stored_field` /
`test_declaration_only_change_is_not_a_revision`。

### D.4 结构守卫

- **判定只有一张表**：AST 扫 `reflection/write_service.py` 的**字符串常量**，
  不许出现任何系统域字面量。配反向测试（注入 `if domain == 'economy'` 验证会红）。
- **修订路径同样受约束**：不能靠 `revise_claim` 绕过。

### D.5 §六 A–D 逐条落实

| §六 | 要求 | 结果 |
|---|---|---|
| **A** | 「我的 AIC 余额是 43」→ `committed=False` + 明确 code | ✅ `system_fact_not_self_owned`；**未声明也拒**（`missing_fact_domain`） |
| **B** | 余额 99 不因普通 Claim 存在而改变 | ✅ `ledger.balance_of() == 99.0` |
| **C** | Controlled Write 之后 Ledger 完全不受影响 | ✅ `ledger_transactions` 行集合逐条相同 |
| **D** | 正常经济策略应能写入 | ✅ 三条 §五 列出的表述 + 一条 Strategy 全部 `committed=True` |

### D.6 ⚠️ 已知残余风险（未修，明确记录）

`fact_domain` 由 LLM 给出，所以：

> **一个故意把 `economy` 写成 `self` 的模型仍然能绕过去。**

彻底关掉需要「结构化 Claim（谓词 + 主体 + 值）」或 NLP 语义审查 ——
前者是大改，后者被 §四 明令禁止。

**这不是「没想到」，而是明确选择了不做**：在 V1 里，
一个能被说清楚的边界比一个假装完备的过滤器更有价值。

---

# 第二部分：Economic Context V1

## E. Economic Context 结构

`cognition/economic.py` —— **冻结 dataclass**：

```python
@dataclass(frozen=True)
class EconomicContext:
    currency: str = "AIC"
    current_balance: Optional[float] = None      # None ≠ 0
    current_task_cost: Optional[float] = None
    current_session_cost: Optional[float] = None
    as_of: str = ""                              # 快照时刻
    unavailable: Tuple[str, ...] = ()            # 取不到的字段
    sources: Dict[str, str] = {}                 # 各字段来源（可追溯）
```

**§十二 要求的结构化字段**（不是一坨不可解析的字符串）：

```
CognitiveContext
├── identity / constitution / drives / current_stimulus
├── current_task / current_stage
├── economic_context          ← 🆕 结构化只读对象
├── self_claims / strategies / relevant_experiences
└── other
```

渲染出来是（真实输出，非示意）：

```
Economic context:
Currency: AIC
Current balance: 99.4631 AIC
Current task cost: not currently available
Current session cost: not currently available
AIC is the Agent's internal production currency used to account for AI production resources consumed by this Agent.
(Facts read from the Ledger at 2026-10-07T08:08:02.885100+00:00.)
```

### E.1 没有增加的东西（§七 明确禁止）

❌ `profit_target` ❌ `spending_policy` ❌ `economic_score`
❌ `optimization_instruction` ❌ `recommended_model` ❌「应该省钱」

守卫沿用本仓已有的 `BANNED_RULE_WORDS`（不另造一份词表），
再加一条 forbidden 短语表，并配**反向测试**：
注入 `"You should save AIC."` 验证守卫真的会红。

## F. Economic Context 数据来源

```
Ledger / Economy                    ← 唯一经济事实源
      ↓  三个**只读**回调（组合根注入）
EconomicContextBuilder              ← cognition/economic.py
      ↓
CognitiveContext.economic_context
      ↓
LLM（Provider 只收到渲染后的文本）
```

**§八 / §十三 的三条禁止来源全部成立**：

| 禁止来源 | 状态 |
|---|---|
| ❌ Self Model | Builder 完全不知道 Self Model 存在 |
| ❌ LLM | 只从注入回调取值，没有任何默认/猜测 |
| ❌ 实验脚本猜测 | 取不到 → `None` + `unavailable`，**不填 0** |

**§十七 I（不会出现多套经济账本）**：`cognition/economic.py` 结构守卫断言
它**不 import** `storage` / `core.ledger` / `core.economy` / `core.wallet`。
组合根里的 `_sum_llm_cost` 只是在 `llm_calls` 上按维度**聚合**，
没有重算价格、没有另开表。**反向测试**：注入 `from core.ledger import Ledger`
验证守卫会红（已实际跑过 ✅）。

## G. 当前可见经济事实

| 事实 | 状态 | 来源 |
|---|---|---|
| `current_balance` | ✅ **可靠** | `Ledger.balance_of(agent_id, currency)` |
| `current_task_cost` | ⚠️ **当前不可得** | `llm_calls.task_id` 这一列存在，但生产路径从未传入 → 恒 NULL。如实报 `not currently available`，**不伪造 0** |
| `current_session_cost` | ⚠️ **需要调用方给起点** | 「会话」概念住在 Runtime（`stage/limit.py`），core 不知道它。Builder 只提供「某时刻之后花了多少」，起点由 Runtime 传 |

§十一 的要求「无法可靠得到就报告 not currently available」——
**渲染时显式写出来**，而不是默默少一行：少了哪一行必须能被看见。

## H. Ledger / Context 一致性

三条测试钉死：

- `test_context_balance_equals_ledger_derivation` —— Context 里的余额
  与 Ledger 公式实算**逐位相等**
- `test_wallets_table_still_has_no_balance_column` —— 余额是**算出来的**，
  不可能存在第二个事实源
- `test_balance_is_dynamic_not_frozen_at_init` —— 99.2 → 发生成本 → 98.7

---

# 第三部分：真实 Runtime 实验（§十四）

## I. 实验设计

`experiments/novel_production/economy_probe.py`，**4 次真实调用**：

| | Run A | Run B |
|---|---|---|
| Agent | `novel_author` | **同一个** |
| 任务 / Prompt | 同一段（**一个字都不提经济**） | **同一段** |
| Economic Context | ✅ 开 | ❌ 关 |
| 样本数 | 2 | 2 |

**§十四 明确要求不做的事，全部没做**：
❌ 没告诉 Agent「请考虑 AIC」❌ 没给省钱策略
❌ 没预写 `Strategy = minimize cost` ❌ 没预写 `Claim = AIC is scarce`

## J. Agent 是否自然提及 AIC？

### 🔴 **没有。这是一次负结果。**

```
Run A（有经济事实）  提到经济词 1/2
Run B（无经济事实）  提到经济词 0/2
```

**但那唯一一次命中是假阳性，必须说清楚：**

> Run A 第 0 次输出：「视角或已知信息一旦滑动，后面要用成倍篇幅回补；
> 而**核对成本极低**，属于投入产出比最高的一步。」

这里「成本」指的是**改稿的工作量**，不是 AIC。
**4 次调用里没有一次出现 AIC、余额、或对自身资源状态的任何提及。**

诚实结论：**在给了真实经济事实的情况下，Agent 在这一次小样本里
没有把它纳入推理。**

## K. Agent 是否自然产生经济判断？

**没有。** 两次 Run A 的产出都是纯创作规划
（「下一场戏怎么排」「信息差放在哪」），
经济事实没有改变它们的**内容方向**。

## L. 是否出现经济 Strategy / Claim？

**没有。** 实验后实测：

```
self_model_claims        : 0 行
self_model_strategies    : 0 行
含经济词的 Claim/Strategy : 0 条
含经济词的 Experience     : 0 条
```

⚠️ 说明：本实验只调用 `generate()`，**没有走 `act()` / Cognition 路径**，
所以本来就不会产生 Reflection Proposal。
**这里的 0 不能证明「Agent 不愿意形成经济策略」** ——
只证明「这次短跑里没有反思环节」。

## ✅ 但有一条**强证据**：动态更新在真实 Runtime 里成立（§十七 H）

```
Run A 第 0 次：Context 里 balance = 99.454
               ↓ 这次调用真的花了钱
Run A 第 1 次：Context 里 balance = 99.4452   ← 变新了
```

**这是 §十 的真实验证**：余额不是初始化时写死的，是每次从 Ledger 现取的。

## M. 如果没有，为什么

三条可能，本实验**不足以区分**它们 —— 如实列出，不挑一个当结论：

1. **样本太小。** A 组只有 2 次调用、1 个 Agent、1 个任务。
   单次负结果不能推出「Agent 不会用经济事实」。
2. **任务里没有经济决策点。** Prompt 是「下一轮推进什么」，
   这是一个**纯创作**问题。余额对回答它没有直接帮助 ——
   所以「没提到」是**合理**的，不一定是「没注意到」。
3. **经济事实确实进了 Context，但没有形成判断的压力。**
   系统里**没有任何**要求 Agent 对成本负责的机制
   （没有预算约束、没有成本门槛、没有激励）。
   §十二 明确禁止本轮预设经济人格 —— 所以这是**预期内的空白**，
   不是缺陷。

**这三条里最要紧的是第 2 条**：要观察「Agent 会不会把经济事实纳入认知」，
任务本身必须**存在一个经济选择**（例如「在两种做法之间选」），
而不是抽象的创作规划。

## N. 下一步建议

> §十五 禁止本轮实现任何一项。以下只是建议。

1. **先设计一个「有经济选择」的任务，再谈观察经济行为。**
   本轮的负结果很大程度上是任务设计造成的。
   最小改法：给 Agent 一个**两种做法成本差异明显**的选择，
   问它选哪个、为什么 —— **仍然不提示 AIC**。

2. **`current_task_cost` 现在恒为 `not available`，值得先补上。**
   `llm_calls.task_id` 列已经存在，只是生产路径没传。
   这是**纯接线**（把 task_id 从 Task 传到 `Brain.generate`），
   不是策略 —— 但它让「任务花了多少」变成可见事实。
   ⚠️ 补它之前先确认：这不会变成事实上的成本门控。

3. **`current_session_cost` 的起点应当由 Runtime 提供。**
   `stage/limit.py` 的 `RuntimeSession` 已经在管会话时间，
   加一个 `started_at` 即可 —— 不需要新概念。

4. **`fact_domain` 的残余风险要有一份记录在案的处置计划。**
   当前只有「拒绝声明为系统域的」这一层。
   如果将来发现真实模型会谎报，再考虑结构化 Claim ——
   **不要**退回关键词过滤。

5. **本轮的 Self Model 事实隔离值得单独审视一次。**
   它现在是一条**必填声明**规则，会影响所有未来的
   Reflection Proposal 生成 —— 建议在写 prompt / 解析器时
   把 `fact_domain` 明确列进契约，否则模型会大量触发
   `missing_fact_domain` 而被静默丢弃。

---

## 本轮**没有**做（§ 禁止范围）

❌ Economy Policy ❌ 价格协商 ❌ Agent 雇佣 ❌ Reverse auction
❌ 利润最大化 ❌ AIC reward/punishment policy ❌ Coordinator ❌ 新 Workflow
❌ 修改 Stage ❌ 修改 Communication Protocol ❌ Durable Inbox
❌ 经济人格（「AIC 是生命资源」/「必须省钱」等一律没有写入任何 prompt 或 Strategy）
❌ 没有修改 Self Model 的**架构**（只加了一层入口校验，没动模型与存储）
❌ 没有修改 Ledger / Wallet / Economy 的任何一行逻辑
❌ **没有开始 10,000 字正式生产**

**按 §十八 要求，完成后停止。**
