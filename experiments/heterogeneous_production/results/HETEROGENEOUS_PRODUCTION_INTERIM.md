# 第一轮真正异质多 Agent 自主生产实验 —— 进行中报告

**Commit** `e47793a` · **Tests 4066 passed / 0 failed**

---

## ⚠️ 这份报告的边界（必读）

**实验仍在运行中。** 我这一轮的上下文预算已耗尽，无法等到 120 分钟墙钟结束。

| | |
|---|---|
| 状态 | **RUNNING**（后台 job `bash-690`）|
| 已完成 | Session 1 的前 4 轮（4.5 轮）|
| 最终结果落地 | `experiments/heterogeneous_production/results/heterogeneous_production_result.json`（脚本退出时写入）|
| 实时数据 | `experiments/heterogeneous_production/data/hetero.db`（append-only，随时可查）|

**以下所有数字都是「截至第 4 轮的实时快照」，不是最终结果。**
**我没有把它写成已完成。**

---

## 一、§二十八 的三层区分：Designed / Enabled / Emerged

### Designed（系统预先设计的）

- Task `task_hetero_001` + Workspace `WS-HETERO-001`
- 5 个命名空间 `/world/` `/plot/` `/manuscript/` `/notes/` `/reader/`
- **一个根 Stage**（§十七：不预设后续阶段）
- Budget 50 AIC / OBSERVE_ONLY；Cognitive Budget 8192
- 轮询调度（**poll-driven**，§十八 要求标记）

### Enabled（系统给 Agent 的能力）

Agent Directory 事实 · Workspace 事实（含**空命名空间**）· Communication ·
Event Wakeup · CREATE/REPLACE/APPEND · 动态 Artifact 创建 · Economic Context ·
Task Delegation · Checkpoint/Resume

### Emerged（**真实发生**的 —— 只写观察到的）

| 行为 | 证据 |
|---|---|
| **5/5 每轮都产出有效内容** | `valid` 计数 17/17 |
| **4/5 自主创建了自选命名的 Artifact** | 见下表 |
| **APPEND 被自然使用** | `world/world_bible.md` **+1308 → R2**；`notes/editorial_brief.md` **+1901 → R3** |
| **全员互相发消息** | 14 条 |
| **Agent 按 Directory 事实找特定对象** | world_builder ↔ plot_architect 反复互发；editor/reader 都发给 author |

---

## 二、Agent 自主创建了什么（§二十六 2）

**没有任何代码规定这些文件名。全部是 Agent 自己决定的：**

| 路径 | 谁创建 | revision | 字数 |
|---|---|---|---|
| `/world/world_bible.md` | novel_world_builder | **R3** | 2,377 |
| `/plot/three_act_structure.md` | novel_plot_architect | R2 | 2,875 |
| `/notes/editorial_brief.md` | novel_editor | **R3** | 3,278 |
| `/reader/reader_profile.md` | novel_reader | R1 | 1,258 |
| `/reader/reader_expectations.md` | novel_reader | R1 | 1,448 |
| `/reader/reader_preflight_feedback.md` | novel_reader | R1 | 829 |

> **注意 `/plot/three_act_structure.md`** —— plot_architect 自己选了「三幕结构」
> 这个具体形态，而不是任何预设大纲模板。

---

## 三、🔴 两个已经显现的真实问题

### ① `novel_author` 三轮 0 产出 —— 高成本低产出（§二十六 20）

| Agent | calls | tokens | AIC | **Artifact** |
|---|---|---|---|---|
| **novel_author** | 3 | **27,910** | **0.2791** | **0 个** |
| novel_reader | 3 | 15,683 | 0.15683 | 3 个 |
| novel_editor | 3 | 13,954 | 0.13954 | 1 个 |
| novel_plot_architect | 4 | 12,313 | 0.12313 | 1 个 |
| novel_world_builder | 4 | 10,511 | 0.10511 | 1 个 |

**author 是唯一「最贵且零 Workspace 产出」的 Agent。**
它每轮 ~9,000 tokens、产出有效文字，但**从不写进 Workspace**：
它的 JSON 里始终没有 `artifact` 字段。

⚠️ 这**不能**直接判为「Agent 行为失败」—— 需要看它的 `assessment` 才知道
它是有意在等上游、还是输出契约没被遵守。**这正是报告要继续追的。**

### ② `CREATE` 撞车被重复尝试 —— 无效成本

`novel_plot_architect` 连续 3 轮尝试
`create plot/three_act_structure.md` → **3 次 `already_exists`**。

系统的行为是**对的**（§二：CREATE 撞已有文件必须拒绝，绝不悄悄降级成 REPLACE），
但它**没有从拒绝中学会改用 replace/append**，于是每轮浪费一次写入机会。

> 这是「**拒绝信息是否可学习**」的问题 ——
> 拒绝理由已经明确写了「想替换请显式用 write_mode=replace」，
> 但 Agent 看不到**上一轮**的拒绝结果（它不在 Context 里）。

---

## 四、进度与节奏

- 每轮约 **400 秒**（5 个异质 Provider，含 doubao / bailian 较慢）
- 30 分钟 Session 上限 → **每 Session 只能跑约 4 轮**
- 当前累计：**17 次真实调用 / 80,371 tokens / 0.8039 AIC**
- Ledger：`GENESIS_MINT ×5 = 500` + `LLM_COST ×17 = 0.8039`
- **Agent-to-Agent TRANSFER = 0**（尚无 Task Delegation 自然发生 §二十五）

**⚠️ poll-driven 的代价（§十八 要求记录）**：
每轮固定 5 次调用，无论 Agent 是否有新信息。
第 4 轮里 world_builder / plot_architect 的调用**很可能没有新增信息**，
这属于轮询带来的额外 Token 消耗。

---

## 五、退出条件现状

| 条件 | 状态 |
|---|---|
| §二十三 正常完成 | 未达到（正文还没开始） |
| §二十 120 分钟总墙钟 | 进行中（已用约 20 分钟）|
| §二十一 NO_PRODUCTIVE_PROGRESS | 未触发（每 Session 增量 > 400 字）|
| §二十二 INFRASTRUCTURE_FAILURE | 未触发 |

---

## 六、我**没有**做的事

❌ 没有把这次实验写成「已完成」
❌ 没有替 Agent 补写 `manuscript`
❌ 没有强迫 `novel_author` 写文件
❌ 没有为了凑字数延长运行
❌ 没有 `git add -A`

---

## 七、下一步（等实验自行结束）

实验会自己走到 120 分钟或某个退出条件，然后写
`results/heterogeneous_production_result.json`。届时需要补的：

1. **`novel_author` 为什么不写 Workspace？** —— 读它的 `assessment` 原文
2. **正文最终多少字？** —— 当前 6 个 Artifact 合计 ~12,065 字，
   但 `manuscript/` 命名空间**仍然是空的**
3. **是否出现 Task Delegation / Settlement** —— 目前 0
4. **完整的 §二十九 41 项报告**

**我没有开始下一轮实验，也没有自动进入 Economic Pressure。**
