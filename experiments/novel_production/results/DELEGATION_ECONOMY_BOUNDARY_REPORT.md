# Communication vs Task Delegation —— 经济边界实验报告

**Commit** `dd5cd97` · **Tests** 3971 passed / 2 skipped / 30 deselected / **0 failed**

基线 `5981eeb` + `a4497f4`。

---

## 结论（先说最重要的）

> **AIECO 确实能把「生产性任务委托」从普通 Communication 中分离出来，
> 并让它进入 AIC 经济系统 —— 但本轮三次真实实验里，
> 没有任何一次走到 Settlement。**

而「没走到结算」**不是系统缺陷**：三次失败各有明确原因，
其中两次是 Agent **主动、正确地拒绝付款**。

---

## A. Communication

| | 数值 |
|---|---|
| 发送消息 | **2**（Author → Editor，Author → Reader） |
| 收到消息 | **2** |
| Wakeup | **2 / 2**（每次都真的叫醒了收件人） |
| **Agent-to-Agent AIC Transfer** | **0** ✅ |

三轮实验（Run 1/2/3）的 Experiment A 结果**完全一致**：2 messages、2 wakeups、0 transfer。

⚠️ **一个必须说清楚的区分（§十五）**：这里「0」说的是
**Agent 之间没有发生经济交易**，**不是**「这几次调用不花钱」。
每次 LLM 调用都照常产生 `LLM_COST` 并进 Ledger ——
那是「使用认知资源的成本」，与「生产能力交换」不是同一种成本。

---

## B. Task Delegation

| | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| Offer | **0** | 1 | 1 |
| Accept（requester 接受报价） | 0 | 1 | 1 |
| Reject | 0 | 0 | 0 |
| Completion | 0 | **0** | **0** |
| **Settlement** | **0** | **0** | **0** |
| **AIC Transfer 总量** | **0** | **0** | **0** |

### 三次的 outcome 与原因

| Run | outcome | 原因 |
|---|---|---|
| 1 | `no_delegation_emerged` | Author 主动判断**不需要**外部生产投入 |
| 2 | `deliverable_rejected` | Provider 交付的是「我拿不到稿件」的说明，**不是**约定的四项诊断 |
| 3 | `deliverable_rejected` | Provider 的 LLM 调用返回 **EMPTY_CONTENT**，交付为空 |

### Run 1 的拒绝理由（原文摘录）

> 「本阶段没有出现必须由外部生产投入才能补上的交付物缺口……
> 我也不掌握一个明确可承接该工作、并可对其交付质量负责的 **agent_id**；
> 在收件人不确定的情况下发起正式委托，只会形成一次
> **成本不可控、验收标准模糊的经济交换**……」

**这不是「它不愿交易」，而是「它没有这个事实」。**
与上一轮审计发现的「Agent 看不到经济世界」是同一类缺口 ——
Agent Directory 里有花名册，但没人把它放进 Context / Prompt。

### Run 2 / Run 3 的报价与验收（原文摘录）

**Editor 自主报价 30 AIC**（Run 2）：
> 「定价 30 AIC 反映的是四段式报告的分析深度与可定位到具体段落的劳动量
> —— **低于此不足以覆盖认真通读与逐段比对的成本**，
> 高于此则超出本章单章编辑的合理单位产出。」

**Author 接受时引用了自己的真实余额**（Run 2）：
> 「**30.0 AIC 相对我 99.4045 AIC 的余额可控**，
> 且一份带段落定位与高/中/低优先级排序的报告能直接转化为下一轮的修改动作，
> **投入产出比合理**。」

**Author 拒绝验收**（Run 3）：
> 「**交付内容为空**：……没有任何正文、批注或文件引用，
> 无法确认编辑工作是否完成，也无法据此修改稿件。」

---

## C. Agent 行为

| 问题 | 观察 |
|---|---|
| **谁主动提出委托** | `novel_author`（Run 2/3，**自发**） |
| **谁主动报价** | `novel_editor` 自主定价 **30 AIC**（Run 2）/ **24 AIC**（Run 3） |
| **谁主动拒绝** | `novel_author` 两次**拒绝验收交付物** |
| **接受了什么任务** | Editor 接受了「对当前章节初稿做正式文学编辑，交付四段式结构化报告并写入 Workspace」 |
| **是否出现没有预设的经济行为** | ✅ **出现了，而且很明确** |

### C.1 最值得记录的行为（全部**没有被预设**）

1. **Agent 自发用余额做决策。**
   Author 接受 30 AIC 的理由里直接引用了「我 99.4045 AIC 的余额」——
   这是 **Economy Awareness V1 第一次在真实推理里被真正用上**。
   （对比上一轮的负结果：给了余额，Agent 完全没提。）

2. **Agent 自发做了成本定价。**
   Editor 的报价理由是「低于此不足以覆盖成本 / 高于此超出合理单位产出」——
   一个真实的双边定价推理，**不是我写死的角色价**。

3. **Agent 自发区分了「问一句」和「委托」。**
   Run 1 的 Author 在没有任何提示细节的情况下写道：
   > 「此外，『请人给个看法』本身不是委托，若我确实需要外部视角，
   > 也应先自己完成一轮可交付的修订稿，再在下一阶段以清晰的
   > 交付物规格……决定是否值得付费购买。」

   **它自己说出了本轮的核心经济原则。**

4. **Agent 自发拒绝为未交付的内容付款**（Run 2/3）——
   两次拒绝都有明确的合同理由，而不是笼统的不满意。

5. **Agent 主动指出缺口在委托方自己身上**（Run 2）：
   > 「缺口在我这侧 —— 我未把本章全文接入其可读通道。
   > 所以这不是对诚实拒单的惩罚，而是『尚未交付』的判定：
   > **契约保持开放**，我现在就补送全稿，编辑按原四项交付即可，
   > **无需重新议价**。」

   这是在**自主设计补救条款**。

---

## D. Ledger

### 交易前后余额（Run 3，最后一轮）

```
交易前   novel_author        99.4187 AIC
         novel_editor        99.4742 AIC
         novel_reader        99.5927 AIC

交易后   novel_author        99.3818 AIC   ← 只减少了 LLM 成本
         novel_editor        99.4455 AIC   ← 只减少了 LLM 成本
         novel_reader        99.5927 AIC
```

**双方都没有因为委托而增减 AIC —— 因为从未结算。**

### Ledger entries

每一次 `settle()` 会**恰好产生一条** `TRANSFER`（有测试）。
本轮三次实验的 `TRANSFER` 条数 = **0**。
Run 3 新增的 5 条 ledger entry **全部是 `LLM_COST`**，没有一条是转账。

### AIC 守恒验证

`test_ledger_total_is_conserved` 连做三笔委托结算，断言 AIC 总量不变 ✅。
本轮真实实验没有结算，所以守恒**未被真实流量检验** —— 如实记录。
（Run 3 的余额下降来自 LLM_COST，那本身也是守恒的：钱流向了 `SYS_TREASURY`。）

---

## E. 失败（§二十 E 要求如实报告）

> ## **Economic delegation did not complete in this run.**
> 委托**出现了**（Run 2/3），双方**达成了价格**（30 / 24 AIC），
> 但 **Settlement 从未发生** —— 三次都是 Author 拒绝验收。

**没有通过 Runner 强迫它成交。** 三次实验里 Runner 只做了一件事：
把 Agent 已经做出的决定交给 Economy 去验证与执行。

### 三次失败是三种**不同**的原因，都指向真实缺口

| Run | 表面现象 | 真实缺口 |
|---|---|---|
| 1 | 不发起委托 | **Provider 花名册没有进入 Agent 的事实范围**（Agent Directory 有，但没接进 Context/Prompt） |
| 2 | 交付「我拿不到稿件」 | **委托没有「输入原料」的通道** —— Requester 从未把 `manuscript.md` 给 Provider |
| 3 | 交付为空 | **Provider 输出契约在真实生产流程里再次触发**（推理模型耗尽输出预算） |

### 🔴 附带发现的事故（已修）

Run 3 暴露了一个**真实的破坏性 Bug**：

```
Provider 的 LLM 调用返回 EMPTY_CONTENT
    ↓
apply_write() **照常写入**
    ↓
editor_notes.md 从「有内容」被覆盖成 **0 字节**，revision 2 → 3
    ↓
看起来像一次**成功**的写入
```

这与我一直在处理的「接口成功掩盖语义 no-op」是同一类，
但这次它**真的毁掉了共享生产资料**。

修法：`apply_write()` 默认**拒绝空内容**，
要清空文件必须显式 `allow_empty=True` ——
让「我确实想清空」成为一个**必须说出来**的决定。配了 4 条测试（含反向）。

---

## 本轮**没有**做（§ 禁止范围）

❌ bidding ❌ auction ❌ dynamic pricing ❌ reputation pricing
❌ profit optimization ❌ market matching ❌ escrow ❌ credit system
❌ 没有复制第二个 Task 状态机 ❌ 没有硬编码任何角色价格
❌ 没有 Runtime 自动接受 ❌ 没有 Runner 直接改 Ledger
❌ 没有修改 Communication Protocol / Stage
❌ **没有进入 10,000 字正式生产**
❌ 没有继续增加 Economy Policy

---

## 下一步建议（未实现）

按优先级，全部对应**已观测到的真实缺口**：

1. **把 Participant 花名册作为事实接进认知。**
   Run 1 的负结果直接指向它。`LocalAgentDirectory` 已经有
   `all()` / `capabilities`，缺的只是「谁把它放进 Context」。
   ⚠️ 只给「谁在、擅长什么」，**不给**「你应该委托给谁」。

2. **Delegation 需要一个「输入引用」字段。**
   Run 2 证明：没有输入通道，Provider 只能交「我无法作业」。
   最小做法是加 `input_ref`（指向 Task Workspace 里的资源路径），
   与 `deliverable_ref` 对称 —— **只存引用，不存内容副本**。

3. **`Estate` 的 `settle()` 需要一个超时/失效语义。**
   Run 2 里 Author 说「契约保持开放，我现在就补送全稿」——
   系统目前**没有**「重新打开已 ACCEPTED 的委托」的路径。
   这是真实需求，但属于后续设计（可能要引入一个小状态）。

4. **`EMPTY_CONTENT` 在委托链路里应当被当作「未交付」。**
   现在它变成了一次空的 Workspace 写入（已加守卫拦下），
   更好的做法是：`complete()` 之前就能看到输出契约状态。

5. **不要为了得到一笔成交而放宽任何一条。**
   本轮的价值恰恰在于：Agent 在**有真实经济事实**的情况下，
   做出了「这笔钱不该付」的判断 —— 那正是经济主体该有的行为。
