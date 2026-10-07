# Self Model V1 架构对齐审查
## Architecture Alignment Review

> **基线**：`e832188` · 3650 passed / 0 failed
> **本轮不改生产代码。** 只审查。
> **最高原则**：「规则限制结构，策略决定行为，激励塑造行为，反馈推动演化。」

---

## 0. 一句话结论

# 当前仓库距离 Self Model V1 = **除「Memory 有一个文件目录形态」外，几乎 100% 未实现。**

而且更重要的一点：

> 🔴 **在 H-1（Agent → Action 通道）打通之前，先建 Self Model
> 会造出一个「无法影响行为」的模型 —— 这恰好违背 §0.5「策略决定行为」。**

---

## 实证：各概念在代码中的存在情况

搜索范围：`core/ agents/ llm/ communication/ services/ storage/ change/`

| 概念 | 命中文件数 | 判定 |
|---|---|---|
| `SelfModel` / `self_model` | **0** | ❌ 不存在 |
| `Strategy`（作为 Self Model 概念） | 0 | ❌ 不存在（2 处命中是同名词的无关用法） |
| `Belief` / `belief` | **0** | ❌ 不存在 |
| `MetaCognition` / `meta_cognition` | **0** | ❌ 不存在 |
| `Experience` / `experience` | **0** | ❌ 不存在 |
| `Reflection` / `reflection` | **0** | ❌ 不存在 |
| `CognitiveContext` / `cognitive_context` | **0** | ❌ 不存在 |
| `WorldModel` / `world_model` | **0** | ❌ 不存在 |
| `WorldPresence` / `world_presence` | **0** | ❌ 不存在 |
| `ContextualSelf` | **0** | ❌ 不存在 |
| `OtherAgentModel` | **0** | ❌ 不存在 |
| `PublicManifest` / `public_manifest` | **0** | ❌ 不存在 |
| `Memory` | 3 | ⚠️ **仅文件目录形态**（见下） |
| `DecisionPolicy` | 5 | ✅ 存在（但**仅用于任务协商**） |
| `capability` | 5 | ✅ 存在（**能力声明与匹配**，非 Self Model） |

---

## A. 当前仓库已与 V1 设计一致的部分

> ⚠️ **本节比想象中短，但每一条都是真的。**

| # | 设计要求 | 当前代码 / 文档 | 为什么算一致 |
|---|---|---|---|
| A-1 | `Agent Identity ≠ Runtime ≠ Provider ≠ Model` | `core/identity.py` · `llm/router.py` · §22.37 · §0.5.11 · ADR-0038 | 代码与 ADR 都把它们分开了；有守卫 |
| A-2 | `Self Model ≠ Memory` | §20 不变量 #11 | **已冻结为架构不变量**（虽然代码里两者都还没建） |
| A-3 | `Self Model ≠ World Model` | §20 #12 · §22.1（已修正） | 已冻结；且上一轮专门改掉了「Self Model 包含世界」的措辞 |
| A-4 | `World ≠ World Model` | §20 #13 · §22.18 | 已冻结 |
| A-5 | `Public Manifest ≠ Capability ≠ Authorization` | §20 #14 · §22.21 · §22.22 | 已冻结；且明确「Manifest 不得进入 Authorization 链」 |
| A-6 | `Self Model ≠ Other-Agent Model` | §22.19 · §22.22 | 已明确「一个是公开事实，一个是我的判断」 |
| A-7 | Decision Owner 是 Agent，不是中央 Engine | §22.12 · §22.28 · §0.5 | **已在文档里确认三次**；且审计实证**代码里确实没有** auto-assign / 中央调度器 |
| A-8 | Belief 必须有 scope / confidence / evidence / provenance | §22.4 | 文档已写；且明确「不应只记录一个真假值」 |
| A-9 | Strategy ≠ Rule | §22.7 · §0.5.8 | 已确认「低质量启发式也可能有局部价值」；明确禁止把合作策略硬编码 |
| A-10 | Meta-Cognition 不做 Meta-Meta | §22.10 | 已明确 V1 只做 L0/L1/L2 |
| A-11 | Experience → Abstraction → Belief/Strategy | §22.11 | 已确认「Strategy = Agent 对过去昂贵认知过程的压缩」 |
| A-12 | **成本必须能被 Agent 感受到** | `llm/cost.py` · `Economy.charge_llm_cost` · 主路径已接线 | ✅ **本项真的有代码**（见 §12 反馈闭环的最低要求） |

**总结**：一致的 12 条里，**11 条是「文档/ADR 层面已确认」，只有 1 条（A-12）有真实代码。**

---

## B. 当前仓库与设计存在偏差的部分

| # | 设计要求 | 当前实现 | 具体偏差 | 影响 |
|---|---|---|---|---|
| **B-1** | Self Model 是「对自己」的工作模型 | **完全没有** | 无 `SelfModel` 类、无存储、无检索 | 🔴 **核心缺口**。Agent 没有「关于自己」的任何持久认知 |
| **B-2** | Strategy 可创建/检索/应用/评价/修改/降 confidence/废弃 | **完全没有** | 零代码 | 🔴 §0.5「策略决定行为」**无从生效** |
| **B-3** | Belief 有 confidence / evidence / scope，会被修正 | **完全没有** | 零代码 | 🔴 §0.5「反馈推动演化」**无从生效** |
| **B-4** | 反馈能推动 Experience / Belief / Strategy 变化 | **只有结果，没有回流** | Task 有验收状态，但**不产生** Experience/Reflection，也不修改任何长期状态 | 🔴 「反馈推动演化」的**闭环是断的** |
| **B-5** | Self Model 参与 Cognitive Context 构建 | **没有 Context 构建层** | `Brain.generate()` 只接收 messages；无检索、无选择、无注入 | 🔴 即使建了 Self Model，也**进不了 LLM 的输入** |
| **B-6** | LLM proposes → Runtime validates → Self Model Service → Persistence | **完全没有** | 零代码 | 无结构化的写入路径（好消息：也**没有**「LLM 任意改写身份」的风险，因为还没这条路） |
| **B-7** | Self Model 可引用 AIC 余额等权威状态 | 余额由 Ledger 权威持有 | 无 Self Model，自然无重复权威 | ✅ **当前无风险**，但建的时候必须守住 |
| **B-8** | **Agent → Action 通道**（H-1） | **完全没有** | 审计已实证：LLM 无 tool 暴露，`tools=` 零匹配 | 🔴🔴 **这条最致命** —— 见 §0 |

### 🔴 关于 B-8：为什么它比 B-1~B-7 都重要

§0.5 说「**策略决定行为**」。这条链需要两端：

```
Self Model / Strategy  ──→  ????  ──→  行动
      （B-1~B-3）          缺的通道      （B-8）
```

**现在两端都缺。但如果只建左端（Self Model 而不建右端）**：

- Agent 会形成 Strategy
- 但**无法用任何 Action 去执行它**
- 于是 Strategy **只影响它自己生成的一段文本**，不影响世界
- → **Self Model 退化成「一个自我描述生成器」**

> 🔑 **这就是为什么建议顺序是 H-1 在前**（见 §E）。
> 不是 H-1 比 Self Model 更本质，而是**先有通道，建模才有意义**。

---

## C. 已经存在但概念混乱的部分

### ⚠️ 先说一个反直觉但重要的结论

> **在代码层面，几乎没有「概念混乱」—— 因为这些概念一个都没实现。**
> 你无法把两个不存在的东西混在一起。
>
> **混乱风险全部在文档层面**，而且**已经发现并修掉了一处**。

### C-1 ✅ 已修正：Self Model 曾包含「世界」

| | |
|---|---|
| **位置** | §22.1（修正前） |
| **原文** | 「Self Model 是 Agent 对自身，**以及与自身行动相关的世界**的动态内部认知模型」 |
| **问题** | 这就是本审查要找的「World Model 与 Self Model 混合」 |
| **处置** | ✅ **已在上上轮修正**为「对**自身**的动态内部认知模型」，并在 §20 加了不变量 #12 `Self Model ≠ World Model` |
| **连带修复** | §22.14 候选结构里的 `World-Relevant Facts` 也一并删除 |

### C-2 ⚠️ 仍然存在：`Memory` 一词在代码里指「文件目录」，在文档里指「长期认知存储」

| | |
|---|---|
| **代码位置** | `core/workspace.py:73` `self.memory_dir` · `:187` `memory_path()` |
| **代码含义** | Agent 私有工作区里**与 workspace 平级的一个目录**，存长期文件 |
| **文档含义** | §22.13 说的 Memory 是「长期经验/信息存储」 |
| **偏差** | 同一个词，一个是**文件系统概念**，一个是**认知概念** |
| **影响** | ⚠️ 中等 —— 未来实现 Memory 认知层时，会与这个目录**同名但不同物** |
| **建议** | **不改代码**（Workspace Core 冻结）。在实现认知 Memory 时**用不同命名**（如 `cognitive memory` / `LongTermMemory`），并在文档里显式说明这两个不是一回事 |

### C-3 ⚠️ 需警惕：`capability` 与 ADR-0038 的 `Capability` 是两个不同东西

| | |
|---|---|
| **代码位置** | `core/capability.py` |
| **代码含义** | **能力声明与匹配** —— 解决「这个任务需要什么能力？谁会？」 |
| **ADR-0038 含义** | **Agent 与 World 之间进行感知与行动的受控接口**（Perception / Action） |
| **偏差** | 同名，一个是**任务路由的匹配标签**，一个是**行动接口** |
| **影响** | ⚠️ 未来实现 Action Capability 时会撞名 |
| **建议** | 文档里明确区分。**不要**在实现 Action Layer 时误用现有的 `capability` 模块 |

### C-4 ✅ 无混乱：`DecisionPolicy` 没有被误用成 Self Model

`DecisionPolicy` 是**任务协商**的纯函数（ACCEPT/COUNTER/REJECT），
来自每个 Agent 自己的 config，且**有守卫**禁止价格阈值出现在 Service 里。
→ 它是 **Agent 的策略**，不是 Rule，**也没有**变成中央决策引擎。**健康。**

### C-5 ✅ 无混乱：Public Manifest 与 Self Model 无混合

两者都**没有代码**，但文档层面 §22.21 / §22.22 已把它们严格分开，
并明确「Manifest 不等于完整 Self Model / 全部 Memory / 全部 Strategy」。

---

## D. V1 最小实现缺口

> 只列**真正必须**补齐的。未来功能（复杂 Belief Graph / 人格模型 / 跨 World Migration …）一律不进。

| # | 缺口 | 最小形态 | 依赖 |
|---|---|---|---|
| **D-1** | **Agent → Action 通道** | 让 Agent 能表达「我要做一个 Action」，并由 OS 校验+执行+返回 Observation | 无（**这是第 0 步**） |
| **D-2** | **Self Model 存储与检索** | 一个持久结构：`self_facts / beliefs / strategies`，每条带 `scope / confidence / evidence / status / timestamps` | D-1（否则无意义） |
| **D-3** | **Strategy 生命周期** | create / find / apply / evaluate / revise / abandon（最低 7 个操作） | D-2 |
| **D-4** | **Experience → Reflection → Belief/Strategy** | 任务结束后产生一条 Experience，一次 Reflection 试图抽象出 Belief/Strategy | D-2, D-3 |
| **D-5** | **Cognitive Context 构建** | 按 Stimulus / Task / World 检索**相关**的 Self Model 内容并注入 prompt | D-2 |
| **D-6** | **受控写入路径** | LLM proposes → Runtime validates/structures → persist | D-2 |

### 🔴 明确**不在** V1 最小缺口里的

```
❌ World Presence / Contextual Self（多 World 是 V2 的事）
❌ World Model / Other-Agent Model（同上）
❌ Public Manifest
❌ Meta-Cognition 的 engine 形态（V1 只需「Agent 能评价/修正 strategy」，
   不需要单独的 Meta-Cognition Engine —— 那会变成中央决策器）
❌ 任何 Belief Graph / 本体 / 人格量表
```

> ⚠️ 注意 D-4 与「Meta-Cognition」的关系：
> **V1 的 Meta-Cognition 不是一个引擎，而是「Agent 能评价和修正自己的 strategy」
> 这一能力的集合。** 一旦做成 `MetaCognitionEngine`，就违背了
> 「Decision Owner = Agent」和「不要中央决策器」。

---

## E. 建议的实现顺序

按**最小依赖**排序：

```
Step 0   Agent → Action 通道（D-1 = 审计的 H-1）
         ↓   理由：没有它，后面建的一切都无法影响行为
              （§B-8 的论证）
Step 1   Self Model 结构 + 持久化 + 检索（D-2）
         ↓   理由：Strategy/Belief 都要存在它里面
Step 2   Strategy 生命周期（D-3）
         ↓   理由：§0.5「策略决定行为」的直接支撑
Step 3   Cognitive Context 构建（D-5）
         ↓   理由：让 Self Model 真正**进入** LLM 的输入，
                  否则 Step 1-2 只是写了个没人读的数据库
Step 4   Experience → Reflection（D-4）
         ↓   理由：§0.5「反馈推动演化」的闭环
Step 5   受控写入路径（D-6）
             理由：放在最后 —— 前 5 步都用显式代码写，
                   这一步才引入「LLM 提议」的路径
```

> 🔑 **Step 1 和 Step 3 之间的顺序是刻意的**：
> 如果只做 Step 1-2 不做 Step 3，Self Model 就是一个**只写不读**的存储 ——
> 看起来做了很多，实际对 Agent 行为**零影响**。
> 这两步应该被看作**一个不可分割的最小可用单元**。

### 与既有路线的冲突提示

上一轮的下一阶段方向说的是「先完成 Self Model V1，再接入 Cognitive Context」（步骤 1→2）。
**本审查建议把 Action 通道（H-1）插到最前面**，理由见 §B-8。
这是本次审查里**唯一一处与既有路线不同的建议**，请优先确认。

---

## F. 本轮【不应该】实现的内容

```
❌ Meta-Meta-Cognition
❌ 复杂 Belief Graph
❌ 全自动神经符号推理
❌ 复杂人格模型 / 心理学式人格量表
❌ 模型内部可解释神经状态
❌ 复杂长期价值学习算法
❌ 完整自治世界模拟
❌ 自动跨 World Identity Migration
❌ 复杂声誉算法 / 完整社会关系图谱
❌ 多层 Self Model ontology
❌ World Presence / World Model / Other-Agent Model / Public Manifest 的**运行时**
❌ 任何形式的中央 Decision / Meta-Cognition Engine
❌ 把 Memory / Self Model / World Model / Other-Agent Model 合成一个上下文库
```

---

## 二十四. 最终输出

### 1. Self Model V1 对齐结论（一句话）

> **设计已基本对齐（12 条一致，11 条在文档层），但实现几乎为零；
> 且当前顺序会把 Self Model 建成一个「无法影响行为」的模型 ——
> 应先打通 Agent → Action 通道。**

### 2. Confirmed

```
✅ Identity ≠ Runtime ≠ Provider ≠ Model            （代码+ADR）
✅ Self Model ≠ Memory                              （§20 #11）
✅ Self Model ≠ World Model                         （§20 #12，已修正过）
✅ World ≠ World Model                              （§20 #13）
✅ Public Manifest ≠ Capability ≠ Authorization      （§20 #14）
✅ Self Model ≠ Other-Agent Model                    （§22.19）
✅ Decision Owner = Agent，无中央 Decision Engine     （文档×3 + 审计实证）
✅ Belief 必须有 scope/confidence/evidence/provenance（§22.4）
✅ Strategy ≠ Rule，低质量启发式也有局部价值          （§22.7）
✅ Meta-Cognition 只做 L0/L1/L2                      （§22.10）
✅ Experience → Abstraction → Belief/Strategy        （§22.11）
✅ 成本必须能被 Agent 感受到                          （代码，主路径已接线）
```

### 3. Implementation Gaps

```
D-1  Agent → Action 通道            ← 第 0 步，最重要
D-2  Self Model 存储与检索
D-3  Strategy 生命周期（7 个操作）
D-4  Experience → Reflection
D-5  Cognitive Context 构建
D-6  受控写入路径（LLM proposes → Runtime validates）
```

### 4. Architecture Risks

| # | 风险 | 严重度 |
|---|---|---|
| **R-1** | **先建 Self Model 而不建 Action 通道** → Strategy 无法影响行为，Self Model 退化成「自我描述生成器」 | 🔴🔴 **最高** |
| **R-2** | 只做存储不做 Cognitive Context → Self Model 变成**只写不读**的数据库 | 🔴 高 |
| **R-3** | 把 Meta-Cognition 做成 `Engine` → 违背「Decision Owner = Agent」 | 🔴 高 |
| **R-4** | `Memory` 一词在代码（文件目录）与文档（认知存储）中含义不同 → 实现时撞概念 | ⚠️ 中 |
| **R-5** | `core/capability.py`（任务路由标签）与 ADR-0038 的 `Capability`（行动接口）同名不同物 | ⚠️ 中 |
| **R-6** | Self Model 被当成 AIC 余额 / Capability 的第二权威源 | ⚠️ 中（当前无风险，建时须守） |
| **R-7** | 把 Belief/Strategy 实现成**不可变的配置**而非**可被修正的认知** | ⚠️ 中 |

### 5. Recommended Implementation Order

```
Step 0  Action 通道（D-1）
Step 1  Self Model 结构+持久化+检索（D-2）
Step 2  Strategy 生命周期（D-3）
Step 3  Cognitive Context（D-5）
Step 4  Experience → Reflection（D-4）
Step 5  受控写入路径（D-6）
```

### 6. Deferred vs Open Questions

**暂缓实现（设计已定，只是不做）**：

```
World Presence · Contextual Self · World Model · Other-Agent Model
Public Manifest（概念已 CONFIRMED，运行时暂缓）
Meta-Meta-Cognition · 复杂 Belief Graph · 人格模型 · 跨 World Migration
```

**设计尚未最终确定（真的还没想清楚）**：

```
· Fact / Belief / Strategy 的最终数据结构            （§22.15 #1）
· Belief 如何**自动**生成                             （§22.15 #2）
· Belief 冲突如何解决                                 （§22.15 #3）
· 来源可信度如何计算                                   （§22.15 #4）
· Strategy 如何形成 / 评价 / 废弃                      （§22.15 #5-7）
· Meta-Cognition 什么时候触发                          （§22.15 #8）
· Self Model 如何从 Memory 中更新                      （§22.15 #9）
· Self Model 中哪些内容必须保持最新                     （§22.15 #10）
· Self Model 与 Cognitive Context 的检索策略           （§22.22 相关系列）
```

> 🔴 **不要把这两类混起来。** 前者是「想好了但先不做」，
> 后者是「还没想好」。把它们混在一起会导致：
> 做前者时以为在做后者，或反过来。

### 7. 最高原则专项检查

> **问题：当前 Self Model 的任何设计，是否会导致 Agent 行为被系统硬编码？**

**答：设计本身没有，但有两处需要盯住。**

| 检查项 | 结论 |
|---|---|
| Rule 是不是被实现成了 Strategy？ | ✅ **不是**。Lock/Permission/Gateway 都是结构约束，不含行为流程 |
| Strategy 会不会被编译成固定 if/else？ | ⚠️ **有这个风险**（R-7）。§22.7 已明确禁止标记 Good/Bad，但实现时容易退化成规则表 |
| Incentive 是否直接规定了行为？ | ✅ **不是**。成本已进 Ledger，是**改变选择环境**而非规定动作 |
| Feedback 是否能推动长期状态变化？ | ❌ **不能** —— 闭环是断的（B-4）。这**不是硬编码**，是**缺失** |
| Runtime 是否成了中央 Decision Engine？ | ✅ **不是**。审计已实证无 auto-assign、无调度器 |
| Decision Owner 是不是 Agent？ | ✅ **是**（文档层），但**Agent 目前无法行使**（B-8：没有 Action 通道） |

**唯一明确的硬编码风险**在 `§22.7` 的落地方式上：

> 「Strategy 不要求绝对正确」 + 「关注 Applicability / Expected Benefit /
> Cost / Risk / Evidence / Failure Conditions」
>
> ⚠️ 如果实现时把这些**变成阈值判断**（例如
> `if benefit > cost * 1.5: apply(strategy)`），
> 那 Strategy 就退化成了 **Rule**，直接违背 §0.5.1。

**建议**：实现 Strategy 时，这些属性应该是**供 Agent 检索和比较的数据**，
**不是** 由 Runtime 求值的门控条件。这条需要写进实现期的验收标准。
