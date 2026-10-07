# Provider 输出契约修复 + Economy Awareness 只读审计 —— 报告

**Commit** `fab6af5` · **Tests** 3898 passed / 2 skipped / 30 deselected / **0 failed**

基线 `6322092` + `06c8efb`。

---

# 第一部分：Provider 输出契约

## A. Commit
`fab6af5`

## B. Tests
**3898 passed / 2 skipped / 30 deselected / 0 failed**

本轮新增 `tests/test_llm_output_contract.py`（**28 项**），
并**迁移**（非删除）`tests/test_collaborative_production.py` 里过期的本地判据。

## C. Provider 输出契约修复内容

### C.1 修在哪

| 文件 | 改动 |
|---|---|
| `llm/base.py` | 新增 `OUTPUT_VALID_CONTENT` / `OUTPUT_EMPTY_CONTENT`；`LLMResponse.output_status`（**派生属性**）；`LLMResponse.reasoning`（独立字段）；`has_reasoning` / `reasoning_only` |
| `llm/openai_compatible.py` | content 为空/全空白 → EMPTY_CONTENT；null/缺失 → `InvalidResponseError`；`_read_reasoning()` 识别 `reasoning_content` 与 `reasoning`；EMPTY_CONTENT 发 WARNING + `metadata["empty_content"]` |
| `actions/intent.py` | `CognitiveRunResult.output_status`（`None` = 契约未上报） |
| `actions/run.py` | `run_cognition` 透传契约状态 + EMPTY_CONTENT 告警 |

### C.2 为什么修在这里，而不是 `agents/brain.py`

§三 明确要求。而且这条规矩**上一轮真的被守卫抓过一次**：
`Brain.preflight_credential()` 第一版写了 `if provider == "mock"`，
被 `test_no_provider_branching_anywhere_outside_providers` 当场抓住。
**Brain 不知道 DeepSeek / Bailian 是谁** —— 规范化是 Provider 的职责。

### C.3 关键设计：契约状态是**派生**的，不是可写字段

```python
@property
def output_status(self) -> str:
    return (OUTPUT_EMPTY_CONTENT if not self.content.strip()
            else OUTPUT_VALID_CONTENT)
```

如果它是一个可单独赋值的字段，就可能出现
`content=""` 而 `output_status="VALID_CONTENT"` 的自相矛盾对象 ——
**那正是本 Bug 的同一类错误（状态与事实不一致）在更高一层的复现。**
测试里有一条专门钉住它不可写。

### C.4 `PROVIDER_ERROR` 为什么没有做成第三个状态

HTTP / 传输 / 鉴权 / 结构错误在 LLM 层是**异常**
（`LLMRequestError` / `InvalidResponseError` / …），根本走不到 `LLMResponse`。

把它们塞成一个「状态」会让**「失败了」和「成功了但没内容」看起来一样** ——
那正是本 Bug 的成因。所以刻意保留这个不对称。

### C.5 `reasoning_content` 的处理（§五）

- ✅ 识别 `reasoning_content`（DeepSeek）与 `reasoning`（其它网关）
- ✅ 存进**独立字段** `LLMResponse.reasoning`，供诊断 / telemetry
- ✅ `to_dict()` 只暴露 `reasoning_chars`（长度），**不泄露推理原文**
- ❌ **不存在** `if not content: content = reasoning_content`
  —— 有一条 AST 守卫扫 `llm/` 全目录，扫的是**赋值节点**，不是子串

## D. 空 content 的标准语义

| 输入 | 语义 | 结果 |
|---|---|---|
| `content="hello"` + `reasoning_content="..."` | `VALID_CONTENT` | content 原样 = `"hello"`，reasoning 单独存放 |
| `content=""` + `reasoning_content="..."` | **`EMPTY_CONTENT`** | content 仍是 `""`，**不生成假 production content** |
| `content="   "` | **`EMPTY_CONTENT`** | 空白不是内容 |
| `content=null` | **明确失败** `InvalidResponseError` | 报错信息点明「reasoning 不能代替 content」 |
| 没有 content 字段 | **明确失败** `InvalidResponseError` | 同上 |
| 正常结构 | `VALID_CONTENT` | 行为不变（无回归） |

**§六 Case C/D 允许「EMPTY_CONTENT 或等价的明确失败状态」，这里选择了后者。**
理由：网关返回畸形结构必须被看见。若把它降级成普通的空产出，
**一个坏掉的网关就会伪装成「模型今天没说话」。**

## E. 新增测试

`tests/test_llm_output_contract.py` —— 28 项，分 5 组：

| 组 | 内容 |
|---|---|
| `TestOutputContractCases` | §六 Case A–F 逐条 |
| `TestContractGuardsFire` | **反向测试**：把被禁止的写法注入进来，确认真的变红 |
| `TestReasoningIsNotContent` | reasoning 识别 / 不拼接 / 不泄露原文 |
| `TestEmptyContentSemantics` | §七 不许悄悄成功 + §八 成本必须留着 |
| `TestEmptyContentDoesNotSilentlySucceedInCognition` | 生产路径 `LLMResponse → Brain → Decision → Action` |
| `TestContractLivesInLLMLayer` | 架构守卫（Brain 无厂商名分支；`llm/` 无 reasoning→content 回填；`actions/` 不 import `llm/`） |

### 反向测试**实际注入验证过**（不是只写在注释里）

1. 往 `llm/` 真的写入一个 `content = reasoning_content` 的文件 →
   守卫变红：`_evil_probe.py:5 content ← reasoning` ✅（随后已删除）
2. 造一个「坏 Provider」把它回填 → 与正向**同一套断言**跑 → `AssertionError` ✅
3. `test_unknown_contract_is_not_treated_as_success_or_failure` ——
   取不到契约时必须 `None`（**不知道**），不许默认成成功也不许默认成失败 ✅

### 迁移而非删除

`test_collaborative_production.py` 里旧的本地判据
`is_empty_completion(content, output_tokens)` 已**迁移**到 Provider 契约。
**迁移带来的语义变化如实记录**：旧判据要求 `output_tokens > 0`，
新契约**不看 token** —— 空就是 EMPTY_CONTENT。
契约描述「拿到了什么」；「为什么没拿到」是 `reasoning_only` / `finish_reason` 的诊断职责。

## F. 真实 API smoke 结果（§九）

**2 次调用，不是 600 秒实验。**

| Agent | output_status | chars | output_tokens | total | reasoning_chars | simulated | finish_reason | ms |
|---|---|---|---|---|---|---|---|---|
| novel_world_builder | **VALID_CONTENT** | 54 | 131 | 209 | **221** | false | stop | 1280 |
| novel_author | **VALID_CONTENT** | 19 | 134 | 300 | **562** | false | stop | 1266 |

- ✅ 正常 content 仍然正常进入生产路径
- ✅ `simulated=False`（真实 API）
- ✅ **`reasoning_chars` 非零（221 / 562）而 content 不含推理**
  —— 直接证明「推理存在但没有污染交付内容」这条语义在真实调用上成立
- ✅ 成本 delta：**509 tokens / 0.00509 AIC**，照常入账

⚠️ **本次没有复现 empty content。** 如 §九 所述，不要求复现 ——
确定性验证由 28 项 mock/unit 测试承担。

## G. 空 content 是否被当作成功？

**修复前：是（静默成功）。修复后：不是。**

| 层面 | 修复前 | 修复后 |
|---|---|---|
| `LLMResponse` | 只有 `is_empty` 属性，**没人调用** | `output_status == EMPTY_CONTENT` |
| Provider 日志 | 无任何提示 | `WARNING` + `metadata["empty_content"]` |
| `CognitiveRunResult` | 与「Agent 主动不作答」**完全无法区分** | `output_status` 透传，`is_empty_output` 可判 |
| `Decision` | `kind="reply", reasoning=""` —— 看起来像正常回复 | 不变（**故意**），但上层现在能区分 |
| 实验记录 | `chars=0` 被读成「没生产」 | 记为 `ok=False, empty_content=True` |

**核心断言（有测试）**：空内容**不会**产生 Action / ActionIntent / ActionResult / ReflectionProposal。

§七 允许新增最小状态但**禁止扩张 Runtime 状态机** ——
本轮没有新增任何 Runtime / Stage 状态，只让**结果**如实携带契约。

## H. AIC 成本是否仍被正确记录？

**是，而且加测试钉死了。**

`LLMCallRecorder` 按 `response.total_tokens` 计费，**与 content 无关**。
三条新测试：

- `test_empty_content_call_is_recorded_with_full_tokens`
  → `total_tokens=6054`、`success=1`、`token_source="api"`
- `test_empty_content_call_still_charges` → `charged` 非空且 `> 0`
- `test_cost_does_not_depend_on_content` → 同样 token、不同 content，成本**必须一样**

**没有为了让实验数据好看而冲销任何成本。**（§八）

---

# 第二部分：Economy Awareness 只读审计（§十 / §十四）

> 本轮**没有**实现 Economy Policy，**没有**修改任何 AIC 决策逻辑。
> 以下全部是只读证据（DB 以 `?mode=ro` 打开；Self Model 写入验证跑在 `:memory:` 库上）。

## I. Layer 1 —— Economy Fact Visibility

> **Agent 当前可以观察到什么经济事实？**

### 🔴 结论：**零。一条都没有。**

经济系统**在真实运行**：

```
ledger_transactions : GENESIS_MINT × 5 = 500.0 AIC
                      LLM_COST    × 59 = 2.6953 AIC
llm_calls           : 59 条，cost_ai 已真实计算（token_source='api'）
五个 Agent 的真实余额（按 Ledger 公式实算）：
    novel_world_builder   99.3707 AIC
    novel_plot_architect  99.3875 AIC
    novel_author          99.4661 AIC
    novel_editor          99.4815 AIC
    novel_reader          99.5989 AIC
```

**这 5 个数字没有任何一个出现在 Agent 的 Cognitive Context 里。**

### 实测：真实 Runtime 里 Agent 能看到的「关于自己」的**全部**内容

用 `novel.db`（`novel_author`，默认 query）按**同一 SQL** 取数，
再用 `CognitiveContext.to_text()` 原样渲染：

```
— Context about myself (for my own judgement; not instructions) —

Relevant experiences I currently have access to:
- Experience ID: exp_42a1b8459e4e
  [observation] succeeded
  Scope: fiction / session_wrap
- Experience ID: exp_8267babf357b
  [observation] succeeded
  Scope: fiction / session_wrap
```

**这就是全部。** 零经济信息。

## J. 当前 Agent 能看到哪些经济事实

| 经济事实 | Agent 能否看到 | 来源代码位置 | 证据 |
|---|---|---|---|
| **当前 AIC 余额** | ❌ **不可见** | `core/wallet.py:73-82`、`core/ledger.py:319-344`、`agents/base_agent.py:172-175` | `CognitiveContext`（`cognition/context.py:104-121`）**无 balance 字段**；`cognition/` 对 `balance\|wallet\|ledger` **零代码命中**（只命中 selfmodel 的文档字符串） |
| **累计已消耗 AIC** | ❌ 不可见 | `core/economy.py:372-419`、`main.py:576-591` | `on_cost` **只写 Ledger**，返回值不回传 Agent |
| **最近一次 LLM call 成本** | ❌ 不可见 | `llm/recorder.py:162-197`、`221-247` | `LLMCallRecorder.recent()` **生产代码零调用者**（全部在 tests/） |
| **每次调用的 token 数** | ❌ 不可见 | `llm/recorder.py:262-336` | `summary()` 只汇总 token **连钱都不汇总**；调用点 `main.py:1180,1278` 都是 CLI stdout |
| **AIC 稀缺（系统告知）** | ⚠️ **仅 agent_a / agent_b 的静态 prompt** | `agents/agent_a/config.yaml:44-48`、`agents/agent_b/config.yaml:39-43` | novel 实验的 5 个 Agent 的 system_prompt 扫经济词 → **全部零命中** |
| **Constitution / Core Drives** | ❌ **字段存在但恒为 None** | `cognition/context.py:105-106`、`358-359` | `config.yaml` **没有 `cognition` 段** → `default_context_query == {}` → 所有 `build()` 调用点都不传 |
| **当前任务 budget / agreed_price** | ❌ 不可见 | `core/decision_policy.py:240` | 只进 `DecisionPolicy`；`cognition/ actions/ agents/ stage/` 对 `budget` 零命中 |
| **破产 / INSOLVENT 状态** | ❌ 不可见 | `agents/base_agent.py:177-197` | 只在 OS 层门控 |

### 唯一的「带余额字符串」

`agents/base_agent.py:571` 的 `"balance": self.balance`（在 `describe()` 里）——
**调用点只有 `core/economy.py:637`（启动横幅）与 CLI 打印，从未喂给 LLM。**

## K. 当前 Cognitive Context 是否包含 AIC？

**❌ 完全不包含。**

`cognition/context.py` 的**全部 11 个**渲染/构建函数，逐条清点：

| # | 函数 | 行号 | 涉及钱/AIC/成本 |
|---|---|---|---|
| 1 | `CognitiveContext.sources()` | 124-165 | ❌ |
| 2 | `is_empty` | 167-169 | ❌ |
| 3 | `has_experiences()` | 171-173 | ❌ |
| 4 | `stats()` | 175-188 | ❌（只有 count/bool） |
| 5 | `to_text()` | 191-261 | ❌ |
| 6 | `_experience_line()` | 264-267 | ❌ |
| 7 | `_experience_detail()` | 270-277 | ❌ |
| 8 | `_experience_block()` | 280-309 | ❌ |
| 9 | `_scope_label()` | 312-314 | ❌ |
| 10 | `CognitiveContextBuilder.__init__` | 333-346 | ❌ |
| 11 | `CognitiveContextBuilder.build` | 348-395 | ❌ |

**`CognitiveContextBuilder.__init__` / `build` 的签名里没有 economy / ledger / wallet 参数。**
`main.py:646-652` 是唯一生产接线点，**只注入 `self_model` + `experience_store`**。

## L. 当前 Self Model 是否包含经济 Claim / Strategy？

**❌ 不存在。**

`novel.db`：`SELECT COUNT(*) FROM self_model_claims` → **0**；`self_model_strategies` → **0**。

全仓 **42 个 `.db`** 交叉验证：**claims 恒为 0**；
strategies 最多 1–2 条，去重后 10 条，**内容全部是小说写作策略**：

```
· STRATEGY_A：先大量扩写人物细节，再考虑情节
  domain='fiction' situation='story_opening' confidence=0.8
· 先确定核心情节点，再让人物细节服务于情节点
  domain='fiction' situation='story_opening' confidence=0.8
· 先大量扩写人物细节，再考虑情节（confidence=0.4，被修订过）
```

**没有一条是经济认知。**

`Experience` 同理：全仓 **46 条**，JSON 键去重后只有 14 个
（`result.success/output/error/action_type/parameters/kind/...`），**零条含 cost / balance / price / aic**。
（唯一一条「命中」是 `metadata.run="feedback_after_run1"` —— `feedback` 含 `fee` 的**假阳性**。）

## M. 当前是否存在经济行为？

**❌ 不存在。**

- **谈判用的是静态配置 + Task.budget，不是余额/成本。**
  `core/decision_policy.py:227-250` 的 `DecisionContext` **没有 `balance` 字段**；
  三个成本字段（`estimated_task_cost` / `current_workload` / `negotiation_cost`）
  注释写着「为未来成本核算预留，本步骤恒为 None」，**全仓库零赋值**。
- **偿付能力门是 OS 结构约束，不是 Agent 选择。**
  `core/economy.py:409`「余额不足 → 这里会抛出（结构约束，不是策略）」；
  `core/economy.py:391-398`「本方法**只记账**。它不判断『这次调用值不值』」。
- **`can_afford` / `assert_solvent` 是死代码**（生产零调用者，只有 tests）。
- **Executor 被明令禁止判断经济价值**（`actions/executor.py:13`）；
  `actions/` 只有 `reply` / `noop` / `observe`，**没有任何花钱动作**。

## N. 如果没有经济行为，是否因为「没有经济事实进入 Context」？

**✅ 是，这是已被证明的充分解释。**

审计证据链完整且单向：

```
LEDGER（事实确实存在）          500 AIC 铸币 · 2.6953 AIC 成本 · 5 个真实余额
        │
        │  ❌ 断在这里
        ▼
COGNITIVE CONTEXT              CognitiveContext 无 balance/cost 字段
                               Builder 签名无 economy 依赖
                               main.py 只注入 self_model + experience_store
        │
        ▼
SELF MODEL                     claims=0, strategies=0（全仓 42 库 claims 恒 0）
        │
        ▼
ECONOMIC BEHAVIOR              DecisionContext 无 balance；成本字段恒 None
```

**所以未来如果观察到「Agent 不关心成本」，不能得出「Agent 没有经济意识」——
它甚至没有获得经济事实。** §十三 担心的正是这个，审计确认了这个担心成立。

**这也解释了上一轮实验报告里的一个现象**：
读者/作者/编辑在 4 轮协作里**从未提到成本、预算或 AIC** ——
不是因为它们不关心，而是**它们看不到**。

## ⚠️ 附带发现（§十一 相关，必须报告）：Self Model 具备成为 Ledger 第二事实源的**结构性条件**

§十一 要求「不要把经济事实塞进 Self Model」。审计发现：
**这句话目前在代码上没有强制，只写在文档字符串里。**

`reflection/write_service.py:68` 允许 `create_claim` 修改 `content`（**自由文本**），
校验只有「非空」+「confidence ∈ [0,1]」，**没有任何经济过滤**。

**实测（`SelfModelWriteService.apply()` 真实生产写入路径，跑在 `:memory:` 库上）**：

```
content='我的 AIC 余额是 43'          → committed=True  validation ok
content='我认为自己的 AIC 不够支持这个任务' → committed=True  validation ok
content='我付得起这个任务，成本上限是 5 AIC' → committed=True  validation ok
```

即 `selfmodel/models.py:24` 写的 `❌「我的 AIC = 43」` **在代码上可以通过校验并被持久化**。

而且闭环是通的：写进去之后下一轮就会被 `retrieve()` 召回
（无 scope 时 `_scope_ok` 直接 `return True`），
渲染成「Relevant beliefs I currently hold about myself」。

**目前没有发生，是因为 claims 全仓为 0 —— 不是因为有机制拦住它。**

⚠️ 本轮**没有修这个**（§十五 禁止改 Economy / Self Model 架构）。
如实记录，供你们决定。

## O. 后续最小建议

> §十五 禁止本轮实现任何一项。以下只是**建议**，供你们决定。

1. **先决定「Agent 该看见哪些经济事实」，再动代码。**
   本轮只证明了「看不见」。要让 Agent 看见，是**架构决定**，不是顺手改一行 ——
   因为「告诉它余额」和「让它形成经济观念」是两件事（§十.4）。

2. **如果将来要让经济事实进入 Context，最小切口是 `CognitiveContextBuilder`。**
   它已经是唯一的注入点，加一个**只读快照**参数即可，
   `cognition/` 仍然不需要 import `core/ledger.py`（注入纯数据，不注入账本）。
   **不要让 Context 直接持有 Ledger 引用** —— 那会让认知层可以查询/改写经济事实。

3. **`Self Model ≠ Ledger` 需要从「文档」变成「守卫」。**
   现在 `create_claim` 的自由文本没有任何约束。
   最小做法是加一条**守卫测试**（不是运行时过滤）：
   断言 `selfmodel/models.py` 里那句禁止仍然存在，
   并记录「这个结构性缺口是已知的」。
   运行时过滤属于 Economy Policy 范畴，**本轮禁止**。

4. **`LLMCallRecorder.recent()` 目前无生产调用者。**
   将来若要让 Agent 看到「刚才花了多少」，它是现成的只读入口 ——
   但**先做第 1 步的决定**。

5. **`config.yaml` 没有 `cognition` 段**，导致
   `identity / constitution / drives / stimulus / task / stage` 在真实运行中**全部为 None**。
   这是与本轮无关但同样值得记录的**接线缺口** ——
   它不是经济问题，但它让 §十一 提到的 Constitution / Core Drives 机制
   **事实上从未生效过**。

---

## 本轮**没有**做（§十五 禁止范围）

❌ Economy Policy · ❌ Price negotiation · ❌ Agent hiring · ❌ Reverse auction
❌ Profit optimization · ❌ AIC reward/punishment policy · ❌ Coordinator
❌ 新 Workflow · ❌ 修改 Stage · ❌ 修改 Communication Protocol · ❌ Durable Inbox
❌ 没有新增任何「经济人格」设定（§十二）
❌ 没有为了审计而修改 Economy / Self Model / Ledger 的任何一行

**按 §十七 要求，完成后停止。**
