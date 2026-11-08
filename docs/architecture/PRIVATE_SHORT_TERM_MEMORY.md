# Private Short-Term Memory（私有短期记忆）

> **状态：`CURRENT`（2026-11-08，第一版）**
>
> 范围：单个 Agent、跨 Task 的近期工作摘要。
> 长期记忆本次只做 **stub**（清理时接收 digest，不提供检索）。
> 当前任务的工作结构见 [`PRIVATE_WORKING_MEMORY.md`](PRIVATE_WORKING_MEMORY.md)。

---

## 1. 它是什么

Agent 最近做过哪些任务、实现过什么架构/结构、哪些现象值得重视。
它是对 **工作 + 私有工作记忆** 的抽象总结，跨 Task 保存。

```text
Private Working Memory   当前任务的操作结构（任务结束清空当前行）
Private Short-Term       已完成任务的抽象摘要（跨任务）
Long-Term                清理时提升的 digest（本版只存不读）
```

---

## 2. 条目

```text
ShortTermEntry
  entry_id
  task_id
  summary         任务完成 + goal 摘要
  architecture    实现过的 branch / task 结构
  notable         finding / issue / note（重大发现、问题）
  importance      1..5
  created_at / updated_at
```

任务完成时，从私有工作记忆的 final snapshot 自动生成：

- `goal` → summary
- `branch` / `task` → architecture
- `finding` / `issue` / `note` → notable

🔒 不复制大段代码或正文；只保留抽象信息。

---

## 3. 固定容量与压缩

```text
CAPACITY             = 8
COMPACT_THRESHOLD    = 4     （超过一半就清理）
KEEP_AFTER_COMPACT   = 4
```

任务完成追加后，如果条目数 > 4：

1. **不自动压缩**；
2. prompt 出现“清理信号”，列出候选旧条目的 entry_id；
3. 抽象成长期记忆是 **Agent 的工作**：
   调用 `long_term_memory_consolidate`，写更抽象的 summary / events；
4. 系统把选中的短期条目移入 archive，并从当前短期记忆删除；
5. Agent 不调用时，短期记忆保留原样（继续提示）。

🔒 长期记忆与权重见
[`PRIVATE_LONG_TERM_MEMORY.md`](PRIVATE_LONG_TERM_MEMORY.md)。

---

## 4. 任务开始时读取一次

- 每个 `(agent, task)` 第一次 decision 时，从 DB 读一次短期记忆到进程内缓存；
- 同一任务后续轮次复用缓存，不重复读 DB；
- prompt 段：

```text
========== 你的短期记忆（跨任务摘要，只有你能读）==========
revision = 1
- [stm-...] task=... importance=4
  summary: 任务 ... 完成；目标：...
  architecture: branch:...
  notable: issue:...
```

如果 Agent 用 `short_term_memory_update` 修正，缓存失效，下次重新读。

---

## 5. 反馈维护

后续任务发现短期记忆内容不正确时：

```text
short_term_memory_update
  expected_revision · entry_id
  summary? · architecture? · notable? · importance?
  remove?

short_term_memory_reinforce
  expected_revision · entry_id · reason
```

- `update`：内容不正确时修正/删除；
- `reinforce`：这条短期记忆对当前任务帮助很大时：
  `use_count += 1`，`importance += 1`，并记录 `helpful` reason。

🔒 只有自己的短期记忆；不会跨 Agent。
🔒 这不是系统自动判定“正确/错误”，是 Agent 的维护。

---

## 6. 动作

| action | 作用 | 参数 |
|---|---|---|
| `short_term_memory_read` | 读自己的短期记忆 | 无 |
| `short_term_memory_update` | 修正/删除一个条目 | `expected_revision` · `entry_id` · 可选字段 · `remove` |

---

## 7. 实验证据（2026-11-08）

- Run：`experiments/tool_collab/runs/1108-1254-208be94a/`
- 任务：`parse_duration` + pytest
- 结果：**COMPLETED**
- 墙钟：**1.4 分钟**
- 成本：**131,407 tokens / 1.31407 AIC**

任务结束后自动追加：

```text
agent_a: stm-3099b6f9f04d  task=task_tool_208be94a  importance=4
agent_b: stm-cfe584fc5f26  task=task_tool_208be94a  importance=4
```

存储大小：

```text
agent_a: 825 字符
agent_b: 1,034 字符
```

内容节选：

- agent_a：summary=“验收 parse_duration 模块”；notable 包含
  “独立验收测试已落盘”“验收人独立执行 pytest 通过”“src/ 不存在（已解决）”。
- agent_b：summary=“parse_duration 实现”；notable 包含
  “验收契约要点”“最终验收结论”“测试结果 15 passed”。

本次任务开始时短期记忆为空（prompt 显示“还没有短期记忆”）；
任务完成后两条条目才被追加。下一次任务会读到它们。

### 7.1 第二个任务：确认跨任务读取

- Run：`experiments/tool_collab/runs/1108-1301-5822a390/`
- 同一个数据库：`runs/1108-1254-208be94a/tool.db`
- 同一个 Agent：`agent_a` / `agent_b`
- 任务：`duration.py`（w/d/h/m/s、组合、小数、前置负号、
  format_duration、sum_durations；≥25 用例）
- 结果：**COMPLETED**
- 墙钟：**3.3 分钟**
- 成本：**209,618 tokens / 2.09618 AIC**
- 测试：**82 passed**

任务开始时的 `short_term_loaded`：

```text
agent_a: revision=1  entry_ids=['stm-3099b6f9f04d']
agent_b: revision=1  entry_ids=['stm-cfe584fc5f26']
```

prompt 里的短期记忆字符：

```text
agent_a: 580 字符 / turn
agent_b: 743 字符 / turn
```

任务完成后，短期记忆 revision = **2**：

```text
agent_a: 2 条，合计 1,367 字符
agent_b: 2 条，合计 1,578 字符
```

新条目：

```text
agent_a: stm-8c549a207579
agent_b: stm-49fc4ff989b1
```

long-term：0 条（2 条未超过一半，未触发压缩）。

**这确认了跨任务读取链路真的工作：**
第二个任务的每个 turn 都加载了第一个任务的 entry_id，
不是只存在于数据库里。

---

## 8. 已做 / 未做

已做：

- 长期记忆（高权重 top-N / search / read / mark_used / consolidate）；
- 短期条目的具体存档（`agent_short_term_memory_archive`）；
- `short_term_memory_reinforce` 反馈。

未实现：

- 短期记忆的自动重要性评估（当前 importance 由 notable 有无决定 3/4）；
- 跨 Agent 的短期记忆共享（不做；短期记忆是私有的）；
- 任务开始时“只读一次”的跨进程持久缓存（当前是进程内缓存）；
- 超过硬容量 8 时的自动兜底（当前由 Agent consolidation 清理）。
