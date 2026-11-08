# Private Long-Term Memory（私有长期记忆）

> **状态：`CURRENT`（2026-11-08，第一版）**
>
> 长期记忆是对短期记忆的**高级抽象 + 权重反馈**。
> 具体短期内容保存在 `agent_short_term_memory_archive`，长期条目通过
> `short_term_entry_ids` 指回它们。

---

## 1. 它是什么

```text
LongTermEntry
  entry_id
  summary            更抽象的总结
  tasks              做过哪些事
  events             哪些事件印象深刻
  short_term_entry_ids 对应的具体短期记忆存档
  weight             记忆权重（0.1..10）
  use_count          被使用次数
  last_used_task_id  最近一次被使用的 Task
```

长期记忆不是“更大的短期记忆”，而是**抽象 + 可追溯**：

- 抽象：summary / tasks / events；
- 可追溯：通过 `long_term_memory_read` 拿回具体短期条目。

---

## 2. 权重反馈

```text
mark_used(entry, task_id)  → weight += 0.5, use_count += 1
decay(entry, task_id)      → 如果本轮没使用，weight -= 0.2
```

任务结束时：

- Agent 通过 `long_term_memory_mark_used` 声明“这条对当前任务有帮助”；
- 其余长期条目自动 `decay_unused`；
- 权重 clamp 到 `0.1..10`。

### 通过长期记忆读取短期存档 = 使用

`long_term_memory_read` 不只是读：

```text
read(agent_id, entry_id, task_id)
  → 返回长期条目 + 对应短期 archive
  → 自动 mark_used(agent_id, [entry_id], task_id)
  → weight += 0.5, use_count += 1
```

因此“通过长期记忆去查询了对应的短期记忆”本身就算一次使用，
不需要 Agent 额外再发 `mark_used`。

### 任务结束可见报告

任务完成时打印/写入结果：

```text
========== 长期记忆使用情况（任务结束）==========
agent_a: used 1 / 6 条
  used:
    [ltm-...] weight=2.1 use_count=3 summary=...
  unused: 5 条（权重已下降；可用 search/read 主动查看）
```

下一次任务的 prompt 里也会带 `上次任务长期记忆使用情况`。

### 读取策略

每个任务只注入 **top-N（默认 3）** 高权重条目：

```text
========== 你的长期记忆（高权重摘要，只有你能读）==========
- [ltm-...] weight=3.0 use_count=4
  summary: ...
  tasks: ...
  events: ...
  short_term_entries: 2 条
（还有 7 条低权重记忆未读取；
 可以用 long_term_memory_search / read 主动查看。）
```

🔒 Agent **知道有低权重条目没有被读取**，这是“遗忘”；
   但可以主动 search/read，不会永久丢失。

---

## 3. 短期记忆的抽象是 Agent 的工作

短期记忆超过一半（>4）时：

1. 系统只在 prompt 里给“清理信号” + 候选 `entry_id`；
2. Agent 调用 `long_term_memory_consolidate`：

```json
{
  "expected_revision": 5,
  "summary": "多个 duration 任务的共同模式",
  "tasks": ["task_tool_...", "..."],
  "events": ["早期缺少契约导致返工"],
  "short_term_entry_ids": ["stm-...", "..."]
}
```

3. 系统把选中的短期条目写入 archive，并从短期记忆删除；
4. 创建长期条目，`short_term_entry_ids` 指回 archive。

🔒 **不调用就不会自动压缩** —— 抽象是 Agent 的判断工作。

---

## 4. 动作

| action | 作用 | 参数 |
|---|---|---|
| `long_term_memory_search` | 检索长期记忆（含低权重） | `query` |
| `long_term_memory_read` | 读长期条目 + 对应短期存档 | `entry_id` |
| `long_term_memory_mark_used` | 声明对当前任务有帮助（权重 +） | `entry_ids` |
| `long_term_memory_consolidate` | 把短期记忆抽象成长期记忆并归档 | `expected_revision` · `summary` · `tasks` · `events` · `short_term_entry_ids` |

`long_term_memory_read` 返回：

```json
{"entry": {...}, "short_term_entries": [{"entry_id": "...", ...}]}
```

---

## 5. 存储

| 表 | 作用 |
|---|---|
| `agent_long_term_memory` | 长期条目（payload JSON，含 weight） |
| `agent_short_term_memory_archive` | 具体短期条目存档，`long_term_entry_id` 指回长期条目 |

---

## 6. 边界

- 私有：只有 Agent 自己能读写；
- 不自动成为 Shared Context；
- 权重是**反馈信号**，不是路由/调度规则；
- 低权重不等于删除；只是默认不注入；
- 本版没有长期记忆的自动摘要质量评估。

---

## 7. 实验证据（2026-11-08）

- Run：`experiments/tool_collab/runs/1108-1336-e5004e54/`
- 复用 DB：`runs/1108-1254-208be94a/tool.db`
- 任务：`slugify` + pytest
- 结果：**COMPLETED**
- 墙钟：**2.2 分钟**
- 成本：**223,508 tokens / 2.23508 AIC**

测试前 seed：

- 每个 Agent 5 条短期记忆（超过一半，触发清理信号）；
- 每个 Agent 5 条长期记忆，权重 `[1.0, 2.5, 0.5, 3.0, 0.8]`。

任务 prompt：

```text
long_term  chars = 519
short_term chars = 1,628 / 1,785
memory total     = 2,187 – 3,839 字符
```

长期记忆权重反馈（任务结束自动 decay）：

```text
agent_a: 1.0→0.8, 2.5→2.3, 0.5→0.3, 3.0→2.8, 0.8→0.6
agent_b: 同样
```

因为本轮没有任何 `long_term_memory_mark_used`，所有条目都下降 0.2。

短期记忆：

- 从 5 条变为 6 条（新增 slugify 任务）；
- `needs_compaction=True` 的清理信号出现在 prompt；
- Agent **没有**调用 `long_term_memory_consolidate`；
- 因此 `agent_short_term_memory_archive` 仍为 0 条。

这验证了：

1. 每任务只注入 top-N 长期记忆（519 字符），低权重未读；
2. 未使用的长期记忆权重会下降；
3. 短期清理是 Agent 的工作，不调用就不会自动压缩。

---

## 8. 未实现

- 长期记忆的自动淘汰（权重极低是否删除）；
- 长期记忆的语义检索（当前是子串匹配 summary/tasks/events）；
- 跨 Agent 共享；
- 短期记忆硬容量 8 的自动兜底（当前由 Agent consolidation 清理）。
