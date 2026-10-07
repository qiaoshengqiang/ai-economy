# 继续 AIECO 第一轮多 Agent 生产实验 —— 报告

**Commit** `6322092` · **Tests** 3871 passed / 2 skipped / 30 deselected / **0 failed**

基线 `3d93d33` → 本轮 `6322092`。

---

## 0. 先说结论：哪些是「接线」，哪些是「真的发生了」

任务书 §十七 明确要求把这两件事分开。本报告全程遵守。

### 【基础设施已经接线】（代码层面的能力）

| | 状态 |
|---|---|
| Communication 接进生产运行路径 | ✅ 新增 `channels.py` |
| 共享生产资料真正写进 Task Workspace | ✅ 新增 `shared_workspace.py` |
| Checkpoint 引用**真实** Workspace revision | ✅ 伪标签已删除 |
| Resume 读回真实 revision | ✅ 已实测 |
| 5 个 Agent 是同一个 Task 的 Participant | ✅ 原先**不是**，所以根本写不进 |

### 【实验中自然发生】（真实 Runtime 里的自主行为）

| | 状态 |
|---|---|
| Agent→Agent 真实消息 | ✅ **7 条**，全部由 Agent 自己发起 |
| 接收方在 Cognitive Context 里看到消息 | ✅ 4 次 |
| Event Wakeup 真的触发 | ✅ 4 次 |
| 共享生产资料真的落进 Workspace | ✅ **19 次 revision 变化** |
| Agent 采纳别人的意见 | ✅ 作者明确采纳了读者的 5 条 |
| Agent **拒绝**意见 / 主动不沟通 | ✅ 有（见 §15/§16） |
| Agent 主动协作 | ✅ reader 连续 4 轮主动找 world_builder |

**🔴 但必须同时报告：这条链上有一个阻塞性 Bug，
它让 5 个 Agent 中的 3 个实际上「交了白卷却照常计费」** —— 见 §20。

---

## 1. Commit
`6322092`（基线 `3d93d33`）

## 2. Tests
**3871 passed / 2 skipped / 30 deselected / 0 failed**

新增 `tests/test_collaborative_production.py`（**31 项**），覆盖 §十四 全部 10 项要求，
外加 3 条架构守卫 + 1 条模板守卫。**每条守卫都配了反向测试**，
并且**真的注入过违规代码验证它会变红**。

## 3. Task
`task_novel_001` · participants = 5 个 novel Agent（本轮补齐）

## 4. Workspace
`WS-NOVEL-001`，5 份共享生产资料，权责已 handoff：

| 文件 | 负责人 | 最终 revision |
|---|---|---|
| `world_bible.md` | novel_world_builder | **R1（从未更新）** |
| `plot_outline.md` | novel_plot_architect | **R1（从未更新）** |
| `manuscript.md` | novel_author | **R4** |
| `editor_notes.md` | novel_editor | **R1（从未更新）** |
| `reader_report.md` | novel_reader | **R6** |

## 5. 5 个 Agent
全部为同一个 Task 的 Participant，各有私有文件责任，读对所有人开放。

## 6. Message 数量
**7 条**（Session 1：5 条；Session 2：2 条）。

## 7. Wakeup 数量
**4 次**（Session 1）。Session 2 无人收到消息（消息是发给 world_builder 的，
它在 Session 2 只跑了 1 轮，那一轮轮到时消息已在其信箱中但未及取出即收工）。

## 8. 实际 sender / receiver

| sender | receiver | 条数 |
|---|---|---|
| novel_author | novel_world_builder | 2 |
| novel_reader | novel_world_builder | 4 |
| novel_reader | novel_author | 1 |

**没有任何一条是代码替它们发的** —— `collab_session.py` 里只有**一处**
`send()` 调用点，其内容 100% 来自 Agent 自己的 JSON 输出。有守卫钉住这一点。

## 9. Message 是否影响后续行为

**这是本轮最值得说清楚的一条。**

读者在 r1 发消息给作者（`msg_d80eec3a`），内容 5 条阅读意见。
作者在 r2 收到（wakeup ✅）。但作者最终**采纳时引用的是 Workspace**：

> 「采纳 novel_reader 在 **reader_report revision=5** 第二节提出的五条『前 200 字读者契约』」

**结论（诚实版）**：

- ✅ 消息**被读到、被消费**（`was_processed=True`，4 次）
- ✅ 行为**确实改变了**（`manuscript.md` R2→R3→R4，5 条逐条落实）
- ⚠️ 但**改变的直接依据是共享 Workspace（reader_report.md），不是 Message**

这**不是缺陷**，恰恰是 §五 设计意图的实测确认：

> Message 是**沟通**，Workspace 是**共享事实**。
> 真正的协作发生在 Workspace 上；Message 起的是「推一把」的作用。

**强证据不足的地方我如实说**：`references_inbox` 在全部 20 轮中**都是 False** ——
即没有任何一个 Agent 在输出里写出 senders 的 agent_id 或 message_id。
所以「因为看到这条消息才改变了行为」**无法被单独证明**，
只能说「消息到了，行为也变了，但归因指向 Workspace」。

## 10. Workspace 文件
见 §4。**5 份全部真实存在**（不是内存对象，是 `workspace_resources` 的持久行）。

## 11. Workspace Revision 变化

```
Session 1 起  ws:WS-NOVEL-001@391b7dc2dd741e2c
Session 1 止  ws:WS-NOVEL-001@a0042cc03245cd0f   ← manuscript 2→3, reader_report 1→5
Session 2 止  ws:WS-NOVEL-001@1ed571b0481fcc96   ← manuscript 3→4, reader_report 5→6
```

全库 `workspace_revisions` 共 **19 条**。
**真实 revision 从 1 走到 6（reader_report）—— 这是内容驱动的，不是实验器编的。**

## 12. Checkpoint

```
ckpt_b52925771611  revision=ws:WS-NOVEL-001@391b7dc2dd741e2c
ckpt_e8374d7e7cec  revision=ws:WS-NOVEL-001@a0042cc03245cd0f
ckpt_41f5b9828a0f  revision=ws:WS-NOVEL-001@1ed571b0481fcc96
```

**🔴 §六 要求删除的伪标签已彻底删除。** 原先写的是 `f"rev-session-{index}"`，
现在 revision = `ws:<workspace>@sha256(每个文件的真实 revision)`，
由 `shared_workspace.fingerprint()` 算出。**本文件不编任何数字。**
`session.py` 里那一处同步修正（全仓库 `grep rev-session` = 0 命中）。

## 13. Resume

```
↩︎ Resume: Stage=stage_novel_1 status=ACTIVE
   上一个 Checkpoint ckpt_e8374d7e7cec revision=ws:WS-NOVEL-001@a0042cc03245cd0f
   本进程读到的 Workspace revision=ws:WS-NOVEL-001@a0042cc03245cd0f
   ✅ 一致：Resume 读的是持久事实
```

**这是新进程、新 Runtime、新 Gateway** —— 读的是 SQLite 里的持久事实。
§十六 J 满足。

⚠️ 诚实补充：`StageStore.resume()` 这个方法**本轮没有被调用**，
因为 Stage 始终是 `ACTIVE`（Hard Limit 中止**不该**把 Stage 弄成失败态）。
所以这里验证的是「跨进程读回真实 revision」，不是「从 STOPPED 恢复」。
后者在上一阶段（`3d93d33`）验证过。

## 14. AIC / Tokens

| Session | wall | Tokens | AIC |
|---|---|---|---|
| collab_01（4 轮 / 20 turns） | 437.3s | 117,584 | **1.17584** |
| collab_02（1 轮 / 5 turns） | 107.9s | 30,637 | **0.30637** |
| 合计 | 545.2s | **148,221** | **1.48221** |

（累计到全库 269,517 token / 2.69517 AIC，含此前阶段。）
`simulated=False` —— **全部是真实 API 调用。**

## 15. Agent 是否主动协作

✅ **是，而且很明确。**

`novel_reader` 在**没有任何代码要求它**的情况下，
连续 4 轮主动给 `novel_world_builder` 发消息，而且每轮内容**都不一样** ——
它自己在升级表达方式（从「逐行感想」→「读者验收测试」→「门槛清单」）。

更关键的是它**主动选择了不联系某个人**，并且**说出了理由**：

> 「**不联系作者**：他的样张已声明等 bible 落地再重写，现在催他是浪费」

这是 §九 允许的行为：「Agent 不通信」也是真实实验结果。

## 16. Agent 是否拒绝建议

✅ **有，而且是双向的。**

- 读者**拒绝**了作者的写法：「我真正想快进的地方——它像作者替我划重点。**删掉**」
- 读者对空文件施加了自己的压力：「如果这两个文件还是空的，我的篇幅会**减到五行以内**」
- 作者也**部分拒绝**：「你可以完全不参考它」「bible 优先，我改稿不心疼」

**没有出现「因为收到建议就照做」的自动服从。**

## 17. Agent 是否发生重复劳动

⚠️ **有，但有限。**

- `novel_reader` 每轮都重写 `reader_report.md`（R1→R6），
  但它**自己识别并声明**了这一点：「我不再复述旧意见，只做两件新事」
- `novel_author` 只在 R1、R4 动手，没有空转
- `novel_plot_architect` **4 轮全部空产出**（见 §20）—— 这是浪费

## 18. 是否存在无价值通信

⚠️ **存在。**

`novel_reader → novel_world_builder` 发了 **4 条**，
其中 r2/r3/r4 三条**核心诉求高度重复**（时间锚 / 资产后果样本 / 称谓惯例）。
而 world_builder **一次都没有回复，也没有更新 world_bible.md**。

读者在消息里说「不必回复我，把条目写进 world_bible 我直接读」——
所以「不回复」本身符合它的请求。但**三个近乎相同的诉求重复投递**，
在接收方持续无产出时，属于**边际价值很低的通信**。

**这正是「没有中央协调者」的真实代价**，也是真实实验结果，不是缺陷。

## 19. 是否发现新的协作模式

✅ **发现了三条，都不是设计出来的：**

1. **「消息 + Workspace 双轨，且 Workspace 胜出」**
   读者既发消息又写 reader_report，作者采纳时**引用的是 report 的 revision**。
   → §五「Message 不承担事实存储」被真实数据验证了。

2. **「读者充当上游的施压者」**
   reader 的角色定义里没有任何「监督」职责，但它自发地给上游设了**后果**
   （「再空我就只写五行」）。这是**角色之外涌现的行为**。

3. **「作者用『临时假设 + 显式可否决』向前推进」**
   上游不产出时，作者没有空等，而是自己假设、写出来、并声明
   「我临时假设了什么，让 world_builder 能低成本地否决或采纳」。
   → **阻塞时的自主降级**，没有任何代码规定过。

## 20. 🔴 阻塞性问题

### 20.1 【最关键】5 个 Agent 里 3 个「交了白卷却照常计费」

Session 1 的 20 轮里，有 **10 轮 `chars=0`**（正好一半），
每轮都消耗了约 4800-6700 output token。参与者：`novel_world_builder` 4/4 轮全空，
`novel_plot_architect` 3/4 轮空，`novel_author` 2/4 轮空，`novel_editor` 1/4 轮空。

最严重的：**`novel_world_builder` 连续 4 轮全部 `chars=0`，烧掉约 25k token，
一个字都没产出。**

**根因（已实测确认）**：

```
deepseek-flash 是推理模型。返回体 message 同时有 content 与 reasoning_content。
当输出预算被推理吃掉时 → content 是空字符串，而 completion_tokens 依然很大。
```

实测原始返回体：

```
finish: stop   keys: ['role', 'content', 'reasoning_content']
usage: {'completion_tokens': 29, 'completion_tokens_details': {'reasoning_tokens': 19}}
```

**为什么这是阻塞性的**：
我第一遍读日志时，把它读成了「这个 Agent 选择不动手」—— **那是错的**。
如果不发现这一点，本报告会写出一个**假的结论**
（「world_builder 判断力有问题 / 消极怠工」），而真相是**它的输出被丢了**。

这正是本项目反复栽的那一类 Bug：**接口成功掩盖语义 no-op**。

**已做的处置（最小、不越界）**：
新增 `collab_session.is_empty_completion(content, output_tokens)`，
把「空 content + 花了 output token」记为**失败**，而不是「主动沉默」。
配了 4 项测试（含反向测试）。

**未做（留给你们决定）**：
真正的位置在 `llm/openai_compatible.py` —— 它应当把 `reasoning_content`
与 `finish_reason` 显式暴露给上层，或者在 `content` 为空而
`completion_tokens > 0` 时发出**可见的告警**。
我没有改它，因为那会动 LLM 层的既有语义（注释明确写着「content 是空字符串是合法的」），
**属于需要你们拍板的改动，不是我该顺手做的**。

### 20.2 上游瓶颈无人能解

`world_bible.md` / `plot_outline.md` / `editor_notes.md` **最终仍是 R1 空文件**。
下游三个 Agent 反复指出这一点（作者、编辑、读者全都点名了），
但**没有任何机制能让责任方动起来** —— 因为：
- 没有 Coordinator（**这是对的，不许加**）
- Message 只是提示，不构成义务
- Hard Limit 到点就停

**这不是缺陷，是 V1 的真实边界。** 但它意味着：
**在只有 Hard Limit、没有经济激励的配置下，一个不产出的 Agent 不会被任何人纠正。**

## 21. 非阻塞性问题

1. **`renders_stage` 从不传 checkpoint** —— `stage/context.py` 支持渲染 checkpoint，
   但 `session.py` / `collab_session.py` 都没传，`Stage.current_checkpoint` 只是 id，
   没有代码把它 load 成 `Checkpoint` 再传进去。**Agent 因此看不到自己在哪个 revision 上。**
2. **Prompt 模板占位符曾经和调用点对不上** —— `{ws}` 没被 `.format()` 填上，
   第一轮第一次调用就 `KeyError` 崩掉整场 Session。**已修，并已加模板守卫 + 反向测试。**
3. **🔴 上一轮（`3d93d33`）的「0 failed」是错的。**
   `agents/brain.py` 在 `3d93d33` 里第 227 行写的是 `if provider == "mock":`，
   而 `test_no_provider_branching_anywhere_outside_providers` **明确禁止**这件事。

   验证：`git show 3d93d33:agents/brain.py | grep -n 'provider == "mock"'` → 命中。

   **即上一轮提交时那条守卫就是红的，但报告写的是「3840 passed / 0 failed」。**
   这正是本项目已经记录过的教训 —— **测试结果必须能「拦住提交」，而不只是被打印出来**。
   本轮修法是让 Provider 自己声明（新增 `requires_api_key`，fail-closed），
   **不是改守卫**。
4. **`_address_hint` 每次 Gateway 调用都做一次 `list_tasks`** （`main.py` 已自述的技术债）。
5. **Task Workspace 没有物理落盘** —— 内容在 SQLite。
   §七 说的「真正发生」指的是**进了 Workspace 这个共享事实源**，
   **不是**在磁盘上出现了 `.md` 文件。物化属于未实现的 Patch 阶段。

---

## 附：本轮**没有**做的事（§ 明确禁止）

- ❌ 没有进入 10,000 字正式生产
- ❌ 没有引入 Economy Policy
- ❌ 没有引入中央 Coordinator / CollaborationEngine / Orchestrator
- ❌ 没有改 Task 12-state lifecycle / Stage 核心 / Self Model / Experience / Ledger / Lock 模型
- ❌ 没有新增 MessageType（协作消息一律复用 `TEXT`）
- ❌ 没有实现 Durable Inbox
- ❌ 没有用代码规定任何协作顺序

**本轮结束时停止。**
