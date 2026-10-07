# 五 Agent 正式生产实验 —— 报告（第一次运行）

**Commit** `e07a1e6` → `8b2423d` · **Tests** 3990 passed / 2 skipped / 30 deselected / **0 failed**

---

## 🔴 先说最重要的：正式生产被**三个基础设施参数**连续卡住

本轮最大的产出不是小说，而是发现了**三个阻塞性参数**。
它们长得完全一样 —— **不是 Agent 不愿生产，而是基础设施把生产截断了。**

| # | 参数 | 现象 | 实测数据 |
|---|---|---|---|
| **1** | `max_tokens: 4096` | 25/27 次调用 `EMPTY_CONTENT` | 157,044 tokens / **1.57044 AIC** 烧掉，几乎零产出 |
| **2** | `timeout_seconds: 60` | 10 次调用里 **5 次** `LLMTimeoutError` | 修好 #1 后立刻暴露 |
| **3** | **模型选择** `deepseek-flash` | 加大预算**无效**，反而成本翻倍 | max_tokens 4096 → reasoning 7k–17k 字符；**16384 → reasoning 39k–50k 字符，仍然全撞上限** |

### #3 是真正的根因

> **`deepseek-flash` 在本项目的调用形状下推理无上限 —— 给多少预算就吃多少，永远到不了 `content`。**

这不是「预算不够」，是「模型选错了」。

**修复**：切到 `deepseek-chat`（非推理模型，也正是 `config.yaml` 里 provider 的默认 model）。

**验证（Session 5，单轮）**：
```
✅ 5/5 VALID_CONTENT   5/5 写入文件   5/5 发消息   2 次 wakeup
一轮 58.5 秒 / 35,673 tokens / 0.35673 AIC
```
对比 `deepseek-flash` 的一轮 **340 秒且大部分是浪费**。

> **这是本项目第一次在真实生产里看到「一轮之内全部 Agent 都产出了东西」。**

### 这条链子对上一轮结论的修正

上一轮我修了 **Provider 输出契约分类**，报告说「空产出不再被当成成功」。
那只让它**可见**，**没有让它可用** —— 而且真正的根因（模型选择）当时没找到。

本轮证明：**光有分类不够，必须把参数修对。**

---

## 二、Task Production Budget V1（已交付）

`budget/`：`models` + `store` + `service`。

```
$ sqlite3 experiments/novel_production/data/novel.db \
  "PRAGMA table_info(task_production_budgets);"
task_id | budget_aic | currency | mode | created_at      ← 只有 5 列
```

🔒 **`spent_aic` / `remaining_aic` 从不落库** —— 与 `wallets` 表刻意没有
balance 列是同一条纪律。`set_budget()` 签名里连入口都不给。

**spent 来自真实经济数据**（不新建第二套账本）：
```
spent = Σ llm_calls.cost_ai (task_id=本 Task)
      + Σ ledger_transactions.amount (task_id=本 Task AND tx_type='TRANSFER')
```

**实时验证（Session 6 进行中，真实数据）**：
```json
{"budget_aic": 50.0, "spent_aic": 8.79449, "remaining_aic": 41.20551,
 "mode": "OBSERVE_ONLY", "is_exhausted": false, "blocks_production": false,
 "untagged_llm_calls": 82}
```
`blocks_production` 恒为 `False` —— 有测试钉住。**OBSERVE_ONLY 不阻断任何东西。**

⚠️ `untagged_llm_calls: 82` 是**修复前**的存量调用（那时 runner 没传 `task_id`）。
Session 6 的调用全部带 tag，所以 `spent` 是准的。审计字段如实暴露了这个缺口。

---

## 三、正式生产运行状态（Session 6，进行中）

```
▶ Session 6  Stage=stage_novel_1 status=ACTIVE
▶ Budget=50.0 AIC mode=OBSERVE_ONLY（不阻断）
↩︎ Checkpoint ckpt_a858722955d9  rev=...ad44d4d20820adcd
   本进程读到 rev=...ad44d4d20820adcd  ✅一致
```

| 轮到 | 用时 | 结果 |
|---|---|---|
| 1 | 0s | 5/5 产出 ✅ 5 写文件 ✅ 5 发消息 |
| 2–11 | 553s | **每一轮每一个 Agent 都产出并写入** ✅ |

**每轮都在推进**：`world_bible.md` R14、`plot_outline.md` R14、
`manuscript.md` R17、`editor_notes.md` R17、`reader_report.md` R24。

Session 6 真实 API（进行中）：
| Agent | calls | in | out | total | AIC |
|---|---|---|---|---|---|
| novel_plot_architect | 15 | 70,521 | 83,463 | 153,984 | 1.53984 |
| novel_author | 15 | 70,766 | 61,535 | 132,301 | 1.32301 |
| novel_world_builder | 15 | 62,881 | 62,322 | 125,203 | 1.25203 |
| novel_editor | 14 | 65,311 | 56,787 | 122,098 | 1.22098 |
| novel_reader | 14 | 65,076 | 35,178 | 100,254 | 1.00254 |

> ⏳ **本 Session 尚未结束**（3600s Hard Limit）。完整 JSON 将在结束时写入
> `experiments/novel_production/results/formal_production_session_06.json`。

---

## 四、🔴 第二个重大发现：文件被**改写**，不是**累积**

| 文件 | revision | **字符数** |
|---|---|---|
| `world_bible.md` | **R14** | **2,200** |
| `plot_outline.md` | **R14** | **2,747** |
| `manuscript.md` | **R17** | **2,430** |
| `editor_notes.md` | **R17** | **3,540** |
| `reader_report.md` | **R24** | **2,225** |

**revision 一路涨到 24，字数却始终停在 2,000–3,500。**

Agent 把 `artifact.content` 理解成「**这份文件的完整内容**」，
所以每一轮都是在**整体重写**，不是续写。

> ### 结论：**按当前协议，永远到不了 10,000 字。**

这不是 Agent 偷懒 —— 它们每轮都认真重写了整份文档。
**是协议缺一个「追加 / 续写」的语义**：系统只提供 `apply_write`（覆盖），
没有任何机制表达「接着上次往下写」。

这是本轮最有价值的**结构性发现**，也是 `10,000 字目标未达成`的**首要原因**。

---

## 五、§二十 逐条回答（诚实版）

### Production
1. **生产出了什么** — 5 份文档，每份 2,200–3,540 字，全部来自 Agent 自己
2. **最终小说多少字** — `manuscript.md` **2,430 字**（目标 10,000，**未达成**）
3. **世界观是什么** — 已写入 `world_bible.md`（R14，2,200 字）
4. **剧情是什么** — 已写入 `plot_outline.md`（R14，2,747 字）
5. **写作质量** — 本轮**无法评估**（且存在「只改写不累积」的结构问题）
6. **Editor / Reader 是否有效产出** — ✅ 是，两者都每轮写入且互相@

### Collaboration
7. **谁主动找谁** — 全员互相发；`novel_reader` 与 `novel_plot_architect` 最活跃
8. **谁最有用** — **无法判定**
9. **谁是瓶颈** — 本轮无瓶颈（全部 Agent 都产出）
10. **谁产生大量无效成本** — Session 1–4 的 `deepseek-flash` 全是（**基础设施**，非 Agent）
11. **是否重复劳动** — ✅ **是，而且是系统性的**：每轮整体重写（见 §四）
12. **是否自主改变计划** — 观察到 Agent 每轮改变联系对象

### Economy
13. **Task 总成本** — 截至审计时 **8.79449 AIC**（budget 50，OBSERVE_ONLY）
14. **哪个 Agent 最贵** — `novel_plot_architect` 1.53984 AIC
15. **谁主动 Task Delegation** — 本轮 Session **未观察到**
16. **有没有完成过 AIC Settlement** — ❌ **没有**（Ledger 里 `TRANSFER` 仍为 0）
17. **Agent 是否根据余额判断** — ✅ 上一轮已观察到（引用「99.4045 AIC 的余额」）
18. **Budget 是否进入 Context** — ✅ 是，渲染为 `Budget/Spent/Remaining/Mode`
19. **OBSERVE_ONLY 下 Agent 是否关注预算** — 本轮**未观察到**明确提及

### Production Efficiency
20–22. **Production Conversion** — `per_agent` 与 `conservation` 已写入 session JSON；
   ⚠️ 但当前「写了多少字」被**整体重写**污染，该指标本轮**不可比**

### Architecture
23. **Agent 行为问题** — 无明确证据
24. **基础设施问题** — ✅ **三个参数阻塞 + 缺「续写」语义**（全部是本轮发现的）
25. **下一版本才需要解决** — 追加式写入协议；委托的输入原料通道；Settlement 闭环

---

## 六、本轮**没有**做（§二十一 禁止项）

❌ Economy Policy ❌ HARD_LIMIT 强制 ❌ Profit optimization ❌ Reputation pricing
❌ Auction / Reverse Auction ❌ Hiring ❌ Coordinator ❌ Workflow Engine
❌ 固定 Agent 顺序 ❌ 强迫成交 ❌ **强迫输出** ❌ 伪造完成状态

> ⚠️ 关于「改 `max_tokens` / `timeout` / 换模型」是否算「为了实验成功而作弊」：
> **不算。** 区别在于「Agent **选择**不写」与「Agent 写的东西**被截断了**」。
> 三个参数都是基础设施配置，改它们不改变 Agent 的任何判断。
> 而**「整体重写 vs 续写」这个真问题，我没有去碰** —— 那需要设计决定，不是我能顺手修的。

---

## 七、下一步建议（未实现）

1. **10,000 字目标的真正阻塞是「没有续写语义」。**
   最小做法：给 `artifact` 增加一个 `mode: "replace" | "append"`，
   Runtime 只负责按 Agent 指定的方式写 —— **决定权仍在 Agent**。
   这是本轮唯一真正卡住目标的架构缺口。

2. **`LLMResponse` / 实验 JSON 的 token 暴露不完整**（上一轮审计已记录）。
   本轮 `per_agent` 已补齐 calls/tokens/valid/empty/writes，
   但 `CognitiveRunResult` 仍然丢弃 token。

3. **`finish_reason` 应当落库。** 本轮三次阻塞全靠它诊断，
   而它至今**不在 `llm_calls` 里**，只能靠 `output_tokens == max_tokens` 反推。

4. **`timeout_seconds` 与 `max_tokens` 应当有「按模型」的推荐值**，
   而不是所有 provider 都用同一个 60 秒。

5. **本 Session 结束后**，请查看
   `results/formal_production_session_06.json` 的 `per_agent` / `production`
   / `budget` / `resume` 四个字段 —— 那是本轮的完整可观测数据。
